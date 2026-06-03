"""
monitors/funding.py — tracks ASTER perpetual funding rate.

Verified Binance Futures endpoint:
  GET https://fapi.binance.com/fapi/v1/fundingRate?symbol=ASTERUSDT&limit=2

Funding is settled every 8h on Binance (00:00 / 08:00 / 16:00 UTC).
We poll every POLL_FUNDING_SECS and compare to previous reading.
"""

import asyncio
import logging

import httpx

import config
from alert_engine import Signal, engine
from state import state
import telegram_bot as tg

logger = logging.getLogger(__name__)

FUNDING_URL = "https://fapi.binance.com/fapi/v1/fundingRate"


async def _fetch_latest_funding(client: httpx.AsyncClient) -> float:
    resp = await client.get(FUNDING_URL, params={
        "symbol": config.SYMBOL_FUTURES,
        "limit": 2,
    })
    resp.raise_for_status()
    data = resp.json()
    # Returns list sorted oldest→newest; take last entry
    return float(data[-1]["fundingRate"]) * 100   # convert to % (e.g. 0.0001 → 0.01%)


async def run_funding_monitor():
    if not config.SYMBOL_FUTURES:
        logger.info("SYMBOL_FUTURES not set — Funding monitor disabled.")
        return

    logger.info("Funding monitor started.")
    async with httpx.AsyncClient(timeout=10) as client:
        while True:
            try:
                funding = await _fetch_latest_funding(client)
                prev    = state.last_funding

                state.last_funding = funding
                state.funding_history.append(funding)

                if prev is not None:
                    # Extreme absolute level
                    extreme = abs(funding) >= config.FUNDING_EXTREME_PCT
                    # Sudden large change from last reading
                    sudden  = prev != 0 and abs((funding - prev) / abs(prev)) >= 0.5

                    if extreme or sudden:
                        await engine.submit(Signal(
                            key="funding_extreme",
                            strong=extreme,                # extreme absolute = strong alone
                            message=tg.fmt_funding_alert(funding, prev),
                            priority=2,
                        ))

                logger.debug("Funding rate: %+.4f%%", funding)

            except httpx.HTTPStatusError as e:
                if e.response.status_code == 400:
                    logger.warning("ASTERUSDT futures not found — Funding monitor sleeping.")
                    await asyncio.sleep(3600)
                    continue
                logger.error("Funding fetch HTTP error %s", e.response.status_code)
            except Exception as e:
                logger.error("Funding monitor error: %s", e)

            await asyncio.sleep(config.POLL_FUNDING_SECS)
