import asyncio
import logging
from typing import Callable, Awaitable, Optional

from meshcore import MeshCore
from meshcore.events import EventType

logger = logging.getLogger(__name__)

MAX_CHUNK_CHARS = 140  # MeshCore channel limit is 143 chars; 140 leaves headroom


def _normalize(text: str) -> str:
    """Collapse all whitespace runs to a single space and strip ends."""
    import re
    return re.sub(r"\s+", " ", text).strip()


def _chunk(text: str, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    """Split text into chunks of at most max_chars characters.

    Priority: sentence boundary (.!?) → comma/colon → word → hard cut.
    Claude writes sentences sized to max_chars so sentence splits cover most cases.
    """
    import re
    text = text.strip()
    if len(text) <= max_chars:
        return [text]
    chunks = []
    while text:
        if len(text) <= max_chars:
            chunks.append(text)
            break
        cut = text[:max_chars]
        # 1. Sentence boundary
        m = max((m for m in re.finditer(r'[.!?…](?=\s|$)', cut)), key=lambda m: m.end(), default=None)
        if m and m.end() > max_chars // 4:
            cut = cut[:m.end()].rstrip()
        else:
            # 2. Comma or colon
            soft = max(cut.rfind(","), cut.rfind(":"))
            if soft > max_chars // 4:
                cut = cut[:soft + 1]
            else:
                # 3. Word boundary
                space = cut.rfind(" ")
                if space > 0:
                    cut = cut[:space]
        chunks.append(cut.strip())
        text = text[len(cut):].lstrip()
    return chunks


class MeshCoreClient:
    def __init__(
        self,
        port: str,
        channel_name: str,
        on_channel_message: Callable[[str, str], Awaitable[None]],
        on_pm: Callable[[str, str], Awaitable[None]],
        on_contacts_updated: Optional[Callable[[dict], Awaitable[None]]] = None,
    ):
        self._port = port
        self._channel_name = channel_name.lower()
        self._on_channel_message = on_channel_message
        self._on_pm = on_pm
        self._on_contacts_updated = on_contacts_updated
        self._mc: Optional[MeshCore] = None
        self._channel_idx: Optional[int] = None
        # pubkey_prefix → Contact object cache for PM replies
        self._contact_cache: dict[str, object] = {}

    async def connect(self, retries: int = 10, retry_delay: float = 3.0):
        for attempt in range(1, retries + 1):
            try:
                logger.info("Connecting to MeshCore on %s (attempt %d/%d)...", self._port, attempt, retries)
                self._mc = await MeshCore.create_serial(self._port, 115200)
                logger.info("Connected to MeshCore")
                return
            except Exception as e:
                if attempt == retries:
                    raise
                logger.warning("Connection failed (%s) — retrying in %.0fs...", e, retry_delay)
                await asyncio.sleep(retry_delay)

    async def discover_channel(self) -> int:
        for idx in range(8):
            try:
                result = await self._mc.commands.get_channel(idx)
                name = (result.payload or {}).get("channel_name", "")
                if name.lower() == self._channel_name:
                    logger.info("Found channel '%s' at index %d", name, idx)
                    self._channel_idx = idx
                    return idx
            except Exception:
                pass
        raise RuntimeError(f"Channel '{self._channel_name}' not found in slots 0–7")

    async def _refresh_contacts(self):
        try:
            result = await self._mc.commands.get_contacts()
            contacts = result.payload or {}
            for full_key, contact in contacts.items():
                prefix = full_key[:12]
                self._contact_cache[prefix] = contact
            logger.debug("Contacts cache refreshed: %d contacts", len(self._contact_cache))
            if self._on_contacts_updated and contacts:
                await self._on_contacts_updated(contacts)
        except Exception:
            logger.warning("Failed to refresh contacts cache")

    async def _handle_channel_msg(self, event):
        payload = event.payload or {}
        text: str = payload.get("text", "").strip()
        pubkey: str = payload.get("pubkey_prefix", "unknown")
        if text:
            logger.info("[#kriis] %s: %s", pubkey, text[:80])
            await self._on_channel_message(pubkey, text)

    async def _handle_advert(self, event):
        # A new node is visible — refresh contacts to capture its position
        await self._refresh_contacts()

    async def _handle_new_contact(self, event):
        # Firmware saw an advert from a node it has no stored contact for. If
        # manual_add_contacts is enabled on the companion device, it won't
        # persist this as a real contact (and can't decrypt/route their PMs)
        # without an explicit add — and nobody is around to tap "approve" in
        # a phone app. Add unconditionally; this is a no-op if the device
        # already auto-added it.
        contact = event.payload or {}
        name = contact.get("adv_name") or contact.get("public_key", "unknown")[:12]
        try:
            result = await self._mc.commands.add_contact(contact)
            if result.type == EventType.ERROR:
                logger.error("Failed to auto-add contact %s: %s", name, result.payload)
            else:
                logger.info("Auto-added new contact: %s", name)
                await self._refresh_contacts()
        except Exception:
            logger.exception("Failed to auto-add new contact %s", name)

    async def _handle_pm(self, event):
        payload = event.payload or {}
        text: str = payload.get("text", "").strip()
        pubkey: str = payload.get("pubkey_prefix", "unknown")
        if text:
            logger.info("[PM] %s: %s", pubkey, text[:80])
            await self._on_pm(pubkey, text)

    async def discover_radio_limits(self):
        try:
            result = await self._mc.commands.send_appstart()
            sf = (result.payload or {}).get("radio_sf", 0)
            logger.info("Radio SF%d — using %d char chunks", sf, MAX_CHUNK_CHARS)
        except Exception:
            logger.warning("Could not query radio config — using %d char chunks", MAX_CHUNK_CHARS)

    async def start(self):
        if self._channel_idx is None:
            raise RuntimeError("Call discover_channel() before start()")

        self._mc.subscribe(
            EventType.CHANNEL_MSG_RECV,
            self._handle_channel_msg,
            attribute_filters={"channel_idx": self._channel_idx},
        )
        self._mc.subscribe(EventType.CONTACT_MSG_RECV, self._handle_pm)
        self._mc.subscribe(EventType.ADVERTISEMENT, self._handle_advert)
        self._mc.subscribe(EventType.NEW_CONTACT, self._handle_new_contact)
        await self._mc.start_auto_message_fetching()
        await self._refresh_contacts()
        await self.send_advert()
        logger.info(
            "Listening on channel '%s' (index %d) and PMs",
            self._channel_name,
            self._channel_idx,
        )

    async def send_advert(self):
        try:
            await self._mc.commands.send_advert(flood=True)
            logger.info("Flood advert sent")
        except Exception:
            logger.exception("Failed to send advert")

    async def run_periodic_advert(self, interval_seconds: int = 3600):
        while True:
            await asyncio.sleep(interval_seconds)
            await self.send_advert()

    async def send_channel(self, text: str):
        if self._mc is None or self._channel_idx is None:
            logger.error("MeshCore not connected — cannot send to channel")
            return
        chunks = _chunk(_normalize(text))
        total = len(chunks)
        for i, chunk in enumerate(chunks):
            try:
                logger.debug("Sending chunk %d/%d (%d chars): %s", i + 1, total, len(chunk), chunk)
                result = await self._mc.commands.send_chan_msg(
                    chan=self._channel_idx, msg=chunk
                )
                if result.type == EventType.ERROR:
                    logger.error("send_chan_msg error: %s", result.payload)
            except Exception:
                logger.exception("Failed to send channel chunk %d", i + 1)
            if i < total - 1:
                await asyncio.sleep(2.0)

    async def send_pm(self, pubkey: str, text: str):
        if self._mc is None:
            logger.error("MeshCore not connected — cannot send PM")
            return
        contact = self._contact_cache.get(pubkey)
        if not contact:
            # Try refreshing the cache once
            await self._refresh_contacts()
            contact = self._contact_cache.get(pubkey)
        if not contact:
            logger.warning("Cannot send PM to %s — contact not found", pubkey)
            return
        chunks = _chunk(_normalize(text))
        total = len(chunks)
        for i, chunk in enumerate(chunks):
            try:
                # send_msg only confirms local hand-off to the radio, not delivery.
                # send_msg_with_retry waits for an ACK and falls back to flood
                # routing if the contact's cached direct path is stale/dead —
                # needed for contacts outside direct range.
                result = await self._mc.commands.send_msg_with_retry(contact, chunk)
                if result is None:
                    logger.warning(
                        "PM to %s chunk %d/%d not acknowledged after retries — "
                        "recipient may be unreachable", pubkey, i + 1, total,
                    )
                else:
                    logger.debug("PM to %s chunk %d/%d delivered: %s", pubkey, i + 1, total, chunk[:40])
            except Exception:
                logger.exception("Failed to send PM chunk %d to %s", i + 1, pubkey)
            if i < total - 1:
                await asyncio.sleep(2.0)

    async def disconnect(self):
        if self._mc:
            try:
                await self._mc.stop_auto_message_fetching()
            except Exception:
                pass
            try:
                await self._mc.disconnect()
            except Exception:
                pass
            self._mc = None

    async def _restart(self, retries: int = 5, retry_delay: float = 10.0):
        """Full disconnect → connect → discover → start cycle. Used by watchdog."""
        await self.disconnect()
        for attempt in range(1, retries + 1):
            try:
                await self.connect()
                await self.discover_radio_limits()
                await self.discover_channel()
                await self.start()
                return
            except Exception as e:
                logger.warning("Reconnect attempt %d/%d failed: %s", attempt, retries, e)
                await self.disconnect()
                if attempt < retries:
                    await asyncio.sleep(retry_delay)
        raise RuntimeError("MeshCore reconnect failed after all retries")

    async def run_watchdog(self, interval_seconds: int = 60):
        """Ping the radio every interval_seconds; reconnect if it stops responding."""
        while True:
            await asyncio.sleep(interval_seconds)
            if self._mc is None:
                continue
            try:
                await asyncio.wait_for(self._mc.commands.send_appstart(), timeout=15.0)
            except Exception as e:
                logger.warning("MeshCore health check failed (%s) — reconnecting", e)
                try:
                    await self._restart()
                    logger.info("MeshCore reconnected successfully")
                except Exception:
                    logger.exception("MeshCore reconnect failed — will retry next cycle")
