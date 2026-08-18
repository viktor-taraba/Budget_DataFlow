"""
AI-поради по бюджету: короткий підсумок і практичні поради від OpenAI на
основі агрегованих сум доходів/витрат по категоріях за обраний період.

Винесено в окремий модуль (а не в app.py), бо це самостійна функціональність,
яка викликається лише за явним запитом користувача (кнопка "🧠 Поради"), а не
на кожен GET /, як інші агрегації в app.py. Ключ і модель ті самі, що й для
підбору emoji категорій (OPENAI_API_KEY/OPENAI_MODEL з app.py) — окремого
ключа тут не заводимо, api_key передається викликачем.
"""
import os

OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_TIMEOUT_SECONDS = 20

_openai_client = None


def _import_openai_client_class():
    """Винесено окремо, щоб тести могли підмінити сам імпорт (перевірити
    гілку «пакет не встановлений»), не деінсталюючи `openai` — той самий
    прийом, що й app._import_openai_client_class()."""
    from openai import OpenAI
    return OpenAI


def get_openai_client(api_key: str):
    """
    Лінивий клієнт OpenAI. Повертає None, якщо ключ не переданий або пакет
    `openai` не встановлений — обидва випадки для викликача означають
    "AI недоступний", а не помилку застосунку. Клієнт кешується на весь час
    життя процесу, той самий підхід, що й у app.get_openai_client().
    """
    global _openai_client
    if _openai_client is not None:
        return _openai_client
    if not api_key:
        return None
    try:
        client_class = _import_openai_client_class()
    except ImportError:
        return None
    _openai_client = client_class(api_key=api_key, timeout=OPENAI_TIMEOUT_SECONDS)
    return _openai_client


def _report_period_label(summary: dict) -> str:
    return summary["start"] if summary["start"] == summary["end"] else f"{summary['start']} – {summary['end']}"


def _format_category_lines(categories: list) -> str:
    if not categories:
        return "  (немає даних)"
    return "\n".join(f"  - {item['category']}: {item['amount']:.2f} ₴" for item in categories)


def build_prompt(summary: dict) -> str:
    """
    Формує текстовий запит для LLM з агрегованих даних періоду.

    summary: {"start", "end", "income": [{"category","amount"}, ...],
    "expense": [...], "income_total", "expense_total"} — та сама форма, яку
    повертає app.get_budget_insights_summary().
    """
    return (
        f"Період: {_report_period_label(summary)}\n"
        f"Загальний дохід: {summary['income_total']:.2f} ₴\n"
        f"Загальні витрати: {summary['expense_total']:.2f} ₴\n\n"
        f"Доходи по категоріях:\n{_format_category_lines(summary['income'])}\n\n"
        f"Витрати по категоріях:\n{_format_category_lines(summary['expense'])}\n"
    )


# System prompt is in English; the model is still told to answer in Ukrainian, since the app's UI, flashes,
# and emails are all Ukrainian-facing (see CLAUDE.md language convention) 
SYSTEM_PROMPT = (
    "You are a financial assistant for a personal budgeting app. The user "
    "sends you income and expense totals for a period, broken down by "
    "category. Give a short summary (2-4 sentences) in Ukrainian: what "
    "stands out (the biggest expense categories, the income-to-expense "
    "ratio), followed by 2-3 concrete, practical tips for saving money or "
    "optimizing the budget. Be concise, skip filler, and don't include "
    "greetings or unnecessary intro phrases."
)


def generate_budget_insights(summary: dict, api_key: str) -> str:
    """
    Питає OpenAI короткий підсумок + поради за агрегованими даними періоду.
    """
    client = get_openai_client(api_key)
    if client is None:
        raise RuntimeError("OpenAI недоступний: немає ключа або пакета `openai`")

    response = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_prompt(summary)},
        ],
        max_tokens=500,
        timeout=OPENAI_TIMEOUT_SECONDS,
    )
    content = (response.choices[0].message.content or "").strip()
    if not content:
        raise RuntimeError("Модель повернула порожню відповідь")
    return content
