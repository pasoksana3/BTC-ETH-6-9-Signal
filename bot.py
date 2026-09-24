import os
import time
import traceback

print("=== BTC + ETH 10m 6-9 CONFIRMATION BOT STARTING ===", flush=True)

try:
    import ccxt
    import requests
    print("Imports OK", flush=True)
except Exception as e:
    print("IMPORT ERROR:", repr(e), flush=True)
    raise

SYMBOLS = ["BTC/USDT:USDT", "ETH/USDT:USDT"]
TIMEFRAME = "10m"
SCAN_SECONDS = max(15, int(os.getenv("SCAN_SECONDS", "30")))
COOLDOWN_SECONDS = max(60, int(os.getenv("COOLDOWN_SECONDS", "900")))
LEVERAGE = int(os.getenv("LEVERAGE", "30"))
TG = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT = os.getenv("CHAT_ID", "").strip()
last_sent = {}
last_heartbeat = 0.0
HEARTBEAT_SECONDS = 300

print("Symbols:", ", ".join(SYMBOLS), flush=True)
print("Timeframe:", TIMEFRAME, flush=True)
print("Rule: 6th candle changes color; 7th-9th stay same as 6th", flush=True)
print("Leverage:", f"{LEVERAGE}x", flush=True)
print("Telegram configured:", bool(TG and CHAT), flush=True)
print("Chat ID configured:", CHAT if CHAT else "<empty>", flush=True)

ex = ccxt.mexc({"enableRateLimit": True, "options": {"defaultType": "swap"}})

def send(text):
    if not TG or not CHAT:
        print("[TG] NOT CONFIGURED", flush=True)
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG}/sendMessage",
            json={"chat_id": CHAT, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=10,
        )
        print(f"[TG] HTTP {r.status_code}", flush=True)
        if not r.ok:
            print("[TG] RESPONSE", r.text[:500], flush=True)
            return False
        return True
    except Exception as e:
        print("[TG ERROR]", repr(e), flush=True)
        return False

def fetch(symbol, limit=100):
    try:
        return ex.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=limit)
    except Exception as e:
        print(f"[FETCH ERROR] {symbol}: {e}", flush=True)
        return None

def color(candle):
    o, c = float(candle[1]), float(candle[4])
    if c > o:
        return "GREEN"
    if c < o:
        return "RED"
    return "DOJI"

def detect(symbol):
    rows = fetch(symbol, 100)
    if not rows or len(rows) < 15:
        return None

    closed = rows[:-1]  # exclude unfinished 10m candle

    # Test each closed candle as a potential start.
    # Start = LAST candle of a same-color run.
    # Example: GREEN GREEN GREEN -> start from 3rd GREEN.
    for i in range(max(0, len(closed) - 30), len(closed) - 9):
        start_color = color(closed[i])
        if start_color == "DOJI":
            continue
        if i + 1 < len(closed) and color(closed[i + 1]) == start_color:
            continue

        c6 = color(closed[i + 6])
        c7 = color(closed[i + 7])
        c8 = color(closed[i + 8])
        c9 = color(closed[i + 9])

        # 6th MUST change color relative to start.
        if c6 == "DOJI" or c6 == start_color:
            continue
        # 7th, 8th and 9th MUST be the same color as 6th.
        if c7 != c6 or c8 != c6 or c9 != c6:
            continue
        # Signal only when candle 9 is the latest fully closed candle.
        if i + 9 != len(closed) - 1:
            continue

        side = "LONG" if c6 == "GREEN" else "SHORT"
        icon = "🟢" if side == "LONG" else "🔴"
        candle_time = int(closed[i + 9][0])
        key = (symbol, side, candle_time)
        now = time.time()
        if now - last_sent.get(key, 0) < COOLDOWN_SECONDS:
            return None
        last_sent[key] = now

        entry = float(closed[i + 9][4])
        msg = (
            f"{icon} <b>{side}</b>\n\n"
            f"<b>{symbol}</b>\n"
            f"Timeframe: {TIMEFRAME}\n\n"
            f"<b>6→9 CONFIRMATION</b>\n"
            f"Start candle: {start_color}\n"
            f"6th candle: {c6} — color changed\n"
            f"7th candle: {c7}\n"
            f"8th candle: {c8}\n"
            f"9th candle: {c9}\n\n"
            f"Entry: {entry:.8g}\n"
            f"Leverage: {LEVERAGE}x\n\n"
            f"Signal only after 9th candle close.\n\n"
            f"<b>BTC + ETH 10m 6-9 BOT</b>"
        )
        print(f"[SIGNAL] {symbol} {side} start={start_color} 6={c6} 7={c7} 8={c8} 9={c9} entry={entry:.8g}", flush=True)
        sent = send(msg)
        if not sent:
            print(f"[SIGNAL WARNING] {symbol} {side} generated but Telegram send failed", flush=True)
        return sent
    return None

def heartbeat():
    global last_heartbeat
    now = time.time()
    if now - last_heartbeat >= HEARTBEAT_SECONDS:
        last_heartbeat = now
        print(f"[HEARTBEAT] BTC+ETH 10m 6-9 BOT alive | scan={SCAN_SECONDS}s", flush=True)

print("Connecting to MEXC...", flush=True)
try:
    ex.load_markets()
    print(f"MEXC connected. Markets loaded: {len(ex.markets)}", flush=True)
except Exception as e:
    print("[MEXC INIT ERROR]", repr(e), flush=True)
    traceback.print_exc()
    raise

print("=== BTC + ETH 10m 6-9 CONFIRMATION BOT RUNNING ===", flush=True)

while True:
    cycle_start = time.time()
    for symbol in SYMBOLS:
        try:
            detect(symbol)
        except Exception as e:
            print(f"[DETECT ERROR] {symbol}: {e}", flush=True)
            traceback.print_exc()
    heartbeat()
    elapsed = time.time() - cycle_start
    sleep_for = max(1, SCAN_SECONDS - elapsed)
    print(f"[CYCLE] completed in {elapsed:.1f}s | sleep {sleep_for:.1f}s", flush=True)
    time.sleep(sleep_for)
