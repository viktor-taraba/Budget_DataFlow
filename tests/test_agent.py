"""
Тести AI-порад по бюджету (agent.py): формування промпту для LLM, лінивий
клієнт OpenAI, генерація тексту поради. Той самий підхід, що й
tests/test_category_emoji.py — фейковий клієнт OpenAI підмінюється через
get_openai_client, мережа ніколи не викликається.
"""
import pytest

import agent

SAMPLE_SUMMARY = {
    "start": "2026-08-01",
    "end": "2026-08-16",
    "income": [{"category": "💼 Зарплата", "amount": 20000.0}],
    "expense": [
        {"category": "🛒 Продукти", "amount": 3000.0},
        {"category": "🚗 Транспорт", "amount": 500.0},
    ],
    "income_total": 20000.0,
    "expense_total": 3500.0,
}


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


@pytest.fixture(autouse=True)
def _reset_client(monkeypatch):
    monkeypatch.setattr(agent, "_openai_client", None)


class TestBuildPrompt:
    def test_includes_totals_and_categories(self):
        prompt = agent.build_prompt(SAMPLE_SUMMARY)
        assert "20000.00" in prompt
        assert "3500.00" in prompt
        assert "🛒 Продукти" in prompt
        assert "💼 Зарплата" in prompt
        assert "2026-08-01" in prompt and "2026-08-16" in prompt

    def test_handles_empty_categories(self):
        summary = {**SAMPLE_SUMMARY, "income": [], "expense": []}
        prompt = agent.build_prompt(summary)
        assert "немає даних" in prompt

    def test_single_day_period_has_no_range_dash(self):
        summary = {**SAMPLE_SUMMARY, "start": "2026-08-01", "end": "2026-08-01"}
        prompt = agent.build_prompt(summary)
        assert "2026-08-01 – 2026-08-01" not in prompt
        assert "Період: 2026-08-01\n" in prompt


class TestSystemPromptLanguage:
    """Системний промпт (інструкції моделі) — англійською; відповідь модель
    все одно має дати українською, оскільки решта інтерфейсу україномовна."""

    def test_system_prompt_is_english(self):
        assert "You are a financial assistant" in agent.SYSTEM_PROMPT

    def test_system_prompt_still_asks_for_ukrainian_output(self):
        assert "Ukrainian" in agent.SYSTEM_PROMPT


class TestGetOpenaiClient:
    def test_returns_none_without_api_key(self):
        assert agent.get_openai_client(None) is None
        assert agent.get_openai_client("") is None

    def test_returns_none_when_package_not_importable(self, monkeypatch):
        def fail_import():
            raise ImportError("no module named openai")

        monkeypatch.setattr(agent, "_import_openai_client_class", fail_import)
        assert agent.get_openai_client("sk-fake") is None

    def test_constructs_and_caches_client(self, monkeypatch):
        captured = {}
        calls = []

        class FakeClientClass:
            def __init__(self, api_key, timeout):
                captured["api_key"] = api_key
                captured["timeout"] = timeout

        def fake_import():
            calls.append(1)
            return FakeClientClass

        monkeypatch.setattr(agent, "_import_openai_client_class", fake_import)

        first = agent.get_openai_client("sk-fake")
        second = agent.get_openai_client("sk-fake")

        assert first is second
        assert len(calls) == 1
        assert captured["api_key"] == "sk-fake"


class TestGenerateBudgetInsights:
    def test_returns_model_response(self, monkeypatch):
        fake_client = FakeOpenAIClient(content="Ви витрачаєте забагато на каву.")
        monkeypatch.setattr(agent, "get_openai_client", lambda api_key: fake_client)

        text = agent.generate_budget_insights(SAMPLE_SUMMARY, "sk-fake")
        assert text == "Ви витрачаєте забагато на каву."
        assert fake_client.completions.calls[0]["model"] == agent.OPENAI_MODEL
        assert fake_client.completions.calls[0]["messages"][0]["content"] == agent.SYSTEM_PROMPT

    def test_raises_without_client(self, monkeypatch):
        monkeypatch.setattr(agent, "get_openai_client", lambda api_key: None)
        with pytest.raises(Exception):
            agent.generate_budget_insights(SAMPLE_SUMMARY, None)

    def test_raises_on_empty_response(self, monkeypatch):
        fake_client = FakeOpenAIClient(content="   ")
        monkeypatch.setattr(agent, "get_openai_client", lambda api_key: fake_client)
        with pytest.raises(Exception):
            agent.generate_budget_insights(SAMPLE_SUMMARY, "sk-fake")

    def test_raises_on_api_error(self, monkeypatch):
        fake_client = FakeOpenAIClient(error=RuntimeError("network down"))
        monkeypatch.setattr(agent, "get_openai_client", lambda api_key: fake_client)
        with pytest.raises(Exception):
            agent.generate_budget_insights(SAMPLE_SUMMARY, "sk-fake")
