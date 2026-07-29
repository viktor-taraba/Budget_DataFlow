import os
import json
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.middleware.proxy_fix import ProxyFix
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials
from config import COLUMN_ORDER, DELETED_COLUMN_ORDER, WORKSHEET_EXPENSE, WORKSHEET_INCOME, WORKSHEET_DELETED

# Кеширование частоти категорій (раз на день)
_category_frequency_cache = None
_category_frequency_date = None

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ["FLASK_SECRET_KEY"]
SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
APP_PASSWORD = os.environ["APP_PASSWORD"]

# Завантажуємо категорії з categories.json
def load_categories():
    try:
        with open("categories.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"expense": [], "income": []}

CATEGORIES = load_categories()

# Render (і більшість хостингів) стоїть за проксі: без цього request.remote_addr завжди буде адресою проксі, а не клієнта,
# і rate-limit нижче рахуватиме всіх користувачів як одного.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)

# Обмеження кількості спроб входу — захист від підбору пароля.
# Рахує спроби по реальному IP клієнта (див. ProxyFix вище); при перевищенні повертає 429 Too Many Requests.
limiter = Limiter(get_remote_address, app=app, default_limits=[])

# Google Sheets
_gs_client = None

def get_client():
    global _gs_client
    if _gs_client is None:
        creds_json = os.environ["GOOGLE_CREDENTIALS_JSON"]
        info = json.loads(creds_json)
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds = Credentials.from_service_account_info(info, scopes=scopes)
        _gs_client = gspread.authorize(creds)
    return _gs_client


def worksheet_for(entry_type: str) -> str:
    return WORKSHEET_INCOME if entry_type == "income" else WORKSHEET_EXPENSE


def append_row(entry_type: str, row: dict, split_info=None):
    """
    Дописує один або кілька рядків до аркуша.

    Якщо split_info передано (list of {category, amount} dicts):
    - Генерує split_id (UUID)
    - Для кожної категорії у split_info створює рядок з цією категорією/сумою
    - Встановлює split_id для всіх рядків
    - Встановлює split_info (JSON) для всіх рядків

    Якщо split_info = None:
    - Дописує один рядок як раніше (split_id та split_info залишаються пустими)
    """
    client = get_client()
    sheet = client.open_by_key(SHEET_ID)
    ws = sheet.worksheet(worksheet_for(entry_type))

    if split_info:
        split_id = str(uuid.uuid4())
        total_amount = sum(item["amount"] for item in split_info)
        split_info_json = json.dumps({
            "total": round(total_amount, 2),
            "categories": split_info
        }, ensure_ascii=False)

        for split_row in split_info:
            row_data = {**row}
            row_data["category"] = split_row["category"]
            row_data["amount"] = split_row["amount"]
            row_data["split_id"] = split_id
            row_data["split_info"] = split_info_json

            values = []
            for col in COLUMN_ORDER:
                val = row_data.get(col, "")
                if col == "amount" and val and isinstance(val, (int, float)):
                    val = str(val).replace(".", ",")
                values.append(val)
            ws.append_row(values, value_input_option="USER_ENTERED")
    else:
        values = []
        for col in COLUMN_ORDER:
            val = row.get(col, "")
            if col == "amount" and val and isinstance(val, (int, float)):
                val = str(val).replace(".", ",")
            values.append(val)
        ws.append_row(values, value_input_option="USER_ENTERED")


def _row_to_entry(row) -> dict:
    """Розкладає сирий рядок аркуша по назвах стовпців з COLUMN_ORDER."""
    return {col: (row[i] if i < len(row) else "") for i, col in enumerate(COLUMN_ORDER)}


def _all_entries(all_values):
    """Розкладає всі рядки даних аркуша (без рядка заголовків) у список записів."""
    if len(all_values) <= 1:
        return []
    return [_row_to_entry(row) for row in all_values[1:]]


def _rows_to_entries(all_values, limit: int):
    """
    Перетворює сирі значення аркуша (разом із рядком заголовків) на останні
    `limit` записів, найновіші першими.

    Крім значень стовпців, кожен запис містить `row_number` — номер рядка в
    аркуші (заголовок — рядок 1, дані починаються з 2). Він потрібен, щоб
    видалити саме цей рядок через delete_rows().
    """
    entries = _all_entries(all_values)
    if not entries:
        return []

    start = max(0, len(entries) - limit)
    sliced = entries[start:]
    for offset, entry in enumerate(sliced, start=start):
        entry["row_number"] = offset + 2

    sliced.reverse()
    return sliced


def _group_split_entries(entries):
    """
    Групує записи за split_id.

    Якщо split_id пусте, кожен запис залишається як є.
    Якщо split_id присутній, всі рядки з однаковим split_id об'єднуються в один логічний запис:
    - Категорії: "Продукти + Кава"
    - Використовується дата/added_at/row_number першого рядка
    - Встановлюється флаг split_count = N
    - Зберігаються оригінальні рядки для операцій редагування/видалення

    Повертає список групованих записів, найновіші першими.
    """
    if not entries:
        return []

    grouped = {}
    result_order = []

    for entry in entries:
        split_id = entry.get("split_id", "").strip()

        if not split_id:
            grouped[f"single_{id(entry)}"] = entry
            result_order.append(f"single_{id(entry)}")
        else:
            if split_id not in grouped:
                grouped[split_id] = {
                    "split_entries": [],
                }
                result_order.append(split_id)
            grouped[split_id]["split_entries"].append(entry)

    result = []
    for key in result_order:
        if key.startswith("single_"):
            result.append(grouped[key])
        else:
            split_group = grouped[key]
            split_entries = split_group["split_entries"]

            first_entry = split_entries[0]
            categories = " + ".join(e.get("category", "Інше") for e in split_entries)
            total_amount = sum(
                validate_amount(e.get("amount", "0")) or 0
                for e in split_entries
            )

            grouped_entry = {
                **first_entry,
                "category": categories,
                "split_count": len(split_entries),
                "split_entries": split_entries,
                "split_total_amount": total_amount,
            }
            result.append(grouped_entry)

    return result


def get_recent_entries(ws_name: str, limit: int = 5):
    """
    Повертає останні `limit` записів з аркуша, найновіші першими.

    Групує рядки розбивки за split_id, так що одна логічна операція
    розбивки розбивки відображається як один запис.

    Читає лише вже записані рядки (без запису), тому безпечно викликати
    при кожному відкритті головної сторінки.
    """
    client = get_client()
    sheet = client.open_by_key(SHEET_ID)
    ws = sheet.worksheet(ws_name)
    entries = _rows_to_entries(ws.get_all_values(), limit * 2)
    grouped = _group_split_entries(entries)
    return grouped[:limit]


# Статистика за період
# Довший період — це все одно один get_all_values() на аркуш, а не N запитів,
# тому обмежуємо лише "розумну" максимальну довжину, а не кількість рядків.
MAX_STATS_RANGE_DAYS = 366


def _date_range(start_date: str, end_date: str) -> list:
    """Список ISO-дат від start_date до end_date включно (для щоденного графіка з нулями)."""
    start = datetime.strptime(start_date, "%Y-%m-%d").date()
    end = datetime.strptime(end_date, "%Y-%m-%d").date()

    days = []
    current = start
    while current <= end:
        days.append(current.isoformat())
        current += timedelta(days=1)
    return days


def _aggregate_stats(entries, start_date: str, end_date: str) -> dict:
    """
    Агрегує записи одного аркуша в межах [start_date, end_date] (включно):
    загальна сума, суми по днях (з нулями для днів без записів — інакше
    графік "стрибав" би по датах) і суми по категоріях, за спаданням.

    Рядки з датою поза періодом чи сумою, яку не вдається розпарсити
    (ручні правки в таблиці), тихо пропускаються — так само, як
    get_recent_entries не падає через окремий зіпсований рядок.
    """
    daily_totals = {}
    category_totals = {}
    total = 0.0

    for entry in entries:
        entry_date = entry.get("date", "")
        if not (start_date <= entry_date <= end_date):
            continue
        amount = validate_amount(entry.get("amount"))
        if amount is None:
            continue

        total += amount
        daily_totals[entry_date] = daily_totals.get(entry_date, 0.0) + amount
        category = entry.get("category") or "Інше"
        category_totals[category] = category_totals.get(category, 0.0) + amount

    daily = [
        {"date": d, "amount": round(daily_totals.get(d, 0.0), 2)}
        for d in _date_range(start_date, end_date)
    ]
    categories = sorted(
        ({"category": c, "amount": round(a, 2)} for c, a in category_totals.items()),
        key=lambda item: item["amount"],
        reverse=True,
    )

    return {"total": round(total, 2), "daily": daily, "categories": categories}


def get_period_stats(ws_name: str, start_date: str, end_date: str) -> dict:
    """Читає весь аркуш і агрегує його за _aggregate_stats для заданого періоду."""
    client = get_client()
    sheet = client.open_by_key(SHEET_ID)
    ws = sheet.worksheet(ws_name)
    return _aggregate_stats(_all_entries(ws.get_all_values()), start_date, end_date)


# Видалення записів
# Набір стовпців, за якими звіряємо, що видаляємо саме той рядок, який
# користувач бачив на сторінці. `added_at` ставить сервер, тому практично
# унікальний; решта — щоб спрацювало і для рядків, дописаних у таблицю вручну.
FINGERPRINT_COLUMNS = ("date", "category", "amount", "added_at")


def row_fingerprint(entry: dict) -> list:
    return [str(entry.get(col, "")) for col in FINGERPRINT_COLUMNS]


def delete_row(ws_name: str, row_number: int, expected_fingerprint: list, entry_type: str = None, split_id: str = None) -> bool:
    """
    Видаляє рядок(и) з аркуша, але лише якщо дані досі збігаються.

    Якщо split_id передано:
    - Знаходить всі рядки з цим split_id
    - Видаляє всі (розбивка видаляється як одна операція)
    - Архівує всі до листа DELETED

    Якщо split_id не передано:
    - Видаляє один рядок як раніше (назад сумісно)

    Сторінка могла бути відкрита давно, а таблиця за цей час — змінитися
    (додали або видалили записи, і номери рядків зсунулись). Тому перед
    видаленням перечитуємо рядок(и) і звіряємо с тим, що був(и) на сторінці.
    Якщо не збігається — не видаляємо нічого і повертаємо False.
    """
    if not any(expected_fingerprint):
        return False

    client = get_client()
    sheet = client.open_by_key(SHEET_ID)
    ws = sheet.worksheet(ws_name)

    if split_id:
        all_values = ws.get_all_values()
        all_entries = _all_entries(all_values)

        rows_to_delete = []
        for offset, entry in enumerate(all_entries):
            if entry.get("split_id", "").strip() == split_id:
                rows_to_delete.append((offset + 2, entry))

        if not rows_to_delete:
            return False

        current = _row_to_entry(ws.row_values(row_number))
        if row_fingerprint(current) != list(expected_fingerprint):
            return False

        if entry_type:
            deleted_ws = sheet.worksheet(WORKSHEET_DELETED)
            for _, entry in rows_to_delete:
                deleted_entry = {**entry}
                deleted_entry["deleted_at"] = datetime.now(timezone.utc).isoformat()
                deleted_entry["income_or_expense"] = "income" if entry_type == "income" else "expense"
                values = [deleted_entry.get(col, "") for col in DELETED_COLUMN_ORDER]
                deleted_ws.append_row(values, value_input_option="USER_ENTERED")

        for row_num, _ in sorted(rows_to_delete, key=lambda x: x[0], reverse=True):
            ws.delete_rows(row_num)

        return True
    else:
        current = _row_to_entry(ws.row_values(row_number))
        if row_fingerprint(current) != list(expected_fingerprint):
            return False

        if entry_type:
            deleted_ws = sheet.worksheet(WORKSHEET_DELETED)
            deleted_entry = {**current}
            deleted_entry["deleted_at"] = datetime.now(timezone.utc).isoformat()
            deleted_entry["income_or_expense"] = "income" if entry_type == "income" else "expense"
            values = [deleted_entry.get(col, "") for col in DELETED_COLUMN_ORDER]
            deleted_ws.append_row(values, value_input_option="USER_ENTERED")

        ws.delete_rows(row_number)
        return True


def update_row(ws_name: str, row_number: int, expected_fingerprint: list, updates: dict, split_id: str = None, split_breakdown=None) -> bool:
    """
    Оновлює рядок(и) в аркуші, але лише якщо дані досі збігаються.

    Якщо split_id передано:
    - Знаходить всі рядки з цим split_id
    - Якщо split_breakdown також передано: оновлює кожен рядок з новою категорією/сумою з split_breakdown
    - Якщо split_breakdown не передано: оновлює всі рядки splits з переданими updates (date, note, etc)
    - Оновлює split_info якщо split_breakdown передано
    - updated_at встановлюється для всіх

    Якщо split_id не передано:
    - Оновлює один рядок як раніше (назад сумісно)

    updates: dict з ключами, які потрібно змінити (date, category, amount, note).
    Системні поля (submitted_at, added_at, device_info) не змінюються.
    Встановлює updated_at на поточний час.
    """
    if not any(expected_fingerprint):
        return False

    client = get_client()
    sheet = client.open_by_key(SHEET_ID)
    ws = sheet.worksheet(ws_name)

    if split_id:
        all_values = ws.get_all_values()
        all_entries = _all_entries(all_values)

        rows_to_update = []
        for offset, entry in enumerate(all_entries):
            if entry.get("split_id", "").strip() == split_id:
                rows_to_update.append((offset + 2, entry))

        if not rows_to_update:
            return False

        current = _row_to_entry(ws.row_values(row_number))
        if row_fingerprint(current) != list(expected_fingerprint):
            return False

        if split_breakdown:
            total_amount = sum(item["amount"] for item in split_breakdown)
            split_info_json = json.dumps({
                "total": round(total_amount, 2),
                "categories": split_breakdown
            }, ensure_ascii=False)
            updates["split_info"] = split_info_json

        updates["updated_at"] = datetime.now(timezone.utc).isoformat()

        for idx, (row_num, entry) in enumerate(rows_to_update):
            updated_entry = {**entry, **updates}
            if split_breakdown and idx < len(split_breakdown):
                updated_entry["category"] = split_breakdown[idx]["category"]
                updated_entry["amount"] = split_breakdown[idx]["amount"]
            values = [updated_entry.get(col, "") for col in COLUMN_ORDER]
            ws.update(f'A{row_num}:Z{row_num}', [values])

        return True
    else:
        current = _row_to_entry(ws.row_values(row_number))
        if row_fingerprint(current) != list(expected_fingerprint):
            return False

        updates["updated_at"] = datetime.now(timezone.utc).isoformat()
        updated_entry = {**current, **updates}
        values = [updated_entry.get(col, "") for col in COLUMN_ORDER]
        ws.update(f'A{row_number}:Z{row_number}', [values])
        return True


# Валідація (винесена в окремі функції — щоб тестувати без Flask/Sheets)
_AMOUNT_PATTERN = re.compile(r"^\d+(\.\d+)?$")
_ROW_NUMBER_PATTERN = re.compile(r"^\d+$")

def validate_amount(raw):
    """
    Парсить і валідує суму з форми.

    Приймає кому як десятковий роздільник. Повертає float > 0, якщо рядок коректний, інакше None. 
    Відхиляє науковий запис (1e10), від'ємні числа, кілька роздільників і сміття.
    """
    if raw is None:
        return None
    cleaned = raw.replace(",", ".").strip()
    if not _AMOUNT_PATTERN.match(cleaned):
        return None
    value = float(cleaned)
    return value if value > 0 else None


def validate_date(raw, max_date=None):
    """
    Валідує дату у форматі YYYY-MM-DD.

    max_date (за замовчуванням — сьогодні) визначає верхню межу: дати з майбутнього відхиляються. 
    Повертає нормалізований ISO-рядок, якщо дата коректна, інакше None.
    """
    if not raw:
        return None
    try:
        parsed = datetime.strptime(raw, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
    if max_date is None:
        max_date = date.today()
    if parsed > max_date:
        return None
    return parsed.isoformat()


def validate_row_number(raw):
    """
    Валідує номер рядка аркуша, отриманий з форми видалення.

    Рядок 1 — це заголовки, тому коректними є лише цілі числа >= 2: так
    підроблений або зіпсований номер не зможе видалити рядок заголовків.
    Повертає int, якщо номер коректний, інакше None.
    """
    if raw is None:
        return None
    cleaned = str(raw).strip()
    if not _ROW_NUMBER_PATTERN.match(cleaned):
        return None
    value = int(cleaned)
    return value if value >= 2 else None


def validate_split(category_amount_pairs, total_amount):
    """
    Валідує розбивку суми на кілька категорій.

    category_amount_pairs: list of dicts {"category": str, "amount": float or str}.
    total_amount: float — загальна сума, яку потрібно розбити.

    Правила:
    - Мінімум 2 категорії (інакше це не розбивка, а звичайна операція)
    - Кожна сума > 0 і повинна парсуватись через validate_amount()
    - Сума всіх вказаних сум повинна бути < total_amount (місце для остатку)
    - Остаток повинен бути >= 0.01 (щоб уникнути floating-point помилок)

    Повертає кортеж (is_valid: bool, error_msg: str | None, split_rows: list | None).
    Якщо valid, split_rows = [{"category": "Cat1", "amount": 100.0}, {...}]
    з остатком у останньому елементі.
    """
    if not category_amount_pairs or len(category_amount_pairs) < 2:
        return False, "Розбивка повинна містити щонайменше 2 категорії", None

    if total_amount is None or total_amount <= 0:
        return False, "Загальна сума повинна бути > 0", None

    split_rows = []
    sum_entered = 0.0

    for pair in category_amount_pairs[:-1]:
        category = pair["category"]
        amount_str = pair["amount"]

        # Якщо amount це число (з JSON), конвертуємо до рядка для validate_amount
        if isinstance(amount_str, (int, float)):
            amount_str = str(amount_str)

        validated = validate_amount(amount_str)
        if validated is None:
            return False, f"Невірна сума для категорії '{category}'", None
        split_rows.append({"category": category, "amount": validated})
        sum_entered += validated

    if sum_entered >= total_amount:
        return False, "Сума розбивки не повинна перевищувати загальну суму", None

    remainder = round(total_amount - sum_entered, 2)
    if remainder < 0.01:
        return False, f"Остаток занадто малий ({remainder}). Перевірте суми.", None

    last_category = category_amount_pairs[-1]["category"]
    split_rows.append({"category": last_category, "amount": remainder})

    return True, None, split_rows


# Авторизація (єдиний користувач — просто пароль у сесії)
def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("authed"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


@app.route("/login", methods=["GET", "POST"])
@limiter.limit("5 per minute", methods=["POST"])
def login():
    if request.method == "POST":
        if request.form.get("password") == APP_PASSWORD:
            session["authed"] = True
            session.permanent = True
            return redirect(request.args.get("next") or url_for("index"))
        flash("Невірний пароль")
    return render_template("login.html")


@app.errorhandler(429)
def ratelimit_handler(e):
    flash("Забагато спроб входу. Спробуйте ще раз за хвилину.")
    return render_template("login.html"), 429


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# Основна сторінка
@app.route("/", methods=["GET"])
@login_required
def index():
    maybe_update_category_order()

    try:
        recent_expenses = get_recent_entries(WORKSHEET_EXPENSE)
        recent_income = get_recent_entries(WORKSHEET_INCOME)
        recent_error = None
    except Exception:
        # Сторінка все одно має відкритись, навіть якщо Google Sheets
        # тимчасово недоступний — просто без блоку останніх записів.
        recent_expenses = []
        recent_income = []
        recent_error = "Не вдалося завантажити останні записи"

    return render_template(
        "index.html",
        today=date.today().isoformat(),
        categories=CATEGORIES,
        recent_expenses=recent_expenses,
        recent_income=recent_income,
        recent_error=recent_error,
        fingerprint_columns=FINGERPRINT_COLUMNS,
    )


@app.route("/submit", methods=["POST"])
@login_required
def submit():
    entry_type = request.form.get("type")
    amount = validate_amount(request.form.get("amount"))
    category = request.form.get("category", "").strip()
    entry_date_raw = request.form.get("date") or date.today().isoformat()
    entry_date = validate_date(entry_date_raw)
    note = request.form.get("note", "").strip()

    error = None
    if amount is None:
        error = "Введіть коректну суму більше нуля"
    if entry_type not in ("income", "expense"):
        error = "Оберіть тип запису"
    if entry_date is None:
        error = "Некоректна дата"
    if error:
        flash(error)
        return redirect(url_for("index"))

    split_breakdown_json = request.form.get("split_breakdown")

    if split_breakdown_json and entry_type == "expense":
        try:
            split_breakdown = json.loads(split_breakdown_json)
            is_valid, error_msg, split_rows = validate_split(split_breakdown, amount)
            if not is_valid:
                flash(error_msg or "Помилка валідації розбивки")
                return redirect(url_for("index"))
        except (json.JSONDecodeError, ValueError) as exc:
            flash(f"Помилка формату розбивки: {exc}")
            return redirect(url_for("index"))

        row = {
            "date": entry_date,
            "category": "",
            "amount": amount,
            "note": note,
            "submitted_at": request.form.get("submitted_at", ""),
            "added_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "device_info": request.headers.get("User-Agent", "unknown"),
        }

        try:
            append_row(entry_type, row, split_info=split_rows)
        except Exception as exc:
            flash(f"Помилка запису розбивки в таблицю: {exc}")
            return redirect(url_for("index"))

        flash("Розбита операція додана", "success")
        return redirect(url_for("index"))
    else:
        if not category:
            error = "Оберіть категорію"
        if error:
            flash(error)
            return redirect(url_for("index"))

        row = {
            "date": entry_date,
            "category": category,
            "amount": amount,
            "note": note,
            "submitted_at": request.form.get("submitted_at", ""),
            "added_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "device_info": request.headers.get("User-Agent", "unknown"),
        }

        try:
            append_row(entry_type, row)
        except Exception as exc:
            flash(f"Помилка запису в таблицю: {exc}")
            return redirect(url_for("index"))

        flash("Запис додано", "success")
        return redirect(url_for("index"))


@app.route("/delete", methods=["POST"])
@login_required
def delete():
    entry_type = request.form.get("type")
    row_number = validate_row_number(request.form.get("row_number"))
    expected_fingerprint = [request.form.get(f"fp_{col}", "") for col in FINGERPRINT_COLUMNS]
    split_id = request.form.get("split_id", "").strip() or None

    if entry_type not in ("income", "expense"):
        flash("Некоректний тип запису")
        return redirect(url_for("index"))
    if row_number is None:
        flash("Некоректний запис для видалення")
        return redirect(url_for("index"))

    try:
        deleted = delete_row(worksheet_for(entry_type), row_number, expected_fingerprint, entry_type, split_id=split_id)
    except Exception as exc:
        flash(f"Помилка видалення з таблиці: {exc}")
        return redirect(url_for("index"))

    if deleted:
        flash("Запис видалено", "success")
    else:
        flash("Запис уже змінився або був видалений — оновіть сторінку")
    return redirect(url_for("index"))


@app.route("/edit", methods=["POST"])
@login_required
def edit():
    entry_type = request.form.get("type")
    row_number = validate_row_number(request.form.get("row_number"))
    expected_fingerprint = [request.form.get(f"fp_{col}", "") for col in FINGERPRINT_COLUMNS]
    split_id = request.form.get("split_id", "").strip() or None
    split_breakdown_json = request.form.get("split_breakdown")

    if entry_type not in ("income", "expense"):
        flash("Некоректний тип запису")
        return redirect(url_for("index"))
    if row_number is None:
        flash("Некоректний запис для редагування")
        return redirect(url_for("index"))

    entry_date_raw = request.form.get("date")
    entry_date = validate_date(entry_date_raw)
    note = request.form.get("note", "").strip()

    if entry_date is None:
        flash("Некоректна дата")
        return redirect(url_for("index"))

    if split_id and not split_breakdown_json:
        # Редагування split без нового breakdown: оновлюємо лише date та note для всіх рядків
        updates = {
            "date": entry_date,
            "note": note,
        }

        try:
            updated = update_row(worksheet_for(entry_type), row_number, expected_fingerprint, updates,
                               split_id=split_id, split_breakdown=None)
        except Exception as exc:
            flash(f"Помилка оновлення розбивки в таблиці: {exc}")
            return redirect(url_for("index"))

        if updated:
            flash("Розбита операція оновлена", "success")
        else:
            flash("Запис уже змінився або був видалений — оновіть сторінку")
        return redirect(url_for("index"))
    elif split_id and split_breakdown_json:
        try:
            split_breakdown = json.loads(split_breakdown_json)
            total_amount = sum(item["amount"] for item in split_breakdown)
            is_valid, error_msg, _ = validate_split(split_breakdown, total_amount)
            if not is_valid:
                flash(error_msg or "Помилка валідації розбивки")
                return redirect(url_for("index"))
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            flash(f"Помилка формату розбивки: {exc}")
            return redirect(url_for("index"))

        updates = {
            "date": entry_date,
            "note": note,
        }

        try:
            updated = update_row(worksheet_for(entry_type), row_number, expected_fingerprint, updates,
                               split_id=split_id, split_breakdown=split_breakdown)
        except Exception as exc:
            flash(f"Помилка оновлення розбивки в таблиці: {exc}")
            return redirect(url_for("index"))

        if updated:
            flash("Розбита операція оновлена", "success")
        else:
            flash("Запис уже змінився або був видалений — оновіть сторінку")
        return redirect(url_for("index"))
    else:
        amount = validate_amount(request.form.get("amount"))
        category = request.form.get("category", "").strip()

        if amount is None:
            flash("Сума повинна бути числом")
            return redirect(url_for("index"))
        if not category:
            flash("Виберіть категорію")
            return redirect(url_for("index"))

        updates = {
            "date": entry_date,
            "category": category,
            "amount": str(amount).replace(".", ","),
            "note": note,
        }

        try:
            updated = update_row(worksheet_for(entry_type), row_number, expected_fingerprint, updates)
        except Exception as exc:
            flash(f"Помилка оновлення в таблиці: {exc}")
            return redirect(url_for("index"))

        if updated:
            flash("Запис оновлено", "success")
        else:
            flash("Запис уже змінився або був видалений — оновіть сторінку")
        return redirect(url_for("index"))


@app.route("/stats", methods=["GET"])
@login_required
def stats():
    today = date.today()
    default_start = (today - timedelta(days=29)).isoformat()  # 30 днів включно з сьогодні

    start_date = validate_date(request.args.get("start")) or default_start
    end_date = validate_date(request.args.get("end")) or today.isoformat()
    if start_date > end_date:
        start_date, end_date = end_date, start_date

    span_days = (date.fromisoformat(end_date) - date.fromisoformat(start_date)).days
    if span_days > MAX_STATS_RANGE_DAYS:
        start_date = (date.fromisoformat(end_date) - timedelta(days=MAX_STATS_RANGE_DAYS)).isoformat()

    try:
        expense = get_period_stats(WORKSHEET_EXPENSE, start_date, end_date)
        income = get_period_stats(WORKSHEET_INCOME, start_date, end_date)
    except Exception as exc:
        return jsonify({"error": f"Не вдалося завантажити статистику: {exc}"}), 502

    return jsonify(
        {
            "start": start_date,
            "end": end_date,
            "expense": expense,
            "income": income,
            "difference": round(income["total"] - expense["total"], 2),
        }
    )


@app.route("/categories", methods=["GET"])
@login_required
def get_categories_endpoint():
    return jsonify(CATEGORIES)


def save_categories():
    with open("categories.json", "w", encoding="utf-8") as f:
        json.dump(CATEGORIES, f, ensure_ascii=False, indent=2)


def count_category_frequency():
    """
    Рахує частоту використання кожної категорії у обох таблицях (без обмеження по періоду).
    Повертає dict {type: {category: count, ...}}.
    """
    try:
        client = get_client()
        sheet = client.open_by_key(SHEET_ID)

        frequency = {"expense": {}, "income": {}}

        for entry_type, ws_name in [("expense", WORKSHEET_EXPENSE), ("income", WORKSHEET_INCOME)]:
            ws = sheet.worksheet(ws_name)
            entries = _all_entries(ws.get_all_values())

            for entry in entries:
                category = entry.get("category", "").strip()
                if category:
                    frequency[entry_type][category] = frequency[entry_type].get(category, 0) + 1

        return frequency
    except Exception:
        return {"expense": {}, "income": {}}


def maybe_update_category_order():
    """
    Щоденно оновлює порядок категорій залежно від частоти використання.
    Якщо кеш відсутній або дата змінилась, перераховує частоту.
    """
    global CATEGORIES, _category_frequency_cache, _category_frequency_date

    today = date.today()

    if _category_frequency_date != today:
        frequency = count_category_frequency()
        _category_frequency_cache = frequency
        _category_frequency_date = today
    else:
        frequency = _category_frequency_cache or count_category_frequency()

    for entry_type in ["expense", "income"]:
        if entry_type not in CATEGORIES:
            continue

        current_cats = CATEGORIES[entry_type]
        cat_counts = frequency.get(entry_type, {})

        def sort_key(cat):
            count = cat_counts.get(cat, 0)
            current_index = current_cats.index(cat) if cat in current_cats else float('inf')
            return (-count, current_index)

        CATEGORIES[entry_type] = sorted(current_cats, key=sort_key)


@app.route("/categories/add", methods=["POST"])
@login_required
def add_category():
    entry_type = request.json.get("type")
    category_name = request.json.get("name", "").strip()

    if entry_type not in ("income", "expense"):
        return jsonify({"error": "Некоректний тип"}), 400
    if not category_name:
        return jsonify({"error": "Назва категорії не може бути порожною"}), 400
    if category_name in CATEGORIES[entry_type]:
        return jsonify({"error": "Така категорія вже існує"}), 409

    CATEGORIES[entry_type].append(category_name)
    save_categories()
    return jsonify({"success": True, "categories": CATEGORIES})


@app.route("/categories/delete", methods=["POST"])
@login_required
def delete_category():
    entry_type = request.json.get("type")
    category_name = request.json.get("name", "").strip()

    if entry_type not in ("income", "expense"):
        return jsonify({"error": "Некоректний тип"}), 400
    if category_name not in CATEGORIES[entry_type]:
        return jsonify({"error": "Категорія не знайдена"}), 404

    CATEGORIES[entry_type].remove(category_name)
    save_categories()
    return jsonify({"success": True, "categories": CATEGORIES})


@app.route("/categories/rename", methods=["POST"])
@login_required
def rename_category():
    entry_type = request.json.get("type")
    old_name = request.json.get("old_name", "").strip()
    new_name = request.json.get("new_name", "").strip()

    if entry_type not in ("income", "expense"):
        return jsonify({"error": "Некоректний тип"}), 400
    if not old_name or not new_name:
        return jsonify({"error": "Назви не можуть бути порожними"}), 400
    if old_name not in CATEGORIES[entry_type]:
        return jsonify({"error": "Стара категорія не знайдена"}), 404
    if new_name in CATEGORIES[entry_type]:
        return jsonify({"error": "Така категорія вже існує"}), 409

    idx = CATEGORIES[entry_type].index(old_name)
    CATEGORIES[entry_type][idx] = new_name
    save_categories()
    return jsonify({"success": True, "categories": CATEGORIES})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)