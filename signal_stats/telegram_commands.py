"""
signal_stats/telegram_commands.py — long-polling listener for /stats /week
/today /month.

The bot has never listened for incoming Telegram messages before (only
sends). This module is fully additive and isolated: it runs as its own
background asyncio task (started from run_live.py's main(), see
DECISIONS.md #12) using its own httpx client and its own getUpdates offset.
A crash or hang in here cannot block or break signal detection / alert
sending — they run as separate tasks.

Only STATS_ALLOWED_CHAT_ID may trigger a report — anything else is logged
and ignored, since this is a personal bot, not a public one.
"""

import asyncio
import logging

import httpx

import config
from signal_stats import reports

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}"

_offset = 0

COMMANDS = {
    "/stats": reports.build_stats_report,
    "/week": reports.build_week_report,
    "/today": reports.build_today_report,
    "/month": reports.build_month_report,
}


async def _get_updates(client: httpx.AsyncClient) -> list[dict]:
    global _offset
    url = f"{TELEGRAM_API_BASE.format(token=config.TELEGRAM_BOT_TOKEN)}/getUpdates"
    r = await client.get(url, params={"timeout": config.TELEGRAM_POLL_INTERVAL_SECS, "offset": _offset},
                          timeout=config.TELEGRAM_POLL_INTERVAL_SECS + 10)
    r.raise_for_status()
    updates = r.json().get("result", [])
    if updates:
        _offset = updates[-1]["update_id"] + 1
    return updates


async def _reply(client: httpx.AsyncClient, chat_id: str, text: str):
    url = f"{TELEGRAM_API_BASE.format(token=config.TELEGRAM_BOT_TOKEN)}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    try:
        r = await client.post(url, json=payload, timeout=10)
        r.raise_for_status()
    except Exception as e:
        logger.error("statistics: failed to reply to command: %s", e)


async def _handle_update(client: httpx.AsyncClient, update: dict):
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return
    chat_id = str(msg.get("chat", {}).get("id", ""))
    text = (msg.get("text") or "").strip()
    if not text.startswith("/"):
        return

    command = text.split()[0].split("@")[0]  # strip args and any @BotName suffix
    handler = COMMANDS.get(command)
    if handler is None:
        return

    if chat_id != str(config.STATS_ALLOWED_CHAT_ID):
        logger.warning("statistics: ignored %s from unauthorized chat_id=%s", command, chat_id)
        return

    try:
        report_text = await handler()
    except Exception as e:
        logger.error("statistics: report generation failed for %s: %s", command, e)
        report_text = "⚠️ Не удалось построить отчёт — подробности в логах."

    await _reply(client, chat_id, report_text)


async def run_command_listener():
    """Runs forever. Call as a background asyncio task from run_live.py's
    main(), alongside the health server and the main polling loop."""
    global _offset
    if not config.TELEGRAM_BOT_TOKEN:
        logger.info("statistics: TELEGRAM_BOT_TOKEN not set — command listener disabled.")
        return

    async with httpx.AsyncClient() as client:
        # Drain updates queued while the bot was offline so old commands
        # aren't replayed on startup.
        try:
            url = f"{TELEGRAM_API_BASE.format(token=config.TELEGRAM_BOT_TOKEN)}/getUpdates"
            r = await client.get(url, params={"timeout": 0, "offset": -1}, timeout=10)
            r.raise_for_status()
            result = r.json().get("result", [])
            if result:
                _offset = result[-1]["update_id"] + 1
        except Exception as e:
            logger.warning("statistics: could not drain stale updates at startup: %s", e)

        logger.info("statistics: Telegram command listener started (/stats /week /today /month).")
        while True:
            try:
                updates = await _get_updates(client)
                for u in updates:
                    await _handle_update(client, u)
            except Exception as e:
                logger.error("statistics: command listener error, retrying: %s", e)
                await asyncio.sleep(5)
