# Kriisibot

An AI-powered crisis information bot for [MeshCore](https://github.com/ripplebiz/MeshCore) mesh radio networks. Designed for the Estonian emergency communications context, it monitors official crisis feeds and answers user questions over radio — even when internet infrastructure is degraded.

## Features

- **Monitors official sources** — polls the [eesti.ee crisis API](https://api.app.eesti.ee/api/sitrep/v1/full-events) and configurable RSS feeds for new events
- **Weather warnings** — fetches active warnings from the [Estonian Weather Service](https://www.ilmateenistus.ee)
- **AI-powered Q&A** — users mention `@[Kriisibot]` on the `#kriis` channel; Claude answers using current event data
- **Private report intake** — users PM the bot to report field observations; Claude gathers details through a multi-turn conversation, geocodes the location, and broadcasts a sanitised summary to `#kriis`
- **Address geocoding** — reported locations are validated and resolved to precise coordinates using the [Estonian Land Board In-ADS API](https://inaadress.maaamet.ee); vague descriptions are accepted gracefully
- **Targeted emergency alerts** — for life-threatening events (air raid, drone threat, chemical hazard, explosion), sends direct PM to all companion nodes within a configurable radius of the event
- **Event database** — all events are classified into a configurable taxonomy, deduplicated, and stored in a local SQLite database; known events are never re-processed by the LLM
- **Flood advertisement** — bot advertises itself on the mesh hourly so new nodes can discover it
- **Multilingual** — responds in whatever language the user writes in

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
# Edit .env and set ANTHROPIC_API_KEY
```

## Configuration

All non-secret settings live in `settings.yaml`. The only secret is the Anthropic API key in `.env`.

```yaml
meshcore:
  port: /dev/ttyUSB0         # Serial port of the companion radio
  channel: "#kriis"           # Channel to listen and broadcast on
  bot_mention: "@[Kriisibot]" # How users address the bot on the channel
  advert_interval_seconds: 3600

eesti_ee:
  enabled: true
  poll_interval_seconds: 300

weather:
  enabled: true
  poll_interval_seconds: 600

rss_feeds:
  - url: https://www.err.ee/rss/uudised
    name: "ERR uudised"
    enabled: true

claude:
  model: claude-haiku-4-5-20251001
  max_response_chars: 140     # Per-sentence limit; MeshCore channel max is 143 chars

event_taxonomy:               # Classification categories — extend as needed
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
  radius_km: 50               # PM companion nodes within this radius of the event
  critical_event_types:
    - air_raid
    - hostile_drone
    - chemical_hazard
    - explosion

user_reports:
  report_trigger: "!raport"   # Triggers a PM redirect on the channel; PM itself needs no trigger
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

Press **Ctrl+C** for a clean shutdown that releases the serial port.

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

```bash
sudo cp kriisibot.service /etc/systemd/system/
sudo systemctl enable kriisibot
sudo systemctl start kriisibot
```

## Usage

### Asking questions on `#kriis`

Mention the bot to get an answer:
```
@[Kriisibot] kas on aktiivseid drooniohtusid?
```

The bot ignores all other messages so users can talk to each other normally.
Multi-turn conversation is supported — follow-up questions work within a 15-minute window.

### Reporting an event via PM

Send a private message to the Kriisibot node — no trigger word needed, just describe what you see:
```
Puu on kukkunud Tartu Riia tänavale
```

The bot asks follow-up questions to gather location, status, and details.
If you don't know the exact address, a rough description works:
```
Kusagil Annelinna poole, lähedal on Ülenurme tee
```

The bot resolves the location using the Estonian Land Board geocoding API when possible,
falls back to the text description otherwise.

Once enough information is collected, a sanitised `[KONTROLLIMATA]` summary is broadcast to `#kriis`.
Personal details (names, phone numbers, individual injury descriptions) are removed before broadcasting.

For life-threatening event types, all companion nodes within the configured radius also receive a direct PM alert.

### Utility scripts

```bash
# List all channels configured on the companion radio
python list_channels.py
```

## Architecture

```
main.py                 Startup, event loop, message routing
├── meshcore_client.py  MeshCore serial connection, channel/PM send & receive, node adverts
├── claude_client.py    All Claude API calls (classify, answer, alert, plausibility check)
├── event_db.py         SQLite event store — classify once, deduplicate, skip known events
├── crisis_fetcher.py   Polls eesti.ee crisis API
├── rss_fetcher.py      Polls RSS/Atom feeds
├── weather_fetcher.py  Polls Estonian Weather Service XML warnings
├── user_reports.py     PM-based multi-turn report intake with rate limiting
├── geocoder.py         Estonian Land Board In-ADS geocoding for user-reported locations
├── conversation.py     Per-user rolling conversation history for multi-turn Q&A
├── node_tracker.py     Tracks companion node positions from MeshCore advertisements
└── config.py           Loads settings.yaml + .env
```

### Data flow

```
eesti.ee API ──┐
RSS feeds ─────┼──► event_db (classify once, skip known) ──► broadcast to #kriis
Weather API ───┘                                          └──► targeted PM to nearby companions

#kriis @mention ──► Claude Q&A (with active events as context) ──► reply to #kriis
PM to bot ────────► multi-turn intake ──► geocode ──► plausibility check ──► [KONTROLLIMATA] to #kriis
                                                                          └──► targeted PM if critical
```

### Trust levels

| Source | Trust level | Label in answers |
|---|---|---|
| eesti.ee | `official` | (none) |
| Weather service | `official` | (none) |
| RSS feeds | `media` | (none) |
| User PM reports | `unverified` | `[KONTROLLIMATA]` |

## Data sources

| Source | URL | Auth |
|---|---|---|
| Estonian crisis events | `https://api.app.eesti.ee/api/sitrep/v1/full-events` | None |
| Weather warnings | `https://www.ilmateenistus.ee/ilma_andmed/xml/hoiatus.php` | None |
| Address geocoding | `https://inaadress.maaamet.ee/inaadress/gazetteer` | None |
| RSS feeds | Configurable in `settings.yaml` | None |
