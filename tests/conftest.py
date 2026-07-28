"""
Встановлюємо фейкові env-змінні ДО імпорту app.py, бо на рівні модуля
app.py читає os.environ["..."] і впаде з KeyError без них. Значення тут
ніколи не використовуються для реального звернення до Google Sheets —
у smoke- та unit-тестах ми не викликаємо append_row().
"""
import os
import sys
import pathlib
import pytest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import dotenv
dotenv.load_dotenv = lambda *args, **kwargs: False

os.environ.setdefault("FLASK_SECRET_KEY", "test-secret-key")
os.environ.setdefault("APP_PASSWORD", "test-password")
os.environ.setdefault("GOOGLE_SHEET_ID", "test-sheet-id")
os.environ.setdefault("GOOGLE_CREDENTIALS_JSON", '{"type": "service_account"}')

from app import app as flask_app, limiter


@pytest.fixture
def client():
    flask_app.config.update(TESTING=True)
    # Вимикаємо rate-limit у тестах: усі запити тестового клієнта йдуть
    # з одного "IP", інакше тести заважали б одне одному лічильником спроб.
    # Саме limiter.enabled, а не config["RATELIMIT_ENABLED"]: конфіг
    # читається один раз під час init_app(), тобто ще при імпорті app.py,
    # і виставляти його тут було б уже запізно.
    limiter.enabled = False
    limiter.reset()
    try:
        with flask_app.test_client() as test_client:
            yield test_client
    finally:
        limiter.enabled = True


@pytest.fixture
def logged_in_client(client):
    # Ставимо сесію напряму, а не через POST /login: тест логіну — окремо,
    # а решті тестів не потрібно щоразу проходити форму входу.
    with client.session_transaction() as sess:
        sess["authed"] = True
    return client
