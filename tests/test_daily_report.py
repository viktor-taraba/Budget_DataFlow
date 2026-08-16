"""
Тести звіту електронною поштою (Resend): підсумок за період, рендер
command-line стилю, надсилання через Resend API та "наздоганяючий" лист за
вчора (стан "чи вже надіслано" зберігається в аркуші "Emails" тієї ж
Google-таблиці, а не у файлі на диску — те саме, що вже було зроблено для
categories.json → аркуш "Categories").

Мережа (Resend API) підміняється через app_module.requests.post — той самий
підхід, що й у tests/test_stats.py / tests/test_currency.py для requests.get.
"""
import pytest

import app as app_module
from app import (
    get_period_summary,
    maybe_send_yesterday_report,
    render_report_html,
    render_report_text,
    send_report_email,
)


def entry(day, entry_type, category, amount):
    return {
        "date": f"2026-07-{day:02d}",
        "category": category,
        "amount": str(amount),
    }


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


class FakeResendResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise app_module.requests.HTTPError(f"status {self.status_code}")


class TestGetPeriodSummary:
    def test_counts_transactions_and_totals_across_both_sheets(self, monkeypatch):
        expense_rows = sheet_rows(
            {"date": "2026-07-05", "category": "🛒 Продукти", "amount": "100"},
            {"date": "2026-07-05", "category": "🚗 Транспорт", "amount": "50"},
        )
        income_rows = sheet_rows(
            {"date": "2026-07-05", "category": "💼 Зарплата", "amount": "1000"},
        )
        client = FakeClient(
            {
                app_module.WORKSHEET_EXPENSE: FakeWorksheet(expense_rows),
                app_module.WORKSHEET_INCOME: FakeWorksheet(income_rows),
            }
        )
        monkeypatch.setattr(app_module, "get_client", lambda: client)

        summary = get_period_summary("2026-07-05", "2026-07-05")

        assert summary["count"] == 3
        assert summary["income"] == 1000.0
        assert summary["expense"] == 150.0
        assert summary["net"] == 850.0

    def test_ignores_entries_outside_the_period(self, monkeypatch):
        expense_rows = sheet_rows(
            {"date": "2026-07-05", "category": "🛒 Продукти", "amount": "100"},
            {"date": "2026-07-06", "category": "🛒 Продукти", "amount": "999"},
        )
        client = FakeClient(
            {
                app_module.WORKSHEET_EXPENSE: FakeWorksheet(expense_rows),
                app_module.WORKSHEET_INCOME: FakeWorksheet(sheet_rows()),
            }
        )
        monkeypatch.setattr(app_module, "get_client", lambda: client)

        summary = get_period_summary("2026-07-05", "2026-07-05")
        assert summary["count"] == 1
        assert summary["expense"] == 100.0

    def test_skips_unparseable_amounts(self, monkeypatch):
        expense_rows = sheet_rows(
            {"date": "2026-07-05", "category": "🛒 Продукти", "amount": "не число"},
        )
        client = FakeClient(
            {
                app_module.WORKSHEET_EXPENSE: FakeWorksheet(expense_rows),
                app_module.WORKSHEET_INCOME: FakeWorksheet(sheet_rows()),
            }
        )
        monkeypatch.setattr(app_module, "get_client", lambda: client)

        summary = get_period_summary("2026-07-05", "2026-07-05")
        assert summary["count"] == 0
        assert summary["expense"] == 0.0

    def test_includes_category_breakdown_for_income_and_expense(self, monkeypatch):
        expense_rows = sheet_rows(
            {"date": "2026-07-05", "category": "🛒 Продукти", "amount": "100"},
            {"date": "2026-07-05", "category": "🛒 Продукти", "amount": "50"},
            {"date": "2026-07-05", "category": "🚗 Транспорт", "amount": "30"},
        )
        income_rows = sheet_rows(
            {"date": "2026-07-05", "category": "💼 Зарплата", "amount": "1000"},
            {"date": "2026-07-05", "category": "💰 Відсотки", "amount": "5"},
        )
        client = FakeClient(
            {
                app_module.WORKSHEET_EXPENSE: FakeWorksheet(expense_rows),
                app_module.WORKSHEET_INCOME: FakeWorksheet(income_rows),})
        monkeypatch.setattr(app_module, "get_client", lambda: client)

        summary = get_period_summary("2026-07-05", "2026-07-05")

        assert summary["categories"]["expense"] == [
            {"category": "🛒 Продукти", "amount": 150.0},
            {"category": "🚗 Транспорт", "amount": 30.0},
        ]
        assert summary["categories"]["income"] == [
            {"category": "💼 Зарплата", "amount": 1000.0},
            {"category": "💰 Відсотки", "amount": 5.0},
        ]

    def test_includes_full_transaction_list(self, monkeypatch):
        expense_rows = sheet_rows(
            {"date": "2026-07-05", "category": "🛒 Продукти", "amount": "100"},
        )
        income_rows = sheet_rows(
            {"date": "2026-07-06", "category": "💼 Зарплата", "amount": "1000"},
        )
        client = FakeClient(
            {
                app_module.WORKSHEET_EXPENSE: FakeWorksheet(expense_rows),
                app_module.WORKSHEET_INCOME: FakeWorksheet(income_rows),})
        monkeypatch.setattr(app_module, "get_client", lambda: client)

        summary = get_period_summary("2026-07-01", "2026-07-31")

        assert len(summary["transactions"]) == 2
        assert summary["transactions"][0]["date"] == "2026-07-05"
        assert summary["transactions"][0]["category"] == "🛒 Продукти"
        assert summary["transactions"][0]["type"] == "expense"
        assert summary["transactions"][1]["date"] == "2026-07-06"

    def test_include_transactions_true_for_short_period(self, monkeypatch):
        client = FakeClient(
            {
                app_module.WORKSHEET_EXPENSE: FakeWorksheet(sheet_rows()),
                app_module.WORKSHEET_INCOME: FakeWorksheet(sheet_rows()),})
        monkeypatch.setattr(app_module, "get_client", lambda: client)

        # 20.06 - 05.07 охоплює 2 календарні місяці (червень, липень)
        summary = get_period_summary("2026-06-20", "2026-07-05")
        assert summary["include_transactions"] is True

    def test_include_transactions_false_for_long_period(self, monkeypatch):
        client = FakeClient(
            {
                app_module.WORKSHEET_EXPENSE: FakeWorksheet(sheet_rows()),
                app_module.WORKSHEET_INCOME: FakeWorksheet(sheet_rows()),})
        monkeypatch.setattr(app_module, "get_client", lambda: client)

        # Січень - Березень охоплює 3 календарні місяці
        summary = get_period_summary("2026-01-01", "2026-03-31")
        assert summary["include_transactions"] is False


class TestSpansAtMostNCalendarMonths:
    def test_single_day_is_one_month(self):
        assert app_module._spans_at_most_n_calendar_months("2026-07-05", "2026-07-05", 2) is True

    def test_two_calendar_months_is_within_limit(self):
        assert app_module._spans_at_most_n_calendar_months("2026-06-20", "2026-07-05", 2) is True

    def test_three_calendar_months_exceeds_limit(self):
        assert app_module._spans_at_most_n_calendar_months("2026-01-01", "2026-03-31", 2) is False

    def test_year_boundary(self):
        assert app_module._spans_at_most_n_calendar_months("2025-12-15", "2026-01-05", 2) is True

class TestRenderReport:
    def test_text_report_uses_command_line_style(self):
        summary = {"start": "2026-08-06", "end": "2026-08-06", "count": 3, "income": 1000.0, "expense": 400.0, "net": 600.0}
        text = render_report_text(summary)
        assert text.startswith("$ budget --report --period 2026-08-06")
        assert "TRANSACTIONS ... 3" in text
        assert "+600" in text

    def test_text_report_shows_period_range_for_multi_day_summary(self):
        summary = {"start": "2026-08-01", "end": "2026-08-06", "count": 0, "income": 0.0, "expense": 0.0, "net": 0.0}
        text = render_report_text(summary)
        assert "2026-08-01 – 2026-08-06" in text

    def test_negative_net_uses_minus_sign(self):
        summary = {"start": "2026-08-06", "end": "2026-08-06", "count": 1, "income": 0.0, "expense": 200.0, "net": -200.0}
        text = render_report_text(summary)
        assert "−200,00 ₴" in text

    def test_html_report_is_monospace_dark_terminal_style(self):
        summary = {"start": "2026-08-06", "end": "2026-08-06", "count": 1, "income": 100.0, "expense": 0.0, "net": 100.0}
        html = render_report_html(summary)
        assert "JetBrains Mono" in html
        assert "#0d1117" in html
        assert "TRANSACTIONS ... 1" in html

    def test_summary_without_categories_or_transactions_omits_those_sections(self):
        # Ручний summary (без categories/transactions) — так само, як у тестах
        # вище: секції просто не додаються, базовий блок лишається як раніше.
        summary = {"start": "2026-08-06", "end": "2026-08-06", "count": 1, "income": 100.0, "expense": 0.0, "net": 100.0}
        text = render_report_text(summary)
        assert "BY CATEGORY" not in text
        assert "TRANSACTION LIST" not in text

    def test_text_report_includes_category_breakdown(self):
        summary = {
            "start": "2026-08-06", "end": "2026-08-06", "count": 2,
            "income": 1000.0, "expense": 100.0, "net": 900.0,
            "categories": {
                "income": [{"category": "💼 Зарплата", "amount": 1000.0}],
                "expense": [{"category": "🛒 Продукти", "amount": 100.0}],
            },
            "transactions": [],
            "include_transactions": True,}
        text = render_report_text(summary)
        assert "INCOME BY CATEGORY" in text
        assert "💼 Зарплата" in text
        assert "EXPENSE BY CATEGORY" in text
        assert "🛒 Продукти" in text

    def test_text_report_includes_transaction_list_for_short_period(self):
        summary = {
            "start": "2026-08-01", "end": "2026-08-06", "count": 1,
            "income": 0.0, "expense": 100.0, "net": -100.0,
            "categories": {"income": [], "expense": [{"category": "🛒 Продукти", "amount": 100.0}]},
            "transactions": [
                {"date": "2026-08-03", "type": "expense", "category": "🛒 Продукти", "amount": 100.0, "note": ""},
            ],
            "include_transactions": True,}
        text = render_report_text(summary)
        assert "TRANSACTION LIST" in text
        assert "2026-08-03" in text
        assert "🛒 Продукти" in text

    def test_text_report_omits_transaction_list_for_long_period(self):
        summary = {
            "start": "2026-01-01", "end": "2026-03-31", "count": 1,
            "income": 0.0, "expense": 100.0, "net": -100.0,
            "categories": {"income": [], "expense": [{"category": "🛒 Продукти", "amount": 100.0}]},
            "transactions": [
                {"date": "2026-02-03", "type": "expense", "category": "🛒 Продукти", "amount": 100.0, "note": ""},
            ],
            "include_transactions": False,}
        text = render_report_text(summary)
        assert "TRANSACTION LIST" not in text
        # Категорії все одно показуємо — обмеження стосується лише деталізованого списку.
        assert "EXPENSE BY CATEGORY" in text

    def test_html_report_includes_category_and_transaction_sections(self):
        summary = {
            "start": "2026-08-01", "end": "2026-08-06", "count": 1,
            "income": 0.0, "expense": 100.0, "net": -100.0,
            "categories": {"income": [], "expense": [{"category": "🛒 Продукти", "amount": 100.0}]},
            "transactions": [
                {"date": "2026-08-03", "type": "expense", "category": "🛒 Продукти", "amount": 100.0, "note": ""},
            ],
            "include_transactions": True,
        }
        html = render_report_html(summary)
        assert "EXPENSE BY CATEGORY" in html
        assert "TRANSACTION LIST" in html


class TestSendReportEmail:
    def test_returns_false_without_resend_config(self, monkeypatch):
        monkeypatch.setattr(app_module, "DAILY_REPORT_EMAIL", None)
        monkeypatch.setattr(app_module, "RESEND_API_KEY", "key")
        assert send_report_email("2026-08-06", "2026-08-06") is False

    def test_returns_false_without_email(self, monkeypatch):
        monkeypatch.setattr(app_module, "DAILY_REPORT_EMAIL", "me@example.com")
        monkeypatch.setattr(app_module, "RESEND_API_KEY", None)
        assert send_report_email("2026-08-06", "2026-08-06") is False

    def test_posts_to_resend_with_expected_payload(self, monkeypatch):
        monkeypatch.setattr(app_module, "DAILY_REPORT_EMAIL", "me@example.com")
        monkeypatch.setattr(app_module, "RESEND_API_KEY", "re_fake_key")
        monkeypatch.setattr(app_module, "RESEND_FROM_EMAIL", "onboarding@resend.dev")
        monkeypatch.setattr(app_module, "get_period_summary", lambda s, e: {
            "start": s, "end": e, "count": 2, "income": 500.0, "expense": 100.0, "net": 400.0,
        })

        captured = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return FakeResendResponse(200)

        monkeypatch.setattr(app_module.requests, "post", fake_post)

        assert send_report_email("2026-08-06", "2026-08-06") is True
        assert captured["url"] == "https://api.resend.com/emails"
        assert captured["headers"]["Authorization"] == "Bearer re_fake_key"
        assert captured["json"]["to"] == ["me@example.com"]
        assert captured["json"]["from"] == "onboarding@resend.dev"
        assert "TRANSACTIONS ... 2" in captured["json"]["text"]
        assert "TRANSACTIONS ... 2" in captured["json"]["html"]

    def test_raises_on_resend_error_response(self, monkeypatch):
        monkeypatch.setattr(app_module, "DAILY_REPORT_EMAIL", "me@example.com")
        monkeypatch.setattr(app_module, "RESEND_API_KEY", "re_fake_key")
        monkeypatch.setattr(app_module, "get_period_summary", lambda s, e: {
            "start": s, "end": e, "count": 0, "income": 0.0, "expense": 0.0, "net": 0.0,
        })
        monkeypatch.setattr(app_module.requests, "post", lambda *a, **kw: FakeResendResponse(500))

        with pytest.raises(Exception):
            send_report_email("2026-08-06", "2026-08-06")


class FakeEmailLogWorksheet:
    def __init__(self, rows=None):
        self.rows = rows if rows is not None else [list(app_module.EMAIL_LOG_COLUMN_ORDER)]

    def get_all_values(self):
        return [list(r) for r in self.rows]

    def append_row(self, values, value_input_option=None):
        self.rows.append(list(values))


class FakeEmailLogClient:
    """
    Той самий трюк, що й у фейкових клієнтах Categories: перший worksheet()
    кидає WorksheetNotFound, add_worksheet() створює його — так тестується
    і гілка "аркуш уже існує", і гілка "створюємо вперше".
    """
    def __init__(self, ws=None, exists=True):
        self.ws = ws or FakeEmailLogWorksheet()
        self.exists = exists

    def open_by_key(self, key):
        return self

    def worksheet(self, name):
        if name == app_module.WORKSHEET_EMAIL_LOG and self.exists:
            return self.ws
        raise app_module.gspread.WorksheetNotFound(name)

    def add_worksheet(self, title, rows, cols):
        self.exists = True
        return self.ws


class TestMaybeSendYesterdayReport:
    @pytest.fixture(autouse=True)
    def _isolate_state(self, monkeypatch):
        monkeypatch.setattr(app_module, "_daily_report_confirmed_date", None)
        monkeypatch.setattr(app_module, "DAILY_REPORT_EMAIL", "me@example.com")
        monkeypatch.setattr(app_module, "RESEND_API_KEY", "re_fake_key")
        yield

    def test_sends_and_logs_to_emails_sheet_when_not_sent_yet(self, monkeypatch):
        fake_client = FakeEmailLogClient()
        monkeypatch.setattr(app_module, "get_client", lambda: fake_client)
        calls = []
        def fake_send(start, end):
            calls.append((start, end))
            return True

        monkeypatch.setattr(app_module, "send_report_email", fake_send)
        maybe_send_yesterday_report()

        yesterday = (app_module.today_kyiv() - app_module.timedelta(days=1)).isoformat()
        assert calls == [(yesterday, yesterday)]
        logged_dates = [row[0] for row in fake_client.ws.rows[1:]]
        assert logged_dates == [yesterday]

    def test_skips_when_already_sent_this_process(self, monkeypatch):
        fake_client = FakeEmailLogClient()
        monkeypatch.setattr(app_module, "get_client", lambda: fake_client)
        calls = []
        monkeypatch.setattr(app_module, "send_report_email", lambda s, e: calls.append((s, e)) or True)

        maybe_send_yesterday_report()
        maybe_send_yesterday_report()

        assert len(calls) == 1

    def test_skips_when_emails_sheet_already_has_yesterday(self, monkeypatch):
        # Регресійний випадок для самого бага: інший процес (попередній
        # контейнер, до сну/redeploy) уже надіслав і залогував вчорашній
        # лист у Sheets — новий процес не повинен дублювати його.
        yesterday = (app_module.today_kyiv() - app_module.timedelta(days=1)).isoformat()
        ws = FakeEmailLogWorksheet(
            rows=[list(app_module.EMAIL_LOG_COLUMN_ORDER), [yesterday, "2026-08-06T22:00:00+00:00"]]
        )
        fake_client = FakeEmailLogClient(ws)
        monkeypatch.setattr(app_module, "get_client", lambda: fake_client)

        def fail(*args, **kwargs):
            raise AssertionError("не мав звертатись до send_report_email — вже надіслано (за Sheets)")

        monkeypatch.setattr(app_module, "send_report_email", fail)
        maybe_send_yesterday_report()  # не повинно кидати помилку

    def test_does_not_log_on_send_failure(self, monkeypatch):
        fake_client = FakeEmailLogClient()
        monkeypatch.setattr(app_module, "get_client", lambda: fake_client)
        def boom(start, end):
            raise RuntimeError("Resend недоступний")

        monkeypatch.setattr(app_module, "send_report_email", boom)
        maybe_send_yesterday_report()  # не кидає — ловиться всередині
        assert len(fake_client.ws.rows) == 1  # лише заголовок, нічого не дописано

    def test_does_nothing_without_resend_config(self, monkeypatch):
        monkeypatch.setattr(app_module, "DAILY_REPORT_EMAIL", None)

        def fail(*args, **kwargs):
            raise AssertionError("не мав звертатись до send_report_email без конфігурації")

        monkeypatch.setattr(app_module, "send_report_email", fail)
        maybe_send_yesterday_report()

    def test_does_not_break_or_send_when_sheets_unavailable(self, monkeypatch):
        # Той самий "не ламати застосунок" принцип, що й get_recent_entries():
        # якщо Sheets недоступний, ми не можемо перевірити стан, тому просто
        # нічого не робимо цього разу — і не ризикуємо надіслати дублікат.
        def boom():
            raise RuntimeError("Sheets недоступний")
        monkeypatch.setattr(app_module, "get_client", boom)
        def fail(*args, **kwargs):
            raise AssertionError("не мав звертатись до send_report_email, якщо не вдалось перевірити стан")
        monkeypatch.setattr(app_module, "send_report_email", fail)
        maybe_send_yesterday_report()  # не кидає помилку

    def test_creates_emails_sheet_on_first_use(self, monkeypatch):
        fake_client = FakeEmailLogClient(exists=False)
        monkeypatch.setattr(app_module, "get_client", lambda: fake_client)
        monkeypatch.setattr(app_module, "send_report_email", lambda s, e: True)
        maybe_send_yesterday_report()
        assert fake_client.ws.rows[0] == list(app_module.EMAIL_LOG_COLUMN_ORDER)

class TestSendReportRoute:
    @pytest.fixture(autouse=True)
    def _skip_catchup_hook(self, monkeypatch):
        # Ці тести перевіряють лише /reports/send; before_request-хук теж
        # викликає send_report_email (за вчора) і зіпсував би підрахунок
        # викликів/аргументів нижче, тому вимикаємо саме його тут.
        monkeypatch.setattr(app_module, "maybe_send_yesterday_report", lambda: None)

    def test_requires_login(self, client):
        response = client.post("/reports/send", data={"start": "2026-08-06"}, follow_redirects=False)
        assert response.status_code == 302
        assert "/login" in response.headers["Location"]

    def test_rejects_invalid_date(self, logged_in_client):
        response = logged_in_client.post("/reports/send", data={"start": "не дата"}, follow_redirects=True)
        assert "Некоректна дата звіту".encode() in response.data

    def test_flashes_when_not_configured(self, logged_in_client, monkeypatch):
        monkeypatch.setattr(app_module, "DAILY_REPORT_EMAIL", None)
        response = logged_in_client.post(
            "/reports/send", data={"start": "2026-08-06", "end": "2026-08-06"}, follow_redirects=True
        )
        assert "не налаштовано".encode() in response.data

    def test_sends_report_for_requested_period(self, logged_in_client, monkeypatch):
        monkeypatch.setattr(app_module, "DAILY_REPORT_EMAIL", "me@example.com")
        monkeypatch.setattr(app_module, "RESEND_API_KEY", "re_fake_key")

        calls = []

        def fake_send(start, end):
            calls.append((start, end))
            return True

        monkeypatch.setattr(app_module, "send_report_email", fake_send)

        response = logged_in_client.post(
            "/reports/send", data={"start": "2026-08-01", "end": "2026-08-06"}, follow_redirects=True
        )
        assert "Звіт надіслано на пошту".encode() in response.data
        assert calls == [("2026-08-01", "2026-08-06")]

    def test_swaps_reversed_range(self, logged_in_client, monkeypatch):
        monkeypatch.setattr(app_module, "DAILY_REPORT_EMAIL", "me@example.com")
        monkeypatch.setattr(app_module, "RESEND_API_KEY", "re_fake_key")

        calls = []
        monkeypatch.setattr(app_module, "send_report_email", lambda s, e: calls.append((s, e)) or True)

        logged_in_client.post(
            "/reports/send", data={"start": "2026-08-06", "end": "2026-08-01"}, follow_redirects=True
        )
        assert calls == [("2026-08-01", "2026-08-06")]

    def test_reports_send_failure(self, logged_in_client, monkeypatch):
        monkeypatch.setattr(app_module, "DAILY_REPORT_EMAIL", "me@example.com")
        monkeypatch.setattr(app_module, "RESEND_API_KEY", "re_fake_key")

        def boom(start, end):
            raise RuntimeError("Resend недоступний")

        monkeypatch.setattr(app_module, "send_report_email", boom)

        response = logged_in_client.post(
            "/reports/send", data={"start": "2026-08-06", "end": "2026-08-06"}, follow_redirects=True
        )
        assert "Помилка надсилання звіту".encode() in response.data
