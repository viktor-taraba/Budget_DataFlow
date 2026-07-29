"""
Unit-тести перетворення сирих значень аркуша на список останніх записів.

Найважливіше тут — `row_number`: саме за ним видаляється рядок, тому
помилка на одиницю означала б видалення сусіднього запису.
"""
from app import _rows_to_entries, COLUMN_ORDER

HEADER = list(COLUMN_ORDER)


def make_row(day, category, amount):
    """Рядок аркуша у порядку COLUMN_ORDER."""
    return [
        f"2026-07-{day:02d}",
        category,
        str(amount),
        "коментар",
        "",  # split_id
        "",  # split_info
        "2026-07-01T10:00:00",
        f"2026-07-{day:02d}T10:00:00+00:00",
        "",
        "pytest",
    ]


class TestRowsToEntries:
    def test_empty_sheet_returns_nothing(self):
        assert _rows_to_entries([], 5) == []

    def test_header_only_returns_nothing(self):
        assert _rows_to_entries([HEADER], 5) == []

    def test_returns_newest_first(self):
        rows = [HEADER, make_row(1, "Продукти", 10), make_row(2, "Транспорт", 20)]
        entries = _rows_to_entries(rows, 5)
        assert [e["date"] for e in entries] == ["2026-07-02", "2026-07-01"]

    def test_keeps_only_last_limit_rows(self):
        rows = [HEADER] + [make_row(day, "Продукти", day) for day in range(1, 11)]
        entries = _rows_to_entries(rows, 3)
        assert len(entries) == 3
        assert [e["date"] for e in entries] == ["2026-07-10", "2026-07-09", "2026-07-08"]

    def test_row_numbers_point_at_sheet_rows(self):
        # Дані починаються з рядка 2 (рядок 1 — заголовки).
        rows = [HEADER] + [make_row(day, "Продукти", day) for day in range(1, 6)]
        entries = _rows_to_entries(rows, 5)
        assert [e["row_number"] for e in entries] == [6, 5, 4, 3, 2]

    def test_row_numbers_are_absolute_when_limit_cuts_older_rows(self):
        # Обрізання до `limit` не має зсувати нумерацію: останній з 10 рядків
        # даних лежить у рядку 11 аркуша, незалежно від limit.
        rows = [HEADER] + [make_row(day, "Продукти", day) for day in range(1, 11)]
        entries = _rows_to_entries(rows, 2)
        assert [e["row_number"] for e in entries] == [11, 10]

    def test_maps_values_onto_column_names(self):
        entries = _rows_to_entries([HEADER, make_row(3, "Кава", 55)], 5)
        assert entries[0]["category"] == "Кава"
        assert entries[0]["amount"] == "55"
        assert entries[0]["note"] == "коментар"

    def test_pads_short_rows(self):
        # Google Sheets не повертає порожні комірки в кінці рядка.
        entries = _rows_to_entries([HEADER, ["2026-07-04", "Кава", "55"]], 5)
        assert entries[0]["note"] == ""
        assert entries[0]["added_at"] == ""
        assert entries[0]["row_number"] == 2
