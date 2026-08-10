# DECISIONS.md

Non-obvious technical decisions and the bugs/investigations that motivated them. Read this before "fixing" something that looks wrong — it may already be a deliberate workaround.

---

### 1. Technical indicators are standard Donchian-channel logic, not TradingView ports

**Context:** the user's original TradingView alerts (Breakout, Turtle Zone Filter, Failure Test) are custom Pine Script indicators. The source code was never available to port exactly.

**Decision:** with the user's explicit approval ("Стандартные версии (Recommended)" when asked), implemented standard/generic versions of each concept:
- Breakout = classic N-bar Donchian channel break.
- Turtle Zone Filter = classic dual-channel Turtle Trading System (fast=20, slow=55).
- Failure Test = false-breakout/trap detector.

**Consequence:** exact signal timing/levels will differ from what TradingView draws. This is expected and was agreed to up front, not a bug.

---

### 2. Failure Test reference channel must exclude the lookback window

**Bug found via synthetic testing, not casual review.** The initial implementation computed the reference Donchian channel from the full trailing N bars, which included the very breakout bars being tested. Since a Donchian channel updates on every new bar, this meant the channel silently absorbed the breakout into itself, and the failure-test condition could never fire for realistic scenarios.

**Fix:** `detect_failure_test()` in `technical_signals.py` slices the reference channel from bars *before* the lookback window (`highs[:-lookback][-n:]`), so the channel reflects what it looked like before the tested breakout happened.

**Verification:** confirmed via three synthetic bash test cases — SHORT failure test, LONG failure test, and a genuine sustained breakout (which must correctly return `None`, not a false failure signal).

---

### 3. Render region must be Frankfurt (or other non-US) — never Oregon/US

**Bug found in production, via deploy logs, not assumption.** The bot was first deployed to Render's default free-tier region (Oregon, US). Every single Binance API call failed with `HTTP 451 Client Error` — Binance.com blocks requests from US-region IPs (Binance.US is a separate, incompatible product).

**Fix:** created a new Render service (`crypto-bot-eu`) in the Frankfurt region with identical code/env vars, and deleted the non-functional Oregon service (`crypto-bot`) once Frankfurt was confirmed working.

**Consequence:** if this bot is ever redeployed, torn down and recreated, or migrated to a different host, the new environment's egress IP **must not be US-based**, or every Binance call will fail with 451.

---

### 4. Render free tier requires an HTTP-responding "Web Service"

**Context:** Render's free tier only supports the "Web Service" instance type (not background workers). Web Services must bind to `$PORT` and respond to HTTP or Render considers the deploy unhealthy.

**Fix:** `run_live.py` starts a minimal stdlib `ThreadingHTTPServer` (`_start_health_server()`) in a daemon thread alongside the main asyncio loop. It only ever returns `{"status": "alive", ...}` — it has no effect on signal logic and is purely to satisfy Render's health check.

---

### 5. Render free tier still sleeps after 15 minutes — health check alone isn't enough

**Bug found via Telegram "Stopped: signal SIGTERM" messages with no corresponding deploy event.** Even with the health-check server running, Render's free tier spins the whole service down after 15 minutes of no *inbound* HTTP traffic. The bot's own outbound calls to Binance/Telegram do not count as "activity" from Render's perspective — only external requests hitting the service's public URL do.

**Fix:** set up a free UptimeRobot HTTP monitor pinging `https://crypto-bot-eu-bulo.onrender.com` every 5 minutes (under the 15-minute threshold), keeping the service continuously alive.

**Consequence:** if the UptimeRobot monitor is ever deleted, paused, or the account lapses, the bot will start sleeping and missing signals in ~15-minute windows again. This is not a code bug if it recurs — check UptimeRobot first.

---

### 6. Two Render services briefly existed simultaneously — Oregon was deleted

**Context:** while diagnosing the region issue (#3), a second service (`crypto-bot-eu`, Frankfurt) was created without immediately deleting the original (`crypto-bot`, Oregon). Both were connected to the same GitHub repo/branch, so every push auto-deployed both, and both sent "Live Mode Started"/"Stopped" messages to the same Telegram chat — producing duplicate, confusing notifications.

**Fix:** deleted the Oregon service entirely once Frankfurt was confirmed working. Only `crypto-bot-eu` should exist going forward.

---

### 7. Logging format bug: `%,.0f` is not valid Python `%`-style syntax

**Bug found in Render deploy logs** (`ValueError: unsupported format character ',' (0x2c)`), pre-existing in the original `run_live.py` before any of this session's changes. Python's `%`-style string formatting (used by the `logging` module for lazy evaluation) does not support the comma thousands-separator flag — that's only valid in `str.format()` / f-strings.

**Impact:** non-fatal. Python's logging module catches formatting exceptions internally (`Handler.handleError`) and just prints `--- Logging error ---` to the console; it does not crash the bot or skip the current loop iteration. Still worth fixing to keep logs readable.

**Fix:** pre-format the value with an f-string (`f"{value:,.0f}"`) and pass it as `%s` instead of using `%,.0f` directly.

---

### 8. `requirements.txt` and `.env.example` in this folder were rewritten, not copied verbatim

**Context:** the versions of these two files that exist in the GitHub repo (`AleksBats/crypto-bot`) were written for the *other*, unrelated `crypto_bot.py` (the 6-hourly Claude-AI BTC analysis bot) — they list `anthropic`, `requests`, `schedule` and reference `CLAUDE_API_KEY`, `CRYPTOPANIC_TOKEN`, `ANALYSIS_INTERVAL_HOURS`, none of which `run_live.py` uses. Notably, they don't even list `httpx`, which `run_live.py` and `telegram_bot.py` require directly — the deploy only worked because `httpx` is a transitive dependency of the (unused, in this pipeline) `anthropic` package.

**Fix:** this folder's `requirements.txt` (`httpx`, `python-dotenv`) and `.env.example` were rewritten from scratch to accurately reflect what `run_live.py`'s actual import graph and `config.py`'s actual `os.environ` reads require. This is the one deliberate deviation from "copy the repo verbatim" in this consolidation — flagged here so it isn't mistaken for scope creep.

**Not yet done:** the GitHub repo's own `requirements.txt` has not been corrected to match (still references the old bot's dependencies). Consider fixing it there too — see TODO.md.

---

### 9. Railway.app was abandoned for Render.com

**Context:** Railway was the first deployment target considered. Its free trial expired mid-project and the user explicitly declined to pay ("я нечего не хочу платить уже достаточно платил толку нет").

**Fix:** migrated entirely to Render.com's free tier (no credit card required, 750 free instance-hours/month). `railway.toml` still exists in the GitHub repo's history but is unused and not included in this consolidated folder.

---

### 10. `CAPUSDT` does not exist on Binance Spot

**Found via production logs:** every `klines` request for `CAPUSDT` returns `400 Bad Request`. The user was informed and initially chose to keep it in `TECHNICAL_SYMBOLS` rather than remove or substitute it. The scan handled it gracefully — logged a warning and continued to the next symbol, never crashing or blocking the other coins.

**РЕЗОЛЮЦИЯ (2026-08-09):** удалён из списка по явному указанию пользователя. Поводом стало то, что после добавления часового контура (#13) он начал возвращать 400 **дважды за цикл** вместо одного раза — на 1D и на 1H. Ценности он не нёс никогда, а шума в логах стало вдвое больше. Не возвращать без явного запроса.

---

### 11. WIN/LOSS accuracy tracking — the evaluation rule, and why no arbitrary % threshold was used

**Context:** the user asked for a statistics system to objectively measure whether Breakout / Turtle Zone Filter / Failure Test signals correctly predicted market direction. Before building it, the codebase was audited for an existing entry/stop/target/invalidation definition — **none existed**. `technical_signals.py`'s detectors only ever returned `direction`, `price` (at signal time), and `level` (the channel boundary broken). No stop, no target, no expected-move %, and no persisted signal history at all (`state.py` only tracks alert cooldowns, not signal outcomes).

**Decision (user-directed, chosen from explicit options, not assumed):** rather than inventing a new percentage move as the win/loss cutoff, `signal_tracker.py` reuses Donchian levels the bot already computes:

- **INVALIDATION band** — the *fast* channel's opposite side (`n=DONCHIAN_LOOKBACK` for Breakout, `fast=TURTLE_FAST_LOOKBACK` for Turtle Zone), recomputed from each new daily bar — i.e. a genuine trailing stop, the same exit rule the real Turtle Trading System uses. For Failure Test, invalidation is the exact level that defined the trap (frozen at signal time) being re-crossed in the wrong direction.
- **CONFIRMATION band** — the *slow* channel's same-direction side (`slow=TURTLE_SLOW_LOOKBACK`, currently 55), applied uniformly to all three signal types as the "did this actually trend" filter.
- **WIN** = a later daily close reaches the confirmation band in the predicted direction before a close breaches the invalidation band.
- **LOSS** = the invalidation band is breached first.
- **PENDING** = neither has happened yet. **No timeout** — explicit user decision, to avoid an arbitrary cutoff. Pending signals are excluded from the win-rate percentage until they resolve.

**Dedup:** the detectors fire on every poll while a crossing condition holds true (daily data is cached for `POLL_TECHNICAL_SECS`, but the bot polls every `POLL_INTERVAL_SECS`), so `signal_tracker.record()` will not create a second pending record for the same `(symbol, signal_type, direction, entry_level)` while one is already pending — otherwise the same real-world event would be logged many times and skew the stats.

**Known limitation — persistence:** `signal_tracker.py` writes to `signals_log.json` next to the other source files. Render's free tier has an **ephemeral filesystem** — this file is wiped on every redeploy (and possibly on a cold restart). Until a persistent disk (paid) or an external store (e.g. a free-tier Postgres like Neon/Supabase, or periodic export to a GitHub Gist) is wired in, accuracy stats will reset whenever the service redeploys. Flagged, not silently ignored — see TODO.md.

**What this deliberately does NOT do:** it does not claim a specific dollar/percent return per signal, since no position sizing or stop-loss was ever defined by the user's original TradingView setup. It only answers the narrower, objectively answerable question the user asked for: did price move far enough in the predicted direction to clear the same channel structure that generated the signal, before invalidating it.

---

### 12. Signal performance tracking / paper trading (Phase 2) — supersedes #11's prototype

**Context:** the first stats prototype (#11 above, top-level `signal_tracker.py` + `signals_log.json`) was a proof of concept, never merged into the live repo. The user then asked for a full paper-trading statistics layer — persistent, with RSI/MFE/MAE/R multiples, and `/stats /week /today /month` Telegram commands — while explicitly forbidding any change to existing indicators, thresholds, symbol lists, or alert behavior. This entry documents the decisions made building it. **The Phase 1 prototype never reached this repository** — it lived only in the working folder and was never pushed, so there is no deprecated `signal_tracker.py` or `signals_log.json` here. The `signal_stats/` package is the only implementation in this repo.

**Where signals originate / where the hook lives:** traced live — the only directional (LONG/SHORT) signals come from `technical_signals.py`'s three detectors, called from `run_live.py`'s `evaluate_signals()` (ASTERUSDT) and `scan_technical_symbols()` (12-coin scan), submitted to `alert_engine.engine.submit()`. **User-directed decision:** record a signal only when it was *actually sent to Telegram* (i.e. `submit()` returned `True`, meaning it wasn't swallowed by the 30-minute cooldown) — this is genuine paper trading: what a subscriber would have actually seen and could have acted on, not just what the raw detector produced. This required one additive change to `alert_engine.py`: `submit()` now returns `bool`. The cooldown/combo decision logic itself is untouched — the return value only *exposes* the existing outcome.

**Combo setups:** when Breakout and Turtle Zone both fire for the same symbol, same direction, same day, and *both* alerts were actually sent, they're recorded as one `breakout_turtle_combo` row instead of two separate rows (`signal_stats/signal_tracker.decide_breakout_turtle_setup`). This is a real, detectable co-occurrence — not an invented category. Failure Test is never merged into a combo; it's conceptually independent (a reversal call, not a trend-following one).

**WIN/LOSS/OPEN rule — unchanged in substance from #11, re-verified with the user:** rolling fast-Donchian opposite band = invalidation (trailing stop, recomputed from live candles each check); rolling slow-Donchian (55-day) same-direction band = confirmation. No timeout — `OPEN` signals stay open indefinitely, exactly as decided in #11. `EXPIRED` exists in the DB's status vocabulary for schema flexibility but this module deliberately never produces it.

**R-multiple — a new wrinkle vs. #11:** R needs a *fixed* risk denominator, but the invalidation band is a *rolling* trailing stop by design (see #11). Resolved by freezing the invalidation band's distance from entry at the moment of signal (`initial_risk_pct`, stored once, never recomputed) purely for R-multiple math, while the *live* rolling band is still what actually triggers WIN/LOSS. This mirrors how real trading journals compute R — against initial risk, not a moving stop.

**RSI:** added as a new, standard Wilder RSI(14) calculation (`signal_stats/signal_tracker.compute_rsi`) purely for record-keeping context on each signal. It does not feed into any detector and does not change signal generation — user-approved explicitly as an additive metric.

**Volume for the 12 technical-only symbols:** deliberately **not** added (user's explicit choice) — `fetch_daily()` only pulls highs/lows/closes for those symbols, no volume. Recorded as N/A wherever it would otherwise appear; no separate field invented.

**Timeframe:** every signal's `timeframe` is `"1d"` — the bot only trades daily candles. Reports render a `"⏱ Таймфреймы: N/A — бот сейчас торгует только 1D"` line rather than a fabricated leaderboard (an earlier user-supplied report mockup assumed 4H/1H/15m existed; corrected in the approved final preview).

**Persistence — Neon Postgres, chosen over Render's own free tier or a bare file:** confirmed via live research (not assumption) that Render's free tier has **no persistent disk at all**, and Render's own free Postgres **expires after 30 days**. Neon's free Postgres plan doesn't expire and survives Render redeploys independently, since it's a separate host. `signal_stats/signal_store.py` owns all SQL; every other statistics module works with plain dicts and never imports `asyncpg` directly. `config.DATABASE_URL` is deliberately **optional**, not `_require()`'d — if unset, `signal_store.get_pool()` returns `None` and every store function degrades to a safe no-op, so the *entire bot* (alerts, indicators, everything) keeps working even with zero statistics configuration. Never make the core trading loop depend on this being set.

**The package is `signal_stats/`, NOT `statistics/` — renamed before deploy to avoid shadowing the stdlib.** It was originally built as `statistics/` (the name the spec suggested). That collides with Python's standard-library `statistics` module, and because `run_live.py` does `sys.path.insert(0, ...)`, the local package would win for the entire process. This was not theoretical: the repo's older `price.py` and `volume.py` monitor stubs both do `import statistics` and call `statistics.mean()`, and this was reproduced — `statistics.mean` raised `AttributeError: module 'statistics' has no attribute 'mean'`. The live bot was unaffected (`run_live.py` never imports those stubs, and the full production import graph — httpx, dotenv, asyncpg — was verified to load fine), but it was a live landmine for anyone re-wiring the stubs or adding a dependency that uses stdlib `statistics` internally. The user approved the rename, so the package ships as `signal_stats/`. **Do not rename it back**, and don't add modules here whose names collide with stdlib modules.

**Telegram commands are new capability, not a modification:** the bot never listened for incoming messages before — only sent them. `signal_stats/telegram_commands.py` runs as its own background `asyncio` task (long-polling `getUpdates`), started alongside the health server in `run_live.py`'s `main()`. It has its own httpx client, its own error handling, and only responds to `config.STATS_ALLOWED_CHAT_ID`. A crash or hang in this task cannot block or break signal detection or alert sending — they're independent tasks on the same event loop.

**No automatic weekly report yet — by explicit instruction:** `signal_stats/reports.build_week_report()` is fully implemented and tested, but nothing calls it on a schedule. The user asked to implement and test the reporting function first and defer wiring up automatic delivery. `/week` on demand works today; a scheduled push is a follow-up, not done here.

**Testing without a live Postgres:** this sandbox has no path to provision real Postgres (no root, `apt-get download` blocked by the network egress allowlist — `403` from the proxy — and no embeddable-Postgres Python package available). `signal_store.py`'s SQL was written and hand-reviewed for correctness (idempotent `CREATE TABLE/INDEX IF NOT EXISTS`, parameterized queries) but never executed against a live database. All business logic (`signal_tracker.py`, `performance.py`, `reports.py` — dedup, WIN/LOSS/OPEN resolution, no-look-ahead, weekly/monthly date filtering, restart/persistence semantics) was verified with a real in-memory store that implements the exact same async interface as `signal_store.py` (see `test_statistics.py`) — 44 checks, all passing. **Do a manual smoke test against the real Neon instance once `DATABASE_URL` is set on Render** — see TODO.md.

---

### 13. Failure Test показывал устаревшую цену и слал дубли — 1H контур, закрытые свечи, две цены

**Найдено пользователем на живом рынке, подтверждено логами и кодом.** В Telegram по XRPUSDT приходило `Текущая цена: 1.046500`, тогда как на TradingView рынок был около 1.0481. Причём одно и то же сообщение с той же ценой пришло трижды: 16:50, 18:05, 18:40.

**Причина №1 — кэш.** `detect_failure_test()` возвращал `price = closes[-1]`, а `closes` приходил из `fetch_daily()`, который кэшировал ответ Binance на `POLL_TECHNICAL_SECS` (900 с). Основной цикл идёт каждые 300 с, то есть два цикла из трёх работали на данных возрастом до 15 минут. Перед отправкой в Telegram свежая цена не запрашивалась вообще. Расхождение 0.15% для XRP за 15 минут — нормальная рыночная динамика, а не сбой математики.

**Причина №2 — не было дедупликации.** `alert_engine._is_on_cooldown()` — это rate limiting («не чаще раза в 30 минут»), а не «одно событие — одно сообщение». Условие Failure Test считается на дневных свечах с окном `lookback=5` и остаётся истинным сутками; каждые 30 минут по истечении cooldown то же самое состояние уходило заново. Интервалы 16:50 → 18:05 → 18:40 это ровно подтверждают. Дедуп в слое статистики (`find_open_duplicate`) существовал, но гейтил только запись в БД, не отправку.

**Причина №3 — незакрытая свеча.** Binance возвращает последним элементом текущую формирующуюся свечу, и она попадала в детекторы. Сигнал мог объявиться в середине дня и затем «передумать».

**Исправления (математика детекторов в `technical_signals.py` не тронута, файл байт в байт):**

- `fetch_klines()` заменил `fetch_daily()`: универсален по таймфрейму, возвращает `close_times` и **отбрасывает незакрытые свечи по фактическому `close_time`**, а не «срезает последнюю вслепую». По решению пользователя правило применено ко **всем трём** индикаторам, а не только к Failure Test — иначе поведение разъезжается.
- `fetch_current_price()` — отдельный лёгкий запрос `ticker/price` непосредственно перед отправкой, и **только** после того, как дедуп пропустил сигнал (иначе летели бы лишние запросы каждую итерацию). При ошибке возвращает `None`, и в сообщении честно пишется «н/д» вместо тихой подстановки старой цены.
- Форматтеры показывают **две отдельные цены**: `Цена сигнала` (close закрытой свечи, по которой считались уровни — неизменна) и `Текущая цена` (рынок на момент отправки) с процентом расхождения.
- `_is_new_candle_signal()` — дедуп по ключу `(symbol, timeframe, setup, direction)` со значением `close_time` свечи. Одна свеча — максимум одно сообщение. **Компромисс:** состояние в памяти процесса, после рестарта Render возможна одна повторная отправка на свечу. Сделано осознанно: иначе дедуп зависел бы от наличия БД, а бот обязан работать и без `DATABASE_URL`.

**Часовой контур (1H) параллельно дневному.** Пользователь исходно считал, что Failure Test работает на 1H — на самом деле бот использовал только `interval="1d"`. По его решению добавлен независимый часовой контур: свой кэш (`POLL_HOURLY_SECS=300`, короче дневного, потому что часовая свеча закрывается каждый час), свой лимит свечей, сигналы помечены таймфреймом. Побочный эффект: поле `timeframe` в статистике наконец различается, и блок «Таймфреймы: N/A» в отчётах заменён реальной разбивкой 1D/1H (см. #12, где N/A был честной заглушкой).

**Устранено дублирование.** Логика трёх индикаторов была скопирована в `evaluate_signals()` и `scan_technical_symbols()`; при добавлении 1H стало бы четыре копии. Теперь единая `scan_technical(symbol, timeframe)` + `scan_all_technical()`. `evaluate_signals()` отвечает только за volume/OI/funding.

**Схема БД:** добавлена колонка `candle_close_ts BIGINT` через `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` — существующие строки в Neon сохраняются с `NULL`. Резолюция OPEN-сигналов теперь фильтруется по таймфрейму: дневной сигнал нельзя закрывать по часовым уровням Дончиана и наоборот.

**Добавлены монеты:** ZECUSDT, SHIBUSDT, NEARUSDT, GRAMUSDT. Все четыре проверены запросом к Binance API **до** добавления. Пользователь просил `SHIBAUSDT` — такого тикера не существует (`{"code":-1121,"msg":"Invalid symbol."}`), правильный `SHIBUSDT`; добавление как есть дало бы второй CAPUSDT. CAPUSDT оставлен по явному решению пользователя.

**Последствие, о котором предупреждён пользователь:** по 1D сигналов станет заметно меньше — теперь ждём закрытия дня. Тишина в первые сутки после деплоя ожидаема и не является сбоем.

---

### 14. 4H контекст тренда: структура рынка вместо отдельного индикатора, и почему он ничего не блокирует

**Задача пользователя:** иерархия таймфреймов — 1D задаёт глобальное направление, 4H основной тренд и структуру, 1H остаётся местом поиска входа. Требование: объективное определение BULLISH/BEARISH/NEUTRAL без «на глаз», разметка HH/HL/LH/LL, динамическая трендовая линия по подтверждённым swing-точкам, без look-ahead и перерисовки.

**Тренд = структура, отдельного индикатора нет.** Самое важное решение здесь. Соблазн был добавить скользящую среднюю или наклон регрессии — это принесло бы новый период, новый порог и второе, конкурирующее определение тренда в системе. Вместо этого тренд выводится прямо из той структуры, которую пользователь и так просил размечать:

```
последний HH и последний HL → BULLISH
последний LH и последний LL → BEARISH
всё остальное               → NEUTRAL
```

Ни одного нового порога. `trend_from_structure()` — три строки. Один и тот же `analyze()` обслуживает и 4H, и 1D: разница только во входных свечах, поэтому двух разных определений тренда в системе не существует.

**Единственное новое число — окно подтверждения swing (`SWING_LOOKBACK`).** Изобретать его я не стал: пользователь выбрал явно из предложенных вариантов и попросил сделать настраиваемым, дефолт 2 (классические фракталы Билла Уильямса, дефолт `ta.pivothigh` в TradingView). Меняется переменной окружения без правки кода.

**Отсутствие look-ahead — три уровня защиты.** Swing на позиции `i` подтверждается, только если он строгий экстремум на `[i-N, i+N]`, то есть нужны N ЗАКРЫВШИХСЯ свечей справа. Поэтому: кандидаты берутся только из `i ∈ [N, len-1-N]`; последние N свечей физически не могут дать подтверждённую точку; массив приходит из `fetch_klines()`, который уже отбросил незакрытую свечу (#13). Проверено тестом, который скармливает один и тот же ряд, обрезанный в разных местах, и требует, чтобы пик НЕ появлялся, пока справа не закрылись ровно N свечей.

Плата: задержка подтверждения N баров. На 4H при N=2 это 8 часов. Альтернативы нет — либо задержка, либо перерисовка. Пользователь предупреждён.

**Трендовая линия по двум точкам, а не регрессией.** Две последние подтверждённые точки: по lows для bullish, по highs для bearish, при NEUTRAL линия не строится вовсе (рисовать её было бы вымыслом). Две точки детерминированы — одна линия, ноль свободы, ровно так линию рисует трейдер руками, и это тривиально повторить в Pine. Регрессия дала бы более гладкий результат ценой ещё одного параметра «сколько точек брать».

Как `x` берётся `close_time` в миллисекундах, а не индекс свечи — тогда формула переносится в TradingView без пересчёта индексов. Перерисовки нет по построению: обе точки в прошлом и уже неизменны; появление нового swing создаёт НОВУЮ линию, а не переписывает старую.

**Контекст ЗАМОРАЖИВАЕТСЯ в момент сигнала — самое опасное место фичи.** `trend_1d`, `trend_4h`, `structure_4h`, `alignment` и параметры линии пишутся в БД один раз при отправке и никогда не пересчитываются. Если бы `resolve_open_signals()` пересчитывал их задним числом, рынок к моменту резолюции уже уехал бы и вся статистика по alignment стала бы мусором — «сигнал был против тренда» превратилось бы в «сигнал был по тренду, который сложился потом». Закрыто отдельным тестом, который резолвит сигнал на ушедшем рынке и требует побайтового совпадения всех семи полей контекста.

**4H НИЧЕГО НЕ БЛОКИРУЕТ — явное требование пользователя на этом этапе.** Сигнал с `alignment=CONFLICT` отправляется ровно так же, как со `STRONG`. Цель первого этапа — накопить данные и проверить гипотезу, а не отсечь сигналы заранее. Проверено анализом AST функции `_emit`: единственные условия отказа — дедуп по свече и результат `alert_engine.submit()`; ни одно ветвление не зависит от `alignment`, `trend_4h`, `trend_1d` или `structure_4h`.

**Контекст грузится только при сработавшем сигнале**, а не на каждой итерации: иначе это были бы лишние 34 запроса к Binance каждые 5 минут ради данных, которые чаще всего никому не нужны. При любой ошибке загрузки или анализа возвращается `None` — сигнал уходит без блока контекста, но не теряется.

**Сигналы без контекста исключены из разбивки по alignment**, а не подмешаны в какую-нибудь группу: `by_alignment` строится только по строкам, где `alignment` не NULL. Общий счёт закрытых сигналов при этом остаётся полным — поле `aligned_closed` показывает, сколько из них имеют контекст. Иначе статистика молча врала бы на исторических сигналах, записанных до Phase 4.

**TradingView.** Python-бот не может рисовать на графике — у TradingView нет API для внешней отрисовки, а автоматизировать браузер ради этого было бы хрупко и бессмысленно. Вместо этого `trendline.pine` повторяет ту же математику: `ta.pivothigh/pivotlow(N, N)` имеют ту же семантику подтверждения (возвращают значение со сдвигом на N баров, будущее не видят), наклон считается по тем же двум точкам во времени. Совпадение гарантируется при трёх условиях: тот же символ на BINANCE, тот же таймфрейм, тот же `swingLookback` = `SWING_LOOKBACK`.


---

## 15. Telegram стал торговым терминалом, а не потоком детекторов

**Проблема.** 2026-08-10 утром в Telegram пришло 104 сообщения: отдельно
Breakout, отдельно Turtle Zone, отдельно Failure Test, к каждому — блок
1D/4H тренда, структуры и alignment. Арифметика объясняет цифру полностью:
16 символов × 2 таймфрейма × 3 детектора = до 96 сообщений за один проход
цикла, а проходов за утро несколько. Это было штатное поведение кода, а не
сбой: каждый детектор независимо вызывал `_emit()` → `engine.submit()` →
`send_alert()`.

**Решение.** Изменён ТОЛЬКО слой показа. Три детектора собираются в один
итоговый сигнал; символ, по которому сигнал ушёл, помечается занятым до
TARGET HIT или STOP HIT; одновременно открытых сделок не больше
`MAX_OPEN_SIGNALS`.

**Формулы не изобретались.** Stop / MMO / 1R / размер позиции перенесены
дословно из локального бота пользователя (`~/Documents/Claude/Projects/
Crypto biot`), включая константы 0.3% буфера, плоского стопа 2%, депозита
$1000, риска 2% и плеча 5x. Ни одного нового порога не появилось.

**Два независимых правила выхода — сознательно.** Стоп в сообщении (узкий,
0.3% внутрь уровня) и правило WIN/LOSS в `signal_stats/` (широкое, границы
каналов Дончиана) НЕ синхронизированы. Синхронизация потребовала бы менять
логику статистики и сделала бы старые записи несравнимыми с новыми. Вместо
этого на одних и тех же сигналах копятся две метрики, и через несколько
недель можно будет объективно сказать, какое правило выхода точнее.
Следствие, о котором надо помнить: сделка может быть STOP_HIT в
`active_trades` и одновременно OPEN или даже WIN в `/week`.

**Что изменилось в наполнении статистики.** Раньше сигнал записывался
только если сообщение реально ушло в Telegram. Теперь Telegram получает
одно сообщение вместо трёх, и такая привязка уничтожила бы статистику —
поэтому записывается КАЖДЫЙ сработавший детектор. Структура таблицы
`signals`, правило WIN/LOSS и агрегации не тронуты, но количество записей
вырастет, и с 2026-08-10 данные до и после не вполне сравнимы по объёму.

**Аварийный откат.** `TRADE_SIGNALS_ONLY=false` в переменных окружения
Render возвращает прежнее поведение целиком, без redeploy и без git revert.

**Что осталось нетронутым:** `technical_signals.py`, `trend_context.py`,
`signal_stats/signal_tracker.py`, `signal_stats/performance.py` — проверено
побайтовым сравнением с `99fbad5` в `test_trade_state.py`.

### 15a. Глобальный лимит слотов и судьба непрошедших кандидатов

`MAX_ACTIVE_TRADES = 3` — лимит по ВСЕМ символам сразу, не по символу.
Плюс отдельное правило: не больше одной открытой сделки на символ.

Слот освобождается ТОЛЬКО событием рынка — TARGET HIT или STOP HIT. Ни
таймаута, ни вытеснения: открытая сделка никогда не закрывается ради нового
сигнала, и система не пытается выбрать «более удачного» кандидата вместо уже
открытого. Это сознательно — иначе бот начал бы принимать торговые решения
за пользователя, а он их не просил.

Кандидаты, не прошедшие гейт, НЕ теряются. Они пишутся в `active_trades` с
полностью посчитанными entry/stop/target/позицией и статусом:

| Статус | Когда |
|---|---|
| `SKIPPED_CAPACITY` | заняты все `MAX_ACTIVE_TRADES` слотов |
| `SKIPPED_SYMBOL_OPEN` | по символу уже открыта сделка |
| `SKIPPED_COOLDOWN` | не прошла пауза в одну свечу после закрытия |

Уровни считаются ДО проверки гейта именно ради этого: через месяц можно будет
объективно посчитать цену ограничения — сколько сигналов лимит съел и куда бы
они пошли, — а не спорить об этом на ощупь. Выборка: `get_skipped_candidates()`.

Уникальный частичный индекс `idx_active_trades_one_per_symbol` действует только
на `status='OPEN'`, поэтому SKIPPED-строк по одному символу может быть сколько
угодно, а гонка двух таймфреймов за один слот по-прежнему невозможна.
