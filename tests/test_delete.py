"""
Тести видалення записів.

Google Sheets тут підмінений фейковим клієнтом: перевіряємо саму логіку
delete_row() (у тому числі захист від видалення не того рядка) і поведінку
маршруту /delete, не звертаючись до реального API.
"""
import pytest
import app as app_module
from app import COLUMN_ORDER, delete_row, row_fingerprint

HEADER = list(COLUMN_ORDER)


def make_row(day, category, amount, added_at=None):
    return [
        f"2026-07-{day:02d}",
        category,
        str(amount),
        "коментар",
        "2026-07-01T10:00:00",
        added_at if added_at is not None else f"2026-07-{day:02d}T10:00:00+00:00",
        "",
        "pytest",
        "",  # split_id
        "",  # split_info
    ]


class FakeWorksheet:
    def __init__(self, rows):
        self.rows = rows
        self.deleted = []
        self.archived = []

    def row_values(self, row_number):
        # Як і справжній gspread: за межами даних — порожній список.
        if 1 <= row_number <= len(self.rows):
            return list(self.rows[row_number - 1])
        return []

    def delete_rows(self, row_number):
        self.deleted.append(row_number)
        del self.rows[row_number - 1]

    def append_row(self, values, value_input_option=None):
        self.archived.append(list(values))


class FakeClient:
    def __init__(self, worksheet):
        self.worksheet_obj = worksheet
        self.deleted_ws = FakeWorksheet([])
        self.opened_keys = []
        self.requested_names = []

    def open_by_key(self, key):
        self.opened_keys.append(key)
        return self

    def worksheet(self, name):
        self.requested_names.append(name)
        if name == "Deleted":
            return self.deleted_ws
        return self.worksheet_obj


@pytest.fixture
def sheet(monkeypatch):
    """Аркуш із трьома записами у рядках 2, 3, 4."""
    ws = FakeWorksheet([HEADER, make_row(1, "Продукти", 10), make_row(2, "Кава", 20), make_row(3, "Таксі", 30)])
    client = FakeClient(ws)
    monkeypatch.setattr(app_module, "get_client", lambda: client)
    return ws


class TestDeleteRow:
    def _fingerprint(self, row):
        return row_fingerprint(app_module._row_to_entry(row))

    def test_deletes_matching_row(self, sheet):
        target = list(sheet.rows[2])  # рядок 3 аркуша — "Кава"
        assert delete_row("Витрати", 3, self._fingerprint(target)) is True
        assert sheet.deleted == [3]
        assert [row[1] for row in sheet.rows[1:]] == ["Продукти", "Таксі"]

    def test_refuses_when_row_contents_changed(self, sheet):
        # Відпечаток від іншого рядка: сторінка застаріла, номери зсунулись.
        stale = self._fingerprint(make_row(9, "Інше", 999))
        assert delete_row("Витрати", 3, stale) is False
        assert sheet.deleted == []
        assert len(sheet.rows) == 4

    def test_refuses_when_row_is_beyond_data(self, sheet):
        target = list(sheet.rows[1])
        assert delete_row("Витрати", 99, self._fingerprint(target)) is False
        assert sheet.deleted == []

    def test_refuses_empty_fingerprint(self, sheet):
        # Інакше порожній відпечаток збігся б із порожнім рядком за межами даних.
        assert delete_row("Витрати", 99, ["", "", "", ""]) is False
        assert sheet.deleted == []

    def test_matches_row_without_added_at(self, sheet):
        # Рядок, дописаний у таблицю вручну: added_at порожній,
        # звіряння має спрацювати за датою/категорією/сумою.
        manual = make_row(4, "Ринок", 40, added_at="")
        sheet.rows.append(manual)
        assert delete_row("Витрати", 5, self._fingerprint(manual)) is True
        assert sheet.deleted == [5]

    def test_uses_requested_worksheet(self, sheet, monkeypatch):
        client_holder = {}

        def fake_get_client():
            client = FakeClient(sheet)
            client_holder["client"] = client
            return client

        monkeypatch.setattr(app_module, "get_client", fake_get_client)
        delete_row("Доходи", 2, self._fingerprint(sheet.rows[1]))
        assert client_holder["client"].requested_names == ["Доходи"]


class TestDeleteRoute:
    def _form(self, row_number=3, entry_type="expense", **overrides):
        data = {
            "type": entry_type,
            "row_number": row_number,
            "fp_date": "2026-07-02",
            "fp_category": "Кава",
            "fp_amount": "20",
            "fp_added_at": "2026-07-02T10:00:00+00:00",
        }
        data.update(overrides)
        return data

    def test_requires_login(self, client):
        response = client.post("/delete", data=self._form(), follow_redirects=False)
        assert response.status_code == 302
        assert "/login" in response.headers["Location"]

    def test_deletes_and_reports_success(self, logged_in_client, sheet):
        response = logged_in_client.post("/delete", data=self._form(), follow_redirects=True)
        assert response.status_code == 200
        assert "Запис видалено".encode() in response.data
        assert sheet.deleted == [3]

    def test_routes_income_to_income_worksheet(self, logged_in_client, monkeypatch):
        calls = {}

        def fake_delete_row(ws_name, row_number, fingerprint, entry_type=None, split_id=None):
            calls["args"] = (ws_name, row_number, fingerprint, entry_type, split_id)
            return True

        monkeypatch.setattr(app_module, "delete_row", fake_delete_row)
        logged_in_client.post("/delete", data=self._form(entry_type="income"), follow_redirects=True)
        assert calls["args"][0] == app_module.WORKSHEET_INCOME
        assert calls["args"][1] == 3
        assert calls["args"][2] == ["2026-07-02", "Кава", "20", "2026-07-02T10:00:00+00:00"]
        assert calls["args"][3] == "income"
        assert calls["args"][4] is None

    def test_reports_stale_page(self, logged_in_client, sheet):
        response = logged_in_client.post(
            "/delete", data=self._form(fp_category="Щось інше"), follow_redirects=True
        )
        assert "оновіть сторінку".encode() in response.data
        assert sheet.deleted == []

    @pytest.mark.parametrize("row_number", ["1", "0", "abc", ""])
    def test_rejects_invalid_row_number(self, logged_in_client, sheet, row_number):
        response = logged_in_client.post(
            "/delete", data=self._form(row_number=row_number), follow_redirects=True
        )
        assert "Некоректний запис для видалення".encode() in response.data
        assert sheet.deleted == []

    def test_rejects_unknown_type(self, logged_in_client, sheet):
        response = logged_in_client.post(
            "/delete", data=self._form(entry_type="донат"), follow_redirects=True
        )
        assert "Некоректний тип запису".encode() in response.data
        assert sheet.deleted == []

    def test_reports_sheets_failure(self, logged_in_client, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("API недоступний")

        monkeypatch.setattr(app_module, "delete_row", boom)
        response = logged_in_client.post("/delete", data=self._form(), follow_redirects=True)
        assert response.status_code == 200
        assert "Помилка видалення з таблиці".encode() in response.data


class TestDeleteFormRendering:
    """
    Форма видалення має нести номер рядка і повний відпечаток запису —
    без них /delete не зможе знайти й звірити рядок.
    """

    @pytest.fixture
    def rendered(self, logged_in_client, monkeypatch):
        entry = app_module._row_to_entry(make_row(2, "Кава", 20))
        entry["row_number"] = 4

        def fake_recent(ws_name, limit=5):
            return [entry] if ws_name == app_module.WORKSHEET_EXPENSE else []

        monkeypatch.setattr(app_module, "get_recent_entries", fake_recent)
        return logged_in_client.get("/").data.decode()

    def test_renders_row_number(self, rendered):
        assert 'name="row_number" value="4"' in rendered

    def test_renders_full_fingerprint(self, rendered):
        for col in app_module.FINGERPRINT_COLUMNS:
            assert f'name="fp_{col}"' in rendered
        assert 'name="fp_category" value="Кава"' in rendered
        assert 'name="fp_amount" value="20"' in rendered

    def test_posts_to_delete_route(self, rendered):
        assert 'action="/delete"' in rendered
        assert 'name="type" value="expense"' in rendered
