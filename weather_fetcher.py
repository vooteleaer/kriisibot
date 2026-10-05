import asyncio
import hashlib
import logging
from datetime import datetime, timezone
from typing import Callable, Awaitable, Optional
from zoneinfo import ZoneInfo

import httpx

from event_db import Event, EventDB

logger = logging.getLogger(__name__)

# Meteoalarm carries Ilmateenistus' official warnings with a graded awareness level,
# unlike hoiatus.php which lists every (mostly yellow, mostly marine) warning ungraded.
WARNINGS_URL = "https://feeds.meteoalarm.org/api/v1/warnings/feeds-estonia"
HEADERS = {"User-Agent": "kriisibot/1.0"}
LOCAL_TZ = ZoneInfo("Europe/Tallinn")

# awareness_level: 1 green, 2 yellow, 3 orange, 4 red
_LEVEL_LABEL = {3: "ORANŽ HOIATUS", 4: "PUNANE HOIATUS"}

# awareness_type number -> event taxonomy
_TYPE_TAXONOMY = {
    1: "storm",            # Wind
    2: "extreme_weather",  # Snow-ice
    3: "storm",            # Thunderstorm
    4: "extreme_weather",  # Fog
    5: "extreme_weather",  # High temperature
    6: "extreme_weather",  # Low temperature
    7: "flood",            # Coastal event
    8: "wildfire",         # Forest fire
    9: "extreme_weather",  # Avalanches
    10: "flood",           # Rain
    12: "flood",           # Flooding
    13: "flood",           # Rain-flood
}


def _leading_int(value: str) -> int:
    """'3; orange; Severe' -> 3"""
    try:
        return int(value.split(";", 1)[0].strip())
    except (ValueError, AttributeError):
        return 0


def _parse_time(value: str | None) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _short_area(area: str) -> str:
    return area.removesuffix(" maakond")


def _format_areas(areas: list[str]) -> str:
    counties = [_short_area(a) for a in areas if a.endswith(" maakond")]
    others = [a for a in areas if not a.endswith(" maakond")]
    parts = []
    if counties:
        parts.append(", ".join(counties) + " mk")
    parts.extend(others)
    return ", ".join(parts)


def _format_period(onset: Optional[datetime], expires: Optional[datetime]) -> str:
    if not onset or not expires:
        return ""
    a = onset.astimezone(LOCAL_TZ)
    b = expires.astimezone(LOCAL_TZ)
    end = b.strftime("%H:%M") if a.date() == b.date() else b.strftime("%d.%m %H:%M")
    return f"{a.strftime('%d.%m %H:%M')}–{end}"


def _parse_warnings(data: dict) -> list[dict]:
    warnings = []
    for w in data.get("warnings", []):
        for info in w.get("alert", {}).get("info", []):
            if info.get("language") != "et-EE":
                continue
            params = {p.get("valueName"): p.get("value", "") for p in info.get("parameter", [])}
            areas = sorted({a.get("areaDesc", "").strip() for a in info.get("area", []) if a.get("areaDesc")})
            warnings.append({
                "level": _leading_int(params.get("awareness_level", "")),
                "type": _leading_int(params.get("awareness_type", "")),
                "event": (info.get("event") or "").strip(),
                "description": (info.get("description") or "").strip(),
                "onset": _parse_time(info.get("onset")),
                "expires": _parse_time(info.get("expires")),
                "areas": areas,
            })
    return warnings


def _warning_id(w: dict) -> str:
    # Updates that don't change type, level, areas or onset keep the same id,
    # so a reissued warning isn't broadcast again.
    onset = w["onset"].isoformat() if w["onset"] else ""
    key = f"{w['type']}:{w['level']}:{'|'.join(w['areas'])}:{onset}"
    return f"weather:{hashlib.md5(key.encode()).hexdigest()[:12]}"


class WeatherFetcher:
    def __init__(
        self,
        poll_interval: int,
        db: EventDB,
        on_new_events: Optional[Callable[[list[Event]], Awaitable[None]]] = None,
        min_level: int = 3,
    ):
        self._interval = poll_interval
        self._db = db
        self._on_new_events = on_new_events
        self._min_level = min_level
        self._active_ids: set[str] = set()

    async def _fetch(self) -> list[dict]:
        async with httpx.AsyncClient(timeout=30, headers=HEADERS) as client:
            resp = await client.get(WARNINGS_URL)
            resp.raise_for_status()
            return _parse_warnings(resp.json())

    def _to_event(self, w: dict) -> Event:
        hazard = w["event"].split(" Tase ")[0] or "Ilmahoiatus"
        area_text = _format_areas(w["areas"])
        title = f"{_LEVEL_LABEL.get(w['level'], 'HOIATUS')}: {hazard}" + (f" — {area_text}" if area_text else "")
        period = _format_period(w["onset"], w["expires"])
        description = " ".join(p for p in [w["description"], period] if p)[:500] or None
        now = datetime.now(timezone.utc).isoformat()
        return Event(
            id=_warning_id(w),
            source="weather",
            trust_level="official",
            event_type=_TYPE_TAXONOMY.get(w["type"], "extreme_weather"),
            title=title,
            description=description,
            location=", ".join(w["areas"]) or None,
            lat=None,
            lon=None,
            status="OPEN",
            start_time=w["onset"].isoformat() if w["onset"] else now,
            end_time=w["expires"].isoformat() if w["expires"] else None,
            raw_text=" | ".join(p for p in [title, description] if p),
            created_at=now,
            updated_at=now,
            severity="high",
        )

    async def _ingest(self, warnings: list[dict]) -> list[Event]:
        new_events: list[Event] = []
        current_ids: set[str] = set()
        now = datetime.now(timezone.utc)

        for w in warnings:
            if w["level"] < self._min_level:
                continue
            if w["expires"] and w["expires"] < now:
                continue
            event = self._to_event(w)
            current_ids.add(event.id)

            if event.id in self._active_ids or await self._db.exists(event.id):
                self._active_ids.add(event.id)
                continue

            await self._db.upsert(event)
            self._active_ids.add(event.id)
            new_events.append(event)

        for event_id in self._active_ids - current_ids:
            await self._db.mark_closed(event_id)
            logger.debug("Closed %s (no longer in feed)", event_id)
        self._active_ids &= current_ids

        return new_events

    async def run(self):
        logger.info(
            "Weather fetcher started (Meteoalarm, level >= %d, polling every %ds)",
            self._min_level, self._interval,
        )
        while True:
            try:
                warnings = await self._fetch()
                new_events = await self._ingest(warnings)
                if new_events and self._on_new_events:
                    await self._on_new_events(new_events)
            except Exception:
                logger.exception("Weather fetcher error")
            await asyncio.sleep(self._interval)
