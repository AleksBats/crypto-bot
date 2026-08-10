"""
test_trade_state.py — синтетические тесты Telegram output layer.

Проверяется главное, ради чего слой писался:
  1. Формулы совпадают с локальным ботом пользователя (стоп, MMO, 1R, позиция).
  2. Гейт «одна сделка на символ» реально блокирует повторные сигналы.
  3. Старые raw-detector сообщения и блок КОНТЕКСТА больше не отправляются.
  4. Математика индикаторов не тронута (побайтовое сравнение с прежней версией).

Запуск: python3 test_trade_state.py
"""

import ast
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import trade_state as tsx

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name if cond else f"{name} — {detail}")


def close_to(a, b, tol=1e-9):
    return a is not None and abs(a - b) <= tol * max(1.0, abs(b))


# ════════════════════════════════════════════════
# 1. Уровни сделки
# ════════════════════════════════════════════════

def test_levels_long():
    lv = tsx.build_levels("LONG", entry=77.02, level=76.88,
                          channel_high=76.88, channel_low=75.87,
                          stop_buffer_pct=0.003, default_stop_pct=0.02)
    check("LONG: сделка построена", lv is not None)
    check("LONG: стоп = level x (1-0.003)", close_to(lv["stop"], 76.88 * 0.997), lv["stop"])
    check("LONG: стоп ниже входа", lv["stop"] < lv["entry"])
    check("LONG: MMO = level + высота канала",
          close_to(lv["target_mmo"], 76.88 + (76.88 - 75.87)), lv["target_mmo"])
    check("LONG: 1R = entry + (entry - stop)",
          close_to(lv["target_1r"], 77.02 + (77.02 - 76.88 * 0.997)), lv["target_1r"])
    check("LONG: risk_pct = |entry-stop|/entry",
          close_to(lv["risk_pct"], abs(77.02 - 76.88 * 0.997) / 77.02 * 100))
    check("LONG: помечен структурным", lv["structural"] is True)


def test_levels_short():
    lv = tsx.build_levels("SHORT", entry=75.50, level=75.90,
                          channel_high=77.00, channel_low=75.90,
                          stop_buffer_pct=0.003, default_stop_pct=0.02)
    check("SHORT: сделка построена", lv is not None)
    check("SHORT: стоп = level x (1+0.003)", close_to(lv["stop"], 75.90 * 1.003))
    check("SHORT: стоп выше входа", lv["stop"] > lv["entry"])
    check("SHORT: MMO = level - высота канала",
          close_to(lv["target_mmo"], 75.90 - (77.00 - 75.90)))
    check("SHORT: 1R ниже входа", lv["target_1r"] < lv["entry"])


def test_levels_turtle_flat_stop():
    """Turtle Zone структурного уровня не даёт — плоский стоп, без MMO."""
    lv = tsx.build_levels("LONG", entry=100.0, level=None,
                          channel_high=None, channel_low=None,
                          stop_buffer_pct=0.003, default_stop_pct=0.02)
    check("Turtle: стоп = вход x (1-0.02)", close_to(lv["stop"], 98.0), lv["stop"])
    check("Turtle: MMO отсутствует", lv["target_mmo"] is None)
    check("Turtle: 1R есть", close_to(lv["target_1r"], 102.0))
    check("Turtle: не структурный", lv["structural"] is False)


def test_levels_rejected():
    """Бессмысленные сделки не отправляются вовсе."""
    bad = tsx.build_levels("LONG", entry=100.0, level=105.0,
                           channel_high=110.0, channel_low=100.0,
                           stop_buffer_pct=0.003, default_stop_pct=0.02)
    check("LONG со стопом ВЫШЕ входа отбракован", bad is None, bad)
    check("нулевой вход отбракован",
          tsx.build_levels("LONG", 0, 1, 2, 1, 0.003, 0.02) is None)


def test_mmo_dropped_when_useless():
    """MMO по ту же сторону от входа бесполезен — выбрасывается."""
    lv = tsx.build_levels("LONG", entry=200.0, level=100.0,
                          channel_high=100.0, channel_low=99.0,
                          stop_buffer_pct=0.003, default_stop_pct=0.02)
    check("MMO ниже входа выброшен", lv["target_mmo"] is None, lv["target_mmo"])


# ════════════════════════════════════════════════
# 2. Размер позиции
# ════════════════════════════════════════════════

def test_position_basic():
    """Из комментария в config локального бота: $1000, 2%, стоп 2% => $1000 номинал, $200 маржа."""
    pos = tsx.position_size(entry=100.0, stop=98.0, account_size=1000, risk_pct=0.02, leverage=5)
    check("позиция: номинал $1000", close_to(pos["notional"], 1000.0), pos["notional"])
    check("позиция: маржа $200 при 5x", close_to(pos["margin"], 200.0), pos["margin"])
    check("позиция: риск ровно $20", close_to(pos["risk_usd"], 20.0), pos["risk_usd"])
    check("позиция: не урезана", pos["capped"] is False)
    check("позиция: объём = номинал/вход", close_to(pos["qty"], 10.0))


def test_position_capped():
    """Узкий стоп упирается в потолок плеча — риск становится МЕНЬШЕ заданного."""
    pos = tsx.position_size(entry=100.0, stop=99.9, account_size=1000, risk_pct=0.02, leverage=5)
    check("узкий стоп: номинал ограничен $5000", close_to(pos["notional"], 5000.0))
    check("узкий стоп: помечено capped", pos["capped"] is True)
    check("узкий стоп: реальный риск < $20", pos["risk_usd"] < 20.0, pos["risk_usd"])


def test_position_guards():
    check("позиция: нулевой стоп -> None", tsx.position_size(100, 100, 1000, 0.02, 5) is None)
    check("позиция: нулевой вход -> None", tsx.position_size(0, 1, 1000, 0.02, 5) is None)


# ════════════════════════════════════════════════
# 3. Цель, закрывающая сделку
# ════════════════════════════════════════════════

def test_primary_target_is_nearest():
    lv = {"entry": 100.0, "target_mmo": 130.0, "target_1r": 105.0}
    check("закрывает БЛИЖАЙШАЯ цель", close_to(tsx.primary_target(lv), 105.0))
    lv2 = {"entry": 100.0, "target_mmo": None, "target_1r": 105.0}
    check("если MMO нет — берётся 1R", close_to(tsx.primary_target(lv2), 105.0))
    check("если целей нет — None", tsx.primary_target({"entry": 1.0}) is None)


# ════════════════════════════════════════════════
# 4. Закрытие сделки
# ════════════════════════════════════════════════

def test_check_close():
    ts_list = [1000, 2000, 3000, 4000]
    highs = [101, 102, 108, 103]
    lows = [99, 98, 100, 90]

    hit = tsx.check_close("LONG", stop=95.0, target=107.0,
                          highs=highs, lows=lows, close_times=ts_list, since_ts=1000)
    check("TARGET HIT найден на нужной свече",
          hit and hit["reason"] == "TARGET_HIT" and hit["ts"] == 3000, hit)

    # lows = [99, 98, 100, 90]: стоп 97 впервые задет только последней свечой
    hit2 = tsx.check_close("LONG", stop=97.0, target=200.0,
                           highs=highs, lows=lows, close_times=ts_list, since_ts=1000)
    check("STOP HIT найден на первой задевшей свече",
          hit2 and hit2["reason"] == "STOP_HIT" and hit2["ts"] == 4000, hit2)

    # а стоп 98.5 срабатывает раньше — на свече 2000
    hit3 = tsx.check_close("LONG", stop=98.5, target=200.0,
                           highs=highs, lows=lows, close_times=ts_list, since_ts=1000)
    check("более близкий стоп срабатывает раньше",
          hit3 and hit3["ts"] == 2000, hit3)

    none = tsx.check_close("LONG", stop=1.0, target=999.0,
                           highs=highs, lows=lows, close_times=ts_list, since_ts=1000)
    check("ничего не задето -> None", none is None)


def test_stop_wins_within_same_candle():
    """В одной свече задеты и стоп, и цель — считаем убыток. Порядок внутри
    бара неизвестен, занижать убыток опаснее, чем занижать прибыль."""
    hit = tsx.check_close("LONG", stop=95.0, target=105.0,
                          highs=[106], lows=[94], close_times=[2000], since_ts=1000)
    check("стоп имеет приоритет внутри свечи", hit["reason"] == "STOP_HIT", hit)


def test_candles_before_signal_ignored():
    """Свечи ДО сигнала не могут его закрыть."""
    hit = tsx.check_close("LONG", stop=95.0, target=105.0,
                          highs=[106, 101], lows=[94, 99],
                          close_times=[1000, 2000], since_ts=1000)
    check("свеча до сигнала проигнорирована", hit is None, hit)


def test_short_close():
    hit = tsx.check_close("SHORT", stop=105.0, target=95.0,
                          highs=[101], lows=[94], close_times=[2000], since_ts=1000)
    check("SHORT: цель снизу засчитана", hit and hit["reason"] == "TARGET_HIT", hit)
    hit2 = tsx.check_close("SHORT", stop=105.0, target=90.0,
                           highs=[106], lows=[99], close_times=[2000], since_ts=1000)
    check("SHORT: стоп сверху засчитан", hit2 and hit2["reason"] == "STOP_HIT", hit2)


def test_cooldown():
    check("пауза 1H = +3600000мс", tsx.cooldown_until(1000, "1h") == 1000 + 3_600_000)
    check("пауза 1D = +86400000мс", tsx.cooldown_until(1000, "1d") == 1000 + 86_400_000)


def test_setup_label():
    check("подпись по приоритету",
          tsx.setup_label(["turtle_zone", "breakout"]) == "Breakout + Turtle Zone",
          tsx.setup_label(["turtle_zone", "breakout"]))
    check("одиночный сетап", tsx.setup_label(["failure_test"]) == "Failure Test")


# ════════════════════════════════════════════════
# 5. Реестр открытых сделок — сам гейт
# ════════════════════════════════════════════════

def test_registry_gate():
    reg = tsx.OpenTradeRegistry(max_active=3)
    check("пустой реестр: можно открывать", reg.blocked_reason("SOLUSDT", now_ms=0) is None)

    reg.mark_open({"symbol": "SOLUSDT", "timeframe": "1h", "direction": "LONG",
                   "entry": 100.0, "stop": 98.0, "target_primary": 105.0,
                   "candle_close_ts": 1000})
    check("после отправки символ занят", reg.is_open("SOLUSDT"))
    check("повторный сигнал заблокирован",
          reg.blocked_reason("SOLUSDT", now_ms=0) == "already_open")
    check("другой символ не задет", reg.blocked_reason("ETHUSDT", now_ms=0) is None)


def test_registry_blocks_other_timeframe():
    """Ключ — только символ: 1D не должен пролезть, пока открыт 1H."""
    reg = tsx.OpenTradeRegistry(max_active=3)
    reg.mark_open({"symbol": "SOLUSDT", "timeframe": "1h", "entry": 1, "stop": 0.9,
                   "direction": "LONG", "candle_close_ts": 0})
    check("1D заблокирован открытым 1H",
          reg.blocked_reason("SOLUSDT", now_ms=0) == "already_open")


def test_registry_max_active():
    """Глобальный лимит: 3 сделки суммарно по ВСЕМ символам."""
    reg = tsx.OpenTradeRegistry(max_active=3)
    for sym in ("A", "B", "C"):
        reg.mark_open({"symbol": sym, "timeframe": "1h", "entry": 1, "stop": 0.9,
                       "direction": "LONG", "candle_close_ts": 0})
    check("при лимите 3 четвёртый символ заблокирован",
          reg.blocked_reason("D", now_ms=0) == "max_active_trades")
    check("счётчик открытых = 3", reg.open_count() == 3)
    check("причина маппится в SKIPPED_CAPACITY",
          tsx.SKIP_STATUS["max_active_trades"] == "SKIPPED_CAPACITY")


def test_slot_frees_only_on_close():
    """Слот освобождается ТОЛЬКО по TARGET/STOP HIT — не по времени и не
    потому, что появился «более удачный» кандидат."""
    reg = tsx.OpenTradeRegistry(max_active=3)
    for sym in ("A", "B", "C"):
        reg.mark_open({"symbol": sym, "timeframe": "1h", "entry": 1, "stop": 0.9,
                       "direction": "LONG", "candle_close_ts": 0})
    check("через сутки лимит всё ещё держит",
          reg.blocked_reason("D", now_ms=86_400_000) == "max_active_trades")

    reg.mark_closed("A", closed_ts=0, timeframe="1h")
    check("после закрытия A освободился ровно один слот", reg.open_count() == 2)
    check("новый символ D теперь проходит", reg.blocked_reason("D", now_ms=0) is None)
    check("B и C остались открытыми", reg.is_open("B") and reg.is_open("C"))
    check("A под паузой, не переоткрывается сразу",
          reg.blocked_reason("A", now_ms=0) == "cooldown")


def test_existing_trades_never_evicted():
    """Новый сигнал не имеет права вытеснить уже открытую сделку."""
    reg = tsx.OpenTradeRegistry(max_active=3)
    for sym in ("A", "B", "C"):
        reg.mark_open({"symbol": sym, "timeframe": "1h", "entry": 1, "stop": 0.9,
                       "direction": "LONG", "candle_close_ts": 0})
    before = set(reg.open_symbols())
    reg.blocked_reason("D", now_ms=0)      # попытка нового сигнала
    check("состав открытых сделок не изменился", set(reg.open_symbols()) == before)
    check("D не открылся", not reg.is_open("D"))


def test_skip_statuses_complete():
    for reason in ("max_active_trades", "already_open", "cooldown"):
        check(f"причина {reason} имеет статус для Neon",
              reason in tsx.SKIP_STATUS and tsx.SKIP_STATUS[reason].startswith("SKIPPED"))
    check("статус лимита называется ровно SKIPPED_CAPACITY",
          tsx.SKIP_STATUS["max_active_trades"] == "SKIPPED_CAPACITY")


def test_registry_cooldown_after_close():
    reg = tsx.OpenTradeRegistry(max_active=3)
    reg.mark_open({"symbol": "SOLUSDT", "timeframe": "1h", "entry": 1, "stop": 0.9,
                   "direction": "LONG", "candle_close_ts": 0})
    reg.mark_closed("SOLUSDT", closed_ts=1_000_000, timeframe="1h")
    check("после закрытия символ свободен от OPEN", not reg.is_open("SOLUSDT"))
    check("но действует пауза в одну свечу",
          reg.blocked_reason("SOLUSDT", now_ms=1_000_000) == "cooldown")
    check("после паузы снова можно",
          reg.blocked_reason("SOLUSDT", now_ms=1_000_000 + 3_600_001) is None)


def test_registry_restore():
    reg = tsx.OpenTradeRegistry(max_active=3)
    reg.restore([{"symbol": "BTCUSDT", "timeframe": "1d", "direction": "LONG",
                  "entry": 1.0, "stop": 0.9, "target_primary": 1.2,
                  "candle_close_ts": 5}])
    check("восстановление из Neon вернуло OPEN", reg.is_open("BTCUSDT"))
    check("после рестарта повторный сигнал заблокирован",
          reg.blocked_reason("BTCUSDT", now_ms=10**13) == "already_open")
    check("флаг restored_from_db выставлен", reg.restored_from_db is True)


# ════════════════════════════════════════════════
# 6. Гарантии по production-коду
# ════════════════════════════════════════════════

OLD_SENDERS = ["fmt_breakout_alert", "fmt_turtle_zone_alert",
               "fmt_failure_test_alert", "fmt_trend_context", "fmt_combo_alert"]


def test_no_raw_detector_messages():
    """Ни один старый форматтер не должен вызываться из run_live.py."""
    tree = ast.parse(pathlib.Path("run_live.py").read_text())
    calls = [ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)]
    for name in OLD_SENDERS:
        check(f"run_live не вызывает {name}()",
              not any(name in c for c in calls),
              [c for c in calls if name in c])
    check("run_live вызывает fmt_trade_signal()",
          any("fmt_trade_signal" in c for c in calls))


def test_single_send_path_for_technical():
    """Технический сигнал уходит ровно из одной функции."""
    tree = ast.parse(pathlib.Path("run_live.py").read_text())
    senders = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call) and "fmt_trade_signal" in ast.unparse(sub.func):
                    senders.append(node.name)
    check("единственная точка отправки — _send_trade_signal",
          senders == ["_send_trade_signal"], senders)


def test_indicators_untouched():
    """Математика детекторов и 4H-контекста не изменена — сравнение с 99fbad5."""
    for f in ("technical_signals.py", "trend_context.py",
              "signal_stats/signal_tracker.py", "signal_stats/performance.py"):
        r = subprocess.run(["git", "diff", "--quiet", "99fbad5", "--", f])
        check(f"{f} не изменён", r.returncode == 0)


def test_no_destructive_sql():
    sql = pathlib.Path("signal_stats/signal_store.py").read_text().upper()
    for bad in ("DROP TABLE", "DROP COLUMN", "TRUNCATE", "DELETE FROM", "ALTER COLUMN"):
        check(f"нет {bad} в signal_store", bad not in sql)


def test_skipped_candidates_are_persisted():
    """Пропущенный кандидат обязан попадать в Neon, а не исчезать."""
    src = pathlib.Path("run_live.py").read_text()
    check("run_live сохраняет пропущенных кандидатов",
          "insert_trade_candidate" in src and "SKIP_STATUS" in src)
    tree = ast.parse(src)
    # уровни должны считаться ДО проверки гейта, иначе сохранять нечего
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "_send_trade_signal")
    body = ast.unparse(fn)
    check("уровни считаются до проверки гейта",
          body.index("build_levels") < body.index("blocked_reason"))


def test_kill_switch_present():
    src = pathlib.Path("run_live.py").read_text()
    check("есть аварийный откат TRADE_SIGNALS_ONLY", "TRADE_SIGNALS_ONLY" in src)


# ════════════════════════════════════════════════

def main():
    for fn in list(globals().values()):
        if callable(fn) and getattr(fn, "__name__", "").startswith("test_"):
            fn()
    print(f"\n{'=' * 60}")
    print(f"  ПРОЙДЕНО: {len(PASS)}   ПРОВАЛЕНО: {len(FAIL)}")
    print(f"{'=' * 60}")
    for f in FAIL:
        print("  ✗", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
