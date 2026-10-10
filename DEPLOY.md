# Run the bot 24/7 on your own server (recommended)

GitHub Actions works, but sessions are capped at 6 hours, cron can be delayed, and commands are only answered
while a session is running. On a small always-on machine the scanner can run every few minutes and Telegram
commands answer instantly.

Any of these is enough (1 CPU / 1 GB RAM): Oracle Cloud "Always Free" VM, a $4-6 VPS, a Raspberry Pi at home.

## Option A - Docker (simplest)
```bash
git clone <your-repo> btc-bot && cd btc-bot
cp .env.example .env && nano .env          # fill TELEGRAM_TOKEN, TELEGRAM_CHAT_ID (+ optional ANTHROPIC_API_KEY)
docker compose up -d --build
docker compose logs -f                      # watch it start
```
State and trade history are kept in `./data`. Update later with `git pull && docker compose up -d --build`.

## Option B - plain Python + systemd
```bash
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
```
Create `/etc/systemd/system/btc-bot.service`:
```ini
[Unit]
Description=BTC Market Bot
After=network-online.target

[Service]
WorkingDirectory=/home/YOU/btc-bot
EnvironmentFile=/home/YOU/btc-bot/.env
Environment=RUN_SECONDS=31536000
ExecStart=/home/YOU/btc-bot/venv/bin/python bot.py
Restart=always
RestartSec=10
User=YOU

[Install]
WantedBy=multi-user.target
```
Then: `sudo systemctl enable --now btc-bot` and `journalctl -u btc-bot -f`.

## Important
- Turn the GitHub schedule OFF (disable the "BTC Market Bot" workflow) or two copies will fight over the
  same Telegram token (getUpdates conflict).
- Some exchanges block cloud IP ranges; the bot falls back between Binance and OKX - check `/health`.
- The Docker files were not run in the build environment (no Docker there); if anything fails, send me the log.
