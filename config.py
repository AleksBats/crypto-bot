"""
config.py — loads all env vars and defines bot-wide thresholds.
"""

import os
from dotenv import load_dotenv

load_dotenv()


def _require(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        raise EnvironmentError(f"Required env var missing: {key}")
    return val


# ── Telegram ────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = _require("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = _require("TELEGRAM_CHAT_ID")

# ── Binance ─────────────────────────────────────────────────────────────────
BINANCE_API_KEY    = os.environ.get("BINANCE_API_KEY", "")   # optional for public endpoints
BINANCE_SECRET     = os.environ.get("BINANCE_SECRET", "")
SYMBOL_SPOT        = os.environ.get("SYMBOL_SPOT",    "ASTERUSDT")   # Binance Spot pair
SYMBOL_FUTURES     = os.environ.get("SYMBOL_FUTURES", "ASTERUSDT")   # Binance Futures pair

# ── Twitter / X ─────────────────────────────────────────────────────────────
TWITTER_BEARER_TOKEN = os.environ.get("TWITTER_BEARER_TOKEN", "")   # Twitter API v2

# ── On-chain whale tracking ──────────────────────────────────────────────────
# TODO: Confirm which blockchain ASTER token lives on, then enable the right key.
# Options:
#   EVM (Ethereum/Base/Arbitrum) → use Etherscan-compatible API
#   Astar Network (Polkadot EVM) → use Subscan API
#   BSC                          → use BscScan API
ETHERSCAN_API_KEY  = os.environ.get("ETHERSCAN_API_KEY", "")
BSCSCAN_API_KEY    = os.environ.get("BSCSCAN_API_KEY", "")
SUBSCAN_API_KEY    = os.environ.get("SUBSCAN_API_KEY", "")
ASTER_CONTRACT     = os.environ.get("ASTER_CONTRACT_ADDRESS", "")   # ERC-20/BEP-20 contract

# ── Alert thresholds ─────────────────────────────────────────────────────────
WHALE_THRESHOLD_ASTER  = float(os.environ.get("WHALE_THRESHOLD_ASTER",  "20000000"))  # 20M ASTER
VOLUME_SPIKE_MULTIPLIER = float(os.environ.get("VOLUME_SPIKE_MULTIPLIER", "3.0"))     # 3× avg
OI_CHANGE_PCT_THRESHOLD = float(os.environ.get("OI_CHANGE_PCT_THRESHOLD", "10.0"))    # 10%
FUNDING_EXTREME_PCT     = float(os.environ.get("FUNDING_EXTREME_PCT",     "0.05"))    # 0.05% per 8h
PRICE_BREAKOUT_PCT      = float(os.environ.get("PRICE_BREAKOUT_PCT",      "3.0"))     # 3% move

# ── Технические сигналы (Donchian / Turtle-style) ─────────────────────────────
DONCHIAN_LOOKBACK     = int(os.environ.get("DONCHIAN_LOOKBACK",     "20"))  # период канала для Breakout
TURTLE_FAST_LOOKBACK  = int(os.environ.get("TURTLE_FAST_LOOKBACK",  "20"))  # Turtle System 1
TURTLE_SLOW_LOOKBACK  = int(os.environ.get("TURTLE_SLOW_LOOKBACK",  "55"))  # Turtle System 2
FAILURE_TEST_LOOKBACK = int(os.environ.get("FAILURE_TEST_LOOKBACK", "5"))   # баров назад для проверки ложного пробоя
DAILY_KLINES_LIMIT    = int(os.environ.get("DAILY_KLINES_LIMIT",    "90"))  # дневных свечей на загрузку
POLL_TECHNICAL_SECS   = int(os.environ.get("POLL_TECHNICAL_SECS",   "900")) # 15 мин — дневные данные не нужно чаще

# ── Polling intervals (seconds) ──────────────────────────────────────────────
POLL_PRICE_SECS    = int(os.environ.get("POLL_PRICE_SECS",   "60"))
POLL_OI_SECS       = int(os.environ.get("POLL_OI_SECS",      "60"))
POLL_FUNDING_SECS  = int(os.environ.get("POLL_FUNDING_SECS", "300"))
POLL_WHALE_SECS    = int(os.environ.get("POLL_WHALE_SECS",   "120"))
POLL_TWITTER_SECS  = int(os.environ.get("POLL_TWITTER_SECS", "300"))

# ── Twitter accounts to monitor ──────────────────────────────────────────────
TWITTER_WATCH_ACCOUNTS = [
    "AsterNetwork",      # TODO: replace with verified @handle once confirmed
    "binance",
    "cz_binance",
    # Add more trusted analysts/DeFi accounts here
]

# ── Alert cooldown — don't re-alert same signal within N seconds ─────────────
ALERT_COOLDOWN_SECS = int(os.environ.get("ALERT_COOLDOWN_SECS", "1800"))  # 30 min
