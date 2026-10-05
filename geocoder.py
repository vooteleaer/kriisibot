import asyncio
import logging
import math
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


async def reverse_geocode_parts(lat: float, lon: float) -> tuple[Optional[str], Optional[str]]:
    """Return (road, "settlement, municipality") for coordinates (Nominatim)."""
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
        return road, admin
    except Exception:
        logger.warning("Reverse geocode failed for %.4f,%.4f", lat, lon, exc_info=True)
        return None, None


async def reverse_geocode(lat: float, lon: float) -> Optional[str]:
    """Return a short human-readable location string from coordinates (Nominatim)."""
    road, admin = await reverse_geocode_parts(lat, lon)
    return ", ".join(p for p in [road, admin] if p) or None


_OVERPASS_URL = "https://overpass-api.de/api/interpreter"
_AMENITIES = (
    "fuel|school|kindergarten|place_of_worship|hospital|police|fire_station|townhall|"
    "community_centre|hotel|museum|marketplace|library|pharmacy"
)
_SETTLEMENT_KINDS = {"city", "town", "village", "suburb", "neighbourhood", "hamlet"}
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


async def nearby_places(lat: float, lon: float, limit: int = 8) -> list[dict]:
    """Named settlements (5 km) and well-known landmarks (500 m) around a point (Overpass).

    Each item: name, kind, distance_m, direction (where the point lies relative to the place).
    Returns [] on any failure — callers fall back to plain reverse geocoding.
    """
    # Plain key filters only — regex-on-key queries get 429/504 from the public server
    query = f"""[out:json][timeout:15];
(
  node(around:5000,{lat},{lon})[place~"^(city|town|village|suburb|neighbourhood|hamlet)$"][name];
  nw(around:500,{lat},{lon})[amenity~"^({_AMENITIES})$"][name];
  nw(around:500,{lat},{lon})[shop~"^(supermarket|convenience|mall)$"][name];
  node(around:500,{lat},{lon})[railway~"^(station|halt)$"][name];
  node(around:500,{lat},{lon})[highway=bus_stop][name];
);
out center tags;"""
    elements = None
    async with httpx.AsyncClient(timeout=25, headers=_HEADERS) as client:
        for attempt in range(2):
            try:
                resp = await client.post(_OVERPASS_URL, data={"data": query})
                resp.raise_for_status()
                elements = resp.json().get("elements", [])
                break
            except Exception as e:
                logger.warning("Overpass lookup failed for %.4f,%.4f (attempt %d): %s", lat, lon, attempt + 1, e)
                if attempt == 0:
                    await asyncio.sleep(10)
    if elements is None:
        return []

    places: dict[tuple[str, str], dict] = {}
    for el in elements:
        center = el.get("center", el)
        tags = el.get("tags", {})
        if "lat" not in center or "name" not in tags:
            continue
        kind = (
            tags.get("place") or tags.get("amenity") or tags.get("shop") or tags.get("railway")
            or tags.get("highway") or "objekt"
        )
        dist = _distance_m(lat, lon, center["lat"], center["lon"])
        key = (tags["name"], kind)
        # Bus stops and the like come in pairs — keep the closest of each name+kind
        if key in places and places[key]["distance_m"] <= dist:
            continue
        places[key] = {
            "name": tags["name"],
            "kind": kind,
            "distance_m": dist,
            "direction": _direction_from(center["lat"], center["lon"], lat, lon),
        }
    by_distance = sorted(places.values(), key=lambda p: p["distance_m"])
    # Always keep the nearest settlements so "X km from <village>" stays possible
    settlements = [p for p in by_distance if p["kind"] in _SETTLEMENT_KINDS][:3]
    landmarks = [p for p in by_distance if p["kind"] not in _SETTLEMENT_KINDS][: limit - len(settlements)]
    return sorted(settlements + landmarks, key=lambda p: p["distance_m"])


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
