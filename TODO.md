# TODO.md

Open items, roughly in priority order. Nothing here is urgent — the bot is live and working — but these are worth addressing.

## Verification

- [ ] Confirm at least one real (non-startup) Telegram alert has actually fired for one of the three technical indicators, on either ASTERUSDT or one of the 12 technical-only coins. As of this consolidation, only startup/shutdown messages have been confirmed delivered — the detection logic has been verified with synthetic data, not yet observed firing on live market data.
- [ ] Let the bot run for a few days and confirm the UptimeRobot keep-alive is holding (check Render's Events log for absence of unexplained `SIGTERM` entries).
- [ ] Let `signal_stats/` accumulate at least a handful of resolved (WIN/LOSS) signals on live data and sanity-check a few by hand against a chart before trusting the win-rate percentage for decisions.

## Signal performance statistics / paper trading (Phase 2)

- [ ] **Run a manual smoke test against the real Neon instance once `DATABASE_URL` is set on Render.** This sandbox could not provision a local Postgres to test against (no root, `apt-get download` blocked by the network egress allowlist, no embeddable-Postgres Python package available) — `signal_store.py`'s SQL was hand-reviewed for correctness but never executed against a live database. After deploy: confirm `/stats` responds with "Данных пока нет" instead of erroring, then confirm one real fired signal shows up in `/today` and survives a Render redeploy (the actual persistence guarantee the whole Neon decision was for).
- [ ] **No automatic weekly report yet — by explicit instruction.** `signal_stats/reports.build_week_report()` is implemented and tested; wiring it to fire automatically every 7 days (e.g. via a periodic check in `run_live.py`'s loop, or an external Render Cron Job hitting a new endpoint) was deliberately deferred. Come back to this once `/week` has been used on demand for a bit.
- [ ] Decide on a policy for what counts as "signal performance" once the bot has been redeployed many times — since `fired_at` timestamps persist in Neon across redeploys (unlike the old JSON-file approach), `/stats` will keep accumulating correctly, but it's worth confirming with the user whether very old signals (e.g. from before a future indicator-logic change) should ever be excluded from `/stats`. Not an issue yet — there's no history.
- [ ] Consider exposing win-rate stats on the health-check HTTP endpoint (currently just returns `{"status": "alive"}`) so they're checkable without needing to send a Telegram command.
- [ ] `signal_stats/` has no `pytest`-based suite — `test_statistics.py` is a plain asyncio script (44 checks) in the same "synthetic testing before shipping" style as the rest of this project (see DECISIONS.md #2). Fine as-is, but if the project ever adopts `pytest` properly, this is the first candidate to convert.

## Superseded (first prototype, kept for history — see DECISIONS.md #12)

- [x] ~~`signals_log.json` is not on persistent storage~~ — resolved by moving to Neon Postgres in Phase 2. The Phase 1 JSON-file prototype never reached this repository at all; the `signal_stats/` package is the only implementation here.

## Cleanup

- [ ] The GitHub repo's own `requirements.txt` and `env_example.txt` still reference the unrelated `crypto_bot.py` bot's dependencies (`anthropic`, `requests`, `schedule`, `CLAUDE_API_KEY`, etc.), not `run_live.py`'s actual needs. This folder's copies were corrected (see DECISIONS.md #8); the GitHub repo itself was not. Consider pushing the corrected versions there too, or clearly separating the two bots into different repos/folders.
- [ ] Consider removing (or moving to a clearly-labeled `legacy/` subfolder) the unwired stub monitors in the GitHub repo (`price.py`, `whale.py`, `funding.py`, `open_interest.py`, `volume.py`, `twitter.py`) and `railway.toml`, so the repo itself matches what's actually deployed. Not done as part of this consolidation since the user's instruction was "do not rewrite the working application" — this would touch the live repo, not just this folder.
- [ ] Decide what to do with `crypto_bot.py` ("Crypto Signal Bot") living in the same GitHub repo — it's a fully separate, unrelated bot. Splitting it into its own repo would prevent future confusion (this consolidation task itself required carefully distinguishing the two).

## Possible future work (not started, not requested yet)

- [ ] Whale on-chain tracking is scaffolded in `config.py` (`ASTER_CONTRACT_ADDRESS`, `ETHERSCAN_API_KEY`/`BSCSCAN_API_KEY`/`SUBSCAN_API_KEY`, `WHALE_THRESHOLD_ASTER`) but never wired into `run_live.py` — needs the ASTER token's actual contract chain confirmed first.
- [ ] Twitter/X monitoring is scaffolded in `config.py` (`TWITTER_BEARER_TOKEN`, `TWITTER_WATCH_ACCOUNTS`) but never wired into `run_live.py`.
- [ ] `CAPUSDT` doesn't exist on Binance Spot (confirmed via repeated 400 responses) — ask the user if they meant a different ticker, or if it's fine to leave it silently skipped indefinitely.
- [ ] No automated tests exist for `technical_signals.py` beyond the ad-hoc synthetic bash checks run during development. Consider adding a small `pytest` suite so future changes to the Donchian logic can be verified without manual synthetic testing each time (`signal_stats/` has its own synthetic suite, `test_statistics.py`, see above — same idea, not yet unified under one `pytest` runner).

## Explicitly not a TODO

- The Flask webhook relay (`webhook_server.py`) built early in this project's history for the original TradingView-alert problem is **not** part of this bot and doesn't need finishing — the project was redirected to extend the existing ASTER bot instead. See PROJECT.md.
