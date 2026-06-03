"""
state.py — shared in-memory state for all monitors.
Keeps rolling history and key levels so monitors can compare current vs past.
"""

from collections import deque
from dataclasses import dataclass, field
from typing import Optional
import time


@dataclass
class PriceCandle:
    timestamp: float
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class BotState:
    # ── Price history (up to 100 1h candles) ────────────────────────────────
    candles_1h:  deque = field(default_factory=lambda: deque(maxlen=100))
    candles_4h:  deque = field(default_factory=lambda: deque(maxlen=50))
    candles_24h: deque = field(default_factory=lambda: deque(maxlen=30))

    last_price: Optional[float] = None
    price_at_last_alert: Optional[float] = None

    # ── Key levels (detected from price history) ────────────────────────────
    support_levels:    list = field(default_factory=list)   # [price, ...]
    resistance_levels: list = field(default_factory=list)

    # ── Volume baselines ─────────────────────────────────────────────────────
    avg_volume_1h:  Optional[float] = None
    avg_volume_4h:  Optional[float] = None
    avg_volume_24h: Optional[float] = None

    # ── Open Interest history ────────────────────────────────────────────────
    oi_history: deque = field(default_factory=lambda: deque(maxlen=60))
    last_oi:    Optional[float] = None

    # ── Funding rate history ─────────────────────────────────────────────────
    funding_history: deque = field(default_factory=lambda: deque(maxlen=30))
    last_funding:    Optional[float] = None

    # ── Whale tx seen (dedup by tx hash) ────────────────────────────────────
    seen_whale_txs: set = field(default_factory=set)

    # ── Twitter: last seen tweet IDs ─────────────────────────────────────────
    seen_tweet_ids: set = field(default_factory=set)

    # ── Alert cooldown tracker {signal_key: last_sent_ts} ───────────────────
    alert_cooldowns: dict = field(default_factory=dict)


# Singleton shared across all monitors
state = BotState()
