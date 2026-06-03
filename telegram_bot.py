"""
telegram_bot.py — sends alerts to Telegram.
Verified endpoint: https://api.telegram.org/bot{TOKEN}/sendMessage
"""

import logging
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
