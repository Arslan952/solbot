"""Tests for the Morning Brief: downside scanner, brief content, scheduling, pinning, scorecard."""
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot  # noqa: E402
from test_features import Sink, _fake_tickers, _ctx  # noqa: E402
from test_scanner import candles, Photos  # noqa: E402

_ORIG = {k: v for k, v in vars(bot).items()
         if callable(v) and not k.startswith("__") and getattr(v, "__module__", None) == bot.__name__}
_ORIG_SLEEP = time.sleep


def _reset():
    for k, v in _ORIG.items():
        setattr(bot, k, v)
    time.sleep = _ORIG_SLEEP
    bot.ANTHROPIC_API_KEY = None
    bot.CANDLES.clear()
    bot.CTX.clear()
    bot.KLINE_CACHE.clear()
    bot.SCAN_CACHE.update(ts=0, picks=[], finalists=[], danger=[])
    bot.SCAN_CACHE.pop("down", None)


def _up(coin, total=70, price=100.0):
    return {"coin": coin, "total": total, "tech": total - 10, "price": price, "chg_24h": 4.0, "blocked": False,
            "reasons": ["1h volatility squeeze just released upward", "volume rising (1.8x normal)"],
            "news": ["headline catalyst: partnership / integration"], "headlines": [f"{coin} announces partnership"],
            "flags": ["funding rising"], "trigger": price * 1.01, "invalid": price * 0.97, "targets": [price * 1.05, price * 1.09]}


def _down(coin, total=65, price=50.0):
    return {"coin": coin, "total": total, "tech": total - 10, "price": price, "chg_24h": -3.0,
            "reasons": ["closed below its 20-bar range ($50.00) on strong volume", "price below falling 21/50 EMAs"],
            "news": ["negative headline: token unlock"], "headlines": [f"{coin} token unlock next week"],
            "flags": ["bullish RSI divergence"], "trigger": price * 0.99, "invalid": price * 1.03,
            "targets": [price * 0.95, price * 0.9]}


# ---------- downside scoring ----------
def test_score_breakdown_detects_weakness():
    weak = candles(160, seed=2, sigma=0.4, tail=[(0.1, 30, -0.02), (0.35, 6, -0.8)])
    sc = bot.score_breakdown(bot.add_indicators(weak), None, {"change_24h": -6.0}, 1.0)
    assert sc and 0 <= sc["tech"] <= 100 and sc["trigger"] < sc["invalid"]
    assert all(t < sc["price"] for t in sc["targets"])
    assert any("weaker than BTC" in r for r in sc["reasons"])
    # oversold & already-dropped coins are penalised (bounce risk)
    crash = candles(150, seed=2, sigma=0.4, tail=[(0.05, 10, 0.0), (0.2, 6, -3.5)])
    sc2 = bot.score_breakdown(bot.add_indicators(crash), None, {"change_24h": -22.0}, 1.0)
    assert any("already down" in f or "oversold" in f for f in sc2["flags"])
    assert bot.score_breakdown(weak.head(50)) is None
    # a strong uptrend must score low as a breakdown
    up = candles(160, seed=2, sigma=0.4, tail=[(0.1, 30, 0.02), (0.35, 6, 0.8)])
    sc3 = bot.score_breakdown(bot.add_indicators(up), None, {"change_24h": 6.0}, 1.0)
    assert sc3["tech"] < sc["tech"]


def test_enrich_downside_positioning_and_news():
    bot.coin_derivs = lambda b: {"funding": 0.0009, "oi_change_24h": 8.0, "oi_change_6h": 6.0}
    bot.coin_news = lambda b: [{"title": "Foo token unlock and exploit drained funds", "source": "X"}]
    c = {"coin": "FOO", "price": 1.0, "chg_6h": -3.0, "flags": [], "tech": 50}
    bot.enrich_downside(c)
    assert c["deriv_pts"] == 20 and c["news_pts"] > 20
    assert any("crowded longs" in n for n in c["news"]) and any("shorts piling" in n for n in c["news"])
    bot.coin_derivs = lambda b: {"funding": -0.0009, "oi_change_24h": None, "oi_change_6h": None}
    bot.coin_news = lambda b: [{"title": "Foo announces partnership and launch", "source": "X"}]
    c2 = {"coin": "FOO", "price": 1.0, "chg_6h": -3.0, "flags": [], "tech": 50}
    bot.enrich_downside(c2)
    assert c2["deriv_pts"] == -8 and any("bounce" in f for f in c2["flags"])


def test_run_downside_scan_ranks_and_caches_klines():
    _fake_tickers()
    bot.TICKERS["data"]["SOL"]["change_24h"] = -4.0
    bot.TICKERS["data"]["DOGE"]["change_24h"] = -2.0
    calls = []

    def fake_klines(interval="1h", limit=200, symbol="BTCUSDT"):
        calls.append((symbol, interval))
        return candles(max(limit, 260), seed=3)
    bot.get_klines = fake_klines
    bot.time.sleep = lambda s: None
    bot.score_breakdown = lambda d, d4=None, tk=None, btc=None: {
        "tech": 70 if tk and tk.get("price") == 180 else 10, "reasons": ["price below falling 21/50 EMAs"], "flags": [],
        "price": 180.0, "trigger": 175.0, "invalid": 186.0, "targets": [170.0, 165.0], "an": {}}
    bot.coin_derivs = lambda b: {"funding": 0.0006, "oi_change_24h": None, "oi_change_6h": None}
    bot.coin_news = lambda b: []
    out = bot.run_downside_scan(bot.default_state())
    assert [c["coin"] for c in out] == ["SOL"] and out[0]["total"] >= 55
    n = len(calls)
    bot.run_downside_scan(bot.default_state())                 # second run reuses cached klines
    assert len(calls) == n


def test_oversold_coin_is_labelled_bounce_risk_not_high_risk():
    d = _down("SOL", 70)
    d["flags"] = ["RSI 9 - oversold, a bounce is likely"]
    txt = "\n".join(bot._down_block(1, d, True))
    assert "oversold, bounce risk" in txt and "high risk" not in txt
    assert "high risk" in "\n".join(bot._down_block(1, _down("DOGE", 70), True))
    crash = candles(150, seed=2, sigma=0.4, tail=[(0.05, 10, 0.0), (0.2, 6, -3.5)])
    a = bot.score_breakdown(bot.add_indicators(crash), None, {"change_24h": -22.0}, 1.0)
    assert a["tech"] < 40                                   # deep oversold crash is not a clean dump setup


def test_breakout_invalid_level_always_beyond_trigger():
    for seed, tail in ((2, [(0.1, 30, 0.02), (0.35, 6, 0.8)]), (11, [(0.05, 20, 0.0), (0.3, 8, 0.7)]), (5, None)):
        df = bot.add_indicators(candles(160, seed=seed, sigma=0.5, tail=tail))
        sc = bot.score_breakout(df, None, {"change_24h": 5.0}, 1.0)
        assert sc["invalid"] < sc["trigger"], (seed, sc["invalid"], sc["trigger"])
        sd = bot.score_breakdown(df, None, {"change_24h": -5.0}, 1.0)
        assert sd["invalid"] > sd["trigger"], (seed, sd["invalid"], sd["trigger"])


# ---------- brief content ----------
def test_build_morning_brief_content_and_limits():
    _ctx()
    st = bot.default_state()
    ups = [_up(c, 80 - i * 4) for i, c in enumerate(["SOL", "AVAX", "LINK", "ADA", "XRP"])]
    downs = [_down(c, 70 - i * 5) for i, c in enumerate(["DOGE", "PEPE", "SUI", "APT", "ARB"])]
    danger = [{"coin": "WIF", "chg": 31.0, "rsi": 82, "reasons": ["RSI 82 (extremely overheated)", "up 31% in 24h"]}]
    text = bot.build_morning_brief(st, ups, downs, danger)
    for part in ("MORNING BRIEF", "POTENTIAL TO GO UP", "AT RISK OF DUMPING", "Overheated after a big pump",
                 "SOL", "DOGE", "WIF", "Confirm", "wrong if", "not predictions"):
        assert part in text, part
    assert len(text) <= 3900 and "&" not in text.replace("&amp;", "").replace("&lt;", "").replace("&gt;", "")
    empty = bot.build_morning_brief(st, [], [], [])
    assert "Nothing clean today" in empty and "No coin shows a clear breakdown" in empty
    kb = bot.brief_buttons(ups, downs)["inline_keyboard"]
    assert kb[0][0]["callback_data"] == "an:SOL:1h" and kb[1][0]["callback_data"] == "an:DOGE:1h"


def _brief_env(s):
    _ctx()
    _fake_tickers()
    bot.ANTHROPIC_API_KEY = None
    bot.SCAN_CACHE.update(ts=time.time(), finalists=[_up("SOL", 78), _up("AVAX", 62), _up("LINK", 45), _up("BAD", 30)],
                          danger=[{"coin": "WIF", "chg": 31.0, "rsi": 82, "reasons": ["RSI 82"]}])
    bot.run_downside_scan = lambda state: [_down("DOGE", 66), _down("PEPE", 40), _down("NOPE", 20)]


def test_send_morning_brief_posts_pins_logs_and_unpins_old():
    s = Sink()
    _brief_env(s)
    st = bot.default_state()
    assert bot.send_morning_brief(st) is True
    assert "MORNING BRIEF" in s.sent[-1]["text"] and s.pins == [st["morning"]["msg"]]
    ent = st["morning_log"][0]
    assert [x["coin"] for x in ent["up"]] == ["SOL", "AVAX", "LINK"]       # 'BAD' (30) filtered out
    assert [x["coin"] for x in ent["down"]] == ["DOGE", "PEPE"]           # 'NOPE' (20) filtered out
    first = st["morning"]["msg"]
    bot.send_morning_brief(st)
    assert first in s.unpins and st["morning"]["msg"] != first
    n_pins = len(s.pins)
    bot.send_morning_brief(st, manual=True)                                # manual: no pin, no log
    assert len(s.pins) == n_pins and len(st["morning_log"]) == 2


def test_morning_schedule_once_per_day_at_local_time():
    s = Sink()
    _brief_env(s)
    st = bot.default_state()
    st["tz"] = "Asia/Karachi"
    bot.apply_user_tz(st)
    real = bot.now_utc
    try:
        bot.now_utc = lambda: datetime(2026, 10, 9, 2, 0, tzinfo=timezone.utc)      # 07:00 PKT - too early
        bot.check_morning_brief(st)
        assert not s.sent
        bot.now_utc = lambda: datetime(2026, 10, 9, 2, 45, tzinfo=timezone.utc)     # 07:45 PKT
        bot.check_morning_brief(st)
        assert "MORNING BRIEF" in s.texts and st["morning"]["last"] == "2026-10-09"
        n = len(s.sent)
        bot.check_morning_brief(st)
        assert len(s.sent) == n                                                      # once a day
        bot.now_utc = lambda: datetime(2026, 10, 10, 3, 0, tzinfo=timezone.utc)     # next morning
        bot.check_morning_brief(st)
        assert len(s.sent) > n
        st["morning"]["on"] = False
        st["morning"]["last"] = ""
        n = len(s.sent)
        bot.now_utc = lambda: datetime(2026, 10, 11, 3, 0, tzinfo=timezone.utc)
        bot.check_morning_brief(st)
        assert len(s.sent) == n                                                      # switched off
    finally:
        bot.now_utc = real
        bot.apply_user_tz(bot.default_state())


def test_morning_retry_limit_when_brief_fails():
    s = Sink()
    _brief_env(s)
    st = bot.default_state()
    bot.send_morning_brief = lambda state, manual=False: False
    real = bot.now_utc
    bot.now_utc = lambda: datetime(2026, 10, 9, 9, 0, tzinfo=timezone.utc)
    try:
        for _ in range(6):
            bot.check_morning_brief(st)
        assert st["morning"]["tries"] == 3 and st["morning"]["last"] == ""
    finally:
        bot.now_utc = real


# ---------- 24h scorecard ----------
def test_morning_outcomes_and_scorecard():
    _fake_tickers()
    st = bot.default_state()
    now = time.time()
    st["morning_log"] = [{"date": "d", "ts": now - 24 * 3600, "btc_price": 66000.0,
                          "up": [{"coin": "SOL", "price": 170.0}, {"coin": "AVAX", "price": 41.0}],
                          "down": [{"coin": "DOGE", "price": 0.22}, {"coin": "PEPE", "price": 0.00001}]},
                         {"date": "d2", "ts": now - 3600, "btc_price": 67000.0, "up": [], "down": []}]
    bot.TICKERS["data"]["BTC"]["price"] = 67320.0          # +2.0%
    bot.update_morning_outcomes(st)
    r = st["morning_log"][0]["res"]
    assert abs(r["btc"] - 2.0) < 0.01 and r["n_up"] == 2 and r["n_down"] == 2
    assert r["up_beat"] == 0.5 and r["down_fell"] == 0.5 and abs(r["up_avg"] - ((180 / 170 - 1) * 100 + (40 / 41 - 1) * 100) / 2) < 1e-6
    assert "res" not in st["morning_log"][1]               # too recent to judge
    assert any("Yesterday" in x for x in bot.yesterday_text(st))
    txt = bot.scan_stats_text(st)
    assert "Morning lists (1 days" in txt and "'Dump risk' list" in txt
    _ctx()
    brief = bot.build_morning_brief(st, [], [], [])
    assert "Yesterday's lists" in brief


# ---------- commands / routing ----------
def test_morning_command_and_routing():
    s = Sink()
    _brief_env(s)
    st = bot.default_state()
    bot.handle_command(st, "/morning", ["06:15"])
    assert st["morning"]["time"] == "06:15" and st["morning"]["on"]
    bot.handle_command(st, "/morning", ["25:99"])
    assert st["morning"]["time"] == "06:15"
    bot.handle_command(st, "/morning", ["off"])
    assert not st["morning"]["on"]
    bot.handle_command(st, "/morning", ["on"])
    assert st["morning"]["on"]
    calls = []
    bot.send_morning_brief = lambda state, manual=False: calls.append(("brief", manual)) or True
    bot.cmd_dump = lambda state: calls.append(("dump",))
    bot.cmd_scan = lambda state: calls.append(("scan",))
    for text, expect in [("give me the morning brief", ("brief", True)),
                         ("which coins will pump or dump today?", ("brief", True)),
                         ("which coins are going to dump?", ("dump",)),
                         ("any coin about to crash", ("dump",)),
                         ("which coin is going to pump", ("scan",))]:
        calls.clear()
        bot.handle_text(st, text)
        assert calls and calls[-1] == expect, (text, calls)
    calls.clear()
    bot.handle_command(st, "/brief", [])
    bot.handle_command(st, "/dump", [])
    assert calls == [("brief", True), ("dump",)]
    assert len(bot.HELP_TEXT) < 4096 and "MORNING BRIEF" in bot.HELP_TEXT
    assert {"brief", "dump", "morning"} <= {c for c, _ in bot.BOT_COMMANDS}


def test_cmd_dump_output_and_scoreboard_callback():
    s = Sink()
    _brief_env(s)
    bot.cmd_dump(bot.default_state())
    last = s.sent[-1]
    assert "COINS AT RISK OF DUMPING" in last["text"] and "DOGE" in last["text"] and "WIF" in last["text"]
    assert "NOPE" not in last["text"] and last["markup"]["inline_keyboard"][0][0]["callback_data"] == "an:DOGE:1h"
    bot.tg_post = lambda *a, **k: {"ok": True}
    bot.CHAT_ID = "1"
    st = bot.default_state()
    bot.handle_update(st, {"callback_query": {"id": "1", "data": "scanstats", "message": {"chat": {"id": 1}, "message_id": 3}}})
    assert "BREAKOUT SCOREBOARD" in s.sent[-1]["text"]


def _wrap(fn):
    def inner():
        _reset()
        return fn()
    inner.__name__ = fn.__name__
    return inner


for _n, _f in list(globals().items()):
    if _n.startswith("test_") and callable(_f):
        globals()[_n] = _wrap(_f)
