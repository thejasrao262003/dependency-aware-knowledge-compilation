"""make_client: the model client of every provider, built from the options of [provider.<name>].

Its fingerprints must be exactly those of 0.7, where make_provider built the clients itself:
a client fingerprint is part of every extractor, writer, summarizer and verifier fingerprint,
so any change would rebuild every existing workspace. The expected values below were
recorded from 0.7.0. The anthropic SDK is a fake module; nothing here reaches a network.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

import dakc.providers as providers
from dakc.conflicts.verifier import LLMVerifier
from dakc.providers import ALIASES, PRESETS, PROVIDERS, ProviderError, make_client, make_provider
from dakc.providers.llm import LLMExtractor, LLMPageWriter, LLMSummarizer

from tests.conftest import POSTGRES_DOCS, write_files

FIXTURES = Path(__file__).parents[1] / "fixtures"
CUSTOM = {"factory": "fake_custom_client:make", "path": str(FIXTURES)}
LOCAL = {"base_url": "http://localhost:8000/v1", "model": "qwen"}
OVERRIDES = {"model": "other-model", "api_key_env": "SOME_KEY", "json_mode": "none", "max_tokens": 99}

# (name, options, the client fingerprint 0.7.0 recorded for that configuration)
RECORDED_IN_0_7 = [
    ("claude-code", {}, "claude-code:haiku"),
    ("claude-code", {"model": "sonnet", "binary": "/x/claude", "timeout": 10, "retries": 0}, "claude-code:sonnet"),
    ("claude-code", {"model": "claude-opus-5-5"}, "claude-code:claude-opus-5-5"),
    ("anthropic", {}, "anthropic:claude-opus-5-5:default"),
    ("anthropic", {"model": "claude-haiku-5-5", "effort": "low", "max_tokens": 4000, "fallbacks": False},
     "anthropic:claude-haiku-5-5:low"),
    ("anthropic", {"model": "claude-sonnet-5-5", "effort": "high"}, "anthropic:claude-sonnet-5-5:high"),
    ("claude", {}, "anthropic:claude-opus-5-5:default"),
    ("claude", {"model": "claude-sonnet-5-5", "effort": "high"}, "anthropic:claude-sonnet-5-5:high"),
    ("openai", {}, "openai-compat:gpt-5.4-mini"),
    ("openai", OVERRIDES, "openai-compat:other-model"),
    ("openai", LOCAL, "openai-compat:qwen"),
    ("gemini", {}, "gemini:gemini-flash-latest"),
    ("gemini", OVERRIDES, "gemini:other-model"),
    ("google", {}, "gemini:gemini-flash-latest"),
    ("grok", {}, "grok:grok-4.3"),
    ("grok", LOCAL, "grok:qwen"),
    ("xai", {}, "grok:grok-4.3"),
    ("deepseek", {}, "deepseek:deepseek-flash"),
    ("deepseek", OVERRIDES, "deepseek:other-model"),
    ("mistral", {}, "mistral:mistral-small-latest"),
    ("mistral", LOCAL, "mistral:qwen"),
    ("groq", {"model": "some-model"}, "groq:some-model"),
    ("openrouter", {"model": "some-model"}, "openrouter:some-model"),
    ("openrouter", OVERRIDES, "openrouter:other-model"),
    ("together", {"model": "some-model"}, "together:some-model"),
    ("ollama", {}, "ollama:llama3.1"),
    ("ollama", OVERRIDES, "ollama:other-model"),
    ("lmstudio", {"model": "some-model"}, "lmstudio:some-model"),
    ("lm-studio", {"model": "some-model"}, "lmstudio:some-model"),
    ("custom", CUSTOM, "echo:echo"),
    ("custom", {**CUSTOM, "options": {"model": "m1"}}, "echo:m1"),
]


class FakeSDK(types.ModuleType):
    """Stands in for the ``anthropic`` package: records what each ``Anthropic()`` receives."""

    def __init__(self):
        super().__init__("anthropic")
        self.constructed: list[dict] = []
        sdk = self

        class Anthropic:
            def __init__(self, **kwargs):
                sdk.constructed.append(kwargs)

        self.Anthropic = Anthropic


@pytest.fixture
def sdk(monkeypatch) -> FakeSDK:
    fake = FakeSDK()
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    return fake


@pytest.fixture
def fixtures_on_path(monkeypatch):
    monkeypatch.syspath_prepend(str(FIXTURES))  # undone after the test; the custom provider finds it there


def test_every_provider_and_alias_has_a_recorded_configuration():
    assert {name for name, _, _ in RECORDED_IN_0_7} == set(PROVIDERS) - {"heuristic", "mock"} | set(ALIASES)


@pytest.mark.parametrize("name,options,fingerprint", RECORDED_IN_0_7)
def test_fingerprints_are_those_of_0_7(name, options, fingerprint, sdk, fixtures_on_path):
    assert make_client(name, dict(options)).fingerprint == fingerprint
    # one construction path: a compile's bundle is built on exactly this client
    bundle = make_provider(name, dict(options))
    client = make_client(name, dict(options))
    assert bundle.extractor.client.fingerprint == fingerprint
    assert bundle.fingerprints() == {"extractor": LLMExtractor(client).fingerprint,
                                     "writer": LLMPageWriter(client).fingerprint,
                                     "summarizer": LLMSummarizer(client).fingerprint}
    assert bundle.verifier.fingerprint == LLMVerifier(client).fingerprint


def test_api_key_env_does_not_change_a_fingerprint(sdk, monkeypatch):
    monkeypatch.setenv("RECKON_ANTHROPIC_KEY", "sk-ant-test-0000")
    assert make_client("anthropic", {"api_key_env": "RECKON_ANTHROPIC_KEY"}).fingerprint == "anthropic:claude-opus-5-5:default"


def test_make_client_returns_just_the_client(sdk):
    client = make_client("ollama")
    assert type(client).__name__ == "OpenAICompatibleClient" and client.base_url == PRESETS["ollama"]["base_url"]
    assert type(make_client("claude-code", {"model": "sonnet"})).__name__ == "ClaudeCodeClient"
    assert type(make_client("claude")).__name__ == "AnthropicClient"


def test_make_client_does_not_change_the_options_it_is_given(fixtures_on_path):
    options = {**CUSTOM, "options": {"model": "m1"}}
    make_client("custom", options)
    assert options == {**CUSTOM, "options": {"model": "m1"}}


def test_unknown_and_deterministic_names_list_the_valid_ones():
    with pytest.raises(ProviderError, match="unknown provider 'nope'") as err:
        make_client("nope")
    message = str(err.value)
    for name in ("claude-code", "anthropic", *PRESETS, "custom", *ALIASES):
        assert name in message
    assert "heuristic" not in message  # not something make_client can build
    with pytest.raises(ProviderError, match="'mock' is deterministic and has no model client; choose one of claude-code"):
        make_client("mock")
    with pytest.raises(ProviderError, match="unknown provider 'nope'; choose one of heuristic, mock, claude-code"):
        make_provider("nope")


def test_provider_errors_of_make_client_are_those_of_make_provider():
    with pytest.raises(ProviderError, match=r"\[provider\.groq\] needs a model"):
        make_client("groq")
    with pytest.raises(ProviderError, match="factory"):
        make_client("custom", {})


def test_a_client_from_make_client_answers(fixtures_on_path):
    client = make_client("custom", CUSTOM)
    reply = client.complete("You write wiki pages.", "Claims: ...")
    assert (reply.text, reply.input_tokens, reply.output_tokens) == ("Lead paragraph [^C1].", 10, 5)


def test_the_public_surface():
    for name in providers.__all__:
        assert hasattr(providers, name), name
    assert {"make_client", "make_provider", "LLMClient", "Completion", "ProviderError"} <= set(providers.__all__)


# --------------------------------------------------------------------------- #
# api_key_env on the anthropic client
# --------------------------------------------------------------------------- #


def test_api_key_env_names_the_variable_whose_value_is_the_key(sdk, monkeypatch):
    monkeypatch.setenv("RECKON_ANTHROPIC_KEY", "sk-ant-test-1234")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-the-default")
    client = make_client("anthropic", {"api_key_env": "RECKON_ANTHROPIC_KEY", "model": "claude-haiku-5-5"})
    assert sdk.constructed == [{"api_key": "sk-ant-test-1234"}]
    # the key goes to the SDK and nowhere else: not kept on DAKC's client, not in its fingerprint
    assert "sk-ant-test-1234" not in repr(vars(client)) + client.fingerprint


@pytest.mark.parametrize("value", [None, ""])
def test_an_unset_or_empty_variable_leaves_the_key_to_the_sdk(value, sdk, monkeypatch):
    if value is None:
        monkeypatch.delenv("RECKON_ANTHROPIC_KEY", raising=False)
    else:
        monkeypatch.setenv("RECKON_ANTHROPIC_KEY", value)
    make_client("anthropic", {"api_key_env": "RECKON_ANTHROPIC_KEY"})
    assert sdk.constructed == [{}]  # Anthropic(): ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN or a profile, as in 0.7


def test_without_api_key_env_the_sdk_resolves_the_key_as_before(sdk, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-the-default")
    make_client("claude")
    assert sdk.constructed == [{}]


def test_api_key_env_from_dakc_toml(tmp_path, sdk, monkeypatch):
    from dakc.workspace import Workspace

    monkeypatch.setenv("TEAM_CLAUDE_KEY", "sk-ant-test-5678")
    write_files(tmp_path / "knowledge", POSTGRES_DOCS)
    (tmp_path / "dakc.toml").write_text('[dakc]\nsource_dir = "knowledge"\nprovider = "anthropic"\n\n'
                                        '[provider.anthropic]\napi_key_env = "TEAM_CLAUDE_KEY"\n', encoding="utf-8")
    bundle = Workspace.load(tmp_path).provider()
    assert sdk.constructed == [{"api_key": "sk-ant-test-5678"}]
    assert bundle.extractor.client.fingerprint == "anthropic:claude-opus-5-5:default"
