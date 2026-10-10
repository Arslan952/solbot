# BTC Market Intelligence Bot (v3.3)

A Telegram bot that watches Bitcoin and the wider crypto market, explains what is happening in plain
English, and sends LONG / SHORT / WAIT setups with a full track record.
**It never places orders. Information only. Any trade can lose money.**

## Ask it things
Just type, or use commands:
- **"why did SOL pump today?"** / `/why SOL` - measured evidence ranked: market vs coin-specific, volume spike,
  open interest + funding (squeeze / liquidation / new shorts), news catalysts, sector move, breakout.
  With `ANTHROPIC_API_KEY` it adds a short plain-English summary. It is a guess backed by data, never a certainty.
- `/coin SOL` - quick technical card for any USDT coin · `/movers` - top gainers/losers (tiny-volume coins hidden)
- **"weekend outlook"** / `/weekend` - what is closed, historical weekend base rates, CME/Nasdaq reopen time,
  base / bullish-break / bearish-break scenarios with levels, what to watch. Auto-posted Friday after the US close
  and Sunday evening.
- Any other market question (with `ANTHROPIC_API_KEY`) is answered from the bot's live data, without buy/sell orders.

## Alerts that matter
- **Pinned messages:** one live trade message that updates itself · "NO-TRADE WINDOW" before big news ·
  daily plan · weekend outlook · data-problem alerts. They are unpinned automatically when done.
- **Priorities:** critical (trade events, stops, data problems) always make a sound. Quiet hours (`/quiet 00:00-07:00`)
  and `/mode important` deliver everything else silently.
- Automatic **pump/dump alerts** (1h move on liquid coins; watchlist coins trigger earlier) with a one-line reason.
- Price alerts: `/alert 70000`, `/alert SOL 200`, `/alert close 1h above 68000`.
- `/watch SOL ETH` watchlist tab.

## Trade alerts
Limit entry, stop, TP1/TP2, size and required leverage (capped; lower cap on weekends). Buttons:
**Why this signal · Chase check · I took it · I skipped** - your own results are tracked separately in `/stats`.
Risk rules: signals must hold several checks, daily loss limit (-3R), max trades per day.

## Setup
1. Create a bot with @BotFather and get the token; send it a message to get your chat id.
2. GitHub → Settings → Secrets and variables → Actions: add `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`
   (optional `ANTHROPIC_API_KEY`).
3. Actions → *BTC Market Bot* → Run workflow. It then runs hourly (sessions queue back-to-back).
4. In Telegram send `/start` - tap your timezone, risk, language and alert style.
5. Locally: `pip install -r requirements.txt && python bot.py` (see `.env.example`).

## Commands
`/report /why /coin /movers /weekend /plan /watch /unwatch /alert /trade /stats /chart /glossary`
`/account /risk /pause /resume /tz /quiet /mode /lang /status /health /export /weekly /start /help`

## Backtest & tests
`python bot.py --backtest 180` or Actions → Backtest (fees, slippage, buy-and-hold, monthly results, half-vs-half).
`pip install -r requirements-dev.txt && pytest -q`. Every live signal is logged with all its inputs
(`/export`) so the real system can be evaluated. **Forward-test 100+ trades before trusting any number.**

## Known limits
- Pinning works in a private chat with the bot; in a group the bot must be an admin.
- Pump alerts need ~1 hour of price history after (re)start; history is saved between sessions.
- Derivatives data for small coins (funding, open interest) may be missing; the explainer says so.
- GitHub Actions cron can be delayed and pauses after 60 days of repo inactivity - use a small always-on server for true 24/7.
- Free data sources can be blocked from GitHub's US servers; `/health` shows what works.
- Spot ETF flows are not included (no reliable free API).
