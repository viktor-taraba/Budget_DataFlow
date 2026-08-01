import pytest
from unittest.mock import patch, MagicMock
from datetime import date, timedelta
import app as app_module


class TestCountCategoryFrequency:
    def test_counts_categories_from_both_worksheets(self):
        fake_expense_entries = [
            {"category": "🛒 Продукти", "amount": "100"},
            {"category": "🚗 Транспорт", "amount": "50"},
            {"category": "🛒 Продукти", "amount": "80"},
        ]
        fake_income_entries = [
            {"category": "💼 Зарплата", "amount": "5000"},
            {"category": "💰 Відсотки", "amount": "10"},
            {"category": "💼 Зарплата", "amount": "5000"},
        ]

        with patch('app.get_client') as mock_client:
            mock_sheet = MagicMock()
            mock_expense_ws = MagicMock()
            mock_income_ws = MagicMock()

            mock_sheet.worksheet.side_effect = lambda name: {
                "Витрати": mock_expense_ws,
                "Доходи": mock_income_ws,
            }[name]

            mock_expense_ws.get_all_values.return_value = [
                ["date", "category", "amount"],
                *[
                    [e["category"], e["category"], e["amount"]]
                    for e in fake_expense_entries
                ],
            ]

            mock_income_ws.get_all_values.return_value = [
                ["date", "category", "amount"],
                *[
                    [e["category"], e["category"], e["amount"]]
                    for e in fake_income_entries
                ],
            ]

            mock_client_instance = MagicMock()
            mock_client_instance.open_by_key.return_value = mock_sheet
            mock_client.return_value = mock_client_instance

            frequency = app_module.count_category_frequency()

            assert frequency["expense"]["🛒 Продукти"] == 2
            assert frequency["expense"]["🚗 Транспорт"] == 1
            assert frequency["income"]["💼 Зарплата"] == 2
            assert frequency["income"]["💰 Відсотки"] == 1

    def test_frequency_returns_empty_on_error(self):
        with patch('app.get_client', side_effect=Exception("Connection error")):
            frequency = app_module.count_category_frequency()
            assert frequency == {"expense": {}, "income": {}}


class TestMaybeUpdateCategoryOrder:
    def test_reorders_categories_by_frequency(self):
        app_module.CATEGORIES = {
            "expense": ["🚗 Транспорт", "🛒 Продукти", "🎁 Подарунки"],
            "income": ["💰 Відсотки", "💼 Зарплата"],
        }

        with patch('app.count_category_frequency') as mock_count:
            mock_count.return_value = {
                "expense": {
                    "🛒 Продукти": 10,
                    "🚗 Транспорт": 5,
                    "🎁 Подарунки": 0,
                },
                "income": {
                    "💼 Зарплата": 20,
                    "💰 Відсотки": 1,
                },
            }

            app_module.maybe_update_category_order()

            assert app_module.CATEGORIES["expense"] == [
                "🛒 Продукти",
                "🚗 Транспорт",
                "🎁 Подарунки",
            ]
            assert app_module.CATEGORIES["income"] == [
                "💼 Зарплата",
                "💰 Відсотки",
            ]

    def test_caches_frequency_same_day(self):
        app_module.CATEGORIES = {
            "expense": ["🛒 Продукти"],
            "income": ["💼 Зарплата"],
        }
        app_module._category_frequency_date = date.today()
        app_module._category_frequency_cache = {
            "expense": {"🛒 Продукти": 5},
            "income": {"💼 Зарплата": 10},
        }

        with patch('app.count_category_frequency') as mock_count:
            app_module.maybe_update_category_order()
            mock_count.assert_not_called()

    def test_recalculates_frequency_next_day(self):
        app_module.CATEGORIES = {
            "expense": ["🛒 Продукти"],
            "income": ["💼 Зарплата"],
        }
        yesterday = date.today() - timedelta(days=1)
        app_module._category_frequency_date = yesterday

        with patch('app.count_category_frequency') as mock_count:
            mock_count.return_value = {
                "expense": {"🛒 Продукти": 5},
                "income": {"💼 Зарплата": 10},
            }
            app_module.maybe_update_category_order()
            mock_count.assert_called_once()
