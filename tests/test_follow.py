"""Tests for /follow: the main report, signals, live trade and alerts follow a chosen coin (not only BTC)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot  # noqa: E402


def fresh(monkeypatch):
    bot.FOCUS.update(base="BTC", epoch=0)
    monkeypatch.setattr(bot, "tickers", lambda max_age=120: {b: {} for b in ("BTC", "ETH", "SOL", "PEPE")})
    return bot.default_state()


def test_default_is_btc(monkeypatch):
    fresh(monkeypatch)
    assert bot.focus_base() == "BTC" and bot.focus_symbol() == "BTCUSDT" and bot.focus_name() == "Bitcoin"


def test_switch_to_sol_clears_old_coin_data(monkeypatch):
    st = fresh(monkeypatch)
    bot.CANDLES["1h"], bot.TECH_CACHE["1h"], bot.CTX["ticker"] = "old", "old", {"price": 1}
    st["oi_history"], st["last_report"] = [[1, 2, "x"]], 12345
    ok, msg = bot.set_focus(st, "sol")
    assert ok and "SOL" in msg
    assert bot.focus_base() == "SOL" and st["focus"] == "SOL" and bot.FOCUS["epoch"] == 1
    assert not bot.CANDLES and not bot.TECH_CACHE and "ticker" not in bot.CTX
    assert st["oi_history"] == [] and st["last_report"] == 0
    bot.FOCUS.update(base="BTC", epoch=0)


def test_switch_refused_while_trade_open(monkeypatch):
    st = fresh(monkeypatch)
    st["active_trade"] = {"id": "1", "status": "OPEN"}
    ok, msg = bot.set_focus(st, "SOL")
    assert not ok and "still open" in msg and bot.focus_base() == "BTC"


def test_unknown_coin_rejected(monkeypatch):
    st = fresh(monkeypatch)
    ok, msg = bot.set_focus(st, "NOTACOIN")
    assert not ok and bot.focus_base() == "BTC"


def test_klines_default_follows_focus(monkeypatch):
    st = fresh(monkeypatch)
    seen = []
    monkeypatch.setattr(bot, "http_json", lambda url, params=None, **k: seen.append((params or {}).get("symbol") or (params or {}).get("instId")) or None)

    def used(fn, *a):
        seen.clear()
        fn("1h", 10, *a)
        return set(seen)

    assert used(bot._klines_binance) == {"BTCUSDT"}
    bot.set_focus(st, "SOL")
    assert used(bot._klines_binance) == {"SOLUSDT"}
    assert used(bot._klines_okx) == {"SOL-USDT"}
    assert used(bot._klines_binance, "ETHUSDT") == {"ETHUSDT"}      # explicit symbol still wins
    bot.FOCUS.update(base="BTC", epoch=0)


def test_news_feeds_and_options_and_liquidations_for_other_coin(monkeypatch):
    st = fresh(monkeypatch)
    assert bot.news_feeds() is bot.NEWS_FEEDS
    bot.set_focus(st, "SOL")
    feeds = bot.news_feeds()
    assert "Solana" in feeds["SOL"] and "Macro" in feeds
    assert bot.get_options_context() is None                       # Deribit options are BTC-only
    monkeypatch.setattr(bot, "http_json", lambda *a, **k: None)
    monkeypatch.setattr(bot, "liq_from_ws", lambda: (999.0, 999.0))   # BTC liquidation numbers must NOT leak
    fut = bot.get_futures_context(bot.default_state())
    assert fut["long_liquidation"] is None and fut["short_liquidation"] is None
    bot.FOCUS.update(base="BTC", epoch=0)


def test_futures_requests_use_followed_coin(monkeypatch):
    st = fresh(monkeypatch)
    bot.set_focus(st, "SOL")
    urls = []
    monkeypatch.setattr(bot, "http_json", lambda url, params=None, **k: urls.append((url, params)) or None)
    bot.get_futures_context(bot.default_state())
    flat = str(urls)
    assert "SOLUSDT" in flat and "SOL-USDT-SWAP" in flat and "BTC" not in flat
    bot.FOCUS.update(base="BTC", epoch=0)


def test_alert_defaults_to_followed_coin(monkeypatch):
    st = fresh(monkeypatch)
    bot.set_focus(st, "SOL")
    sent = []
    monkeypatch.setattr(bot, "send", lambda m, *a, **k: sent.append(m))
    bot.CTX["ticker"] = {"price": 100.0}
    bot.cmd_alert(st, ["120"])
    assert st["alerts"][-1]["sym"] == "SOL" and "SOL" in sent[-1]
    bot.FOCUS.update(base="BTC", epoch=0)
    bot.CTX.pop("ticker", None)


def test_follow_menu_and_callback(monkeypatch):
    st = fresh(monkeypatch)
    out = []
    monkeypatch.setattr(bot, "tg_send_html", lambda text, kb=None, **k: out.append((text, kb)))
    monkeypatch.setattr(bot, "send", lambda m, *a, **k: out.append((m, None)))
    bot.cmd_follow(st, [])
    text, kb = out[-1]
    assert "BTC" in text
    for row in kb["inline_keyboard"]:
        for b in row:
            assert len(b["callback_data"].encode()) <= 64
    bot.handle_coins_callback(st, {"data": "cc:follow:ETH", "message": {"message_id": 1}})
    assert bot.focus_base() == "ETH"
    bot.FOCUS.update(base="BTC", epoch=0)


def test_new_trade_records_coin(monkeypatch):
    st = fresh(monkeypatch)
    bot.set_focus(st, "SOL")
    plan = {"action": "LONG", "entry": 100, "stop": 98, "tp1": 103, "tp2": 105, "risk": 2, "rr1": 1.5, "rr2": 2.5}
    assert bot.new_trade(plan, 1000)["coin"] == "SOL"
    bot.FOCUS.update(base="BTC", epoch=0)
