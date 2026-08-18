import asyncio
import logging
import os
from typing import Callable, Awaitable, Optional

import RNS
import LXMF

logger = logging.getLogger(__name__)

APP_ASPECT = ("lxmf", "delivery")


class ReticulumClient:
    """Reticulum/LXMF transport, run alongside MeshCore.

    Connects as a client to an already-running rnsd (shared instance) using
    the same config dir — it does not manage its own radio interfaces.
    Provides a single LXMF delivery identity for direct-message Q&A/report
    intake, and broadcasts crisis alerts to an existing LXMF distribution
    group (fan-out to subscribers is handled by that group, not by us).

    RNS/LXMF callbacks fire on RNS's own background thread, not the asyncio
    loop, so inbound messages are handed off via run_coroutine_threadsafe
    and outbound sends run in a thread via asyncio.to_thread.
    """

    def __init__(
        self,
        config_dir: str,
        identity_dir: str,
        display_name: str,
        distribution_group_hash: Optional[str],
        on_pm: Callable[[str, str], Awaitable[None]],
        on_group_message: Optional[Callable[[str], Awaitable[None]]] = None,
    ):
        self._config_dir = os.path.expanduser(config_dir)
        self._identity_dir = identity_dir
        self._display_name = display_name
        self._group_hash = (
            bytes.fromhex(distribution_group_hash) if distribution_group_hash else None
        )
        self._on_pm = on_pm
        self._on_group_message = on_group_message
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._rns: Optional[RNS.Reticulum] = None
        self._router: Optional[LXMF.LXMRouter] = None
        self._identity: Optional[RNS.Identity] = None
        self._destination: Optional[RNS.Destination] = None

    async def connect(self):
        self._loop = asyncio.get_running_loop()
        # RNS.Reticulum() and LXMF.LXMRouter() both install a SIGINT handler,
        # which only works from the main thread — must not run via asyncio.to_thread.
        self._connect_sync()

    def _connect_sync(self):
        self._rns = RNS.Reticulum(configdir=self._config_dir)

        os.makedirs(self._identity_dir, exist_ok=True)

        identity_path = os.path.join(self._identity_dir, "kriisibot.identity")
        if os.path.exists(identity_path):
            self._identity = RNS.Identity.from_file(identity_path)
            logger.info("Loaded Reticulum identity from %s", identity_path)
        else:
            self._identity = RNS.Identity()
            self._identity.to_file(identity_path)
            logger.info("Generated new Reticulum identity at %s", identity_path)

        lxmf_storage = os.path.join(self._identity_dir, "lxmf_storage")
        os.makedirs(lxmf_storage, exist_ok=True)
        self._router = LXMF.LXMRouter(storagepath=lxmf_storage)
        self._router.register_delivery_callback(self._handle_inbound)

        self._destination = self._router.register_delivery_identity(
            self._identity, display_name=self._display_name
        )
        self._destination.announce()
        logger.info(
            "Reticulum connected — LXMF address %s (%s)",
            RNS.prettyhexrep(self._destination.hash),
            self._display_name,
        )

    def _handle_inbound(self, message: "LXMF.LXMessage"):
        # Runs on RNS's internal thread — never touch asyncio state directly here.
        try:
            content = message.content
            text = content.decode("utf-8", "ignore") if isinstance(content, (bytes, bytearray)) else str(content or "")
            text = text.strip()
            if not text:
                return

            # The distribution group relays every member's message under its own
            # identity (not the original sender's), so this is indistinguishable
            # from a single shared sender — route it through the mention-gated
            # group handler instead of treating it as a 1:1 PM.
            if self._group_hash is not None and message.source_hash == self._group_hash:
                logger.info("[Reticulum group] %s", text[:80])
                if self._loop is not None and self._on_group_message is not None:
                    asyncio.run_coroutine_threadsafe(self._on_group_message(text), self._loop)
                return

            source_hash = message.source_hash.hex()
            logger.info("[Reticulum PM] %s: %s", source_hash, text[:80])
            if self._loop is not None:
                asyncio.run_coroutine_threadsafe(self._on_pm(source_hash, text), self._loop)
        except Exception:
            logger.exception("Failed to handle inbound Reticulum message")

    async def send_pm(self, dest_hash_hex: str, text: str):
        await asyncio.to_thread(self._send_pm_sync, dest_hash_hex, text)

    def _send_pm_sync(self, dest_hash_hex: str, text: str):
        if self._router is None or self._destination is None:
            logger.error("Reticulum not connected — cannot send PM")
            return
        try:
            dest_hash = bytes.fromhex(dest_hash_hex)
        except ValueError:
            logger.error("Invalid Reticulum destination hash: %s", dest_hash_hex)
            return

        identity = RNS.Identity.recall(dest_hash)
        if identity is None:
            # We can only reply to senders whose identity RNS has already
            # resolved — which it must have, to have delivered their message
            # to us in the first place. A None here means the recall cache
            # was flushed since; nothing to do but drop it.
            logger.warning("Cannot send Reticulum PM to %s — identity not resolved", dest_hash_hex)
            return

        dest = RNS.Destination(identity, RNS.Destination.OUT, RNS.Destination.SINGLE, *APP_ASPECT)
        lxm = LXMF.LXMessage(
            dest, self._destination, text,
            desired_method=LXMF.LXMessage.OPPORTUNISTIC,
        )
        self._router.handle_outbound(lxm)
        logger.debug("Reticulum PM sent to %s", dest_hash_hex)

    async def send_broadcast(self, text: str):
        await asyncio.to_thread(self._send_broadcast_sync, text)

    def _send_broadcast_sync(self, text: str):
        if self._group_hash is None:
            logger.debug("No distribution_group_hash configured — skipping Reticulum broadcast")
            return
        if self._router is None or self._destination is None:
            logger.error("Reticulum not connected — cannot broadcast")
            return

        identity = RNS.Identity.recall(self._group_hash)
        if identity is None:
            logger.warning(
                "Distribution group %s not resolved yet (no announce seen) — skipping broadcast",
                self._group_hash.hex(),
            )
            return

        dest = RNS.Destination(identity, RNS.Destination.OUT, RNS.Destination.SINGLE, *APP_ASPECT)
        lxm = LXMF.LXMessage(
            dest, self._destination, text,
            title=self._display_name,
            desired_method=LXMF.LXMessage.OPPORTUNISTIC,
        )
        self._router.handle_outbound(lxm)
        logger.debug("Reticulum broadcast sent to distribution group: %s", text[:60])

    async def run_periodic_announce(self, interval_seconds: int = 3600):
        """Re-announce periodically so other nodes' path caches don't go stale."""
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                await asyncio.to_thread(self._destination.announce)
                logger.debug("Reticulum announce sent")
            except Exception:
                logger.exception("Failed to send Reticulum announce")

    async def disconnect(self):
        # Shared-instance client — no interfaces to tear down, nothing to release.
        pass
