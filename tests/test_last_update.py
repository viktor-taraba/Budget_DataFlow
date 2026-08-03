"""
Тести дати/часу останнього коміта (востаннє змерженого PR), який показується
в футері сторінок у київському часі.

_format_commit_time() навмисно винесена окремо від get_last_update_time(),
яка викликає git підпроцесом — так само, як validate_amount/validate_date
тестуються без Flask, парсинг/конвертацію тут можна тестувати без git.
"""
import subprocess

import pytest

import app as app_module
from app import _format_commit_time, get_last_update_time


class TestFormatCommitTime:
    def test_formats_kyiv_offset_date_as_is(self):
        # Влітку Київ — UTC+3 (EEST), тому дата коміта з тим самим зміщенням
        # не повинна зсуватись.
        assert _format_commit_time("2026-08-02T15:30:00+03:00") == "02.08.2026 15:30"

    def test_converts_utc_to_kyiv_time(self):
        # 20:00 UTC влітку — це 23:00 в Києві (того самого дня).
        assert _format_commit_time("2026-08-02T20:00:00+00:00") == "02.08.2026 23:00"

    def test_converts_and_rolls_over_to_next_day(self):
        # 22:15 UTC влітку — це вже 01:15 наступного дня в Києві.
        assert _format_commit_time("2026-08-02T22:15:00+00:00") == "03.08.2026 01:15"

    def test_assumes_utc_for_naive_datetime(self):
        # git --format=%cI завжди пише зміщення, але про всяк випадок —
        # рядок без зони не повинен трактуватись як уже київський час.
        assert _format_commit_time("2026-08-02T20:00:00") == "02.08.2026 23:00"

    def test_returns_none_for_invalid_string(self):
        assert _format_commit_time("не дата") is None

    def test_returns_none_for_empty_string(self):
        assert _format_commit_time("") is None

    def test_returns_none_for_none(self):
        assert _format_commit_time(None) is None


class TestGetLastUpdateTime:
    def _reset_cache(self, monkeypatch):
        monkeypatch.setattr(app_module, "_last_update_cache", None)

    def test_reads_from_git_log(self, monkeypatch):
        self._reset_cache(monkeypatch)

        class FakeCompletedProcess:
            stdout = "2026-08-02T15:30:00+03:00\n"

        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return FakeCompletedProcess()

        monkeypatch.setattr(app_module.subprocess, "run", fake_run)

        assert get_last_update_time() == "02.08.2026 15:30"
        assert calls[0] == ["git", "log", "-1", "--format=%cI"]

    def test_caches_result_across_calls(self, monkeypatch):
        self._reset_cache(monkeypatch)
        calls = []

        class FakeCompletedProcess:
            stdout = "2026-08-02T15:30:00+03:00\n"

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return FakeCompletedProcess()

        monkeypatch.setattr(app_module.subprocess, "run", fake_run)

        get_last_update_time()
        get_last_update_time()

        assert len(calls) == 1

    def test_returns_none_when_git_is_unavailable(self, monkeypatch):
        self._reset_cache(monkeypatch)

        def fake_run(cmd, **kwargs):
            raise FileNotFoundError("git not found")

        monkeypatch.setattr(app_module.subprocess, "run", fake_run)

        assert get_last_update_time() is None

    def test_returns_none_when_git_command_fails(self, monkeypatch):
        self._reset_cache(monkeypatch)

        def fake_run(cmd, **kwargs):
            raise subprocess.CalledProcessError(128, cmd)

        monkeypatch.setattr(app_module.subprocess, "run", fake_run)

        assert get_last_update_time() is None

    def test_returns_none_when_timeout(self, monkeypatch):
        self._reset_cache(monkeypatch)

        def fake_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, 5)

        monkeypatch.setattr(app_module.subprocess, "run", fake_run)

        assert get_last_update_time() is None


class TestInjectLastUpdate:
    def test_returns_dict_with_last_update_key(self, monkeypatch):
        monkeypatch.setattr(app_module, "get_last_update_time", lambda: "02.08.2026 15:30")
        with app_module.app.test_request_context("/"):
            result = app_module.inject_last_update()
        assert result == {"last_update": "02.08.2026 15:30"}

    def test_returns_none_when_unavailable(self, monkeypatch):
        monkeypatch.setattr(app_module, "get_last_update_time", lambda: None)
        with app_module.app.test_request_context("/"):
            result = app_module.inject_last_update()
        assert result == {"last_update": None}


class TestLastUpdateInTemplates:
    def test_index_shows_last_update_when_available(self, logged_in_client, monkeypatch):
        monkeypatch.setattr(app_module, "get_recent_entries", lambda ws_name, limit=5: [])
        monkeypatch.setattr(app_module, "get_last_update_time", lambda: "02.08.2026 15:30")

        response = logged_in_client.get("/")

        assert "Оновлено: 02.08.2026 15:30".encode() in response.data

    def test_index_hides_block_when_unavailable(self, logged_in_client, monkeypatch):
        monkeypatch.setattr(app_module, "get_recent_entries", lambda ws_name, limit=5: [])
        monkeypatch.setattr(app_module, "get_last_update_time", lambda: None)

        response = logged_in_client.get("/")

        assert "Оновлено:".encode() not in response.data

    def test_currency_page_shows_last_update(self, logged_in_client, monkeypatch):
        monkeypatch.setattr(app_module, "get_last_update_time", lambda: "02.08.2026 15:30")

        response = logged_in_client.get("/currency")

        assert "Оновлено: 02.08.2026 15:30".encode() in response.data
