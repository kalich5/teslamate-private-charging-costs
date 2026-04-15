import logging
import os
import sys

import psycopg2
import yaml
from dateutil import parser
from datetime import timezone

from geo import find_location
from pricing import calculate_cost
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


# TeslaMate stores GPS coordinates in the `positions` table linked via
# the car's positions at charge start.  We join on the position closest
# to the start of the charging process.
FETCH_QUERY = """
SELECT
    cp.id,
    cp.start_date,
    cp.end_date,
    p.latitude,
    p.longitude,
    cp.charge_energy_added
FROM charging_processes cp
LEFT JOIN positions p ON p.id = (
    SELECT id FROM positions
    WHERE car_id = cp.car_id
      AND date <= cp.start_date
    ORDER BY date DESC
    LIMIT 1
)
WHERE cp.cost IS NULL
  AND cp.end_date IS NOT NULL
  AND cp.charge_energy_added IS NOT NULL
  AND cp.charge_energy_added > 0
"""

UPDATE_QUERY = "UPDATE charging_processes SET cost = %s WHERE id = %s"


def process_session(session, locations, base_currency):
    lat = session.get("latitude")
    lon = session.get("longitude")

    location = find_location(lat, lon, locations)
    if not location:
        log.debug("Session %s: no matching location (lat=%s, lon=%s)", session["id"], lat, lon)
        return None

    start = parser.parse(session["start_date"]).astimezone(timezone.utc)
    end = parser.parse(session["end_date"]).astimezone(timezone.utc)

    if end <= start:
        log.warning("Session %s: end_date (%s) <= start_date (%s), skipping", session["id"], end, start)
        return None

    kwh = session.get("kwh", 0)
    if not kwh:
        log.warning("Session %s: energy_added is zero, skipping", session["id"])
        return None

    local_cost = calculate_cost(start, end, kwh, location)

    final_cost = convert(
        local_cost,
        location["currency"],
        base_currency,
        start,
    )

    return final_cost, location["name"]


def main():
    config = load_config()
    locations = config["LOCATIONS"]
    base_currency = config["BASE_CURRENCY"]
    db_cfg = config.get("DATABASE", {})

    try:
        conn = db_connect(db_cfg)
    except psycopg2.OperationalError as e:
        log.error("Cannot connect to database: %s", e)
        sys.exit(1)

    cur = conn.cursor()
    cur.execute(FETCH_QUERY)
    rows = cur.fetchall()
    log.info("Found %d charging session(s) with missing cost.", len(rows))

    updated = 0
    skipped = 0

    for row in rows:
        tm_id, start, end, lat, lon, kwh = row

        session = {
            "id": tm_id,
            "start_date": start.isoformat() if start else None,
            "end_date": end.isoformat() if end else None,
            "latitude": lat,
            "longitude": lon,
            "kwh": kwh,
        }

        try:
            result = process_session(session, locations, base_currency)
        except Exception as e:
            log.error("Session %s: unexpected error — %s", tm_id, e)
            skipped += 1
            continue

        if result is None:
            skipped += 1
            continue

        cost, location_name = result
        cur.execute(UPDATE_QUERY, (cost, tm_id))
        log.info("Session %s @ %s → %.4f %s", tm_id, location_name, cost, base_currency)
        updated += 1

    conn.commit()
    cur.close()
    conn.close()

    log.info("Done. Updated: %d, Skipped: %d", updated, skipped)


if __name__ == "__main__":
    main()
