# Aster Intelligence Bot — Project Specification

## 1. Project Goal

Build an automated monitoring bot that watches the ASTER token across multiple data sources and sends Telegram alerts **only when something significant happens** — not on every small price movement.

The bot runs continuously on Railway (cloud) and requires zero manual intervention once deployed.

---

## 2. What the Bot Monitors

### 2.1 Large Wallet Movements (Whale Monitor)
- Detect transfers of **≥ 20,000,000 ASTER** (or equivalent USD value)
- Classify each transfer as:
  - Whale transfer (wallet → wallet)
  - Deposit to exchange (wallet → known exchange address)
  - Withdrawal from exchange (exchange → wallet)
- Alert immediately — whale transfers are always a strong signal

**Status:** TODO — blocked on confirming ASTER contract address and blockchain (see Section 6).

### 2.2 Price Action
- Track current ASTER/USDT spot price via Binance
- Detect key support and resistance levels from swing highs/lows (last 100 × 1h candles)
- Alert on:
  - **Breakout** — price closes above resistance by ≥ 3%
  - **Breakdown** — price closes below support by ≥ 3%
  - **Holding support** — price near support level after a volume spike (weak signal, needs combo)

### 2.3 Volume Spikes
- Compare current 1h candle volume against 1h / 4h averages
- Alert if current volume ≥ **3× the 1h average** (weak signal)
- Alert immediately (strong) if current volume ≥ **6× the 1h average**

### 2.4 Open Interest
- Track ASTER perpetual futures OI
- Alert if OI changes by ≥ **10%** since last reading (weak signal)
- Alert immediately (strong) if OI changes by ≥ **20%**
- Source priority: CoinGlass aggregated (all exchanges) → Binance Futures fallback

### 2.5 Funding Rate
- Track current funding rate across all exchanges (aggregated via CoinGlass)
- Binance Futures `premiumIndex` as free fallback
- Alert if funding reaches ≥ **±0.05% per 8h** (extreme — strong signal alone)
- Alert if funding changes by ≥ 50% relative to last reading (sudden shift — weak signal)

### 2.6 X / Twitter Monitoring
- Monitor posts from: Aster official account, Binance, CZ, major crypto analysts
- Filter noise: ignore giveaways, retweets, community posts
- Only alert if post contains signal keywords: listing, delist, hack, exploit, partnership, mainnet, launch, breakout, whale, accumulate
- Posts from Binance or CZ = strong signal; others = weak signal

**Status:** Paid Twitter API disabled. Free alternatives configured (see Section 7).

---

## 3. Alert Logic

### 3.1 Signal Strength
Every detected event is classified as **strong** or **weak**:

| Signal | Strength |
|---|---|
| Whale transfer ≥ 20M ASTER | Strong |
| Price breakout / breakdown ≥ 3% | Strong |
| Extreme funding (≥ ±0.05%) | Strong |
| OI change ≥ 20% | Strong |
| Volume spike ≥ 6× avg | Strong |
| Large liquidation spike | Strong |
| Post from Binance / CZ | Strong |
| Volume spike 3–6× avg | Weak |
| OI change 10–20% | Weak |
| Price holding support | Weak |
| Sudden funding shift | Weak |
| Long/short ratio extreme | Weak |
| Post from other trusted account | Weak |

### 3.2 Alert Rules
- **Strong signal** → send Telegram alert immediately
- **Single weak signal** → do NOT send (accumulate)
- **Two or more weak signals** → send combo alert listing all active signals
- **Cooldown** → same signal type cannot alert again within **30 minutes**

### 3.3 Example Combo Scenarios
- Volume spike 3× + price holds support → combo alert
- OI spike 12% + funding shifts → combo alert
- Volume spike + OI spike + price approaches resistance → combo alert
- Long/short ratio extreme + funding extreme → combo alert

### 3.4 Anti-Spam Rules
- Never send a Telegram message for routine price movement (< 3%)
- Never send for volume that is within 3× of the average
- Never send the same signal type twice within 30 minutes
- Combine multiple weak signals into one message, never send separately

---

## 4. Alert Message Formats

All alerts are sent as HTML-formatted Telegram messages:

- **Whale:** 🐋 direction, amount in ASTER, USD equivalent, from/to addresses, tx hash
- **Price breakout/down:** 🚀/🔻 event type, current price, % change, level broken
- **Volume spike:** 📊 current volume, avg volume, ratio, source exchange
- **OI change:** 📈/📉 current OI, previous OI, % change, current price, source
- **Funding extreme:** 🔥/🧊 current rate, previous rate, source
- **Liquidation spike:** 💥 total liquidated in USD, direction (longs/shorts)
- **Twitter signal:** 🐦 @handle, post text (truncated 280 chars), link
- **Combo:** ⚡️ list of active signal names + individual details

---

## 5. Current Data Sources (Active)

These are working now in free test mode. No API key required.

| Source | Endpoint | Used For | Status |
|---|---|---|---|
| Binance Spot | `api.binance.com/api/v3/ticker/price` | ASTER price | ✅ Active |
| Binance Spot | `api.binance.com/api/v3/ticker/24hr` | 24h volume, high/low | ✅ Active |
| Binance Spot | `api.binance.com/api/v3/klines` | Candles, volume avg | ✅ Active |
| Binance Futures | `fapi.binance.com/fapi/v1/premiumIndex` | Funding rate | ✅ Active (if perp exists) |
| Binance Futures | `fapi.binance.com/fapi/v1/openInterest` | OI | ✅ Active (if perp exists) |
| Binance Futures | `fapi.binance.com/fapi/v1/fundingRate` | Funding history | ✅ Active (if perp exists) |
| Binance Futures | `fapi.binance.com/fapi/v1/klines` | Futures candles | ✅ Active (if perp exists) |
| Telegram Bot API | `api.telegram.org/bot{TOKEN}/sendMessage` | Sending alerts | ✅ Active |

---

## 6. Future Data Sources (Roadmap)

### 6.1 CoinGlass — Aggregated Futures Data
**Priority: High. Implement in Phase 3.**

CoinGlass aggregates futures data across all major exchanges (Binance, Bybit, OKX, Gate, Huobi). This is far superior to Binance-only data because ASTER futures may be more liquid on other exchanges.

**What CoinGlass provides:**
- Total Open Interest across all exchanges (not just Binance)
- Funding rates from every exchange in one call
- Liquidation data (long and short liquidations in real-time)
- Long/Short ratio (retail sentiment)
- Volume aggregated across exchanges

**API:**
- Base URL: `https://open-api.coinglass.com/public/v2/`
- Auth: `coinglassSecret` header (API key required)
- Free tier: available, rate-limited
- Docs: `https://coinglass.com/pricing` → Free tier includes basic endpoints
- Register: `https://www.coinglass.com/pricing`

**Key endpoints to implement:**
```
GET /open-api/futures/openInterest/chart?symbol=ASTER&interval=h1
GET /open-api/futures/fundingRate?symbol=ASTER
GET /open-api/futures/liquidation/chart?symbol=ASTER&interval=h1
GET /open-api/futures/longShortRatio?symbol=ASTER&interval=h1
```
**TODO:** Verify exact endpoint paths once CoinGlass API key is obtained. Endpoint structure may differ between v1/v2/v3 — always check official docs before implementing.

**New env var needed:** `COINGLASS_API_KEY`

**New monitor to create:** `monitors/coinglass.py`

---

### 6.2 CoinGecko — Reference Price & Market Cap
**Priority: Medium. Implement in Phase 3.**

CoinGecko provides reference price data independent of any single exchange, plus market cap and global volume — useful for cross-checking Binance price and detecting market cap manipulation signals.

**What to use:**
- Reference price (aggregate of all exchanges)
- Market cap
- Total global 24h volume (all exchanges combined)
- Price change 1h / 24h / 7d

**API:**
- Base URL: `https://api.coingecko.com/api/v3/`
- Free tier: 30 calls/min, no API key required for basic endpoints
- Docs: `https://www.coingecko.com/en/api/documentation`

**Key endpoint:**
```
GET https://api.coingecko.com/api/v3/simple/price
    ?ids=aster-network&vs_currencies=usd
    &include_market_cap=true&include_24hr_vol=true&include_24hr_change=true
```
**TODO:** Confirm CoinGecko coin ID for ASTER. Could be `aster-network`, `aster`, or another slug. Verify at `https://www.coingecko.com/en/coins/aster-network` before implementing.

**Free tier:** No key needed. Add `COINGECKO_API_KEY` to `.env` only if upgrading to Pro for higher rate limits.

**Use in bot:** Cross-reference Binance price. If CoinGecko price diverges > 2% from Binance, that could indicate exchange-specific manipulation or thin liquidity — weak signal.

**New monitor addition:** Add CoinGecko price as secondary source in `monitors/price.py`.

---

### 6.3 CoinMarketCap — Alternative Reference
**Priority: Low. Only if CoinGecko is insufficient.**

CoinMarketCap is an alternative to CoinGecko for reference price and market data. Requires a free API key.

**API:**
- Base URL: `https://pro-api.coinmarketcap.com/v1/`
- Free tier: 333 calls/day (basic plan, free)
- Register: `https://pro.coinmarketcap.com/signup`
- Docs: `https://coinmarketcap.com/api/documentation/v1/`

**Key endpoint:**
```
GET /v1/cryptocurrency/quotes/latest?symbol=ASTER&convert=USD
Headers: X-CMC_PRO_API_KEY: {key}
```
**TODO:** Confirm CMC symbol for ASTER (`ASTER` or another ticker). Verify at `https://coinmarketcap.com/currencies/aster-network/`.

**New env var needed:** `CMC_API_KEY`

---

### 6.4 On-Chain Whale Monitoring
**Priority: High (once contract confirmed). Implement in Phase 2.**

See Section 7 (TODOs — Blocked Items) for full details.

**Source options ranked by preference:**
1. **Arkham Intelligence** — labels wallets (exchange, whale, protocol), best for context
   - `https://intel.arkm.com/` — requires account, has free tier
   - Best for: identifying if a whale is a known fund, exchange, or new wallet
2. **Bitquery** — GraphQL API for on-chain transfers, supports multiple chains
   - `https://bitquery.io/` — free tier available
   - Best for: flexible queries across chains without knowing the chain upfront
3. **Etherscan** — for Ethereum ERC-20 ASTER transfers
   - `https://api.etherscan.io/` — free tier: 5 req/s
4. **BscScan** — for BSC BEP-20 ASTER transfers
   - `https://api.bscscan.com/` — free tier: 5 req/s
5. **Subscan** — for Astar Network (Polkadot EVM) transfers
   - `https://astar.api.subscan.io/` — free tier available

---

## 7. TODOs — Blocked Items

### 7.1 Whale Monitor — BLOCKED
**What's needed before activating:**
1. Confirm which blockchain ASTER token lives on
   - Ethereum mainnet?
   - BNB Smart Chain (BSC)?
   - Astar Network (Polkadot EVM)?
   - Multiple chains?
2. Find the official ASTER token contract address from official sources only
3. Add `ASTER_CONTRACT_ADDRESS` to `.env`
4. Add the corresponding API key (`ETHERSCAN_API_KEY`, `BSCSCAN_API_KEY`, or `SUBSCAN_API_KEY`)
5. Populate known exchange hot-wallet addresses in `monitors/whale.py` → `EXCHANGE_LABELS`

**Where to confirm:**
- Official Aster website / docs
- CoinMarketCap or CoinGecko token page (check contract address listed there)
- Official Telegram or Discord announcement

### 7.2 Twitter @handle — BLOCKED
- Official Aster Network Twitter handle needs confirmation
- Currently placeholder `"AsterNetwork"` in `config.py` and `monitors/twitter.py`

### 7.3 Binance Futures Availability — UNCERTAIN
- Uncertain if ASTERUSDT perpetual futures are listed on Binance
- Bot handles this gracefully (skips with warning if 400 error)
- If not on Binance, CoinGlass (Phase 3) will cover futures data from other exchanges

### 7.4 CoinGecko Coin ID — NEEDS VERIFICATION
- Must confirm the exact CoinGecko slug for ASTER before implementing
- Do not guess — wrong slug returns wrong price silently

### 7.5 CoinGlass Symbol — NEEDS VERIFICATION
- Must confirm CoinGlass uses `ASTER` as the symbol (not `ASTERUSDT` or another variant)
- Check after obtaining API key

---

## 8. Twitter / X — Free Alternatives

Paid Twitter API ($100/mo Basic tier) is disabled. Free options ranked:

### Option A — Nitter RSS (Recommended)
- Parse `https://nitter.net/{username}/rss` per account
- No API key, ~5 min delay, free
- Requires `pip install feedparser`
- Backup instances: `nitter.poast.org`, `nitter.privacydev.net`
- Self-host on Railway for reliability: `github.com/zedeus/nitter`
- Implementation skeleton in `monitors/twitter.py` — uncomment to activate

### Option B — Telegram Channels
- Many crypto accounts cross-post to Telegram
- Monitor via Bot API `getUpdates` on channels
- Suggested channels: `@binance`, `@whale_alert_io`, `@cryptoquant_alerts`
- Add Aster official channel once confirmed

### Option C — CryptoPanic RSS
- `https://cryptopanic.com/news/aster/rss/`
- News aggregator, no API key, covers ASTER news broadly

### Option D — Manual
- TweetDeck or Twitter lists for manual watching during test phase
- Zero cost, zero code

---

## 9. Polling Intervals

| Monitor | Default | Configurable via |
|---|---|---|
| Price | 60s | `POLL_PRICE_SECS` |
| Volume | 60s | `POLL_PRICE_SECS` (same loop) |
| Open Interest | 60s | `POLL_OI_SECS` |
| Funding Rate | 300s (5 min) | `POLL_FUNDING_SECS` |
| Whale Transfers | 120s | `POLL_WHALE_SECS` |
| Twitter / RSS | 300s (5 min) | `POLL_TWITTER_SECS` |
| Alert cooldown | 1800s (30 min) | `ALERT_COOLDOWN_SECS` |

---

## 10. Deployment

- **Platform:** Railway (`railway.app`)
- **Start command:** `python main.py`
- **Restart policy:** ON_FAILURE, max 10 retries
- **Environment variables:** set in Railway dashboard — never in code
- **Auto-deploy:** push to `main` branch triggers redeploy

All secrets go in Railway environment variables. `.env` is for local development only. `.env` must never be committed to git (add to `.gitignore`).

---

## 11. Development Phases

### Phase 1 — Free Test Mode ✅ CURRENT
- Binance public API only (no key needed)
- Price, volume, candles, OI, funding (Binance only)
- Alert engine logic validated
- Telegram delivery confirmed
- Run: `python run_test.py`

### Phase 2 — Whale Monitor ⏳ BLOCKED
- Requires: ASTER contract address + blockchain confirmation
- Activate `monitors/whale.py` once unblocked
- Add exchange wallet labels
- Recommended source: Bitquery or Arkham for cross-chain coverage

### Phase 3 — Multi-Source Market Data ⏳ PLANNED
- Add CoinGlass: aggregated OI, funding, liquidations, long/short ratio
- Add CoinGecko: reference price, market cap, global volume
- Create `monitors/coinglass.py`
- Extend `monitors/price.py` with CoinGecko cross-check
- Bot still runs on Binance free tier as fallback if new sources fail

### Phase 4 — Social Monitoring ⏳ PENDING
- Start with Nitter RSS (free, no key)
- Add CryptoPanic RSS for news
- Upgrade to paid Twitter API only if RSS proves unreliable

### Phase 5 — Full Production
- Deploy to Railway with all monitors active
- Tune thresholds based on 2–4 weeks of real signal data
- Add persistence (Redis or SQLite) so state survives restarts
- Add `/status` Telegram command to query bot health

---

## 12. File Map

| File | Purpose |
|---|---|
| `main.py` | Entry point, asyncio loop, all monitors |
| `config.py` | All thresholds and env var loading |
| `state.py` | Shared in-memory state (prices, history, seen IDs) |
| `alert_engine.py` | Signal aggregation, strong/weak logic, cooldown |
| `telegram_bot.py` | `send_alert()` + all message formatters |
| `monitors/price.py` | Price action, breakout/breakdown, key levels |
| `monitors/volume.py` | Volume spike detection vs rolling average |
| `monitors/open_interest.py` | OI tracking and spike detection (Binance) |
| `monitors/funding.py` | Funding rate extremes and sudden changes (Binance) |
| `monitors/whale.py` | On-chain large transfer detection (TODO) |
| `monitors/twitter.py` | Social monitoring (disabled, free alternatives documented) |
| `monitors/coinglass.py` | **TODO Phase 3** — CoinGlass aggregated futures data |
| `run_test.py` | Standalone free test mode — no paid APIs |
| `tests/test_alert_engine.py` | Unit tests for alert logic |
| `.env.example` | All required env var names with descriptions |
| `railway.toml` | Railway deployment config |
| `PROJECT_SPECIFICATION.md` | This file |

---

## 13. Environment Variables — Full Reference

| Variable | Required | Default | Description |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ Yes | — | Telegram bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | ✅ Yes | — | Your Telegram chat/user ID |
| `SYMBOL_SPOT` | No | `ASTERUSDT` | Binance spot symbol |
| `SYMBOL_FUTURES` | No | `ASTERUSDT` | Binance futures symbol |
| `WHALE_THRESHOLD_ASTER` | No | `20000000` | Min ASTER amount for whale alert |
| `VOLUME_SPIKE_MULTIPLIER` | No | `3.0` | Alert if vol > N× 1h average |
| `OI_CHANGE_PCT_THRESHOLD` | No | `10.0` | Alert if OI changes by N% |
| `FUNDING_EXTREME_PCT` | No | `0.05` | Alert if funding ≥ ±N% per 8h |
| `PRICE_BREAKOUT_PCT` | No | `3.0` | Alert if price moves N% through level |
| `ALERT_COOLDOWN_SECS` | No | `1800` | Min seconds between same signal type |
| `POLL_PRICE_SECS` | No | `60` | Price polling interval |
| `POLL_OI_SECS` | No | `60` | OI polling interval |
| `POLL_FUNDING_SECS` | No | `300` | Funding rate polling interval |
| `POLL_WHALE_SECS` | No | `120` | Whale monitor polling interval |
| `POLL_TWITTER_SECS` | No | `300` | Twitter/RSS polling interval |
| `ASTER_CONTRACT_ADDRESS` | No | — | **TODO Phase 2** — ASTER token contract |
| `ETHERSCAN_API_KEY` | No | — | **TODO Phase 2** — Ethereum on-chain |
| `BSCSCAN_API_KEY` | No | — | **TODO Phase 2** — BSC on-chain |
| `SUBSCAN_API_KEY` | No | — | **TODO Phase 2** — Astar Network on-chain |
| `COINGLASS_API_KEY` | No | — | **TODO Phase 3** — CoinGlass futures data |
| `CMC_API_KEY` | No | — | **TODO Phase 3** — CoinMarketCap (optional) |
| `TWITTER_BEARER_TOKEN` | No | — | **TODO Phase 4** — Twitter API v2 |
| `BINANCE_API_KEY` | No | — | Future: private Binance endpoints |
| `BINANCE_SECRET` | No | — | Future: private Binance endpoints |
