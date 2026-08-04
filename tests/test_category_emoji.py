"""
Тести автодобавлення emoji до категорій/підкатегорій через OpenAI.

Розклад той самий, що й у решті проєкту: pure-функції (has_emoji,
_category_core_text, add_emoji_if_missing) тестуються без мережі; get_openai_client()
і suggest_emoji() — з підміненим клієнтом/імпортом; маршрути — через
logged_in_client з підміненим suggest_emoji, без реального API OpenAI.

Global CATEGORIES та save_categories() (запис у categories.json) ізолюються
автоматично для кожного тесту (див. _isolated_categories нижче) — інакше
маршрутні тести писали б у реальний файл на диску.
"""
import copy

import pytest

import app as app_module
from app import (
    _category_core_text,
    add_emoji_if_missing,
    get_openai_client,
    has_emoji,
    suggest_emoji,
)


@pytest.fixture(autouse=True)
def _isolated_categories(monkeypatch):
    """
    Маршрути /categories/* і /subcategories/* мутують глобальний CATEGORIES і
    викликають save_categories() (запис у categories.json). Підміняємо запис
    на no-op і відновлюємо CATEGORIES після кожного тесту, щоб тести не
    лишали слідів ні в пам'яті процесу, ні на диску.
    """
    original = copy.deepcopy(app_module.CATEGORIES)
    monkeypatch.setattr(app_module, "save_categories", lambda: None)
    yield
    app_module.CATEGORIES.clear()
    app_module.CATEGORIES.update(copy.deepcopy(original))


@pytest.fixture(autouse=True)
def _reset_openai_client(monkeypatch):
    """Кеш клієнта OpenAI — глобальний; скидаємо його для кожного тесту окремо від CATEGORIES."""
    monkeypatch.setattr(app_module, "_openai_client", None)


class FakeMessage:
    def __init__(self, content):
        self.content = content


class FakeChoice:
    def __init__(self, content):
        self.message = FakeMessage(content)


class FakeCompletionResponse:
    def __init__(self, content):
        self.choices = [FakeChoice(content)]


class FakeCompletions:
    def __init__(self, content=None, error=None):
        self._content = content
        self._error = error
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._error:
            raise self._error
        return FakeCompletionResponse(self._content)


class FakeChat:
    def __init__(self, completions):
        self.completions = completions


class FakeOpenAIClient:
    def __init__(self, content=None, error=None):
        self.completions = FakeCompletions(content=content, error=error)
        self.chat = FakeChat(self.completions)


class TestHasEmoji:
    @pytest.mark.parametrize(
        "text",
        [
            "🛒 Продукти",
            "💻 Техніка",
            "✂️ Перукарня",  # dingbat + variation selector
            "⚽ Спорт",
            "📜 ОВДП",
        ],
    )
    def test_detects_existing_category_emoji(self, text):
        assert has_emoji(text) is True

    @pytest.mark.parametrize("text", ["Кіно", "Продукти", "  Таксі  ", "123"])
    def test_plain_text_has_no_emoji(self, text):
        assert has_emoji(text) is False

    def test_empty_and_none_have_no_emoji(self):
        assert has_emoji("") is False
        assert has_emoji(None) is False


class TestCategoryCoreText:
    def test_strips_leading_emoji_and_lowercases(self):
        assert _category_core_text("🛒 Продукти") == "продукти"

    def test_plain_text_is_just_lowercased(self):
        assert _category_core_text("Продукти") == "продукти"

    def test_strips_surrounding_whitespace(self):
        assert _category_core_text("  Кіно  ") == "кіно"

    def test_empty_string(self):
        assert _category_core_text("") == ""
        assert _category_core_text(None) == ""


class TestGetOpenaiClient:
    def test_returns_none_without_api_key(self, monkeypatch):
        monkeypatch.setattr(app_module, "OPENAI_API_KEY", None)
        assert get_openai_client() is None

    def test_returns_none_when_package_not_importable(self, monkeypatch):
        monkeypatch.setattr(app_module, "OPENAI_API_KEY", "sk-fake")

        def fail_import():
            raise ImportError("no module named openai")

        monkeypatch.setattr(app_module, "_import_openai_client_class", fail_import)
        assert get_openai_client() is None

    def test_constructs_client_with_key_and_timeout(self, monkeypatch):
        monkeypatch.setattr(app_module, "OPENAI_API_KEY", "sk-fake")
        captured = {}

        class FakeClientClass:
            def __init__(self, api_key, timeout):
                captured["api_key"] = api_key
                captured["timeout"] = timeout

        monkeypatch.setattr(app_module, "_import_openai_client_class", lambda: FakeClientClass)

        client = get_openai_client()
        assert isinstance(client, FakeClientClass)
        assert captured["api_key"] == "sk-fake"
        assert captured["timeout"] == app_module.OPENAI_TIMEOUT_SECONDS

    def test_caches_client_across_calls(self, monkeypatch):
        monkeypatch.setattr(app_module, "OPENAI_API_KEY", "sk-fake")
        calls = []

        class FakeClientClass:
            def __init__(self, api_key, timeout):
                pass

        def fake_import():
            calls.append(1)
            return FakeClientClass

        monkeypatch.setattr(app_module, "_import_openai_client_class", fake_import)

        first = get_openai_client()
        second = get_openai_client()

        assert first is second
        assert len(calls) == 1


class TestSuggestEmoji:
    def test_returns_emoji_from_model_response(self, monkeypatch):
        fake_client = FakeOpenAIClient(content="🎬")
        monkeypatch.setattr(app_module, "get_openai_client", lambda: fake_client)

        assert suggest_emoji("Кіно") == "🎬"
        assert fake_client.completions.calls[0]["model"] == app_module.OPENAI_MODEL
        assert fake_client.completions.calls[0]["messages"][-1]["content"] == "Кіно"

    def test_extracts_emoji_even_if_model_adds_extra_text(self, monkeypatch):
        fake_client = FakeOpenAIClient(content="Ось: 🎬 (кіно)")
        monkeypatch.setattr(app_module, "get_openai_client", lambda: fake_client)

        assert suggest_emoji("Кіно") == "🎬"

    def test_raises_when_client_unavailable(self, monkeypatch):
        monkeypatch.setattr(app_module, "get_openai_client", lambda: None)
        with pytest.raises(Exception):
            suggest_emoji("Кіно")

    def test_raises_when_model_returns_no_emoji(self, monkeypatch):
        fake_client = FakeOpenAIClient(content="кіно")
        monkeypatch.setattr(app_module, "get_openai_client", lambda: fake_client)

        with pytest.raises(Exception):
            suggest_emoji("Кіно")

    def test_raises_when_api_call_fails(self, monkeypatch):
        fake_client = FakeOpenAIClient(error=RuntimeError("network down"))
        monkeypatch.setattr(app_module, "get_openai_client", lambda: fake_client)

        with pytest.raises(Exception):
            suggest_emoji("Кіно")


class TestAddEmojiIfMissing:
    def test_returns_unchanged_when_emoji_already_present(self, monkeypatch):
        def fail(name):
            raise AssertionError("не мав звертатись до LLM — emoji вже є")

        monkeypatch.setattr(app_module, "suggest_emoji", fail)

        name, warning = add_emoji_if_missing("🎮 Ігри")
        assert name == "🎮 Ігри"
        assert warning is None

    def test_prefixes_emoji_from_llm_when_missing(self, monkeypatch):
        monkeypatch.setattr(app_module, "suggest_emoji", lambda name: "🎬")

        name, warning = add_emoji_if_missing("Кіно")
        assert name == "🎬 Кіно"
        assert warning is None

    def test_falls_back_to_plain_name_with_warning_on_llm_failure(self, monkeypatch):
        def boom(name):
            raise RuntimeError("no api key")

        monkeypatch.setattr(app_module, "suggest_emoji", boom)

        name, warning = add_emoji_if_missing("Кіно")
        assert name == "Кіно"
        assert warning is not None
        assert isinstance(warning, str) and len(warning) > 0

    def test_blank_name_short_circuits_without_calling_llm(self, monkeypatch):
        def fail(name):
            raise AssertionError("не мав звертатись до LLM для порожньої назви")

        monkeypatch.setattr(app_module, "suggest_emoji", fail)

        name, warning = add_emoji_if_missing("   ")
        assert name == ""
        assert warning is None


class TestAddCategoryRoute:
    def test_adds_emoji_via_llm_when_missing(self, logged_in_client, monkeypatch):
        monkeypatch.setattr(app_module, "suggest_emoji", lambda name: "🎬")

        response = logged_in_client.post("/categories/add", json={"type": "expense", "name": "Кіно"})
        data = response.get_json()

        assert response.status_code == 200
        assert "🎬 Кіно" in data["categories"]["expense"]
        assert "warning" not in data

    def test_keeps_existing_emoji_without_calling_llm(self, logged_in_client, monkeypatch):
        def fail(name):
            raise AssertionError("не мав звертатись до LLM — emoji вже є")

        monkeypatch.setattr(app_module, "suggest_emoji", fail)

        response = logged_in_client.post("/categories/add", json={"type": "expense", "name": "🎮 Ігри"})
        data = response.get_json()

        assert response.status_code == 200
        assert "🎮 Ігри" in data["categories"]["expense"]

    def test_saves_without_emoji_and_warns_when_llm_unavailable(self, logged_in_client, monkeypatch):
        def boom(name):
            raise RuntimeError("no api key")

        monkeypatch.setattr(app_module, "suggest_emoji", boom)

        response = logged_in_client.post("/categories/add", json={"type": "expense", "name": "Кіно"})
        data = response.get_json()

        assert response.status_code == 200
        assert "Кіно" in data["categories"]["expense"]
        assert "warning" in data and data["warning"]

    def test_rejects_duplicate_ignoring_emoji_difference(self, logged_in_client, monkeypatch):
        # "🛒 Продукти" вже є у стандартному наборі категорій витрат.
        monkeypatch.setattr(app_module, "suggest_emoji", lambda name: "🛒")

        response = logged_in_client.post("/categories/add", json={"type": "expense", "name": "продукти"})
        data = response.get_json()

        assert response.status_code == 409
        assert "вже існує" in data["error"]


class TestRenameCategoryRoute:
    def test_adds_emoji_on_rename_when_missing(self, logged_in_client, monkeypatch):
        monkeypatch.setattr(app_module, "suggest_emoji", lambda name: "🎬")

        response = logged_in_client.post(
            "/categories/rename",
            json={"type": "expense", "old_name": "🛒 Продукти", "new_name": "Кіно"},
        )
        data = response.get_json()

        assert response.status_code == 200
        assert "🎬 Кіно" in data["categories"]["expense"]
        assert "🛒 Продукти" not in data["categories"]["expense"]

    def test_warns_but_still_renames_when_llm_unavailable(self, logged_in_client, monkeypatch):
        def boom(name):
            raise RuntimeError("no api key")

        monkeypatch.setattr(app_module, "suggest_emoji", boom)

        response = logged_in_client.post(
            "/categories/rename",
            json={"type": "expense", "old_name": "🛒 Продукти", "new_name": "Кіно"},
        )
        data = response.get_json()

        assert response.status_code == 200
        assert "Кіно" in data["categories"]["expense"]
        assert "warning" in data and data["warning"]

    def test_rejects_rename_that_duplicates_another_category_ignoring_emoji(self, logged_in_client, monkeypatch):
        monkeypatch.setattr(app_module, "suggest_emoji", lambda name: "🚗")

        response = logged_in_client.post(
            "/categories/rename",
            json={"type": "expense", "old_name": "🛒 Продукти", "new_name": "транспорт"},
        )
        data = response.get_json()

        assert response.status_code == 409
        assert "вже існує" in data["error"]


class TestAddSubcategoryRoute:
    def test_adds_emoji_via_llm_when_missing(self, logged_in_client, monkeypatch):
        monkeypatch.setattr(app_module, "suggest_emoji", lambda name: "🎧")

        response = logged_in_client.post(
            "/subcategories/add",
            json={"type": "expense", "category": "💻 Техніка", "name": "Навушники"},
        )
        data = response.get_json()

        assert response.status_code == 200
        assert "🎧 Навушники" in data["categories"]["subcategories"]["expense"]["💻 Техніка"]

    def test_warns_but_still_adds_when_llm_unavailable(self, logged_in_client, monkeypatch):
        def boom(name):
            raise RuntimeError("no api key")

        monkeypatch.setattr(app_module, "suggest_emoji", boom)

        response = logged_in_client.post(
            "/subcategories/add",
            json={"type": "expense", "category": "💻 Техніка", "name": "Навушники"},
        )
        data = response.get_json()

        assert response.status_code == 200
        assert "Навушники" in data["categories"]["subcategories"]["expense"]["💻 Техніка"]
        assert "warning" in data and data["warning"]


class TestRenameSubcategoryRoute:
    def test_adds_emoji_on_rename_when_missing(self, logged_in_client, monkeypatch):
        monkeypatch.setattr(app_module, "suggest_emoji", lambda name: "🖨️")

        response = logged_in_client.post(
            "/subcategories/rename",
            json={
                "type": "expense",
                "category": "💻 Техніка",
                "old_name": "🖨️ Принтер (кредит)",
                "new_name": "Сканер",
            },
        )
        data = response.get_json()

        assert response.status_code == 200
        assert "🖨️ Сканер" in data["categories"]["subcategories"]["expense"]["💻 Техніка"]
