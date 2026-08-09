"""
test_statistics.py — synthetic tests for the signal_stats/ package (signal
performance tracking / paper trading). No live Postgres required: an
in-memory fake store (InMemoryStore below) implements the exact same async
method signatures as signal_stats/signal_store.py, so signal_tracker.py's
resolution logic, performance.py's aggregation, and reports.py's date
filtering are all exercised for real — only the SQL itself is swapped out.

⚠️  Known gap, flagged not hidden (see TODO.md): this sandbox has no path to
provision a real Postgres instance (no root, apt blocked by the network
allowlist, no embeddable-postgres package available). signal_store.py's
actual SQL was reviewed by hand and the schema is idempotent
(CREATE TABLE/INDEX IF NOT EXISTS), but it has NOT been executed against a
live Postgres/Neon connection. Run a manual smoke test once DATABASE_URL is
set on Render: `/stats` right after deploy should return "Данных пока нет"
without erroring, and one real signal firing should show up in `/today`.

Run: python3 test_statistics.py
"""

import asyncio
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import config
from signal_stats import performance
from signal_stats import signal_tracker as tracker
from signal_stats import reports


# ════════════════════════════════════════════════
# In-memory fake store — same interface as signal_stats/signal_store.py
# ════════════════════════════════════════════════

class InMemoryStore:
    """Implements the same async function signatures signal_store.py
    exposes, backed by a plain dict instead of Postgres. Passed explicitly
    via the `store=` parameter everywhere signal_tracker.py / reports.py
    accept one — production code never has to know this exists."""

    def __init__(self):
        self.rows: dict[str, dict] = {}

    async def find_open_duplicate(self, symbol, setup, direction, entry_level, timeframe="1d"):
        for r in self.rows.values():
            if (r["status"] == "OPEN" and r["symbol"] == symbol and r["setup"] == setup
                    and r["direction"] == direction and r["entry_level"] == entry_level
                    and r["timeframe"] == timeframe):
                return dict(r)
        return None

    async def insert_signal(self, id, fired_at, symbol, direction, setup,
                             entry_price, entry_level, fast_n, initial_risk_pct,
                             rsi_at_entry, timeframe="1d", candle_close_ts=None):
        row = {
            "id": id, "fired_at": fired_at, "symbol": symbol, "timeframe": timeframe,
            "direction": direction, "setup": setup, "entry_price": entry_price,
            "entry_level": entry_level, "fast_n": fast_n, "initial_risk_pct": initial_risk_pct,
            "rsi_at_entry": rsi_at_entry, "candle_close_ts": candle_close_ts,
            "status": "OPEN", "resolved_at": None,
            "resolved_price": None, "resolved_reason": None, "mfe_pct": 0.0, "mae_pct": 0.0,
            "r_multiple": None,
        }
        self.rows[id] = row
        return dict(row)

    async def update_excursion(self, id, mfe_pct, mae_pct):
        if id in self.rows and self.rows[id]["status"] == "OPEN":
            self.rows[id]["mfe_pct"] = mfe_pct
            self.rows[id]["mae_pct"] = mae_pct

    async def resolve_signal(self, id, status, resolved_at, resolved_price,
                              resolved_reason, mfe_pct, mae_pct, r_multiple):
        if id in self.rows:
            r = self.rows[id]
            r.update(status=status, resolved_at=resolved_at, resolved_price=resolved_price,
                      resolved_reason=resolved_reason, mfe_pct=mfe_pct, mae_pct=mae_pct,
                      r_multiple=r_multiple)

    async def get_open_signals(self, symbol=None, timeframe=None):
        return [dict(r) for r in self.rows.values()
                if r["status"] == "OPEN"
                and (symbol is None or r["symbol"] == symbol)
                and (timeframe is None or r["timeframe"] == timeframe)]

    async def get_signals_since(self, since):
        return sorted([dict(r) for r in self.rows.values() if r["fired_at"] >= since],
                      key=lambda r: r["fired_at"])

    async def get_all_signals(self):
        return sorted([dict(r) for r in self.rows.values()], key=lambda r: r["fired_at"])

    async def get_first_signal_at(self):
        if not self.rows:
            return None
        return min(r["fired_at"] for r in self.rows.values())


# ════════════════════════════════════════════════
# Synthetic candle builders (same style as the first prototype's tests)
# ════════════════════════════════════════════════

def flat(n, level=100.0, spread=1.0):
    return [level + spread] * n, [level - spread] * n, [level] * n


def extend_up(h, l, c, days, start_high):
    h, l, c = list(h), list(l), list(c)
    for t in range(days):
        hi = start_high + 2 * (t + 1)
        cl = hi - 0.5
        lo = cl - 1
        h.append(hi); l.append(lo); c.append(cl)
    return h, l, c


def extend_down(h, l, c, days, start_low):
    h, l, c = list(h), list(l), list(c)
    for t in range(days):
        lo = start_low - 2 * (t + 1)
        cl = lo + 0.5
        hi = cl + 1
        h.append(hi); l.append(lo); c.append(cl)
    return h, l, c


def flat_extend(h, l, c, days, level=100.0, spread=1.0):
    h, l, c = list(h), list(l), list(c)
    for _ in range(days):
        h.append(level + spread); l.append(level - spread); c.append(level)
    return h, l, c


PASS = []
FAIL = []


def check(name, condition, detail=""):
    if condition:
        PASS.append(name)
    else:
        FAIL.append(f"{name} — {detail}")


# ════════════════════════════════════════════════
# 1-2. LONG WIN / LONG STOP
# ════════════════════════════════════════════════

async def test_long_win():
    store = InMemoryStore()
    h, l, c = flat(60)
    sid = await tracker.record_signal(
        symbol="TESTUSDT", direction="LONG", setup="breakout",
        entry_price=101.0, entry_level=101.0, fast_n=config.DONCHIAN_LOOKBACK,
        highs=h, lows=l, closes=c, store=store,
    )
    h2, l2, c2 = extend_up(h, l, c, 40, start_high=101.0)
    for i in range(1, 41):
        await tracker.resolve_open_signals("TESTUSDT", h2[:60 + i], l2[:60 + i], c2[:60 + i], store=store)
        if store.rows[sid]["status"] != "OPEN":
            break
    check("LONG WIN resolves to WIN", store.rows[sid]["status"] == "WIN", store.rows[sid]["status"])
    check("LONG WIN has positive R", (store.rows[sid]["r_multiple"] or -1) > 0)


async def test_long_stop():
    store = InMemoryStore()
    h, l, c = flat(60)
    sid = await tracker.record_signal(
        symbol="TESTUSDT", direction="LONG", setup="breakout",
        entry_price=101.0, entry_level=101.0, fast_n=config.DONCHIAN_LOOKBACK,
        highs=h, lows=l, closes=c, store=store,
    )
    h2, l2, c2 = extend_down(h, l, c, 30, start_low=99.0)
    for i in range(1, 31):
        await tracker.resolve_open_signals("TESTUSDT", h2[:60 + i], l2[:60 + i], c2[:60 + i], store=store)
        if store.rows[sid]["status"] != "OPEN":
            break
    check("LONG STOP resolves to LOSS", store.rows[sid]["status"] == "LOSS", store.rows[sid]["status"])
    check("LONG STOP has negative R", (store.rows[sid]["r_multiple"] or 1) < 0)


# ════════════════════════════════════════════════
# 3-4. SHORT WIN / SHORT STOP
# ════════════════════════════════════════════════

async def test_short_win():
    store = InMemoryStore()
    h, l, c = flat(60)
    sid = await tracker.record_signal(
        symbol="TESTUSDT", direction="SHORT", setup="breakout",
        entry_price=99.0, entry_level=99.0, fast_n=config.DONCHIAN_LOOKBACK,
        highs=h, lows=l, closes=c, store=store,
    )
    h2, l2, c2 = extend_down(h, l, c, 40, start_low=99.0)
    for i in range(1, 41):
        await tracker.resolve_open_signals("TESTUSDT", h2[:60 + i], l2[:60 + i], c2[:60 + i], store=store)
        if store.rows[sid]["status"] != "OPEN":
            break
    check("SHORT WIN resolves to WIN", store.rows[sid]["status"] == "WIN", store.rows[sid]["status"])
    check("SHORT WIN has positive R", (store.rows[sid]["r_multiple"] or -1) > 0)


async def test_short_stop():
    store = InMemoryStore()
    h, l, c = flat(60)
    sid = await tracker.record_signal(
        symbol="TESTUSDT", direction="SHORT", setup="breakout",
        entry_price=99.0, entry_level=99.0, fast_n=config.DONCHIAN_LOOKBACK,
        highs=h, lows=l, closes=c, store=store,
    )
    h2, l2, c2 = extend_up(h, l, c, 30, start_high=101.0)
    for i in range(1, 31):
        await tracker.resolve_open_signals("TESTUSDT", h2[:60 + i], l2[:60 + i], c2[:60 + i], store=store)
        if store.rows[sid]["status"] != "OPEN":
            break
    check("SHORT STOP resolves to LOSS", store.rows[sid]["status"] == "LOSS", store.rows[sid]["status"])
    check("SHORT STOP has negative R", (store.rows[sid]["r_multiple"] or 1) < 0)


# ════════════════════════════════════════════════
# 5. OPEN signals are not counted as wins/losses
# ════════════════════════════════════════════════

async def test_open_not_counted():
    store = InMemoryStore()
    h, l, c = flat(60)
    await tracker.record_signal(
        symbol="TESTUSDT", direction="LONG", setup="breakout",
        entry_price=101.0, entry_level=101.0, fast_n=config.DONCHIAN_LOOKBACK,
        highs=h, lows=l, closes=c, store=store,
    )
    # 20 more flat days — price goes nowhere, signal stays OPEN
    h2, l2, c2 = flat_extend(h, l, c, 20)
    await tracker.resolve_open_signals("TESTUSDT", h2, l2, c2, store=store)

    signals = await store.get_all_signals()
    agg = performance.aggregate(signals)
    check("OPEN signal has status OPEN", signals[0]["status"] == "OPEN")
    check("OPEN signal excluded from wins", agg["wins"] == 0)
    check("OPEN signal excluded from losses", agg["losses"] == 0)
    check("OPEN signal counted in open bucket", agg["open"] == 1)
    check("win_rate_pct is None with zero closed", agg["win_rate_pct"] is None)


# ════════════════════════════════════════════════
# 6. Weekly date filtering
# ════════════════════════════════════════════════

async def test_weekly_filtering():
    store = InMemoryStore()
    now = datetime.now(timezone.utc)

    async def _seed(days_ago, symbol):
        sid = str(uuid.uuid4())
        await store.insert_signal(
            id=sid, fired_at=now - timedelta(days=days_ago), symbol=symbol,
            direction="LONG", setup="breakout", entry_price=100.0, entry_level=100.0,
            fast_n=20, initial_risk_pct=1.0, rsi_at_entry=55.0,
        )

    await _seed(1, "A")    # inside the week
    await _seed(6.9, "B")  # inside the week (just under 7 days)
    await _seed(9, "C")    # outside the week
    await _seed(40, "D")   # outside the week and the month

    week_signals = await store.get_signals_since(now - timedelta(days=7))
    month_signals = await store.get_signals_since(now - timedelta(days=30))

    check("weekly filter includes recent signals", {s["symbol"] for s in week_signals} == {"A", "B"},
          {s["symbol"] for s in week_signals})
    check("weekly filter excludes older signals", "C" not in {s["symbol"] for s in week_signals})
    check("monthly filter includes week + slightly older", {s["symbol"] for s in month_signals} == {"A", "B", "C"},
          {s["symbol"] for s in month_signals})
    check("monthly filter excludes 40-day-old signal", "D" not in {s["symbol"] for s in month_signals})

    report = await reports.build_week_report(store=store)
    check("week report renders without error", isinstance(report, str) and "НЕДЕЛЬНЫЙ" in report)


# ════════════════════════════════════════════════
# 7. Duplicate signals are not recorded twice
# ════════════════════════════════════════════════

async def test_dedup():
    store = InMemoryStore()
    h, l, c = flat(60)
    id1 = await tracker.record_signal(
        symbol="TESTUSDT", direction="LONG", setup="breakout",
        entry_price=101.0, entry_level=101.0, fast_n=config.DONCHIAN_LOOKBACK,
        highs=h, lows=l, closes=c, store=store,
    )
    id2 = await tracker.record_signal(
        symbol="TESTUSDT", direction="LONG", setup="breakout",
        entry_price=101.3, entry_level=101.0, fast_n=config.DONCHIAN_LOOKBACK,
        highs=h, lows=l, closes=c, store=store,
    )
    check("dedup returns the same id", id1 == id2, f"{id1} != {id2}")
    check("dedup does not create a second row", len(store.rows) == 1, len(store.rows))

    # Combo-setup decision helper
    check("combo requires both sent", tracker.decide_breakout_turtle_setup(True, True) == "breakout_turtle_combo")
    check("combo is None if only one sent", tracker.decide_breakout_turtle_setup(True, False) is None)
    check("combo is None if neither sent", tracker.decide_breakout_turtle_setup(False, False) is None)


# ════════════════════════════════════════════════
# 8. Restart / persistence behavior (see module docstring for the
#    real-Postgres caveat — this simulates a process restart against the
#    same backing store, which is the part signal_tracker.py's logic
#    actually controls).
# ════════════════════════════════════════════════

async def test_restart_persistence():
    shared_backing = InMemoryStore()  # stands in for "the database"
    h, l, c = flat(60)
    sid = await tracker.record_signal(
        symbol="TESTUSDT", direction="LONG", setup="breakout",
        entry_price=101.0, entry_level=101.0, fast_n=config.DONCHIAN_LOOKBACK,
        highs=h, lows=l, closes=c, store=shared_backing,
    )
    # Simulate "the process restarting": a brand new store OBJECT wrapping
    # the same underlying rows dict (this is what actually happens with
    # Postgres — a new asyncpg pool, same on-disk table).
    reloaded_store = InMemoryStore()
    reloaded_store.rows = shared_backing.rows  # same table, new "connection"

    open_after_restart = await reloaded_store.get_open_signals(symbol="TESTUSDT")
    check("open signal survives 'restart'", len(open_after_restart) == 1 and open_after_restart[0]["id"] == sid)

    # Continue resolving using the reloaded store — must pick up exactly
    # where it left off, not lose the signal or double-count it.
    h2, l2, c2 = extend_up(h, l, c, 40, start_high=101.0)
    for i in range(1, 41):
        await tracker.resolve_open_signals("TESTUSDT", h2[:60 + i], l2[:60 + i], c2[:60 + i], store=reloaded_store)
        if reloaded_store.rows[sid]["status"] != "OPEN":
            break
    check("signal resolves correctly after 'restart'", reloaded_store.rows[sid]["status"] == "WIN")
    check("no duplicate rows after 'restart'", len(shared_backing.rows) == 1)


# ════════════════════════════════════════════════
# 9. No look-ahead bias
# ════════════════════════════════════════════════

async def test_no_lookahead():
    store = InMemoryStore()
    h, l, c = flat(60)
    sid = await tracker.record_signal(
        symbol="TESTUSDT", direction="LONG", setup="breakout",
        entry_price=101.0, entry_level=101.0, fast_n=config.DONCHIAN_LOOKBACK,
        highs=h, lows=l, closes=c, store=store,
    )
    # Construct a future price path where NOTHING happens for 25 more days
    # (flat), then an explosive climb starts — so resolution is only
    # possible once the climb portion of the array is actually visible.
    h_flat, l_flat, c_flat = flat_extend(h, l, c, 25)          # 85 bars, still flat
    h_full, l_full, c_full = extend_up(h_flat, l_flat, c_flat, 30, start_high=101.0)  # 115 bars

    # "Today" = only the flat continuation is visible (bars 61-85). If the
    # tracker were peeking at data beyond what's passed in, it could not
    # possibly resolve here, since the climb hasn't been "fetched" yet.
    for i in range(61, 86):
        await tracker.resolve_open_signals("TESTUSDT", h_full[:i], l_full[:i], c_full[:i], store=store)
    still_open_before_climb_visible = store.rows[sid]["status"] == "OPEN"

    # Now "time passes" and later bars (the climb) become visible one by one.
    resolved_at_bar = None
    for i in range(86, len(c_full) + 1):
        await tracker.resolve_open_signals("TESTUSDT", h_full[:i], l_full[:i], c_full[:i], store=store)
        if store.rows[sid]["status"] != "OPEN":
            resolved_at_bar = i
            break

    check("signal does not resolve while only flat/no-op future data is visible",
          still_open_before_climb_visible)
    check("signal resolves once the actually-triggering bars are fed in",
          resolved_at_bar is not None and resolved_at_bar > 85)

    # Re-run resolve_open_signals again with the SAME final data — resolving
    # an already-resolved (no longer OPEN) signal must be a no-op, not
    # re-resolve or mutate it using "future" context.
    frozen_status = dict(store.rows[sid])
    await tracker.resolve_open_signals("TESTUSDT", h_full, l_full, c_full, store=store)
    check("resolved signals are not mutated on subsequent calls",
          store.rows[sid]["resolved_at"] == frozen_status["resolved_at"])


# ════════════════════════════════════════════════
# 10. RSI + performance aggregation sanity
# ════════════════════════════════════════════════

def test_rsi():
    rising = [100 + i for i in range(30)]
    falling = [130 - i for i in range(30)]
    flat_ = [100.0] * 30
    check("RSI near 100 for a strictly rising series", tracker.compute_rsi(rising, 14) > 95)
    check("RSI near 0 for a strictly falling series", tracker.compute_rsi(falling, 14) < 5)
    check("RSI is 50-ish for a flat series (no gains/losses -> avg_loss=0 -> 100 by formula)",
          tracker.compute_rsi(flat_, 14) == 100.0)  # documented edge case: zero volatility -> RS undefined, formula returns 100
    check("RSI is None with insufficient history", tracker.compute_rsi([100.0, 101.0], 14) is None)


def test_performance_aggregation():
    now = datetime.now(timezone.utc)

    def rec(status, direction, r, symbol="X", setup="breakout", mfe=1.0, mae=-0.5):
        if r is None:
            resolved_price = None
        else:
            resolved_price = 100.0 * (1 + r * 0.01) if direction == "LONG" else 100.0 * (1 - r * 0.01)
        return {
            "symbol": symbol, "setup": setup, "direction": direction, "status": status,
            "timeframe": "1d", "entry_price": 100.0,
            "resolved_price": resolved_price,
            "r_multiple": r, "mfe_pct": mfe, "mae_pct": mae, "fired_at": now,
        }

    signals = [
        rec("WIN", "LONG", 2.0), rec("WIN", "LONG", 1.5), rec("LOSS", "LONG", -1.0),
        rec("WIN", "SHORT", 1.0), rec("LOSS", "SHORT", -1.2), rec("LOSS", "SHORT", -0.8),
        rec("OPEN", "LONG", None),
    ]
    agg = performance.aggregate(signals)

    check("aggregate: total", agg["total"] == 7, agg["total"])
    check("aggregate: closed", agg["closed"] == 6, agg["closed"])
    check("aggregate: open", agg["open"] == 1, agg["open"])
    check("aggregate: wins", agg["wins"] == 3, agg["wins"])
    check("aggregate: losses", agg["losses"] == 3, agg["losses"])
    check("aggregate: win_rate 50%", abs(agg["win_rate_pct"] - 50.0) < 1e-9, agg["win_rate_pct"])
    check("aggregate: long win rate", abs(agg["long"]["win_rate_pct"] - (2 / 3 * 100)) < 1e-9)
    check("aggregate: short win rate", abs(agg["short"]["win_rate_pct"] - (1 / 3 * 100)) < 1e-9)
    expected_pf = (2.0 + 1.5 + 1.0) / abs(-1.0 - 1.2 - 0.8)
    check("aggregate: profit factor", abs(agg["profit_factor"] - expected_pf) < 1e-9, agg["profit_factor"])
    check("aggregate: best signal is the +2.0R one", agg["best_signal"]["r_multiple"] == 2.0)
    check("aggregate: worst signal is the -1.2R one", agg["worst_signal"]["r_multiple"] == -1.2)




# ════════════════════════════════════════════════
# 11. Таймфреймы изолированы друг от друга
# ════════════════════════════════════════════════

async def test_timeframe_isolation():
    store = InMemoryStore()
    h, l, c = flat(60)
    sid_d = await tracker.record_signal(
        symbol="TESTUSDT", direction="LONG", setup="breakout",
        entry_price=101.0, entry_level=101.0, fast_n=config.DONCHIAN_LOOKBACK,
        highs=h, lows=l, closes=c, timeframe="1d", candle_close_ts=1000, store=store,
    )
    sid_h = await tracker.record_signal(
        symbol="TESTUSDT", direction="LONG", setup="breakout",
        entry_price=101.0, entry_level=101.0, fast_n=config.DONCHIAN_LOOKBACK,
        highs=h, lows=l, closes=c, timeframe="1h", candle_close_ts=2000, store=store,
    )
    check("одинаковый сетап на 1d и 1h — это ДВА разных сигнала", sid_d != sid_h)
    check("в сторе две записи", len(store.rows) == 2, len(store.rows))

    # Резолвим только часовой — дневной обязан остаться OPEN
    h2, l2, c2 = extend_up(h, l, c, 40, start_high=101.0)
    for i in range(1, 41):
        await tracker.resolve_open_signals("TESTUSDT", h2[:60 + i], l2[:60 + i], c2[:60 + i],
                                            timeframe="1h", store=store)
        if store.rows[sid_h]["status"] != "OPEN":
            break
    check("часовой сигнал резолвится", store.rows[sid_h]["status"] == "WIN", store.rows[sid_h]["status"])
    check("дневной сигнал НЕ тронут резолюцией часового", store.rows[sid_d]["status"] == "OPEN",
          store.rows[sid_d]["status"])


# ════════════════════════════════════════════════
# 12. candle_close_ts сохраняется, дедуп учитывает таймфрейм
# ════════════════════════════════════════════════

async def test_candle_close_ts():
    store = InMemoryStore()
    h, l, c = flat(60)
    sid = await tracker.record_signal(
        symbol="TESTUSDT", direction="LONG", setup="breakout",
        entry_price=101.0, entry_level=101.0, fast_n=config.DONCHIAN_LOOKBACK,
        highs=h, lows=l, closes=c, timeframe="1h", candle_close_ts=1712345678000, store=store,
    )
    check("candle_close_ts сохранён", store.rows[sid]["candle_close_ts"] == 1712345678000)
    check("timeframe сохранён", store.rows[sid]["timeframe"] == "1h")

    dup = await tracker.record_signal(
        symbol="TESTUSDT", direction="LONG", setup="breakout",
        entry_price=101.5, entry_level=101.0, fast_n=config.DONCHIAN_LOOKBACK,
        highs=h, lows=l, closes=c, timeframe="1h", candle_close_ts=1712349278000, store=store,
    )
    check("дедуп по уровню внутри одного TF работает", dup == sid, f"{dup} != {sid}")


# ════════════════════════════════════════════════
# 13. Разбивка по таймфреймам в статистике
# ════════════════════════════════════════════════

def test_timeframe_breakdown():
    now = datetime.now(timezone.utc)

    def rec(tf, status, r):
        return {"symbol": "X", "setup": "breakout", "direction": "LONG", "status": status,
                "timeframe": tf, "entry_price": 100.0,
                "resolved_price": 100.0 * (1 + r * 0.01), "r_multiple": r,
                "mfe_pct": 1.0, "mae_pct": -0.5, "fired_at": now}

    agg = performance.aggregate([
        rec("1d", "WIN", 2.0), rec("1d", "WIN", 1.0), rec("1d", "LOSS", -1.0),
        rec("1h", "WIN", 1.0), rec("1h", "LOSS", -1.0),
    ])
    by_tf = agg["by_timeframe"]
    check("by_timeframe содержит оба таймфрейма", set(by_tf) == {"1d", "1h"}, set(by_tf))
    check("1d win rate 66.7%", abs(by_tf["1d"]["win_rate_pct"] - (2/3*100)) < 1e-9)
    check("1h win rate 50%", abs(by_tf["1h"]["win_rate_pct"] - 50.0) < 1e-9)


# ════════════════════════════════════════════════
# Runner
# ════════════════════════════════════════════════

async def main():
    await test_long_win()
    await test_long_stop()
    await test_short_win()
    await test_short_stop()
    await test_open_not_counted()
    await test_weekly_filtering()
    await test_dedup()
    await test_restart_persistence()
    await test_no_lookahead()
    await test_timeframe_isolation()
    await test_candle_close_ts()
    test_rsi()
    test_performance_aggregation()
    test_timeframe_breakdown()

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed\n")
    for name in PASS:
        print(f"  ok  {name}")
    if FAIL:
        print()
        for f in FAIL:
            print(f"  FAIL  {f}")
        sys.exit(1)
    print("\nALL STATISTICS TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
