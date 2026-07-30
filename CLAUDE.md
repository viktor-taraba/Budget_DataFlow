# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-user Flask app for entering income/expense records from a phone. Each submission is appended as a row to a Google Sheets spreadsheet ("Доходи" / "Витрати" worksheets) — there is no database. Deployed on Render's free plan via gunicorn.

The UI, comments, commit messages, and docs are in Ukrainian. Match that language when writing user-facing strings, comments, and docstrings.

## Commands

Package/venv management is `uv` (Python 3.13, `uv.lock` is committed).

```powershell
uv sync                              # install deps into .venv
uv run python app.py                 # local dev server on :5000
uv run pytest                        # all tests
uv run pytest -v                     # verbose
uv run pytest tests/test_validation.py::TestValidateAmount::test_accepts_valid_amounts
uv run pytest -k validate_date       # by name
```

Render build/start commands: `pip install uv && uv sync --frozen --no-dev` / `uv run gunicorn app:app`.

## Pre-commit hook

`hooks/pre-commit` runs the full pytest suite and aborts the commit on failure. It is enabled via `git config core.hooksPath hooks` (already set in this clone; re-run after a fresh clone). Tests must pass before any commit — don't bypass with `--no-verify`.

## Architecture

Everything lives in a handful of root-level files:

- `app.py` — the whole application: env loading, Google Sheets client, validation helpers, auth, and all seven routes (`/login`, `/logout`, `/`, `/submit`, `/delete`, `/edit`, `/stats`).
- `config.py` — `CATEGORIES` (expense/income lists with emoji prefixes), `COLUMN_ORDER`, and the two worksheet names. Edit categories here, not in `app.py`.
- `templates/index.html` — the form, the "Останні записи" block, the edit modal, and the "Статистика" modal. All client-side JS (category switching, pre-submit validation, theme toggle, delete confirmation, edit modal, stats fetch/render) is inline in this file, in one `<script>` block per feature. `templates/login.html` is the password gate.
- `static/style.css` — CSS custom properties themed by `data-theme` on `<html>`; theme is chosen by an inline script before paint to avoid a flash, persisted in `localStorage`.
- `main.py` and `test.py` are scratch/leftover files, not part of the app.

Key structural points:

- **Sheets write path**: `append_row(entry_type, row)` maps the row dict through `COLUMN_ORDER` into a positional list, so the sheet's column order must match `COLUMN_ORDER`. The gspread client is lazily created and cached in the module-level `_gs_client`.
- **Sheets read path**: `get_recent_entries(ws_name, limit=5)` does a full `get_all_values()` on every `GET /` and delegates to the pure `_rows_to_entries(all_values, limit)`. Failures are caught in `index()` so the form still renders with `recent_error` set — never let a Sheets outage break the page.
- **Sheets delete path**: sheet row numbers shift after every deletion, so a row number alone is not a safe identifier for a page that may have been open for hours. Each rendered entry carries `row_number` plus a fingerprint (`FINGERPRINT_COLUMNS`: date, category, amount, added_at) as hidden form fields; `delete_row` re-reads the row and deletes only if the fingerprint still matches, returning `False` otherwise so the user is told to reload. Preserve that read-then-compare guard when touching this code — dropping it silently deletes the wrong record.
- **Sheets edit path**: uses the same fingerprint guard as delete — `update_row` re-reads the row, compares fingerprint, and only updates if it matches. Editable fields: date, category, amount, note. System fields (submitted_at, added_at, device_info) are never modified by `update_row`. The per-entry "📝" button is rendered directly inside the `recent_item` Jinja macro (`data-edit-*` attributes carry the entry's values and fingerprint) — don't move it to a JS-side DOM-injection pass, that was tried and is fragile (easy to get the dataset-key mapping wrong and silently render no button).
- **Split expenses** (`use_split` checkbox): allow splitting one entry across multiple categories. When submitted, `append_row` creates multiple sheet rows (one per category), all with the same `split_id` (UUID). The grouped display (`_group_split_entries`) merges these back into a virtual single entry for the UI: categories are concatenated with " + ", `split_total_amount` is calculated from all rows, and `split_entries` contains the original rows. When editing a split, the modal switches to a split-editing interface (similar to the submit form): user can add/remove categories, adjust amounts, and the last amount auto-calculates as remainder from the total in the (still visible) amount field. Editable fields: date, note, category/amount breakdown. `update_row(split_id=..., split_breakdown=...)` rewrites the group atomically and **adds/removes sheet rows when the category count changes** — skipping that leaves orphaned rows carrying the old split_id. Deletes remove all rows in the split atomically via `delete_row(split_id=...)`, archiving all to the DELETED sheet.
  - **Split identity is `split_id`, not `row_number`.** For the split paths, `_split_rows()` locates rows by the UUID and `_split_fingerprint_matches()` accepts a match against *any* row of the group; `row_number` is ignored. A UUID can never point at somebody else's record, so the row-shift hazard the fingerprint exists to guard against doesn't apply — whereas re-reading `row_values(row_number)` positionally made every split delete/edit fail with "Запис уже змінився" as soon as anything shifted the numbering (including a manual edit in the sheet). Don't reintroduce a positional re-read here. The single-row paths still use `row_number` + strict fingerprint, and that's correct for them.
  - The edit modal hides the category field for splits. It is `required`, so the JS must also `disabled = true` it — a `required` control that is `display:none` makes the browser refuse to submit *and* fail to show a message, so "Зберегти" silently does nothing. `closeEdit()` has to re-enable it, since `form.reset()` doesn't touch `disabled`.
- **Stats path**: `GET /stats` is a JSON endpoint (not a page), fetched lazily from the modal's JS only when it's opened — it's a second full `get_all_values()` per worksheet (via `get_period_stats` → `_all_entries` → `_aggregate_stats`), so it deliberately isn't loaded on every `GET /` the way recent entries are. `_aggregate_stats` fills every date in the range with `0` (via `_date_range`) even where no entries exist, so the daily chart never has gaps. Malformed amounts (manual sheet edits) are skipped via `validate_amount`, same as elsewhere. The range is clamped to `MAX_STATS_RANGE_DAYS` (366) server-side regardless of what the client requests.
- **Validation is deliberately Flask-free**: `validate_amount` and `validate_date` are plain functions taking raw strings, so `tests/test_validation.py` can test them without app context or network. Keep new validation logic in the same shape. Client-side validation in `index.html` mirrors these rules but is not a substitute for them.
- **Auth** is a single shared password (`APP_PASSWORD`) checked against `session["authed"]` by the `@login_required` decorator. No user accounts.
- **Rate limiting**: `flask-limiter` caps `POST /login` at 5/min; `ProxyFix(x_for=1)` is required for the limiter to see real client IPs behind Render's proxy. The 429 handler re-renders the login page with a flash. Storage is in-memory (known limitation, see ROADMAP).

## Environment

Required env vars (see `.env.example`), read at import time — `app.py` raises `KeyError` without them: `FLASK_SECRET_KEY`, `APP_PASSWORD`, `GOOGLE_SHEET_ID`, `GOOGLE_CREDENTIALS_JSON` (the service-account JSON key, on one line).

`tests/conftest.py` stubs out `dotenv.load_dotenv` and sets fake values for all four *before* importing `app`. Any new module-level env read in `app.py` must be added there too or the whole suite fails to collect.

Two test-harness details worth knowing:

- The rate limiter is disabled via `limiter.enabled = False`, **not** `config["RATELIMIT_ENABLED"]` — flask-limiter reads that config key once during `init_app()`, which happens at `app.py` import time, so setting it from a fixture is too late. Getting this wrong is quiet: the suite passes until it accumulates more than 5 `POST /login` calls per minute, then unrelated tests start failing with 429.
- `logged_in_client` sets `session["authed"]` directly through `session_transaction()` rather than posting the login form, so tests neither depend on the login flow nor consume the login rate limit.
- Tests that need Sheets monkeypatch `app.get_client` with a fake client (see `tests/test_delete.py`, `tests/test_edit.py`, `tests/test_stats.py`) or replace `app.get_recent_entries`/`app.delete_row`/`app.update_row`/`app.get_period_stats` outright. Nothing in the suite touches the network — never start the real dev server or hit the live Google Sheet to verify a change; the fake-client pattern is the verification path, and it also avoids ever needing the real `.env` secrets outside of actual local/Render runs.

## Docs to keep current

- `CHANGELOG.md` — dated bullet list of shipped changes (`[DD.MM.YYYY]`); add an entry when shipping a user-visible change.
- `ROADMAP.md` — `## Planned` / `## Ideas`; move or remove items as they are implemented.
- `README_test.md` is the real, detailed setup/deploy guide (Google service account, Render). `README.md` is currently just a stub about installing the git hook.
