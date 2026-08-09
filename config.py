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

# ── Часовой (1H) контур технических сигналов ─────────────────────────────────
# Те же три индикатора считаются ПАРАЛЛЕЛЬНО на часовых свечах. Дневной контур
# при этом не меняется — это отдельный, независимый набор сигналов, помеченный
# таймфреймом "1h". См. DECISIONS.md #13.
ENABLE_HOURLY_SIGNALS = os.environ.get("ENABLE_HOURLY_SIGNALS", "true").lower() in ("1", "true", "yes")
HOURLY_KLINES_LIMIT   = int(os.environ.get("HOURLY_KLINES_LIMIT", "200"))  # часовых свечей на загрузку
# Кэш часовых свечей короче дневного: часовая свеча закрывается каждый час,
# и 15-минутный кэш задерживал бы обнаружение новой закрытой свечи.
POLL_HOURLY_SECS      = int(os.environ.get("POLL_HOURLY_SECS", "300"))     # 5 мин

# Список монет, по которым Breakout / Turtle Zone Filter / Failure Test
# сканируются ОТДЕЛЬНО от основного SYMBOL_SPOT (ASTERUSDT). Для этих монет
# шлются только сигналы этих трёх индикаторов — без volume/OI/funding.
TECHNICAL_SYMBOLS = [
    s.strip().upper()
    for s in os.environ.get(
        "TECHNICAL_SYMBOLS",
        "SOLUSDT,LINKUSDT,ETHUSDT,BTCUSDT,XRPUSDT,XLMUSDT,HYPERUSDT,"
        "ADAUSDT,DOGEUSDT,PEPEUSDT,PENGUUSDT,CAPUSDT,"
        "ZECUSDT,SHIBUSDT,NEARUSDT,GRAMUSDT",
    ).split(",")
    if s.strip()
]

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

# ── Signal performance statistics / paper trading (Phase 2) ──────────────────
# Superseded design: the standalone top-level signal_tracker.py + JSON-file
# approach from the first stats prototype is replaced by the statistics/
# package (Postgres-backed). See DECISIONS.md #12.
#
# DATABASE_URL is intentionally OPTIONAL here, not _require()'d — the whole
# bot must keep working (alerts, indicators, everything) even if nobody has
# provisioned a database yet. If empty, run_live.py skips initializing the
# statistics subsystem entirely (logs one warning at startup) instead of
# crashing. Never make the core trading loop depend on this being set.
DATABASE_URL = os.environ.get("DATABASE_URL", "")  # Neon Postgres connection string (postgres://...)

# Standard Wilder RSI period — 14 is the universal default, not a value we
# invented for this project. Recorded per-signal for context only; it does
# NOT feed into signal generation or the WIN/LOSS rule.
RSI_PERIOD = int(os.environ.get("RSI_PERIOD", "14"))

# Long-poll timeout (seconds) for the Telegram command listener's getUpdates
# calls — standard long-polling pattern, not a busy-loop interval. Higher =
# fewer HTTP requests, still near-instant command responses. Irrelevant to
# alert sending.
TELEGRAM_POLL_INTERVAL_SECS = int(os.environ.get("TELEGRAM_POLL_INTERVAL_SECS", "25"))

# Only this chat may trigger /stats /week /today /month. Defaults to the
# same chat the bot already alerts into.
STATS_ALLOWED_CHAT_ID = os.environ.get("STATS_ALLOWED_CHAT_ID", TELEGRAM_CHAT_ID)

# Minimum number of closed signals a symbol/setup needs before it's eligible
# for "best/worst" ranking in reports — avoids a single lucky/unlucky trade
# looking like a trend. Display threshold only, not a trading threshold.
MIN_SAMPLE_FOR_RANKING = int(os.environ.get("MIN_SAMPLE_FOR_RANKING", "3"))
