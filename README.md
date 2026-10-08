# BTC Market Intelligence Bot

A Telegram bot that watches Bitcoin and tells you, in plain English, what the market is doing
and whether there is a trade setup (LONG / SHORT / WAIT). It also tracks every signal until it
hits stop, TP1 or TP2 and keeps an honest track record.

**It never places orders. Information only. Any trade can lose money.**

## What you get in Telegram
- **Headline card** every 30 min: verdict, one-line reason, setup strength (stars), trend on 1d/4h/1h/15m,
  momentum, crowd positioning, macro, key levels, next big event, "what changed since last update".
- **Tabs** that edit the same message: Trend · Futures & Options · Macro · News · Levels · Stats · Glossary.
- **Trade alerts** with limit entry, stop, TP1/TP2, position size and required leverage, then follow-ups
  (fill, TP1 → breakeven, trailing stop, exit). A signal must hold for several checks before it is sent.
- **Event alerts** 60/15/5 min before big US data (CPI, FOMC, jobs...) and again when the result is out,
  plus how BTC reacted after the previous releases (the bot builds this history itself).
- **Breaking news** (de-duplicated, whole-word matching, optional Claude filter).

## Commands
`/report` `/status` `/trade` `/stats` `/chart` `/glossary` `/health` `/export`
`/account 500` `/risk 1` `/pause` `/resume` `/lang en|simple|ur`

## Setup
1. Create a bot with @BotFather, get the token; send it a message and get your chat id.
2. In GitHub: **Settings → Secrets and variables → Actions** add `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`
   (and optionally `ANTHROPIC_API_KEY`).
3. **Actions → BTC Market Bot → Run workflow**. It then runs hourly; sessions queue back-to-back.
4. Locally: `pip install -r requirements.txt && python bot.py` (see `.env.example`).

## Backtest
`python bot.py --backtest 180` or **Actions → Backtest**. Results include fees, slippage, a buy-and-hold
comparison, monthly results and a first-half / second-half split. The backtest uses only technical rules
(no news/macro history), so live signals can differ - every live signal is logged with all its inputs in
`btc_signal_log.jsonl` (download with `/export`) so the real system can be evaluated later.
**Forward-test for 100+ trades before trusting any number.**

## Tests
`pip install -r requirements-dev.txt && pytest -q`

## Known limits
- GitHub Actions cron can be delayed, and scheduled workflows pause after 60 days without repo activity.
  For true 24/7 use a small always-on server.
- Free data sources can be blocked from GitHub's US servers; `/health` shows which ones work.
- Spot ETF flows are not included (no reliable free API).
