# SportyBot — Claude Code Instructions

SportyBot is a sports betting analysis and booking tool with three stages:

1. **Data Collection** — `data_collector.py` fetches fixtures, team form, H2H, and stats from API-Football v3.
2. **Analysis** — `analyzer.py` scores matches and builds optimal bet slips based on configurable strategies.
3. **Booking** — `booker.py` maps selections to SportyBet (placeholder — needs API reverse-engineering).

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# Edit .env and add your API-Football key
```

## Commands

```bash
# List fixtures for a date
python sportybot.py fixtures --date 2026-03-22 --league 39

# Check a team's recent form (team ID from fixtures output)
python sportybot.py form --team 33

# Head-to-head between two teams
python sportybot.py h2h --team1 33 --team2 34

# League standings
python sportybot.py standings --league premier_league

# Full analysis with a strategy
python sportybot.py analyze --date 2026-03-22 --strategy safe10

# Analyze and generate booking code (once booker is implemented)
python sportybot.py book --date 2026-03-22 --strategy safe10
```

## Built-in Strategies

- `safe5` — 1 straight win + over 1.5s targeting ~5 total odds
- `safe10` — 2 straight wins + over 1.5s targeting ~10 total odds
- `risky20` — 3 straight wins + over 2.5s targeting ~20 total odds
- Custom: pass a path to a JSON strategy file

## League Names / IDs

Use either the numeric ID or a name alias:
- `premier_league` / `epl` = 39
- `la_liga` = 140
- `serie_a` = 135
- `bundesliga` = 78
- `ligue_1` = 61
- `champions_league` / `ucl` = 2
- `nations_league` = 5
- `afcon` = 6, `afcon_qualifiers` = 36
- `international_friendlies` = 10

## Architecture Notes

- All API-Football responses are cached in `.cache/` as JSON with a 6-hour TTL.
- The 100 req/day API limit is respected via caching — avoid clearing the cache unnecessarily.
- `booker.py` has placeholder functions with documented interfaces. SportyBet API endpoints need to be reverse-engineered before implementation.
- `config.py` loads settings from `.env` via python-dotenv.

## Key Files

- `sportybot.py` — CLI entry point (argparse)
- `config.py` — API keys, league IDs, strategy definitions
- `data_collector.py` — API-Football v3 client with caching
- `analyzer.py` — Scoring engine and combo builder
- `booker.py` — SportyBet integration (TODO)
