# CHANGELOG.md

All notable changes to the Aster Intelligence Bot, in chronological order. Dates are approximate (from the working conversation, not git commit timestamps — see the GitHub repo's commit history at `github.com/AleksBats/crypto-bot/commits/main` for exact timestamps and hashes).

## 2026-08-09 (удаление CAPUSDT)

- **Удалён** `CAPUSDT` из `TECHNICAL_SYMBOLS` по явному указанию пользователя. Тикера никогда не существовало на Binance Spot (`400 Bad Request` на каждый запрос, см. DECISIONS.md #10); после добавления часового контура он стал 400-ить дважды за цикл вместо одного раза. Осталось 16 символов: ASTERUSDT + 15 технических.
- Больше ничего не изменено: индикаторы, пороги, cooldown, логика сигналов, статистика и контекст тренда не затронуты.

## 2026-08-09 (Phase 4 — контекст тренда 4H/1D, структура рынка, трендовая линия)

Иерархия таймфреймов: 1D — глобальное направление, 4H — основной тренд и структура, 1H — поиск входа. **4H на этом этапе ничего не блокирует** — только описывает обстановку и копится в статистику, чтобы сначала проверить гипотезу на данных. Полный разбор в DECISIONS.md #14.

- **Добавлен** `trend_context.py` — чистые функции: `find_swings()` (фракталы без look-ahead), `classify_structure()` (HH/HL/LH/LL), `trend_from_structure()`, `build_trendline()`, `compute_alignment()`. Ни одного обращения в сеть, никаких побочных эффектов.
- **Тренд выводится из структуры, а не из отдельного индикатора** — HH+HL → BULLISH, LH+LL → BEARISH, остальное → NEUTRAL. Так в системе одно определение тренда вместо двух конкурирующих и не появляется ни одного нового порога.
- **Единственное новое число** — `SWING_LOOKBACK` (дефолт 2, фракталы Билла Уильямса). Выбрано пользователем явно, настраивается переменной окружения.
- **Трендовая линия** по двум последним подтверждённым swing-точкам: по lows для bullish, по highs для bearish, при NEUTRAL не строится. `x` — время в миллисекундах, чтобы формула один-в-один переносилась в TradingView.
- **Контекст замораживается в момент сигнала** и никогда не пересчитывается при резолюции — иначе статистика по alignment превратилась бы в мусор. Закрыто отдельным тестом.
- **Добавлены** 7 колонок в Neon через `ALTER TABLE ADD COLUMN IF NOT EXISTS`: `trend_1d`, `trend_4h`, `structure_4h`, `alignment`, `trendline_slope`, `trendline_anchor_ts`, `trendline_anchor_price`. Существующие строки сохраняются с NULL.
- **Добавлен** блок «📐 КОНТЕКСТ» в Telegram-сообщение и блок «🧭 СОГЛАСОВАННОСТЬ ТАЙМФРЕЙМОВ» в `/week` `/month` `/stats` — win rate, средний R и MFE/MAE по группам STRONG / PARTIAL / CONFLICT, плюс разбивка по направлению 4H. Группы с малой выборкой помечаются явно.
- **Добавлен** `trendline.pine` — индикатор TradingView с идентичной формулой. Python физически не может рисовать на чужом графике; совпадение обеспечивается тем, что `ta.pivothigh(N,N)` имеет ту же семантику подтверждения.
- **Не тронуто:** `technical_signals.py`, `state.py`, `alert_engine.py` — байт в байт. Пороги Дончиана, cooldown, правила отправки и список монет не изменены.
- **Проверено анализом AST**, что решение об отправке сигнала не зависит ни от одного поля контекста: единственные условия отказа — дедуп по свече и результат `alert_engine.submit()`.
- **Тесты:** 53 новых в `test_trend_context.py` (look-ahead, отсутствие перерисовки, заморозка контекста, структура, линия, alignment, агрегация) + прежние 54 в `test_statistics.py` не сломаны. Итого 107.

## 2026-08-08 (Phase 3 — закрытые свечи, две цены, дедуп по свече, часовой контур)

Реакция на реальный дефект, найденный пользователем на живом рынке: Failure Test по XRPUSDT показывал `1.046500` при рынке ~1.0481 и присылал одно и то же сообщение трижды за два часа. Разбор до кода показал три независимые причины — кэш свечей на 15 минут без запроса свежей цены перед отправкой, отсутствие дедупликации (cooldown ≠ дедуп) и участие незакрытой свечи в расчёте. Полный разбор в DECISIONS.md #13.

- **Исправлено** отображение цены: теперь две отдельные строки — `Цена сигнала` (close закрытой свечи, по которой считались уровни) и `Текущая цена` (свежий запрос к Binance непосредственно перед отправкой) с процентом расхождения. При недоступности свежей цены честно пишется «н/д» вместо тихой подстановки устаревшей.
- **Исправлены** повторные отправки: дедупликация по `(symbol, timeframe, setup, direction, candle_close_ts)` — одна свеча даёт максимум одно сообщение. Прежний cooldown был rate limiting'ом, а не дедупом.
- **Изменено** время срабатывания: незакрытые свечи отбрасываются по фактическому `close_time`. По решению пользователя — для **всех трёх** индикаторов, не только Failure Test. Следствие: по 1D сигналов станет заметно меньше.
- **Добавлен** параллельный часовой контур (1H) со своим кэшем и лимитом свечей. Дневной не изменился. Поле `timeframe` в статистике наконец различается — блок «Таймфреймы: N/A» в `/week` и `/stats` заменён реальной разбивкой 1D/1H.
- **Добавлены** монеты ZECUSDT, SHIBUSDT, NEARUSDT, GRAMUSDT (итого 17 символов). Все проверены через Binance API до добавления: запрошенный пользователем `SHIBAUSDT` не существует, правильный тикер `SHIBUSDT` — иначе получили бы второй CAPUSDT. CAPUSDT оставлен по решению пользователя.
- **Устранено** дублирование логики: три индикатора были скопированы в двух местах, при добавлении 1H стало бы четыре копии. Теперь единая `scan_technical(symbol, timeframe)`; `evaluate_signals()` отвечает только за volume/OI/funding.
- **Схема БД:** колонка `candle_close_ts` через `ALTER TABLE ADD COLUMN IF NOT EXISTS` — существующие данные в Neon сохраняются. Резолюция OPEN-сигналов фильтруется по таймфрейму.
- **Не тронуто:** `technical_signals.py` и `state.py` байт в байт, ни один торговый порог не изменён, в `config.py` только добавления.
- **Тесты:** 54 проверки (было 44) — добавлены изоляция таймфреймов, сохранение `candle_close_ts`, разбивка по TF. Отдельно проверены отбрасывание незакрытой свечи и все ветки дедупа.

## 2026-08-08 (Phase 2 — signal performance statistics / paper trading)

Full spec-driven build: persistent WIN/LOSS/OPEN tracking with R-multiples, MFE/MAE, RSI context, combo-setup detection, and on-demand Telegram reports — superseding the first prototype from earlier the same day. Went through an explicit Phase 1 (architecture analysis + plan, no code) and a Telegram-preview approval step before any file was touched, per the user's process requirement.

- **Analyzed first, wrote nothing:** traced exactly where signals originate (`technical_signals.py`'s three detectors), where they reach Telegram (`alert_engine.submit()` → `telegram_bot.send_alert()`), and confirmed no entry/stop/target/RSI/volume-for-12-coins/multi-timeframe data existed to record — presented as an explicit plan with open decisions, not assumed.
- **User decisions collected via structured questions, not assumed defaults:** record signals only once actually sent to Telegram (not raw detection) · Neon Postgres for persistence (researched live — confirmed Render's free tier has no persistent disk and Render's own free Postgres expires after 30 days) · reuse the Phase-1 Donchian WIN/LOSS rule · add RSI(14) as a new context-only field · do NOT add volume for the 12 technical-only symbols.
- **Shipped a full Telegram-message preview with fabricated-but-consistent numbers before writing any code**, then revised it after user feedback: removed a timeframe leaderboard the architecture can't support (bot only trades 1D), replaced a non-existent symbol (`SPX/USDT`) with real ones, and added a new "Breakout + Turtle" combo-setup category after confirming it's genuinely detectable (both detectors firing same symbol/direction/day), not fabricated.
- **Added** `signal_stats/` package: `signal_store.py` (all SQL, Neon Postgres via `asyncpg`, degrades to a safe no-op if `DATABASE_URL` is unset), `signal_tracker.py` (record/resolve, Wilder RSI(14), combo-setup decision — DB access injected via a `store=` parameter for testability), `performance.py` (pure aggregation, no I/O), `reports.py` (builds `/today /week /month /stats` message text), `telegram_commands.py` (new: the bot's first-ever incoming-message listener, long-polling `getUpdates`, isolated as its own background task).
- **R-multiple design decision:** freezes the initial risk (distance from entry to the invalidation band *as it existed at signal time*) even though the band used to actually trigger LOSS keeps rolling forward as a trailing stop — otherwise R would be measured against a moving target. See DECISIONS.md #12.
- **One additive change to existing files, both backward-compatible:** `alert_engine.submit()` now returns `bool` (whether the message was actually sent) instead of `None` — cooldown/combo logic itself is untouched. `run_live.py` gained statistics hooks at the exact points signals already fire, plus a background task for the command listener — no detector, threshold, symbol list, or alert-sending behavior changed.
- **Added** `DATABASE_URL`, `RSI_PERIOD`, `TELEGRAM_POLL_INTERVAL_SECS`, `STATS_ALLOWED_CHAT_ID`, `MIN_SAMPLE_FOR_RANKING` to `config.py` — none are trading thresholds; `MIN_SAMPLE_FOR_RANKING` (default 3) exists so a single lucky/unlucky trade can't look like a symbol/setup "trend" in `/week` `/month` `/stats`.
- **Added** `asyncpg` to `requirements.txt`.
- **Tested** with `test_statistics.py` — 44 checks covering all 10 required scenarios (LONG/SHORT WIN/STOP, OPEN excluded from win rate, weekly/monthly date filtering, dedup, restart/persistence semantics, no-look-ahead, RSI edge cases, aggregation math) against an in-memory fake store implementing the exact same async interface as `signal_store.py`.
- **Flagged, not silently skipped:** `signal_store.py`'s actual SQL was hand-reviewed but never run against live Postgres — this sandbox has no root access and apt is blocked by the network egress allowlist, so no local Postgres could be provisioned. A manual smoke test against the real Neon instance is listed in TODO.md. Also **not done, by explicit instruction:** wiring an automatic weekly report send — `/week` works on demand; scheduling it was deliberately deferred to a follow-up.

## 2026-08-08 (later same day) — first prototype, superseded above

- **Added** `signal_tracker.py` — objective WIN/LOSS/PENDING accuracy tracking for Breakout, Turtle Zone Filter, and Failure Test signals. Before implementing, audited the existing signal system for an entry/stop/target/invalidation definition; none existed, so per explicit instruction, implementation was paused and the user was asked to choose an objective rule rather than one being invented. See DECISIONS.md #11.
- **Rule chosen (user-selected from presented options):** reuse Donchian levels the bot already computes — fast-channel opposite band (rolling) = invalidation, slow-channel (55-day) same-direction band = confirmation. No new percentage threshold. Pending signals never time out (explicit user decision).
- **Wired** `signal_tracker.tracker.record()` and `.evaluate_pending()` into both `evaluate_signals()` (ASTERUSDT) and `scan_technical_symbols()` (12-coin scan) in `run_live.py`, right where each detector already fires.
- **Added** `fmt_stats_summary()` to `telegram_bot.py` and `maybe_send_stats_report()` to `run_live.py` — posts a WIN/LOSS/PENDING summary to Telegram roughly once a day (`config.STATS_REPORT_INTERVAL_SECS`, a reporting cadence only, not a scoring threshold).
- **Added** `STATS_REPORT_INTERVAL_SECS` to `config.py`.
- **Verified** `signal_tracker.py`'s WIN, LOSS, PENDING, dedup, and stats-aggregation logic with six synthetic test cases before wiring it in (same discipline as DECISIONS.md #2 — see `test_signal_tracker.py`-style checks run during development).
- **Flagged, not fixed:** `signals_log.json` (the persisted accuracy history) lives on Render's ephemeral free-tier disk and will be wiped on redeploy — see TODO.md. No persistent store was wired in as part of this change since it wasn't requested and would add a paid dependency or an external service.

## 2026-08-08

- **Added** `technical_signals.py` with `detect_breakout`, `detect_turtle_zone`, `detect_failure_test` — standard Donchian-channel implementations of the user's original TradingView indicator concepts (commit `c62d89a`, "Add Breakout, Turtle Zone Filter, Failure Test signals").
- **Fixed** a bug in `detect_failure_test` where the reference channel included the breakout bars themselves, masking real failure-test signals. Fixed by excluding the lookback window from the channel calculation. Verified with synthetic test cases before shipping.
- **Added** three new Telegram formatters (`fmt_breakout_alert`, `fmt_turtle_zone_alert`, `fmt_failure_test_alert`) to `telegram_bot.py`.
- **Added** `DONCHIAN_LOOKBACK`, `TURTLE_FAST_LOOKBACK`, `TURTLE_SLOW_LOOKBACK`, `FAILURE_TEST_LOOKBACK`, `DAILY_KLINES_LIMIT`, `POLL_TECHNICAL_SECS` to `config.py`.
- **Wired** the three new signals into `run_live.py`'s `evaluate_signals()` for the main ASTERUSDT symbol.
- **Added** a stdlib HTTP health-check server (`_HealthHandler`, `_start_health_server()`) to `run_live.py` so Render's free-tier "Web Service" health check passes (commit `2505359`, "Add Render health-check server for free-tier Web Service").
- **Deployed** to Render.com, region Oregon (US) — **failed in production**: Binance returned HTTP 451 (blocked) for every API call from the US datacenter.
- **Created** a new Render service `crypto-bot-eu` in the Frankfurt region with identical code and env vars — confirmed working (Binance calls succeeding).
- **Added** `TECHNICAL_SYMBOLS` config list (12 coins: SOL, LINK, ETH, BTC, XRP, XLM, HYPER, ADA, DOGE, PEPE, PENGU, CAP vs USDT) and `scan_technical_symbols()` in `run_live.py` — extends Breakout/Turtle Zone/Failure Test to these coins without adding volume/OI/funding noise for them. Made `fetch_daily()` per-symbol (was ASTER-only) with a per-symbol cache dict (commit `9d30939`, "Scan Breakout/Turtle Zone/Failure Test across 12 additional coins").
- **Fixed** a pre-existing (not introduced this session) logging bug: `%,.0f` is not valid Python `%`-style format syntax. Non-fatal (Python's logging module caught it internally) but polluted logs. Fixed by pre-formatting with an f-string (commit `82ad2b2`, "Fix logging format bug (comma flag unsupported in %-style)").
- **Diagnosed** that Render's free tier spins services down after 15 minutes of no inbound HTTP traffic, even with the health-check server running. The bot's own outbound API calls don't count as activity.
- **Set up** an UptimeRobot HTTP monitor (5-minute interval) pinging `crypto-bot-eu`'s public URL to keep it alive continuously.
- **Deleted** the non-functional Oregon service (`crypto-bot`) to stop duplicate Telegram notifications from two services auto-deploying off the same repo.
- **Consolidated** the entire project into a clean `trading_bot/` folder (this folder) with full documentation, for a permanent Claude Project.

## Earlier (pre-existing, before this session)

- Original `AleksBats/crypto-bot` repo already contained a working ASTER-monitoring bot (`run_live.py`, `alert_engine.py`, `telegram_bot.py`, `config.py`, `state.py`) sending Telegram alerts for volume spikes, open-interest changes, and extreme funding rate on ASTERUSDT via Binance's public API.
- Several disabled/unwired monitor stubs also existed in the repo (`price.py`, `whale.py`, `funding.py`, `open_interest.py`, `volume.py`, `twitter.py`) — never imported by `run_live.py`, not part of the live pipeline.
- An unrelated bot (`crypto_bot.py`, "Crypto Signal Bot" — 6-hourly BTC analysis via Claude AI + CoinGecko + Fear & Greed + Reddit + CryptoPanic) also lived in the same repo and in the user's local `crypto_bot_project 2` folder. Not deployed, not related to this project.

## 2026-08-10 — Telegram output layer: один торговый сигнал вместо потока детекторов

**Причина:** утром пришло 104 сообщения (raw detector messages + блок контекста).
Аудит показал, что согласованные накануне изменения никогда не были написаны —
на Render работал `99fbad5`, последний коммит репозитория.

**Убрано из Telegram:** отдельные Breakout / Turtle Zone / Failure Test
сообщения, MULTIPLE SIGNALS, блок КОНТЕКСТ (1D тренд, 4H тренд, 4H структура,
Alignment), повторы по открытой сделке.

**Добавлено:** единый формат с Entry / Stop / Target1 / Target2 / риском в % и
блоком ПОЗИЦИЯ (объём, маржа при 5x, риск в долларах) — формулы перенесены из
локального бота пользователя без изменений. Гейт «одна сделка на символ» с
восстановлением состояния из Neon после рестарта Render. `MAX_OPEN_SIGNALS=3`.

**Не изменено:** математика индикаторов, пороги, список монет, расчёты 4H/1D
контекста (продолжают писаться в Neon), правило WIN/LOSS, агрегации отчётов.

**Новое:** `trade_state.py`, `test_trade_state.py` (74 проверки), таблица
`active_trades` в Neon (идемпотентная миграция).

**Откат:** `TRADE_SIGNALS_ONLY=false`.

**Уточнение по лимиту (то же изменение):** переменная называется
`MAX_ACTIVE_TRADES` (было `MAX_OPEN_SIGNALS`), лимит глобальный — 3 сделки по
всем символам сразу. Непрошедшие кандидаты сохраняются в Neon со статусами
`SKIPPED_CAPACITY` / `SKIPPED_SYMBOL_OPEN` / `SKIPPED_COOLDOWN` вместе с
посчитанными уровнями. Слот освобождается только по TARGET HIT / STOP HIT;
открытые сделки не вытесняются.
