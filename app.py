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


def append_row(entry_type: str, row: dict):
    client = get_client()
    sheet = client.open_by_key(SHEET_ID)
    ws_name = WORKSHEET_INCOME if entry_type == "income" else WORKSHEET_EXPENSE
    ws = sheet.worksheet(ws_name)
    values = [row.get(col, "") for col in COLUMN_ORDER]
    ws.append_row(values, value_input_option="USER_ENTERED")


# Валідація (винесена в окремі функції — щоб тестувати без Flask/Sheets)
_AMOUNT_PATTERN = re.compile(r"^\d+(\.\d+)?$")

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
    return render_template(
        "index.html",
        today=date.today().isoformat(),
        categories=CATEGORIES,
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)