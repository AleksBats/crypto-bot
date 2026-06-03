"""
monitors/volume.py — detects abnormal trading volume spikes.

Verified Binance endpoints:
  Klines: GET https://api.binance.com/api/v3/klines?symbol=ASTERUSDT&interval=1h&limit=25
"""

import asyncio
import logging
import statistics

import httpx

import config
from alert_engine import Signal, engine
from state import PriceCandle, state
import telegram_bot as tg

logger = logging.getLogger(__name__)

KLINES_URL = "https://api.binance.com/api/v3/klines"


async def _fetch_volumes(client: httpx.AsyncClient, interval: str, limit: int) -> list[float]:
    resp = await client.get(KLINES_URL, params={
        "symbol": config.SYMBOL_SPOT,
        "interval": interval,
        "limit": limit,
    })
    resp.raise_for_status()
    # index 5 = volume
    return [float(row[5]) for row in resp.json()]


async def run_volume_monitor():
    logger.info("Volume monitor started.")
    async with httpx.AsyncClient(timeout=10) as client:
        while True:
            try:
                # Fetch last 25 1h, 7 4h, 2 1d candles
                vols_1h  = await _fetch_volumes(client, "1h", 25)
                vols_4h  = await _fetch_volumes(client, "4h", 7)
                vols_1d  = await _fetch_volumes(client, "1d", 2)

                # Candle at index[-1] is current (incomplete), [-2] is last complete
                current_vol = vols_1h[-1]
                avg_1h  = statistics.mean(vols_1h[:-1])   # exclude current
                avg_4h  = statistics.mean(vols_4h[:-1])
                avg_24h = vols_1d[-2] if len(vols_1d) >= 2 else avg_1h * 24

                state.avg_volume_1h  = avg_1h
                state.avg_volume_4h  = avg_4h
                state.avg_volume_24h = avg_24h

                ratio = current_vol / avg_1h if avg_1h else 0
                logger.debug("Volume ratio vs 1h avg: %.2f×", ratio)

                if ratio >= config.VOLUME_SPIKE_MULTIPLIER:
                    price = state.last_price or 0.0
                    await engine.submit(Signal(
                        key="volume_spike",
                        strong=ratio >= config.VOLUME_SPIKE_MULTIPLIER * 2,   # 6× = strong alone
                        message=tg.fmt_volume_spike(current_vol, avg_1h, ratio, price),
                        priority=3,
                    ))

            except httpx.HTTPStatusError as e:
                logger.error("Volume fetch HTTP error %s", e.response.status_code)
            except Exception as e:
                logger.error("Volume monitor error: %s", e)

            await asyncio.sleep(config.POLL_PRICE_SECS)
