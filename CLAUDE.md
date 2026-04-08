# SportyBot — Architecture Guide

SportyBot is a Telegram-based sports betting analysis bot that scores football matches, builds optimal bet slips, and generates SportyBet booking codes.

## Quick Start

```bash
cd /Users/0x/Documents/Playground/SportyBot
pip install -r requirements.txt
cp .env.example .env
# Edit .env: add TELEGRAM_BOT_TOKEN, API_FOOTBALL_KEY, FOOTBALL_DATA_KEY
python main.py
```

## Architecture

```
main.py → bot/telegram_bot.py (Application setup, health, error handling)
              │
              ├── bot/handlers/pick.py      — /pick flow (type → count → odds → analyze → review → book)
              ├── bot/handlers/check.py     — /check flow (paste codes → review → target odds → combo)
              ├── bot/handlers/strategy.py  — /settings flow (confidence, markets, leagues, timeframe)
              ├── bot/handlers/split.py     — /split flow (paste code → split into smaller tickets)
              ├── bot/handlers/sort.py      — /sort flow (paste code → reorder by kickoff/league)
              ├── bot/formatters.py         — Message formatting (picks, combos, reviews)
              └── bot/keyboards.py          — InlineKeyboard builders (league toggles)
              │
              ├── core/config.py            — API keys, league IDs, market mappings, strategy presets
              ├── core/scorer.py            — Match scoring engine (20+ markets, Poisson model)
              │
              ├── data/data_collector.py    — API-Football v3 client (fixtures, form, H2H, stats, caching)
              ├── data/web_analyzer.py      — football-data.org client (form analysis, team results)
              ├── data/sportybet_events.py  — SportyBet event matching + booking code creation
              └── data/booking_service.py   — SportyBet booking code fetching + outcome parsing
              │
              ├── services/analyzer.py      — Match scoring + combo building
              ├── services/analysis_service.py — Fixture scoring pipeline with extended markets
              ├── services/ticket_engine.py — Pool building, ticket generation, qualification logic
              ├── services/ticket_splitter.py — Ticket splitting algorithms
              ├── services/chat_agent.py    — Gemini function-calling conversational AI
              └── services/gemini_chat.py   — Intent parsing via OpenRouter
```

## Telegram Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message and current config |
| `/pick` | Guided pick flow (single or multi-ticket bundles) |
| `/check` | Analyze SportyBet booking codes |
| `/split` | Split a large ticket into smaller ones |
| `/sort` | Reorder booking code by kickoff time or league |
| `/chat` | AI chat mode (natural language) |
| `/settings` | Leagues, timeframe, confidence, markets |
| `/budget` | API-Football calls remaining today |
| `/status` | Current bot configuration |
| `/health` | Runtime health and error history |

Natural language is also supported — messages not caught by commands go through Gemini intent parsing.

## API Integrations

| API | Purpose | Limits | Env Var |
|-----|---------|--------|---------|
| API-Football v3 | Fixtures, form, H2H, stats | 100 req/day, 10 req/min | `API_FOOTBALL_KEY` |
| football-data.org | Fixtures lookahead, team results | 10 req/min, no daily cap | `FOOTBALL_DATA_KEY` |
| SportyBet Nigeria | Event matching, booking codes | No documented limits | `SPORTYBET_BASE_URL` |
| OpenRouter | Intent parsing + chat agent | Varies by model | `OPENROUTER_API_KEY` |

All API responses are cached to `.cache/` with 6-hour TTL. Budget tracking persists in `.cache/api_usage.json`.

## Configuration

User preferences stored in `.config/user_{chat_id}.json`:
- `leagues` — active league IDs (default: top 5 European)
- `min_confidence` — minimum score 0-100 (default: 70)
- `min_odds` — minimum odds per pick (default: 1.15)
- `enabled_markets` — toggled market types (13 markets default)
- `timeframe` — lookahead window (default: 14 days)

## Bot Modes

Set `BOT_MODE` in `.env`:
- `polling` — long-polling (default, good for local dev)
- `webhook` — requires `WEBHOOK_URL` and `PORT`
- `auto` — webhook if `WEBHOOK_URL` set, else polling

## Key Scoring Logic

`core/scorer.py` scores matches across 20+ markets using:
- Team form (last N matches, goals scored/conceded)
- Season stats (home/away specific)
- Head-to-head history
- Poisson probability model for over/under
- Optional extra signals (xG, injuries, motivation)

Each market returns confidence 0-100 with human-readable reasons.

## Development Notes

- No test suite yet — priority: scorer.py, ticket_engine.py
- `services/ticket_engine.py` is pure logic (no Telegram imports) — testable
- Rate limiters use `threading.Lock` (consider migrating to `asyncio.Lock`)
- SportyBet API endpoints need reverse-engineering for full booking automation
