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


def energy_profile(samples: list, start: datetime, end: datetime, total_kwh: float) -> list:
    """
    Distribute *total_kwh* over time using TeslaMate ``charges`` samples.

    Args:
        samples:   ``[(date, charge_energy_added, charger_power_kw), ...]`` sorted
                   by date; dates are timezone-aware, energy is cumulative kWh.
        start/end: Session boundaries, used for the uniform fallback.
        total_kwh: Energy to distribute (the profile is scaled to this total).

    Returns:
        ``[(segment_start, segment_end, kwh), ...]``.

    Each interval between two consecutive samples is weighted by the increase
    of the cumulative ``charge_energy_added``.  If that gives nothing usable,
    the average ``charger_power`` × duration is used instead.  If the samples
    carry no information at all, the energy is spread evenly over the session.
    Periods where the car is plugged in but not charging thus get no energy.
    """
    total_kwh = float(total_kwh)
    uniform = [(start, end, total_kwh)]

    def weighted(pairs):
        return [(a, b, w) for a, b, w in pairs if w > 0 and b > a]

    # 1) Increase of cumulative energy between consecutive samples.
    with_energy = [(d, float(e)) for d, e, _ in samples if e is not None]
    segments = weighted(
        (d1, d2, e2 - e1) for (d1, e1), (d2, e2) in zip(with_energy, with_energy[1:])
    )

    # 2) Fallback: average charger power × time.
    if not segments:
        with_power = [(d, float(p)) for d, _, p in samples if p is not None]
        segments = weighted(
            (d1, d2, (p1 + p2) / 2 * (d2 - d1).total_seconds() / 3600)
            for (d1, p1), (d2, p2) in zip(with_power, with_power[1:])
        )

    if not segments:
        return uniform

    weight_sum = sum(w for _, _, w in segments)
    scale = total_kwh / weight_sum
    return [(a, b, w * scale) for a, b, w in segments]


def calculate_cost(start: datetime, end: datetime, kwh: float, location: dict) -> float:
    """
    Calculate the cost of a session with energy spread evenly over its duration.

    See :func:`calculate_cost_profile` for details; this is the single-segment case.
    """
    return calculate_cost_profile([(start, end, kwh)], location)


def calculate_cost_profile(segments: list, location: dict) -> float:
    """
    Calculate the electricity cost of an energy profile.

    Each segment ``(start, end, kwh)`` is split into buckets aligned to whole
    clock minutes (the first and last bucket may be shorter).  The segment's
    energy is spread evenly over its buckets and each bucket is priced at the
    tariff active during it.  As tariff boundaries fall on whole minutes,
    every bucket lies in exactly one tariff block.

    Args:
        segments: ``[(start, end, kwh), ...]`` with timezone-aware datetimes.
        location: Location config dict (must contain 'tariffs', 'name' and
                  'timezone').  Tariff times and weekday/weekend are evaluated
                  in the location's local time, including DST changes.

    Returns:
        Total cost in the location's currency (rounded to 4 decimal places).
    """
    tz = location_zone(location)
    cost = 0.0
    gap_minutes = 0.0

    for start, end, kwh in segments:
        kwh = float(kwh)  # psycopg2 returns numeric columns as Decimal
        duration_s = (end - start).total_seconds()
        if duration_s <= 0:
            continue

        kwh_per_s = kwh / duration_s
        current = start

        while current < end:
            # Advance to the next whole minute (or the segment end).
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
