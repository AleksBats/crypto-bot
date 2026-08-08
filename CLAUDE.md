# CLAUDE.md

Instructions for any Claude session working in this project folder.

## Before you change anything

1. **Read this file fully, then read PROJECT.md and DECISIONS.md.** They explain what exists, why it exists, and what's already been tried and rejected.
2. **Open the actual code before assuming what it does.** `run_live.py` is the entry point — trace its imports (`config`, `alert_engine`, `state`, `telegram_bot`, `technical_signals`, `signal_stats.signal_tracker`, `signal_stats.telegram_commands`) before changing any of them. Don't guess at behavior from filenames.
3. **Check the live GitHub repo, not just this folder, before deploying.** The source of truth for what's actually running in production is `https://github.com/AleksBats/crypto-bot` (branch `main`). This folder is a curated snapshot for the Claude Project — if you're about to deploy or debug a live issue, fetch the current repo state first (e.g. `https://raw.githubusercontent.com/AleksBats/crypto-bot/main/run_live.py`) since the user may have made dashboard/GitHub changes outside of a Claude session. **Note:** `raw.githubusercontent.com` has a CDN cache that can serve stale content for a few minutes after a push — append a throwaway query string (`?nocache=<anything>`) if you suspect you're seeing an old version.
4. **Do not silently rewrite working logic.** This bot has already been debugged (see DECISIONS.md for the false-breakout channel bug, the Binance region block, the `%,` logging bug, the Render free-tier sleep issue, and the WIN/LOSS/paper-trading methodology). If something looks wrong, check DECISIONS.md before "fixing" it — it may be intentional or already-solved-elsewhere.
5. **This is not the same project as `crypto_bot.py` / "Crypto Signal Bot".** That's a different, older, unrelated bot (6-hourly BTC analysis via Claude AI, CoinGecko, Fear & Greed, Reddit, CryptoPanic) that happens to live in the same GitHub repo history and the user's local `crypto_bot_project 2` folder. It is NOT deployed, NOT part of this pipeline, and its `requirements.txt`/`SETUP_GUIDE.md`/`.env.example` do not apply here. Do not merge the two.
6. **Never invent a WIN/LOSS threshold for signal accuracy stats.** `signal_stats/signal_tracker.py`'s evaluation rule reuses Donchian levels the bot already computes (see DECISIONS.md #11 and #12) specifically to avoid an arbitrary percentage cutoff. If asked to change how signals are scored, re-derive from existing levels/config constants, or stop and ask — don't pick a new number because it "feels reasonable."
7. **Never touch indicator logic, thresholds, symbol lists, or alert-sending behavior while working on statistics.** The `signal_stats/` package (Phase 2) was built under an explicit constraint: it only *observes* signals that already fire and get sent — it must never change whether/when/how an alert is sent. If a statistics change seems to require touching `technical_signals.py`'s detection logic or `config.py`'s trading thresholds, stop and ask first.
8. **Never name a top-level package after a stdlib module.** The statistics package is called `signal_stats/`, not `statistics/`, precisely because the latter shadowed Python's built-in `statistics` module for the whole process (`run_live.py` puts the project root first on `sys.path`) and broke `statistics.mean()` in the repo's `price.py` / `volume.py` stubs. It was caught and renamed before deploy — don't rename it back. See DECISIONS.md #12.

## What this project is

**Aster Intelligence Bot** — a 24/7 Python bot that polls Binance's free public REST API and pushes Telegram alerts when trading signals fire. Originally built to monitor the ASTER token; now also scans 12 additional coins for three technical-pattern indicators, tracks whether those signals actually worked, and answers `/stats /week /today /month` on demand in Telegram.

Two signal families:

- **ASTER-only** (`SYMBOL_SPOT` / `SYMBOL_FUTURES`, default `ASTERUSDT`): volume spikes, open-interest changes, extreme funding rate, plus all three technical indicators below.
- **Technical-only, multi-symbol** (`config.TECHNICAL_SYMBOLS`, 12 coins by default: SOL, LINK, ETH, BTC, XRP, XLM, HYPER, ADA, DOGE, PEPE, PENGU, CAP — all vs USDT): only the three technical indicators, no volume/OI/funding noise.

The three technical indicators (`technical_signals.py`) are **standard/generic Donchian-channel implementations**, not ports of the user's original custom TradingView Pine scripts — TradingView source code was never available, so these were built from scratch with the user's explicit approval to use "standard" versions:

- **Breakout** — classic N-bar (default 20) Donchian channel break.
- **Turtle Zone Filter** — dual-channel Turtle Trading style system (fast=20, slow=55); "zone" = fast channel broken, "confirmed" = both broken.
- **Failure Test** — false-breakout / trap detector. Computes the reference channel from bars *before* the lookback test window (critical: using the trailing window that includes the breakout bar itself masks the failure — see DECISIONS.md).

Every Breakout/Turtle Zone/Failure Test alert that is **actually sent to Telegram** (survives `alert_engine`'s cooldown) is recorded by `signal_stats/signal_tracker.py` and later resolved to WIN/LOSS/OPEN against the same Donchian levels — see "Signal performance statistics" below and DECISIONS.md #11/#12.

## Architecture at a glance

```
run_live.py          entry point — asyncio loop, polls every POLL_INTERVAL_SECS (5 min default)
  │                   also starts a tiny stdlib HTTP server on $PORT (health check for Render)
  │                   also starts signal_stats/telegram_commands.py as a background task
  ├─ config.py            loads all env vars, thresholds, symbol lists
  ├─ state.py              in-memory BotState (price/OI/funding history, cooldown tracker)
  ├─ alert_engine.py       AlertEngine — cooldown dedup + weak-signal combo aggregation; submit() returns bool
  ├─ telegram_bot.py       sends formatted HTML messages via Telegram Bot API
  ├─ technical_signals.py  pure functions: detect_breakout / detect_turtle_zone / detect_failure_test
  └─ signal_stats/           signal performance tracking / paper trading (Phase 2, see below)
       ├─ signal_store.py       ALL SQL lives here (Postgres/Neon via asyncpg); degrades to no-op if DATABASE_URL unset
       ├─ signal_tracker.py     record + resolve (WIN/LOSS/OPEN), RSI, combo-setup detection — pure-ish, store injected
       ├─ performance.py        pure aggregation (win rate, R, MFE/MAE, Profit Factor, breakdowns) — no I/O
       ├─ reports.py            builds the /today /week /month /stats Telegram text
       └─ telegram_commands.py  long-polls Telegram for incoming /stats /week /today /month commands

```

Flow per loop iteration: fetch ASTER spot+futures data → evaluate ASTER signals (volume/OI/funding/technical; technical signals also resolved/recorded against `signal_stats/`) → fetch+evaluate technical-only signals for the 12-coin list (same tracking) → sleep until next interval. The Telegram command listener runs as a fully independent background task, not part of this loop.

Signals are `Signal` dataclasses (`key`, `strong`, `message`, `priority`) submitted to the singleton `alert_engine.engine`. Each signal `key` is unique per symbol+direction (e.g. `breakout_bull_SOLUSDT`) so cooldowns and combos never collide across coins. `engine.submit()` returns `True` only if the message was actually sent just now — `run_live.py` uses that to decide whether to record a statistics row (see DECISIONS.md #12: paper trading should reflect what a subscriber actually saw, not every raw detector firing).

## Signal performance statistics (Phase 2)

Answers two things: "did our indicators actually predict direction correctly?" (WIN/LOSS/OPEN, same Donchian-based rule as the original #11 prototype — no invented percentage target) and "how well, in R/MFE/MAE/%, and broken down by symbol/setup?" (new in Phase 2). Full derivation in DECISIONS.md #12 — read it before changing the rule, the combo-setup logic, or the R-multiple math.

- `/stats` `/week` `/today` `/month` in Telegram — built by `signal_stats/reports.py`, answered by `signal_stats/telegram_commands.py` (a new long-polling listener; the bot never received messages before this).
- No automatic weekly push yet — deliberately deferred, see TODO.md.
- RSI(14) is recorded per signal for context only — it does not feed signal generation.
- Volume is **not** recorded for the 12 technical-only symbols (not fetched there) — shown as N/A, not fabricated.
- Timeframe is always `"1d"` — the bot only trades daily candles. Reports show an explicit N/A line instead of a fake multi-timeframe breakdown.

**Known limitation:** `DATABASE_URL` (Neon Postgres) must be set for any of this to persist — if unset, the bot logs one warning and every statistics function becomes a safe no-op (core alerts are completely unaffected). See TODO.md for the Postgres integration-test gap (verified with an in-memory fake store in this sandbox, not yet against live Neon).

## Deployment (current, live)

- **Host:** Render.com, free tier, service name **`crypto-bot-eu`**, region **Frankfurt**.
- **Why Frankfurt and not the default Oregon:** Binance blocks API requests from US-region IPs (HTTP 451). The original Oregon deployment (`crypto-bot`) was non-functional for this reason and has been **deleted**. Never redeploy this bot to a US Render region.
- **Build command:** `pip install -r requirements.txt`
- **Start command:** `python run_live.py`
- **Env vars required:** `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (see `.env.example` for the full optional list). `DATABASE_URL` is required only for the statistics feature to actually persist — the bot runs fine without it.
- **Keep-alive:** Render's free tier spins the service down after 15 minutes with *no inbound HTTP traffic* — the bot's own outbound Binance/Telegram calls do not count as activity. An UptimeRobot HTTP monitor pings the service's public URL (`https://crypto-bot-eu-bulo.onrender.com`) every 5 minutes to prevent this. If alerts stop arriving and Render shows repeated `SIGTERM` in the service's Events log, check that the UptimeRobot monitor is still active first, before assuming a code bug.
- See `render.yaml` for the settings as infrastructure-as-code, and DECISIONS.md for the full incident history that led to this setup.

## Known gaps / intentionally not included

- `price.py`, `whale.py`, `funding.py`, `open_interest.py`, `volume.py`, `twitter.py` exist in the GitHub repo's history as earlier/alternative monitor stubs. **None of them are imported by `run_live.py`** — they're dead code as far as the live bot is concerned. Don't assume they run.
- Whale on-chain tracking (`ASTER_CONTRACT_ADDRESS` etc. in `config.py`) is loaded but never used — the contract address was never confirmed.
- Twitter/X monitoring (`TWITTER_BEARER_TOKEN`, `TWITTER_WATCH_ACCOUNTS` in `config.py`) is loaded but never used.
- `CAPUSDT` (one of the 12 technical-only symbols) does not exist on Binance Spot as of this writing — it 400s on every request and is skipped with a warning log. The user asked to keep it in the list regardless; do not silently remove it without asking.
- `signal_stats/signal_store.py` has never run against a live Postgres connection (sandbox couldn't provision one) — see TODO.md for the smoke test to run once deployed.

## If you're asked to add a new signal or symbol

1. Add detection logic as a pure function in `technical_signals.py` (no side effects, take price arrays, return a dict or `None`) — follow the existing pattern.
2. Add a `fmt_*_alert()` formatter in `telegram_bot.py` that takes `symbol` as its first argument.
3. Wire it into both `evaluate_signals()` (ASTER-only path) and `scan_technical_symbols()` (multi-symbol path) in `run_live.py` if it should apply to both — give the `Signal.key` a `_{symbol}` suffix so cooldowns don't collide across coins.
4. Test the pure detection function with synthetic OHLC arrays before wiring it in (see DECISIONS.md for why — the Failure Test channel-window bug only showed up under synthetic testing, not casual reading).
5. Decide how the new signal should be scored WIN/LOSS in `signal_stats/signal_tracker.py` — reuse existing channel levels/config constants (see DECISIONS.md #11/#12), don't invent a new percentage threshold without asking the user first. Only record it after confirming `engine.submit()` returned `True` (see "Signal performance statistics" above).
6. Add synthetic test cases to `test_statistics.py` (WIN/LOSS/OPEN paths at minimum) before wiring it in.
7. Push to GitHub `main` on `AleksBats/crypto-bot` — Render auto-deploys `crypto-bot-eu` on every push to `main`.
