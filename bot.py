import os
import sys
import io
import re
import csv
import json
import math
import time
import hashlib
import html as _html
import signal
import threading
import traceback
import xml.etree.ElementTree as ET
from urllib.parse import urlparse, quote
from collections import deque
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests


# ============================================================
# BTC MARKET INTELLIGENCE TELEGRAM BOT  (v3.3)
# ------------------------------------------------------------
#  1. Signal tracking: every LONG/SHORT is logged and followed
#     until it hits stop / TP1 / TP2; win-rate + R stats
#  2. Backtest:  python bot.py --backtest 180
#  3. Better signals: ADX range filter, volume filter, cooldown
#     after a loss, limit-pullback entries, volatility sizing
#  4. Trade management messages (TP1 -> breakeven, trailing, ...)
#  5. More data: Fear & Greed, taker ratio, long/short ratio,
#     live liquidation websocket
#  6. Smarter news: de-duplicated breaking alerts + optional
#     Claude summary (ANTHROPIC_API_KEY)
#  7. Telegram commands/buttons/charts, crash alerts, health
#     checks, atomic state saves
#  8. NEW: headline card + tabs (Trend / Futures / Macro / News /
#     Levels / Stats / Glossary) that edit one message in place
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

# "short" = headline card + tabs, "full" = everything in one go
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
MAKER_FEE = 0.0002             # limit orders (entry, take-profit)
TAKER_FEE = 0.0004             # market/stop orders
SLIPPAGE = 0.0002              # 0.02% extra cost on stop-type exits
MAX_LEVERAGE = float(os.environ.get("MAX_LEVERAGE", "3"))   # position size cap vs account
SIGNAL_CONFIRM = int(os.environ.get("SIGNAL_CONFIRM", "3"))  # consecutive checks before alert
STALE_MIN = 15                 # fallback cache max age (minutes)
FEATURE_LOG = os.environ.get("FEATURE_LOG", "btc_signal_log.jsonl")

# ---- data sources ------------------------------------------------
BINANCE_SPOT_HOSTS = [
    "https://data-api.binance.vision",
    "https://api.binance.com",
    "https://api1.binance.com",
]
BINANCE_FUTURES = "https://fapi.binance.com"
OKX = "https://www.okx.com"
BYBIT = "https://api.bybit.com"
DERIBIT = "https://www.deribit.com"
COINGECKO = "https://api.coingecko.com"

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
    "User-Agent": "BTC-Market-Intelligence-Bot/3.3",
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
GLOBAL_CACHE = {"ts": 0, "data": None}
OPT_CACHE = {"ts": 0, "data": None}
FF_CACHE = {"ts": 0, "events": None}
TF_TS = {}                         # last fetch time of slow timeframes (1d, 1w)
SOURCE_HEALTH = {}                 # per-host request success/failure counters
LANG_CACHE = {}

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
    return fmt_local(dt or now_utc())


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
    """GET json with one retry. Geo-blocks / auth errors are not retried.
    Success/failure per host is tracked for the /health command."""
    for attempt in range(retries + 1):
        try:
            r = session.get(url, params=params or {}, headers=headers, timeout=timeout)
            if r.status_code in (400, 401, 403, 404, 451):
                print("HTTP", r.status_code, url[:90])
                _track(url, False)
                return None
            if r.status_code == 429:
                time.sleep(min(float(r.headers.get("Retry-After", 2)), 5))
                continue
            r.raise_for_status()
            data = r.json()
            _track(url, True)
            return data
        except Exception as e:
            print("HTTP error:", url[:90], str(e)[:100])
            if attempt < retries:
                time.sleep(0.8 * (attempt + 1))
    _track(url, False)
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


def send(msg, reply_markup=None, level="normal"):
    return tg_send(msg, reply_markup, False, level) is not None


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
        "last_card": None,
        "price_hist": [],
        "event_reactions": {},
        "sig_streak": {"action": None, "count": 0},
        "lang": "en",
        "tz": "UTC",
        "quiet": None,
        "alert_mode": "all",
        "watchlist": [],
        "alerts": [],
        "alert_seq": 0,
        "decisions": {},
        "tick_hist": {},
        "trade_msg": None,
        "banner": None,
        "plan_msg": None,
        "weekend_msg": None,
        "health_msg": None,
        "last_plan_date": "",
        "last_weekend_fri": "",
        "last_weekend_sun": "",
        "last_weekly": "",
        "guard_notice": "",
        "pump_alert_times": [],
        "pump_cooldown": {},
        "onboarded": False,
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
        state["price_hist"] = [p for p in state.get("price_hist", [])
                               if time.time() - p[0] < 3 * 3600][-400:]
        _trim_state(state)
        for k in ("event_alerts", "market_open_alerts", "last_actual_events"):
            if len(state[k]) > 500:
                state[k] = dict(list(state[k].items())[-300:])

        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=1)
        os.replace(tmp, STATE_FILE)  # atomic: never leaves a half-written file
    except Exception:
        traceback.print_exc()


# ============================================================
# PRICE DATA  (Binance -> OKX -> Coinbase)
# ============================================================

OKX_BAR = {"5m": "5m", "15m": "15m", "1h": "1H", "4h": "4H", "1d": "1Dutc", "1w": "1Wutc"}
KLINE_COLS = ["time", "open", "high", "low", "close", "volume"]


def _klines_binance(interval, limit, symbol=BTC_SYMBOL):
    for base in BINANCE_SPOT_HOSTS:
        data = http_json(base + "/api/v3/klines",
                         {"symbol": symbol, "interval": interval, "limit": limit})
        if isinstance(data, list) and data:
            return pd.DataFrame([row[:6] for row in data], columns=KLINE_COLS)
    return pd.DataFrame()


def _klines_okx(interval, limit, symbol=BTC_SYMBOL):
    inst = symbol[:-4] + "-USDT" if symbol.endswith("USDT") else "BTC-USDT"
    data = http_json(OKX + "/api/v5/market/candles",
                     {"instId": inst, "bar": OKX_BAR[interval], "limit": min(limit, 300)})
    rows = (data or {}).get("data") or []
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([r[:6] for r in rows], columns=KLINE_COLS)
    return df.iloc[::-1].reset_index(drop=True)


def get_klines(interval="5m", limit=500, symbol=BTC_SYMBOL):
    df = _klines_binance(interval, limit, symbol)
    if df.empty:
        df = _klines_okx(interval, limit, symbol)
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
    for tf, limit in [("5m", 300), ("15m", 300), ("1h", 300), ("4h", 250),
                      ("1d", 300), ("1w", 250)]:
        ttl = SLOW_TFS.get(tf)
        if ttl and TECH_CACHE.get(tf) and time.time() - TF_TS.get(tf, 0) < ttl:
            result[tf] = TECH_CACHE[tf]
            continue
        if ttl:
            TF_TS[tf] = time.time()
        df = add_indicators(get_klines(tf, limit))
        if len(df) < (60 if ttl else 80):
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
           "long_liquidation": None, "short_liquidation": None, "stale": []}
    oi_src = None

    p = http_json(BINANCE_FUTURES + "/fapi/v1/premiumIndex", {"symbol": BTC_SYMBOL})
    if isinstance(p, dict):
        res["funding"] = safe_float(p.get("lastFundingRate"))
    o = http_json(BINANCE_FUTURES + "/fapi/v1/openInterest", {"symbol": BTC_SYMBOL})
    if isinstance(o, dict):
        res["open_interest"] = safe_float(o.get("openInterest"))
        if res["open_interest"] is not None:
            oi_src = "binance"

    if res["funding"] is None or res["open_interest"] is None:
        b = http_json(BYBIT + "/v5/market/tickers", {"category": "linear", "symbol": BTC_SYMBOL})
        try:
            item = b["result"]["list"][0]
            if res["funding"] is None:
                res["funding"] = safe_float(item.get("fundingRate"))
            if res["open_interest"] is None:
                res["open_interest"] = safe_float(item.get("openInterest"))
                if res["open_interest"] is not None:
                    oi_src = "bybit"
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
            if res["open_interest"] is not None:
                oi_src = "okx"
        except Exception:
            pass

    now = time.time()
    if res["open_interest"] is not None and oi_src:
        hist = state.setdefault("oi_history", [])
        hist.append([now, res["open_interest"], oi_src])
        state["oi_history"] = hist = [h for h in hist if now - h[0] < 3 * 3600]
        # Only compare readings from the SAME exchange (units/definitions differ).
        older = [h for h in hist if len(h) > 2 and h[2] == oi_src and h[0] <= now - 5 * 60]
        if older:
            ref = min(older, key=lambda h: abs(h[0] - (now - 30 * 60)))
            res["oi_change_pct"] = pct_change(res["open_interest"], ref[1])
            res["oi_window_min"] = int((now - ref[0]) / 60)
        res["oi_source"] = oi_src

    lng, sht = liq_from_ws()
    if lng is None:
        lng, sht = _okx_liquidations()
    res["long_liquidation"], res["short_liquidation"] = lng, sht

    # Short-lived fallback cache: reuse a value only if it is < STALE_MIN old, and say so.
    for k in ("funding", "open_interest"):
        if res[k] is not None:
            FUT_CACHE[k] = (res[k], now)
        elif k in FUT_CACHE and now - FUT_CACHE[k][1] < STALE_MIN * 60:
            res[k] = FUT_CACHE[k][0]
            res["stale"].append(k)
    return res


def get_crowd_context():
    """Taker buy/sell pressure + long/short account ratio (OKX public stats)."""
    out = {"taker_buy_ratio": None, "ls_ratio": None}

    d = http_json(OKX + "/api/v5/rubik/stat/taker-volume",
                  {"ccy": "BTC", "instType": "CONTRACTS", "period": "5m"})
    try:
        rows = sorted(d["data"], key=lambda r: float(r[0]), reverse=True)[:6]  # newest first
        sell = sum(float(r[1]) for r in rows)
        buy = sum(float(r[2]) for r in rows)
        if buy + sell > 0:
            out["taker_buy_ratio"] = buy / (buy + sell)
    except Exception:
        pass

    d = http_json(OKX + "/api/v5/rubik/stat/contracts/long-short-account-ratio",
                  {"ccy": "BTC", "period": "5m"})
    try:
        rows = sorted(d["data"], key=lambda r: float(r[0]), reverse=True)
        out["ls_ratio"] = float(rows[0][1])
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
            q["ts"] = time.time()
            CROSS_CACHE[name] = q
            result[name] = q
        else:
            old = CROSS_CACHE.get(name)
            if old and time.time() - old.get("ts", 0) < 30 * 60:
                result[name] = dict(old, stale=True)
            else:
                result[name] = {}
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
    text = f"{item.get('title', '')} {item.get('description', '')}"
    hs = sum(1 for rx in _HIGH_RX if rx.search(text))
    bs = sum(1 for rx in _BULL_RX if rx.search(text))
    rs = sum(1 for rx in _BEAR_RX if rx.search(text))

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

CAL_ALL_CCY = os.environ.get("CAL_ALL_CCY", "0") == "1"   # 1 = also EUR/JPY/GBP/CNY events
CALENDAR_CURRENCIES = (("USD", "US", "GLOBAL", "ALL", "") if not CAL_ALL_CCY else
                       ("USD", "US", "GLOBAL", "EUR", "EU", "JPY", "CNY", "CN", "GBP", "ALL", ""))


def _none_if_empty(v):
    if v is None:
        return None
    v = str(v).strip()
    return v or None


def fetch_calendar_ff():
    if FF_CACHE["events"] is not None and time.time() - FF_CACHE["ts"] < _ff_ttl():
        return FF_CACHE["events"]
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
    if not got_any:
        return FF_CACHE["events"]   # keep serving the last good copy (or None -> fallback source)
    FF_CACHE.update(ts=time.time(), events=events)
    return events


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
    stale = " (stale)" if "open_interest" in (futures.get("stale") or []) else ""
    base = f"Open interest: {oi:,.0f} BTC{stale}"
    if ch is None:
        return base + " (change shows after ~5 min of history from the same exchange)"
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
    stale = " (stale)" if a.get("stale") else ""
    ch = a.get("change")
    price_txt = f"{a['price']:,.2f}"
    if ch is None:
        return f"{label}: {price_txt}{stale}"
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
    return f"{label}: {price_txt} ({ch:+.2f}%) -> {meaning}{stale}"


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
                     risk_pct=RISK_PCT_DEFAULT, cooldown_min=0.0, paused=False, guard_text=None):
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
    elif guard_text:
        why.append(guard_text)
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
        max_size = account * max_lev_now() / entry if entry > 0 else size_btc
        capped = size_btc > max_size
        if capped:
            size_btc = max_size
            risk_usd = size_btc * risk      # real risk after the cap

        plan.update({
            "entry": entry, "stop": stop, "tp1": tp1, "tp2": tp2,
            "risk": risk, "risk_pct": risk / price * 100,
            "rr1": RR1, "rr2": RR2,
            "risk_mult": mult, "risk_used_pct": used_pct,
            "vol_note": ("high volatility -> position size reduced" if mult < 1 else ""),
            "account": account, "risk_usd": risk_usd,
            "size_btc": size_btc, "notional": size_btc * entry,
            "leverage": (size_btc * entry / account) if account else 0, "capped": capped,
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
            f"- setup strength {strength_stars(bias['score'])}",
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
        lev = plan.get("leverage") or 0
        lines += [
            "",
            f"💰 Position size: risk ${plan['risk_usd']:,.2f} "
            f"({plan['risk_usd'] / plan['account'] * 100:.2f}% of ${plan['account']:,.0f})"
            + (f"  ({plan['vol_note']})" if plan.get("vol_note") else ""),
            f"-> size ~{plan['size_btc']:.4f} BTC (~${plan['notional']:,.0f} position, ≈{lev:.1f}x leverage)",
        ]
        if plan.get("capped"):
            lines.append(f"⚠️ Size was capped at {max_lev_now():.1f}x leverage, so your real risk is "
                         "smaller than planned. The stop is wide for this account size.")
        elif lev > 1:
            lines.append(f"Needs ≈{lev:.1f}x leverage / margin - set this on your exchange first.")
        lines.append("Change account size with /account 500, risk with /risk 0.5")
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
    """Result in R after realistic costs: maker fee on entry/TP exits,
    taker fee + slippage on stop-type exits."""
    is_long = t["action"] == "LONG"
    risk, entry = t["risk"], t["entry"]
    taker_exit = outcome in ("STOP", "BREAKEVEN", "TRAIL")
    px = exit_price
    if taker_exit:
        px = exit_price * (1 - SLIPPAGE) if is_long else exit_price * (1 + SLIPPAGE)

    def r_of(p):
        return ((p - entry) if is_long else (entry - p)) / risk

    if t["tp1_hit"]:
        r = TP1_FRACTION * t["rr1"] + (1 - TP1_FRACTION) * r_of(px)
        exit_fee = TP1_FRACTION * MAKER_FEE + (1 - TP1_FRACTION) * (TAKER_FEE if taker_exit else MAKER_FEE)
    else:
        r = r_of(px)
        exit_fee = TAKER_FEE if taker_exit else MAKER_FEE

    fee_r = (MAKER_FEE + exit_fee) * entry / risk
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
    if s["n"] < 100:
        lines.append(f"⚠️ Only {s['n']} closed trades - too few to trust the win rate (aim for 100+).")
    return lines


# ---------- live trade management --------------------------------

def finish_trade(state, t):
    state["last_close_ts"] = time.time()
    if t["status"] == "CLOSED":
        log_trade_csv(t)
        if t["outcome"] == "STOP":
            state["cooldown_until"] = time.time() + COOLDOWN_MIN * 60
    log_trade_features(t)
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
        send(trade_event_text(t, name, price, state), level="critical")

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

    # Signal persistence: the same LONG/SHORT must hold for several checks in a row.
    sig = plan["action"] if plan["action"] in ("LONG", "SHORT") else None
    streak = state.setdefault("sig_streak", {"action": None, "count": 0})
    if sig and streak.get("action") == sig:
        streak["count"] = streak.get("count", 0) + 1
    else:
        streak["action"], streak["count"] = sig, (1 if sig else 0)

    if t:
        direction = 1 if t["action"] == "LONG" else -1
        if t["status"] == "PENDING":
            if upcoming_high_event(CTX.get("events") or [], 20):
                send(f"❎ SETUP CANCELLED - {t['action']}\n"
                     "Big news is about to be released. Cancel the unfilled limit order "
                     "and wait for the move to settle.", level="critical")
                t.update(status="EXPIRED", outcome="CANCELLED", closed_at=now)
                finish_trade(state, t)
                return
            faded = plan["action"] != t["action"] and (
                plan["action"] != "WAIT" or abs(bias["score"]) < 1.0
                or bias["score"] * direction < 0)
            if faded:
                send(f"❎ SETUP CANCELLED - {t['action']}\n"
                     "Conditions changed before the limit order filled. Cancel the order.", level="critical")
                t.update(status="EXPIRED", outcome="CANCELLED", closed_at=now)
                finish_trade(state, t)
        elif t["status"] == "OPEN" and not t.get("warned_flip"):
            if bias["score"] * direction <= -2:
                send(f"⚠️ CONDITIONS FLIPPED against your open {t['action']}.\n"
                     "Keep your stop-loss. You may close early if you prefer to be safe.", level="critical")
                t["warned_flip"] = True
        return

    if now - state.get("last_close_ts", 0) < REENTRY_MIN * 60:
        return  # short breather after any trade ends

    if sig and streak["count"] >= confirm_needed():
        t = new_trade(plan, now)
        t["features"] = signal_features(bias, plan)
        state["active_trade"] = t
        streak["count"] = 0
        lines = ["🚨 TRADE ALERT", "━━━━━━━━━━━━━━━━━━━━"]
        lines += plan_text_lines(plan, bias)
        lines += ["", "You will get follow-up messages for fill, TP1, trailing stop and exit.",
                  "Tap a button below, or mark whether you took the trade.",
                  "Information only - the bot never places trades."]
        send("\n".join(lines), reply_markup=trade_buttons(t["id"]), level="critical")
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
    lines += ["", f"BTC reading: {direction}", explanation]
    rt = reaction_text(CTX.get("state") or {}, event["name"])
    if rt:
        lines.append(rt)
    lines += ["", todo, "⚠️ The first reaction can reverse quickly."]
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
        lines = ["🗓 DAILY RECAP", "━━━━━━━━━━━━━━━━━━━━"]
        lines += stats_lines(day, "Last 24 hours") + _best_worst(day)
        lines += [""] + stats_lines(state["trades"], "All time", state.get("account"))
        lines += my_stats_lines(state)
        bad = [h for h, v in SOURCE_HEALTH.items()
               if v["ok"] + v["fail"] >= 5 and v["fail"] / (v["ok"] + v["fail"]) > 0.5]
        if bad:
            lines += ["", "⚠️ Data sources failing often: " + ", ".join(bad) + "  (see /health)"]
        send("\n".join(lines), level="low")
        state["last_daily"] = today


# ============================================================
# REPORT
# ============================================================

def active_trade_lines(t, price):
    long = t["action"] == "LONG"
    if t["status"] == "PENDING":
        exp = fmt_local(datetime.fromtimestamp(t["expires"], UTC), "%H:%M")
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
                fmt_local(e["time"], "%a %d %b %H:%M"))
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
        f"Mood: {bias['bias']} (strength {strength_stars(bias['score'])}) | 1h: {trend_words(h1.get('trend'))} "
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
    if REPORT_MODE == "full":
        summary, details, full = build_report(state)
        CTX["details"] = details
        send(full)
    else:
        send_card(state)
    if CHART_WITH_REPORT:
        png = make_chart()
        if png:
            send_photo(png, "BTC 15m chart")


# ============================================================
# v3.3 FEATURES: coin intelligence, weekend outlook, pinning, UX
# ============================================================

PUMP_1H_PCT = float(os.environ.get("PUMP_1H_PCT", "6"))        # alt-coin 1h move that triggers an alert


PUMP_MIN_VOL = float(os.environ.get("PUMP_MIN_VOL", "20000000"))  # min 24h USDT volume for alerts


DAILY_LOSS_LIMIT_R = float(os.environ.get("DAILY_LOSS_LIMIT", "3"))


MAX_TRADES_PER_DAY = int(os.environ.get("MAX_TRADES_PER_DAY", "4"))


WEEKEND_MODE = os.environ.get("WEEKEND_MODE", "1") == "1"      # stricter signals on Sat/Sun


USER_TZ = {"zone": UTC}


TICKERS = {"ts": 0, "data": {}}


EXPLAIN_CACHE = {}


MAJOR_PUMP_PCT = {"BTC": 2.0, "ETH": 3.0, "BNB": 3.0, "XRP": 4.0}


BOT_COMMANDS = [
    ("report", "Market card now"), ("why", "Why did a coin pump/dump? /why SOL"),
    ("coin", "Quick analysis of any coin /coin SOL"), ("movers", "Top gainers and losers"),
    ("weekend", "Weekend / market-closed outlook"), ("plan", "Today's plan"),
    ("watch", "Watchlist /watch SOL ETH"), ("alert", "Price alerts /alert 70000"),
    ("trade", "Current trade"), ("stats", "Track record"), ("status", "Bot status"),
    ("health", "Data source health"), ("tz", "Set timezone /tz Asia/Karachi"),
    ("quiet", "Quiet hours /quiet 00:00-07:00"), ("lang", "Language en|simple|ur"),
    ("help", "All commands"),
]


def apply_user_tz(state):
    try:
        USER_TZ["zone"] = ZoneInfo((state or {}).get("tz") or "UTC")
    except Exception:
        USER_TZ["zone"] = UTC


def local_now(state=None):
    return now_utc().astimezone(USER_TZ["zone"])


def fmt_local(dt, fmt="%Y-%m-%d %H:%M"):
    d = dt.astimezone(USER_TZ["zone"])
    return d.strftime(fmt) + " " + (d.tzname() or "")


def _parse_quiet(q):
    try:
        a, b = str(q).split("-")
        h1, m1 = a.split(":")
        h2, m2 = b.split(":")
        s, e = int(h1) * 60 + int(m1), int(h2) * 60 + int(m2)
        if 0 <= s < 1440 and 0 <= e < 1440 and s != e:
            return s, e
    except Exception:
        pass
    return None


def in_quiet(state):
    q = _parse_quiet((state or {}).get("quiet"))
    if not q:
        return False
    lt = local_now()
    m = lt.hour * 60 + lt.minute
    s, e = q
    return (s <= m < e) if s < e else (m >= s or m < e)


def is_weekend():
    return now_utc().weekday() >= 5


def max_lev_now():
    return MAX_LEVERAGE * (0.67 if (WEEKEND_MODE and is_weekend()) else 1.0)


def confirm_needed():
    return SIGNAL_CONFIRM + (2 if (WEEKEND_MODE and is_weekend()) else 0)


def _silent(level):
    st = CTX.get("state") or {}
    if level == "critical":
        return False
    if in_quiet(st):
        return True
    return st.get("alert_mode") == "important" and level == "low"


def tg_send(msg, reply_markup=None, html=False, level="normal"):
    """Send a message; returns the message_id (or None). level: critical | normal | low.
    Quiet hours / 'important only' mode deliver non-critical messages silently (no sound)."""
    if not TOKEN or not CHAT_ID:
        print("[Telegram not configured] ->\n" + (_plain(msg) if html else msg) + "\n")
        return None
    chunks = [msg[:4000]] if html else split_message(msg)
    last_id = None
    for i, chunk in enumerate(chunks):
        data = {"chat_id": CHAT_ID, "text": chunk, "disable_web_page_preview": "true"}
        if html:
            data["parse_mode"] = "HTML"
        if _silent(level):
            data["disable_notification"] = "true"
        if reply_markup and i == len(chunks) - 1:
            data["reply_markup"] = json.dumps(reply_markup)
        res = tg_post("sendMessage", data)
        if (not res or not res.get("ok")) and html:
            data.pop("parse_mode", None)
            data["text"] = _plain(chunk)
            res = tg_post("sendMessage", data)
        if res and res.get("ok"):
            last_id = (res.get("result") or {}).get("message_id", last_id)
    return last_id


def tg_edit(mid, text, markup=None, html=True):
    if not mid or not TOKEN:
        return False
    data = {"chat_id": CHAT_ID, "message_id": mid, "text": text[:4000],
            "disable_web_page_preview": "true"}
    if html:
        data["parse_mode"] = "HTML"
    if markup:
        data["reply_markup"] = json.dumps(markup)
    res = tg_post("editMessageText", data)
    if res is None and html:
        data.pop("parse_mode")
        data["text"] = _plain(text)[:4000]
        res = tg_post("editMessageText", data)
    return res is not None


def pin_message(mid):
    if mid and TOKEN:
        tg_post("pinChatMessage", {"chat_id": CHAT_ID, "message_id": mid, "disable_notification": "true"})


def unpin_message(mid):
    if mid and TOKEN:
        tg_post("unpinChatMessage", {"chat_id": CHAT_ID, "message_id": mid})


def delete_message(mid):
    if mid and TOKEN:
        tg_post("deleteMessage", {"chat_id": CHAT_ID, "message_id": mid})


def register_commands():
    """Shows the command menu inside Telegram (the '/' button)."""
    tg_post("setMyCommands", {"commands": json.dumps(
        [{"command": c, "description": d[:250]} for c, d in BOT_COMMANDS])})


def _trim_state(state):
    state["decisions"] = dict(list((state.get("decisions") or {}).items())[-300:])
    state["alerts"] = (state.get("alerts") or [])[-20:]
    state["pump_cooldown"] = {k: v for k, v in (state.get("pump_cooldown") or {}).items()
                              if time.time() - v < 6 * 3600}


def get_all_tickers():
    out = {}
    for base in BINANCE_SPOT_HOSTS:
        d = http_json(base + "/api/v3/ticker/24hr", timeout=20)
        if isinstance(d, list) and d:
            for r in d:
                s = str(r.get("symbol", ""))
                if s.endswith("USDT") and len(s) > 4:
                    out[s[:-4]] = {"price": safe_float(r.get("lastPrice")),
                                   "change_24h": safe_float(r.get("priceChangePercent")),
                                   "quote_vol": safe_float(r.get("quoteVolume")),
                                   "high": safe_float(r.get("highPrice")),
                                   "low": safe_float(r.get("lowPrice"))}
            if out:
                return out
    d = http_json(OKX + "/api/v5/market/tickers", {"instType": "SPOT"}, timeout=20)
    for r in (d or {}).get("data") or []:
        inst = str(r.get("instId", ""))
        if inst.endswith("-USDT"):
            last, op = safe_float(r.get("last")), safe_float(r.get("open24h"))
            out[inst[:-5]] = {"price": last, "change_24h": pct_change(last, op),
                              "quote_vol": safe_float(r.get("volCcy24h")),
                              "high": safe_float(r.get("high24h")), "low": safe_float(r.get("low24h"))}
    return out


def refresh_tickers():
    data = get_all_tickers()
    if data:
        TICKERS.update(ts=time.time(), data=data)
    return TICKERS["data"]


def tickers(max_age=120):
    if time.time() - TICKERS["ts"] > max_age or not TICKERS["data"]:
        refresh_tickers()
    return TICKERS["data"]


COIN_NAMES = {
    "BTC": "Bitcoin", "ETH": "Ethereum", "SOL": "Solana", "XRP": "XRP Ripple", "BNB": "BNB",
    "DOGE": "Dogecoin", "ADA": "Cardano", "AVAX": "Avalanche", "LINK": "Chainlink",
    "DOT": "Polkadot", "TON": "Toncoin", "SHIB": "Shiba Inu", "PEPE": "Pepe", "SUI": "Sui",
    "NEAR": "NEAR Protocol", "APT": "Aptos", "ARB": "Arbitrum", "OP": "Optimism",
    "INJ": "Injective", "WIF": "dogwifhat", "BONK": "Bonk", "FET": "Fetch.ai", "TAO": "Bittensor",
    "RENDER": "Render", "LTC": "Litecoin", "TRX": "Tron", "POL": "Polygon", "UNI": "Uniswap",
    "AAVE": "Aave", "HBAR": "Hedera", "ATOM": "Cosmos", "FIL": "Filecoin", "ETC": "Ethereum Classic",
}


NAME_TO_SYM = {v.lower().split()[0]: k for k, v in COIN_NAMES.items()}


NAME_TO_SYM.update({"ripple": "XRP", "shiba": "SHIB", "matic": "POL", "bitcoins": "BTC"})


SECTORS = {
    "Memecoins": ["DOGE", "SHIB", "PEPE", "WIF", "BONK", "FLOKI", "TRUMP", "PENGU"],
    "Layer-1 chains": ["ETH", "SOL", "AVAX", "ADA", "DOT", "NEAR", "SUI", "APT", "TON", "TRX", "ATOM", "SEI"],
    "AI coins": ["FET", "TAO", "RENDER", "WLD", "ARKM", "VIRTUAL"],
    "DeFi": ["UNI", "AAVE", "MKR", "LDO", "CRV", "INJ", "JUP", "PENDLE", "ENA"],
    "Exchange tokens": ["BNB", "OKB", "CRO"],
    "Payments / legacy": ["XRP", "LTC", "BCH", "XLM", "ETC"],
}


STOPWORDS = {"WHY", "THE", "WILL", "TODAY", "AND", "FOR", "NOT", "ARE", "WHAT", "WHEN", "HOW", "DID",
             "DOES", "COIN", "PUMP", "DUMP", "UP", "DOWN", "IS", "IT", "ME", "MY", "ON", "IN", "OF",
             "TO", "A", "BUY", "SELL", "NOW", "THIS", "THAT", "WITH", "ABOUT", "GOING", "MOVE",
             "MOVED", "MOVING", "PRICE", "WEEKEND", "MARKET", "CRYPTO", "TRADE", "HOLD", "SHOULD",
             "YES", "NO", "OK", "USDT", "CAN", "GET", "ALL", "ANY", "HAS", "HAD", "WAS", "ITS", "WHO",
             "TOP", "BEST", "TELL", "SHOW", "GIVE", "LAST", "NEXT", "WEEK", "DAY", "HOUR", "FALL",
             "RISE", "DROP", "CRASH", "RALLY", "SURGE", "GAIN", "LOSS", "RISK", "SAFE"}


CATALYSTS = {
    "exchange listing": r"\b(?:listing|lists|listed|to list)\b",
    "delisting": r"\bdelist\w*",
    "token unlock": r"\b(?:unlock\w*|vesting)\b",
    "hack / exploit": r"\b(?:hack\w*|exploit\w*|drained|breach\w*)\b",
    "partnership / integration": r"\b(?:partnership|partners?|integrat\w+|collaborat\w+)\b",
    "ETF / regulation": r"\b(?:etf|sec|lawsuit|regulat\w+|approval|approved)\b",
    "upgrade / launch / airdrop": r"\b(?:upgrade|mainnet|launch\w*|testnet|airdrop)\b",
    "whale / large flows": r"\b(?:whales?|accumulat\w+|outflows?|inflows?)\b",
    "burn / buyback": r"\b(?:burns?|burned|buyback)\b",
}


_CAT_RX = {k: re.compile(v, re.I) for k, v in CATALYSTS.items()}


def sector_of(base):
    for name, members in SECTORS.items():
        if base in members:
            return name, members
    return None, []


def detect_coin(text):
    """Find a coin mentioned in free text ('why did DOGE pump', 'solana today', '$PEPE')."""
    tk = tickers()
    raw = re.findall(r"\$?[A-Za-z0-9]{2,10}", text)
    for w in raw:
        if w.lstrip("$").lower() in NAME_TO_SYM:
            return NAME_TO_SYM[w.lstrip("$").lower()]
    for w in raw:
        u = w.lstrip("$").upper()
        if u in STOPWORDS:
            continue
        typed_upper = w.lstrip("$").isupper() or w.startswith("$")
        if u in tk and (typed_upper or len(u) >= 3):
            return u
    return None


def coin_snapshot(base):
    sym = base + "USDT"
    h1 = add_indicators(get_klines("1h", 200, sym))
    if len(h1) < 60:
        return None
    h4 = add_indicators(get_klines("4h", 200, sym))
    tk = tickers().get(base) or {}
    price = tk.get("price") or float(h1.iloc[-1]["close"])
    c = h1["close"]

    def chg(n):
        return pct_change(price, float(c.iloc[-1 - n])) if len(c) > n + 1 else None

    v = h1["volume"]
    last24 = float(v.iloc[-24:].sum())
    prev_avg = float(v.iloc[-168:-24].sum()) / 6 if len(v) >= 100 else 0
    vol_ratio = last24 / prev_avg if prev_avg > 0 else None

    last = h1.iloc[-25:-1]
    moves = ((last["close"] - last["open"]) / last["open"] * 100)
    big_i = moves.abs().idxmax() if len(moves) else None
    big = None
    if big_i is not None:
        big = {"pct": float(moves.loc[big_i]),
               "time": datetime.fromtimestamp(float(h1.loc[big_i, "time"]) / 1000, UTC)}
    s1 = snapshot(h1.iloc[-2])
    s4 = snapshot(h4.iloc[-2]) if len(h4) >= 60 else {}
    ch24 = tk.get("change_24h") if tk.get("change_24h") is not None else chg(24)
    return {"base": base, "price": price, "chg_1h": chg(1), "chg_4h": chg(4), "chg_24h": ch24,
            "chg_7d": chg(168), "vol_ratio": vol_ratio, "biggest": big, "tr1": s1, "tr4": s4,
            "quote_vol": tk.get("quote_vol"), "high24": tk.get("high"), "low24": tk.get("low")}


def coin_derivs(base):
    out = {"funding": None, "oi_change_24h": None, "oi_change_6h": None}
    f = http_json(OKX + "/api/v5/public/funding-rate", {"instId": f"{base}-USDT-SWAP"})
    try:
        out["funding"] = safe_float(f["data"][0].get("fundingRate"))
    except Exception:
        pass
    d = http_json(OKX + "/api/v5/rubik/stat/contracts/open-interest-volume", {"ccy": base, "period": "1H"})
    try:
        rows = sorted(d["data"], key=lambda r: float(r[0]), reverse=True)   # newest first
        if len(rows) >= 25:
            out["oi_change_24h"] = pct_change(float(rows[0][1]), float(rows[24][1]))
        if len(rows) >= 7:
            out["oi_change_6h"] = pct_change(float(rows[0][1]), float(rows[6][1]))
    except Exception:
        pass
    return out


def coin_news(base):
    name = COIN_NAMES.get(base)
    q = f"{name} crypto when:1d" if name else f"{base} crypto token when:1d"
    url = f"https://news.google.com/rss/search?q={quote(q)}&hl=en-US&gl=US&ceid=US%3Aen"
    items = parse_rss(url, "Coin")
    key = [base.lower()] + ([w.lower() for w in name.split() if len(w) > 3] if name else [])
    rel = [i for i in items if any(k in (i["title"] + " " + i["description"]).lower() for k in key)]
    return (rel or items)[:6]


def explain_drivers(snap, derivs, items, tk):
    """Rank the most likely reasons for the move, each backed by a measurable fact."""
    base, drivers = snap["base"], []
    move = snap["chg_24h"] if snap["chg_24h"] is not None else snap["chg_4h"]
    if move is None:
        return [(1, "Not enough data to explain this move.")]
    up = move > 0
    btc = (tk.get("BTC") or {}).get("change_24h")

    if base != "BTC" and btc is not None:
        if abs(btc) >= 1.0 and (btc > 0) == up and abs(btc) >= 0.5 * abs(move):
            drivers.append((3.0, f"Whole market moved the same way (BTC {btc:+.1f}% today) - "
                                 "this coin is mostly following the market."))
        elif abs(move - btc) >= 5:
            drivers.append((3.0, f"Coin-specific move: it {'beat' if move > btc else 'lagged'} BTC "
                                 f"({btc:+.1f}%) by {abs(move - btc):.1f} points."))
    vr = snap.get("vol_ratio")
    if vr is not None:
        if vr >= 2.5:
            drivers.append((3.0, f"Trading volume is {vr:.1f}x normal - real money is behind the move."))
        elif vr >= 1.5:
            drivers.append((1.5, f"Trading volume is {vr:.1f}x normal (above average)."))
        elif vr < 0.8 and abs(move) >= 5:
            drivers.append((2.0, f"Volume is below normal ({vr:.1f}x) - a big move on thin liquidity "
                                 "can reverse quickly."))
    f, oi = derivs.get("funding"), derivs.get("oi_change_24h")
    if oi is None:
        oi = derivs.get("oi_change_6h")
    if oi is not None:
        if up and oi >= 5 and f is not None and f > 0.0003:
            drivers.append((2.5, f"Leveraged long build-up: open interest {oi:+.0f}% and funding "
                                 f"{f * 100:+.3f}% - fragile if price turns."))
        elif up and oi <= -3:
            drivers.append((2.5, f"Likely short squeeze: price rose while open interest fell {abs(oi):.0f}% "
                                 "(shorts closing)."))
        elif (not up) and oi <= -5:
            drivers.append((2.5, f"Long-liquidation flush: open interest fell {abs(oi):.0f}% as price dropped."))
        elif (not up) and oi >= 5:
            drivers.append((2.5, f"New shorts piling in: open interest {oi:+.0f}% while price falls."))
    if f is not None and f > 0.0008:
        drivers.append((1.5, f"Funding is very high ({f * 100:+.3f}%) - crowded longs, squeeze risk."))
    elif f is not None and f < -0.0008:
        drivers.append((1.5, f"Funding is very negative ({f * 100:+.3f}%) - crowded shorts, squeeze risk."))

    text = " ".join(i["title"] for i in items)
    tags = [name for name, rx in _CAT_RX.items() if rx.search(text)]
    if tags:
        drivers.append((3.0, "Headlines mention: " + ", ".join(tags) + "."))

    sname, members = sector_of(base)
    if sname:
        peers = [tk[m]["change_24h"] for m in members if m != base and m in tk
                 and tk[m].get("change_24h") is not None and (tk[m].get("quote_vol") or 0) > 5e6]
        if len(peers) >= 2:
            avg = sum(peers) / len(peers)
            if (avg > 0) == up and abs(avg) >= 2 and abs(avg) >= 0.4 * abs(move):
                drivers.append((2.5, f"Sector move: {sname} averaged {avg:+.1f}% today."))

    res, sup = snap["tr1"].get("resistance"), snap["tr1"].get("support")
    if up and res and snap["price"] >= res * 0.998:
        drivers.append((1.5, "Price is breaking above its recent 50-hour high (technical breakout)."))
    elif (not up) and sup and snap["price"] <= sup * 1.002:
        drivers.append((1.5, "Price is breaking below its recent 50-hour low (technical breakdown)."))

    if not any(s >= 2 for s, _ in drivers):
        drivers.append((1.0, "No clear news, sector or market cause found - this is often leverage, "
                             "a large whale order or thin liquidity. Treat the move with caution."))
    drivers.sort(key=lambda x: -x[0])
    return drivers[:4]


def ai_explain(snap, drivers, items, derivs, state):
    if not ANTHROPIC_API_KEY:
        return None
    facts = {
        "coin": snap["base"], "price": snap["price"],
        "change": {"1h": snap["chg_1h"], "4h": snap["chg_4h"], "24h": snap["chg_24h"], "7d": snap["chg_7d"]},
        "volume_vs_normal": snap["vol_ratio"], "funding": derivs.get("funding"),
        "open_interest_change_24h_pct": derivs.get("oi_change_24h"),
        "measured_drivers": [d for _, d in drivers],
        "headlines": [f"{i['title']} ({i['source']})" for i in items[:5]],
    }
    lang = LANG_NAMES.get((state or {}).get("lang", "en"))
    return claude_call(
        "You explain crypto price moves to beginners. Use ONLY the facts given. Rank the 2-3 most likely "
        "reasons, say clearly that this is an evidence-based guess, mention what is unknown, never give "
        "buy/sell advice, max 110 words, plain text." + (f" Write in {lang}." if lang else ""),
        json.dumps(facts, default=str), max_tokens=350)


def explain_move(base, state=None):
    base = base.upper()
    cached = EXPLAIN_CACHE.get(base)
    if cached and time.time() - cached[0] < 180:
        return cached[1]
    snap = coin_snapshot(base)
    if not snap:
        return None
    tk = tickers()
    derivs = coin_derivs(base)
    items = coin_news(base)
    drivers = explain_drivers(snap, derivs, items, tk)
    res = {"snap": snap, "derivs": derivs, "items": items, "drivers": drivers,
           "ai": ai_explain(snap, drivers, items, derivs, state),
           "btc": (tk.get("BTC") or {}).get("change_24h"), "eth": (tk.get("ETH") or {}).get("change_24h")}
    EXPLAIN_CACHE[base] = (time.time(), res)
    if len(EXPLAIN_CACHE) > 30:
        EXPLAIN_CACHE.pop(next(iter(EXPLAIN_CACHE)))
    return res


def _pct(x):
    return "n/a" if x is None else f"{x:+.1f}%"


def explain_html(res):
    s, base = res["snap"], res["snap"]["base"]
    mv = s["chg_24h"]
    word = "UP" if (mv or 0) >= 0 else "DOWN"
    L = [B(f"🔍 WHY IS {base} {word} {_pct(mv)} TODAY?"),
         "<i>An evidence-based guess - nobody can be sure why a price moves.</i>", "",
         f"Price {esc(fmt_price(s['price']))} | last hour {_pct(s['chg_1h'])} | 4h {_pct(s['chg_4h'])} "
         f"| 24h {_pct(mv)} | 7d {_pct(s['chg_7d'])}"]
    if s.get("biggest"):
        b = s["biggest"]
        L.append(f"Biggest hourly candle: {esc(fmt_local(b['time'], '%H:%M'))} ({b['pct']:+.1f}%)")
    if res.get("btc") is not None and base != "BTC":
        L.append(f"BTC today {_pct(res['btc'])} | ETH {_pct(res.get('eth'))}")
    if s.get("vol_ratio"):
        L.append(f"Volume vs normal: {s['vol_ratio']:.1f}x")
    L += ["", B("Most likely reasons (strongest evidence first)")]
    for i, (_, d) in enumerate(res["drivers"], 1):
        L.append(f"{i}. {esc(d)}")
    if res.get("ai"):
        L += ["", B("Summary"), esc(res["ai"])]
    if res["items"]:
        L += ["", B("Headlines (last 24h)")]
        for it in res["items"][:3]:
            L.append(f"• {esc(it['title'][:110])} ({esc(it['source'])})")
    L += ["", "⚠️ <i>Late pumps often reverse and dumps often bounce. This is not a buy or sell signal.</i>"]
    return "\n".join(L)


def coin_buttons(base):
    return {"inline_keyboard": [[
        {"text": f"🔍 Why {base}?", "callback_data": f"why:{base}"},
        {"text": f"📊 {base} card", "callback_data": f"coin:{base}"},
        {"text": "🚀 Movers", "callback_data": "movers"}]]}


def coin_card_html(base):
    snap = coin_snapshot(base)
    if not snap:
        return None
    d = coin_derivs(base)
    tk = tickers()
    btc = (tk.get("BTC") or {}).get("change_24h")
    s1, s4 = snap["tr1"], snap["tr4"]
    L = [B(f"🪙 {base}  {fmt_price(snap['price'])}"), esc(utc_text()), "━━━━━━━━━━━━━━━━━━━━",
         f"Change: 1h {_pct(snap['chg_1h'])} | 4h {_pct(snap['chg_4h'])} | 24h {_pct(snap['chg_24h'])} "
         f"| 7d {_pct(snap['chg_7d'])}"]
    if btc is not None and snap["chg_24h"] is not None and base != "BTC":
        rel = snap["chg_24h"] - btc
        L.append(f"vs BTC today: {rel:+.1f} points ({'stronger' if rel > 0 else 'weaker'} than BTC)")
    L += ["", B("Trend & momentum"),
          f"1h: {arrow(s1.get('trend'))} {esc(trend_words(s1.get('trend')))} | RSI {_num(s1.get('rsi'))} | ADX {_num(s1.get('adx'))}"]
    if s4:
        L.append(f"4h: {arrow(s4.get('trend'))} {esc(trend_words(s4.get('trend')))} | RSI {_num(s4.get('rsi'))}")
    L.append("Strength: " + esc(trend_strength_words(s1.get("adx"))))
    L += ["", B("Levels"),
          f"Ceiling (50h high): {esc(fmt_price(s1.get('resistance')))}",
          f"Floor (50h low): {esc(fmt_price(s1.get('support')))}"]
    if snap.get("high24"):
        L.append(f"24h range: {esc(fmt_price(snap['low24']))} - {esc(fmt_price(snap['high24']))}")
    L += ["", B("Activity")]
    if snap.get("vol_ratio"):
        L.append(f"Volume vs normal: {snap['vol_ratio']:.1f}x")
    if snap.get("quote_vol"):
        L.append(f"24h volume: ${snap['quote_vol'] / 1e6:,.0f}M")
    if d.get("funding") is not None:
        L.append("Funding: " + esc(funding_short(d["funding"])))
    if d.get("oi_change_24h") is not None:
        L.append(f"Open interest 24h: {d['oi_change_24h']:+.1f}%")
    L += ["", "<i>Technical snapshot only - not a trade signal. Smaller coins move faster and can be manipulated.</i>"]
    return "\n".join(L)


def movers_html():
    tk = tickers()
    liquid = [(b, v) for b, v in tk.items()
              if (v.get("quote_vol") or 0) >= PUMP_MIN_VOL and v.get("change_24h") is not None]
    if not liquid:
        return None, None
    liquid.sort(key=lambda kv: kv[1]["change_24h"])
    losers, gainers = liquid[:6], liquid[::-1][:6]
    L = [B("🚀 TOP MOVERS (24h, liquid coins only)"), "", B("Gainers")]
    L += [f"🟢 {esc(b)}  {v['change_24h']:+.1f}%  (${v['quote_vol'] / 1e6:,.0f}M vol)" for b, v in gainers]
    L += ["", B("Losers")]
    L += [f"🔴 {esc(b)}  {v['change_24h']:+.1f}%  (${v['quote_vol'] / 1e6:,.0f}M vol)" for b, v in losers]
    L += ["", "<i>Tap a coin to see why it moved. Coins with tiny volume are hidden - they are easy to manipulate.</i>"]
    picks = [b for b, _ in gainers[:3]] + [b for b, _ in losers[:3]]
    kb = {"inline_keyboard": [[{"text": f"🔍 {b}", "callback_data": f"why:{b}"} for b in picks[i:i + 3]]
                              for i in range(0, len(picks), 3)]}
    return "\n".join(L), kb


def update_tick_hist(state):
    tk = TICKERS["data"]
    if not tk:
        return
    hist = state.setdefault("tick_hist", {})
    now = time.time()
    ranked = sorted(tk.items(), key=lambda kv: -(kv[1].get("quote_vol") or 0))[:60]
    wanted = {b for b, _ in ranked} | set(state.get("watchlist") or []) | {"BTC", "ETH"}
    for b in wanted:
        p = (tk.get(b) or {}).get("price")
        if not p:
            continue
        h = hist.setdefault(b, [])
        if not h or now - h[-1][0] >= 100:
            h.append([now, p])
        hist[b] = [x for x in h if now - x[0] < 80 * 60]
    for b in list(hist):
        if b not in wanted:
            del hist[b]


def _chg_1h(h, now):
    if len(h) < 2 or now - h[0][0] < 50 * 60:
        return None
    ref = min(h, key=lambda x: abs(x[0] - (now - 3600)))
    return pct_change(h[-1][1], ref[1])


def quick_reason(base, ch, state):
    tk, hist, now = TICKERS["data"], state.get("tick_hist") or {}, time.time()
    parts = []
    btc1 = _chg_1h(hist.get("BTC") or [], now)
    if base != "BTC" and btc1 is not None:
        if (btc1 > 0) == (ch > 0) and abs(btc1) >= 0.4 * abs(ch):
            parts.append(f"market-wide (BTC {btc1:+.1f}% too)")
        else:
            parts.append(f"coin-specific (BTC only {btc1:+.1f}%)")
    sname, members = sector_of(base)
    if sname:
        peers = [tk[m]["change_24h"] for m in members if m != base and m in tk and tk[m].get("change_24h") is not None]
        if len(peers) >= 2:
            avg = sum(peers) / len(peers)
            if abs(avg) >= 2 and (avg > 0) == (ch > 0):
                parts.append(f"{sname} also moving ({avg:+.1f}% avg)")
    f = coin_derivs(base)["funding"] if base not in ("BTC",) else None
    if f is not None and abs(f) > 0.0005:
        parts.append("crowded " + ("longs" if f > 0 else "shorts") + f" (funding {f * 100:+.3f}%)")
    return "; ".join(parts) if parts else "no obvious cause yet - tap Why for the full check"


def check_pump_dump(state):
    hist, tk = state.get("tick_hist") or {}, TICKERS["data"]
    now = time.time()
    recent = [t for t in state.setdefault("pump_alert_times", []) if now - t < 3600]
    state["pump_alert_times"] = recent
    cd = state.setdefault("pump_cooldown", {})
    watch = set(state.get("watchlist") or [])
    cands = []
    for b, h in hist.items():
        ch = _chg_1h(h, now)
        if ch is None or now - cd.get(b, 0) < 2 * 3600:
            continue
        thr = MAJOR_PUMP_PCT.get(b, PUMP_1H_PCT) * (0.7 if b in watch else 1.0)
        vol = (tk.get(b) or {}).get("quote_vol") or 0
        if abs(ch) >= thr and (vol >= PUMP_MIN_VOL or b in watch):
            cands.append((abs(ch), b, ch, vol))
    for _, b, ch, vol in sorted(cands, reverse=True):
        if len(recent) >= 3:
            break
        icon = "🚀" if ch > 0 else "🩸"
        reason = quick_reason(b, ch, state)
        text = "\n".join([B(f"{icon} {'PUMP' if ch > 0 else 'DUMP'}: {b} {ch:+.1f}% in ~1h"),
                          f"Price {esc(fmt_price((tk.get(b) or {}).get('price')))} | 24h volume ${vol / 1e6:,.0f}M",
                          "Quick read: " + esc(reason), "",
                          "<i>Chasing a fast move is risky. Tap Why for the full check.</i>"])
        tg_send_html(text, coin_buttons(b), level="normal")
        cd[b] = now
        recent.append(now)


def tab_watch(state):
    wl = state.get("watchlist") or []
    tk, hist, now = TICKERS["data"] or tickers(), state.get("tick_hist") or {}, time.time()
    L = [B("👀 WATCHLIST"), ""]
    if not wl:
        return "\n".join(L + ["Empty. Add coins with /watch SOL ETH DOGE"])
    for b in wl:
        v = tk.get(b) or {}
        h1 = _chg_1h(hist.get(b) or [], now)
        L.append(f"{esc(b)}: {esc(fmt_price(v.get('price')))} | 1h {_pct(h1)} | 24h {_pct(v.get('change_24h'))}")
    L += ["", "<i>Alerts fire earlier for watchlist coins. /unwatch SOL removes one. /why SOL explains a move.</i>"]
    return "\n".join(L)


def cmd_watch(state, args, remove=False):
    wl = state.setdefault("watchlist", [])
    if not args:
        tg_send_html(tab_watch(state), tab_keyboard("watch"))
        return
    tk = tickers()
    for a in args[:10]:
        b = (NAME_TO_SYM.get(a.lower()) or a.upper().lstrip("$"))
        if remove:
            if b in wl:
                wl.remove(b)
        elif b in tk and b not in wl and len(wl) < 15:
            wl.append(b)
        elif b not in tk:
            send(f"❓ {b} was not found as a USDT pair.")
    send(("Removed. " if remove else "Saved. ") + "Watchlist: " + (", ".join(wl) or "empty"))


TF_SECS = {"15m": 900, "1h": 3600, "4h": 14400}


def _price_of(base):
    if base == "BTC" and (CTX.get("ticker") or {}).get("price"):
        return CTX["ticker"]["price"]
    return (tickers().get(base) or {}).get("price")


def cmd_alert(state, args):
    alerts = state.setdefault("alerts", [])
    if not args or args[0].lower() == "list":
        if not alerts:
            send("No alerts. Examples:\n/alert 70000  (BTC price)\n/alert SOL 200\n/alert close 1h above 68000")
            return
        lines = ["🔔 YOUR ALERTS"]
        for a in alerts:
            if a["kind"] == "price":
                lines.append(f"#{a['id']}: {a['sym']} {a['dir']} {a['level']:,.4g}")
            else:
                lines.append(f"#{a['id']}: BTC {a['tf']} candle closes {a['dir']} {a['level']:,.0f}")
        send("\n".join(lines + ["", "/alert del <id>  or  /alert clear"]))
        return
    cmd = args[0].lower()
    if cmd == "clear":
        state["alerts"] = []
        send("All alerts removed.")
        return
    if cmd == "del" and len(args) > 1:
        state["alerts"] = [a for a in alerts if str(a["id"]) != args[1].lstrip("#")]
        send("Removed.")
        return
    state["alert_seq"] = state.get("alert_seq", 0) + 1
    if len(alerts) >= 20:
        send("Maximum 20 alerts. Use /alert clear.")
        return
    if cmd == "close" and len(args) >= 4:
        tf, direction, level = args[1].lower(), args[2].lower(), safe_float(args[3])
        if tf not in TF_SECS or direction not in ("above", "below") or not level:
            send("Usage: /alert close 1h above 68000   (timeframes: 15m, 1h, 4h)")
            return
        alerts.append({"id": state["alert_seq"], "kind": "close", "sym": "BTC", "tf": tf,
                       "dir": direction, "level": level, "created": time.time()})
        send(f"✅ Alert #{state['alert_seq']}: I'll tell you when a BTC {tf} candle CLOSES {direction} {level:,.0f}.")
        return
    base, level = ("BTC", safe_float(args[0])) if len(args) == 1 else (args[0].upper().lstrip("$"), safe_float(args[1]))
    price = _price_of(base)
    if not level or price is None:
        send("Usage: /alert 70000   or   /alert SOL 200   (coin must be a USDT pair)")
        return
    direction = "above" if level > price else "below"
    alerts.append({"id": state["alert_seq"], "kind": "price", "sym": base, "dir": direction,
                   "level": level, "created": time.time()})
    send(f"✅ Alert #{state['alert_seq']}: I'll tell you when {base} goes {direction} {level:,.4g} "
         f"(now {price:,.4g}).")


def check_price_alerts(state):
    alerts = state.get("alerts") or []
    if not alerts:
        return
    keep = []
    for a in alerts:
        hit, info = False, ""
        if a["kind"] == "price":
            p = _price_of(a["sym"])
            if p is not None:
                hit = p >= a["level"] if a["dir"] == "above" else p <= a["level"]
                info = f"{a['sym']} is {p:,.4g} (target {a['dir']} {a['level']:,.4g})"
        else:
            df = CANDLES.get(a["tf"])
            if df is not None and len(df) > 5:
                row = df.iloc[-2]
                closed_at = float(row["time"]) / 1000 + TF_SECS[a["tf"]]
                if closed_at > a["created"]:
                    c = float(row["close"])
                    hit = c > a["level"] if a["dir"] == "above" else c < a["level"]
                    info = f"BTC {a['tf']} candle closed at {c:,.0f} ({a['dir']} {a['level']:,.0f})"
        if hit:
            send(f"🔔 ALERT #{a['id']}\n{info}", level="critical")
        else:
            keep.append(a)
    state["alerts"] = keep


def weekend_stats():
    """Base rates from the last ~300 daily BTC candles (history, not a prediction)."""
    df = CANDLES.get("1d")
    if df is None or len(df) < 60:
        return None
    d = df.iloc[:-1].copy()
    d["dt"] = pd.to_datetime(d["time"], unit="ms", utc=True)
    d["wd"] = d["dt"].dt.weekday
    d["rng"] = (d["high"] - d["low"]) / d["open"] * 100
    d["ret"] = (d["close"] - d["open"]) / d["open"] * 100
    d["week"] = (d["dt"] - pd.to_timedelta(d["wd"], unit="D")).dt.strftime("%Y-%m-%d")
    moves = []
    for _, g in d.groupby("week"):
        fri, sun = g[g["wd"] == 4], g[g["wd"] == 6]
        if len(fri) and len(sun):
            f, s = float(fri.iloc[0]["close"]), float(sun.iloc[0]["close"])
            moves.append((s - f) / f * 100)
    mon = d[d["wd"] == 0]["ret"]
    if not moves or len(mon) < 5:
        return None
    return {"n": len(moves),
            "weekend_range": float(d[d["wd"] >= 5]["rng"].mean()),
            "weekday_range": float(d[d["wd"] < 5]["rng"].mean()),
            "up_share": sum(1 for m in moves if m > 0) / len(moves) * 100,
            "avg_abs": sum(abs(m) for m in moves) / len(moves),
            "mon_up": float((mon > 0).mean() * 100), "mon_abs": float(mon.abs().mean())}


def _last_friday_close_ny():
    now_ny = now_utc().astimezone(NY_TZ)
    back = (now_ny.weekday() - 4) % 7
    fri = (now_ny - timedelta(days=back)).replace(hour=16, minute=0, second=0, microsecond=0)
    if fri > now_ny:
        fri -= timedelta(days=7)
    return fri


def move_since_us_close():
    """BTC move since Friday's US stock-market close (a proxy for the Monday gap mood)."""
    df = CANDLES.get("1h")
    price = (CTX.get("ticker") or {}).get("price") or (CTX.get("levels") or {}).get("price")
    if df is None or not price:
        return None
    fri = _last_friday_close_ny()
    ts = fri.astimezone(UTC).timestamp() * 1000
    row = df[(df["time"] - ts).abs() < 1]
    if row.empty:
        return None
    ref = float(row.iloc[0]["open"])
    return {"pct": pct_change(price, ref), "ref": ref, "time": fri}


def cme_reopen_utc():
    ct = ZoneInfo("America/Chicago")
    now_ct = now_utc().astimezone(ct)
    days = (6 - now_ct.weekday()) % 7
    reopen = (now_ct + timedelta(days=days)).replace(hour=17, minute=0, second=0, microsecond=0)
    if reopen < now_ct:
        reopen += timedelta(days=7)
    return reopen.astimezone(UTC)


def weekend_text(state, phase="now"):
    c = CTX
    levels, kl = c.get("levels") or {}, c.get("keylevels") or {}
    futures, opt = c.get("futures") or {}, c.get("options") or {}
    price = (c.get("ticker") or {}).get("price") or levels.get("price")
    title = {"friday": "🌙 WEEKEND OUTLOOK (stock markets just closed)",
             "sunday": "🌅 SUNDAY UPDATE (markets reopen soon)"}.get(phase, "🗓 WEEKEND / MARKET-CLOSED OUTLOOK")
    L = [B(title), esc(utc_text()), "━━━━━━━━━━━━━━━━━━━━", "",
         B("What is closed"),
         "Crypto trades 24/7. But US stocks, ETFs and CME Bitcoin futures are closed, so volume and "
         "liquidity are thinner: sudden wicks and fake breakouts are more common, and news can hit "
         "with no stock market to absorb it.",
         f"CME futures / Nasdaq futures reopen: {esc(fmt_local(cme_reopen_utc(), '%a %H:%M'))}"]
    nxt = [f"{n} {fmt_local(d, '%a %H:%M')}" for n, d in next_market_opens()[:4]]
    if nxt:
        L.append("Next stock-market opens: " + esc(" | ".join(nxt)))
    mv = move_since_us_close()
    if mv and mv["pct"] is not None:
        L.append(f"BTC since Friday's US close ({esc(fmt_price(mv['ref']))}): {mv['pct']:+.1f}% "
                 "- a hint of the mood Monday's stock open may inherit.")

    ws = weekend_stats()
    if ws:
        L += ["", B(f"What usually happens (last {ws['n']} weekends - history, not a forecast)"),
              f"• A typical weekend day moves {ws['weekend_range']:.1f}% high-to-low vs "
              f"{ws['weekday_range']:.1f}% on weekdays.",
              f"• Friday close → Sunday close: BTC finished higher {ws['up_share']:.0f}% of the time; "
              f"average move ±{ws['avg_abs']:.1f}%.",
              f"• The following Monday closed green {ws['mon_up']:.0f}% of the time (average move ±{ws['mon_abs']:.1f}%)."]

    L += ["", B("Scenarios until markets reopen")]
    if levels and price:
        res, sup = levels.get("resistance"), levels.get("support")
        L += [f"↔️ <b>Base case - range:</b> price stays between {esc(fmt_price(sup))} and {esc(fmt_price(res))}. "
              "Weekend ranges are usually quiet; range trades beat breakout trades.",
              f"🟢 <b>Bullish break:</b> a candle CLOSE above {esc(fmt_price(res))} with rising volume → "
              f"next stops {esc(fmt_price(levels['bull1']))}, {esc(fmt_price(levels['bull2']))}. "
              "Weekend breakouts without volume often fail - wait for the close.",
              f"🔴 <b>Bearish break:</b> a candle CLOSE below {esc(fmt_price(sup))} → "
              f"next stops {esc(fmt_price(levels['bear1']))}, {esc(fmt_price(levels['bear2']))}. "
              "Thin books can turn a dip into a flush (liquidations)."]
    ref = [(label, kl.get(k)) for k, label in (("week_open", "this week's open"), ("prev_week_high", "last week high"),
                                               ("prev_week_low", "last week low")) if kl.get(k)]
    if ref:
        L.append("Reference levels: " + esc(" | ".join(f"{lab} {fmt_price(v)}" for lab, v in ref)))

    watch = []
    f = futures.get("funding")
    if f is not None and f * 100 > 0.03:
        watch.append("funding is high → crowded longs, a drop can snowball")
    elif f is not None and f * 100 < -0.03:
        watch.append("funding is negative → crowded shorts, a squeeze can spike price")
    if opt.get("pc_oi") is not None and opt["pc_oi"] > 1.2:
        watch.append(f"options put/call {opt['pc_oi']:.2f} → traders are hedging")
    if (futures.get("oi_change_pct") or 0) > 3:
        watch.append("open interest is rising → bigger move building")
    if futures.get("open_interest") is not None:
        watch.append("liquidation heat-maps: stop-hunts are common on weekends")
    if watch:
        L += ["", B("What to watch")] + ["• " + esc(w) for w in watch]

    events = [e for e in (c.get("events") or []) if e["time"] >= now_utc()][:5]
    if events:
        L += ["", B("Coming up")]
        L += ["• " + esc(f"{e['name']} - {fmt_local(e['time'], '%a %H:%M')}") for e in events]
    if WEEKEND_MODE:
        L += ["", f"🛡 <i>Weekend mode: new signals need {2} extra confirmations and the leverage cap is "
                  f"{max_lev_now():.1f}x on Sat/Sun.</i>"]
    L += ["", "<i>Scenarios are conditions to watch, not predictions. Not advice.</i>"]
    return "\n".join(L)


def check_weekend_posts(state):
    """Friday after the US close: post + pin the outlook. Sunday evening: refresh it.
    Monday after the US open: unpin."""
    if not CTX.get("bias"):
        return
    now_ny = now_utc().astimezone(NY_TZ)
    key = now_ny.strftime("%Y-%m-%d")
    wd = now_ny.weekday()
    if wd == 4 and now_ny.hour * 60 + now_ny.minute >= 16 * 60 + 30 and state.get("last_weekend_fri") != key:
        _post_weekend(state, "friday")
        state["last_weekend_fri"] = key
    elif wd == 6 and now_utc().hour >= 18 and state.get("last_weekend_sun") != key:
        _post_weekend(state, "sunday")
        state["last_weekend_sun"] = key
    elif wd == 0 and now_ny.hour * 60 + now_ny.minute >= 9 * 60 + 35 and state.get("weekend_msg"):
        unpin_message(state["weekend_msg"])
        state["weekend_msg"] = None


def _post_weekend(state, phase):
    mid = tg_send_html(localize(weekend_text(state, phase), state), None, level="normal")
    if mid:
        if state.get("weekend_msg"):
            unpin_message(state["weekend_msg"])
        pin_message(mid)
        state["weekend_msg"] = mid


def trade_buttons(trade_id):
    return {"inline_keyboard": [
        [{"text": "🔎 Why this signal", "callback_data": f"tr:why:{trade_id}"},
         {"text": "🏃 Chase check", "callback_data": f"tr:chase:{trade_id}"}],
        [{"text": "✅ I took it", "callback_data": f"tr:took:{trade_id}"},
         {"text": "⏭ I skipped", "callback_data": f"tr:skip:{trade_id}"}]]}


def _r_now(t, price):
    if not price or not t.get("risk"):
        return None
    long = t["action"] == "LONG"
    return ((price - t["entry"]) if long else (t["entry"] - price)) / t["risk"]


def live_trade_text(t, price, final=False):
    L = [B(f"📌 {'CLOSED' if final else 'LIVE'} TRADE - {t['action']} BTC")]
    if final:
        r = t.get("result_r")
        L.append(f"Result: {esc(t.get('outcome') or 'ended')}" + (f" | {r:+.2f}R" if r is not None else ""))
    elif t["status"] == "PENDING":
        exp = fmt_local(datetime.fromtimestamp(t["expires"], UTC), "%H:%M")
        L += [f"⏳ Waiting for limit fill at {esc(fmt_price(t['entry']))}",
              f"Price now {esc(fmt_price(price))} | auto-cancels {esc(exp)}"]
    else:
        r = _r_now(t, price)
        L.append(f"🟢 OPEN from {esc(fmt_price(t['entry']))}" + (f" | now {r:+.2f}R" if r is not None else "")
                 + f" | price {esc(fmt_price(price))}")
    L.append(f"Stop {esc(fmt_price(t['current_stop']))} | TP1 "
             f"{'✅ done' if t['tp1_hit'] else esc(fmt_price(t['tp1']))} | TP2 {esc(fmt_price(t['tp2']))}")
    L.append(f"<i>Updated {esc(fmt_local(now_utc(), '%H:%M'))} - this message updates itself</i>")
    return "\n".join(L)


def _live_key(t, price):
    r = _r_now(t, price)
    return [t["status"], t["tp1_hit"], round(t["current_stop"]), round((r or 0) * 5)]


def update_trade_pin(state, price):
    """One pinned message that follows the trade: created at the signal, edited as it moves, unpinned at the end."""
    t, tm = state.get("active_trade"), state.get("trade_msg")
    if t and (not tm or tm.get("trade_id") != t["id"]):
        mid = tg_send_html(live_trade_text(t, price), trade_buttons(t["id"]), level="critical")
        if mid:
            pin_message(mid)
            state["trade_msg"] = {"id": mid, "trade_id": t["id"], "key": _live_key(t, price), "ts": time.time()}
    elif t and tm:
        key = _live_key(t, price)
        if key != tm.get("key") or time.time() - tm["ts"] > 900:
            tg_edit(tm["id"], live_trade_text(t, price), trade_buttons(t["id"]))
            tm.update(key=key, ts=time.time())
    elif tm and not t:
        done = next((x for x in reversed(state.get("trades", [])) if x.get("id") == tm["trade_id"]), None)
        tg_edit(tm["id"], live_trade_text(done, price, final=True) if done else B("📌 Trade ended."), None)
        unpin_message(tm["id"])
        state["trade_msg"] = None


def update_event_banner(state, events):
    """Pinned 'NO-TRADE WINDOW' before big news; removed ~10 minutes after the release."""
    soon, b = upcoming_high_event(events, NO_TRADE_BEFORE_EVENT_MIN), state.get("banner")
    if soon:
        e, mins = soon
        when = f"in {int(mins)} min" if mins > 0 else "just released"
        if not b or b.get("event") != e["id"]:
            mid = tg_send_html(B(f"🚫 NO-TRADE WINDOW: {e['name']} {when}") + "\n"
                               "Spreads widen and price whips both ways. Stay out until it settles.",
                               None, level="normal")
            if mid:
                pin_message(mid)
                state["banner"] = {"id": mid, "event": e["id"]}
    elif b:
        unpin_message(b["id"])
        delete_message(b["id"])
        state["banner"] = None


def build_daily_plan(state):
    c = CTX
    bias, plan, levels = c["bias"], c["plan"], c.get("levels") or {}
    kl, events = c.get("keylevels") or {}, c.get("events") or []
    price = (c.get("ticker") or {}).get("price") or levels.get("price")
    lt = local_now(state)
    L = [B(f"🗓 DAILY PLAN - {lt.strftime('%a %d %b')}"), "━━━━━━━━━━━━━━━━━━━━",
         f"BTC {esc(fmt_price(price))} | mood {esc(bias['bias'])} {strength_stars(bias['score'])}",
         f"Signal now: <b>{esc(plan['action'])}</b>"
         + (f" - {esc(plan['why'][0])}" if plan.get("why") else "")]
    if levels:
        L += ["", B("Levels to respect"),
              f"Ceiling {esc(_dist(levels.get('resistance'), price))} | Floor {esc(_dist(levels.get('support'), price))}"]
        extra = [f"{lab} {fmt_price(kl[k])}" for k, lab in (("prev_high", "yesterday high"), ("prev_low", "yesterday low"),
                                                           ("vwap", "VWAP")) if kl.get(k)]
        if extra:
            L.append(esc(" | ".join(extra)))
    end = now_utc() + timedelta(hours=24)
    today = [e for e in events if now_utc() <= e["time"] <= end]
    L += ["", B("Big events (next 24h)")]
    L += ["• " + esc(f"{e['name']} - {fmt_local(e['time'], '%H:%M')}") for e in today[:5]] or ["• None - quieter day"]
    L += ["", f"🛡 <i>Risk rules today: max {MAX_TRADES_PER_DAY} trades, stop after -{DAILY_LOSS_LIMIT_R:.0f}R, "
              f"leverage cap {max_lev_now():.1f}x.</i>"]
    note = liquidity_note()
    if note:
        L.append("⚠️ " + esc(note))
    return "\n".join(L)


def check_daily_plan(state):
    if not CTX.get("bias"):
        return
    lt = local_now(state)
    today = lt.strftime("%Y-%m-%d")
    if lt.hour >= 7 and state.get("last_plan_date") != today:
        mid = tg_send_html(localize(build_daily_plan(state), state), tab_keyboard("summary"), level="normal")
        if mid:
            if state.get("plan_msg"):
                unpin_message(state["plan_msg"])
            pin_message(mid)
            state["plan_msg"] = mid
        state["last_plan_date"] = today


def daily_guard(state):
    lt = local_now(state)
    start = lt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    today = [t for t in state.get("trades", [])
             if t.get("status") == "CLOSED" and (t.get("closed_at") or 0) >= start]
    total = sum(t.get("result_r") or 0 for t in today)
    if total <= -DAILY_LOSS_LIMIT_R:
        return (f"🛡 Daily loss limit reached ({total:+.1f}R). No new signals until tomorrow - "
                "stepping away protects your account.")
    if len(today) >= MAX_TRADES_PER_DAY:
        return f"🛡 Daily trade limit reached ({len(today)}/{MAX_TRADES_PER_DAY}). New signals resume tomorrow."
    return None


def check_risk_guard(state):
    g = daily_guard(state)
    day = local_now(state).strftime("%Y-%m-%d")
    if g and state.get("guard_notice") != day:
        send(g, level="critical")
        state["guard_notice"] = day
    return g


def find_trade(state, tid):
    t = state.get("active_trade")
    if t and t["id"] == tid:
        return t
    return next((x for x in reversed(state.get("trades", [])) if x.get("id") == tid), None)


def why_signal_text(t):
    f = t.get("features") or {}
    L = [B(f"🔎 WHY THIS {t['action']} SIGNAL"), ""]
    if f:
        L.append(f"Score {f.get('score', 0):+.1f} → setup strength {strength_stars(f.get('score', 0))}")
        L += ["• " + esc(r) for r in (f.get("reasons") or [])[:6]]
        L += ["", B("Filters that passed"),
              f"• Trend alignment: 4h {esc(f.get('t4h'))}, 1h {esc(f.get('t1h'))}, 15m {esc(f.get('t15'))}",
              f"• Trend strength ADX {_num(f.get('adx_1h'))} (needs ≥ {ADX_MIN:.0f})",
              f"• Volume {_num(f.get('volratio_15m'), '{:.1f}')}x normal (needs ≥ {VOLUME_MIN_RATIO})",
              f"• RSI {_num(f.get('rsi_1h'))} (not stretched)",
              f"• No big news within {NO_TRADE_BEFORE_EVENT_MIN} min, and it held for several checks in a row"]
        if f.get("t1d"):
            L.append(f"Context: daily trend {esc(f['t1d'])}, weekly {esc(f.get('t1w'))}")
    else:
        L.append("Details were not saved for this older signal.")
    L += ["", "<i>Signals are probabilities, not promises. The stop-loss is part of the plan.</i>"]
    return "\n".join(L)


def chase_text(t, price):
    if not price:
        return "Live price is not available right now."
    long = t["action"] == "LONG"
    sgn = 1 if long else -1
    risk_now = (price - t["stop"]) * sgn
    L = [B("🏃 CHASE CHECK"), f"Planned entry {esc(fmt_price(t['entry']))} | price now {esc(fmt_price(price))}"]
    if t["status"] == "PENDING":
        gap = (price - t["entry"]) / t["entry"] * 100
        L.append(f"The limit order is still waiting ({gap:+.2f}% from price). Don't switch to a market order.")
    if risk_now <= 0:
        L.append("Price is already beyond the stop - the setup is invalid. Skip it.")
        return "\n".join(L)
    rew1, rew2 = (t["tp1"] - price) * sgn, (t["tp2"] - price) * sgn
    if rew1 <= 0:
        L.append("Price already passed TP1 - the move is mostly done. Skip it.")
        return "\n".join(L)
    rr1, rr2 = rew1 / risk_now, rew2 / risk_now
    L += [f"If you entered NOW with the same stop: risk {risk_now / price * 100:.2f}% for "
          f"reward 1:{rr1:.1f} (TP1) to 1:{rr2:.1f} (TP2). Original plan was 1:{t['rr1']:.1f} to 1:{t['rr2']:.1f}.", ""]
    L.append("✅ Still decent - entering is reasonable." if rr2 >= 1.5 else
             "⚠️ Marginal - use a smaller size or wait for a pullback." if rr2 >= 1.0 else
             "❌ You'd be chasing - reward no longer justifies the risk. Skip this one.")
    return "\n".join(L)


def my_stats_lines(state):
    took = [t for t in state.get("trades", []) if (state.get("decisions") or {}).get(t.get("id")) == "took"]
    skipped = sum(1 for v in (state.get("decisions") or {}).values() if v == "skip")
    if not took and not skipped:
        return []
    L = ["", "🙋 YOUR OWN CALLS"]
    s = trade_stats(took)
    if s["n"]:
        L.append(f"Trades you took: {s['n']} | win rate {s['win_rate']:.0f}% | total {s['total_r']:+.2f}R")
    else:
        L.append("Trades you took: none closed yet")
    L.append(f"Signals you skipped: {skipped}")
    return L


def handle_trade_callback(state, cq):
    parts = str(cq.get("data", "")).split(":")
    if len(parts) < 3:
        return
    action, tid = parts[1], parts[2]
    t = find_trade(state, tid)
    if not t:
        send("That trade is no longer available.")
        return
    price = (CTX.get("ticker") or {}).get("price")
    if action == "why":
        tg_send_html(why_signal_text(t), None, level="low")
    elif action == "chase":
        tg_send_html(chase_text(t, price), None, level="low")
    elif action in ("took", "skip"):
        state.setdefault("decisions", {})[tid] = action
        send("✅ Noted - I'll track your results separately in /stats." if action == "took"
             else "👍 Noted. Skipping is a valid decision.")


def onboarding_keyboard():
    return {"inline_keyboard": [
        [{"text": "🇵🇰 Karachi", "callback_data": "set:tz:Asia/Karachi"},
         {"text": "🇦🇪 Dubai", "callback_data": "set:tz:Asia/Dubai"},
         {"text": "🇬🇧 London", "callback_data": "set:tz:Europe/London"}],
        [{"text": "🇺🇸 New York", "callback_data": "set:tz:America/New_York"},
         {"text": "UTC", "callback_data": "set:tz:UTC"}],
        [{"text": "Risk 0.5%", "callback_data": "set:risk:0.5"}, {"text": "Risk 1%", "callback_data": "set:risk:1"},
         {"text": "Risk 2%", "callback_data": "set:risk:2"}],
        [{"text": "English", "callback_data": "set:lang:en"}, {"text": "Simple English", "callback_data": "set:lang:simple"},
         {"text": "اردو", "callback_data": "set:lang:ur"}],
        [{"text": "🔔 All alerts", "callback_data": "set:alerts:all"},
         {"text": "⭐ Important only", "callback_data": "set:alerts:important"}]]}


def send_onboarding(state):
    send("👋 Welcome! Quick setup - tap your choices (you can change them anytime):\n\n"
         "• Timezone (all times will use it)\n• Risk per trade\n• Language\n• Alert style\n\n"
         "Then set your account size with /account 500 and (optional) quiet hours with /quiet 00:00-07:00.\n"
         "Try asking: \"why did SOL pump today?\", \"weekend outlook\", or /movers.\n"
         "Type /help for everything.", onboarding_keyboard())
    state["onboarded"] = True


def handle_setup_callback(state, cq):
    _, kind, val = (str(cq.get("data", "")).split(":", 2) + ["", ""])[:3]
    if kind == "tz":
        try:
            ZoneInfo(val)
        except Exception:
            return
        state["tz"] = val
        apply_user_tz(state)
        send(f"✅ Timezone set to {val}. Time now: {fmt_local(now_utc())}")
    elif kind == "risk" and safe_float(val):
        state["risk_pct"] = safe_float(val)
        send(f"✅ Risk per trade set to {state['risk_pct']}%.")
    elif kind == "lang" and val in LANG_NAMES:
        state["lang"] = val
        send(f"✅ Language set to {val}" + ("" if (ANTHROPIC_API_KEY or val == "en") else " (needs ANTHROPIC_API_KEY to translate)") + ".")
    elif kind == "alerts" and val in ("all", "important"):
        state["alert_mode"] = val
        send("✅ " + ("You'll get every alert with sound." if val == "all"
                     else "Routine updates will arrive silently; trade and critical alerts still make a sound."))


def _best_worst(trades):
    closed = [t for t in trades if t.get("status") == "CLOSED" and t.get("result_r") is not None]
    if not closed:
        return []
    b, w = max(closed, key=lambda t: t["result_r"]), min(closed, key=lambda t: t["result_r"])
    return [f"Best: {b['action']} {b['result_r']:+.2f}R ({b.get('outcome')}) | "
            f"Worst: {w['action']} {w['result_r']:+.2f}R ({w.get('outcome')})"]


def weekly_report_text(state):
    cutoff = time.time() - 7 * 24 * 3600
    wk = [t for t in state["trades"] if (t.get("closed_at") or 0) >= cutoff]
    lines = ["📅 WEEKLY REPORT", "━━━━━━━━━━━━━━━━━━━━"]
    lines += stats_lines(wk, "Last 7 days") + _best_worst(wk)
    lines += my_stats_lines(state)
    lines += ["", "Reminder: judge the strategy on 100+ trades, not one good or bad week."]
    return "\n".join(lines)


def check_weekly_report(state):
    n = now_utc()
    key = n.strftime("%G-W%V")
    if n.weekday() == 0 and n.hour == 0 and n.minute >= 10 and state.get("last_weekly") != key:
        send(weekly_report_text(state), level="low")
        state["last_weekly"] = key


def market_context_text(state):
    c = CTX
    if not c.get("bias"):
        return "Market data is still loading."
    tech, fut, lv = c.get("tech") or {}, c.get("futures") or {}, c.get("levels") or {}
    kl, plan, bias = c.get("keylevels") or {}, c["plan"], c["bias"]
    price = (c.get("ticker") or {}).get("price")
    lines = [f"Time: {utc_text()} (weekend={is_weekend()})",
             f"BTC price {price}, 24h change {(c.get('ticker') or {}).get('change_24h')}%",
             f"Mood {bias['bias']} score {bias['score']:+.1f}; bot signal {plan['action']}; reasons: "
             + "; ".join(bias.get("reasons") or [])[:300]]
    for tf in ("1w", "1d", "4h", "1h", "15m"):
        s = tech.get(tf) or {}
        if s:
            lines.append(f"Trend {tf}: {s.get('trend')} RSI {_num(s.get('rsi'))} ADX {_num(s.get('adx'))}")
    lines.append(f"Funding {fut.get('funding')}, OI change {fut.get('oi_change_pct')}%, Fear&Greed "
                 f"{(c.get('fng') or {}).get('value')}, options put/call {(c.get('options') or {}).get('pc_oi')}, "
                 f"BTC dominance {(c.get('glob') or {}).get('btc_dom')}")
    if lv:
        lines.append(f"Support {lv.get('support')}, resistance {lv.get('resistance')}, 1h ATR {lv.get('atr')}")
    if kl:
        lines.append("Key levels: " + ", ".join(f"{k} {v:,.0f}" for k, v in kl.items() if v))
    nxt = upcoming_high_event(c.get("events") or [], 7 * 24 * 60)
    if nxt:
        lines.append(f"Next big event: {nxt[0]['name']} in {int(nxt[1])} min")
    t = state.get("active_trade")
    if t:
        lines.append(f"Open bot trade: {t['action']} status {t['status']} entry {t['entry']} stop {t['current_stop']}")
    return "\n".join(lines)


def ai_answer(question, state):
    if not ANTHROPIC_API_KEY:
        return None
    lang = LANG_NAMES.get(state.get("lang", "en"))
    return claude_call(
        "You are a calm, honest crypto market assistant inside a Telegram bot. Use ONLY the market data "
        "provided. Plain English, max 150 words. Never promise outcomes and never say 'buy now' or 'sell now': "
        "give scenarios, levels and risks instead. If the data does not contain the answer, say so. "
        "This is information, not financial advice." + (f" Reply in {lang}." if lang else ""),
        f"Market data:\n{market_context_text(state)}\n\nQuestion: {question}", max_tokens=450)


FREE_TEXT_HINT = ("I can answer things like:\n• why did SOL pump today?\n• weekend outlook\n• top movers\n"
                  "• how is DOGE doing?\n• what's my current trade?\n\nOr use /help for all commands."
                  + ("" if ANTHROPIC_API_KEY else "\n(Add ANTHROPIC_API_KEY to also answer general market questions.)"))


_WHY_RX = re.compile(r"\b(why|reason|cause|how come|what happened|explain|pump\w*|dump\w*|rall\w+|crash\w*|"
                     r"surg\w+|drop\w*|fall\w*|moon\w*|spik\w+|bleed\w*)\b", re.I)


_CARD_RX = re.compile(r"\b(price|how is|how's|analysis|chart|status|doing|look|update|card)\b", re.I)


def cmd_why(state, args):
    raw = args[0] if args else "BTC"
    base = NAME_TO_SYM.get(raw.lower()) or raw.upper().lstrip("$")
    send(f"🔍 Checking {base}: price action, volume, derivatives, news and sector...", level="low")
    res = explain_move(base, state)
    if not res:
        send(f"Couldn't load data for {base}. It must be a USDT pair on Binance/OKX, and the data source may be busy.")
        return
    tg_send_html(localize(explain_html(res), state), coin_buttons(base), level="normal")


def cmd_coin(state, args):
    raw = args[0] if args else "BTC"
    base = NAME_TO_SYM.get(raw.lower()) or raw.upper().lstrip("$")
    html_text = coin_card_html(base)
    if not html_text:
        send(f"Couldn't load data for {base}. It must be a USDT pair on Binance/OKX.")
        return
    tg_send_html(localize(html_text, state), coin_buttons(base), level="normal")


def cmd_movers(state):
    text, kb = movers_html()
    if not text:
        send("Couldn't load coin data right now. Try again in a minute (see /health).")
        return
    tg_send_html(text, kb, level="normal")


def handle_text(state, text):
    t = text.strip()
    low = t.lower()
    if not t:
        return
    if re.search(r"\b(weekend|saturday|sunday)\b|market.{0,12}(close|closed)|closed market", low):
        if CTX.get("bias"):
            tg_send_html(localize(weekend_text(state, "now"), state), None, level="normal")
        else:
            send("Still loading market data. Try again in a minute.")
        return
    if re.search(r"\b(movers?|gainers?|losers?|top coins|biggest moves?)\b", low):
        cmd_movers(state)
        return
    if re.search(r"\b(my trade|current trade|am i in|open trade|my position)\b", low):
        handle_command(state, "/trade", [])
        return
    coin = detect_coin(t)
    if not coin and _WHY_RX.search(low) and re.search(r"\b(market|crypto|everything|all coins)\b", low):
        coin = "BTC"
    if coin and _WHY_RX.search(low):
        cmd_why(state, [coin])
        return
    if coin and _CARD_RX.search(low):
        cmd_coin(state, [coin])
        return
    ans = ai_answer(t, state)
    send(ans if ans else FREE_TEXT_HINT, level="normal")


def cmd_tz(state, args):
    if not args:
        send(f"Timezone: {state.get('tz', 'UTC')} (now {fmt_local(now_utc())}).\nChange: /tz Asia/Karachi")
        return
    try:
        ZoneInfo(args[0])
    except Exception:
        send("Unknown timezone. Examples: Asia/Karachi, Asia/Dubai, Europe/London, America/New_York, UTC")
        return
    state["tz"] = args[0]
    apply_user_tz(state)
    send(f"✅ Timezone set to {args[0]}. Time now: {fmt_local(now_utc())}")


def cmd_quiet(state, args):
    if not args:
        send(f"Quiet hours: {state.get('quiet') or 'off'} ({state.get('tz', 'UTC')}).\n"
             "Set: /quiet 00:00-07:00   Turn off: /quiet off\n"
             "During quiet hours messages arrive silently; critical trade alerts still make a sound.")
    elif args[0].lower() in ("off", "none"):
        state["quiet"] = None
        send("✅ Quiet hours off.")
    elif _parse_quiet(args[0]):
        state["quiet"] = args[0]
        send(f"✅ Quiet hours {args[0]} ({state.get('tz', 'UTC')}). Only critical trade alerts will make a sound.")
    else:
        send("Usage: /quiet 00:00-07:00   or   /quiet off")


# ============================================================
# REPORT UI, EXTRA DATA, HELPERS
# ============================================================

def esc(x):
    return _html.escape(str(x), quote=False)


def B(x):
    return f"<b>{esc(x)}</b>"


ARROWS = {"BULLISH": "⬆️", "WEAK BULLISH": "↗️", "NEUTRAL": "➡️",
          "WEAK BEARISH": "↘️", "BEARISH": "⬇️"}


def arrow(trend):
    return ARROWS.get(trend, "➡️")


def _num(x, fmt="{:.0f}", default="n/a"):
    return default if x is None else fmt.format(x)


def _age(ts):
    if not ts:
        return "n/a"
    m = int((time.time() - ts) / 60)
    return "just now" if m < 1 else f"{m} min ago"


def _dist(level, price):
    """'$68,100.00 (+1.0%)' - distance from current price."""
    if level is None or not price:
        return "n/a"
    return f"{fmt_price(level)} ({(level - price) / price * 100:+.1f}%)"


def _plain(html_text):
    return _html.unescape(re.sub(r"<[^>]+>", "", html_text))


def tg_send_html(text, reply_markup=None, level="low"):
    return tg_send(text, reply_markup, True, level)


TAB_LABELS = {
    "summary": "🏠 Summary", "trend": "📈 Trend", "futures": "🎲 Futures",
    "macro": "🌍 Macro", "news": "📰 News", "levels": "🧭 Levels",
    "stats": "📊 Stats", "glossary": "📖 Glossary", "watch": "👀 Watch",
}


def tab_keyboard(active="summary"):
    btns = [{"text": ("• " if k == active else "") + label, "callback_data": f"tab:{k}"}
            for k, label in TAB_LABELS.items()]
    rows = [btns[i:i + 3] for i in range(0, len(btns), 3)]
    rows.append([{"text": "🖼 Chart", "callback_data": "chart"}])
    return {"inline_keyboard": rows}


def funding_short(funding):
    if funding is None:
        return "n/a"
    f = funding * 100
    mood = ("longs very crowded ⚠️" if f > 0.05 else "longs slightly ahead" if f > 0.01 else
            "shorts very crowded ⚠️" if f < -0.05 else "shorts slightly ahead" if f < -0.01 else
            "balanced")
    return f"{mood} (funding {f:+.3f}%)"


def macro_short(cross):
    parts = []
    dxy = (cross.get("DXY") or {}).get("change")
    nq = (cross.get("NASDAQ") or {}).get("change")
    if dxy is not None:
        tag = "✅" if dxy < -0.15 else "❌" if dxy > 0.15 else "➖"
        parts.append(f"Dollar {dxy:+.2f}% {tag}")
    if nq is not None:
        tag = "✅" if nq > 0.3 else "❌" if nq < -0.3 else "➖"
        parts.append(f"Nasdaq {nq:+.2f}% {tag}")
    return " | ".join(parts) if parts else "n/a"


def trend_meaning(h1, h4, m15, d1=None, w1=None):
    t4, t1 = h4.get("trend"), h1.get("trend")
    bull, bear = ("BULLISH", "WEAK BULLISH"), ("BEARISH", "WEAK BEARISH")
    if t4 in bull and t1 in bull:
        txt = "Bigger charts agree on UP. Buying dips is safer than buying breakouts."
    elif t4 in bear and t1 in bear:
        txt = "Bigger charts agree on DOWN. Selling bounces is safer than selling breakdowns."
    elif (t4 in bull and t1 in bear) or (t4 in bear and t1 in bull):
        txt = "Timeframes disagree - this is how choppy, unreliable markets look. Be patient."
    else:
        txt = "No clear direction yet. Waiting is a position."
    if m15.get("trend") in bear and t4 in bull:
        txt += " Short-term is pulling back inside the bigger uptrend."
    elif m15.get("trend") in bull and t4 in bear:
        txt += " Short-term is bouncing inside the bigger downtrend."
    td, tw = (d1 or {}).get("trend"), (w1 or {}).get("trend")
    if td in bull and tw in bull and t4 in bear:
        txt += " Daily and weekly are still UP, so this looks like a pullback in a bigger uptrend."
    elif td in bear and tw in bear and t4 in bull:
        txt += " Daily and weekly are still DOWN, so this rise is a bounce in a bigger downtrend - be careful."
    return txt


def strength_stars(score):
    n = int(clamp(round(abs(score) / 4.5 * 5), 1, 5))
    return "★" * n + "☆" * (5 - n)


def build_card(state, update_delta=False):
    c = CTX
    ticker, tech = c.get("ticker") or {}, c.get("tech") or {}
    cross, news, events = c.get("cross") or {}, c.get("news") or [], c.get("events") or []
    bias, levels, plan = c["bias"], c.get("levels") or {}, c["plan"]
    futures, kl = c.get("futures") or {}, c.get("keylevels") or {}
    h1, h4, m15, d1 = (tech.get("1h") or {}, tech.get("4h") or {},
                       tech.get("15m") or {}, tech.get("1d") or {})
    price = ticker.get("price") or levels.get("price")
    t = state.get("active_trade")

    L = [B(f"₿ BTC {fmt_price(price)}") + f"  (24h {esc(fmt_pct(ticker.get('change_24h')))})",
         esc(utc_text()), "━━━━━━━━━━━━━━━━━━━━"]

    # ---- verdict
    if t:
        L.append(B(f"📌 VERDICT: IN A {t['action']} TRADE"))
        L += [esc(x) for x in active_trade_lines(t, price)]
        L.append("No new trade while this one is active.")
    elif plan["action"] in ("LONG", "SHORT"):
        icon = "🟢" if plan["action"] == "LONG" else "🔴"
        n = (state.get("sig_streak") or {}).get("count", 0)
        confirming = f" - confirming {n}/{confirm_needed()}" if n < confirm_needed() else ""
        L.append(B(f"{icon} VERDICT: {plan['action']} setup{confirming}"))
        L.append(f"Setup strength: {strength_stars(bias['score'])}")
        L.append(f"Order: {'BUY' if plan['action'] == 'LONG' else 'SELL'} LIMIT at {esc(fmt_price(plan['entry']))}")
        L.append(f"Stop: {esc(fmt_price(plan['stop']))} | TP1: {esc(fmt_price(plan['tp1']))} | "
                 f"TP2: {esc(fmt_price(plan['tp2']))}")
        L.append(f"Risk ${plan['risk_usd']:,.2f} → size ≈ {plan['size_btc']:.4f} BTC "
                 f"(≈ ${plan['notional']:,.0f}, ~{plan.get('leverage', 0):.1f}x leverage)")
        if plan.get("capped"):
            L.append(f"⚠️ Size capped at {max_lev_now():.1f}x leverage (stop is wide for your account).")
        L.append("Why: trend, momentum, volume and trend-strength filters all agree.")
    else:
        L.append(B("🟡 VERDICT: WAIT"))
        why = (plan.get("why") or ["No clear edge right now."])[0]
        L.append("Why: " + esc(why))
        L.append(f"Mood strength: {strength_stars(bias['score'])} ({esc(bias['bias'])})")

    # ---- quick read
    stale_f = " (stale)" if "funding" in (futures.get("stale") or []) else ""
    L += ["",
          B("Quick read"),
          f"Trend: 1d {arrow(d1.get('trend'))} | 4h {arrow(h4.get('trend'))} | "
          f"1h {arrow(h1.get('trend'))} | 15m {arrow(m15.get('trend'))}",
          "Momentum: " + esc(momentum_words(h1.get("rsi"))),
          "Crowd: " + esc(funding_short(futures.get("funding")) + stale_f),
          "Macro: " + esc(macro_short(cross))]
    extra = []
    if (c.get("glob") or {}).get("btc_dom"):
        extra.append(f"BTC dominance {c['glob']['btc_dom']:.1f}%")
    if (c.get("options") or {}).get("pc_oi") is not None:
        extra.append(f"options put/call {c['options']['pc_oi']:.2f}")
    if extra:
        L.append("Market: " + esc(" | ".join(extra)))
    L.append("News: " + esc(news_mood(news).replace("News mood: ", "")))

    # ---- levels
    if levels:
        L += ["",
              f"🧭 Ceiling {esc(_dist(levels.get('resistance'), price))}",
              f"    Floor   {esc(_dist(levels.get('support'), price))}"]
        if kl.get("vwap") and price:
            L.append(f"    Today's VWAP {esc(fmt_price(kl['vwap']))} "
                     f"(price is {'above' if price > kl['vwap'] else 'below'} it)")
        if not t and plan["action"] == "WAIT":
            L.append(f"LONG only above {esc(fmt_price(plan.get('trigger_long')))} | "
                     f"SHORT only below {esc(fmt_price(plan.get('trigger_short')))}")

    # ---- next event
    nxt = upcoming_high_event(events, 7 * 24 * 60)
    if nxt and nxt[1] > 0:
        e, mins = nxt
        mins = int(mins)
        when = f"{mins} min" if mins < 60 else f"{mins // 60}h {mins % 60}m"
        warn = " → avoid new trades" if mins <= NO_TRADE_BEFORE_EVENT_MIN else ""
        L += ["", f"⏰ Next big event: {esc(e['name'])} in {when}{warn}"]
        rt = reaction_text(state, e["name"])
        if rt:
            L.append(esc(rt))

    note = liquidity_note()
    if note:
        L += ["", "⚠️ " + esc(note)]

    # ---- what changed
    prev = state.get("last_card")
    if prev and price and prev.get("price"):
        ch = (price - prev["price"]) / prev["price"] * 100
        bits = [f"price {ch:+.2f}%"]
        bits.append("mood unchanged" if prev.get("bias") == bias["bias"]
                    else f"mood {prev.get('bias')} → {bias['bias']}")
        if prev.get("action") != plan["action"]:
            bits.append(f"signal {prev.get('action')} → {plan['action']}")
        L += ["", "🔄 Since last update: " + esc(", ".join(bits))]
    if update_delta and price:
        state["last_card"] = {"price": price, "bias": bias["bias"], "action": plan["action"]}

    L += ["",
          f"<i>Data age: price {_age(HEALTH.get('price'))} | macro/news {_age(CTX.get('slow_ts'))}</i>",
          "<i>Tap a tab for details. Not advice - any trade can lose. The bot never places orders.</i>"]
    return "\n".join(L)


def tab_trend(state):
    tech = CTX.get("tech") or {}
    L = [B("📈 TREND - which way is price heading?"), ""]
    for tf, label in (("5m", "5 min "), ("15m", "15 min"), ("1h", "1 hour"),
                      ("4h", "4 hour"), ("1d", "1 day "), ("1w", "1 week")):
        s = tech.get(tf) or {}
        if not s:
            L.append(f"{esc(label)}: loading...")
            continue
        L.append(f"{esc(label)}: {esc(trend_words(s.get('trend')))} | RSI {_num(s.get('rsi'))} "
                 f"| ADX {_num(s.get('adx'))}")
    h1, h4, m15 = tech.get("1h") or {}, tech.get("4h") or {}, tech.get("15m") or {}
    L += ["", B("What it means"),
          esc(trend_meaning(h1, h4, m15, tech.get("1d"), tech.get("1w"))),
          "", "Strength: " + esc(trend_strength_words(h1.get("adx"))),
          "Momentum: " + esc(momentum_words(h1.get("rsi"))),
          "", f"<i>Overall score {CTX['bias']['score']:+.1f} (daily/weekly are shown for context; "
              "the trade score uses 15m, 1h and 4h)</i>"]
    return "\n".join(L)


def tab_futures(state):
    c = CTX
    fut = c.get("futures") or {}
    L = [B("🎲 FUTURES & OPTIONS - what the crowd is doing"), "",
         "• " + esc(funding_words(fut.get("funding"))),
         "• " + esc(oi_words(fut)),
         "• " + esc(liq_words(fut))]
    L += ["• " + esc(x) for x in crowd_words(c.get("crowd"))]
    L += ["• " + esc(fng_words(c.get("fng"))),
          "• " + esc(options_words(c.get("options")))]
    if fut.get("stale"):
        L += ["", "⚠️ Some values above are from a previous cycle (marked stale): "
              + esc(", ".join(fut["stale"]))]
    L += ["", "<i>Extreme crowding is a contrarian warning, not a signal by itself.</i>"]
    return "\n".join(L)


def tab_macro(state):
    c = CTX
    cross = c.get("cross") or {}
    L = [B("🌍 MACRO - outside markets"), "",
         "• " + esc(cross_line("DXY", cross.get("DXY"), False, "US Dollar (DXY)")),
         "• " + esc(cross_line("NASDAQ", cross.get("NASDAQ"), True, "Nasdaq futures")),
         "• " + esc(cross_line("US10Y", cross.get("US10Y"), False, "US 10Y yield")),
         "• " + esc(cross_line("VIX", cross.get("VIX"), False, "Fear index (VIX)")),
         "• " + esc(cross_line("GOLD", cross.get("GOLD"), None, "Gold")),
         "• " + esc(global_words(c.get("glob"))),
         "", B("⏰ Big events - next 7 days")]
    events = c.get("events") or []
    L += [esc(x) for x in upcoming_events_text(events)]
    seen = set()
    for e in events:
        rt = reaction_text(state, e["name"])
        if rt and e["name"] not in seen and e["time"] >= now_utc() and len(seen) < 3:
            seen.add(e["name"])
            L.append("   " + esc(rt))
    return "\n".join(L)


def tab_news(state):
    news = CTX.get("news") or []
    L = [B("📰 NEWS"), esc(news_mood(news)), ""]
    ai = NEWS_AI_CACHE.get("text")
    if ai:
        L += [esc(ai), ""]
    L += [esc(x) for x in format_news(news, state)]
    return "\n".join(L)


def tab_levels(state):
    c = CTX
    lv, kl = c.get("levels") or {}, c.get("keylevels") or {}
    if not lv:
        return "Levels not available yet."
    p = lv["price"]
    L = [B("🧭 KEY LEVELS"), "",
         f"Ceiling (resistance): {esc(_dist(lv.get('resistance'), p))}",
         f"Floor (support):      {esc(_dist(lv.get('support'), p))}",
         f"Normal 1h swing: ±{esc(fmt_price(lv['atr']))} (±{lv['atr_pct']:.2f}%)"]
    names = [("vwap", "Today's VWAP"), ("day_open", "Today's open"), ("prev_high", "Yesterday high"),
             ("prev_low", "Yesterday low"), ("week_open", "This week's open"),
             ("prev_week_high", "Last week high"), ("prev_week_low", "Last week low")]
    rows = [f"{esc(label)}: {esc(_dist(kl.get(k), p))}" for k, label in names if kl.get(k)]
    if rows:
        L += ["", B("Reference levels (where traders react)")] + rows
    L += ["", B("Scenarios"),
          f"🟢 If price breaks UP with volume: next stops {esc(fmt_price(lv['bull1']))}, then {esc(fmt_price(lv['bull2']))}",
          f"🔴 If price breaks DOWN: next stops {esc(fmt_price(lv['bear1']))}, then {esc(fmt_price(lv['bear2']))}",
          "↔️ If it stays between floor and ceiling: range - trend trades often fail.",
          "", "<i>A break only counts if a candle CLOSES beyond the level. Wicks often reverse.</i>"]
    return "\n".join(L)


def tab_stats(state):
    L = [B("📊 TRACK RECORD"), ""]
    L += [esc(x) for x in stats_lines(state["trades"], "All time", state.get("account"))]
    L += [esc(x) for x in my_stats_lines(state)]
    t = state.get("active_trade")
    if t:
        L += ["", B("Current trade")] + [esc(x) for x in active_trade_lines(t, (CTX.get("ticker") or {}).get("price"))]
    L += ["", "<i>Costs included: maker/taker fees and slippage. Use /export to download every trade.</i>"]
    return "\n".join(L)


def tab_glossary(state):
    return "\n".join([
        B("📖 GLOSSARY"), "",
        "<b>Trend</b> - direction price has been moving on that chart.",
        "<b>RSI</b> - momentum 0-100. Above 70 = stretched up, below 30 = stretched down.",
        "<b>ADX</b> - trend STRENGTH. Below 18 = choppy sideways market.",
        "<b>ATR</b> - normal swing size; used for stop distance.",
        "<b>VWAP</b> - average price paid today, weighted by volume. Above = buyers in control.",
        "<b>Support / Resistance</b> - floor / ceiling where price often reacts.",
        "<b>Funding</b> - fee between longs and shorts. High = crowd is too long.",
        "<b>Open interest (OI)</b> - total open futures bets. Rising = new money entering.",
        "<b>Put/call ratio</b> - puts are bets on a fall, calls on a rise. Above 1 = more put bets.",
        "<b>Implied volatility</b> - how big a move the options market expects.",
        "<b>Liquidation</b> - forced closing of a leveraged position.",
        "<b>Leverage</b> - position size divided by your account. 3x = a position 3 times your money.",
        "<b>Limit order</b> - waits for your price instead of buying at once.",
        "<b>R</b> - your risk on one trade. +2R = won twice what you risked.",
        "<b>TP1 / TP2</b> - take-profit levels. At TP1 close half, stop to breakeven.",
        "<b>Setup strength</b> - how many signals agree. NOT a probability of winning.",
    ])


TAB_BUILDERS = {"summary": lambda s: build_card(s), "trend": tab_trend, "futures": tab_futures,
                "macro": tab_macro, "news": tab_news, "levels": tab_levels,
                "stats": tab_stats, "glossary": tab_glossary, "watch": tab_watch}


def build_tab(tab, state):
    fn = TAB_BUILDERS.get(tab, TAB_BUILDERS["summary"])
    try:
        return fn(state)[:3900]
    except Exception:
        traceback.print_exc()
        return "Could not build this section right now. Try again in a minute."


def send_card(state):
    tg_send_html(localize(build_card(state, update_delta=True), state), tab_keyboard("summary"))


def handle_tab_callback(state, cq):
    tab = cq.get("data", "tab:summary")[4:]
    if not CTX.get("bias"):
        return
    msg = cq.get("message") or {}
    text = localize(build_tab(tab, state), state)
    payload = {
        "chat_id": msg.get("chat", {}).get("id"),
        "message_id": msg.get("message_id"),
        "text": text[:4000],
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
        "reply_markup": json.dumps(tab_keyboard(tab)),
    }
    if tg_post("editMessageText", payload) is None:
        # bad markup (e.g. after translation) -> retry as plain text
        payload.pop("parse_mode")
        payload["text"] = _plain(text)[:4000]
        tg_post("editMessageText", payload)


def _track(url, ok):
    try:
        host = urlparse(url).netloc
    except Exception:
        return
    h = SOURCE_HEALTH.setdefault(host, {"ok": 0, "fail": 0})
    h["ok" if ok else "fail"] += 1


SLOW_TFS = {"1d": 900, "1w": 3600}   # seconds between refreshes of slow timeframes


def _compile_words(words):
    return [re.compile(r"\b" + re.escape(w) + r"(?:s|es)?\b", re.I) for w in words]


_HIGH_RX = _compile_words([
    "fed", "federal reserve", "fomc", "rate hike", "rate cut",
    "interest rate", "cpi", "pce", "ppi", "nonfarm", "payroll",
    "unemployment", "jobs report", "treasury", "yield",
    "etf", "sec", "regulation", "ban", "banned", "approval", "hack", "hacked",
    "liquidation", "war", "sanction", "tariff", "china",
    "bitcoin reserve", "sovereign", "default", "recession",
])


_BULL_RX = _compile_words([
    "etf inflow", "inflow", "approval", "approved", "adoption",
    "reserve", "rate cut", "cuts rates", "dovish", "easing",
    "liquidity", "stimulus", "buying", "accumulation",
    "bullish", "breakout", "surge", "surged", "soared", "rally", "rallied", "record inflow",
])


_BEAR_RX = _compile_words([
    "etf outflow", "outflow", "rejected", "ban", "banned", "hack", "hacked",
    "rate hike", "hikes rates", "hawkish", "tightening",
    "liquidation", "selling", "sell-off", "bearish", "crash", "crashed",
    "sanction", "tariff", "inflation", "yields rise",
])


def _ff_ttl():
    now = now_utc()
    for e in FF_CACHE.get("events") or []:
        mins = (e["time"] - now).total_seconds() / 60
        if -45 <= mins <= 60:
            return 240      # near an event: refresh often so results show up quickly
    return 1800             # otherwise be gentle with the rate limit


def signal_features(bias, plan):
    """Snapshot of every input at signal time, so the REAL (live) system can be evaluated later."""
    c = CTX
    tech, fut = c.get("tech") or {}, c.get("futures") or {}
    cross, crowd = c.get("cross") or {}, c.get("crowd") or {}
    h1, m15 = tech.get("1h") or {}, tech.get("15m") or {}
    try:
        return {
            "score": round(bias["score"], 2), "bias": bias["bias"], "reasons": bias.get("reasons"),
            "t15": m15.get("trend"), "t1h": h1.get("trend"),
            "t4h": (tech.get("4h") or {}).get("trend"),
            "t1d": (tech.get("1d") or {}).get("trend"),
            "t1w": (tech.get("1w") or {}).get("trend"),
            "adx_1h": h1.get("adx"), "rsi_1h": h1.get("rsi"),
            "volratio_15m": m15.get("volume_ratio"), "atr_pct_1h": h1.get("atr_pct"),
            "funding": fut.get("funding"), "oi_change_pct": fut.get("oi_change_pct"),
            "fng": (c.get("fng") or {}).get("value"),
            "taker_buy": crowd.get("taker_buy_ratio"), "ls_ratio": crowd.get("ls_ratio"),
            "dxy": (cross.get("DXY") or {}).get("change"),
            "nasdaq": (cross.get("NASDAQ") or {}).get("change"),
            "us10y": (cross.get("US10Y") or {}).get("change"),
            "vix": (cross.get("VIX") or {}).get("change"),
            "options_pc_oi": (c.get("options") or {}).get("pc_oi"),
            "btc_dominance": (c.get("glob") or {}).get("btc_dom"),
            "leverage": plan.get("leverage"),
        }
    except Exception:
        traceback.print_exc()
        return {}


def log_trade_features(t):
    try:
        with open(FEATURE_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(t, default=str) + "\n")
    except Exception:
        traceback.print_exc()


def backtest_extras(ind, trades, warm):
    first, last = float(ind["close"].iloc[warm]), float(ind["close"].iloc[-1])
    bh = (last - first) / first * 100
    s = trade_stats(trades)
    if s["n"]:
        print(f"Buy & hold BTC: {bh:+.1f}%  |  Strategy at {RISK_PCT_DEFAULT:.0f}% risk/trade "
              f"(not compounded): {s['total_r'] * RISK_PCT_DEFAULT:+.1f}%")
    closed = [t for t in trades if t.get("status") == "CLOSED" and t.get("result_r") is not None]
    by_month = {}
    for t in closed:
        m = datetime.fromtimestamp(t["closed_at"], UTC).strftime("%Y-%m")
        by_month.setdefault(m, []).append(t["result_r"])
    if by_month:
        print("\nMonthly results (R):")
        for m in sorted(by_month):
            rs = by_month[m]
            print(f"  {m}: {len(rs):3d} trades | {sum(1 for r in rs if r > 0) / len(rs) * 100:3.0f}% wins "
                  f"| {sum(rs):+6.2f}R")
    if len(closed) >= 20:
        half = len(closed) // 2
        for name, part in (("First half", closed[:half]), ("Second half", closed[half:])):
            rs = [t["result_r"] for t in part]
            print(f"{name}: {len(rs)} trades | {sum(1 for r in rs if r > 0) / len(rs) * 100:.0f}% wins "
                  f"| {sum(rs):+.2f}R  (big gap between halves = unstable strategy)")
    if s["n"] < 100:
        print(f"\n⚠️ Only {s['n']} trades - statistically weak. Test a longer period before trusting this.")
    print("Costs included: maker fee on entry/TP, taker fee + slippage on stops.")


LANG_NAMES = {"en": None,
              "simple": "very simple English (short sentences, no jargon, explain terms in a few words)",
              "ur": "Urdu (اردو), written in Urdu script"}


def localize(text, state):
    """Optional: rewrite a message in simple English or Urdu using Claude (needs ANTHROPIC_API_KEY)."""
    lang = (state or {}).get("lang", "en")
    if lang == "en" or lang not in LANG_NAMES or not ANTHROPIC_API_KEY:
        return text
    key = (lang, hashlib.sha1(text.encode("utf-8")).hexdigest())
    if key in LANG_CACHE:
        return LANG_CACHE[key]
    out = claude_call(
        "You rewrite Telegram messages. Keep every HTML tag (<b>, <i>) exactly as is, and keep all "
        "numbers, prices, tickers, symbols and emojis unchanged. Output only the rewritten message.",
        f"Rewrite this message in {LANG_NAMES[lang]}:\n\n{text}", max_tokens=1800)
    if out:
        if len(LANG_CACHE) > 40:
            LANG_CACHE.clear()
        LANG_CACHE[key] = out
        return out
    return text


def record_price(state, price):
    if not price:
        return
    h = state.setdefault("price_hist", [])
    if h and time.time() - h[-1][0] < 50:
        return
    h.append([time.time(), price])


def _price_near(hist, ts, tol=300):
    best = None
    for t, p in hist:
        d = abs(t - ts)
        if d <= tol and (best is None or d < best[0]):
            best = (d, p)
    return best[1] if best else None


def update_event_reactions(state, events):
    """30 minutes after each big event, store how far BTC moved (builds a personal history)."""
    hist = state.get("price_hist") or []
    react = state.setdefault("event_reactions", {})
    now = time.time()
    for e in events or []:
        key = f"{e['id']}:REACT"
        if state["event_alerts"].get(key):
            continue
        t0 = e["time"].timestamp()
        if now < t0 + 35 * 60 or now > t0 + 3 * 3600:
            continue
        p0, p1 = _price_near(hist, t0), _price_near(hist, t0 + 30 * 60)
        if p0 and p1:
            lst = react.setdefault(e["name"], [])
            lst.append({"t": t0, "pct": round((p1 - p0) / p0 * 100, 2)})
            react[e["name"]] = lst[-8:]
        state["event_alerts"][key] = now


def reaction_text(state, name):
    lst = ((state or {}).get("event_reactions") or {}).get(name) or []
    if not lst:
        return None
    return (f"BTC after the last {min(3, len(lst))} '{name}': "
            + ", ".join(f"{x['pct']:+.1f}%" for x in lst[-3:]) + " (30 min later)")


def get_key_levels():
    out = {}
    d1, w1, m5 = CANDLES.get("1d"), CANDLES.get("1w"), CANDLES.get("5m")
    try:
        if d1 is not None and len(d1) >= 3:
            out["day_open"] = safe_float(d1.iloc[-1]["open"])
            out["prev_high"] = safe_float(d1.iloc[-2]["high"])
            out["prev_low"] = safe_float(d1.iloc[-2]["low"])
        if w1 is not None and len(w1) >= 3:
            out["week_open"] = safe_float(w1.iloc[-1]["open"])
            out["prev_week_high"] = safe_float(w1.iloc[-2]["high"])
            out["prev_week_low"] = safe_float(w1.iloc[-2]["low"])
        if m5 is not None and len(m5) > 20:
            midnight = int(datetime.now(UTC).replace(hour=0, minute=0, second=0,
                                                     microsecond=0).timestamp() * 1000)
            day = m5[m5["time"] >= midnight]
            if len(day) >= 3 and day["volume"].sum() > 0:
                tp = (day["high"] + day["low"] + day["close"]) / 3
                out["vwap"] = float((tp * day["volume"]).sum() / day["volume"].sum())
    except Exception:
        traceback.print_exc()
    return out


def get_global_market():
    if time.time() - GLOBAL_CACHE["ts"] < 600 and GLOBAL_CACHE["data"]:
        return GLOBAL_CACHE["data"]
    d = http_json(COINGECKO + "/api/v3/global")
    try:
        g = d["data"]
        GLOBAL_CACHE.update(ts=time.time(), data={
            "btc_dom": safe_float(g["market_cap_percentage"]["btc"]),
            "mcap_change": safe_float(g.get("market_cap_change_percentage_24h_usd"))})
    except Exception:
        pass
    return GLOBAL_CACHE["data"]


def get_options_context():
    """Deribit public data: put/call ratio and implied volatility near the money."""
    if time.time() - OPT_CACHE["ts"] < 600 and OPT_CACHE["data"]:
        return OPT_CACHE["data"]
    d = http_json(DERIBIT + "/api/v2/public/get_book_summary_by_currency",
                  {"currency": "BTC", "kind": "option"}, timeout=20)
    rows = (d or {}).get("result") or []
    if not rows:
        return OPT_CACHE["data"]
    c_oi = p_oi = c_vol = p_vol = iv_w = iv_oi = 0.0
    for r in rows:
        name = str(r.get("instrument_name", ""))
        oi = safe_float(r.get("open_interest"), 0) or 0
        vol = safe_float(r.get("volume"), 0) or 0
        if name.endswith("-C"):
            c_oi += oi
            c_vol += vol
        elif name.endswith("-P"):
            p_oi += oi
            p_vol += vol
        else:
            continue
        iv, und = safe_float(r.get("mark_iv")), safe_float(r.get("underlying_price"))
        try:
            strike = float(name.split("-")[2])
        except Exception:
            continue
        if iv and und and oi > 0 and abs(strike / und - 1) <= 0.10:
            iv_w += iv * oi
            iv_oi += oi
    if c_oi <= 0:
        return OPT_CACHE["data"]
    data = {"pc_oi": p_oi / c_oi,
            "pc_vol": (p_vol / c_vol) if c_vol > 0 else None,
            "iv": (iv_w / iv_oi) if iv_oi > 0 else None}
    OPT_CACHE.update(ts=time.time(), data=data)
    return data


def options_words(opt):
    if not opt:
        return "Options: data unavailable this cycle"
    pc = opt["pc_oi"]
    mood = ("far more puts than calls -> traders are hedging / betting on a drop" if pc > 1.2 else
            "slightly more puts than calls" if pc > 0.9 else
            "far more calls than puts -> traders are betting on a rise" if pc < 0.6 else
            "balanced puts and calls")
    txt = f"Options put/call (open interest): {pc:.2f} -> {mood}"
    if opt.get("iv"):
        iv = opt["iv"]
        txt += (f"\n• Implied volatility ~{iv:.0f}% -> "
                + ("calm, options market expects small moves" if iv < 40 else
                   "normal" if iv < 60 else "high, options market expects big moves"))
    return txt


def global_words(g):
    if not g:
        return "Crypto market: data unavailable this cycle"
    txt = f"BTC dominance: {g['btc_dom']:.1f}%"
    if g.get("mcap_change") is not None:
        txt += f" | whole crypto market 24h: {g['mcap_change']:+.1f}%"
    txt += (" -> money is flowing into BTC vs altcoins" if g["btc_dom"] and g["btc_dom"] > 58 else
            " -> altcoins are getting a larger share" if g["btc_dom"] and g["btc_dom"] < 50 else "")
    return txt


def liquidity_note():
    n = now_utc()
    if n.weekday() >= 5:
        return "Weekend: thinner liquidity, fake breakouts and sudden wicks are more common."
    return None


def health_text():
    lines = ["🩺 DATA SOURCES (this session)"]
    if not SOURCE_HEALTH:
        lines.append("No requests made yet.")
    for host, h in sorted(SOURCE_HEALTH.items()):
        tot = h["ok"] + h["fail"]
        rate = h["ok"] / tot * 100 if tot else 0
        icon = "✅" if rate >= 90 else "⚠️" if rate >= 50 else "❌"
        lines.append(f"{icon} {host}: {h['ok']}/{tot} ok")
    lines.append(f"Liquidation websocket: {'connected (' + WS_STATE['source'] + ')' if WS_STATE['connected'] else 'not connected (REST fallback)'}")
    return "\n".join(lines)


def send_document(path, caption=""):
    if not TOKEN or not CHAT_ID or not os.path.exists(path):
        return False
    with open(path, "rb") as f:
        data = f.read()
    res = tg_post("sendDocument", data={"chat_id": CHAT_ID, "caption": caption[:1000]},
                  files={"document": (os.path.basename(path), data)}, timeout=60)
    return bool(res and res.get("ok"))


# ============================================================
# TELEGRAM COMMANDS
# ============================================================

HELP_TEXT = (
    "🤖 WHAT I CAN DO\n\n"
    "ASK ME (just type):\n"
    "• why did SOL pump today?\n• weekend outlook\n• top movers\n• how is DOGE doing?\n\n"
    "MARKET\n"
    "/report - market card (tabs inside)\n/why SOL - why a coin pumped or dumped\n"
    "/coin SOL - quick analysis of any coin\n/movers - top gainers and losers\n"
    "/weekend - weekend / market-closed outlook\n/plan - today's plan\n/glossary - plain-English terms\n\n"
    "TRADES\n"
    "/trade - live trade\n/stats - track record (+ your own calls)\n/chart - chart with levels\n"
    "/account 500 - account size in USD\n/risk 1 - risk per trade % (0.1-3)\n"
    "/pause /resume - stop / allow new signals\n\n"
    "ALERTS\n"
    "/alert 70000 - price alert (also /alert SOL 200, /alert close 1h above 68000)\n"
    "/alert list | /alert del 3 | /alert clear\n"
    "/watch SOL ETH - watchlist (earlier pump/dump alerts) | /unwatch SOL\n\n"
    "SETTINGS\n"
    "/tz Asia/Karachi - your timezone\n/quiet 00:00-07:00 - silent hours\n"
    "/mode important - only trade alerts make sound\n/lang en|simple|ur - language\n\n"
    "SYSTEM\n"
    "/status /health /export /weekly /start (setup)"
)


def cmd_status(state):
    c = CTX
    if not c.get("bias"):
        send("Still loading market data. Try again in a minute.")
        return
    price = (c.get("ticker") or {}).get("price")
    age = int((time.time() - HEALTH["price"]) / 60)
    past = [e for e in (c.get("events") or [])
            if e["time"] < now_utc() and (now_utc() - e["time"]).days < 7]
    has_actual = any(e.get("actual") is not None for e in past) if past else None
    results_line = ("n/a yet (no past events in feed)" if has_actual is None else
                    "working" if has_actual else
                    "NOT provided by the calendar source - only pre-event alerts will fire")
    lines = [f"📡 STATUS - {utc_text()}",
             f"BTC: {fmt_price(price)}",
             f"Mood: {c['bias']['bias']} (score {c['bias']['score']:+.1f})",
             f"Signal now: {c['plan']['action']} "
             f"(confirming {(state.get('sig_streak') or {}).get('count', 0)}/{confirm_needed()})",
             f"Signals: {'PAUSED' if state.get('paused') else 'active'}",
             f"Account: ${state['account']:,.0f} | Risk: {state['risk_pct']}% per trade "
             f"| Max leverage: {max_lev_now():.1f}x",
             f"Price data age: {age} min | Liquidation feed: "
             f"{'live websocket' if WS_STATE['connected'] else 'REST fallback'}",
             f"Event results feed: {results_line}",
             f"Claude news: {'on' if ANTHROPIC_API_KEY else 'off (no ANTHROPIC_API_KEY)'} "
             f"| Language: {state.get('lang', 'en')}"]
    cd = state.get("cooldown_until", 0) - time.time()
    if cd > 0:
        lines.append(f"Cooldown: {cd / 60:.0f} min left")
    t = state.get("active_trade")
    if t:
        lines += [""] + active_trade_lines(t, price)
    send("\n".join(lines))


def handle_command(state, cmd, args):
    if cmd == "/start":
        send_onboarding(state)
    elif cmd == "/help":
        send(HELP_TEXT)
    elif cmd == "/report":
        send_market_report(state)
    elif cmd == "/details":
        if CTX.get("bias"):
            send_card(state)
        else:
            send("No report yet. Send /report first.")
    elif cmd == "/glossary":
        tg_send_html(localize(build_tab("glossary", state), state), tab_keyboard("glossary"))
    elif cmd == "/status":
        cmd_status(state)
    elif cmd == "/health":
        send(health_text())
    elif cmd == "/export":
        sent = False
        for p, cap in ((TRADE_LOG_CSV, "All closed trades"), (FEATURE_LOG, "Signals with all inputs (JSON lines)")):
            sent = send_document(p, cap) or sent
        if not sent:
            send("No trade files yet - they are created after the first closed trade.")
    elif cmd == "/lang":
        choice = args[0].lower() if args else ""
        if choice in LANG_NAMES:
            state["lang"] = choice
            note = "" if (ANTHROPIC_API_KEY or choice == "en") else " (needs ANTHROPIC_API_KEY to translate)"
            send(f"✅ Language set to {choice}{note}.")
        else:
            send("Usage: /lang en | simple | ur")
    elif cmd == "/why":
        cmd_why(state, args)
    elif cmd == "/coin":
        cmd_coin(state, args)
    elif cmd == "/movers":
        cmd_movers(state)
    elif cmd == "/weekend":
        if CTX.get("bias"):
            tg_send_html(localize(weekend_text(state, "now"), state), None, level="normal")
        else:
            send("Still loading market data. Try again in a minute.")
    elif cmd == "/plan":
        if CTX.get("bias"):
            tg_send_html(localize(build_daily_plan(state), state), tab_keyboard("summary"), level="normal")
        else:
            send("Still loading market data. Try again in a minute.")
    elif cmd == "/weekly":
        send(weekly_report_text(state))
    elif cmd == "/watch":
        cmd_watch(state, args)
    elif cmd == "/unwatch":
        cmd_watch(state, args, remove=True)
    elif cmd == "/alert":
        cmd_alert(state, args)
    elif cmd == "/tz":
        cmd_tz(state, args)
    elif cmd == "/quiet":
        cmd_quiet(state, args)
    elif cmd == "/mode":
        if args and args[0].lower() in ("all", "important"):
            state["alert_mode"] = args[0].lower()
            send(f"✅ Alert mode: {state['alert_mode']}.")
        else:
            send(f"Alert mode: {state.get('alert_mode', 'all')}. Change: /mode all | /mode important")
    elif cmd == "/trade":
        t = state.get("active_trade")
        if t:
            tg_send_html(live_trade_text(t, (CTX.get("ticker") or {}).get("price")), trade_buttons(t["id"]), level="normal")
        else:
            send("No active trade right now.")
    elif cmd == "/stats":
        tg_send_html(build_tab("stats", state), tab_keyboard("stats"), level="normal")
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
        data = cq.get("data", "")
        if data.startswith("tab:"):
            handle_tab_callback(state, cq)
        elif data.startswith("tr:"):
            handle_trade_callback(state, cq)
        elif data.startswith("set:"):
            handle_setup_callback(state, cq)
        elif data.startswith("why:"):
            cmd_why(state, [data[4:]])
        elif data.startswith("coin:"):
            cmd_coin(state, [data[5:]])
        elif data == "movers":
            cmd_movers(state)
        elif data == "details":
            handle_command(state, "/details", [])
        elif data == "chart":
            handle_command(state, "/chart", [])
        return

    msg = u.get("message") or {}
    if str(msg.get("chat", {}).get("id", "")) != str(CHAT_ID):
        return  # ignore strangers
    text = (msg.get("text") or "").strip()
    if not text:
        return
    if text.startswith("/"):
        parts = text.split()
        handle_command(state, parts[0].split("@")[0].lower(), parts[1:])
    else:
        handle_text(state, text)


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
        backtest_extras(ind, trades, warm)
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
        mid = tg_send("⚠️ DATA PROBLEM\nNo fresh BTC price data for "
                      f"{stale_min:.0f} minutes (exchange APIs may be blocked or down).\n"
                      "Signals are unreliable until this recovers. Existing trades: follow your stop-loss.",
                      level="critical")
        pin_message(mid)
        state["health_msg"] = mid
        state["health_alert"] = True
    elif stale_min <= 10 and state.get("health_alert"):
        unpin_message(state.get("health_msg"))
        send("✅ Data feeds recovered. Normal operation resumed.")
        state["health_alert"] = False
        state["health_msg"] = None


def send_startup(restart=False):
    if restart:
        send("🔄 Bot restarted after an error and is running again. Open trades are restored.")
        return
    send(
        "₿ BTC MARKET BOT v3.3 STARTED\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "• A clear action every 30 min: LONG / SHORT / WAIT\n"
        "• Headline card + tabs: Trend, Futures, Macro, News, Levels, Stats\n"
        "• Limit-entry, stop-loss, TP1/TP2 and position size\n"
        "• Follow-up messages: fill, TP1 -> breakeven, trailing, exit\n"
        "• Instant alerts: trade setups, big news, market opens\n"
        "• Track record of every signal (/stats)\n\n"
        "Type /help for commands, /glossary for plain-English terms.\n"
        "⚠️ No guaranteed predictions. The bot never places trades."
    )


# ============================================================
# MAIN LOOP
# ============================================================

def run_bot(state, deadline, restart=False):
    send_startup(restart)
    start_liq_ws()
    register_commands()

    t_fast = t_slow = t_cal = t_tick = 0
    tech, futures, cross, news, events, ticker = {}, {}, {}, [], [], {}
    fng, crowd, glob, opts = None, {}, None, None
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
                    record_price(state, ticker["price"])
                t_fast = now

            if now - t_slow >= SLOW_POLL_INTERVAL:
                cross = get_cross_market()
                news = fetch_news()
                fng = get_fear_greed()
                crowd = get_crowd_context()
                glob = get_global_market()
                opts = get_options_context()
                check_breaking_news(state, news)
                CTX["slow_ts"] = time.time()
                t_slow = now

            if now - t_cal >= CALENDAR_POLL_INTERVAL:
                new_events = fetch_calendar()
                if new_events:
                    events = new_events
                t_cal = now

            if now - t_tick >= 120:
                try:
                    refresh_tickers()
                    update_tick_hist(state)
                    check_pump_dump(state)
                except Exception:
                    traceback.print_exc()
                t_tick = now
            update_event_reactions(state, events)
            check_event_alerts(state, events)
            check_actual_event_changes(state, events)
            check_market_open_alerts(state)
            check_daily_summary(state)
            check_health(state)
            update_event_banner(state, events)
            check_price_alerts(state)
            check_weekend_posts(state)
            check_weekly_report(state)

            if (tech.get("1h") or {}).get("price") is not None:
                live_price = ticker.get("price")
                bias = calculate_bias(tech, futures, cross, news, fng, crowd)
                levels = scenario_levels(tech, live_price)
                cd_min = max(0.0, (state.get("cooldown_until", 0) - time.time()) / 60)
                plan = build_trade_plan(bias, tech, levels, events,
                                        account=state["account"], risk_pct=state["risk_pct"],
                                        cooldown_min=cd_min, paused=state.get("paused", False),
                                        guard_text=check_risk_guard(state))
                CTX.update(ticker=ticker, tech=tech, futures=futures, cross=cross,
                           news=news, events=events, bias=bias, levels=levels,
                           plan=plan, fng=fng, crowd=crowd, glob=glob, options=opts,
                           keylevels=get_key_levels())

                manage_active_trade(state, live_price)
                process_signal(state, plan, bias, live_price)
                update_trade_pin(state, live_price)
                check_daily_plan(state)

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
    apply_user_tz(state)
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
