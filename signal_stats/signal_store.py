"""
signal_stats/signal_store.py — Postgres persistence layer (Neon free tier).

Why Postgres and not a file: Render's free tier has no persistent disk —
any JSON/SQLite file written next to the code is wiped on every redeploy.
Neon's free Postgres plan doesn't expire and survives redeploys/restarts
independently of Render. See DECISIONS.md #12.

This module owns ALL SQL. Nothing else in the project should talk to the
database directly — signal_tracker.py / performance.py / reports.py only
call functions here and work with plain dicts.

Design choices, so future changes don't accidentally reintroduce risk:
- The whole module degrades gracefully if config.DATABASE_URL is empty:
  `get_pool()` returns None and every function becomes a no-op / returns
  empty results instead of raising. This lets run_live.py's core alerting
  keep working even if nobody has provisioned a database yet — see
  config.py's comment on DATABASE_URL.
- One connection pool, created lazily on first use, reused for the life of
  the process (asyncpg's own recommended pattern).
- `entry_level` dedup uses exact float equality on purpose — it's compared
  against a value computed by the exact same technical_signals._donchian()
  call on the exact same daily candle set moments earlier in the same
  process, so it's deterministic, not a rounding-sensitive comparison.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

import asyncpg

import config

logger = logging.getLogger(__name__)

_pool: Optional[asyncpg.Pool] = None
_pool_init_failed = False

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id              TEXT PRIMARY KEY,
    fired_at        TIMESTAMPTZ NOT NULL,
    symbol          TEXT NOT NULL,
    timeframe       TEXT NOT NULL DEFAULT '1d',
    direction       TEXT NOT NULL,              -- LONG | SHORT
    setup           TEXT NOT NULL,               -- breakout | turtle_zone | failure_test | breakout_turtle_combo
    entry_price     DOUBLE PRECISION NOT NULL,
    entry_level     DOUBLE PRECISION NOT NULL,
    fast_n          INTEGER NOT NULL,
    candle_close_ts BIGINT,                      -- close_time свечи-источника (мс), для дедупа по свече
    initial_risk_pct DOUBLE PRECISION NOT NULL, -- |entry - invalidation band| / entry * 100, frozen at entry, for R multiples
    rsi_at_entry    DOUBLE PRECISION,
    -- ── Контекст 4H/1D (Phase 4). ЗАМОРАЖИВАЕТСЯ в момент сигнала и
    -- никогда не пересчитывается — иначе статистика по alignment теряет
    -- смысл. NULL = контекст был недоступен. См. DECISIONS.md #14.
    trend_1d        TEXT,                        -- BULLISH | BEARISH | NEUTRAL
    trend_4h        TEXT,
    structure_4h    TEXT,                        -- HH_HL | LH_LL | MIXED
    alignment       TEXT,                        -- STRONG | PARTIAL | CONFLICT | UNKNOWN
    trendline_slope       DOUBLE PRECISION,      -- цена за миллисекунду
    trendline_anchor_ts   BIGINT,
    trendline_anchor_price DOUBLE PRECISION,
    status          TEXT NOT NULL DEFAULT 'OPEN', -- OPEN | WIN | LOSS
    resolved_at     TIMESTAMPTZ,
    resolved_price  DOUBLE PRECISION,
    resolved_reason TEXT,
    mfe_pct         DOUBLE PRECISION NOT NULL DEFAULT 0,
    mae_pct         DOUBLE PRECISION NOT NULL DEFAULT 0,
    r_multiple      DOUBLE PRECISION,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_signals_status ON signals (status);
CREATE INDEX IF NOT EXISTS idx_signals_fired_at ON signals (fired_at);
CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals (symbol);
CREATE INDEX IF NOT EXISTS idx_signals_open_dedup ON signals (symbol, direction, setup, entry_level)
    WHERE status = 'OPEN';
CREATE INDEX IF NOT EXISTS idx_signals_timeframe ON signals (timeframe);
-- Миграция для БД, созданных до появления часового контура: колонка
-- добавляется идемпотентно, существующие строки остаются с NULL.
ALTER TABLE signals ADD COLUMN IF NOT EXISTS candle_close_ts BIGINT;
-- Миграция Phase 4: контекст тренда. Существующие строки остаются с NULL.
ALTER TABLE signals ADD COLUMN IF NOT EXISTS trend_1d TEXT;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS trend_4h TEXT;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS structure_4h TEXT;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS alignment TEXT;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS trendline_slope DOUBLE PRECISION;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS trendline_anchor_ts BIGINT;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS trendline_anchor_price DOUBLE PRECISION;
CREATE INDEX IF NOT EXISTS idx_signals_alignment ON signals (alignment);
"""


async def get_pool() -> Optional[asyncpg.Pool]:
    """Returns the shared connection pool, or None if DATABASE_URL isn't
    configured or the DB is unreachable. Callers must handle None by
    skipping statistics work — never crash the caller."""
    global _pool, _pool_init_failed
    if _pool is not None:
        return _pool
    if not config.DATABASE_URL:
        return None
    if _pool_init_failed:
        return None
    try:
        _pool = await asyncpg.create_pool(config.DATABASE_URL, min_size=1, max_size=5)
        async with _pool.acquire() as conn:
            await conn.execute(SCHEMA)
        logger.info("statistics: connected to Postgres and ensured schema.")
        return _pool
    except Exception as e:
        logger.error("statistics: could not connect to DATABASE_URL — statistics disabled for this run: %s", e)
        _pool_init_failed = True
        return None


def _row_to_dict(row) -> dict:
    return dict(row) if row is not None else None


# ── writes ───────────────────────────────────────────────────────────────────

async def find_open_duplicate(symbol: str, setup: str, direction: str, entry_level: float,
                               timeframe: str = "1d") -> Optional[dict]:
    pool = await get_pool()
    if pool is None:
        return None
    row = await pool.fetchrow(
        """SELECT * FROM signals
           WHERE status = 'OPEN' AND symbol = $1 AND setup = $2
             AND direction = $3 AND entry_level = $4 AND timeframe = $5
           LIMIT 1""",
        symbol, setup, direction, entry_level, timeframe,
    )
    return _row_to_dict(row)


async def insert_signal(
    id: str, fired_at: datetime, symbol: str, direction: str, setup: str,
    entry_price: float, entry_level: float, fast_n: int, initial_risk_pct: float,
    rsi_at_entry: Optional[float], timeframe: str = "1d",
    candle_close_ts: Optional[int] = None,
    trend_1d: Optional[str] = None, trend_4h: Optional[str] = None,
    structure_4h: Optional[str] = None, alignment: Optional[str] = None,
    trendline_slope: Optional[float] = None, trendline_anchor_ts: Optional[int] = None,
    trendline_anchor_price: Optional[float] = None,
) -> Optional[dict]:
    pool = await get_pool()
    if pool is None:
        logger.debug("statistics: DB unavailable, signal not recorded (%s %s %s)", symbol, setup, direction)
        return None
    row = await pool.fetchrow(
        """INSERT INTO signals
             (id, fired_at, symbol, timeframe, direction, setup,
              entry_price, entry_level, fast_n, initial_risk_pct, rsi_at_entry,
              candle_close_ts, trend_1d, trend_4h, structure_4h, alignment,
              trendline_slope, trendline_anchor_ts, trendline_anchor_price)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19)
           ON CONFLICT (id) DO NOTHING
           RETURNING *""",
        id, fired_at, symbol, timeframe, direction, setup,
        entry_price, entry_level, fast_n, initial_risk_pct, rsi_at_entry,
        candle_close_ts, trend_1d, trend_4h, structure_4h, alignment,
        trendline_slope, trendline_anchor_ts, trendline_anchor_price,
    )
    return _row_to_dict(row)


async def update_excursion(id: str, mfe_pct: float, mae_pct: float):
    """Update running MFE/MAE for a still-OPEN signal (called every time we
    re-check it against fresh daily candles, whether or not it resolves)."""
    pool = await get_pool()
    if pool is None:
        return
    await pool.execute(
        "UPDATE signals SET mfe_pct = $2, mae_pct = $3 WHERE id = $1 AND status = 'OPEN'",
        id, mfe_pct, mae_pct,
    )


async def resolve_signal(
    id: str, status: str, resolved_at: datetime, resolved_price: float,
    resolved_reason: str, mfe_pct: float, mae_pct: float, r_multiple: Optional[float],
):
    pool = await get_pool()
    if pool is None:
        return
    await pool.execute(
        """UPDATE signals SET
             status = $2, resolved_at = $3, resolved_price = $4, resolved_reason = $5,
             mfe_pct = $6, mae_pct = $7, r_multiple = $8
           WHERE id = $1 AND status = 'OPEN'""",
        id, status, resolved_at, resolved_price, resolved_reason, mfe_pct, mae_pct, r_multiple,
    )


# ── reads ────────────────────────────────────────────────────────────────────

async def get_open_signals(symbol: Optional[str] = None,
                            timeframe: Optional[str] = None) -> list[dict]:
    pool = await get_pool()
    if pool is None:
        return []
    if symbol and timeframe:
        rows = await pool.fetch(
            "SELECT * FROM signals WHERE status = 'OPEN' AND symbol = $1 AND timeframe = $2",
            symbol, timeframe)
    elif symbol:
        rows = await pool.fetch("SELECT * FROM signals WHERE status = 'OPEN' AND symbol = $1", symbol)
    else:
        rows = await pool.fetch("SELECT * FROM signals WHERE status = 'OPEN'")
    return [dict(r) for r in rows]


async def get_signals_since(since: datetime) -> list[dict]:
    """All signals fired at or after `since` (timezone-aware UTC), regardless
    of current status — used by /today /week /month reports."""
    pool = await get_pool()
    if pool is None:
        return []
    rows = await pool.fetch("SELECT * FROM signals WHERE fired_at >= $1 ORDER BY fired_at", since)
    return [dict(r) for r in rows]


async def get_all_signals() -> list[dict]:
    pool = await get_pool()
    if pool is None:
        return []
    rows = await pool.fetch("SELECT * FROM signals ORDER BY fired_at")
    return [dict(r) for r in rows]


async def get_first_signal_at() -> Optional[datetime]:
    """Earliest fired_at in the table — used as the start date for /stats."""
    pool = await get_pool()
    if pool is None:
        return None
    val = await pool.fetchval("SELECT MIN(fired_at) FROM signals")
    return val
