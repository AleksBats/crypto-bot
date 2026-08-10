"""
trade_state.py — Telegram OUTPUT layer: превращает сработавший детектор в один
конечный торговый сигнал и следит за тем, открыт ли уже трейд по символу.

ЧТО ЭТОТ МОДУЛЬ НЕ ДЕЛАЕТ (важно):
  - не считает индикаторы и не решает, был ли сигнал;
  - не меняет пороги;
  - не трогает таблицу `signals` и правило WIN/LOSS в signal_stats/.

Он получает УЖЕ готовый сигнал от детектора и добавляет к нему уровни сделки
(entry/stop/target) и размер позиции — ровно по формулам локального бота
пользователя, ничего не придумано заново:

    Stop     = level x (1 -+ STOP_BUFFER_PCT)     # чуть внутрь пробитого уровня
    MMO      = level +- (channel_high - channel_low)
    1R       = entry +- (entry - stop)
    risk_usd = ACCOUNT_SIZE x RISK_PCT
    notional = min(risk_usd / stop_dist_pct, ACCOUNT_SIZE x LEVERAGE)
    margin   = notional / LEVERAGE

ДВА НЕЗАВИСИМЫХ СЛОЯ — сознательное решение, см. DECISIONS.md #15.
Стоп, который показан в Telegram и по которому закрывается OPEN, — это стоп
локального бота (узкий). Правило WIN/LOSS в signal_stats/ осталось прежним
(границы каналов Дончиана, широкий). Они НЕ синхронизированы намеренно: так
на одних и тех же сигналах копится статистика по двум разным правилам выхода,
и через несколько недель можно будет объективно сравнить, какое точнее.
Ожидаемое следствие: сделка может числиться STOP_HIT здесь и одновременно
OPEN или WIN в /week. Это не рассинхрон, это две разные метрики.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

# Приоритет при нескольких детекторах на одной свече. Первый в списке даёт
# числа (у него есть структурный уровень), остальные попадают только в подпись.
SETUP_PRIORITY = ["breakout", "failure_test", "turtle_zone"]

SETUP_LABELS = {
    "breakout": "Breakout",
    "failure_test": "Failure Test",
    "turtle_zone": "Turtle Zone",
    "breakout_turtle_combo": "Breakout + Turtle Zone",
}

# Причина блокировки -> статус, под которым кандидат сохраняется в Neon.
# Кандидаты НЕ теряются: даже не отправленный сигнал остаётся в базе с
# посчитанными уровнями, чтобы потом можно было оценить, что мы пропустили.
SKIP_STATUS = {
    "max_active_trades": "SKIPPED_CAPACITY",
    "already_open": "SKIPPED_SYMBOL_OPEN",
    "cooldown": "SKIPPED_COOLDOWN",
}

# Длительность свечи в миллисекундах — для паузы после закрытия сделки.
TF_MS = {
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}


# ============================================================================
# ЧИСТЫЕ ФУНКЦИИ — уровни сделки и размер позиции
# ============================================================================

def build_levels(direction: str, entry: float, level: Optional[float],
                 channel_high: Optional[float], channel_low: Optional[float],
                 stop_buffer_pct: float, default_stop_pct: float) -> Optional[dict]:
    """Считает stop / MMO / 1R для одного сигнала.

    `level` = пробитый (или протестированный) уровень. Если его нет — Turtle
    Zone, направление без структуры — стоп берётся плоским от входа.

    Возвращает None, если получилась бессмысленная сделка (стоп по ту же
    сторону от входа, что и цель, или нулевая дистанция). Такой сигнал в
    Telegram не уходит — лучше промолчать, чем показать сделку с риском 0.
    """
    if not entry or entry <= 0:
        return None
    is_long = direction.upper() == "LONG"

    if level and level > 0:
        stop = level * (1 - stop_buffer_pct) if is_long else level * (1 + stop_buffer_pct)
        structural = True
    else:
        stop = entry * (1 - default_stop_pct) if is_long else entry * (1 + default_stop_pct)
        structural = False

    # Санити-проверка: для LONG стоп обязан быть ниже входа, для SHORT — выше.
    # Нарушается, например, если Failure Test сработал ровно на уровне.
    if (is_long and stop >= entry) or (not is_long and stop <= entry):
        return None

    risk = abs(entry - stop)
    if risk <= 0:
        return None

    target_1r = entry + risk if is_long else entry - risk

    target_mmo = None
    if structural and channel_high is not None and channel_low is not None:
        height = channel_high - channel_low
        if height > 0:
            target_mmo = level + height if is_long else level - height
            # Цель обязана быть по нужную сторону от входа, иначе она бесполезна.
            if (is_long and target_mmo <= entry) or (not is_long and target_mmo >= entry):
                target_mmo = None

    return {
        "direction": direction.upper(),
        "entry": float(entry),
        "stop": float(stop),
        "target_mmo": float(target_mmo) if target_mmo is not None else None,
        "target_1r": float(target_1r),
        "risk_abs": float(risk),
        "risk_pct": float(risk / entry * 100),
        "structural": structural,
    }


def position_size(entry: float, stop: float, account_size: float,
                  risk_pct: float, leverage: float) -> Optional[dict]:
    """Fixed-fractional sizing — порт fmt_position_block() из локального бота.

    Позиция подбирается так, чтобы выбивание по стопу стоило ровно
    account_size x risk_pct. Номинал ограничен плечом; когда ограничение
    срабатывает, реальный риск получается МЕНЬШЕ заданного, и это явно
    помечается флагом `capped`.
    """
    if not entry or not stop or entry <= 0:
        return None
    stop_dist_pct = abs(entry - stop) / entry
    if stop_dist_pct <= 0:
        return None

    risk_usd = account_size * risk_pct
    ideal_notional = risk_usd / stop_dist_pct
    max_notional = account_size * leverage

    notional = min(ideal_notional, max_notional)
    capped = ideal_notional > max_notional

    return {
        "qty": notional / entry,
        "notional": notional,
        "margin": notional / leverage,
        "risk_usd": notional * stop_dist_pct,
        "risk_pct_of_account": notional * stop_dist_pct / account_size * 100,
        "stop_dist_pct": stop_dist_pct * 100,
        "capped": capped,
        "leverage": leverage,
        "account_size": account_size,
    }


def primary_target(levels: dict) -> Optional[float]:
    """Цель, по которой закрывается OPEN — БЛИЖАЙШАЯ из имеющихся.

    Сознательный выбор: если показаны две цели, трейдер почти наверняка
    начнёт фиксировать на первой достигнутой, поэтому состояние тоже
    закрывается на ней. Дальняя цель остаётся информационной.
    """
    entry = levels["entry"]
    cands = [t for t in (levels.get("target_mmo"), levels.get("target_1r")) if t is not None]
    if not cands:
        return None
    return min(cands, key=lambda t: abs(t - entry))


def check_close(direction: str, stop: float, target: Optional[float],
                highs: list, lows: list, close_times: list,
                since_ts: int) -> Optional[dict]:
    """Ищет STOP HIT / TARGET HIT среди ЗАКРЫТЫХ свечей после since_ts.

    Проверяются все свечи новее since_ts, а не только последняя: если Render
    простоял час, пропущенные свечи всё равно будут учтены.

    Если внутри одной свечи задеты и стоп, и цель — считаем STOP HIT.
    Порядок событий внутри бара по OHLC неизвестен, и занижать убыток хуже,
    чем занижать прибыль.
    """
    is_long = direction.upper() == "LONG"
    n = min(len(highs), len(lows), len(close_times))

    for i in range(n):
        ts = close_times[i]
        if ts <= since_ts:
            continue
        hi, lo = highs[i], lows[i]

        stop_hit = (lo <= stop) if is_long else (hi >= stop)
        target_hit = False
        if target is not None:
            target_hit = (hi >= target) if is_long else (lo <= target)

        if stop_hit:
            return {"reason": "STOP_HIT", "price": stop, "ts": ts}
        if target_hit:
            return {"reason": "TARGET_HIT", "price": target, "ts": ts}
    return None


def cooldown_until(closed_ts: int, timeframe: str) -> int:
    """Пауза после закрытия — одна закрытая свеча того же таймфрейма."""
    return int(closed_ts) + TF_MS.get(timeframe, TF_MS["1h"])


def setup_label(setups: list) -> str:
    """'Breakout + Turtle Zone' из списка сработавших детекторов."""
    ordered = [s for s in SETUP_PRIORITY if s in setups]
    ordered += [s for s in setups if s not in SETUP_PRIORITY]
    return " + ".join(SETUP_LABELS.get(s, s) for s in ordered) or "-"


# ============================================================================
# РЕЕСТР ОТКРЫТЫХ СДЕЛОК
# ============================================================================

class OpenTradeRegistry:
    """Кто сейчас занят. Зеркало в памяти + источник правды в Neon.

    Ключ — ТОЛЬКО символ, без таймфрейма: пока по SOLUSDT открыта сделка с 1H,
    сигнал по 1D тоже не уходит. Это прямое требование ("один symbol не
    получает новый trade signal, пока предыдущий OPEN").

    Если DATABASE_URL не задан, реестр работает только в памяти: гейт
    действует до перезапуска, после рестарта состояние теряется. Так
    сохраняется существующий принцип — бот полностью работоспособен без БД.
    """

    def __init__(self, max_active: int):
        self.max_active = max_active
        self._open: dict = {}       # symbol -> trade dict
        self._cooldown: dict = {}   # symbol -> ts (ms), до которого нельзя открывать
        self.restored_from_db = False

    # -- чтение --------------------------------------------------------------

    def is_open(self, symbol: str) -> bool:
        return symbol in self._open

    def get(self, symbol: str) -> Optional[dict]:
        return self._open.get(symbol)

    def open_count(self) -> int:
        return len(self._open)

    def open_symbols(self) -> list:
        return sorted(self._open.keys())

    def blocked_reason(self, symbol: str, now_ms: Optional[int] = None) -> Optional[str]:
        """Почему по символу нельзя отправлять сигнал. None = можно."""
        if symbol in self._open:
            return "already_open"
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        until = self._cooldown.get(symbol)
        if until and now_ms < until:
            return "cooldown"
        if len(self._open) >= self.max_active:
            return "max_active_trades"
        return None

    # -- запись --------------------------------------------------------------

    def restore(self, rows: list):
        """Восстановление после рестарта Render из active_trades (status='OPEN')."""
        self._open.clear()
        for r in rows:
            self._open[r["symbol"]] = dict(r)
        self.restored_from_db = True
        logger.info(
            "trade_state: restored %d open trades from Neon: %s",
            len(self._open), ", ".join(self.open_symbols()) or "-",
        )

    def mark_open(self, trade: dict):
        self._open[trade["symbol"]] = trade
        self._cooldown.pop(trade["symbol"], None)

    def mark_closed(self, symbol: str, closed_ts: int, timeframe: str):
        self._open.pop(symbol, None)
        self._cooldown[symbol] = cooldown_until(closed_ts, timeframe)
