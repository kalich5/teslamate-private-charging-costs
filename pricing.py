import logging
from datetime import datetime, timedelta, time
from functools import lru_cache
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

MINUTES_PER_DAY = 24 * 60


def parse_time(t: str) -> int:
    """
    Parse an ``HH:MM`` string into minutes since midnight (0–1440).

    ``"24:00"`` is allowed and means end of day (1440).
    """
    t = t.strip()
    if t == "24:00":
        return MINUTES_PER_DAY
    parsed = datetime.strptime(t, "%H:%M").time()
    return parsed.hour * 60 + parsed.minute


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
    """
    Return the tariff block that covers *current_time*, or None.

    Blocks are half-open intervals ``[from, to)``, so a boundary minute such as
    06:00 belongs to the block that *starts* at 06:00.
    """
    minute = current_time.hour * 60 + current_time.minute + current_time.second / 60
    for block in blocks:
        if parse_time(block["from"]) <= minute < parse_time(block["to"]):
            return block
    return None


def calculate_cost(start: datetime, end: datetime, kwh: float, location: dict) -> float:
    """
    Calculate the electricity cost for a charging session.

    The session is split into buckets aligned to whole clock minutes (the
    first and last bucket may be shorter).  Energy is spread evenly over the
    session and each bucket is priced at the tariff active during it.  As
    tariff boundaries fall on whole minutes, every bucket lies in exactly
    one tariff block.

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
    duration_s = (end - start).total_seconds()
    if duration_s <= 0:
        return 0.0

    tz = location_zone(location)
    kwh_per_s = kwh / duration_s
    current = start
    cost = 0.0
    gap_minutes = 0.0

    while current < end:
        # Advance to the next whole minute (or the session end).
        next_minute = current.replace(second=0, microsecond=0) + timedelta(minutes=1)
        step_end = min(next_minute, end)
        step_s = (step_end - current).total_seconds()

        local = current.astimezone(tz)
        blocks = get_tariff_blocks(location, local)
        tariff = find_tariff(blocks, local.time())

        if tariff is None:
            gap_minutes += step_s / 60
        else:
            cost += kwh_per_s * step_s * tariff["price"]

        current = step_end

    if gap_minutes:
        log.warning(
            "Location '%s': %.1f minute(s) had no matching tariff and were priced at 0. "
            "Check your tariff config for gaps.",
            location["name"],
            gap_minutes,
        )

    return round(cost, 4)
