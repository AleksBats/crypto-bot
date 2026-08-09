"""
trend_context.py — рыночная структура и направление тренда (HH/HL/LH/LL),
динамическая трендовая линия по подтверждённым swing-точкам.

⚠️  ЭТОТ МОДУЛЬ НЕ ПОРОЖДАЕТ СИГНАЛЫ И НИЧЕГО НЕ БЛОКИРУЕТ.
Он только описывает контекст рынка. Breakout / Turtle Zone / Failure Test
в technical_signals.py работают ровно как раньше и ничего отсюда не
получают. См. DECISIONS.md #14.

Все функции чистые: принимают массивы OHLC (старые → новые), возвращают
dict или None. Никаких побочных эффектов, никаких запросов в сеть.

═══════════════════════════════════════════════════════════════════════
  ГЛАВНОЕ ПРАВИЛО: НИКАКОГО LOOK-AHEAD
═══════════════════════════════════════════════════════════════════════
Swing point на позиции i считается подтверждённым, только если он —
экстремум на отрезке [i-N, i+N], а значит нужны N ЗАКРЫВШИХСЯ свечей
СПРАВА от него. Поэтому:

  - кандидаты берутся только из диапазона i ∈ [N, len-1-N];
  - последние N свечей НИКОГДА не могут дать подтверждённый swing;
  - вызывающий код обязан подавать сюда массив уже БЕЗ незакрытой свечи
    (run_live.fetch_klines это гарантирует).

Отсюда неизбежная задержка подтверждения: N баров. На 4H при N=2 это
8 часов. Это честная цена за отсутствие перерисовки — альтернативы нет.

Трендовая линия строится по ДВУМ последним подтверждённым swing-точкам.
Обе они в прошлом и уже неизменны, поэтому линия не перерисовывает
историю: появление нового swing создаёт НОВУЮ линию, а не переписывает
старую.
"""

from typing import Optional

# Значения структуры
STRUCT_BULLISH = "HH_HL"
STRUCT_BEARISH = "LH_LL"
STRUCT_MIXED = "MIXED"

# Значения тренда
TREND_BULLISH = "BULLISH"
TREND_BEARISH = "BEARISH"
TREND_NEUTRAL = "NEUTRAL"

# Значения согласованности таймфреймов
ALIGN_STRONG = "STRONG"
ALIGN_PARTIAL = "PARTIAL"
ALIGN_CONFLICT = "CONFLICT"
ALIGN_UNKNOWN = "UNKNOWN"


# ─────────────────────────────────────────────────────────────
#  1. Поиск подтверждённых swing-точек (фракталы)
# ─────────────────────────────────────────────────────────────

def find_swings(highs: list[float], lows: list[float],
                 close_times: Optional[list[int]] = None,
                 n: int = 2) -> dict:
    """Все ПОДТВЕРЖДЁННЫЕ swing highs и swing lows.

    swing high на i: highs[i] строго больше всех highs в [i-n, i+n]\\{i}
    swing low  на i: lows[i]  строго меньше всех lows  в [i-n, i+n]\\{i}

    Строгое неравенство выбрано намеренно: при нестрогом плато из
    одинаковых значений дало бы несколько «swing» подряд на одном уровне.

    Возвращает {"highs": [...], "lows": [...]}, где каждый элемент —
    {"index", "price", "close_time"}, отсортированные по времени.
    Пустые списки, если данных не хватает — это нормальное состояние,
    а не ошибка.
    """
    result = {"highs": [], "lows": []}
    if n < 1 or len(highs) < 2 * n + 1 or len(lows) != len(highs):
        return result

    last_confirmable = len(highs) - 1 - n   # дальше подтверждения быть не может
    for i in range(n, last_confirmable + 1):
        window = range(i - n, i + n + 1)

        if all(highs[i] > highs[j] for j in window if j != i):
            result["highs"].append({
                "index": i, "price": highs[i],
                "close_time": close_times[i] if close_times else None,
            })

        if all(lows[i] < lows[j] for j in window if j != i):
            result["lows"].append({
                "index": i, "price": lows[i],
                "close_time": close_times[i] if close_times else None,
            })

    return result


# ─────────────────────────────────────────────────────────────
#  2. Классификация структуры рынка
# ─────────────────────────────────────────────────────────────

def classify_structure(swings: dict) -> dict:
    """HH/HL/LH/LL по двум последним подтверждённым точкам каждого типа.

    HH  — последний swing high выше предыдущего
    HL  — последний swing low  выше предыдущего
    LH  — последний swing high ниже предыдущего
    LL  — последний swing low  ниже предыдущего

    HH + HL → bullish · LH + LL → bearish · всё остальное → mixed

    Требуется минимум по 2 подтверждённые точки каждого типа. Если их
    меньше — структура неизвестна (MIXED с labels=None), и это честно
    отражается в отчётах, а не подменяется «нейтральным трендом».
    """
    sh, sl = swings.get("highs", []), swings.get("lows", [])
    if len(sh) < 2 or len(sl) < 2:
        return {"structure": STRUCT_MIXED, "high_label": None, "low_label": None,
                "last_high": sh[-1] if sh else None, "last_low": sl[-1] if sl else None}

    high_label = "HH" if sh[-1]["price"] > sh[-2]["price"] else "LH"
    low_label = "HL" if sl[-1]["price"] > sl[-2]["price"] else "LL"

    if high_label == "HH" and low_label == "HL":
        structure = STRUCT_BULLISH
    elif high_label == "LH" and low_label == "LL":
        structure = STRUCT_BEARISH
    else:
        structure = STRUCT_MIXED

    return {"structure": structure, "high_label": high_label, "low_label": low_label,
            "last_high": sh[-1], "last_low": sl[-1]}


# ─────────────────────────────────────────────────────────────
#  3. Направление тренда
# ─────────────────────────────────────────────────────────────

def trend_from_structure(structure: str) -> str:
    """Тренд ЕСТЬ структура — отдельного индикатора намеренно нет.

    Так в системе одно определение тренда, а не два конкурирующих, и не
    появляется ни одного нового порога. См. DECISIONS.md #14.
    """
    if structure == STRUCT_BULLISH:
        return TREND_BULLISH
    if structure == STRUCT_BEARISH:
        return TREND_BEARISH
    return TREND_NEUTRAL


# ─────────────────────────────────────────────────────────────
#  4. Динамическая трендовая линия
# ─────────────────────────────────────────────────────────────

def build_trendline(swings: dict, trend: str) -> Optional[dict]:
    """Линия по ДВУМ последним подтверждённым swing-точкам.

    bullish → по swing lows (поддержка снизу)
    bearish → по swing highs (сопротивление сверху)
    neutral → линия не строится (None) — рисовать её было бы вымыслом

    Как x берётся close_time в миллисекундах, а не индекс свечи: тогда
    формула один-в-один переносится в TradingView без пересчёта индексов
    (см. Pine-скрипт в trendline.pine).

        slope = (y₂ − y₁) / (x₂ − x₁)
        value(x) = y₁ + slope · (x − x₁)

    Перерисовки нет: обе точки подтверждены и лежат в прошлом. Новый
    swing порождает НОВУЮ линию, старая остаётся в истории как была.
    """
    if trend == TREND_BULLISH:
        points = swings.get("lows", [])
    elif trend == TREND_BEARISH:
        points = swings.get("highs", [])
    else:
        return None

    if len(points) < 2:
        return None

    p1, p2 = points[-2], points[-1]
    x1, x2 = p1["close_time"], p2["close_time"]
    if x1 is None or x2 is None or x2 == x1:
        return None

    slope = (p2["price"] - p1["price"]) / (x2 - x1)
    return {
        "slope": slope,                 # цена за миллисекунду
        "anchor_ts": x1,                # x₁
        "anchor_price": p1["price"],    # y₁
        "second_ts": x2,
        "second_price": p2["price"],
        "basis": "lows" if trend == TREND_BULLISH else "highs",
    }


def trendline_value_at(trendline: Optional[dict], ts: int) -> Optional[float]:
    """Значение линии в произвольный момент времени (мс)."""
    if not trendline:
        return None
    return trendline["anchor_price"] + trendline["slope"] * (ts - trendline["anchor_ts"])


# ─────────────────────────────────────────────────────────────
#  5. Полный контекст по одному таймфрейму
# ─────────────────────────────────────────────────────────────

def analyze(highs: list[float], lows: list[float],
            close_times: Optional[list[int]] = None, n: int = 2) -> dict:
    """Swing-точки → структура → тренд → линия. Одним вызовом.

    Один и тот же код обслуживает и 4H, и 1D: разница только во входных
    свечах. Двух разных определений тренда в системе быть не должно.
    """
    swings = find_swings(highs, lows, close_times, n=n)
    struct = classify_structure(swings)
    trend = trend_from_structure(struct["structure"])
    return {
        "trend": trend,
        "structure": struct["structure"],
        "high_label": struct["high_label"],
        "low_label": struct["low_label"],
        "last_high": struct["last_high"],
        "last_low": struct["last_low"],
        "trendline": build_trendline(swings, trend),
        "swing_count": {"highs": len(swings["highs"]), "lows": len(swings["lows"])},
    }


# ─────────────────────────────────────────────────────────────
#  6. Согласованность таймфреймов
# ─────────────────────────────────────────────────────────────

def compute_alignment(direction: str, trend_4h: Optional[str],
                       trend_1d: Optional[str]) -> str:
    """Насколько сигнал согласован со старшими таймфреймами.

        STRONG   — 1D, 4H и направление сигнала смотрят в одну сторону
        PARTIAL  — 4H согласован, 1D нейтрален или расходится
        CONFLICT — 4H против сигнала
        UNKNOWN  — 4H неизвестен (не хватило данных / не загрузился)

    ⚠️  Это ТОЛЬКО метка для статистики. На отправку сигнала она не
    влияет никак — на первом этапе 4H не блокирует ничего (явное
    требование пользователя). Значение вычисляется один раз в момент
    сигнала и замораживается в БД: пересчёт задним числом сделал бы
    всю статистику по alignment бессмысленной.
    """
    if not trend_4h or trend_4h == TREND_NEUTRAL:
        return ALIGN_UNKNOWN if not trend_4h else ALIGN_PARTIAL

    wanted = TREND_BULLISH if direction == "LONG" else TREND_BEARISH
    if trend_4h != wanted:
        return ALIGN_CONFLICT
    if trend_1d == wanted:
        return ALIGN_STRONG
    return ALIGN_PARTIAL
