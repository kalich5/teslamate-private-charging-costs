"""
Web UI for TeslaMate Private Charging Costs.

Serves a small configuration and status page under ROOT_PATH
(default ``/private-charging``) and runs the importer on a schedule.

Environment:
    CONFIG_PATH     Path to config.yaml (default: config.yaml)
    ROOT_PATH       URL prefix (default: /private-charging)
    PORT            Listen port (default: 4001)
    WEB_USER        Login user name (default: admin)
    WEB_PASSWORD    Login password; without it the UI is open to anyone who
                    can reach the port (a warning is shown)
    SECRET_KEY      Session signing key (default: random per start)
    DATABASE_*      Same as TeslaMate, see importer.py
"""

import hmac
import logging
import os
import secrets
import threading
import time
from datetime import datetime, timezone
from functools import wraps
from zoneinfo import available_timezones

import psycopg2
from flask import (Blueprint, Flask, Response, abort, flash, jsonify, redirect,
                   render_template, request, session, url_for)

import config_store
from geo import find_location
from importer import (ConfigError, db_connect, db_settings, prepare_config, run_import,
                      to_float, billed_energy)
from pricing import parse_time

log = logging.getLogger(__name__)

COMMON_CURRENCIES = ["CZK", "EUR", "CHF", "PLN", "HUF", "GBP", "USD", "SEK", "NOK", "DKK"]
DAY_TYPES = ("weekday", "weekend")

GEOFENCES_QUERY = """
SELECT id, name, latitude, longitude, radius
FROM geofences
ORDER BY name
"""

RECENT_QUERY = """
SELECT cp.id, cp.start_date, cp.end_date, cp.charge_energy_added, cp.charge_energy_used,
       cp.cost, p.latitude, p.longitude
FROM charging_processes cp
LEFT JOIN positions p ON p.id = cp.position_id
WHERE cp.end_date IS NOT NULL
ORDER BY cp.start_date DESC
LIMIT %s
"""


# ─── Background scheduler ─────────────────────────────────────────────────────

class Runner:
    """Runs the importer (manually or on a schedule), one run at a time."""

    def __init__(self, config_path: str):
        self.config_path = config_path
        self.lock = threading.Lock()
        self.last_run = None       # summary dict of the last real (non dry-run) run
        self.next_run = None       # epoch seconds
        self._stop = threading.Event()

    def run(self, dry_run: bool = False) -> dict:
        """Run once.  Returns the summary, or ``{"error": ...}``."""
        if not self.lock.acquire(blocking=False):
            return {"error": "Another run is in progress, try again in a moment."}
        try:
            started = datetime.now(timezone.utc)
            try:
                config = prepare_config(config_store.load(self.config_path))
                result = run_import(config, dry_run=dry_run)
            except ConfigError as e:
                result = {"error": f"Invalid configuration: {e}"}
            except psycopg2.OperationalError as e:
                result = {"error": f"Cannot connect to the TeslaMate database: {e}"}
            except Exception as e:  # never let the scheduler thread die
                log.exception("Run failed")
                result = {"error": f"Unexpected error: {e}"}
            result["started"] = started
            result["dry_run"] = dry_run
            if not dry_run:
                self.last_run = result
            return result
        finally:
            self.lock.release()

    def schedule_hours(self) -> float:
        try:
            return float(config_store.load(self.config_path).get("SCHEDULE_HOURS") or 0)
        except Exception:
            return 0

    def loop(self):
        while not self._stop.is_set():
            hours = self.schedule_hours()
            if hours <= 0:
                self.next_run = None
                self._stop.wait(60)
                continue
            if self.next_run is None:
                self.next_run = time.time() + 60  # first run shortly after start
            if time.time() >= self.next_run:
                log.info("Scheduled run")
                self.run()
                self.next_run = time.time() + hours * 3600
            self._stop.wait(30)

    def start(self):
        threading.Thread(target=self.loop, name="scheduler", daemon=True).start()

    def stop(self):
        self._stop.set()


# ─── Form handling ────────────────────────────────────────────────────────────

def tariffs_from_form(form) -> dict:
    """Read tariff rows (``weekday_from`` / ``weekday_to`` / ``weekday_price`` lists)."""
    tariffs = {}
    for day_type in DAY_TYPES:
        source = day_type
        if day_type == "weekend" and form.get("weekend_same"):
            source = "weekday"
        rows = zip(form.getlist(f"{source}_from"), form.getlist(f"{source}_to"),
                   form.getlist(f"{source}_price"))
        blocks = []
        for frm, to, price in rows:
            if not (frm or to or price):
                continue  # empty row
            try:
                price_value = float(str(price).replace(",", "."))
            except ValueError:
                raise ConfigError(f"{day_type.capitalize()}: '{price}' is not a valid price.")
            blocks.append({"from": frm.strip(), "to": to.strip(), "price": price_value})
        blocks.sort(key=lambda b: (b["from"] or ""))
        tariffs[day_type] = blocks
    return tariffs


def location_from_form(form) -> dict:
    def number(field, label, cast=float):
        value = (form.get(field) or "").strip().replace(",", ".")
        try:
            return cast(float(value)) if cast is int else cast(value)
        except ValueError:
            raise ConfigError(f"{label}: '{value}' is not a valid number.")

    location = {
        "name": (form.get("name") or "").strip(),
        "lat": number("lat", "Latitude"),
        "lon": number("lon", "Longitude"),
        "radius_m": number("radius_m", "Radius", int),
        "currency": (form.get("currency") or "").strip().upper(),
        "tariffs": tariffs_from_form(form),
    }
    tz = (form.get("timezone") or "").strip()
    if tz:
        location["timezone"] = tz
    if not location["name"]:
        raise ConfigError("Name is required.")
    if len(location["currency"]) != 3 or not location["currency"].isalpha():
        raise ConfigError("Currency must be a 3-letter ISO code, e.g. CZK.")
    if not -90 <= location["lat"] <= 90 or not -180 <= location["lon"] <= 180:
        raise ConfigError("Coordinates are out of range.")
    if location["radius_m"] <= 0:
        raise ConfigError("Radius must be positive.")
    return location


def timeline(blocks: list) -> list:
    """Return ``[{left, width, price, label}]`` percentages for a 24 h bar."""
    out = []
    prices = sorted({b["price"] for b in blocks})
    for b in blocks:
        try:
            start, end = parse_time(str(b["from"])), parse_time(str(b["to"]))
        except ValueError:
            continue
        rank = prices.index(b["price"]) / max(len(prices) - 1, 1)
        out.append({"left": start / 14.4, "width": (end - start) / 14.4, "price": b["price"],
                    "label": f'{b["from"]}–{b["to"]}', "level": round(rank * 4)})
    return out


# ─── App ──────────────────────────────────────────────────────────────────────

def create_app(config_path=None, root_path=None, password=None, user=None,
               start_scheduler=False, db_connector=None) -> Flask:
    config_path = config_path or os.environ.get("CONFIG_PATH", "config.yaml")
    root_path = "/" + (root_path if root_path is not None
                       else os.environ.get("ROOT_PATH", "/private-charging")).strip("/")
    root_path = "" if root_path == "/" else root_path
    password = password if password is not None else os.environ.get("WEB_PASSWORD", "")
    user = user or os.environ.get("WEB_USER", "admin")
    connect = db_connector or (lambda: db_connect(db_settings(config_store.load(config_path)
                                                               .get("DATABASE"))))

    app = Flask(__name__, static_folder=None)
    app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                      SESSION_COOKIE_PATH=root_path or "/")
    runner = Runner(config_path)
    app.extensions["runner"] = runner

    bp = Blueprint("ui", __name__, url_prefix=root_path or None,
                   static_folder="static", template_folder="templates")

    # ── auth & CSRF ──
    def check_auth():
        if not password:
            return True
        auth = request.authorization
        return bool(auth and hmac.compare_digest(auth.username or "", user)
                    and hmac.compare_digest(auth.password or "", password))

    @bp.before_request
    def guard():
        if request.endpoint == "ui.health":
            return None
        if not check_auth():
            return Response("Login required", 401,
                            {"WWW-Authenticate": 'Basic realm="Private charging"'})
        if request.method == "POST":
            token = request.form.get("csrf_token", "")
            expected = session.get("csrf_token", "")
            if not expected or not hmac.compare_digest(token, expected):
                abort(400, "Invalid or expired form token — reload the page and try again.")
        return None

    @bp.app_context_processor
    def inject():
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_hex(16)
        return {"csrf_token": session["csrf_token"], "auth_enabled": bool(password),
                "runner": runner}

    # ── helpers ──
    def load_config():
        return config_store.load(config_path)

    def save_config(config):
        """Validate (when there is something to validate) and write the config."""
        if config.get("LOCATIONS"):
            prepare_config(config)
        config_store.save(config_path, config)

    def render_location(location, index, error=None):
        cfg = load_config()
        return render_template(
            "location.html", location=location, index=index, error=error,
            default_tz=cfg.get("TIMEZONE", ""), timezones=sorted(available_timezones()),
            currencies=COMMON_CURRENCIES, day_types=DAY_TYPES,
        ), 400 if error else 200

    # ── pages ──
    @bp.route("/health")
    def health():
        return {"status": "ok"}

    @bp.route("/")
    def index():
        cfg = load_config()
        problem = None
        try:
            if cfg.get("LOCATIONS"):
                prepare_config(cfg)
        except ConfigError as e:
            problem = str(e)
        locations = [{**loc, "timelines": {d: timeline(loc.get("tariffs", {}).get(d, []))
                                           for d in DAY_TYPES}}
                     for loc in cfg.get("LOCATIONS", [])]
        return render_template("index.html", config=cfg, locations=locations, problem=problem,
                               last_run=runner.last_run,
                               next_run=datetime.fromtimestamp(runner.next_run, timezone.utc)
                               if runner.next_run else None)

    @bp.route("/sessions")
    def sessions():
        cfg = load_config()
        rows, error = [], None
        try:
            prepared = prepare_config(cfg)
            locations = prepared["LOCATIONS"]
        except ConfigError:
            locations = cfg.get("LOCATIONS", [])
        try:
            conn = connect()
            try:
                with conn.cursor() as cur:
                    cur.execute(RECENT_QUERY, (100,))
                    for sid, start, end, added, used, cost, lat, lon in cur.fetchall():
                        loc = find_location(to_float(lat), to_float(lon), locations)
                        rows.append({"id": sid, "start": start, "end": end,
                                     "kwh": billed_energy(added, used),
                                     "cost": to_float(cost),
                                     "location": loc["name"] if loc else None})
            finally:
                conn.close()
        except Exception as e:
            error = f"Cannot read sessions from the TeslaMate database: {e}"
        return render_template("sessions.html", rows=rows, error=error, config=cfg)

    @bp.route("/location/new", methods=["GET", "POST"])
    def location_new():
        if request.method == "GET":
            blank = {"name": "", "lat": "", "lon": "", "radius_m": 150,
                     "currency": load_config().get("BASE_CURRENCY", "CZK"),
                     "tariffs": {"weekday": [{"from": "00:00", "to": "24:00", "price": ""}],
                                 "weekend": [{"from": "00:00", "to": "24:00", "price": ""}]}}
            return render_location(blank, None)
        return save_location(None)

    @bp.route("/location/<int:index>", methods=["GET", "POST"])
    def location_edit(index):
        cfg = load_config()
        if not 0 <= index < len(cfg["LOCATIONS"]):
            abort(404)
        if request.method == "GET":
            return render_location(cfg["LOCATIONS"][index], index)
        return save_location(index)

    def save_location(index):
        cfg = load_config()
        try:
            location = location_from_form(request.form)
        except ConfigError as e:
            draft = {**request.form.to_dict(),
                     "tariffs": {d: [{"from": f, "to": t, "price": p} for f, t, p in
                                     zip(request.form.getlist(f"{d}_from"),
                                         request.form.getlist(f"{d}_to"),
                                         request.form.getlist(f"{d}_price"))]
                                 for d in DAY_TYPES}}
            return render_location(draft, index, str(e))
        locations = list(cfg["LOCATIONS"])
        if index is None:
            locations.append(location)
        else:
            locations[index] = location
        new_cfg = {**cfg, "LOCATIONS": locations}
        try:
            save_config(new_cfg)
        except ConfigError as e:
            return render_location(location, index, str(e))
        flash(f"Location '{location['name']}' saved.")
        return redirect(url_for("ui.index"))

    @bp.route("/location/<int:index>/delete", methods=["POST"])
    def location_delete(index):
        cfg = load_config()
        if not 0 <= index < len(cfg["LOCATIONS"]):
            abort(404)
        removed = cfg["LOCATIONS"].pop(index)
        config_store.save(config_path, cfg)
        flash(f"Location '{removed['name']}' deleted.")
        return redirect(url_for("ui.index"))

    @bp.route("/settings", methods=["GET", "POST"])
    def settings():
        cfg = load_config()
        error = None
        if request.method == "POST":
            new_cfg = {**cfg,
                       "BASE_CURRENCY": (request.form.get("base_currency") or "").strip().upper(),
                       "TIMEZONE": (request.form.get("timezone") or "").strip()}
            try:
                hours = float((request.form.get("schedule_hours") or "0").replace(",", "."))
                if hours < 0:
                    raise ValueError
                new_cfg["SCHEDULE_HOURS"] = int(hours) if hours.is_integer() else hours
                if len(new_cfg["BASE_CURRENCY"]) != 3 or not new_cfg["BASE_CURRENCY"].isalpha():
                    raise ConfigError("Base currency must be a 3-letter ISO code, e.g. CZK.")
                if new_cfg["TIMEZONE"] not in available_timezones():
                    raise ConfigError(f"Unknown timezone '{new_cfg['TIMEZONE']}'.")
                save_config(new_cfg)
                runner.next_run = None  # re-plan with the new interval
                flash("Settings saved.")
                return redirect(url_for("ui.index"))
            except ValueError as e:
                error = str(e) if isinstance(e, ConfigError) else "Interval must be a number ≥ 0."
                cfg = new_cfg
        db = db_settings(cfg.get("DATABASE"))
        return render_template("settings.html", config=cfg, error=error,
                               timezones=sorted(available_timezones()),
                               currencies=COMMON_CURRENCIES,
                               db={k: v for k, v in db.items() if k != "password"}), \
            400 if error else 200

    @bp.route("/run", methods=["POST"])
    def run():
        dry_run = request.form.get("mode") != "write"
        result = runner.run(dry_run=dry_run)
        return render_template("run.html", result=result, config=load_config())

    @bp.route("/api/geofences")
    def geofences():
        try:
            conn = connect()
            try:
                with conn.cursor() as cur:
                    cur.execute(GEOFENCES_QUERY)
                    rows = cur.fetchall()
            finally:
                conn.close()
        except Exception as e:
            return jsonify({"error": str(e), "geofences": []}), 503
        return jsonify({"geofences": [
            {"id": gid, "name": name, "lat": to_float(lat), "lon": to_float(lon),
             "radius_m": int(radius) if radius is not None else None}
            for gid, name, lat, lon, radius in rows]})

    app.register_blueprint(bp)

    if root_path:
        @app.route("/")
        def to_root():
            return redirect(root_path + "/")

    @app.template_filter("dt")
    def fmt_dt(value, tz_name=None):
        if not value:
            return ""
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        try:
            from zoneinfo import ZoneInfo
            value = value.astimezone(ZoneInfo(tz_name)) if tz_name else value
        except Exception:
            pass
        return value.strftime("%Y-%m-%d %H:%M")

    if start_scheduler:
        runner.start()
    return app


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    app = create_app(start_scheduler=True)
    port = int(os.environ.get("PORT", "4001"))
    if not os.environ.get("WEB_PASSWORD"):
        log.warning("WEB_PASSWORD is not set — the UI is open to anyone who can reach port %d.", port)
    from waitress import serve
    log.info("Serving on http://0.0.0.0:%d%s/", port, os.environ.get("ROOT_PATH", "/private-charging").rstrip("/"))
    serve(app, host="0.0.0.0", port=port, threads=4)


if __name__ == "__main__":
    main()
