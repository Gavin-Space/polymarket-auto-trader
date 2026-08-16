# ============================================================
# Polymarket Auto-Trader — remote deployment (Docker)
# ============================================================
# Build:   docker build -t polymarket-bot .
# Run:     docker compose up -d
#
# State (bot_state.json, trading_config.json, encrypted credentials,
# logs) lives in /data — mount ./data:/data to persist it.
# ============================================================

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY auto_trader.py strategy_engine.py ./

# Data directory override (state files are persisted to the mounted volume)
ENV POLY_WORKSPACE=/data
VOLUME /data

EXPOSE 5000

CMD ["python", "auto_trader.py"]
