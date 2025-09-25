# Telegram Tech+AI News Bot (aiogram) — Starter

A production-friendly starter for a Telegram bot that delivers Tech/AI/IoT/Robotics news and handy code snippets.

## Quickstart (local)
```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# Put your token in .env
python bot.py
```

## Docker (VPS)
```bash
sudo apt update && sudo apt install -y docker.io docker-compose-plugin
mkdir -p data
sudo docker compose up -d --build
sudo docker compose logs -f
```

## Commands
- /start, /help, /news, /ai_news, /iot_news, /code
- Use inline buttons for quick navigation
