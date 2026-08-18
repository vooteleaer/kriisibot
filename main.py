import asyncio
import logging
import signal
import sys

from config import load_settings
from event_db import Event, EventDB
from claude_client import ClaudeClient
from conversation import ConversationHistory
from crisis_fetcher import CrisisFetcher
from rss_fetcher import RssFetcher
from weather_fetcher import WeatherFetcher
from tarktee_fetcher import TarkteeFetcher
from user_reports import UserReportStore
from meshcore_client import MeshCoreClient
from reticulum_client import ReticulumClient
from node_tracker import NodeTracker, location_to_coords as _county_coords

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)


async def main():
    settings = load_settings()

    db = EventDB(settings.database.path)
    await db.init()

    claude = ClaudeClient(
        api_key=settings.anthropic_api_key,
        model=settings.claude.model,
        max_response_chars=settings.claude.max_response_chars,
        taxonomy=settings.event_taxonomy,
    )

    history = ConversationHistory(max_turns=6, max_age_minutes=15)
    nodes = NodeTracker()

    # Resolved after mesh/reticulum are constructed so callbacks can reference them
    _mesh_ref: list[MeshCoreClient] = []
    _reticulum_ref: list[ReticulumClient] = []

    def _is_channel_broadcastable(event: Event) -> bool:
        """Only serious accidents and weather warnings go out on the public channel.

        Other event types (eesti.ee sitrep, RSS news, minor tarktee hazards) are still
        stored and remain eligible for targeted critical PMs and bot Q&A — they just
        don't get broadcast to #kriis / the Reticulum group.
        """
        if event.source == "weather":
            return True
        if event.event_type == "road_accident" and event.severity == "high":
            return True
        return False

    def _format_official_alert(event: Event) -> str:
        """Return raw source text for official events — no AI paraphrasing."""
        parts = []
        if event.title:
            parts.append(event.title)
        if event.description:
            parts.append(event.description)
        return " | ".join(parts) if parts else event.raw_text

    async def on_new_events(events: list[Event]):
        for event in events:
            if event.trust_level == "official":
                # Official warnings must go out verbatim
                alert = _format_official_alert(event)
            else:
                # User reports: Claude sanitises and formats
                alert = await claude.alert_for_event(event)
            if _is_channel_broadcastable(event):
                logger.info("Broadcasting alert: %s", alert[:60])
                if _mesh_ref:
                    await _mesh_ref[0].send_channel(alert)
                if _reticulum_ref:
                    await _reticulum_ref[0].send_broadcast(alert)
            else:
                logger.info("Stored (not broadcast to channel): %s", alert[:60])

            # Targeted PM to nearby companion nodes for life-threatening events
            if (
                settings.targeted_alerts.enabled
                and event.event_type in settings.targeted_alerts.critical_event_types
                and _mesh_ref
            ):
                # Use precise geocoded coords if available, fall back to county centre
                if event.lat and event.lon:
                    coords = (event.lat, event.lon)
                else:
                    coords = _county_coords(event.location or "")
                if coords:
                    nearby = nodes.get_nodes_near(*coords, settings.targeted_alerts.radius_km)
                    if nearby:
                        logger.info(
                            "Sending targeted PM to %d companion(s) near %s",
                            len(nearby), event.location,
                        )
                        pm_text = f"OHUHOIATUS: {alert}"
                        for node in nearby:
                            await _mesh_ref[0].send_pm(node.pubkey_prefix, pm_text)
                            await asyncio.sleep(1.0)

            await asyncio.sleep(3.0)

    async def on_new_report(event: Event):
        # Generate a sanitized, anonymized broadcast — no personal details
        alert = await claude.alert_for_event(event)
        broadcast = f"[KONTROLLIMATA] {alert}"
        logger.info("Broadcasting user report: %s", broadcast[:60])
        if _mesh_ref:
            await _mesh_ref[0].send_channel(broadcast)
        if _reticulum_ref:
            await _reticulum_ref[0].send_broadcast(broadcast)

    user_report_store = UserReportStore(
        report_trigger=settings.user_reports.report_trigger,
        cooldown_seconds=settings.user_reports.cooldown_seconds,
        max_stored=settings.user_reports.max_stored,
        max_age_hours=settings.user_reports.max_age_hours,
        session_timeout_minutes=settings.user_reports.session_timeout_minutes,
        db=db,
        claude=claude,
        on_new_report=on_new_report,
    )

    mention = settings.meshcore.bot_mention.lower()

    async def on_channel_message(pubkey: str, text: str):
        """Handle public #kriis channel messages — only react when mentioned."""
        mesh = _mesh_ref[0]
        if user_report_store.is_report_trigger(text):
            await mesh.send_channel("Raporteerimiseks saada mulle erasõnum.")
            return
        if mention not in text.lower():
            return  # users talking to each other — stay silent
        question = text.lower().replace(mention, "").strip() or text.strip()
        history.add(pubkey, "user", question)
        active = await db.get_active_events()
        reports = user_report_store.get_recent()
        turns = history.get_turns(pubkey)
        answer = await claude.answer_question(question, active, reports, history=turns)
        history.add(pubkey, "assistant", answer)
        await mesh.send_channel(answer)

    async def on_pm(sender_id: str, text: str, send_reply):
        """Handle private messages — freeform Q&A and report intake, no trigger needed.

        Transport-agnostic: sender_id and send_reply come from whichever
        client (MeshCore or Reticulum) received the message, so both share
        the same conversation/report-intake state keyed by sender_id.
        """
        active = await db.get_active_events()
        reports = user_report_store.get_recent()
        reply = await user_report_store.handle_pm(sender_id, text, active, reports)
        await send_reply(sender_id, reply)

    async def on_meshcore_pm(pubkey: str, text: str):
        await on_pm(pubkey, text, _mesh_ref[0].send_pm)

    async def on_reticulum_pm(dest_hash: str, text: str):
        await on_pm(dest_hash, text, _reticulum_ref[0].send_pm)

    async def on_reticulum_group_message(text: str):
        """Handle messages relayed via the LXMF distribution group — only react when mentioned.

        The group relays every member's message under its own identity, so unlike a
        real PM there's no way to tell members apart — no per-sender conversation
        history is kept, and replies go back to the group for everyone to see.
        """
        if mention not in text.lower():
            return
        question = text.lower().replace(mention, "").strip() or text.strip()
        active = await db.get_active_events()
        reports = user_report_store.get_recent()
        answer = await claude.answer_question(question, active, reports, history=[])
        await _reticulum_ref[0].send_broadcast(answer)

    async def on_contacts_updated(contacts: dict):
        await nodes.update_from_contacts(contacts)
        logger.info("Node tracker updated: %d companion(s) with position", nodes.count())

    mesh = MeshCoreClient(
        port=settings.meshcore.port,
        channel_name=settings.meshcore.channel,
        on_channel_message=on_channel_message,
        on_pm=on_meshcore_pm,
        on_contacts_updated=on_contacts_updated,
    )
    _mesh_ref.append(mesh)

    try:
        await mesh.connect()
        await mesh.discover_radio_limits()
        await mesh.discover_channel()
        await mesh.start()
    except Exception:
        await mesh.disconnect()
        raise

    reticulum = None
    if settings.reticulum.enabled:
        reticulum = ReticulumClient(
            config_dir=settings.reticulum.config_dir,
            identity_dir=settings.reticulum.identity_dir,
            display_name=settings.reticulum.display_name,
            distribution_group_hash=settings.reticulum.distribution_group_hash or None,
            on_pm=on_reticulum_pm,
            on_group_message=on_reticulum_group_message,
            propagation_node_hash=settings.reticulum.propagation_node_hash or None,
        )
        try:
            await reticulum.connect()
            _reticulum_ref.append(reticulum)
        except Exception:
            # Reticulum is a secondary transport — MeshCore must keep running without it.
            logger.exception("Reticulum connect failed — continuing with MeshCore only")
            reticulum = None

    crisis_fetcher = CrisisFetcher(
        url=settings.eesti_ee.url,
        poll_interval=settings.eesti_ee.poll_interval_seconds,
        db=db,
        claude=claude,
        on_new_events=on_new_events if settings.eesti_ee.enabled else None,
    )

    rss_fetcher = RssFetcher(
        feeds=settings.rss_feeds,
        poll_interval=settings.eesti_ee.poll_interval_seconds,
        db=db,
        claude=claude,
        on_new_events=on_new_events,
    )

    stop_event = asyncio.Event()

    def _handle_sig(*_):
        logger.info("Shutdown signal received")
        stop_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_sig)
        except NotImplementedError:
            # Windows — Ctrl+C arrives as KeyboardInterrupt, handled below
            pass

    weather_fetcher = WeatherFetcher(
        poll_interval=settings.weather.poll_interval_seconds,
        db=db,
        claude=claude,
        on_new_events=on_new_events,
        api_key=settings.weather.api_key or None,
    )

    tarktee_fetcher = TarkteeFetcher(
        poll_interval=settings.tarktee.poll_interval_seconds,
        db=db,
        claude=claude,
        on_new_events=on_new_events,
        accidents_enabled=settings.tarktee.accidents_enabled,
        hazards_enabled=settings.tarktee.hazards_enabled,
    )

    tasks = []
    if settings.eesti_ee.enabled:
        tasks.append(asyncio.create_task(crisis_fetcher.run(), name="crisis_fetcher"))
    tasks.append(asyncio.create_task(rss_fetcher.run(), name="rss_fetcher"))
    if settings.weather.enabled:
        tasks.append(asyncio.create_task(weather_fetcher.run(), name="weather_fetcher"))
    if settings.tarktee.enabled:
        tasks.append(asyncio.create_task(tarktee_fetcher.run(), name="tarktee_fetcher"))
    tasks.append(asyncio.create_task(
        mesh.run_periodic_advert(settings.meshcore.advert_interval_seconds),
        name="advert",
    ))
    tasks.append(asyncio.create_task(
        mesh.run_watchdog(interval_seconds=60),
        name="watchdog",
    ))
    if reticulum:
        tasks.append(asyncio.create_task(
            reticulum.run_periodic_announce(settings.reticulum.announce_interval_seconds),
            name="reticulum_announce",
        ))

    logger.info(
        "Kriisibot running — channel '%s' on %s, Reticulum %s (Ctrl+C to stop)",
        settings.meshcore.channel,
        settings.meshcore.port,
        "connected" if reticulum else "disabled",
    )

    try:
        await stop_event.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        logger.info("Shutting down...")
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await mesh.disconnect()
        if reticulum:
            await reticulum.disconnect()
        logger.info("Disconnected — port released.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass  # clean shutdown already handled inside main()
