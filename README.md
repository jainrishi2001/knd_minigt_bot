# Mini GT Product Monitor

Python script to monitor Mini GT product listings on Karz and Dolls and send Telegram alerts for new listings, restocks, and quantity changes.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# then edit .env and fill in:
# TELEGRAM_BOT_TOKEN=your_bot_token_here
# TELEGRAM_CHAT_ID=your_chat_id_here
python monitor.py
```

