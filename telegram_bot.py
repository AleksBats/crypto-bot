"""
telegram_bot.py — sends alerts to Telegram.
Verified endpoint: https://api.telegram.org/bot{TOKEN}/sendMessage
"""

import logging
from typing import Optional

import httpx
import config

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


async def send_alert(text: str, parse_mode: str = "HTML") -> bool:
    """Send a message to the configured Telegram chat. Returns True on success."""
    url = TELEGRAM_API.format(token=config.TELEGRAM_BOT_TOKEN)
    payload = {
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            logger.info("Telegram alert sent: %s", text[:80])
            return True
    except httpx.HTTPStatusError as e:
        logger.error("Telegram HTTP error %s: %s", e.response.status_code, e.response.text)
    except Exception as e:
        logger.error("Telegram send failed: %s", e)
    return False


def fmt_whale(direction: str, amount: float, price: float, tx_hash: str, from_addr: str, to_addr: str) -> str:
    usd = amount * price
    return (
        f"🐋 <b>WHALE ALERT</b>\n"
        f"Direction: <b>{direction}</b>\n"
        f"Amount: <b>{amount:,.0f} ASTER</b> (~${usd:,.0f})\n"
        f"From: <code>{from_addr[:10]}…</code>\n"
        f"To:   <code>{to_addr[:10]}…</code>\n"
        f"Tx: <code>{tx_hash[:16]}…</code>"
    )


def fmt_price_alert(event: str, price: float, change_pct: float, level: float) -> str:
    emoji = "🚀" if change_pct > 0 else "🔻"
    return (
        f"{emoji} <b>PRICE {event.upper()}</b>\n"
        f"Price: <b>${price:.6f}</b>\n"
        f"Move:  <b>{change_pct:+.2f}%</b>\n"
        f"Level: ${level:.6f}"
    )


def fmt_volume_spike(current_vol: float, avg_vol: float, multiplier: float, price: float) -> str:
    return (
        f"📊 <b>VOLUME SPIKE</b>\n"
        f"Current: <b>{current_vol:,.0f} ASTER</b>\n"
        f"Avg 1h:  {avg_vol:,.0f} ASTER\n"
        f"Ratio:   <b>{multiplier:.1f}×</b>\n"
        f"Price:   ${price:.6f}"
    )


def fmt_oi_alert(event: str, oi: float, oi_prev: float, change_pct: float, price: float) -> str:
    emoji = "📈" if change_pct > 0 else "📉"
    return (
        f"{emoji} <b>OPEN INTEREST {event.upper()}</b>\n"
        f"OI now:  <b>{oi:,.0f}</b>\n"
        f"OI prev: {oi_prev:,.0f}\n"
        f"Change:  <b>{change_pct:+.2f}%</b>\n"
        f"Price:   ${price:.6f}"
    )


def fmt_funding_alert(funding: float, prev_funding: float) -> str:
    emoji = "🔥" if funding > 0 else "🧊"
    return (
        f"{emoji} <b>EXTREME FUNDING RATE</b>\n"
        f"Current: <b>{funding:+.4f}%</b>\n"
        f"Prev:    {prev_funding:+.4f}%"
    )


def fmt_twitter_alert(handle: str, text: str, url: str) -> str:
    return (
        f"🐦 <b>IMPORTANT POST</b> @{handle}\n\n"
        f"{text[:280]}\n\n"
        f"<a href='{url}'>View tweet</a>"
    )


def fmt_combo_alert(signals: list[str]) -> str:
    joined = "\n".join(f"  • {s}" for s in signals)
    return f"⚡️ <b>MULTIPLE SIGNALS</b>\n\n{joined}"


# ─────────────────────────────────────────────────────────────
#  Форматтеры Breakout / Turtle Zone Filter / Failure Test
#
#  ВАЖНО — две РАЗНЫЕ цены, не путать (см. DECISIONS.md #13):
#    signal_price  — close СВЕЧИ, создавшей сигнал (закрытой!). Именно по ней
#                    считались уровни Дончиана, она неизменна навсегда.
#    current_price — свежая рыночная цена, запрошенная у Binance
#                    непосредственно перед отправкой сообщения. Может
#                    отличаться от signal_price — это нормально и ожидаемо.
#  Раньше показывалась одна цена — закэшированный close (до 15 мин давности),
#  что выглядело как расхождение с TradingView. Теперь обе видны явно.
# ─────────────────────────────────────────────────────────────

def _tf_label(timeframe: str) -> str:
    return {"1d": "1D (дневной)", "1h": "1H (часовой)"}.get(timeframe, timeframe)


def _price_block(signal_price: float, current_price: Optional[float]) -> str:
    """Две цены + расхождение между ними. Если свежую цену получить не
    удалось (сеть/лимиты), показываем это честно, а не молча подставляем
    цену сигнала."""
    lines = [f"Цена сигнала: <b>${signal_price:.6f}</b>"]
    if current_price is None:
        lines.append("Текущая цена: <i>н/д (не удалось запросить)</i>")
    else:
        delta_pct = (current_price - signal_price) / signal_price * 100 if signal_price else 0.0
        lines.append(f"Текущая цена: <b>${current_price:.6f}</b> ({delta_pct:+.2f}%)")
    return "\n".join(lines)


def fmt_breakout_alert(symbol: str, direction: str, level: float, signal_price: float,
                        n: int, timeframe: str = "1d", current_price: Optional[float] = None) -> str:
    emoji = "🚀" if direction == "bullish" else "🔻"
    label = "BREAKOUT LONG" if direction == "bullish" else "BREAKOUT SHORT"
    return (
        f"{emoji} <b>{label}</b> — <b>{symbol}</b>\n"
        f"Таймфрейм: {_tf_label(timeframe)}\n"
        f"Канал: {n}-периодный Donchian\n"
        f"Уровень пробоя: ${level:.6f}\n"
        f"{_price_block(signal_price, current_price)}"
    )


def fmt_turtle_zone_alert(symbol: str, direction: str, stage: str, fast_level: float,
                           slow_level: float, signal_price: float, timeframe: str = "1d",
                           current_price: Optional[float] = None) -> str:
    emoji = "🐢🚀" if direction == "bullish" else "🐢🔻"
    label = "TURTLE ZONE LONG" if direction == "bullish" else "TURTLE ZONE SHORT"
    stage_ru = ("подтверждено (пробит и медленный канал)" if stage == "confirmed"
                else "ранняя зона (пробит только быстрый канал)")
    return (
        f"{emoji} <b>{label}</b> — <b>{symbol}</b>\n"
        f"Таймфрейм: {_tf_label(timeframe)}\n"
        f"Стадия: {stage_ru}\n"
        f"Fast: ${fast_level:.6f}\n"
        f"Slow: ${slow_level:.6f}\n"
        f"{_price_block(signal_price, current_price)}"
    )


def fmt_failure_test_alert(symbol: str, direction: str, level: float, signal_price: float,
                            timeframe: str = "1d", current_price: Optional[float] = None) -> str:
    emoji = "⚠️🔻" if direction == "SHORT" else "⚠️🚀"
    return (
        f"{emoji} <b>FAILURE TEST {direction}</b> — <b>{symbol}</b>\n"
        f"Таймфрейм: {_tf_label(timeframe)}\n"
        f"Ложный пробой уровня ${level:.6f}\n"
        f"{_price_block(signal_price, current_price)}\n"
        f"<i>Цена не удержала пробой — вероятность разворота</i>"
    )


# ─────────────────────────────────────────────────────────────
#  Блок КОНТЕКСТ (4H / 1D) — Phase 4
#
#  ⚠️  Это ЧИСТО ИНФОРМАЦИОННЫЙ блок. 4H на данном этапе не блокирует
#  сигналы и не меняет их — он только описывает обстановку и копится в
#  статистику. Сигнал с alignment=CONFLICT отправляется ровно так же,
#  как и со STRONG. См. DECISIONS.md #14.
# ─────────────────────────────────────────────────────────────

_TREND_EMOJI = {"BULLISH": "🟢", "BEARISH": "🔴", "NEUTRAL": "🟡"}
_ALIGN_LABEL = {
    "STRONG":   "STRONG (1D + 4H + сигнал в одну сторону)",
    "PARTIAL":  "PARTIAL (4H согласован, 1D нет)",
    "CONFLICT": "CONFLICT (4H против сигнала)",
    "UNKNOWN":  "н/д (контекст недоступен)",
}


def _trend_line(label: str, trend: Optional[str]) -> str:
    if not trend:
        return f"{label}: <i>н/д</i>"
    return f"{label}: {_TREND_EMOJI.get(trend, '')} {trend}"


def fmt_trend_context(trend_1d: Optional[str], trend_4h: Optional[str],
                       structure_4h: Optional[str], high_label: Optional[str],
                       low_label: Optional[str], alignment: Optional[str]) -> str:
    """Дописывается в конец сообщения о сигнале.

    Если контекст вообще не удалось получить — возвращается пустая строка,
    и сообщение выглядит ровно как раньше. Никаких выдуманных значений."""
    if not trend_4h and not trend_1d:
        return ""

    lines = ["", "📐 <b>КОНТЕКСТ</b>"]
    lines.append(_trend_line("1D тренд", trend_1d))
    lines.append(_trend_line("4H тренд", trend_4h))

    if high_label and low_label:
        lines.append(f"4H структура: {high_label} / {low_label}")
    elif structure_4h == "MIXED":
        lines.append("4H структура: <i>не подтверждена (мало swing-точек)</i>")

    if alignment:
        lines.append(f"→ Alignment: <b>{_ALIGN_LABEL.get(alignment, alignment)}</b>")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────
#  DEPRECATED — first-prototype signal accuracy report (see top-level
#  signal_tracker.py). Superseded by statistics/reports.py, which builds
#  the /stats /week /today /month messages directly. Not called by
#  run_live.py anymore. Kept only for history — see DECISIONS.md #12.
# ─────────────────────────────────────────────────────────────
def fmt_stats_summary(overall: dict, by_type: dict) -> str:
    def _wr(s: dict) -> str:
        return f"{s['win_rate_pct']:.1f}%" if s["win_rate_pct"] is not None else "n/a"

    lines = ["📊 <b>Signal Accuracy Report</b>", ""]
    lines.append(
        f"Overall: <b>{overall['wins']}W / {overall['losses']}L</b> "
        f"({_wr(overall)}) — {overall['pending']} pending"
    )
    lines.append("")

    labels = {"breakout": "Breakout", "turtle_zone": "Turtle Zone", "failure_test": "Failure Test"}
    for sig_type, s in by_type.items():
        if s["total"] == 0:
            continue
        label = labels.get(sig_type, sig_type.replace("_", " ").title())
        lines.append(f"<b>{label}</b>: {s['wins']}W / {s['losses']}L ({_wr(s)}) — {s['pending']} pending")

    lines.append("")
    lines.append(
        "<i>WIN = price closed beyond the 55-day slow band in the predicted "
        "direction before closing past the fast-band/level invalidation. "
        "LOSS = the reverse. Pending signals aren't counted yet. "
        "See DECISIONS.md #11.</i>"
    )
    return "\n".join(lines)
