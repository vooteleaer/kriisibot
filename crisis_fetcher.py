import asyncio
import logging
from datetime import datetime, timezone
from typing import Callable, Awaitable, Optional
import httpx

from event_db import Event, EventDB
from claude_client import ClaudeClient

logger = logging.getLogger(__name__)


def _parse_events(data: list[dict]) -> list[dict]:
    parsed = []
    for item in data:
        # API wraps payload in {"type": "EVENT_FULL", "data": {...}}
        item = item.get("data", item)
        ev = item.get("event", {})
        if not ev:
            continue
        alerts = item.get("alerts", [])
        et_texts = []
        for alert in alerts:
            for content in alert.get("content", []):
                if content.get("languageCode") == "ET":
                    text = content.get("text", "").strip()
                    if text:
                        et_texts.append(text)

        parsed.append(
            {
                "id": str(ev.get("id", "")),
                "title": ev.get("title", ""),
                "status": ev.get("eventStatus", "UNKNOWN"),
                "start_time": ev.get("startDate"),
                "end_time": ev.get("finishedDate"),
                "alert_texts": et_texts,
            }
        )
    return parsed


def _build_raw_text(item: dict) -> str:
    parts = [item["title"]]
    parts.extend(item["alert_texts"])
    return " | ".join(p for p in parts if p)


class CrisisFetcher:
    def __init__(
        self,
        url: str,
        poll_interval: int,
        db: EventDB,
        claude: ClaudeClient,
        on_new_events: Optional[Callable[[list[Event]], Awaitable[None]]] = None,
    ):
        self._url = url
        self._interval = poll_interval
        self._db = db
        self._claude = claude
        self._on_new_events = on_new_events
        self._seen_ids: set[str] = set()

    async def _fetch(self) -> list[dict]:
        async with httpx.AsyncClient(timeout=30, headers={"User-Agent": "kriisibot/1.0"}) as client:
            resp = await client.get(self._url)
            resp.raise_for_status()
            return resp.json()

    async def _ingest(self, raw_items: list[dict]) -> list[Event]:
        new_events: list[Event] = []
        for item in raw_items:
            event_id = f"eesti_ee:{item['id']}"
            raw_text = _build_raw_text(item)
            if not raw_text.strip():
                continue

            status = "OPEN" if item["status"] == "OPEN" else "CLOSED"

            # Known event — update status/end_time only, no Claude involved
            if event_id in self._seen_ids or await self._db.exists(event_id):
                await self._db.update_status(event_id, status, item["end_time"])
                self._seen_ids.add(item["id"])
                continue

            # New event — classify with Claude, check for duplicates, store
            classified = await self._claude.classify_event(raw_text)

            candidate = await self._db.find_duplicate_candidate(
                classified.event_type, classified.location
            )
            if candidate and candidate.id != event_id and await self._claude.check_duplicate(raw_text, candidate):
                await self._db.append_to_existing(candidate.id, raw_text)
                logger.debug("Merged eesti.ee event %s into %s", event_id, candidate.id)
                self._seen_ids.add(item["id"])
                continue

            now = datetime.now(timezone.utc).isoformat()
            event = Event(
                id=event_id,
                source="eesti_ee",
                trust_level="official",
                event_type=classified.event_type,
                title=item["title"],
                description=" | ".join(item["alert_texts"])[:500] or None,
                location=classified.location,
                status=status,
                start_time=item["start_time"],
                end_time=item["end_time"],
                raw_text=raw_text,
                created_at=now,
                updated_at=now,
            )
            await self._db.upsert(event)
            self._seen_ids.add(item["id"])

            if status == "OPEN":
                new_events.append(event)

        return new_events

    async def run(self):
        logger.info("Crisis fetcher started (polling every %ds)", self._interval)
        while True:
            try:
                data = await self._fetch()
                new_events = await self._ingest(_parse_events(data))
                if new_events and self._on_new_events:
                    await self._on_new_events(new_events)
            except Exception:
                logger.exception("Crisis fetcher error")
            await asyncio.sleep(self._interval)
