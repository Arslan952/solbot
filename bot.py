import os
import json
import time
import traceback
from datetime import datetime, timezone

import ccxt
import numpy as np
import pandas as pd
import requests


# ============================================================
# CONFIGURATION  (SIGNALS ONLY - the bot never opens trades)
# ============================================================

TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

STATE_FILE = os.environ.get("STATE_FILE", "bot_state.json")

SYMBOL = "SOL/USDT"
ASSETS = {"SOL": "SOL/USDT", "BTC": "BTC/USDT", "ETH": "ETH/USDT"}

TF = "5m"
TF_MS = 5 * 60 * 1000
POLL = 15
RUN_SECONDS = 350 * 60            # GitHub Actions safe runtime
EXTERNAL_DATA_INTERVAL = 60
FRESH_MS = 120_000                # only alert on candles closed < 2 min ago

HOURLY_REPORT_INTERVAL = 60 * 60
DAILY_REPORT_INTERVAL = 24 * 60 * 60

# ---- Suggested position size shown in alerts (information only) ----
ACCOUNT_SIZE = 100.0              # your real account size, USDT
RISK_PCT = 0.005                  # 0.5% = 0.50 USDT risk per trade
LEVERAGE = 5                      # isolated margin
MAX_MARGIN_PER_TRADE = 20.0       # 2 trades x 20 = 40 USDT max, 60 stays free
FEE_RATE = 0.0005                 # 0.05% taker per side (for size estimate)

# ---- Global filters ----
MIN_ATR_PCT = 0.0025              # 0.25% ATR / price
MIN_VOLUME_RATIO = 0.8
BTC_CRASH_1H = -1.5               # % : block SOL LONG signals
BTC_PUMP_1H = 1.5                 # % : block SOL SHORT signals
MAX_SPREAD_PCT = 0.05
FUNDING_WARN = 0.0005             # 0.05%

# ---- Strategy settings (R = stop distance) ----
STRATEGIES = {
    "EMA_PULLBACK": {
        "label": "EMA Pullback",
        "min_vr": 1.0, "tp1_r": 1.2, "tp2_r": 2.2,
        "sl_min_atr": 0.8, "sl_max_atr": 2.0,
    },
    "VWAP_BREAKOUT": {
        "label": "VWAP Breakout",
        "min_vr": 1.5, "tp1_r": 1.5, "tp2_r": 2.5,
        "sl_min_atr": 0.8, "sl_max_atr": 2.0,
    },
    "RSI_REVERSAL": {
        "label": "RSI S/R Reversal",
        "min_vr": 0.9, "tp1_r": 1.0, "tp2_r": 2.0,
        "sl_min_atr": 0.8, "sl_max_atr": 2.0,
    },
}


# ============================================================
# EXCHANGE / HTTP
# ============================================================

exchange = ccxt.okx({"enableRateLimit": True, "timeout": 15000})

session = requests.Session()
session.headers.update({"User-Agent": "SOL-Signal-Bot/3.0"})


# ============================================================
# TELEGRAM
# ============================================================

def send(msg):
    if not TOKEN or not CHAT_ID:
        print("ERROR: TELEGRAM_TOKEN or TELEGRAM_CHAT_ID is missing.")
        return False

    ok = True
    chunks = [msg[i:i + 3900] for i in range(0, len(msg), 3900)] or [""]

    for chunk in chunks:
        try:
            r = session.post(
                f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                data={"chat_id": CHAT_ID, "text": chunk},
                timeout=15,
            )
            print("Telegram:", r.status_code)
            if not r.ok:
                print("Telegram response:", r.text[:300])
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


def fmt_pct(value, decimals=2):
    if value is None:
        return "N/A"
    return f"{value:+.{decimals}f}%"


def pct(new, old):
    if old in (None, 0) or new is None:
        return None
    return (new - old) / old * 100


def now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def label(strategy):
    return STRATEGIES.get(strategy, {}).get("label", strategy)


def side_emoji(side):
    return "🟢" if side == "LONG" else "🔴"


# ============================================================
# PERSISTENT STATE (signal history only)
# ============================================================

def default_state():
    return {
        "active": [],            # signals still being tracked
        "closed": [],            # finished signals
        "signals": [],           # log of every strategy signal
        "counter": 0,
        "last_eval_candle": 0,
        "last_hourly": 0,
        "last_daily_report": 0,
    }


def load_state():
    state = default_state()
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r") as f:
                state.update(json.load(f))
            print("State loaded:", STATE_FILE)
    except Exception:
        traceback.print_exc()
        print("Could not load state, starting fresh.")
    return state


def save_state(state):
    try:
        state["signals"] = state["signals"][-3000:]
        state["closed"] = state["closed"][-1500:]
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, STATE_FILE)
    except Exception:
        traceback.print_exc()


# ============================================================
# OHLCV + INDICATORS
# ============================================================

def get_df(symbol=SYMBOL, timeframe=TF, limit=300):
    raw = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
    return pd.DataFrame(
        raw, columns=["time", "open", "high", "low", "close", "volume"]
    )


def add_indicators(df):
    df = df.copy()

    df["ema_fast"] = df["close"].ewm(span=9, adjust=False).mean()
    df["ema_slow"] = df["close"].ewm(span=21, adjust=False).mean()
    df["ema_trend"] = df["close"].ewm(span=50, adjust=False).mean()

    delta = df["close"].diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))

    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - df["close"].shift()).abs(),
            (df["low"] - df["close"].shift()).abs(),
        ],
        axis=1,
    ).max(axis=1)
    df["atr"] = tr.ewm(alpha=1 / 14, adjust=False).mean()

    day = pd.to_datetime(df["time"], unit="ms").dt.date
    tp = (df["high"] + df["low"] + df["close"]) / 3
    cum_vol = df["volume"].groupby(day).cumsum()
    cum_pv = (tp * df["volume"]).groupby(day).cumsum()
    df["vwap"] = cum_pv / cum_vol.replace(0, np.nan)

    df["vol_avg"] = df["volume"].rolling(20).mean()
    df["volume_ratio"] = df["volume"] / df["vol_avg"].replace(0, np.nan)

    return df


def timeframe_status(df):
    if df is None or len(df) < 60:
        return "UNAVAILABLE"

    c = df.iloc[-2]
    price = c["close"]

    if price > c["ema_fast"] > c["ema_slow"] > c["ema_trend"] and price > c["vwap"]:
        return "BULLISH"
    if price < c["ema_fast"] < c["ema_slow"] < c["ema_trend"] and price < c["vwap"]:
        return "BEARISH"
    if price > c["ema_slow"] and price > c["ema_trend"]:
        return "WEAK BULLISH"
    if price < c["ema_slow"] and price < c["ema_trend"]:
        return "WEAK BEARISH"
    return "NEUTRAL"


# ============================================================
# EXTERNAL DATA
# ============================================================

def get_market_context():
    ctx = {}

    for name, sym in ASSETS.items():
        try:
            d5 = add_indicators(get_df(sym, "5m", 300))
            d1h = add_indicators(get_df(sym, "1h", 100))

            price = float(d5.iloc[-1]["close"])
            ch1h = pct(price, float(d5.iloc[-13]["close"])) if len(d5) >= 13 else None
            ch24h = pct(price, float(d5.iloc[-289]["close"])) if len(d5) >= 289 else None

            ctx[name] = {
                "price": price,
                "ch1h": ch1h,
                "ch24h": ch24h,
                "trend_5m": timeframe_status(d5),
                "trend_1h": timeframe_status(d1h),
            }
        except Exception as e:
            print(f"Context error {name}:", e)
            ctx[name] = {}

    return ctx


def get_binance_futures():
    result = {
        "funding": None,
        "open_interest": None,
        "oi_change_pct": None,
        "long_liquidation": 0.0,
        "short_liquidation": 0.0,
    }
    symbol = "SOLUSDT"

    try:
        r = session.get(
            "https://fapi.binance.com/fapi/v1/premiumIndex",
            params={"symbol": symbol}, timeout=10,
        )
        result["funding"] = safe_float(r.json().get("lastFundingRate"))
    except Exception as e:
        print("Funding error:", e)

    try:
        r = session.get(
            "https://fapi.binance.com/fapi/v1/openInterest",
            params={"symbol": symbol}, timeout=10,
        )
        result["open_interest"] = safe_float(r.json().get("openInterest"))
    except Exception as e:
        print("Open interest error:", e)

    try:
        r = session.get(
            "https://fapi.binance.com/futures/data/openInterestHist",
            params={"symbol": symbol, "period": "5m", "limit": 2}, timeout=10,
        )
        data = r.json()
        if isinstance(data, list) and len(data) >= 2:
            old_oi = safe_float(data[-2].get("sumOpenInterest"))
            new_oi = safe_float(data[-1].get("sumOpenInterest"))
            if old_oi and new_oi:
                result["oi_change_pct"] = (new_oi - old_oi) / old_oi * 100
    except Exception as e:
        print("OI history error:", e)

    try:
        r = session.get(
            "https://fapi.binance.com/fapi/v1/allForceOrders",
            params={"symbol": symbol, "limit": 100}, timeout=10,
        )
        data = r.json()
        if isinstance(data, list):
            one_hour_ago = int(time.time() * 1000) - 3600 * 1000
            for order in data:
                if order.get("time", 0) < one_hour_ago:
                    continue
                value = safe_float(order.get("origQty"), 0) * safe_float(order.get("price"), 0)
                if order.get("side") == "SELL":
                    result["long_liquidation"] += value
                elif order.get("side") == "BUY":
                    result["short_liquidation"] += value
    except Exception as e:
        print("Liquidation error:", e)

    return result


def get_spread_pct():
    try:
        t = exchange.fetch_ticker(SYMBOL)
        bid, ask = t.get("bid"), t.get("ask")
        if bid and ask:
            return (ask - bid) / ((ask + bid) / 2) * 100
    except Exception as e:
        print("Spread error:", e)
    return None


# ============================================================
# STRATEGIES
# (written once for LONG; SHORT uses "signed" mirrored prices)
# ============================================================

INDICATOR_COLS = [
    "ema_fast", "ema_slow", "ema_trend", "rsi",
    "atr", "vwap", "vol_avg", "volume_ratio",
]


def row_ok(row):
    return not any(pd.isna(row[k]) for k in INDICATOR_COLS)


def signed(row, d):
    if d == 1:
        return {
            "close": row["close"], "open": row["open"],
            "high": row["high"], "low": row["low"],
            "ema_fast": row["ema_fast"], "ema_slow": row["ema_slow"],
            "ema_trend": row["ema_trend"], "vwap": row["vwap"],
            "rsi": row["rsi"], "atr": row["atr"], "vr": row["volume_ratio"],
        }
    return {
        "close": -row["close"], "open": -row["open"],
        "high": -row["low"], "low": -row["high"],
        "ema_fast": -row["ema_fast"], "ema_slow": -row["ema_slow"],
        "ema_trend": -row["ema_trend"], "vwap": -row["vwap"],
        "rsi": 100 - row["rsi"], "atr": row["atr"], "vr": row["volume_ratio"],
    }


def strat_ema_pullback(df, d):
    c, p, old = (signed(df.iloc[-2], d), signed(df.iloc[-3], d), signed(df.iloc[-8], d))

    if not (
        c["ema_fast"] > c["ema_slow"] > c["ema_trend"]
        and c["close"] > c["ema_trend"]
        and c["ema_slow"] > old["ema_slow"]
        and c["close"] > c["vwap"]
    ):
        return None

    touched = min(c["low"], p["low"]) <= c["ema_slow"] + 0.25 * c["atr"]
    held = c["close"] > c["ema_slow"]
    resumed = (
        c["close"] > c["open"]
        and c["close"] > c["ema_fast"]
        and c["close"] > p["close"]
        and p["close"] <= p["ema_fast"] + 0.1 * p["atr"]
    )

    if not (touched and held and resumed and 42 < c["rsi"] < 66):
        return None

    return {
        "struct_sl": min(c["low"], p["low"]) - 0.2 * c["atr"],
        "reasons": [
            "Trend aligned: EMA 9 > 21 > 50, price on the right side of VWAP",
            "Pullback touched EMA 21 zone and held",
            "Resume candle closed beyond EMA 9",
            f"RSI healthy ({df.iloc[-2]['rsi']:.1f})",
        ],
    }


def strat_vwap_breakout(df, d):
    c, p, pp = (signed(df.iloc[-2], d), signed(df.iloc[-3], d), signed(df.iloc[-4], d))

    crossed = (
        (p["close"] <= p["vwap"] or pp["close"] <= pp["vwap"])
        and c["close"] - c["vwap"] > 0.1 * c["atr"]
    )

    rng = c["high"] - c["low"]
    body = c["close"] - c["open"]
    strong = (
        rng > 0
        and body > 0
        and body >= 0.4 * rng
        and (c["high"] - c["close"]) <= 0.3 * rng
    )

    if not (
        crossed
        and strong
        and c["vr"] >= STRATEGIES["VWAP_BREAKOUT"]["min_vr"]
        and c["close"] > c["ema_trend"]
        and 50 < c["rsi"] < 72
    ):
        return None

    return {
        "struct_sl": min(c["low"], c["vwap"] - 0.3 * c["atr"]) - 0.1 * c["atr"],
        "reasons": [
            "Fresh VWAP break with strong candle body",
            f"Volume spike ({c['vr']:.2f}x average)",
            "Price beyond EMA 50",
            f"RSI momentum ({df.iloc[-2]['rsi']:.1f})",
        ],
    }


def strat_rsi_reversal(df, d):
    c, p, pp = (signed(df.iloc[-2], d), signed(df.iloc[-3], d), signed(df.iloc[-4], d))

    lookback = df.iloc[-52:-4]
    if len(lookback) < 30:
        return None

    level = lookback["low"].min() if d == 1 else -lookback["high"].max()

    touched = min(c["low"], p["low"]) <= level + 0.3 * c["atr"]
    not_broken = min(c["low"], p["low"]) >= level - 1.0 * c["atr"] and c["close"] > level

    rsi_washed = min(p["rsi"], pp["rsi"]) <= 33
    rsi_turning = c["rsi"] > p["rsi"] and c["rsi"] < 50
    reversal = c["close"] > c["open"] and c["close"] > p["close"]

    if not (
        touched and not_broken and rsi_washed and rsi_turning and reversal
        and c["vr"] >= STRATEGIES["RSI_REVERSAL"]["min_vr"]
    ):
        return None

    return {
        "struct_sl": min(min(c["low"], p["low"]) - 0.2 * c["atr"], level - 0.2 * c["atr"]),
        "reasons": [
            "Price tested key 50-candle support/resistance and held",
            "RSI reached extreme zone and is turning back",
            "Reversal candle confirmed",
            f"Volume {c['vr']:.2f}x average",
        ],
    }


STRATEGY_FUNCS = [
    ("EMA_PULLBACK", strat_ema_pullback),
    ("VWAP_BREAKOUT", strat_vwap_breakout),
    ("RSI_REVERSAL", strat_rsi_reversal),
]


def build_levels(side, entry, atr, struct_sl_signed, cfg):
    d = 1 if side == "LONG" else -1
    dist_struct = d * entry - struct_sl_signed
    dist = min(max(dist_struct, cfg["sl_min_atr"] * atr), cfg["sl_max_atr"] * atr)

    sl = entry - d * dist
    tp1 = entry + d * dist * cfg["tp1_r"]
    tp2 = entry + d * dist * cfg["tp2_r"]
    return sl, tp1, tp2


def run_strategies(df):
    c = df.iloc[-2]
    signals = []

    if not all(row_ok(df.iloc[i]) for i in (-2, -3, -4, -8)):
        return signals

    if c["atr"] / c["close"] < MIN_ATR_PCT:
        return signals
    if c["volume_ratio"] < MIN_VOLUME_RATIO:
        return signals

    entry = float(c["close"])
    atr = float(c["atr"])

    for name, func in STRATEGY_FUNCS:
        for d, side in ((1, "LONG"), (-1, "SHORT")):
            try:
                res = func(df, d)
            except Exception:
                traceback.print_exc()
                res = None

            if not res:
                continue

            sl, tp1, tp2 = build_levels(side, entry, atr, res["struct_sl"], STRATEGIES[name])

            signals.append({
                "strategy": name,
                "side": side,
                "entry": entry,
                "sl": sl,
                "tp1": tp1,
                "tp2": tp2,
                "atr": atr,
                "rsi": float(c["rsi"]),
                "vr": float(c["volume_ratio"]),
                "reasons": res["reasons"],
            })

    return signals


# ============================================================
# SUGGESTED SIZE (information only - nothing is executed)
# ============================================================

def suggest_size(entry, sl, side):
    risk = ACCOUNT_SIZE * RISK_PCT
    stop_pct = abs(entry - sl) / entry
    effective = stop_pct + 2 * FEE_RATE

    notional = risk / effective
    margin = notional / LEVERAGE

    capped = False
    if margin > MAX_MARGIN_PER_TRADE:
        margin = MAX_MARGIN_PER_TRADE
        notional = margin * LEVERAGE
        capped = True

    qty = round(notional / entry, 2)
    notional = qty * entry
    margin = notional / LEVERAGE
    max_loss = notional * effective

    liq = entry * (1 - 1 / LEVERAGE) if side == "LONG" else entry * (1 + 1 / LEVERAGE)

    return {
        "qty": qty,
        "notional": notional,
        "margin": margin,
        "max_loss": max_loss,
        "stop_pct": stop_pct * 100,
        "liq": liq,
        "capped": capped,
    }


# ============================================================
# FILTERS + WARNINGS
# ============================================================

def blocked_by_filter(side, ctx):
    btc_1h = (ctx.get("BTC") or {}).get("ch1h")
    if btc_1h is None:
        return None
    if side == "LONG" and btc_1h <= BTC_CRASH_1H:
        return f"BTC crash filter (BTC 1h {btc_1h:+.2f}%)"
    if side == "SHORT" and btc_1h >= BTC_PUMP_1H:
        return f"BTC pump filter (BTC 1h {btc_1h:+.2f}%)"
    return None


def get_signal_warnings(side, futures, spread_pct, ctx):
    warnings = []

    if spread_pct is not None and spread_pct > MAX_SPREAD_PCT:
        warnings.append(f"Wide spread ({spread_pct:.3f}%) - slippage risk")

    funding = futures.get("funding")
    if funding is not None:
        if side == "LONG" and funding > FUNDING_WARN:
            warnings.append(f"High positive funding ({funding * 100:+.4f}%) - crowded longs")
        if side == "SHORT" and funding < -FUNDING_WARN:
            warnings.append(f"High negative funding ({funding * 100:+.4f}%) - crowded shorts")

    long_liq = futures.get("long_liquidation", 0)
    short_liq = futures.get("short_liquidation", 0)
    if long_liq > 1_000_000 or short_liq > 1_000_000:
        warnings.append(f"Heavy liquidations ~1h (L ${long_liq:,.0f} / S ${short_liq:,.0f})")

    sol_1h = (ctx.get("SOL") or {}).get("trend_1h", "")
    if side == "LONG" and "BEARISH" in sol_1h:
        warnings.append(f"Against SOL 1h trend ({sol_1h})")
    if side == "SHORT" and "BULLISH" in sol_1h:
        warnings.append(f"Against SOL 1h trend ({sol_1h})")

    return warnings


# ============================================================
# SIGNAL OUTCOME TRACKING (statistics only, no money)
# ============================================================

def log_signal(state, sig, candle_time, action):
    state["signals"].append({
        "ts": time.time(),
        "candle": candle_time,
        "strategy": sig["strategy"],
        "side": sig["side"],
        "action": action,
    })


def close_signal(state, s, reason):
    state["closed"].append({
        "id": s["id"],
        "strategy": s["strategy"],
        "side": s["side"],
        "reason": reason,           # SL / TP2 / BE
        "tp1_hit": s["tp1_hit"],
        "closed_ts": time.time(),
    })
    if s in state["active"]:
        state["active"].remove(s)

    titles = {
        "SL": "❌ SIGNAL STOP LOSS HIT",
        "TP2": "✅ SIGNAL TP2 HIT",
        "BE": "🔒 SIGNAL CLOSED AT BREAKEVEN (after TP1)",
    }
    send(
        f"{titles[reason]}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"#{s['id']} {s['side']} | {label(s['strategy'])}\n"
        f"Entry: {s['entry']:.3f}\n"
        f"SL: {s['sl']:.3f} | TP1: {s['tp1']:.3f} | TP2: {s['tp2']:.3f}\n"
        f"TP1 hit: {'Yes' if s['tp1_hit'] else 'No'}\n\n"
        f"Tracking only - no real trade."
    )


def check_active_signals(state, df):
    """Same-candle SL/TP conflicts count as SL (conservative)."""
    closed_df = df.iloc[:-1]

    for s in state["active"][:]:
        d = 1 if s["side"] == "LONG" else -1
        new_candles = closed_df[closed_df["time"] > s["last_checked"]]

        for _, candle in new_candles.iterrows():
            fav = float(candle["high"]) if d == 1 else float(candle["low"])
            adv = float(candle["low"]) if d == 1 else float(candle["high"])
            s["last_checked"] = int(candle["time"])

            sl_now = s["entry"] if s["tp1_hit"] else s["sl"]

            if adv * d <= sl_now * d:
                close_signal(state, s, "BE" if s["tp1_hit"] else "SL")
                break

            if fav * d >= s["tp2"] * d:
                s["tp1_hit"] = True
                close_signal(state, s, "TP2")
                break

            if not s["tp1_hit"] and fav * d >= s["tp1"] * d:
                s["tp1_hit"] = True
                send(
                    f"🎯 SIGNAL TP1 HIT\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"#{s['id']} {s['side']} | {label(s['strategy'])}\n"
                    f"TP1: {s['tp1']:.3f}\n"
                    f"Suggestion: take partial profit and move SL to entry ({s['entry']:.3f}).\n"
                    f"Next target TP2: {s['tp2']:.3f}\n"
                    f"Tracking only - no real trade."
                )


# ============================================================
# SIGNAL ALERTS
# ============================================================

def send_signal_alert(state, sig, group, candle_time, ctx, futures, opposite_active):
    side = sig["side"]

    state["counter"] += 1
    sid = state["counter"]
    others = [g["strategy"] for g in group if g is not sig]

    state["active"].append({
        "id": sid,
        "strategy": sig["strategy"],
        "confirmations": others,
        "side": side,
        "entry": sig["entry"],
        "sl": sig["sl"],
        "tp1": sig["tp1"],
        "tp2": sig["tp2"],
        "atr": sig["atr"],
        "tp1_hit": False,
        "opened_ts": time.time(),
        "last_checked": candle_time,
    })

    stop_dist = abs(sig["entry"] - sig["sl"])
    rr1 = abs(sig["tp1"] - sig["entry"]) / stop_dist
    rr2 = abs(sig["tp2"] - sig["entry"]) / stop_dist

    size = suggest_size(sig["entry"], sig["sl"], side)

    spread = get_spread_pct()
    warnings = get_signal_warnings(side, futures, spread, ctx)
    if opposite_active:
        warnings.append("Opposite-direction signal still active (possible reversal setup)")
    if size["capped"]:
        warnings.append(
            f"Tight stop: size capped at {MAX_MARGIN_PER_TRADE:.0f} USDT margin, so real risk is below {RISK_PCT * 100:.1f}%"
        )

    reasons = "\n".join(f"• {r}" for r in sig["reasons"])
    conf_text = ""
    if others:
        conf_text = "\n✅ Also confirmed by: " + ", ".join(label(x) for x in others)

    warn_text = ""
    if warnings:
        warn_text = "\n\n⚠️ WARNINGS\n" + "\n".join(f"• {w}" for w in warnings)

    funding = futures.get("funding")
    deriv = (
        f"Funding {fmt(None if funding is None else funding * 100, 4)}% | "
        f"OI 5m {fmt_pct(futures.get('oi_change_pct'))} | "
        f"Liq 1h L/S ${futures.get('long_liquidation', 0):,.0f}/${futures.get('short_liquidation', 0):,.0f}"
    )
    btc = ctx.get("BTC") or {}

    send(
        f"{side_emoji(side)} {side} SIGNAL - {label(sig['strategy'])}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Pair: {SYMBOL} | TF: {TF}\n"
        f"Time: {now_utc()}\n"
        f"Signal ID: #{sid}\n\n"
        f"💰 Entry: {sig['entry']:.3f}\n"
        f"🛑 Stop Loss: {sig['sl']:.3f}  ({size['stop_pct']:.2f}% away)\n"
        f"🎯 TP1: {sig['tp1']:.3f}  (R:R 1:{rr1:.2f})\n"
        f"🎯 TP2: {sig['tp2']:.3f}  (R:R 1:{rr2:.2f})\n"
        f"↪ At TP1: take partial profit, move SL to entry\n\n"
        f"📐 SUGGESTED SIZE for {ACCOUNT_SIZE:.0f} USDT account\n"
        f"Mode: ISOLATED | Leverage: {LEVERAGE}x\n"
        f"Position: {size['qty']:.2f} SOL (≈ {size['notional']:.2f} USDT)\n"
        f"Margin: {size['margin']:.2f} USDT\n"
        f"Est. max loss at SL: {size['max_loss']:.2f} USDT\n"
        f"Approx. liquidation: {size['liq']:.3f}\n\n"
        f"📈 RSI {sig['rsi']:.1f} | Volume {sig['vr']:.2f}x | BTC 1h {fmt_pct(btc.get('ch1h'))}\n"
        f"📊 {deriv}\n\n"
        f"🧠 WHY\n{reasons}{conf_text}{warn_text}\n\n"
        f"⚠️ Signal only. The bot places NO orders."
    )


def handle_signals(state, signals, candle_time, ctx, futures):
    if not signals:
        return

    longs = [s for s in signals if s["side"] == "LONG"]
    shorts = [s for s in signals if s["side"] == "SHORT"]

    if longs and shorts:
        for s in signals:
            log_signal(state, s, candle_time, "CONFLICT")
        send(
            "⚪ CONFLICTING SIGNALS (ignored)\n"
            f"LONG: {', '.join(label(s['strategy']) for s in longs)}\n"
            f"SHORT: {', '.join(label(s['strategy']) for s in shorts)}"
        )
        return

    group = longs or shorts
    side = group[0]["side"]
    primary = group[0]

    reason = blocked_by_filter(side, ctx)
    if reason:
        for s in group:
            log_signal(state, s, candle_time, f"FILTERED: {reason}")
        send(
            f"⛔ {side} signal filtered - {label(primary['strategy'])}\n"
            f"Reason: {reason}\nPrice: {primary['entry']:.3f}"
        )
        return

    same_side = [a for a in state["active"] if a["side"] == side]
    if same_side:
        active = same_side[0]
        for s in group:
            log_signal(state, s, candle_time, "CONFIRMATION")
            if s["strategy"] != active["strategy"] and s["strategy"] not in active["confirmations"]:
                active["confirmations"].append(s["strategy"])
        send(
            f"➕ {side} CONFIRMATION\n"
            f"{', '.join(label(s['strategy']) for s in group)} agree with active signal "
            f"#{active['id']} ({label(active['strategy'])}). No new signal."
        )
        return

    opposite_active = any(a["side"] != side for a in state["active"])
    for s in group:
        log_signal(state, s, candle_time, "SIGNAL" if s is primary else "CONFIRMATION")

    send_signal_alert(state, primary, group, candle_time, ctx, futures, opposite_active)


# ============================================================
# REPORTS
# ============================================================

def send_hourly_report(state, ctx, futures):
    lines = ["🕐 HOURLY MARKET REPORT", "━━━━━━━━━━━━━━━━━━━━", now_utc(), ""]

    for name in ("SOL", "BTC", "ETH"):
        a = ctx.get(name) or {}
        if not a:
            lines.append(f"{name}: N/A")
            continue
        lines.append(
            f"{name}: ${a['price']:,.3f}\n"
            f"   1h {fmt_pct(a['ch1h'])} | 24h {fmt_pct(a['ch24h'])}\n"
            f"   Trend 5m: {a['trend_5m']} | 1h: {a['trend_1h']}"
        )

    funding = futures.get("funding")
    since = time.time() - 3600
    last_hour = [s for s in state["signals"] if s["ts"] >= since]

    lines += [
        "",
        "📊 SOL DERIVATIVES",
        f"Funding: {fmt(None if funding is None else funding * 100, 4)}%",
        f"Open interest: {fmt(futures.get('open_interest'), 0)} | 5m change {fmt_pct(futures.get('oi_change_pct'))}",
        f"Liquidations ~1h: L ${futures.get('long_liquidation', 0):,.0f} / S ${futures.get('short_liquidation', 0):,.0f}",
        "",
        f"📡 Signals last hour: {len(last_hour)}",
        f"📂 ACTIVE SIGNALS ({len(state['active'])})",
    ]

    if state["active"]:
        for s in state["active"]:
            lines.append(
                f"{side_emoji(s['side'])} #{s['id']} {s['side']} {label(s['strategy'])}\n"
                f"   Entry {s['entry']:.3f} | SL {s['sl']:.3f} | TP1 {s['tp1']:.3f} | TP2 {s['tp2']:.3f}"
                f"{' | TP1 HIT' if s['tp1_hit'] else ''}"
            )
    else:
        lines.append("None")

    lines += ["", "Signals only. No orders are placed."]
    send("\n".join(lines))


def send_daily_report(state):
    since = time.time() - 24 * 3600

    sigs = [s for s in state["signals"] if s["ts"] >= since]
    closed = [t for t in state["closed"] if t["closed_ts"] >= since]
    running_tp1 = sum(1 for a in state["active"] if a["tp1_hit"])

    n_long = sum(1 for s in sigs if s["side"] == "LONG")
    n_short = sum(1 for s in sigs if s["side"] == "SHORT")

    tp1_hits = sum(1 for t in closed if t["tp1_hit"]) + running_tp1
    tp2_hits = sum(1 for t in closed if t["reason"] == "TP2")
    sl_hits = sum(1 for t in closed if t["reason"] == "SL")
    be_exits = sum(1 for t in closed if t["reason"] == "BE")

    winners = sum(1 for t in closed if t["tp1_hit"])
    win_rate = (winners / len(closed) * 100) if closed else 0.0

    lines = [
        "📈 24-HOUR SIGNAL STATISTICS",
        "━━━━━━━━━━━━━━━━━━━━",
        now_utc(),
        "",
        "📡 SIGNALS",
        f"Total strategy signals: {len(sigs)}",
    ]
    for name in STRATEGIES:
        lines.append(f"  {label(name)}: {sum(1 for s in sigs if s['strategy'] == name)}")
    lines += [
        f"LONG: {n_long} | SHORT: {n_short}",
        f"Alerts sent: {sum(1 for s in sigs if s['action'] == 'SIGNAL')}",
        f"Confirmations: {sum(1 for s in sigs if s['action'] == 'CONFIRMATION')}",
        f"Filtered/conflict: {sum(1 for s in sigs if s['action'].startswith('FILTERED') or s['action'] == 'CONFLICT')}",
        "",
        "🎯 OUTCOMES (signals closed in 24h)",
        f"TP1 hits: {tp1_hits}",
        f"TP2 hits: {tp2_hits}",
        f"SL hits: {sl_hits}",
        f"Breakeven after TP1: {be_exits}",
        f"Still running: {len(state['active'])}",
        f"Hit rate (TP1 or better): {win_rate:.1f}% ({winners}/{len(closed)})",
        "",
        "🧩 STRATEGY BREAKDOWN",
    ]

    for name in STRATEGIES:
        st_closed = [t for t in closed if t["strategy"] == name]
        st_win = sum(1 for t in st_closed if t["tp1_hit"])
        st_sl = sum(1 for t in st_closed if t["reason"] == "SL")
        st_tp2 = sum(1 for t in st_closed if t["reason"] == "TP2")
        st_sig = sum(1 for s in sigs if s["strategy"] == name)
        rate = (st_win / len(st_closed) * 100) if st_closed else 0.0
        lines.append(
            f"{label(name)}: {st_sig} signals | TP1+ {st_win} | TP2 {st_tp2} | SL {st_sl} | hit {rate:.0f}%"
        )

    lines += ["", "Signals only. No orders are placed."]
    send("\n".join(lines))


# ============================================================
# MAIN
# ============================================================

def main():
    state = load_state()

    send(
        "🤖 SOL SIGNAL BOT STARTED\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Strategies: EMA Pullback | VWAP Breakout | RSI S/R Reversal\n"
        f"Suggested sizing for {ACCOUNT_SIZE:.0f} USDT: isolated, {LEVERAGE}x, "
        f"risk {RISK_PCT * 100:.1f}%, max 2 signals at a time\n"
        f"Active signals restored: {len(state['active'])}\n"
        "❌ Signals only - no orders placed"
    )

    start = time.time()
    last_external = 0
    ctx, futures = {}, {}

    while time.time() - start < RUN_SECONDS:
        try:
            df = add_indicators(get_df(SYMBOL, TF, 300))

            if len(df) < 60:
                print("Not enough candles.")
                time.sleep(POLL)
                continue

            c = df.iloc[-2]
            candle_time = int(c["time"])

            if time.time() - last_external >= EXTERNAL_DATA_INTERVAL:
                print("Updating external data...")
                ctx = get_market_context()
                futures = get_binance_futures()
                last_external = time.time()

            check_active_signals(state, df)

            if candle_time != state["last_eval_candle"]:
                state["last_eval_candle"] = candle_time

                age_ms = time.time() * 1000 - (candle_time + TF_MS)
                if 0 <= age_ms < FRESH_MS:
                    handle_signals(state, run_strategies(df), candle_time, ctx, futures)

            now = time.time()

            if state["last_hourly"] == 0 or now - state["last_hourly"] >= HOURLY_REPORT_INTERVAL:
                if ctx:
                    send_hourly_report(state, ctx, futures)
                    state["last_hourly"] = now

            if state["last_daily_report"] == 0:
                state["last_daily_report"] = now
            elif now - state["last_daily_report"] >= DAILY_REPORT_INTERVAL:
                send_daily_report(state)
                state["last_daily_report"] = now

            save_state(state)

            print(
                datetime.now().strftime("%H:%M:%S"),
                "| Price:", round(float(c["close"]), 3),
                "| Active signals:", len(state["active"]),
            )

        except Exception:
            traceback.print_exc()

        time.sleep(POLL)

    save_state(state)

    send(
        "🛑 BOT SESSION FINISHED\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"Runtime: {RUN_SECONDS // 60} minutes\n"
        f"Active signals carried over: {len(state['active'])}\n"
        "State saved. Signals only."
    )


if __name__ == "__main__":
    main()
