"""
Smoke-тести: не перевіряють бізнес-логіку, лише те, що застосунок
"живий" — піднімається без падіння і базові маршрути повертають
очікувані статуси. Мета — впіймати зламаний деплой (забутий імпорт,
синтаксична помилка, зламаний маршрут) ще до git push.
"""
import os
import pytest
import app as app_module
from app import COLUMN_ORDER


def test_login_page_loads(client):
    response = client.get("/login")
    assert response.status_code == 200


def test_index_requires_login(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_login_with_correct_password_grants_access(client):
    response = client.post(
        "/login",
        data={"password": os.environ["APP_PASSWORD"]},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "Бюджет".encode() in response.data


def test_login_with_wrong_password_denies_access(client):
    response = client.post(
        "/login",
        data={"password": "неправильний-пароль"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "Невірний пароль".encode() in response.data


def test_logout_clears_session(logged_in_client):
    logged_in_client.get("/logout", follow_redirects=False)
    protected = logged_in_client.get("/", follow_redirects=False)
    assert protected.status_code == 302


def test_submit_without_login_redirects(client):
    response = client.post("/submit", data={}, follow_redirects=False)
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_submit_with_invalid_data_shows_error(logged_in_client):
    response = logged_in_client.post(
        "/submit",
        data={"type": "expense", "amount": "0", "category": "Продукти", "date": "2020-01-01"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "Введіть коректну суму більше нуля".encode() in response.data


class FakeWorksheet:
    def __init__(self):
        self.appended_rows = []

    def append_row(self, values, value_input_option=None):
        self.appended_rows.append(list(values))


class FakeClient:
    def __init__(self):
        self.ws = FakeWorksheet()

    def open_by_key(self, key):
        return self

    def worksheet(self, name):
        return self.ws


class TestAmountFormatting:
    """Тести що сума записується з комою, а не крапкою."""

    def test_submit_records_amount_with_comma_decimal_separator(self, logged_in_client, monkeypatch):
        client = FakeClient()
        monkeypatch.setattr(app_module, "get_client", lambda: client)

        response = logged_in_client.post(
            "/submit",
            data={
                "type": "expense",
                "amount": "515.5",
                "category": "🛒 Продукти",
                "date": "2026-07-28",
            },
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "Запис додано".encode() in response.data

        # Перевіряємо, що сума записана з комою
        assert len(client.ws.appended_rows) == 1
        row = client.ws.appended_rows[0]
        amount_index = COLUMN_ORDER.index("amount")
        assert row[amount_index] == "515,5", f"Очікувалось '515,5', отримано '{row[amount_index]}'"

    def test_submit_records_comma_input_with_comma(self, logged_in_client, monkeypatch):
        client = FakeClient()
        monkeypatch.setattr(app_module, "get_client", lambda: client)

        response = logged_in_client.post(
            "/submit",
            data={
                "type": "income",
                "amount": "1000,50",
                "category": "💼 Зарплата",
                "date": "2026-07-28",
            },
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "Запис додано".encode() in response.data

        assert len(client.ws.appended_rows) == 1
        row = client.ws.appended_rows[0]
        amount_index = COLUMN_ORDER.index("amount")
        assert row[amount_index] == "1000,5", f"Очікувалось '1000,5', отримано '{row[amount_index]}'"
