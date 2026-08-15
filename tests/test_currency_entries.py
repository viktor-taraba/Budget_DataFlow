"""
Тести введення операцій у валюті (USD/EUR): підбір курсу, конвертація в
гривневий еквівалент, поведінка /submit і /edit, та захист розбитих
операцій від валюти (вони завжди лишаються в гривні).

Google Sheets тут підмінений фейковим клієнтом — той самий підхід, що й у
tests/test_edit.py / tests/test_delete.py; курс НБУ підміняється через
app_module.get_exchange_rate (або одразу app_module.get_latest_exchange_rate
для тестів маршрутів, щоб не залежати від дати "сьогодні").
"""
import json

import pytest

import app as app_module
from app import COLUMN_ORDER, get_latest_exchange_rate, resolve_currency_amount


def entry_from_row(row):
    return {col: (row[i] if i < len(row) else "") for i, col in enumerate(COLUMN_ORDER)}


class TestGetLatestExchangeRate:
    def test_uses_todays_rate_when_available(self, monkeypatch):
        monkeypatch.setattr(app_module, "today_kyiv", lambda: app_module.date(2026, 8, 14))

        def fake_rate(date_iso, currency="USD"):
            return 41.5 if date_iso == "2026-08-14" else None

        monkeypatch.setattr(app_module, "get_exchange_rate", fake_rate)
        rate, used_date = get_latest_exchange_rate("USD")
        assert rate == 41.5
        assert used_date == "2026-08-14"

    def test_falls_back_to_yesterday_when_today_missing(self, monkeypatch):
        monkeypatch.setattr(app_module, "today_kyiv", lambda: app_module.date(2026, 8, 14))

        def fake_rate(date_iso, currency="USD"):
            return 41.0 if date_iso == "2026-08-13" else None

        monkeypatch.setattr(app_module, "get_exchange_rate", fake_rate)
        rate, used_date = get_latest_exchange_rate("USD")
        assert rate == 41.0
        assert used_date == "2026-08-13"

    def test_returns_none_when_both_days_unavailable(self, monkeypatch):
        monkeypatch.setattr(app_module, "today_kyiv", lambda: app_module.date(2026, 8, 14))
        monkeypatch.setattr(app_module, "get_exchange_rate", lambda date_iso, currency="USD": None)
        assert get_latest_exchange_rate("USD") == (None, None)


class TestResolveCurrencyAmount:
    def test_converts_using_latest_rate(self, monkeypatch):
        monkeypatch.setattr(app_module, "get_latest_exchange_rate", lambda currency: (40.0, "2026-08-14"))
        amount, rate, warning = resolve_currency_amount("USD", 10.0)
        assert amount == 400.0
        assert rate == 40.0
        assert warning is None

    def test_rounds_to_two_decimals(self, monkeypatch):
        monkeypatch.setattr(app_module, "get_latest_exchange_rate", lambda currency: (41.257, "2026-08-14"))
        amount, rate, warning = resolve_currency_amount("EUR", 3.0)
        assert amount == round(41.257 * 3.0, 2)

    def test_falls_back_to_rate_one_when_unavailable(self, monkeypatch):
        monkeypatch.setattr(app_module, "get_latest_exchange_rate", lambda currency: (None, None))
        amount, rate, warning = resolve_currency_amount("USD", 25.0)
        assert amount == 25.0
        assert rate == 1.0
        assert warning is not None and "недоступний" in warning


class FakeWorksheet:
    def __init__(self):
        self.appended_rows = []

    def append_row(self, values, value_input_option=None):
        self.appended_rows.append(list(values))

    def row_values(self, row_number):
        return []

    def update_cell(self, row, col, value):
        pass


class FakeClient:
    def __init__(self):
        self.ws = FakeWorksheet()

    def open_by_key(self, key):
        return self

    def worksheet(self, name):
        return self.ws


class TestSubmitCurrency:
    def test_defaults_to_uah_when_currency_not_selected(self, logged_in_client, monkeypatch):
        client = FakeClient()
        monkeypatch.setattr(app_module, "get_client", lambda: client)

        response = logged_in_client.post(
            "/submit",
            data={"type": "expense", "amount": "100", "category": "🛒 Продукти", "date": "2026-08-14"},
            follow_redirects=True,
        )
        assert response.status_code == 200
        entry = entry_from_row(client.ws.appended_rows[0])
        assert entry["currency"] == "UAH"
        assert entry["curr_amount"] == ""
        assert entry["exchange_rate"] == ""
        assert entry["amount"] == "100,0"

    def test_converts_usd_expense_using_latest_rate(self, logged_in_client, monkeypatch):
        client = FakeClient()
        monkeypatch.setattr(app_module, "get_client", lambda: client)
        monkeypatch.setattr(app_module, "get_latest_exchange_rate", lambda currency: (41.5, "2026-08-14"))

        response = logged_in_client.post(
            "/submit",
            data={
                "type": "expense", "amount": "10", "category": "🛒 Продукти",
                "date": "2026-08-14", "currency": "usd",  # регістр не має значення
            },
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "Запис додано".encode() in response.data
        entry = entry_from_row(client.ws.appended_rows[0])
        assert entry["currency"] == "USD"
        assert entry["curr_amount"] == "10,0"
        assert entry["exchange_rate"] == "41,5"
        assert entry["amount"] == "415,0"

    def test_converts_eur_income(self, logged_in_client, monkeypatch):
        client = FakeClient()
        monkeypatch.setattr(app_module, "get_client", lambda: client)
        monkeypatch.setattr(app_module, "get_latest_exchange_rate", lambda currency: (45.0, "2026-08-14"))

        response = logged_in_client.post(
            "/submit",
            data={
                "type": "income", "amount": "20", "category": "💼 Зарплата",
                "date": "2026-08-14", "currency": "EUR",
            },
            follow_redirects=True,
        )
        entry = entry_from_row(client.ws.appended_rows[0])
        assert entry["currency"] == "EUR"
        assert entry["curr_amount"] == "20,0"
        assert entry["exchange_rate"] == "45,0"
        assert entry["amount"] == "900,0"

    def test_falls_back_to_rate_one_and_warns_when_nbu_unavailable(self, logged_in_client, monkeypatch):
        client = FakeClient()
        monkeypatch.setattr(app_module, "get_client", lambda: client)
        monkeypatch.setattr(app_module, "get_latest_exchange_rate", lambda currency: (None, None))

        response = logged_in_client.post(
            "/submit",
            data={
                "type": "expense", "amount": "50", "category": "🛒 Продукти",
                "date": "2026-08-14", "currency": "USD",
            },
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "недоступний".encode() in response.data
        entry = entry_from_row(client.ws.appended_rows[0])
        assert entry["currency"] == "USD"
        assert entry["curr_amount"] == "50,0"
        assert entry["exchange_rate"] == "1,0"
        assert entry["amount"] == "50,0"

    def test_rejects_unknown_currency_code_as_uah(self, logged_in_client, monkeypatch):
        client = FakeClient()
        monkeypatch.setattr(app_module, "get_client", lambda: client)

        response = logged_in_client.post(
            "/submit",
            data={
                "type": "expense", "amount": "100", "category": "🛒 Продукти",
                "date": "2026-08-14", "currency": "GBP",
            },
            follow_redirects=True,
        )
        entry = entry_from_row(client.ws.appended_rows[0])
        assert entry["currency"] == "UAH"
        assert entry["curr_amount"] == ""
        assert entry["amount"] == "100,0"

    def test_split_expense_ignores_currency_selector(self, logged_in_client, monkeypatch):
        client = FakeClient()
        monkeypatch.setattr(app_module, "get_client", lambda: client)
        monkeypatch.setattr(app_module, "get_latest_exchange_rate", lambda currency: (41.5, "2026-08-14"))

        breakdown = [{"category": "🛒 Продукти", "amount": 100}, {"category": "🎭 Розваги", "amount": 200}]
        response = logged_in_client.post(
            "/submit",
            data={
                "type": "expense", "amount": "300", "date": "2026-08-14", "note": "",
                "currency": "USD",  # має бути проігноровано для розбивки
                "split_breakdown": json.dumps(breakdown),
            },
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "Розбита операція додана".encode() in response.data
        for row in client.ws.appended_rows:
            entry = entry_from_row(row)
            assert entry["currency"] == "UAH"
            assert entry["curr_amount"] == ""
            assert entry["exchange_rate"] == ""
        # Сума розбивки лишається в гривні (300), а не сконвертованою в USD.
        amounts = sorted(float(entry_from_row(r)["amount"].replace(",", ".")) for r in client.ws.appended_rows)
        assert amounts == [100.0, 200.0]


class FakeEditWorksheet:
    def __init__(self, rows):
        self.rows = [list(r) for r in rows]

    def row_values(self, row_number):
        if 1 <= row_number <= len(self.rows):
            return list(self.rows[row_number - 1])
        return []

    def update(self, values, range_name, value_input_option=None):
        start = range_name.split(":")[0]
        row_num = int("".join(c for c in start if c.isdigit()))
        self.rows[row_num - 1] = list(values[0])

    def update_cell(self, row, col, value):
        pass


class FakeEditClient:
    def __init__(self, ws):
        self.ws = ws

    def open_by_key(self, key):
        return self

    def worksheet(self, name):
        return self.ws


def make_currency_row(date, category, amount, curr_amount="", rate="", currency="UAH"):
    return [
        date, category, str(amount), "", "2026-08-01T00:00:00",
        "2026-08-01T00:00:00+00:00", "", "pytest", "", "", "",
        str(curr_amount), str(rate), currency,
    ]


class TestEditCurrency:
    def test_switches_uah_entry_to_usd(self, logged_in_client, monkeypatch):
        HEADER = list(COLUMN_ORDER)
        ws = FakeEditWorksheet([HEADER, make_currency_row("2026-08-01", "🛒 Продукти", "100,0")])
        client = FakeEditClient(ws)
        monkeypatch.setattr(app_module, "get_client", lambda: client)
        monkeypatch.setattr(app_module, "get_latest_exchange_rate", lambda currency: (40.0, "2026-08-14"))

        response = logged_in_client.post(
            "/edit",
            data={
                "type": "expense", "row_number": "2",
                "date": "2026-08-01", "category": "🛒 Продукти", "amount": "5", "note": "",
                "currency": "USD",
                "fp_date": "2026-08-01", "fp_category": "🛒 Продукти",
                "fp_amount": "100,0", "fp_added_at": "2026-08-01T00:00:00+00:00",
            },
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "Запис оновлено".encode() in response.data
        entry = entry_from_row(ws.rows[1])
        assert entry["currency"] == "USD"
        assert entry["curr_amount"] == "5,0"
        assert entry["exchange_rate"] == "40,0"
        assert entry["amount"] == "200,0"

    def test_switches_usd_entry_back_to_uah_and_clears_currency_fields(self, logged_in_client, monkeypatch):
        HEADER = list(COLUMN_ORDER)
        ws = FakeEditWorksheet(
            [HEADER, make_currency_row("2026-08-01", "🛒 Продукти", "415,0", "10,0", "41,5", "USD")]
        )
        client = FakeEditClient(ws)
        monkeypatch.setattr(app_module, "get_client", lambda: client)

        response = logged_in_client.post(
            "/edit",
            data={
                "type": "expense", "row_number": "2",
                "date": "2026-08-01", "category": "🛒 Продукти", "amount": "500", "note": "",
                "currency": "UAH",
                "fp_date": "2026-08-01", "fp_category": "🛒 Продукти",
                "fp_amount": "415,0", "fp_added_at": "2026-08-01T00:00:00+00:00",
            },
            follow_redirects=True,
        )
        assert response.status_code == 200
        entry = entry_from_row(ws.rows[1])
        assert entry["currency"] == "UAH"
        assert entry["curr_amount"] == ""
        assert entry["exchange_rate"] == ""
        assert entry["amount"] == "500,0"

    def test_reresolves_rate_at_save_time_not_original_rate(self, logged_in_client, monkeypatch):
        # Запис колись збережений за курсом 41,5 — за наступного редагування
        # курс НБУ вже інший (42,0), і саме він має бути використаний.
        HEADER = list(COLUMN_ORDER)
        ws = FakeEditWorksheet(
            [HEADER, make_currency_row("2026-08-01", "🛒 Продукти", "415,0", "10,0", "41,5", "USD")]
        )
        client = FakeEditClient(ws)
        monkeypatch.setattr(app_module, "get_client", lambda: client)
        monkeypatch.setattr(app_module, "get_latest_exchange_rate", lambda currency: (42.0, "2026-08-15"))

        logged_in_client.post(
            "/edit",
            data={
                "type": "expense", "row_number": "2",
                "date": "2026-08-01", "category": "🛒 Продукти", "amount": "10", "note": "",
                "currency": "USD",
                "fp_date": "2026-08-01", "fp_category": "🛒 Продукти",
                "fp_amount": "415,0", "fp_added_at": "2026-08-01T00:00:00+00:00",
            },
            follow_redirects=True,
        )
        entry = entry_from_row(ws.rows[1])
        assert entry["exchange_rate"] == "42,0"
        assert entry["amount"] == "420,0"

    def test_fingerprint_still_keys_off_uah_amount_not_curr_amount(self, logged_in_client, monkeypatch):
        # fp_amount переданий зі сторінки — це завжди гривневий еквівалент
        # (entry.amount), навіть для валютного запису; curr_amount там не
        # використовується.
        HEADER = list(COLUMN_ORDER)
        ws = FakeEditWorksheet(
            [HEADER, make_currency_row("2026-08-01", "🛒 Продукти", "415,0", "10,0", "41,5", "USD")]
        )
        client = FakeEditClient(ws)
        monkeypatch.setattr(app_module, "get_client", lambda: client)
        monkeypatch.setattr(app_module, "get_latest_exchange_rate", lambda currency: (41.5, "2026-08-14"))

        # Застарілий (неправильний) fingerprint зі значенням curr_amount замість amount -> відмова.
        response = logged_in_client.post(
            "/edit",
            data={
                "type": "expense", "row_number": "2",
                "date": "2026-08-01", "category": "🛒 Продукти", "amount": "12", "note": "",
                "currency": "USD",
                "fp_date": "2026-08-01", "fp_category": "🛒 Продукти",
                "fp_amount": "10,0",  # неправильно: це curr_amount, а не amount
                "fp_added_at": "2026-08-01T00:00:00+00:00",
            },
            follow_redirects=True,
        )
        assert "оновіть сторінку".encode() in response.data
        # Рядок не змінився
        entry = entry_from_row(ws.rows[1])
        assert entry["curr_amount"] == "10,0"