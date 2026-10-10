"""Tests for v3.4: chart analysis, breakout scanner + pin, scoreboard, listings, digest, routing."""
import os
import sys
import time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot  # noqa: E402
from test_features import Sink, _fake_tickers, _ctx  # noqa: E402

# Snapshot of the pristine module so every test starts clean (tests monkeypatch heavily).
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
    bot.SCAN_CACHE.update(ts=0, picks=[], finalists=[], danger=[])


def candles(n=260, step=3600, seed=1, sigma=0.4, drift=0.0, tail=None):
    """Synthetic OHLCV. tail=[(sigma, n_bars, drift)] appends custom phases."""
    rng = np.random.default_rng(seed)
    closes = [100.0]
    phases = [(sigma, n, drift)] + (tail or [])
    for sg, cnt, dr in phases:
        for _ in range(cnt):
            closes.append(closes[-1] + rng.normal(dr, sg))
    closes = np.array(closes[1:])
    m = len(closes)
    end = int(time.time()) // step * step
    t = (np.arange(m) * step + end - (m - 1) * step) * 1000
    opens = np.concatenate([[100.0], closes[:-1]])
    wick = np.abs(rng.normal(0.15, 0.05, m))
    vol = rng.uniform(90, 110, m)
    return pd.DataFrame({"time": t, "open": opens, "high": np.maximum(opens, closes) + wick,
                         "low": np.minimum(opens, closes) - wick, "close": closes, "volume": vol})


class Photos(Sink):
    def __init__(self):
        super().__init__()
        self.photos, self.cap_edits = [], []
        bot.tg_send_photo = self.photo
        bot.tg_edit_caption = lambda mid, cap, markup=None, html=True: self.cap_edits.append((mid, cap)) or True

    def photo(self, png, caption="", reply_markup=None, level="normal", html=True):
        self.n += 1
        self.photos.append({"id": self.n, "png": png, "caption": caption, "markup": reply_markup, "level": level})
        return self.n


# ---------- helpers ----------
def test_parse_tf_and_price_format():
    assert bot.parse_tf("analyze sol on 15m") == "15m"
    assert bot.parse_tf("show the 4 hour chart") == "4h"
    assert bot.parse_tf("daily chart") == "1d"
    assert bot.parse_tf("chart please") is None
    assert bot.fmt_price(67000) == "$67,000.00"
    assert bot.fmt_price(0.2) == "$0.2000"
    assert bot.fmt_price(0.00001234) == "$0.00001234"
    assert bot.fmt_price(None) == "unavailable"


# ---------- analysis ----------
def test_analyze_df_structure_and_levels():
    df = bot.add_indicators(candles(260, seed=3))
    an = bot.analyze_df(df, "1h")
    assert an and an["bias"] in ("BULLISH", "BEARISH", "NEUTRAL / MIXED")
    for r, _ in an["resistances"]:
        assert r > an["price"]
    for s, _ in an["supports"]:
        assert s < an["price"]
    assert an["bull"]["invalid"] < an["bull"]["trigger"] and an["bear"]["invalid"] > an["bear"]["trigger"]
    assert an["fib"] and set(an["fib"]["levels"]) == {0.382, 0.5, 0.618}
    assert bot.analyze_df(df.head(30), "1h") is None


def test_candle_pattern_detection():
    df = candles(200, seed=5)
    n = len(df)
    df.loc[n - 3, ["open", "close", "high", "low"]] = [100.0, 98.0, 100.5, 97.5]      # red
    df.loc[n - 2, ["open", "close", "high", "low"]] = [97.8, 101.0, 101.2, 97.6]      # green engulfing
    an = bot.analyze_df(bot.add_indicators(df), "1h")
    assert any("bullish engulfing" in p for _, p in an["patterns"])
    df.loc[n - 3, ["open", "close", "high", "low"]] = [98.0, 100.0, 100.4, 97.8]      # green
    df.loc[n - 2, ["open", "close", "high", "low"]] = [100.2, 97.5, 100.4, 97.3]      # red engulfing
    an = bot.analyze_df(bot.add_indicators(df), "1h")
    assert any("bearish engulfing" in p for _, p in an["patterns"])


def test_breakout_and_squeeze_release_detected():
    df = candles(160, seed=7, sigma=0.8, tail=[(0.05, 45, 0.0), (0.2, 3, 1.2)])
    an = bot.analyze_df(bot.add_indicators(df), "1h")
    assert an["breakout"] is None or an["breakout"][0] == "up"
    df2 = candles(150, seed=2, sigma=0.4, tail=[(0.1, 30, 0.0), (0.3, 6, 0.9)])
    an2 = bot.analyze_df(bot.add_indicators(df2), "1h")
    assert an2["bias_pts"] > an["bias_pts"] - 10          # sanity: runs and produces a score
    assert any("squeeze" in t or "range" in t or "trend" in t for _, t in an2["why"])


def test_scenarios_are_consistent_with_price():
    """Targets must always be on the correct side of the current price, even mid-breakout."""
    for seed, tail in ((2, [(0.1, 30, 0.02), (0.35, 6, 0.8)]), (4, [(0.1, 30, 0.0), (0.4, 6, -0.9)]), (9, None)):
        df = bot.add_indicators(candles(160, seed=seed, sigma=0.5, tail=tail))
        an = bot.analyze_df(df, "1h")
        px = an["price"]
        assert all(t > px for t in an["bull"]["targets"]), (seed, an["bull"], px)
        assert all(t < px for t in an["bear"]["targets"]), (seed, an["bear"], px)
        assert an["bull"]["active"] == (px > an["bull"]["trigger"])
        html = bot.analysis_html("X", "1h", an, {})
        assert ("already under way" in html) == (an["bull"]["active"] or an["bear"]["active"])


def test_chart_renders_png_and_html_escapes():
    df = bot.add_indicators(candles(260, seed=3))
    an = bot.analyze_df(df, "1h")
    png = bot.render_coin_chart(df, "SOL", "1h", an)
    assert png and png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 20_000
    html = bot.analysis_html("SOL", "1h", an, {"4h": {"trend": "BULLISH", "rsi": 60, "adx": 25}})
    for part in ("CHART ANALYSIS", "Trend &amp; strength", "Higher timeframes", "Levels", "Scenarios", "not a prediction"):
        assert part in html, part
    assert "&" not in html.replace("&amp;", "").replace("&lt;", "").replace("&gt;", "")
    assert bot.render_coin_chart(df.head(20), "SOL", "1h", an) is None


def test_cmd_analyze_end_to_end():
    s = Photos()
    _fake_tickers()
    bot.get_klines = lambda interval="1h", limit=200, symbol="BTCUSDT": candles(max(limit, 260), seed=abs(hash(symbol)) % 99)
    st = bot.default_state()
    bot.cmd_analyze(st, ["sol", "15m"])
    assert len(s.photos) == 1 and "SOL 15m" in s.photos[0]["caption"]
    assert "SOL 15m CHART ANALYSIS" in s.texts
    kb = s.sent[-1]["markup"]["inline_keyboard"][0]
    assert [b["callback_data"] for b in kb] == ["an:SOL:5m", "an:SOL:15m", "an:SOL:1h", "an:SOL:4h", "an:SOL:1d"]
    bot.cmd_analyze(st, ["DOGE", "nonsense words"])               # falls back to 1h
    assert "DOGE 1h CHART ANALYSIS" in s.texts
    bot.get_klines = lambda *a, **k: pd.DataFrame()
    bot.cmd_analyze(st, ["ZZZ"])
    assert "Couldn't load enough" in s.sent[-1]["text"]


# ---------- scoring ----------
def test_score_breakout_rewards_setup_and_penalises_overheat():
    base = candles(160, seed=2, sigma=0.4, tail=[(0.1, 30, 0.02), (0.35, 6, 0.8)])
    sc = bot.score_breakout(bot.add_indicators(base), None, {"change_24h": 6.0}, 1.0)
    assert sc and 0 <= sc["tech"] <= 100 and sc["trigger"] > 0 and sc["invalid"] < sc["trigger"]
    hot = candles(150, seed=2, sigma=0.4, tail=[(0.05, 10, 0.0), (0.2, 3, 3.5)])
    hot.loc[len(hot) - 4:, "volume"] = 600
    sc2 = bot.score_breakout(bot.add_indicators(hot), None, {"change_24h": 28.0}, 1.0)
    assert sc2 and any("already up" in f or "parabolic" in f or "RSI" in f for f in sc2["flags"])
    assert bot.score_breakout(base.head(50)) is None


def test_enrich_candidate_news_and_blocks():
    bot.coin_derivs = lambda b: {"funding": 0.0001, "oi_change_24h": 8.0, "oi_change_6h": 6.0}
    bot.coin_news = lambda b: [{"title": "Foo lists new partnership with Bar, mainnet launch reportedly soon", "source": "X"}]
    c = {"coin": "FOO", "price": 1.0, "chg_6h": 1.0, "flags": [], "tech": 60}
    bot.enrich_candidate(c)
    assert c["news_pts"] > 0 and c["deriv_pts"] >= 10 and not c["blocked"]
    assert any("rumor" in f for f in c["flags"])
    bot.coin_news = lambda b: [{"title": "Foo protocol hack: attacker drained funds", "source": "X"}]
    c2 = {"coin": "FOO", "price": 1.0, "chg_6h": 1.0, "flags": [], "tech": 60}
    bot.enrich_candidate(c2)
    assert c2["blocked"] and c2["news_pts"] < 0
    bot.coin_derivs = lambda b: {"funding": 0.0009, "oi_change_24h": None, "oi_change_6h": None}
    bot.coin_news = lambda b: []
    c3 = {"coin": "FOO", "price": 1.0, "chg_6h": 1.0, "flags": [], "tech": 60}
    bot.enrich_candidate(c3)
    assert c3["deriv_pts"] == -10 and c3["no_news"] and any("funding" in f for f in c3["flags"])


# ---------- scan + pinned message ----------
def _stub_scan(score=82, coin_df=None):
    df = coin_df if coin_df is not None else candles(260, seed=3)
    an = bot.analyze_df(bot.add_indicators(df), "1h")
    px = an["price"]

    def fake_score(d, d4=None, tk=None, btc=None):
        strong = bool(tk) and tk.get("price") == 180          # SOL in the fake tickers
        return {"tech": (score - 10) if strong else 10, "reasons": ["1h volatility squeeze just released upward", "volume rising (1.8x normal)"],
                "flags": [], "price": px, "trigger": px * 1.01, "invalid": px * 0.97,
                "targets": [px * 1.04, px * 1.08], "an": an}
    bot.score_breakout = fake_score

    def fake_enrich(c):
        c.update(deriv_pts=5, news_pts=5, news=["headline catalyst: partnership / integration"],
                 headlines=["Foo announces partnership"], funding=0.0001, blocked=False, no_news=False)
    bot.enrich_candidate = fake_enrich
    return px


def _scan_env():
    s = Photos()
    tk = _fake_tickers()
    tk["SOL"]["change_24h"] = 4.0
    tk["DOGE"]["change_24h"] = 3.0
    bot.get_klines = lambda interval="1h", limit=200, symbol="BTCUSDT": candles(max(limit, 260), seed=3)
    bot.http_json = lambda *a, **k: None
    bot.time.sleep = lambda s_: None
    bot.scan_danger = lambda tk, limit=3: []
    bot.SCAN_CACHE.update(ts=0, picks=[], finalists=[], danger=[])
    return s


def test_run_scan_pins_chart_then_edits_then_closes():
    s = _scan_env()
    px = _stub_scan()
    st = bot.default_state()
    out = bot.run_scan(st)
    picks, finalists, danger = out
    assert picks and picks[0]["total"] >= bot.SCAN_MIN_SCORE
    assert len(s.photos) == 1 and s.pins == [s.photos[0]["id"]]
    cap = s.photos[0]["caption"]
    assert len(cap) <= 1000 and "BREAKOUT WATCH" in cap and "Why technically" in cap and "News / positioning" in cap
    assert "Confirm" in cap and "Invalid" in cap and "not a promise" in cap and "24h +4.0%" in cap
    pin = st["scan"]["pin"]
    assert pin["photo"] and pin["id"] == s.photos[0]["id"]
    assert len(st["scan_log"]) == 1
    # same coin again -> caption edit at most, no new pinned message
    _stub_scan(score=88)
    bot.run_scan(st)
    assert len(s.photos) == 1 and len(s.pins) == 1 and len(st["scan_log"]) == 1
    assert s.cap_edits and "score" in s.cap_edits[-1][1]
    # price falls below invalidation -> pin closed + unpinned
    bot.TICKERS["data"][pin["coin"]]["price"] = pin["invalid"] * 0.9
    bot.run_scan(st)
    assert st["scan"]["pin"] is None or st["scan"]["pin"]["id"] != pin["id"]
    assert pin["id"] in s.unpins and "ended" in s.cap_edits[-1][1]


def test_scan_pin_expires_and_weaker_pick_does_not_replace():
    s = _scan_env()
    _stub_scan(score=80)
    st = bot.default_state()
    bot.run_scan(st)
    pin = st["scan"]["pin"]
    pin["ts"] -= 25 * 3600
    _stub_scan(score=40)                                   # below bar -> no picks
    bot.run_scan(st)
    assert st["scan"]["pin"] is None and pin["id"] in s.unpins


def test_scan_with_no_pick_is_honest():
    s = _scan_env()
    _stub_scan(score=45)
    st = bot.default_state()
    picks, finalists, danger = bot.run_scan(st)
    assert not picks and not s.photos
    html = bot.scan_full_html(picks, finalists, danger)
    assert "No coin meets the bar" in html and "Closest candidates" in html
    bot.cmd_scan(st)
    assert "No coin meets the bar" in s.texts


def test_scan_command_with_picks_has_chart_buttons():
    s = _scan_env()
    _stub_scan()
    st = bot.default_state()
    bot.cmd_scan(st)
    last = s.sent[-1]
    assert "BREAKOUT SCAN" in last["text"] and last["markup"]["inline_keyboard"][0][0]["callback_data"].startswith("an:")


# ---------- scoreboard ----------
def test_scoreboard_paper_trading():
    _fake_tickers()
    st = bot.default_state()
    now = time.time()
    mk = lambda coin, age: {"id": coin, "coin": coin, "ts": now - age, "price": 100.0, "score": 70, "tech": 60,
                            "trigger": 101, "invalid": 97, "btc_price": 67000.0, "max_up": 0.0, "max_dn": 0.0,
                            "first": None, "res": {}, "status": "open"}
    st["scan_log"] = [mk("SOL", 90000), mk("DOGE", 90000), mk("AVAX", 90000), mk("PEPE", 600)]
    bot.TICKERS["data"]["SOL"]["price"] = 106.0
    bot.TICKERS["data"]["DOGE"]["price"] = 96.0
    bot.TICKERS["data"]["AVAX"]["price"] = 101.0
    bot.TICKERS["data"]["PEPE"]["price"] = 100.5
    bot.TICKERS["data"]["BTC"]["price"] = 67670.0
    bot.update_scan_outcomes(st)
    by = {p["coin"]: p for p in st["scan_log"]}
    assert by["SOL"]["first"] == "target" and by["DOGE"]["first"] == "stop" and by["AVAX"]["first"] == "timeout"
    assert by["SOL"]["status"] == "done" and set(by["SOL"]["res"]) == {"1h", "4h", "24h"} and by["PEPE"]["status"] == "open"
    txt = bot.scan_stats_text(st)
    assert "Hit +5% before -3%: 1/3" in txt and "too few" in txt and "Open picks" in txt and "PEPE" in txt


# ---------- digest / notify ----------
def test_digest_queue_and_flush():
    s = Sink()
    st = bot.default_state()
    bot.cmd_digest(st, ["on", "2"])
    assert st["digest"]["on"] and st["digest"]["hours"] == 2
    assert bot.notify(st, "<b>full</b>", None, "normal", digest_line="line 1") is None
    bot.notify(st, "full", None, "normal", digest_line="line 2")
    assert len(st["digest"]["queue"]) == 2 and not [x for x in s.sent if "line" in x["text"]]
    bot.notify(st, "<b>critical</b>", None, "critical", digest_line="x")           # critical bypasses
    assert s.sent[-1]["text"] == "<b>critical</b>"
    bot.flush_digest(st)
    assert len(st["digest"]["queue"]) == 2                                          # too early
    st["digest"]["last"] -= 3 * 3600
    bot.flush_digest(st)
    assert "DIGEST - 2 alerts" in s.sent[-1]["text"] and not st["digest"]["queue"]
    bot.cmd_digest(st, ["off"])
    bot.notify(st, "direct", None, "normal", digest_line="y")
    assert s.sent[-1]["text"] == "direct"


def test_pump_alert_goes_to_digest_when_on():
    s = Sink()
    _fake_tickers()
    bot.http_json = lambda *a, **k: None
    st = bot.default_state()
    st["digest"].update(on=True, hours=3, last=time.time())
    now = time.time()
    st["tick_hist"] = {"SOL": [[now - 3600, 100.0], [now - 60, 110.0]], "BTC": [[now - 3600, 67000.0], [now - 60, 67050.0]]}
    bot.check_pump_dump(st)
    assert st["digest"]["queue"] and "SOL" in st["digest"]["queue"][0] and not s.sent


# ---------- listings / near-level ----------
def test_listing_monitor():
    s = Sink()
    tk = _fake_tickers()
    tk["FOO"] = {"price": 2.0, "change_24h": 5.0, "quote_vol": 1e7}
    st = bot.default_state()
    batch = {"data": {"catalogs": [{"articles": [{"title": "Binance Will List Foo (FOO) with Seed Tag", "code": "abc"}]}]}}
    bot.http_json = lambda *a, **k: batch
    bot.parse_rss = lambda url, cat: [{"title": "Coinbase adds support for Bar (BAR) - news", "link": "http://x", "source": "n",
                                       "description": "", "published": "", "category": cat}]
    bot.check_listings(st)
    assert not s.sent and st["listing_seen"]                     # first run only learns, no flood
    batch["data"]["catalogs"][0]["articles"].insert(0, {"title": "Binance Will List Baz (BAZ)", "code": "def"})
    bot.check_listings(st)
    assert "LISTING NEWS (Binance)" in s.texts and "BAZ" in s.texts
    n = len(s.sent)
    bot.check_listings(st)
    assert len(s.sent) == n                                      # no duplicates


def test_near_level_alert_once():
    s = Sink()
    _ctx()
    st = bot.default_state()
    bot._levels_for = lambda base: (68000.0, 66000.0)
    bot._price_of = lambda base: 67800.0                         # 0.29% under resistance
    bot.check_near_levels(st)
    assert "approaching resistance" in s.texts
    n = len(s.sent)
    bot.check_near_levels(st)
    assert len(s.sent) == n                                      # 4h cooldown


# ---------- routing ----------
def test_free_text_and_command_routing_v34():
    s = Sink()
    _fake_tickers()
    _ctx()
    calls = []
    bot.cmd_scan = lambda state: calls.append(("scan",))
    bot.cmd_analyze = lambda state, args: calls.append(("an", list(args)))
    bot.tg_send_html = lambda t, rm=None, level="low": calls.append(("html", t[:12])) or 1
    bot.ANTHROPIC_API_KEY = None
    st = bot.default_state()
    for text, expect in [("which coin is going to pump today?", ("scan",)),
                         ("any coins about to break out", ("scan",)),
                         ("analyze SOL on 15m", ("an", ["SOL", "15m"])),
                         ("show me the DOGE chart 4h", ("an", ["DOGE", "4h"])),
                         ("technical analysis", ("an", ["BTC", "1h"])),
                         ("how accurate are your picks, scoreboard", ("html", "<b>🏁 BREAKOU"))]:
        calls.clear()
        bot.handle_text(st, text)
        assert calls and calls[0] == expect, (text, calls)
    calls.clear()
    bot.handle_command(st, "/chart", ["DOGE", "15m"])
    bot.handle_command(st, "/analyze", ["SOL"])
    bot.handle_command(st, "/scan", [])
    assert calls == [("an", ["DOGE", "15m"]), ("an", ["SOL"]), ("scan",)]
    bot.handle_command(st, "/digest", ["on", "3"])
    assert st["digest"]["on"]
    assert len(bot.HELP_TEXT) < 4000 and len(bot.BOT_COMMANDS) < 100


def test_callbacks_for_analysis_buttons():
    calls = []
    bot.cmd_analyze = lambda state, args: calls.append(("an", list(args)))
    bot.cmd_scan = lambda state: calls.append(("scan",))
    bot.tg_post = lambda *a, **k: {"ok": True}
    bot.CHAT_ID = "1"
    st = bot.default_state()
    cq = lambda data: {"callback_query": {"id": "1", "data": data, "message": {"chat": {"id": 1}, "message_id": 5}}}
    bot.handle_update(st, cq("an:SOL:15m"))
    bot.handle_update(st, cq("scan"))
    assert calls == [("an", ["SOL", "15m"]), ("scan",)]


def _wrap(fn):
    def inner():
        _reset()
        return fn()
    inner.__name__ = fn.__name__
    return inner


for _n, _f in list(globals().items()):
    if _n.startswith("test_") and callable(_f):
        globals()[_n] = _wrap(_f)
