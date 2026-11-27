import argparse
import logging
import os
import sys

import psycopg2
import yaml
from datetime import date, datetime, time, timedelta, timezone

from geo import find_location
from i18n import _
from pricing import calculate_cost_profile, energy_profile, location_zone, validate_location
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


# Environment variables override config.yaml.  Names match TeslaMate's own,
# so the same .env file can be shared with the TeslaMate stack.
DB_ENV_VARS = {
    "host": "DATABASE_HOST",
    "port": "DATABASE_PORT",
    "dbname": "DATABASE_NAME",
    "user": "DATABASE_USER",
    "password": "DATABASE_PASS",
}

DB_DEFAULTS = {
    "host": "database",
    "port": 5432,
    "dbname": "teslamate",
    "user": "teslamate",
    "password": "teslamate",
}


def db_settings(cfg: dict | None, env=os.environ) -> dict:
    """Merge DB settings: environment variables > config.yaml > defaults."""
    settings = {**DB_DEFAULTS, **(cfg or {})}
    for key, var in DB_ENV_VARS.items():
        if env.get(var):
            settings[key] = env[var]
    settings["port"] = int(settings["port"])
    return settings


def db_connect(settings):
    return psycopg2.connect(**settings)


# TeslaMate links every charging process to the position recorded when it
# started (charging_processes.position_id), so a direct join is enough — no
# need to scan the (very large) positions table per session.
_FETCH = """
SELECT
    cp.id,
    cp.start_date,
    cp.end_date,
    p.latitude,
    p.longitude,
    cp.charge_energy_added,
    cp.charge_energy_used,
    cp.cost
FROM charging_processes cp
LEFT JOIN positions p ON p.id = cp.position_id
WHERE {condition}
  AND cp.end_date IS NOT NULL
  AND (cp.charge_energy_added > 0 OR cp.charge_energy_used > 0)
ORDER BY cp.start_date
"""

# Normal run: only sessions without a cost.
FETCH_QUERY = _FETCH.format(condition="cp.cost IS NULL")

# Recalculation: every session that started in [from, to) (UTC), whatever
# its current cost.
FETCH_RANGE_QUERY = _FETCH.format(condition="cp.start_date >= %s AND cp.start_date < %s")

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

    # Use the session's local calendar date for the FX rate (a session
    # starting 00:30 CET on the 13th is 23:30 UTC on the 12th).
    final_cost = convert(
        local_cost,
        location["currency"],
        base_currency,
        start.astimezone(location_zone(location)),
    )

    return final_cost, location["name"]


class ConfigError(ValueError):
    """Raised when config.yaml is incomplete or invalid."""


def prepare_config(config: dict) -> dict:
    """
    Validate *config* and fill in derived values (per-location timezone).

    Returns a new dict; the input is not modified.  Raises ``ConfigError``.
    """
    if not isinstance(config, dict):
        raise ConfigError(_("Config is empty or not a mapping."))
    for key in ("BASE_CURRENCY", "LOCATIONS"):
        if not config.get(key):
            raise ConfigError(_("Missing '%(key)s'.", key=key))

    # Tariff times are local wall-clock times.  Each location may override the
    # global TIMEZONE (e.g. a cottage abroad).
    default_tz = config.get("TIMEZONE")
    locations = []
    for loc in config["LOCATIONS"]:
        loc = {**loc}
        loc["timezone"] = loc.get("timezone") or default_tz
        if not loc["timezone"]:
            raise ConfigError(_("Location '%(name)s' has no timezone. Set a default timezone "
                                "(e.g. Europe/Prague) or one for the location.",
                                name=loc.get("name")))
        # Fail fast on config errors (gaps, overlaps, midnight-crossing blocks…)
        # instead of writing wrong costs that would never be recalculated.
        try:
            validate_location(loc)
        except ValueError as e:
            raise ConfigError(str(e)) from e
        locations.append(loc)

    names = [loc["name"] for loc in locations]
    if len(set(names)) != len(names):
        raise ConfigError(_("Location names must be unique."))

    return {**config, "LOCATIONS": locations}


def recalculation_range(location: dict, date_from: date, date_to: date) -> tuple:
    """
    UTC bounds ``[start, end)`` (naive, like TeslaMate's columns) for the local
    dates *date_from* … *date_to* (inclusive) of *location*.
    """
    tz = location_zone(location)
    start = datetime.combine(date_from, time.min, tz)
    end = datetime.combine(date_to + timedelta(days=1), time.min, tz)
    as_naive_utc = lambda dt: dt.astimezone(timezone.utc).replace(tzinfo=None)  # noqa: E731
    return as_naive_utc(start), as_naive_utc(end)


def run_import(config: dict, dry_run: bool = False, conn=None, recalculate: dict = None) -> dict:
    """
    Price all sessions with missing cost and (unless *dry_run*) write them.

    With *recalculate* ``{"location": name, "date_from": date, "date_to": date}``
    every session at that location that started within the (local, inclusive)
    dates is priced again and its cost **overwritten**, whatever it was.

    *config* must already be prepared with :func:`prepare_config`.  If *conn*
    is not given, a connection is opened from the config / environment.

    Returns ``{"updated", "skipped", "errors", "found", "dry_run", "sessions"}``
    where ``sessions`` lists ``{"id", "start", "location", "kwh", "cost",
    "old_cost", "status", "error"}`` for every session matched to a location.

    Raises ``psycopg2.OperationalError`` if the database is unreachable.
    """
    locations = config["LOCATIONS"]
    base_currency = config["BASE_CURRENCY"]
    db_cfg = db_settings(config.get("DATABASE"))

    query, params = FETCH_QUERY, ()
    if recalculate:
        locations = [loc for loc in locations if loc["name"] == recalculate["location"]]
        if not locations:
            raise ConfigError(_("Unknown location '%(name)s'.", name=recalculate["location"]))
        query = FETCH_RANGE_QUERY
        params = recalculation_range(locations[0], recalculate["date_from"],
                                     recalculate["date_to"])

    log.info("=" * 50)
    log.info("  TeslaMate Private Charging Cost Importer")
    log.info("  DB:        %s@%s:%s/%s",
             db_cfg["user"], db_cfg["host"], db_cfg["port"], db_cfg["dbname"])
    log.info("  Currency:  %s", base_currency)
    log.info("  Locations: %s", ", ".join(f'{loc["name"]} ({loc["timezone"]})' for loc in locations))
    if recalculate:
        log.info("  Recalculate: %s, %s – %s (overwrites existing costs)", recalculate["location"],
                 recalculate["date_from"], recalculate["date_to"])
    if dry_run:
        log.info("  Mode:      DRY-RUN (no writes)")
    log.info("=" * 50)

    own_conn = conn is None
    if own_conn:
        conn = db_connect(db_cfg)

    summary = {"updated": 0, "skipped": 0, "errors": 0, "found": 0,
               "dry_run": dry_run, "recalculate": recalculate, "sessions": []}
    cur = conn.cursor()
    try:
        def load_samples(session_id):
            cur.execute(CHARGES_QUERY, (session_id,))
            return cur.fetchall()

        cur.execute(query, params)
        rows = cur.fetchall()
        summary["found"] = len(rows)
        if recalculate:
            log.info("Found %d charging session(s) in the period.", len(rows))
        else:
            log.info("Found %d charging session(s) with missing cost.", len(rows))

        for row in rows:
            tm_id, start, end, lat, lon, added, used, old_cost = row

            session = {
                "id":         tm_id,
                "start_date": start,
                "end_date":   end,
                "latitude":   to_float(lat),
                "longitude":  to_float(lon),
                "kwh":        billed_energy(added, used),
            }
            entry = {"id": tm_id, "start": start, "location": None, "kwh": session["kwh"],
                     "cost": None, "old_cost": to_float(old_cost), "status": None, "error": None}

            try:
                result = process_session(session, locations, base_currency, load_samples)
            except Exception as e:
                log.error("Session %s: unexpected error — %s", tm_id, e)
                summary["errors"] += 1
                matched = find_location(session["latitude"], session["longitude"], locations)
                entry.update(status="error", error=str(e),
                             location=matched["name"] if matched else None)
                summary["sessions"].append(entry)
                continue

            if result is None:
                summary["skipped"] += 1
                continue

            cost, location_name = result
            entry.update(location=location_name, cost=cost)

            if dry_run:
                log.info("  DRY-RUN  Session %s @ %s -> %.4f %s", tm_id, location_name, cost, base_currency)
                summary["updated"] += 1
                entry["status"] = "preview"
                summary["sessions"].append(entry)
                continue

            try:
                cur.execute(UPDATE_QUERY, (cost, tm_id))
                # Commit immediately after each successful update so that a DB error
                # on a later row cannot roll back costs that have already been written.
                conn.commit()
                log.info("  UPDATED  Session %s @ %s -> %.4f %s", tm_id, location_name, cost, base_currency)
                summary["updated"] += 1
                entry["status"] = "updated"
            except Exception as e:
                log.error("DB error updating session %s: %s", tm_id, e)
                conn.rollback()
                summary["errors"] += 1
                entry.update(status="error", error=str(e))
            summary["sessions"].append(entry)
    finally:
        cur.close()
        if own_conn:
            conn.close()

    log.info("-" * 50)
    if dry_run:
        log.info("  DRY-RUN SUMMARY")
        log.info("  Would be updated: %d", summary["updated"])
    else:
        log.info("  SUMMARY")
        log.info("  Updated:  %d", summary["updated"])
    log.info("  Skipped:  %d", summary["skipped"])
    if summary["errors"]:
        log.warning("  Errors:   %d", summary["errors"])
    log.info("-" * 50)
    return summary


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

  # Recalculate (and OVERWRITE) all costs at "home" in 2025
  python importer.py --recalculate home --from 2025-01-01 --to 2025-12-31 --dry-run
        """,
    )
    arg_parser.add_argument(
        "--dry-run", "-n",
        action="store_true",
        help="Preview changes without writing to the database",
    )
    arg_parser.add_argument(
        "--config", "-c",
        default=os.environ.get("CONFIG_PATH", "config.yaml"),
        metavar="FILE",
        help="Path to config.yaml (default: $CONFIG_PATH or config.yaml)",
    )
    arg_parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show debug messages including skipped sessions",
    )
    arg_parser.add_argument(
        "--recalculate",
        metavar="LOCATION",
        help="Recalculate all charges at LOCATION between --from and --to, "
             "overwriting existing costs",
    )
    arg_parser.add_argument("--from", dest="date_from", type=date.fromisoformat,
                            metavar="YYYY-MM-DD", help="First day to recalculate (local date)")
    arg_parser.add_argument("--to", dest="date_to", type=date.fromisoformat,
                            metavar="YYYY-MM-DD", help="Last day to recalculate (default: today)")
    args = arg_parser.parse_args()
    if args.recalculate and not args.date_from:
        arg_parser.error("--recalculate needs --from")

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    try:
        config = prepare_config(load_config(args.config))
    except ConfigError as e:
        log.error("Invalid config: %s", e)
        sys.exit(1)

    recalculate = None
    if args.recalculate:
        recalculate = {"location": args.recalculate, "date_from": args.date_from,
                       "date_to": args.date_to or date.today()}

    try:
        run_import(config, dry_run=args.dry_run, recalculate=recalculate)
    except ConfigError as e:
        log.error("%s", e)
        sys.exit(1)
    except psycopg2.OperationalError as e:
        log.error("Cannot connect to database: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
