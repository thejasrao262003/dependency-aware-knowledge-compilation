"""LLM provider code paths, exercised against fake transports (no network).

These verify DAKC's request construction and response handling, and the
compiler's LLM path (concurrency, accounting, dry-run upper bounds). They do
not and cannot verify any real model's behaviour.
"""

import json
from types import SimpleNamespace

import httpx
import pytest

from dakc.compiler import Compiler
from dakc.providers import ProviderBundle
from dakc.providers.anthropic_provider import FALLBACK_BETA, AnthropicClient
from dakc.providers.llm import LLMExtractor, LLMPageWriter, LLMSummarizer, OpenAICompatibleClient, ProviderError

from tests.conftest import POSTGRES_DOCS, write_files


def fake_openai(handler) -> OpenAICompatibleClient:
    client = OpenAICompatibleClient(base_url="http://fake/v1", model="m", retries=2)
    client.http = httpx.Client(transport=httpx.MockTransport(handler))
    return client


def chat_response(content: str, finish="stop", usage=(11, 7)):
    return httpx.Response(200, json={
        "choices": [{"message": {"content": content}, "finish_reason": finish}],
        "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1]},
    })


def test_openai_extractor_parses_json_and_sends_anchors():
    seen = {}

    def handler(request: httpx.Request):
        body = json.loads(request.content)
        seen["body"] = body
        return chat_response('```json\n{"claims": [{"text": "PostgreSQL supports JSONB.", "entities": ["PostgreSQL", "JSONB"]}, {"text": "No entities here.", "entities": []}]}\n```')

    ex = LLMExtractor(fake_openai(handler))
    res = ex.extract("PostgreSQL supports JSONB.", anchors=("PostgreSQL supports Transactions.",))
    assert [c.text for c in res.claims] == ["PostgreSQL supports JSONB."]  # claims without entities dropped
    assert res.claims[0].mentions == ("JSONB", "PostgreSQL")
    assert (res.input_tokens, res.output_tokens) == (11, 7)
    user = seen["body"]["messages"][1]["content"]
    assert "reuse its wording exactly" in user and "PostgreSQL supports Transactions." in user
    assert seen["body"]["temperature"] == 0.0 and seen["body"]["seed"] == 0


def test_openai_client_retries_then_fails_and_detects_truncation(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        return httpx.Response(503, text="busy") if calls["n"] == 1 else chat_response("ok")

    assert fake_openai(flaky).complete("s", "u").text == "ok"
    with pytest.raises(ProviderError, match="failed after"):
        fake_openai(lambda r: httpx.Response(500, text="down")).complete("s", "u")
    with pytest.raises(ProviderError, match="truncated"):
        fake_openai(lambda r: chat_response("partial", finish="length")).complete("s", "u")
    with pytest.raises(ProviderError, match="JSON"):
        LLMExtractor(fake_openai(lambda r: chat_response("no json here"))).extract("x")


def test_fingerprints_change_with_model_and_prompt():
    a = LLMExtractor(OpenAICompatibleClient(model="a")).fingerprint
    b = LLMExtractor(OpenAICompatibleClient(model="b")).fingerprint
    assert a != b and a.startswith("llm-extractor/1:openai-compat:a:")
    assert LLMPageWriter(OpenAICompatibleClient(model="a")).fingerprint != a


class FakeAnthropic:
    def __init__(self, stop_reason="end_turn", text='{"claims": []}'):
        self.calls = []
        self.stop_reason = stop_reason
        self.text = text
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            stop_reason=self.stop_reason,
            stop_details=SimpleNamespace(category="cyber") if self.stop_reason == "refusal" else None,
            content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=self.text)],
            usage=SimpleNamespace(input_tokens=5, output_tokens=3),
        )


def test_anthropic_request_shape():
    fake = FakeAnthropic(text='{"claims": [{"text": "Kafka streams events.", "entities": ["Kafka"]}]}')
    client = AnthropicClient(client=fake, effort="low")
    res = LLMExtractor(client).extract("Kafka streams events.")
    call = fake.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["betas"] == [FALLBACK_BETA] and call["fallbacks"] == "default"
    assert call["output_config"]["effort"] == "low"
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert "temperature" not in call
    assert [c.text for c in res.claims] == ["Kafka streams events."]
    plain = AnthropicClient(client=fake, fallbacks=False)
    plain.complete("s", "u")
    assert "betas" not in fake.calls[-1] and "output_config" not in fake.calls[-1]


def test_anthropic_refusal_and_truncation_raise():
    with pytest.raises(ProviderError, match="declined"):
        AnthropicClient(client=FakeAnthropic(stop_reason="refusal")).complete("s", "u")
    with pytest.raises(ProviderError, match="max_tokens"):
        AnthropicClient(client=FakeAnthropic(stop_reason="max_tokens")).complete("s", "u")


def test_compiler_llm_path_end_to_end(kb):
    """Full compile through LLM components backed by a fake OpenAI endpoint."""
    src, store = kb
    write_files(src, POSTGRES_DOCS)

    def handler(request):
        body = json.loads(request.content)
        system, user = body["messages"][0]["content"], body["messages"][1]["content"]
        if "extract atomic factual claims" in system:
            passage = user.split("<<<\n", 1)[1].split("\n>>>", 1)[0]
            claims = [{"text": s.strip() + ".", "entities": [w for w in s.split() if w[:1].isupper()]} for s in passage.split(".") if s.strip()]
            return chat_response(json.dumps({"claims": claims}))
        if "wiki pages" in system:
            return chat_response("A short lead paragraph [^C1].\n\n## Details\n\nMore text [^C1].")
        return chat_response("Topic overview.")

    client = fake_openai(handler)
    bundle = ProviderBundle("openai", LLMExtractor(client), LLMPageWriter(client), LLMSummarizer(client))
    rep, _ = Compiler(store, bundle, src, jobs=4).run()
    assert rep.status == "committed" and rep.usage.llm_calls == rep.usage.calls > 0
    assert rep.usage.input_tokens == 11 * rep.usage.calls
    page = store.current_artifact("page:jsonb")[0]
    assert page.lead == "A short lead paragraph." and "[^C1]: databases/postgres.md" in page.rendered

    write_files(src, {"databases/redis.md": "Redis supports Clustering.\n"})
    plan, _ = Compiler(store, bundle, src, dry_run=True).run()
    assert not plan.exact and plan.usage.calls == 0
    assert plan.planned_llm_calls["extract"] == 1
