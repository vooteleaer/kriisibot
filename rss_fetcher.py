import asyncio
import logging
from datetime import datetime, timezone
from typing import Callable, Awaitable, Optional
import feedparser
import httpx

from config import RssFeed
from event_db import Event, EventDB
from claude_client import ClaudeClient

logger = logging.getLogger(__name__)


def _entry_id(feed_name: str, entry: dict) -> str:
    guid = entry.get("id") or entry.get("link") or entry.get("title", "")
    return f"rss:{feed_name}:{guid}"


def _entry_raw_text(entry: dict) -> str:
    title = entry.get("title", "")
    summary = entry.get("summary", "") or entry.get("description", "")
    return f"{title} | {summary}".strip(" |")


def _entry_published(entry: dict) -> Optional[str]:
    ts = entry.get("published_parsed") or entry.get("updated_parsed")
    if ts:
        try:
            return datetime(*ts[:6], tzinfo=timezone.utc).isoformat()
        except Exception:
            pass
    return None


class RssFetcher:
    def __init__(
        self,
        feeds: list[RssFeed],
        poll_interval: int,
        db: EventDB,
        claude: ClaudeClient,
        on_new_events: Optional[Callable[[list[Event]], Awaitable[None]]] = None,
    ):
        self._feeds = [f for f in feeds if f.enabled]
        self._interval = poll_interval
        self._db = db
        self._claude = claude
        self._on_new_events = on_new_events
        self._seen_ids: set[str] = set()

    async def _fetch_feed(self, feed: RssFeed) -> list[dict]:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(feed.url, follow_redirects=True)
            resp.raise_for_status()
            parsed = feedparser.parse(resp.text)
            return parsed.get("entries", [])

    async def _ingest_entry(self, feed: RssFeed, entry: dict) -> Optional[Event]:
        event_id = _entry_id(feed.name, entry)
        if event_id in self._seen_ids:
            return None
        already_known = await self._db.exists(event_id)
        self._seen_ids.add(event_id)
        if already_known:
            return None  # seen in a previous run — don't re-broadcast

        raw_text = _entry_raw_text(entry)
        if not raw_text.strip():
            return None

        classified = await self._claude.classify_event(raw_text)
        candidate = await self._db.find_duplicate_candidate(
            classified.event_type, classified.location
        )
        if candidate and await self._claude.check_duplicate(raw_text, candidate):
            await self._db.append_to_existing(candidate.id, raw_text)
            logger.debug("Merged RSS entry %s into %s", event_id, candidate.id)
            return None

        now = datetime.now(timezone.utc).isoformat()
        event = Event(
            id=event_id,
            source=f"rss:{feed.name}",
            trust_level="media",
            event_type=classified.event_type,
            title=entry.get("title"),
            description=entry.get("summary", "")[:500] or None,
            location=classified.location,
            status=classified.status,
            start_time=_entry_published(entry),
            end_time=None,
            raw_text=raw_text,
            created_at=now,
            updated_at=now,
        )
        await self._db.upsert(event)
        return event

    async def _poll_feed(self, feed: RssFeed) -> list[Event]:
        try:
            entries = await self._fetch_feed(feed)
            new_events = []
            for entry in entries:
                event = await self._ingest_entry(feed, entry)
                if event:
                    new_events.append(event)
            if new_events:
                logger.info("RSS %s: %d new events", feed.name, len(new_events))
            return new_events
        except Exception:
            logger.exception("RSS feed error: %s", feed.url)
            return []

    async def run(self):
        if not self._feeds:
            logger.info("No RSS feeds configured — RSS fetcher idle")
            return
        logger.info("RSS fetcher started (%d feeds, polling every %ds)", len(self._feeds), self._interval)
        while True:
            all_new: list[Event] = []
            for feed in self._feeds:
                all_new.extend(await self._poll_feed(feed))
            if all_new and self._on_new_events:
                await self._on_new_events(all_new)
            await asyncio.sleep(self._interval)
