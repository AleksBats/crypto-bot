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

**Found via production logs:** every `klines` request for `CAPUSDT` returns `400 Bad Request`. The user was informed and explicitly chose to keep it in `TECHNICAL_SYMBOLS` anyway rather than remove or substitute it. `scan_technical_symbols()` already handles this gracefully — it logs a warning and continues to the next symbol, it does not crash or block the other 11 coins.

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
