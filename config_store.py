"""Reading and writing config.yaml for the web UI."""

import os
import shutil
import tempfile

import yaml

# Order in which top-level keys are written, so the file stays readable.
_KEY_ORDER = ("BASE_CURRENCY", "TIMEZONE", "SCHEDULE_HOURS", "DATABASE", "LOCATIONS")
_LOCATION_KEY_ORDER = ("name", "lat", "lon", "radius_m", "currency", "timezone", "tariffs")
_PRICE_LIST_KEY_ORDER = ("valid_from", "weekday", "weekend")

HEADER = """\
# TeslaMate Private Charging Costs — config.yaml
# This file is managed by the web UI (/private-charging); manual edits are
# fine, but comments will not be preserved when saving from the UI.
# Database credentials belong in environment variables (DATABASE_PASS, …).
"""

DEFAULT_CONFIG = {
    "BASE_CURRENCY": "CZK",
    "TIMEZONE": "Europe/Prague",
    "SCHEDULE_HOURS": 6,
    "LOCATIONS": [],
}


def load(path: str) -> dict:
    """Load the config, returning a copy of the defaults if the file is missing or empty."""
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        data = None
    if not data:
        return {**DEFAULT_CONFIG, "LOCATIONS": []}
    data.setdefault("LOCATIONS", [])
    return data


def _ordered(d: dict, order: tuple) -> dict:
    out = {k: d[k] for k in order if k in d}
    out.update({k: v for k, v in d.items() if k not in out})
    return out


def dump(config: dict) -> str:
    config = _ordered(config, _KEY_ORDER)
    locations = []
    for loc in config.get("LOCATIONS", []):
        loc = _ordered(loc, _LOCATION_KEY_ORDER)
        if isinstance(loc.get("tariffs"), list):
            loc["tariffs"] = sorted((_ordered(p, _PRICE_LIST_KEY_ORDER) for p in loc["tariffs"]),
                                    key=lambda p: str(p.get("valid_from", "")))
        locations.append(loc)
    config["LOCATIONS"] = locations
    return HEADER + "\n" + yaml.safe_dump(config, sort_keys=False, allow_unicode=True,
                                          default_flow_style=None, width=100)


def save(path: str, config: dict) -> None:
    """
    Write *config* atomically (temp file + rename) and keep a ``.bak`` copy.

    If the file is a single-file bind mount (rename across mount points is
    impossible), fall back to rewriting it in place.
    """
    text = dump(config)
    directory = os.path.dirname(os.path.abspath(path))
    if os.path.exists(path):
        try:
            shutil.copy2(path, path + ".bak")
        except OSError:
            pass

    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".config.", suffix=".yaml")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        try:
            os.replace(tmp, path)
            return
        except OSError:
            pass
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)

    with open(path, "w") as f:
        f.write(text)
