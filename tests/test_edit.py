"""
Тести редагування записів.

Google Sheets тут підмінений фейковим клієнтом: перевіряємо саму логіку
update_row() (у тому числі захист від редагування не того рядка) і поведінку
маршруту /edit, не звертаючись до реального API.
"""
import pytest
import app as app_module
from app import COLUMN_ORDER, update_row, row_fingerprint

HEADER = list(COLUMN_ORDER)


def make_row(day, category, amount, added_at=None):
    return [
        f"2026-07-{day:02d}",
        category,
        str(amount),
        "коментар",
        "2026-07-01T10:00:00",
        added_at if added_at is not None else f"2026-07-{day:02d}T10:00:00+00:00",
        "pytest",
    ]


class FakeWorksheet:
    def __init__(self, rows):
        self.rows = [list(row) for row in rows]
        self.updated = []

    def row_values(self, row_number):
        if 1 <= row_number <= len(self.rows):
            return list(self.rows[row_number - 1])
        return []

    def update(self, cell_range, values):
        # Парсимо діапазон типу 'A3:Z3'
        if ':' in cell_range:
            start = cell_range.split(':')[0]
            row_num = int(''.join(c for c in start if c.isdigit()))
        else:
            row_num = int(''.join(c for c in cell_range if c.isdigit()))

        if 1 <= row_num <= len(self.rows):
            self.rows[row_num - 1] = values[0]
            self.updated.append(row_num)


class FakeClient:
    def __init__(self, worksheet):
        self.worksheet_obj = worksheet
        self.opened_keys = []
        self.requested_names = []

    def open_by_key(self, key):
        self.opened_keys.append(key)
        return self

    def worksheet(self, name):
        self.requested_names.append(name)
        return self.worksheet_obj


@pytest.fixture
def sheet(monkeypatch):
    """Аркуш із трьома записами у рядках 2, 3, 4."""
    ws = FakeWorksheet([HEADER, make_row(1, "Продукти", 10), make_row(2, "Кава", 20), make_row(3, "Таксі", 30)])
    client = FakeClient(ws)
    monkeypatch.setattr(app_module, "get_client", lambda: client)
    return ws


class TestUpdateRow:
    def _fingerprint(self, row):
        return row_fingerprint(app_module._row_to_entry(row))

    def test_updates_matching_row(self, sheet):
        target = list(sheet.rows[2])  # рядок 3 аркуша — "Кава", 20 ₴
        updates = {"amount": "50", "category": "Каша"}
        assert update_row("Витрати", 3, self._fingerprint(target), updates) is True
        assert sheet.updated == [3]
        # Проверяємо що рядок оновлено
        updated_row = app_module._row_to_entry(sheet.rows[2])
        assert updated_row["amount"] == "50"
        assert updated_row["category"] == "Каша"
        assert updated_row["date"] == "2026-07-02"  # дата не змінилась

    def test_refuses_when_row_contents_changed(self, sheet):
        stale = self._fingerprint(make_row(9, "Інше", 999))
        updates = {"amount": "50"}
        assert update_row("Витрати", 3, stale, updates) is False
        assert sheet.updated == []

    def test_refuses_when_row_is_beyond_data(self, sheet):
        target = list(sheet.rows[1])
        updates = {"amount": "50"}
        assert update_row("Витрати", 99, self._fingerprint(target), updates) is False
        assert sheet.updated == []

    def test_refuses_empty_fingerprint(self, sheet):
        updates = {"amount": "50"}
        assert update_row("Витрати", 3, ["", "", "", ""], updates) is False
        assert sheet.updated == []

    def test_preserves_system_fields(self, sheet):
        target = list(sheet.rows[2])
        original_submitted_at = target[4]
        original_added_at = target[5]
        updates = {"amount": "100", "note": "нова примітка"}
        assert update_row("Витрати", 3, self._fingerprint(target), updates) is True
        updated_row = sheet.rows[2]
        assert updated_row[4] == original_submitted_at
        assert updated_row[5] == original_added_at

    def test_uses_requested_worksheet(self, sheet, monkeypatch):
        client_holder = {}

        def fake_get_client():
            client = FakeClient(sheet)
            client_holder["client"] = client
            return client

        monkeypatch.setattr(app_module, "get_client", fake_get_client)
        target = list(sheet.rows[1])
        updates = {"amount": "50"}
        update_row("Доходи", 2, self._fingerprint(target), updates)
        assert client_holder["client"].requested_names == ["Доходи"]


class TestEditRoute:
    def _form(self, row_number=3, entry_type="expense", **overrides):
        data = {
            "type": entry_type,
            "row_number": row_number,
            "date": "2026-07-02",
            "category": "Кава",
            "amount": "50",
            "note": "примітка",
            "fp_date": "2026-07-02",
            "fp_category": "Кава",
            "fp_amount": "20",
            "fp_added_at": "2026-07-02T10:00:00+00:00",
        }
        data.update(overrides)
        return data

    def test_requires_login(self, client):
        response = client.post("/edit", data=self._form(), follow_redirects=False)
        assert response.status_code == 302
        assert "/login" in response.location

    def test_successful_edit(self, logged_in_client, sheet):
        response = logged_in_client.post("/edit", data=self._form(), follow_redirects=True)
        assert response.status_code == 200
        text = response.data.decode()
        assert "Запис оновлено" in text

    def test_rejects_invalid_amount(self, logged_in_client, sheet):
        response = logged_in_client.post("/edit", data=self._form(amount="не число"), follow_redirects=True)
        text = response.data.decode()
        assert "Сума повинна бути числом" in text

    def test_rejects_zero_amount(self, logged_in_client, sheet):
        response = logged_in_client.post("/edit", data=self._form(amount="0"), follow_redirects=True)
        text = response.data.decode()
        assert "Сума повинна бути числом" in text

    def test_rejects_missing_category(self, logged_in_client, sheet):
        response = logged_in_client.post("/edit", data=self._form(category=""), follow_redirects=True)
        text = response.data.decode()
        assert "Виберіть категорію" in text

    def test_rejects_invalid_date(self, logged_in_client, sheet):
        response = logged_in_client.post("/edit", data=self._form(date="не дата"), follow_redirects=True)
        text = response.data.decode()
        assert "Некоректна дата" in text

    def test_rejects_stale_fingerprint(self, logged_in_client, sheet):
        stale_form = self._form(
            fp_date="2026-09-09",
            fp_category="Інше",
            fp_amount="999",
            fp_added_at="2026-09-09T10:00:00+00:00",
        )
        response = logged_in_client.post("/edit", data=stale_form, follow_redirects=True)
        text = response.data.decode()
        assert "Запис уже змінився" in text

    def test_rejects_invalid_type(self, logged_in_client, sheet):
        response = logged_in_client.post("/edit", data=self._form(type="invalid"), follow_redirects=True)
        text = response.data.decode()
        assert "Некоректний тип запису" in text

    def test_rejects_invalid_row_number(self, logged_in_client, sheet):
        response = logged_in_client.post("/edit", data=self._form(row_number="не число"), follow_redirects=True)
        text = response.data.decode()
        assert "Некоректний запис для редагування" in text

