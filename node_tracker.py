import asyncio
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Approximate centres of Estonian counties (lat, lon) for location → coords fallback
_COUNTY_CENTRES: dict[str, tuple[float, float]] = {
    "harju":       (59.411,  24.745),
    "tallinn":     (59.437,  24.754),
    "tartu":       (58.378,  26.729),
    "ida-viru":    (59.359,  27.419),
    "narva":       (59.377,  28.179),
    "pärnu":       (58.385,  24.500),
    "lääne":       (58.967,  23.541),
    "rapla":       (58.999,  24.800),
    "järva":       (58.880,  25.550),
    "lääne-viru":  (59.350,  26.330),
    "põlva":       (58.062,  27.060),
    "võru":        (57.833,  26.993),
    "valga":       (57.778,  26.047),
    "viljandi":    (58.364,  25.590),
    "jõgeva":      (58.745,  26.395),
    "saare":       (58.455,  22.562),
    "hiiu":        (58.924,  22.592),
}


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = math.sin(d_lat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(d_lon / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def location_to_coords(location_text: str) -> tuple[float, float] | None:
    """Best-effort: match a location string to Estonian county centre coordinates."""
    text = location_text.lower()
    for county, coords in _COUNTY_CENTRES.items():
        if county in text:
            return coords
    return None


@dataclass
class NodeInfo:
    pubkey_prefix: str
    name: str
    lat: float
    lon: float
    last_seen: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class NodeTracker:
    def __init__(self):
        self._nodes: dict[str, NodeInfo] = {}
        self._lock = asyncio.Lock()

    async def update_from_contacts(self, contacts: dict):
        """Ingest the contacts dict returned by get_contacts(). Only companion (CLI) nodes."""
        async with self._lock:
            for full_key, contact in contacts.items():
                # type 1 = CLI (companion radio); skip repeaters, rooms, sensors
                if contact.get("type", -1) != 1:
                    continue
                lat = contact.get("adv_lat", 0.0)
                lon = contact.get("adv_lon", 0.0)
                if lat == 0.0 and lon == 0.0:
                    continue  # no position data
                prefix = full_key[:12]
                name = contact.get("adv_name", prefix)
                self._nodes[prefix] = NodeInfo(
                    pubkey_prefix=prefix,
                    name=name,
                    lat=lat,
                    lon=lon,
                )
        logger.debug("Node tracker: %d nodes with position", len(self._nodes))

    def get_nodes_near(self, lat: float, lon: float, radius_km: float) -> list[NodeInfo]:
        return [
            n for n in self._nodes.values()
            if _haversine_km(lat, lon, n.lat, n.lon) <= radius_km
        ]

    def all_nodes(self) -> list[NodeInfo]:
        return list(self._nodes.values())

    def count(self) -> int:
        return len(self._nodes)
