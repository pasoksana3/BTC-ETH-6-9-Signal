import os, time
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

exchange = ccxt.mexc({"enableRateLimit": True, "options": {"defaultType": "swap"}})
sent_keys = set()
last_error = {}

def now_ms(): return int(time.time() * 1000)
def utc_text(ms): return datetime.fromtimestamp(ms/1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def color(c):
    o, cl = float(c[1]), float(c[4])
    return "GREEN" if cl > o else "RED" if cl < o else "DOJI"

def emoji(c): return "🟢" if c == "GREEN" else "🔴" if c == "RED" else "⚪"

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
        if b is None or ts + 2*five > now:
            continue
        out.append([ts, float(a[1]), max(float(a[2]), float(b[2])), min(float(a[3]), float(b[3])), float(b[4]), float(a[5]) + float(b[5])])
    return out

def fetch_10m(symbol):
    raw = exchange.fetch_ohlcv(symbol, SOURCE_TIMEFRAME, limit=HISTORY_5M)
    candles = build_10m(raw)
    if len(candles) < 15:
        raise RuntimeError(f"not enough completed 10m candles: {len(candles)}")
    return candles

def find_signal(candles):
    if len(candles) < 10: return None
    i = len(candles) - 10  # #9 is the latest closed 10m candle
    start = candles[i]
    c6, c7, c8, c9 = candles[i+6], candles[i+7], candles[i+8], candles[i+9]
    cs, c6c, c7c, c8c, c9c = color(start), color(c6), color(c7), color(c8), color(c9)
    if cs not in ("GREEN", "RED") or c6c not in ("GREEN", "RED"): return None
    # Start must be the last candle of its same-color run.
    if i + 1 < len(candles) and color(candles[i+1]) == cs: return None
    # #6 changes color; #7-#9 stay with #6.
    if c6c == cs or not (c7c == c6c and c8c == c6c and c9c == c6c): return None
    return {"side": "LONG" if c6c == "GREEN" else "SHORT", "start": start, "c6": c6, "c7": c7, "c8": c8, "c9": c9, "start_color": cs}

def telegram(text):
    if not TELEGRAM_BOT_TOKEN or not CHAT_ID:
        print("[TELEGRAM] not configured"); return
    try:
        r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json={"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": True}, timeout=15)
        if not r.ok: print(f"[TELEGRAM ERROR] {r.status_code}: {r.text}")
    except Exception as e: print(f"[TELEGRAM ERROR] {e}")

def message(symbol, s):
    coin = symbol.split('/')[0]
    side = s["side"]
    c6,c7,c8,c9 = s["c6"],s["c7"],s["c8"],s["c9"]
    entry = float(c9[4])
    title = "🟢 LONG" if side == "LONG" else "🔴 SHORT"
    return (f"{title}\n\n{coin}USDT Futures\nTimeframe: 10m\nLeverage: {LEVERAGE}x\n\n"
            f"6→9 CONFIRMATION\nStart: {emoji(s['start_color'])} {s['start_color']}\n"
            f"6: {emoji(color(c6))} {color(c6)}\n7: {emoji(color(c7))} {color(c7)}\n"
            f"8: {emoji(color(c8))} {color(c8)}\n9: {emoji(color(c9))} {color(c9)}\n\n"
            f"Entry: {entry}\nSignal candle #9 closed: {utc_text(c9[0])}\n\n"
            f"Трейдер Василь Павлів\n@vasylpavliv\nt.me/vasylpavliv")

def process(symbol):
    try:
        candles = fetch_10m(symbol)
        s = find_signal(candles)
        if not s: return
        key = (symbol, s["side"], int(s["c9"][0]))
        if key in sent_keys: return
        sent_keys.add(key)
        text = message(symbol, s)
        print("\n=== SIGNAL ===\n" + text + "\n==============\n")
        telegram(text)
    except Exception as e:
        msg = str(e)
        if last_error.get(symbol) != msg:
            print(f"[FETCH ERROR] {symbol}: {msg}")
            last_error[symbol] = msg

def main():
    print("=== BTC + ETH 10m 6-9 CONFIRMATION BOT STARTING ===")
    print("Imports OK")
    print(f"Symbols: {', '.join(SYMBOLS)}")
    print("Timeframe: 10m (built from closed 5m candles)")
    print("Rule: 6th candle changes color; 7th-9th stay same as 6th")
    print(f"Leverage: {LEVERAGE}x")
    print(f"Telegram configured: {bool(TELEGRAM_BOT_TOKEN and CHAT_ID)}")
    print(f"Chat ID configured: {CHAT_ID or 'NO'}")
    print("Connecting to MEXC...")
    exchange.load_markets()
    print(f"MEXC connected. Markets loaded: {len(exchange.markets)}")
    print("=== BTC + ETH 10m 6-9 CONFIRMATION BOT RUNNING ===")
    n=0
    while True:
        t=time.time()
        for symbol in SYMBOLS: process(symbol)
        n+=1
        if n % 10 == 0: print(f"[HEARTBEAT] BTC+ETH 10m 6-9 BOT alive | scan={SCAN_SECONDS}s")
        elapsed=time.time()-t; sleep=max(1, SCAN_SECONDS-elapsed)
        print(f"[CYCLE] completed in {elapsed:.1f}s | sleep {sleep:.1f}s")
        time.sleep(sleep)

if __name__ == "__main__": main()
