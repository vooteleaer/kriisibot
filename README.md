# Kriisibot

An AI-powered crisis information bot for [MeshCore](https://github.com/ripplebiz/MeshCore) mesh radio networks. Designed for the Estonian emergency communications context, it monitors official crisis feeds and answers user questions over radio — even when internet infrastructure is degraded.

## Features

- **Monitors official sources** — polls the [eesti.ee crisis API](https://api.app.eesti.ee/api/sitrep/v1/full-events) and configurable RSS feeds for new events
- **Weather warnings** — fetches active warnings from the Estonian Weather Service
- **AI-powered Q&A** — users on the `#kriis` channel mention `@[Kriisibot]` to ask questions; Claude answers using current event data
- **Private report intake** — users send a PM to the bot to report field observations; Claude gathers details through a multi-turn conversation and broadcasts accepted reports to `#kriis`
- **Targeted alerts** — for life-threatening events (air raid, drone threat, chemical hazard), sends direct PM to companion nodes in the affected area
- **Event database** — all events are classified, deduplicated, and stored in a local SQLite database; known events are never re-processed
- **Flood advertisement** — bot advertises itself on the mesh every hour so it can be discovered by new nodes
- **Multilingual** — responds in the same language the user writes in

## Requirements

- Python 3.10+
- MeshCore companion radio connected via USB serial
- [Anthropic API key](https://console.anthropic.com/)

## Installation

```bash
git clone https://github.com/yourname/kriisibot
cd kriisibot
pip install -r requirements.txt
cp .env.example .env
# Edit .env and add your ANTHROPIC_API_KEY
```

## Configuration

All non-secret settings live in `settings.yaml`. The only secret needed is the Anthropic API key in `.env`.

### Key settings

```yaml
meshcore:
  port: /dev/ttyUSB0        # Serial port of the companion radio
  channel: "#kriis"          # Channel to listen and broadcast on
  bot_mention: "@[Kriisibot]" # How users mention the bot on the channel
  advert_interval_seconds: 3600

eesti_ee:
  enabled: true
  poll_interval_seconds: 300  # How often to check for new crisis events

weather:
  enabled: true
  poll_interval_seconds: 600

rss_feeds:
  - url: https://www.err.ee/rss/uudised
    name: "ERR uudised"
    enabled: true

claude:
  model: claude-haiku-4-5-20251001
  max_response_chars: 140     # Per-sentence character limit (MeshCore channel limit is 143)

event_taxonomy:               # Event classification categories
  - hostile_drone
  - fallen_tree
  - wildfire
  - flood
  - storm
  - extreme_weather
  - power_outage
  - road_blocked
  - chemical_hazard
  - air_raid
  - explosion
  - missing_person
  - civil_unrest
  - other

targeted_alerts:
  enabled: true
  radius_km: 50               # Send PM to companions within this radius
  critical_event_types:       # These event types trigger targeted PMs
    - air_raid
    - hostile_drone
    - chemical_hazard
    - explosion

user_reports:
  report_trigger: "!raport"   # Keyword to start a report via PM (channel redirects to PM)
  cooldown_seconds: 600
  session_timeout_minutes: 30
```

## Running

```bash
python main.py
```

Expected startup output:
```
Event database initialized at events.db
Connecting to MeshCore on /dev/ttyUSB0 (attempt 1/10)...
Connected to MeshCore
Found channel '#kriis' at index 1
Flood advert sent
Listening on channel '#kriis' (index 1) and PMs
Kriisibot running — channel '#kriis' on /dev/ttyUSB0 (Ctrl+C to stop)
Crisis fetcher started (polling every 300s)
RSS fetcher started (1 feeds, polling every 300s)
Weather fetcher started (polling every 600s)
```

Press **Ctrl+C** for a clean shutdown (releases the serial port).

### Systemd service (Raspberry Pi)

```ini
[Unit]
Description=Kriisibot MeshCore crisis info bot
After=network.target

[Service]
WorkingDirectory=/home/pi/kriisibot
ExecStart=/usr/bin/python3 main.py
Restart=on-failure
RestartSec=10
EnvironmentFile=/home/pi/kriisibot/.env

[Install]
WantedBy=multi-user.target
```

Save as `/etc/systemd/system/kriisibot.service`, then:
```bash
systemctl enable kriisibot
systemctl start kriisibot
```

## Usage

### Asking questions on `#kriis`

Mention the bot to get a response:
```
@[Kriisibot] kas on aktiivseid drooniohtusid?
```

The bot ignores all messages that don't mention it, so users can talk to each other normally.

### Reporting an event via PM

Send a private message to the Kriisibot node — no trigger word needed:
```
Puu on kukkunud Tartu Riia tänavale, tee blokeeritud
```

The bot will ask follow-up questions (location, status, details) until it has enough information, then broadcast a sanitised `[KONTROLLIMATA]` summary to `#kriis`. Personal details (names, phone numbers, injuries of specific individuals) are removed before broadcasting.

### Utility scripts

```bash
# List all channels configured on the companion radio
python list_channels.py
```

## Architecture

```
main.py                 Startup, event loop, message routing
├── meshcore_client.py  MeshCore serial connection, channel/PM send & receive
├── claude_client.py    All Claude API calls (classify, answer, alert, plausibility)
├── event_db.py         SQLite event store with deduplication
├── crisis_fetcher.py   Polls eesti.ee crisis API
├── rss_fetcher.py      Polls RSS/Atom feeds
├── weather_fetcher.py  Polls Estonian Weather Service warnings
├── user_reports.py     PM-based report intake with rate limiting
├── conversation.py     Per-user conversation history for multi-turn Q&A
├── node_tracker.py     Tracks companion node positions from MeshCore adverts
└── config.py           Loads settings.yaml + .env
```

### Data flow

```
eesti.ee API ──┐
RSS feeds ─────┼──► event_db (classify once, skip known) ──► broadcast to #kriis
Weather API ───┘                                          └──► targeted PM to nearby nodes

#kriis @mention ──► Claude Q&A ──► reply to #kriis
PM to bot ────────► multi-turn report intake ──► plausibility check ──► broadcast to #kriis
```

### Trust levels

| Source | Trust level | Label in answers |
|---|---|---|
| eesti.ee | `official` | (no label) |
| RSS feeds | `media` | (no label) |
| User PM reports | `unverified` | `[KONTROLLIMATA]` |

## Data sources

| Source | URL | Notes |
|---|---|---|
| Estonian crisis events | `https://api.app.eesti.ee/api/sitrep/v1/full-events` | Public JSON API |
| Weather warnings | `https://www.ilmateenistus.ee/ilma_andmed/xml/hoiatus.php` | Public XML feed |
| ERR news (example) | `https://www.err.ee/rss/uudised` | Configurable RSS |
