"""
technical_signals.py — Donchian-based technical signals (Breakout, Turtle Zone
Filter, Failure Test), computed from daily OHLC candles.

⚠️  ВАЖНО: это СТАНДАРТНЫЕ, общепринятые реализации этих трёх концепций,
а не точная копия твоих кастомных Pine-скриптов на TradingView (у нас нет
их исходного кода). Логика близка по смыслу, но сигналы могут отличаться
по точным точкам входа от того, что рисует TradingView.

Все функции — чистые (без побочных эффектов), принимают списки daily
highs/lows/closes (старые → новые), возвращают dict с сигналом или None.
"""

from typing import Optional


def _donchian(highs: list[float], lows: list[float], n: int) -> tuple[float, float]:
    """Верхний/нижний канал за N баров ПЕРЕД текущим (текущий бар исключён)."""
    return max(highs[-n - 1:-1]), min(lows[-n - 1:-1])


# ─────────────────────────────────────────────────────────────
#  1. BREAKOUT — пробой N-периодного канала (классический Donchian)
# ─────────────────────────────────────────────────────────────
def detect_breakout(
    highs: list[float], lows: list[float], closes: list[float], n: int = 20
) -> Optional[dict]:
    if len(closes) < n + 2:
        return None

    upper, lower = _donchian(highs, lows, n)
    price, prev = closes[-1], closes[-2]

    if prev <= upper < price:
        return {"direction": "bullish", "level": upper, "price": price, "n": n}
    if prev >= lower > price:
        return {"direction": "bearish", "level": lower, "price": price, "n": n}
    return None


# ─────────────────────────────────────────────────────────────
#  2. TURTLE ZONE FILTER — классическая двухканальная система
#     (System 1 = 20 баров вход, System 2 = 55 баров подтверждение)
# ─────────────────────────────────────────────────────────────
def detect_turtle_zone(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    fast: int = 20,
    slow: int = 55,
) -> Optional[dict]:
    if len(closes) < slow + 2:
        return None

    fast_upper, fast_lower = _donchian(highs, lows, fast)
    slow_upper, slow_lower = _donchian(highs, lows, slow)
    price, prev = closes[-1], closes[-2]

    # Пробой быстрого (20-барного) канала вверх
    if prev <= fast_upper < price:
        stage = "confirmed" if price > slow_upper else "zone"
        return {
            "direction": "bullish", "stage": stage,
            "fast_level": fast_upper, "slow_level": slow_upper, "price": price,
        }

    # Пробой быстрого (20-барного) канала вниз
    if prev >= fast_lower > price:
        stage = "confirmed" if price < slow_lower else "zone"
        return {
            "direction": "bearish", "stage": stage,
            "fast_level": fast_lower, "slow_level": slow_lower, "price": price,
        }

    return None


# ─────────────────────────────────────────────────────────────
#  3. FAILURE TEST — ложный пробой (цена пробила канал, но не удержалась)
# ─────────────────────────────────────────────────────────────
def detect_failure_test(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    n: int = 20,
    lookback: int = 5,
) -> Optional[dict]:
    if len(closes) < n + lookback + 2:
        return None

    # Канал берём ТАКИМ, КАКИМ ОН БЫЛ ДО начала окна проверки (`lookback` баров
    # назад) — иначе сам пробой попадает в расчёт канала и маскирует ложный пробой,
    # так как Donchian канал обновляется на каждом новом баре.
    ref_highs = highs[:-lookback][-n:]
    ref_lows  = lows[:-lookback][-n:]
    upper, lower = max(ref_highs), min(ref_lows)

    price = closes[-1]
    recent_highs = highs[-lookback:-1]
    recent_lows = lows[-lookback:-1]

    # Был пробой вверх за последние `lookback` баров (относительно старого канала),
    # но цена вернулась под канал → ловушка для лонгов, сигнал на SHORT
    if max(recent_highs) > upper and price < upper:
        return {"direction": "SHORT", "level": upper, "price": price}

    # Был пробой вниз за последние `lookback` баров, но цена вернулась над каналом
    # → ловушка для шортов, сигнал на LONG
    if min(recent_lows) < lower and price > lower:
        return {"direction": "LONG", "level": lower, "price": price}

    return None
