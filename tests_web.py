"""
Tests for the web UI (web.py) and config_store.py.

Run with:
    pytest tests.py tests_web.py -v
"""

import re

import pytest
import yaml

import config_store
from web import create_app

AUTH = {"Authorization": "Basic YWRtaW46c2VjcmV0"}  # admin:secret


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        self.query = query

    def fetchall(self):
        if "geofences" in self.query:
            return self.rows["geofences"]
        return self.rows.get("recent", [])

    def close(self):
        pass


class FakeConn:
    def __init__(self, rows):
        self.rows = rows

    def cursor(self):
        return FakeCursor(self.rows)

    def close(self):
        pass


GEOFENCES = [(1, "Home", 49.1951, 16.6068, 50)]


@pytest.fixture
def app(tmp_path):
    path = tmp_path / "config.yaml"
    application = create_app(config_path=str(path), root_path="/private-charging",
                             password="secret", user="admin",
                             db_connector=lambda: FakeConn({"geofences": GEOFENCES}))
    application.config["TESTING"] = True
    application.config_path = path
    return application


@pytest.fixture
def client(app):
    return app.test_client()


def csrf(client, url="/private-charging/"):
    html = client.get(url, headers=AUTH).get_data(as_text=True)
    return re.search(r'name="csrf_token" value="([0-9a-f]+)"', html).group(1)


def location_form(token, **overrides):
    form = {
        "csrf_token": token, "name": "Home", "lat": "49.1951", "lon": "16,6068",
        "radius_m": "100", "currency": "czk", "timezone": "",
        "weekday_from": ["00:00", "06:00"], "weekday_to": ["06:00", "24:00"],
        "weekday_price": ["4", "7,5"], "weekend_same": "on",
    }
    form.update(overrides)
    return form


class TestAuth:
    def test_login_required(self, client):
        assert client.get("/private-charging/").status_code == 401

    def test_wrong_password(self, client):
        assert client.get("/private-charging/",
                          headers={"Authorization": "Basic YWRtaW46d3Jvbmc="}).status_code == 401

    def test_health_is_public(self, client):
        assert client.get("/private-charging/health").status_code == 200

    def test_root_redirects_to_prefix(self, client):
        resp = client.get("/", headers=AUTH)
        assert resp.status_code == 302
        assert resp.headers["Location"].endswith("/private-charging/")

    def test_post_without_csrf_rejected(self, client):
        resp = client.post("/private-charging/location/new", data=location_form(""), headers=AUTH)
        assert resp.status_code == 400

    def test_no_password_shows_warning(self, tmp_path):
        app = create_app(config_path=str(tmp_path / "c.yaml"), root_path="/pc", password="")
        html = app.test_client().get("/pc/").get_data(as_text=True)
        assert "No password set" in html


class TestPages:
    @pytest.mark.parametrize("url", ["/", "/location/new", "/settings", "/sessions"])
    def test_pages_render(self, client, url):
        assert client.get("/private-charging" + url, headers=AUTH).status_code == 200

    def test_geofences_api(self, client):
        data = client.get("/private-charging/api/geofences", headers=AUTH).get_json()
        assert data["geofences"] == [
            {"id": 1, "name": "Home", "lat": 49.1951, "lon": 16.6068, "radius_m": 50}]

    def test_geofences_db_down(self, tmp_path):
        def broken():
            raise OSError("connection refused")
        app = create_app(config_path=str(tmp_path / "c.yaml"), root_path="/pc", password="",
                         db_connector=broken)
        resp = app.test_client().get("/pc/api/geofences")
        assert resp.status_code == 503
        assert resp.get_json()["geofences"] == []


class TestLocationEditing:
    def test_create_location(self, app, client):
        token = csrf(client)
        resp = client.post("/private-charging/location/new", data=location_form(token), headers=AUTH)
        assert resp.status_code == 302
        saved = yaml.safe_load(app.config_path.read_text())
        loc = saved["LOCATIONS"][0]
        assert loc["name"] == "Home"
        assert loc["lon"] == pytest.approx(16.6068)          # comma decimal accepted
        assert loc["currency"] == "CZK"                      # upper-cased
        assert "timezone" not in loc                         # empty → global default
        assert loc["tariffs"]["weekday"] == [
            {"from": "00:00", "to": "06:00", "price": 4.0},
            {"from": "06:00", "to": "24:00", "price": 7.5}]
        assert loc["tariffs"]["weekend"] == loc["tariffs"]["weekday"]  # "same as weekdays"

    def test_gap_is_rejected_and_not_saved(self, app, client):
        token = csrf(client)
        form = location_form(token, weekday_to=["05:00", "24:00"])
        resp = client.post("/private-charging/location/new", data=form, headers=AUTH)
        assert resp.status_code == 400
        assert "no tariff between 05:00 and 06:00" in resp.get_data(as_text=True)
        assert not app.config_path.exists()

    def test_invalid_price(self, client):
        token = csrf(client)
        form = location_form(token, weekday_price=["4", "abc"])
        resp = client.post("/private-charging/location/new", data=form, headers=AUTH)
        assert resp.status_code == 400
        assert "not a valid price" in resp.get_data(as_text=True)

    def test_edit_and_delete(self, app, client):
        token = csrf(client)
        client.post("/private-charging/location/new", data=location_form(token), headers=AUTH)
        client.post("/private-charging/location/0",
                    data=location_form(token, name="Cottage", weekend_same="",
                                       weekend_from=["00:00"], weekend_to=["24:00"],
                                       weekend_price=["3"]),
                    headers=AUTH)
        loc = yaml.safe_load(app.config_path.read_text())["LOCATIONS"][0]
        assert loc["name"] == "Cottage"
        assert loc["tariffs"]["weekend"] == [{"from": "00:00", "to": "24:00", "price": 3.0}]

        client.post("/private-charging/location/0/delete", data={"csrf_token": token}, headers=AUTH)
        assert yaml.safe_load(app.config_path.read_text())["LOCATIONS"] == []

    def test_unknown_location_404(self, client):
        assert client.get("/private-charging/location/5", headers=AUTH).status_code == 404


class TestSettings:
    def test_save_settings(self, app, client):
        token = csrf(client)
        resp = client.post("/private-charging/settings", headers=AUTH, data={
            "csrf_token": token, "base_currency": "chf", "timezone": "Europe/Zurich",
            "schedule_hours": "12"})
        assert resp.status_code == 302
        saved = yaml.safe_load(app.config_path.read_text())
        assert saved["BASE_CURRENCY"] == "CHF"
        assert saved["TIMEZONE"] == "Europe/Zurich"
        assert saved["SCHEDULE_HOURS"] == 12

    def test_unknown_timezone(self, client):
        token = csrf(client)
        resp = client.post("/private-charging/settings", headers=AUTH, data={
            "csrf_token": token, "base_currency": "CZK", "timezone": "Mars/Olympus",
            "schedule_hours": "6"})
        assert resp.status_code == 400
        assert "Unknown timezone" in resp.get_data(as_text=True)


class TestConfigStore:
    def test_missing_file_gives_defaults(self, tmp_path):
        cfg = config_store.load(str(tmp_path / "nope.yaml"))
        assert cfg["LOCATIONS"] == []
        assert cfg["TIMEZONE"]

    def test_round_trip_and_backup(self, tmp_path):
        path = tmp_path / "config.yaml"
        cfg = {"LOCATIONS": [{"tariffs": {}, "name": "a"}], "BASE_CURRENCY": "CZK"}
        config_store.save(str(path), cfg)
        config_store.save(str(path), {**cfg, "BASE_CURRENCY": "EUR"})
        assert config_store.load(str(path))["BASE_CURRENCY"] == "EUR"
        assert yaml.safe_load((tmp_path / "config.yaml.bak").read_text())["BASE_CURRENCY"] == "CZK"
        text = path.read_text()
        assert text.index("BASE_CURRENCY") < text.index("LOCATIONS")   # stable key order
        assert text.index("name: a") < text.index("tariffs")


def test_tariff_bar():
    from web import tariff_bar
    segs = tariff_bar([{"from": "00:00", "to": "06:00", "price": 4.0},
                       {"from": "06:00", "to": "24:00", "price": 7.5},
                       {"from": "bad", "to": "24:00", "price": 1}])
    assert [(s["left"], s["width"], s["level"]) for s in segs] == [(0, 25, 0), (25, 75, 4)]
    assert tariff_bar([{"from": "00:00", "to": "24:00", "price": 4}])[0]["level"] == 0


class TestRunner:
    def test_invalid_config_reports_error(self, tmp_path):
        from web import Runner
        result = Runner(str(tmp_path / "missing.yaml")).run()
        assert "Invalid configuration" in result["error"]

    def test_only_one_run_at_a_time(self, tmp_path):
        from web import Runner
        runner = Runner(str(tmp_path / "missing.yaml"))
        runner.lock.acquire()
        try:
            assert "in progress" in runner.run()["error"]
        finally:
            runner.lock.release()


# ─── Translations ─────────────────────────────────────────────────────────────

class TestLocaleMatching:
    @pytest.mark.parametrize("tag, expected", [
        ("cs", "cs"), ("cs-CZ", "cs"), ("de-AT", "de"), ("en-US", "en"),
        ("nb", "nb"), ("nn", "nb"), ("no", "nb"),
        ("zh", "zh_Hans"), ("zh-CN", "zh_Hans"), ("zh-Hans", "zh_Hans"),
        ("zh-TW", "zh_Hant"), ("zh-HK", "zh_Hant"), ("zh-Hant-TW", "zh_Hant"),
        ("zh_Hant", "zh_Hant"),
        ("ru", None), ("pl", None), ("", None), (None, None),
    ])
    def test_match(self, tag, expected):
        from i18n import match_locale
        assert match_locale(tag) == expected

    def test_order_query_session_browser_default(self):
        from i18n import select_locale
        session = {}
        assert select_locale(None, session, ["ru", "de"]) == "de"       # strict browser match
        assert select_locale(None, session, ["ru"]) == "en"             # default
        assert select_locale("fr", session, ["de"]) == "fr"             # query wins …
        assert session["locale"] == "fr"
        assert select_locale(None, session, ["de"]) == "fr"             # … and is remembered
        assert select_locale("xx", session, ["de"]) == "fr"             # invalid query ignored


class TestTranslatedPages:
    def test_browser_language(self, client):
        html = client.get("/private-charging/",
                          headers={**AUTH, "Accept-Language": "cs-CZ,cs;q=0.9"}).get_data(as_text=True)
        assert '<html lang="cs">' in html
        assert ">Místa<" in html and ">Nastavení<" in html

    def test_switcher_is_remembered(self, client):
        client.get("/private-charging/settings?locale=ja", headers=AUTH)
        html = client.get("/private-charging/",
                          headers={**AUTH, "Accept-Language": "de"}).get_data(as_text=True)
        assert ">場所<" in html

    def test_validation_message_translated(self, client):
        token = csrf(client)
        form = location_form(token, weekday_to=["05:00", "24:00"])
        resp = client.post("/private-charging/location/new", data=form,
                           headers={**AUTH, "Accept-Language": "de"})
        assert "kein Tarif zwischen 05:00 und 06:00" in resp.get_data(as_text=True)

    def test_unsupported_language_falls_back_to_english(self, client):
        html = client.get("/private-charging/",
                          headers={**AUTH, "Accept-Language": "ru"}).get_data(as_text=True)
        assert '<html lang="en">' in html and ">Locations<" in html


class TestCatalogs:
    """Every TeslaMate UI language has a complete, compiled catalog."""

    def _catalogs(self):
        import os
        from babel.messages.pofile import read_po
        from i18n import LANGUAGES
        base = os.path.join(os.path.dirname(__file__), "translations")
        pot = read_po(open(os.path.join(base, "messages.pot"), "rb"))
        for lang in LANGUAGES:
            if lang == "en":
                continue
            path = os.path.join(base, lang, "LC_MESSAGES")
            yield lang, pot, read_po(open(os.path.join(path, "messages.po"), "rb")), path

    def test_complete(self):
        import re
        ph = lambda s: sorted(re.findall(r"%\(\w+\)s", s))  # noqa: E731
        for lang, pot, po, _path in self._catalogs():
            ids = {m.id for m in pot if m.id}
            assert {m.id for m in po if m.id} == ids, lang
            for m in po:
                if m.id:
                    assert m.string, f"{lang}: untranslated {m.id!r}"
                    assert ph(m.string) == ph(m.id), f"{lang}: placeholders in {m.id!r}"

    def test_compiled_mo_is_up_to_date(self):
        import io, os
        from babel.messages.mofile import write_mo
        for lang, _pot, po, path in self._catalogs():
            buf = io.BytesIO()
            write_mo(buf, po)
            with open(os.path.join(path, "messages.mo"), "rb") as fh:
                assert fh.read() == buf.getvalue(), \
                    f"{lang}: run `pybabel compile -d translations`"
