FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py .
# Always-on mode: run for a year, then the container restarts it (state lives in /data)
ENV RUN_SECONDS=31536000 \
    STATE_FILE=/data/btc_market_state.json \
    TRADE_LOG_CSV=/data/btc_trade_log.csv \
    FEATURE_LOG=/data/btc_signal_log.jsonl \
    PYTHONUNBUFFERED=1
VOLUME /data
CMD ["python", "bot.py"]
