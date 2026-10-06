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
# BTC MARKET INTELLIGENCE TELEGRAM BOT
# ------------------------------------------------------------
# - BTC only: no SOL / ETH strategies
# - 30-minute BTC market report
# - Breaking/relevant crypto + macro news
# - Economic calendar + important-event countdown
# - Actual vs forecast surprise analysis when available
# - US / London / Tokyo / Hong Kong market-open warnings
# - BTC technical trend + volatility + support/resistance
# - Binance futures funding, OI and liquidation pressure
# - DXY / Nasdaq / US10Y / VIX context when Yahoo is available
# - Bull / Base / Bear scenarios (NOT guaranteed predictions)
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
DATA_POLL_INTERVAL = 60
RUN_SECONDS = int(os.environ.get("RUN_SECONDS", str(350 * 60)))

# Send a separate alert when a high-impact event is approaching.
EVENT_LOOKAHEAD_MIN = 90
EVENT_ALERT_WINDOWS = (60, 15, 5)

# Market open alerts: 30 min before + at open.
MARKET_OPEN_WINDOWS = (30, 5, 0)

# How many news items are shown in each 30-minute report.
MAX_NEWS_ITEMS = 6

# Binance public API
BINANCE_SPOT = "https://api.binance.com"
BINANCE_FUTURES = "https://fapi.binance.com"

# Free economic calendar. No API key required.
# Source asks users to link back to financecalendar.com.
CALENDAR_API = "https://www.financecalendar.com/wp-json/fc/v1"

# Google News RSS is used as a free news aggregator.
NEWS_FEEDS = {
    "Bitcoin": "https://news.google.com/rss/search?q=Bitcoin%20when%3A2h&hl=en-US&gl=US&ceid=US%3Aen",
    "Crypto": "https://news.google.com/rss/search?q=crypto%20Bitcoin%20when%3A2h&hl=en-US&gl=US&ceid=US%3Aen",
    "Macro": "https://news.google.com/rss/search?q=Federal%20Reserve%20CPI%20jobs%20Treasury%20Bitcoin%20when%3A6h&hl=en-US&gl=US&ceid=US%3Aen",
}

# Yahoo public chart endpoint for cross-market context.
YAHOO_SYMBOLS = {
    "DXY": "DX-Y.NYB",
    "NASDAQ": "NQ=F",
    "US10Y": "^TNX",
    "VIX": "^VIX",
    "GOLD": "GC=F",
}

NY_TZ = ZoneInfo("America/New_York")
UTC = timezone.utc

session = requests.Session()
session.headers.update({
    "User-Agent": "BTC-Market-Intelligence-Bot/1.0",
    "Accept": "*/*",
})


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
                data={"chat_id": CHAT_ID, "text": chunk},
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


def fmt_price(value):
    if value is None:
        return "N/A"
    return f"${value:,.2f}"


def fmt_pct(value, decimals=2):
    if value is None:
        return "N/A"
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


# ============================================================
# PERSISTENT STATE
# ============================================================

def default_state():
    return {
        "last_report": 0,
        "last_calendar_fetch": 0,
        "last_news_hashes": [],
        "event_alerts": {},
        "market_open_alerts": {},
        "last_actual_events": {},
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
        if len(state["event_alerts"]) > 500:
            state["event_alerts"] = dict(
                list(state["event_alerts"].items())[-300:]
            )
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception:
        traceback.print_exc()


# ============================================================
# BINANCE MARKET DATA
# ============================================================

def binance_get(path, params=None, futures=False, timeout=12):
    base = BINANCE_FUTURES if futures else BINANCE_SPOT
    try:
        r = session.get(base + path, params=params or {}, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print("Binance error:", path, e)
        return None


def get_klines(interval="5m", limit=500):
    data = binance_get(
        "/api/v3/klines",
        {"symbol": BTC_SYMBOL, "interval": interval, "limit": limit},
    )
    if not isinstance(data, list):
        return pd.DataFrame()

    df = pd.DataFrame(data, columns=[
        "time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore"
    ])

    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    df["time"] = pd.to_numeric(df["time"], errors="coerce").astype("int64")
    return df


def get_btc_ticker():
    data = binance_get(
        "/api/v3/ticker/24hr",
        {"symbol": BTC_SYMBOL},
    )
    if not isinstance(data, dict):
        return {}
    return {
        "price": safe_float(data.get("lastPrice")),
        "change_24h": safe_float(data.get("priceChangePercent")),
        "high_24h": safe_float(data.get("highPrice")),
        "low_24h": safe_float(data.get("lowPrice")),
        "volume": safe_float(data.get("quoteVolume")),
    }


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

    # Rolling levels: useful for scenario targets.
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

    for tf, limit in [("5m", 500), ("15m", 400), ("1h", 300), ("4h", 250)]:
        df = add_indicators(get_klines(tf, limit))
        if len(df) < 80:
            result[tf] = {}
            continue

        c = df.iloc[-2]  # last closed candle
        price = float(c["close"])

        result[tf] = {
            "price": price,
            "trend": trend_from_row(c),
            "rsi": safe_float(c["rsi"]),
            "atr": safe_float(c["atr"]),
            "atr_pct": safe_float(c["atr_pct"]),
            "volume_ratio": safe_float(c["volume_ratio"]),
            "ema9": safe_float(c["ema9"]),
            "ema21": safe_float(c["ema21"]),
            "ema50": safe_float(c["ema50"]),
            "ema200": safe_float(c["ema200"]),
            "support": safe_float(c["support_50"]),
            "resistance": safe_float(c["resistance_50"]),
        }

    return result


# ============================================================
# BINANCE FUTURES CONTEXT
# ============================================================

def get_futures_context():
    result = {
        "funding": None,
        "open_interest": None,
        "oi_change_pct": None,
        "long_liquidation": 0.0,
        "short_liquidation": 0.0,
    }

    premium = binance_get(
        "/fapi/v1/premiumIndex",
        {"symbol": BTC_SYMBOL},
        futures=True,
    )
    if isinstance(premium, dict):
        result["funding"] = safe_float(premium.get("lastFundingRate"))

    oi = binance_get(
        "/fapi/v1/openInterest",
        {"symbol": BTC_SYMBOL},
        futures=True,
    )
    if isinstance(oi, dict):
        result["open_interest"] = safe_float(oi.get("openInterest"))

    hist = binance_get(
        "/futures/data/openInterestHist",
        {"symbol": BTC_SYMBOL, "period": "5m", "limit": 2},
        futures=True,
    )
    if isinstance(hist, list) and len(hist) >= 2:
        old = safe_float(hist[-2].get("sumOpenInterest"))
        new = safe_float(hist[-1].get("sumOpenInterest"))
        result["oi_change_pct"] = pct_change(new, old)

    orders = binance_get(
        "/fapi/v1/allForceOrders",
        {"symbol": BTC_SYMBOL, "limit": 100},
        futures=True,
    )

    if isinstance(orders, list):
        cutoff = int(time.time() * 1000) - 3600 * 1000

        for order in orders:
            if safe_float(order.get("time"), 0) < cutoff:
                continue

            qty = safe_float(order.get("origQty"), 0) or 0
            price = safe_float(order.get("price"), 0) or 0
            value = qty * price

            # Forced SELL generally represents a liquidated long.
            # Forced BUY generally represents a liquidated short.
            if order.get("side") == "SELL":
                result["long_liquidation"] += value
            elif order.get("side") == "BUY":
                result["short_liquidation"] += value

    return result


# ============================================================
# CROSS-MARKET CONTEXT
# ============================================================

def yahoo_quote(symbol):
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        r = session.get(
            url,
            params={"range": "1d", "interval": "5m"},
            timeout=12,
        )
        r.raise_for_status()
        data = r.json()["chart"]["result"][0]

        meta = data.get("meta", {})
        price = safe_float(meta.get("regularMarketPrice"))

        closes = []
        timestamps = data.get("timestamp", [])
        quote = (data.get("indicators", {}).get("quote") or [{}])[0]
        values = quote.get("close", [])

        for ts, value in zip(timestamps, values):
            if value is not None:
                closes.append((ts, float(value)))

        change = None
        if len(closes) >= 2:
            change = pct_change(closes[-1][1], closes[0][1])

        return {"price": price, "change": change}

    except Exception as e:
        print("Yahoo error", symbol, e)
        return {}


def get_cross_market():
    result = {}
    for name, symbol in YAHOO_SYMBOLS.items():
        result[name] = yahoo_quote(symbol)
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

            # Google News titles often contain " - Source".
            source = "Unknown"
            if " - " in title:
                title, source = title.rsplit(" - ", 1)

            items.append({
                "title": title,
                "source": source,
                "link": link,
                "published": pub,
                "description": desc[:400],
                "category": category,
            })

    except Exception as e:
        print("RSS error:", category, e)

    return items


def news_key(item):
    raw = item.get("link") or item.get("title", "")
    return hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()


def news_impact(item):
    text = (
        f"{item.get('title', '')} {item.get('description', '')}"
    ).lower()

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

    # Deduplicate.
    unique = {}
    for item in all_items:
        unique[news_key(item)] = item

    items = list(unique.values())

    for item in items:
        impact, direction = news_impact(item)
        item["impact"] = impact
        item["direction"] = direction

    # High impact first, then medium, then low.
    rank = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    items.sort(key=lambda x: (rank.get(x["impact"], 3), x["title"]))

    return items


# ============================================================
# ECONOMIC CALENDAR
# ============================================================

def fetch_calendar():
    """
    Uses Finance Calendar's free API.

    The API provides scheduled releases, central-bank decisions and
    market holidays. We request a short rolling window around now.
    """
    now = now_utc()
    start = (now - timedelta(days=1)).date().isoformat()
    end = (now + timedelta(days=8)).date().isoformat()

    try:
        r = session.get(
            f"{CALENDAR_API}/calendar",
            params={
                "from": start,
                "to": end,
                "impact": "high",
                "limit": 300,
            },
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()

        if isinstance(data, list):
            raw_events = data
        elif isinstance(data, dict):
            raw_events = (
                data.get("events")
                or data.get("data")
                or data.get("results")
                or []
            )
            if isinstance(raw_events, dict):
                raw_events = (
                    raw_events.get("events")
                    or raw_events.get("data")
                    or []
                )
        else:
            raw_events = []

        events = []

        for e in raw_events:
            if not isinstance(e, dict):
                continue

            name = (
                e.get("name")
                or e.get("title")
                or e.get("event")
                or e.get("indicator")
                or "Economic event"
            )

            time_value = (
                e.get("time_utc")
                or e.get("date")
                or e.get("datetime")
                or e.get("timestamp")
            )

            dt = None

            if isinstance(time_value, (int, float)):
                # Handle seconds or milliseconds.
                ts = float(time_value)
                if ts > 10_000_000_000:
                    ts /= 1000
                dt = datetime.fromtimestamp(ts, UTC)
            else:
                dt = parse_iso(time_value)

            if dt is None:
                continue

            currency = (
                e.get("currency")
                or e.get("country")
                or e.get("country_code")
                or ""
            )

            impact = str(e.get("impact") or "high").lower()

            if impact not in ("high", "medium", "low"):
                impact = "high"

            events.append({
                "id": str(
                    e.get("id")
                    or hashlib.sha1(
                        f"{dt.isoformat()}|{currency}|{name}".encode()
                    ).hexdigest()[:16]
                ),
                "name": clean_text(name),
                "time": dt,
                "currency": clean_text(currency),
                "impact": impact,
                "forecast": e.get("consensus", e.get("forecast")),
                "previous": e.get("prior", e.get("previous")),
                "actual": e.get("actual"),
                "url": e.get("url") or "https://www.financecalendar.com/",
            })

        # BTC is most sensitive to USD/global macro.
        events = [
            e for e in events
            if str(e["currency"]).upper() in (
                "USD", "US", "GLOBAL", "EUR", "EU", "JPY", "CN", "CNY", ""
            )
        ]

        events.sort(key=lambda x: x["time"])
        return events

    except Exception as e:
        print("Calendar error:", e)
        return []


# ============================================================
# MACRO EVENT INTERPRETATION
# ============================================================

def numeric_surprise(actual, forecast):
    a = safe_float(actual)
    f = safe_float(forecast)

    if a is None or f is None:
        return None

    return a - f


def event_direction(event):
    """
    Direction is a market interpretation, not a guaranteed outcome.

    For many BTC-sensitive releases:
      - Hot inflation / hawkish Fed => bearish BTC
      - Cooler inflation / dovish Fed => bullish BTC
      - Stronger USD/yields => generally bearish BTC
      - Easier liquidity => generally bullish BTC

    Jobs data is more nuanced, so the bot calls it "mixed" when
    the labor signal could be interpreted both ways.
    """
    name = event["name"].lower()
    actual = event.get("actual")
    forecast = event.get("forecast")

    surprise = numeric_surprise(actual, forecast)

    if surprise is None:
        return "WAIT", "No actual-vs-forecast surprise yet."

    # Inflation / price pressure
    inflation = any(k in name for k in [
        "cpi", "pce", "ppi", "inflation", "consumer price",
        "producer price", "price index",
    ])

    if inflation:
        if surprise > 0:
            return "BEARISH", "Inflation came in hotter than forecast; this can lift rate/yield pressure."
        if surprise < 0:
            return "BULLISH", "Inflation came in cooler than forecast; this can reduce rate/yield pressure."
        return "NEUTRAL", "Inflation matched expectations."

    # Fed / central bank
    if any(k in name for k in ["fomc", "fed rate", "interest rate decision", "fed decision"]):
        txt = f"{actual} {forecast}".lower()
        if any(k in txt for k in ["cut", "dovish", "hold"]):
            return "BULLISH", "The result/wording is being interpreted as less restrictive."
        if any(k in txt for k in ["hike", "hawkish"]):
            return "BEARISH", "The result/wording is being interpreted as more restrictive."
        return "MIXED", "Fed outcome needs to be judged together with yields and the press conference."

    # Labor market
    if any(k in name for k in [
        "nonfarm", "payroll", "unemployment", "jobless", "employment",
        "job openings", "jolts", "average hourly earnings", "wage",
    ]):
        if "unemployment" in name or "jobless" in name:
            # Higher unemployment can lower hike odds, but recession risk can offset.
            if surprise > 0:
                return "MIXED", "Weaker labor data can lower rate pressure but can also raise recession risk."
            if surprise < 0:
                return "MIXED", "Stronger labor data supports growth but can keep rates/yields higher."
        if surprise > 0:
            return "MIXED", "Stronger labor data may support risk appetite but can also keep Fed policy tighter."
        if surprise < 0:
            return "MIXED", "Weaker labor data may support easier policy but can raise growth concerns."
        return "NEUTRAL", "Labor data matched expectations."

    # Growth / activity
    if any(k in name for k in ["gdp", "retail sales", "pmi", "ism"]):
        if surprise > 0:
            return "MIXED", "Stronger growth can support risk appetite but may keep yields elevated."
        if surprise < 0:
            return "MIXED", "Weaker growth can support easier-policy expectations but may raise recession concerns."

    return "MIXED", "Macro surprise detected; BTC direction depends on yields, USD and risk sentiment."


# ============================================================
# MARKET OPEN CALENDAR
# ============================================================

MARKETS = [
    ("Tokyo", 9, 0),
    ("Hong Kong", 9, 30),
    ("London", 8, 0),
    ("New York", 9, 30),
]


def market_open_utc(name, date_utc):
    """
    Returns today's open converted to UTC.

    Tokyo/Hong Kong have no DST.
    London/New York are handled using ZoneInfo.
    """
    if name == "Tokyo":
        local = datetime(
            date_utc.year, date_utc.month, date_utc.day,
            9, 0, tzinfo=ZoneInfo("Asia/Tokyo")
        )
    elif name == "Hong Kong":
        local = datetime(
            date_utc.year, date_utc.month, date_utc.day,
            9, 30, tzinfo=ZoneInfo("Asia/Hong_Kong")
        )
    elif name == "London":
        local = datetime(
            date_utc.year, date_utc.month, date_utc.day,
            8, 0, tzinfo=ZoneInfo("Europe/London")
        )
    else:
        local = datetime(
            date_utc.year, date_utc.month, date_utc.day,
            9, 30, tzinfo=NY_TZ
        )

    return local.astimezone(UTC)


def next_market_opens():
    now = now_utc()
    result = []

    for name, _, _ in MARKETS:
        for day_offset in range(0, 3):
            date = (now + timedelta(days=day_offset)).date()
            open_dt = market_open_utc(name, datetime.combine(date, datetime.min.time(), tzinfo=UTC))

            # Avoid weekends for major stock markets.
            if open_dt.weekday() >= 5:
                continue

            if open_dt > now:
                result.append((name, open_dt))
                break

    result.sort(key=lambda x: x[1])
    return result


# ============================================================
# BTC BIAS / SCENARIO ENGINE
# ============================================================

def calculate_bias(tech, futures, cross, news):
    score = 0.0
    reasons = []

    h1 = tech.get("1h") or {}
    h4 = tech.get("4h") or {}
    m15 = tech.get("15m") or {}

    # Technical structure.
    if h4.get("trend") == "BULLISH":
        score += 2
        reasons.append("4h structure bullish")
    elif h4.get("trend") == "BEARISH":
        score -= 2
        reasons.append("4h structure bearish")

    if h1.get("trend") == "BULLISH":
        score += 1.5
        reasons.append("1h trend bullish")
    elif h1.get("trend") == "BEARISH":
        score -= 1.5
        reasons.append("1h trend bearish")

    if m15.get("trend") == "BULLISH":
        score += 0.5
    elif m15.get("trend") == "BEARISH":
        score -= 0.5

    rsi = h1.get("rsi")
    if rsi is not None:
        if 50 <= rsi <= 68:
            score += 0.5
            reasons.append(f"1h RSI supportive ({rsi:.1f})")
        elif rsi > 75:
            score -= 0.5
            reasons.append(f"1h RSI overheated ({rsi:.1f})")
        elif rsi < 30:
            score += 0.5
            reasons.append(f"1h RSI deeply oversold ({rsi:.1f})")

    # Funding.
    funding = futures.get("funding")
    if funding is not None:
        funding_pct = funding * 100
        if funding_pct > 0.05:
            score -= 0.75
            reasons.append(f"very positive funding ({funding_pct:.3f}%)")
        elif funding_pct < -0.05:
            score += 0.75
            reasons.append(f"very negative funding ({funding_pct:.3f}%)")

    # OI.
    oi_change = futures.get("oi_change_pct")
    if oi_change is not None:
        if oi_change > 2:
            reasons.append(f"OI rising sharply ({oi_change:+.2f}%)")
        elif oi_change < -2:
            reasons.append(f"OI falling sharply ({oi_change:+.2f}%)")

    # Cross-market.
    dxy = cross.get("DXY") or {}
    nasdaq = cross.get("NASDAQ") or {}
    us10y = cross.get("US10Y") or {}
    vix = cross.get("VIX") or {}

    if dxy.get("change") is not None:
        if dxy["change"] > 0.4:
            score -= 0.75
            reasons.append("DXY stronger")
        elif dxy["change"] < -0.4:
            score += 0.75
            reasons.append("DXY weaker")

    if nasdaq.get("change") is not None:
        if nasdaq["change"] > 0.5:
            score += 0.5
            reasons.append("Nasdaq futures risk-on")
        elif nasdaq["change"] < -0.5:
            score -= 0.5
            reasons.append("Nasdaq futures risk-off")

    if us10y.get("change") is not None:
        if us10y["change"] > 1.0:
            score -= 0.5
            reasons.append("10Y yield rising")
        elif us10y["change"] < -1.0:
            score += 0.5
            reasons.append("10Y yield falling")

    if vix.get("change") is not None:
        if vix["change"] > 5:
            score -= 0.75
            reasons.append("VIX volatility/risk-off rising")
        elif vix["change"] < -5:
            score += 0.5
            reasons.append("VIX cooling")

    # News.
    bull_news = sum(1 for n in news[:15] if n["direction"] == "BULLISH" and n["impact"] != "LOW")
    bear_news = sum(1 for n in news[:15] if n["direction"] == "BEARISH" and n["impact"] != "LOW")

    if bull_news > bear_news:
        score += min(1.0, 0.25 * (bull_news - bear_news))
        reasons.append("recent news flow leans bullish")
    elif bear_news > bull_news:
        score -= min(1.0, 0.25 * (bear_news - bull_news))
        reasons.append("recent news flow leans bearish")

    if score >= 2.0:
        bias = "BULLISH"
    elif score <= -2.0:
        bias = "BEARISH"
    else:
        bias = "NEUTRAL / MIXED"

    confidence = int(clamp(50 + abs(score) * 10, 50, 90))

    return {
        "score": score,
        "bias": bias,
        "confidence": confidence,
        "reasons": reasons[:8],
    }


def scenario_levels(tech):
    h1 = tech.get("1h") or {}
    h4 = tech.get("4h") or {}

    price = h1.get("price") or h4.get("price")
    atr = h1.get("atr")

    if price is None:
        return {}

    if atr is None:
        # Fallback to a conservative 1% scenario distance.
        atr = price * 0.01

    support = h1.get("support")
    resistance = h1.get("resistance")

    bull_target_1 = price + atr
    bull_target_2 = price + 2 * atr

    bear_target_1 = price - atr
    bear_target_2 = price - 2 * atr

    if resistance and resistance > price:
        bull_target_1 = max(bull_target_1, resistance)

    if support and support < price:
        bear_target_1 = min(bear_target_1, support)

    return {
        "price": price,
        "atr": atr,
        "atr_pct": atr / price * 100,
        "bull1": bull_target_1,
        "bull2": bull_target_2,
        "bear1": bear_target_1,
        "bear2": bear_target_2,
        "support": support,
        "resistance": resistance,
    }


# ============================================================
# EVENT / NEWS ALERTS
# ============================================================

def event_key(event):
    return str(event["id"])


def event_alert_message(event, minutes_to_event):
    direction, explanation = event_direction(event)

    if minutes_to_event > 0:
        timing = f"in about {minutes_to_event} minutes"
        title = "⏰ IMPORTANT BTC EVENT SOON"
    else:
        timing = "released / happening now"
        title = "🚨 IMPORTANT BTC EVENT NOW"

    forecast = event.get("forecast")
    previous = event.get("previous")
    actual = event.get("actual")

    lines = [
        title,
        "━━━━━━━━━━━━━━━━━━━━",
        f"Event: {event['name']}",
        f"Currency: {event['currency'] or 'Global'}",
        f"Time: {utc_text(event['time'])}",
        f"Timing: {timing}",
        f"Impact: {event['impact'].upper()}",
    ]

    if forecast is not None:
        lines.append(f"Forecast: {forecast}")
    if previous is not None:
        lines.append(f"Previous: {previous}")
    if actual is not None:
        lines.append(f"Actual: {actual}")

    lines += [
        "",
        f"BTC interpretation: {direction}",
        f"Why: {explanation}",
        "",
        "⚠️ Important: BTC can move opposite to the first reaction if yields, DXY or Fed guidance changes.",
        f"Source: {event.get('url') or 'Finance Calendar'}",
    ]

    return "\n".join(lines)


def check_event_alerts(state, events):
    now = now_utc()

    for event in events:
        if event["impact"] != "high":
            continue

        diff_min = (event["time"] - now).total_seconds() / 60

        # Upcoming alerts.
        for window in EVENT_ALERT_WINDOWS:
            if window - 1 <= diff_min <= window + 1:
                key = f"{event_key(event)}:T-{window}"
                if not state["event_alerts"].get(key):
                    send(event_alert_message(event, window))
                    state["event_alerts"][key] = time.time()

        # Release alert. Only if the event has arrived.
        if -3 <= diff_min <= 1:
            key = f"{event_key(event)}:NOW"

            # If actual data is present, this is especially important.
            if event.get("actual") is not None and not state["event_alerts"].get(key):
                send(event_alert_message(event, 0))
                state["event_alerts"][key] = time.time()


def check_actual_event_changes(state, events):
    """
    Sends an alert if an event's actual value appears/changes.
    This is useful even if the calendar timestamp was missed.
    """
    for event in events:
        if event.get("actual") is None:
            continue

        key = event_key(event)
        actual = str(event.get("actual"))

        if state["last_actual_events"].get(key) == actual:
            continue

        # Only alert for events close to now or recently released.
        age_min = (now_utc() - event["time"]).total_seconds() / 60

        if -5 <= age_min <= 30:
            send(event_alert_message(event, 0))
            state["last_actual_events"][key] = actual


def check_market_open_alerts(state):
    now = now_utc()

    for name, open_dt in next_market_opens():
        diff_min = (open_dt - now).total_seconds() / 60

        for window in MARKET_OPEN_WINDOWS:
            if window - 1 <= diff_min <= window + 1:
                key = f"{name}:{open_dt.date()}:{window}"

                if state["market_open_alerts"].get(key):
                    continue

                if window == 0:
                    timing = f"{name} market is OPEN now."
                else:
                    timing = f"{name} market opens in about {window} minutes."

                extra = ""
                if name == "New York":
                    extra = (
                        "\n\n🔥 US OPEN = higher probability of BTC volatility."
                        "\nWatch DXY, Nasdaq, Treasury yields and ETF/crypto flows."
                    )
                elif name in ("London", "Tokyo", "Hong Kong"):
                    extra = (
                        "\n\nCrypto trades 24/7, but this regional session can "
                        "change liquidity and volatility."
                    )

                send(
                    f"🌍 MARKET SESSION ALERT\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"{timing}\n"
                    f"UTC open: {utc_text(open_dt)}"
                    f"{extra}\n\n"
                    f"⚠️ This is a volatility warning, not a guaranteed BTC direction."
                )

                state["market_open_alerts"][key] = time.time()


# ============================================================
# 30-MINUTE REPORT
# ============================================================

def format_news(news):
    if not news:
        return ["News feed unavailable right now."]

    lines = []

    for item in news[:MAX_NEWS_ITEMS]:
        icon = {
            "BULLISH": "🟢",
            "BEARISH": "🔴",
            "MIXED": "🟡",
        }.get(item["direction"], "⚪")

        lines.append(
            f"{icon} [{item['impact']}] {item['title']}"
            f"\n   Source: {item['source']}"
        )

    return lines


def upcoming_events_text(events):
    now = now_utc()
    future = [
        e for e in events
        if e["time"] >= now and e["time"] <= now + timedelta(days=7)
    ]

    if not future:
        return ["No high-impact BTC-relevant events found in the next 7 days."]

    lines = []

    for e in future[:8]:
        mins = int((e["time"] - now).total_seconds() / 60)

        if mins < 60:
            when = f"in {max(0, mins)}m"
        elif mins < 24 * 60:
            when = f"in {mins // 60}h {mins % 60}m"
        else:
            when = e["time"].strftime("%d %b %H:%M UTC")

        forecast = e.get("forecast")
        forecast_text = f" | Forecast {forecast}" if forecast is not None else ""

        lines.append(
            f"• {e['name']} — {when}{forecast_text}"
        )

    return lines


def send_market_report(tech, futures, cross, news, events):
    ticker = get_btc_ticker()

    h1 = tech.get("1h") or {}
    h4 = tech.get("4h") or {}
    m15 = tech.get("15m") or {}

    bias = calculate_bias(tech, futures, cross, news)
    levels = scenario_levels(tech)

    price = ticker.get("price") or h1.get("price")
    day_change = ticker.get("change_24h")

    lines = [
        "₿ BTC MARKET INTELLIGENCE — 30 MIN",
        "━━━━━━━━━━━━━━━━━━━━",
        f"Time: {utc_text()}",
        f"BTC: {fmt_price(price)} | 24h: {fmt_pct(day_change)}",
        "",
        f"🧠 CURRENT BIAS: {bias['bias']}",
        f"Confidence: {bias['confidence']}%  | Score: {bias['score']:+.2f}",
        "",
    ]

    # Technical
    lines += [
        "📊 TECHNICAL PICTURE",
        f"5m:  {(tech.get('5m') or {}).get('trend', 'N/A')}",
        f"15m: {m15.get('trend', 'N/A')} | RSI {m15.get('rsi', 0):.1f}" if m15.get("rsi") is not None else f"15m: {m15.get('trend', 'N/A')}",
        f"1h:  {h1.get('trend', 'N/A')} | RSI {h1.get('rsi', 0):.1f}" if h1.get("rsi") is not None else f"1h: {h1.get('trend', 'N/A')}",
        f"4h:  {h4.get('trend', 'N/A')}",
    ]

    if levels:
        lines += [
            f"1h ATR: {fmt_price(levels['atr'])} ({levels['atr_pct']:.2f}%)",
            f"Support: {fmt_price(levels['support'])}",
            f"Resistance: {fmt_price(levels['resistance'])}",
        ]

    # Scenario
    if levels:
        lines += [
            "",
            "🎯 SCENARIOS — NOT GUARANTEED",
            f"🟢 Bull case: above current structure → {fmt_price(levels['bull1'])} → {fmt_price(levels['bull2'])}",
            f"🔴 Bear case: below current structure → {fmt_price(levels['bear1'])} → {fmt_price(levels['bear2'])}",
            f"Expected normal 1h volatility estimate: ±{levels['atr_pct']:.2f}%",
        ]

    # Derivatives
    funding = futures.get("funding")
    lines += [
        "",
        "📈 BINANCE FUTURES",
        f"Funding: {funding * 100:+.4f}%" if funding is not None else "Funding: N/A",
        f"OI: {futures.get('open_interest', 0):,.2f}" if futures.get("open_interest") is not None else "OI: N/A",
        f"OI 5m: {fmt_pct(futures.get('oi_change_pct'))}",
        f"Liquidations ~1h: Long ${futures.get('long_liquidation', 0):,.0f} | Short ${futures.get('short_liquidation', 0):,.0f}",
    ]

    # Cross market
    lines += ["", "🌐 MACRO / RISK ASSETS"]

    for name in ("DXY", "NASDAQ", "US10Y", "VIX", "GOLD"):
        a = cross.get(name) or {}
        if a.get("price") is not None:
            lines.append(
                f"{name}: {a['price']:,.2f} | session {fmt_pct(a.get('change'))}"
            )
        else:
            lines.append(f"{name}: N/A")

    # Bias reasons
    lines += [
        "",
        "🧩 WHY THIS BIAS",
    ]

    if bias["reasons"]:
        lines.extend(f"• {x}" for x in bias["reasons"])
    else:
        lines.append("• Not enough confirmation; treat BTC as neutral.")

    # Important upcoming events
    lines += [
        "",
        "⏰ IMPORTANT EVENTS — NEXT 7 DAYS",
    ]
    lines.extend(upcoming_events_text(events))

    # News
    lines += [
        "",
        "📰 LATEST BTC / MACRO NEWS",
    ]
    lines.extend(format_news(news))

    lines += [
        "",
        "⚠️ HOW TO READ THIS",
        "Bullish/Bearish means the combined data currently leans that way.",
        "It does NOT guarantee BTC will move in that direction.",
        "The biggest risk is a new headline, macro release, Fed comment, yield/DXY shock or liquidation cascade.",
        "",
        "Calendar source: https://www.financecalendar.com/",
        "Signals/analysis only — NO orders are placed.",
    ]

    send("\n".join(lines))


# ============================================================
# STARTUP MESSAGE
# ============================================================

def send_startup():
    send(
        "₿ BTC MARKET INTELLIGENCE BOT STARTED\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Asset: BTC/USDT only\n"
        "Reports: every 30 minutes\n"
        "News: crypto + macro headlines\n"
        "Events: important economic releases + countdowns\n"
        "Market sessions: Tokyo / Hong Kong / London / New York\n"
        "Derivatives: funding + OI + liquidations\n"
        "Macro: DXY + Nasdaq futures + US10Y + VIX + Gold\n"
        "Scenarios: Bull / Base / Bear with estimated levels\n"
        "⚠️ No guaranteed predictions.\n"
        "❌ The bot never places trades."
    )


# ============================================================
# MAIN
# ============================================================

def main():
    state = load_state()
    send_startup()

    start = time.time()
    last_data_update = 0
    last_calendar_update = 0

    tech = {}
    futures = {}
    cross = {}
    news = []
    events = []

    while time.time() - start < RUN_SECONDS:
        try:
            now = time.time()

            # Market/technical data approximately once per minute.
            if now - last_data_update >= DATA_POLL_INTERVAL:
                print("Updating BTC market data...")

                tech = build_technical_context()
                futures = get_futures_context()
                cross = get_cross_market()
                news = fetch_news()

                last_data_update = now

            # Calendar can be fetched less often.
            if now - last_calendar_update >= 5 * 60:
                print("Updating economic calendar...")
                events = fetch_calendar()

                check_event_alerts(state, events)
                check_actual_event_changes(state, events)

                last_calendar_update = now

            # Market session warnings are checked every minute.
            check_market_open_alerts(state)

            # 30-minute full report.
            if state["last_report"] == 0 or now - state["last_report"] >= REPORT_INTERVAL:
                if tech:
                    send_market_report(
                        tech,
                        futures,
                        cross,
                        news,
                        events,
                    )
                    state["last_report"] = now

            save_state(state)

            price = (tech.get("1h") or {}).get("price")
            bias = calculate_bias(tech, futures, cross, news) if tech else {}
            print(
                datetime.now().strftime("%H:%M:%S"),
                "| BTC:", price,
                "| Bias:", bias.get("bias"),
                "| Events:", len(events),
            )

        except Exception:
            traceback.print_exc()

        time.sleep(DATA_POLL_INTERVAL)

    save_state(state)

    send(
        "🛑 BTC MARKET INTELLIGENCE SESSION FINISHED\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"Runtime: {RUN_SECONDS // 60} minutes\n"
        "State saved.\n"
        "Signals only — no orders were placed."
    )


if __name__ == "__main__":
    main()
