import os
import time
import ccxt
import pandas as pd
import requests
import traceback

# =========================
# CONFIGURATION
# =========================

TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

SYMBOL = "SOL/USDT"
TF = "5m"
TF_MS = 5 * 60 * 1000

POLL = 15
RUN_SECONDS = 350 * 60

MIN_ATR_PCT = 0.0025

SL_MULT = 1.0
TP_MULT = 1.5

# Send detailed market report every 10 minutes
REPORT_INTERVAL = 10 * 60


# =========================
# EXCHANGE
# =========================

exchange = ccxt.okx({
    "enableRateLimit": True
})


# =========================
# TELEGRAM
# =========================

def send(msg):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            data={
                "chat_id": CHAT_ID,
                "text": msg
            },
            timeout=15
        )

        print("Telegram:", r.status_code)

    except Exception:
        traceback.print_exc()


# =========================
# GET MARKET DATA
# =========================

def get_df(limit=300):

    raw = exchange.fetch_ohlcv(
        SYMBOL,
        TF,
        limit=limit
    )

    return pd.DataFrame(
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


# =========================
# INDICATORS
# =========================

def add_indicators(df):

    # EMA
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


    # RSI
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

    df["rsi"] = 100 - (
        100 / (1 + gain / loss)
    )


    # ATR
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


    # VWAP
    day = pd.to_datetime(
        df.time,
        unit="ms"
    ).dt.date

    tp = (
        df.high +
        df.low +
        df.close
    ) / 3

    df["vwap"] = (
        (tp * df.volume)
        .groupby(day)
        .cumsum()
        /
        df.volume
        .groupby(day)
        .cumsum()
    )


    # Average volume
    df["vol_avg"] = df.volume.rolling(20).mean()


    # Candle percentage
    df["change_pct"] = (
        (df.close - df.open)
        / df.open
    ) * 100


    return df


# =========================
# SIGNAL
# =========================

def signal_at(df, i):

    prev = df.iloc[i - 1]
    c = df.iloc[i]

    if pd.isna(c.vol_avg) or pd.isna(c.vwap):
        return None

    # Volatility filter
    if c.atr / c.close < MIN_ATR_PCT:
        return None

    # Volume filter
    if c.volume < 1.2 * c.vol_avg:
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


    # LONG
    if (
        up
        and c.close > c.vwap
        and c.close > c.ema_trend
        and 50 < c.rsi < 70
    ):
        return "LONG"


    # SHORT
    if (
        down
        and c.close < c.vwap
        and c.close < c.ema_trend
        and 30 < c.rsi < 50
    ):
        return "SHORT"


    return None


# =========================
# SL / TP
# =========================

def levels(side, entry, atr):

    if side == "LONG":

        sl = entry - SL_MULT * atr
        tp = entry + TP_MULT * atr

    else:

        sl = entry + SL_MULT * atr
        tp = entry - TP_MULT * atr

    return sl, tp


# =========================
# MARKET CONDITION
# =========================

def get_market_condition(c):

    price = c.close
    ema9 = c.ema_fast
    ema21 = c.ema_slow
    ema50 = c.ema_trend
    vwap = c.vwap
    rsi = c.rsi


    # Strong bullish
    if (
        price > ema9 > ema21 > ema50
        and price > vwap
        and rsi >= 55
    ):
        return "🟢 STRONG BULLISH"


    # Bullish
    if (
        price > ema21
        and price > ema50
        and price > vwap
    ):
        return "🟢 BULLISH"


    # Strong bearish
    if (
        price < ema9 < ema21 < ema50
        and price < vwap
        and rsi <= 45
    ):
        return "🔴 STRONG BEARISH"


    # Bearish
    if (
        price < ema21
        and price < ema50
        and price < vwap
    ):
        return "🔴 BEARISH"


    return "🟡 NEUTRAL / SIDEWAYS"


# =========================
# SUPPORT / RESISTANCE
# =========================

def get_support_resistance(df):

    recent = df.iloc[-50:-1]

    support = recent.low.min()
    resistance = recent.high.max()

    return support, resistance


# =========================
# DETAILED 10-MINUTE REPORT
# =========================

def send_market_report(df):

    c = df.iloc[-2]

    price = c.close

    condition = get_market_condition(c)

    support, resistance = get_support_resistance(df)


    # Volume status
    volume_ratio = c.volume / c.vol_avg

    if volume_ratio >= 1.5:
        volume_status = "🔥 Very High"
    elif volume_ratio >= 1.2:
        volume_status = "🟢 High"
    elif volume_ratio >= 0.8:
        volume_status = "🟡 Normal"
    else:
        volume_status = "🔴 Low"


    # Volatility
    atr_pct = (c.atr / price) * 100

    if atr_pct >= 1.0:
        volatility = "🔥 High"
    elif atr_pct >= 0.5:
        volatility = "🟢 Moderate"
    else:
        volatility = "🟡 Low"


    # EMA relationship
    if c.ema_fast > c.ema_slow:
        ema_signal = "Bullish (EMA 9 > EMA 21)"
    elif c.ema_fast < c.ema_slow:
        ema_signal = "Bearish (EMA 9 < EMA 21)"
    else:
        ema_signal = "Neutral"


    # RSI interpretation
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


    # Candle
    if c.close > c.open:
        candle = "🟢 Bullish candle"
    elif c.close < c.open:
        candle = "🔴 Bearish candle"
    else:
        candle = "🟡 Doji / Neutral"


    # Current strategy signal
    signal = signal_at(df, len(df) - 2)

    if signal == "LONG":
        signal_text = "🟢 LONG setup detected"

    elif signal == "SHORT":
        signal_text = "🔴 SHORT setup detected"

    else:
        signal_text = "⚪ No confirmed entry"


    message = (
        f"📊 SOLANA MARKET REPORT\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Pair: {SYMBOL}\n"
        f"Timeframe: {TF}\n\n"

        f"💰 Price: {price:.3f}\n"
        f"📈 Market: {condition}\n"
        f"🕯 Candle: {candle}\n\n"

        f"📊 INDICATORS\n"
        f"EMA 9: {c.ema_fast:.3f}\n"
        f"EMA 21: {c.ema_slow:.3f}\n"
        f"EMA 50: {c.ema_trend:.3f}\n"
        f"VWAP: {c.vwap:.3f}\n"
        f"RSI: {c.rsi:.1f} ({rsi_status})\n"
        f"ATR: {c.atr:.3f}\n\n"

        f"📦 VOLUME\n"
        f"Current: {c.volume:.2f}\n"
        f"Average: {c.vol_avg:.2f}\n"
        f"Ratio: {volume_ratio:.2f}x\n"
        f"Status: {volume_status}\n\n"

        f"⚡ VOLATILITY\n"
        f"ATR %: {atr_pct:.2f}%\n"
        f"Status: {volatility}\n\n"

        f"📐 LEVELS\n"
        f"Support: {support:.3f}\n"
        f"Resistance: {resistance:.3f}\n\n"

        f"🔀 EMA STATUS\n"
        f"{ema_signal}\n\n"

        f"🎯 STRATEGY\n"
        f"{signal_text}\n\n"

        f"⏱ Next report: 10 minutes"
    )


    send(message)


# =========================
# MAIN
# =========================

def main():

    # Test Telegram
    send("🤖 SOL/USDT bot started successfully!")

    start = time.time()

    last_alert = 0
    last_report = 0

    open_trades = []

    wins = 0
    losses = 0

    # Track which candles have already been processed
    processed_trade_candles = set()


    print("Bot started")


    while time.time() - start < RUN_SECONDS:

        try:

            # Get market data
            df = add_indicators(
                get_df()
            )


            # Last CLOSED candle
            c = df.iloc[-2]

            t = int(c.time)


            # =================================
            # 1. CHECK OPEN TRADES
            # =================================

            for tr in open_trades[:]:

                for j in range(len(df) - 1):

                    k = df.iloc[j]

                    # Only candles after entry
                    if k.time <= tr["t"]:
                        continue

                    # Already checked candle
                    trade_key = (
                        tr["t"],
                        k.time
                    )

                    if trade_key in processed_trade_candles:
                        continue

                    processed_trade_candles.add(
                        trade_key
                    )


                    if tr["side"] == "LONG":

                        hit_sl = k.low <= tr["sl"]
                        hit_tp = k.high >= tr["tp"]

                    else:

                        hit_sl = k.high >= tr["sl"]
                        hit_tp = k.low <= tr["tp"]


                    if hit_sl or hit_tp:

                        # Conservative assumption:
                        # if both happen in same candle,
                        # count SL first

                        if hit_sl:

                            losses += 1

                            result = "❌ SL"

                        else:

                            wins += 1

                            result = "✅ TP"


                        send(
                            f"{result} HIT\n"
                            f"━━━━━━━━━━━━━━━━━━\n"
                            f"Side: {tr['side']}\n"
                            f"Pair: {SYMBOL}\n"
                            f"Entry: {tr['entry']:.3f}\n"
                            f"SL: {tr['sl']:.3f}\n"
                            f"TP: {tr['tp']:.3f}\n\n"
                            f"Score: {wins}W / {losses}L"
                        )


                        open_trades.remove(tr)

                        break


            # =================================
            # 2. NEW SIGNAL
            # =================================

            fresh = (
                time.time() * 1000
                -
                (t + TF_MS)
            ) < 120_000


            if (
                t != last_alert
                and fresh
            ):

                side = signal_at(
                    df,
                    len(df) - 2
                )


                if side:

                    entry = c.close

                    sl, tp = levels(
                        side,
                        entry,
                        c.atr
                    )


                    emoji = (
                        "🟢"
                        if side == "LONG"
                        else "🔴"
                    )


                    send(
                        f"{emoji} {side} SIGNAL\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"Pair: {SYMBOL}\n"
                        f"Timeframe: {TF}\n\n"
                        f"Entry: {entry:.3f}\n"
                        f"Stop Loss: {sl:.3f}\n"
                        f"Take Profit: {tp:.3f}\n\n"
                        f"RSI: {c.rsi:.1f}\n"
                        f"ATR: {c.atr:.3f}\n"
                        f"VWAP: {c.vwap:.3f}\n\n"
                        f"Risk/Reward: 1 : 1.5"
                    )


                    open_trades.append({

                        "t": t,
                        "side": side,
                        "entry": entry,
                        "sl": sl,
                        "tp": tp

                    })


                    last_alert = t


            # =================================
            # 3. DETAILED REPORT EVERY 10 MIN
            # =================================

            now = time.time()


            if now - last_report >= REPORT_INTERVAL:

                send_market_report(df)

                last_report = now


        except Exception:

            traceback.print_exc()


        time.sleep(POLL)


# =========================
# START
# =========================

if __name__ == "__main__":
    main()
