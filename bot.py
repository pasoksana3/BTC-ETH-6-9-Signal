import os
import time
from datetime import datetime, timezone

import ccxt
import requests


# ============================================================
# CONFIG
# ============================================================

SYMBOLS = [
    "BTC/USDT:USDT",
    "ETH/USDT:USDT",
]

DISPLAY_TIMEFRAME = "10m"
SOURCE_TIMEFRAME = "5m"

SCAN_SECONDS = int(os.getenv("SCAN_SECONDS", "30"))
LEVERAGE = int(os.getenv("LEVERAGE", "30"))
HISTORY_5M = int(os.getenv("HISTORY_5M", "240"))

# PRE-SIGNAL:
# approximately 2 minutes before candle #9 closes
PRE_MIN_SECONDS = int(os.getenv("PRE_MIN_SECONDS", "90"))
PRE_MAX_SECONDS = int(os.getenv("PRE_MAX_SECONDS", "150"))

TELEGRAM_BOT_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN",
    ""
).strip()

CHAT_ID = os.getenv(
    "CHAT_ID",
    "-5370612713"
).strip()


# ============================================================
# MEXC
# ============================================================

exchange = ccxt.mexc({
    "enableRateLimit": True,
    "timeout": 15000,
    "options": {
        "defaultType": "swap"
    },
})


# ============================================================
# STATE
# ============================================================

sent_keys = set()
warning_keys = set()
last_error = {}


# ============================================================
# TIME
# ============================================================

def now_ms():
    return int(time.time() * 1000)


def utc_text(ms):
    return datetime.fromtimestamp(
        ms / 1000,
        tz=timezone.utc
    ).strftime("%Y-%m-%d %H:%M:%S UTC")


# ============================================================
# CANDLE COLOR
# ============================================================

def color(candle):
    open_price = float(candle[1])
    close_price = float(candle[4])

    if close_price > open_price:
        return "GREEN"

    if close_price < open_price:
        return "RED"

    return "DOJI"


def emoji(c):
    if c == "GREEN":
        return "🟢"

    if c == "RED":
        return "🔴"

    return "⚪"


# ============================================================
# BUILD CLOSED 10m CANDLES FROM 5m
# ============================================================

def build_10m(candles5):
    """
    Build only fully closed UTC-aligned 10m candles.

    00 + 05
    10 + 15
    20 + 25
    30 + 35
    40 + 45
    50 + 55
    """

    by_ts = {
        int(c[0]): c
        for c in candles5
    }

    out = []

    five = 5 * 60 * 1000
    now = now_ms()

    for ts, a in sorted(by_ts.items()):

        minute = (ts // 60000) % 60

        if minute % 10 != 0:
            continue

        if ts % 60000 != 0:
            continue

        b = by_ts.get(ts + five)

        if b is None:
            continue

        # Both 5m candles must be fully closed.
        if ts + 2 * five > now:
            continue

        out.append([
            ts,
            float(a[1]),
            max(
                float(a[2]),
                float(b[2])
            ),
            min(
                float(a[3]),
                float(b[3])
            ),
            float(b[4]),
            float(a[5]) + float(b[5]),
        ])

    return out


# ============================================================
# CURRENT 10m CANDLE
# ============================================================

def build_current_10m(raw5, live_price):
    """
    Build the currently forming 10m candle.

    This is used only for PRE-SIGNAL.

    For the PRE-SIGNAL we need:
    #1 completed
    #6 completed
    #7 completed
    #8 completed
    #9 currently forming
    """

    if not raw5:
        return None

    by_ts = {
        int(c[0]): c
        for c in raw5
    }

    now = now_ms()

    five = 5 * 60 * 1000
    ten = 10 * 60 * 1000

    bucket = (now // ten) * ten

    a = by_ts.get(bucket)

    if a is None:
        return None

    b = by_ts.get(bucket + five)

    # Before the second 5m candle appears,
    # the current 10m candle cannot be evaluated.
    if b is None:
        return None

    open_price = float(a[1])

    high_price = max(
        float(a[2]),
        float(b[2]),
        float(live_price)
    )

    low_price = min(
        float(a[3]),
        float(b[3]),
        float(live_price)
    )

    volume = (
        float(a[5]) +
        float(b[5])
    )

    return [
        bucket,
        open_price,
        high_price,
        low_price,
        float(live_price),
        volume,
    ]


# ============================================================
# MEXC DATA
# ============================================================

def fetch_raw_5m(symbol):
    return exchange.fetch_ohlcv(
        symbol,
        SOURCE_TIMEFRAME,
        limit=max(
            HISTORY_5M,
            260
        )
    )


def fetch_live_price(symbol):
    ticker = exchange.fetch_ticker(symbol)

    last = ticker.get("last")

    if last is None:
        raise RuntimeError(
            "ticker last price unavailable"
        )

    return float(last)


# ============================================================
# FINAL SIGNAL
# ============================================================

def find_signal(candles):
    """
    FINAL LOGIC

    #1 = START

    #6, #7, #8, #9
    must ALL be opposite color to #1.

    Then:

    #10, #11, #12, #13, #14, #15

    At least ONE candle must have
    the same color as #1.

    ANY matching #10-#15
    = WIN / SIGNAL

    NONE matching #10-#15
    = LOSS / NO SIGNAL

    Final check happens ONLY
    after #15 has fully closed.

    Direction:

    #1 GREEN -> LONG
    #1 RED   -> SHORT
    """

    if len(candles) < 15:
        return None

    # Last 15 CLOSED candles.
    i = len(candles) - 15

    start = candles[i]

    c6 = candles[i + 5]
    c7 = candles[i + 6]
    c8 = candles[i + 7]
    c9 = candles[i + 8]

    c10 = candles[i + 9]
    c11 = candles[i + 10]
    c12 = candles[i + 11]
    c13 = candles[i + 12]
    c14 = candles[i + 13]
    c15 = candles[i + 14]

    cs = color(start)

    # Start cannot be DOJI.
    if cs not in ("GREEN", "RED"):
        return None

    # ========================================================
    # START MUST BE LAST CANDLE OF SAME-COLOR RUN
    # ========================================================

    if i + 1 < len(candles):

        next_candle_color = color(
            candles[i + 1]
        )

        if next_candle_color == cs:
            return None

    # ========================================================
    # OPPOSITE COLOR
    # ========================================================

    opposite = (
        "RED"
        if cs == "GREEN"
        else "GREEN"
    )

    # ========================================================
    # #6 - #9 ALL OPPOSITE TO #1
    # ========================================================

    candles_6_9 = [
        c6,
        c7,
        c8,
        c9,
    ]

    if not all(
        color(c) == opposite
        for c in candles_6_9
    ):
        return None

    # ========================================================
    # #10 - #15
    # AT LEAST ONE MUST MATCH START COLOR
    # ========================================================

    candles_10_15 = [
        c10,
        c11,
        c12,
        c13,
        c14,
        c15,
    ]

    if not any(
        color(c) == cs
        for c in candles_10_15
    ):
        # LOSS
        # No final signal is sent.
        return None

    # ========================================================
    # DIRECTION
    # ========================================================

    side = (
        "LONG"
        if cs == "GREEN"
        else "SHORT"
    )

    return {
        "side": side,

        "start": start,
        "start_color": cs,

        "c6": c6,
        "c7": c7,
        "c8": c8,
        "c9": c9,

        "c10": c10,
        "c11": c11,
        "c12": c12,
        "c13": c13,
        "c14": c14,
        "c15": c15,
    }


# ============================================================
# PRE-SIGNAL
# ============================================================

def pre_signal(
    candles_completed,
    current_10m
):
    """
    PRE-SIGNAL LOGIC

    This is NOT checked on #8.

    It is checked while #9 is forming.

    At that moment:

    #1 = START
    #6 = opposite
    #7 = opposite
    #8 = opposite
    #9 = currently forming and must be opposite

    Warning is sent approximately
    90-150 seconds before #9 closes.
    """

    if (
        current_10m is None
        or len(candles_completed) < 8
    ):
        return None

    # The last 8 completed candles are:
    #
    #1
    #2
    #3
    #4
    #5
    #6
    #7
    #8

    seq8 = candles_completed[-8:]

    start = seq8[0]
    c6 = seq8[5]
    c7 = seq8[6]
    c8 = seq8[7]

    # Current forming candle = #9
    c9 = current_10m

    cs = color(start)

    if cs not in ("GREEN", "RED"):
        return None

    # #1 must be the last candle
    # of its same-color run.
    if len(candles_completed) >= 9:

        previous = candles_completed[-9]

        if color(previous) == cs:
            return None

    opposite = (
        "RED"
        if cs == "GREEN"
        else "GREEN"
    )

    # ========================================================
    # #6 - #9
    # ========================================================

    if not (
        color(c6) == opposite
        and color(c7) == opposite
        and color(c8) == opposite
        and color(c9) == opposite
    ):
        return None

    # ========================================================
    # #9 CLOSE TIME
    # ========================================================

    close_ms = (
        int(c9[0])
        + 10 * 60 * 1000
    )

    remaining = (
        close_ms - now_ms()
    ) / 1000.0

    # Approximately 2 minutes before #9 closes.
    if not (
        PRE_MIN_SECONDS
        <= remaining
        <= PRE_MAX_SECONDS
    ):
        return None

    return {
        "side": (
            "LONG"
            if cs == "GREEN"
            else "SHORT"
        ),

        "start_color": cs,

        "c6": c6,
        "c7": c7,
        "c8": c8,
        "c9": c9,

        "close_ms": close_ms,
    }


# ============================================================
# TELEGRAM
# ============================================================

def telegram(text):

    if (
        not TELEGRAM_BOT_TOKEN
        or not CHAT_ID
    ):
        print(
            "[TELEGRAM] not configured",
            flush=True
        )
        return False

    try:

        response = requests.post(
            (
                "https://api.telegram.org/"
                f"bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            ),

            json={
                "chat_id": CHAT_ID,
                "text": text,
                "disable_web_page_preview": True,
            },

            timeout=15,
        )

        if not response.ok:

            print(
                "[TELEGRAM ERROR] "
                f"{response.status_code}: "
                f"{response.text}",
                flush=True
            )

        return response.ok

    except Exception as e:

        print(
            f"[TELEGRAM ERROR] {e}",
            flush=True
        )

        return False


# ============================================================
# PRE-SIGNAL MESSAGE
# ============================================================

def warning_message(symbol):

    coin = symbol.split("/")[0]

    return (
        "Всі готові?\n\n"
        "Скоро дам СИГНАЛ!\n\n"
        f"{coin}USDT Futures\n\n"
        "Timeframe: 10m\n\n"
        "⚠️ Сигнал буде тільки "
        "після закриття 9-ї свічки."
    )


# ============================================================
# FINAL SIGNAL MESSAGE
# ============================================================

def signal_message(
    symbol,
    signal
):

    coin = symbol.split("/")[0]

    if signal["side"] == "LONG":
        title = "🟢 LONG"
    else:
        title = "🔴 SHORT"

    # Entry = close price of #15.
    entry = float(
        signal["c15"][4]
    )

    return (
        f"{title}\n\n"
        f"{coin}USDT Futures\n\n"
        "Timeframe: 10m\n\n"
        f"Entry: {entry}\n\n"
        "Трейдер Василь Павлів\n\n"
        "t.me/vasylpavliv"
    )


# ============================================================
# PROCESS SYMBOL
# ============================================================

def process(symbol):

    try:

        raw = fetch_raw_5m(symbol)

        live_price = fetch_live_price(
            symbol
        )

        completed = build_10m(raw)

        current = build_current_10m(
            raw,
            live_price
        )

        if len(completed) < 15:

            raise RuntimeError(
                "not enough completed "
                f"10m candles: {len(completed)}"
            )

        # ====================================================
        # PRE-SIGNAL
        # ====================================================

        warning = pre_signal(
            completed,
            current
        )

        if warning:

            key = (
                symbol,
                int(warning["c9"][0]),
                "PRE",
            )

            if key not in warning_keys:

                text = warning_message(
                    symbol
                )

                if telegram(text):

                    warning_keys.add(
                        key
                    )

                print(
                    "\n=== PRE-SIGNAL ===\n"
                    + text
                    + "\n=================\n",
                    flush=True
                )

        # ====================================================
        # FINAL CONFIRMATION
        # ====================================================
        #
        # This uses ONLY fully CLOSED candles.
        #
        # Therefore #15 must already be closed.
        #

        signal = find_signal(
            completed
        )

        if not signal:
            return

        # Entry = close of #15.
        entry = float(
            signal["c15"][4]
        )

        key = (
            symbol,
            signal["side"],
            int(signal["c15"][0]),
            "SIGNAL",
        )

        if key in sent_keys:
            return

        text = signal_message(
            symbol,
            signal
        )

        if telegram(text):

            sent_keys.add(
                key
            )

        print(
            "\n=== FINAL SIGNAL ===\n"
            + text
            + "\n====================\n",
            flush=True
        )

    except Exception as e:

        msg = str(e)

        if (
            last_error.get(symbol)
            != msg
        ):

            print(
                f"[FETCH ERROR] "
                f"{symbol}: {msg}",
                flush=True
            )

            last_error[symbol] = msg


# ============================================================
# MAIN
# ============================================================

def main():

    print(
        "=== BTC + ETH 10m "
        "6-9 / 10-15 ANY WIN BOT "
        "STARTING ===",
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
        "Timeframe: 10m "
        "(built from 5m candles)",
        flush=True
    )

    print(
        "Rule: #1 Start; "
        "#6-#9 opposite Start; "
        "ANY #10-#15 Start color = WIN",
        flush=True
    )

    print(
        f"Pre-signal: "
        f"{PRE_MIN_SECONDS}-"
        f"{PRE_MAX_SECONDS}s "
        "before #9 close",
        flush=True
    )

    print(
        "Final confirmation: "
        "only after candle #15 closes",
        flush=True
    )

    print(
        "If no #10-#15 candle "
        "matches Start color = LOSS",
        flush=True
    )

    print(
        f"Chat ID: {CHAT_ID}",
        flush=True
    )

    print(
        "Signal format: "
        "LONG/SHORT + Futures + "
        "Timeframe + Entry + footer",
        flush=True
    )

    print(
        "Connecting to MEXC...",
        flush=True
    )

    exchange.load_markets()

    print(
        "MEXC connected. "
        f"Markets loaded: "
        f"{len(exchange.markets)}",
        flush=True
    )

    print(
        "=== BTC + ETH 10m "
        "6-9 / 10-15 ANY WIN BOT "
        "RUNNING ===",
        flush=True
    )

    cycle = 0

    while True:

        started = time.time()

        for symbol in SYMBOLS:

            process(symbol)

        cycle += 1

        if cycle % 10 == 0:

            print(
                "[HEARTBEAT] "
                "BTC+ETH bot alive | "
                f"scan={SCAN_SECONDS}s",
                flush=True
            )

        elapsed = (
            time.time()
            - started
        )

        sleep_for = max(
            1,
            SCAN_SECONDS - elapsed
        )

        print(
            f"[CYCLE] completed in "
            f"{elapsed:.1f}s | "
            f"sleep {sleep_for:.1f}s",
            flush=True
        )

        time.sleep(
            sleep_for
        )


if __name__ == "__main__":
    main()
