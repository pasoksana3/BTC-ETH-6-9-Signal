import os
import time
from datetime import datetime, timezone
import ccxt
import requests

SYMBOLS = ["BTC/USDT:USDT", "ETH/USDT:USDT"]
DISPLAY_TIMEFRAME = "10m"
SOURCE_TIMEFRAME = "5m"
SCAN_SECONDS = int(os.getenv("SCAN_SECONDS", "30"))
LEVERAGE = int(os.getenv("LEVERAGE", "30"))
HISTORY_5M = int(os.getenv("HISTORY_5M", "240"))
PRE_MIN_SECONDS = int(os.getenv("PRE_MIN_SECONDS", "90"))
PRE_MAX_SECONDS = int(os.getenv("PRE_MAX_SECONDS", "150"))
# WIN is evaluated after candles #10-#13. Default: price must close
# at least 0.50% in the signal direction at any point in #10-#13.
WIN_MOVE_PCT = float(os.getenv("WIN_MOVE_PCT", "0.005"))
WIN_CHECK_CANDLES = int(os.getenv("WIN_CHECK_CANDLES", "4"))
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", os.getenv("TELEGRAM_CHAT_ID", "")).strip()

exchange = ccxt.mexc({
    "enableRateLimit": True,
    "timeout": 15000,
    "options": {"defaultType": "swap"},
})

sent_keys = set()
warning_keys = set()
win_keys = set()
last_error = {}


def now_ms():
    return int(time.time() * 1000)


def utc_text(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )


def color(c):
    o, cl = float(c[1]), float(c[4])
    return "GREEN" if cl > o else "RED" if cl < o else "DOJI"


def emoji(c):
    return "🟢" if c == "GREEN" else "🔴" if c == "RED" else "⚪"


def build_10m(candles5):
    """Build only fully closed UTC-aligned 10m candles from 5m candles."""
    by_ts = {int(c[0]): c for c in candles5}
    out = []
    five = 5 * 60 * 1000
    now = now_ms()

    for ts, a in sorted(by_ts.items()):
        minute = (ts // 60000) % 60
        if minute % 10 != 0 or ts % 60000 != 0:
            continue
        b = by_ts.get(ts + five)
        if b is None or ts + 2 * five > now:
            continue
        out.append([
            ts,
            float(a[1]),
            max(float(a[2]), float(b[2])),
            min(float(a[3]), float(b[3])),
            float(b[4]),
            float(a[5]) + float(b[5]),
        ])
    return out


def build_current_10m(raw5, live_price):
    """Build the current 10m candle using the live ticker price for close."""
    if len(raw5) < 1:
        return None
    by_ts = {int(c[0]): c for c in raw5}
    now = now_ms()
    five = 5 * 60 * 1000
    ten = 10 * 60 * 1000
    bucket = (now // ten) * ten
    a = by_ts.get(bucket)
    if a is None:
        return None
    b = by_ts.get(bucket + five)
    if b is None:
        # Before the second 5m candle appears, #9 cannot yet be evaluated.
        return None
    o = float(a[1])
    h = max(float(a[2]), float(b[2]), float(live_price))
    l = min(float(a[3]), float(b[3]), float(live_price))
    v = float(a[5]) + float(b[5])
    return [bucket, o, h, l, float(live_price), v]


def fetch_raw_5m(symbol):
    return exchange.fetch_ohlcv(symbol, SOURCE_TIMEFRAME, limit=max(HISTORY_5M, 260))


def fetch_live_price(symbol):
    ticker = exchange.fetch_ticker(symbol)
    last = ticker.get("last")
    if last is None:
        raise RuntimeError("ticker last price unavailable")
    return float(last)


def find_signal(candles):
    if len(candles) < 10:
        return None
    i = len(candles) - 10
    start = candles[i]
    c6, c7, c8, c9 = candles[i + 6], candles[i + 7], candles[i + 8], candles[i + 9]
    cs = color(start)
    c6c, c7c, c8c, c9c = color(c6), color(c7), color(c8), color(c9)

    if cs not in ("GREEN", "RED") or c6c not in ("GREEN", "RED"):
        return None
    if i + 1 < len(candles) and color(candles[i + 1]) == cs:
        return None
    if c6c == cs:
        return None
    if not (c7c == c6c and c8c == c6c and c9c == c6c):
        return None

    side = "LONG" if cs == "GREEN" else "SHORT"
    return {
        "side": side, "start": start, "c6": c6, "c7": c7,
        "c8": c8, "c9": c9, "start_color": cs,
    }


def pre_signal(candles_completed, current_10m):
    """Warn once during the 90-150s window before #9 closes."""
    if current_10m is None or len(candles_completed) < 8:
        return None

    seq8 = candles_completed[-8:]  # #2..#8
    start, c6, c7, c8, c9 = seq8[0], seq8[5], seq8[6], seq8[7], current_10m
    cs, c6c, c7c, c8c, c9c = color(start), color(c6), color(c7), color(c8), color(c9)

    if cs not in ("GREEN", "RED") or c6c not in ("GREEN", "RED") or c9c not in ("GREEN", "RED"):
        return None
    if len(candles_completed) >= 9 and color(candles_completed[-9]) == cs:
        return None
    if c6c == cs or c7c != c6c or c8c != c6c or c9c != c6c:
        return None

    close_ms = int(start[0]) + 9 * 10 * 60 * 1000
    remaining = (close_ms - now_ms()) / 1000.0
    if not (PRE_MIN_SECONDS <= remaining <= PRE_MAX_SECONDS):
        return None

    return {
        "side": "LONG" if cs == "GREEN" else "SHORT",
        "start_color": cs, "c6": c6, "c7": c7, "c8": c8,
        "c9": c9, "close_ms": close_ms,
    }


def telegram(text):
    if not TELEGRAM_BOT_TOKEN or not CHAT_ID:
        print("[TELEGRAM] not configured", flush=True)
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": True},
            timeout=15,
        )
        if not r.ok:
            print(f"[TELEGRAM ERROR] {r.status_code}: {r.text}", flush=True)
        return r.ok
    except Exception as e:
        print(f"[TELEGRAM ERROR] {e}", flush=True)
        return False


def warning_message(symbol):
    coin = symbol.split("/")[0]
    return (
        "Всі готові?\n\n"
        "Скоро дам СИГНАЛ!\n\n"
        f"{coin}USDT Futures\n\n"
        "Timeframe: 10m\n\n"
        "⚠️ Сигнал буде тільки після закриття свічки."
    )


def message(symbol, s):
    coin = symbol.split("/")[0]
    side = s["side"]
    c6, c7, c8, c9 = s["c6"], s["c7"], s["c8"], s["c9"]
    entry = float(c9[4])
    title = "🟢 LONG" if side == "LONG" else "🔴 SHORT"
    return (
        f"{title}\n\n{coin}USDT Futures\n"
        "Timeframe: 10m\n"
        f"Leverage: {LEVERAGE}x\n\n"
        "6→9 CONFIRMATION\n"
        f"Start: {emoji(s['start_color'])} {s['start_color']}\n"
        f"6: {emoji(color(c6))} {color(c6)}\n"
        f"7: {emoji(color(c7))} {color(c7)}\n"
        f"8: {emoji(color(c8))} {color(c8)}\n"
        f"9: {emoji(color(c9))} {color(c9)}\n\n"
        f"Entry: {entry}\n"
        f"Signal candle #9 closed: {utc_text(c9[0])}\n\n"
        "Трейдер Василь Павлів\n@vasylpavliv\nt.me/vasylpavliv"
    )


def check_win(candles, signal):
    """Check candles #10-#13 after a confirmed signal.

    WIN criterion is explicit and configurable: at least one closing price
    reaches +/- WIN_MOVE_PCT from the #9 close in the signal direction.
    """
    if len(candles) < 10 + WIN_CHECK_CANDLES:
        return None
    c9 = signal["c9"]
    idx = next((i for i, c in enumerate(candles) if int(c[0]) == int(c9[0])), None)
    if idx is None or idx + WIN_CHECK_CANDLES >= len(candles):
        return None
    entry = float(c9[4])
    future = candles[idx + 1: idx + 1 + WIN_CHECK_CANDLES]
    if len(future) < WIN_CHECK_CANDLES:
        return None
    if signal["side"] == "LONG":
        target = entry * (1 + WIN_MOVE_PCT)
        hit = any(float(c[4]) >= target for c in future)
    else:
        target = entry * (1 - WIN_MOVE_PCT)
        hit = any(float(c[4]) <= target for c in future)
    return {"win": hit, "entry": entry, "target": target, "last": future[-1], "c9_ts": c9[0]}


def win_message(symbol, signal, result):
    coin = symbol.split("/")[0]
    label = "✅ WIN" if result["win"] else "❌ LOSS"
    return (
        f"{label}\n\n{coin}USDT Futures\n"
        "Timeframe: 10m\n\n"
        f"Entry: {result['entry']}\n"
        f"Target: {result['target']} ({WIN_MOVE_PCT*100:.2f}%)\n"
        f"Checked through candle #9 + {WIN_CHECK_CANDLES} candles\n\n"
        "6→9 CONFIRMATION\n"
        f"Result: {'WIN' if result['win'] else 'LOSS'}"
    )


def process(symbol):
    try:
        raw = fetch_raw_5m(symbol)
        live = fetch_live_price(symbol)
        completed = build_10m(raw)
        current = build_current_10m(raw, live)
        if len(completed) < 10:
            raise RuntimeError(f"not enough completed 10m candles: {len(completed)}")

        warning = pre_signal(completed, current)
        if warning:
            key = (symbol, int(warning["c9"][0]), "PRE")
            if key not in warning_keys:
                text = warning_message(symbol)
                if telegram(text):
                    warning_keys.add(key)
                print("\n=== PRE-SIGNAL ===\n" + text + "\n=================\n", flush=True)

        s = find_signal(completed)
        if not s:
            return
        key = (symbol, s["side"], int(s["c9"][0]))
        if key not in sent_keys:
            text = message(symbol, s)
            if telegram(text):
                sent_keys.add(key)
            print("\n=== SIGNAL ===\n" + text + "\n==============\n", flush=True)

        # WIN/LOSS is sent only after the full #10-#13 check is available.
        wkey = (symbol, int(s["c9"][0]), "WIN")
        if wkey not in win_keys:
            result = check_win(completed, s)
            if result is not None:
                text = win_message(symbol, s, result)
                if telegram(text):
                    win_keys.add(wkey)
                print("\n=== RESULT ===\n" + text + "\n==============\n", flush=True)

    except Exception as e:
        msg = str(e)
        if last_error.get(symbol) != msg:
            print(f"[FETCH ERROR] {symbol}: {msg}", flush=True)
            last_error[symbol] = msg


def main():
    print("=== BTC + ETH 10m 6-9 PRE2MIN + WIN BOT STARTING ===", flush=True)
    print("Imports OK", flush=True)
    print(f"Symbols: {', '.join(SYMBOLS)}", flush=True)
    print("Timeframe: 10m (built from 5m candles)", flush=True)
    print("Rule: Start color sets direction; #6 changes; #7-#9 match #6", flush=True)
    print(f"Pre-signal: {PRE_MIN_SECONDS}-{PRE_MAX_SECONDS}s before #9 close", flush=True)
    print(f"WIN check: candles #10-#{9+WIN_CHECK_CANDLES}, move={WIN_MOVE_PCT*100:.2f}%", flush=True)
    print(f"Leverage: {LEVERAGE}x", flush=True)
    print(f"Telegram configured: {bool(TELEGRAM_BOT_TOKEN and CHAT_ID)}", flush=True)
    print(f"Chat ID configured: {CHAT_ID or 'NO'}", flush=True)
    print("Connecting to MEXC...", flush=True)
    exchange.load_markets()
    print(f"MEXC connected. Markets loaded: {len(exchange.markets)}", flush=True)
    print("=== BTC + ETH 10m 6-9 PRE2MIN + WIN BOT RUNNING ===", flush=True)
    n = 0
    while True:
        t = time.time()
        for symbol in SYMBOLS:
            process(symbol)
        n += 1
        if n % 10 == 0:
            print(f"[HEARTBEAT] BTC+ETH bot alive | scan={SCAN_SECONDS}s", flush=True)
        elapsed = time.time() - t
        sleep = max(1, SCAN_SECONDS - elapsed)
        print(f"[CYCLE] completed in {elapsed:.1f}s | sleep {sleep:.1f}s", flush=True)
        time.sleep(sleep)


if __name__ == "__main__":
    main()
