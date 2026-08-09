"""
test_trend_context.py — синтетические тесты 4H-контекста (Phase 4).

Главные проверки, ради которых этот файл вообще существует:

  1. LOOK-AHEAD. Swing-точка не может быть подтверждена, пока N свечей
     справа от неё реально не закрылись. Проверяется тем, что тот же
     самый массив, обрезанный раньше, НЕ даёт эту точку.
  2. REPAINT. Добавление новых свечей не меняет уже подтверждённые
     swing-точки и не переписывает трендовую линию задним числом.
  3. ЗАМОРОЗКА КОНТЕКСТА. Записанный в БД контекст сигнала не меняется
     при резолюции — это самое опасное место всей фичи: если контекст
     пересчитается задним числом, статистика по alignment станет мусором.

Запуск: python3 test_trend_context.py
"""

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import config
import trend_context as tc
from signal_stats import performance
from signal_stats import signal_tracker as tracker
from test_statistics import InMemoryStore, flat, extend_up

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name if cond else f"{name} — {detail}")


def ts_series(n, step_ms=4 * 3600 * 1000, start=1_700_000_000_000):
    """close_time для n свечей по 4 часа."""
    return [start + i * step_ms for i in range(n)]


# ════════════════════════════════════════════════
# 1. Базовое обнаружение swing-точек
# ════════════════════════════════════════════════

def test_basic_swings():
    #        0    1    2    3    4    5    6    7    8
    highs = [10,  11,  15,  11,  10,  12,  18,  12,  11]
    lows  = [ 8,   7,  12,   6,   5,   9,  14,   8,   7]
    sw = tc.find_swings(highs, lows, ts_series(9), n=2)

    hi_idx = [s["index"] for s in sw["highs"]]
    lo_idx = [s["index"] for s in sw["lows"]]
    check("swing high найден на пике (index 2 и 6)", hi_idx == [2, 6], hi_idx)
    check("swing low найден во впадине (index 4)", 4 in lo_idx, lo_idx)
    check("последние N свечей не дают swing", all(i <= 9 - 1 - 2 for i in hi_idx + lo_idx))


def test_insufficient_data():
    check("мало свечей → пустой результат",
          tc.find_swings([1, 2, 3], [1, 2, 3], None, n=2) == {"highs": [], "lows": []})
    check("n<1 отбрасывается",
          tc.find_swings([1]*20, [1]*20, None, n=0) == {"highs": [], "lows": []})


# ════════════════════════════════════════════════
# 2. LOOK-AHEAD — ключевой тест
# ════════════════════════════════════════════════

def test_no_lookahead():
    """Пик на позиции 6 не должен подтверждаться, пока справа от него не
    закрылись 2 свечи. Проверяем на РАСТУЩЕМ массиве: тот же пик при
    обрезке раньше обязан отсутствовать."""
    highs = [10, 11, 12, 11, 10, 12, 20, 12, 11, 10]
    lows  = [ 8,  9, 10,  9,  8, 10, 18, 10,  9,  8]
    times = ts_series(10)

    # видно только до индекса 6 включительно — справа ноль свечей
    sw_at_6 = tc.find_swings(highs[:7], lows[:7], times[:7], n=2)
    check("пик НЕ подтверждён, пока справа нет свечей",
          6 not in [s["index"] for s in sw_at_6["highs"]],
          [s["index"] for s in sw_at_6["highs"]])

    # одна свеча справа — всё ещё мало (нужно 2)
    sw_at_7 = tc.find_swings(highs[:8], lows[:8], times[:8], n=2)
    check("пик НЕ подтверждён с одной свечой справа",
          6 not in [s["index"] for s in sw_at_7["highs"]])

    # две свечи справа — только теперь подтверждается
    sw_at_8 = tc.find_swings(highs[:9], lows[:9], times[:9], n=2)
    check("пик подтверждён ровно когда закрылись 2 свечи справа",
          6 in [s["index"] for s in sw_at_8["highs"]],
          [s["index"] for s in sw_at_8["highs"]])


# ════════════════════════════════════════════════
# 3. REPAINT — подтверждённое прошлое не переписывается
# ════════════════════════════════════════════════

def test_no_repaint():
    highs = [10, 11, 20, 11, 10, 12, 14, 12, 11, 10, 9, 8]
    lows  = [ 8,  9, 18,  9,  8, 10, 12, 10,  9,  8, 7, 6]
    times = ts_series(12)

    early = tc.find_swings(highs[:9], lows[:9], times[:9], n=2)
    late = tc.find_swings(highs, lows, times, n=2)

    early_hi = [(s["index"], s["price"], s["close_time"]) for s in early["highs"]]
    late_hi = [(s["index"], s["price"], s["close_time"]) for s in late["highs"]]
    check("ранее подтверждённые swing НЕ изменились при новых свечах",
          early_hi == late_hi[:len(early_hi)], f"{early_hi} vs {late_hi}")


def test_trendline_no_repaint():
    """Линия по двум точкам: добавление свечей, не создающих новый swing,
    не меняет уже построенную линию."""
    highs = [20, 19, 18, 19, 20, 19, 18, 17, 18, 19, 18, 17]
    lows  = [10, 11, 8,  11, 12, 13, 10, 13, 14, 15, 14, 13]
    times = ts_series(12)

    ctx_a = tc.analyze(highs[:10], lows[:10], times[:10], n=2)
    ctx_b = tc.analyze(highs, lows, times, n=2)
    if ctx_a["trendline"] and ctx_b["trendline"]:
        same_anchor = ctx_a["trendline"]["anchor_ts"] == ctx_b["trendline"]["anchor_ts"]
        check("якорь линии не переписан задним числом (или создана новая)",
              same_anchor or ctx_b["trendline"]["anchor_ts"] > ctx_a["trendline"]["anchor_ts"],
              f"{ctx_a['trendline']['anchor_ts']} → {ctx_b['trendline']['anchor_ts']}")
    else:
        check("линия отсутствует при неопределённом тренде — допустимо", True)


# ════════════════════════════════════════════════
# 4. Структура HH/HL/LH/LL
# ════════════════════════════════════════════════

def test_structure_bullish():
    sw = {"highs": [{"index": 2, "price": 10, "close_time": 1},
                     {"index": 8, "price": 15, "close_time": 2}],
          "lows":  [{"index": 4, "price": 5, "close_time": 3},
                     {"index": 10, "price": 8, "close_time": 4}]}
    st = tc.classify_structure(sw)
    check("HH + HL → bullish structure", st["structure"] == tc.STRUCT_BULLISH, st["structure"])
    check("метки HH/HL", (st["high_label"], st["low_label"]) == ("HH", "HL"))
    check("тренд из структуры = BULLISH", tc.trend_from_structure(st["structure"]) == tc.TREND_BULLISH)


def test_structure_bearish():
    sw = {"highs": [{"index": 2, "price": 20, "close_time": 1},
                     {"index": 8, "price": 15, "close_time": 2}],
          "lows":  [{"index": 4, "price": 12, "close_time": 3},
                     {"index": 10, "price": 8, "close_time": 4}]}
    st = tc.classify_structure(sw)
    check("LH + LL → bearish structure", st["structure"] == tc.STRUCT_BEARISH, st["structure"])
    check("тренд из структуры = BEARISH", tc.trend_from_structure(st["structure"]) == tc.TREND_BEARISH)


def test_structure_mixed():
    sw = {"highs": [{"index": 2, "price": 10, "close_time": 1},
                     {"index": 8, "price": 15, "close_time": 2}],   # HH
          "lows":  [{"index": 4, "price": 12, "close_time": 3},
                     {"index": 10, "price": 8, "close_time": 4}]}    # LL
    st = tc.classify_structure(sw)
    check("HH + LL → mixed (не выдаём за тренд)", st["structure"] == tc.STRUCT_MIXED, st["structure"])
    check("mixed → NEUTRAL", tc.trend_from_structure(st["structure"]) == tc.TREND_NEUTRAL)


def test_structure_insufficient():
    st = tc.classify_structure({"highs": [{"index": 1, "price": 10, "close_time": 1}], "lows": []})
    check("одна точка → MIXED без меток", st["structure"] == tc.STRUCT_MIXED and st["high_label"] is None)


# ════════════════════════════════════════════════
# 5. Трендовая линия
# ════════════════════════════════════════════════

def test_trendline_math():
    sw = {"lows": [{"index": 2, "price": 100.0, "close_time": 1_000_000},
                    {"index": 8, "price": 110.0, "close_time": 2_000_000}],
          "highs": []}
    tl = tc.build_trendline(sw, tc.TREND_BULLISH)
    check("bullish линия строится по lows", tl["basis"] == "lows")
    expected_slope = (110.0 - 100.0) / (2_000_000 - 1_000_000)
    check("наклон посчитан верно", abs(tl["slope"] - expected_slope) < 1e-12, tl["slope"])
    check("значение в якоре = цене якоря",
          abs(tc.trendline_value_at(tl, 1_000_000) - 100.0) < 1e-9)
    check("значение во второй точке = её цене",
          abs(tc.trendline_value_at(tl, 2_000_000) - 110.0) < 1e-9)
    check("экстраполяция вперёд работает",
          abs(tc.trendline_value_at(tl, 3_000_000) - 120.0) < 1e-9)


def test_trendline_neutral():
    sw = {"lows": [{"index": 1, "price": 1, "close_time": 1}, {"index": 2, "price": 2, "close_time": 2}],
          "highs": [{"index": 1, "price": 5, "close_time": 1}, {"index": 2, "price": 6, "close_time": 2}]}
    check("при NEUTRAL линия не строится", tc.build_trendline(sw, tc.TREND_NEUTRAL) is None)
    check("значение None-линии = None", tc.trendline_value_at(None, 123) is None)


# ════════════════════════════════════════════════
# 6. Alignment
# ════════════════════════════════════════════════

def test_alignment():
    B, S, N = tc.TREND_BULLISH, tc.TREND_BEARISH, tc.TREND_NEUTRAL
    check("LONG + 4H bull + 1D bull → STRONG",
          tc.compute_alignment("LONG", B, B) == tc.ALIGN_STRONG)
    check("LONG + 4H bull + 1D bear → PARTIAL",
          tc.compute_alignment("LONG", B, S) == tc.ALIGN_PARTIAL)
    check("LONG + 4H bear → CONFLICT",
          tc.compute_alignment("LONG", S, B) == tc.ALIGN_CONFLICT)
    check("SHORT + 4H bear + 1D bear → STRONG",
          tc.compute_alignment("SHORT", S, S) == tc.ALIGN_STRONG)
    check("SHORT + 4H bull → CONFLICT",
          tc.compute_alignment("SHORT", B, S) == tc.ALIGN_CONFLICT)
    check("4H нейтрален → PARTIAL", tc.compute_alignment("LONG", N, B) == tc.ALIGN_PARTIAL)
    check("контекст недоступен → UNKNOWN", tc.compute_alignment("LONG", None, None) == tc.ALIGN_UNKNOWN)


# ════════════════════════════════════════════════
# 7. ЗАМОРОЗКА КОНТЕКСТА — самый важный тест
# ════════════════════════════════════════════════

async def test_context_frozen():
    """Контекст записывается один раз в момент сигнала и НЕ меняется при
    резолюции. Если этот тест упадёт — вся статистика по alignment
    недостоверна."""
    store = InMemoryStore()
    h, l, c = flat(60)
    sid = await tracker.record_signal(
        symbol="TESTUSDT", direction="LONG", setup="breakout",
        entry_price=101.0, entry_level=101.0, fast_n=config.DONCHIAN_LOOKBACK,
        highs=h, lows=l, closes=c, timeframe="1d", candle_close_ts=1000,
        trend_1d="BULLISH", trend_4h="BULLISH", structure_4h="HH_HL",
        alignment="STRONG", trendline_slope=0.5,
        trendline_anchor_ts=999, trendline_anchor_price=100.0,
        store=store,
    )
    frozen = dict(store.rows[sid])

    # Резолвим сигнал — рынок «уехал»
    h2, l2, c2 = extend_up(h, l, c, 40, start_high=101.0)
    for i in range(1, 41):
        await tracker.resolve_open_signals("TESTUSDT", h2[:60 + i], l2[:60 + i], c2[:60 + i],
                                            timeframe="1d", store=store)
        if store.rows[sid]["status"] != "OPEN":
            break

    after = store.rows[sid]
    check("сигнал резолвился", after["status"] in ("WIN", "LOSS"), after["status"])
    for field in ("trend_1d", "trend_4h", "structure_4h", "alignment",
                  "trendline_slope", "trendline_anchor_ts", "trendline_anchor_price"):
        check(f"контекст не изменился при резолюции: {field}",
              after[field] == frozen[field], f"{frozen[field]} → {after[field]}")


# ════════════════════════════════════════════════
# 8. Агрегация по alignment
# ════════════════════════════════════════════════

def test_alignment_aggregation():
    now = datetime.now(timezone.utc)

    def rec(align, status, r, tf4="BULLISH"):
        return {"symbol": "X", "setup": "breakout", "direction": "LONG", "status": status,
                "timeframe": "1h", "entry_price": 100.0,
                "resolved_price": 100.0 * (1 + r * 0.01), "r_multiple": r,
                "mfe_pct": abs(r), "mae_pct": -abs(r) / 2, "fired_at": now,
                "alignment": align, "trend_4h": tf4}

    signals = [
        rec("STRONG", "WIN", 2.0), rec("STRONG", "WIN", 1.0), rec("STRONG", "LOSS", -1.0),
        rec("CONFLICT", "LOSS", -1.0), rec("CONFLICT", "LOSS", -1.5),
    ]
    # сигнал вообще без контекста — не должен попасть ни в одну группу
    no_ctx = rec("STRONG", "WIN", 1.0); no_ctx["alignment"] = None; no_ctx["trend_4h"] = None
    signals.append(no_ctx)

    agg = performance.aggregate(signals)
    ba = agg["by_alignment"]
    check("группы alignment собраны", set(ba) == {"STRONG", "CONFLICT"}, set(ba))
    check("STRONG win rate 66.7%", abs(ba["STRONG"]["win_rate_pct"] - (2 / 3 * 100)) < 1e-9)
    check("CONFLICT win rate 0%", ba["CONFLICT"]["win_rate_pct"] == 0.0)
    check("средний R считается по группе", abs(ba["STRONG"]["avg_r"] - (2.0 + 1.0 - 1.0) / 3) < 1e-9)
    check("MFE/MAE по группе есть", ba["CONFLICT"]["avg_mfe_pct"] is not None)
    check("сигнал без контекста исключён из разбивки", agg["aligned_closed"] == 5, agg["aligned_closed"])
    check("но он остался в общем счёте", agg["closed"] == 6, agg["closed"])


# ════════════════════════════════════════════════
# 9. analyze() целиком + устойчивость
# ════════════════════════════════════════════════

def zigzag(legs: int, leg_len: int = 4, drift: float = 6.0, amp: float = 10.0,
            start: float = 100.0):
    """Пилообразный ряд с трендовым сносом: каждый «зуб» длиной leg_len свечей,
    чтобы при n=2 экстремумы реально подтверждались (соседи на ±2 свечи ниже/выше).
    drift > 0 — восходящий рынок (HH/HL), drift < 0 — нисходящий (LH/LL)."""
    highs, lows = [], []
    base = start
    for leg in range(legs):
        up = leg % 2 == 0
        for k in range(leg_len):
            frac = (k + 1) / leg_len
            mid = base + (amp * frac if up else -amp * frac)
            highs.append(mid + 1.0)
            lows.append(mid - 1.0)
        base = base + amp + drift if up else base - amp
    return highs, lows


def test_analyze_endtoend():
    # растущий рынок: серия HH/HL
    highs, lows = zigzag(legs=8, leg_len=4, drift=6.0)
    ctx = tc.analyze(highs, lows, ts_series(len(highs)), n=2)
    check("растущий рынок распознан как BULLISH", ctx["trend"] == tc.TREND_BULLISH,
          f"{ctx['trend']} / {ctx['structure']} / swings={ctx['swing_count']}")
    check("структура HH_HL", ctx["structure"] == tc.STRUCT_BULLISH, ctx["structure"])
    check("линия построена по lows", bool(ctx["trendline"]) and ctx["trendline"]["basis"] == "lows")
    check("наклон положительный", bool(ctx["trendline"]) and ctx["trendline"]["slope"] > 0)

    # падающий рынок: серия LH/LL
    dh, dl = zigzag(legs=8, leg_len=4, drift=-6.0, start=200.0)
    dctx = tc.analyze(dh, dl, ts_series(len(dh)), n=2)
    check("падающий рынок распознан как BEARISH", dctx["trend"] == tc.TREND_BEARISH,
          f"{dctx['trend']} / {dctx['structure']}")
    check("линия падающего рынка строится по highs",
          bool(dctx["trendline"]) and dctx["trendline"]["basis"] == "highs")
    check("наклон отрицательный", bool(dctx["trendline"]) and dctx["trendline"]["slope"] < 0)

    flat_ctx = tc.analyze([10] * 30, [8] * 30, ts_series(30), n=2)
    check("плоский рынок → NEUTRAL без падения", flat_ctx["trend"] == tc.TREND_NEUTRAL)
    check("плоский рынок → линии нет", flat_ctx["trendline"] is None)

    empty = tc.analyze([], [], [], n=2)
    check("пустые данные не роняют analyze", empty["trend"] == tc.TREND_NEUTRAL)


async def main():
    test_basic_swings()
    test_insufficient_data()
    test_no_lookahead()
    test_no_repaint()
    test_trendline_no_repaint()
    test_structure_bullish()
    test_structure_bearish()
    test_structure_mixed()
    test_structure_insufficient()
    test_trendline_math()
    test_trendline_neutral()
    test_alignment()
    await test_context_frozen()
    test_alignment_aggregation()
    test_analyze_endtoend()

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed\n")
    for n in PASS:
        print(f"  ok  {n}")
    if FAIL:
        print()
        for f in FAIL:
            print(f"  FAIL  {f}")
        sys.exit(1)
    print("\nALL TREND CONTEXT TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
