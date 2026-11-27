# ⚡ TeslaMate Private Charging Costs

[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue?logo=python)](https://www.python.org)

Automatically calculate charging costs for **private AC locations** (home, work, cottage, …) using time-of-use tariffs and GPS detection.

---

## 🚀 Overview

TeslaMate does not natively support dynamic electricity tariffs (VT/NT, weekend rates, etc.).  
This project fills that gap by reading charging sessions from the TeslaMate PostgreSQL database, matching them to configured locations, distributing the energy over time according to TeslaMate's per-sample `charges` data, applying the correct local-time tariff per minute, and writing the result back as `charging_processes.cost`.

---

## ✨ Features

| Feature | Description |
|---|---|
| 📍 GPS geofencing | Sessions are matched to locations by radius |
| 🏠 Multiple locations | Home, work, cottage — unlimited |
| ⏱ Time-of-use tariffs | VT / NT / custom blocks per weekday & weekend |
| 💱 Currency conversion | Per-location currency → base currency via Frankfurter API |
| 🔒 Safe by default | Only processes sessions where `cost IS NULL` |
| 📊 Grafana-ready | Writes directly to `charging_processes.cost` |
| 🖥 Web interface | Manage locations and tariffs in the browser, pick TeslaMate geofences, preview & run |

---

## 🎯 Scope

Handles **private AC charging only**:

- Home
- Work
- Cottage / secondary locations
- Any user-defined location

**Out of scope:** Superchargers and public DC charging (use a dedicated importer for those).

---

## 🧠 How It Works

```
TeslaMate DB
  └─ charging_processes WHERE cost IS NULL
        │
        ▼
  GPS match → find location in config
        │
        ▼
  Distribute energy over time using `charges` samples
  (scheduled charging / pauses get no energy)
        │
        ▼
  Apply local-time tariff price per minute
        │
        ▼
  Convert currency (e.g. CZK → CHF)
        │
        ▼
  UPDATE charging_processes SET cost = …
```

---

## ⚙️ Configuration

The easiest way is the [web interface](#-web-interface). Under the hood everything is stored in `config.yaml`, which you can also edit by hand:

```yaml
BASE_CURRENCY: CHF
TIMEZONE: Europe/Prague     # tariff times are local; DST handled automatically

DATABASE:
  host: database       # hostname of the TeslaMate PostgreSQL container
  port: 5432
  dbname: teslamate
  user: teslamate
  # password: set DATABASE_PASS in .env instead (see below)

LOCATIONS:
  - name: home
    lat: 49.1951
    lon: 16.6068
    radius_m: 150
    currency: CZK

    tariffs:
      weekday:
        - { from: "00:00", to: "06:00", price: 4.0 }
        - { from: "06:00", to: "12:00", price: 7.5 }
        - { from: "12:00", to: "15:00", price: 4.0 }
        - { from: "15:00", to: "24:00", price: 7.5 }
      weekend:
        - { from: "00:00", to: "24:00", price: 4.0 }
```

### Database credentials

Environment variables override the `DATABASE` section. They use the same names as TeslaMate, so you can reuse its values:

| Variable | Default |
|---|---|
| `DATABASE_HOST` | `database` |
| `DATABASE_PORT` | `5432` |
| `DATABASE_NAME` | `teslamate` |
| `DATABASE_USER` | `teslamate` |
| `DATABASE_PASS` | `teslamate` |

With Docker, copy `.env.example` to `.env` and set at least `DATABASE_PASS`. `.env` is git-ignored.

### Tariff rules

- Prices are in `currency` per kWh.
- Cost is calculated from `charge_energy_used` (energy drawn from the grid incl. charging losses — what your meter measures), falling back to `charge_energy_added` if missing.
- Time blocks use `HH:MM` format. Use `"24:00"` for the end of the last block.
- Blocks must cover the full 24 hours without gaps or overlaps. The config is validated at startup and the importer refuses to run otherwise, so no session is ever written with a partial cost.
- Blocks are `[from, to)` — `06:00` belongs to the block starting at 06:00.
- A block crossing midnight (e.g. NT 22:00–06:00) must be split into `22:00–24:00` and `00:00–06:00`.
- Times are local time in `TIMEZONE` (or the location's `timezone`).
- `weekday` = Monday–Friday, `weekend` = Saturday–Sunday.

---

## 🖥 Web Interface

The container serves a simple page in the same style as TeslaMate (Bulma, bundled — no external requests) at **`http://<server>:4001/private-charging`** where you manage everything without editing YAML:

- **Locations** — add a place with **Add location**, or pick one of your existing **TeslaMate geofences** to fill in name, coordinates and radius.
- **Tariffs** — enter time blocks (24-hour `HH:MM`) and prices per kWh for weekdays and weekends. **Add block** splits a block in two; changing a block's end moves the next block's start, so the day stays covered. A 24-hour bar (green = cheapest, red = most expensive) shows the tariff at a glance on the overview and updates live while editing; gaps or overlaps are pointed out before you save. “Same as Monday – Friday” copies the weekday tariff.
- **Calculate costs** — **Preview** shows what would be written; **Calculate & save** writes it. Costs are also calculated automatically every *N* hours (Settings, default 6 h), so no cron job is needed.
- **Sessions** — the last 100 TeslaMate charging sessions with their location, grid energy, cost and price per kWh.
- **Settings** — TeslaMate currency, default timezone and the automatic run interval.

No Tesla account is needed: TeslaMate already talks to Tesla, this tool only reads TeslaMate's database.

Changes are saved to `data/config.yaml` (a `.bak` copy of the previous version is kept). The importer CLI reads the same file.

### Login

Set `WEB_PASSWORD` in `.env` (user `admin`, or `WEB_USER`). Without it, anyone who can reach the port can change your tariffs, and the page shows a warning.

---

## 🐳 Docker Usage

### Prerequisites

Your TeslaMate stack must be running. The container joins TeslaMate's Docker network, which Compose names `<folder>_default` — `teslamate_default` by default. Check the name with:

```bash
docker network ls | grep teslamate
```

### Setup

```bash
cp .env.example .env
```

```bash
nano .env   # set WEB_PASSWORD, DATABASE_PASS and, if needed, TESLAMATE_NETWORK
```

Create the data directory, so it is owned by you (uid 1000) and writable by the container:

```bash
mkdir -p data
```

Upgrading from the CLI-only version? Move your existing config into it:

```bash
cp config.yaml data/config.yaml
```

### Start

```bash
docker compose up -d --build
```

Then open `http://<server>:4001/private-charging`.

### One-off CLI run

```bash
docker compose run --rm private-costs python importer.py --dry-run
```

### Behind a reverse proxy (same host as Grafana)

To serve it next to Grafana, e.g. `https://teslamate.example.com/private-charging`, route that path to the container and keep the prefix (the app is served under `/private-charging` itself; change it with `ROOT_PATH`).

nginx:

```nginx
location /private-charging/ {
    proxy_pass http://127.0.0.1:4001;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

Traefik (labels on the `private-costs` service, like TeslaMate's own Traefik setup):

```yaml
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.private-costs.rule=Host(`teslamate.example.com`) && PathPrefix(`/private-charging`)"
      - "traefik.http.routers.private-costs.entrypoints=websecure"
      - "traefik.http.routers.private-costs.tls.certresolver=tmhttpchallenge"
      - "traefik.http.services.private-costs.loadbalancer.server.port=4001"
```

If the proxy already asks for a password (e.g. TeslaMate's basic-auth middleware), you can reuse it and leave `WEB_PASSWORD` empty; then remove the `ports:` mapping so the UI is reachable only through the proxy.

---

## 📊 Grafana Examples

### Average price per kWh (from the grid)

```sql
SELECT
  date_trunc('month', start_date) AS time,
  ROUND(SUM(cost) / NULLIF(SUM(GREATEST(charge_energy_used, charge_energy_added)), 0), 4) AS "CHF/kWh"
FROM charging_processes
WHERE cost IS NOT NULL
GROUP BY 1
ORDER BY 1
```

### Cost per 100 km (assuming ~18 kWh/100 km from the battery)

```sql
SELECT
  ROUND(SUM(cost) / NULLIF(SUM(charge_energy_added), 0) * 18, 2) AS "CHF/100km"
FROM charging_processes
WHERE cost IS NOT NULL
```

---

## 🧩 Project Structure

```
web.py             Web interface + scheduler (default container command)
config_store.py    Reading/writing config.yaml for the web interface
templates/, static/  Web interface pages, JS and bundled Bulma CSS
importer.py        Importer — DB read/write loop, also usable as CLI
pricing.py         Tariff engine — minute-by-minute cost calculation
geo.py             Geofencing — Haversine distance matching
fx.py              Currency conversion — Frankfurter API with in-process cache
tests.py           Unit tests (pytest)
tests_web.py       Web interface tests
config.yaml        User configuration
requirements.txt   Python dependencies
Dockerfile
docker-compose.yml
.env.example       Template for web password, DB password, Docker network name
```

---

## 🔒 Safety

- **Never overwrites existing costs** — only processes rows where `cost IS NULL`.
- **Validated before saving** — the web interface refuses tariffs with gaps or overlaps, so no partial cost is ever written.
- **Login & CSRF protection** — `WEB_PASSWORD` protects the web interface; forms carry a CSRF token.
- **Non-root container** — runs as `appuser` inside Docker.

---

## 🌍 Currency Conversion

Uses the free [Frankfurter API](https://frankfurter.dev/) (ECB reference rates) with EUR as the pivot:

```
from_currency → EUR → to_currency (BASE_CURRENCY)
```

The rate is taken for the session's local start date (on weekends and holidays the last published rate). Rates are fetched once per currency per date and cached in-process.

---

## 🔮 Roadmap

- [ ] Home Assistant / HDO integration (automatic tariff switching)
- [ ] Grafana dashboard templates
- [ ] NT-savings optimizer
- [ ] Fallback flat rate for unknown locations

---

## 🤝 Contributing

Pull requests are welcome!

- Keep the code simple and readable
- Add logging for anything that can fail silently
- Test with a real TeslaMate instance before submitting

---

## 📄 License

MIT License — see [LICENSE](LICENSE).
