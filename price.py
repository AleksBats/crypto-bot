"""
monitors/price.py — tracks ASTER price action.

Verified Binance endpoints used:
  Spot price:  GET https://api.binance.com/api/v3/ticker/price?symbol=ASTERUSDT
  Klines:      GET https://api.binance.com/api/v3/klines?symbol=ASTERUSDT&interval=1h&limit=N
"""

import asyncio
import logging
import statistics
import time

import httpx

import config
from alert_engine import Signal, engine
from state import PriceCandle, state
import telegram_bot as tg

logger = logging.getLogger(__name__)

SPOT_PRICE_URL = "https://api.binance.com/api/v3/ticker/price"
KLINES_URL     = "https://api.binance.com/api/v3/klines"


async def _fetch_price(client: httpx.AsyncClient) -> float:
    resp = await client.get(SPOT_PRICE_URL, params={"symbol": config.SYMBOL_SPOT})
    resp.raise_for_status()
    return float(resp.json()["price"])


async def _fetch_klines(client: httpx.AsyncClient, interval: str, limit: int) -> list[PriceCandle]:
    resp = await client.get(KLINES_URL, params={
        "symbol": config.SYMBOL_SPOT,
        "interval": interval,
        "limit": limit,
    })
    resp.raise_for_status()
    candles = []
    for row in resp.json():
        # Binance kline format: [open_time, open, high, low, close, volume, ...]
        candles.append(PriceCandle(
            timestamp=row[0] / 1000,
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]),
        ))
    return candles


def _detect_key_levels(candles: list[PriceCandle]) -> tuple[list[float], list[float]]:
    """Simple swing-high / swing-low detector over last N candles."""
    supports = []
    resistances = []
    closes = [c.close for c in candles]
    for i in range(2, len(closes) - 2):
        # Local min = support
        if closes[i] < closes[i-1] and closes[i] < closes[i-2] \
                and closes[i] < closes[i+1] and closes[i] < closes[i+2]:
            supports.append(closes[i])
        # Local max = resistance
        if closes[i] > closes[i-1] and closes[i] > closes[i-2] \
                and closes[i] > closes[i+1] and closes[i] > closes[i+2]:
            resistances.append(closes[i])
    return sorted(supports[-5:]), sorted(resistances[-5:])


def _nearest_level(price: float, levels: list, tolerance_pct: float = 1.0):
    for lvl in levels:
        if abs(price - lvl) / lvl * 100 <= tolerance_pct:
            return lvl
    return None


async def run_price_monitor():
    logger.info("Price monitor started.")
    async with httpx.AsyncClient(timeout=10) as client:
        # Bootstrap: load 100 × 1h candles
        try:
            candles = await _fetch_klines(client, "1h", 100)
            state.candles_1h.extend(candles)
            state.support_levels, state.resistance_levels = _detect_key_levels(list(state.candles_1h))
            logger.info("Bootstrapped %d 1h candles. Supports: %s  Resistances: %s",
                        len(candles),
                        [f"{l:.6f}" for l in state.support_levels[-3:]],
                        [f"{l:.6f}" for l in state.resistance_levels[-3:]])
        except Exception as e:
            logger.warning("Bootstrap klines failed: %s", e)

        while True:
            try:
                price = await _fetch_price(client)
                prev  = state.last_price

                state.last_price = price

                if prev is None:
                    prev = price

                change_pct = (price - prev) / prev * 100 if prev else 0.0

                # ── Breakout above resistance ───────────────────────────────
                for lvl in state.resistance_levels:
                    if prev <= lvl < price:
                        pct = (price - lvl) / lvl * 100
                        if pct >= config.PRICE_BREAKOUT_PCT:
                            await engine.submit(Signal(
                                key="price_breakout",
                                strong=True,
                                message=tg.fmt_price_alert("breakout", price, change_pct, lvl),
                                priority=2,
                            ))

                # ── Breakdown below support ─────────────────────────────────
                for lvl in state.support_levels:
                    if prev >= lvl > price:
                        pct = (lvl - price) / lvl * 100
                        if pct >= config.PRICE_BREAKOUT_PCT:
                            await engine.submit(Signal(
                                key="price_breakdown",
                                strong=True,
                                message=tg.fmt_price_alert("breakdown", price, change_pct, lvl),
                                priority=2,
                            ))

                # ── Price holds support after volume spike ──────────────────
                if state.avg_volume_1h:
                    near_sup = _nearest_level(price, state.support_levels)
                    if near_sup:
                        await engine.submit(Signal(
                            key="price_holds_support",
                            strong=False,
                            message=tg.fmt_price_alert("holding support", price, change_pct, near_sup),
                        ))

            except httpx.HTTPStatusError as e:
                logger.error("Price fetch HTTP error %s", e.response.status_code)
            except Exception as e:
                logger.error("Price monitor error: %s", e)

            await asyncio.sleep(config.POLL_PRICE_SECS)
