# Yamaja WhatsApp Chatbot

Render webhook server powering the Claudia WhatsApp chatbot for Yamaja Engines Ltd.

## Architecture

```
Customer WhatsApp → Interakt → Render (this server) → Make.com → Email + WhatsApp to sales
Website forms → Render → Proactive WhatsApp → Make.com
```

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/webhook` | Interakt incoming WhatsApp messages |
| GET | `/webhook` | Health check / verification |
| POST | `/webhook/website` | Website contact form submissions |
| GET | `/leads` | View all captured leads |
| GET | `/leads/<phone>` | View single conversation |
| GET | `/health` | Server health status |

## Environment Variables

Set these in Render dashboard → Environment:

| Variable | Description |
|----------|-------------|
| `INTERAKT_API_KEY` | Your Interakt API key (Settings → Developer → API Key) |
| `MAKE_WEBHOOK_URL` | Your Make.com scenario webhook URL |

## Deploy to Render

1. Push this repo to GitHub
2. Go to [render.com](https://render.com) → New → Web Service
3. Connect your GitHub repo
4. Render auto-detects `render.yaml` and configures everything
5. Add environment variables in the Render dashboard
6. Deploy

## Local Development

```bash
pip install -r requirements.txt
export INTERAKT_API_KEY="your_key"
export MAKE_WEBHOOK_URL="your_make_webhook_url"
python yamaja_cold_intake.py
```

Server starts on port 10000 by default.
