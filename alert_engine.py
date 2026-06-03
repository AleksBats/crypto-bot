"""
alert_engine.py — aggregates signals and decides when to send Telegram alerts.

Rules:
- Any single "strong" signal triggers an alert.
- Multiple "weak" signals together trigger a combo alert.
- Cooldown per signal type prevents spam.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import config
import telegram_bot as tg
from state import state

logger = logging.getLogger(__name__)


@dataclass
class Signal:
    key: str             # unique identifier for cooldown, e.g. "whale", "oi_spike"
    strong: bool         # strong = alert alone; weak = only alert in combo
    message: str         # pre-formatted Telegram text
    priority: int = 5    # 1 = highest


class AlertEngine:
    def __init__(self):
        self._pending_weak: list[Signal] = []
        self._lock = asyncio.Lock()

    def _is_on_cooldown(self, key: str) -> bool:
        last = state.alert_cooldowns.get(key, 0)
        return (time.time() - last) < config.ALERT_COOLDOWN_SECS

    def _mark_sent(self, key: str):
        state.alert_cooldowns[key] = time.time()

    async def submit(self, signal: Signal):
        """Submit a signal for evaluation. Thread-safe via asyncio lock."""
        async with self._lock:
            if self._is_on_cooldown(signal.key):
                logger.debug("Signal %s is on cooldown, skipping.", signal.key)
                return

            if signal.strong:
                await tg.send_alert(signal.message)
                self._mark_sent(signal.key)
                # Also flush any pending weak signals alongside it
                if self._pending_weak:
                    combo_names = [s.key for s in self._pending_weak]
                    logger.info("Flushing %d pending weak signals with strong alert.", len(combo_names))
                    self._pending_weak.clear()
            else:
                # Accumulate weak signals; fire combo if ≥ 2 unique keys
                existing_keys = {s.key for s in self._pending_weak}
                if signal.key not in existing_keys:
                    self._pending_weak.append(signal)

                if len(self._pending_weak) >= 2:
                    await self._send_combo()

    async def _send_combo(self):
        names = [s.key.replace("_", " ").title() for s in self._pending_weak]
        combo_msg = tg.fmt_combo_alert(names)
        # Append individual details
        details = "\n\n" + "\n\n".join(s.message for s in self._pending_weak)
        await tg.send_alert(combo_msg + details)
        for s in self._pending_weak:
            self._mark_sent(s.key)
        self._pending_weak.clear()


# Singleton
engine = AlertEngine()
