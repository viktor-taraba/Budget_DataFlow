import os
import json
import re
import subprocess
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from functools import partial, wraps
from zoneinfo import ZoneInfo
import requests
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.middleware.proxy_fix import ProxyFix
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials
from config import COLUMN_ORDER, DELETED_COLUMN_ORDER, WORKSHEET_EXPENSE, WORKSHEET_INCOME, WORKSHEET_DELETED, WORKSHEET_CATEGORIES

# Кешування частоти категорій (раз на день)
_category_frequency_cache = None
_category_frequency_date = None

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ["FLASK_SECRET_KEY"]
SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
APP_PASSWORD = os.environ["APP_PASSWORD"]

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

CATEGORIES_CELL = "A1"


def _normalize_categories(categories: dict) -> dict:
    categories.setdefault("expense", [])
    categories.setdefault("income", [])
    subcategories = categories.setdefault("subcategories", {})
    subcategories.setdefault("expense", {})
    subcategories.setdefault("income", {})
    return categories


def _categories_worksheet():
    """Аркуш 'Categories' у тій самій таблиці, що й Доходи/Витрати.
    Створюється автоматично при першому зверненні, якщо його ще немає."""
    client = get_client()
    sheet = client.open_by_key(SHEET_ID)
    try:
        return sheet.worksheet(WORKSHEET_CATEGORIES)
    except gspread.WorksheetNotFound:
        return sheet.add_worksheet(title=WORKSHEET_CATEGORIES, rows=1, cols=1)


def _load_categories_from_sheet() -> dict:
    ws = _categories_worksheet()
    raw = ws.acell(CATEGORIES_CELL).value
    if not raw:
        raise ValueError("Аркуш Categories порожній")
    return _normalize_categories(json.loads(raw))


def _write_categories_to_sheet(categories: dict) -> None:
    ws = _categories_worksheet()
    ws.update([[json.dumps(categories, ensure_ascii=False)]], CATEGORIES_CELL, value_input_option="RAW")


def _load_categories_from_file() -> dict:
    """Резервний варіант — локальна розробка без доступу до Sheets, або
    перший запуск до першої міграції."""
    with open("categories.json", "r", encoding="utf-8") as f:
        return _normalize_categories(json.load(f))


def load_categories() -> dict:
    """
    Категорії живуть в аркуші 'Categories' тієї ж Google-таблиці, що й
    доходи/витрати — так вони переживають кожен redeploy на Render, на
    відміну від categories.json, який пишеться лише у файлову систему
    контейнера і зникає з кожним новим деплоєм.

    Sheets недоступний чи ще порожній -> падаємо на локальний файл, той
    самий принцип "не ламати застосунок", що й get_recent_entries().
    """
    try:
        return _load_categories_from_sheet()
    except Exception:
        pass

    try:
        categories = _load_categories_from_file()
    except (FileNotFoundError, json.JSONDecodeError):
        return {"expense": [], "income": [], "subcategories": {"expense": {}, "income": {}}}

    # Одноразова міграція: локальний файл є, а в Sheets ще порожньо —
    # переносимо зараз, щоб наступний деплой уже читав із Sheets.
    try:
        _write_categories_to_sheet(categories)
    except Exception:
        pass

    return categories


CATEGORIES = load_categories()


def save_categories():
    """Джерело істини — аркуш 'Categories' (переживає redeploy).
    Локальний categories.json лишається як швидкий офлайн-резерв."""
    _write_categories_to_sheet(CATEGORIES)
    with open("categories.json", "w", encoding="utf-8") as f:
        json.dump(CATEGORIES, f, ensure_ascii=False, indent=2)

# Сервер (Render) працює в UTC, а користувач — у Києві
KYIV_TZ = ZoneInfo("Europe/Kyiv")
def today_kyiv() -> date:
    """Поточна дата за київським часом"""
    return datetime.now(KYIV_TZ).date()


# Дата й час останнього коміта (на практиці — останнього змерженого PR) у
# гілці, з якої задеплоєно застосунок. Обчислюється раз за час життя процесу:
# новий деплой на Render — це новий процес, тож "останній коміт" не може
# змінитися, поки застосунок працює, і повторний виклик git на кожен GET /
# був би зайвим.
_last_update_cache = None


def _format_commit_time(commit_date_iso: str):
    """
    Перетворює ISO-дату коміта (`git log --format=%cI`, напр.
    '2026-08-02T15:30:00+03:00') на рядок у київському часі.

    Винесено окремо від get_last_update_time(), яка викликає git, щоб
    парсинг/конвертацію можна було тестувати без підпроцесу — так само, як
    validate_amount/validate_date тестуються без Flask.
    """
    try:
        commit_dt = datetime.fromisoformat(commit_date_iso)
    except (ValueError, TypeError):
        return None

    if commit_dt.tzinfo is None:
        # git завжди пише зміщення для %cI, але про всяк випадок —
        # без зони вважаємо час UTC, а не наївно локальним.
        commit_dt = commit_dt.replace(tzinfo=timezone.utc)

    return commit_dt.astimezone(KYIV_TZ).strftime("%d.%m.%Y %H:%M")


def get_last_update_time():
    """
    Дата й час останнього коміта репозиторію у київському часі, або None,
    якщо git недоступний (наприклад, деплой без .git) чи стався збій.

    Ніколи не кидає виняток — відсутність цієї інформації не повинна
    ламати сторінку, так само як недоступність Google Sheets не ламає
    блок "Останні записи" в index().
    """
    global _last_update_cache
    if _last_update_cache is not None:
        return _last_update_cache

    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%cI"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        _last_update_cache = _format_commit_time(result.stdout.strip())
    except (subprocess.SubprocessError, OSError):
        _last_update_cache = None

    return _last_update_cache


def subcategories_for(entry_type: str, category: str) -> list:
    """Повертає підкатегорії, доступні для заданої категорії."""
    return CATEGORIES.get("subcategories", {}).get(entry_type, {}).get(category, [])


def subcategory_is_valid(entry_type: str, category: str, subcategory: str) -> bool:
    """Порожня підкатегорія допустима; непорожня має належати категорії."""
    return not subcategory or subcategory in subcategories_for(entry_type, category)

# Render (і більшість хостингів) стоїть за проксі: без цього request.remote_addr завжди буде адресою проксі, а не клієнта,
# і rate-limit нижче рахуватиме всіх користувачів як одного.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)
# Обмеження кількості спроб входу — захист від підбору пароля.
# Рахує спроби по реальному IP клієнта (див. ProxyFix вище); при перевищенні повертає 429 Too Many Requests.
limiter = Limiter(get_remote_address, app=app, default_limits=[])
# Кеш курсів НБУ {(currency, date_iso): rate}
_exchange_rates_cache = {}
# Скільки одночасних запитів до НБУ дозволяємо при пре-фетчі курсів.
EXCHANGE_RATE_MAX_WORKERS = 8


def get_exchange_rate(date_iso: str, currency: str = "USD") -> float:
    """
    Отримує курс `currency` (USD або EUR) до UAH від НБУ для конкретної дати.
    date_iso у форматі 'YYYY-MM-DD', наприклад '2025-09-01'
    Повертає курс (float) або None при помилці.
    Результати кешуються протягом сесії, окремо для кожної валюти.
    """
    cache_key = (currency, date_iso)
    if cache_key in _exchange_rates_cache:
        return _exchange_rates_cache[cache_key]

    try:
        date_obj = datetime.strptime(date_iso, "%Y-%m-%d").date()
        date_str = date_obj.strftime("%Y%m%d")

        url = f"https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange?date={date_str}&valcode={currency}&json"
        response = requests.get(url, timeout=5)
        data = response.json()

        if data and len(data) > 0:
            rate = float(data[0]["rate"])
            _exchange_rates_cache[cache_key] = rate
            return rate
    except (requests.RequestException, json.JSONDecodeError, KeyError, ValueError, IndexError, TypeError):
        pass

    return None


def get_latest_exchange_rate(currency: str):
    """
    Останній доступний курс НБУ для `currency` (USD/EUR) — для введення
    валютних операцій, а не для історичного графіка (див.
    get_exchange_rate_range нижче, який працює за період).

    Пробує сьогоднішню дату за київським часом; якщо НБУ ще не опублікував
    сьогоднішній курс (буває вранці) — пробує вчорашню. Якщо недоступний і
    вчорашній курс — повертає (None, None), і викликач сам вирішує, як це
    обробити (див. resolve_currency_amount).

    Повертає (rate, date_iso) або (None, None).
    """
    today_iso = today_kyiv().isoformat()
    rate = get_exchange_rate(today_iso, currency)
    if rate is not None:
        return rate, today_iso

    yesterday_iso = (today_kyiv() - timedelta(days=1)).isoformat()
    rate = get_exchange_rate(yesterday_iso, currency)
    if rate is not None:
        return rate, yesterday_iso

    return None, None


def resolve_currency_amount(currency: str, curr_amount: float):
    """
    Обчислює суму в гривнях (amount) для валютної операції (USD/EUR), за
    останнім доступним курсом НБУ (get_latest_exchange_rate).

    Якщо курс недоступний (НБУ не відповідає ні сьогодні, ні вчора) —
    операція все одно зберігається: курс приймається за 1, а amount = curr_amount
    (те саме числове значення в обох полях), і повертається текст попередження
    для користувача — той самий принцип "не блокувати запис, попередити", що й
    у add_emoji_if_missing.

    Повертає (amount: float, exchange_rate: float, warning: str | None).
    """
    rate, _ = get_latest_exchange_rate(currency)
    if rate is None:
        return curr_amount, 1.0, (
            "Курс НБУ недоступний — суму збережено без конвертації в гривні (курс 1)."
        )
    return round(curr_amount * rate, 2), rate, None


def get_exchange_rate_range(start_date_iso: str, end_date_iso: str, currency: str = "USD") -> dict:
    """
    Отримує курс `currency` до UAH від НБУ за весь період одним запитом
    (НБУ підтримує `start`/`end` для одного `valcode`, на відміну від
    get_exchange_rate(), який запитує по одній даті за раз).

    Повертає {date_iso: rate}. У відповіді НБУ можуть бути відсутні деякі
    дати (наприклад, вихідні) — це нормально, викликач сам вирішує, як
    заповнювати пропуски (див. _fill_rate_gaps).
    """
    try:
        start_str = datetime.strptime(start_date_iso, "%Y-%m-%d").strftime("%Y%m%d")
        end_str = datetime.strptime(end_date_iso, "%Y-%m-%d").strftime("%Y%m%d")
    except (ValueError, TypeError):
        return {}

    try:
        url = (
            "https://bank.gov.ua/NBU_Exchange/exchange_site"
            f"?start={start_str}&end={end_str}&valcode={currency}&sort=exchangedate&order=asc&json"
        )
        response = requests.get(url, timeout=10)
        data = response.json()
    except (requests.RequestException, json.JSONDecodeError):
        return {}

    rates = {}
    for item in data or []:
        try:
            raw_date = item["exchangedate"]
            rate = float(item["rate"])
        except (KeyError, TypeError, ValueError):
            continue
        try:
            date_iso = datetime.strptime(raw_date, "%d.%m.%Y").date().isoformat()
        except ValueError:
            continue
        rates[date_iso] = rate

    return rates


def _fill_rate_gaps(rates: dict, start_date: str, end_date: str) -> list:
    """
    Список {date, rate} для кожного дня періоду включно.

    НБУ не завжди публікує новий курс на кожен календарний день (вихідні),
    тому дні без власного значення успадковують останній відомий курс —
    так само, як _aggregate_stats заповнює нулями дні без операцій, тільки
    тут "порожній" день означає "курс не змінювався", а не "нуль".
    """
    series = []
    last_rate = None
    for d in _date_range(start_date, end_date):
        if d in rates:
            last_rate = rates[d]
        series.append({"date": d, "rate": last_rate})

    # Якщо перші дні періоду теж без курсу (наприклад, період починається
    # у вихідний), назад заповнюємо їх найпершим відомим значенням.
    first_known = next((point["rate"] for point in series if point["rate"] is not None), None)
    for point in series:
        if point["rate"] is None:
            point["rate"] = first_known
        else:
            break

    return series


def get_currency_period_analysis(currency: str, start_date: str, end_date: str) -> dict:
    """
    Курс `currency` за період: щоденний ряд (з заповненими пропусками) плюс
    підсумок — курс на початок/кінець періоду, зміна у відсотках і напрямок
    (гривня девальвувала чи ревальвувала).
    """
    rates = get_exchange_rate_range(start_date, end_date, currency)
    series = _fill_rate_gaps(rates, start_date, end_date)

    known_values = [point["rate"] for point in series if point["rate"] is not None]
    if not known_values:
        return {
            "currency": currency,
            "start": start_date,
            "end": end_date,
            "series": series,
            "start_rate": None,
            "end_rate": None,
            "change_percent": None,
            "direction": None,
        }

    start_rate = known_values[0]
    end_rate = known_values[-1]
    change_percent = round((end_rate - start_rate) / start_rate * 100, 2) if start_rate else None

    if change_percent is None or change_percent == 0:
        direction = "stable"
    elif change_percent > 0:
        # Курс іноземної валюти зріс — за неї платять більше гривень,
        # тобто гривня девальвувала (ослабла).
        direction = "devaluation"
    else:
        # Курс іноземної валюти впав — гривня ревальвувала (зміцніла).
        direction = "revaluation"

    return {
        "currency": currency,
        "start": start_date,
        "end": end_date,
        "series": series,
        "start_rate": start_rate,
        "end_rate": end_rate,
        "change_percent": change_percent,
        "direction": direction,
    }


def _prefetch_exchange_rates(dates, currency: str = "USD") -> None:
    """
    Заздалегідь підвантажує курси НБУ для набору дат — паралельно.

    get_exchange_rate сам кладе результат у _exchange_rates_cache, тому
    після виклику цієї функції наступні виклики get_exchange_rate для тих
    самих дат — просто читання з кешу.
    """
    missing = sorted({d for d in dates if (currency, d) not in _exchange_rates_cache})
    if not missing:
        return

    fetch = partial(get_exchange_rate, currency=currency)
    with ThreadPoolExecutor(max_workers=min(EXCHANGE_RATE_MAX_WORKERS, len(missing))) as executor:
        list(executor.map(fetch, missing))


def worksheet_for(entry_type: str) -> str:
    return WORKSHEET_INCOME if entry_type == "income" else WORKSHEET_EXPENSE


def _amount_for_sheet(value):
    """
    Готує суму до запису: кома як десятковий роздільник.

    Один хелпер для append_row і update_row — інакше дописані й відредаговані
    рядки лежать у таблиці в різних форматах ("100,0" проти "100.0"), і
    відбиток, знятий зі сторінки, перестає збігатися з рядком.
    """
    if isinstance(value, (int, float)):
        return str(value).replace(".", ",")
    return value


# Діапазон рядка для ws.update(): рівно стільки стовпців, скільки в COLUMN_ORDER.
_LAST_COLUMN = chr(ord("A") + len(COLUMN_ORDER) - 1)


def _write_row(ws, row_number: int, entry: dict):
    """Перезаписує рядок аркуша значеннями entry за порядком COLUMN_ORDER."""
    values = [entry.get(col, "") for col in COLUMN_ORDER]
    ws.update([values], f"A{row_number}:{_LAST_COLUMN}{row_number}", value_input_option="USER_ENTERED")


def _ensure_new_column_headers(ws, columns):
    """
    Дописує заголовки нових полів (кінець COLUMN_ORDER) лише до аркушів
    старої структури — раніше це стосувалось тільки 'subcategory', тепер
    може дописати кілька відсутніх заголовків одразу (напр. curr_amount,
    exchange_rate, currency), якщо аркуш ще старіший.
    """
    if not hasattr(ws, "update_cell"):
        return
    header = ws.row_values(1)
    for idx in range(len(header) + 1, len(columns) + 1):
        ws.update_cell(1, idx, columns[idx - 1])


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
    _ensure_new_column_headers(ws, COLUMN_ORDER)

    if split_info:
        # Розбиті операції завжди в гривні (див. CLAUDE.md) — currency
        # завжди "UAH", curr_amount/exchange_rate лишаються порожніми,
        # незалежно від того, що міг тримати перемикач валюти у формі.
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
            row_data["currency"] = "UAH"
            row_data["curr_amount"] = ""
            row_data["exchange_rate"] = ""

            row_data["amount"] = _amount_for_sheet(row_data["amount"])
            ws.append_row([row_data.get(col, "") for col in COLUMN_ORDER], value_input_option="USER_ENTERED")
    else:
        row_data = {**row, "amount": _amount_for_sheet(row.get("amount", ""))}
        row_data.setdefault("currency", "UAH")
        if row_data.get("curr_amount", "") != "":
            row_data["curr_amount"] = _amount_for_sheet(row_data["curr_amount"])
        if row_data.get("exchange_rate", "") != "":
            row_data["exchange_rate"] = _amount_for_sheet(row_data["exchange_rate"])
        ws.append_row([row_data.get(col, "") for col in COLUMN_ORDER], value_input_option="USER_ENTERED")


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
            # `entries` іде найновішими вперед, тому рядки розбивки прийшли сюди
            # в зворотному порядку. Повертаємо порядок аркуша: модалка
            # редагування показує категорії так само, як вони лежать у таблиці,
            # і update_row не перевертає їх місцями при кожному збереженні.
            split_entries = sorted(split_group["split_entries"], key=lambda e: e.get("row_number", 0))

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
MAX_STATS_RANGE_DAYS = 1825  # ~5 років


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


def _aggregate_stats_foreign(entries, start_date: str, end_date: str, currency: str) -> dict:
    """
    Агрегує записи в іноземній валюті (USD або EUR), конвертуючи кожний запис
    за курсом НБУ на його дату. Якщо курс недоступний для якої-небудь дати,
    записи за цією датою пропускаються.

    Курси на всі дати періоду підвантажуються заздалегідь одним паралельним
    пре-фетчем (_prefetch_exchange_rates) — інакше цей цикл сам робив би до
    366 послідовних мережевих запитів, по одному на кожну унікальну дату.
    """
    _prefetch_exchange_rates(
        {entry.get("date", "") for entry in entries if start_date <= entry.get("date", "") <= end_date},
        currency,
    )

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

        rate = get_exchange_rate(entry_date, currency)
        if rate is None:
            continue

        amount_foreign = amount / rate
        total += amount_foreign
        daily_totals[entry_date] = daily_totals.get(entry_date, 0.0) + amount_foreign
        category = entry.get("category") or "Інше"
        category_totals[category] = category_totals.get(category, 0.0) + amount_foreign

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


def get_period_stats(ws_name: str, start_date: str, end_date: str, currency: str = "UAH") -> dict:
    """Читає весь аркуш і агрегує його за _aggregate_stats для заданого періоду."""
    client = get_client()
    sheet = client.open_by_key(SHEET_ID)
    ws = sheet.worksheet(ws_name)
    entries = _all_entries(ws.get_all_values())

    if currency in ("USD", "EUR"):
        return _aggregate_stats_foreign(entries, start_date, end_date, currency)
    else:
        return _aggregate_stats(entries, start_date, end_date)


# Звіт електронною поштою — надсилається через Resend (https://resend.com),
# не Gmail SMTP: send-only API-ключ структурно не має доступу до жодної
# поштової скриньки (на відміну від Gmail App Password, який працює і для
# SMTP, і для IMAP) — тому нема чого читати, навіть якщо ключ витече.
#
# Два шляхи надсилання:
# 1. "Наздоганяючий" лист за вчора — при першому запиті кожного нового
#    процесу (справжній "рівно о 22:00" планувальник ненадійний на
#    безкоштовному Render: дайн засинає і не прокинеться по годиннику).
#    Стан "чи вже надіслано" зберігається у daily_report_state.json, щоб
#    повторний запит того самого процесу (чи наступний деплой) не дублював
#    лист; якщо відправка не вдалась, стан не пишеться, і наступний запит
#    спробує ще раз.
# 2. За вимогою користувача через кнопку 📧 в інтерфейсі — на довільний
#    період, без жодного стану.
RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
RESEND_FROM_EMAIL = os.environ.get("RESEND_FROM_EMAIL", "onboarding@resend.dev")
DAILY_REPORT_EMAIL = os.environ.get("DAILY_REPORT_EMAIL")

DAILY_REPORT_STATE_PATH = "daily_report_state.json"


def _load_daily_report_state() -> dict:
    try:
        with open(DAILY_REPORT_STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_daily_report_state(state: dict) -> None:
    with open(DAILY_REPORT_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _spans_at_most_n_calendar_months(start_date: str, end_date: str, n: int) -> bool:
    """
    Чи вкладається [start_date, end_date] у щонайбільше `n` календарних
    місяців (місяці рахуються як окремі "комірки" рік-місяць, які період
    зачіпає — напр. 20.01–05.02 це 2 місяці, а 01.01–31.03 це вже 3).

    Використовується, щоб вирішити, чи додавати повний список операцій до
    листа-звіту: за довший період список був би завеликий для листа.
    """
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    months_spanned = (end.year - start.year) * 12 + (end.month - start.month) + 1
    return months_spanned <= n


def get_period_summary(start_date: str, end_date: str) -> dict:
    """
    Підсумок операцій за період [start_date, end_date] включно, з обох
    аркушів: кількість операцій, сума доходів/витрат, чистий результат, суми
    по категоріях (окремо доходи/витрати) і повний список операцій.

    `include_transactions` каже, чи період достатньо короткий (щонайбільше 2
    календарних місяці — див. _spans_at_most_n_calendar_months), щоб додати
    список усіх операцій до листа; сам список рахується завжди (дешево —
    ті самі рядки, які й так уже прочитані для сум), рішення "показувати чи
    ні" — окремо, у render_report_text/html.
    """
    client = get_client()
    sheet = client.open_by_key(SHEET_ID)

    count, income_total, expense_total = 0, 0.0, 0.0
    category_totals = {"income": {}, "expense": {}}
    transactions = []

    for entry_type, ws_name in (("income", WORKSHEET_INCOME), ("expense", WORKSHEET_EXPENSE)):
        ws = sheet.worksheet(ws_name)
        for entry in _all_entries(ws.get_all_values()):
            entry_date = entry.get("date", "")
            if not (start_date <= entry_date <= end_date):
                continue
            amount = validate_amount(entry.get("amount"))
            if amount is None:
                continue

            count += 1
            category = entry.get("category") or "Інше"
            category_totals[entry_type][category] = category_totals[entry_type].get(category, 0.0) + amount
            transactions.append({
                "date": entry_date,
                "type": entry_type,
                "category": category,
                "amount": amount,
                "note": entry.get("note", ""),
            })
            if entry_type == "income":
                income_total += amount
            else:
                expense_total += amount

    categories = {
        kind: sorted(
            ({"category": c, "amount": round(a, 2)} for c, a in totals.items()),
            key=lambda item: item["amount"],
            reverse=True)
        for kind, totals in category_totals.items()}
    transactions.sort(key=lambda t: (t["date"], t["type"]))

    return {
        "start": start_date,
        "end": end_date,
        "count": count,
        "income": round(income_total, 2),
        "expense": round(expense_total, 2),
        "net": round(income_total - expense_total, 2),
        "categories": categories,
        "transactions": transactions,
        "include_transactions": _spans_at_most_n_calendar_months(start_date, end_date, 2),
    }


def _format_uah(value: float) -> str:
    return f"{value:,.2f}".replace(",", " ").replace(".", ",") + " ₴"


def _report_period_label(summary: dict) -> str:
    return summary["start"] if summary["start"] == summary["end"] else f"{summary['start']} – {summary['end']}"


def _format_category_line(category: str, amount: float, width: int = 22) -> str:
    pad_len = max(1, width - len(category))
    return f"  {category} {'.' * pad_len} {_format_uah(amount)}"


def _format_transaction_line(transaction: dict, width: int = 22) -> str:
    sign = "+" if transaction["type"] == "income" else "−"
    category = transaction["category"]
    pad_len = max(1, width - len(category))
    return f"  {transaction['date']}  {sign} {category} {'.' * pad_len} {_format_uah(transaction['amount'])}"


def _report_extra_sections(summary: dict) -> list:
    """
    Секції зі списком категорій і (для короткого періоду) усіх операцій —
    спільна логіка text/html версій листа. Якщо `summary` не несе
    "categories"/"transactions" (напр. переданий вручну в тестах), секції
    просто не додаються — базовий підсумковий блок лишається як раніше.
    """
    lines = []
    categories = summary.get("categories") or {}
    income_categories = categories.get("income") or []
    expense_categories = categories.get("expense") or []

    if income_categories:
        lines.append("")
        lines.append("  INCOME BY CATEGORY")
        lines.extend(_format_category_line(item["category"], item["amount"]) for item in income_categories)

    if expense_categories:
        lines.append("")
        lines.append("  EXPENSE BY CATEGORY")
        lines.extend(_format_category_line(item["category"], item["amount"]) for item in expense_categories)

    transactions = summary.get("transactions") or []
    if summary.get("include_transactions") and transactions:
        lines.append("")
        lines.append("  TRANSACTION LIST")
        lines.extend(_format_transaction_line(t) for t in transactions)

    return lines


def render_report_text(summary: dict) -> str:
    """Текстовий варіант (для клієнтів без HTML) у command-line стилі."""
    sign = "+" if summary["net"] >= 0 else "−"
    lines = [
        f"$ budget --report --period {_report_period_label(summary)}",
        "-" * 34,
        f"  TRANSACTIONS ... {summary['count']}",
        f"  INCOME ......... {_format_uah(summary['income'])}",
        f"  EXPENSE ........ {_format_uah(summary['expense'])}",
        f"  NET ............ {sign}{_format_uah(abs(summary['net']))}",
        "-" * 34]
    lines.extend(_report_extra_sections(summary))
    return "\n".join(lines)


def render_report_html(summary: dict) -> str:
    """HTML-версія в термінальному стилі: моноширинний шрифт, темне тло."""
    sign = "+" if summary["net"] >= 0 else "−"
    net_color = "#57B98B" if summary["net"] >= 0 else "#E0825E"
    lines = [
        f"$ budget --report --period {_report_period_label(summary)}",
        "-" * 34,
        f"  TRANSACTIONS ... {summary['count']}",
        f"  INCOME ......... {_format_uah(summary['income'])}",
        f"  EXPENSE ........ {_format_uah(summary['expense'])}",
        f"  NET ............ <span style=\"color:{net_color}\">{sign}{_format_uah(abs(summary['net']))}</span>",
        "-" * 34]
    lines.extend(_report_extra_sections(summary))
    body = "\n".join(lines)
    return (
        "<!DOCTYPE html><html><body style=\"margin:0;padding:24px;background:#0d1117;\">"
        "<pre style=\"font-family:'JetBrains Mono','Courier New',monospace;font-size:14px;"
        "line-height:1.5;color:#c9d1d9;background:#0d1117;margin:0;white-space:pre-wrap;\">"
        f"{body}</pre></body></html>"
    )


def send_report_email(start_date: str, end_date: str) -> bool:
    """
    Надсилає лист зі статистикою за [start_date, end_date] на
    DAILY_REPORT_EMAIL через Resend API
    (https://resend.com/docs/api-reference/emails/send-email).

    Повертає False без надсилання, якщо email/API-ключ не налаштовані —
    те саме "тихо вимкнено без ключа", що й OpenAI-інтеграція.
    Кидає виняток при мережевій помилці чи неуспішній відповіді API —
    викликач (maybe_send_yesterday_report / send_report_route) сам вирішує,
    як це обробити.
    """
    if not (DAILY_REPORT_EMAIL and RESEND_API_KEY):
        return False

    summary = get_period_summary(start_date, end_date)

    response = requests.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "from": RESEND_FROM_EMAIL,
            "to": [DAILY_REPORT_EMAIL],
            "subject": f"Бюджет · звіт за {_report_period_label(summary)}",
            "text": render_report_text(summary),
            "html": render_report_html(summary),
        },
        timeout=10,
    )
    response.raise_for_status()
    return True


# "Наздоганяючий" лист за вчора. _daily_report_confirmed_date кешує в пам'яті
# процесу дату, за яку вже точно підтверджено відправку, щоб не читати
# daily_report_state.json на кожен запит; _daily_report_lock захищає від
# гонки, якщо кілька запитів прийдуть одночасно одразу після старту процесу.
_daily_report_lock = threading.Lock()
_daily_report_confirmed_date = None


def maybe_send_yesterday_report():
    """
    Якщо звіт за вчорашній день (за Києвом) ще не надіслано — надсилає його
    зараз. Стан ("за яку дату востаннє надіслано") зберігається у
    daily_report_state.json, щоб повторний виклик (наступний запит, новий
    деплой) не дублював лист.

    Примітка: цей файл, як і categories.json, живе на диску процесу — на
    Render без persistent-диску він не переживає редеплой, тож зрідка після
    деплою можливий один зайвий лист. Це прийнятний компроміс для
    однокористувацького застосунку.
    """
    global _daily_report_confirmed_date

    if not (DAILY_REPORT_EMAIL and RESEND_API_KEY):
        return

    yesterday = (today_kyiv() - timedelta(days=1)).isoformat()
    if _daily_report_confirmed_date == yesterday:
        return  # вже підтверджено в цьому процесі — не читаємо файл повторно

    with _daily_report_lock:
        if _daily_report_confirmed_date == yesterday:
            return

        state = _load_daily_report_state()
        if state.get("last_sent_date") == yesterday:
            _daily_report_confirmed_date = yesterday
            return

        try:
            sent = send_report_email(yesterday, yesterday)
        except Exception:
            sent = False

        if sent:
            _save_daily_report_state({"last_sent_date": yesterday})
            _daily_report_confirmed_date = yesterday
        # Якщо не вдалось — нічого не зберігаємо й не кешуємо, наступний
        # запит (наприклад, коли дайн прокинеться знову) спробує ще раз.


@app.before_request
def _maybe_send_yesterday_report_hook():
    maybe_send_yesterday_report()


# Видалення записів
# Набір стовпців, за якими звіряємо, що видаляємо саме той рядок, який
# користувач бачив на сторінці. `added_at` ставить сервер, тому практично
# унікальний; решта — щоб спрацювало і для рядків, дописаних у таблицю вручну.
FINGERPRINT_COLUMNS = ("date", "category", "amount", "added_at")


def row_fingerprint(entry: dict) -> list:
    return [str(entry.get(col, "")) for col in FINGERPRINT_COLUMNS]


def _split_rows(ws, split_id: str) -> list:
    """Усі рядки розбивки за split_id, у порядку аркуша: [(row_number, entry), ...]."""
    return [
        (offset + 2, entry)
        for offset, entry in enumerate(_all_entries(ws.get_all_values()))
        if entry.get("split_id", "").strip() == split_id
    ]


def _split_fingerprint_matches(rows: list, expected_fingerprint: list) -> bool:
    """
    Звіряє відпечаток зі сторінки з рядками розбивки.

    Для розбивки номер рядка — ненадійний ідентифікатор: досить будь-якої
    вставки чи видалення вище в таблиці (у тому числі вручну), і рядок за цим
    номером — вже інший запис. А от split_id — UUID, який ніколи не вкаже на
    чужу операцію, тому рядки шукаємо саме за ним.

    Відпечаток лишається захистом «запис не змінився з моменту рендеру
    сторінки». Сторінка несе відпечаток одного конкретного рядка розбивки
    (див. recent_item у index.html), тому достатньо збігу з будь-яким рядком
    групи — інакше зсув нумерації назавжди блокував би видалення й
    редагування, скільки б сторінку не оновлювали.
    """
    expected = list(expected_fingerprint)
    return any(row_fingerprint(entry) == expected for _, entry in rows)


def delete_row(ws_name: str, row_number: int, expected_fingerprint: list, entry_type: str = None, split_id: str = None) -> bool:
    """
    Видаляє рядок(и) з аркуша, але лише якщо дані досі збігаються.

    Сторінка могла бути відкрита давно, а таблиця за цей час — змінитися
    (додали або видалили записи, і номери рядків зсунулись). Тому перед
    видаленням перечитуємо рядок(и) і звіряємо с тим, що був(и) на сторінці.
    Якщо не збігається — не видаляємо нічого і повертаємо False.

    Якщо split_id передано:
    - Знаходить усі рядки розбивки за split_id (row_number ігнорується — див.
      _split_fingerprint_matches), звіряє відпечаток з ними
    - Видаляє всі (розбивка видаляється як одна операція)
    - Архівує всі до листа DELETED

    Якщо split_id не передано:
    - Видаляє один рядок за row_number як раніше (назад сумісно)
    """
    if not any(expected_fingerprint):
        return False

    client = get_client()
    sheet = client.open_by_key(SHEET_ID)
    ws = sheet.worksheet(ws_name)

    if split_id:
        rows_to_delete = _split_rows(ws, split_id)
        if not rows_to_delete:
            return False
        if not _split_fingerprint_matches(rows_to_delete, expected_fingerprint):
            return False

        if entry_type:
            deleted_ws = sheet.worksheet(WORKSHEET_DELETED)
            _ensure_new_column_headers(deleted_ws, DELETED_COLUMN_ORDER)
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
            _ensure_new_column_headers(deleted_ws, DELETED_COLUMN_ORDER)
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
    - Знаходить усі рядки розбивки за split_id (row_number ігнорується — див.
      _split_fingerprint_matches), звіряє відпечаток з ними
    - Якщо split_breakdown також передано: переписує рядки під нову розбивку,
      видаляючи зайві й дописуючи нові, якщо кількість категорій змінилась
    - Якщо split_breakdown не передано: оновлює всі рядки розбивки переданими updates (date, note, ...)
    - Оновлює split_info якщо split_breakdown передано
    - updated_at встановлюється для всіх

    Якщо split_id не передано:
    - Оновлює один рядок за row_number як раніше (назад сумісно)

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
        rows_to_update = _split_rows(ws, split_id)
        if not rows_to_update:
            return False
        if not _split_fingerprint_matches(rows_to_update, expected_fingerprint):
            return False

        if split_breakdown:
            total_amount = sum(item["amount"] for item in split_breakdown)
            updates["split_info"] = json.dumps({
                "total": round(total_amount, 2),
                "categories": split_breakdown
            }, ensure_ascii=False)

        updates["updated_at"] = datetime.now(timezone.utc).isoformat()

        if not split_breakdown:
            for row_num, entry in rows_to_update:
                _write_row(ws, row_num, {**entry, **updates})
            return True

        # Кількість категорій могла змінитись: перші рядки переписуємо на місці,
        # зайві — видаляємо, а нові — дописуємо. Без цього рядки старої розбивки
        # лишались би в таблиці як «сироти» з тим самим split_id.
        for idx, (row_num, entry) in enumerate(rows_to_update[:len(split_breakdown)]):
            _write_row(ws, row_num, {
                **entry,
                **updates,
                "category": split_breakdown[idx]["category"],
                "subcategory": split_breakdown[idx].get("subcategory", ""),
                "amount": _amount_for_sheet(split_breakdown[idx]["amount"]),
            })

        surplus = rows_to_update[len(split_breakdown):]
        for row_num, _ in sorted(surplus, key=lambda item: item[0], reverse=True):
            ws.delete_rows(row_num)

        template = {**rows_to_update[0][1], **updates}
        for item in split_breakdown[len(rows_to_update):]:
            new_entry = {
                **template,
                "category": item["category"],
                "subcategory": item.get("subcategory", ""),
                "amount": _amount_for_sheet(item["amount"]),
            }
            ws.append_row([new_entry.get(col, "") for col in COLUMN_ORDER], value_input_option="USER_ENTERED")

        return True
    else:
        current = _row_to_entry(ws.row_values(row_number))
        if row_fingerprint(current) != list(expected_fingerprint):
            return False

        updates["updated_at"] = datetime.now(timezone.utc).isoformat()
        _write_row(ws, row_number, {**current, **updates})
        return True


# Валідація (винесена в окремі функції — щоб тестувати без Flask/Sheets)
_AMOUNT_PATTERN = re.compile(r"^\d+(\.\d+)?$")
_ROW_NUMBER_PATTERN = re.compile(r"^\d+$")

def validate_amount(raw):
    """
    Парсить і валідує суму з форми.

    Приймає кому як десятковий роздільник. Повертає float > 0, якщо рядок коректний, інакше None.
    Видаляє пробіли й неблокуючі пробіли (тисячні розділювачі).
    """
    if raw is None:
        return None
    cleaned = "".join(raw.replace(",", ".").split())
    if not _AMOUNT_PATTERN.match(cleaned):
        return None
    value = float(cleaned)
    return value if value > 0 else None


def validate_date(raw, max_date=None):
    """
    Валідує дату у форматі YYYY-MM-DD або Excel serial number.

    Приймає:
    - YYYY-MM-DD (ISO формат)
    - Excel serial number (число, де 1 = 1900-01-01)

    max_date (за замовчуванням — сьогодні за київським часом) визначає верхню межу: дати з майбутнього відхиляються.
    Повертає нормалізований ISO-рядок, якщо дата коректна, інакше None.
    """
    if not raw:
        return None

    parsed = None

    # Try ISO format first (YYYY-MM-DD)
    try:
        parsed = datetime.strptime(str(raw), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        pass

    # If ISO format failed, try Excel serial date
    if parsed is None:
        try:
            serial = float(raw)
            if serial < 0 or serial > 60000:  # Sanity check (covers 1900-2063)
                return None
            # Excel epoch is December 30, 1899
            excel_epoch = datetime(1899, 12, 30).date()
            parsed = excel_epoch + timedelta(days=serial)
        except (ValueError, TypeError):
            return None

    if max_date is None:
        max_date = today_kyiv()
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


def validate_split(category_amount_pairs, total_amount, entry_type: str = "expense"):
    """
    Валідує розбивку суми на кілька категорій.

    category_amount_pairs: list of dicts з category, optional subcategory та amount.
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
        subcategory = pair.get("subcategory", "").strip()
        amount_str = pair["amount"]

        if not category:
            return False, "Оберіть категорію для кожної частини", None
        if not subcategory_is_valid(entry_type, category, subcategory):
            return False, "Підкатегорія не належить обраній категорії", None

        # Якщо amount це число (з JSON), конвертуємо до рядка для validate_amount
        if isinstance(amount_str, (int, float)):
            amount_str = str(amount_str)

        validated = validate_amount(amount_str)
        if validated is None:
            return False, f"Невірна сума для категорії '{category}'", None
        split_rows.append({"category": category, "subcategory": subcategory, "amount": validated})
        sum_entered += validated

    if sum_entered >= total_amount:
        return False, "Сума розбивки не повинна перевищувати загальну суму", None

    remainder = round(total_amount - sum_entered, 2)
    if remainder < 0.01:
        return False, f"Остаток занадто малий ({remainder}). Перевірте суми.", None

    last_category = category_amount_pairs[-1]["category"]
    last_subcategory = category_amount_pairs[-1].get("subcategory", "").strip()
    if not last_category:
        return False, "Оберіть категорію для останньої частини", None
    if not subcategory_is_valid(entry_type, last_category, last_subcategory):
        return False, "Підкатегорія не належить обраній категорії", None
    split_rows.append({"category": last_category, "subcategory": last_subcategory, "amount": remainder})

    return True, None, split_rows


@app.context_processor
def inject_last_update():
    """Робить `last_update` (дата й час останнього коміта, Київ) доступним у кожному шаблоні без явної передачі в кожен render_template()."""
    return {"last_update": get_last_update_time()}


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
        today=today_kyiv().isoformat(),
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
    raw_amount = validate_amount(request.form.get("amount"))
    category = request.form.get("category", "").strip()
    subcategory = request.form.get("subcategory", "").strip()
    entry_date_raw = request.form.get("date") or today_kyiv().isoformat()
    entry_date = validate_date(entry_date_raw)
    note = request.form.get("note", "").strip()

    currency = request.form.get("currency", "UAH").strip().upper()
    if currency not in ("UAH", "USD", "EUR"):
        currency = "UAH"

    error = None
    if raw_amount is None:
        error = "Введіть коректну суму більше нуля"
    if entry_type not in ("income", "expense"):
        error = "Оберіть тип запису"
    if entry_date is None:
        error = "Некоректна дата"
    if error:
        flash(error)
        return redirect(url_for("index"))

    # Розбиті операції не підтримують валюту (див. CLAUDE.md) — перемикач
    # ігнорується для split-гілки нижче незалежно від того, що надіслала форма.
    currency_warning = None
    if currency == "UAH":
        amount = raw_amount
        curr_amount = None
        exchange_rate = None
    else:
        # У валютному режимі поле "amount" містить суму у введеній валюті,
        # а не в гривнях — гривневий еквівалент рахується тут за курсом.
        curr_amount = raw_amount
        amount, exchange_rate, currency_warning = resolve_currency_amount(currency, curr_amount)

    split_breakdown_json = request.form.get("split_breakdown")

    if split_breakdown_json and entry_type == "expense":
        # Розбивка завжди в гривні — незалежно від currency, беремо raw_amount
        # (те, що фактично ввів користувач), а не потенційно сконвертовану
        # валютну суму.
        try:
            split_breakdown = json.loads(split_breakdown_json)
            is_valid, error_msg, split_rows = validate_split(split_breakdown, raw_amount, entry_type)
            if not is_valid:
                flash(error_msg or "Помилка валідації розбивки")
                return redirect(url_for("index"))
        except (json.JSONDecodeError, ValueError) as exc:
            flash(f"Помилка формату розбивки: {exc}")
            return redirect(url_for("index"))

        row = {
            "date": entry_date,
            "category": "",
            "subcategory": "",
            "amount": raw_amount,
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
        elif not subcategory_is_valid(entry_type, category, subcategory):
            error = "Підкатегорія не належить обраній категорії"
        if error:
            flash(error)
            return redirect(url_for("index"))

        row = {
            "date": entry_date,
            "category": category,
            "subcategory": subcategory,
            "amount": amount,
            "note": note,
            "submitted_at": request.form.get("submitted_at", ""),
            "added_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "device_info": request.headers.get("User-Agent", "unknown"),
            "currency": currency,
        }
        if currency != "UAH":
            row["curr_amount"] = curr_amount
            row["exchange_rate"] = exchange_rate

        try:
            append_row(entry_type, row)
        except Exception as exc:
            flash(f"Помилка запису в таблицю: {exc}")
            return redirect(url_for("index"))

        success_message = "Запис додано"
        if currency_warning:
            success_message += ". " + currency_warning
        flash(success_message, "success")
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
            is_valid, error_msg, _ = validate_split(split_breakdown, total_amount, entry_type)
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
        raw_amount = validate_amount(request.form.get("amount"))
        category = request.form.get("category", "").strip()
        subcategory = request.form.get("subcategory", "").strip()

        currency = request.form.get("currency", "UAH").strip().upper()
        if currency not in ("UAH", "USD", "EUR"):
            currency = "UAH"

        if raw_amount is None:
            flash("Сума повинна бути числом")
            return redirect(url_for("index"))
        if not category:
            flash("Виберіть категорію")
            return redirect(url_for("index"))
        if not subcategory_is_valid(entry_type, category, subcategory):
            flash("Підкатегорія не належить обраній категорії")
            return redirect(url_for("index"))

        # Курс переобчислюється "зараз" (останній доступний), а не береться
        # зі старого запису — людина заново підтверджує суму на поточний момент.
        currency_warning = None
        if currency == "UAH":
            amount = raw_amount
            curr_amount = None
            exchange_rate = None
        else:
            curr_amount = raw_amount
            amount, exchange_rate, currency_warning = resolve_currency_amount(currency, curr_amount)

        updates = {
            "date": entry_date,
            "category": category,
            "subcategory": subcategory,
            "amount": _amount_for_sheet(amount),
            "note": note,
            "currency": currency,
        }
        if currency != "UAH":
            updates["curr_amount"] = _amount_for_sheet(curr_amount)
            updates["exchange_rate"] = _amount_for_sheet(exchange_rate)
        else:
            # Перемикання назад на UAH очищує валютні поля — інакше лишився б
            # "осиротілий" курс/сума від попереднього збереження в USD/EUR.
            updates["curr_amount"] = ""
            updates["exchange_rate"] = ""

        try:
            updated = update_row(worksheet_for(entry_type), row_number, expected_fingerprint, updates)
        except Exception as exc:
            flash(f"Помилка оновлення в таблиці: {exc}")
            return redirect(url_for("index"))

        if updated:
            success_message = "Запис оновлено"
            if currency_warning:
                success_message += ". " + currency_warning
            flash(success_message, "success")
        else:
            flash("Запис уже змінився або був видалений — оновіть сторінку")
        return redirect(url_for("index"))


@app.route("/stats", methods=["GET"])
@login_required
def stats():
    today = today_kyiv()
    default_start = (today - timedelta(days=29)).isoformat()  # 30 днів включно з сьогодні

    start_date = validate_date(request.args.get("start")) or default_start
    end_date = validate_date(request.args.get("end")) or today.isoformat()
    currency = request.args.get("currency", "UAH").upper()

    if currency not in ("UAH", "USD", "EUR"):
        currency = "UAH"

    if start_date > end_date:
        start_date, end_date = end_date, start_date

    span_days = (date.fromisoformat(end_date) - date.fromisoformat(start_date)).days
    warning = None
    if span_days > MAX_STATS_RANGE_DAYS:
        start_date = (date.fromisoformat(end_date) - timedelta(days=MAX_STATS_RANGE_DAYS)).isoformat()
        warning = f"Період обмежено до {MAX_STATS_RANGE_DAYS} днів (~5 років). Показується останніх {MAX_STATS_RANGE_DAYS} днів до {end_date}"

    try:
        expense = get_period_stats(WORKSHEET_EXPENSE, start_date, end_date, currency)
        income = get_period_stats(WORKSHEET_INCOME, start_date, end_date, currency)
    except Exception as exc:
        return jsonify({"error": f"Не вдалося завантажити статистику: {exc}"}), 502

    result = {
        "start": start_date,
        "end": end_date,
        "currency": currency,
        "expense": expense,
        "income": income,
        "difference": round(income["total"] - expense["total"], 2),
    }
    if warning:
        result["warning"] = warning

    return jsonify(result)


@app.route("/currency", methods=["GET"])
@login_required
def currency_page():
    """
    Окрема сторінка аналітики курсів валют (USD/EUR до UAH) — без прив'язки
    до доходів/витрат користувача. Відкривається з вікна "Статистика".
    """
    return render_template("currency.html", today=today_kyiv().isoformat())


@app.route("/currency/rates", methods=["GET"])
@login_required
def currency_rates():
    today = today_kyiv()
    default_start = (today - timedelta(days=29)).isoformat()  # 30 днів включно з сьогодні

    start_date = validate_date(request.args.get("start")) or default_start
    end_date = validate_date(request.args.get("end")) or today.isoformat()
    currency = request.args.get("currency", "USD").upper()

    if currency not in ("USD", "EUR"):
        currency = "USD"

    if start_date > end_date:
        start_date, end_date = end_date, start_date

    span_days = (date.fromisoformat(end_date) - date.fromisoformat(start_date)).days
    warning = None
    if span_days > MAX_STATS_RANGE_DAYS:
        start_date = (date.fromisoformat(end_date) - timedelta(days=MAX_STATS_RANGE_DAYS)).isoformat()
        warning = f"Період обмежено до {MAX_STATS_RANGE_DAYS} днів (~5 років). Показується останніх {MAX_STATS_RANGE_DAYS} днів до {end_date}"

    try:
        result = get_currency_period_analysis(currency, start_date, end_date)
    except Exception as exc:
        return jsonify({"error": f"Не вдалося завантажити курси валют: {exc}"}), 502

    if warning:
        result["warning"] = warning

    return jsonify(result)


@app.route("/reports/send", methods=["POST"])
@login_required
def send_report_route():
    """Надсилає звіт за обраний користувачем період негайно, на вимогу (кнопка 📧)."""
    start_date = validate_date(request.form.get("start"))
    end_date = validate_date(request.form.get("end") or request.form.get("start"))

    if start_date is None or end_date is None:
        flash("Некоректна дата звіту")
        return redirect(url_for("index"))

    if start_date > end_date:
        start_date, end_date = end_date, start_date

    if not (DAILY_REPORT_EMAIL and RESEND_API_KEY):
        flash("Розсилку звітів не налаштовано (немає RESEND_API_KEY/email у середовищі)")
        return redirect(url_for("index"))

    try:
        sent = send_report_email(start_date, end_date)
    except Exception as exc:
        flash(f"Помилка надсилання звіту: {exc}")
        return redirect(url_for("index"))

    flash("Звіт надіслано на пошту" if sent else "Не вдалося надіслати звіт", "success" if sent else None)
    return redirect(url_for("index"))


@app.route("/categories", methods=["GET"])
@login_required
def get_categories_endpoint():
    return jsonify(CATEGORIES)


# Автодобавлення emoji до категорій/підкатегорій без неї (через OpenAI)
# Ключ необов'язковий: без нього чи за будь-якого збою LLM просто не
# викликається, категорія зберігається як є, а користувач бачить попередження.
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_TIMEOUT_SECONDS = 8

_openai_client = None


def _import_openai_client_class():
    """Винесено окремо від get_openai_client(), щоб тести могли підмінити сам
    імпорт (і перевірити гілку «пакет не встановлений»), не деінсталюючи openai."""
    from openai import OpenAI
    return OpenAI


def get_openai_client():
    """
    Лінивий клієнт OpenAI — так само, як get_client() лінивий для Google
    Sheets. Повертає None, якщо ключ не налаштований або пакет `openai` не
    встановлений: обидва випадки для викликача means "LLM недоступний",
    а не помилка застосунку.
    """
    global _openai_client
    if _openai_client is not None:
        return _openai_client
    if not OPENAI_API_KEY:
        return None
    try:
        openai_client_class = _import_openai_client_class()
    except ImportError:
        return None
    _openai_client = openai_client_class(api_key=OPENAI_API_KEY, timeout=OPENAI_TIMEOUT_SECONDS)
    return _openai_client


# Основні unicode-блоки emoji (символи, емоційки, транспорт, прапори тощо) —
# використовується і для перевірки "чи вже є emoji", і для вирізання emoji з
# відповіді моделі, і для порівняння категорій на дублікат незалежно від emoji.
_EMOJI_PATTERN = re.compile(
    "["
    "\U0001F1E6-\U0001F1FF"  # прапори (regional indicators)
    "\U0001F300-\U0001F5FF"  # символи й піктограми
    "\U0001F600-\U0001F64F"  # емоційки
    "\U0001F680-\U0001F6FF"  # транспорт і карти
    "\U0001F700-\U0001F7FF"  # алхімічні символи, геометричні фігури
    "\U0001F800-\U0001F8FF"  # додаткові стрілки
    "\U0001F900-\U0001F9FF"  # додаткові символи й піктограми
    "\U0001FA00-\U0001FAFF"  # шахи, додаткові символи (напр. 🩹, 🪑)
    "\U00002600-\U000026FF"  # різні символи (☀️, ⚽, ⚔️...)
    "\U00002700-\U000027BF"  # дінгбати (✂️, ✈️...)
    "\U00002B00-\U00002BFF"  # стрілки/зірки (⭐...)
    "\U0000FE0F"             # variation selector (модифікатор emoji-стилю)
    "]+",
    flags=re.UNICODE,
)


def has_emoji(text: str) -> bool:
    """Чи містить рядок хоч один emoji-символ — якщо так, LLM не викликається."""
    return bool(_EMOJI_PATTERN.search(text or ""))


def _category_core_text(name: str) -> str:
    """
    Назва категорії без ведучих emoji й зайвих пробілів, у нижньому регістрі.

    Потрібна для перевірки на дублікат незалежно від emoji: інакше "Кіно" і
    щойно додане "🎬 Кіно" вважались би різними категоріями лише через те, що
    одну з них (не) вдалося доповнити emoji.
    """
    return _EMOJI_PATTERN.sub("", name or "").strip().casefold()


def suggest_emoji(name: str) -> str:
    """
    Питає OpenAI одну емодзі, яка найкраще пасує до назви категорії.

    Кидає виняток за будь-якого збою (немає клієнта, мережа, модель
    відповіла незрозуміло) — add_emoji_if_missing() ловить це самостійно.
    """
    client = get_openai_client()
    if client is None:
        raise RuntimeError("OpenAI недоступний: немає ключа або пакета `openai`")

    response = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {
                "role": "system",
                "content": (
                    "Користувач додає категорію витрат або доходів у застосунку "
                    "обліку бюджету. Підбери ОДНУ емодзі, яка найкраще її "
                    "ілюструє. Відповідай лише цим одним emoji-символом, без "
                    "жодного тексту, лапок чи пояснень."
                ),
            },
            {"role": "user", "content": name},
        ],
        max_tokens=10,
        timeout=OPENAI_TIMEOUT_SECONDS,
    )
    content = (response.choices[0].message.content or "").strip()
    match = _EMOJI_PATTERN.search(content)
    if not match:
        raise RuntimeError(f"Модель не повернула emoji: {content!r}")
    return match.group(0)


def add_emoji_if_missing(name: str):
    """
    Якщо `name` вже містить emoji — повертає (name, None) без звернень до LLM.
    Інакше просить OpenAI підібрати emoji і повертає ("{emoji} {name}", None).

    Якщо LLM недоступний чи стався будь-який збій — повертає (name, warning):
    `name` лишається без змін, а `warning` — текст для користувача. Категорія
    ЗАВЖДИ зберігається, навіть якщо emoji підібрати не вдалось.
    """
    name = (name or "").strip()
    if not name or has_emoji(name):
        return name, None

    try:
        emoji = suggest_emoji(name)
    except Exception:
        return name, "Не вдалося підібрати emoji автоматично (LLM недоступний) — категорію збережено без неї."

    return f"{emoji} {name}", None


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

    category_name, emoji_warning = add_emoji_if_missing(category_name)

    existing_core = {_category_core_text(c) for c in CATEGORIES[entry_type]}
    if _category_core_text(category_name) in existing_core:
        return jsonify({"error": "Така категорія вже існує"}), 409

    CATEGORIES[entry_type].append(category_name)
    save_categories()
    response = {"success": True, "categories": CATEGORIES}
    if emoji_warning:
        response["warning"] = emoji_warning
    return jsonify(response)


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
    CATEGORIES["subcategories"][entry_type].pop(category_name, None)
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

    new_name, emoji_warning = add_emoji_if_missing(new_name)

    existing_core = {_category_core_text(c) for c in CATEGORIES[entry_type] if c != old_name}
    if _category_core_text(new_name) in existing_core:
        return jsonify({"error": "Така категорія вже існує"}), 409

    idx = CATEGORIES[entry_type].index(old_name)
    CATEGORIES[entry_type][idx] = new_name
    children = CATEGORIES["subcategories"][entry_type].pop(old_name, None)
    if children is not None:
        CATEGORIES["subcategories"][entry_type][new_name] = children
    save_categories()
    response = {"success": True, "categories": CATEGORIES}
    if emoji_warning:
        response["warning"] = emoji_warning
    return jsonify(response)


def _subcategory_request_data():
    data = request.get_json(silent=True) or {}
    entry_type = data.get("type")
    category = str(data.get("category", "")).strip()
    name = str(data.get("name", "")).strip()
    return entry_type, category, name


@app.route("/subcategories/add", methods=["POST"])
@login_required
def add_subcategory():
    entry_type, category, name = _subcategory_request_data()
    if entry_type not in ("income", "expense") or category not in CATEGORIES[entry_type]:
        return jsonify({"error": "Некоректна категорія"}), 400
    if not name:
        return jsonify({"error": "Назва підкатегорії не може бути порожньою"}), 400

    name, emoji_warning = add_emoji_if_missing(name)

    all_subcategories = CATEGORIES["subcategories"][entry_type]
    existing_core = {
        _category_core_text(existing) for values in all_subcategories.values() for existing in values
    }
    if _category_core_text(name) in existing_core:
        return jsonify({"error": "Така підкатегорія вже належить іншій категорії"}), 409

    all_subcategories.setdefault(category, []).append(name)
    save_categories()
    response = {"success": True, "categories": CATEGORIES}
    if emoji_warning:
        response["warning"] = emoji_warning
    return jsonify(response)


@app.route("/subcategories/delete", methods=["POST"])
@login_required
def delete_subcategory():
    entry_type, category, name = _subcategory_request_data()
    children = CATEGORIES.get("subcategories", {}).get(entry_type, {}).get(category, [])
    if entry_type not in ("income", "expense") or name not in children:
        return jsonify({"error": "Підкатегорія не знайдена"}), 404

    children.remove(name)
    if not children:
        CATEGORIES["subcategories"][entry_type].pop(category, None)
    save_categories()
    return jsonify({"success": True, "categories": CATEGORIES})


@app.route("/subcategories/rename", methods=["POST"])
@login_required
def rename_subcategory():
    data = request.get_json(silent=True) or {}
    entry_type = data.get("type")
    category = str(data.get("category", "")).strip()
    old_name = str(data.get("old_name", "")).strip()
    new_name = str(data.get("new_name", "")).strip()
    children = CATEGORIES.get("subcategories", {}).get(entry_type, {}).get(category, [])

    if entry_type not in ("income", "expense") or old_name not in children:
        return jsonify({"error": "Підкатегорія не знайдена"}), 404
    if not new_name:
        return jsonify({"error": "Назва підкатегорії не може бути порожньою"}), 400

    new_name, emoji_warning = add_emoji_if_missing(new_name)

    existing_core = {
        _category_core_text(existing)
        for values in CATEGORIES["subcategories"][entry_type].values()
        for existing in values
        if existing != old_name
    }
    if _category_core_text(new_name) in existing_core:
        return jsonify({"error": "Така підкатегорія вже належить іншій категорії"}), 409

    children[children.index(old_name)] = new_name
    save_categories()
    response = {"success": True, "categories": CATEGORIES}
    if emoji_warning:
        response["warning"] = emoji_warning
    return jsonify(response)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
