from collections import deque
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field


@dataclass
class Turn:
    role: str   # "user" or "assistant"
    text: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class ConversationHistory:
    """Per-user rolling conversation window for multi-turn Q&A."""

    def __init__(self, max_turns: int = 6, max_age_minutes: int = 15):
        self._max_turns = max_turns
        self._max_age = timedelta(minutes=max_age_minutes)
        self._histories: dict[str, deque[Turn]] = {}

    def _get(self, pubkey: str) -> deque[Turn]:
        if pubkey not in self._histories:
            self._histories[pubkey] = deque(maxlen=self._max_turns * 2)
        return self._histories[pubkey]

    def _expire(self, pubkey: str):
        history = self._get(pubkey)
        cutoff = datetime.now(timezone.utc) - self._max_age
        while history and history[0].timestamp < cutoff:
            history.popleft()

    def add(self, pubkey: str, role: str, text: str):
        self._expire(pubkey)
        self._get(pubkey).append(Turn(role=role, text=text))

    def get_turns(self, pubkey: str) -> list[Turn]:
        self._expire(pubkey)
        return list(self._get(pubkey))

    def clear(self, pubkey: str):
        if pubkey in self._histories:
            self._histories[pubkey].clear()
