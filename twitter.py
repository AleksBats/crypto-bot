"""
monitors/twitter.py — X/Twitter monitoring for ASTER signals.

STATUS: DISABLED — paid Twitter API not used in free test mode.

════════════════════════════════════════════════════════════
FREE ALTERNATIVES (ranked best → worst for this use case)
════════════════════════════════════════════════════════════

1. NITTER RSS (free, no API key, works now)
   ─────────────────────────────────────────
   Nitter is an open-source Twitter front-end. Several public instances
   expose RSS feeds per account.

   RSS URL format:
     https://nitter.net/{username}/rss
     https://nitter.poast.org/{username}/rss     ← backup instance
     https://nitter.privacydev.net/{username}/rss ← backup instance

   Accounts to watch:
     https://nitter.net/AsterNetwork/rss          ← TODO: confirm handle
     https://nitter.net/binance/rss
     https://nitter.net/cz_binance/rss

   How to use: parse RSS XML with feedparser (pip install feedparser).
   Free, no auth, ~5-10 min delay vs real-time.

   Caveat: Nitter public instances go down occasionally. Run your own
   instance on Railway for reliability:
     https://github.com/zedeus/nitter
     Docker image: zedeus/nitter:latest

   Implementation skeleton: see _fetch_rss() below (uncomment to activate).

2. Telegram Channels (re-broadcast, free)
   ────────────────────────────────────────
   Many important crypto accounts cross-post to Telegram.
   Use Telegram Bot API to monitor a channel by polling getUpdates.
   No cost. Slight delay.

   Useful channels to watch (verify these are official before adding):
     @AsterNetwork       ← TODO: confirm official channel
     @binance
     @whale_alert_io     ← whale transfers, free channel
     @cryptoquant_alerts ← on-chain analytics alerts

   Implementation: use python-telegram-bot or raw Bot API:
     GET https://api.telegram.org/bot{TOKEN}/getUpdates

3. CryptoPanic RSS (free tier available)
   ────────────────────────────────────────
   News aggregator with free RSS for specific currencies.
   Feed: https://cryptopanic.com/news/astr/rss/
   No API key needed for RSS.

4. Manual monitoring (zero cost, zero code)
   ────────────────────────────────────────
   Use TweetDeck or Twitter lists to manually watch:
     - @AsterNetwork (official)
     - @binance
     - @cz_binance
     - Large DeFi / crypto analysts you trust
   No automation needed for initial testing.

5. Official Twitter API v2 (paid, future option)
   ────────────────────────────────────────────────
   Basic tier: $100/month
   Docs: https://developer.twitter.com/en/docs/twitter-api
   Only needed if you want real-time automated monitoring at scale.

════════════════════════════════════════════════════════════
TO ACTIVATE NITTER RSS (option 1):
  1. pip install feedparser
  2. Add NITTER_BASE_URL=https://nitter.net to .env
  3. Uncomment run_twitter_monitor() below
════════════════════════════════════════════════════════════
"""

import asyncio
import logging
import time
from typing import Optional

import httpx

import config
from alert_engine import Signal, engine
from state import state
import telegram_bot as tg

logger = logging.getLogger(__name__)

# ── Nitter RSS fetcher (uncomment to activate) ───────────────────────────────

NITTER_BASE = "https://nitter.net"   # swap to backup if down

WATCH_ACCOUNTS = [
    "AsterNetwork",    # TODO: confirm official @handle
    "binance",
    "cz_binance",
]

SIGNAL_KEYWORDS = [
    "listing", "listed", "delist", "airdrop", "hack", "exploit",
    "partnership", "integration", "mainnet", "launch", "upgrade",
    "breakout", "accumulate", "whale",
]


async def _fetch_rss(client: httpx.AsyncClient, handle: str) -> list[dict]:
    """
    Fetch RSS from a Nitter instance for a given Twitter handle.
    Returns list of {id, title, link, published}.

    UNCOMMENT this function body to activate Nitter RSS.
    """
    # import xml.etree.ElementTree as ET
    # url = f"{NITTER_BASE}/{handle}/rss"
    # try:
    #     r = await client.get(url, timeout=10, follow_redirects=True)
    #     r.raise_for_status()
    #     root = ET.fromstring(r.text)
    #     ns = {"dc": "http://purl.org/dc/elements/1.1/"}
    #     items = []
    #     for item in root.findall(".//item"):
    #         items.append({
    #             "id":        item.findtext("guid", ""),
    #             "title":     item.findtext("title", ""),
    #             "link":      item.findtext("link", ""),
    #             "published": item.findtext("pubDate", ""),
    #         })
    #     return items
    # except Exception as e:
    #     logger.warning("Nitter RSS failed for @%s: %s", handle, e)
    #     return []
    return []   # remove when activated


def _is_signal(text: str) -> bool:
    text_lower = text.lower()
    return any(kw in text_lower for kw in SIGNAL_KEYWORDS)


async def run_twitter_monitor():
    """
    Currently DISABLED.
    To activate: uncomment _fetch_rss() body above, then uncomment
    the main loop below.
    """
    logger.info(
        "Twitter/X monitor DISABLED.\n"
        "  Free options: Nitter RSS, Telegram channels, CryptoPanic RSS.\n"
        "  See monitors/twitter.py for setup instructions."
    )
    return   # ← remove this line to activate

    # ── Nitter RSS loop (activate by removing the return above) ──────────────
    # async with httpx.AsyncClient(timeout=15) as client:
    #     while True:
    #         for handle in WATCH_ACCOUNTS:
    #             try:
    #                 items = await _fetch_rss(client, handle)
    #                 for item in items:
    #                     item_id = item["id"]
    #                     if item_id in state.seen_tweet_ids:
    #                         continue
    #                     state.seen_tweet_ids.add(item_id)
    #                     text = item["title"]
    #                     if not _is_signal(text):
    #                         continue
    #                     strong = handle.lower() in {"binance", "cz_binance"}
    #                     await engine.submit(Signal(
    #                         key=f"twitter_{handle}",
    #                         strong=strong,
    #                         message=tg.fmt_twitter_alert(handle, text, item["link"]),
    #                         priority=3,
    #                     ))
    #             except Exception as e:
    #                 logger.error("Twitter RSS error for @%s: %s", handle, e)
    #         await asyncio.sleep(config.POLL_TWITTER_SECS)
