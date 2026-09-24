import os
import json
import time
import logging
from collections import OrderedDict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_IDS = [x.strip() for x in os.getenv("TELEGRAM_CHAT_ID", "").split(",") if x.strip()]
SYMBOLS = [x.strip().upper() for x in os.getenv("MEXC_SYMBOLS", "BTC_USDT,ETH_USDT").split(",") if x.strip()]
POLL = int(os.getenv("POLL_SECONDS", "15"))
STATE_DIR = os.getenv("STATE_DIR", ".")
URL = "https://api.mexc.com/api/v1/contract/kline/{symbol}"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("btc-eth-10m-rule1")


def utc(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def kyiv(ts):
    return datetime.fromtimestamp(ts, tz=ZoneInfo("Europe/Kyiv")).strftime("%Y-%m-%d %H:%M")


def symbol_text(symbol):
    return symbol.replace("_USDT", "USDT")


def candle_color(c):
    if c["close"] > c["open"]:
        return "GREEN"
    if c["close"] < c["open"]:
        return "RED"
    return "DOJI"


def load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"pending": [], "last_processed_10m": None}


def save_state(state, path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, path)


def telegram(text, group_text=None):
    if not TOKEN or not CHAT_IDS:
        log.error("TELEGRAM NOT CONFIGURED")
        return False

    ok_count = 0
    for chat_id in CHAT_IDS:
        try:
            send_text = group_text if (group_text is not None and chat_id.startswith("-")) else text
            r = requests.post(
                f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": send_text,
                    "disable_web_page_preview": True,
                },
                timeout=15,
            )
            body = r.json()
            if r.ok and body.get("ok"):
                ok_count += 1
                log.info("TELEGRAM SENT | chat=%s", chat_id)
            else:
                log.error(
                    "TELEGRAM ERROR | chat=%s | status=%s | body=%s",
                    chat_id, r.status_code, r.text[:300]
                )
        except Exception as e:
            log.exception("TELEGRAM EXCEPTION | chat=%s: %s", chat_id, e)

    return ok_count == len(CHAT_IDS)


def fetch_1m(symbol):
    r = requests.get(
        URL.format(symbol=symbol),
        params={
            "interval": "Min1",
            "limit": 300,
            "_ts": int(time.time() * 1000),
        },
        headers={
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "User-Agent": "BTC-ETH-10m-Rule1-Bot/1.0",
        },
        timeout=15,
    )
    r.raise_for_status()
    payload = r.json()
    data = payload.get("data")
    if not data:
        raise RuntimeError(f"MEXC empty response: {payload}")

    out = []

    if isinstance(data, dict) and isinstance(data.get("time"), list):
        times = data["time"]
        opens = data.get("open", [])
        closes = data.get("close", [])
        for i, ts in enumerate(times):
            out.append({
                "ts": int(ts),
                "open": float(opens[i]),
                "close": float(closes[i]),
            })

    elif isinstance(data, list):
        for row in data:
            if isinstance(row, dict):
                out.append({
                    "ts": int(row.get("time", row.get("t"))),
                    "open": float(row.get("open", row.get("o"))),
                    "close": float(row.get("close", row.get("c"))),
                })
            else:
                out.append({
                    "ts": int(row[0]),
                    "open": float(row[1]),
                    "close": float(row[2]),
                })
    else:
        raise RuntimeError(f"Unknown MEXC data format: {type(data).__name__}")

    out.sort(key=lambda x: x["ts"])
    return out


def current_10m(mins):
    if not mins:
        return None

    bucket = (mins[-1]["ts"] // 600) * 600
    rows = [x for x in mins if (x["ts"] // 600) * 600 == bucket]
    if not rows:
        return None

    rows.sort(key=lambda x: x["ts"])
    return {
        "ts": bucket,
        "open": rows[0]["open"],
        "close": rows[-1]["close"],
        "count": len(rows),
    }


def aggregate_closed_10m(mins):
    buckets = OrderedDict()
    for c in mins:
        b = (c["ts"] // 600) * 600
        buckets.setdefault(b, []).append(c)

    now = int(time.time())
    out = []

    for b, rows in buckets.items():
        rows.sort(key=lambda x: x["ts"])

        # Require a fully closed 10m candle and at least 9 one-minute
        # candles. This avoids treating a partial candle as closed.
        if b + 600 <= now and len(rows) >= 9:
            out.append({
                "ts": b,
                "open": rows[0]["open"],
                "close": rows[-1]["close"],
                "count": len(rows),
            })

    return out


class Engine:
    """
    RULE #1 ONLY — shared by BTCUSDT and ETHUSDT.

    Sequence:
      Start -> #1 -> #2 -> #3 -> #4 -> #5 -> #6 -> #7 -> #8 -> #9

    Mandatory confirmation:
      #6 == #7 == #8 == #9 in candle color.
      #6-#9 must be opposite to Start.

    Signal:
      Sent ONLY after #9 fully closes.

    PRE-ALERT:
      Sent ONLY during the final minute of #9, while #6/#7/#8
      are already confirmed and the live #9 has the same color.

    Result:
      Only #10-#15 are used.
      LONG: any GREEN in #10-#15 => WIN; all RED => LOSS.
      SHORT: any RED in #10-#15 => WIN; all GREEN => LOSS.

    If #6-#9 are not all the same color, there is NO signal.
    """

    def __init__(self, symbol, state_file):
        self.symbol = symbol
        self.state_file = state_file
        self.state = load_state(state_file)
        self.candles = OrderedDict()
        self.pending = []
        self.pre_alerted = set()
        self.initialized = False

        # Restore pending setups from disk if present.
        for p in self.state.get("pending", []):
            self.pending.append(p)

    def seed(self, closed):
        self.candles = OrderedDict((x["ts"], x) for x in closed[-180:])
        self.candles = OrderedDict(sorted(self.candles.items()))

        # On startup we do not generate historical signals/results.
        # Existing persisted pending setups are kept only if their
        # start/trigger timestamps still exist in the current history.
        valid = []
        keys = set(self.candles)
        for p in self.pending:
            if p.get("start_ts") in keys and p.get("trigger_ts") in keys:
                valid.append(p)
        self.pending = valid

        self.state["pending"] = self.pending
        self.state["last_processed_10m"] = next(reversed(self.candles)) if self.candles else None
        save_state(self.state, self.state_file)

        self.initialized = True
        if self.candles:
            latest = next(reversed(self.candles.values()))
            log.info(
                "INITIALIZED | %s | history=%d | latest=%s %s | waiting for NEW 10m candle",
                symbol_text(self.symbol), len(self.candles), utc(latest["ts"]),
                candle_color(latest)
            )

    def maybe_pre_alert(self, live10):
        """
        PRE-ALERT for RULE #1.

        live10 is #9.
        Therefore:
          Start = #9 - 9 candles
          #6    = #9 - 3 candles
          #7    = #9 - 2 candles
          #8    = #9 - 1 candle
          #9    = live10

        Last-minute window:
          elapsed >= 540 seconds and < 600 seconds.
        """
        if not self.initialized or not live10:
            return

        now = time.time()
        elapsed = now - live10["ts"]

        # ONLY final minute of #9.
        if elapsed < 540 or elapsed >= 600:
            return

        target_ts = live10["ts"]

        # The live #9 must not already be a closed candle in history.
        if target_ts in self.candles:
            return

        keys = list(self.candles)
        start_ts = target_ts - 9 * 600
        c6_ts = target_ts - 3 * 600
        c7_ts = target_ts - 2 * 600
        c8_ts = target_ts - 1 * 600

        if not all(ts in self.candles for ts in (start_ts, c6_ts, c7_ts, c8_ts)):
            return

        start = self.candles[start_ts]
        c6 = self.candles[c6_ts]
        c7 = self.candles[c7_ts]
        c8 = self.candles[c8_ts]

        sc = candle_color(start)
        c6c = candle_color(c6)
        c7c = candle_color(c7)
        c8c = candle_color(c8)
        c9c = candle_color(live10)

        if sc not in ("GREEN", "RED"):
            return

        # Start must be the LAST candle of its same-color run.
        try:
            sidx = keys.index(start_ts)
        except ValueError:
            return

        if sidx + 1 < len(keys) and candle_color(self.candles[keys[sidx + 1]]) == sc:
            return

        # Critical condition: #6 = #7 = #8 = #9
        if c6c not in ("GREEN", "RED"):
            return
        if not (c6c == c7c == c8c == c9c):
            return

        # #6-#9 must be opposite Start.
        if c6c == sc:
            return

        if target_ts in self.pre_alerted:
            return

        direction = "LONG" if sc == "GREEN" else "SHORT"

        text = (
            f"Всі готові?\n"
            f"Скоро дам СИГНАЛ!\n\n"
            f"{symbol_text(self.symbol)} Futures\n"
            f"Timeframe: 10m\n\n"
            f"⚠️ Сигнал буде тільки після закриття свічки #9."
        )

        telegram(text, text)
        self.pre_alerted.add(target_ts)

        log.info(
            "PRE-ALERT | %s | %s | Start=%s %s | #6-#9=%s",
            symbol_text(self.symbol), direction,
            utc(start_ts), sc, c6c
        )

    def ingest_new(self, closed):
        if not self.initialized:
            self.seed(closed)
            return 0

        known = set(self.candles)
        new = [x for x in closed if x["ts"] not in known]

        for x in new:
            self.candles[x["ts"]] = x

        self.candles = OrderedDict(sorted(self.candles.items()))
        while len(self.candles) > 180:
            self.candles.popitem(last=False)

        for x in new:
            self.evaluate(x["ts"])

        if new:
            self.state["pending"] = self.pending
            self.state["last_processed_10m"] = new[-1]["ts"]
            save_state(self.state, self.state_file)

        return len(new)

    def evaluate(self, ts):
        keys = list(self.candles)
        if ts not in self.candles:
            return

        idx = keys.index(ts)
        cur = self.candles[ts]

        # ------------------------------------------------------------
        # 1) EXISTING PENDING SETUPS
        # ------------------------------------------------------------
        keep = []

        for p in self.pending:
            trigger_idx = p["trigger_idx"]
            rel = idx - trigger_idx
            trigger_color = p["trigger_color"]

            # #7, #8, #9 MUST match #6.
            if rel in (1, 2, 3):
                if candle_color(cur) != trigger_color:
                    log.info(
                        "CANCEL SETUP | %s | start=%s | #%d changed color",
                        symbol_text(self.symbol),
                        utc(p["start_ts"]),
                        rel + 6
                    )
                    continue

                # Signal ONLY after #9 closes.
                if rel == 3 and not p.get("signal_sent", False):
                    direction = p["direction"]
                    signal = (
                        f"🟢 SIGNAL {direction}\n\n"
                        f"{symbol_text(self.symbol)} Futures\n"
                        f"Timeframe: 10m\n"
                        f"Start: {kyiv(p['start_ts'])} Kyiv time\n"
                        f"Confirmed: #6 = #7 = #8 = #9 ({trigger_color})\n\n"
                        f"Signal only - no automatic trading.\n\n"
                        f"Трейдер Василь Павлів\n"
                        f"@vasylpavliv\n"
                        f"https://t.me/vasylpavliv"
                    )
                    telegram(signal, signal)
                    p["signal_sent"] = True
                    log.info(
                        "SIGNAL SENT | %s | %s | Start=%s | #6-#9=%s",
                        symbol_text(self.symbol), direction,
                        utc(p["start_ts"]), trigger_color
                    )

                keep.append(p)
                continue

            # Before #7-#9, keep the setup alive.
            if rel < 1:
                keep.append(p)
                continue

            # --------------------------------------------------------
            # 2) RESULT ONLY ON #10-#15
            # --------------------------------------------------------
            # rel=4 => candle #10
            # rel=9 => candle #15
            if rel < 4:
                keep.append(p)
                continue

            want = "GREEN" if p["direction"] == "LONG" else "RED"

            if rel <= 9 and candle_color(cur) == want:
                result = (
                    f"🟢 WIN\n"
                    f"{symbol_text(self.symbol)} Futures\n"
                    f"Direction: {p['direction']}\n"
                    f"Result candle: #{rel + 6}/15\n\n"
                    f"Трейдер Василь Павлів\n"
                    f"@vasylpavliv\n"
                    f"https://t.me/vasylpavliv"
                )
                telegram(result, result)
                log.info(
                    "RESULT WIN | %s | %s | result=#%d",
                    symbol_text(self.symbol), p["direction"], rel + 6
                )
                continue

            if rel == 9:
                loss = (
                    f"🔴 LOSS\n"
                    f"{symbol_text(self.symbol)} Futures\n"
                    f"Direction: {p['direction']}\n"
                    f"All #10-#15 were {trigger_color}\n\n"
                    f"Трейдер Василь Павлів\n"
                    f"@vasylpavliv\n"
                    f"https://t.me/vasylpavliv"
                )
                telegram(loss, loss)
                log.info(
                    "RESULT LOSS | %s | %s | all #10-#15=%s",
                    symbol_text(self.symbol), p["direction"], trigger_color
                )
                continue

            keep.append(p)

        self.pending = keep

        # ------------------------------------------------------------
        # 3) CREATE NEW CANDIDATE AT #6
        # ------------------------------------------------------------
        # Current candle is #6, therefore Start is 6 candles earlier.
        if idx < 6:
            return

        start_idx = idx - 6
        start = self.candles[keys[start_idx]]
        sc = candle_color(start)
        trig = candle_color(cur)

        if sc not in ("GREEN", "RED"):
            return
        if trig not in ("GREEN", "RED"):
            return

        # Start must be the LAST candle of its same-color run.
        if start_idx + 1 < len(keys):
            if candle_color(self.candles[keys[start_idx + 1]]) == sc:
                return

        # #6 must be opposite Start.
        if trig == sc:
            return

        direction = "LONG" if sc == "GREEN" else "SHORT"

        if any(p["start_ts"] == start["ts"] for p in self.pending):
            return

        self.pending.append({
            "start_ts": start["ts"],
            "trigger_idx": idx,
            "trigger_ts": ts,
            "trigger_color": trig,
            "direction": direction,
            "signal_sent": False,
        })

        log.info(
            "CANDIDATE #6 | %s | %s | Start=%s %s | #6=%s | waiting #7-#9",
            symbol_text(self.symbol), direction,
            utc(start["ts"]), sc, trig
        )


def build_engines():
    engines = {}
    for symbol in SYMBOLS:
        state_file = os.path.join(
            STATE_DIR,
            f"state_{symbol.replace('/', '_')}.json"
        )
        engines[symbol] = Engine(symbol, state_file)
    return engines


def main():
    log.info("STARTED | BTCUSDT + ETHUSDT 10m RULE #1")
    log.info("Symbols: %s", ", ".join(SYMBOLS))
    log.info(
        "RULE: Start -> #1..#5 -> #6=#7=#8=#9 -> SIGNAL after #9 -> result #10..#15"
    )
    log.info(
        "RESULT: LONG any GREEN in #10..#15 = WIN; all RED = LOSS"
    )
    log.info(
        "RESULT: SHORT any RED in #10..#15 = WIN; all GREEN = LOSS"
    )

    if TOKEN and CHAT_IDS:
        telegram(
            "BOT ONLINE\nBTCUSDT + ETHUSDT Futures\nRule #1 is active.\n"
            "#6=#7=#8=#9 required. Signal after #9.",
            "BOT ONLINE\nBTCUSDT + ETHUSDT Futures\nRule #1 is active.\n"
            "#6=#7=#8=#9 required. Signal after #9."
        )
    else:
        log.error("Telegram is not configured.")

    engines = build_engines()
    last_heartbeat = {}

    while True:
        for symbol, engine in engines.items():
            try:
                mins = fetch_1m(symbol)
                live10 = current_10m(mins)

                # PRE-ALERT is checked before ingesting closed candles,
                # because #9 must still be live.
                if live10:
                    engine.maybe_pre_alert(live10)

                closed = aggregate_closed_10m(mins)

                if not closed:
                    log.warning("%s | no closed 10m candles", symbol_text(symbol))
                    continue

                latest = closed[-1]
                added = engine.ingest_new(closed)

                if added:
                    log.info(
                        "NEW DATA | %s | 1m=%d | closed10m=%d | added=%d | latest=%s %s | pending=%d",
                        symbol_text(symbol), len(mins), len(closed), added,
                        utc(latest["ts"]), candle_color(latest), len(engine.pending)
                    )
                else:
                    now = time.time()
                    if now - last_heartbeat.get(symbol, 0) >= 60:
                        age = max(int(now - (latest["ts"] + 600)), 0)
                        log.info(
                            "HEARTBEAT | %s | latest=%s %s | age=%ss | pending=%d",
                            symbol_text(symbol), utc(latest["ts"]),
                            candle_color(latest), age, len(engine.pending)
                        )
                        last_heartbeat[symbol] = now

            except Exception as e:
                log.exception("LOOP ERROR | %s | %s", symbol_text(symbol), e)

        time.sleep(POLL)


if __name__ == "__main__":
    main()
