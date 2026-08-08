"""
run_live.py — Continuous 24/7 local monitoring loop for Aster Intelligence Bot.

Uses only free Binance public API. No Twitter. No whale monitor.
Sends Telegram alerts only when alert_engine detects important signals.

Signals monitored:
  - Volume spike (1h volume vs average)
  - Open Interest change
  - Funding rate extremes
  - Breakout          (daily Donchian channel break)          ← NEW
  - Turtle Zone Filter (dual-channel Turtle-style system)      ← NEW
  - Failure Test       (false-breakout / trap detector)        ← NEW

Run:
    python run_live.py

Stop:
    Ctrl+C  (graceful shutdown, sends Telegram notice)

Logs:
    Console + logs/aster_bot.log

Check interval: 5 minutes (POLL_INTERVAL_SECS)
Technical (daily) signals refresh: 15 minutes (config.POLL_TECHNICAL_SECS)
Alert cooldown: 30 minutes per signal type (from config.py)
"""

import asyncio
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path

import httpx
from dotenv import load_dotenv
from typing import Optional

load_dotenv()

# ── Bootstrap path so local imports work regardless of cwd ───────────────────
sys.path.insert(0, str(Path(__file__).parent))

import config
from alert_engine import Signal, engine
from state import state
import telegram_bot as tg
import technical_signals as ts

# ── Logging setup ─────────────────────────────────────────────────────────────
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

log_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s — %(message)s")

file_handler = RotatingFileHandler(
    LOG_DIR / "aster_bot.log",
    maxBytes=5 * 1024 * 1024,   # 5 MB per file
    backupCount=5,
)
file_handler.setFormatter(log_formatter)

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(log_formatter)

logging.basicConfig(
    level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO")),
    handlers=[file_handler, console_handler],
)
logger = logging.getLogger("run_live")

# ── Constants ─────────────────────────────────────────────────────────────────
POLL_INTERVAL_SECS = int(os.environ.get("POLL_INTERVAL_SECS", "300"))   # 5 min default

SPOT_PRICE_URL  = "https://api.binance.com/api/v3/ticker/price"
SPOT_24HR_URL   = "https://api.binance.com/api/v3/ticker/24hr"
SPOT_KLINES_URL = "https://api.binance.com/api/v3/klines"
FUT_PREMIUM_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"
FUT_OI_URL      = "https://fapi.binance.com/fapi/v1/openInterest"
FUT_FUNDING_URL = "https://fapi.binance.com/fapi/v1/fundingRate"

_futures_available = None   # None = unknown, True = available, False = skip
_shutdown = False

# ── Daily-candle cache for technical signals (refreshed every POLL_TECHNICAL_SECS) ──
# Keyed by symbol so ASTERUSDT and all coins in config.TECHNICAL_SYMBOLS cache independently.
_daily_cache: dict = {}   # symbol -> {"data": {...}, "ts": float}


# ════════════════════════════════════════════════
# HEALTH-CHECK HTTP SERVER (для бесплатного Web Service на Render)
# ════════════════════════════════════════════════
# Render (бесплатный тариф) поддерживает только "Web Service" — требует, чтобы
# приложение слушало $PORT и отвечало на HTTP-запросы, иначе деплой считается
# нездоровым. Бот сам по себе — это asyncio-цикл без веб-сервера, поэтому
# поднимаем крошечный HTTP-сервер в отдельном потоке ТОЛЬКО ради health check —
# он никак не влияет на логику сигналов и алертов.

class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status": "alive", "service": "aster-intelligence-bot"}')

    def log_message(self, format, *args):
        pass  # не засоряем логи health-check запросами


def _start_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = ThreadingHTTPServer(("0.0.0.0", port), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logging.getLogger("run_live").info("Health-check server listening on :%d", port)


# ════════════════════════════════════════════════
# DATA FETCHERS
# ════════════════════════════════════════════════

async def fetch_spot(client: httpx.AsyncClient) -> dict:
    """Returns price, 24h volume, and 1h/4h volume averages."""
    result = {}

    # Price
    r = await client.get(SPOT_PRICE_URL, params={"symbol": config.SYMBOL_SPOT})
    r.raise_for_status()
    result["price"] = float(r.json()["price"])

    # 24h ticker
    r = await client.get(SPOT_24HR_URL, params={"symbol": config.SYMBOL_SPOT})
    r.raise_for_status()
    d = r.json()
    result["vol_24h"]       = float(d["volume"])
    result["vol_quote_24h"] = float(d["quoteVolume"])
    result["change_24h"]    = float(d["priceChangePercent"])
    result["high_24h"]      = float(d["highPrice"])
    result["low_24h"]       = float(d["lowPrice"])

    # 1h candles (last 25) for volume spike detection
    r = await client.get(SPOT_KLINES_URL, params={
        "symbol": config.SYMBOL_SPOT, "interval": "1h", "limit": 25
    })
    r.raise_for_status()
    vols_1h = [float(c[5]) for c in r.json()]
    result["vol_1h_current"] = vols_1h[-1]
    result["vol_1h_avg"]     = sum(vols_1h[:-1]) / len(vols_1h[:-1])
    result["vol_1h_ratio"]   = result["vol_1h_current"] / result["vol_1h_avg"] if result["vol_1h_avg"] else 0

    # 4h candles (last 7) for broader volume context
    r = await client.get(SPOT_KLINES_URL, params={
        "symbol": config.SYMBOL_SPOT, "interval": "4h", "limit": 7
    })
    r.raise_for_status()
    vols_4h = [float(c[5]) for c in r.json()]
    result["vol_4h_avg"] = sum(vols_4h[:-1]) / max(len(vols_4h[:-1]), 1)

    return result


async def fetch_futures(client: httpx.AsyncClient) -> Optional[dict]:
    """Returns funding rate and OI. Returns None if futures not available."""
    global _futures_available
    if _futures_available is False:
        return None

    result = {}

    try:
        r = await client.get(FUT_PREMIUM_URL, params={"symbol": config.SYMBOL_FUTURES})
        if r.status_code == 400:
            if _futures_available is None:
                logger.warning("ASTERUSDT perp not found on Binance Futures — futures monitor disabled.")
            _futures_available = False
            return None

        r.raise_for_status()
        d = r.json()
        result["funding"]    = float(d.get("lastFundingRate", 0)) * 100   # → %
        result["mark_price"] = float(d.get("markPrice", 0))
        _futures_available = True

    except httpx.HTTPStatusError:
        return None

    # OI
    try:
        r = await client.get(FUT_OI_URL, params={"symbol": config.SYMBOL_FUTURES})
        r.raise_for_status()
        result["oi"] = float(r.json()["openInterest"])
    except Exception as e:
        logger.debug("OI fetch failed: %s", e)

    # Funding history (last 2 for change detection)
    try:
        r = await client.get(FUT_FUNDING_URL, params={"symbol": config.SYMBOL_FUTURES, "limit": 2})
        r.raise_for_status()
        history = [float(x["fundingRate"]) * 100 for x in r.json()]
        result["funding_prev"] = history[0] if len(history) >= 2 else result["funding"]
    except Exception as e:
        logger.debug("Funding history fetch failed: %s", e)
        result["funding_prev"] = result.get("funding", 0)

    return result


async def fetch_daily(client: httpx.AsyncClient, symbol: str) -> dict:
    """Fetch daily candles for Donchian-based technical signals (Breakout,
    Turtle Zone Filter, Failure Test) for a given symbol. Cached per-symbol
    for POLL_TECHNICAL_SECS since daily data doesn't change meaningfully
    every 5 minutes."""
    now = time.monotonic()
    cached = _daily_cache.get(symbol)
    if cached is not None and (now - cached["ts"]) < config.POLL_TECHNICAL_SECS:
        return cached["data"]

    r = await client.get(SPOT_KLINES_URL, params={
        "symbol": symbol, "interval": "1d", "limit": config.DAILY_KLINES_LIMIT
    })
    r.raise_for_status()
    rows = r.json()
    result = {
        "highs":  [float(c[2]) for c in rows],
        "lows":   [float(c[3]) for c in rows],
        "closes": [float(c[4]) for c in rows],
    }
    _daily_cache[symbol] = {"data": result, "ts": now}
    return result


# ════════════════════════════════════════════════
# SIGNAL EVALUATION
# ════════════════════════════════════════════════

async def evaluate_signals(spot: dict, futures: Optional[dict], daily: Optional[dict]):
    """Check all thresholds and submit signals to alert_engine."""
    price = spot["price"]

    # ── Update shared state ───────────────────────────────────────────────────
    state.last_price     = price
    state.avg_volume_1h  = spot["vol_1h_avg"]
    state.avg_volume_4h  = spot.get("vol_4h_avg")
    if futures:
        state.last_funding = futures.get("funding")
        if futures.get("oi") is not None:
            prev_oi    = state.last_oi
            state.last_oi = futures["oi"]

    # ── Volume spike ──────────────────────────────────────────────────────────
    ratio = spot["vol_1h_ratio"]
    if ratio >= config.VOLUME_SPIKE_MULTIPLIER:
        strong = ratio >= config.VOLUME_SPIKE_MULTIPLIER * 2
        await engine.submit(Signal(
            key="volume_spike",
            strong=strong,
            message=tg.fmt_volume_spike(
                spot["vol_1h_current"],
                spot["vol_1h_avg"],
                ratio,
                price,
            ),
        ))

    # ── OI change ────────────────────────────────────────────────────────────
    if futures and futures.get("oi") is not None and state.last_oi is not None:
        prev_oi = state.last_oi
        oi      = futures["oi"]
        # prev_oi was set above; recalculate based on oi_history
        if state.oi_history:
            _, prev_oi_hist = state.oi_history[-1] if state.oi_history else (0, oi)
            oi_change_pct = (oi - prev_oi_hist) / prev_oi_hist * 100 if prev_oi_hist else 0
            if abs(oi_change_pct) >= config.OI_CHANGE_PCT_THRESHOLD:
                strong = abs(oi_change_pct) >= config.OI_CHANGE_PCT_THRESHOLD * 2
                event  = "spike" if oi_change_pct > 0 else "dump"
                await engine.submit(Signal(
                    key=f"oi_{event}",
                    strong=strong,
                    message=tg.fmt_oi_alert(event, oi, prev_oi_hist, oi_change_pct, price),
                ))
        state.oi_history.append((time.time(), oi))

    # ── Funding rate ──────────────────────────────────────────────────────────
    if futures and futures.get("funding") is not None:
        funding      = futures["funding"]
        funding_prev = futures.get("funding_prev", funding)
        extreme = abs(funding) >= config.FUNDING_EXTREME_PCT
        sudden  = (
            funding_prev != 0
            and abs((funding - funding_prev) / abs(funding_prev)) >= 0.5
        )
        if extreme or sudden:
            await engine.submit(Signal(
                key="funding_extreme",
                strong=extreme,
                message=tg.fmt_funding_alert(funding, funding_prev),
            ))

    # ── Технические сигналы (daily) — для основного SYMBOL_SPOT (ASTERUSDT) ────
    if daily:
        highs, lows, closes = daily["highs"], daily["lows"], daily["closes"]
        symbol = config.SYMBOL_SPOT

        # Breakout
        bo = ts.detect_breakout(highs, lows, closes, n=config.DONCHIAN_LOOKBACK)
        if bo:
            key = f"breakout_{'bull' if bo['direction'] == 'bullish' else 'bear'}_{symbol}"
            await engine.submit(Signal(
                key=key,
                strong=True,
                message=tg.fmt_breakout_alert(symbol, bo["direction"], bo["level"], bo["price"], bo["n"]),
                priority=2,
            ))

        # Turtle Zone Filter
        tzone = ts.detect_turtle_zone(
            highs, lows, closes,
            fast=config.TURTLE_FAST_LOOKBACK, slow=config.TURTLE_SLOW_LOOKBACK,
        )
        if tzone:
            key = f"turtle_zone_{'bull' if tzone['direction'] == 'bullish' else 'bear'}_{symbol}"
            strong = tzone["stage"] == "confirmed"
            await engine.submit(Signal(
                key=key,
                strong=strong,
                message=tg.fmt_turtle_zone_alert(
                    symbol, tzone["direction"], tzone["stage"],
                    tzone["fast_level"], tzone["slow_level"], tzone["price"],
                ),
                priority=2,
            ))

        # Failure Test
        ft = ts.detect_failure_test(
            highs, lows, closes,
            n=config.DONCHIAN_LOOKBACK, lookback=config.FAILURE_TEST_LOOKBACK,
        )
        if ft:
            key = f"failure_test_{ft['direction'].lower()}_{symbol}"
            await engine.submit(Signal(
                key=key,
                strong=True,
                message=tg.fmt_failure_test_alert(symbol, ft["direction"], ft["level"], ft["price"]),
                priority=2,
            ))


# ════════════════════════════════════════════════
# MULTI-SYMBOL TECHNICAL SCAN (Breakout / Turtle Zone / Failure Test only)
# ════════════════════════════════════════════════

async def scan_technical_symbols(client: httpx.AsyncClient):
    """Проходит по config.TECHNICAL_SYMBOLS и шлёт алерты ТОЛЬКО по трём
    техническим индикаторам (Breakout, Turtle Zone Filter, Failure Test).
    Никаких volume/OI/funding алертов для этих монет — они считаются
    только для основного SYMBOL_SPOT (ASTERUSDT) в evaluate_signals()."""
    for symbol in config.TECHNICAL_SYMBOLS:
        try:
            daily = await fetch_daily(client, symbol)
        except Exception as e:
            logger.warning("Daily klines fetch failed for %s: %s", symbol, e)
            continue

        highs, lows, closes = daily["highs"], daily["lows"], daily["closes"]

        bo = ts.detect_breakout(highs, lows, closes, n=config.DONCHIAN_LOOKBACK)
        if bo:
            key = f"breakout_{'bull' if bo['direction'] == 'bullish' else 'bear'}_{symbol}"
            await engine.submit(Signal(
                key=key,
                strong=True,
                message=tg.fmt_breakout_alert(symbol, bo["direction"], bo["level"], bo["price"], bo["n"]),
                priority=2,
            ))

        tzone = ts.detect_turtle_zone(
            highs, lows, closes,
            fast=config.TURTLE_FAST_LOOKBACK, slow=config.TURTLE_SLOW_LOOKBACK,
        )
        if tzone:
            key = f"turtle_zone_{'bull' if tzone['direction'] == 'bullish' else 'bear'}_{symbol}"
            strong = tzone["stage"] == "confirmed"
            await engine.submit(Signal(
                key=key,
                strong=strong,
                message=tg.fmt_turtle_zone_alert(
                    symbol, tzone["direction"], tzone["stage"],
                    tzone["fast_level"], tzone["slow_level"], tzone["price"],
                ),
                priority=2,
            ))

        ft = ts.detect_failure_test(
            highs, lows, closes,
            n=config.DONCHIAN_LOOKBACK, lookback=config.FAILURE_TEST_LOOKBACK,
        )
        if ft:
            key = f"failure_test_{ft['direction'].lower()}_{symbol}"
            await engine.submit(Signal(
                key=key,
                strong=True,
                message=tg.fmt_failure_test_alert(symbol, ft["direction"], ft["level"], ft["price"]),
                priority=2,
            ))


# ════════════════════════════════════════════════
# MAIN LOOP
# ════════════════════════════════════════════════

def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


async def run_loop():
    global _shutdown
    iteration = 0

    logger.info("=" * 60)
    logger.info("  Aster Intelligence Bot — LIVE MODE")
    logger.info("  Symbol:   %s", config.SYMBOL_SPOT)
    logger.info("  Technical-only symbols: %s", ", ".join(config.TECHNICAL_SYMBOLS))
    logger.info("  Interval: %ds", POLL_INTERVAL_SECS)
    logger.info("  Cooldown: %ds per signal type", config.ALERT_COOLDOWN_SECS)
    logger.info("  Log file: logs/aster_bot.log")
    logger.info("=" * 60)

    await tg.send_alert(
        "🟢 <b>Aster Bot — Live Mode Started</b>\n\n"
        f"Main symbol: <code>{config.SYMBOL_SPOT}</code> — Volume, OI, Funding, Breakout, Turtle Zone, Failure Test\n"
        f"Technical-only: <code>{', '.join(config.TECHNICAL_SYMBOLS)}</code> — Breakout, Turtle Zone, Failure Test\n"
        f"Interval: every {POLL_INTERVAL_SECS // 60} min\n"
        f"Alerts: only on significant signals\n"
        f"Started: {_now_utc()}"
    )

    async with httpx.AsyncClient(
        timeout=12,
        headers={"User-Agent": "AsterIntelBot/1.0"},
    ) as client:

        while not _shutdown:
            iteration += 1
            t_start = time.monotonic()
            logger.info("── Check #%d at %s ──", iteration, _now_utc())

            # ── Spot data ─────────────────────────────────────────────────────
            try:
                spot = await fetch_spot(client)
                logger.info(
                    "SPOT  price=$%.6f  24h=%+.2f%%  vol_ratio=%.2f×  "
                    "24hVol=$%s",
                    spot["price"],
                    spot["change_24h"],
                    spot["vol_1h_ratio"],
                    f"{spot['vol_quote_24h']:,.0f}",
                )
            except Exception as e:
                logger.error("Spot fetch failed: %s", e)
                await asyncio.sleep(30)
                continue

            # ── Futures data (optional) ───────────────────────────────────────
            futures = None
            try:
                futures = await fetch_futures(client)
                if futures:
                    logger.info(
                        "FUTURES  funding=%+.4f%%  OI=%s",
                        futures.get("funding", 0),
                        f"{futures['oi']:,.0f}" if futures.get("oi") else "n/a",
                    )
                else:
                    logger.debug("Futures: not available or skipped.")
            except Exception as e:
                logger.warning("Futures fetch failed: %s", e)

            # ── Daily data for technical signals (cached, refreshed every POLL_TECHNICAL_SECS) ──
            daily = None
            try:
                daily = await fetch_daily(client, config.SYMBOL_SPOT)
                logger.debug("Daily candles: %d loaded (cached).", len(daily["closes"]))
            except Exception as e:
                logger.warning("Daily klines fetch failed: %s", e)

            # ── Evaluate & submit signals (ASTER: volume/OI/funding + technical) ──
            try:
                await evaluate_signals(spot, futures, daily)
            except Exception as e:
                logger.error("Signal evaluation error: %s", e)

            # ── Multi-symbol technical scan (Breakout/Turtle Zone/Failure Test only) ──
            try:
                await scan_technical_symbols(client)
            except Exception as e:
                logger.error("Multi-symbol technical scan error: %s", e)

            # ── Sleep until next interval ─────────────────────────────────────
            elapsed = time.monotonic() - t_start
            sleep_for = max(0, POLL_INTERVAL_SECS - elapsed)
            logger.debug("Check took %.1fs. Sleeping %.0fs.", elapsed, sleep_for)

            # Sleep in small chunks so Ctrl+C is responsive
            deadline = time.monotonic() + sleep_for
            while not _shutdown and time.monotonic() < deadline:
                await asyncio.sleep(min(5, deadline - time.monotonic()))


async def shutdown(reason: str = "manual stop"):
    global _shutdown
    _shutdown = True
    logger.info("Shutdown requested: %s", reason)
    await tg.send_alert(
        f"🔴 <b>Aster Bot — Stopped</b>\n"
        f"Reason: {reason}\n"
        f"Time: {_now_utc()}"
    )


async def main():
    loop = asyncio.get_running_loop()

    # Health-check сервер для Render Web Service (бесплатный тариф) — не мешает
    # основному циклу, просто отвечает "alive" на HTTP-пинги платформы.
    _start_health_server()

    # Handle Ctrl+C and kill signals gracefully
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(
            sig,
            lambda s=sig: asyncio.create_task(shutdown(f"signal {s.name}"))
        )

    try:
        await run_loop()
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.critical("Unhandled error in main loop: %s", e, exc_info=True)
        await tg.send_alert(f"🚨 <b>Aster Bot CRASHED</b>\n{e}")
        raise


if __name__ == "__main__":
    asyncio.run(main())
