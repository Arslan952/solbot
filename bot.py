```python
import os
import time
import ccxt
import pandas as pd
import requests
import traceback
import xml.etree.ElementTree as ET
from datetime import datetime, timezone


# ============================================================
# CONFIGURATION
# ============================================================

TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

SYMBOL = "SOL/USDT"

# Main trading timeframe
TF = "5m"
TF_MS = 5 * 60 * 1000

# How often the bot checks the market
POLL = 15

# GitHub Actions / runner safety limit
RUN_SECONDS = 350 * 60

# Detailed report
REPORT_INTERVAL = 10 * 60

# Signal filters
MIN_ATR_PCT = 0.0025
MIN_VOLUME_RATIO = 1.20

# Trade levels
SL_MULT = 1.0
TP1_MULT = 1.5
TP2_MULT = 2.5

# Minimum confirmation score
MIN_LONG_SCORE = 8
MIN_SHORT_SCORE = 8

# External data refresh intervals
EXTERNAL_DATA_INTERVAL = 60
NEWS_INTERVAL = 10 * 60


# ============================================================
# EXCHANGE
# ============================================================

exchange = ccxt.okx({
    "enableRateLimit": True,
    "timeout": 15000
})


# ============================================================
# HTTP SESSION
# ============================================================

session = requests.Session()

session.headers.update({
    "User-Agent": "SOL-Market-Bot/1.0"
})


# ============================================================
# TELEGRAM
# ============================================================

def send(msg):

    if not TOKEN or not CHAT_ID:
        print("Telegram credentials are missing.")
        return False

    try:

        r = session.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            data={
                "chat_id": CHAT_ID,
                "text": msg
            },
            timeout=15
        )

        print("Telegram:", r.status_code)

        return r.ok

    except Exception:

        traceback.print_exc()

        return False


# ============================================================
# HELPERS
# ============================================================

def safe_float(value, default=None):

    try:

        return float(value)

    except Exception:

        return default


def fmt(value, decimals=3):

    if value is None:
        return "N/A"

    try:

        return f"{float(value):.{decimals}f}"

    except Exception:

        return "N/A"


def now_utc():

    return datetime.now(timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )


# ============================================================
# GET OHLCV
# ============================================================

def get_df(symbol=SYMBOL, timeframe=TF, limit=300):

    raw = exchange.fetch_ohlcv(
        symbol,
        timeframe,
        limit=limit
    )

    df = pd.DataFrame(
        raw,
        columns=[
            "time",
            "open",
            "high",
            "low",
            "close",
            "volume"
        ]
    )

    return df


# ============================================================
# INDICATORS
# ============================================================

def add_indicators(df):

    df = df.copy()

    # --------------------------------------------------------
    # EMA
    # --------------------------------------------------------

    df["ema_fast"] = df.close.ewm(
        span=9,
        adjust=False
    ).mean()

    df["ema_slow"] = df.close.ewm(
        span=21,
        adjust=False
    ).mean()

    df["ema_trend"] = df.close.ewm(
        span=50,
        adjust=False
    ).mean()


    # --------------------------------------------------------
    # RSI
    # --------------------------------------------------------

    delta = df.close.diff()

    gain = delta.clip(
        lower=0
    ).ewm(
        alpha=1 / 14,
        adjust=False
    ).mean()

    loss = (-delta.clip(
        upper=0
    )).ewm(
        alpha=1 / 14,
        adjust=False
    ).mean()

    rs = gain / loss.replace(0, pd.NA)

    df["rsi"] = (
        100 -
        (
            100 /
            (1 + rs)
        )
    )


    # --------------------------------------------------------
    # ATR
    # --------------------------------------------------------

    tr = pd.concat(
        [
            df.high - df.low,
            (df.high - df.close.shift()).abs(),
            (df.low - df.close.shift()).abs()
        ],
        axis=1
    ).max(axis=1)

    df["atr"] = tr.ewm(
        alpha=1 / 14,
        adjust=False
    ).mean()


    # --------------------------------------------------------
    # DAILY VWAP
    # --------------------------------------------------------

    day = pd.to_datetime(
        df.time,
        unit="ms"
    ).dt.date

    typical_price = (
        df.high +
        df.low +
        df.close
    ) / 3

    cumulative_volume = (
        df.volume
        .groupby(day)
        .cumsum()
    )

    cumulative_pv = (
        typical_price * df.volume
    ).groupby(day).cumsum()

    df["vwap"] = (
        cumulative_pv /
        cumulative_volume
    )


    # --------------------------------------------------------
    # VOLUME
    # --------------------------------------------------------

    df["vol_avg"] = (
        df.volume
        .rolling(20)
        .mean()
    )

    df["volume_ratio"] = (
        df.volume /
        df.vol_avg
    )


    # --------------------------------------------------------
    # CANDLE CHANGE
    # --------------------------------------------------------

    df["change_pct"] = (
        (
            df.close -
            df.open
        )
        /
        df.open
    ) * 100


    return df


# ============================================================
# MULTI-TIMEFRAME ANALYSIS
# ============================================================

def timeframe_status(df):

    c = df.iloc[-2]

    price = c.close

    if (
        price >
        c.ema_fast >
        c.ema_slow >
        c.ema_trend
        and
        price > c.vwap
    ):

        return "BULLISH"

    if (
        price <
        c.ema_fast <
        c.ema_slow <
        c.ema_trend
        and
        price < c.vwap
    ):

        return "BEARISH"

    if (
        price > c.ema_slow
        and
        price > c.ema_trend
    ):

        return "WEAK BULLISH"

    if (
        price < c.ema_slow
        and
        price < c.ema_trend
    ):

        return "WEAK BEARISH"

    return "NEUTRAL"


def get_multi_timeframe():

    timeframes = [
        "5m",
        "15m",
        "1h",
        "4h"
    ]

    results = {}

    for timeframe in timeframes:

        try:

            df = add_indicators(
                get_df(
                    SYMBOL,
                    timeframe,
                    300
                )
            )

            results[timeframe] = {
                "status": timeframe_status(df),
                "df": df
            }

        except Exception as e:

            print(
                f"MTF error {timeframe}:",
                e
            )

            results[timeframe] = {
                "status": "UNAVAILABLE",
                "df": None
            }

    return results


# ============================================================
# MARKET STRUCTURE
# ============================================================

def market_structure(df):

    recent = df.iloc[-21:-1]

    if len(recent) < 20:

        return "UNKNOWN"

    first_half = recent.iloc[:10]
    second_half = recent.iloc[10:]

    first_high = first_half.high.max()
    second_high = second_half.high.max()

    first_low = first_half.low.min()
    second_low = second_half.low.min()

    if (
        second_high > first_high
        and
        second_low > first_low
    ):

        return "HH + HL / Bullish Structure"

    if (
        second_high < first_high
        and
        second_low < first_low
    ):

        return "LH + LL / Bearish Structure"

    return "Mixed / Range"


# ============================================================
# SUPPORT / RESISTANCE
# ============================================================

def get_support_resistance(df):

    recent = df.iloc[-50:-1]

    support = recent.low.min()
    resistance = recent.high.max()

    return support, resistance


# ============================================================
# BTC + CRYPTO MARKET CONTEXT
# ============================================================

def get_crypto_context():

    result = {
        "btc_price": None,
        "btc_change": None,
        "sol_price": None,
        "sol_change": None,
        "eth_change": None,
        "btc_dominance": None
    }

    try:

        url = (
            "https://api.coingecko.com/api/v3/simple/price"
            "?ids=bitcoin,solana,ethereum"
            "&vs_currencies=usd"
            "&include_24hr_change=true"
        )

        data = session.get(
            url,
            timeout=10
        ).json()

        result["btc_price"] = safe_float(
            data["bitcoin"]["usd"]
        )

        result["btc_change"] = safe_float(
            data["bitcoin"].get(
                "usd_24h_change"
            )
        )

        result["sol_price"] = safe_float(
            data["solana"]["usd"]
        )

        result["sol_change"] = safe_float(
            data["solana"].get(
                "usd_24h_change"
            )
        )

        result["eth_change"] = safe_float(
            data["ethereum"].get(
                "usd_24h_change"
            )
        )

    except Exception as e:

        print(
            "Crypto context error:",
            e
        )


    # --------------------------------------------------------
    # Global market data
    # --------------------------------------------------------

    try:

        data = session.get(
            "https://api.coingecko.com/api/v3/global",
            timeout=10
        ).json()

        result["btc_dominance"] = safe_float(
            data["data"]["market_cap_percentage"]["btc"]
        )

    except Exception as e:

        print(
            "BTC dominance error:",
            e
        )


    return result


# ============================================================
# BINANCE FUTURES DATA
# ============================================================

def get_binance_futures():

    result = {
        "funding": None,
        "open_interest": None,
        "oi_change_pct": None,
        "long_liquidation": 0,
        "short_liquidation": 0
    }

    symbol = "SOLUSDT"


    # --------------------------------------------------------
    # Funding
    # --------------------------------------------------------

    try:

        data = session.get(
            "https://fapi.binance.com/fapi/v1/premiumIndex",
            params={
                "symbol": symbol
            },
            timeout=10
        ).json()

        result["funding"] = safe_float(
            data.get("lastFundingRate")
        )

    except Exception as e:

        print(
            "Funding error:",
            e
        )


    # --------------------------------------------------------
    # Open Interest
    # --------------------------------------------------------

    try:

        data = session.get(
            "https://fapi.binance.com/fapi/v1/openInterest",
            params={
                "symbol": symbol
            },
            timeout=10
        ).json()

        result["open_interest"] = safe_float(
            data.get("openInterest")
        )

    except Exception as e:

        print(
            "OI error:",
            e
        )


    # --------------------------------------------------------
    # OI historical change
    # --------------------------------------------------------

    try:

        data = session.get(
            "https://fapi.binance.com/futures/data/openInterestHist",
            params={
                "symbol": symbol,
                "period": "5m",
                "limit": 2
            },
            timeout=10
        ).json()

        if isinstance(data, list) and len(data) >= 2:

            old_oi = safe_float(
                data[-2].get(
                    "sumOpenInterest"
                )
            )

            new_oi = safe_float(
                data[-1].get(
                    "sumOpenInterest"
                )
            )

            if old_oi and new_oi:

                result["oi_change_pct"] = (
                    (
                        new_oi -
                        old_oi
                    )
                    /
                    old_oi
                ) * 100

    except Exception as e:

        print(
            "Historical OI error:",
            e
        )


    # --------------------------------------------------------
    # Liquidations
    # --------------------------------------------------------

    try:

        data = session.get(
            "https://fapi.binance.com/fapi/v1/allForceOrders",
            params={
                "symbol": symbol,
                "limit": 100
            },
            timeout=10
        ).json()

        if isinstance(data, list):

            one_hour_ago = (
                int(time.time() * 1000)
                -
                60 * 60 * 1000
            )

            for order in data:

                order_time = order.get("time", 0)

                if order_time < one_hour_ago:
                    continue

                qty = safe_float(
                    order.get("origQty"),
                    0
                )

                price = safe_float(
                    order.get("price"),
                    0
                )

                value = qty * price

                side = order.get(
                    "side"
                )

                # A forced SELL generally closes a LONG.
                if side == "SELL":

                    result[
                        "long_liquidation"
                    ] += value

                # A forced BUY generally closes a SHORT.
                elif side == "BUY":

                    result[
                        "short_liquidation"
                    ] += value

    except Exception as e:

        print(
            "Liquidation error:",
            e
        )


    return result


# ============================================================
# SOLANA NETWORK DATA
# ============================================================

def solana_rpc(method, params=None):

    try:

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method
        }

        if params is not None:

            payload["params"] = params

        r = session.post(
            "https://api.mainnet-beta.solana.com",
            json=payload,
            timeout=10
        )

        return r.json()

    except Exception as e:

        print(
            "Solana RPC error:",
            e
        )

        return None


def get_solana_network():

    result = {
        "health": None,
        "slot": None,
        "epoch": None,
        "tps": None,
        "transactions": None
    }


    # --------------------------------------------------------
    # Health
    # --------------------------------------------------------

    try:

        data = solana_rpc(
            "getHealth"
        )

        if data:

            result["health"] = data.get(
                "result"
            )

    except Exception as e:

        print(
            "Health error:",
            e
        )


    # --------------------------------------------------------
    # Epoch
    # --------------------------------------------------------

    try:

        data = solana_rpc(
            "getEpochInfo"
        )

        if data and data.get("result"):

            epoch_data = data["result"]

            result["slot"] = epoch_data.get(
                "absoluteSlot"
            )

            result["epoch"] = epoch_data.get(
                "epoch"
            )

    except Exception as e:

        print(
            "Epoch error:",
            e
        )


    # --------------------------------------------------------
    # Network performance
    # --------------------------------------------------------

    try:

        data = solana_rpc(
            "getRecentPerformanceSamples",
            [1]
        )

        if (
            data
            and
            data.get("result")
        ):

            sample = data["result"][0]

            transactions = sample.get(
                "numTransactions"
            )

            seconds = sample.get(
                "samplePeriodSecs"
            )

            if seconds:

                result["tps"] = (
                    transactions /
                    seconds
                )

                result[
                    "transactions"
                ] = transactions

    except Exception as e:

        print(
            "TPS error:",
            e
        )


    return result


# ============================================================
# NEWS
# ============================================================

news_cache = []
last_news_update = 0


def get_news():

    global news_cache
    global last_news_update

    now = time.time()

    if (
        news_cache
        and
        now - last_news_update < NEWS_INTERVAL
    ):

        return news_cache


    try:

        url = (
            "https://news.google.com/rss/search"
            "?q=Solana%20SOL%20crypto"
            "&hl=en-US"
            "&gl=US"
            "&ceid=US:en"
        )

        r = session.get(
            url,
            timeout=10
        )

        root = ET.fromstring(
            r.text
        )

        headlines = []

        for item in root.findall(
            ".//item"
        )[:5]:

            title = item.findtext(
                "title"
            )

            if title:

                headlines.append(
                    title.strip()
                )


        news_cache = headlines

        last_news_update = now

        return news_cache

    except Exception as e:

        print(
            "News error:",
            e
        )

        return news_cache


# ============================================================
# ORIGINAL SIGNAL ENGINE
# ============================================================

def signal_at(df, i):

    prev = df.iloc[i - 1]

    c = df.iloc[i]


    if (
        pd.isna(c.vol_avg)
        or
        pd.isna(c.vwap)
        or
        pd.isna(c.rsi)
    ):

        return None


    # Volatility
    if (
        c.atr / c.close
        <
        MIN_ATR_PCT
    ):

        return None


    # Volume
    if (
        c.volume
        <
        MIN_VOLUME_RATIO *
        c.vol_avg
    ):

        return None


    # EMA crossover
    up = (
        prev.ema_fast <= prev.ema_slow
        and
        c.ema_fast > c.ema_slow
    )

    down = (
        prev.ema_fast >= prev.ema_slow
        and
        c.ema_fast < c.ema_slow
    )


    if (
        up
        and
        c.close > c.vwap
        and
        c.close > c.ema_trend
        and
        50 < c.rsi < 70
    ):

        return "LONG"


    if (
        down
        and
        c.close < c.vwap
        and
        c.close < c.ema_trend
        and
        30 < c.rsi < 50
    ):

        return "SHORT"


    return None


# ============================================================
# ADVANCED SIGNAL SCORING
# ============================================================

def advanced_signal(df, mtf, crypto, futures):

    c = df.iloc[-2]

    prev = df.iloc[-3]

    long_score = 0
    short_score = 0

    long_reasons = []
    short_reasons = []


    # --------------------------------------------------------
    # EMA crossover
    # --------------------------------------------------------

    if (
        prev.ema_fast <= prev.ema_slow
        and
        c.ema_fast > c.ema_slow
    ):

        long_score += 2

        long_reasons.append(
            "Fresh EMA 9/21 bullish crossover"
        )


    if (
        prev.ema_fast >= prev.ema_slow
        and
        c.ema_fast < c.ema_slow
    ):

        short_score += 2

        short_reasons.append(
            "Fresh EMA 9/21 bearish crossover"
        )


    # --------------------------------------------------------
    # EMA direction
    # --------------------------------------------------------

    if c.ema_fast > c.ema_slow:

        long_score += 1

        long_reasons.append(
            "EMA 9 above EMA 21"
        )

    elif c.ema_fast < c.ema_slow:

        short_score += 1

        short_reasons.append(
            "EMA 9 below EMA 21"
        )


    # --------------------------------------------------------
    # EMA 50
    # --------------------------------------------------------

    if c.close > c.ema_trend:

        long_score += 1

        long_reasons.append(
            "Price above EMA 50"
        )

    elif c.close < c.ema_trend:

        short_score += 1

        short_reasons.append(
            "Price below EMA 50"
        )


    # --------------------------------------------------------
    # VWAP
    # --------------------------------------------------------

    if c.close > c.vwap:

        long_score += 1

        long_reasons.append(
            "Price above VWAP"
        )

    elif c.close < c.vwap:

        short_score += 1

        short_reasons.append(
            "Price below VWAP"
        )


    # --------------------------------------------------------
    # RSI
    # --------------------------------------------------------

    if 50 < c.rsi < 70:

        long_score += 1

        long_reasons.append(
            f"RSI bullish zone ({c.rsi:.1f})"
        )

    elif 30 < c.rsi < 50:

        short_score += 1

        short_reasons.append(
            f"RSI bearish zone ({c.rsi:.1f})"
        )


    # --------------------------------------------------------
    # Volume
    # --------------------------------------------------------

    if c.volume_ratio >= 1.2:

        if c.close > c.open:

            long_score += 1

            long_reasons.append(
                f"High bullish volume ({c.volume_ratio:.2f}x)"
            )

        elif c.close < c.open:

            short_score += 1

            short_reasons.append(
                f"High bearish volume ({c.volume_ratio:.2f}x)"
            )


    # --------------------------------------------------------
    # ATR
    # --------------------------------------------------------

    atr_pct = (
        c.atr /
        c.close
    )

    if atr_pct >= MIN_ATR_PCT:

        long_score += 1
        short_score += 1


    # --------------------------------------------------------
    # MULTI-TIMEFRAME
    # --------------------------------------------------------

    bullish_count = 0
    bearish_count = 0

    for timeframe in [
        "5m",
        "15m",
        "1h",
        "4h"
    ]:

        status = mtf.get(
            timeframe,
            {}
        ).get(
            "status",
            "UNAVAILABLE"
        )

        if "BULLISH" in status:

            bullish_count += 1

        if "BEARISH" in status:

            bearish_count += 1


    if bullish_count >= 3:

        long_score += 2

        long_reasons.append(
            f"Multi-timeframe bullish ({bullish_count}/4)"
        )


    if bearish_count >= 3:

        short_score += 2

        short_reasons.append(
            f"Multi-timeframe bearish ({bearish_count}/4)"
        )


    # --------------------------------------------------------
    # BTC
    # --------------------------------------------------------

    btc_change = crypto.get(
        "btc_change"
    )

    if btc_change is not None:

        if btc_change > 0.5:

            long_score += 1

            long_reasons.append(
                f"BTC 24h positive ({btc_change:+.2f}%)"
            )

        elif btc_change < -0.5:

            short_score += 1

            short_reasons.append(
                f"BTC 24h negative ({btc_change:+.2f}%)"
            )


    # --------------------------------------------------------
    # SOL/BTC relative strength
    # --------------------------------------------------------

    sol_change = crypto.get(
        "sol_change"
    )

    if (
        sol_change is not None
        and
        btc_change is not None
    ):

        sol_btc_relative = (
            sol_change -
            btc_change
        )

        if sol_btc_relative > 0.5:

            long_score += 1

            long_reasons.append(
                f"SOL outperforming BTC ({sol_btc_relative:+.2f}%)"
            )

        elif sol_btc_relative < -0.5:

            short_score += 1

            short_reasons.append(
                f"SOL underperforming BTC ({sol_btc_relative:+.2f}%)"
            )


    # --------------------------------------------------------
    # OPEN INTEREST
    # --------------------------------------------------------

    oi_change = futures.get(
        "oi_change_pct"
    )

    if oi_change is not None:

        if (
            c.close > prev.close
            and
            oi_change > 0
        ):

            long_score += 1

            long_reasons.append(
                f"Price + OI rising ({oi_change:+.2f}%)"
            )


        elif (
            c.close < prev.close
            and
            oi_change > 0
        ):

            short_score += 1

            short_reasons.append(
                f"Price falling + OI rising ({oi_change:+.2f}%)"
            )


    # --------------------------------------------------------
    # FINAL DECISION
    # --------------------------------------------------------

    side = None

    if (
        long_score >= MIN_LONG_SCORE
        and
        long_score > short_score + 1
    ):

        side = "LONG"


    elif (
        short_score >= MIN_SHORT_SCORE
        and
        short_score > long_score + 1
    ):

        side = "SHORT"


    return {
        "side": side,
        "long_score": long_score,
        "short_score": short_score,
        "long_reasons": long_reasons,
        "short_reasons": short_reasons
    }


# ============================================================
# TRADE LEVELS
# ============================================================

def levels(
    side,
    entry,
    atr
):

    if side == "LONG":

        sl = entry - (
            SL_MULT *
            atr
        )

        tp1 = entry + (
            TP1_MULT *
            atr
        )

        tp2 = entry + (
            TP2_MULT *
            atr
        )

    else:

        sl = entry + (
            SL_MULT *
            atr
        )

        tp1 = entry - (
            TP1_MULT *
            atr
        )

        tp2 = entry - (
            TP2_MULT *
            atr
        )

    return sl, tp1, tp2


# ============================================================
# MARKET CONDITION
# ============================================================

def get_market_condition(c):

    price = c.close

    if (
        price >
        c.ema_fast >
        c.ema_slow >
        c.ema_trend
        and
        price > c.vwap
        and
        c.rsi >= 55
    ):

        return "🟢 STRONG BULLISH"


    if (
        price > c.ema21
        if False
        else (
            price > c.ema_slow
            and
            price > c.ema_trend
            and
            price > c.vwap
        )
    ):

        return "🟢 BULLISH"


    if (
        price <
        c.ema_fast <
        c.ema_slow <
        c.ema_trend
        and
        price < c.vwap
        and
        c.rsi <= 45
    ):

        return "🔴 STRONG BEARISH"


    if (
        price < c.ema_slow
        and
        price < c.ema_trend
        and
        price < c.vwap
    ):

        return "🔴 BEARISH"


    return "🟡 NEUTRAL / SIDEWAYS"


# ============================================================
# WARNINGS
# ============================================================

def get_warnings(
    df,
    crypto,
    futures,
    mtf
):

    c = df.iloc[-2]

    warnings = []


    # RSI
    if c.rsi >= 70:

        warnings.append(
            "RSI is overbought"
        )

    elif c.rsi <= 30:

        warnings.append(
            "RSI is oversold"
        )


    # Low volume
    if c.volume_ratio < 0.8:

        warnings.append(
            "Volume is below average"
        )


    # High funding
    funding = futures.get(
        "funding"
    )

    if funding is not None:

        if funding > 0.001:

            warnings.append(
                "Funding is relatively positive"
            )

        elif funding < -0.001:

            warnings.append(
                "Funding is relatively negative"
            )


    # BTC weakness
    btc_change = crypto.get(
        "btc_change"
    )

    if (
        btc_change is not None
        and
        btc_change < -2
    ):

        warnings.append(
            "BTC has significant negative 24h movement"
        )


    # Timeframe conflict
    statuses = []

    for tf in [
        "5m",
        "15m",
        "1h",
        "4h"
    ]:

        statuses.append(
            mtf.get(
                tf,
                {}
            ).get(
                "status",
                "UNAVAILABLE"
            )
        )


    if (
        "BULLISH" in statuses
        and
        "BEARISH" in statuses
    ):

        warnings.append(
            "Multi-timeframe trend conflict"
        )


    return warnings


# ============================================================
# DETAILED REPORT
# ============================================================

def send_market_report(
    df,
    mtf,
    crypto,
    futures,
    network,
    news,
    scoring
):

    c = df.iloc[-2]

    price = c.close

    condition = get_market_condition(
        c
    )

    support, resistance = (
        get_support_resistance(df)
    )

    structure = market_structure(
        df
    )


    # --------------------------------------------------------
    # Volume
    # --------------------------------------------------------

    volume_ratio = c.volume_ratio

    if volume_ratio >= 1.5:

        volume_status = "🔥 Very High"

    elif volume_ratio >= 1.2:

        volume_status = "🟢 High"

    elif volume_ratio >= 0.8:

        volume_status = "🟡 Normal"

    else:

        volume_status = "🔴 Low"


    # --------------------------------------------------------
    # Volatility
    # --------------------------------------------------------

    atr_pct = (
        c.atr /
        price
    ) * 100


    if atr_pct >= 1:

        volatility = "🔥 High"

    elif atr_pct >= 0.5:

        volatility = "🟢 Moderate"

    else:

        volatility = "🟡 Low"


    # --------------------------------------------------------
    # RSI
    # --------------------------------------------------------

    if c.rsi >= 70:

        rsi_status = "⚠️ Overbought"

    elif c.rsi >= 55:

        rsi_status = "🟢 Bullish momentum"

    elif c.rsi <= 30:

        rsi_status = "⚠️ Oversold"

    elif c.rsi <= 45:

        rsi_status = "🔴 Bearish momentum"

    else:

        rsi_status = "🟡 Neutral"


    # --------------------------------------------------------
    # Candle
    # --------------------------------------------------------

    if c.close > c.open:

        candle = "🟢 Bullish"

    elif c.close < c.open:

        candle = "🔴 Bearish"

    else:

        candle = "🟡 Doji"


    # --------------------------------------------------------
    # Funding
    # --------------------------------------------------------

    funding = futures.get(
        "funding"
    )

    if funding is not None:

        funding_pct = funding * 100

        funding_text = (
            f"{funding_pct:+.4f}%"
        )

    else:

        funding_text = "N/A"


    # --------------------------------------------------------
    # OI
    # --------------------------------------------------------

    oi = futures.get(
        "open_interest"
    )

    oi_change = futures.get(
        "oi_change_pct"
    )


    # --------------------------------------------------------
    # BTC
    # --------------------------------------------------------

    btc_price = crypto.get(
        "btc_price"
    )

    btc_change = crypto.get(
        "btc_change"
    )

    sol_change = crypto.get(
        "sol_change"
    )

    eth_change = crypto.get(
        "eth_change"
    )

    btc_dom = crypto.get(
        "btc_dominance"
    )


    # --------------------------------------------------------
    # Liquidations
    # --------------------------------------------------------

    long_liq = futures.get(
        "long_liquidation",
        0
    )

    short_liq = futures.get(
        "short_liquidation",
        0
    )


    # --------------------------------------------------------
    # Signal
    # --------------------------------------------------------

    side = scoring["side"]

    if side == "LONG":

        signal_text = (
            "🟢 LONG"
        )

    elif side == "SHORT":

        signal_text = (
            "🔴 SHORT"
        )

    else:

        signal_text = (
            "⚪ NO CONFIRMED SIGNAL"
        )


    # --------------------------------------------------------
    # MTF
    # --------------------------------------------------------

    mtf_text = ""

    for timeframe in [
        "5m",
        "15m",
        "1h",
        "4h"
    ]:

        status = mtf.get(
            timeframe,
            {}
        ).get(
            "status",
            "UNAVAILABLE"
        )

        mtf_text += (
            f"{timeframe}: {status}\n"
        )


    # --------------------------------------------------------
    # News
    # --------------------------------------------------------

    news_text = ""

    if news:

        for headline in news[:5]:

            news_text += (
                f"• {headline}\n"
            )

    else:

        news_text = "No news available\n"


    # --------------------------------------------------------
    # Warnings
    # --------------------------------------------------------

    warnings = get_warnings(
        df,
        crypto,
        futures,
        mtf
    )

    warning_text = ""

    if warnings:

        for warning in warnings:

            warning_text += (
                f"⚠️ {warning}\n"
            )

    else:

        warning_text = (
            "No major automated warnings\n"
        )


    # --------------------------------------------------------
    # NETWORK
    # --------------------------------------------------------

    network_health = (
        network.get("health")
        or
        "N/A"
    )

    slot = network.get(
        "slot"
    )

    epoch = network.get(
        "epoch"
    )

    tps = network.get(
        "tps"
    )


    message = (

        f"📊 SOLANA FULL MARKET REPORT\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 {now_utc()}\n"
        f"Pair: {SYMBOL}\n"
        f"Main TF: {TF}\n\n"


        f"💰 PRICE & MARKET\n"
        f"Price: {price:.3f}\n"
        f"Condition: {condition}\n"
        f"Candle: {candle}\n"
        f"Structure: {structure}\n\n"


        f"📈 TECHNICAL INDICATORS\n"
        f"EMA 9: {c.ema_fast:.3f}\n"
        f"EMA 21: {c.ema_slow:.3f}\n"
        f"EMA 50: {c.ema_trend:.3f}\n"
        f"VWAP: {c.vwap:.3f}\n"
        f"RSI: {c.rsi:.1f} — {rsi_status}\n"
        f"ATR: {c.atr:.3f}\n\n"


        f"📦 VOLUME\n"
        f"Current: {c.volume:.2f}\n"
        f"Average: {c.vol_avg:.2f}\n"
        f"Ratio: {volume_ratio:.2f}x\n"
        f"Status: {volume_status}\n\n"


        f"⚡ VOLATILITY\n"
        f"ATR: {atr_pct:.2f}%\n"
        f"Status: {volatility}\n\n"


        f"📐 SUPPORT / RESISTANCE\n"
        f"Support: {support:.3f}\n"
        f"Resistance: {resistance:.3f}\n\n"


        f"🕐 MULTI-TIMEFRAME\n"
        f"{mtf_text}\n"


        f"₿ BTC / MARKET CONTEXT\n"
        f"BTC: ${fmt(btc_price, 2)}\n"
        f"BTC 24h: "
        f"{fmt(btc_change, 2)}%\n"
        f"SOL 24h: "
        f"{fmt(sol_change, 2)}%\n"
        f"ETH 24h: "
        f"{fmt(eth_change, 2)}%\n"
        f"BTC Dominance: "
        f"{fmt(btc_dom, 2)}%\n\n"


        f"📊 DERIVATIVES\n"
        f"Funding: {funding_text}\n"
        f"Open Interest: "
        f"{fmt(oi, 2)}\n"
        f"OI 5m Change: "
        f"{fmt(oi_change, 2)}%\n"
        f"Long Liquidations ~1h: "
        f"${long_liq:,.0f}\n"
        f"Short Liquidations ~1h: "
        f"${short_liq:,.0f}\n\n"


        f"⛓ SOLANA NETWORK\n"
        f"Health: {network_health}\n"
        f"Slot: {slot or 'N/A'}\n"
        f"Epoch: {epoch or 'N/A'}\n"
        f"Approx TPS: "
        f"{fmt(tps, 0)}\n\n"


        f"🎯 SIGNAL ENGINE\n"
        f"Current Signal: {signal_text}\n"
        f"LONG Score: "
        f"{scoring['long_score']}\n"
        f"SHORT Score: "
        f"{scoring['short_score']}\n\n"


        f"🟢 LONG FACTORS\n"
    )

    for reason in scoring[
        "long_reasons"
    ][:8]:

        message += (
            f"• {reason}\n"
        )


    message += (
        "\n🔴 SHORT FACTORS\n"
    )

    for reason in scoring[
        "short_reasons"
    ][:8]:

        message += (
            f"• {reason}\n"
        )


    message += (
        "\n📰 RECENT NEWS\n"
        f"{news_text}\n"

        f"⚠️ WARNINGS\n"
        f"{warning_text}\n"

        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Next report: ~10 minutes\n"
        f"Bot uses rule-based market data analysis.\n"
        f"No real orders are placed."
    )


    send(message)


# ============================================================
# SIGNAL ALERT
# ============================================================

def send_signal_alert(
    side,
    df,
    scoring,
    crypto,
    futures
):

    c = df.iloc[-2]

    entry = c.close

    sl, tp1, tp2 = levels(
        side,
        entry,
        c.atr
    )

    emoji = (
        "🟢"
        if side == "LONG"
        else
        "🔴"
    )


    if side == "LONG":

        reasons = scoring[
            "long_reasons"
        ]

    else:

        reasons = scoring[
            "short_reasons"
        ]


    reason_text = ""

    for reason in reasons[:6]:

        reason_text += (
            f"• {reason}\n"
        )


    btc_change = crypto.get(
        "btc_change"
    )

    funding = futures.get(
        "funding"
    )

    if funding is not None:

        funding_text = (
            f"{funding * 100:+.4f}%"
        )

    else:

        funding_text = "N/A"


    message = (

        f"{emoji} {side} SIGNAL\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"

        f"Pair: {SYMBOL}\n"
        f"Timeframe: {TF}\n"
        f"Time: {now_utc()}\n\n"

        f"💰 Entry: {entry:.3f}\n"
        f"🛑 Stop Loss: {sl:.3f}\n"
        f"🎯 TP1: {tp1:.3f}\n"
        f"🎯 TP2: {tp2:.3f}\n\n"

        f"📊 SCORE\n"
        f"LONG: "
        f"{scoring['long_score']}\n"
        f"SHORT: "
        f"{scoring['short_score']}\n\n"

        f"📈 TECHNICAL\n"
        f"RSI: {c.rsi:.1f}\n"
        f"ATR: {c.atr:.3f}\n"
        f"VWAP: {c.vwap:.3f}\n"
        f"Volume: "
        f"{c.volume_ratio:.2f}x average\n\n"

        f"₿ BTC 24h: "
        f"{fmt(btc_change, 2)}%\n"
        f"Funding: "
        f"{funding_text}\n\n"

        f"🧠 CONFIRMATION FACTORS\n"
        f"{reason_text}\n"

        f"⚠️ This is a rule-based market alert, "
        f"not a guaranteed prediction or trade order."
    )


    send(message)


# ============================================================
# TRADE MANAGEMENT
# ============================================================

def check_open_trades(
    df,
    open_trades,
    processed_trade_candles
):

    wins = 0
    losses = 0

    for tr in open_trades[:]:

        for j in range(
            len(df) - 1
        ):

            k = df.iloc[j]

            if k.time <= tr["t"]:
                continue


            trade_key = (
                tr["t"],
                k.time
            )

            if (
                trade_key
                in
                processed_trade_candles
            ):

                continue


            processed_trade_candles.add(
                trade_key
            )


            # ------------------------------------------------
            # LONG
            # ------------------------------------------------

            if tr["side"] == "LONG":

                hit_sl = (
                    k.low <= tr["sl"]
                )

                hit_tp1 = (
                    k.high >= tr["tp1"]
                )

                hit_tp2 = (
                    k.high >= tr["tp2"]
                )


            # ------------------------------------------------
            # SHORT
            # ------------------------------------------------

            else:

                hit_sl = (
                    k.high >= tr["sl"]
                )

                hit_tp1 = (
                    k.low <= tr["tp1"]
                )

                hit_tp2 = (
                    k.low <= tr["tp2"]
                )


            # ------------------------------------------------
            # SL HAS PRIORITY
            # ------------------------------------------------

            if hit_sl:

                losses += 1

                send(
                    f"❌ STOP LOSS HIT\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"Side: {tr['side']}\n"
                    f"Pair: {SYMBOL}\n"
                    f"Entry: {tr['entry']:.3f}\n"
                    f"SL: {tr['sl']:.3f}\n"
                    f"TP1: {tr['tp1']:.3f}\n"
                    f"TP2: {tr['tp2']:.3f}\n\n"
                    f"Result: LOSS\n"
                    f"⚠️ Same-candle SL/TP conflicts "
                    f"are handled conservatively."
                )

                open_trades.remove(
                    tr
                )

                break


            # ------------------------------------------------
            # TP2 = COMPLETE WIN
            # ------------------------------------------------

            if hit_tp2:

                wins += 1

                send(
                    f"✅ TP2 HIT\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"Side: {tr['side']}\n"
                    f"Pair: {SYMBOL}\n"
                    f"Entry: {tr['entry']:.3f}\n"
                    f"TP1: {tr['tp1']:.3f}\n"
                    f"TP2: {tr['tp2']:.3f}\n\n"
                    f"Result: COMPLETE WIN"
                )

                open_trades.remove(
                    tr
                )

                break


            # ------------------------------------------------
            # TP1
            # ------------------------------------------------

            if (
                hit_tp1
                and
                not tr["tp1_hit"]
            ):

                tr["tp1_hit"] = True

                send(
                    f"🎯 TP1 HIT\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"Side: {tr['side']}\n"
                    f"Pair: {SYMBOL}\n"
                    f"Entry: {tr['entry']:.3f}\n"
                    f"TP1: {tr['tp1']:.3f}\n"
                    f"TP2: {tr['tp2']:.3f}\n\n"
                    f"Trade remains open for TP2."
                )


    return wins, losses


# ============================================================
# MAIN
# ============================================================

def main():

    send(
        "🤖 SOL/USDT ADVANCED MARKET BOT STARTED\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "✅ 5m signal monitoring\n"
        "✅ Multi-timeframe analysis\n"
        "✅ BTC market context\n"
        "✅ Funding + Open Interest\n"
        "✅ Liquidation data\n"
        "✅ Solana network data\n"
        "✅ Crypto news\n"
        "✅ 10-minute detailed reports\n"
        "✅ Hypothetical SL/TP tracking\n"
        "❌ No real trades are placed"
    )


    start = time.time()

    last_alert = 0

    last_report = 0

    last_external = 0

    open_trades = []

    processed_trade_candles = set()

    wins = 0

    losses = 0


    # Cached external data
    crypto = {}

    futures = {}

    network = {}

    news = []

    mtf = {}


    print(
        "Advanced SOL bot started"
    )


    while (
        time.time() - start
        <
        RUN_SECONDS
    ):

        try:

            # =================================================
            # MAIN SOL DATA
            # =================================================

            df = add_indicators(
                get_df(
                    SYMBOL,
                    TF,
                    300
                )
            )


            # Last CLOSED candle
            c = df.iloc[-2]

            candle_time = int(
                c.time
            )


            # =================================================
            # EXTERNAL DATA
            # =================================================

            if (
                time.time()
                -
                last_external
                >=
                EXTERNAL_DATA_INTERVAL
            ):

                print(
                    "Updating external market data..."
                )


                crypto = (
                    get_crypto_context()
                )


                futures = (
                    get_binance_futures()
                )


                network = (
                    get_solana_network()
                )


                mtf = (
                    get_multi_timeframe()
                )


                last_external = (
                    time.time()
                )


            # =================================================
            # NEWS
            # =================================================

            news = get_news()


            # =================================================
            # ADVANCED SIGNAL SCORE
            # =================================================

            scoring = advanced_signal(
                df,
                mtf,
                crypto,
                futures
            )


            # =================================================
            # CHECK OPEN TRADES
            # =================================================

            w, l = check_open_trades(
                df,
                open_trades,
                processed_trade_candles
            )

            wins += w

            losses += l


            # =================================================
            # NEW SIGNAL
            # =================================================

            fresh = (
                time.time() * 1000
                -
                (
                    candle_time +
                    TF_MS
                )
            ) < 120_000


            if (
                candle_time !=
                last_alert
                and
                fresh
            ):

                side = scoring[
                    "side"
                ]


                if side:

                    # Avoid stacking many
                    # identical-direction trades.
                    existing_side = any(
                        tr["side"] == side
                        for tr in open_trades
                    )


                    if not existing_side:

                        send_signal_alert(
                            side,
                            df,
                            scoring,
                            crypto,
                            futures
                        )


                        entry = c.close

                        sl, tp1, tp2 = (
                            levels(
                                side,
                                entry,
                                c.atr
                            )
                        )


                        open_trades.append({

                            "t":
                                candle_time,

                            "side":
                                side,

                            "entry":
                                entry,

                            "sl":
                                sl,

                            "tp1":
                                tp1,

                            "tp2":
                                tp2,

                            "tp1_hit":
                                False

                        })


                        last_alert = (
                            candle_time
                        )


            # =================================================
            # 10-MINUTE REPORT
            # =================================================

            now = time.time()


            if (
                now - last_report
                >=
                REPORT_INTERVAL
            ):

                send_market_report(
                    df,
                    mtf,
                    crypto,
                    futures,
                    network,
                    news,
                    scoring
                )


                last_report = now


            # =================================================
            # CONSOLE
            # =================================================

            print(
                datetime.now().strftime(
                    "%H:%M:%S"
                ),
                "| Price:",
                round(c.close, 3),
                "| LONG:",
                scoring["long_score"],
                "| SHORT:",
                scoring["short_score"],
                "| Signal:",
                scoring["side"]
            )


        except Exception:

            traceback.print_exc()


        time.sleep(
            POLL
        )


    # =========================================================
    # FINAL
    # =========================================================

    send(
        "🛑 BOT SESSION FINISHED\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"Session: ~{RUN_SECONDS // 60} minutes\n"
        f"Completed hypothetical wins: {wins}\n"
        f"Completed hypothetical losses: {losses}\n"
        f"Open hypothetical trades: "
        f"{len(open_trades)}\n\n"
        f"⚠️ These are simulated signal outcomes "
        f"based on candle data. No real orders were placed."
    )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    main()
```
