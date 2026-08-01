"""Unit-тести валідації суми, дати та номера рядка."""
from datetime import date, datetime, timezone
import pytest
import app as app_module
from app import validate_amount, validate_date, validate_row_number, today_kyiv


class TestValidateAmount:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("150", 150.0),
            ("150.50", 150.5),
            ("150,50", 150.5),
            (" 150 ", 150.0),
            ("0.01", 0.01),
            ("999999", 999999.0),
        ],
    )
    def test_accepts_valid_amounts(self, raw, expected):
        assert validate_amount(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "0",  
            "-50",
            "",
            "   ",
            "абв",
            "1,50,20",
            "100₴",
            "1e10",
            None,  # поле відсутнє у формі
        ],
    )
    def test_rejects_invalid_amounts(self, raw):
        assert validate_amount(raw) is None


class TestValidateDate:
    def test_accepts_today(self):
        today = date.today().isoformat()
        assert validate_date(today) == today

    def test_accepts_past_date(self):
        assert validate_date("2020-01-01") == "2020-01-01"

    def test_rejects_future_date(self):
        future = date(date.today().year + 1, 1, 1).isoformat()
        assert validate_date(future) is None

    @pytest.mark.parametrize(
        "raw",
        [
            "06.07.2026",  # інший формат
            "2026/07/06",  # інший роздільник
            "не дата",
            "",
            None,
        ],
    )
    def test_rejects_invalid_format(self, raw):
        assert validate_date(raw) is None

    def test_respects_custom_max_date(self):
        # Дата пізніша за штучну межу — відхиляється,
        # навіть якщо вона в минулому відносно "сьогодні".
        assert validate_date("2025-06-01", max_date=date(2025, 5, 1)) is None
        assert validate_date("2025-04-01", max_date=date(2025, 5, 1)) == "2025-04-01"

    @pytest.mark.parametrize(
        "serial, expected_iso",
        [
            (44192, "2020-12-27"),  # з BudgetApp.xlsx
            (44194, "2020-12-29"),
            (44196, "2020-12-31"),
            (44205, "2021-01-09"),
            (44211, "2021-01-15"),
            ("44192", "2020-12-27"),  # серійні дати можуть бути рядками
            (43831, "2020-01-01"),
            (45000, "2023-03-15"),
        ],
    )
    def test_accepts_excel_serial_dates(self, serial, expected_iso):
        assert validate_date(serial) == expected_iso

    def test_rejects_invalid_excel_serial(self):
        # Занадто великі значення (поза розумними межами)
        assert validate_date("100000") is None
        assert validate_date("-100") is None


class TestTodayKyiv:
    """
    Сервер (Render) працює в UTC. Пізно ввечері за UTC у Києві вже настав
    наступний день — `today_kyiv()` має зважати на це.
    """

    def _freeze(self, monkeypatch, fixed_utc):
        class FrozenDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed_utc.astimezone(tz) if tz else fixed_utc.replace(tzinfo=None)

        monkeypatch.setattr(app_module, "datetime", FrozenDateTime)

    def test_returns_next_day_when_utc_is_still_on_the_previous_one(self, monkeypatch):
        # 23:30 UTC 31 липня — у Києві (літній час, UTC+3) вже 02:30, 1 серпня.
        self._freeze(monkeypatch, datetime(2026, 7, 31, 23, 30, tzinfo=timezone.utc))
        assert today_kyiv().isoformat() == "2026-08-01"

    def test_matches_utc_day_during_kyiv_daytime(self, monkeypatch):
        # 10:00 UTC — і в Києві, і в UTC один і той самий календарний день.
        self._freeze(monkeypatch, datetime(2026, 7, 31, 10, 0, tzinfo=timezone.utc))
        assert today_kyiv().isoformat() == "2026-07-31"

    def test_validate_date_accepts_kyiv_today_even_if_utc_is_still_yesterday(self, monkeypatch):
        self._freeze(monkeypatch, datetime(2026, 7, 31, 23, 30, tzinfo=timezone.utc))
        assert validate_date("2026-08-01") == "2026-08-01"


class TestValidateRowNumber:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("2", 2),
            ("10", 10),
            (" 7 ", 7),
            ("1000", 1000),
            (5, 5),  # уже число, а не рядок з форми
        ],
    )
    def test_accepts_valid_row_numbers(self, raw, expected):
        assert validate_row_number(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "1",  # рядок заголовків — видаляти не можна
            "0",
            "-5",
            "2.5",
            "",
            "   ",
            "abc",
            "²",  # isdigit() == True, але int() падає — тому й regex, а не isdigit
            None,  # поле відсутнє у формі
        ],
    )
    def test_rejects_invalid_row_numbers(self, raw):
        assert validate_row_number(raw) is None
