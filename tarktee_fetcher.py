import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Callable, Awaitable, Optional
import httpx

from event_db import Event, EventDB
from claude_client import ClaudeClient
from geocoder import reverse_geocode

logger = logging.getLogger(__name__)

_BASE = "https://tarktee.ee/tarktee/rest/services/tram/operative_info/MapServer"
ACCIDENTS_URL = f"{_BASE}/1/query"
HAZARDS_URL = f"{_BASE}/0/query"

_QUERY = {"where": "1=1", "outFields": "*", "outSR": "4326", "f": "json"}

_WORKTYPE_TAXONOMY = {
    "T1001_ROAD_BLOCKED": "road_blocked",
    "T1002_ROAD_BLOCKED_PARTIAL": "road_blocked",
    "T1005_TREE": "fallen_tree",
    "T1006_ROADKILL_LARGE": "road_hazard",
    "T1007_ROADKILL_SMALL": "road_hazard",
}

_IMPORTANCE = {"H": "kõrge", "M": "keskmine", "L": "madal"}
_PRIORITY = {"P3_HIGH": "kõrge", "P2_MEDIUM": "keskmine", "P1_LOW": "madal"}

MAX_AGE_HOURS = 12  # ignore accidents older than this on startup


def _location_str(road_name: str | None, road_nr: int | None) -> str | None:
    parts = []
    if road_name:
        parts.append(road_name)
    if road_nr:
        parts.append(f"(tee {road_nr})")
    return " ".join(parts) or None


class TarkteeFetcher:
    def __init__(
        self,
        poll_interval: int,
        db: EventDB,
        claude: ClaudeClient,
        on_new_events: Optional[Callable[[list[Event]], Awaitable[None]]] = None,
        accidents_enabled: bool = True,
        hazards_enabled: bool = True,
    ):
        self._interval = poll_interval
        self._db = db
        self._claude = claude
        self._on_new_events = on_new_events
        self._accidents_enabled = accidents_enabled
        self._hazards_enabled = hazards_enabled
        self._active_ids: set[str] = set()

    async def _fetch(self, url: str) -> list[dict]:
        async with httpx.AsyncClient(timeout=30, headers={"User-Agent": "kriisibot/1.0"}) as client:
            resp = await client.get(url, params=_QUERY)
            resp.raise_for_status()
            return resp.json().get("features", [])

    async def _accident_to_event(self, feat: dict) -> Optional[Event]:
        attrs = feat.get("attributes", {})
        geom = feat.get("geometry", {})
        oid = attrs.get("objectid")
        if oid is None:
            return None

        created_ms = attrs.get("created_at")
        if created_ms:
            created_dt = datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc)
            if created_dt < datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS):
                return None
            start_time = created_dt.isoformat()
        else:
            start_time = None

        lat = geom.get("y")
        lon = geom.get("x")

        location = _location_str(attrs.get("road_name"), attrs.get("road_nr"))
        if not location and lat and lon:
            location = await reverse_geocode(lat, lon)

        importance = attrs.get("importance", "")
        imp_label = _IMPORTANCE.get(importance, importance)

        title = "Liiklusõnnetus" + (f": {location}" if location else "")
        description = f"Tõsidus: {imp_label}." if imp_label else None
        raw_text = " ".join(p for p in [title, description] if p)

        now = datetime.now(timezone.utc).isoformat()
        return Event(
            id=f"tarktee:accident:{oid}",
            source="tarktee",
            trust_level="official",
            event_type="road_accident",
            title=title,
            description=description,
            location=location,
            lat=lat,
            lon=lon,
            status="OPEN",
            start_time=start_time,
            end_time=None,
            raw_text=raw_text,
            created_at=now,
            updated_at=now,
        )

    async def _hazard_to_event(self, feat: dict) -> Optional[Event]:
        attrs = feat.get("attributes", {})
        geom = feat.get("geometry", {})
        oid = attrs.get("objectid")
        if oid is None:
            return None

        worktype = attrs.get("worktype_code") or ""
        priority = attrs.get("priority") or ""
        additional_info = (attrs.get("additional_info") or "").strip()
        source = attrs.get("source") or ""
        location = _location_str(attrs.get("road_name"), attrs.get("road_number"))

        event_type = _WORKTYPE_TAXONOMY.get(worktype, "road_hazard")
        priority_label = _PRIORITY.get(priority, "")

        worktype_readable = worktype.split("_", 1)[-1].replace("_", " ").capitalize() if worktype else "Teeohu teade"
        title = worktype_readable + (f": {location}" if location else "")

        desc_parts = []
        if priority_label:
            desc_parts.append(f"Prioriteet: {priority_label}")
        if additional_info:
            desc_parts.append(additional_info[:200])
        description = ". ".join(desc_parts) or None

        raw_text = " ".join(p for p in [title, description] if p)

        created_ms = attrs.get("hosis_created_at")
        start_time = (
            datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc).isoformat()
            if created_ms else None
        )

        trust = "official"

        now = datetime.now(timezone.utc).isoformat()
        return Event(
            id=f"tarktee:hazard:{oid}",
            source="tarktee",
            trust_level=trust,
            event_type=event_type,
            title=title,
            description=description,
            location=location,
            lat=geom.get("y"),
            lon=geom.get("x"),
            status="OPEN",
            start_time=start_time,
            end_time=None,
            raw_text=raw_text,
            created_at=now,
            updated_at=now,
        )

    async def _ingest(self, features: list[dict], converter, id_prefix: str) -> list[Event]:
        new_events: list[Event] = []
        current_ids: set[str] = set()

        for feat in features:
            event = await converter(feat)
            if event is None:
                continue
            current_ids.add(event.id)

            if event.id in self._active_ids or await self._db.exists(event.id):
                self._active_ids.add(event.id)
                continue

            await self._db.upsert(event)
            self._active_ids.add(event.id)
            new_events.append(event)

        # Mark events no longer in the feed as CLOSED
        gone = {eid for eid in self._active_ids if eid.startswith(id_prefix)} - current_ids
        for event_id in gone:
            await self._db.mark_closed(event_id)
            self._active_ids.discard(event_id)
            logger.debug("Closed %s (no longer in feed)", event_id)

        return new_events

    async def run(self):
        logger.info("Tarktee fetcher started (polling every %ds)", self._interval)
        while True:
            try:
                new_events: list[Event] = []
                if self._accidents_enabled:
                    feats = await self._fetch(ACCIDENTS_URL)
                    new_events.extend(await self._ingest(feats, self._accident_to_event, "tarktee:accident:"))
                if self._hazards_enabled:
                    feats = await self._fetch(HAZARDS_URL)
                    new_events.extend(await self._ingest(feats, self._hazard_to_event, "tarktee:hazard:"))
                if new_events and self._on_new_events:
                    await self._on_new_events(new_events)
            except Exception:
                logger.exception("Tarktee fetcher error")
            await asyncio.sleep(self._interval)
