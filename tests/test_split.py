"""
Тести розбитих операцій (split): видалення та редагування.

Google Sheets підмінений фейковим аркушем, який поводиться як справжній:
append_row реально дописує рядок, update() має сигнатуру gspread 6
(значення першим аргументом), get_all_values() доповнює рядки до прямокутника,
а row_values() — навпаки, обрізає порожні хвости.

Головне, що тут перевіряється: операція розбивки живе в кількох рядках
аркуша, тому і видалення, і редагування мають бути атомарними, а знайти ці
рядки треба за split_id, а не за номером рядка — номери зсуваються.
"""
import json
import re

import pytest

import app as app_module
from app import COLUMN_ORDER

HEADER = list(COLUMN_ORDER)


class FakeWorksheet:
    def __init__(self, rows):
        self.rows = [list(row) for row in rows]

    def get_all_values(self):
        width = max(len(row) for row in self.rows)
        return [list(row) + [""] * (width - len(row)) for row in self.rows]

    def row_values(self, row_number):
        if not 1 <= row_number <= len(self.rows):
            return []
        row = list(self.rows[row_number - 1])
        while row and row[-1] == "":
            row.pop()
        return row

    def delete_rows(self, row_number):
        del self.rows[row_number - 1]

    def append_row(self, values, value_input_option=None):
        self.rows.append([str(value) for value in values])

    def update(self, values, range_name, value_input_option=None):
        row_number = int(re.search(r"A(\d+)", range_name).group(1))
        self.rows[row_number - 1] = [str(value) for value in values[0]]

    @property
    def entries(self):
        """Рядки даних без заголовка."""
        return self.rows[1:]


class FakeClient:
    def __init__(self):
        self.expense = FakeWorksheet([HEADER])
        self.income = FakeWorksheet([HEADER])
        self.deleted = FakeWorksheet([app_module.DELETED_COLUMN_ORDER])

    def open_by_key(self, key):
        return self

    def worksheet(self, name):
        if name == app_module.WORKSHEET_DELETED:
            return self.deleted
        if name == app_module.WORKSHEET_INCOME:
            return self.income
        return self.expense


@pytest.fixture
def sheets(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(app_module, "get_client", lambda: client)
    # Порядок категорій читає обидва аркуші на кожен GET / — для цих тестів зайве.
    monkeypatch.setattr(app_module, "maybe_update_category_order", lambda: None)
    return client


def submit_split(client, total, parts, day="2026-07-20", note="нотатка"):
    """Створює розбивку через справжній /submit, а не підкладанням рядків."""
    breakdown = [{"category": category, "amount": amount} for category, amount in parts]
    return client.post(
        "/submit",
        data={
            "type": "expense",
            "amount": total,
            "date": day,
            "note": note,
            "split_breakdown": json.dumps(breakdown),
        },
        follow_redirects=True,
    )


def split_delete_form(html):
    """Поля форми видалення розбивки, як їх надішле браузер."""
    forms = [
        form
        for form in re.findall(r'<form class="recent-delete".*?</form>', html, re.S)
        if 'name="split_id"' in form
    ]
    assert forms, "розбивку не показано серед останніх записів"
    return dict(re.findall(r'name="([^"]+)" value="([^"]*)"', forms[0]))


def split_edit_attrs(html):
    """data-edit-* атрибути кнопки редагування розбивки."""
    button = re.search(
        r'<button type="button" class="recent-edit__btn".*?data-edit-split-id.*?>📝</button>', html, re.S
    )
    assert button, "кнопку редагування розбивки не знайдено"
    return dict(re.findall(r'data-edit-([a-z-]+)="([^"]*)"', button.group(0)))


def edit_payload(attrs, breakdown=None, **overrides):
    data = {
        "type": "expense",
        "row_number": attrs["row"],
        "split_id": attrs["split-id"],
        "date": attrs["date"],
        "note": "нотатка",
        "fp_date": attrs["fp-date"],
        "fp_category": attrs["fp-category"],
        "fp_amount": attrs["fp-amount"],
        "fp_added_at": attrs["fp-added-at"],
    }
    if breakdown is not None:
        data["split_breakdown"] = json.dumps(breakdown)
    data.update(overrides)
    return data


class TestSplitDelete:
    def test_deletes_every_row_of_the_split(self, sheets, logged_in_client):
        submit_split(logged_in_client, "300", [("🛒 Продукти", "100"), ("🎭 Розваги", "200")])
        assert len(sheets.expense.entries) == 2

        form = split_delete_form(logged_in_client.get("/").data.decode())
        body = logged_in_client.post("/delete", data=form, follow_redirects=True).data.decode()

        assert "Запис видалено" in body
        assert sheets.expense.entries == []

    def test_archives_every_row_of_the_split(self, sheets, logged_in_client):
        submit_split(logged_in_client, "600", [("🛒 Продукти", "100"), ("🎭 Розваги", "200"), ("📌 Інше", "300")])
        form = split_delete_form(logged_in_client.get("/").data.decode())
        logged_in_client.post("/delete", data=form, follow_redirects=True)

        assert len(sheets.deleted.entries) == 3
        assert all(row[9] == "expense" for row in sheets.deleted.entries)

    def test_survives_rows_shifting_under_the_page(self, sheets, logged_in_client):
        """
        Найважливіший випадок: сторінку відрендерили, а потім у таблиці
        з'явився рядок вище — усі номери рядків зсунулись. Розбивку шукаємо за
        split_id, тому видалення має спрацювати, а не впертись у «оновіть сторінку».
        """
        submit_split(logged_in_client, "300", [("🛒 Продукти", "100"), ("🎭 Розваги", "200")])
        form = split_delete_form(logged_in_client.get("/").data.decode())

        sheets.expense.rows.insert(1, ["2026-07-01", "📌 Інше", "5", "", "", "вручну", "", "", "", ""])
        body = logged_in_client.post("/delete", data=form, follow_redirects=True).data.decode()

        assert "Запис видалено" in body
        assert [row[1] for row in sheets.expense.entries] == ["📌 Інше"]

    def test_deletes_only_the_requested_split(self, sheets, logged_in_client):
        submit_split(logged_in_client, "300", [("🛒 Продукти", "100"), ("🎭 Розваги", "200")], day="2026-07-18")
        submit_split(logged_in_client, "500", [("📌 Інше", "200"), ("👕 Одяг", "300")], day="2026-07-19")

        form = split_delete_form(logged_in_client.get("/").data.decode())
        logged_in_client.post("/delete", data=form, follow_redirects=True)

        remaining = {row[8] for row in sheets.expense.entries}
        assert len(remaining) == 1 and form["split_id"] not in remaining

    def test_refuses_when_the_split_actually_changed(self, sheets, logged_in_client):
        submit_split(logged_in_client, "300", [("🛒 Продукти", "100"), ("🎭 Розваги", "200")])
        form = split_delete_form(logged_in_client.get("/").data.decode())
        form["fp_amount"] = "999"

        body = logged_in_client.post("/delete", data=form, follow_redirects=True).data.decode()

        assert "оновіть сторінку" in body
        assert len(sheets.expense.entries) == 2


class TestSplitEdit:
    def test_updates_categories_and_amounts(self, sheets, logged_in_client):
        submit_split(logged_in_client, "300", [("🛒 Продукти", "100"), ("🎭 Розваги", "200")])
        attrs = split_edit_attrs(logged_in_client.get("/").data.decode())

        body = logged_in_client.post(
            "/edit",
            data=edit_payload(
                attrs,
                breakdown=[
                    {"category": "🛒 Продукти", "amount": 120.0},
                    {"category": "👕 Одяг", "amount": 180.0},
                ],
            ),
            follow_redirects=True,
        ).data.decode()

        assert "Розбита операція оновлена" in body
        assert [(row[1], row[2]) for row in sheets.expense.entries] == [
            ("🛒 Продукти", "120,0"),
            ("👕 Одяг", "180,0"),
        ]

    def test_writes_amounts_in_the_same_format_as_submit(self, sheets, logged_in_client):
        """Дописані й відредаговані суми мають лежати в таблиці однаково ("100,0")."""
        submit_split(logged_in_client, "300", [("🛒 Продукти", "100"), ("🎭 Розваги", "200")])
        attrs = split_edit_attrs(logged_in_client.get("/").data.decode())

        logged_in_client.post(
            "/edit",
            data=edit_payload(
                attrs,
                breakdown=[
                    {"category": "🛒 Продукти", "amount": 150.0},
                    {"category": "🎭 Розваги", "amount": 150.0},
                ],
            ),
            follow_redirects=True,
        )

        assert all("." not in row[2] for row in sheets.expense.entries)

    def test_removing_a_category_removes_its_row(self, sheets, logged_in_client):
        submit_split(logged_in_client, "600", [("🛒 Продукти", "100"), ("🎭 Розваги", "200"), ("📌 Інше", "300")])
        attrs = split_edit_attrs(logged_in_client.get("/").data.decode())

        logged_in_client.post(
            "/edit",
            data=edit_payload(
                attrs,
                breakdown=[
                    {"category": "🛒 Продукти", "amount": 300.0},
                    {"category": "🎭 Розваги", "amount": 300.0},
                ],
            ),
            follow_redirects=True,
        )

        assert [row[1] for row in sheets.expense.entries] == ["🛒 Продукти", "🎭 Розваги"]

    def test_adding_a_category_adds_a_row(self, sheets, logged_in_client):
        submit_split(logged_in_client, "300", [("🛒 Продукти", "100"), ("🎭 Розваги", "200")])
        attrs = split_edit_attrs(logged_in_client.get("/").data.decode())

        logged_in_client.post(
            "/edit",
            data=edit_payload(
                attrs,
                breakdown=[
                    {"category": "🛒 Продукти", "amount": 100.0},
                    {"category": "🎭 Розваги", "amount": 100.0},
                    {"category": "📌 Інше", "amount": 100.0},
                ],
            ),
            follow_redirects=True,
        )

        rows = sheets.expense.entries
        assert [row[1] for row in rows] == ["🛒 Продукти", "🎭 Розваги", "📌 Інше"]
        assert len({row[8] for row in rows}) == 1, "у дописаного рядка інший split_id"

    def test_updates_date_and_note_across_all_rows(self, sheets, logged_in_client):
        submit_split(logged_in_client, "300", [("🛒 Продукти", "100"), ("🎭 Розваги", "200")])
        attrs = split_edit_attrs(logged_in_client.get("/").data.decode())

        logged_in_client.post(
            "/edit",
            data=edit_payload(attrs, date="2026-07-21", note="оновлено"),
            follow_redirects=True,
        )

        assert all(row[0] == "2026-07-21" and row[3] == "оновлено" for row in sheets.expense.entries)

    def test_survives_rows_shifting_under_the_page(self, sheets, logged_in_client):
        submit_split(logged_in_client, "300", [("🛒 Продукти", "100"), ("🎭 Розваги", "200")])
        attrs = split_edit_attrs(logged_in_client.get("/").data.decode())
        sheets.expense.rows.insert(1, ["2026-07-01", "📌 Інше", "5", "", "", "вручну", "", "", "", ""])

        body = logged_in_client.post(
            "/edit", data=edit_payload(attrs, note="оновлено"), follow_redirects=True
        ).data.decode()

        assert "Розбита операція оновлена" in body
        assert [row[3] for row in sheets.expense.entries[1:]] == ["оновлено", "оновлено"]

    def test_rejects_breakdown_with_one_category(self, sheets, logged_in_client):
        submit_split(logged_in_client, "300", [("🛒 Продукти", "100"), ("🎭 Розваги", "200")])
        attrs = split_edit_attrs(logged_in_client.get("/").data.decode())

        body = logged_in_client.post(
            "/edit",
            data=edit_payload(attrs, breakdown=[{"category": "🛒 Продукти", "amount": 300.0}]),
            follow_redirects=True,
        ).data.decode()

        assert "щонайменше 2 категорії" in body
        assert len(sheets.expense.entries) == 2

    def test_keeps_category_order_stable(self, sheets, logged_in_client):
        """Збереження без змін не повинно перевертати категорії місцями."""
        submit_split(logged_in_client, "600", [("🛒 Продукти", "100"), ("🎭 Розваги", "200"), ("📌 Інше", "300")])
        before = [row[1] for row in sheets.expense.entries]
        attrs = split_edit_attrs(logged_in_client.get("/").data.decode())

        logged_in_client.post(
            "/edit",
            data=edit_payload(
                attrs,
                breakdown=[
                    {"category": "🛒 Продукти", "amount": 100.0},
                    {"category": "🎭 Розваги", "amount": 200.0},
                    {"category": "📌 Інше", "amount": 300.0},
                ],
            ),
            follow_redirects=True,
        )

        assert [row[1] for row in sheets.expense.entries] == before

    def test_details_are_listed_in_sheet_order(self, sheets, logged_in_client):
        submit_split(logged_in_client, "600", [("🛒 Продукти", "100"), ("🎭 Розваги", "200"), ("📌 Інше", "300")])
        html = logged_in_client.get("/").data.decode()

        details = re.findall(r'<div class="split-detail-item">(.*?) ·', html, re.S)
        assert [d.strip() for d in details] == ["🛒 Продукти", "🎭 Розваги", "📌 Інше"]

    def test_editing_then_deleting_still_works(self, sheets, logged_in_client):
        """Після редагування відпечаток на сторінці новий — видалення має його прийняти."""
        submit_split(logged_in_client, "300", [("🛒 Продукти", "100"), ("🎭 Розваги", "200")])
        attrs = split_edit_attrs(logged_in_client.get("/").data.decode())
        logged_in_client.post(
            "/edit",
            data=edit_payload(
                attrs,
                breakdown=[
                    {"category": "🛒 Продукти", "amount": 150.0},
                    {"category": "🎭 Розваги", "amount": 150.0},
                ],
            ),
            follow_redirects=True,
        )

        form = split_delete_form(logged_in_client.get("/").data.decode())
        body = logged_in_client.post("/delete", data=form, follow_redirects=True).data.decode()

        assert "Запис видалено" in body
        assert sheets.expense.entries == []


class TestSplitEditModalMarkup:
    """
    Модалка редагування розбивки ховає поле категорії. Воно required, тому
    має бути ще й вимкненим у JS — інакше браузер не пропускає форму і не може
    показати помилку на display:none полі: кнопка "Зберегти" мовчки не працює.
    """

    @pytest.fixture
    def rendered(self, sheets, logged_in_client):
        submit_split(logged_in_client, "300", [("🛒 Продукти", "100"), ("🎭 Розваги", "200")])
        return logged_in_client.get("/").data.decode()

    def test_disables_hidden_required_category(self, rendered):
        assert "editCategorySelect.disabled = true;" in rendered

    def test_close_reenables_controls(self, rendered):
        close_body = re.search(r"function closeEdit\(\).*?currentEditSplitEntries = null;", rendered, re.S).group(0)
        assert "editCategorySelect.disabled = false;" in close_body
        assert "editAmount.disabled = false;" in close_body

    def test_split_total_is_available_to_the_modal(self, rendered):
        attrs = split_edit_attrs(rendered)
        assert attrs["split-total"] == "300.0"
        assert "editBtn.dataset.editSplitTotal" in rendered
