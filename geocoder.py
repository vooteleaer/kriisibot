import logging
import math
import re
from dataclasses import dataclass
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_URL = "https://inaadress.maaamet.ee/inaadress/gazetteer"
_HEADERS = {"User-Agent": "kriisibot/1.0"}


@dataclass
class GeoResult:
    lat: float
    lon: float
    address: str        # normalized full address from In-ADS
    quality: str        # e.g. "tapne_nr" (exact), "tänav" (street only)


# "Jõhvi linn" → "Jõhvi", "Vardja küla" → "Vardja"; the official suffix only adds length
_SETTLEMENT_SUFFIX_RE = re.compile(r"\s+(linn|alev|alevik|küla)$")


async def reverse_geocode_parts(
    lat: float, lon: float
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Return (road, "settlement, municipality", town) for coordinates (Nominatim).

    town is set only when the point lies within a town or city — unlike villages, which in
    Estonia cover all rural land, that reliably means "in the settlement".
    """
    try:
        async with httpx.AsyncClient(
            timeout=10,
            headers={"User-Agent": "kriisibot/1.0", "Accept-Language": "et"},
        ) as client:
            resp = await client.get(
                "https://nominatim.openstreetmap.org/reverse",
                params={"lat": lat, "lon": lon, "format": "json", "zoom": 16},
            )
            resp.raise_for_status()
            data = resp.json()
        addr = data.get("address", {})
        road = (
            addr.get("road")
            or addr.get("motorway")
            or addr.get("trunk")
            or addr.get("primary")
        )
        # Estonian villages often come back as city_district/hamlet rather than village
        place = (
            addr.get("suburb")
            or addr.get("village")
            or addr.get("hamlet")
            or addr.get("city_district")
            or addr.get("town")
            or addr.get("city")
        )
        municipality = addr.get("municipality") or addr.get("county")
        admin = ", ".join(p for p in [place, municipality] if p) or None
        town = addr.get("city") or addr.get("town")
        if town:
            # "Jõhvi linn" → "Jõhvi"; the official suffix only adds length
            town = _SETTLEMENT_SUFFIX_RE.sub("", town)
        return road, admin, town
    except Exception:
        logger.warning("Reverse geocode failed for %.4f,%.4f", lat, lon, exc_info=True)
        return None, None, None


async def reverse_geocode(lat: float, lon: float) -> Optional[str]:
    """Return a short human-readable location string from coordinates (Nominatim)."""
    road, admin, _ = await reverse_geocode_parts(lat, lon)
    return ", ".join(p for p in [road, admin] if p) or None


_DIRECTIONS = ["põhjas", "kirdes", "idas", "kagus", "lõunas", "edelas", "läänes", "loodes"]


def _distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> int:
    dy = math.radians(lat2 - lat1)
    dx = math.radians(lon2 - lon1) * math.cos(math.radians(lat1))
    return int(6371000 * math.hypot(dx, dy))


def _direction_from(lat_from: float, lon_from: float, lat_to: float, lon_to: float) -> str:
    """Compass direction of the 'to' point as seen from the 'from' point."""
    dy = lat_to - lat_from
    dx = (lon_to - lon_from) * math.cos(math.radians(lat_from))
    bearing = math.degrees(math.atan2(dx, dy)) % 360
    return _DIRECTIONS[round(bearing / 45) % 8]


async def settlement_at(lat: float, lon: float) -> Optional[dict]:
    """The village/town whose area contains the point, with its centre (Nominatim, zoom 13).

    Returns {name, distance_m, direction} — direction is where the point lies as seen from
    the settlement centre — or None on failure.
    """
    try:
        async with httpx.AsyncClient(
            timeout=10,
            headers={"User-Agent": "kriisibot/1.0", "Accept-Language": "et"},
        ) as client:
            resp = await client.get(
                "https://nominatim.openstreetmap.org/reverse",
                params={"lat": lat, "lon": lon, "format": "json", "zoom": 13},
            )
            resp.raise_for_status()
            data = resp.json()
        # Ranks 16–20 are towns, villages and hamlets; below that it is a municipality or county
        rank = data.get("place_rank", 0)
        if not data.get("name") or not data.get("lat") or not 16 <= rank <= 20:
            return None
        clat, clon = float(data["lat"]), float(data["lon"])
        return {
            "name": _SETTLEMENT_SUFFIX_RE.sub("", data["name"]),
            "distance_m": _distance_m(lat, lon, clat, clon),
            "direction": _direction_from(clat, clon, lat, lon),
        }
    except Exception:
        logger.warning("Settlement lookup failed for %.4f,%.4f", lat, lon, exc_info=True)
        return None


async def geocode(location_text: str) -> Optional[GeoResult]:
    """Look up a location using the Estonian Land Board In-ADS API.

    Returns None if the address cannot be resolved to a real Estonian location.
    """
    if not location_text or not location_text.strip():
        return None
    try:
        async with httpx.AsyncClient(timeout=10, headers=_HEADERS) as client:
            resp = await client.get(
                _URL,
                params={"address": location_text.strip(), "results": 1},
            )
            resp.raise_for_status()
            data = resp.json()

        addresses = data.get("addresses", [])
        if not addresses:
            logger.debug("Geocode: no result for %r", location_text)
            return None

        hit = addresses[0]
        lat = hit.get("viitepunkt_b")
        lon = hit.get("viitepunkt_l")
        if not lat or not lon:
            return None

        return GeoResult(
            lat=float(lat),
            lon=float(lon),
            address=hit.get("taisaadress") or hit.get("pikkaadress") or location_text,
            quality=hit.get("kvaliteet", ""),
        )
    except Exception:
        logger.warning("Geocode failed for %r", location_text, exc_info=True)
        return None
