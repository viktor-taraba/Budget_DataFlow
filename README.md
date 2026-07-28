# Budget_DataFlow

Мінімальний Flask-застосунок для швидкого внесення доходів/витрат з телефона.
Запис одразу дописується у Google-таблицю (аркуші "Доходи" / "Витрати"),
власної бази даних немає. Розгортається на Render.

## Швидкий старт

```powershell
uv sync
copy .env.example .env   # заповнити своїми значеннями
uv run python app.py
```

Відкрити `http://localhost:5000`.

Детальний покроковий гайд (сервісний акаунт Google, доступ до таблиці,
деплой на Render) — у [README_test.md](README_test.md).

## Тести

```powershell
uv run pytest
```

## Git-хук перед комітом

Хук `hooks/pre-commit` прогонює тести й скасовує коміт, якщо вони не
проходять. Увімкнути (одноразово після клонування):

```powershell
git config core.hooksPath hooks
git config core.hooksPath   # має вивести: hooks
```

## Документація

- [CHANGELOG.md](CHANGELOG.md) — що зроблено
- [ROADMAP.md](ROADMAP.md) — що заплановано
- [CLAUDE.md](CLAUDE.md) — архітектура проєкту (для AI-асистентів)
