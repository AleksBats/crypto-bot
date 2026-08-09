# Aster Intelligence Bot

A 24/7 Python bot that polls Binance's free public REST API and sends Telegram alerts on trading signals. Runs continuously on Render.com (free tier) as service **`crypto-bot-eu`**.

## What it watches

**Main symbol — ASTERUSDT** (configurable via `SYMBOL_SPOT`/`SYMBOL_FUTURES`):
- Volume spikes (1h volume vs rolling average)
- Open Interest changes
- Extreme funding rate
- Breakout / Turtle Zone Filter / Failure Test (see below)

**16 additional coins, technical signals only** (SOL, LINK, ETH, BTC, XRP, XLM, HYPER, ADA, DOGE, PEPE, PENGU, CAP, ZEC, SHIB, NEAR, GRAM — all vs USDT, configurable via `TECHNICAL_SYMBOLS`):
- Breakout, Turtle Zone Filter, Failure Test only — no volume/OI/funding noise for these.

**Two timeframes in parallel — 1D and 1H**, computed on **closed candles only**. A signal can never appear mid-candle and then change its mind. Alerts show two separate prices: the close of the candle that created the signal, and a fresh market price fetched right before sending. The same signal is never re-sent for the same candle. See DECISIONS.md #13.

**Trend context (4H + 1D)** — every signal is annotated with market structure (HH/HL/LH/LL), trend direction and a dynamic trendline built from confirmed swing points. **It never blocks or alters a signal** — it is informational and accumulates in the statistics so the hypothesis can be tested on real data first. Swing points are only confirmed once `SWING_LOOKBACK` candles have closed to their right, so there is no look-ahead and the trendline never repaints. `trendline.pine` reproduces the identical formula on TradingView. See DECISIONS.md #14.

All three technical indicators are standard Donchian-channel implementations (not exact copies of any TradingView Pine script — see CLAUDE.md/DECISIONS.md).

## Signal performance statistics / paper trading

Every Breakout / Turtle Zone / Failure Test signal that's actually sent to Telegram (survives the 30-minute cooldown) is recorded and later resolved to **WIN**, **LOSS**, or **OPEN**:

- **WIN** — price closed beyond the 55-day slow Donchian band in the predicted direction before invalidation.
- **LOSS** — price closed past the fast-channel opposite band (or, for Failure Test, re-crossed the trap level) first.
- **OPEN** — neither has happened yet; stays open indefinitely (no arbitrary timeout).

No entry/stop/target was ever defined in the original signal system, so this rule was built entirely from Donchian levels the bot already computes — see `DECISIONS.md` #11/#12. Each closed signal also tracks R-multiple (against a *frozen* initial-risk snapshot), MFE/MAE, and the RSI(14) at entry (context only, doesn't affect signal generation).

Check it in Telegram any time:

| Command | Shows |
|---|---|
| `/today` | Today's signals, individually listed if few enough |
| `/week` | Last 7 days — win rate, LONG/SHORT breakdown, R stats, best/worst signal, setup breakdown |
| `/month` | Same as `/week`, last 30 days |
| `/stats` | All-time, since the first recorded signal |

No automatic weekly push yet — implemented and tested, not scheduled (deliberately deferred, see TODO.md).

**Timeframe breakdown is real** now that 1D and 1H run in parallel — `/week` and `/stats` show genuine 1D-vs-1H win rates. Volume still isn't tracked for the technical-only symbols (not fetched there) and renders as an explicit `N/A` rather than an invented number.

**Storage:** Neon Postgres (free tier) via `DATABASE_URL` — chosen because Render's free tier has no persistent disk and Render's own free Postgres expires after 30 days. If `DATABASE_URL` isn't set, the bot runs completely normally (alerts, indicators, everything) with statistics silently disabled — see `signal_stats/signal_store.py`.

## Files

| File | Purpose |
|---|---|
| `run_live.py` | Entry point. Asyncio polling loop + tiny HTTP health-check server for Render + statistics background task. |
| `config.py` | Loads all environment variables and thresholds. |
| `state.py` | In-memory shared state (price/OI/funding history, alert cooldown tracker). |
| `alert_engine.py` | Decides when a signal becomes a Telegram message (cooldowns, weak-signal combos). |
| `telegram_bot.py` | Sends HTML-formatted messages via the Telegram Bot API. |
| `technical_signals.py` | Pure functions: `detect_breakout`, `detect_turtle_zone`, `detect_failure_test`. |
| `trend_context.py` | Pure functions: swing detection, HH/HL/LH/LL structure, trend, trendline. Informational only. |
| `trendline.pine` | TradingView indicator reproducing the same trendline formula. |
| `signal_stats/signal_store.py` | Postgres persistence (Neon) — all SQL lives here. |
| `signal_stats/signal_tracker.py` | Records signals, resolves WIN/LOSS/OPEN, computes RSI, detects combo setups. |
| `signal_stats/performance.py` | Pure aggregation: win rate, R, MFE/MAE, Profit Factor, breakdowns. |
| `signal_stats/reports.py` | Builds the `/today /week /month /stats` Telegram messages. |
| `signal_stats/telegram_commands.py` | Long-polls Telegram for incoming commands. |
| `test_statistics.py` | Synthetic tests for the whole statistics package (54 checks, in-memory store). |
| `test_trend_context.py` | Synthetic tests for 4H context (53 checks: look-ahead, repaint, frozen context). |
| `requirements.txt` | Python dependencies (`httpx`, `python-dotenv`, `asyncpg`). |

## Running locally

```bash
pip install -r requirements.txt
# create a .env with TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID at minimum (see config.py)
python run_live.py
```

Stop with `Ctrl+C` — it sends a graceful "Stopped" notice to Telegram before exiting.

To also test the statistics layer without Postgres: `python test_statistics.py`.

## Deployment

Live on **Render.com**, free tier, region **Frankfurt** (`crypto-bot-eu`):

- Build command: `pip install -r requirements.txt`
- Start command: `python run_live.py`
- Required env vars: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
- Optional (for statistics to persist): `DATABASE_URL` (Neon Postgres connection string)
- Auto-deploys on every push to `main` on `github.com/AleksBats/crypto-bot`

**Region matters:** Binance blocks API requests from US-region IPs (HTTP 451). This bot must run in a non-US Render region — Frankfurt or Singapore. It was originally deployed to Oregon and was completely non-functional until moved. See DECISIONS.md.

### Keep-alive

Render's free tier spins a Web Service down after 15 minutes with no *inbound* HTTP traffic — the bot's own outbound calls to Binance/Telegram don't count. An UptimeRobot HTTP monitor pings `https://crypto-bot-eu-bulo.onrender.com` every 5 minutes to keep the service alive around the clock. If alerts stop arriving, check the UptimeRobot dashboard before assuming a code issue.

## Signal cooldown

Every signal type has a 30-minute cooldown per symbol (`ALERT_COOLDOWN_SECS`, `alert_engine.py`) to prevent spam. Signal keys are symbol-scoped (e.g. `breakout_bull_SOLUSDT`) so different coins never share a cooldown. Statistics only records a signal if it actually cleared this cooldown and was sent — see DECISIONS.md #12 for why (paper trading should reflect what a subscriber actually saw).

## Known limitations

- `CAPUSDT` doesn't exist on Binance Spot — it's in `TECHNICAL_SYMBOLS` by user request but every request for it 400s and is skipped with a log warning.
- Signal deduplication (one message per candle) lives in process memory, so a Render restart can allow one repeat per candle. Deliberate — see DECISIONS.md #13.
- Whale on-chain tracking and Twitter/X monitoring are configured in `config.py` but not wired into `run_live.py` — see CLAUDE.md.
- `signal_stats/signal_store.py` has been reviewed but never executed against a live Postgres connection (sandbox limitation, not a code gap) — see TODO.md for the smoke test to run once `DATABASE_URL` is live.
- No automatic weekly report yet — `/week` works on demand; scheduled delivery is a deliberate follow-up.

## More context

- **CLAUDE.md** — instructions for AI assistants working in this codebase.
- **PROJECT.md** — full project narrative and current status.
- **DECISIONS.md** — every non-obvious technical decision and the bug (or, for #11/#12, the missing spec) that motivated it.
- **CHANGELOG.md** — chronological log of what changed and when.
- **TODO.md** — open items.
