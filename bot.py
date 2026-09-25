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
signal_keys = set()
last_error = {}


def now_ms():
    return int(time.time() * 1000)


def utc_text(ms):
    return datetime.fromtimestamp(
        ms / 1000,
        tz=timezone.utc
    ).strftime("%Y-%m-%d %H:%M:%S UTC")


def color(c):
    o, cl = float(c[1]), float(c[4])
    return "GREEN" if cl > o else "RED" if cl < o else "DOJI"


def emoji(c):
    return "🟢" if c == "GREEN" else "🔴" if c == "RED" else "⚪"


def build_10m(candles5):
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


def build_current_10m(raw5):
    if len(raw5) < 2:
        return None

    by_ts = {int(c[0]): c for c in raw5}
    now = now_ms()
    five = 5 * 60 * 1000

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
        symbol,
        SOURCE_TIMEFRAME,
        limit=max(HISTORY_5M, 260)
    )


def find_signal(candles):
    if len(candles) < 10:
        return None

    i = len(candles) - 10

    start = candles[i]
    c6 = candles[i + 6]
    c7 = candles[i + 7]
    c8 = candles[i + 8]
    c9 = candles[i + 9]

    cs = color(start)
    c6c = color(c6)
    c7c = color(c7)
    c8c = color(c8)
    c9c = color(c9)

    if cs not in ("GREEN", "RED"):
        return None

    if c6c not in ("GREEN", "RED"):
        return None

    # Start must be the LAST candle of the same-color run.
    if i + 1 < len(candles):
        if color(candles[i + 1]) == cs:
            return None

    # #6 must change color.
    if c6c == cs:
        return None

    # #7, #8, #9 must remain the same color as #6.
    if not (
        c7c == c6c
        and c8c == c6c
        and c9c == c6c
    ):
        return None

    # Direction is determined by Start color.
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
    return None


def pre_signal(candles_completed, current_10m):
    """PRE-ALERT тільки в останню хвилину живої свічки #9."""

    if current_10m is None:
        return None

    if len(candles_completed) < 8:
        return None

    # Поточна жива свічка повинна бути #9.
    now = time.time()
    target_ts = int(current_10m[0])
    elapsed = now - (target_ts / 1000.0)

    # Попередження тільки в останні 60 секунд #9.
    if elapsed < 540 or elapsed >= 600:
        return None

    # Якщо ця свічка вже є серед закритих —
    # вона вже не є живою #9.
    if any(int(c[0]) == target_ts for c in candles_completed):
        return None

    # Для #9:
    #
    # Start = #1
    # #2
    # #3
    # #4
    # #5
    # #6 = trigger
    # #7
    # #8
    # #9 = current_10m
    #
    # Отже Start знаходиться на 8 свічок раніше.
    s_ts = target_ts - 8 * 600 * 1000

    start_index = None
    for idx, candle in enumerate(candles_completed):
        if int(candle[0]) == s_ts:
            start_index = idx
            break

    if start_index is None:
        return None

    start = candles_completed[start_index]
    sc = color(start)

    if sc not in ("GREEN", "RED"):
        return None

    # Start повинен бути останньою свічкою
    # своєї серії однакового кольору.
    if start_index + 1 < len(candles_completed):
        if color(candles_completed[start_index + 1]) == sc:
            return None

    # Знаходимо вже закриті #6, #7, #8.
    c6_ts = target_ts - 3 * 600 * 1000
    c7_ts = target_ts - 2 * 600 * 1000
    c8_ts = target_ts - 1 * 600 * 1000

    candle_map = {int(c[0]): c for c in candles_completed}

    if c6_ts not in candle_map:
        return None

    if c7_ts not in candle_map:
        return None

    if c8_ts not in candle_map:
        return None

    c6 = candle_map[c6_ts]
    c7 = candle_map[c7_ts]
    c8 = candle_map[c8_ts]

    c6c = color(c6)
    c7c = color(c7)
    c8c = color(c8)
    c9c = color(current_10m)

    # #6 повинен змінити колір відносно Start.
    if c6c not in ("GREEN", "RED"):
        return None

    if c6c == sc:
        return None

    # #7 і #8 повинні залишатися кольором #6.
    if c7c != c6c:
        return None

    if c8c != c6c:
        return None

    # Жива #9 також повинна бути такого самого кольору.
    if c9c != c6c:
        return None

    side = "LONG" if sc == "GREEN" else "SHORT"

    # PRE для цієї #9 не дублюється на рівні process()
    # через warning_keys.
    return {
        "side": side,
        "start_color": sc,
        "c6": c6,
        "c7": c7,
        "c8": c8,
        "c9": current_10m,
        "close_ms": target_ts + 10 * 60 * 1000,
    }


def telegram(text):
    if not TELEGRAM_BOT_TOKEN or not CHAT_ID:
        print("[TELEGRAM] not configured", flush=True)
        return False

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
            print(
                f"[TELEGRAM ERROR] {r.status_code}: {r.text}",
                flush=True
            )
            return False

        return True

    except Exception as e:
        print(f"[TELEGRAM ERROR] {e}", flush=True)
        return False


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
    c6 = s["c6"]
    c7 = s["c7"]
    c8 = s["c8"]
    c9 = s["c9"]

    entry = float(c9[4])

    title = "🟢 LONG" if side == "LONG" else "🔴 SHORT"

    return (
        f"{title}\n\n"
        f"{coin}USDT Futures\n"
        "Timeframe: 10m\n"
        f"Leverage: {LEVERAGE}x\n\n"
        "6→9 CONFIRMATION\n\n"
        f"Start: {emoji(s['start_color'])} "
        f"{s['start_color']}\n\n"
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

        # =========================================================
        # 1. SIGNAL ПЕРЕВІРЯЄМО ПЕРШИМ
        # =========================================================

        s = find_signal(completed)

        if s:
            signal_key = (
                symbol,
                int(s["c9"][0])
            )

            # Запам'ятовуємо, що для цієї #9 вже є SIGNAL.
            signal_keys.add(signal_key)

            if signal_key not in sent_keys:

                text = message(symbol, s)

                if telegram(text):
                    sent_keys.add(signal_key)

                    print(
                        "\n=== SIGNAL ===\n"
                        + text
                        + "\n==============\n",
                        flush=True
                    )

        # =========================================================
        # 2. PRE-ALERT
        # =========================================================

        warning = pre_signal(
            completed,
            current
        )

        if warning:

            warning_key = (
                symbol,
                int(warning["c9"][0]),
                "PRE"
            )

            signal_key = (
                symbol,
                int(warning["c9"][0])
            )

            # Якщо SIGNAL для цієї #9 вже був —
            # PRE НЕ ВІДПРАВЛЯЄМО.
            if signal_key in signal_keys:
                return

            if warning_key not in warning_keys:

                text = warning_message(
                    symbol,
                    warning
                )

                if telegram(text):
                    warning_keys.add(warning_key)

                    print(
                        "\n=== PRE-SIGNAL ===\n"
                        + text
                        + "\n=================\n",
                        flush=True
                    )

    except Exception as e:

        msg = str(e)

        if last_error.get(symbol) != msg:

            print(
                f"[FETCH ERROR] {symbol}: {msg}",
                flush=True
            )

            last_error[symbol] = msg


def main():

    print(
        "=== BTC + ETH 10m 6-9 CONFIRMATION BOT STARTING ===",
        flush=True
    )

    print(
        "Imports OK",
        flush=True
    )

    print(
        f"Symbols: {', '.join(SYMBOLS)}",
        flush=True
    )

    print(
        "Timeframe: 10m (built from closed 5m candles)",
        flush=True
    )

    print(
        "Rule: Start color sets direction; "
        "6th changes color; 7th-9th stay same as 6th",
        flush=True
    )

    print(
        "Pre-signal: last 60 sec of #9 "
        "when current #9 still matches #6",
        flush=True
    )

    print(
        f"Leverage: {LEVERAGE}x",
        flush=True
    )

    print(
        "Telegram configured: "
        f"{bool(TELEGRAM_BOT_TOKEN and CHAT_ID)}",
        flush=True
    )

    print(
        f"Chat ID configured: "
        f"{CHAT_ID or 'NO'}",
        flush=True
    )

    print(
        "Connecting to MEXC...",
        flush=True
    )

    exchange.load_markets()

    print(
        f"MEXC connected. Markets loaded: "
        f"{len(exchange.markets)}",
        flush=True
    )

    print(
        "=== BTC + ETH 10m 6-9 CONFIRMATION BOT RUNNING ===",
        flush=True
    )

    n = 0

    while True:

        t = time.time()

        for symbol in SYMBOLS:
            process(symbol)

        n += 1

        if n % 10 == 0:
            print(
                "[HEARTBEAT] BTC+ETH 10m 6-9 BOT alive "
                f"| scan={SCAN_SECONDS}s",
                flush=True
            )

        elapsed = time.time() - t

        sleep = max(
            1,
            SCAN_SECONDS - elapsed
        )

        print(
            f"[CYCLE] completed in {elapsed:.1f}s "
            f"| sleep {sleep:.1f}s",
            flush=True
        )

        time.sleep(sleep)


if __name__ == "__main__":
    main()
