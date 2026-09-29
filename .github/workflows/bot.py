import os, time, ccxt, pandas as pd, requests, traceback

TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
SYMBOL = "SOL/USDT"
TF = "5m"
TF_MS = 5 * 60 * 1000
POLL = 15                      # seconds between checks
RUN_SECONDS = 350 * 60         # stop before GitHub's 6h limit
MIN_ATR_PCT = 0.0025           # skip if volatility too low (fees)
SL_MULT, TP_MULT = 1.0, 1.5

exchange = ccxt.okx({"enableRateLimit": True})   # if it fails: ccxt.kucoin()

def send(msg):
    try:
        r = requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                          data={"chat_id": CHAT_ID, "text": msg}, timeout=15)
        print("Telegram:", r.status_code)
    except Exception:
        traceback.print_exc()

def get_df(limit=300):
    raw = exchange.fetch_ohlcv(SYMBOL, TF, limit=limit)
    return pd.DataFrame(raw, columns=["time", "open", "high", "low", "close", "volume"])

def add_indicators(df):
    df["ema_fast"] = df.close.ewm(span=9, adjust=False).mean()
    df["ema_slow"] = df.close.ewm(span=21, adjust=False).mean()
    df["ema_trend"] = df.close.ewm(span=50, adjust=False).mean()

    delta = df.close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    df["rsi"] = 100 - 100 / (1 + gain / loss)

    tr = pd.concat([df.high - df.low,
                    (df.high - df.close.shift()).abs(),
                    (df.low - df.close.shift()).abs()], axis=1).max(axis=1)
    df["atr"] = tr.ewm(alpha=1/14, adjust=False).mean()

    day = pd.to_datetime(df.time, unit="ms").dt.date
    tp = (df.high + df.low + df.close) / 3
    df["vwap"] = (tp * df.volume).groupby(day).cumsum() / df.volume.groupby(day).cumsum()
    df["vol_avg"] = df.volume.rolling(20).mean()
    return df

def signal_at(df, i):
    prev, c = df.iloc[i-1], df.iloc[i]
    if pd.isna(c.vol_avg) or pd.isna(c.vwap):
        return None
    if c.atr / c.close < MIN_ATR_PCT or c.volume < 1.2 * c.vol_avg:
        return None
    up = prev.ema_fast <= prev.ema_slow and c.ema_fast > c.ema_slow
    down = prev.ema_fast >= prev.ema_slow and c.ema_fast < c.ema_slow
    if up and c.close > c.vwap and c.close > c.ema_trend and 50 < c.rsi < 70:
        return "LONG"
    if down and c.close < c.vwap and c.close < c.ema_trend and 30 < c.rsi < 50:
        return "SHORT"
    return None

def levels(side, entry, atr):
    if side == "LONG":
        return entry - SL_MULT * atr, entry + TP_MULT * atr
    return entry + SL_MULT * atr, entry - TP_MULT * atr

def main():
    send("test ✅")          # <-- TEST LINE: delete this after your phone gets the message
    start = time.time()
    last_alert = 0
    open_trades = []
    wins = losses = 0
    print("Bot started")

    while time.time() - start < RUN_SECONDS:
        try:
            df = add_indicators(get_df())
            c = df.iloc[-2]                         # last CLOSED candle
            t = int(c.time)

            # 1) check open trades on candles after entry
            for tr in open_trades[:]:
                for j in range(len(df) - 1):        # skip unfinished candle
                    k = df.iloc[j]
                    if k.time <= tr["t"]:
                        continue
                    if tr["side"] == "LONG":
                        hit_sl, hit_tp = k.low <= tr["sl"], k.high >= tr["tp"]
                    else:
                        hit_sl, hit_tp = k.high >= tr["sl"], k.low <= tr["tp"]
                    if hit_sl or hit_tp:            # SL first if both (conservative)
                        if hit_sl: losses += 1
                        else: wins += 1
                        send(f"{'❌ SL' if hit_sl else '✅ TP'} hit: {tr['side']} {SYMBOL}\n"
                             f"Entry {tr['entry']:.3f}\nScore: {wins}W / {losses}L")
                        open_trades.remove(tr)
                        break

            # 2) look for a new signal (only fresh candles, no duplicates)
            fresh = (time.time() * 1000 - (t + TF_MS)) < 120_000
            if t != last_alert and fresh:
                side = signal_at(df, len(df) - 2)
                if side:
                    entry = c.close
                    sl, tp = levels(side, entry, c.atr)
                    send(f"{'🟢' if side == 'LONG' else '🔴'} {side} {SYMBOL} ({TF})\n"
                         f"Entry: {entry:.3f}\nStop loss: {sl:.3f}\nTake profit: {tp:.3f}\n"
                         f"RSI: {c.rsi:.1f} | ATR: {c.atr:.3f}")
                    open_trades.append({"t": t, "side": side, "entry": entry, "sl": sl, "tp": tp})
                    last_alert = t
        except Exception:
            traceback.print_exc()
        time.sleep(POLL)

if __name__ == "__main__":
    main()