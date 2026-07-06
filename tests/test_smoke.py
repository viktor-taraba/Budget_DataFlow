"""
Smoke-тести: не перевіряють бізнес-логіку, лише те, що застосунок
"живий" — піднімається без падіння і базові маршрути повертають
очікувані статуси. Мета — впіймати зламаний деплой (забутий імпорт,
синтаксична помилка, зламаний маршрут) ще до git push.
"""
import os


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
