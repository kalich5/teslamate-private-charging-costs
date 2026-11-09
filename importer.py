import argparse
import logging
import os
import sys

import psycopg2
import yaml
from datetime import datetime, timezone

from geo import find_location
from pricing import calculate_cost_profile, energy_profile, validate_location
from fx import convert

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


def load_config(path="config.yaml"):
    with open(path) as f:
        return yaml.safe_load(f)


def db_connect(cfg):
    return psycopg2.connect(
        host=cfg.get("host", "database"),
        port=cfg.get("port", 5432),
        dbname=cfg.get("dbname", "teslamate"),
        user=cfg.get("user", "teslamate"),
        password=cfg.get("password", "teslamate"),
    )


# TeslaMate links every charging process to the position recorded when it
# started (charging_processes.position_id), so a direct join is enough — no
# need to scan the (very large) positions table per session.
FETCH_QUERY = """
SELECT
    cp.id,
    cp.start_date,
    cp.end_date,
    p.latitude,
    p.longitude,
    cp.charge_energy_added,
    cp.charge_energy_used
FROM charging_processes cp
LEFT JOIN positions p ON p.id = cp.position_id
WHERE cp.cost IS NULL
  AND cp.end_date IS NOT NULL
  AND (cp.charge_energy_added > 0 OR cp.charge_energy_used > 0)
ORDER BY cp.id
"""

# Per-sample charging data, used to find *when* the energy was actually
# delivered (e.g. plugged in at 18:00 but scheduled charging starts at 00:00).
CHARGES_QUERY = """
SELECT date, charge_energy_added, charger_power
FROM charges
WHERE charging_process_id = %s
ORDER BY date
"""

UPDATE_QUERY = "UPDATE charging_processes SET cost = %s WHERE id = %s"


def to_float(value):
    """Convert DB numeric values (psycopg2 returns ``Decimal``) to float; keep None."""
    return None if value is None else float(value)


def billed_energy(added, used):
    """
    Return the kWh to bill for a session.

    The bill follows the electricity meter, i.e. energy drawn from the grid
    including charging losses (``charge_energy_used``).  Falls back to
    ``charge_energy_added`` when ``used`` is missing, and never goes below
    ``added`` (``used`` is computed by TeslaMate from sparse power samples and
    can come out too low).
    """
    candidates = [v for v in (to_float(used), to_float(added)) if v is not None]
    return max(candidates) if candidates else None


def as_utc(dt: datetime) -> datetime:
    """
    Return *dt* as a timezone-aware UTC datetime.

    TeslaMate stores timestamps as ``timestamp without time zone`` in UTC, so
    psycopg2 returns naive datetimes.  These must be tagged as UTC — calling
    ``.astimezone()`` on them would interpret them in the host's local zone.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def process_session(session, locations, base_currency, load_samples=None):
    """
    Price one charging session.

    *load_samples* is an optional callable ``(session_id) -> [(date, energy, power)]``
    returning the session's ``charges`` rows.  It is only called for sessions
    that match a location.  Without samples, energy is spread evenly.
    """
    lat = session.get("latitude")
    lon = session.get("longitude")

    location = find_location(lat, lon, locations)
    if not location:
        log.debug("Session %s: no matching location (lat=%s, lon=%s)", session["id"], lat, lon)
        return None

    start = as_utc(session["start_date"])
    end = as_utc(session["end_date"])

    if end <= start:
        log.warning("Session %s: end_date (%s) <= start_date (%s), skipping", session["id"], end, start)
        return None

    kwh = session.get("kwh", 0)
    if not kwh:
        log.warning("Session %s: energy_added is zero, skipping", session["id"])
        return None

    samples = load_samples(session["id"]) if load_samples else []
    samples = [(as_utc(d), e, p) for d, e, p in samples if d is not None]
    profile = energy_profile(samples, start, end, kwh)
    if len(profile) == 1 and samples:
        log.debug("Session %s: charges samples unusable, spreading energy evenly", session["id"])

    local_cost = calculate_cost_profile(profile, location)

    final_cost = convert(
        local_cost,
        location["currency"],
        base_currency,
        start,
    )

    return final_cost, location["name"]


def main():
    arg_parser = argparse.ArgumentParser(
        description="Calculate and write home charging costs into TeslaMate",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Preview what would be written without touching the database
  python importer.py --dry-run

  # Use a custom config file location
  python importer.py --config /data/my_config.yaml

  # Verbose output (show skipped sessions)
  python importer.py --verbose
        """,
    )
    arg_parser.add_argument(
        "--dry-run", "-n",
        action="store_true",
        help="Preview changes without writing to the database",
    )
    arg_parser.add_argument(
        "--config", "-c",
        default="config.yaml",
        metavar="FILE",
        help="Path to config.yaml (default: config.yaml)",
    )
    arg_parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show debug messages including skipped sessions",
    )
    args = arg_parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    config = load_config(args.config)
    locations = config["LOCATIONS"]
    base_currency = config["BASE_CURRENCY"]

    # Tariff times are local wall-clock times.  Each location may override the
    # global TIMEZONE (e.g. a cottage abroad).
    default_tz = config.get("TIMEZONE")
    for loc in locations:
        loc.setdefault("timezone", default_tz)
        if not loc["timezone"]:
            log.error("Location '%s' has no timezone. Set TIMEZONE (e.g. Europe/Prague) "
                      "in config.yaml or 'timezone' on the location.", loc.get("name"))
            sys.exit(1)
        # Fail fast on config errors (gaps, overlaps, midnight-crossing blocks…)
        # instead of writing wrong costs that would never be recalculated.
        try:
            validate_location(loc)
        except ValueError as e:
            log.error("Invalid config: %s", e)
            sys.exit(1)
    db_cfg = config.get("DATABASE", {})

    log.info("=" * 50)
    log.info("  TeslaMate Private Charging Cost Importer")
    log.info("  DB:        %s@%s:%s/%s",
             db_cfg.get("user", "teslamate"),
             db_cfg.get("host", "database"),
             db_cfg.get("port", 5432),
             db_cfg.get("dbname", "teslamate"))
    log.info("  Currency:  %s", base_currency)
    log.info("  Locations: %s", ", ".join(f'{loc["name"]} ({loc["timezone"]})' for loc in locations))
    if args.dry_run:
        log.info("  Mode:      DRY-RUN (no writes)")
    log.info("=" * 50)

    try:
        conn = db_connect(db_cfg)
    except psycopg2.OperationalError as e:
        log.error("Cannot connect to database: %s", e)
        sys.exit(1)

    cur = conn.cursor()

    def load_samples(session_id):
        cur.execute(CHARGES_QUERY, (session_id,))
        return cur.fetchall()

    cur.execute(FETCH_QUERY)
    rows = cur.fetchall()
    log.info("Found %d charging session(s) with missing cost.", len(rows))

    updated = 0
    skipped = 0
    errors  = 0

    for row in rows:
        tm_id, start, end, lat, lon, added, used = row

        session = {
            "id":         tm_id,
            "start_date": start,
            "end_date":   end,
            "latitude":   to_float(lat),
            "longitude":  to_float(lon),
            "kwh":        billed_energy(added, used),
        }

        try:
            result = process_session(session, locations, base_currency, load_samples)
        except Exception as e:
            log.error("Session %s: unexpected error — %s", tm_id, e)
            errors += 1
            continue

        if result is None:
            skipped += 1
            continue

        cost, location_name = result

        if args.dry_run:
            log.info("  DRY-RUN  Session %s @ %s -> %.4f %s", tm_id, location_name, cost, base_currency)
            updated += 1
            continue

        try:
            cur.execute(UPDATE_QUERY, (cost, tm_id))
            # Commit immediately after each successful update so that a DB error
            # on a later row cannot roll back costs that have already been written.
            conn.commit()
            log.info("  UPDATED  Session %s @ %s -> %.4f %s", tm_id, location_name, cost, base_currency)
            updated += 1
        except Exception as e:
            log.error("DB error updating session %s: %s", tm_id, e)
            conn.rollback()
            errors += 1

    cur.close()
    conn.close()

    log.info("-" * 50)
    if args.dry_run:
        log.info("  DRY-RUN SUMMARY")
        log.info("  Would be updated: %d", updated)
    else:
        log.info("  SUMMARY")
        log.info("  Updated:  %d", updated)
    log.info("  Skipped:  %d", skipped)
    if errors:
        log.warning("  Errors:   %d", errors)
    log.info("-" * 50)


if __name__ == "__main__":
    main()
