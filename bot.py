import os
import json
import time
import hashlib
import traceback
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests


# ============================================================
# BTC MARKET INTELLIGENCE TELEGRAM BOT  (v2 - easy mode)
# ------------------------------------------------------------
# - Every report starts with ONE clear action: LONG / SHORT / WAIT
# - Entry, stop-loss, take-profit and position-size example
# - Plain-English explanation of every indicator
# - Multiple data-source fallbacks so values are not "N/A"
# - Signals are informational only. NO ORDERS ARE PLACED.
# ============================================================


# ============================================================
# CONFIGURATION
# ============================================================

TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
STATE_FILE = os.environ.get("STATE_FILE", "btc_market_state.json")

BTC_SYMBOL = "BTCUSDT"
REPORT_INTERVAL = 30 * 60
DATA_POLL_INTERVAL = 60          # technical + futures refresh
SLOW_POLL_INTERVAL = 5 * 60      # news + cross-market refresh
CALENDAR_POLL_INTERVAL = 15 * 60  # economic calendar refresh
RUN_SECONDS = int(os.environ.get("RUN_SECONDS", str(350 * 60)))

EVENT_ALERT_WINDOWS = (60, 15, 5)
MARKET_OPEN_WINDOWS = (30, 5, 0)
NO_TRADE_BEFORE_EVENT_MIN = 45   # stay out this long before big news

MAX_NEWS_ITEMS = 6

# Position-size example shown in trade plans
ACCOUNT_EXAMPLE = 1000.0   # USD
RISK_PCT = 1.0             # % of account risked per trade

BINANCE_SPOT_HOSTS = [
    "https://data-api.binance.vision",   # works from most cloud hosts
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
    "User-Agent": "BTC-Market-Intelligence-Bot/2.0",
    "Accept": "*/*",
})

# Last-known-good caches so one failed request doesn't create "N/A".
TECH_CACHE = {}
FUT_CACHE = {}
CROSS_CACHE = {}


# ============================================================
# TELEGRAM
# ============================================================

def send(msg):
    if not TOKEN or not CHAT_ID:
        print("ERROR: TELEGRAM_TOKEN or TELEGRAM_CHAT_ID is missing.")
        return False

    chunks = [msg[i:i + 3900] for i in range(0, len(msg), 3900)] or [""]
    ok = True

    for chunk in chunks:
        try:
            r = session.post(
                f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                data={"chat_id": CHAT_ID, "text": chunk,
                      "disable_web_page_preview": "true"},
                timeout=20,
            )
            print("Telegram:", r.status_code)
            if not r.ok:
                print("Telegram response:", r.text[:500])
                ok = False
        except Exception:
            traceback.print_exc()
            ok = False

    return ok


# ============================================================
# HELPERS
# ============================================================

def safe_float(value, default=None):
    try:
        if value is None or value == "":
            return default
        return float(str(value).replace(",", "").replace("%", "").strip())
    except Exception:
        return default


def parse_number(value):
    """Parses numbers like 160K, 3.2%, 1.5M, -0.3."""
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
    if value is None:
        return "unavailable"
    return f"${value:,.2f}"


def fmt_pct(value, decimals=2):
    if value is None:
        return "unavailable"
    return f"{value:+.{decimals}f}%"


def now_utc():
    return datetime.now(UTC)


def utc_text(dt=None):
    dt = dt or now_utc()
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def parse_iso(value):
    if not value:
        return None
    try:
        s = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
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


def http_json(url, params=None, headers=None, timeout=12):
    try:
        r = session.get(url, params=params or {}, headers=headers,
                        timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print("HTTP error:", url, str(e)[:120])
        return None


# ============================================================
# PERSISTENT STATE
# ============================================================

def default_state():
    return {
        "last_report": 0,
        "last_news_hashes": [],
        "event_alerts": {},
        "market_open_alerts": {},
        "last_actual_events": {},
        "oi_history": [],
        "last_signal": "NONE",
        "last_signal_time": 0,
        "started_at": time.time(),
    }


def load_state():
    state = default_state()
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                old = json.load(f)
            state.update(old)
            print("State loaded:", STATE_FILE)
    except Exception:
        traceback.print_exc()
        print("Starting with fresh state.")
    return state


def save_state(state):
    try:
        state["last_news_hashes"] = state["last_news_hashes"][-300:]
        state["oi_history"] = state["oi_history"][-400:]
        for k in ("event_alerts", "market_open_alerts", "last_actual_events"):
            if len(state[k]) > 500:
                state[k] = dict(list(state[k].items())[-300:])
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception:
        traceback.print_exc()


# ============================================================
# PRICE DATA  (Binance -> OKX -> Coinbase fallbacks)
# ============================================================

OKX_BAR = {"5m": "5m", "15m": "15m", "1h": "1H", "4h": "4H"}
KLINE_COLS = ["time", "open", "high", "low", "close", "volume"]


def _klines_binance(interval, limit):
    for base in BINANCE_SPOT_HOSTS:
        data = http_json(
            base + "/api/v3/klines",
            {"symbol": BTC_SYMBOL, "interval": interval, "limit": limit},
        )
        if isinstance(data, list) and data:
            df = pd.DataFrame([row[:6] for row in data], columns=KLINE_COLS)
            return df
    return pd.DataFrame()


def _klines_okx(interval, limit):
    data = http_json(
        OKX + "/api/v5/market/candles",
        {"instId": "BTC-USDT", "bar": OKX_BAR[interval],
         "limit": min(limit, 300)},
    )
    rows = (data or {}).get("data") or []
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([r[:6] for r in rows], columns=KLINE_COLS)
    return df.iloc[::-1].reset_index(drop=True)  # OKX is newest-first


def get_klines(interval="5m", limit=500):
    df = _klines_binance(interval, limit)
    if df.empty:
        df = _klines_okx(interval, limit)
    if df.empty:
        return df

    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["time"] = pd.to_numeric(df["time"], errors="coerce")
    return df.dropna(subset=["close"]).reset_index(drop=True)


def get_btc_ticker():
    # 1) Binance
    for base in BINANCE_SPOT_HOSTS:
        d = http_json(base + "/api/v3/ticker/24hr", {"symbol": BTC_SYMBOL})
        if isinstance(d, dict) and d.get("lastPrice"):
            return {
                "price": safe_float(d.get("lastPrice")),
                "change_24h": safe_float(d.get("priceChangePercent")),
                "high_24h": safe_float(d.get("highPrice")),
                "low_24h": safe_float(d.get("lowPrice")),
            }

    # 2) OKX
    d = http_json(OKX + "/api/v5/market/ticker", {"instId": "BTC-USDT"})
    try:
        t = d["data"][0]
        last = safe_float(t.get("last"))
        open24 = safe_float(t.get("open24h"))
        return {
            "price": last,
            "change_24h": pct_change(last, open24),
            "high_24h": safe_float(t.get("high24h")),
            "low_24h": safe_float(t.get("low24h")),
        }
    except Exception:
        pass

    # 3) Coinbase (price only)
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

    df["ema9"] = df["close"].ewm(span=9, adjust=False).mean()
    df["ema21"] = df["close"].ewm(span=21, adjust=False).mean()
    df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()
    df["ema200"] = df["close"].ewm(span=200, adjust=False).mean()

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

    df["atr"] = tr.ewm(alpha=1 / 14, adjust=False).mean()
    df["atr_pct"] = df["atr"] / df["close"] * 100

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


def build_technical_context():
    result = {}

    for tf, limit in [("5m", 300), ("15m", 300), ("1h", 300), ("4h", 250)]:
        df = add_indicators(get_klines(tf, limit))

        if len(df) < 80:
            # Keep last known good data instead of showing N/A.
            result[tf] = TECH_CACHE.get(tf, {})
            continue

        c = df.iloc[-2]  # last CLOSED candle
        data = {
            "price": float(c["close"]),
            "trend": trend_from_row(c),
            "rsi": safe_float(c["rsi"]),
            "atr": safe_float(c["atr"]),
            "atr_pct": safe_float(c["atr_pct"]),
            "volume_ratio": safe_float(c["volume_ratio"]),
            "ema50": safe_float(c["ema50"]),
            "ema200": safe_float(c["ema200"]),
            "support": safe_float(c["support_50"]),
            "resistance": safe_float(c["resistance_50"]),
        }
        TECH_CACHE[tf] = data
        result[tf] = data

    return result


# ============================================================
# FUTURES CONTEXT  (Binance -> Bybit -> OKX fallbacks)
# ============================================================

def _okx_liquidations():
    """Approximate 1h liquidation USD from OKX public feed."""
    d = http_json(
        OKX + "/api/v5/public/liquidation-orders",
        {"instType": "SWAP", "uly": "BTC-USDT", "state": "filled"},
    )
    long_usd, short_usd = 0.0, 0.0
    try:
        cutoff = int(time.time() * 1000) - 3600 * 1000
        for block in d["data"]:
            for det in block.get("details", []):
                if safe_float(det.get("ts"), 0) < cutoff:
                    continue
                contracts = safe_float(det.get("sz"), 0) or 0
                px = safe_float(det.get("bkPx"), 0) or 0
                usd = contracts * 0.01 * px  # 1 contract = 0.01 BTC
                pos_side = det.get("posSide")
                side = det.get("side")
                if pos_side == "long" or (pos_side in (None, "", "net") and side == "sell"):
                    long_usd += usd
                else:
                    short_usd += usd
        return long_usd, short_usd
    except Exception:
        return None, None


def get_futures_context(state):
    res = {
        "funding": None,
        "open_interest": None,
        "oi_change_pct": None,
        "long_liquidation": None,
        "short_liquidation": None,
    }

    # 1) Binance futures (often blocked on cloud hosts)
    p = http_json(BINANCE_FUTURES + "/fapi/v1/premiumIndex",
                  {"symbol": BTC_SYMBOL})
    if isinstance(p, dict):
        res["funding"] = safe_float(p.get("lastFundingRate"))

    o = http_json(BINANCE_FUTURES + "/fapi/v1/openInterest",
                  {"symbol": BTC_SYMBOL})
    if isinstance(o, dict):
        res["open_interest"] = safe_float(o.get("openInterest"))

    # 2) Bybit fallback
    if res["funding"] is None or res["open_interest"] is None:
        b = http_json(BYBIT + "/v5/market/tickers",
                      {"category": "linear", "symbol": BTC_SYMBOL})
        try:
            item = b["result"]["list"][0]
            if res["funding"] is None:
                res["funding"] = safe_float(item.get("fundingRate"))
            if res["open_interest"] is None:
                res["open_interest"] = safe_float(item.get("openInterest"))
        except Exception:
            pass

    # 3) OKX fallback
    if res["funding"] is None:
        f = http_json(OKX + "/api/v5/public/funding-rate",
                      {"instId": "BTC-USDT-SWAP"})
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

    # Open-interest change: compare with ~30 minutes ago (own history).
    now = time.time()
    if res["open_interest"] is not None:
        hist = state.setdefault("oi_history", [])
        hist.append([now, res["open_interest"]])
        state["oi_history"] = hist = [h for h in hist if now - h[0] < 3 * 3600]

        target = now - 30 * 60
        older = [h for h in hist if h[0] <= now - 5 * 60]
        if older:
            ref = min(older, key=lambda h: abs(h[0] - target))
            res["oi_change_pct"] = pct_change(res["open_interest"], ref[1])
            res["oi_window_min"] = int((now - ref[0]) / 60)

    # Liquidations (OKX public feed)
    long_liq, short_liq = _okx_liquidations()
    res["long_liquidation"] = long_liq
    res["short_liquidation"] = short_liq

    # Fill gaps with last known good values.
    for k, v in res.items():
        if v is None and FUT_CACHE.get(k) is not None and k not in (
            "long_liquidation", "short_liquidation"
        ):
            res[k] = FUT_CACHE[k]
        elif v is not None:
            FUT_CACHE[k] = v

    return res


# ============================================================
# CROSS-MARKET CONTEXT  (Yahoo with cache)
# ============================================================

def yahoo_quote(symbol):
    for host in ("query1", "query2"):
        data = http_json(
            f"https://{host}.finance.yahoo.com/v8/finance/chart/{symbol}",
            {"range": "1d", "interval": "5m"},
            headers=YAHOO_HEADERS,
        )
        try:
            meta = data["chart"]["result"][0]["meta"]
            price = safe_float(meta.get("regularMarketPrice"))
            prev = (safe_float(meta.get("chartPreviousClose"))
                    or safe_float(meta.get("previousClose")))
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
# NEWS
# ============================================================

def parse_rss(url, category):
    items = []
    try:
        r = session.get(url, timeout=15)
        r.raise_for_status()
        root = ET.fromstring(r.content)

        for item in root.findall(".//item")[:20]:
            title = clean_text(item.findtext("title"))
            link = clean_text(item.findtext("link"))
            pub = clean_text(item.findtext("pubDate"))
            desc = clean_text(item.findtext("description"))

            if not title:
                continue

            source = "Unknown"
            if " - " in title:
                title, source = title.rsplit(" - ", 1)

            items.append({
                "title": title, "source": source, "link": link,
                "published": pub, "description": desc[:400],
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

    high_score = sum(1 for x in high_words if x in text)
    bull_score = sum(1 for x in bullish_words if x in text)
    bear_score = sum(1 for x in bearish_words if x in text)

    if bull_score > bear_score:
        direction = "BULLISH"
    elif bear_score > bull_score:
        direction = "BEARISH"
    else:
        direction = "MIXED"

    if high_score >= 2:
        impact = "HIGH"
    elif high_score == 1:
        impact = "MEDIUM"
    else:
        impact = "LOW"

    return impact, direction


def fetch_news():
    all_items = []
    for category, url in NEWS_FEEDS.items():
        all_items.extend(parse_rss(url, category))

    unique = {}
    for item in all_items:
        unique[news_key(item)] = item

    items = list(unique.values())
    for item in items:
        item["impact"], item["direction"] = news_impact(item)

    rank = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    items.sort(key=lambda x: (rank.get(x["impact"], 3), x["title"]))
    return items


# ============================================================
# ECONOMIC CALENDAR  (ForexFactory JSON -> FinanceCalendar fallback)
# ============================================================

CALENDAR_CURRENCIES = ("USD", "US", "GLOBAL", "EUR", "EU", "JPY", "CNY",
                       "CN", "GBP", "ALL", "")


def _none_if_empty(v):
    if v is None:
        return None
    v = str(v).strip()
    return v if v else None


def fetch_calendar_ff():
    events = []
    got_any = False

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
                "id": hashlib.sha1(
                    f"{dt.isoformat()}|{currency}|{name}".encode()
                ).hexdigest()[:16],
                "name": name,
                "time": dt,
                "currency": currency,
                "impact": "high",
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
        r = session.get(
            f"{CALENDAR_API}/calendar",
            params={
                "from": (now - timedelta(days=1)).date().isoformat(),
                "to": (now + timedelta(days=8)).date().isoformat(),
                "impact": "high", "limit": 300,
            },
            timeout=15,
        )
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
            tv = (e.get("time_utc") or e.get("date") or e.get("datetime")
                  or e.get("timestamp"))
            if isinstance(tv, (int, float)):
                ts = float(tv)
                if ts > 10_000_000_000:
                    ts /= 1000
                dt = datetime.fromtimestamp(ts, UTC)
            else:
                dt = parse_iso(tv)
            if dt is None:
                continue

            currency = e.get("currency") or e.get("country") or e.get("country_code") or ""
            events.append({
                "id": str(e.get("id") or hashlib.sha1(
                    f"{dt.isoformat()}|{currency}|{name}".encode()).hexdigest()[:16]),
                "name": clean_text(name),
                "time": dt,
                "currency": clean_text(currency),
                "impact": "high",
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

    events = [e for e in events
              if str(e["currency"]).upper() in CALENDAR_CURRENCIES]
    events.sort(key=lambda x: x["time"])
    return events


def upcoming_high_event(events, within_min):
    """Returns (event, minutes) for a high-impact event close to now."""
    now = now_utc()
    best = None
    for e in events:
        mins = (e["time"] - now).total_seconds() / 60
        if -10 <= mins <= within_min:
            if best is None or mins < best[1]:
                best = (e, mins)
    return best


# ============================================================
# MACRO EVENT INTERPRETATION
# ============================================================

def numeric_surprise(actual, forecast):
    a = parse_number(actual)
    f = parse_number(forecast)
    if a is None or f is None:
        return None
    return a - f


def event_direction(event):
    name = event["name"].lower()
    surprise = numeric_surprise(event.get("actual"), event.get("forecast"))

    if surprise is None:
        return "WAIT", "Result not out yet. Wait for the number, then watch how yields and the dollar react."

    if any(k in name for k in ["cpi", "pce", "ppi", "inflation",
                               "consumer price", "producer price",
                               "price index"]):
        if surprise > 0:
            return "BEARISH", "Inflation hotter than expected -> rate/yield pressure -> usually bad for BTC."
        if surprise < 0:
            return "BULLISH", "Inflation cooler than expected -> less rate pressure -> usually good for BTC."
        return "NEUTRAL", "Inflation matched expectations."

    if any(k in name for k in ["fomc", "fed rate", "interest rate decision",
                               "fed decision"]):
        return "MIXED", "Judge the Fed together with the press conference, yields and the dollar."

    if any(k in name for k in ["nonfarm", "payroll", "unemployment", "jobless",
                               "employment", "job openings", "jolts",
                               "average hourly earnings", "wage"]):
        return "MIXED", "Jobs data can cut both ways: strong = growth but higher rates, weak = lower rates but recession fear."

    if any(k in name for k in ["gdp", "retail sales", "pmi", "ism"]):
        return "MIXED", "Growth data: watch the 10Y yield reaction before trusting a direction."

    return "MIXED", "Direction depends on yields, the dollar and risk sentiment."


# ============================================================
# MARKET OPEN CALENDAR
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
            date = (now + timedelta(days=offset)).date()
            open_dt = market_open_utc(name, date)

            if open_dt.weekday() >= 5:
                continue
            # include a market that opened in the last 5 minutes
            if open_dt > now - timedelta(minutes=5):
                result.append((name, open_dt))
                break

    result.sort(key=lambda x: x[1])
    return result


# ============================================================
# PLAIN-ENGLISH TRANSLATORS
# ============================================================

def trend_words(t):
    return {
        "BULLISH": "UP 📈 (strong)",
        "WEAK BULLISH": "Slightly up 📈",
        "BEARISH": "DOWN 📉 (strong)",
        "WEAK BEARISH": "Slightly down 📉",
        "NEUTRAL": "Sideways ➡️",
    }.get(t, "Loading...")


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
    oi = futures.get("open_interest")
    ch = futures.get("oi_change_pct")
    if oi is None:
        return "Open interest: could not be fetched from any exchange this cycle"
    base = f"Open interest: {oi:,.0f} BTC"
    if ch is None:
        return base + " (change shows after ~5 min of history)"
    win = futures.get("oi_window_min", 30)
    if ch > 2:
        meaning = "lots of NEW money entering -> bigger move likely"
    elif ch < -2:
        meaning = "traders closing positions -> move may be fading"
    else:
        meaning = "no big change"
    return f"{base}, {ch:+.2f}% in {win}m -> {meaning}"


def liq_words(futures):
    lng = futures.get("long_liquidation")
    sht = futures.get("short_liquidation")
    if lng is None or sht is None:
        return "Liquidations (1h): exchange feed unavailable this cycle"
    if lng + sht < 1:
        return "Liquidations (1h): quiet (nothing significant reported)"
    side = "longs got wiped out (bearish pressure)" if lng > sht else "shorts got wiped out (bullish pressure)"
    return f"Liquidations (1h): Longs ${lng:,.0f} | Shorts ${sht:,.0f} -> {side}"


def cross_line(name, a, good_if_up, label):
    if not a or a.get("price") is None:
        return f"{label}: temporarily unavailable"
    ch = a.get("change")
    price_txt = f"{a['price']:,.2f}"
    if ch is None:
        return f"{label}: {price_txt}"
    if good_if_up is None:
        meaning = "safe-haven demand rising" if ch > 0.4 else "safe-haven demand falling" if ch < -0.4 else "flat"
    else:
        thr = 0.15 if name == "DXY" else 0.3
        if abs(ch) < thr:
            meaning = "flat - no pressure on BTC"
        else:
            up = ch > 0
            helps = (up and good_if_up) or (not up and not good_if_up)
            meaning = "good for BTC ✅" if helps else "bad for BTC ❌"
    return f"{label}: {price_txt} ({ch:+.2f}%) -> {meaning}"


# ============================================================
# BIAS ENGINE
# ============================================================

def calculate_bias(tech, futures, cross, news):
    score = 0.0
    reasons = []

    h1 = tech.get("1h") or {}
    h4 = tech.get("4h") or {}
    m15 = tech.get("15m") or {}

    if h4.get("trend") == "BULLISH":
        score += 2
        reasons.append("4-hour chart trend is UP")
    elif h4.get("trend") == "BEARISH":
        score -= 2
        reasons.append("4-hour chart trend is DOWN")
    elif h4.get("trend") == "WEAK BULLISH":
        score += 0.75
        reasons.append("4-hour chart leans up")
    elif h4.get("trend") == "WEAK BEARISH":
        score -= 0.75
        reasons.append("4-hour chart leans down")

    if h1.get("trend") == "BULLISH":
        score += 1.5
        reasons.append("1-hour chart trend is UP")
    elif h1.get("trend") == "BEARISH":
        score -= 1.5
        reasons.append("1-hour chart trend is DOWN")
    elif h1.get("trend") == "WEAK BULLISH":
        score += 0.5
    elif h1.get("trend") == "WEAK BEARISH":
        score -= 0.5

    if m15.get("trend") == "BULLISH":
        score += 0.5
    elif m15.get("trend") == "BEARISH":
        score -= 0.5

    rsi = h1.get("rsi")
    if rsi is not None:
        if 50 <= rsi <= 68:
            score += 0.5
            reasons.append(f"Momentum supportive (RSI {rsi:.0f})")
        elif rsi > 75:
            score -= 0.5
            reasons.append(f"Price overheated (RSI {rsi:.0f})")
        elif rsi < 30:
            score += 0.5
            reasons.append(f"Price oversold, bounce possible (RSI {rsi:.0f})")

    funding = futures.get("funding")
    if funding is not None:
        fp = funding * 100
        if fp > 0.05:
            score -= 0.75
            reasons.append("Too many traders are long (funding high)")
        elif fp < -0.05:
            score += 0.75
            reasons.append("Too many traders are short (funding negative)")

    oi_change = futures.get("oi_change_pct")
    if oi_change is not None:
        if oi_change > 2:
            reasons.append("New money entering futures (OI up)")
        elif oi_change < -2:
            reasons.append("Traders closing positions (OI down)")

    dxy = cross.get("DXY") or {}
    nasdaq = cross.get("NASDAQ") or {}
    us10y = cross.get("US10Y") or {}
    vix = cross.get("VIX") or {}

    if dxy.get("change") is not None:
        if dxy["change"] > 0.4:
            score -= 0.75
            reasons.append("US Dollar getting stronger (bad for BTC)")
        elif dxy["change"] < -0.4:
            score += 0.75
            reasons.append("US Dollar getting weaker (good for BTC)")

    if nasdaq.get("change") is not None:
        if nasdaq["change"] > 0.5:
            score += 0.5
            reasons.append("Stocks (Nasdaq) are rising - risk-on")
        elif nasdaq["change"] < -0.5:
            score -= 0.5
            reasons.append("Stocks (Nasdaq) are falling - risk-off")

    if us10y.get("change") is not None:
        if us10y["change"] > 1.0:
            score -= 0.5
            reasons.append("Bond yields rising (bad for BTC)")
        elif us10y["change"] < -1.0:
            score += 0.5
            reasons.append("Bond yields falling (good for BTC)")

    if vix.get("change") is not None:
        if vix["change"] > 5:
            score -= 0.75
            reasons.append("Fear index (VIX) jumping")
        elif vix["change"] < -5:
            score += 0.5
            reasons.append("Fear index (VIX) cooling")

    bull_news = sum(1 for n in news[:15] if n["direction"] == "BULLISH" and n["impact"] != "LOW")
    bear_news = sum(1 for n in news[:15] if n["direction"] == "BEARISH" and n["impact"] != "LOW")

    if bull_news > bear_news:
        score += min(1.0, 0.25 * (bull_news - bear_news))
        reasons.append("Recent news leans positive")
    elif bear_news > bull_news:
        score -= min(1.0, 0.25 * (bear_news - bull_news))
        reasons.append("Recent news leans negative")

    if score >= 2.0:
        bias = "BULLISH"
    elif score <= -2.0:
        bias = "BEARISH"
    else:
        bias = "NEUTRAL / MIXED"

    return {
        "score": score,
        "bias": bias,
        "confidence": int(clamp(50 + abs(score) * 10, 50, 90)),
        "reasons": reasons[:8],
    }


def scenario_levels(tech, live_price=None):
    h1 = tech.get("1h") or {}
    h4 = tech.get("4h") or {}

    price = live_price or h1.get("price") or h4.get("price")
    if price is None:
        return {}

    atr = h1.get("atr") or price * 0.01
    support = h1.get("support")
    resistance = h1.get("resistance")

    return {
        "price": price,
        "atr": atr,
        "atr_pct": atr / price * 100,
        "support": support,
        "resistance": resistance,
        "bull1": price + atr,
        "bull2": price + 2 * atr,
        "bear1": price - atr,
        "bear2": price - 2 * atr,
    }


# ============================================================
# TRADE PLAN  (the part that tells the user what to do)
# ============================================================

def build_trade_plan(bias, tech, levels, events):
    plan = {"action": "WAIT", "why": [], "strength": ""}

    if not levels:
        plan["why"] = ["Not enough price data yet."]
        return plan

    price = levels["price"]
    atr = levels["atr"]
    h1 = tech.get("1h") or {}
    m15 = tech.get("15m") or {}
    score = bias["score"]
    t1 = h1.get("trend", "NEUTRAL")
    t15 = m15.get("trend", "NEUTRAL")
    rsi1 = h1.get("rsi")

    action = "WAIT"
    why = []

    soon = upcoming_high_event(events, NO_TRADE_BEFORE_EVENT_MIN)

    if soon:
        e, mins = soon
        when = f"in {int(mins)} min" if mins > 0 else "just released"
        why.append(
            f"Big news ({e['name']}) is {when}. Price can spike both ways - "
            "stay out until the move settles."
        )
    elif score >= 2 and t1 in BULL_TRENDS and t15 != "BEARISH":
        if rsi1 is not None and rsi1 > 75:
            why.append("Uptrend, but price is overheated. Buying now = chasing. Wait for a small dip.")
        else:
            action = "LONG"
    elif score <= -2 and t1 in BEAR_TRENDS and t15 != "BULLISH":
        if rsi1 is not None and rsi1 < 25:
            why.append("Downtrend, but price is oversold. Selling now = chasing. Wait for a small bounce.")
        else:
            action = "SHORT"
    else:
        if abs(score) < 2:
            why.append(f"Signals are mixed (score {score:+.1f}). No clear edge - the best trade is often no trade.")
        elif score >= 2:
            why.append("Data leans bullish, but the 1h/15m chart does not confirm yet.")
        else:
            why.append("Data leans bearish, but the 1h/15m chart does not confirm yet.")

    plan["action"] = action
    plan["strength"] = "STRONG" if abs(score) >= 3.5 else "MODERATE"

    support = levels.get("support")
    resistance = levels.get("resistance")

    if action in ("LONG", "SHORT"):
        risk = 1.2 * atr

        if action == "LONG":
            if support and 0.6 * atr <= price - support <= 2.5 * atr:
                risk = price - (support - 0.2 * atr)
            stop = price - risk
            tp1 = price + 1.5 * risk
            tp2 = price + 2.5 * risk
        else:
            if resistance and 0.6 * atr <= resistance - price <= 2.5 * atr:
                risk = (resistance + 0.2 * atr) - price
            stop = price + risk
            tp1 = price - 1.5 * risk
            tp2 = price - 2.5 * risk

        risk_usd = ACCOUNT_EXAMPLE * RISK_PCT / 100
        size_btc = risk_usd / risk if risk > 0 else 0

        plan.update({
            "entry": price, "stop": stop, "tp1": tp1, "tp2": tp2,
            "risk": risk,
            "risk_pct": risk / price * 100,
            "rr1": 1.5, "rr2": 2.5,
            "risk_usd": risk_usd,
            "size_btc": size_btc,
            "notional": size_btc * price,
        })
    else:
        up = resistance if resistance and price < resistance <= price + 3 * atr else price + atr
        dn = support if support and price - 3 * atr <= support < price else price - atr
        plan["trigger_long"] = up
        plan["trigger_short"] = dn

    plan["why"] = why
    return plan


def plan_text_lines(plan, bias):
    lines = []

    if plan["action"] == "LONG":
        lines += [
            f"✅ LONG (BUY) setup - {plan['strength']} - confidence {bias['confidence']}%",
            f"Entry:        ~{fmt_price(plan['entry'])}",
            f"Stop-loss:    {fmt_price(plan['stop'])}  (-{plan['risk_pct']:.2f}%)  <- exit here if wrong",
            f"Take-profit 1: {fmt_price(plan['tp1'])}  (+{plan['risk_pct'] * plan['rr1']:.2f}%)  <- take half off",
            f"Take-profit 2: {fmt_price(plan['tp2'])}  (+{plan['risk_pct'] * plan['rr2']:.2f}%)  <- take the rest",
            f"Reward:Risk = 1:{plan['rr1']:.1f} to 1:{plan['rr2']:.1f}",
        ]
    elif plan["action"] == "SHORT":
        lines += [
            f"✅ SHORT (SELL) setup - {plan['strength']} - confidence {bias['confidence']}%",
            f"Entry:        ~{fmt_price(plan['entry'])}",
            f"Stop-loss:    {fmt_price(plan['stop'])}  (+{plan['risk_pct']:.2f}%)  <- exit here if wrong",
            f"Take-profit 1: {fmt_price(plan['tp1'])}  (-{plan['risk_pct'] * plan['rr1']:.2f}%)  <- take half off",
            f"Take-profit 2: {fmt_price(plan['tp2'])}  (-{plan['risk_pct'] * plan['rr2']:.2f}%)  <- take the rest",
            f"Reward:Risk = 1:{plan['rr1']:.1f} to 1:{plan['rr2']:.1f}",
        ]
    else:
        lines.append("⏸ WAIT - no good trade right now")

    for w in plan.get("why", []):
        lines.append(f"Why: {w}")

    if plan["action"] in ("LONG", "SHORT"):
        lines += [
            "",
            f"💰 Position size (risk only {RISK_PCT:.0f}% of your account):",
            f"Example: ${ACCOUNT_EXAMPLE:,.0f} account -> risk ${plan['risk_usd']:,.0f} "
            f"-> size ~{plan['size_btc']:.4f} BTC (~${plan['notional']:,.0f} position)",
            "Formula: (account x 1%) / (distance to stop-loss in $)",
            "If price hits the stop-loss, close the trade. No exceptions.",
        ]
    else:
        lines += [
            "",
            f"👀 Watch: LONG only if BTC holds above {fmt_price(plan.get('trigger_long'))}",
            f"          SHORT only if BTC drops below {fmt_price(plan.get('trigger_short'))}",
        ]

    return lines


# ============================================================
# ALERTS
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

    lines = [
        title,
        "━━━━━━━━━━━━━━━━━━━━",
        f"Event: {event['name']} ({event['currency'] or 'Global'})",
        f"Time: {utc_text(event['time'])} - {timing}",
    ]
    if event.get("forecast") is not None:
        lines.append(f"Expected: {event['forecast']}")
    if event.get("previous") is not None:
        lines.append(f"Previous: {event['previous']}")
    if event.get("actual") is not None:
        lines.append(f"Actual: {event['actual']}")

    lines += [
        "",
        f"BTC reading: {direction}",
        explanation,
        "",
        todo,
        "⚠️ The first reaction can reverse quickly.",
    ]
    return "\n".join(lines)


def check_event_alerts(state, events):
    now = now_utc()

    for event in events:
        diff_min = (event["time"] - now).total_seconds() / 60

        for window in EVENT_ALERT_WINDOWS:
            # fire once when we enter the last `window` minutes
            if window - 3 < diff_min <= window:
                key = f"{event['id']}:T-{window}"
                if not state["event_alerts"].get(key):
                    send(event_alert_message(event, window))
                    state["event_alerts"][key] = time.time()

        if -3 <= diff_min <= 1 and event.get("actual") is not None:
            key = f"{event['id']}:NOW"
            if not state["event_alerts"].get(key):
                send(event_alert_message(event, 0))
                state["event_alerts"][key] = time.time()


def check_actual_event_changes(state, events):
    for event in events:
        if event.get("actual") is None:
            continue

        key = str(event["id"])
        actual = str(event.get("actual"))
        if state["last_actual_events"].get(key) == actual:
            continue

        age_min = (now_utc() - event["time"]).total_seconds() / 60
        if -5 <= age_min <= 30:
            send(event_alert_message(event, 0))
            state["last_actual_events"][key] = actual


def check_market_open_alerts(state):
    now = now_utc()

    for name, open_dt in next_market_opens():
        diff_min = (open_dt - now).total_seconds() / 60

        for window in MARKET_OPEN_WINDOWS:
            if window - 3 < diff_min <= window:
                key = f"{name}:{open_dt.date()}:{window}"
                if state["market_open_alerts"].get(key):
                    continue

                timing = (f"{name} stock market is OPEN now."
                          if window == 0
                          else f"{name} stock market opens in ~{window} minutes.")

                if name == "New York":
                    extra = ("\n\n🔥 The US open is usually the most volatile time for BTC."
                             "\nExpect faster moves. Use stop-losses.")
                else:
                    extra = ("\n\nBTC trades 24/7, but this session can change "
                             "liquidity and volatility.")

                send(
                    f"🌍 MARKET SESSION\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"{timing}"
                    f"{extra}\n\n"
                    f"This is a volatility heads-up, not a direction signal."
                )
                state["market_open_alerts"][key] = time.time()


def check_trade_signal(state, plan, bias):
    """Instant alert when a LONG/SHORT setup appears or disappears."""
    action = plan["action"]
    last = state.get("last_signal", "NONE")
    now = time.time()

    if action in ("LONG", "SHORT"):
        stale = now - state.get("last_signal_time", 0) > 2 * 3600
        if action != last or stale:
            lines = ["🚨 TRADE ALERT", "━━━━━━━━━━━━━━━━━━━━"]
            lines += plan_text_lines(plan, bias)
            lines += ["", "Information only - the bot never places trades."]
            send("\n".join(lines))
            state["last_signal"] = action
            state["last_signal_time"] = now

    elif last in ("LONG", "SHORT"):
        send(
            f"ℹ️ The previous {last} setup is no longer valid.\n"
            "Conditions changed. If you are in a trade, follow your "
            "stop-loss; do not add to it."
        )
        state["last_signal"] = "WAIT"
        state["last_signal_time"] = now
    else:
        state["last_signal"] = "WAIT"


# ============================================================
# 30-MINUTE REPORT
# ============================================================

def format_news(news):
    if not news:
        return ["News feed is empty right now (will retry)."]

    lines = []
    for item in news[:MAX_NEWS_ITEMS]:
        icon = {"BULLISH": "🟢", "BEARISH": "🔴", "MIXED": "🟡"}.get(item["direction"], "⚪")
        lines.append(f"{icon} {item['title']}\n   {item['source']} | impact: {item['impact']}")
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
        if mins < 60:
            when = f"in {max(0, mins)} min"
        elif mins < 24 * 60:
            when = f"in {mins // 60}h {mins % 60}m"
        else:
            when = e["time"].strftime("%a %d %b %H:%M UTC")

        extra = ""
        if e.get("forecast") is not None:
            extra += f" | expected {e['forecast']}"
        if e.get("previous") is not None:
            extra += f" | previous {e['previous']}"
        lines.append(f"• {e['name']} ({e['currency'] or 'Global'}) - {when}{extra}")
    return lines


def send_market_report(ticker, tech, futures, cross, news, events, bias, levels, plan):
    h1 = tech.get("1h") or {}
    h4 = tech.get("4h") or {}
    m15 = tech.get("15m") or {}
    m5 = tech.get("5m") or {}

    price = ticker.get("price") or (levels or {}).get("price")

    lines = [
        "₿ BTC UPDATE",
        f"{utc_text()}",
        f"Price: {fmt_price(price)}  |  24h: {fmt_pct(ticker.get('change_24h'))}",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        "🎯 WHAT TO DO NOW",
    ]
    lines += plan_text_lines(plan, bias)

    lines += [
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "📖 IN SIMPLE WORDS",
        f"Overall mood: {bias['bias']} (score {bias['score']:+.1f})",
        "",
        "Trend (which way is price heading?)",
        f"• Next few minutes (5m): {trend_words(m5.get('trend'))}",
        f"• Next few hours (15m): {trend_words(m15.get('trend'))}",
        f"• Today (1h): {trend_words(h1.get('trend'))}",
        f"• Big picture (4h): {trend_words(h4.get('trend'))}",
        "",
        f"Momentum (1h): {momentum_words(h1.get('rsi'))}",
    ]

    if levels:
        lines += [
            "",
            "Key price levels",
            f"• Support (floor): {fmt_price(levels.get('support'))}",
            f"• Resistance (ceiling): {fmt_price(levels.get('resistance'))}",
            f"• Normal 1h swing: about ±{fmt_price(levels['atr'])} (±{levels['atr_pct']:.2f}%)",
            f"• If it breaks UP: {fmt_price(levels['bull1'])} then {fmt_price(levels['bull2'])}",
            f"• If it breaks DOWN: {fmt_price(levels['bear1'])} then {fmt_price(levels['bear2'])}",
        ]

    lines += [
        "",
        "Futures traders (what the crowd is doing)",
        f"• {funding_words(futures.get('funding'))}",
        f"• {oi_words(futures)}",
        f"• {liq_words(futures)}",
        "",
        "Outside markets",
        f"• {cross_line('DXY', cross.get('DXY'), False, 'US Dollar (DXY)')}",
        f"• {cross_line('NASDAQ', cross.get('NASDAQ'), True, 'Nasdaq futures')}",
        f"• {cross_line('US10Y', cross.get('US10Y'), False, 'US 10Y yield')}",
        f"• {cross_line('VIX', cross.get('VIX'), False, 'Fear index (VIX)')}",
        f"• {cross_line('GOLD', cross.get('GOLD'), None, 'Gold')}",
    ]

    lines += ["", "🧩 WHY THIS MOOD"]
    if bias["reasons"]:
        lines += [f"• {x}" for x in bias["reasons"]]
    else:
        lines.append("• Nothing strong enough to lean either way.")

    lines += ["", "⏰ BIG EVENTS - NEXT 7 DAYS"]
    lines += upcoming_events_text(events)

    lines += ["", f"📰 NEWS ({news_mood(news)})"]
    lines += format_news(news)

    lines += [
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "⚠️ Read this carefully",
        "• These are probabilities, not promises. Any trade can lose.",
        "• Always use the stop-loss and risk only ~1% per trade.",
        "• A sudden headline, Fed comment or liquidation wave can override everything.",
        "• Information only - this bot NEVER places orders.",
    ]

    send("\n".join(lines))


# ============================================================
# STARTUP MESSAGE
# ============================================================

def send_startup():
    send(
        "₿ BTC MARKET BOT STARTED\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Every 30 minutes you get:\n"
        "• A clear action: LONG / SHORT / WAIT\n"
        "• Entry, stop-loss, take-profit and position size\n"
        "• A plain-English explanation of why\n\n"
        "You also get instant alerts for:\n"
        "• New trade setups\n"
        "• Big economic news (CPI, Fed, jobs...)\n"
        "• Tokyo / Hong Kong / London / New York market opens\n\n"
        "⚠️ No guaranteed predictions. The bot never places trades."
    )


# ============================================================
# MAIN
# ============================================================

def main():
    state = load_state()
    send_startup()

    start = time.time()
    t_fast = t_slow = t_cal = 0

    tech, futures, cross, news, events, ticker = {}, {}, {}, [], [], {}

    while time.time() - start < RUN_SECONDS:
        try:
            now = time.time()

            if now - t_fast >= DATA_POLL_INTERVAL:
                print("Updating price / technical / futures data...")
                tech = build_technical_context()
                futures = get_futures_context(state)
                ticker = get_btc_ticker()
                t_fast = now

            if now - t_slow >= SLOW_POLL_INTERVAL:
                print("Updating cross-market and news...")
                cross = get_cross_market()
                news = fetch_news()
                t_slow = now

            if now - t_cal >= CALENDAR_POLL_INTERVAL:
                print("Updating economic calendar...")
                new_events = fetch_calendar()
                if new_events:
                    events = new_events
                t_cal = now

            # Time-based alerts are checked every loop using cached data.
            check_event_alerts(state, events)
            check_actual_event_changes(state, events)
            check_market_open_alerts(state)

            if tech:
                live_price = ticker.get("price")
                bias = calculate_bias(tech, futures, cross, news)
                levels = scenario_levels(tech, live_price)
                plan = build_trade_plan(bias, tech, levels, events)

                check_trade_signal(state, plan, bias)

                due = (state["last_report"] == 0
                       or now - state["last_report"] >= REPORT_INTERVAL)
                if due:
                    send_market_report(ticker, tech, futures, cross, news,
                                       events, bias, levels, plan)
                    state["last_report"] = now

                print(datetime.now().strftime("%H:%M:%S"),
                      "| BTC:", live_price,
                      "| Bias:", bias["bias"],
                      "| Action:", plan["action"],
                      "| Events:", len(events))

            save_state(state)

        except Exception:
            traceback.print_exc()

        time.sleep(DATA_POLL_INTERVAL)

    save_state(state)
    send(
        "🛑 BTC MARKET BOT SESSION FINISHED\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"Runtime: {RUN_SECONDS // 60} minutes\n"
        "State saved. Signals only - no orders were placed."
    )


if __name__ == "__main__":
    main()
