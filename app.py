import os
import json
import re
from datetime import date, datetime, timezone
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, flash
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.middleware.proxy_fix import ProxyFix
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials
from config import CATEGORIES, COLUMN_ORDER, WORKSHEET_EXPENSE, WORKSHEET_INCOME

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ["FLASK_SECRET_KEY"]
SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
APP_PASSWORD = os.environ["APP_PASSWORD"]

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


def append_row(entry_type: str, row: dict):
    client = get_client()
    sheet = client.open_by_key(SHEET_ID)
    ws = sheet.worksheet(worksheet_for(entry_type))
    values = [row.get(col, "") for col in COLUMN_ORDER]
    ws.append_row(values, value_input_option="USER_ENTERED")


def _row_to_entry(row) -> dict:
    """Розкладає сирий рядок аркуша по назвах стовпців з COLUMN_ORDER."""
    return {col: (row[i] if i < len(row) else "") for i, col in enumerate(COLUMN_ORDER)}


def _rows_to_entries(all_values, limit: int):
    """
    Перетворює сирі значення аркуша (разом із рядком заголовків) на останні
    `limit` записів, найновіші першими.

    Крім значень стовпців, кожен запис містить `row_number` — номер рядка в
    аркуші (заголовок — рядок 1, дані починаються з 2). Він потрібен, щоб
    видалити саме цей рядок через delete_rows().
    """
    if len(all_values) <= 1:
        return []

    data_rows = all_values[1:]
    start = max(0, len(data_rows) - limit)

    entries = []
    for offset, row in enumerate(data_rows[start:], start=start):
        entry = _row_to_entry(row)
        entry["row_number"] = offset + 2
        entries.append(entry)

    entries.reverse()
    return entries


def get_recent_entries(ws_name: str, limit: int = 5):
    """
    Повертає останні `limit` записів з аркуша, найновіші першими.

    Читає лише вже записані рядки (без запису), тому безпечно викликати
    при кожному відкритті головної сторінки.
    """
    client = get_client()
    sheet = client.open_by_key(SHEET_ID)
    ws = sheet.worksheet(ws_name)
    return _rows_to_entries(ws.get_all_values(), limit)


# Видалення записів
# Набір стовпців, за якими звіряємо, що видаляємо саме той рядок, який
# користувач бачив на сторінці. `added_at` ставить сервер, тому практично
# унікальний; решта — щоб спрацювало і для рядків, дописаних у таблицю вручну.
FINGERPRINT_COLUMNS = ("date", "category", "amount", "added_at")


def row_fingerprint(entry: dict) -> list:
    return [str(entry.get(col, "")) for col in FINGERPRINT_COLUMNS]


def delete_row(ws_name: str, row_number: int, expected_fingerprint: list) -> bool:
    """
    Видаляє рядок з аркуша, але лише якщо він досі містить ті самі дані.

    Сторінка могла бути відкрита давно, а таблиця за цей час — змінитися
    (додали або видалили записи, і номери рядків зсунулись). Тому перед
    видаленням перечитуємо рядок і звіряємо його з тим, що був на сторінці.
    Якщо не збігається — не видаляємо нічого і повертаємо False, щоб не
    втратити чужий запис.
    """
    if not any(expected_fingerprint):
        return False

    client = get_client()
    sheet = client.open_by_key(SHEET_ID)
    ws = sheet.worksheet(ws_name)

    current = _row_to_entry(ws.row_values(row_number))
    if row_fingerprint(current) != list(expected_fingerprint):
        return False

    ws.delete_rows(row_number)
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
    if not category:
        error = "Оберіть категорію"
    if entry_date is None:
        error = "Некоректна дата"
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

    if entry_type not in ("income", "expense"):
        flash("Некоректний тип запису")
        return redirect(url_for("index"))
    if row_number is None:
        flash("Некоректний запис для видалення")
        return redirect(url_for("index"))

    try:
        deleted = delete_row(worksheet_for(entry_type), row_number, expected_fingerprint)
    except Exception as exc:
        flash(f"Помилка видалення з таблиці: {exc}")
        return redirect(url_for("index"))

    if deleted:
        flash("Запис видалено", "success")
    else:
        flash("Запис уже змінився або був видалений — оновіть сторінку")
    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)