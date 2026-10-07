import asyncio
import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Callable, Awaitable, Optional
import httpx

from event_db import Event, EventDB
from claude_client import ClaudeClient
from geocoder import reverse_geocode_parts, settlement_at

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
_IMPORTANCE_SEVERITY = {"H": "high", "M": "medium", "L": "low"}
_PRIORITY = {"P3_HIGH": "kõrge", "P2_MEDIUM": "keskmine", "P1_LOW": "madal"}

MAX_AGE_HOURS = 12  # ignore accidents older than this on startup
VILLAGE_RADIUS_M = 1000  # closer than this to a village centre counts as "in the village"

_ROAD_TYPE_RE = re.compile(
    r"\b(tee|mnt|maantee|tn|tänav|pst|puiestee|põik|allee|väljak|plats|rada|sild)\b",
    re.IGNORECASE,
)


def _with_road_type(road: str | None) -> str | None:
    """OSM gives town streets bare ("Tehase") — add "tn" so the text reads as a road."""
    if not road or _ROAD_TYPE_RE.search(road):
        return road
    return f"{road} tn"


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

    async def _describe_location(self, lat: float, lon: float, road_hint: str | None) -> str | None:
        """"Tehase tn, Narva" inside a settlement, "Tallinna mnt, 3,3 km Narvast lõunas" outside."""
        road, admin, town = await reverse_geocode_parts(lat, lon)
        road = _with_road_type(road_hint or road)
        if town:
            return ", ".join(p for p in [road, town] if p)
        village = await settlement_at(lat, lon)
        if village and village["distance_m"] < VILLAGE_RADIUS_M:
            return ", ".join(p for p in [road, village["name"]] if p)
        if village:
            return ", ".join(p for p in [road, await self._distance_phrase(village)] if p)
        return ", ".join(p for p in [road, admin] if p) or None

    async def _distance_phrase(self, place: dict) -> str:
        dist = place["distance_m"]
        dist_text = f"{dist // 100 * 100} m" if dist < 1000 else f"{dist / 1000:.1f}".replace(".", ",") + " km"
        elative = await self._claude.elative(place["name"])
        if elative:
            return f"{dist_text} {elative} {place['direction']}"
        return f"{dist_text} {place['direction']} asulast {place['name']}"

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
        if lat and lon:
            location = await self._describe_location(lat, lon, location)

        importance = attrs.get("importance", "")
        imp_label = _IMPORTANCE.get(importance, importance)

        title = "Liiklusõnnetus" + (f": {location}" if location else "")
        desc_parts = []
        if imp_label:
            desc_parts.append(f"Tõsidus: {imp_label}.")
        if lat and lon:
            desc_parts.append(f"{lat:.4f},{lon:.4f}")
        description = " ".join(desc_parts) or None
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
            severity=_IMPORTANCE_SEVERITY.get(importance),
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
            # Skip known features before converting — conversion geocodes, which should
            # happen once per incident, not on every poll.
            oid = feat.get("attributes", {}).get("objectid")
            if oid is not None:
                known_id = f"{id_prefix}{oid}"
                if known_id in self._active_ids or await self._db.exists(known_id):
                    current_ids.add(known_id)
                    self._active_ids.add(known_id)
                    continue

            event = await converter(feat)
            if event is None:
                continue
            current_ids.add(event.id)

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
