"""
run_live.py — Continuous 24/7 local monitoring loop for Aster Intelligence Bot.

Uses only free Binance public API. No Twitter. No whale monitor.
Sends Telegram alerts only when alert_engine detects important signals.

Signals monitored:
  - Volume spike (1h volume vs average)
  - Open Interest change
  - Funding rate extremes
  - Breakout          (Donchian channel break)
  - Turtle Zone Filter (dual-channel Turtle-style system)
  - Failure Test       (false-breakout / trap detector)

Технические индикаторы считаются на ДВУХ таймфреймах параллельно (1D и 1H)
и ТОЛЬКО по закрытым свечам. В сообщении показываются две отдельные цены:
цена свечи, создавшей сигнал, и свежая рыночная цена на момент отправки.
Повторная отправка того же сигнала по той же свече исключена. См. DECISIONS.md #13.

Every Breakout / Turtle Zone / Failure Test alert that is ACTUALLY sent to
Telegram (i.e. survives alert_engine's cooldown) is also recorded in the
signal_stats/ package for objective WIN/LOSS/OPEN paper-trading tracking —
see signal_stats/signal_tracker.py and DECISIONS.md #12 for the methodology.
A separate background task listens for /stats /week /today /month commands
(signal_stats/telegram_commands.py). None of this changes indicator logic,
thresholds, symbol lists, or existing alert behavior — it only observes
what already happens and records it.

Run:
    python run_live.py

Stop:
    Ctrl+C  (graceful shutdown, sends Telegram notice)

Logs:
    Console + logs/aster_bot.log

Check interval: 5 minutes (POLL_INTERVAL_SECS)
Technical (daily) signals refresh: 15 minutes (config.POLL_TECHNICAL_SECS)
Alert cooldown: 30 minutes per signal type (from config.py)
"""

import asyncio
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path

import httpx
from dotenv import load_dotenv
from typing import Optional

load_dotenv()

# ── Bootstrap path so local imports work regardless of cwd ───────────────────
sys.path.insert(0, str(Path(__file__).parent))

import config
from alert_engine import Signal, engine
from state import state
import telegram_bot as tg
import technical_signals as ts
import trend_context as tc
from signal_stats import signal_tracker as stats_tracker
from signal_stats.telegram_commands import run_command_listener

# ── Logging setup ─────────────────────────────────────────────────────────────
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

log_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s — %(message)s")

file_handler = RotatingFileHandler(
    LOG_DIR / "aster_bot.log",
    maxBytes=5 * 1024 * 1024,   # 5 MB per file
    backupCount=5,
)
file_handler.setFormatter(log_formatter)

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(log_formatter)

logging.basicConfig(
    level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO")),
    handlers=[file_handler, console_handler],
)
logger = logging.getLogger("run_live")

# ── Constants ─────────────────────────────────────────────────────────────────
POLL_INTERVAL_SECS = int(os.environ.get("POLL_INTERVAL_SECS", "300"))   # 5 min default

SPOT_PRICE_URL  = "https://api.binance.com/api/v3/ticker/price"
SPOT_24HR_URL   = "https://api.binance.com/api/v3/ticker/24hr"
SPOT_KLINES_URL = "https://api.binance.com/api/v3/klines"
FUT_PREMIUM_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"
FUT_OI_URL      = "https://fapi.binance.com/fapi/v1/openInterest"
FUT_FUNDING_URL = "https://fapi.binance.com/fapi/v1/fundingRate"

_futures_available = None   # None = unknown, True = available, False = skip
_shutdown = False

# ── Кэш свечей: ключ (symbol, interval) ──────────────────────────────────────
# Дневные и часовые кэшируются независимо и с разным TTL (config.POLL_TECHNICAL_SECS
# и config.POLL_HOURLY_SECS соответственно).
_klines_cache: dict = {}   # (symbol, interval) -> {"data": {...}, "ts": float}

# ── Дедупликация отправленных сигналов ПО СВЕЧЕ ──────────────────────────────
# Ключ — (symbol, timeframe, setup, direction), значение — close_time свечи,
# по которой сигнал уже был отправлен. Повторная отправка возможна только
# когда появилась НОВАЯ свеча с тем же условием.
#
# Зачем: cooldown в alert_engine — это rate limiting ("не чаще раза в 30 мин"),
# а не дедупликация. Условие Failure Test остаётся истинным сутками, и каждые
# 30 минут по истечении cooldown улетало одно и то же сообщение с одной и той
# же ценой. См. DECISIONS.md #13.
#
# Хранится в памяти процесса: после рестарта Render возможна ОДНА повторная
# отправка на свечу. Это осознанный компромисс — иначе пришлось бы делать
# дедупликацию зависимой от наличия БД, а бот обязан работать и без неё.
_sent_signal_candles: dict = {}   # (symbol, timeframe, setup, direction) -> close_time ms


# ════════════════════════════════════════════════
# HEALTH-CHECK HTTP SERVER (для бесплатного Web Service на Render)
# ════════════════════════════════════════════════
# Render (бесплатный тариф) поддерживает только "Web Service" — требует, чтобы
# приложение слушало $PORT и отвечало на HTTP-запросы, иначе деплой считается
# нездоровым. Бот сам по себе — это asyncio-цикл без веб-сервера, поэтому
# поднимаем крошечный HTTP-сервер в отдельном потоке ТОЛЬКО ради health check —
# он никак не влияет на логику сигналов и алертов.

class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status": "alive", "service": "aster-intelligence-bot"}')

    def log_message(self, format, *args):
        pass  # не засоряем логи health-check запросами


def _start_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = ThreadingHTTPServer(("0.0.0.0", port), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logging.getLogger("run_live").info("Health-check server listening on :%d", port)


# ════════════════════════════════════════════════
# DATA FETCHERS
# ════════════════════════════════════════════════

async def fetch_spot(client: httpx.AsyncClient) -> dict:
    """Returns price, 24h volume, and 1h/4h volume averages."""
    result = {}

    # Price
    r = await client.get(SPOT_PRICE_URL, params={"symbol": config.SYMBOL_SPOT})
    r.raise_for_status()
    result["price"] = float(r.json()["price"])

    # 24h ticker
    r = await client.get(SPOT_24HR_URL, params={"symbol": config.SYMBOL_SPOT})
    r.raise_for_status()
    d = r.json()
    result["vol_24h"]       = float(d["volume"])
    result["vol_quote_24h"] = float(d["quoteVolume"])
    result["change_24h"]    = float(d["priceChangePercent"])
    result["high_24h"]      = float(d["highPrice"])
    result["low_24h"]       = float(d["lowPrice"])

    # 1h candles (last 25) for volume spike detection
    r = await client.get(SPOT_KLINES_URL, params={
        "symbol": config.SYMBOL_SPOT, "interval": "1h", "limit": 25
    })
    r.raise_for_status()
    vols_1h = [float(c[5]) for c in r.json()]
    result["vol_1h_current"] = vols_1h[-1]
    result["vol_1h_avg"]     = sum(vols_1h[:-1]) / len(vols_1h[:-1])
    result["vol_1h_ratio"]   = result["vol_1h_current"] / result["vol_1h_avg"] if result["vol_1h_avg"] else 0

    # 4h candles (last 7) for broader volume context
    r = await client.get(SPOT_KLINES_URL, params={
        "symbol": config.SYMBOL_SPOT, "interval": "4h", "limit": 7
    })
    r.raise_for_status()
    vols_4h = [float(c[5]) for c in r.json()]
    result["vol_4h_avg"] = sum(vols_4h[:-1]) / max(len(vols_4h[:-1]), 1)

    return result


async def fetch_futures(client: httpx.AsyncClient) -> Optional[dict]:
    """Returns funding rate and OI. Returns None if futures not available."""
    global _futures_available
    if _futures_available is False:
        return None

    result = {}

    try:
        r = await client.get(FUT_PREMIUM_URL, params={"symbol": config.SYMBOL_FUTURES})
        if r.status_code == 400:
            if _futures_available is None:
                logger.warning("ASTERUSDT perp not found on Binance Futures — futures monitor disabled.")
            _futures_available = False
            return None

        r.raise_for_status()
        d = r.json()
        result["funding"]    = float(d.get("lastFundingRate", 0)) * 100   # → %
        result["mark_price"] = float(d.get("markPrice", 0))
        _futures_available = True

    except httpx.HTTPStatusError:
        return None

    # OI
    try:
        r = await client.get(FUT_OI_URL, params={"symbol": config.SYMBOL_FUTURES})
        r.raise_for_status()
        result["oi"] = float(r.json()["openInterest"])
    except Exception as e:
        logger.debug("OI fetch failed: %s", e)

    # Funding history (last 2 for change detection)
    try:
        r = await client.get(FUT_FUNDING_URL, params={"symbol": config.SYMBOL_FUTURES, "limit": 2})
        r.raise_for_status()
        history = [float(x["fundingRate"]) * 100 for x in r.json()]
        result["funding_prev"] = history[0] if len(history) >= 2 else result["funding"]
    except Exception as e:
        logger.debug("Funding history fetch failed: %s", e)
        result["funding_prev"] = result.get("funding", 0)

    return result


async def fetch_klines(client: httpx.AsyncClient, symbol: str, interval: str,
                        limit: int, cache_ttl: int) -> dict:
    """Загружает свечи и возвращает ТОЛЬКО ЗАКРЫТЫЕ.

    Ключевое отличие от прежней fetch_daily(): Binance возвращает последним
    элементом ТЕКУЩУЮ, ещё формирующуюся свечу. Раньше она попадала в
    индикаторы, из-за чего сигнал мог объявиться в середине свечи и потом
    "передумать". Теперь незакрытые свечи отбрасываются по close_time —
    сигнал физически не может появиться до закрытия своей свечи.
    См. DECISIONS.md #13.

    Формат kline: [open_time, open, high, low, close, volume, close_time, ...]
    """
    cache_key = (symbol, interval)
    now = time.monotonic()
    cached = _klines_cache.get(cache_key)
    if cached is not None and (now - cached["ts"]) < cache_ttl:
        return cached["data"]

    r = await client.get(SPOT_KLINES_URL, params={
        "symbol": symbol, "interval": interval, "limit": limit
    })
    r.raise_for_status()
    rows = r.json()

    # Отбрасываем незакрытые свечи по фактическому close_time, а не «срезаем
    # последнюю вслепую» — так корректно обрабатываются пограничные случаи.
    now_ms = time.time() * 1000
    closed = [c for c in rows if float(c[6]) <= now_ms]

    result = {
        "highs":       [float(c[2]) for c in closed],
        "lows":        [float(c[3]) for c in closed],
        "closes":      [float(c[4]) for c in closed],
        "close_times": [int(c[6]) for c in closed],
    }
    _klines_cache[cache_key] = {"data": result, "ts": now}
    return result


async def fetch_current_price(client: httpx.AsyncClient, symbol: str) -> Optional[float]:
    """Свежая рыночная цена НЕПОСРЕДСТВЕННО перед отправкой в Telegram.

    Отдельный лёгкий запрос — только когда сигнал реально собирается уходить,
    не на каждой итерации цикла. Возвращает None при любой ошибке: цена в
    сообщении тогда честно помечается как недоступная, но сам сигнал всё
    равно отправляется."""
    try:
        r = await client.get(SPOT_PRICE_URL, params={"symbol": symbol}, timeout=8)
        r.raise_for_status()
        return float(r.json()["price"])
    except Exception as e:
        logger.warning("Не удалось получить свежую цену для %s: %s", symbol, e)
        return None


def _is_new_candle_signal(symbol: str, timeframe: str, setup: str,
                           direction: str, candle_close_ts: int) -> bool:
    """True, если по этой свече такой сигнал ещё не отправлялся.
    Побочно запоминает свечу — вызывать только когда сигнал действительно
    собирается уходить в Telegram."""
    key = (symbol, timeframe, setup, direction)
    last = _sent_signal_candles.get(key)
    if last is not None and candle_close_ts <= last:
        return False
    _sent_signal_candles[key] = candle_close_ts
    return True


# ════════════════════════════════════════════════
# ЕДИНЫЙ ТЕХНИЧЕСКИЙ СКАН (Breakout / Turtle Zone / Failure Test)
# ════════════════════════════════════════════════
# Один и тот же код обслуживает и дневной, и часовой контур, и основной
# символ, и остальные монеты. Раньше эта логика была продублирована в
# evaluate_signals() и scan_technical_symbols(), что при добавлении 1H
# означало бы четыре копии. См. DECISIONS.md #13.

async def fetch_trend_context(client: httpx.AsyncClient, symbol: str) -> Optional[dict]:
    """Контекст рынка по 4H и 1D для одного символа.

    ⚠️  ВЫЗЫВАЕТСЯ ТОЛЬКО КОГДА СИГНАЛ УЖЕ СРАБОТАЛ И ПРОШЁЛ ДЕДУП —
    не на каждой итерации цикла. Иначе это были бы лишние 34 запроса к
    Binance каждые 5 минут ради данных, которые чаще всего никому не нужны.

    ⚠️  РЕЗУЛЬТАТ ЗАМОРАЖИВАЕТСЯ В БД В МОМЕНТ СИГНАЛА. Пересчитывать его
    при резолюции задним числом нельзя: рынок к тому времени уедет, и вся
    статистика по alignment превратится в мусор. См. DECISIONS.md #14.

    Возвращает None при любой ошибке — сигнал уйдёт без блока контекста,
    но НЕ потеряется. 4H на этом этапе ничего не блокирует.
    """
    if not config.ENABLE_TREND_CONTEXT:
        return None
    try:
        h4 = await fetch_klines(client, symbol, "4h",
                                config.H4_KLINES_LIMIT, config.POLL_H4_SECS)
        d1 = await fetch_klines(client, symbol, "1d",
                                config.DAILY_KLINES_LIMIT, config.POLL_TECHNICAL_SECS)
    except Exception as e:
        logger.warning("Trend context fetch failed for %s: %s", symbol, e)
        return None

    try:
        n = config.SWING_LOOKBACK
        ctx_4h = tc.analyze(h4["highs"], h4["lows"], h4["close_times"], n=n)
        ctx_1d = tc.analyze(d1["highs"], d1["lows"], d1["close_times"], n=n)
        return {"h4": ctx_4h, "d1": ctx_1d}
    except Exception as e:
        logger.error("Trend context analyze failed for %s: %s", symbol, e)
        return None


async def _emit(client: httpx.AsyncClient, symbol: str, timeframe: str, setup: str,
                direction: str, message_builder, signal_price: float, entry_level: float,
                fast_n: int, candle_close_ts: int, strong: bool,
                highs: list, lows: list, closes: list) -> bool:
    """Дедуп по свече → свежая цена → отправка → запись в статистику.

    Порядок важен: свежая цена запрашивается ТОЛЬКО после того, как дедуп
    пропустил сигнал — иначе на каждой итерации летели бы лишние запросы."""
    if not _is_new_candle_signal(symbol, timeframe, setup, direction, candle_close_ts):
        return False

    current_price = await fetch_current_price(client, symbol)

    # Контекст 4H/1D — чистая информация. Если не получился, сигнал всё
    # равно уходит: 4H ничего не блокирует (явное требование пользователя).
    ctx = await fetch_trend_context(client, symbol)
    trend_4h = ctx["h4"]["trend"] if ctx else None
    trend_1d = ctx["d1"]["trend"] if ctx else None
    structure_4h = ctx["h4"]["structure"] if ctx else None
    alignment = tc.compute_alignment(direction, trend_4h, trend_1d)
    trendline = ctx["h4"]["trendline"] if ctx else None

    key = f"{setup}_{direction.lower()}_{symbol}_{timeframe}"
    sent = await engine.submit(Signal(
        key=key, strong=strong,
        message=message_builder(current_price) + tg.fmt_trend_context(
            trend_1d, trend_4h, structure_4h,
            ctx["h4"]["high_label"] if ctx else None,
            ctx["h4"]["low_label"] if ctx else None,
            alignment,
        ),
        priority=2,
    ))
    if not sent:
        return False

    try:
        await stats_tracker.record_signal(
            symbol=symbol, direction=direction, setup=setup,
            entry_price=signal_price, entry_level=entry_level, fast_n=fast_n,
            highs=highs, lows=lows, closes=closes,
            timeframe=timeframe, candle_close_ts=candle_close_ts,
            trend_1d=trend_1d, trend_4h=trend_4h, structure_4h=structure_4h,
            alignment=alignment,
            trendline_slope=trendline["slope"] if trendline else None,
            trendline_anchor_ts=trendline["anchor_ts"] if trendline else None,
            trendline_anchor_price=trendline["anchor_price"] if trendline else None,
        )
    except Exception as e:
        logger.error("statistics: record_signal failed (%s %s %s): %s", symbol, setup, timeframe, e)
    return True


async def scan_technical(client: httpx.AsyncClient, symbol: str, timeframe: str,
                          limit: int, cache_ttl: int):
    """Считает три индикатора по ЗАКРЫТЫМ свечам заданного таймфрейма."""
    try:
        data = await fetch_klines(client, symbol, timeframe, limit, cache_ttl)
    except Exception as e:
        logger.warning("Klines fetch failed for %s %s: %s", symbol, timeframe, e)
        return

    highs, lows, closes = data["highs"], data["lows"], data["closes"]
    if not closes:
        return
    candle_close_ts = data["close_times"][-1]

    # Резолюция ранее записанных OPEN-сигналов (слой статистики, на алерты не влияет)
    try:
        await stats_tracker.resolve_open_signals(symbol, highs, lows, closes, timeframe=timeframe)
    except Exception as e:
        logger.error("statistics: resolve_open_signals failed for %s %s: %s", symbol, timeframe, e)

    # ── Breakout ─────────────────────────────────────────────────────────────
    bo = ts.detect_breakout(highs, lows, closes, n=config.DONCHIAN_LOOKBACK)
    sent_breakout = False
    if bo:
        direction = "LONG" if bo["direction"] == "bullish" else "SHORT"
        sent_breakout = await _emit(
            client, symbol, timeframe, "breakout", direction,
            lambda cp: tg.fmt_breakout_alert(symbol, bo["direction"], bo["level"],
                                              bo["price"], bo["n"], timeframe, cp),
            signal_price=bo["price"], entry_level=bo["level"],
            fast_n=config.DONCHIAN_LOOKBACK, candle_close_ts=candle_close_ts,
            strong=True, highs=highs, lows=lows, closes=closes,
        )

    # ── Turtle Zone Filter ───────────────────────────────────────────────────
    tzone = ts.detect_turtle_zone(
        highs, lows, closes,
        fast=config.TURTLE_FAST_LOOKBACK, slow=config.TURTLE_SLOW_LOOKBACK,
    )
    if tzone:
        direction = "LONG" if tzone["direction"] == "bullish" else "SHORT"
        # Комбо: оба детектора сработали на одной свече в одну сторону, и
        # Breakout уже реально ушёл в Telegram — пишем в статистику как один
        # combo-сетап вместо двух строк (см. DECISIONS.md #12).
        setup = ("breakout_turtle_combo"
                 if (sent_breakout and bo and direction ==
                     ("LONG" if bo["direction"] == "bullish" else "SHORT"))
                 else "turtle_zone")
        await _emit(
            client, symbol, timeframe, setup, direction,
            lambda cp: tg.fmt_turtle_zone_alert(symbol, tzone["direction"], tzone["stage"],
                                                 tzone["fast_level"], tzone["slow_level"],
                                                 tzone["price"], timeframe, cp),
            signal_price=tzone["price"], entry_level=tzone["fast_level"],
            fast_n=config.TURTLE_FAST_LOOKBACK, candle_close_ts=candle_close_ts,
            strong=(tzone["stage"] == "confirmed"), highs=highs, lows=lows, closes=closes,
        )

    # ── Failure Test ─────────────────────────────────────────────────────────
    ft = ts.detect_failure_test(
        highs, lows, closes,
        n=config.DONCHIAN_LOOKBACK, lookback=config.FAILURE_TEST_LOOKBACK,
    )
    if ft:
        await _emit(
            client, symbol, timeframe, "failure_test", ft["direction"],
            lambda cp: tg.fmt_failure_test_alert(symbol, ft["direction"], ft["level"],
                                                  ft["price"], timeframe, cp),
            signal_price=ft["price"], entry_level=ft["level"],
            fast_n=config.DONCHIAN_LOOKBACK, candle_close_ts=candle_close_ts,
            strong=True, highs=highs, lows=lows, closes=closes,
        )


async def scan_all_technical(client: httpx.AsyncClient):
    """Основной символ + все TECHNICAL_SYMBOLS, дневной и часовой контуры."""
    symbols = [config.SYMBOL_SPOT] + [s for s in config.TECHNICAL_SYMBOLS if s != config.SYMBOL_SPOT]
    for symbol in symbols:
        await scan_technical(client, symbol, "1d",
                             config.DAILY_KLINES_LIMIT, config.POLL_TECHNICAL_SECS)
        if config.ENABLE_HOURLY_SIGNALS:
            await scan_technical(client, symbol, "1h",
                                 config.HOURLY_KLINES_LIMIT, config.POLL_HOURLY_SECS)


# ════════════════════════════════════════════════
# SIGNAL EVALUATION (volume / OI / funding — только основной символ)
# ════════════════════════════════════════════════

async def evaluate_signals(spot: dict, futures: Optional[dict]):
    """Volume / OI / funding сигналы по основному символу.

    Технические индикаторы здесь БОЛЬШЕ НЕ считаются — они вынесены в
    scan_technical(), общий для всех символов и обоих таймфреймов."""
    price = spot["price"]

    # ── Update shared state ───────────────────────────────────────────────────
    state.last_price     = price
    state.avg_volume_1h  = spot["vol_1h_avg"]
    state.avg_volume_4h  = spot.get("vol_4h_avg")
    if futures:
        state.last_funding = futures.get("funding")
        if futures.get("oi") is not None:
            prev_oi    = state.last_oi
            state.last_oi = futures["oi"]

    # ── Volume spike ──────────────────────────────────────────────────────────
    ratio = spot["vol_1h_ratio"]
    if ratio >= config.VOLUME_SPIKE_MULTIPLIER:
        strong = ratio >= config.VOLUME_SPIKE_MULTIPLIER * 2
        await engine.submit(Signal(
            key="volume_spike",
            strong=strong,
            message=tg.fmt_volume_spike(
                spot["vol_1h_current"],
                spot["vol_1h_avg"],
                ratio,
                price,
            ),
        ))

    # ── OI change ────────────────────────────────────────────────────────────
    if futures and futures.get("oi") is not None and state.last_oi is not None:
        prev_oi = state.last_oi
        oi      = futures["oi"]
        # prev_oi was set above; recalculate based on oi_history
        if state.oi_history:
            _, prev_oi_hist = state.oi_history[-1] if state.oi_history else (0, oi)
            oi_change_pct = (oi - prev_oi_hist) / prev_oi_hist * 100 if prev_oi_hist else 0
            if abs(oi_change_pct) >= config.OI_CHANGE_PCT_THRESHOLD:
                strong = abs(oi_change_pct) >= config.OI_CHANGE_PCT_THRESHOLD * 2
                event  = "spike" if oi_change_pct > 0 else "dump"
                await engine.submit(Signal(
                    key=f"oi_{event}",
                    strong=strong,
                    message=tg.fmt_oi_alert(event, oi, prev_oi_hist, oi_change_pct, price),
                ))
        state.oi_history.append((time.time(), oi))

    # ── Funding rate ──────────────────────────────────────────────────────────
    if futures and futures.get("funding") is not None:
        funding      = futures["funding"]
        funding_prev = futures.get("funding_prev", funding)
        extreme = abs(funding) >= config.FUNDING_EXTREME_PCT
        sudden  = (
            funding_prev != 0
            and abs((funding - funding_prev) / abs(funding_prev)) >= 0.5
        )
        if extreme or sudden:
            await engine.submit(Signal(
                key="funding_extreme",
                strong=extreme,
                message=tg.fmt_funding_alert(funding, funding_prev),
            ))


# ════════════════════════════════════════════════
# MAIN LOOP
# ════════════════════════════════════════════════

def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


async def run_loop():
    global _shutdown
    iteration = 0

    logger.info("=" * 60)
    logger.info("  Aster Intelligence Bot — LIVE MODE")
    logger.info("  Symbol:   %s", config.SYMBOL_SPOT)
    logger.info("  Technical-only symbols: %s", ", ".join(config.TECHNICAL_SYMBOLS))
    logger.info("  Timeframes: 1d%s (только ЗАКРЫТЫЕ свечи)",
                " + 1h" if config.ENABLE_HOURLY_SIGNALS else "")
    logger.info("  Interval: %ds", POLL_INTERVAL_SECS)
    logger.info("  Cooldown: %ds per signal type", config.ALERT_COOLDOWN_SECS)
    logger.info("  Statistics DB: %s", "configured" if config.DATABASE_URL else "NOT configured (statistics disabled)")
    logger.info("  Log file: logs/aster_bot.log")
    logger.info("=" * 60)

    await tg.send_alert(
        "🟢 <b>Aster Bot — Live Mode Started</b>\n\n"
        f"Main symbol: <code>{config.SYMBOL_SPOT}</code> — Volume, OI, Funding, Breakout, Turtle Zone, Failure Test\n"
        f"Technical-only: <code>{', '.join(config.TECHNICAL_SYMBOLS)}</code> — Breakout, Turtle Zone, Failure Test\n"
        f"Interval: every {POLL_INTERVAL_SECS // 60} min\n"
        f"Alerts: only on significant signals\n"
        f"Started: {_now_utc()}"
    )

    async with httpx.AsyncClient(
        timeout=12,
        headers={"User-Agent": "AsterIntelBot/1.0"},
    ) as client:

        while not _shutdown:
            iteration += 1
            t_start = time.monotonic()
            logger.info("── Check #%d at %s ──", iteration, _now_utc())

            # ── Spot data ─────────────────────────────────────────────────────
            try:
                spot = await fetch_spot(client)
                logger.info(
                    "SPOT  price=$%.6f  24h=%+.2f%%  vol_ratio=%.2f×  "
                    "24hVol=$%s",
                    spot["price"],
                    spot["change_24h"],
                    spot["vol_1h_ratio"],
                    f"{spot['vol_quote_24h']:,.0f}",
                )
            except Exception as e:
                logger.error("Spot fetch failed: %s", e)
                await asyncio.sleep(30)
                continue

            # ── Futures data (optional) ───────────────────────────────────────
            futures = None
            try:
                futures = await fetch_futures(client)
                if futures:
                    logger.info(
                        "FUTURES  funding=%+.4f%%  OI=%s",
                        futures.get("funding", 0),
                        f"{futures['oi']:,.0f}" if futures.get("oi") else "n/a",
                    )
                else:
                    logger.debug("Futures: not available or skipped.")
            except Exception as e:
                logger.warning("Futures fetch failed: %s", e)

            # ── Volume / OI / funding по основному символу ─────────────────────
            try:
                await evaluate_signals(spot, futures)
            except Exception as e:
                logger.error("Signal evaluation error: %s", e)

            # ── Технический скан: все символы × (1D и 1H), только закрытые свечи ──
            try:
                await scan_all_technical(client)
            except Exception as e:
                logger.error("Technical scan error: %s", e)

            # ── Sleep until next interval ─────────────────────────────────────
            elapsed = time.monotonic() - t_start
            sleep_for = max(0, POLL_INTERVAL_SECS - elapsed)
            logger.debug("Check took %.1fs. Sleeping %.0fs.", elapsed, sleep_for)

            # Sleep in small chunks so Ctrl+C is responsive
            deadline = time.monotonic() + sleep_for
            while not _shutdown and time.monotonic() < deadline:
                await asyncio.sleep(min(5, deadline - time.monotonic()))


async def shutdown(reason: str = "manual stop"):
    global _shutdown
    _shutdown = True
    logger.info("Shutdown requested: %s", reason)
    await tg.send_alert(
        f"🔴 <b>Aster Bot — Stopped</b>\n"
        f"Reason: {reason}\n"
        f"Time: {_now_utc()}"
    )


async def main():
    loop = asyncio.get_running_loop()

    # Health-check сервер для Render Web Service (бесплатный тариф) — не мешает
    # основному циклу, просто отвечает "alive" на HTTP-пинги платформы.
    _start_health_server()

    # Telegram command listener (/stats /week /today /month) — independent
    # background task. Fully additive: if it crashes, alerting is unaffected
    # (asyncio.create_task fire-and-forget; errors are logged inside the
    # task itself, see signal_stats/telegram_commands.py).
    asyncio.create_task(run_command_listener())

    # Handle Ctrl+C and kill signals gracefully
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(
            sig,
            lambda s=sig: asyncio.create_task(shutdown(f"signal {s.name}"))
        )

    try:
        await run_loop()
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.critical("Unhandled error in main loop: %s", e, exc_info=True)
        await tg.send_alert(f"🚨 <b>Aster Bot CRASHED</b>\n{e}")
        raise


if __name__ == "__main__":
    asyncio.run(main())
