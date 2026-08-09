"""
signal_stats/signal_tracker.py — pure resolution logic + recording orchestration
for the signal performance / paper-trading statistics layer.

Everything that touches the database goes through a `store` parameter
(defaults to signal_stats.signal_store) instead of importing signal_store
functions directly by name. This is the seam tests use to inject an
in-memory fake store — see test_statistics.py — so the WIN/LOSS/RSI/combo/
dedup logic can be verified with synthetic candles without a live Postgres
connection.

Methodology recap (see DECISIONS.md #11/#12 for the full derivation —
unchanged from the first stats prototype, just re-implemented on Postgres):

  INVALIDATION (triggers LOSS) — rolling fast-Donchian opposite band
    (recomputed from live daily candles each check) for breakout /
    turtle_zone / breakout_turtle_combo; the exact trap level, frozen, for
    failure_test.
  CONFIRMATION (triggers WIN) — rolling slow-Donchian (55-day) same-direction
    band, applied uniformly to all setups.
  PENDING/OPEN — neither hit yet. No timeout, by explicit user decision —
    "EXPIRED" status exists in the schema's vocabulary but is intentionally
    never produced by this module.

No look-ahead: resolution only ever reads highs/lows/closes that run_live.py
already fetched *after* the signal's fired_at, from the live Binance feed at
call time. This module never receives or looks at future candles — it can't,
since it's only given whatever run_live.py has fetched so far.

R-multiple: uses a *frozen* initial risk (distance from entry to the
invalidation band as it existed at signal time), even though the band used
to actually trigger LOSS keeps rolling forward as a trailing stop. Otherwise
"R" would be measured against a moving target and be meaningless to compare
across signals. This mirrors how R multiples are computed in real trading
journals — initial risk, not final stop.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

import config
from technical_signals import _donchian  # same channel math as live detection
from signal_stats import signal_store as _default_store

logger = logging.getLogger(__name__)


# ── RSI (context-only, does not feed signal generation) ──────────────────────

def compute_rsi(closes: list[float], period: int = None) -> Optional[float]:
    """Standard Wilder RSI over the given close series. Returns None if
    there isn't enough history yet. `period` defaults to config.RSI_PERIOD
    (14, the universal default — not a value invented for this project)."""
    period = period or config.RSI_PERIOD
    if len(closes) < period + 1:
        return None

    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


# ── combo-setup decision (Breakout + Turtle Zone same day, same direction) ───

def decide_breakout_turtle_setup(sent_breakout: bool, sent_turtle: bool) -> Optional[str]:
    """Only called when both detectors fired AND both alerts were actually
    sent to Telegram (paper-trading semantics — see DECISIONS.md #12: we
    record what a subscriber would have actually seen). Returns the setup
    label to record, or None if nothing should be recorded as a combo
    (caller then records whichever single one applies, as before)."""
    if sent_breakout and sent_turtle:
        return "breakout_turtle_combo"
    return None


# ── recording ────────────────────────────────────────────────────────────────

async def record_signal(
    symbol: str, direction: str, setup: str,
    entry_price: float, entry_level: float, fast_n: int,
    highs: list[float], lows: list[float], closes: list[float],
    timeframe: str = "1d", candle_close_ts: Optional[int] = None,
    store=_default_store,
) -> Optional[str]:
    """Records a fired-and-actually-sent signal. Returns the new signal id,
    the id of an existing OPEN duplicate (dedup — see module docstring), or
    None if the store is unavailable (DATABASE_URL not set)."""
    existing = await store.find_open_duplicate(symbol, setup, direction, entry_level, timeframe)
    if existing is not None:
        return existing["id"]

    # Frozen initial risk, for R multiples (see module docstring).
    if setup == "failure_test":
        invalidation_at_entry = entry_level
    else:
        fast_upper, fast_lower = _donchian(highs, lows, fast_n)
        invalidation_at_entry = fast_lower if direction == "LONG" else fast_upper
    initial_risk_pct = abs(entry_price - invalidation_at_entry) / entry_price * 100
    if initial_risk_pct == 0:
        # Degenerate case (flat channel) — avoid a division by zero later
        # when computing R multiples; treat as a very tight but nonzero risk.
        initial_risk_pct = 0.01

    rsi_at_entry = compute_rsi(closes)

    sid = str(uuid.uuid4())
    row = await store.insert_signal(
        id=sid, fired_at=datetime.now(timezone.utc), symbol=symbol, direction=direction,
        setup=setup, entry_price=entry_price, entry_level=entry_level, fast_n=fast_n,
        initial_risk_pct=initial_risk_pct, rsi_at_entry=rsi_at_entry,
        timeframe=timeframe, candle_close_ts=candle_close_ts,
    )
    if row is None:
        return None
    return row["id"]


# ── resolution ───────────────────────────────────────────────────────────────

async def resolve_open_signals(symbol: str, highs: list[float], lows: list[float],
                                closes: list[float], timeframe: str = "1d",
                                store=_default_store):
    """Re-checks every OPEN signal for this symbol against the latest daily
    candles fetched by run_live.py. Call once per daily-cache refresh, same
    cadence as the old prototype's evaluate_pending()."""
    if len(closes) < config.TURTLE_SLOW_LOOKBACK + 2:
        return

    slow_upper, slow_lower = _donchian(highs, lows, config.TURTLE_SLOW_LOOKBACK)
    price = closes[-1]

    # Резолвим только сигналы СВОЕГО таймфрейма: дневной сигнал нельзя
    # закрывать по часовым уровням Дончиана и наоборот.
    open_signals = await store.get_open_signals(symbol=symbol, timeframe=timeframe)
    for rec in open_signals:
        direction = rec["direction"]
        entry_price = rec["entry_price"]

        pct_move = (
            (price - entry_price) / entry_price * 100 if direction == "LONG"
            else (entry_price - price) / entry_price * 100
        )
        mfe_pct = max(rec["mfe_pct"], pct_move)
        mae_pct = min(rec["mae_pct"], pct_move)

        resolution = None  # ("win"|"loss", reason)

        if rec["setup"] == "failure_test":
            if direction == "LONG":
                if price < rec["entry_level"]:
                    resolution = ("loss", "level_recross")
                elif price > slow_upper:
                    resolution = ("win", "confirmation_band")
            else:
                if price > rec["entry_level"]:
                    resolution = ("loss", "level_recross")
                elif price < slow_lower:
                    resolution = ("win", "confirmation_band")
        else:
            fast_n = rec["fast_n"]
            if len(closes) < fast_n + 2:
                await store.update_excursion(rec["id"], mfe_pct, mae_pct)
                continue
            fast_upper, fast_lower = _donchian(highs, lows, fast_n)
            if direction == "LONG":
                if price < fast_lower:
                    resolution = ("loss", "invalidation_band")
                elif price > slow_upper:
                    resolution = ("win", "confirmation_band")
            else:
                if price > fast_upper:
                    resolution = ("loss", "invalidation_band")
                elif price < slow_lower:
                    resolution = ("win", "confirmation_band")

        if resolution is None:
            await store.update_excursion(rec["id"], mfe_pct, mae_pct)
            continue

        status, reason = resolution
        r_multiple = pct_move / rec["initial_risk_pct"] if rec["initial_risk_pct"] else None
        await store.resolve_signal(
            id=rec["id"], status=status.upper(),
            resolved_at=datetime.now(timezone.utc), resolved_price=price,
            resolved_reason=reason, mfe_pct=mfe_pct, mae_pct=mae_pct, r_multiple=r_multiple,
        )
        logger.info("statistics: %s %s %s -> %s (%s) @ %.6f, R=%.2f",
                    rec["symbol"], rec["setup"], direction, status.upper(), reason, price,
                    r_multiple if r_multiple is not None else float("nan"))
