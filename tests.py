"""
Unit tests for teslamate-private-charging-costs.

Run with:
    pip install pytest
    pytest tests.py -v
"""

import math
from datetime import datetime, timezone

import pytest

from geo import distance_m, find_location
from pricing import parse_time, find_tariff, calculate_cost


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

    def test_missing_coordinates(self):
        assert find_location(None, None, self.LOCATIONS) is None
        assert find_location(49.1951, None, self.LOCATIONS) is None


# ─── pricing.py ───────────────────────────────────────────────────────────────

class TestParseTime:
    def test_normal_time(self):
        from datetime import time
        assert parse_time("06:00") == time(6, 0)

    def test_midnight_end(self):
        from pricing import _END_OF_DAY
        assert parse_time("24:00") == _END_OF_DAY

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
        assert cost == pytest.approx(11.5, rel=1e-2)
