import os
import sys
import io
import csv
import json
import math
import time
import hashlib
import signal
import threading
import traceback
import xml.etree.ElementTree as ET
from collections import deque
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests


# ============================================================
# BTC MARKET INTELLIGENCE TELEGRAM BOT  (v3)
# ------------------------------------------------------------
#  1. Signal tracking: every LONG/SHORT is logged and followed
#     until it hits stop / TP1 / TP2; win-rate + R stats
#  2. Backtest:  python btc_market_bot.py --backtest 180
#  3. Better signals: ADX range filter, volume filter, cooldown
#     after a loss, limit-pullback entries, volatility sizing
#  4. Trade management messages (TP1 -> breakeven, trailing, ...)
#  5. More data: Fear & Greed, taker ratio, long/short ratio,
#     live liquidation websocket
#  6. Smarter news: de-duplicated breaking alerts + optional
#     Claude summary (ANTHROPIC_API_KEY)
#  7. Telegram commands/buttons/charts, crash alerts, health
#     checks, atomic state saves
#
#  Signals are informational only. NO ORDERS ARE PLACED.
# ============================================================


# ============================================================
# CONFIGURATION
# ============================================================

TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
STATE_FILE = os.environ.get("STATE_FILE", "btc_market_state.json")
TRADE_LOG_CSV = os.environ.get("TRADE_LOG_CSV", "btc_trade_log.csv")

# Optional: Claude-powered news summary / breaking-news filter
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")

# "short" = compact summary + [Details] [Chart] buttons, "full" = everything
REPORT_MODE = os.environ.get("REPORT_MODE", "short")
CHART_WITH_REPORT = os.environ.get("CHART_WITH_REPORT", "0") == "1"
ENABLE_LIQ_WS = os.environ.get("LIQ_WS", "1") == "1"

BTC_SYMBOL = "BTCUSDT"
REPORT_INTERVAL = 30 * 60
DATA_POLL_INTERVAL = 60
SLOW_POLL_INTERVAL = 5 * 60
CALENDAR_POLL_INTERVAL = 15 * 60
RUN_SECONDS = int(os.environ.get("RUN_SECONDS", str(350 * 60)))

EVENT_ALERT_WINDOWS = (60, 15, 5)
MARKET_OPEN_WINDOWS = (30, 5, 0)
NO_TRADE_BEFORE_EVENT_MIN = 45
MAX_NEWS_ITEMS = 6

# ---- trade model -------------------------------------------------
ACCOUNT_DEFAULT = 1000.0       # USD, change with /account
RISK_PCT_DEFAULT = 1.0         # % of account per trade, change with /risk
STOP_ATR = 1.2                 # stop distance = 1.2 x 1h ATR
PULLBACK_ATR = 0.25            # limit entry waits for a 0.25 ATR pullback
RR1, RR2 = 1.5, 2.5            # take-profit multiples of risk
TP1_FRACTION = 0.5             # close 50% at TP1
TRAIL_R = 1.5                  # trailing stop distance after TP1 (in R)
EXPIRE_MIN = 240               # cancel unfilled limit after 4h
COOLDOWN_MIN = 120             # pause new signals after a full stop-out
REENTRY_MIN = 30               # minimum gap after ANY trade closes/expires
FEE_RATE = 0.0004              # 0.04% per side (taker) used in R results
ADX_MIN = 18.0                 # below this the market is "ranging"
VOLUME_MIN_RATIO = 0.9         # need at least ~average volume

# ---- data sources ------------------------------------------------
BINANCE_SPOT_HOSTS = [
    "https://data-api.binance.vision",
    "https://api.binance.com",
    "https://api1.binance.com",
]
BINANCE_FUTURES = "https://fapi.binance.com"
OKX = "https://www.okx.com"
BYBIT = "https://api.bybit.com"

FF_CALENDAR_URLS = [
    "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
    "https://nfs.faireconomy.media/ff_calendar_nextweek.json",
]
CALENDAR_API = "https://www.financecalendar.com/wp-json/fc/v1"

NEWS_FEEDS = {
    "Bitcoin": "https://news.google.com/rss/search?q=Bitcoin%20when%3A2h&hl=en-US&gl=US&ceid=US%3Aen",
    "Crypto": "https://news.google.com/rss/search?q=crypto%20Bitcoin%20when%3A2h&hl=en-US&gl=US&ceid=US%3Aen",
    "Macro": "https://news.google.com/rss/search?q=Federal%20Reserve%20CPI%20jobs%20Treasury%20Bitcoin%20when%3A6h&hl=en-US&gl=US&ceid=US%3Aen",
}

YAHOO_SYMBOLS = {
    "DXY": "DX-Y.NYB",
    "NASDAQ": "NQ=F",
    "US10Y": "^TNX",
    "VIX": "^VIX",
    "GOLD": "GC=F",
}
YAHOO_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
}

NY_TZ = ZoneInfo("America/New_York")
UTC = timezone.utc

BULL_TRENDS = ("BULLISH", "WEAK BULLISH")
BEAR_TRENDS = ("BEARISH", "WEAK BEARISH")

session = requests.Session()
session.headers.update({
    "User-Agent": "BTC-Market-Intelligence-Bot/3.0",
    "Accept": "*/*",
})

# ---- runtime globals --------------------------------------------
STOP_FLAG = {"stop": False}
TECH_CACHE, FUT_CACHE, CROSS_CACHE = {}, {}, {}
CANDLES = {}                       # latest dataframes per timeframe (for charts/tracking)
CTX = {}                           # latest everything (for commands/buttons)
HEALTH = {"price": time.time()}    # last time price data was OK
NEWS_AI_CACHE = {"sig": None, "text": None}
FNG_CACHE = {"ts": 0, "data": None}

LIQ_EVENTS = deque(maxlen=5000)
LIQ_LOCK = threading.Lock()
WS_STATE = {"connected": False, "last_msg": 0.0, "source": ""}


# ============================================================
# HELPERS
# ============================================================

def safe_float(value, default=None):
    try:
        if value is None or value == "":
            return default
        v = float(str(value).replace(",", "").replace("%", "").strip())
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except Exception:
        return default


def parse_number(value):
    """Parses 160K, 3.2%, 1.5M, -0.3 ..."""
    if value is None:
        return None
    s = str(value).strip().replace(",", "").replace("%", "")
    if not s:
        return None
    mult = 1.0
    if s[-1] in "KMBTkmbt":
        mult = {"k": 1e3, "m": 1e6, "b": 1e9, "t": 1e12}[s[-1].lower()]
        s = s[:-1]
    try:
        return float(s) * mult
    except Exception:
        return None


def fmt_price(value):
    return "unavailable" if value is None else f"${value:,.2f}"


def fmt_pct(value, decimals=2):
    return "unavailable" if value is None else f"{value:+.{decimals}f}%"


def now_utc():
    return datetime.now(UTC)


def utc_text(dt=None):
    return (dt or now_utc()).strftime("%Y-%m-%d %H:%M UTC")


def parse_iso(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except Exception:
        return None


def pct_change(new, old):
    if new is None or old in (None, 0):
        return None
    return (new - old) / old * 100


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def clean_text(text):
    return " ".join(str(text or "").split())


def http_json(url, params=None, headers=None, timeout=12, retries=1):
    """GET json with one retry. Geo-blocks / auth errors are not retried."""
    for attempt in range(retries + 1):
        try:
            r = session.get(url, params=params or {}, headers=headers, timeout=timeout)
            if r.status_code in (400, 401, 403, 404, 451):
                print("HTTP", r.status_code, url[:90])
                return None
            if r.status_code == 429:
                time.sleep(min(float(r.headers.get("Retry-After", 2)), 5))
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            print("HTTP error:", url[:90], str(e)[:100])
            if attempt < retries:
                time.sleep(0.8 * (attempt + 1))
    return None


# ============================================================
# TELEGRAM
# ============================================================

def tg_post(method, data=None, files=None, timeout=25):
    if not TOKEN:
        return None
    for attempt in range(3):
        try:
            r = session.post(
                f"https://api.telegram.org/bot{TOKEN}/{method}",
                data=data, files=files, timeout=timeout,
            )
            if r.status_code == 429:
                wait = 3
                try:
                    wait = r.json().get("parameters", {}).get("retry_after", 3)
                except Exception:
                    pass
                time.sleep(min(wait, 30))
                continue
            if r.ok:
                return r.json()
            print("Telegram error:", method, r.status_code, r.text[:300])
            if r.status_code < 500:
                return None
        except Exception:
            traceback.print_exc()
        time.sleep(1.5 * (attempt + 1))
    return None


def split_message(msg, limit=3900):
    if len(msg) <= limit:
        return [msg]
    chunks, cur = [], ""
    for line in msg.split("\n"):
        if len(cur) + len(line) + 1 > limit:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur)
    return chunks


def send(msg, reply_markup=None):
    if not TOKEN or not CHAT_ID:
        print("[Telegram not configured] ->\n" + msg + "\n")
        return False

    ok = True
    chunks = split_message(msg)
    for i, chunk in enumerate(chunks):
        data = {"chat_id": CHAT_ID, "text": chunk, "disable_web_page_preview": "true"}
        if reply_markup and i == len(chunks) - 1:
            data["reply_markup"] = json.dumps(reply_markup)
        res = tg_post("sendMessage", data)
        if not res or not res.get("ok"):
            ok = False
    return ok


def send_photo(png_bytes, caption=""):
    if not TOKEN or not CHAT_ID or not png_bytes:
        return False
    res = tg_post(
        "sendPhoto",
        data={"chat_id": CHAT_ID, "caption": caption[:1000]},
        files={"photo": ("chart.png", png_bytes, "image/png")},
        timeout=40,
    )
    return bool(res and res.get("ok"))


REPORT_BUTTONS = {"inline_keyboard": [[
    {"text": "📖 Full details", "callback_data": "details"},
    {"text": "📈 Chart", "callback_data": "chart"},
]]}


# ============================================================
# PERSISTENT STATE  (atomic writes)
# ============================================================

def default_state():
    return {
        "last_report": 0,
        "last_news_hashes": [],
        "reported_news": [],
        "breaking_times": [],
        "event_alerts": {},
        "market_open_alerts": {},
        "last_actual_events": {},
        "oi_history": [],
        "active_trade": None,
        "trades": [],
        "cooldown_until": 0,
        "paused": False,
        "account": ACCOUNT_DEFAULT,
        "risk_pct": RISK_PCT_DEFAULT,
        "tg_offset": 0,
        "last_daily": "",
        "health_alert": False,
        "started_at": time.time(),
    }


def load_state():
    state = default_state()
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state.update(json.load(f))
            print("State loaded:", STATE_FILE)
    except Exception:
        traceback.print_exc()
        print("Starting with fresh state.")
    return state


def save_state(state):
    try:
        state["last_news_hashes"] = state["last_news_hashes"][-400:]
        state["reported_news"] = state["reported_news"][-100:]
        state["breaking_times"] = [t for t in state["breaking_times"] if time.time() - t < 3600]
        state["oi_history"] = state["oi_history"][-400:]
        state["trades"] = state["trades"][-500:]
        for k in ("event_alerts", "market_open_alerts", "last_actual_events"):
            if len(state[k]) > 500:
                state[k] = dict(list(state[k].items())[-300:])

        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, STATE_FILE)  # atomic: never leaves a half-written file
    except Exception:
        traceback.print_exc()


# ============================================================
# PRICE DATA  (Binance -> OKX -> Coinbase)
# ============================================================

OKX_BAR = {"5m": "5m", "15m": "15m", "1h": "1H", "4h": "4H"}
KLINE_COLS = ["time", "open", "high", "low", "close", "volume"]


def _klines_binance(interval, limit):
    for base in BINANCE_SPOT_HOSTS:
        data = http_json(base + "/api/v3/klines",
                         {"symbol": BTC_SYMBOL, "interval": interval, "limit": limit})
        if isinstance(data, list) and data:
            return pd.DataFrame([row[:6] for row in data], columns=KLINE_COLS)
    return pd.DataFrame()


def _klines_okx(interval, limit):
    data = http_json(OKX + "/api/v5/market/candles",
                     {"instId": "BTC-USDT", "bar": OKX_BAR[interval],
                      "limit": min(limit, 300)})
    rows = (data or {}).get("data") or []
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([r[:6] for r in rows], columns=KLINE_COLS)
    return df.iloc[::-1].reset_index(drop=True)


def get_klines(interval="5m", limit=500):
    df = _klines_binance(interval, limit)
    if df.empty:
        df = _klines_okx(interval, limit)
    if df.empty:
        return df
    for c in ["open", "high", "low", "close", "volume", "time"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(subset=["close"]).reset_index(drop=True)


def get_btc_ticker():
    for base in BINANCE_SPOT_HOSTS:
        d = http_json(base + "/api/v3/ticker/24hr", {"symbol": BTC_SYMBOL})
        if isinstance(d, dict) and d.get("lastPrice"):
            return {"price": safe_float(d.get("lastPrice")),
                    "change_24h": safe_float(d.get("priceChangePercent")),
                    "high_24h": safe_float(d.get("highPrice")),
                    "low_24h": safe_float(d.get("lowPrice"))}

    d = http_json(OKX + "/api/v5/market/ticker", {"instId": "BTC-USDT"})
    try:
        t = d["data"][0]
        last = safe_float(t.get("last"))
        return {"price": last,
                "change_24h": pct_change(last, safe_float(t.get("open24h"))),
                "high_24h": safe_float(t.get("high24h")),
                "low_24h": safe_float(t.get("low24h"))}
    except Exception:
        pass

    d = http_json("https://api.coinbase.com/v2/prices/BTC-USD/spot")
    try:
        return {"price": safe_float(d["data"]["amount"]),
                "change_24h": None, "high_24h": None, "low_24h": None}
    except Exception:
        return {}


# ============================================================
# TECHNICAL INDICATORS
# ============================================================

def add_indicators(df):
    if df.empty:
        return df

    df = df.copy()
    for span in (9, 21, 50, 200):
        df[f"ema{span}"] = df["close"].ewm(span=span, adjust=False).mean()

    delta = df["close"].diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))

    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"] - df["close"].shift()).abs(),
    ], axis=1).max(axis=1)

    atr = tr.ewm(alpha=1 / 14, adjust=False).mean()
    df["atr"] = atr
    df["atr_pct"] = atr / df["close"] * 100

    # ADX (Wilder) - measures trend STRENGTH. Low = sideways market.
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)
    plus_di = 100 * plus_dm.ewm(alpha=1 / 14, adjust=False).mean() / atr.replace(0, np.nan)
    minus_di = 100 * minus_dm.ewm(alpha=1 / 14, adjust=False).mean() / atr.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    df["adx"] = dx.ewm(alpha=1 / 14, adjust=False).mean()

    # Bollinger band width (%)
    ma20 = df["close"].rolling(20).mean()
    sd20 = df["close"].rolling(20).std()
    df["bb_width"] = (4 * sd20) / ma20 * 100

    df["vol_avg"] = df["volume"].rolling(20).mean()
    df["volume_ratio"] = df["volume"] / df["vol_avg"].replace(0, np.nan)

    df["support_50"] = df["low"].rolling(50).min()
    df["resistance_50"] = df["high"].rolling(50).max()
    return df


def trend_from_row(row):
    try:
        price = row["close"]
        if price > row["ema9"] > row["ema21"] > row["ema50"]:
            return "BULLISH"
        if price < row["ema9"] < row["ema21"] < row["ema50"]:
            return "BEARISH"
        if price > row["ema21"] and price > row["ema50"]:
            return "WEAK BULLISH"
        if price < row["ema21"] and price < row["ema50"]:
            return "WEAK BEARISH"
    except Exception:
        pass
    return "NEUTRAL"


def snapshot(row):
    """One timeframe's indicator values as a plain dict (live + backtest)."""
    return {
        "price": safe_float(row["close"]),
        "trend": trend_from_row(row),
        "rsi": safe_float(row["rsi"]),
        "atr": safe_float(row["atr"]),
        "atr_pct": safe_float(row["atr_pct"]),
        "adx": safe_float(row["adx"]),
        "bb_width": safe_float(row["bb_width"]),
        "volume_ratio": safe_float(row["volume_ratio"]),
        "ema50": safe_float(row["ema50"]),
        "ema200": safe_float(row["ema200"]),
        "support": safe_float(row["support_50"]),
        "resistance": safe_float(row["resistance_50"]),
    }


def build_technical_context():
    result = {}
    for tf, limit in [("5m", 300), ("15m", 300), ("1h", 300), ("4h", 250)]:
        df = add_indicators(get_klines(tf, limit))
        if len(df) < 80:
            result[tf] = TECH_CACHE.get(tf, {})
            continue
        CANDLES[tf] = df
        data = snapshot(df.iloc[-2])  # last CLOSED candle
        TECH_CACHE[tf] = data
        result[tf] = data

    if (result.get("1h") or {}).get("price") is not None:
        HEALTH["price"] = time.time()
    return result


# ============================================================
# LIQUIDATIONS  (websocket live feed + REST fallback)
# ============================================================

def _liq_add(liquidated_side, usd):
    if usd and usd > 0:
        with LIQ_LOCK:
            LIQ_EVENTS.append((time.time(), liquidated_side, usd))


def liq_ws_worker():
    try:
        import websocket  # pip install websocket-client
    except ImportError:
        print("websocket-client not installed -> liquidations use REST fallback.")
        return

    sources = [
        ("binance", "wss://fstream.binance.com/ws/btcusdt@forceOrder"),
        ("okx", "wss://ws.okx.com:8443/ws/v5/public"),
    ]
    idx = 0

    while not STOP_FLAG["stop"]:
        name, url = sources[idx % len(sources)]

        def on_open(ws, name=name):
            WS_STATE.update(connected=True, source=name, last_msg=time.time())
            if name == "okx":
                ws.send(json.dumps({"op": "subscribe", "args": [
                    {"channel": "liquidation-orders", "instType": "SWAP"}]}))

        def on_message(ws, message, name=name):
            WS_STATE["last_msg"] = time.time()
            try:
                msg = json.loads(message)
                if name == "binance":
                    o = msg.get("o") or {}
                    px = safe_float(o.get("ap")) or safe_float(o.get("p")) or 0
                    qty = safe_float(o.get("z")) or safe_float(o.get("q")) or 0
                    # SELL order = a LONG position got liquidated
                    _liq_add("long" if o.get("S") == "SELL" else "short", px * qty)
                else:
                    for item in msg.get("data", []):
                        inst = item.get("instId", "")
                        if not inst.startswith("BTC-"):
                            continue
                        for d in item.get("details", []):
                            px = safe_float(d.get("bkPx"), 0) or 0
                            sz = safe_float(d.get("sz"), 0) or 0
                            usd = sz * 0.01 * px if inst.endswith("USDT-SWAP") else sz * 100
                            pos = d.get("posSide")
                            long_liq = pos == "long" or (pos in (None, "", "net") and d.get("side") == "sell")
                            _liq_add("long" if long_liq else "short", usd)
            except Exception:
                pass

        def on_close(ws, *a):
            WS_STATE["connected"] = False

        def on_error(ws, err):
            WS_STATE["connected"] = False

        started = time.time()
        try:
            app = websocket.WebSocketApp(url, on_open=on_open, on_message=on_message,
                                         on_close=on_close, on_error=on_error)
            app.run_forever(ping_interval=20, ping_timeout=10)
        except Exception:
            pass
        WS_STATE["connected"] = False
        if time.time() - started < 20:
            idx += 1  # this source is not working -> try the other one
        time.sleep(5)


def start_liq_ws():
    if ENABLE_LIQ_WS:
        threading.Thread(target=liq_ws_worker, daemon=True).start()


def liq_from_ws():
    if not WS_STATE["connected"]:
        return None, None
    cutoff = time.time() - 3600
    lng = sht = 0.0
    with LIQ_LOCK:
        for ts, side, usd in LIQ_EVENTS:
            if ts >= cutoff:
                if side == "long":
                    lng += usd
                else:
                    sht += usd
    return lng, sht


def _okx_liquidations():
    d = http_json(OKX + "/api/v5/public/liquidation-orders",
                  {"instType": "SWAP", "uly": "BTC-USDT", "state": "filled"})
    try:
        cutoff = int(time.time() * 1000) - 3600 * 1000
        lng = sht = 0.0
        for block in d["data"]:
            for det in block.get("details", []):
                if safe_float(det.get("ts"), 0) < cutoff:
                    continue
                usd = (safe_float(det.get("sz"), 0) or 0) * 0.01 * (safe_float(det.get("bkPx"), 0) or 0)
                pos = det.get("posSide")
                if pos == "long" or (pos in (None, "", "net") and det.get("side") == "sell"):
                    lng += usd
                else:
                    sht += usd
        return lng, sht
    except Exception:
        return None, None


# ============================================================
# FUTURES + CROWD CONTEXT
# ============================================================

def get_futures_context(state):
    res = {"funding": None, "open_interest": None, "oi_change_pct": None,
           "long_liquidation": None, "short_liquidation": None}

    p = http_json(BINANCE_FUTURES + "/fapi/v1/premiumIndex", {"symbol": BTC_SYMBOL})
    if isinstance(p, dict):
        res["funding"] = safe_float(p.get("lastFundingRate"))
    o = http_json(BINANCE_FUTURES + "/fapi/v1/openInterest", {"symbol": BTC_SYMBOL})
    if isinstance(o, dict):
        res["open_interest"] = safe_float(o.get("openInterest"))

    if res["funding"] is None or res["open_interest"] is None:
        b = http_json(BYBIT + "/v5/market/tickers", {"category": "linear", "symbol": BTC_SYMBOL})
        try:
            item = b["result"]["list"][0]
            if res["funding"] is None:
                res["funding"] = safe_float(item.get("fundingRate"))
            if res["open_interest"] is None:
                res["open_interest"] = safe_float(item.get("openInterest"))
        except Exception:
            pass

    if res["funding"] is None:
        f = http_json(OKX + "/api/v5/public/funding-rate", {"instId": "BTC-USDT-SWAP"})
        try:
            res["funding"] = safe_float(f["data"][0].get("fundingRate"))
        except Exception:
            pass
    if res["open_interest"] is None:
        oi = http_json(OKX + "/api/v5/public/open-interest",
                       {"instType": "SWAP", "instId": "BTC-USDT-SWAP"})
        try:
            res["open_interest"] = safe_float(oi["data"][0].get("oiCcy"))
        except Exception:
            pass

    now = time.time()
    if res["open_interest"] is not None:
        hist = state.setdefault("oi_history", [])
        hist.append([now, res["open_interest"]])
        state["oi_history"] = hist = [h for h in hist if now - h[0] < 3 * 3600]
        older = [h for h in hist if h[0] <= now - 5 * 60]
        if older:
            ref = min(older, key=lambda h: abs(h[0] - (now - 30 * 60)))
            res["oi_change_pct"] = pct_change(res["open_interest"], ref[1])
            res["oi_window_min"] = int((now - ref[0]) / 60)

    lng, sht = liq_from_ws()
    if lng is None:
        lng, sht = _okx_liquidations()
    res["long_liquidation"], res["short_liquidation"] = lng, sht

    for k, v in res.items():
        if v is None and k in FUT_CACHE and k not in ("long_liquidation", "short_liquidation"):
            res[k] = FUT_CACHE[k]
        elif v is not None:
            FUT_CACHE[k] = v
    return res


def get_crowd_context():
    """Taker buy/sell pressure + long/short account ratio (OKX public stats)."""
    out = {"taker_buy_ratio": None, "ls_ratio": None}

    d = http_json(OKX + "/api/v5/rubik/stat/taker-volume",
                  {"ccy": "BTC", "instType": "CONTRACTS", "period": "5m"})
    try:
        rows = d["data"][:6]  # newest first: [ts, sellVol, buyVol]
        sell = sum(float(r[1]) for r in rows)
        buy = sum(float(r[2]) for r in rows)
        if buy + sell > 0:
            out["taker_buy_ratio"] = buy / (buy + sell)
    except Exception:
        pass

    d = http_json(OKX + "/api/v5/rubik/stat/contracts/long-short-account-ratio",
                  {"ccy": "BTC", "period": "5m"})
    try:
        out["ls_ratio"] = float(d["data"][0][1])
    except Exception:
        pass
    return out


def get_fear_greed():
    if time.time() - FNG_CACHE["ts"] < 1800 and FNG_CACHE["data"]:
        return FNG_CACHE["data"]
    d = http_json("https://api.alternative.me/fng/", {"limit": 1})
    try:
        item = d["data"][0]
        FNG_CACHE["data"] = {"value": int(item["value"]),
                             "label": item.get("value_classification", "")}
        FNG_CACHE["ts"] = time.time()
    except Exception:
        pass
    return FNG_CACHE["data"]


# ============================================================
# CROSS-MARKET CONTEXT
# ============================================================

def yahoo_quote(symbol):
    for host in ("query1", "query2"):
        data = http_json(f"https://{host}.finance.yahoo.com/v8/finance/chart/{symbol}",
                         {"range": "1d", "interval": "5m"}, headers=YAHOO_HEADERS)
        try:
            meta = data["chart"]["result"][0]["meta"]
            price = safe_float(meta.get("regularMarketPrice"))
            prev = safe_float(meta.get("chartPreviousClose")) or safe_float(meta.get("previousClose"))
            if price is not None:
                return {"price": price, "change": pct_change(price, prev)}
        except Exception:
            continue
    return {}


def get_cross_market():
    result = {}
    for name, symbol in YAHOO_SYMBOLS.items():
        q = yahoo_quote(symbol)
        if q.get("price") is not None:
            CROSS_CACHE[name] = q
            result[name] = q
        else:
            result[name] = CROSS_CACHE.get(name, {})
        time.sleep(0.3)
    return result


# ============================================================
# NEWS  (dedupe + breaking alerts + optional Claude)
# ============================================================

def parse_rss(url, category):
    items = []
    try:
        r = session.get(url, timeout=15)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        for item in root.findall(".//item")[:20]:
            title = clean_text(item.findtext("title"))
            if not title:
                continue
            source = "Unknown"
            if " - " in title:
                title, source = title.rsplit(" - ", 1)
            items.append({
                "title": title, "source": source,
                "link": clean_text(item.findtext("link")),
                "published": clean_text(item.findtext("pubDate")),
                "description": clean_text(item.findtext("description"))[:400],
                "category": category,
            })
    except Exception as e:
        print("RSS error:", category, e)
    return items


def news_key(item):
    raw = item.get("link") or item.get("title", "")
    return hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()


def news_impact(item):
    text = f"{item.get('title', '')} {item.get('description', '')}".lower()

    high_words = [
        "fed", "federal reserve", "fomc", "rate hike", "rate cut",
        "interest rate", "cpi", "pce", "ppi", "nonfarm", "payroll",
        "unemployment", "jobs report", "treasury", "yield",
        "etf", "sec", "regulation", "ban", "approval", "hack",
        "liquidation", "war", "sanction", "tariff", "china",
        "bitcoin reserve", "sovereign", "default", "recession",
    ]
    bullish_words = [
        "etf inflow", "inflows", "approval", "approved", "adoption",
        "reserve", "rate cut", "cuts rates", "dovish", "easing",
        "liquidity", "stimulus", "buying", "accumulation",
        "bullish", "breakout", "surge", "rally", "record inflow",
    ]
    bearish_words = [
        "etf outflow", "outflows", "rejected", "ban", "hack",
        "rate hike", "hikes rates", "hawkish", "tightening",
        "liquidation", "selling", "sell-off", "bearish", "crash",
        "sanction", "tariff", "inflation", "yields rise",
    ]
    hs = sum(1 for x in high_words if x in text)
    bs = sum(1 for x in bullish_words if x in text)
    rs = sum(1 for x in bearish_words if x in text)

    direction = "BULLISH" if bs > rs else "BEARISH" if rs > bs else "MIXED"
    impact = "HIGH" if hs >= 2 else "MEDIUM" if hs == 1 else "LOW"
    return impact, direction


def fetch_news():
    all_items = []
    for category, url in NEWS_FEEDS.items():
        all_items.extend(parse_rss(url, category))

    unique = {news_key(i): i for i in all_items}
    items = list(unique.values())
    for item in items:
        item["impact"], item["direction"] = news_impact(item)

    rank = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    items.sort(key=lambda x: (rank.get(x["impact"], 3), x["title"]))
    return items


def claude_call(system, user, max_tokens=300):
    if not ANTHROPIC_API_KEY:
        return None
    try:
        r = session.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": CLAUDE_MODEL, "max_tokens": max_tokens,
                  "system": system,
                  "messages": [{"role": "user", "content": user}]},
            timeout=40,
        )
        r.raise_for_status()
        return "".join(b.get("text", "") for b in r.json().get("content", [])
                       if b.get("type") == "text").strip()
    except Exception as e:
        print("Claude API error:", str(e)[:150])
        return None


def ai_news_summary(news):
    """2 bullets + BTC impact line. Cached so it only re-runs on new headlines."""
    top = news[:12]
    if not top or not ANTHROPIC_API_KEY:
        return None
    sig = hashlib.sha1("|".join(n["title"] for n in top).encode()).hexdigest()
    if NEWS_AI_CACHE["sig"] == sig:
        return NEWS_AI_CACHE["text"]

    lines = "\n".join(f"- [{n['impact']}] {n['title']} ({n['source']})" for n in top)
    text = claude_call(
        "You are a calm crypto market analyst writing for beginners. "
        "Plain English, no hype, no financial advice.",
        f"Recent headlines:\n{lines}\n\n"
        "Write exactly: two short bullets with what matters most for Bitcoin, "
        "then one line starting 'BTC impact:' with Bullish, Bearish or Neutral and a reason. "
        "Max 70 words total.",
        max_tokens=250,
    )
    NEWS_AI_CACHE.update(sig=sig, text=text)
    return text


def claude_classify(item):
    text = claude_call(
        "You filter breaking news for a Bitcoin trader. Reply with JSON only.",
        f"Headline: {item['title']}\nSource: {item['source']}\n\n"
        'Reply: {"important": true/false, "direction": "bullish|bearish|neutral", '
        '"summary": "one plain-English sentence on why it matters for BTC"}. '
        "important=true only if it could move BTC within hours.",
        max_tokens=150,
    )
    if not text:
        return None
    try:
        return json.loads(text.replace("```json", "").replace("```", "").strip())
    except Exception:
        return None


def check_breaking_news(state, news):
    """Alert once per genuinely new HIGH-impact headline. Max 6 per hour."""
    seen = set(state["last_news_hashes"])
    first_run = not seen
    fresh = [n for n in news if news_key(n) not in seen]

    for n in fresh:
        state["last_news_hashes"].append(news_key(n))
    if first_run:
        return  # don't flood on the first start

    state["breaking_times"] = [t for t in state["breaking_times"] if time.time() - t < 3600]

    for n in [x for x in fresh if x["impact"] == "HIGH"][:3]:
        if len(state["breaking_times"]) >= 6:
            break
        info = claude_classify(n)
        if info is not None and not info.get("important"):
            continue

        direction = (info or {}).get("direction", n["direction"]).upper()
        icon = {"BULLISH": "🟢", "BEARISH": "🔴"}.get(direction, "🟡")
        why = (info or {}).get("summary")
        send("\n".join([
            "📰 BREAKING NEWS", "━━━━━━━━━━━━━━━━━━━━",
            f"{icon} {n['title']}",
            f"Source: {n['source']}",
            f"Likely BTC effect: {direction}",
        ] + ([f"Why: {why}"] if why else []) + [
            "", "News can reverse fast. Do not chase the first spike.",
        ]))
        state["breaking_times"].append(time.time())


# ============================================================
# ECONOMIC CALENDAR
# ============================================================

CALENDAR_CURRENCIES = ("USD", "US", "GLOBAL", "EUR", "EU", "JPY", "CNY", "CN", "GBP", "ALL", "")


def _none_if_empty(v):
    if v is None:
        return None
    v = str(v).strip()
    return v or None


def fetch_calendar_ff():
    events, got_any = [], False
    for url in FF_CALENDAR_URLS:
        data = http_json(url, timeout=15)
        if not isinstance(data, list):
            continue
        got_any = True
        for e in data:
            if str(e.get("impact", "")).lower() != "high":
                continue
            dt = parse_iso(e.get("date"))
            if dt is None:
                continue
            name = clean_text(e.get("title") or "Economic event")
            currency = clean_text(e.get("country") or "")
            events.append({
                "id": hashlib.sha1(f"{dt.isoformat()}|{currency}|{name}".encode()).hexdigest()[:16],
                "name": name, "time": dt, "currency": currency, "impact": "high",
                "forecast": _none_if_empty(e.get("forecast")),
                "previous": _none_if_empty(e.get("previous")),
                "actual": _none_if_empty(e.get("actual")),
                "url": "https://www.forexfactory.com/calendar",
            })
        time.sleep(1)
    return events if got_any else None


def fetch_calendar_fc():
    now = now_utc()
    try:
        r = session.get(f"{CALENDAR_API}/calendar", params={
            "from": (now - timedelta(days=1)).date().isoformat(),
            "to": (now + timedelta(days=8)).date().isoformat(),
            "impact": "high", "limit": 300}, timeout=15)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list):
            raw = data
        elif isinstance(data, dict):
            raw = data.get("events") or data.get("data") or data.get("results") or []
            if isinstance(raw, dict):
                raw = raw.get("events") or raw.get("data") or []
        else:
            raw = []

        events = []
        for e in raw:
            if not isinstance(e, dict):
                continue
            name = (e.get("name") or e.get("title") or e.get("event")
                    or e.get("indicator") or "Economic event")
            tv = e.get("time_utc") or e.get("date") or e.get("datetime") or e.get("timestamp")
            if isinstance(tv, (int, float)):
                ts = float(tv)
                ts = ts / 1000 if ts > 10_000_000_000 else ts
                dt = datetime.fromtimestamp(ts, UTC)
            else:
                dt = parse_iso(tv)
            if dt is None:
                continue
            currency = e.get("currency") or e.get("country") or e.get("country_code") or ""
            events.append({
                "id": str(e.get("id") or hashlib.sha1(
                    f"{dt.isoformat()}|{currency}|{name}".encode()).hexdigest()[:16]),
                "name": clean_text(name), "time": dt,
                "currency": clean_text(currency), "impact": "high",
                "forecast": e.get("consensus", e.get("forecast")),
                "previous": e.get("prior", e.get("previous")),
                "actual": e.get("actual"),
                "url": e.get("url") or "https://www.financecalendar.com/",
            })
        return events
    except Exception as e:
        print("FinanceCalendar error:", e)
        return None


def fetch_calendar():
    events = fetch_calendar_ff()
    if events is None:
        events = fetch_calendar_fc()
    if not events:
        return []
    events = [e for e in events if str(e["currency"]).upper() in CALENDAR_CURRENCIES]
    events.sort(key=lambda x: x["time"])
    return events


def upcoming_high_event(events, within_min):
    now = now_utc()
    best = None
    for e in events or []:
        mins = (e["time"] - now).total_seconds() / 60
        if -10 <= mins <= within_min and (best is None or mins < best[1]):
            best = (e, mins)
    return best


def numeric_surprise(actual, forecast):
    a, f = parse_number(actual), parse_number(forecast)
    return None if a is None or f is None else a - f


def event_direction(event):
    name = event["name"].lower()
    surprise = numeric_surprise(event.get("actual"), event.get("forecast"))

    if surprise is None:
        return "WAIT", "Result not out yet. Wait for the number, then watch how yields and the dollar react."

    if any(k in name for k in ["cpi", "pce", "ppi", "inflation", "consumer price",
                               "producer price", "price index"]):
        if surprise > 0:
            return "BEARISH", "Inflation hotter than expected -> rate/yield pressure -> usually bad for BTC."
        if surprise < 0:
            return "BULLISH", "Inflation cooler than expected -> less rate pressure -> usually good for BTC."
        return "NEUTRAL", "Inflation matched expectations."

    if any(k in name for k in ["fomc", "fed rate", "interest rate decision", "fed decision"]):
        return "MIXED", "Judge the Fed together with the press conference, yields and the dollar."

    if any(k in name for k in ["nonfarm", "payroll", "unemployment", "jobless", "employment",
                               "job openings", "jolts", "average hourly earnings", "wage"]):
        return "MIXED", "Jobs data cuts both ways: strong = growth but higher rates, weak = lower rates but recession fear."

    if any(k in name for k in ["gdp", "retail sales", "pmi", "ism"]):
        return "MIXED", "Growth data: watch the 10Y yield reaction before trusting a direction."

    return "MIXED", "Direction depends on yields, the dollar and risk sentiment."


# ============================================================
# MARKET SESSIONS
# ============================================================

MARKETS = ["Tokyo", "Hong Kong", "London", "New York"]


def market_open_utc(name, d):
    if name == "Tokyo":
        local = datetime(d.year, d.month, d.day, 9, 0, tzinfo=ZoneInfo("Asia/Tokyo"))
    elif name == "Hong Kong":
        local = datetime(d.year, d.month, d.day, 9, 30, tzinfo=ZoneInfo("Asia/Hong_Kong"))
    elif name == "London":
        local = datetime(d.year, d.month, d.day, 8, 0, tzinfo=ZoneInfo("Europe/London"))
    else:
        local = datetime(d.year, d.month, d.day, 9, 30, tzinfo=NY_TZ)
    return local.astimezone(UTC)


def next_market_opens():
    now = now_utc()
    result = []
    for name in MARKETS:
        for offset in range(0, 3):
            open_dt = market_open_utc(name, (now + timedelta(days=offset)).date())
            if open_dt.weekday() >= 5:
                continue
            if open_dt > now - timedelta(minutes=5):
                result.append((name, open_dt))
                break
    result.sort(key=lambda x: x[1])
    return result


# ============================================================
# PLAIN-ENGLISH TRANSLATORS
# ============================================================

def trend_words(t):
    return {"BULLISH": "UP 📈 (strong)", "WEAK BULLISH": "Slightly up 📈",
            "BEARISH": "DOWN 📉 (strong)", "WEAK BEARISH": "Slightly down 📉",
            "NEUTRAL": "Sideways ➡️"}.get(t, "Loading...")


def momentum_words(rsi):
    if rsi is None:
        return "unknown"
    if rsi >= 75:
        return f"Overheated ({rsi:.0f}) - price rose fast, pullback risk"
    if rsi >= 60:
        return f"Strong buying ({rsi:.0f})"
    if rsi >= 45:
        return f"Balanced ({rsi:.0f})"
    if rsi >= 30:
        return f"Weak / sellers in control ({rsi:.0f})"
    return f"Oversold ({rsi:.0f}) - bounce possible"


def trend_strength_words(adx):
    if adx is None:
        return "unknown"
    if adx < ADX_MIN:
        return f"Ranging / choppy (ADX {adx:.0f}) - trend signals unreliable"
    if adx < 25:
        return f"Trend is starting (ADX {adx:.0f})"
    return f"Strong trend (ADX {adx:.0f})"


def funding_words(funding):
    if funding is None:
        return "Funding rate: could not be fetched from any exchange this cycle"
    f = funding * 100
    if f > 0.05:
        txt = "Crowd is VERY long -> risk of a sudden drop (long squeeze)"
    elif f > 0.01:
        txt = "Slightly more longs than shorts (normal bullish mood)"
    elif f < -0.05:
        txt = "Crowd is VERY short -> risk of a sudden spike (short squeeze)"
    elif f < -0.01:
        txt = "Slightly more shorts than longs"
    else:
        txt = "Longs and shorts are balanced"
    return f"{txt} (funding {f:+.4f}%)"


def oi_words(futures):
    oi, ch = futures.get("open_interest"), futures.get("oi_change_pct")
    if oi is None:
        return "Open interest: could not be fetched from any exchange this cycle"
    base = f"Open interest: {oi:,.0f} BTC"
    if ch is None:
        return base + " (change shows after ~5 min of history)"
    meaning = ("lots of NEW money entering -> bigger move likely" if ch > 2 else
               "traders closing positions -> move may be fading" if ch < -2 else
               "no big change")
    return f"{base}, {ch:+.2f}% in {futures.get('oi_window_min', 30)}m -> {meaning}"


def liq_words(futures):
    lng, sht = futures.get("long_liquidation"), futures.get("short_liquidation")
    if lng is None or sht is None:
        return "Liquidations (1h): exchange feed unavailable this cycle"
    if lng + sht < 1:
        return "Liquidations (1h): quiet (nothing significant reported)"
    side = ("longs got wiped out (bearish pressure)" if lng > sht
            else "shorts got wiped out (bullish pressure)")
    src = " [live]" if WS_STATE["connected"] else ""
    return f"Liquidations (1h){src}: Longs ${lng:,.0f} | Shorts ${sht:,.0f} -> {side}"


def crowd_words(crowd):
    lines = []
    tb = (crowd or {}).get("taker_buy_ratio")
    if tb is not None:
        meaning = ("buyers are hitting the market harder" if tb > 0.52 else
                   "sellers are hitting the market harder" if tb < 0.48 else "balanced")
        lines.append(f"Aggressive buyers vs sellers: {tb * 100:.0f}% buy -> {meaning}")
    ls = (crowd or {}).get("ls_ratio")
    if ls is not None:
        meaning = ("many more accounts are long (crowded)" if ls > 2 else
                   "many more accounts are short (crowded)" if ls < 0.7 else "balanced")
        lines.append(f"Long/short accounts: {ls:.2f} -> {meaning}")
    return lines


def fng_words(fng):
    if not fng:
        return "Fear & Greed index: unavailable this cycle"
    v = fng["value"]
    note = ("extreme fear - often near bottoms (contrarian)" if v <= 20 else
            "fear" if v <= 40 else "neutral" if v <= 60 else
            "greed" if v <= 80 else "extreme greed - risk of a top (contrarian)")
    return f"Fear & Greed: {v} ({fng.get('label', '')}) -> {note}"


def cross_line(name, a, good_if_up, label):
    if not a or a.get("price") is None:
        return f"{label}: temporarily unavailable"
    ch = a.get("change")
    price_txt = f"{a['price']:,.2f}"
    if ch is None:
        return f"{label}: {price_txt}"
    if good_if_up is None:
        meaning = ("safe-haven demand rising" if ch > 0.4 else
                   "safe-haven demand falling" if ch < -0.4 else "flat")
    else:
        thr = 0.15 if name == "DXY" else 0.3
        if abs(ch) < thr:
            meaning = "flat - no pressure on BTC"
        else:
            helps = (ch > 0) == bool(good_if_up)
            meaning = "good for BTC ✅" if helps else "bad for BTC ❌"
    return f"{label}: {price_txt} ({ch:+.2f}%) -> {meaning}"


# ============================================================
# BIAS ENGINE
# ============================================================

def tech_score(tech):
    """Technical-only score. Shared by live bot and backtest."""
    score, reasons = 0.0, []
    h1, h4, m15 = tech.get("1h") or {}, tech.get("4h") or {}, tech.get("15m") or {}

    t4 = h4.get("trend")
    if t4 == "BULLISH":
        score += 2; reasons.append("4-hour chart trend is UP")
    elif t4 == "BEARISH":
        score -= 2; reasons.append("4-hour chart trend is DOWN")
    elif t4 == "WEAK BULLISH":
        score += 0.75; reasons.append("4-hour chart leans up")
    elif t4 == "WEAK BEARISH":
        score -= 0.75; reasons.append("4-hour chart leans down")

    t1 = h1.get("trend")
    if t1 == "BULLISH":
        score += 1.5; reasons.append("1-hour chart trend is UP")
    elif t1 == "BEARISH":
        score -= 1.5; reasons.append("1-hour chart trend is DOWN")
    elif t1 == "WEAK BULLISH":
        score += 0.5
    elif t1 == "WEAK BEARISH":
        score -= 0.5

    if m15.get("trend") == "BULLISH":
        score += 0.5
    elif m15.get("trend") == "BEARISH":
        score -= 0.5

    rsi = h1.get("rsi")
    if rsi is not None:
        if 50 <= rsi <= 68:
            score += 0.5; reasons.append(f"Momentum supportive (RSI {rsi:.0f})")
        elif rsi > 75:
            score -= 0.5; reasons.append(f"Price overheated (RSI {rsi:.0f})")
        elif rsi < 30:
            score += 0.5; reasons.append(f"Price oversold, bounce possible (RSI {rsi:.0f})")
    return score, reasons


def bias_from_score(score, reasons=None):
    bias = "BULLISH" if score >= 2.0 else "BEARISH" if score <= -2.0 else "NEUTRAL / MIXED"
    return {"score": score, "bias": bias,
            "confidence": int(clamp(50 + abs(score) * 10, 50, 90)),
            "reasons": (reasons or [])[:8]}


def calculate_bias(tech, futures, cross, news, fng=None, crowd=None):
    score, reasons = tech_score(tech)

    funding = futures.get("funding")
    if funding is not None:
        fp = funding * 100
        if fp > 0.05:
            score -= 0.75; reasons.append("Too many traders are long (funding high)")
        elif fp < -0.05:
            score += 0.75; reasons.append("Too many traders are short (funding negative)")

    oi = futures.get("oi_change_pct")
    if oi is not None:
        if oi > 2:
            reasons.append("New money entering futures (OI up)")
        elif oi < -2:
            reasons.append("Traders closing positions (OI down)")

    tb = (crowd or {}).get("taker_buy_ratio")
    if tb is not None:
        if tb > 0.55:
            score += 0.25; reasons.append("Aggressive buyers dominate")
        elif tb < 0.45:
            score -= 0.25; reasons.append("Aggressive sellers dominate")
    ls = (crowd or {}).get("ls_ratio")
    if ls is not None:
        if ls > 2.0:
            score -= 0.25; reasons.append("Crowd extremely long (contrarian risk)")
        elif ls < 0.7:
            score += 0.25; reasons.append("Crowd extremely short (contrarian chance)")

    if fng:
        if fng["value"] <= 20:
            score += 0.5; reasons.append("Extreme fear in the market (contrarian bullish)")
        elif fng["value"] >= 80:
            score -= 0.5; reasons.append("Extreme greed in the market (contrarian bearish)")

    dxy, nq = cross.get("DXY") or {}, cross.get("NASDAQ") or {}
    y10, vix = cross.get("US10Y") or {}, cross.get("VIX") or {}

    if dxy.get("change") is not None:
        if dxy["change"] > 0.4:
            score -= 0.75; reasons.append("US Dollar getting stronger (bad for BTC)")
        elif dxy["change"] < -0.4:
            score += 0.75; reasons.append("US Dollar getting weaker (good for BTC)")
    if nq.get("change") is not None:
        if nq["change"] > 0.5:
            score += 0.5; reasons.append("Stocks (Nasdaq) are rising - risk-on")
        elif nq["change"] < -0.5:
            score -= 0.5; reasons.append("Stocks (Nasdaq) are falling - risk-off")
    if y10.get("change") is not None:
        if y10["change"] > 1.0:
            score -= 0.5; reasons.append("Bond yields rising (bad for BTC)")
        elif y10["change"] < -1.0:
            score += 0.5; reasons.append("Bond yields falling (good for BTC)")
    if vix.get("change") is not None:
        if vix["change"] > 5:
            score -= 0.75; reasons.append("Fear index (VIX) jumping")
        elif vix["change"] < -5:
            score += 0.5; reasons.append("Fear index (VIX) cooling")

    bull = sum(1 for n in news[:15] if n["direction"] == "BULLISH" and n["impact"] != "LOW")
    bear = sum(1 for n in news[:15] if n["direction"] == "BEARISH" and n["impact"] != "LOW")
    if bull > bear:
        score += min(1.0, 0.25 * (bull - bear)); reasons.append("Recent news leans positive")
    elif bear > bull:
        score -= min(1.0, 0.25 * (bear - bull)); reasons.append("Recent news leans negative")

    return bias_from_score(score, reasons)


def scenario_levels(tech, live_price=None):
    h1, h4 = tech.get("1h") or {}, tech.get("4h") or {}
    price = live_price or h1.get("price") or h4.get("price")
    if price is None:
        return {}
    atr = h1.get("atr") or price * 0.01
    return {
        "price": price, "atr": atr, "atr_pct": atr / price * 100,
        "support": h1.get("support"), "resistance": h1.get("resistance"),
        "bull1": price + atr, "bull2": price + 2 * atr,
        "bear1": price - atr, "bear2": price - 2 * atr,
    }


# ============================================================
# TRADE PLAN  (with filters, limit entry, volatility sizing)
# ============================================================

def build_trade_plan(bias, tech, levels, events, account=ACCOUNT_DEFAULT,
                     risk_pct=RISK_PCT_DEFAULT, cooldown_min=0.0, paused=False):
    plan = {"action": "WAIT", "why": [], "strength": "", "score": bias["score"]}

    if not levels:
        plan["why"] = ["Not enough price data yet."]
        return plan

    price, atr = levels["price"], levels["atr"]
    h1, m15 = tech.get("1h") or {}, tech.get("15m") or {}
    score = bias["score"]
    t1, t15, rsi1 = h1.get("trend", "NEUTRAL"), m15.get("trend", "NEUTRAL"), h1.get("rsi")
    support, resistance = levels.get("support"), levels.get("resistance")

    cand = None
    if score >= 2 and t1 in BULL_TRENDS and t15 != "BEARISH":
        cand = "LONG"
    elif score <= -2 and t1 in BEAR_TRENDS and t15 != "BULLISH":
        cand = "SHORT"

    action, why = "WAIT", []
    soon = upcoming_high_event(events, NO_TRADE_BEFORE_EVENT_MIN)

    if paused:
        why.append("Signals are paused. Send /resume to turn them back on.")
    elif cooldown_min > 0:
        why.append(f"Cooling off after a stopped-out trade ({cooldown_min:.0f} min left). "
                   "Avoids revenge-trading in choppy conditions.")
    elif soon:
        e, mins = soon
        when = f"in {int(mins)} min" if mins > 0 else "just released"
        why.append(f"Big news ({e['name']}) is {when}. Price can spike both ways - "
                   "stay out until the move settles.")
    elif cand is None:
        if abs(score) < 2:
            why.append(f"Signals are mixed (score {score:+.1f}). No clear edge - "
                       "the best trade is often no trade.")
        elif score >= 2:
            why.append("Data leans bullish, but the 1h/15m chart does not confirm yet.")
        else:
            why.append("Data leans bearish, but the 1h/15m chart does not confirm yet.")
    else:
        adx = h1.get("adx")
        vols = [v for v in (m15.get("volume_ratio"), h1.get("volume_ratio")) if v is not None]
        vr = max(vols) if vols else None

        if cand == "LONG" and rsi1 is not None and rsi1 > 75:
            why.append("Uptrend, but price is overheated. Buying now = chasing. Wait for a dip.")
        elif cand == "SHORT" and rsi1 is not None and rsi1 < 25:
            why.append("Downtrend, but price is oversold. Selling now = chasing. Wait for a bounce.")
        elif adx is not None and adx < ADX_MIN:
            why.append(f"Market is ranging (ADX {adx:.0f} < {ADX_MIN:.0f}). Trend setups fail "
                       "often in sideways markets - waiting for a real trend.")
        elif vr is not None and vr < VOLUME_MIN_RATIO:
            why.append(f"Volume is thin ({vr:.1f}x normal). Moves without volume often fade.")
        else:
            action = cand

    plan["action"] = action
    plan["strength"] = "STRONG" if abs(score) >= 3.5 else "MODERATE"

    if action in ("LONG", "SHORT"):
        is_long = action == "LONG"
        sign = 1 if is_long else -1

        # Limit entry: wait for a small pullback instead of chasing.
        entry = price - sign * PULLBACK_ATR * atr
        risk = STOP_ATR * atr
        if is_long and support and 0.6 * atr <= entry - support <= 2.5 * atr:
            risk = entry - (support - 0.2 * atr)
        elif not is_long and resistance and 0.6 * atr <= resistance - entry <= 2.5 * atr:
            risk = (resistance + 0.2 * atr) - entry

        stop = entry - sign * risk
        tp1 = entry + sign * RR1 * risk
        tp2 = entry + sign * RR2 * risk

        # Volatility scaling: smaller position when the market is wild.
        atr_pct = levels["atr_pct"]
        mult = 0.5 if atr_pct > 1.0 else 0.75 if atr_pct > 0.7 else 1.0
        used_pct = risk_pct * mult
        risk_usd = account * used_pct / 100
        size_btc = risk_usd / risk if risk > 0 else 0

        plan.update({
            "entry": entry, "stop": stop, "tp1": tp1, "tp2": tp2,
            "risk": risk, "risk_pct": risk / price * 100,
            "rr1": RR1, "rr2": RR2,
            "risk_mult": mult, "risk_used_pct": used_pct,
            "vol_note": ("high volatility -> position size reduced" if mult < 1 else ""),
            "account": account, "risk_usd": risk_usd,
            "size_btc": size_btc, "notional": size_btc * entry,
        })
    else:
        plan["trigger_long"] = (resistance if resistance and price < resistance <= price + 3 * atr
                                else price + atr)
        plan["trigger_short"] = (support if support and price - 3 * atr <= support < price
                                 else price - atr)

    plan["why"] = why
    return plan


def plan_text_lines(plan, bias):
    lines = []
    if plan["action"] in ("LONG", "SHORT"):
        long = plan["action"] == "LONG"
        sgn = "+" if long else "-"
        sgn2 = "-" if long else "+"
        lines += [
            f"✅ {plan['action']} ({'BUY' if long else 'SELL'}) setup - {plan['strength']} "
            f"- confidence {bias['confidence']}%",
            f"Order:         {'BUY' if long else 'SELL'} LIMIT at {fmt_price(plan['entry'])} "
            f"(waits for a small pullback; auto-cancels if not filled in {EXPIRE_MIN // 60}h)",
            f"Stop-loss:     {fmt_price(plan['stop'])}  ({sgn2}{plan['risk_pct']:.2f}%)  <- exit here if wrong",
            f"Take-profit 1: {fmt_price(plan['tp1'])}  ({sgn}{plan['risk_pct'] * plan['rr1']:.2f}%)  "
            f"<- close {int(TP1_FRACTION * 100)}% and move stop to breakeven",
            f"Take-profit 2: {fmt_price(plan['tp2'])}  ({sgn}{plan['risk_pct'] * plan['rr2']:.2f}%)  "
            "<- close the rest",
            f"Reward:Risk = 1:{plan['rr1']:.1f} to 1:{plan['rr2']:.1f}",
        ]
    else:
        lines.append("⏸ WAIT - no good trade right now")

    for w in plan.get("why", []):
        lines.append(f"Why: {w}")

    if plan["action"] in ("LONG", "SHORT"):
        lines += [
            "",
            f"💰 Position size: risk {plan['risk_used_pct']:.2f}% of ${plan['account']:,.0f} "
            f"= ${plan['risk_usd']:,.2f}"
            + (f"  ({plan['vol_note']})" if plan.get("vol_note") else ""),
            f"-> size ~{plan['size_btc']:.4f} BTC (~${plan['notional']:,.0f} position)",
            "Change account size with /account 500, risk with /risk 0.5",
        ]
    else:
        lines += [
            "",
            f"👀 Watch: LONG only if BTC holds above {fmt_price(plan.get('trigger_long'))}",
            f"          SHORT only if BTC drops below {fmt_price(plan.get('trigger_short'))}",
        ]
    return lines


# ============================================================
# TRADE TRACKING  (shared by live bot and backtest)
# ============================================================

def new_trade(plan, ts):
    return {
        "id": str(int(ts)), "action": plan["action"], "created": ts,
        "expires": ts + EXPIRE_MIN * 60, "status": "PENDING",
        "entry": plan["entry"], "stop": plan["stop"],
        "tp1": plan["tp1"], "tp2": plan["tp2"], "risk": plan["risk"],
        "rr1": plan["rr1"], "rr2": plan["rr2"],
        "current_stop": plan["stop"], "tp1_hit": False, "peak": plan["entry"],
        "entered_at": None, "closed_at": None, "exit_price": None,
        "outcome": None, "result_r": None,
        "risk_usd": plan.get("risk_usd", 0), "size_btc": plan.get("size_btc", 0),
        "score": plan.get("score"), "last_candle_ts": ts * 1000,
        "last_trail_msg": plan["stop"], "warned_flip": False,
    }


def close_trade(t, exit_price, ts, outcome):
    is_long = t["action"] == "LONG"
    risk, entry = t["risk"], t["entry"]

    def r_of(px):
        return ((px - entry) if is_long else (entry - px)) / risk

    if t["tp1_hit"]:
        r = TP1_FRACTION * t["rr1"] + (1 - TP1_FRACTION) * r_of(exit_price)
    else:
        r = r_of(exit_price)

    fee_r = 2 * FEE_RATE * entry / risk
    t.update(status="CLOSED", closed_at=ts, exit_price=exit_price,
             outcome=outcome, result_r=round(r - fee_r, 3))


def advance_trade(t, high, low, ts):
    """Feed one candle (or a price tick). Returns [(event, price), ...].
    If one candle touches both stop and target we assume the STOP came first."""
    ev = []
    if t["status"] in ("CLOSED", "EXPIRED"):
        return ev

    is_long = t["action"] == "LONG"
    entry, risk = t["entry"], t["risk"]

    if t["status"] == "PENDING":
        touched = (low <= entry) if is_long else (high >= entry)
        if not touched:
            ran = (high >= t["tp1"]) if is_long else (low <= t["tp1"])
            if ran:
                t.update(status="EXPIRED", outcome="MISSED", closed_at=ts)
                ev.append(("MISSED", None))
            elif ts >= t["expires"]:
                t.update(status="EXPIRED", outcome="EXPIRED", closed_at=ts)
                ev.append(("EXPIRED", None))
            return ev
        t.update(status="OPEN", entered_at=ts, peak=entry)
        ev.append(("ENTERED", entry))

    stop = t["current_stop"]
    if (low <= stop) if is_long else (high >= stop):
        if not t["tp1_hit"]:
            outcome = "STOP"
        elif abs(stop - entry) < 1e-9 * max(entry, 1):
            outcome = "BREAKEVEN"
        else:
            outcome = "TRAIL"
        close_trade(t, stop, ts, outcome)
        ev.append((outcome, stop))
        return ev

    if is_long:
        t["peak"] = max(t["peak"], high)
        if not t["tp1_hit"] and high >= t["tp1"]:
            t["tp1_hit"] = True
            t["current_stop"] = entry
            ev.append(("TP1", t["tp1"]))
        if t["tp1_hit"]:
            if high >= t["tp2"]:
                close_trade(t, t["tp2"], ts, "TP2")
                ev.append(("TP2", t["tp2"]))
                return ev
            new_stop = max(t["current_stop"], t["peak"] - TRAIL_R * risk)
            if new_stop > t["current_stop"] + 1e-9:
                t["current_stop"] = new_stop
                ev.append(("TRAIL_UPDATE", new_stop))
    else:
        t["peak"] = min(t["peak"], low)
        if not t["tp1_hit"] and low <= t["tp1"]:
            t["tp1_hit"] = True
            t["current_stop"] = entry
            ev.append(("TP1", t["tp1"]))
        if t["tp1_hit"]:
            if low <= t["tp2"]:
                close_trade(t, t["tp2"], ts, "TP2")
                ev.append(("TP2", t["tp2"]))
                return ev
            new_stop = min(t["current_stop"], t["peak"] + TRAIL_R * risk)
            if new_stop < t["current_stop"] - 1e-9:
                t["current_stop"] = new_stop
                ev.append(("TRAIL_UPDATE", new_stop))
    return ev


def trade_event_text(t, name, price, state):
    a = t["action"]
    usd = lambda r: r * (t.get("risk_usd") or 0)

    if name == "ENTERED":
        return (f"✅ ENTRY FILLED - {a}\nPrice reached {fmt_price(t['entry'])}.\n"
                f"Make sure your STOP-LOSS is set at {fmt_price(t['stop'])}.\n"
                f"TP1 {fmt_price(t['tp1'])} | TP2 {fmt_price(t['tp2'])}")
    if name == "TP1":
        cs = t["current_stop"]
        at_be = abs(cs - t["entry"]) <= 0.05 * t["risk"]
        stop_line = (f"Move your stop-loss to BREAKEVEN {fmt_price(t['entry'])} - the trade can no longer lose."
                     if at_be else
                     f"Move your stop-loss to {fmt_price(cs)} (above breakeven) - profit is locked in.")
        return (f"🎯 TP1 HIT - {a}\nClose {int(TP1_FRACTION * 100)}% of the position at {fmt_price(t['tp1'])}.\n"
                f"{stop_line}\n"
                f"The rest runs to TP2 {fmt_price(t['tp2'])} with a trailing stop.")
    if name == "TRAIL_UPDATE":
        return (f"🔼 TRAILING STOP\nMove your stop-loss to {fmt_price(price)} "
                "to lock in more profit.")
    if name == "TP2":
        r = t["result_r"]
        return (f"🏆 TP2 HIT - {a}\nClose the remaining position at {fmt_price(t['tp2'])}.\n"
                f"Result: {r:+.2f}R (about ${usd(r):+,.2f}).")
    if name == "STOP":
        cd = COOLDOWN_MIN
        return (f"🛑 STOP-LOSS HIT - {a}\nExit at {fmt_price(price)}.\n"
                f"Result: {t['result_r']:+.2f}R (about ${usd(t['result_r']):+,.2f}).\n"
                f"A loss is part of the plan. New signals paused for {cd} min.")
    if name in ("BREAKEVEN", "TRAIL"):
        r = t["result_r"]
        label = "stopped at breakeven" if name == "BREAKEVEN" else "trailing stop hit"
        return (f"✅ TRADE CLOSED ({label}) - {a}\nExit {fmt_price(price)} after TP1.\n"
                f"Result: {r:+.2f}R (about ${usd(r):+,.2f}).")
    if name == "EXPIRED":
        return (f"⌛ SETUP EXPIRED - {a}\nPrice never reached the limit entry "
                f"{fmt_price(t['entry'])} within {EXPIRE_MIN // 60}h. Cancel the order. No trade taken.")
    if name == "MISSED":
        return (f"💨 SETUP MISSED - {a}\nPrice ran to TP1 without filling the limit entry. "
                "Cancel the order - do not chase.")
    return f"{name}: {a}"


def log_trade_csv(t):
    try:
        new = not os.path.exists(TRADE_LOG_CSV)
        with open(TRADE_LOG_CSV, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["id", "action", "created_utc", "entry", "stop", "tp1", "tp2",
                            "outcome", "exit", "result_R", "score"])
            w.writerow([t["id"], t["action"],
                        datetime.fromtimestamp(t["created"], UTC).isoformat(),
                        round(t["entry"], 2), round(t["stop"], 2), round(t["tp1"], 2),
                        round(t["tp2"], 2), t["outcome"], t.get("exit_price"),
                        t.get("result_r"), t.get("score")])
    except Exception:
        traceback.print_exc()


def trade_stats(trades):
    closed = [t for t in trades if t.get("status") == "CLOSED" and t.get("result_r") is not None]
    unfilled = sum(1 for t in trades if t.get("status") == "EXPIRED")
    s = {"n": len(closed), "unfilled": unfilled}
    if not closed:
        return s

    rs = [t["result_r"] for t in closed]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    s.update(
        win_rate=len(wins) / len(rs) * 100,
        avg_r=sum(rs) / len(rs),
        total_r=sum(rs),
        best=max(rs), worst=min(rs),
        profit_factor=(sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else None,
    )
    cum, peak, dd = 0.0, 0.0, 0.0
    streak = max_streak = 0
    for r in rs:
        cum += r
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
        streak = streak + 1 if r <= 0 else 0
        max_streak = max(max_streak, streak)
    s["max_dd"], s["max_losing_streak"] = dd, max_streak
    for d in ("LONG", "SHORT"):
        sub = [t["result_r"] for t in closed if t["action"] == d]
        s[d] = (len(sub), (sum(1 for r in sub if r > 0) / len(sub) * 100) if sub else 0,
                sum(sub) if sub else 0)
    return s


def stats_lines(trades, title="Track record", account=None):
    s = trade_stats(trades)
    if s["n"] == 0:
        extra = f" ({s['unfilled']} setups never filled)" if s["unfilled"] else ""
        return [f"📊 {title}: no closed trades yet{extra}."]
    lines = [
        f"📊 {title}",
        f"Trades: {s['n']} | Win rate: {s['win_rate']:.0f}% | Avg: {s['avg_r']:+.2f}R | Total: {s['total_r']:+.2f}R",
        f"Best {s['best']:+.2f}R | Worst {s['worst']:+.2f}R | "
        f"Profit factor: {('%.2f' % s['profit_factor']) if s['profit_factor'] else 'n/a'}",
        f"Max drawdown: {s['max_dd']:.2f}R | Longest losing streak: {s['max_losing_streak']}",
    ]
    for d in ("LONG", "SHORT"):
        if s[d][0]:
            lines.append(f"{d}: {s[d][0]} trades, {s[d][1]:.0f}% wins, {s[d][2]:+.2f}R")
    if s["unfilled"]:
        lines.append(f"Setups that never filled: {s['unfilled']}")
    if account:
        lines.append(f"(1R = {RISK_PCT_DEFAULT:.0f}% of account -> total ~ "
                     f"{s['total_r'] * RISK_PCT_DEFAULT:+.1f}% on ${account:,.0f})")
    return lines


# ---------- live trade management --------------------------------

def finish_trade(state, t):
    state["last_close_ts"] = time.time()
    if t["status"] == "CLOSED":
        log_trade_csv(t)
        if t["outcome"] == "STOP":
            state["cooldown_until"] = time.time() + COOLDOWN_MIN * 60
    state["trades"].append(t)
    state["active_trade"] = None


def apply_trade_events(state, t, events):
    for name, price in events:
        if name == "TP1":
            t["last_trail_msg"] = t["current_stop"]  # TP1 message already states the stop
        if name == "TRAIL_UPDATE":
            moved = abs(price - t["last_trail_msg"])
            if moved < 0.5 * t["risk"]:
                continue  # don't spam small trailing moves
            t["last_trail_msg"] = price
        send(trade_event_text(t, name, price, state))

    if t["status"] in ("CLOSED", "EXPIRED"):
        finish_trade(state, t)


def manage_active_trade(state, live_price):
    t = state.get("active_trade")
    if not t:
        return

    df5 = CANDLES.get("5m")
    if df5 is not None and len(df5) > 3:
        closed = df5.iloc[:-1]
        new = closed[closed["time"] > t["last_candle_ts"]]
        for _, row in new.iterrows():
            close_ts = row["time"] / 1000 + 300
            events = advance_trade(t, float(row["high"]), float(row["low"]), close_ts)
            t["last_candle_ts"] = float(row["time"])
            apply_trade_events(state, t, events)
            if state.get("active_trade") is None:
                return

    if live_price:
        events = advance_trade(t, live_price, live_price, time.time())
        apply_trade_events(state, t, events)


def process_signal(state, plan, bias, live_price):
    t = state.get("active_trade")
    now = time.time()

    if t:
        direction = 1 if t["action"] == "LONG" else -1
        if t["status"] == "PENDING":
            if upcoming_high_event(CTX.get("events") or [], 20):
                send(f"❎ SETUP CANCELLED - {t['action']}\n"
                     "Big news is about to be released. Cancel the unfilled limit order "
                     "and wait for the move to settle.")
                t.update(status="EXPIRED", outcome="CANCELLED", closed_at=now)
                finish_trade(state, t)
                return
            faded = plan["action"] != t["action"] and (
                plan["action"] != "WAIT" or abs(bias["score"]) < 1.0
                or bias["score"] * direction < 0)
            if faded:
                send(f"❎ SETUP CANCELLED - {t['action']}\n"
                     "Conditions changed before the limit order filled. Cancel the order.")
                t.update(status="EXPIRED", outcome="CANCELLED", closed_at=now)
                finish_trade(state, t)
        elif t["status"] == "OPEN" and not t.get("warned_flip"):
            if bias["score"] * direction <= -2:
                send(f"⚠️ CONDITIONS FLIPPED against your open {t['action']}.\n"
                     "Keep your stop-loss. You may close early if you prefer to be safe.")
                t["warned_flip"] = True
        return

    if now - state.get("last_close_ts", 0) < REENTRY_MIN * 60:
        return  # short breather after any trade ends

    if plan["action"] in ("LONG", "SHORT"):
        t = new_trade(plan, now)
        state["active_trade"] = t
        lines = ["🚨 TRADE ALERT", "━━━━━━━━━━━━━━━━━━━━"]
        lines += plan_text_lines(plan, bias)
        lines += ["", "You will get follow-up messages for fill, TP1, trailing stop and exit.",
                  "Information only - the bot never places trades."]
        send("\n".join(lines))
        png = make_chart(trade=t)
        if png:
            send_photo(png, f"{plan['action']} setup")


# ============================================================
# CHART
# ============================================================

def make_chart(trade=None, tf="15m"):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return None

    df = CANDLES.get(tf)
    if df is None or len(df) < 30:
        return None

    d = df.tail(96).reset_index(drop=True)
    fig, ax = plt.subplots(figsize=(10, 5.2), dpi=110)
    for i, r in d.iterrows():
        color = "#16a34a" if r["close"] >= r["open"] else "#dc2626"
        ax.vlines(i, r["low"], r["high"], color=color, linewidth=1)
        ax.bar(i, max(abs(r["close"] - r["open"]), 1e-9), bottom=min(r["open"], r["close"]),
               width=0.6, color=color)
    ax.plot(d["ema21"], color="#2563eb", linewidth=1, label="EMA21")
    ax.plot(d["ema50"], color="#f59e0b", linewidth=1, label="EMA50")

    def hline(y, color, label, style="-"):
        if y is not None:
            ax.axhline(y, color=color, linestyle=style, linewidth=1, alpha=0.9)
            ax.text(len(d) - 1, y, f" {label} {y:,.0f}", color=color, fontsize=8, va="bottom", ha="right")

    levels = CTX.get("levels") or {}
    hline(levels.get("support"), "#64748b", "Support", ":")
    hline(levels.get("resistance"), "#64748b", "Resistance", ":")

    t = trade or (CTX.get("state") or {}).get("active_trade")
    plan = CTX.get("plan") or {}
    src = t or (plan if plan.get("action") in ("LONG", "SHORT") else None)
    if src:
        hline(src["entry"], "#0ea5e9", "Entry", "--")
        hline(src.get("current_stop", src["stop"]), "#dc2626", "Stop", "--")
        hline(src["tp1"], "#16a34a", "TP1", "--")
        hline(src["tp2"], "#15803d", "TP2", "--")

    ax.set_title(f"BTC/USDT {tf} - last 24h", fontsize=11)
    ax.legend(loc="upper left", fontsize=8)
    ax.grid(alpha=0.2)
    ax.set_xticks([])
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()


# ============================================================
# ALERTS (events / sessions / daily summary)
# ============================================================

def event_alert_message(event, minutes_to_event):
    direction, explanation = event_direction(event)
    if minutes_to_event > 0:
        timing = f"in about {minutes_to_event} minutes"
        title = "⏰ BIG NEWS COMING"
        todo = ("What to do: avoid opening new trades from 5 minutes before "
                "until ~10 minutes after. Spreads widen and price whipsaws.")
    else:
        timing = "released / happening now"
        title = "🚨 BIG NEWS NOW"
        todo = ("What to do: let the first spike pass. Trade only after price "
                "settles and the 5m/15m trend is clear.")

    lines = [title, "━━━━━━━━━━━━━━━━━━━━",
             f"Event: {event['name']} ({event['currency'] or 'Global'})",
             f"Time: {utc_text(event['time'])} - {timing}"]
    for label, key in (("Expected", "forecast"), ("Previous", "previous"), ("Actual", "actual")):
        if event.get(key) is not None:
            lines.append(f"{label}: {event[key]}")
    lines += ["", f"BTC reading: {direction}", explanation, "", todo,
              "⚠️ The first reaction can reverse quickly."]
    return "\n".join(lines)


def check_event_alerts(state, events):
    now = now_utc()
    for event in events:
        diff = (event["time"] - now).total_seconds() / 60
        for window in EVENT_ALERT_WINDOWS:
            if window - 3 < diff <= window:
                key = f"{event['id']}:T-{window}"
                if not state["event_alerts"].get(key):
                    send(event_alert_message(event, window))
                    state["event_alerts"][key] = time.time()
        if -3 <= diff <= 1 and event.get("actual") is not None:
            key = f"{event['id']}:NOW"
            if not state["event_alerts"].get(key):
                send(event_alert_message(event, 0))
                state["event_alerts"][key] = time.time()


def check_actual_event_changes(state, events):
    for event in events:
        if event.get("actual") is None:
            continue
        key, actual = str(event["id"]), str(event["actual"])
        if state["last_actual_events"].get(key) == actual:
            continue
        age = (now_utc() - event["time"]).total_seconds() / 60
        if -5 <= age <= 30:
            send(event_alert_message(event, 0))
            state["last_actual_events"][key] = actual


def check_market_open_alerts(state):
    now = now_utc()
    for name, open_dt in next_market_opens():
        diff = (open_dt - now).total_seconds() / 60
        for window in MARKET_OPEN_WINDOWS:
            if window - 3 < diff <= window:
                key = f"{name}:{open_dt.date()}:{window}"
                if state["market_open_alerts"].get(key):
                    continue
                timing = (f"{name} stock market is OPEN now." if window == 0
                          else f"{name} stock market opens in ~{window} minutes.")
                extra = ("\n\n🔥 The US open is usually the most volatile time for BTC.\n"
                         "Expect faster moves. Use stop-losses." if name == "New York" else
                         "\n\nBTC trades 24/7, but this session can change liquidity and volatility.")
                send(f"🌍 MARKET SESSION\n━━━━━━━━━━━━━━━━━━━━\n{timing}{extra}\n\n"
                     "This is a volatility heads-up, not a direction signal.")
                state["market_open_alerts"][key] = time.time()


def check_daily_summary(state):
    n = now_utc()
    today = n.strftime("%Y-%m-%d")
    if n.hour == 0 and n.minute >= 5 and state.get("last_daily") != today:
        cutoff = time.time() - 24 * 3600
        day = [t for t in state["trades"] if (t.get("closed_at") or 0) >= cutoff]
        lines = ["🗓 DAILY SUMMARY", "━━━━━━━━━━━━━━━━━━━━"]
        lines += stats_lines(day, "Last 24 hours")
        lines += [""] + stats_lines(state["trades"], "All time", state.get("account"))
        send("\n".join(lines))
        state["last_daily"] = today


# ============================================================
# REPORT
# ============================================================

def active_trade_lines(t, price):
    long = t["action"] == "LONG"
    if t["status"] == "PENDING":
        exp = datetime.fromtimestamp(t["expires"], UTC).strftime("%H:%M UTC")
        return [f"📌 PENDING {t['action']} - limit at {fmt_price(t['entry'])} "
                f"(price now {fmt_price(price)}). Auto-cancels {exp}.",
                f"Stop {fmt_price(t['stop'])} | TP1 {fmt_price(t['tp1'])} | TP2 {fmt_price(t['tp2'])}"]
    r_now = None
    if price:
        r_now = ((price - t["entry"]) if long else (t["entry"] - price)) / t["risk"]
    return [f"📌 OPEN {t['action']} from {fmt_price(t['entry'])}"
            + (f" | now {r_now:+.2f}R" if r_now is not None else ""),
            f"Stop now {fmt_price(t['current_stop'])} | TP1 {'✅ done' if t['tp1_hit'] else fmt_price(t['tp1'])}"
            f" | TP2 {fmt_price(t['tp2'])}"]


def format_news(news, state):
    if not news:
        return ["News feed is empty right now (will retry)."]
    seen = set(state.get("reported_news", []))
    ordered = sorted(news[:25], key=lambda n: news_key(n) in seen)  # unseen first (stable)
    ordered = sorted(ordered, key=lambda n: {"HIGH": 0, "MEDIUM": 1, "LOW": 2}[n["impact"]])
    lines = []
    for item in ordered[:MAX_NEWS_ITEMS]:
        icon = {"BULLISH": "🟢", "BEARISH": "🔴", "MIXED": "🟡"}.get(item["direction"], "⚪")
        new = "🆕 " if news_key(item) not in seen else ""
        lines.append(f"{icon} {new}{item['title']}\n   {item['source']} | impact: {item['impact']}")
        state["reported_news"].append(news_key(item))
    return lines


def news_mood(news):
    top = [n for n in news[:15] if n["impact"] != "LOW"]
    b = sum(1 for n in top if n["direction"] == "BULLISH")
    r = sum(1 for n in top if n["direction"] == "BEARISH")
    if b > r:
        return f"News mood: positive ({b} bullish vs {r} bearish)"
    if r > b:
        return f"News mood: negative ({r} bearish vs {b} bullish)"
    return "News mood: mixed / neutral"


def upcoming_events_text(events):
    now = now_utc()
    future = [e for e in events if now <= e["time"] <= now + timedelta(days=7)]
    if not future:
        return ["No high-impact events found in the next 7 days."]
    lines = []
    for e in future[:6]:
        mins = int((e["time"] - now).total_seconds() / 60)
        when = (f"in {max(0, mins)} min" if mins < 60 else
                f"in {mins // 60}h {mins % 60}m" if mins < 1440 else
                e["time"].strftime("%a %d %b %H:%M UTC"))
        extra = ""
        if e.get("forecast") is not None:
            extra += f" | expected {e['forecast']}"
        if e.get("previous") is not None:
            extra += f" | previous {e['previous']}"
        lines.append(f"• {e['name']} ({e['currency'] or 'Global'}) - {when}{extra}")
    return lines


def build_report(state):
    c = CTX
    ticker, tech = c.get("ticker") or {}, c.get("tech") or {}
    futures, cross = c.get("futures") or {}, c.get("cross") or {}
    news, events = c.get("news") or [], c.get("events") or []
    bias, levels, plan = c["bias"], c.get("levels") or {}, c["plan"]
    h1, h4 = tech.get("1h") or {}, tech.get("4h") or {}
    m15, m5 = tech.get("15m") or {}, tech.get("5m") or {}
    price = ticker.get("price") or levels.get("price")
    t = state.get("active_trade")

    head = ["₿ BTC UPDATE", utc_text(),
            f"Price: {fmt_price(price)}  |  24h: {fmt_pct(ticker.get('change_24h'))}",
            "━━━━━━━━━━━━━━━━━━━━", "", "🎯 WHAT TO DO NOW"]
    if t:
        head += active_trade_lines(t, price)
        if plan["action"] == "WAIT" or plan["action"] != t["action"]:
            head.append("(No new trade while this one is active.)")
    else:
        head += plan_text_lines(plan, bias)

    summary = list(head) + [
        "",
        f"Mood: {bias['bias']} ({bias['confidence']}%) | 1h: {trend_words(h1.get('trend'))} "
        f"| 4h: {trend_words(h4.get('trend'))}",
    ]
    nxt = upcoming_high_event(events, 7 * 24 * 60)
    if nxt and nxt[1] > 0:
        mins = int(nxt[1])
        when = f"{mins} min" if mins < 60 else f"{mins // 60}h {mins % 60}m"
        summary.append(f"Next big event: {nxt[0]['name']} in {when}")
    ai = ai_news_summary(news)
    summary.append(ai if ai else news_mood(news))
    summary += ["", "Tap a button for full details or a chart.",
                "Not advice. Any trade can lose. The bot never places orders."]

    d = ["📖 FULL DETAILS", "━━━━━━━━━━━━━━━━━━━━",
         f"Overall mood: {bias['bias']} (score {bias['score']:+.1f})", "",
         "Trend (which way is price heading?)",
         f"• Next few minutes (5m): {trend_words(m5.get('trend'))}",
         f"• Next few hours (15m): {trend_words(m15.get('trend'))}",
         f"• Today (1h): {trend_words(h1.get('trend'))}",
         f"• Big picture (4h): {trend_words(h4.get('trend'))}",
         f"• Trend strength (1h): {trend_strength_words(h1.get('adx'))}",
         f"• Momentum (1h): {momentum_words(h1.get('rsi'))}"]

    if levels:
        d += ["", "Key price levels",
              f"• Support (floor): {fmt_price(levels.get('support'))}",
              f"• Resistance (ceiling): {fmt_price(levels.get('resistance'))}",
              f"• Normal 1h swing: about ±{fmt_price(levels['atr'])} (±{levels['atr_pct']:.2f}%)",
              f"• If it breaks UP: {fmt_price(levels['bull1'])} then {fmt_price(levels['bull2'])}",
              f"• If it breaks DOWN: {fmt_price(levels['bear1'])} then {fmt_price(levels['bear2'])}"]

    d += ["", "Futures traders (what the crowd is doing)",
          f"• {funding_words(futures.get('funding'))}",
          f"• {oi_words(futures)}",
          f"• {liq_words(futures)}"]
    d += [f"• {x}" for x in crowd_words(c.get("crowd"))]
    d += [f"• {fng_words(c.get('fng'))}"]

    d += ["", "Outside markets",
          f"• {cross_line('DXY', cross.get('DXY'), False, 'US Dollar (DXY)')}",
          f"• {cross_line('NASDAQ', cross.get('NASDAQ'), True, 'Nasdaq futures')}",
          f"• {cross_line('US10Y', cross.get('US10Y'), False, 'US 10Y yield')}",
          f"• {cross_line('VIX', cross.get('VIX'), False, 'Fear index (VIX)')}",
          f"• {cross_line('GOLD', cross.get('GOLD'), None, 'Gold')}"]

    d += ["", "🧩 WHY THIS MOOD"]
    d += ([f"• {x}" for x in bias["reasons"]] if bias["reasons"]
          else ["• Nothing strong enough to lean either way."])

    d += ["", "⏰ BIG EVENTS - NEXT 7 DAYS"] + upcoming_events_text(events)
    d += ["", f"📰 NEWS ({news_mood(news)})"]
    if ai:
        d += [ai, ""]
    d += format_news(news, state)
    d += [""] + stats_lines(state["trades"], "Track record")
    d += ["", "━━━━━━━━━━━━━━━━━━━━", "⚠️ Read this carefully",
          "• These are probabilities, not promises. Any trade can lose.",
          "• Always use the stop-loss and risk only ~1% per trade.",
          "• A sudden headline, Fed comment or liquidation wave can override everything.",
          "• Information only - this bot NEVER places orders."]

    return "\n".join(summary), "\n".join(d), "\n".join(head + [""] + d[2:])


def send_market_report(state):
    if not CTX.get("bias"):
        send("Still loading market data. Try again in a minute.")
        return
    summary, details, full = build_report(state)
    CTX["details"] = details
    if REPORT_MODE == "full":
        send(full)
    else:
        send(summary, reply_markup=REPORT_BUTTONS)
    if CHART_WITH_REPORT:
        png = make_chart()
        if png:
            send_photo(png, "BTC 15m chart")


# ============================================================
# TELEGRAM COMMANDS
# ============================================================

HELP_TEXT = (
    "🤖 COMMANDS\n"
    "/report  - full update now\n"
    "/status  - quick status + data health\n"
    "/trade   - current trade\n"
    "/stats   - track record (win rate, R)\n"
    "/chart   - chart with levels\n"
    "/account 500 - set your account size in USD\n"
    "/risk 1  - risk per trade in % (0.1 - 3)\n"
    "/pause   - stop new signals\n"
    "/resume  - allow new signals again"
)


def cmd_status(state):
    c = CTX
    if not c.get("bias"):
        send("Still loading market data. Try again in a minute.")
        return
    price = (c.get("ticker") or {}).get("price")
    age = int((time.time() - HEALTH["price"]) / 60)
    lines = [f"📡 STATUS - {utc_text()}",
             f"BTC: {fmt_price(price)}",
             f"Mood: {c['bias']['bias']} (score {c['bias']['score']:+.1f})",
             f"Signal now: {c['plan']['action']}",
             f"Signals: {'PAUSED' if state.get('paused') else 'active'}",
             f"Account: ${state['account']:,.0f} | Risk: {state['risk_pct']}% per trade",
             f"Price data age: {age} min | Liquidation feed: "
             f"{'live websocket' if WS_STATE['connected'] else 'REST fallback'}",
             f"Claude news: {'on' if ANTHROPIC_API_KEY else 'off (no ANTHROPIC_API_KEY)'}"]
    cd = state.get("cooldown_until", 0) - time.time()
    if cd > 0:
        lines.append(f"Cooldown: {cd / 60:.0f} min left")
    t = state.get("active_trade")
    if t:
        lines += [""] + active_trade_lines(t, price)
    send("\n".join(lines))


def handle_command(state, cmd, args):
    if cmd in ("/start", "/help"):
        send(HELP_TEXT)
    elif cmd == "/report":
        send_market_report(state)
    elif cmd == "/details":
        send(CTX.get("details") or "No report yet. Send /report first.")
    elif cmd == "/status":
        cmd_status(state)
    elif cmd == "/trade":
        t = state.get("active_trade")
        send("\n".join(active_trade_lines(t, (CTX.get("ticker") or {}).get("price")))
             if t else "No active trade right now.")
    elif cmd == "/stats":
        send("\n".join(stats_lines(state["trades"], "All-time track record", state["account"])))
    elif cmd == "/chart":
        png = make_chart()
        if png:
            send_photo(png, "BTC 15m chart")
        else:
            send("Chart not available (needs matplotlib and some candle data).")
    elif cmd == "/account":
        v = safe_float(args[0]) if args else None
        if v and 10 <= v <= 100_000_000:
            state["account"] = v
            send(f"✅ Account size set to ${v:,.0f}. New trade plans will use it.")
        else:
            send("Usage: /account 500   (USD)")
    elif cmd == "/risk":
        v = safe_float(args[0]) if args else None
        if v and 0.1 <= v <= 3:
            state["risk_pct"] = v
            send(f"✅ Risk per trade set to {v}%.")
        else:
            send("Usage: /risk 1   (between 0.1 and 3 percent)")
    elif cmd == "/pause":
        state["paused"] = True
        send("⏸ New signals paused. Open trades are still tracked. /resume to restart.")
    elif cmd == "/resume":
        state["paused"] = False
        send("▶️ Signals resumed.")


def handle_update(state, u):
    if "callback_query" in u:
        cq = u["callback_query"]
        tg_post("answerCallbackQuery", {"callback_query_id": cq["id"]})
        chat = str(cq.get("message", {}).get("chat", {}).get("id", ""))
        if chat != str(CHAT_ID):
            return
        if cq.get("data") == "details":
            handle_command(state, "/details", [])
        elif cq.get("data") == "chart":
            handle_command(state, "/chart", [])
        return

    msg = u.get("message") or {}
    if str(msg.get("chat", {}).get("id", "")) != str(CHAT_ID):
        return  # ignore strangers
    text = (msg.get("text") or "").strip()
    if not text.startswith("/"):
        return
    parts = text.split()
    handle_command(state, parts[0].split("@")[0].lower(), parts[1:])


def poll_updates(state, timeout=20):
    """Long-poll Telegram so commands are answered within seconds."""
    if not TOKEN:
        time.sleep(min(timeout, 5))
        return
    try:
        r = session.post(
            f"https://api.telegram.org/bot{TOKEN}/getUpdates",
            data={"offset": state.get("tg_offset", 0), "timeout": timeout,
                  "allowed_updates": json.dumps(["message", "callback_query"])},
            timeout=timeout + 10,
        )
        j = r.json()
    except Exception as e:
        print("getUpdates error:", str(e)[:100])
        time.sleep(3)
        return
    if not j.get("ok"):
        print("getUpdates not ok:", str(j)[:200])
        time.sleep(5)
        return
    for u in j.get("result", []):
        state["tg_offset"] = u["update_id"] + 1
        try:
            handle_update(state, u)
        except Exception:
            traceback.print_exc()


def idle(state, seconds):
    end = time.time() + seconds
    while time.time() < end and not STOP_FLAG["stop"]:
        poll_updates(state, timeout=int(clamp(end - time.time(), 1, 20)))


# ============================================================
# BACKTEST  (technical rules only - news/macro/futures history
#            is not available, so those factors are excluded)
# ============================================================

def fetch_history_15m(days):
    need = days * 96
    rows, cur_end = [], int(time.time() * 1000)

    while len(rows) < need:
        data = None
        for base in BINANCE_SPOT_HOSTS:
            data = http_json(base + "/api/v3/klines",
                             {"symbol": BTC_SYMBOL, "interval": "15m",
                              "limit": 1000, "endTime": cur_end})
            if data:
                break
        if not data:
            break
        rows = [r[:6] for r in data] + rows
        cur_end = int(data[0][0]) - 1
        if len(data) < 1000:
            break
        time.sleep(0.15)

    if len(rows) < 1500:  # Binance blocked -> OKX history
        print("Binance history unavailable, using OKX (slower)...")
        rows, after = [], None
        while len(rows) < need:
            params = {"instId": "BTC-USDT", "bar": "15m", "limit": 100}
            if after:
                params["after"] = after
            d = http_json(OKX + "/api/v5/market/history-candles", params)
            batch = (d or {}).get("data") or []
            if not batch:
                break
            rows = [r[:6] for r in reversed(batch)] + rows
            after = batch[-1][0]
            time.sleep(0.12)

    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=KLINE_COLS)
    for c in KLINE_COLS:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna().drop_duplicates("time").sort_values("time").reset_index(drop=True)


def run_backtest_on_df(df15, verbose=True):
    ind = add_indicators(df15)
    ind["dt"] = pd.to_datetime(ind["time"], unit="ms", utc=True)
    ind["close_dt"] = ind["dt"] + pd.Timedelta("15min")

    def resampled(rule):
        o = (ind.set_index("dt").resample(rule, label="left", closed="left")
             .agg({"open": "first", "high": "max", "low": "min",
                   "close": "last", "volume": "sum"}).dropna().reset_index())
        o = add_indicators(o)
        o["avail"] = o["dt"] + pd.Timedelta(rule)
        return o

    cols = ["close", "ema9", "ema21", "ema50", "ema200", "rsi", "atr", "atr_pct",
            "adx", "bb_width", "volume_ratio", "support_50", "resistance_50"]

    def aligned(rule):
        r = resampled(rule)
        merged = pd.merge_asof(ind[["close_dt"]], r[["avail"] + cols],
                               left_on="close_dt", right_on="avail", direction="backward")
        return merged[cols].to_dict("records")

    rec15 = ind[cols].to_dict("records")
    rec1h, rec4h = aligned("1h"), aligned("4h")
    highs, lows = ind["high"].values, ind["low"].values
    times = (ind["time"].values / 1000.0 + 900.0)  # candle close time (s)

    trades, active, cooldown_until, blocked_until = [], None, 0.0, 0.0
    warm = 1000

    for i in range(warm, len(ind)):
        ts = float(times[i])

        if active is not None:
            advance_trade(active, float(highs[i]), float(lows[i]), ts)
            if active["status"] in ("CLOSED", "EXPIRED"):
                if active["status"] == "CLOSED" and active["outcome"] == "STOP":
                    cooldown_until = ts + COOLDOWN_MIN * 60
                blocked_until = ts + REENTRY_MIN * 60
                trades.append(active)
                active = None
            else:
                continue

        if ts < blocked_until:
            continue
        if any(rec1h[i].get(k) is None or pd.isna(rec1h[i].get(k)) for k in ("ema50", "rsi", "atr")):
            continue
        if rec4h[i].get("ema50") is None or pd.isna(rec4h[i].get("ema50")):
            continue

        tech = {"15m": snapshot(rec15[i]), "1h": snapshot(rec1h[i]), "4h": snapshot(rec4h[i])}
        score, reasons = tech_score(tech)
        bias = bias_from_score(score, reasons)
        levels = scenario_levels(tech, tech["15m"]["price"])
        plan = build_trade_plan(bias, tech, levels, [],
                                cooldown_min=max(0.0, (cooldown_until - ts) / 60))
        if plan["action"] in ("LONG", "SHORT"):
            active = new_trade(plan, ts)

    if active is not None:
        trades.append(active)

    if verbose:
        days = (ind["time"].iloc[-1] - ind["time"].iloc[warm]) / 86_400_000
        print(f"\nBacktest period: ~{days:.0f} days | candles: {len(ind)}")
        for line in stats_lines(trades, "BACKTEST RESULT (technical rules, after fees)"):
            print(line)
        s = trade_stats(trades)
        if s["n"]:
            print(f"Trades per month: {s['n'] / max(days / 30, 0.1):.1f}")
        print("\nNOTE: past results do not guarantee future results. This excludes "
              "news/macro/futures factors and assumes the stop is hit first when "
              "stop and target are in the same candle.")
    return trades


def run_backtest(days=180):
    print(f"Downloading ~{days} days of 15m BTC candles...")
    df = fetch_history_15m(days)
    if len(df) < 2000:
        print("Not enough data downloaded. Check your internet/exchange access.")
        return
    trades = run_backtest_on_df(df)
    if trades:
        with open("backtest_trades.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["action", "created_utc", "entry", "stop", "tp1", "tp2",
                        "status", "outcome", "result_R"])
            for t in trades:
                w.writerow([t["action"], datetime.fromtimestamp(t["created"], UTC).isoformat(),
                            round(t["entry"], 2), round(t["stop"], 2), round(t["tp1"], 2),
                            round(t["tp2"], 2), t["status"], t["outcome"], t["result_r"]])
        print("Saved: backtest_trades.csv")


# ============================================================
# HEALTH / STARTUP
# ============================================================

def check_health(state):
    stale_min = (time.time() - HEALTH["price"]) / 60
    if stale_min > 10 and not state.get("health_alert"):
        send("⚠️ DATA PROBLEM\nNo fresh BTC price data for "
             f"{stale_min:.0f} minutes (exchange APIs may be blocked or down).\n"
             "Signals are unreliable until this recovers. Existing trades: follow your stop-loss.")
        state["health_alert"] = True
    elif stale_min <= 10 and state.get("health_alert"):
        send("✅ Data feeds recovered. Normal operation resumed.")
        state["health_alert"] = False


def send_startup(restart=False):
    if restart:
        send("🔄 Bot restarted after an error and is running again. Open trades are restored.")
        return
    send(
        "₿ BTC MARKET BOT v3 STARTED\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "• A clear action every 30 min: LONG / SHORT / WAIT\n"
        "• Limit-entry, stop-loss, TP1/TP2 and position size\n"
        "• Follow-up messages: fill, TP1 -> breakeven, trailing, exit\n"
        "• Instant alerts: trade setups, big news, market opens\n"
        "• Track record of every signal (/stats)\n\n"
        "Type /help for commands.\n"
        "⚠️ No guaranteed predictions. The bot never places trades."
    )


# ============================================================
# MAIN LOOP
# ============================================================

def run_bot(state, deadline, restart=False):
    send_startup(restart)
    start_liq_ws()

    t_fast = t_slow = t_cal = 0
    tech, futures, cross, news, events, ticker = {}, {}, {}, [], [], {}
    fng, crowd = None, {}
    errors = 0
    CTX["state"] = state

    while time.time() < deadline and not STOP_FLAG["stop"]:
        try:
            now = time.time()

            if now - t_fast >= DATA_POLL_INTERVAL:
                tech = build_technical_context()
                futures = get_futures_context(state)
                ticker = get_btc_ticker()
                if ticker.get("price"):
                    HEALTH["price"] = time.time()
                t_fast = now

            if now - t_slow >= SLOW_POLL_INTERVAL:
                cross = get_cross_market()
                news = fetch_news()
                fng = get_fear_greed()
                crowd = get_crowd_context()
                check_breaking_news(state, news)
                t_slow = now

            if now - t_cal >= CALENDAR_POLL_INTERVAL:
                new_events = fetch_calendar()
                if new_events:
                    events = new_events
                t_cal = now

            check_event_alerts(state, events)
            check_actual_event_changes(state, events)
            check_market_open_alerts(state)
            check_daily_summary(state)
            check_health(state)

            if (tech.get("1h") or {}).get("price") is not None:
                live_price = ticker.get("price")
                bias = calculate_bias(tech, futures, cross, news, fng, crowd)
                levels = scenario_levels(tech, live_price)
                cd_min = max(0.0, (state.get("cooldown_until", 0) - time.time()) / 60)
                plan = build_trade_plan(bias, tech, levels, events,
                                        account=state["account"], risk_pct=state["risk_pct"],
                                        cooldown_min=cd_min, paused=state.get("paused", False))
                CTX.update(ticker=ticker, tech=tech, futures=futures, cross=cross,
                           news=news, events=events, bias=bias, levels=levels,
                           plan=plan, fng=fng, crowd=crowd)

                manage_active_trade(state, live_price)
                process_signal(state, plan, bias, live_price)

                if state["last_report"] == 0 or now - state["last_report"] >= REPORT_INTERVAL:
                    send_market_report(state)
                    state["last_report"] = now

                print(datetime.now().strftime("%H:%M:%S"), "| BTC:", live_price,
                      "| Bias:", bias["bias"], f"({bias['score']:+.1f})",
                      "| Action:", plan["action"],
                      "| Trade:", (state.get("active_trade") or {}).get("status"),
                      "| Events:", len(events))

            save_state(state)
            errors = 0

        except Exception:
            traceback.print_exc()
            errors += 1
            if errors == 5:
                send("⚠️ The bot hit 5 errors in a row. It keeps retrying; "
                     "check the logs if this persists.")

        idle(state, DATA_POLL_INTERVAL)

    save_state(state)
    send("🛑 BTC MARKET BOT SESSION FINISHED\n━━━━━━━━━━━━━━━━━━━━\n"
         f"Runtime: {RUN_SECONDS // 60} minutes. State saved.\n"
         "Open trades (if any) continue when the bot starts again.\n"
         "Signals only - no orders were placed.")


def _handle_sigterm(signum, frame):
    STOP_FLAG["stop"] = True


def main():
    if "--backtest" in sys.argv:
        i = sys.argv.index("--backtest")
        days = int(sys.argv[i + 1]) if len(sys.argv) > i + 1 and sys.argv[i + 1].isdigit() else 180
        run_backtest(days)
        return

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _handle_sigterm)
        except Exception:
            pass

    state = load_state()
    deadline = time.time() + RUN_SECONDS
    restarts = 0

    while not STOP_FLAG["stop"] and time.time() < deadline:
        try:
            run_bot(state, deadline, restart=restarts > 0)
            break
        except Exception:
            err = traceback.format_exc()
            print(err)
            save_state(state)
            restarts += 1
            last = err.strip().splitlines()[-1][:200]
            send(f"💥 BOT CRASHED: {last}\nRestarting in 20 seconds (attempt {restarts}/5).")
            if restarts >= 5:
                send("❌ Too many crashes. The bot has stopped - please check it.")
                break
            time.sleep(20)

    save_state(state)


if __name__ == "__main__":
    main()
