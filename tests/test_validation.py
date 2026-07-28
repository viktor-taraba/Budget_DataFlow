"""Unit-тести валідації суми, дати та номера рядка."""
from datetime import date
import pytest
from app import validate_amount, validate_date, validate_row_number


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
