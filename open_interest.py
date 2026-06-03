"""
monitors/open_interest.py — tracks ASTER perpetual futures Open Interest.

Verified Binance Futures endpoints:
  Current OI:  GET https://fapi.binance.com/fapi/v1/openInterest?symbol=ASTERUSDT
  OI history:  GET https://fapi.binance.com/futures/data/openInterestHist
                   ?symbol=ASTERUSDT&period=1h&limit=2

Note: ASTER futures may not exist on Binance yet.
If ASTERUSDT is not listed on Binance Futures, set SYMBOL_FUTURES="" in .env
and this monitor will skip gracefully.
"""

import asyncio
import logging

import httpx

import config
from alert_engine import Signal, engine
from state import state
import telegram_bot as tg

logger = logging.getLogger(__name__)

OI_URL      = "https://fapi.binance.com/fapi/v1/openInterest"
OI_HIST_URL = "https://fapi.binance.com/futures/data/openInterestHist"


async def _fetch_oi(client: httpx.AsyncClient) -> float:
    resp = await client.get(OI_URL, params={"symbol": config.SYMBOL_FUTURES})
    resp.raise_for_status()
    return float(resp.json()["openInterest"])


async def run_oi_monitor():
    if not config.SYMBOL_FUTURES:
        logger.info("SYMBOL_FUTURES not set — OI monitor disabled.")
        return

    logger.info("OI monitor started.")
    async with httpx.AsyncClient(timeout=10) as client:
        while True:
            try:
                oi = await _fetch_oi(client)
                price = state.last_price or 0.0

                if state.last_oi is not None:
                    change_pct = (oi - state.last_oi) / state.last_oi * 100

                    if abs(change_pct) >= config.OI_CHANGE_PCT_THRESHOLD:
                        event = "spike" if change_pct > 0 else "dump"
                        # Strong if OI moves same direction as price (trend) or opposite (divergence)
                        strong = abs(change_pct) >= config.OI_CHANGE_PCT_THRESHOLD * 2
                        await engine.submit(Signal(
                            key=f"oi_{event}",
                            strong=strong,
                            message=tg.fmt_oi_alert(event, oi, state.last_oi, change_pct, price),
                            priority=2,
                        ))

                state.last_oi = oi
                state.oi_history.append((asyncio.get_event_loop().time(), oi))

            except httpx.HTTPStatusError as e:
                if e.response.status_code == 400:
                    logger.warning("ASTERUSDT not found on Binance Futures — OI monitor sleeping.")
                    await asyncio.sleep(3600)
                    continue
                logger.error("OI fetch HTTP error %s", e.response.status_code)
            except Exception as e:
                logger.error("OI monitor error: %s", e)

            await asyncio.sleep(config.POLL_OI_SECS)
