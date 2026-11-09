import logging
from datetime import datetime, timedelta, time
from functools import lru_cache
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

# Sentinel used to represent "end of day" (24:00 → 00:00 next day).
_END_OF_DAY = time(23, 59, 59, 999999)


def parse_time(t: str) -> time:
    """Parse HH:MM time string.  '24:00' is treated as end-of-day."""
    if t.strip() == "24:00":
        return _END_OF_DAY
    return datetime.strptime(t.strip(), "%H:%M").time()


@lru_cache(maxsize=None)
def _zone(name: str) -> ZoneInfo:
    return ZoneInfo(name)


def location_zone(location: dict) -> ZoneInfo:
    """Return the timezone in which the location's tariff times are defined."""
    name = location.get("timezone")
    if not name:
        raise ValueError(f"Location '{location['name']}' has no 'timezone' defined.")
    return _zone(name)


def get_tariff_blocks(location: dict, dt: datetime) -> list:
    """Return the correct tariff block list for the given (local) datetime."""
    is_weekday = dt.weekday() < 5
    key = "weekday" if is_weekday else "weekend"
    blocks = location["tariffs"].get(key, [])
    if not blocks:
        raise ValueError(
            f"Location '{location['name']}' has no tariffs defined for '{key}'."
        )
    return blocks


def find_tariff(blocks: list, current_time: time) -> dict | None:
    """Return the tariff block that covers *current_time*, or None."""
    for block in blocks:
        start = parse_time(block["from"])
        end = parse_time(block["to"])
        # Handle midnight-spanning blocks that end at 24:00 (stored as _END_OF_DAY).
        if start <= current_time <= end:
            return block
    return None


def calculate_cost(start: datetime, end: datetime, kwh: float, location: dict) -> float:
    """
    Calculate the electricity cost for a charging session.

    The session is split into 1-minute buckets.  Each bucket's energy
    consumption is priced at the tariff rate active at that minute.

    Args:
        start:    Session start (timezone-aware datetime).
        end:      Session end   (timezone-aware datetime).
        kwh:      Total energy delivered in kWh.
        location: Location config dict (must contain 'tariffs', 'name' and
                  'timezone').  Tariff times and weekday/weekend are evaluated
                  in the location's local time, including DST changes.

    Returns:
        Total cost in the location's currency (rounded to 4 decimal places).
    """
    kwh = float(kwh)  # psycopg2 returns numeric columns as Decimal
    duration_min = (end - start).total_seconds() / 60.0
    if duration_min <= 0:
        return 0.0

    tz = location_zone(location)
    kwh_per_min = kwh / duration_min
    current = start
    cost = 0.0
    gap_minutes = 0

    while current < end:
        local = current.astimezone(tz)
        blocks = get_tariff_blocks(location, local)
        tariff = find_tariff(blocks, local.time())

        if tariff is None:
            gap_minutes += 1
        else:
            cost += kwh_per_min * tariff["price"]

        current += timedelta(minutes=1)

    if gap_minutes:
        log.warning(
            "Location '%s': %d minute(s) had no matching tariff and were priced at 0. "
            "Check your tariff config for gaps.",
            location["name"],
            gap_minutes,
        )

    return round(cost, 4)
