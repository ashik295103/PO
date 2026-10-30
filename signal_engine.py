import asyncio
import logging
import os
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import pandas as pd
import requests
from supabase import create_client

SUPABASE_URL = os.getenv("SUPABASE_URL", "https://dcbovugmihmwprsidcvk.supabase.co")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
SYMBOLS = [s.strip().upper() for s in os.getenv("SYMBOLS", "EURUSD,GBPUSD,USDJPY,AUDUSD,USDCAD,EURJPY,GBPJPY").split(",") if s.strip()]
PORT = int(os.getenv("PORT", "10000"))

RSI_PERIOD = 14
EMA_FAST = 9
EMA_SLOW = 21
MAX_CANDLES = 500

YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}

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


def fetch_candles(symbol: str):
    """Yahoo Finance থেকে ১-মিনিট ক্যান্ডেল: [(open_time_ms, close), ...]"""
    r = requests.get(
        YAHOO_URL.format(ticker=f"{symbol}=X"),
        params={"interval": "1m", "range": "1d"},
        headers=HEADERS,
        timeout=15,
    )
    r.raise_for_status()
    result = (r.json().get("chart", {}).get("result") or [None])[0]
    if not result or "timestamp" not in result:
        return []
    closes = result["indicators"]["quote"][0]["close"]
    return [(int(t) * 1000, float(c)) for t, c in zip(result["timestamp"], closes) if c is not None]


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
    strength = int(min(90, 55 + gap_pct * 3000 + abs(last.rsi - 50) * 0.4))

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
            log.info("%s %s -> %s (entry %.5f, close %.5f)", symbol, p["direction"], status, p["entry"], close_price)
        else:
            still_pending.append(p)
    pending[symbol] = still_pending

    # পুরোনো ক্যান্ডেলে (২.৫ মিনিটের বেশি আগের) নতুন সিগন্যাল বানানো হবে না
    if time.time() * 1000 - open_ms > 150_000:
        return

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


def current_minute_ms() -> int:
    return int(time.time() // 60) * 60 * 1000


async def warmup():
    for s in SYMBOLS:
        try:
            rows = await asyncio.to_thread(fetch_candles, s)
            cur = current_minute_ms()
            for t, c in rows:
                if t < cur:
                    candles[s].append((t, c))
            log.info("Warmup %s: %d candles", s, len(candles[s]))
        except Exception as e:
            log.error("Warmup failed for %s: %s", s, e)
        await asyncio.sleep(0.5)


async def poll_loop():
    while True:
        # প্রতি মিনিট শুরুর ৩ সেকেন্ড পরে নতুন ক্লোজড ক্যান্ডেল আনা
        await asyncio.sleep(60 - (time.time() % 60) + 3)
        for s in SYMBOLS:
            try:
                rows = await asyncio.to_thread(fetch_candles, s)
                cur = current_minute_ms()
                for t, c in rows:
                    if t < cur and (not candles[s] or t > candles[s][-1][0]):
                        await on_closed_candle(s, t, c)
            except Exception as e:
                log.warning("Fetch failed for %s: %s", s, e)
            await asyncio.sleep(0.5)


async def main():
    start_health_server()
    await asyncio.to_thread(cleanup_stale)
    await warmup()
    await poll_loop()


if __name__ == "__main__":
    asyncio.run(main())
