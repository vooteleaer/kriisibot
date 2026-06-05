import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional, Callable, Awaitable

from event_db import Event, EventDB
from claude_client import ClaudeClient

logger = logging.getLogger(__name__)


@dataclass
class PMSession:
    """Freeform PM conversation per user — handles both Q&A and report intake."""
    pubkey: str
    timeout: timedelta
    turns: list[dict] = field(default_factory=list)
    last_activity: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def is_expired(self) -> bool:
        return datetime.now(timezone.utc) - self.last_activity > self.timeout

    def add_turn(self, role: str, text: str):
        self.last_activity = datetime.now(timezone.utc)
        self.turns.append({"role": role, "content": text})


class UserReportStore:
    def __init__(
        self,
        report_trigger: str,           # kept for channel-side redirect only
        cooldown_seconds: int,
        max_stored: int,
        max_age_hours: int,
        session_timeout_minutes: int,
        db: EventDB,
        claude: ClaudeClient,
        on_new_report: Optional[Callable[[Event], Awaitable[None]]] = None,
    ):
        self._trigger = report_trigger.lower()
        self._cooldown = cooldown_seconds
        self._max_stored = max_stored
        self._max_age = timedelta(hours=max_age_hours)
        self._session_timeout = timedelta(minutes=session_timeout_minutes)
        self._db = db
        self._claude = claude
        self._on_new_report = on_new_report

        self._reports: deque[dict] = deque(maxlen=max_stored)
        self._last_report_time: dict[str, datetime] = {}
        self._sessions: dict[str, PMSession] = {}
        self._lock = asyncio.Lock()

    # ── Channel-side helper ───────────────────────────────────────────────────

    def is_report_trigger(self, text: str) -> bool:
        """True if a channel message looks like a report attempt (redirect to PM)."""
        return text.strip().lower().startswith(self._trigger)

    # ── PM conversation ───────────────────────────────────────────────────────

    def _get_session(self, pubkey: str) -> PMSession:
        session = self._sessions.get(pubkey)
        if session and session.is_expired():
            del self._sessions[pubkey]
            session = None
        if not session:
            session = PMSession(pubkey=pubkey, timeout=self._session_timeout)
            self._sessions[pubkey] = session
        return session

    def _is_rate_limited(self, pubkey: str) -> bool:
        last = self._last_report_time.get(pubkey)
        if not last:
            return False
        return (datetime.now(timezone.utc) - last).total_seconds() < self._cooldown

    def _expire_old_reports(self):
        cutoff = datetime.now(timezone.utc) - self._max_age
        self._reports = deque(
            (r for r in self._reports if r["timestamp"] > cutoff),
            maxlen=self._max_stored,
        )

    async def handle_pm(
        self,
        pubkey: str,
        text: str,
        active_events: list[Event],
        user_reports: list[dict],
    ) -> str:
        async with self._lock:
            session = self._get_session(pubkey)
            session.add_turn("user", text)

            reply, fields = await self._claude.pm_turn(
                session.turns, active_events, user_reports
            )

            if fields:
                # Claude collected a report — check rate limit before saving
                if self._is_rate_limited(pubkey):
                    remaining = int(
                        self._cooldown
                        - (datetime.now(timezone.utc) - self._last_report_time[pubkey]).total_seconds()
                    )
                    session.add_turn("assistant", reply)
                    return f"{reply}\n(Raport ei salvestatud: liiga palju raporteid. Oota {remaining}s.)"

                save_reply = await self._save_report(pubkey, fields)
                full_reply = f"{reply}\n{save_reply}" if reply else save_reply
                session.add_turn("assistant", full_reply)
                return full_reply

            session.add_turn("assistant", reply)
            return reply

    async def _save_report(self, pubkey: str, fields: dict) -> str:
        description = fields.get("description", "")
        verdict = await self._claude.check_plausibility(description)
        if verdict == "ei":
            logger.warning("Rejected implausible report from %s: %s", pubkey, description[:60])
            return "Raport lükati tagasi: ei tundu usutav kriisiinfo."

        now = datetime.now(timezone.utc)
        self._last_report_time[pubkey] = now
        self._expire_old_reports()

        event_id = f"user:{pubkey}:{int(now.timestamp())}"
        event_type = fields.get("event_type", "other")
        if event_type not in self._claude.taxonomy:
            event_type = "other"

        event = Event(
            id=event_id,
            source="user",
            trust_level="unverified",
            event_type=event_type,
            title=None,
            description=description[:500],
            location=fields.get("location"),
            status=fields.get("status", "OPEN"),
            start_time=now.isoformat(),
            end_time=None,
            raw_text=description,
            created_at=now.isoformat(),
            updated_at=now.isoformat(),
        )
        await self._db.upsert(event)

        self._reports.append({
            "pubkey_prefix": pubkey,
            "text": description,
            "timestamp": now,
            "event_id": event_id,
            "verdict": verdict,
        })

        if self._on_new_report:
            await self._on_new_report(event)

        label = "[KONTROLLIMATA]" if verdict == "jah" else "[KAHTLANE]"
        return f"Raport salvestatud {label}. Täname!"

    def get_recent(self) -> list[dict]:
        self._expire_old_reports()
        return [
            {"pubkey_prefix": r["pubkey_prefix"], "text": r["text"]}
            for r in self._reports
        ]
