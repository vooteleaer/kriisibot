import logging
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
