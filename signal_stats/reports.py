"""
signal_stats/reports.py — builds the Telegram-ready text for /today /week
/month /stats. Formatting matches the mockup approved by the user in chat
(HTML-lite bold, emoji, no ТАЙМФРЕЙМЫ block — see DECISIONS.md #12).

Each build_*_report() function returns a ready-to-send HTML string (same
parse_mode="HTML" the rest of telegram_bot.py already uses).
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

import config
from signal_stats import performance
from signal_stats import signal_store as _default_store

SETUP_LABELS = {
    "breakout": "Breakout",
    "turtle_zone": "Turtle Zone",
    "failure_test": "Failure Test",
    "breakout_turtle_combo": "Breakout + Turtle",
}


def _label(setup: str) -> str:
    return SETUP_LABELS.get(setup, setup.replace("_", " ").title())


def _pct(v: Optional[float]) -> str:
    return f"{v:+.1f}%" if v is not None else "N/A"


def _r(v: Optional[float]) -> str:
    return f"{v:+.2f}R" if v is not None else "N/A"


def _wr(v: Optional[float]) -> str:
    return f"{v:.1f}%" if v is not None else "N/A"


def _pf(v: Optional[float]) -> str:
    return f"{v:.2f}" if v is not None else "N/A (недостаточно проигрышей)"


def _signal_one_liner(rec: dict) -> str:
    if rec is None:
        return "N/A"
    r_txt = f" ({rec['r_multiple']:+.2f}R)" if rec.get("r_multiple") is not None else ""
    return f"{rec['symbol']} • {rec['timeframe']} • {rec['direction']} • {_label(rec['setup'])}{r_txt}"


def _core_block(agg: dict) -> list[str]:
    lines = []
    lines.append(f"Сигналов: {agg['total']}")
    lines.append(f"Закрыто: {agg['closed']}")
    lines.append(f"Открыто: {agg['open']}")
    lines.append("")
    lines.append(f"✅ WIN: {agg['wins']}")
    lines.append(f"❌ LOSS: {agg['losses']}")
    wr_note = " <i>(мало данных)</i>" if agg["closed"] < 5 else ""
    lines.append(f"🎯 Win Rate: {_wr(agg['win_rate_pct'])}{wr_note}")
    return lines


def _direction_block(agg: dict) -> list[str]:
    long_ = agg["long"]; short_ = agg["short"]
    return [
        "",
        "🟢 <b>LONG</b>",
        f"Сигналов: {long_['signals']}",
        f"WIN: {long_['wins']}",
        f"LOSS: {long_['losses']}",
        f"Win Rate: {_wr(long_['win_rate_pct'])}",
        "",
        "🔴 <b>SHORT</b>",
        f"Сигналов: {short_['signals']}",
        f"WIN: {short_['wins']}",
        f"LOSS: {short_['losses']}",
        f"Win Rate: {_wr(short_['win_rate_pct'])}",
    ]


def _performance_block(agg: dict) -> list[str]:
    return [
        "",
        "📈 <b>РЕЗУЛЬТАТИВНОСТЬ</b>",
        f"Средний WIN: {_pct(agg['avg_winner_pct'])}",
        f"Средний LOSS: {_pct(agg['avg_loser_pct'])}",
        f"Average R: {_r(agg['avg_r'])}",
        f"Total R: {_r(agg['total_r'])}",
        f"Profit Factor: {_pf(agg['profit_factor'])}",
        f"Avg MFE: {_pct(agg['avg_mfe_pct'])}",
        f"Avg MAE: {_pct(agg['avg_mae_pct'])}",
        "",
        f"🏆 Лучший сигнал\n{_signal_one_liner(agg['best_signal'])}",
        "",
        f"💀 Худший сигнал\n{_signal_one_liner(agg['worst_signal'])}",
    ]


def _setups_block(agg: dict) -> list[str]:
    lines = ["", "🎯 <b>СЕТАПЫ</b>"]
    if not agg["by_setup"]:
        lines.append("N/A — пока нет закрытых сигналов")
        return lines
    for setup, s in sorted(agg["by_setup"].items(), key=lambda kv: -kv[1]["win_rate_pct"]):
        lines.append(f"{_label(setup)} — {s['signals']} сигналов, {_wr(s['win_rate_pct'])} WR")
    return lines


def _symbols_block(agg: dict) -> list[str]:
    lines = ["", "🪙 <b>ЛУЧШИЙ / ХУДШИЙ СИМВОЛ</b>"]
    if agg["best_symbol"] is None:
        lines.append(f"N/A — нужно ≥{config.MIN_SAMPLE_FOR_RANKING} закрытых сигналов на символ")
        return lines
    bs, ws = agg["by_symbol"][agg["best_symbol"]], agg["by_symbol"][agg["worst_symbol"]]
    lines.append(f"Лучший: {agg['best_symbol']} ({bs['wins']}W/{bs['losses']}L, {_wr(bs['win_rate_pct'])})")
    lines.append(f"Худший: {agg['worst_symbol']} ({ws['wins']}W/{ws['losses']}L, {_wr(ws['win_rate_pct'])})")
    return lines


TF_LABELS = {"1d": "1D", "1h": "1H"}


ALIGN_LABELS = {
    "STRONG":   "1D + 4H + сигнал",
    "PARTIAL":  "только 4H + сигнал",
    "CONFLICT": "против 4H",
    "UNKNOWN":  "контекст н/д",
}


def _alignment_block(agg: dict) -> list[str]:
    """Эффективность сигналов в разрезе согласованности со старшими ТФ.

    Группы с малой выборкой помечаются явно: показывать «100% win rate»
    на трёх сделках — это вводить в заблуждение, а не информировать.
    """
    lines = ["", "🧭 <b>СОГЛАСОВАННОСТЬ ТАЙМФРЕЙМОВ</b>"]
    by_align = agg.get("by_alignment") or {}
    if not by_align:
        lines.append("N/A — пока нет закрытых сигналов с контекстом 4H")
        return lines

    order = ["STRONG", "PARTIAL", "CONFLICT", "UNKNOWN"]
    for key in sorted(by_align, key=lambda k: order.index(k) if k in order else 99):
        s = by_align[key]
        label = ALIGN_LABELS.get(key, key)
        small = " <i>(мало данных)</i>" if s["signals"] < config.MIN_SAMPLE_FOR_RANKING else ""
        lines.append(
            f"<b>{key}</b> ({label}) — {s['signals']} сигн., {_wr(s['win_rate_pct'])} WR, "
            f"R {_r(s['avg_r'])}{small}"
        )
        lines.append(f"    MFE {_pct(s['avg_mfe_pct'])} · MAE {_pct(s['avg_mae_pct'])}")

    by_tf4 = agg.get("by_trend_4h") or {}
    if by_tf4:
        lines.append("")
        lines.append("<b>По направлению 4H тренда</b>")
        for tr, s in sorted(by_tf4.items()):
            lines.append(f"{tr} — {s['signals']} сигн., {_wr(s['win_rate_pct'])} WR, R {_r(s['avg_r'])}")
    return lines


def _timeframe_block(agg: dict) -> list[str]:
    """Реальная разбивка по таймфреймам. До появления часового контура здесь
    стояло честное N/A, потому что все сигналы были дневными — теперь данные
    действительно различаются. См. DECISIONS.md #13."""
    lines = ["", "⏱ <b>ТАЙМФРЕЙМЫ</b>"]
    by_tf = agg.get("by_timeframe") or {}
    if not by_tf:
        lines.append("N/A — пока нет закрытых сигналов")
        return lines
    for tf, s in sorted(by_tf.items(), key=lambda kv: -kv[1]["win_rate_pct"]):
        label = TF_LABELS.get(tf, tf)
        lines.append(f"{label} — {s['signals']} сигналов, {_wr(s['win_rate_pct'])} WR")
    return lines


def _assemble(header: str, period_line: str, agg: dict) -> str:
    lines = [header, period_line, ""]
    lines += _core_block(agg)
    lines += _direction_block(agg)
    lines += _performance_block(agg)
    lines += _setups_block(agg)
    lines += _symbols_block(agg)
    lines += _timeframe_block(agg)
    lines += _alignment_block(agg)
    return "\n".join(lines)


# ── report builders ────────────────────────────────────────────────────────

async def build_today_report(store=_default_store) -> str:
    now = datetime.now(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    signals = await store.get_signals_since(start)
    agg = performance.aggregate(signals)

    if 0 < len(signals) <= 10:
        # Small enough to list individually, like the approved mockup.
        lines = [f"🗓 <b>СЕГОДНЯ</b> — {now.strftime('%d.%m.%Y')}", ""]
        lines += _core_block(agg)
        lines.append("")
        lines.append("――――――――――――")
        for i, s in enumerate(sorted(signals, key=lambda r: r["fired_at"]), 1):
            status_emoji = {"OPEN": "🟡", "WIN": "✅", "LOSS": "❌"}[s["status"]]
            r_txt = f" ({s['r_multiple']:+.2f}R)" if s.get("r_multiple") is not None else ""
            lines.append(
                f"{i}️⃣ {s['symbol']} · {s['direction']} · {_label(s['setup'])}\n"
                f"   Entry ${s['entry_price']:,.4f} → {status_emoji} {s['status']}{r_txt}"
            )
        return "\n".join(lines)

    return _assemble(f"🗓 <b>СЕГОДНЯ</b> — {now.strftime('%d.%m.%Y')}", "", agg)


async def build_week_report(store=_default_store) -> str:
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=7)
    signals = await store.get_signals_since(start)
    agg = performance.aggregate(signals)
    period = f"{start.strftime('%d.%m.%Y')} — {now.strftime('%d.%m.%Y')}"
    return _assemble("📊 <b>НЕДЕЛЬНЫЙ ОТЧЁТ</b>", period, agg)


async def build_month_report(store=_default_store) -> str:
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=30)
    signals = await store.get_signals_since(start)
    agg = performance.aggregate(signals)
    period = f"{start.strftime('%d.%m.%Y')} — {now.strftime('%d.%m.%Y')}"
    return _assemble("📅 <b>МЕСЯЧНЫЙ ОТЧЁТ</b>", period, agg)


async def build_stats_report(store=_default_store) -> str:
    first_at = await store.get_first_signal_at()
    signals = await store.get_all_signals()
    agg = performance.aggregate(signals)
    if first_at is not None:
        days = (datetime.now(timezone.utc) - first_at).days
        period = f"С {first_at.strftime('%d.%m.%Y')} ({days} дней)"
    else:
        period = "Данных пока нет"
    return _assemble("📈 <b>ВСЯ СТАТИСТИКА</b>", period, agg)
