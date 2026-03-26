# SportyBot Deployment

## Goal
Run exactly one managed bot instance online so Telegram updates are handled reliably and your local machine is no longer the source of truth.

## Runtime Modes
- `BOT_MODE=auto`
  If `WEBHOOK_URL` is set, SportyBot starts in webhook mode.
  Otherwise it falls back to polling mode.
- `BOT_MODE=polling`
  Best for local development and one-off manual runs.
- `BOT_MODE=webhook`
  Best for hosted deployments with a public HTTPS URL.

## Required Environment Variables
- `TELEGRAM_BOT_TOKEN`
- `API_FOOTBALL_KEY`
- `FOOTBALL_DATA_KEY`
- `GEMINI_API_KEY`

## Hosted Deployment Variables
- `BOT_MODE=webhook`
- `WEBHOOK_URL=https://your-app.example.com/telegram`
- `PORT=8080`
- `WEBHOOK_SECRET=optional-shared-secret`
- `LISTEN=0.0.0.0`

## Local Development
1. Create `.env` from `.env.example`
2. Install dependencies:
   `pip install -r requirements.txt`
3. Run locally:
   `python3 telegram_bot.py`

If `WEBHOOK_URL` is not set, local startup uses polling automatically.

## Docker Deployment
1. Build:
   `docker build -t sportybot .`
2. Run:
   `docker run --env-file .env -p 8080:8080 sportybot`

For hosted Docker usage, set:
- `BOT_MODE=webhook`
- `WEBHOOK_URL` to the public HTTPS endpoint for your deployed app

## Platform Guidance
- Railway or Render:
  Use one service only and one replica only.
  Prefer webhook mode if the platform gives you a public HTTPS URL.
- VPS or VM:
  Docker or a process manager both work.
  Webhook mode is still preferred over polling.

## Important Rule
Never run two live bot instances with the same Telegram token.

That includes:
- local polling plus deployed polling
- local polling plus deployed webhook
- two deployed replicas

If you want a staging bot, create a second Telegram bot token.
