"""Тести агрегації статистики за період і маршруту /stats."""
from datetime import date, timedelta
import pytest
import app as app_module
from app import _date_range, _aggregate_stats, get_period_stats, WORKSHEET_EXPENSE, WORKSHEET_INCOME


def entry(day, category, amount):
    return {"date": f"2026-07-{day:02d}", "category": category, "amount": str(amount)}


class TestDateRange:
    def test_single_day(self):
        assert _date_range("2026-07-05", "2026-07-05") == ["2026-07-05"]

    def test_inclusive_range(self):
        assert _date_range("2026-07-01", "2026-07-03") == [
            "2026-07-01",
            "2026-07-02",
            "2026-07-03",
        ]

    def test_crosses_month_boundary(self):
        assert _date_range("2026-06-29", "2026-07-01") == [
            "2026-06-29",
            "2026-06-30",
            "2026-07-01",
        ]


class TestAggregateStats:
    def test_sums_total(self):
        entries = [entry(1, "Продукти", 100), entry(2, "Кава", 50)]
        result = _aggregate_stats(entries, "2026-07-01", "2026-07-31")
        assert result["total"] == 150.0

    def test_ignores_entries_outside_range(self):
        entries = [entry(1, "Продукти", 100), entry(15, "Кава", 50)]
        result = _aggregate_stats(entries, "2026-07-10", "2026-07-31")
        assert result["total"] == 50.0

    def test_range_boundaries_are_inclusive(self):
        entries = [entry(1, "Продукти", 100), entry(31, "Кава", 50)]
        result = _aggregate_stats(entries, "2026-07-01", "2026-07-31")
        assert result["total"] == 150.0

    def test_daily_series_fills_zero_for_days_without_entries(self):
        entries = [entry(1, "Продукти", 100)]
        result = _aggregate_stats(entries, "2026-07-01", "2026-07-03")
        assert result["daily"] == [
            {"date": "2026-07-01", "amount": 100.0},
            {"date": "2026-07-02", "amount": 0.0},
            {"date": "2026-07-03", "amount": 0.0},
        ]

    def test_daily_series_sums_multiple_entries_same_day(self):
        entries = [entry(1, "Продукти", 100), entry(1, "Кава", 20)]
        result = _aggregate_stats(entries, "2026-07-01", "2026-07-01")
        assert result["daily"] == [{"date": "2026-07-01", "amount": 120.0}]

    def test_categories_sorted_descending(self):
        entries = [entry(1, "Кава", 10), entry(1, "Продукти", 200), entry(1, "Таксі", 50)]
        result = _aggregate_stats(entries, "2026-07-01", "2026-07-01")
        assert result["categories"] == [
            {"category": "Продукти", "amount": 200.0},
            {"category": "Таксі", "amount": 50.0},
            {"category": "Кава", "amount": 10.0},
        ]

    def test_merges_same_category(self):
        entries = [entry(1, "Кава", 10), entry(2, "Кава", 15)]
        result = _aggregate_stats(entries, "2026-07-01", "2026-07-31")
        assert result["categories"] == [{"category": "Кава", "amount": 25.0}]

    def test_skips_unparseable_amount(self):
        entries = [entry(1, "Кава", "не число"), entry(1, "Продукти", 100)]
        result = _aggregate_stats(entries, "2026-07-01", "2026-07-01")
        assert result["total"] == 100.0
        assert result["categories"] == [{"category": "Продукти", "amount": 100.0}]

    def test_empty_category_falls_back_to_inshe(self):
        entries = [entry(1, "", 100)]
        result = _aggregate_stats(entries, "2026-07-01", "2026-07-01")
        assert result["categories"] == [{"category": "Інше", "amount": 100.0}]

    def test_empty_entries_return_zeroed_result(self):
        result = _aggregate_stats([], "2026-07-01", "2026-07-02")
        assert result["total"] == 0.0
        assert result["categories"] == []
        assert result["daily"] == [
            {"date": "2026-07-01", "amount": 0.0},
            {"date": "2026-07-02", "amount": 0.0},
        ]


class TestGetExchangeRate:
    def test_caches_per_currency_and_date(self, monkeypatch):
        app_module._exchange_rates_cache.clear()

        class FakeResponse:
            def __init__(self, rate):
                self._rate = rate

            def json(self):
                return [{"rate": self._rate}]

        def fake_get(url, timeout=5):
            return FakeResponse(45.0 if "valcode=EUR" in url else 40.0)

        monkeypatch.setattr(app_module.requests, "get", fake_get)

        usd_rate = app_module.get_exchange_rate("2026-07-01", "USD")
        eur_rate = app_module.get_exchange_rate("2026-07-01", "EUR")

        assert usd_rate == 40.0
        assert eur_rate == 45.0
        assert app_module._exchange_rates_cache[("USD", "2026-07-01")] == 40.0
        assert app_module._exchange_rates_cache[("EUR", "2026-07-01")] == 45.0

    def test_defaults_to_usd(self, monkeypatch):
        app_module._exchange_rates_cache.clear()
        seen_urls = []

        class FakeResponse:
            def json(self):
                return [{"rate": 41.0}]

        def fake_get(url, timeout=5):
            seen_urls.append(url)
            return FakeResponse()

        monkeypatch.setattr(app_module.requests, "get", fake_get)
        app_module.get_exchange_rate("2026-07-01")

        assert "valcode=USD" in seen_urls[0]


class TestPrefetchExchangeRates:
    def test_populates_cache_for_missing_dates(self, monkeypatch):
        app_module._exchange_rates_cache.clear()
        calls = []

        def fake_rate(date_iso, currency="USD"):
            calls.append((currency, date_iso))
            app_module._exchange_rates_cache[(currency, date_iso)] = 40.0
            return 40.0

        monkeypatch.setattr(app_module, "get_exchange_rate", fake_rate)
        app_module._prefetch_exchange_rates({"2026-07-01", "2026-07-02"}, "USD")

        assert sorted(calls) == [("USD", "2026-07-01"), ("USD", "2026-07-02")]

    def test_skips_already_cached_dates(self, monkeypatch):
        app_module._exchange_rates_cache.clear()
        app_module._exchange_rates_cache[("USD", "2026-07-01")] = 40.0
        calls = []

        def fake_rate(date_iso, currency="USD"):
            calls.append((currency, date_iso))
            return 40.0

        monkeypatch.setattr(app_module, "get_exchange_rate", fake_rate)
        app_module._prefetch_exchange_rates({"2026-07-01", "2026-07-02"}, "USD")

        assert calls == [("USD", "2026-07-02")]

    def test_currencies_are_cached_independently(self, monkeypatch):
        app_module._exchange_rates_cache.clear()
        app_module._exchange_rates_cache[("USD", "2026-07-01")] = 40.0
        calls = []

        def fake_rate(date_iso, currency="USD"):
            calls.append((currency, date_iso))
            return 45.0

        monkeypatch.setattr(app_module, "get_exchange_rate", fake_rate)
        app_module._prefetch_exchange_rates({"2026-07-01"}, "EUR")

        assert calls == [("EUR", "2026-07-01")]

    def test_does_nothing_when_everything_is_cached(self, monkeypatch):
        app_module._exchange_rates_cache.clear()
        app_module._exchange_rates_cache[("USD", "2026-07-01")] = 40.0

        def fail(*args, **kwargs):
            raise AssertionError("get_exchange_rate не мав викликатись")

        monkeypatch.setattr(app_module, "get_exchange_rate", fail)
        app_module._prefetch_exchange_rates({"2026-07-01"}, "USD")  # не повинно кидати помилку


class TestAggregateStatsForeign:
    def test_converts_using_rate_per_date(self, monkeypatch):
        monkeypatch.setattr(app_module, "get_exchange_rate", lambda d, currency="USD": 40.0)
        entries = [entry(1, "Кава", 400)]
        result = app_module._aggregate_stats_foreign(entries, "2026-07-01", "2026-07-01", "USD")
        assert result["total"] == 10.0

    def test_skips_entries_without_available_rate(self, monkeypatch):
        monkeypatch.setattr(app_module, "get_exchange_rate", lambda d, currency="USD": None)
        entries = [entry(1, "Кава", 400)]
        result = app_module._aggregate_stats_foreign(entries, "2026-07-01", "2026-07-01", "USD")
        assert result["total"] == 0.0

    def test_supports_eur(self, monkeypatch):
        monkeypatch.setattr(
            app_module,
            "get_exchange_rate",
            lambda d, currency="USD": 45.0 if currency == "EUR" else 40.0,
        )
        entries = [entry(1, "Кава", 450)]
        result = app_module._aggregate_stats_foreign(entries, "2026-07-01", "2026-07-01", "EUR")
        assert result["total"] == 10.0

    def test_ignores_entries_outside_range(self, monkeypatch):
        monkeypatch.setattr(app_module, "get_exchange_rate", lambda d, currency="USD": 40.0)
        entries = [entry(1, "Кава", 400), entry(15, "Продукти", 40)]
        result = app_module._aggregate_stats_foreign(entries, "2026-07-10", "2026-07-31", "USD")
        assert result["total"] == 1.0


class FakeWorksheet:
    def __init__(self, rows):
        self.rows = rows

    def get_all_values(self):
        return self.rows


class FakeClient:
    def __init__(self, worksheets):
        self.worksheets = worksheets

    def open_by_key(self, key):
        return self

    def worksheet(self, name):
        return self.worksheets[name]


def sheet_rows(*entries):
    header = list(app_module.COLUMN_ORDER)
    rows = [header]
    for e in entries:
        rows.append([e.get(col, "") for col in app_module.COLUMN_ORDER])
    return rows


class TestGetPeriodStats:
    def test_reads_requested_worksheet(self, monkeypatch):
        expense_rows = sheet_rows(entry(1, "Продукти", 100))
        income_rows = sheet_rows(entry(1, "Зарплата", 1000))
        client = FakeClient(
            {
                WORKSHEET_EXPENSE: FakeWorksheet(expense_rows),
                WORKSHEET_INCOME: FakeWorksheet(income_rows),
            }
        )
        monkeypatch.setattr(app_module, "get_client", lambda: client)

        expense_result = get_period_stats(WORKSHEET_EXPENSE, "2026-07-01", "2026-07-31")
        income_result = get_period_stats(WORKSHEET_INCOME, "2026-07-01", "2026-07-31")

        assert expense_result["total"] == 100.0
        assert income_result["total"] == 1000.0


class TestStatsRoute:
    def _stub_stats(self, monkeypatch, calls):
        def fake_get_period_stats(ws_name, start_date, end_date, currency="UAH"):
            calls.append((ws_name, start_date, end_date, currency))
            return {"total": 0.0, "daily": [], "categories": []}

        monkeypatch.setattr(app_module, "get_period_stats", fake_get_period_stats)

    def test_requires_login(self, client):
        response = client.get("/stats", follow_redirects=False)
        assert response.status_code == 302
        assert "/login" in response.headers["Location"]

    def test_defaults_to_last_30_days(self, logged_in_client, monkeypatch):
        calls = []
        self._stub_stats(monkeypatch, calls)

        response = logged_in_client.get("/stats")
        data = response.get_json()

        today = date.today()
        expected_start = (today - timedelta(days=29)).isoformat()
        assert data["start"] == expected_start
        assert data["end"] == today.isoformat()
        assert {c[1:] for c in calls} == {(expected_start, today.isoformat(), "UAH")}

    def test_uses_custom_range(self, logged_in_client, monkeypatch):
        calls = []
        self._stub_stats(monkeypatch, calls)

        response = logged_in_client.get("/stats?start=2026-06-01&end=2026-06-10")
        data = response.get_json()

        assert data["start"] == "2026-06-01"
        assert data["end"] == "2026-06-10"
        assert {c[1:] for c in calls} == {("2026-06-01", "2026-06-10", "UAH")}

    def test_swaps_reversed_range(self, logged_in_client, monkeypatch):
        calls = []
        self._stub_stats(monkeypatch, calls)

        response = logged_in_client.get("/stats?start=2026-06-10&end=2026-06-01")
        data = response.get_json()

        assert data["start"] == "2026-06-01"
        assert data["end"] == "2026-06-10"

    def test_falls_back_to_default_on_invalid_dates(self, logged_in_client, monkeypatch):
        calls = []
        self._stub_stats(monkeypatch, calls)

        response = logged_in_client.get("/stats?start=не-дата&end=теж-не-дата")
        data = response.get_json()

        today = date.today()
        assert data["start"] == (today - timedelta(days=29)).isoformat()
        assert data["end"] == today.isoformat()

    def test_caps_overly_long_range(self, logged_in_client, monkeypatch):
        calls = []
        self._stub_stats(monkeypatch, calls)

        response = logged_in_client.get("/stats?start=2020-01-01&end=2026-07-28")
        data = response.get_json()

        span = (date.fromisoformat(data["end"]) - date.fromisoformat(data["start"])).days
        assert span == app_module.MAX_STATS_RANGE_DAYS

    def test_accepts_eur_currency(self, logged_in_client, monkeypatch):
        calls = []
        self._stub_stats(monkeypatch, calls)

        response = logged_in_client.get("/stats?currency=eur")
        data = response.get_json()

        assert data["currency"] == "EUR"
        assert {c[3] for c in calls} == {"EUR"}

    def test_falls_back_to_uah_for_unknown_currency(self, logged_in_client, monkeypatch):
        calls = []
        self._stub_stats(monkeypatch, calls)

        response = logged_in_client.get("/stats?currency=GBP")
        data = response.get_json()

        assert data["currency"] == "UAH"
        assert {c[3] for c in calls} == {"UAH"}

    def test_computes_difference(self, logged_in_client, monkeypatch):
        def fake_get_period_stats(ws_name, start_date, end_date, currency="UAH"):
            if ws_name == WORKSHEET_EXPENSE:
                return {"total": 300.0, "daily": [], "categories": []}
            return {"total": 1000.0, "daily": [], "categories": []}

        monkeypatch.setattr(app_module, "get_period_stats", fake_get_period_stats)
        response = logged_in_client.get("/stats")
        data = response.get_json()

        assert data["expense"]["total"] == 300.0
        assert data["income"]["total"] == 1000.0
        assert data["difference"] == 700.0

    def test_reports_sheets_failure(self, logged_in_client, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("API недоступний")

        monkeypatch.setattr(app_module, "get_period_stats", boom)
        response = logged_in_client.get("/stats")
        assert response.status_code == 502
        assert "Не вдалося завантажити статистику" in response.get_json()["error"]
