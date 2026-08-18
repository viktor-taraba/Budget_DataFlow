"""
Тести AI-порад по бюджету: /insights (генерація через agent.py за поточний
чи попередній місяць) та /insights/send (надсилання вже згенерованого
тексту на пошту — лише за явним запитом користувача, ніколи автоматично).
"""
import app as app_module
from app import _month_range


class TestMonthRange:
    def test_current_month(self, monkeypatch):
        monkeypatch.setattr(app_module, "today_kyiv", lambda: app_module.date(2026, 8, 16))
        assert _month_range("current-month") == ("2026-08-01", "2026-08-16")

    def test_previous_month(self, monkeypatch):
        monkeypatch.setattr(app_module, "today_kyiv", lambda: app_module.date(2026, 8, 16))
        assert _month_range("previous-month") == ("2026-07-01", "2026-07-31")

    def test_previous_month_across_year_boundary(self, monkeypatch):
        monkeypatch.setattr(app_module, "today_kyiv", lambda: app_module.date(2026, 1, 10))
        assert _month_range("previous-month") == ("2025-12-01", "2025-12-31")


class TestInsightsRoute:
    def test_requires_login(self, client):
        response = client.post("/insights", data={"period": "current-month"}, follow_redirects=False)
        assert response.status_code == 302
        assert "/login" in response.headers["Location"]

    def test_returns_error_without_openai_key(self, logged_in_client, monkeypatch):
        monkeypatch.setattr(app_module, "OPENAI_API_KEY", None)
        response = logged_in_client.post("/insights", data={"period": "current-month"})
        assert response.status_code == 503
        assert "OPENAI_API_KEY" in response.get_json()["error"]

    def test_generates_insights_for_current_month(self, logged_in_client, monkeypatch):
        monkeypatch.setattr(app_module, "OPENAI_API_KEY", "sk-fake")
        monkeypatch.setattr(
            app_module,
            "get_budget_insights_summary",
            lambda start, end: {
                "start": start, "end": end, "income": [], "expense": [],
                "income_total": 0.0, "expense_total": 0.0,
            },
        )
        monkeypatch.setattr(app_module.agent, "generate_budget_insights", lambda summary, api_key: "Тестова порада")

        response = logged_in_client.post("/insights", data={"period": "current-month"})
        data = response.get_json()
        assert response.status_code == 200
        assert data["text"] == "Тестова порада"
        assert data["period"] == "current-month"

    def test_defaults_to_current_month_for_invalid_period(self, logged_in_client, monkeypatch):
        monkeypatch.setattr(app_module, "OPENAI_API_KEY", "sk-fake")
        captured = {}

        def fake_summary(start, end):
            captured["range"] = (start, end)
            return {"start": start, "end": end, "income": [], "expense": [], "income_total": 0.0, "expense_total": 0.0}

        monkeypatch.setattr(app_module, "get_budget_insights_summary", fake_summary)
        monkeypatch.setattr(app_module.agent, "generate_budget_insights", lambda summary, api_key: "порада")

        response = logged_in_client.post("/insights", data={"period": "щось-незрозуміле"})
        assert response.status_code == 200
        today = app_module.today_kyiv()
        assert captured["range"] == (today.replace(day=1).isoformat(), today.isoformat())

    def test_reports_sheets_failure(self, logged_in_client, monkeypatch):
        monkeypatch.setattr(app_module, "OPENAI_API_KEY", "sk-fake")

        def boom(start, end):
            raise RuntimeError("Sheets недоступний")

        monkeypatch.setattr(app_module, "get_budget_insights_summary", boom)
        response = logged_in_client.post("/insights", data={"period": "current-month"})
        assert response.status_code == 502
        assert "Не вдалося завантажити дані" in response.get_json()["error"]

    def test_reports_llm_failure(self, logged_in_client, monkeypatch):
        monkeypatch.setattr(app_module, "OPENAI_API_KEY", "sk-fake")
        monkeypatch.setattr(
            app_module,
            "get_budget_insights_summary",
            lambda start, end: {
                "start": start, "end": end, "income": [], "expense": [],
                "income_total": 0.0, "expense_total": 0.0,
            },
        )

        def boom(summary, api_key):
            raise RuntimeError("OpenAI недоступний")

        monkeypatch.setattr(app_module.agent, "generate_budget_insights", boom)
        response = logged_in_client.post("/insights", data={"period": "current-month"})
        assert response.status_code == 502
        assert "Не вдалося отримати відповідь від AI" in response.get_json()["error"]


class TestSendInsightsRoute:
    def test_requires_login(self, client):
        response = client.post("/insights/send", data={"text": "порада"}, follow_redirects=False)
        assert response.status_code == 302
        assert "/login" in response.headers["Location"]

    def test_rejects_empty_text(self, logged_in_client):
        response = logged_in_client.post(
            "/insights/send", data={"start": "2026-08-01", "end": "2026-08-16", "text": "  "}, follow_redirects=True
        )
        assert "Немає тексту поради".encode() in response.data

    def test_flashes_when_not_configured(self, logged_in_client, monkeypatch):
        monkeypatch.setattr(app_module, "DAILY_REPORT_EMAIL", None)
        response = logged_in_client.post(
            "/insights/send",
            data={"start": "2026-08-01", "end": "2026-08-16", "text": "порада"},
            follow_redirects=True,
        )
        assert "не налаштовано".encode() in response.data

    def test_sends_email_with_expected_payload(self, logged_in_client, monkeypatch):
        monkeypatch.setattr(app_module, "DAILY_REPORT_EMAIL", "me@example.com")
        monkeypatch.setattr(app_module, "RESEND_API_KEY", "re_fake_key")
        monkeypatch.setattr(app_module, "RESEND_FROM_EMAIL", "onboarding@resend.dev")

        captured = {}

        class FakeResponse:
            status_code = 200

            def raise_for_status(self):
                pass

        def fake_post(url, headers=None, json=None, timeout=None):
            captured["url"] = url
            captured["json"] = json
            return FakeResponse()

        monkeypatch.setattr(app_module.requests, "post", fake_post)

        response = logged_in_client.post(
            "/insights/send",
            data={"start": "2026-08-01", "end": "2026-08-16", "text": "Забагато витрат на каву."},
            follow_redirects=True,
        )
        assert "Поради надіслано на пошту".encode() in response.data
        assert captured["url"] == "https://api.resend.com/emails"
        assert captured["json"]["to"] == ["me@example.com"]
        assert "Забагато витрат на каву." in captured["json"]["text"]
        assert "Забагато витрат на каву." in captured["json"]["html"]

    def test_reports_send_failure(self, logged_in_client, monkeypatch):
        monkeypatch.setattr(app_module, "DAILY_REPORT_EMAIL", "me@example.com")
        monkeypatch.setattr(app_module, "RESEND_API_KEY", "re_fake_key")

        def boom(*args, **kwargs):
            raise RuntimeError("Resend недоступний")

        monkeypatch.setattr(app_module, "send_insights_email", boom)
        response = logged_in_client.post(
            "/insights/send",
            data={"start": "2026-08-01", "end": "2026-08-16", "text": "порада"},
            follow_redirects=True,
        )
        assert "Помилка надсилання поради".encode() in response.data
