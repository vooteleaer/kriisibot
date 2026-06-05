import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional
import aiosqlite

logger = logging.getLogger(__name__)

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS events (
    id          TEXT PRIMARY KEY,
    source      TEXT NOT NULL,
    trust_level TEXT NOT NULL,
    event_type  TEXT,
    title       TEXT,
    description TEXT,
    location    TEXT,
    lat         REAL,
    lon         REAL,
    status      TEXT DEFAULT 'UNKNOWN',
    start_time  TEXT,
    end_time    TEXT,
    raw_text    TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
)
"""


@dataclass
class Event:
    id: str
    source: str
    trust_level: str
    event_type: Optional[str]
    title: Optional[str]
    description: Optional[str]
    location: Optional[str]
    lat: Optional[float]
    lon: Optional[float]
    status: str
    start_time: Optional[str]
    end_time: Optional[str]
    raw_text: str
    created_at: str
    updated_at: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class EventDB:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = asyncio.Lock()

    async def init(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(CREATE_TABLE)
            # Migrate existing DBs that predate the lat/lon columns
            for col in ("lat", "lon"):
                try:
                    await db.execute(f"ALTER TABLE events ADD COLUMN {col} REAL")
                except Exception:
                    pass  # column already exists
            await db.commit()
        logger.info("Event database initialized at %s", self.db_path)

    async def update_status(self, event_id: str, status: str, end_time: str | None):
        now = _now()
        async with self._lock:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "UPDATE events SET status=?, end_time=?, updated_at=? WHERE id=?",
                    (status, end_time, now, event_id),
                )
                await db.commit()

    async def exists(self, event_id: str) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            row = await (await db.execute(
                "SELECT 1 FROM events WHERE id = ?", (event_id,)
            )).fetchone()
        return row is not None

    async def upsert(self, event: Event):
        now = _now()
        async with self._lock:
            async with aiosqlite.connect(self.db_path) as db:
                existing = await (await db.execute(
                    "SELECT id FROM events WHERE id = ?", (event.id,)
                )).fetchone()
                if existing:
                    await db.execute(
                        """UPDATE events SET
                            event_type=?, title=?, description=?, location=?,
                            lat=?, lon=?, status=?, end_time=?, raw_text=?, updated_at=?
                           WHERE id=?""",
                        (
                            event.event_type, event.title, event.description,
                            event.location, event.lat, event.lon,
                            event.status, event.end_time,
                            event.raw_text, now, event.id,
                        ),
                    )
                else:
                    await db.execute(
                        """INSERT INTO events
                           (id, source, trust_level, event_type, title, description,
                            location, lat, lon, status, start_time, end_time,
                            raw_text, created_at, updated_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            event.id, event.source, event.trust_level,
                            event.event_type, event.title, event.description,
                            event.location, event.lat, event.lon,
                            event.status, event.start_time, event.end_time,
                            event.raw_text, event.created_at or now, now,
                        ),
                    )
                await db.commit()

    async def get_active_events(self, hours: int = 24, limit: int = 20) -> list[Event]:
        # Default to today only — start of current UTC day
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        cutoff = (today if hours >= 24 else datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """SELECT * FROM events
                   WHERE status != 'CLOSED'
                     AND (start_time IS NULL OR start_time >= ?)
                   ORDER BY start_time DESC
                   LIMIT ?""",
                (cutoff, limit),
            )
            rows = await cursor.fetchall()
        return [Event(**dict(r)) for r in rows]

    async def find_duplicate_candidate(
        self, event_type: str, location: Optional[str], hours: int = 24
    ) -> Optional[Event]:
        if not event_type or event_type == "other":
            return None
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            if location:
                cursor = await db.execute(
                    """SELECT * FROM events
                       WHERE event_type = ?
                         AND status != 'CLOSED'
                         AND (start_time IS NULL OR start_time >= ?)
                         AND location LIKE ?
                       ORDER BY start_time DESC LIMIT 1""",
                    (event_type, cutoff, f"%{location[:20]}%"),
                )
            else:
                cursor = await db.execute(
                    """SELECT * FROM events
                       WHERE event_type = ?
                         AND status != 'CLOSED'
                         AND (start_time IS NULL OR start_time >= ?)
                       ORDER BY start_time DESC LIMIT 1""",
                    (event_type, cutoff),
                )
            row = await cursor.fetchone()
        return Event(**dict(row)) if row else None

    async def append_to_existing(self, event_id: str, extra_raw: str):
        now = _now()
        async with self._lock:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    """UPDATE events
                       SET raw_text = raw_text || '\n---\n' || ?,
                           updated_at = ?
                       WHERE id = ?""",
                    (extra_raw, now, event_id),
                )
                await db.commit()

    async def mark_closed(self, event_id: str):
        now = _now()
        async with self._lock:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "UPDATE events SET status='CLOSED', end_time=?, updated_at=? WHERE id=?",
                    (now, now, event_id),
                )
                await db.commit()
