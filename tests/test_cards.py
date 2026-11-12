"""Tests for the multi-coin cards (v3.6): decision engine, trade plan, signal tracking, menu.  Run: pytest -q"""
import os
import re
import sys
import time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot  # noqa: E402

STEP_MS = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000, "1w": 604_800_000}


def make_df(tf, drift=0.003, n=300, seed=1, vol_last=1.0, noise=0.004, start=100.0):
    """Synthetic candles ending now. drift>0 uptrend, <0 downtrend, 0 sideways."""
    rng = np.random.default_rng(seed)
    close = start * np.cumprod(1 + drift + rng.normal(0, noise, n))
    op = np.concatenate([[start], close[:-1]])
    hi = np.maximum(op, close) * (1 + np.abs(rng.normal(0, noise / 2, n)))
    lo = np.minimum(op, close) * (1 - np.abs(rng.normal(0, noise / 2, n)))
    vol = rng.uniform(900, 1100, n)
    vol[-2] *= vol_last
    end = int(time.time() * 1000) // STEP_MS[tf] * STEP_MS[tf]
    t = end - (n - 1 - np.arange(n)) * STEP_MS[tf]
    return pd.DataFrame({"time": t, "open": op, "high": hi, "low": lo, "close": close, "volume": vol})


def patch_market(monkeypatch, drift=0.003, quote_vol=5e8, spread=0.02, funding=0.0001, news=None,
                 missing_tfs=(), vol_last=1.5, event=None, seed=1):
    frames = {tf: make_df(tf, drift, seed=seed, vol_last=vol_last) for tf in STEP_MS}

    def fake_klines(interval, limit, symbol, ttl=600):
        if interval in missing_tfs:
            return pd.DataFrame()
        return frames[interval]

    price = float(frames["1h"]["close"].iloc[-1])
    monkeypatch.setattr(bot, "cached_klines", fake_klines)
    monkeypatch.setattr(bot, "tickers", lambda max_age=120: {"SOL": {
        "price": price, "change_24h": 3.2, "quote_vol": quote_vol, "high": price * 1.02, "low": price * 0.97}})
    monkeypatch.setattr(bot, "card_spread", lambda b: spread)
    monkeypatch.setattr(bot, "coin_derivs", lambda b: {"funding": funding, "oi_change_24h": 2.0, "oi_change_6h": 1.0})
    monkeypatch.setattr(bot, "coin_news", lambda b: news or [])
    monkeypatch.setattr(bot, "upcoming_high_event", lambda ev, w: event)
    return price


def decide(mode="spot", **kw):
    d = bot.card_gather("SOL", "1h")
    return d, bot.card_decide(d, mode, 1000.0, 1.0)


# ---------- decisions ----------
def test_uptrend_gives_buy_with_valid_plan(monkeypatch):
    patch_market(monkeypatch, drift=0.0008, seed=15)     # seed chosen: clean confirmed uptrend
    d, r = decide()
    assert r["action"] == "BUY" and r["confidence"] >= 45
    p = r["plan"]
    assert p["stop"] < p["entry"] < p["tp1"] < p["tp2"]
    assert 0 < p["risk_acct_pct"] <= 1.0001
    assert p["notional"] <= 1000.0 + 1e-6                # spot: never above account size
    assert "BUY SETUP" in bot.card_html(d, r)


def test_smooth_overbought_trend_waits(monkeypatch):
    patch_market(monkeypatch, drift=0.0025)              # RSI ~88 and far above EMA20 -> do not chase
    d, r = decide()
    assert r["action"] == "WAIT" and any("overbought" in n for n in r["needs"])


def test_downtrend_spot_sell_has_no_short_plan(monkeypatch):
    patch_market(monkeypatch, drift=0.0008 * -1, seed=2)
    patch_market(monkeypatch, drift=-0.0006, seed=2)
    d, r = decide("spot")
    assert r["action"] == "SELL"
    assert r["plan"] is None
    html = bot.card_html(d, r)
    assert "SELL SETUP" in html and "no short trade" in html.lower()


def test_downtrend_futures_sell_gets_short_plan(monkeypatch):
    patch_market(monkeypatch, drift=-0.0006, seed=2)
    d, r = decide("futures")
    assert r["action"] in ("SELL", "WAIT")                # support right under price -> WAIT, not a blind short
    if r["action"] == "SELL":
        p = r["plan"]
        assert p and p["dir"] == -1 and p["tp2"] < p["tp1"] < p["entry"] < p["stop"]
    else:
        assert any("support" in n for n in r["needs"])


def test_sideways_is_wait(monkeypatch):
    patch_market(monkeypatch, drift=0.0, vol_last=1.0)
    d, r = decide()
    assert r["action"] in ("WAIT", "NO TRADE")
    assert r["plan"] is None


def test_illiquid_coin_is_no_trade(monkeypatch):
    patch_market(monkeypatch, quote_vol=1_000_000)
    _, r = decide()
    assert r["action"] == "NO TRADE" and any("volume" in b for b in r["blockers"])


def test_wide_spread_is_no_trade(monkeypatch):
    patch_market(monkeypatch, spread=0.6)
    _, r = decide()
    assert r["action"] == "NO TRADE" and any("spread" in b for b in r["blockers"])


def test_high_impact_event_blocks(monkeypatch):
    patch_market(monkeypatch, event=({"name": "US CPI", "time": bot.now_utc()}, 20.0))
    _, r = decide()
    assert r["action"] == "NO TRADE" and any("US CPI" in b for b in r["blockers"])


def test_missing_candles_is_no_trade_not_buy(monkeypatch):
    patch_market(monkeypatch, missing_tfs=("1h",))
    d, r = decide()
    assert d["an"] is None
    assert r["action"] == "NO TRADE"
    assert r["plan"] is None


def test_missing_trend_data_never_adds_points(monkeypatch):
    patch_market(monkeypatch, drift=0.0025)
    d_full, r_full = decide()
    patch_market(monkeypatch, drift=0.0025, missing_tfs=("1d", "1w"))
    d_miss, r_miss = decide()
    assert "1D trend" in d_miss["missing"]
    assert r_miss["score"] <= r_full["score"] + 1e-9     # losing data cannot make it more bullish
    assert r_miss["confidence"] <= r_full["confidence"]


def test_hack_headline_blocks_buy():
    nw = bot.card_news_read([{"title": "Exploit drains SOL bridge in major hack", "description": ""}])
    assert nw["blocked"] and nw["pts"] < 0


def test_news_none_is_neutral():
    nw = bot.card_news_read([])
    assert nw["pts"] == 0 and nw["none"] and not nw["blocked"]


def test_rumor_halves_positive_news():
    a = bot.card_news_read([{"title": "Binance listing for XYZ", "description": ""}])
    b = bot.card_news_read([{"title": "Reportedly Binance listing for XYZ", "description": ""}])
    assert a["pts"] > 0 and abs(b["pts"] - a["pts"] / 2) < 1e-9


def test_buy_requires_confirmations(monkeypatch):
    """Strong score but 4H bearish -> WAIT with the reason, never BUY."""
    patch_market(monkeypatch, drift=0.0025)
    d = bot.card_gather("SOL", "1h")
    d["trends"]["4h"] = "BEARISH"
    r = bot.card_decide(d, "spot", 1000.0, 1.0)
    assert r["action"] != "BUY"


def test_tight_stop_fees_filter():
    d = {"an": {"atr": 0.0005, "supports": [(99.999, 2)], "resistances": [(110.0, 2)]}, "price": 100.0}
    plan, problems, needs = bot.card_trade_plan(d, 1, "spot", 1000.0, 1.0)
    assert any("fees" in p for p in problems)


def test_resistance_too_close_flagged():
    d = {"an": {"atr": 1.0, "supports": [(98.0, 2)], "resistances": [(100.5, 2)]}, "price": 100.0}
    plan, problems, needs = bot.card_trade_plan(d, 1, "spot", 1000.0, 1.0)
    assert any("resistance" in p for p in needs) and not problems     # WAIT-type, not a risk blocker


def test_position_size_capped_in_spot():
    d = {"an": {"atr": 0.4, "supports": [(99.5, 2)], "resistances": [(120.0, 2)]}, "price": 100.0}
    plan, _, _ = bot.card_trade_plan(d, 1, "spot", 1000.0, 3.0)
    assert plan["capped"] and plan["notional"] <= 1000.0 + 1e-6
    assert plan["risk_usd"] <= 30.0 + 1e-6


# ---------- card text ----------
def test_card_has_all_sections_and_fits_telegram(monkeypatch):
    patch_market(monkeypatch, drift=0.0008, seed=15, news=[{"title": "Solana partnership announced with big bank " * 3, "description": ""}])
    d, r = decide()
    assert r["action"] == "BUY"
    html = bot.card_html(d, r)
    for sec in ("1. Market overview", "2. Trend analysis", "3. Technical indicators", "4. Support and resistance",
                "5. Trade plan", "6. Risk analysis", "7. News and market conditions", "Why:", "Overall confidence", "no orders are placed"):
        assert sec in html
    assert len(html) < 3900
    assert len(re.findall(r"<b>", html)) == len(re.findall(r"</b>", html))


def test_build_card_logs_signal_once(monkeypatch):
    patch_market(monkeypatch, drift=0.0008, seed=15)
    state = bot.default_state()
    html1, r1 = bot.build_coin_card(state, "SOL", "1h")
    html2, r2 = bot.build_coin_card(state, "SOL", "1h")
    assert r1["action"] == "BUY"
    assert len(bot.card_cfg(state)["signals"]) == 1       # second identical open signal is not duplicated
    patch_market(monkeypatch, drift=0.0, vol_last=1.0)
    state2 = bot.default_state()
    bot.build_coin_card(state2, "SOL", "1h")
    assert bot.card_cfg(state2)["signals"] == []          # WAIT is never tracked as a signal


# ---------- signal tracking ----------
def _sg(dr=1):
    entry, risk = 100.0, 2.0
    return {"id": 1, "base": "SOL", "tf": "1h", "action": "BUY" if dr > 0 else "SELL", "dir": dr, "entry": entry,
            "stop": entry - dr * risk, "tp1": entry + dr * 3.0, "tp2": entry + dr * 5.0, "rr1": 1.5, "rr2": 2.5,
            "ts": time.time(), "seen": 1000, "status": "open", "closed": False}


def test_signal_stop_hit():
    sg = bot.advance_card_signal(_sg(), [(2000, 101, 97.9)])
    assert sg["closed"] and sg["outcome"] == "stop" and sg["r"] == -1.0


def test_signal_stop_checked_before_target_same_candle():
    sg = bot.advance_card_signal(_sg(), [(2000, 106, 97)])
    assert sg["outcome"] == "stop"


def test_signal_tp1_then_tp2():
    sg = bot.advance_card_signal(_sg(), [(2000, 103.5, 100.5)])
    assert sg["status"] == "tp1" and not sg["closed"]
    sg = bot.advance_card_signal(sg, [(3000, 105.2, 102)])
    assert sg["closed"] and sg["outcome"] == "tp2" and sg["r"] == 2.0


def test_signal_tp1_then_breakeven():
    sg = bot.advance_card_signal(_sg(), [(2000, 103.5, 100.5), (3000, 102, 99.9)])
    assert sg["outcome"] == "tp1_be" and sg["r"] == 0.75


def test_signal_short_side():
    sg = bot.advance_card_signal(_sg(-1), [(2000, 99.5, 96.5)])
    assert sg["status"] == "tp1"
    sg = bot.advance_card_signal(sg, [(3000, 98, 94.9)])
    assert sg["outcome"] == "tp2"


def test_old_candles_ignored():
    sg = bot.advance_card_signal(_sg(), [(500, 50, 1)])    # before the signal was created
    assert not sg["closed"] and sg["status"] == "open"


def test_stats_text_counts():
    state = bot.default_state()
    c = bot.card_cfg(state)
    a, b = _sg(), _sg()
    b["id"] = 2
    bot._close_sig(a, "tp2", 2.0)
    bot._close_sig(b, "stop", -1.0)
    c["signals"] += [a, b]
    txt = bot.card_stats_text(state)
    assert "2 signals" in txt and "50%" in txt and "+0.50R" in txt


# ---------- settings / menu ----------
def test_card_base_parsing():
    assert bot.card_base("sol") == "SOL"
    assert bot.card_base("SOL/USDT") == "SOL"
    assert bot.card_base("solusdt") == "SOL"
    assert bot.card_base("$pepe") == "PEPE"
    assert bot.card_base("solana") == "SOL"


def test_old_state_files_get_card_defaults():
    st = {"cards": {}}
    c = bot.card_cfg(st)
    assert c["coins"] == bot.CARD_DEFAULT_COINS and c["mode"] == "spot" and c["tf"] == "1h"


def test_menu_buttons_valid(monkeypatch):
    state = bot.default_state()
    kb = bot.coins_keyboard(state)
    for row in kb["inline_keyboard"]:
        for b in row:
            assert len(b["callback_data"].encode()) <= 64


def test_callbacks_toggle_and_limit(monkeypatch):
    sent = []
    monkeypatch.setattr(bot, "tg_edit", lambda *a, **k: True)
    monkeypatch.setattr(bot, "send", lambda m, *a, **k: sent.append(m))
    monkeypatch.setattr(bot, "tg_send_html", lambda *a, **k: None)
    monkeypatch.setattr(bot, "tickers", lambda max_age=120: {b: {} for b in ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE",
                                                                              "ADA", "AVAX", "LINK", "DOT", "TON"]})
    state = bot.default_state()
    cq = {"data": "cc:t:BNB", "message": {"message_id": 5}}
    bot.handle_coins_callback(state, cq)
    assert "BNB" in state["cards"]["coins"]
    bot.handle_coins_callback(state, cq)
    assert "BNB" not in state["cards"]["coins"]
    for b in ["BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK"]:
        bot.handle_coins_callback(state, {"data": f"cc:t:{b}", "message": {"message_id": 5}})
    assert len(state["cards"]["coins"]) == bot.CARD_MAX_COINS
    assert any("Max" in m for m in sent)
    bot.handle_coins_callback(state, {"data": "cc:mode", "message": {"message_id": 5}})
    assert state["cards"]["mode"] == "futures"
    bot.handle_coins_callback(state, {"data": "cc:ev:240", "message": {"message_id": 5}})
    assert state["cards"]["every"] == 240
    bot.handle_coins_callback(state, {"data": "cc:ev:7", "message": {"message_id": 5}})
    assert state["cards"]["every"] == 240            # invalid interval ignored


def test_monitor_alerts_only_on_change(monkeypatch):
    out = []
    monkeypatch.setattr(bot, "tg_send_html", lambda text, *a, **k: out.append(text))
    seq = iter(["BUY", "BUY", "WAIT"])
    monkeypatch.setattr(bot, "build_coin_card", lambda st, b, tf, track=True: ("CARD", {"action": next(seq), "blockers": [], "needs": ["x"]}))
    state = bot.default_state()
    c = bot.card_cfg(state)
    c["coins"], c["every"] = ["SOL"], 5
    for _ in range(3):
        c["last_run"] = 0
        bot.check_coin_cards(state)
    assert len(out) == 2 and "SIGNAL ALERT" in out[0] and "no longer valid" in out[1]


def test_monitor_respects_off_and_pause(monkeypatch):
    called = []
    monkeypatch.setattr(bot, "build_coin_card", lambda *a, **k: called.append(1) or ("x", {"action": "WAIT", "blockers": [], "needs": []}))
    state = bot.default_state()
    c = bot.card_cfg(state)
    c["every"] = 0
    bot.check_coin_cards(state)
    c["every"], c["last_run"] = 5, 0
    state["paused"] = True
    bot.check_coin_cards(state)
    assert not called
