"""Named providers: each OpenAI-compatible preset sends what its API expects, asks for what is
missing in plain words, and lets [provider.<name>] override anything. Fake transports only:
these check DAKC's requests, not any provider's real behaviour."""

from __future__ import annotations

import json

import httpx
import pytest

from dakc.providers import ALIASES, PRESETS, PROVIDERS, compatible_client, make_provider
from dakc.providers.anthropic_provider import AnthropicClient
from dakc.providers.llm import LLMExtractor, ProviderError

KEYS = {"openai": "OPENAI_API_KEY", "gemini": "GEMINI_API_KEY", "grok": "XAI_API_KEY", "deepseek": "DEEPSEEK_API_KEY",
        "mistral": "MISTRAL_API_KEY", "groq": "GROQ_API_KEY", "openrouter": "OPENROUTER_API_KEY",
        "together": "TOGETHER_API_KEY"}


def captured(client):
    """Route the client's requests to a fake server; returns the list of (url, headers, body) seen."""
    seen = []

    def handler(request: httpx.Request):
        seen.append((str(request.url), dict(request.headers), json.loads(request.content)))
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"claims": []}'}, "finish_reason": "stop"}],
                                         "usage": {"prompt_tokens": 3, "completion_tokens": 2}})

    client.http = httpx.Client(transport=httpx.MockTransport(handler), headers=client.http.headers)
    return seen


@pytest.mark.parametrize("name", sorted(KEYS))
def test_each_hosted_preset_calls_its_endpoint_with_its_key(name, monkeypatch):
    monkeypatch.setenv(KEYS[name], "sk-test")
    options = {} if PRESETS[name]["model"] else {"model": "some-model"}
    client = compatible_client(name, options)
    seen = captured(client)
    LLMExtractor(client).extract("PostgreSQL supports JSONB.")
    ((url, headers, body),) = seen
    assert url == PRESETS[name]["base_url"].rstrip("/") + "/chat/completions"
    assert headers["authorization"] == "Bearer sk-test"
    assert body["model"] == (PRESETS[name]["model"] or "some-model")
    assert "temperature" not in body and "seed" not in body  # hosted models choose their own sampling
    assert body.get(PRESETS[name].get("token_param", "max_tokens")) == 16000
    mode = PRESETS[name]["json_mode"]
    assert body.get("response_format", {}).get("type") == (None if mode == "none" else mode)


def test_openai_uses_max_completion_tokens_and_a_strict_schema(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    client = compatible_client("openai", {})
    seen = captured(client)
    LLMExtractor(client).extract("PostgreSQL supports JSONB.")
    body = seen[0][2]
    assert "max_tokens" not in body and body["max_completion_tokens"] == 16000
    assert body["response_format"]["json_schema"]["strict"] is True


def test_a_missing_key_is_reported_at_the_first_call_not_at_startup(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    client = compatible_client("gemini", {})  # `dakc status` builds the provider: this must not fail
    with pytest.raises(ProviderError, match="GEMINI_API_KEY is not set"):
        client.complete("s", "u")


@pytest.mark.parametrize("name", ["groq", "openrouter", "together", "lmstudio"])
def test_providers_of_many_models_ask_for_one(name):
    with pytest.raises(ProviderError, match=rf"\[provider\.{name}\] needs a model"):
        make_provider(name)


def test_options_override_the_preset(monkeypatch):
    monkeypatch.setenv("MY_KEY", "k")
    client = compatible_client("deepseek", {"model": "deepseek-v4-pro", "api_key_env": "MY_KEY", "json_mode": "none"})
    seen = captured(client)
    client.complete("s", "u", json_schema={"type": "object"})
    assert seen[0][2]["model"] == "deepseek-v4-pro" and "response_format" not in seen[0][2]
    assert seen[0][1]["authorization"] == "Bearer k"


def test_openai_pointed_at_another_server_keeps_its_old_behaviour():
    """`provider = "openai"` with base_url = a local server: as before presets, so existing
    workspaces build and cache exactly as they did."""
    client = compatible_client("openai", {"base_url": "http://localhost:8000/v1", "model": "qwen"})
    assert client.fingerprint == "openai-compat:qwen" and client.missing_key is None
    seen = captured(client)
    client.complete("s", "u")
    body = seen[0][2]
    assert body["temperature"] == 0.0 and body["seed"] == 0 and body["max_tokens"] == 4096


def test_fingerprints_name_the_provider():
    assert compatible_client("gemini", {}).fingerprint == "gemini:gemini-flash-latest"
    assert compatible_client("openai", {}).fingerprint == "openai-compat:gpt-5.4-mini"  # unchanged, caches stay valid
    assert compatible_client("ollama", {}).fingerprint == "ollama:llama3.1"


def test_aliases_and_the_provider_list():
    assert {"claude-code", "anthropic", "openai", "gemini", "grok", "deepseek", "mistral", "ollama"} <= set(PROVIDERS)
    assert make_provider("xai").name == "grok" and make_provider("google").name == "gemini"
    assert ALIASES["claude"] == "anthropic"
    with pytest.raises(ProviderError, match="unknown provider"):
        make_provider("nope")


class _Fake:
    def __init__(self):
        self.calls = []
        msg = type("M", (), {"stop_reason": "end_turn", "content": [], "usage": type("U", (), {"input_tokens": 1, "output_tokens": 1})()})()
        outer = self

        class Messages:
            def create(self, **kw):
                outer.calls.append(kw)
                return msg

        self.messages = Messages()
        self.beta = type("B", (), {"messages": Messages()})()


@pytest.mark.parametrize("model,fallbacks", [("claude-opus-5-5", True), ("claude-sonnet-5-5", True),
                                              ("claude-fable-5-2", True), ("claude-haiku-5-5", False)])
def test_claude_refusal_fallbacks_only_where_the_model_has_them(model, fallbacks):
    fake = _Fake()
    AnthropicClient(model=model, client=fake).complete("s", "u")
    assert ("fallbacks" in fake.calls[0]) == fallbacks
