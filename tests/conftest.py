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

from app import app as flask_app 

@pytest.fixture
def client():
    flask_app.config.update(TESTING=True)
    # Вимикаємо rate-limit у тестах: усі запити тестового клієнта йдуть
    # з одного "IP", інакше тести заважали б одне одному лічильником спроб.
    flask_app.config["RATELIMIT_ENABLED"] = False
    with flask_app.test_client() as test_client:
        yield test_client


@pytest.fixture
def logged_in_client(client):
    client.post("/login", data={"password": os.environ["APP_PASSWORD"]})
    return client
