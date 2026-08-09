"""
signal_stats/performance.py — pure aggregation over a list of signal rows.

Takes plain dicts shaped like signal_stats.signal_store rows (or the
equivalent from an in-memory test store) and computes every number the
weekly/monthly/all-time reports need. No I/O, no database — easy to unit
test with synthetic rows.

Deliberate omissions (see reports.py for how these surface as N/A):
- Timeframe breakdown IS computed now (`by_timeframe`) — since the hourly
  contour was added, `timeframe` genuinely varies between "1d" and "1h".
  Before that it was deliberately omitted as an honest N/A.
- No indicator-confluence score beyond `setup` (which already includes
  "breakout_turtle_combo" when both fired the same day — see
  signal_tracker.decide_breakout_turtle_setup). No separate "confluence"
  metric is invented.
- "Best/worst symbol" and "best/worst setup" require at least
  config.MIN_SAMPLE_FOR_RANKING closed signals to be eligible, so a single
  lucky/unlucky trade doesn't look like a trend.
"""

from typing import Optional

import config


def _pct_move(rec: dict) -> Optional[float]:
    """Realized % move at resolution, in the predicted direction (positive
    = favorable). None for signals that haven't resolved yet."""
    if rec.get("resolved_price") is None:
        return None
    entry = rec["entry_price"]
    exit_price = rec["resolved_price"]
    if rec["direction"] == "LONG":
        return (exit_price - entry) / entry * 100
    return (entry - exit_price) / entry * 100


def _direction_breakdown(signals: list[dict], direction: str) -> dict:
    subset = [s for s in signals if s["direction"] == direction]
    closed = [s for s in subset if s["status"] in ("WIN", "LOSS")]
    wins = [s for s in closed if s["status"] == "WIN"]
    losses = [s for s in closed if s["status"] == "LOSS"]
    return {
        "signals": len(subset),
        "wins": len(wins),
        "losses": len(losses),
        "open": len(subset) - len(closed),
        "win_rate_pct": (len(wins) / len(closed) * 100) if closed else None,
    }


def _group_breakdown(closed_signals: list[dict], key: str) -> dict:
    groups: dict = {}
    for s in closed_signals:
        groups.setdefault(s[key], []).append(s)
    result = {}
    for k, subset in groups.items():
        wins = [s for s in subset if s["status"] == "WIN"]
        rs = [s["r_multiple"] for s in subset if s.get("r_multiple") is not None]
        result[k] = {
            "signals": len(subset),
            "wins": len(wins),
            "losses": len(subset) - len(wins),
            "win_rate_pct": len(wins) / len(subset) * 100,
            "avg_r": (sum(rs) / len(rs)) if rs else None,
            "avg_mfe_pct": sum(s["mfe_pct"] for s in subset) / len(subset),
            "avg_mae_pct": sum(s["mae_pct"] for s in subset) / len(subset),
        }
    return result


def _best_worst_by_win_rate(breakdown: dict, min_sample: int = None) -> tuple[Optional[str], Optional[str]]:
    min_sample = min_sample if min_sample is not None else config.MIN_SAMPLE_FOR_RANKING
    eligible = {k: v for k, v in breakdown.items() if v["signals"] >= min_sample}
    if not eligible:
        return None, None
    best = max(eligible.items(), key=lambda kv: kv[1]["win_rate_pct"])[0]
    worst = min(eligible.items(), key=lambda kv: kv[1]["win_rate_pct"])[0]
    return best, worst


def aggregate(signals: list[dict]) -> dict:
    """Full stats bundle for a list of signal rows (already filtered to the
    desired date window by the caller, e.g. via signal_store.get_signals_since)."""
    closed = [s for s in signals if s["status"] in ("WIN", "LOSS")]
    open_signals = [s for s in signals if s["status"] == "OPEN"]
    wins = [s for s in closed if s["status"] == "WIN"]
    losses = [s for s in closed if s["status"] == "LOSS"]

    winner_pcts = [p for p in (_pct_move(s) for s in wins) if p is not None]
    loser_pcts = [p for p in (_pct_move(s) for s in losses) if p is not None]

    r_values = [s["r_multiple"] for s in closed if s.get("r_multiple") is not None]
    win_r = [s["r_multiple"] for s in wins if s.get("r_multiple") is not None]
    loss_r = [s["r_multiple"] for s in losses if s.get("r_multiple") is not None]
    sum_win_r = sum(win_r) if win_r else 0.0
    sum_loss_r = sum(loss_r) if loss_r else 0.0  # already negative

    profit_factor = (sum_win_r / abs(sum_loss_r)) if loss_r and sum_loss_r != 0 else None

    by_symbol = _group_breakdown(closed, "symbol")
    by_timeframe = _group_breakdown(closed, "timeframe")
    # Группировка по согласованности со старшими таймфреймами (Phase 4).
    # Сигналы без контекста (alignment IS NULL) в разбивку не попадают —
    # они не относятся ни к одной группе, и подмешивать их было бы враньём.
    with_align = [s for s in closed if s.get("alignment")]
    by_alignment = _group_breakdown(with_align, "alignment")
    by_trend_4h = _group_breakdown([s for s in closed if s.get("trend_4h")], "trend_4h")
    by_setup = _group_breakdown(closed, "setup")
    best_symbol, worst_symbol = _best_worst_by_win_rate(by_symbol)
    best_setup, worst_setup = _best_worst_by_win_rate(by_setup)

    best_signal = max(closed, key=lambda s: s["r_multiple"]) if r_values else None
    worst_signal = min(closed, key=lambda s: s["r_multiple"]) if r_values else None

    return {
        "total": len(signals),
        "closed": len(closed),
        "open": len(open_signals),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": (len(wins) / len(closed) * 100) if closed else None,

        "long": _direction_breakdown(signals, "LONG"),
        "short": _direction_breakdown(signals, "SHORT"),

        "avg_winner_pct": (sum(winner_pcts) / len(winner_pcts)) if winner_pcts else None,
        "avg_loser_pct": (sum(loser_pcts) / len(loser_pcts)) if loser_pcts else None,
        "avg_r": (sum(r_values) / len(r_values)) if r_values else None,
        "total_r": sum(r_values) if r_values else None,
        "profit_factor": profit_factor,
        "avg_mfe_pct": (sum(s["mfe_pct"] for s in closed) / len(closed)) if closed else None,
        "avg_mae_pct": (sum(s["mae_pct"] for s in closed) / len(closed)) if closed else None,

        "best_signal": best_signal,
        "worst_signal": worst_signal,

        "by_symbol": by_symbol,
        "by_timeframe": by_timeframe,
        "by_alignment": by_alignment,
        "by_trend_4h": by_trend_4h,
        "aligned_closed": len(with_align),
        "by_setup": by_setup,
        "best_symbol": best_symbol,
        "worst_symbol": worst_symbol,
        "best_setup": best_setup,
        "worst_setup": worst_setup,
    }
