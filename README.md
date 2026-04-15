# ⚡ TeslaMate Private Charging Costs

Automatically calculate charging costs for **private AC locations** (home, work, cottage, …) using time-of-use tariffs and GPS detection.

---

## 🚀 Overview

TeslaMate does not natively support dynamic electricity tariffs (VT/NT, weekend rates, etc.).  
This project fills that gap by reading charging sessions from the TeslaMate PostgreSQL database, matching them to configured locations, splitting each session into 1-minute buckets, applying the correct tariff per minute, and writing the result back as `charging_processes.cost`.

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
  Split session into 1-minute buckets
        │
        ▼
  Apply tariff price per minute
        │
        ▼
  Convert currency (e.g. CZK → CHF)
        │
        ▼
  UPDATE charging_processes SET cost = …
```

---

## ⚙️ Configuration

Copy `config.yaml` and edit it:

```yaml
BASE_CURRENCY: CHF

DATABASE:
  host: database       # hostname of the TeslaMate PostgreSQL container
  port: 5432
  dbname: teslamate
  user: teslamate
  password: teslamate

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

### Tariff rules

- Prices are in `currency` per kWh.
- Time blocks use `HH:MM` format. Use `"24:00"` for the end of the last block.
- Blocks must cover the full 24 hours without gaps. A gap logs a warning and prices that time at 0.
- `weekday` = Monday–Friday, `weekend` = Saturday–Sunday.

---

## 🐳 Docker Usage

### Prerequisites

Your TeslaMate stack must be running and the `teslamate` Docker network must exist.  
Check with:

```bash
docker network ls | grep teslamate
```

If the network name is different, update `docker-compose.yml` accordingly.

### Build

```bash
docker compose build
```

### Run once

```bash
docker compose run --rm private-costs
```

---

## ⏱ Automation (Cron)

Run daily at 06:00:

```bash
0 6 * * * cd /path/to/teslamate-private-charging-costs && docker compose run --rm private-costs >> /var/log/teslamate-costs.log 2>&1
```

---

## 📊 Grafana Examples

### Average price per kWh

```sql
SELECT
  date_trunc('month', start_date) AS time,
  ROUND(AVG(cost / NULLIF(charge_energy_added, 0))::numeric, 4) AS "CHF/kWh"
FROM charging_processes
WHERE cost IS NOT NULL
GROUP BY 1
ORDER BY 1
```

### Cost per 100 km (assuming ~18 kWh/100 km)

```sql
SELECT
  ROUND(AVG(cost / NULLIF(charge_energy_added, 0) * 18)::numeric, 2) AS "CHF/100km"
FROM charging_processes
WHERE cost IS NOT NULL
```

---

## 🧩 Project Structure

```
importer.py        Main entry point — DB read/write loop
pricing.py         Tariff engine — minute-by-minute cost calculation
geo.py             Geofencing — Haversine distance matching
fx.py              Currency conversion — Frankfurter API with in-process cache
config.yaml        User configuration
requirements.txt   Python dependencies
Dockerfile
docker-compose.yml
```

---

## 🔒 Safety

- **Never overwrites existing costs** — only processes rows where `cost IS NULL`.
- **Read-only config** — `config.yaml` is mounted as `:ro` in Docker.
- **Non-root container** — runs as `appuser` inside Docker.

---

## 🌍 Currency Conversion

Uses the free [Frankfurter API](https://www.frankfurter.app/) with EUR as the pivot:

```
from_currency → EUR → to_currency (BASE_CURRENCY)
```

Rates are fetched once per currency per date and cached in-process.

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
