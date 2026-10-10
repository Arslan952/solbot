"""Tests for v3.3 features (coin explainer, weekend, pinning, alerts, quiet hours, Q&A...)."""
import os
import sys
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot  # noqa: E402


class Sink:
    """Captures everything the bot would send to Telegram."""
    def __init__(self):
        self.sent, self.edits, self.pins, self.unpins, self.deleted, self.n = [], [], [], [], [], 100
        bot.TOKEN, bot.CHAT_ID = "t", "1"
        bot.tg_send = self.tg_send
        bot.send = lambda m, reply_markup=None, level="normal": self.tg_send(m, reply_markup, False, level) is not None
        bot.tg_send_html = lambda t, rm=None, level="low": self.tg_send(t, rm, True, level)
        bot.tg_edit = lambda mid, text, markup=None, html=True: self.edits.append((mid, text)) or True
        bot.pin_message = lambda mid: self.pins.append(mid)
        bot.unpin_message = lambda mid: self.unpins.append(mid)
        bot.delete_message = lambda mid: self.deleted.append(mid)
        bot.make_chart = lambda **k: None

    def tg_send(self, msg, reply_markup=None, html=False, level="normal"):
        self.n += 1
        self.sent.append({"id": self.n, "text": msg, "markup": reply_markup, "level": level, "html": html})
        return self.n

    @property
    def texts(self):
        return "\n".join(s["text"] for s in self.sent)


def _fake_tickers():
    t = {"BTC": {"price": 67000, "change_24h": 1.0, "quote_vol": 5e9},
         "ETH": {"price": 3500, "change_24h": 1.5, "quote_vol": 2e9},
         "SOL": {"price": 180, "change_24h": 14.0, "quote_vol": 8e8},
         "DOGE": {"price": 0.2, "change_24h": 12.0, "quote_vol": 4e8},
         "PEPE": {"price": 0.00001, "change_24h": 11.0, "quote_vol": 3e8},
         "AVAX": {"price": 40, "change_24h": 9.0, "quote_vol": 1e8},
         "XYZ": {"price": 1, "change_24h": 50.0, "quote_vol": 1e5},
         "LOSE": {"price": 1, "change_24h": -20.0, "quote_vol": 9e7}}
    bot.TICKERS.update(ts=time.time(), data=t)
    return t


def _candles(n=300, step=3600, drift=0.0, seed=1):
    rng = np.random.default_rng(seed)
    end = int(time.time()) // step * step
    t = (np.arange(n) * step + end - (n - 1) * step) * 1000
    close = 100 + np.cumsum(rng.normal(drift, 0.5, n))
    return pd.DataFrame({"time": t, "open": close - rng.normal(0, 0.1, n), "high": close + 0.6,
                         "low": close - 0.6, "close": close, "volume": rng.uniform(80, 120, n)})


def _ctx():
    bot.CTX.clear()
    bot.CTX.update({
        "ticker": {"price": 67000, "change_24h": 1.0},
        "tech": {"1h": {"trend": "BULLISH", "rsi": 55, "adx": 25}, "4h": {"trend": "BULLISH"},
                 "15m": {"trend": "BULLISH"}, "1d": {"trend": "BULLISH"}, "1w": {"trend": "BULLISH"}},
        "cross": {}, "news": [], "events": [], "futures": {"funding": 0.0004, "oi_change_pct": 4, "open_interest": 1e5},
        "bias": {"score": 3.0, "bias": "BULLISH", "confidence": 80, "reasons": ["4-hour chart trend is UP"]},
        "levels": {"price": 67000, "atr": 300, "atr_pct": 0.45, "support": 66000, "resistance": 68000,
                   "bull1": 67300, "bull2": 67600, "bear1": 66700, "bear2": 66400},
        "keylevels": {"vwap": 66800, "prev_week_high": 68500, "prev_week_low": 65000, "week_open": 66000},
        "options": {"pc_oi": 1.3, "iv": 50}, "glob": {"btc_dom": 57.0},
        "plan": {"action": "WAIT", "why": ["Choppy"], "trigger_long": 68000, "trigger_short": 66000}})


# ---------- timezone & quiet hours ----------
def test_timezone_formatting():
    st = bot.default_state()
    st["tz"] = "Asia/Karachi"
    bot.apply_user_tz(st)
    dt = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    assert bot.fmt_local(dt, "%H:%M") == "17:00 PKT"
    st["tz"] = "UTC"
    bot.apply_user_tz(st)
    assert bot.fmt_local(dt, "%H:%M") == "12:00 UTC"


def test_quiet_hours_parse_and_silent_delivery():
    assert bot._parse_quiet("00:00-07:00") == (0, 420)
    assert bot._parse_quiet("nonsense") is None
    st = bot.default_state()
    st["quiet"] = "00:00-23:59"
    bot.CTX["state"] = st
    assert bot._silent("normal") and bot._silent("low") and not bot._silent("critical")
    st["quiet"] = None
    st["alert_mode"] = "important"
    assert bot._silent("low") and not bot._silent("normal")
    st["alert_mode"] = "all"
    assert not bot._silent("low")


# ---------- coin detection & explainer ----------
def test_detect_coin():
    _fake_tickers()
    assert bot.detect_coin("why did SOL pump today?") == "SOL"
    assert bot.detect_coin("why did dogecoin dump") == "DOGE"
    assert bot.detect_coin("what is $PEPE doing") == "PEPE"
    assert bot.detect_coin("why is the market weird today") is None


def test_explain_drivers_ranking():
    tk = _fake_tickers()
    snap = {"base": "SOL", "chg_24h": 14.0, "chg_4h": 5.0, "vol_ratio": 3.2, "price": 180,
            "tr1": {"resistance": 179, "support": 150}}
    d = bot.explain_drivers(snap, {"funding": 0.0001, "oi_change_24h": -6.0}, [], tk)
    texts = " | ".join(x for _, x in d)
    assert "volume is 3.2x" in texts and "short squeeze" in texts and "Coin-specific" in texts
    assert d[0][0] >= d[-1][0] and len(d) <= 4


def test_explain_drivers_market_wide_and_fallback():
    tk = {"BTC": {"change_24h": -6.0}, "ETH": {"change_24h": -7.0}}
    snap = {"base": "ETH", "chg_24h": -7.0, "chg_4h": -2.0, "vol_ratio": 1.0, "price": 3000,
            "tr1": {"resistance": 4000, "support": 2000}}
    d = bot.explain_drivers(snap, {}, [], tk)
    assert "Whole market" in d[0][1]
    snap2 = dict(snap, chg_24h=8.0)
    d2 = bot.explain_drivers(snap2, {}, [], {"BTC": {"change_24h": 7.5}})
    assert d2 and isinstance(d2[0][1], str)
    snap3 = dict(snap, chg_24h=3.0)
    d3 = bot.explain_drivers(snap3, {}, [], {"BTC": {"change_24h": 0.1}})
    assert "No clear" in d3[-1][1]


def test_explain_move_end_to_end_and_html():
    _fake_tickers()
    bot.EXPLAIN_CACHE.clear()
    bot.ANTHROPIC_API_KEY = None
    bot.get_klines = lambda interval="1h", limit=200, symbol="BTCUSDT": _candles(200, 3600 if interval == "1h" else 14400, 0.2)
    bot.http_json = lambda *a, **k: None
    bot.parse_rss = lambda url, cat: [{"title": "Solana lists new partnership & launch", "source": "X<y>",
                                       "description": "", "link": "l", "published": "", "category": cat}]
    res = bot.explain_move("SOL", bot.default_state())
    assert res and res["drivers"]
    html = bot.explain_html(res)
    assert "WHY IS SOL" in html and "&amp;" in html and "X&lt;y&gt;" in html
    assert "partnership" in html.lower()


def test_coin_card_and_movers():
    _fake_tickers()
    bot.get_klines = lambda interval="1h", limit=200, symbol="BTCUSDT": _candles(200, 3600 if interval == "1h" else 14400)
    bot.http_json = lambda *a, **k: None
    assert "SOL" in bot.coin_card_html("SOL")
    text, kb = bot.movers_html()
    assert "SOL" in text and "XYZ" not in text           # tiny-volume coin hidden
    assert "LOSE" in text and kb["inline_keyboard"][0][0]["callback_data"].startswith("why:")


# ---------- pump / dump alerts ----------
def test_pump_dump_alert_and_cooldown():
    s = Sink()
    tk = _fake_tickers()
    bot.http_json = lambda *a, **k: None
    st = bot.default_state()
    now = time.time()
    st["tick_hist"] = {"SOL": [[now - 3600, 100.0], [now - 1800, 104.0], [now - 60, 110.0]],
                       "BTC": [[now - 3600, 67000.0], [now - 60, 67100.0]],
                       "ETH": [[now - 3600, 3500.0], [now - 60, 3502.0]]}
    bot.check_pump_dump(st)
    assert "PUMP: SOL" in s.texts and "coin-specific" in s.texts
    n = len(s.sent)
    bot.check_pump_dump(st)                                # cooldown: no duplicate
    assert len(s.sent) == n


# ---------- watchlist & alerts ----------
def test_watchlist_commands():
    s = Sink()
    _fake_tickers()
    st = bot.default_state()
    bot.cmd_watch(st, ["sol", "doge", "nope"])
    assert st["watchlist"] == ["SOL", "DOGE"]
    bot.cmd_watch(st, ["SOL"], remove=True)
    assert st["watchlist"] == ["DOGE"]


def test_price_alerts_trigger_once():
    s = Sink()
    _fake_tickers()
    _ctx()
    st = bot.default_state()
    bot.cmd_alert(st, ["68000"])                           # BTC above 68000
    bot.cmd_alert(st, ["SOL", "100"])                      # SOL below 100
    assert len(st["alerts"]) == 2 and st["alerts"][0]["dir"] == "above" and st["alerts"][1]["dir"] == "below"
    bot.check_price_alerts(st)
    assert len(st["alerts"]) == 2                          # nothing hit yet
    bot.CTX["ticker"]["price"] = 68100
    bot.check_price_alerts(st)
    assert len(st["alerts"]) == 1 and "ALERT #1" in s.texts
    assert s.sent[-1]["level"] == "critical"


def test_candle_close_alert():
    s = Sink()
    _ctx()
    st = bot.default_state()
    df = _candles(100, 3600)
    df.loc[df.index[-2], "close"] = 69000.0               # last CLOSED candle closed above
    bot.CANDLES["1h"] = df
    bot.cmd_alert(st, ["close", "1h", "above", "68000"])
    st["alerts"][0]["created"] -= 7200                    # alert was created 2h ago
    bot.check_price_alerts(st)
    assert not st["alerts"] and "candle closed" in s.texts


# ---------- weekend ----------
def test_weekend_stats_and_text():
    _ctx()
    df = _candles(300, 86400, seed=3)
    bot.CANDLES["1d"] = df
    bot.CANDLES["1h"] = _candles(300, 3600)
    ws = bot.weekend_stats()
    assert ws and ws["n"] >= 30 and 0 <= ws["up_share"] <= 100
    st = bot.default_state()
    text = bot.weekend_text(st, "friday")
    for part in ("What is closed", "Base case", "Bullish break", "Bearish break", "What to watch", "not predictions"):
        assert part in text
    assert "&" not in text.replace("&amp;", "").replace("&lt;", "").replace("&gt;", "")


def test_weekend_mode_tightens_rules():
    real = bot.now_utc
    from datetime import timedelta
    sat = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
    mon = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
    bot.now_utc = lambda: sat
    try:
        assert bot.is_weekend() and bot.confirm_needed() == bot.SIGNAL_CONFIRM + 2
        assert abs(bot.max_lev_now() - bot.MAX_LEVERAGE * 0.67) < 1e-9
        bot.now_utc = lambda: mon
        assert not bot.is_weekend() and bot.confirm_needed() == bot.SIGNAL_CONFIRM
    finally:
        bot.now_utc = real


def test_weekend_post_pins_and_monday_unpins():
    s = Sink()
    _ctx()
    bot.CANDLES["1d"] = _candles(300, 86400, seed=3)
    bot.CANDLES["1h"] = _candles(300, 3600)
    st = bot.default_state()
    real = bot.now_utc
    fri = datetime(2026, 10, 9, 20, 45, tzinfo=timezone.utc)      # Friday 16:45 New York
    bot.now_utc = lambda: fri
    try:
        bot.check_weekend_posts(st)
        assert st["weekend_msg"] and s.pins == [st["weekend_msg"]]
        bot.check_weekend_posts(st)                                 # no duplicate the same day
        assert len(s.pins) == 1
        mon = datetime(2026, 10, 12, 14, 0, tzinfo=timezone.utc)    # Monday 10:00 New York
        bot.now_utc = lambda: mon
        bot.check_weekend_posts(st)
        assert st["weekend_msg"] is None and s.unpins
    finally:
        bot.now_utc = real


# ---------- pinned live trade ----------
def _trade():
    p = {"action": "LONG", "entry": 100.0, "stop": 98.0, "tp1": 103.0, "tp2": 105.0, "risk": 2.0,
         "rr1": 1.5, "rr2": 2.5, "risk_usd": 10, "size_btc": 5, "score": 3}
    return bot.new_trade(p, time.time())


def test_live_trade_pin_lifecycle():
    s = Sink()
    st = bot.default_state()
    t = _trade()
    st["active_trade"] = t
    bot.update_trade_pin(st, 99.0)
    assert st["trade_msg"] and s.pins == [st["trade_msg"]["id"]] and "LIVE TRADE" in s.texts
    bot.advance_trade(t, 100.5, 99.5, time.time())               # filled
    bot.update_trade_pin(st, 101.0)
    assert s.edits and "OPEN" in s.edits[-1][1]
    n = len(s.edits)
    bot.update_trade_pin(st, 101.0)                               # unchanged -> no extra edit
    assert len(s.edits) == n
    bot.advance_trade(t, 106, 100.5, time.time())
    bot.finish_trade(st, t)
    bot.update_trade_pin(st, 105.0)
    assert st["trade_msg"] is None and s.unpins and "CLOSED TRADE" in s.edits[-1][1]


def test_event_banner_pin_and_removal():
    s = Sink()
    st = bot.default_state()
    ev = [{"id": "e1", "name": "US CPI", "time": datetime.now(timezone.utc) + __import__("datetime").timedelta(minutes=30)}]
    bot.update_event_banner(st, ev)
    assert st["banner"] and "NO-TRADE WINDOW" in s.texts and len(s.pins) == 1
    bot.update_event_banner(st, ev)
    assert len(s.pins) == 1                                       # not re-posted
    bot.update_event_banner(st, [])
    assert st["banner"] is None and s.deleted


def test_health_alert_pinned_then_unpinned():
    s = Sink()
    st = bot.default_state()
    bot.HEALTH["price"] = time.time() - 20 * 60
    bot.check_health(st)
    assert st["health_alert"] and s.pins and s.sent[-1]["level"] == "critical"
    bot.HEALTH["price"] = time.time()
    bot.check_health(st)
    assert not st["health_alert"] and s.unpins


def test_daily_plan_posted_once_and_pinned():
    s = Sink()
    _ctx()
    st = bot.default_state()
    st["tz"] = "UTC"
    bot.apply_user_tz(st)
    real = bot.now_utc
    bot.now_utc = lambda: datetime(2026, 10, 9, 9, 0, tzinfo=timezone.utc)
    try:
        bot.check_daily_plan(st)
        bot.check_daily_plan(st)
        assert s.texts.count("DAILY PLAN") == 1 and len(s.pins) == 1
    finally:
        bot.now_utc = real


# ---------- risk guard ----------
def test_daily_guard_blocks_signals():
    s = Sink()
    st = bot.default_state()
    now = time.time()
    st["trades"] = [{"status": "CLOSED", "result_r": -1.2, "closed_at": now - 60, "action": "LONG"} for _ in range(3)]
    assert "loss limit" in bot.daily_guard(st)
    assert "loss limit" in bot.check_risk_guard(st)
    n = len(s.sent)
    bot.check_risk_guard(st)
    assert len(s.sent) == n                                       # notified once per day
    tech = {"1h": {"trend": "BULLISH", "rsi": 60, "adx": 25, "volume_ratio": 1.2, "atr": 300, "support": None,
                   "resistance": None, "price": 67000},
            "15m": {"trend": "BULLISH", "volume_ratio": 1.2}, "4h": {"trend": "BULLISH"}}
    bias = {"score": 3.0, "bias": "BULLISH", "confidence": 80, "reasons": []}
    plan = bot.build_trade_plan(bias, tech, bot.scenario_levels(tech, 67000), [], guard_text=bot.daily_guard(st))
    assert plan["action"] == "WAIT" and "loss limit" in plan["why"][0]
    st["trades"] = [{"status": "CLOSED", "result_r": 0.5, "closed_at": now - 60, "action": "LONG"}] * bot.MAX_TRADES_PER_DAY
    assert "trade limit" in bot.daily_guard(st)


# ---------- trade buttons ----------
def test_trade_buttons_chase_why_took():
    s = Sink()
    _ctx()
    st = bot.default_state()
    t = _trade()
    t["features"] = {"score": 3.0, "reasons": ["4-hour chart trend is UP"], "t4h": "BULLISH", "t1h": "BULLISH",
                     "t15": "BULLISH", "adx_1h": 25, "volratio_15m": 1.2, "rsi_1h": 58, "t1d": "BULLISH", "t1w": "BULLISH"}
    st["active_trade"] = t
    cq = lambda a: {"data": f"tr:{a}:{t['id']}"}
    bot.CTX["ticker"]["price"] = 101.0
    bot.handle_trade_callback(st, cq("why"))
    assert "WHY THIS LONG" in s.texts and "ADX" in s.texts
    bot.handle_trade_callback(st, cq("chase"))
    assert "CHASE CHECK" in s.texts
    assert "invalid" in bot.chase_text(t, 97.0) and "passed TP1" in bot.chase_text(t, 104.0)
    assert "Still decent" in bot.chase_text(t, 100.0) or "Marginal" in bot.chase_text(t, 100.0)
    bot.handle_trade_callback(st, cq("took"))
    assert st["decisions"][t["id"]] == "took"
    t.update(status="CLOSED", result_r=1.5, outcome="TP2")
    st["trades"].append(t)
    st["active_trade"] = None
    assert any("Trades you took: 1" in x for x in bot.my_stats_lines(st))


# ---------- onboarding / commands / free text ----------
def test_onboarding_callbacks():
    s = Sink()
    st = bot.default_state()
    bot.handle_command(st, "/start", [])
    assert st["onboarded"] and s.sent[-1]["markup"]
    bot.handle_setup_callback(st, {"data": "set:tz:Asia/Karachi"})
    bot.handle_setup_callback(st, {"data": "set:risk:0.5"})
    bot.handle_setup_callback(st, {"data": "set:lang:ur"})
    bot.handle_setup_callback(st, {"data": "set:alerts:important"})
    assert (st["tz"], st["risk_pct"], st["lang"], st["alert_mode"]) == ("Asia/Karachi", 0.5, "ur", "important")
    bot.handle_setup_callback(st, {"data": "set:tz:Not/AZone"})
    assert st["tz"] == "Asia/Karachi"
    bot.handle_command(st, "/tz", ["UTC"]); bot.handle_command(st, "/quiet", ["22:00-06:00"])
    assert st["quiet"] == "22:00-06:00"
    bot.handle_command(st, "/quiet", ["off"])
    assert st["quiet"] is None


def test_free_text_routing():
    s = Sink()
    _fake_tickers()
    _ctx()
    calls = []
    bot.cmd_why = lambda state, args: calls.append(("why", args))
    bot.cmd_coin = lambda state, args: calls.append(("coin", args))
    bot.cmd_movers = lambda state: calls.append(("movers",))
    bot.ANTHROPIC_API_KEY = None
    bot.CANDLES["1d"] = _candles(300, 86400, seed=3)
    bot.CANDLES["1h"] = _candles(300, 3600)
    st = bot.default_state()
    for text, expect in [("why did SOL pump today?", ("why", ["SOL"])),
                         ("why is dogecoin dumping", ("why", ["DOGE"])),
                         ("why did the market crash", ("why", ["BTC"])),
                         ("how is AVAX doing", ("coin", ["AVAX"])),
                         ("show me the top gainers", ("movers",))]:
        calls.clear()
        bot.handle_text(st, text)
        assert calls and calls[0] == expect, (text, calls)
    bot.handle_text(st, "what will happen on the weekend?")
    assert "WEEKEND" in s.texts
    bot.handle_text(st, "tell me a joke")
    assert "I can answer things like" in s.sent[-1]["text"]


def test_free_text_uses_ai_when_available():
    s = Sink()
    _ctx()
    bot.ANTHROPIC_API_KEY = "k"
    seen = {}
    bot.claude_call = lambda system, user, max_tokens=300: seen.update(system=system, user=user) or "Short answer."
    bot.handle_text(bot.default_state(), "what do you think about the next few hours?")
    assert s.sent[-1]["text"] == "Short answer."
    assert "Market data" in seen["user"] and "never say 'buy now'" in seen["system"]
    bot.ANTHROPIC_API_KEY = None


def test_critical_trade_alert_has_buttons_and_pin_flow():
    s = Sink()
    _ctx()
    st = bot.default_state()
    tech = {"1h": {"trend": "BULLISH", "rsi": 60, "adx": 25, "volume_ratio": 1.2, "atr": 300, "support": None,
                   "resistance": None, "price": 67000},
            "15m": {"trend": "BULLISH", "volume_ratio": 1.2}, "4h": {"trend": "BULLISH"}}
    bias = {"score": 3.0, "bias": "BULLISH", "confidence": 80, "reasons": []}
    plan = bot.build_trade_plan(bias, tech, bot.scenario_levels(tech, 67000), [], account=100000)
    for _ in range(bot.confirm_needed()):
        bot.process_signal(st, plan, bias, 67000)
    alert = [x for x in s.sent if "TRADE ALERT" in x["text"]][0]
    assert alert["level"] == "critical" and alert["markup"]["inline_keyboard"][0][0]["callback_data"].startswith("tr:why:")
    bot.update_trade_pin(st, 67000)
    assert s.pins and "LIVE TRADE" in s.sent[-1]["text"]


def test_all_tabs_and_card_render_with_new_features():
    _ctx()
    _fake_tickers()
    st = bot.default_state()
    st["watchlist"] = ["SOL"]
    for tab in bot.TAB_BUILDERS:
        out = bot.build_tab(tab, st)
        assert out and "Could not build" not in out, tab
    assert "WATCHLIST" in bot.build_tab("watch", st)
    assert len(bot.HELP_TEXT) < 4000
    assert len(bot.tab_keyboard("watch")["inline_keyboard"]) >= 3
