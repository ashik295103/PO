import asyncio
import json
import logging
import os
import threading
from collections import deque
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import pandas as pd
import requests
import websockets
from supabase import create_client

SUPABASE_URL = os.getenv("SUPABASE_URL", "https://dcbovugmihmwprsidcvk.supabase.co")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
SYMBOLS = [s.strip().upper() for s in os.getenv("SYMBOLS", "BTCUSDT,ETHUSDT,BNBUSDT,XRPUSDT,SOLUSDT").split(",") if s.strip()]
REST_BASE = os.getenv("REST_BASE", "https://data-api.binance.vision")
WS_BASE = os.getenv("WS_BASE", "wss://data-stream.binance.vision")
PORT = int(os.getenv("PORT", "10000"))

RSI_PERIOD = 14
EMA_FAST = 9
EMA_SLOW = 21
WARMUP_CANDLES = 300
MAX_CANDLES = 500

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("signal_engine")

if not SUPABASE_KEY:
    raise SystemExit("SUPABASE_KEY সেট করা নেই। Render Environment-এ Secret key বসান।")

sb = create_client(SUPABASE_URL, SUPABASE_KEY)

candles: dict[str, deque] = {s: deque(maxlen=MAX_CANDLES) for s in SYMBOLS}
pending: dict[str, list] = {s: [] for s in SYMBOLS}


class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"signal engine running")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


def start_health_server():
    server = HTTPServer(("0.0.0.0", PORT), Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("Health server listening on port %s", PORT)


def compute_indicators(symbol: str) -> pd.DataFrame:
    df = pd.DataFrame(list(candles[symbol]), columns=["t", "close"])
    close = df["close"]
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / RSI_PERIOD, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / RSI_PERIOD, adjust=False).mean()
    rs = gain / loss.replace(0, float("nan"))
    df["rsi"] = (100 - 100 / (1 + rs)).fillna(50)
    df["ema_fast"] = close.ewm(span=EMA_FAST, adjust=False).mean()
    df["ema_slow"] = close.ewm(span=EMA_SLOW, adjust=False).mean()
    return df


def analyze(symbol: str):
    if len(candles[symbol]) < max(EMA_SLOW, RSI_PERIOD) * 3:
        return None

    df = compute_indicators(symbol)
    prev, last = df.iloc[-2], df.iloc[-1]

    cross_up = prev.ema_fast <= prev.ema_slow and last.ema_fast > last.ema_slow
    cross_dn = prev.ema_fast >= prev.ema_slow and last.ema_fast < last.ema_slow

    direction, strategy = None, None

    if cross_up and 45 <= last.rsi <= 68:
        direction, strategy = "CALL", "EMA Cross + RSI"
    elif cross_dn and 32 <= last.rsi <= 55:
        direction, strategy = "PUT", "EMA Cross + RSI"
    elif prev.rsi < 30 <= last.rsi and last.close > last.ema_fast:
        direction, strategy = "CALL", "RSI Reversal"
    elif prev.rsi > 70 >= last.rsi and last.close < last.ema_fast:
        direction, strategy = "PUT", "RSI Reversal"

    if not direction:
        return None

    gap_pct = abs(last.ema_fast - last.ema_slow) / last.close * 100
    strength = int(min(90, 55 + gap_pct * 400 + abs(last.rsi - 50) * 0.4))

    return {
        "direction": direction,
        "strategy": strategy,
        "entry_price": float(last.close),
        "rsi": round(float(last.rsi), 2),
        "ema_fast": float(last.ema_fast),
        "ema_slow": float(last.ema_slow),
        "strength": strength,
    }


def db_insert(row: dict):
    try:
        res = sb.table("signals").insert(row).execute()
        return res.data[0]["id"] if res.data else None
    except Exception as e:
        log.warning("Insert failed: %s", e)
        return None


def db_update(sig_id: int, fields: dict):
    try:
        sb.table("signals").update(fields).eq("id", sig_id).execute()
    except Exception as e:
        log.warning("Update failed: %s", e)


def cleanup_stale():
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        sb.table("signals").update({"status": "expired"}).eq("status", "pending").lt("created_at", cutoff).execute()
    except Exception as e:
        log.warning("Cleanup failed: %s", e)


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


async def on_closed_candle(symbol: str, open_ms: int, close_price: float):
    dq = candles[symbol]
    if dq and open_ms <= dq[-1][0]:
        return
    dq.append((open_ms, close_price))

    still_pending = []
    for p in pending[symbol]:
        if open_ms >= p["resolve_at"]:
            if close_price == p["entry"]:
                status = "draw"
            elif (p["direction"] == "CALL") == (close_price > p["entry"]):
                status = "win"
            else:
                status = "loss"
            await asyncio.to_thread(db_update, p["id"], {"status": status, "close_price": close_price})
            log.info("%s %s -> %s (entry %.6f, close %.6f)", symbol, p["direction"], status, p["entry"], close_price)
        else:
            still_pending.append(p)
    pending[symbol] = still_pending

    sig = analyze(symbol)
    if not sig:
        return

    row = {
        "symbol": symbol,
        "timeframe": "1m",
        "candle_time": iso(open_ms),
        "expires_at": iso(open_ms + 120_000),
        "status": "pending",
        **sig,
    }
    sig_id = await asyncio.to_thread(db_insert, row)
    if sig_id:
        pending[symbol].append(
            {"id": sig_id, "direction": sig["direction"], "entry": sig["entry_price"], "resolve_at": open_ms + 60_000}
        )
        log.info("SIGNAL %s %s | %s | RSI %.1f | strength %s", symbol, sig["direction"], sig["strategy"], sig["rsi"], sig["strength"])


def warmup(symbol: str):
    r = requests.get(
        f"{REST_BASE}/api/v3/klines",
        params={"symbol": symbol, "interval": "1m", "limit": WARMUP_CANDLES},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()[:-1]
    for k in data:
        candles[symbol].append((int(k[0]), float(k[4])))
    log.info("Warmup %s: %d candles", symbol, len(candles[symbol]))


async def stream_loop():
    streams = "/".join(f"{s.lower()}@kline_1m" for s in SYMBOLS)
    url = f"{WS_BASE}/stream?streams={streams}"
    backoff = 1
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                log.info("WebSocket connected: %s", ", ".join(SYMBOLS))
                backoff = 1
                async for raw in ws:
                    msg = json.loads(raw).get("data", {})
                    k = msg.get("k")
                    if k and k.get("x"):
                        await on_closed_candle(k["s"], int(k["t"]), float(k["c"]))
        except Exception as e:
            log.warning("WebSocket error: %s - reconnecting in %ss", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)


async def main():
    start_health_server()
    await asyncio.to_thread(cleanup_stale)
    for s in SYMBOLS:
        try:
            await asyncio.to_thread(warmup, s)
        except Exception as e:
            log.error("Warmup failed for %s: %s", s, e)
    await stream_loop()


if __name__ == "__main__":
    asyncio.run(main())
