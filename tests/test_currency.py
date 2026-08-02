"""
Тести аналітики курсів валют (/currency, /currency/rates).

get_exchange_rate_range() навмисно відрізняється від get_exchange_rate():
один запит із start/end на весь період замість одного запиту на кожну
дату — тому тут окремо перевіряємо саме параметри запиту (period, а не
per-date), а не лише результат парсингу.
"""
from datetime import date, timedelta

import pytest

import app as app_module
from app import (
    _fill_rate_gaps,
    get_currency_period_analysis,
    get_exchange_rate_range,
)


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def nbu_row(day_month_year: str, rate: float):
    return {"exchangedate": day_month_year, "rate": rate}


class TestGetExchangeRateRange:
    def test_requests_period_not_per_date(self, monkeypatch):
        seen_urls = []

        def fake_get(url, timeout=10):
            seen_urls.append(url)
            return FakeResponse([nbu_row("01.07.2026", 40.0), nbu_row("02.07.2026", 40.5)])

        monkeypatch.setattr(app_module.requests, "get", fake_get)
        app_module.get_exchange_rate_range("2026-07-01", "2026-07-02", "USD")

        assert len(seen_urls) == 1
        url = seen_urls[0]
        assert "start=20260701" in url
        assert "end=20260702" in url
        assert "valcode=USD" in url

    def test_parses_dd_mm_yyyy_dates_to_iso(self, monkeypatch):
        monkeypatch.setattr(
            app_module.requests,
            "get",
            lambda url, timeout=10: FakeResponse([nbu_row("15.07.2026", 41.25)]),
        )
        result = app_module.get_exchange_rate_range("2026-07-01", "2026-07-31", "USD")
        assert result == {"2026-07-15": 41.25}

    def test_returns_empty_dict_on_request_error(self, monkeypatch):
        def boom(url, timeout=10):
            raise app_module.requests.RequestException("network down")

        monkeypatch.setattr(app_module.requests, "get", boom)
        assert app_module.get_exchange_rate_range("2026-07-01", "2026-07-31", "USD") == {}

    def test_skips_malformed_rows(self, monkeypatch):
        monkeypatch.setattr(
            app_module.requests,
            "get",
            lambda url, timeout=10: FakeResponse(
                [{"exchangedate": "01.07.2026"}, nbu_row("02.07.2026", 40.0)]
            ),
        )
        result = app_module.get_exchange_rate_range("2026-07-01", "2026-07-02", "USD")
        assert result == {"2026-07-02": 40.0}

    def test_returns_empty_dict_for_invalid_dates(self):
        assert app_module.get_exchange_rate_range("не дата", "2026-07-02", "USD") == {}


class TestFillRateGaps:
    def test_no_gaps_returns_values_as_is(self):
        rates = {"2026-07-01": 40.0, "2026-07-02": 40.5}
        series = _fill_rate_gaps(rates, "2026-07-01", "2026-07-02")
        assert series == [
            {"date": "2026-07-01", "rate": 40.0},
            {"date": "2026-07-02", "rate": 40.5},
        ]

    def test_forward_fills_missing_middle_days(self):
        # Вихідний день (07-02) без власного курсу успадковує п'ятничний.
        rates = {"2026-07-01": 40.0, "2026-07-03": 40.5}
        series = _fill_rate_gaps(rates, "2026-07-01", "2026-07-03")
        assert series == [
            {"date": "2026-07-01", "rate": 40.0},
            {"date": "2026-07-02", "rate": 40.0},
            {"date": "2026-07-03", "rate": 40.5},
        ]

    def test_backfills_leading_gap_from_first_known_value(self):
        # Період починається у вихідний, перший курс з'являється лише 07-02.
        rates = {"2026-07-02": 41.0}
        series = _fill_rate_gaps(rates, "2026-07-01", "2026-07-03")
        assert series == [
            {"date": "2026-07-01", "rate": 41.0},
            {"date": "2026-07-02", "rate": 41.0},
            {"date": "2026-07-03", "rate": 41.0},
        ]

    def test_all_missing_returns_all_none(self):
        series = _fill_rate_gaps({}, "2026-07-01", "2026-07-02")
        assert series == [
            {"date": "2026-07-01", "rate": None},
            {"date": "2026-07-02", "rate": None},
        ]


class TestGetCurrencyPeriodAnalysis:
    def test_detects_devaluation_when_rate_increases(self, monkeypatch):
        monkeypatch.setattr(
            app_module,
            "get_exchange_rate_range",
            lambda start, end, currency: {"2026-07-01": 40.0, "2026-07-10": 44.0},
        )
        result = get_currency_period_analysis("USD", "2026-07-01", "2026-07-10")

        assert result["start_rate"] == 40.0
        assert result["end_rate"] == 44.0
        assert result["change_percent"] == 10.0
        assert result["direction"] == "devaluation"

    def test_detects_revaluation_when_rate_decreases(self, monkeypatch):
        monkeypatch.setattr(
            app_module,
            "get_exchange_rate_range",
            lambda start, end, currency: {"2026-07-01": 44.0, "2026-07-10": 41.8},
        )
        result = get_currency_period_analysis("EUR", "2026-07-01", "2026-07-10")

        assert result["start_rate"] == 44.0
        assert result["end_rate"] == 41.8
        assert result["change_percent"] == -5.0
        assert result["direction"] == "revaluation"

    def test_stable_when_rate_unchanged(self, monkeypatch):
        monkeypatch.setattr(
            app_module,
            "get_exchange_rate_range",
            lambda start, end, currency: {"2026-07-01": 40.0, "2026-07-10": 40.0},
        )
        result = get_currency_period_analysis("USD", "2026-07-01", "2026-07-10")

        assert result["change_percent"] == 0.0
        assert result["direction"] == "stable"

    def test_returns_nulls_when_no_data_available(self, monkeypatch):
        monkeypatch.setattr(
            app_module, "get_exchange_rate_range", lambda start, end, currency: {}
        )
        result = get_currency_period_analysis("USD", "2026-07-01", "2026-07-10")

        assert result["start_rate"] is None
        assert result["end_rate"] is None
        assert result["change_percent"] is None
        assert result["direction"] is None
        assert result["series"] == [{"date": d, "rate": None} for d in app_module._date_range("2026-07-01", "2026-07-10")]

    def test_series_covers_every_day_in_range(self, monkeypatch):
        monkeypatch.setattr(
            app_module,
            "get_exchange_rate_range",
            lambda start, end, currency: {"2026-07-01": 40.0},
        )
        result = get_currency_period_analysis("USD", "2026-07-01", "2026-07-03")
        assert [p["date"] for p in result["series"]] == ["2026-07-01", "2026-07-02", "2026-07-03"]


class TestCurrencyPageRoute:
    def test_requires_login(self, client):
        response = client.get("/currency", follow_redirects=False)
        assert response.status_code == 302
        assert "/login" in response.headers["Location"]

    def test_loads_for_logged_in_user(self, logged_in_client):
        response = logged_in_client.get("/currency")
        assert response.status_code == 200
        assert "Курси валют".encode() in response.data

    def test_linked_from_stats_modal(self, logged_in_client, monkeypatch):
        monkeypatch.setattr(app_module, "get_recent_entries", lambda ws_name, limit=5: [])
        response = logged_in_client.get("/")
        assert response.status_code == 200
        assert b'href="/currency"' in response.data


class TestCurrencyRatesRoute:
    def _stub(self, monkeypatch, calls, result=None):
        def fake_analysis(currency, start_date, end_date):
            calls.append((currency, start_date, end_date))
            return result or {
                "currency": currency,
                "start": start_date,
                "end": end_date,
                "series": [],
                "start_rate": None,
                "end_rate": None,
                "change_percent": None,
                "direction": None,
            }

        monkeypatch.setattr(app_module, "get_currency_period_analysis", fake_analysis)

    def test_requires_login(self, client):
        response = client.get("/currency/rates", follow_redirects=False)
        assert response.status_code == 302
        assert "/login" in response.headers["Location"]

    def test_defaults_to_usd_and_last_30_days(self, logged_in_client, monkeypatch):
        calls = []
        self._stub(monkeypatch, calls)

        response = logged_in_client.get("/currency/rates")
        data = response.get_json()

        today = date.today()
        expected_start = (today - timedelta(days=29)).isoformat()
        assert data["start"] == expected_start
        assert data["end"] == today.isoformat()
        assert calls == [("USD", expected_start, today.isoformat())]

    def test_accepts_eur(self, logged_in_client, monkeypatch):
        calls = []
        self._stub(monkeypatch, calls)

        logged_in_client.get("/currency/rates?currency=eur")
        assert calls[0][0] == "EUR"

    def test_falls_back_to_usd_for_unsupported_currency(self, logged_in_client, monkeypatch):
        calls = []
        self._stub(monkeypatch, calls)

        logged_in_client.get("/currency/rates?currency=UAH")
        assert calls[0][0] == "USD"

    def test_uses_custom_range(self, logged_in_client, monkeypatch):
        calls = []
        self._stub(monkeypatch, calls)

        logged_in_client.get("/currency/rates?start=2026-06-01&end=2026-06-10")
        assert calls == [("USD", "2026-06-01", "2026-06-10")]

    def test_swaps_reversed_range(self, logged_in_client, monkeypatch):
        calls = []
        self._stub(monkeypatch, calls)

        response = logged_in_client.get("/currency/rates?start=2026-06-10&end=2026-06-01")
        data = response.get_json()
        assert data["start"] == "2026-06-01"
        assert data["end"] == "2026-06-10"

    def test_caps_overly_long_range(self, logged_in_client, monkeypatch):
        calls = []
        self._stub(monkeypatch, calls)

        response = logged_in_client.get("/currency/rates?start=2015-01-01&end=2026-07-28")
        data = response.get_json()
        span = (date.fromisoformat(data["end"]) - date.fromisoformat(data["start"])).days
        assert span == app_module.MAX_STATS_RANGE_DAYS
        assert "warning" in data

    def test_reports_failure(self, logged_in_client, monkeypatch):
        def boom(currency, start_date, end_date):
            raise RuntimeError("НБУ недоступний")

        monkeypatch.setattr(app_module, "get_currency_period_analysis", boom)
        response = logged_in_client.get("/currency/rates")
        assert response.status_code == 502
        assert "Не вдалося завантажити курси валют" in response.get_json()["error"]

    def test_passes_through_direction_and_change(self, logged_in_client, monkeypatch):
        calls = []
        self._stub(
            monkeypatch,
            calls,
            result={
                "currency": "USD",
                "start": "2026-07-01",
                "end": "2026-07-10",
                "series": [{"date": "2026-07-01", "rate": 40.0}, {"date": "2026-07-10", "rate": 44.0}],
                "start_rate": 40.0,
                "end_rate": 44.0,
                "change_percent": 10.0,
                "direction": "devaluation",
            },
        )

        response = logged_in_client.get("/currency/rates?start=2026-07-01&end=2026-07-10")
        data = response.get_json()
        assert data["direction"] == "devaluation"
        assert data["change_percent"] == 10.0
