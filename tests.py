"""
Unit tests for teslamate-private-charging-costs.

Run with:
    pip install pytest
    pytest tests.py -v
"""

import math
from decimal import Decimal
from datetime import datetime, timezone

import pytest

from geo import distance_m, find_location
from pricing import (parse_time, find_tariff, calculate_cost, energy_profile,
                     validate_location, TariffGapError)


# ─── geo.py ───────────────────────────────────────────────────────────────────

class TestDistanceM:
    def test_same_point(self):
        assert distance_m(49.0, 16.0, 49.0, 16.0) == pytest.approx(0.0)

    def test_known_distance(self):
        # Prague → Brno ≈ 185 km
        dist = distance_m(50.0755, 14.4378, 49.1951, 16.6068)
        assert 180_000 < dist < 190_000

    def test_symmetry(self):
        d1 = distance_m(49.0, 16.0, 50.0, 17.0)
        d2 = distance_m(50.0, 17.0, 49.0, 16.0)
        assert d1 == pytest.approx(d2)


class TestFindLocation:
    LOCATIONS = [
        {"name": "home", "lat": 49.1951, "lon": 16.6068, "radius_m": 150,
         "currency": "CZK", "tariffs": {}},
    ]

    def test_match_inside_radius(self):
        # Same coordinates → distance 0
        loc = find_location(49.1951, 16.6068, self.LOCATIONS)
        assert loc is not None
        assert loc["name"] == "home"

    def test_no_match_outside_radius(self):
        # Prague coordinates → far from home
        loc = find_location(50.0755, 14.4378, self.LOCATIONS)
        assert loc is None

    def test_decimal_coordinates(self):
        # psycopg2 returns TeslaMate numeric columns as Decimal
        loc = find_location(Decimal("49.195100"), Decimal("16.606800"), self.LOCATIONS)
        assert loc is not None

    def test_missing_coordinates(self):
        assert find_location(None, None, self.LOCATIONS) is None
        assert find_location(49.1951, None, self.LOCATIONS) is None


# ─── pricing.py ───────────────────────────────────────────────────────────────

class TestParseTime:
    def test_normal_time(self):
        assert parse_time("06:00") == 360
        assert parse_time("00:00") == 0

    def test_midnight_end(self):
        assert parse_time("24:00") == 1440

    def test_invalid(self):
        with pytest.raises(ValueError):
            parse_time("25:00")


WEEKDAY_TARIFFS = [
    {"from": "00:00", "to": "06:00", "price": 4.0},
    {"from": "06:00", "to": "12:00", "price": 7.5},
    {"from": "12:00", "to": "15:00", "price": 4.0},
    {"from": "15:00", "to": "24:00", "price": 7.5},
]

FLAT_TARIFFS = [
    {"from": "00:00", "to": "24:00", "price": 5.0},
]

LOCATION = {
    "name": "home",
    "lat": 49.1951,
    "lon": 16.6068,
    "radius_m": 150,
    "currency": "CZK",
    "timezone": "UTC",
    "tariffs": {
        "weekday": WEEKDAY_TARIFFS,
        "weekend": [{"from": "00:00", "to": "24:00", "price": 4.0}],
    },
}


class TestFindTariff:
    def test_finds_correct_block(self):
        from datetime import time
        t = time(3, 30)
        block = find_tariff(WEEKDAY_TARIFFS, t)
        assert block is not None
        assert block["price"] == 4.0

    def test_finds_high_tariff(self):
        from datetime import time
        block = find_tariff(WEEKDAY_TARIFFS, time(9, 0))
        assert block["price"] == 7.5

    def test_boundary_belongs_to_next_block(self):
        from datetime import time
        assert find_tariff(WEEKDAY_TARIFFS, time(6, 0))["price"] == 7.5
        assert find_tariff(WEEKDAY_TARIFFS, time(5, 59, 59))["price"] == 4.0
        assert find_tariff(WEEKDAY_TARIFFS, time(23, 59, 59))["price"] == 7.5

    def test_no_match_returns_none(self):
        # Empty block list → always None
        assert find_tariff([], __import__("datetime").time(12, 0)) is None


class TestCalculateCost:
    def _utc(self, year, month, day, hour, minute):
        return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)

    def test_flat_rate_1kwh_1hour(self):
        """1 kWh over 1 hour at flat 5.0 → cost ≈ 5.0"""
        loc = {**LOCATION, "tariffs": {"weekday": FLAT_TARIFFS, "weekend": FLAT_TARIFFS}}
        # 2026-04-13 is a Monday
        start = self._utc(2026, 4, 13, 10, 0)
        end = self._utc(2026, 4, 13, 11, 0)
        cost = calculate_cost(start, end, 1.0, loc)
        assert cost == pytest.approx(5.0, rel=1e-3)

    def test_decimal_kwh(self):
        loc = {**LOCATION, "tariffs": {"weekday": FLAT_TARIFFS, "weekend": FLAT_TARIFFS}}
        start = self._utc(2026, 4, 13, 10, 0)
        end = self._utc(2026, 4, 13, 11, 0)
        assert calculate_cost(start, end, Decimal("1.00"), loc) == pytest.approx(5.0, rel=1e-3)

    def test_zero_duration(self):
        start = end = self._utc(2026, 4, 13, 10, 0)
        assert calculate_cost(start, end, 1.0, LOCATION) == 0.0

    def test_weekend_rate(self):
        """Saturday charging should use weekend rate (4.0 CZK/kWh)."""
        # 2026-04-11 is a Saturday
        start = self._utc(2026, 4, 11, 10, 0)
        end = self._utc(2026, 4, 11, 11, 0)
        cost = calculate_cost(start, end, 1.0, LOCATION)
        assert cost == pytest.approx(4.0, rel=1e-3)

    def test_mixed_tariff(self):
        """Session spanning NT→VT boundary should use blended rate."""
        # Monday 05:00–07:00: first hour at 4.0, second at 7.5 → avg 5.75
        start = self._utc(2026, 4, 13, 5, 0)
        end = self._utc(2026, 4, 13, 7, 0)
        cost = calculate_cost(start, end, 2.0, LOCATION)
        # 1 kWh * 4.0 + 1 kWh * 7.5 = 11.5
        assert cost == pytest.approx(11.5, rel=1e-9)

    def test_partial_last_minute_not_overcounted(self):
        """30 s session with 1 kWh at flat 5.0 must cost 5.0, not 10.0."""
        loc = {**LOCATION, "tariffs": {"weekday": FLAT_TARIFFS, "weekend": FLAT_TARIFFS}}
        start = self._utc(2026, 4, 13, 10, 0)
        end = start.replace(second=30)
        assert calculate_cost(start, end, 1.0, loc) == pytest.approx(5.0, rel=1e-9)

    def test_unaligned_start_and_end(self):
        """Seconds offsets on both ends are priced exactly across a tariff boundary."""
        # Monday 05:59:30–06:00:30 UTC: half at 4.0, half at 7.5 → 2 kWh → 11.5
        start = datetime(2026, 4, 13, 5, 59, 30, tzinfo=timezone.utc)
        end = datetime(2026, 4, 13, 6, 0, 30, tzinfo=timezone.utc)
        assert calculate_cost(start, end, 2.0, LOCATION) == pytest.approx(11.5, rel=1e-9)

    def test_tariff_uses_local_time_winter(self):
        """06:30–07:30 CET is VT (7.5) even though it is 05:30–06:30 UTC."""
        loc = {**LOCATION, "timezone": "Europe/Prague"}
        start = self._utc(2026, 1, 12, 5, 30)  # Monday
        end = self._utc(2026, 1, 12, 6, 30)
        assert calculate_cost(start, end, 1.0, loc) == pytest.approx(7.5, rel=1e-3)

    def test_tariff_uses_local_time_summer(self):
        """04:30–05:30 UTC in summer is 06:30–07:30 CEST → VT."""
        loc = {**LOCATION, "timezone": "Europe/Prague"}
        start = self._utc(2026, 7, 13, 4, 30)  # Monday
        end = self._utc(2026, 7, 13, 5, 30)
        assert calculate_cost(start, end, 1.0, loc) == pytest.approx(7.5, rel=1e-3)

    def test_weekend_boundary_uses_local_time(self):
        """Sunday 23:30 UTC is already Monday 00:30 CET → weekday NT 4.0, not weekend."""
        loc = {**LOCATION, "timezone": "Europe/Prague",
               "tariffs": {"weekday": FLAT_TARIFFS,
                           "weekend": [{"from": "00:00", "to": "24:00", "price": 1.0}]}}
        start = self._utc(2026, 1, 11, 23, 30)  # Sunday UTC
        end = self._utc(2026, 1, 12, 0, 30)
        assert calculate_cost(start, end, 1.0, loc) == pytest.approx(5.0, rel=1e-3)

    def test_missing_timezone_raises(self):
        loc = {k: v for k, v in LOCATION.items() if k != "timezone"}
        with pytest.raises(ValueError):
            calculate_cost(self._utc(2026, 1, 12, 5, 0), self._utc(2026, 1, 12, 6, 0), 1.0, loc)
    def test_gap_raises_instead_of_pricing_zero(self):
        gap = [{"from": "00:00", "to": "06:00", "price": 4.0},
               {"from": "07:00", "to": "24:00", "price": 7.5}]
        loc = {**LOCATION, "tariffs": {"weekday": gap, "weekend": gap}}
        with pytest.raises(TariffGapError):
            calculate_cost(self._utc(2026, 4, 13, 5, 0), self._utc(2026, 4, 13, 8, 0), 1.0, loc)


class TestValidateLocation:
    def _loc(self, weekday):
        return {**LOCATION, "tariffs": {"weekday": weekday, "weekend": FLAT_TARIFFS}}

    def test_valid_config(self):
        validate_location(LOCATION)

    def test_shipped_config_is_valid(self):
        import yaml
        cfg = yaml.safe_load(open("config.yaml"))
        for loc in cfg["LOCATIONS"]:
            validate_location({**loc, "timezone": loc.get("timezone", cfg["TIMEZONE"])})

    @pytest.mark.parametrize("weekday, message", [
        ([{"from": "00:00", "to": "06:00", "price": 1}, {"from": "07:00", "to": "24:00", "price": 1}],
         "no tariff between 06:00 and 07:00"),
        ([{"from": "00:00", "to": "12:00", "price": 1}, {"from": "11:00", "to": "24:00", "price": 1}],
         "overlapping"),
        ([{"from": "00:00", "to": "22:00", "price": 1}], "no tariff after 22:00"),
        ([{"from": "22:00", "to": "06:00", "price": 1}, {"from": "06:00", "to": "22:00", "price": 1}],
         "crossing midnight"),
        ([{"from": "00:00", "to": "24:00"}], "invalid block"),
        ([{"from": "00:00", "to": "24:00", "price": -1}], "negative price"),
        ([], "no tariffs defined"),
    ])
    def test_invalid_tariffs(self, weekday, message):
        with pytest.raises(ValueError, match=message):
            validate_location(self._loc(weekday))

    def test_unknown_timezone(self):
        with pytest.raises(ValueError, match="unknown timezone"):
            validate_location({**LOCATION, "timezone": "Mars/Olympus"})

    def test_missing_key(self):
        loc = {k: v for k, v in LOCATION.items() if k != "currency"}
        with pytest.raises(ValueError, match="missing 'currency'"):
            validate_location(loc)


class TestEnergyProfile:
    START = datetime(2026, 4, 13, 18, 0, tzinfo=timezone.utc)
    END = datetime(2026, 4, 14, 2, 0, tzinfo=timezone.utc)

    def _t(self, day, hour, minute=0):
        return datetime(2026, 4, day, hour, minute, tzinfo=timezone.utc)

    def test_no_samples_is_uniform(self):
        assert energy_profile([], self.START, self.END, 10.0) == [(self.START, self.END, 10.0)]

    def test_uses_energy_deltas_and_scales_to_total(self):
        samples = [
            (self._t(13, 18), Decimal("0.00"), 0),
            (self._t(14, 0), Decimal("0.00"), 0),   # waiting for scheduled start
            (self._t(14, 1), Decimal("5.00"), 11),
            (self._t(14, 2), Decimal("10.00"), 11),
        ]
        profile = energy_profile(samples, self.START, self.END, 12.0)  # e.g. energy_used
        assert [(a, b) for a, b, _ in profile] == [
            (self._t(14, 0), self._t(14, 1)), (self._t(14, 1), self._t(14, 2))]
        assert sum(k for _, _, k in profile) == pytest.approx(12.0)
        assert profile[0][2] == pytest.approx(6.0)

    def test_falls_back_to_power(self):
        samples = [
            (self._t(13, 18), None, 0),
            (self._t(14, 0), None, 0),
            (self._t(14, 1), None, 10),
            (self._t(14, 2), None, 10),
        ]
        profile = energy_profile(samples, self.START, self.END, 10.0)
        # 18:00–00:00 avg 0 kW → no energy; 00:00–01:00 avg 5 kW; 01:00–02:00 avg 10 kW
        assert [k for _, _, k in profile] == [pytest.approx(10 / 3), pytest.approx(20 / 3)]

    def test_useless_samples_are_uniform(self):
        samples = [(self._t(13, 18), Decimal("1.0"), 0), (self._t(13, 19), Decimal("1.0"), 0)]
        assert energy_profile(samples, self.START, self.END, 5.0) == [(self.START, self.END, 5.0)]


class TestProcessSession:
    LOC = {**LOCATION, "timezone": "UTC"}

    def test_scheduled_charging_priced_in_actual_charging_window(self):
        """Plugged in 18:00 (VT), charged only 00:00–02:00 (NT) → NT price."""
        from importer import process_session
        session = {
            "id": 1,
            "start_date": datetime(2026, 4, 13, 18, 0),  # naive UTC, Monday
            "end_date": datetime(2026, 4, 14, 2, 0),
            "latitude": Decimal("49.195100"),
            "longitude": Decimal("16.606800"),
            "kwh": 10.0,
        }
        samples = [
            (datetime(2026, 4, 13, 18, 0), Decimal("0.00"), 0),
            (datetime(2026, 4, 14, 0, 0), Decimal("0.00"), 0),
            (datetime(2026, 4, 14, 2, 0), Decimal("10.00"), 5),
        ]
        cost, name = process_session(session, [self.LOC], "CZK", lambda _id: samples)
        assert name == "home"
        assert cost == pytest.approx(40.0)  # 10 kWh × 4.0 NT


# ─── importer.py ──────────────────────────────────────────────────────────────

class TestAsUtc:
    def test_naive_is_treated_as_utc(self, monkeypatch):
        import time
        from importer import as_utc
        # Even with a non-UTC host timezone, naive DB timestamps are UTC.
        monkeypatch.setenv("TZ", "America/New_York")
        time.tzset()
        try:
            dt = as_utc(datetime(2026, 1, 12, 5, 30))
        finally:
            monkeypatch.delenv("TZ")
            time.tzset()
        assert dt == datetime(2026, 1, 12, 5, 30, tzinfo=timezone.utc)

    def test_aware_is_converted(self):
        from datetime import timedelta
        from importer import as_utc
        cet = timezone(timedelta(hours=1))
        assert as_utc(datetime(2026, 1, 12, 6, 30, tzinfo=cet)) == \
            datetime(2026, 1, 12, 5, 30, tzinfo=timezone.utc)


class TestBilledEnergy:
    def test_prefers_used(self):
        from importer import billed_energy
        assert billed_energy(Decimal("10.00"), Decimal("11.50")) == pytest.approx(11.5)

    def test_used_missing_falls_back_to_added(self):
        from importer import billed_energy
        assert billed_energy(Decimal("10.00"), None) == pytest.approx(10.0)

    def test_used_never_below_added(self):
        from importer import billed_energy
        assert billed_energy(Decimal("10.00"), Decimal("9.00")) == pytest.approx(10.0)
