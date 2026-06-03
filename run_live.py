"""
run_live.py — Continuous 24/7 local monitoring loop for Aster Intelligence Bot.

Uses only free Binance public API. No Twitter. No whale monitor.
Sends Telegram alerts only when alert_engine detects important signals.

Run:
    python run_live.py

Stop:
    Ctrl+C  (graceful shutdown, sends Telegram notice)

Logs:
    Console + logs/aster_bot.log

Check interval: 5 minutes (POLL_INTERVAL_SECS)
Alert cooldown: 30 minutes per signal type (from config.py)
"""

import asyncio
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
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


# ════════════════════════════════════════════════
# SIGNAL EVALUATION
# ════════════════════════════════════════════════

async def evaluate_signals(spot: dict, futures: Optional[dict]):
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
    logger.info("  Interval: %ds", POLL_INTERVAL_SECS)
    logger.info("  Cooldown: %ds per signal type", config.ALERT_COOLDOWN_SECS)
    logger.info("  Log file: logs/aster_bot.log")
    logger.info("=" * 60)

    await tg.send_alert(
        "🟢 <b>Aster Bot — Live Mode Started</b>\n\n"
        f"Symbol: <code>{config.SYMBOL_SPOT}</code>\n"
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
                    "24hVol=$%,.0f",
                    spot["price"],
                    spot["change_24h"],
                    spot["vol_1h_ratio"],
                    spot["vol_quote_24h"],
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

            # ── Evaluate & submit signals ─────────────────────────────────────
            try:
                await evaluate_signals(spot, futures)
            except Exception as e:
                logger.error("Signal evaluation error: %s", e)

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
