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
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", os.getenv("TELEGRAM_CHAT_ID", "")).strip()

exchange = ccxt.mexc({
    "enableRateLimit": True,
    "options": {"defaultType": "swap"},
})

sent_keys = set()
warning_keys = set()
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
    # Exact UTC 10m candles: 00+05, 10+15, 20+25, ...
    by_ts = {int(c[0]): c for c in candles5}
    out = []
    five = 5 * 60 * 1000
    now = now_ms()

    for ts, a in sorted(by_ts.items()):
        minute = (ts // 60000) % 60
        if minute % 10 != 0 or ts % 60000 != 0:
            continue

        b = by_ts.get(ts + five)

        # Both 5m candles must be closed.
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


def fetch_10m(symbol):
    raw = exchange.fetch_ohlcv(symbol, SOURCE_TIMEFRAME, limit=HISTORY_5M)
    candles = build_10m(raw)

    if len(candles) < 15:
        raise RuntimeError(
            f"not enough completed 10m candles: {len(candles)}"
        )

    return candles


def find_signal(candles):
    if len(candles) < 10:
        return None

    # #9 is the latest closed 10m candle.
    i = len(candles) - 10

    start = candles[i]
    c6, c7, c8, c9 = (
        candles[i + 6],
        candles[i + 7],
        candles[i + 8],
        candles[i + 9],
    )

    cs = color(start)
    c6c, c7c, c8c, c9c = (
        color(c6),
        color(c7),
        color(c8),
        color(c9),
    )

    if cs not in ("GREEN", "RED") or c6c not in ("GREEN", "RED"):
        return None

    # Start must be the last candle of its same-color run.
    if i + 1 < len(candles) and color(candles[i + 1]) == cs:
        return None

    # #6 changes color; #7-#9 stay with #6.
    if c6c == cs:
        return None

    if not (c7c == c6c and c8c == c6c and c9c == c6c):
        return None

    # Direction is determined ONLY by Start color.
    side = "LONG" if cs == "GREEN" else "SHORT"

    return {
        "side": side,
        "start": start,
        "c6": c6,
        "c7": c7,
        "c8": c8,
        "c9": c9,
        "start_color": cs,
    }


def check_pre_signal(candles):
    """
    Warning approximately 2 minutes before #9 closes.

    At this point #9 is still forming, so it must NOT be treated as a
    confirmed signal. We only warn when:
      - the start direction is valid,
      - #6 changed color,
      - #7 and #8 match #6,
      - current forming #9 matches #6,
      - roughly 2 minutes remain until #9 closes.
    """
    if len(candles) < 9:
        return None

    # Last item is the currently forming 10m candle only if it has a
    # completed 5m first half and a current 5m second half. build_10m()
    # intentionally returns only completed 10m candles, so we construct
    # the current 10m candle separately below in process().
    return None


def build_current_10m(raw5):
    """Return the current in-progress 10m candle from its two 5m candles."""
    if len(raw5) < 2:
        return None

    by_ts = {int(c[0]): c for c in raw5}
    now = now_ms()
    five = 5 * 60 * 1000

    # Find the latest 10m bucket whose first 5m candle exists.
    latest = None
    for ts in sorted(by_ts):
        minute = (ts // 60000) % 60
        if minute % 10 != 0:
            continue
        b = by_ts.get(ts + five)
        if b is not None and ts <= now < ts + 2 * five:
            latest = (ts, by_ts[ts], b)
    if latest is None:
        return None

    ts, a, b = latest
    # Current 5m candle may still be forming. Its current close is the
    # latest traded/returned close from MEXC.
    return [
        ts,
        float(a[1]),
        max(float(a[2]), float(b[2])),
        min(float(a[3]), float(b[3])),
        float(b[4]),
        float(a[5]) + float(b[5]),
    ]


def fetch_raw_5m(symbol):
    return exchange.fetch_ohlcv(
        symbol, SOURCE_TIMEFRAME, limit=max(HISTORY_5M, 260)
    )


def pre_signal(candles_completed, current_10m):
    """
    Return a warning payload only in the last ~2 minutes of #9.

    Completed candles are ... #1..#8, and current_10m is #9.
    The start candle is the first candle in this 9-candle sequence.
    """
    if current_10m is None or len(candles_completed) < 8:
        return None

    # Last 8 completed candles are #2..#8.
    seq8 = candles_completed[-8:]
    start = seq8[0]
    c6 = seq8[5]   # #6
    c7 = seq8[6]   # #7
    c8 = seq8[7]   # #8
    c9 = current_10m

    cs = color(start)
    c6c = color(c6)
    c7c = color(c7)
    c8c = color(c8)
    c9c = color(c9)

    if cs not in ("GREEN", "RED"):
        return None
    if c6c not in ("GREEN", "RED") or c9c not in ("GREEN", "RED"):
        return None

    # Start must be the last candle of its same-color run.
    if len(candles_completed) >= 9:
        previous = candles_completed[-9]
        if color(previous) == cs:
            return None

    if c6c == cs:
        return None
    if not (c7c == c6c and c8c == c6c and c9c == c6c):
        return None

    # End of #9 is exactly 10 minutes after start.
    close_ms = int(start[0]) + 9 * 10 * 60 * 1000
    remaining = (close_ms - now_ms()) / 1000.0

    # Warn once when between 90 and 150 seconds remain.
    if not (90 <= remaining <= 150):
        return None

    side = "LONG" if cs == "GREEN" else "SHORT"

    return {
        "side": side,
        "start_color": cs,
        "c6": c6,
        "c7": c7,
        "c8": c8,
        "c9": c9,
        "close_ms": close_ms,
    }


def telegram(text):
    if not TELEGRAM_BOT_TOKEN or not CHAT_ID:
        print("[TELEGRAM] not configured", flush=True)
        return

    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": CHAT_ID,
                "text": text,
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        if not r.ok:
            print(f"[TELEGRAM ERROR] {r.status_code}: {r.text}", flush=True)
    except Exception as e:
        print(f"[TELEGRAM ERROR] {e}", flush=True)


def warning_message(symbol, s):
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
        f"{title}\n\n"
        f"{coin}USDT Futures\n"
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
        "Трейдер Василь Павлів\n"
        "@vasylpavliv\n"
        "t.me/vasylpavliv"
    )


def process(symbol):
    try:
        raw = fetch_raw_5m(symbol)
        completed = build_10m(raw)
        current = build_current_10m(raw)

        if len(completed) < 10:
            raise RuntimeError(
                f"not enough completed 10m candles: {len(completed)}"
            )

        # Pre-warning: only once per #9 start timestamp.
        warning = pre_signal(completed, current)
        if warning:
            key = (symbol, int(warning["c9"][0]), "PRE")
            if key not in warning_keys:
                warning_keys.add(key)
                text = warning_message(symbol, warning)
                print("\n=== PRE-SIGNAL ===\n" + text + "\n=================\n", flush=True)
                telegram(text)

        # Confirmed signal only after #9 closes.
        s = find_signal(completed)
        if not s:
            return

        key = (symbol, s["side"], int(s["c9"][0]))
        if key in sent_keys:
            return

        sent_keys.add(key)
        text = message(symbol, s)
        print("\n=== SIGNAL ===\n" + text + "\n==============\n", flush=True)
        telegram(text)

    except Exception as e:
        msg = str(e)
        if last_error.get(symbol) != msg:
            print(f"[FETCH ERROR] {symbol}: {msg}", flush=True)
            last_error[symbol] = msg


def main():
    print("=== BTC + ETH 10m 6-9 CONFIRMATION BOT STARTING ===", flush=True)
    print("Imports OK", flush=True)
    print(f"Symbols: {', '.join(SYMBOLS)}", flush=True)
    print("Timeframe: 10m (built from closed 5m candles)", flush=True)
    print(
        "Rule: Start color sets direction; 6th changes color; "
        "7th-9th stay same as 6th",
        flush=True,
    )
    print("Pre-signal: ~2 min before #9 close when current #9 still matches #6", flush=True)
    print(f"Leverage: {LEVERAGE}x", flush=True)
    print(
        f"Telegram configured: {bool(TELEGRAM_BOT_TOKEN and CHAT_ID)}",
        flush=True,
    )
    print(f"Chat ID configured: {CHAT_ID or 'NO'}", flush=True)
    print("Connecting to MEXC...", flush=True)

    exchange.load_markets()

    print(
        f"MEXC connected. Markets loaded: {len(exchange.markets)}",
        flush=True,
    )
    print("=== BTC + ETH 10m 6-9 CONFIRMATION BOT RUNNING ===", flush=True)

    n = 0

    while True:
        t = time.time()

        for symbol in SYMBOLS:
            process(symbol)

        n += 1

        if n % 10 == 0:
            print(
                f"[HEARTBEAT] BTC+ETH 10m 6-9 BOT alive | scan={SCAN_SECONDS}s",
                flush=True,
            )

        elapsed = time.time() - t
        sleep = max(1, SCAN_SECONDS - elapsed)

        print(
            f"[CYCLE] completed in {elapsed:.1f}s | sleep {sleep:.1f}s",
            flush=True,
        )

        time.sleep(sleep)


if __name__ == "__main__":
    main()
