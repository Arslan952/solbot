"""Unit tests for the pure logic of bot.py.  Run:  pytest -q"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot  # noqa: E402


def _plan(entry=100.0, stop=98.0, action="LONG"):
    return {"action": action, "entry": entry, "stop": stop, "tp1": 103.0, "tp2": 105.0,
            "risk": 2.0, "rr1": 1.5, "rr2": 2.5, "risk_usd": 10, "size_btc": 5, "score": 3}


# ---------- helpers ----------
def test_parse_number():
    assert bot.parse_number("160K") == 160_000
    assert bot.parse_number("3.2%") == 3.2
    assert bot.parse_number("-0.3") == -0.3
    assert bot.parse_number("") is None


# ---------- news: whole-word matching ----------
def test_news_no_false_positives_from_substrings():
    item = {"title": "Bank warns second quarter sector slowdown", "description": "urban software"}
    impact, _ = bot.news_impact(item)
    assert impact == "LOW"


def test_news_real_high_impact():
    impact, direction = bot.news_impact({"title": "Fed cuts rates as CPI cools", "description": ""})
    assert impact == "HIGH" and direction == "BULLISH"
    impact, direction = bot.news_impact({"title": "SEC rejected the ETF, exchange hack feared", "description": ""})
    assert impact == "HIGH" and direction == "BEARISH"


# ---------- trade engine ----------
def test_stop_first_when_candle_touches_both():
    t = bot.new_trade(_plan(), 1000.0)
    ev = bot.advance_trade(t, 106, 97, 1100)          # fills, then hits stop AND target
    assert [e[0] for e in ev] == ["ENTERED", "STOP"]
    assert t["status"] == "CLOSED"
    assert -1.07 < t["result_r"] < -1.02              # -1R minus fees and slippage


def test_tp1_then_tp2_result_includes_costs():
    t = bot.new_trade(_plan(), 1000.0)
    bot.advance_trade(t, 100.5, 99.5, 1100)           # fill
    ev = bot.advance_trade(t, 103.5, 101, 1200)       # TP1 -> breakeven stop
    assert "TP1" in [e[0] for e in ev] and t["tp1_hit"] and t["current_stop"] >= 100.0
    bot.advance_trade(t, 105.5, 104, 1300)            # TP2
    assert t["outcome"] == "TP2"
    assert 1.9 < t["result_r"] < 2.0                  # 2.0R minus ~0.02R fees


def test_breakeven_after_tp1():
    t = bot.new_trade(_plan(), 1000.0)
    bot.advance_trade(t, 100.5, 99.5, 1100)
    bot.advance_trade(t, 103.2, 101, 1200)
    ev = bot.advance_trade(t, 102, 99.9, 1300)
    assert t["outcome"] in ("BREAKEVEN", "TRAIL") and t["result_r"] > 0.6


def test_pending_expires_and_missed():
    t = bot.new_trade(_plan(), 1000.0)
    ev = bot.advance_trade(t, 110, 105, 1100)         # ran to TP1 without filling
    assert ev[0][0] == "MISSED" and t["status"] == "EXPIRED"
    t2 = bot.new_trade(_plan(), 1000.0)
    ev = bot.advance_trade(t2, 102, 101, 1000 + bot.EXPIRE_MIN * 60 + 1)
    assert ev[0][0] == "EXPIRED"


def test_short_trade_mirror():
    p = _plan(entry=100.0, stop=102.0, action="SHORT")
    p.update(tp1=97.0, tp2=95.0)
    t = bot.new_trade(p, 1000.0)
    bot.advance_trade(t, 100.5, 99.5, 1100)
    bot.advance_trade(t, 99, 96.5, 1200)
    bot.advance_trade(t, 98, 94.5, 1300)
    assert t["outcome"] == "TP2" and 1.9 < t["result_r"] < 2.0


def test_trade_stats_and_sample_warning():
    trades = []
    for r in (2.0, -1.0, 1.0, -1.0, -1.0):
        trades.append({"status": "CLOSED", "result_r": r, "action": "LONG"})
    s = bot.trade_stats(trades)
    assert s["n"] == 5 and round(s["win_rate"]) == 40 and s["max_losing_streak"] == 2
    lines = bot.stats_lines(trades)
    assert any("too few" in x for x in lines)


# ---------- trade plan: leverage cap ----------
def _tech():
    h1 = {"trend": "BULLISH", "rsi": 60, "adx": 25, "volume_ratio": 1.2, "atr": 300,
          "support": None, "resistance": None, "price": 67000}
    return {"1h": h1, "15m": {"trend": "BULLISH", "volume_ratio": 1.2}, "4h": {"trend": "BULLISH"}}


def test_leverage_cap():
    tech = _tech()
    bias = {"score": 3.0, "bias": "BULLISH", "confidence": 80, "reasons": []}
    levels = bot.scenario_levels(tech, 67000)
    plan = bot.build_trade_plan(bias, tech, levels, [], account=100, risk_pct=3.0)
    assert plan["action"] == "LONG" and plan["capped"]
    assert plan["leverage"] <= bot.MAX_LEVERAGE + 1e-6
    assert plan["risk_usd"] < 3.0                       # real risk is lower after the cap
    plan2 = bot.build_trade_plan(bias, tech, levels, [], account=100000, risk_pct=1.0)
    assert not plan2["capped"]


# ---------- signal persistence ----------
def test_signal_needs_confirmation(monkeypatch=None):
    sent = []
    bot.send = lambda msg, reply_markup=None, level="normal": sent.append(msg) or True
    bot.make_chart = lambda **k: None
    bot.CTX.clear()
    tech = _tech()
    bias = {"score": 3.0, "bias": "BULLISH", "confidence": 80, "reasons": []}
    plan = bot.build_trade_plan(bias, tech, bot.scenario_levels(tech, 67000), [], account=100000)
    state = bot.default_state()
    for i in range(bot.confirm_needed() - 1):
        bot.process_signal(state, plan, bias, 67000)
        assert state["active_trade"] is None
    bot.process_signal(state, plan, bias, 67000)
    assert state["active_trade"] is not None and "features" in state["active_trade"]
    # a WAIT in between resets the streak
    state2 = bot.default_state()
    wait = {"action": "WAIT", "why": []}
    bot.process_signal(state2, plan, bias, 1)
    bot.process_signal(state2, wait, bias, 1)
    assert state2["sig_streak"]["count"] == 0


# ---------- open interest: same-source comparison ----------
def test_oi_change_only_between_same_exchange(monkeypatch=None):
    def fake_http(url, params=None, **k):
        if "openInterest" in url and "binance" in url:
            return {"openInterest": "100"}
        if "premiumIndex" in url:
            return {"lastFundingRate": "0.0001"}
        return None
    bot.http_json = fake_http
    bot.liq_from_ws = lambda: (0.0, 0.0)
    now = time.time()
    st = bot.default_state()
    st["oi_history"] = [[now - 1800, 50.0, "okx"]]      # different exchange -> must be ignored
    res = bot.get_futures_context(st)
    assert res["open_interest"] == 100.0 and res["oi_change_pct"] is None
    st["oi_history"] = [[now - 1800, 80.0, "binance"]]
    res = bot.get_futures_context(st)
    assert abs(res["oi_change_pct"] - 25.0) < 0.01


def test_stale_cache_expires():
    bot.http_json = lambda *a, **k: None
    bot.liq_from_ws = lambda: (0.0, 0.0)
    st = bot.default_state()
    bot.FUT_CACHE["funding"] = (0.0002, time.time() - 60)
    res = bot.get_futures_context(st)
    assert res["funding"] == 0.0002 and "funding" in res["stale"]
    bot.FUT_CACHE["funding"] = (0.0002, time.time() - 3600)
    res = bot.get_futures_context(st)
    assert res["funding"] is None


# ---------- event reactions ----------
def test_event_reaction_recording():
    from datetime import datetime, timezone
    st = bot.default_state()
    t0 = time.time() - 40 * 60
    st["price_hist"] = [[t0, 100.0], [t0 + 1800, 101.5]]
    ev = [{"id": "x1", "name": "CPI", "time": datetime.fromtimestamp(t0, timezone.utc)}]
    bot.update_event_reactions(st, ev)
    assert st["event_reactions"]["CPI"][0]["pct"] == 1.5
    assert "CPI" in bot.reaction_text(st, "CPI")


# ---------- report UI never crashes ----------
def test_all_tabs_render():
    bot.CTX.clear()
    bot.CTX.update({
        "ticker": {"price": 67000, "change_24h": -1.2},
        "tech": {"1h": {"trend": "BULLISH", "rsi": 55, "adx": 20}, "4h": {"trend": "BULLISH"},
                 "15m": {"trend": "WEAK BEARISH"}, "1d": {"trend": "BULLISH"}, "1w": {"trend": "BULLISH"}},
        "cross": {"DXY": {"price": 100, "change": -0.3}}, "news": [], "events": [],
        "futures": {"funding": 0.0001, "stale": ["funding"]},
        "bias": {"score": 3.0, "bias": "BULLISH", "confidence": 80, "reasons": []},
        "levels": {"price": 67000, "atr": 300, "atr_pct": 0.45, "support": 66000, "resistance": 68000,
                   "bull1": 67300, "bull2": 67600, "bear1": 66700, "bear2": 66400},
        "keylevels": {"vwap": 66800, "day_open": 66500, "prev_high": 67500},
        "glob": {"btc_dom": 57.0, "mcap_change": 1.2}, "options": {"pc_oi": 0.7, "iv": 48},
        "plan": {"action": "WAIT", "why": ["Choppy <market> & more"],
                 "trigger_long": 68000, "trigger_short": 66000}})
    st = bot.default_state()
    for tab in bot.TAB_BUILDERS:
        out = bot.build_tab(tab, st)
        assert out and "Could not build" not in out, tab
    assert "&lt;market&gt;" in bot.build_card(st)       # HTML is escaped
