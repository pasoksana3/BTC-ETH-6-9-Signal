
import os, time, math, logging, asyncio
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import pandas as pd

# ============================================================
# MEXC ENTRY POINT BOT
# Strategy:
# 1H direction -> 1H zone/imbalance -> liquidity sweep ->
# 15m CHoCH/BOS -> POI/FVG -> entry -> SL/TP.
# Alert only. No order execution.
# ============================================================

MEXC_BASE = os.getenv("MEXC_BASE", "https://api.mexc.com")
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

SCAN_SECONDS = int(os.getenv("SCAN_SECONDS", "60"))
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "8"))
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "45"))

# Entry-quality filters
SWING_LOOKBACK = int(os.getenv("SWING_LOOKBACK", "3"))
SWEEP_LOOKBACK = int(os.getenv("SWEEP_LOOKBACK", "12"))
ZONE_LOOKBACK = int(os.getenv("ZONE_LOOKBACK", "80"))
ATR_PERIOD = int(os.getenv("ATR_PERIOD", "14"))
MIN_RR = float(os.getenv("MIN_RR", "1.5"))
TP1_RR = float(os.getenv("TP1_RR", "1.5"))
TP2_RR = float(os.getenv("TP2_RR", "2.2"))
SL_ATR_BUFFER = float(os.getenv("SL_ATR_BUFFER", "0.15"))

COINS = [
    "BTC","ETH","SOL","XRP","DOGE","SUI","AVAX","LINK","DOT","LTC",
    "APT","ARB","OP","PEPE","WIF","INJ","FIL","ATOM","UNI","TON",
    "SEI","TRX","NEAR","PYTH","ADA","ENA","BNB","BCH","ETC","XLM"
]

session = requests.Session()
session.headers.update({"Content-Type": "application/json", "User-Agent": "MEXC-EntryPoint-Bot/1.0"})
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

last_sent = {}

def symbol_for(coin):
    return f"{coin}_USDT"

def mexc_get(path, params=None):
    r = session.get(MEXC_BASE + path, params=params or {}, timeout=15)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict) and data.get("success") is False:
        raise RuntimeError(str(data))
    return data

def get_contracts():
    data = mexc_get("/api/v1/contract/detail")
    rows = data.get("data", data) if isinstance(data, dict) else data
    active = set()
    if isinstance(rows, list):
        for x in rows:
            sym = x.get("symbol")
            state = x.get("state", 0)
            if sym and str(state) in ("0", "1"):
                active.add(sym)
    return active

def get_klines(symbol, interval, limit=250):
    # MEXC contract K-line uses interval values such as Min15, Min60.
    path = f"/api/v1/contract/kline/{symbol}"
    data = mexc_get(path, {"interval": interval})
    d = data.get("data", data)
    if not isinstance(d, dict):
        raise RuntimeError(f"Unexpected kline response for {symbol}")
    keys = ["time","open","high","low","close","vol"]
    if not all(k in d for k in keys):
        raise RuntimeError(f"Missing kline fields for {symbol}")
    n = min(len(d[k]) for k in keys)
    n = min(n, limit)
    rows = []
    for i in range(-n, 0):
        rows.append({
            "time": int(d["time"][i]),
            "open": float(d["open"][i]),
            "high": float(d["high"][i]),
            "low": float(d["low"][i]),
            "close": float(d["close"][i]),
            "volume": float(d["vol"][i]),
        })
    df = pd.DataFrame(rows)
    if df.empty:
        raise RuntimeError("No candles")
    df = df.sort_values("time").drop_duplicates("time").reset_index(drop=True)
    # Drop current, still-forming candle.
    if len(df) > 3:
        df = df.iloc[:-1].copy()
    return df

def atr(df, period=14):
    h, l, c = df.high, df.low, df.close
    tr = pd.concat([(h-l), (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    return float(tr.rolling(period).mean().iloc[-1])

def pivot_highs(df, left=3, right=3):
    out = []
    for i in range(left, len(df)-right):
        v = df.high.iloc[i]
        if v == df.high.iloc[i-left:i+right+1].max():
            out.append((i, float(v)))
    return out

def pivot_lows(df, left=3, right=3):
    out = []
    for i in range(left, len(df)-right):
        v = df.low.iloc[i]
        if v == df.low.iloc[i-left:i+right+1].min():
            out.append((i, float(v)))
    return out

def fvg_bull(df, i):
    # 3-candle bullish imbalance: candle i low > candle i-2 high
    return i >= 2 and df.low.iloc[i] > df.high.iloc[i-2]

def fvg_bear(df, i):
    return i >= 2 and df.high.iloc[i] < df.low.iloc[i-2]

def htf_direction(df):
    # Structure-based 1H bias, not a moving-average-only signal.
    highs = pivot_highs(df, 3, 3)
    lows = pivot_lows(df, 3, 3)
    if len(highs) < 2 or len(lows) < 2:
        return None, "insufficient structure"
    h1, h2 = highs[-1][1], highs[-2][1]
    l1, l2 = lows[-1][1], lows[-2][1]
    close = float(df.close.iloc[-1])
    if h1 > h2 and l1 > l2 and close > l2:
        return "LONG", "1H higher-high + higher-low structure"
    if h1 < h2 and l1 < l2 and close < h2:
        return "SHORT", "1H lower-high + lower-low structure"
    # If structure is mixed, allow close relative to recent range midpoint.
    rh = max(x[1] for x in highs[-3:])
    rl = min(x[1] for x in lows[-3:])
    mid = (rh + rl) / 2
    if close > mid:
        return "LONG", "1H range location above midpoint"
    if close < mid:
        return "SHORT", "1H range location below midpoint"
    return None, "1H range/no directional edge"

def htf_zone(df, direction):
    recent = df.iloc[-ZONE_LOOKBACK:].copy()
    if direction == "LONG":
        # Last bearish candle before an impulsive bullish displacement.
        for i in range(len(recent)-4, 2, -1):
            c = recent.iloc[i]
            nxt = recent.iloc[i+1:i+4]
            if c.close < c.open and float(nxt.close.max()) > c.high * 1.003:
                return float(c.low), float(c.high), "1H demand/OB"
        return float(recent.low.min()), float(recent.low.quantile(.25)), "1H demand range"
    else:
        for i in range(len(recent)-4, 2, -1):
            c = recent.iloc[i]
            nxt = recent.iloc[i+1:i+4]
            if c.close > c.open and float(nxt.close.min()) < c.low * 0.997:
                return float(c.low), float(c.high), "1H supply/OB"
        return float(recent.high.quantile(.75)), float(recent.high.max()), "1H supply range"

def liquidity_sweep(df, direction):
    # Look for a wick through a recent swing and close back inside.
    n = min(SWEEP_LOOKBACK, len(df)-5)
    recent = df.iloc[-(n+3):].copy()
    lows = pivot_lows(recent, 2, 2)
    highs = pivot_highs(recent, 2, 2)
    if direction == "LONG" and lows:
        level = lows[-1][1]
        for i in range(max(2, len(recent)-6), len(recent)):
            c = recent.iloc[i]
            if c.low < level and c.close > level:
                return True, level, "sell-side liquidity sweep"
    if direction == "SHORT" and highs:
        level = highs[-1][1]
        for i in range(max(2, len(recent)-6), len(recent)):
            c = recent.iloc[i]
            if c.high > level and c.close < level:
                return True, level, "buy-side liquidity sweep"
    return False, None, "no recent liquidity sweep"

def choch_bos(df, direction):
    # After the sweep, require a close through a nearby opposing pivot.
    highs = pivot_highs(df, 2, 2)
    lows = pivot_lows(df, 2, 2)
    if direction == "LONG" and highs:
        level = highs[-1][1]
        if float(df.close.iloc[-1]) > level:
            return True, level, "15m bullish CHoCH/BOS"
    if direction == "SHORT" and lows:
        level = lows[-1][1]
        if float(df.close.iloc[-1]) < level:
            return True, level, "15m bearish CHoCH/BOS"
    return False, None, "CHoCH/BOS not confirmed"

def find_poi_fvg(df, direction):
    start = max(3, len(df)-10)
    for i in range(len(df)-1, start-1, -1):
        if direction == "LONG" and fvg_bull(df, i):
            # FVG zone between candle i-2 high and candle i low.
            return float(df.high.iloc[i-2]), float(df.low.iloc[i]), "15m bullish FVG"
        if direction == "SHORT" and fvg_bear(df, i):
            return float(df.high.iloc[i]), float(df.low.iloc[i-2]), "15m bearish FVG"
    # fallback to last opposite candle as POI
    for i in range(len(df)-2, start-1, -1):
        c = df.iloc[i]
        if direction == "LONG" and c.close < c.open:
            return float(c.low), float(c.high), "15m bearish candle POI"
        if direction == "SHORT" and c.close > c.open:
            return float(c.low), float(c.high), "15m bullish candle POI"
    return None, None, "no POI/FVG"

def make_signal(symbol, d1h, d15):
    direction, bias_reason = htf_direction(d1h)
    if not direction:
        return None

    zlow, zhigh, zone_reason = htf_zone(d1h, direction)
    price = float(d15.close.iloc[-1])

    # The setup must be near the HTF zone; avoid chasing a distant breakout.
    atr15 = atr(d15, ATR_PERIOD)
    if not math.isfinite(atr15) or atr15 <= 0:
        return None

    zone_distance = 1.5 * atr15
    if direction == "LONG":
        near_zone = price >= zlow - zone_distance and price <= zhigh + zone_distance
    else:
        near_zone = price >= zlow - zone_distance and price <= zhigh + zone_distance
    if not near_zone:
        return None

    swept, sweep_level, sweep_reason = liquidity_sweep(d15, direction)
    if not swept:
        return None

    confirmed, break_level, choch_reason = choch_bos(d15, direction)
    if not confirmed:
        return None

    plow, phigh, poi_reason = find_poi_fvg(d15, direction)
    if plow is None:
        return None

    # Entry is the midpoint of the POI/FVG, with a tolerance around current price.
    entry = (plow + phigh) / 2
    if abs(price-entry) > 1.25 * atr15:
        return None

    if direction == "LONG":
        sl = min(sweep_level, plow) - SL_ATR_BUFFER * atr15
        risk = entry - sl
        if risk <= 0:
            return None
        tp1 = entry + risk * TP1_RR
        tp2 = entry + risk * TP2_RR
    else:
        sl = max(sweep_level, phigh) + SL_ATR_BUFFER * atr15
        risk = sl - entry
        if risk <= 0:
            return None
        tp1 = entry - risk * TP1_RR
        tp2 = entry - risk * TP2_RR

    rr2 = abs(tp2-entry) / risk
    if rr2 < MIN_RR:
        return None

    # Entry should not be absurdly far from live price.
    entry_distance_pct = abs(price-entry) / price * 100
    if entry_distance_pct > 1.0:
        return None

    return {
        "symbol": symbol,
        "direction": direction,
        "entry": entry,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "rr": rr2,
        "price": price,
        "bias_reason": bias_reason,
        "zone_reason": zone_reason,
        "sweep_reason": sweep_reason,
        "choch_reason": choch_reason,
        "poi_reason": poi_reason,
        "sweep_level": sweep_level,
        "time": int(d15.time.iloc[-1]),
    }

def fmt_price(x):
    if x >= 1000: return f"{x:.2f}"
    if x >= 1: return f"{x:.4f}"
    if x >= .01: return f"{x:.6f}"
    return f"{x:.8f}"

def telegram_send(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("Telegram credentials are not configured")
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    r = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=15)
    r.raise_for_status()

def render_signal(s):
    arrow = "🟢 LONG" if s["direction"] == "LONG" else "🔴 SHORT"
    return (
        f"🎯 ENTRY POINT FOUND\n\n"
        f"{arrow}  {s['symbol']}\n\n"
        f"Entry: {fmt_price(s['entry'])}\n"
        f"SL:    {fmt_price(s['sl'])}\n"
        f"TP1:   {fmt_price(s['tp1'])}\n"
        f"TP2:   {fmt_price(s['tp2'])}\n"
        f"RR:    1:{s['rr']:.2f}\n\n"
        f"✅ 1H: {s['bias_reason']}\n"
        f"✅ Zone: {s['zone_reason']}\n"
        f"✅ Sweep: {s['sweep_reason']}\n"
        f"✅ Structure: {s['choch_reason']}\n"
        f"✅ POI: {s['poi_reason']}\n\n"
        f"Current: {fmt_price(s['price'])}\n"
        f"Mode: alert-only"
    )

def scan_one(symbol):
    try:
        d1h = get_klines(symbol, "Min60", 220)
        d15 = get_klines(symbol, "Min15", 220)
        return make_signal(symbol, d1h, d15)
    except Exception as e:
        logging.warning("%s: %s", symbol, e)
        return None

def should_send(s):
    key = f"{s['symbol']}:{s['direction']}"
    now = time.time()
    if now - last_sent.get(key, 0) < COOLDOWN_MINUTES * 60:
        return False
    last_sent[key] = now
    return True

def main():
    logging.info("ENTRY POINT BOT started | 30 coins | 1H ZONE + SWEEP + CHoCH + POI/FVG")
    logging.info("MEXC=%s | poll=%ss | cooldown=%smin", MEXC_BASE, SCAN_SECONDS, COOLDOWN_MINUTES)
    active = get_contracts()
    symbols = [symbol_for(c) for c in COINS if symbol_for(c) in active]
    logging.info("Active symbols: %s/%s", len(symbols), len(COINS))
    missing = [symbol_for(c) for c in COINS if symbol_for(c) not in active]
    if missing:
        logging.warning("Unavailable symbols: %s", ", ".join(missing))

    while True:
        started = time.time()
        found = 0
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futures = [ex.submit(scan_one, s) for s in symbols]
            for f in as_completed(futures):
                signal = f.result()
                if signal and should_send(signal):
                    try:
                        telegram_send(render_signal(signal))
                        found += 1
                        logging.info("SIGNAL SENT: %s %s", signal["symbol"], signal["direction"])
                    except Exception as e:
                        logging.error("Telegram error: %s", e)
        elapsed = time.time() - started
        logging.info("scan complete | %.1fs | signals=%s", elapsed, found)
        time.sleep(max(1, SCAN_SECONDS - elapsed))

if __name__ == "__main__":
    main()
