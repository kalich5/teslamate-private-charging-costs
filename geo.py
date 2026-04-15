import math
import logging

log = logging.getLogger(__name__)


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return the great-circle distance in metres between two GPS coordinates."""
    R = 6_371_000  # Earth radius in metres
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)

    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def find_location(lat: float | None, lon: float | None, locations: list) -> dict | None:
    """
    Match GPS coordinates against configured locations.

    Returns the first location whose geofence contains (lat, lon),
    or None if no match is found or coordinates are missing.
    """
    if lat is None or lon is None:
        log.debug("GPS coordinates missing — cannot match location.")
        return None

    for loc in locations:
        dist = distance_m(lat, lon, loc["lat"], loc["lon"])
        if dist <= loc["radius_m"]:
            log.debug("Matched location '%s' (distance: %.0f m).", loc["name"], dist)
            return loc

    log.debug("No location matched for lat=%s, lon=%s.", lat, lon)
    return None
