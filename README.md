# BTC Market Intelligence Bot (v3.4)

A Telegram bot that watches Bitcoin and the wider crypto market, explains what is happening in plain
English, finds coins with technical + news setups, and sends LONG / SHORT / WAIT setups with a public track record.
**It never places orders. Information only. Nobody can reliably predict pumps - it shows evidence and keeps score.**

## New in v3.4
- **Chart analysis for any coin and timeframe:** `/analyze SOL 15m` (5m 15m 30m 1h 4h 1d 1w) or just type
  "analyze DOGE on 4h". You get a chart image (candles, EMAs, VWAP, support/resistance zones, swings, volume,
  RSI, scenario lines) plus a written read: trend + strength, higher-timeframe agreement, levels, Fibonacci
  zones, squeeze, divergence, candle patterns, breakout with/without volume, and bullish/bearish scenarios with
  triggers, targets and "wrong if" levels. Timeframe buttons under every analysis.
- **Breakout scanner:** `/scan` or ask "which coin may break out?". Every 15 minutes it scores ~50 liquid coins:
  squeeze release, pressing resistance, volume build-up, strength vs BTC, EMA alignment, higher lows, ADX waking up,
  healthy RSI, 4h trend, open interest rising while price is flat, funding, and news catalysts (listings,
  partnerships, upgrades) - minus penalties (already pumped, overheated, crowded longs, unlock/hack/delisting).
  The best idea is **pinned with its chart**, with "why technically", "why news-wise", confirm/invalid levels and
  risk flags. It is unpinned automatically when invalidated, replaced by a stronger setup, or after 24h.
  An "overheated - pullback risk" list shows coins likely to dump.
- **Scoreboard (`/scanstats`):** every pick is paper-traded (+5% target / -3% stop / 24h) and compared with BTC, so
  you can see whether the scanner has any edge. Judge it after 100+ picks.
- **Listing monitor**, **"approaching resistance/support"** alerts (BTC + watchlist) and **`/digest on 3`** to bundle
  routine alerts into one message.
- Always-on server option: see `DEPLOY.md` (Docker / systemd).

## Everything else
- **Ask it things:** "why did SOL pump today?" (`/why`), `/coin`, `/movers`, "weekend outlook" (`/weekend`),
  any market question (needs `ANTHROPIC_API_KEY`, answered from live data, never "buy now").
- **Pinned messages:** live trade (self-updating), NO-TRADE WINDOW before big news, daily plan, weekend outlook,
  data-problem alerts, Breakout Watch. Critical alerts always make a sound; `/quiet` and `/mode important` silence the rest.
- **Trade alerts** with limit entry, stop, TP1/TP2, size and required leverage (capped), buttons
  (Why this signal · Chase check · I took it · I skipped), daily loss limit, max trades per day, weekend mode.
- Price alerts (`/alert 70000`, `/alert SOL 200`, `/alert close 1h above 68000`), watchlist, timezone, language
  (`/lang ur`), onboarding (`/start`), daily and weekly recaps, `/export`, `/health`.

## Setup
1. Create a bot with @BotFather; send it a message to get your chat id.
2. GitHub → Settings → Secrets and variables → Actions: `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID` (optional `ANTHROPIC_API_KEY`).
3. Actions → *BTC Market Bot* → Run workflow (then hourly, queued back-to-back), **or** run it on a server (`DEPLOY.md`).
4. In Telegram send `/start` and tap your timezone, risk, language and alert style.

## Commands
`/scan /analyze /scanstats /report /why /coin /movers /weekend /plan /watch /unwatch /alert /digest`
`/trade /stats /chart /glossary /account /risk /pause /resume /tz /quiet /mode /lang /status /health /export /weekly /start /help`

## Backtest & tests
`python bot.py --backtest 180` or Actions → Backtest. `pip install -r requirements-dev.txt && pytest -q`.

## Honest limits
- The scanner finds *conditions that often precede* bigger moves. Most hours it finds nothing clean, and many picks fail -
  that is what the scoreboard is for. Small coins can be manipulated: use small size and a stop.
- News is read from headlines (Google News) - it can lag, be rumor-based, or miss a story. The bot says "no catalyst found" instead of inventing one.
- The scanner uses ~100 API calls per run; free sources can rate-limit or be blocked from GitHub's US servers (`/health`).
- On GitHub Actions the scan runs only while a session is active and commands wait during a scan (~1 min); a server is smoother.
- Pinning works in a private chat; in a group the bot must be admin. Spot ETF flows and whale/social data are not included (no free reliable API).
