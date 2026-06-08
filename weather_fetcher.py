import asyncio
import hashlib
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Callable, Awaitable, Optional

import httpx

from event_db import Event, EventDB
from claude_client import ClaudeClient

logger = logging.getLogger(__name__)

WARNINGS_URL = "https://www.ilmateenistus.ee/ilma_andmed/xml/hoiatus.php"
HEADERS = {"User-Agent": "kriisibot/1.0"}


def _warning_id(area: str, timestamp: str) -> str:
    key = f"{area}:{timestamp}"
    return f"weather:{hashlib.md5(key.encode()).hexdigest()[:12]}"


def _parse_warnings(xml_text: str) -> list[dict]:
    warnings = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        logger.warning("Failed to parse weather XML: %s", e)
        return warnings

    for item in root.findall(".//warning") or root.findall(".//hoiatus") or root:
        area = (
            (item.findtext("area_est") or item.findtext("area_eng") or "").strip()
        )
        content = (
            (item.findtext("content_est") or item.findtext("content_eng") or "").strip()
        )
        timestamp = (item.findtext("timestamp") or "").strip()

        if not content:
            continue

        warnings.append({
            "id": _warning_id(area, timestamp or content[:30]),
            "area": area,
            "content": content,
            "timestamp": timestamp,
        })
    return warnings


class WeatherFetcher:
    def __init__(
        self,
        poll_interval: int,
        db: EventDB,
        claude: ClaudeClient,
        on_new_events: Optional[Callable[[list[Event]], Awaitable[None]]] = None,
        api_key: Optional[str] = None,
    ):
        self._interval = poll_interval
        self._db = db
        self._claude = claude
        self._on_new_events = on_new_events
        self._api_key = api_key
        self._seen_ids: set[str] = set()

    async def _fetch(self) -> list[dict]:
        async with httpx.AsyncClient(timeout=30, headers=HEADERS) as client:
            resp = await client.get(WARNINGS_URL)
            resp.raise_for_status()
            return _parse_warnings(resp.text)

    async def _ingest(self, warnings: list[dict]) -> list[Event]:
        new_events: list[Event] = []
        for w in warnings:
            event_id = w["id"]
            if event_id in self._seen_ids or await self._db.exists(event_id):
                self._seen_ids.add(event_id)
                continue

            raw_text = f"{w['area']}: {w['content']}".strip(": ")
            classified = await self._claude.classify_event(raw_text)

            now = datetime.now(timezone.utc).isoformat()
            event = Event(
                id=event_id,
                source="weather",
                trust_level="official",
                event_type=classified.event_type,
                title=w["area"] or "Ilmahoiatus",
                description=w["content"][:500],
                location=w["area"] or classified.location,
                lat=None,
                lon=None,
                status="OPEN",
                start_time=now,
                end_time=None,
                raw_text=raw_text,
                created_at=now,
                updated_at=now,
            )
            await self._db.upsert(event)
            self._seen_ids.add(event_id)
            new_events.append(event)

        return new_events

    async def run(self):
        logger.info("Weather fetcher started (polling every %ds)", self._interval)
        while True:
            try:
                warnings = await self._fetch()
                new_events = await self._ingest(warnings)
                if new_events and self._on_new_events:
                    await self._on_new_events(new_events)
            except Exception:
                logger.exception("Weather fetcher error")
            await asyncio.sleep(self._interval)
