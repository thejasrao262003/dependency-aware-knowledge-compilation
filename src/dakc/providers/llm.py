"""LLM-backed extractor, page writer and summarizer over a small client protocol.

Clients: :class:`OpenAICompatibleClient` (OpenAI, Ollama, vLLM, LM Studio, any
``/v1/chat/completions`` server) here, and the Claude client in
:mod:`dakc.providers.anthropic_provider`.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Protocol

import httpx

from dakc.claims import ExtractedClaim
from dakc.hashing import h
from dakc.providers.base import ClaimView, ExtractResult, GenResult, PageLead


class ProviderError(RuntimeError):
    pass


@dataclass
class Completion:
    text: str
    input_tokens: int
    output_tokens: int


class LLMClient(Protocol):
    fingerprint: str

    def complete(self, system: str, user: str, json_schema: dict | None = None) -> Completion: ...


# --------------------------------------------------------------------------- #
# Prompts. Their hashes are part of each component's fingerprint, so editing a
# prompt invalidates exactly the artifacts that prompt produced.
# --------------------------------------------------------------------------- #

EXTRACT_SYSTEM = """You extract atomic factual claims from one passage of a document, for a knowledge base.
Rules:
- Each claim is one self-contained declarative sentence stated by the passage. Prefer the passage's own wording.
- Replace pronouns with the named subject so each claim stands alone.
- For each claim, list the named entities it mentions (people, organizations, products, technologies, places, named concepts), spelled as in the passage.
- Skip opinions, questions, instructions, navigation text and boilerplate.
- If the passage states no facts, return an empty list.
Respond only with JSON of the form {"claims": [{"text": "...", "entities": ["..."]}]}."""

EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "entities": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["text", "entities"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["claims"],
    "additionalProperties": False,
}

PAGE_SYSTEM = """You write concise, encyclopedia-style wiki pages from verified claims.
- Use only the facts in the numbered claims. Do not add outside knowledge.
- Begin with a one-paragraph summary of two or three sentences.
- Then organize the remaining facts under short "##" sections where that helps.
- Cite every factual sentence with the labels of the claims it relies on, as footnote markers such as [^C3].
- Do not write a title, a references section or a list of sources."""

SUMMARY_SYSTEM = """You write a short overview of one topic of a knowledge base, from the leads of its pages.
- One to three paragraphs. Mention the most important pages by their exact titles.
- Do not state anything the leads do not support."""


def _fp(kind: str, client: LLMClient, prompt: str) -> str:
    return f"{kind}/1:{client.fingerprint}:{h(prompt)[:10]}"


def _parse_json_object(text: str) -> dict:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ProviderError(f"expected a JSON object, got: {text[:200]!r}")
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ProviderError(f"invalid JSON from model: {exc}") from exc


class LLMExtractor:
    is_llm = True
    free = False

    def __init__(self, client: LLMClient):
        self.client = client
        self.fingerprint = _fp("llm-extractor", client, EXTRACT_SYSTEM + json.dumps(EXTRACT_SCHEMA, sort_keys=True))

    def extract(self, passage_text: str, anchors: tuple[str, ...] = ()) -> ExtractResult:
        user = ""
        if anchors:
            # Anchoring: unchanged facts keep their exact wording, so they keep
            # their content-addressed claim id and stop invalidation early.
            user += (
                "Claims extracted from an earlier version of this passage. When one of these facts is "
                "still stated, reuse its wording exactly:\n" + "\n".join(f"- {a}" for a in anchors) + "\n\n"
            )
        user += f"Passage:\n<<<\n{passage_text}\n>>>"
        c = self.client.complete(EXTRACT_SYSTEM, user, json_schema=EXTRACT_SCHEMA)
        data = _parse_json_object(c.text)
        claims: list[ExtractedClaim] = []
        for item in data.get("claims", []):
            text = str(item.get("text", "")).strip()
            ents = tuple(sorted({str(e).strip() for e in item.get("entities", []) if str(e).strip()}))
            if text and ents:
                claims.append(ExtractedClaim(text, ents))
        return ExtractResult(claims, c.input_tokens, c.output_tokens)


class LLMPageWriter:
    is_llm = True
    free = False

    def __init__(self, client: LLMClient):
        self.client = client
        self.fingerprint = _fp("llm-writer", client, PAGE_SYSTEM)

    def write(self, name: str, claims: tuple[ClaimView, ...]) -> GenResult:
        user = f"Subject: {name}\n\nClaims:\n" + "\n".join(f"[{c.ref}] {c.text}" for c in claims)
        c = self.client.complete(PAGE_SYSTEM, user)
        return GenResult(c.text.strip(), c.input_tokens, c.output_tokens)


class LLMSummarizer:
    is_llm = True
    free = False

    def __init__(self, client: LLMClient):
        self.client = client
        self.fingerprint = _fp("llm-summarizer", client, SUMMARY_SYSTEM)

    def summarize(self, topic: str, pages: tuple[PageLead, ...]) -> GenResult:
        user = f"Topic: {topic}\n\nPages:\n" + "\n".join(f"- {p.title}: {p.lead}" for p in pages)
        c = self.client.complete(SUMMARY_SYSTEM, user)
        return GenResult(c.text.strip(), c.input_tokens, c.output_tokens)


# --------------------------------------------------------------------------- #
# OpenAI-compatible chat completions client
# --------------------------------------------------------------------------- #


class OpenAICompatibleClient:
    """Minimal ``/v1/chat/completions`` client (OpenAI, Ollama, vLLM, LM Studio).

    ``json_mode`` controls structured output: ``"none"`` (prompt + lenient
    parsing, works everywhere), ``"json_object"`` or ``"json_schema"`` (server
    must support it).
    """

    def __init__(
        self,
        base_url: str = "http://localhost:11434/v1",
        model: str = "llama3.1",
        api_key_env: str = "OPENAI_API_KEY",
        timeout: float = 180.0,
        json_mode: str = "none",
        temperature: float = 0.0,
        seed: int | None = 0,
        max_tokens: int = 4096,
        retries: int = 3,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.json_mode = json_mode
        self.temperature = temperature
        self.seed = seed
        self.max_tokens = max_tokens
        self.retries = retries
        headers = {"Content-Type": "application/json"}
        key = os.environ.get(api_key_env, "")
        if key:
            headers["Authorization"] = f"Bearer {key}"
        self.http = httpx.Client(timeout=timeout, headers=headers)
        self.fingerprint = f"openai-compat:{model}"

    def complete(self, system: str, user: str, json_schema: dict | None = None) -> Completion:
        payload: dict = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if self.seed is not None:
            payload["seed"] = self.seed
        if json_schema is not None and self.json_mode == "json_schema":
            payload["response_format"] = {"type": "json_schema", "json_schema": {"name": "output", "schema": json_schema, "strict": True}}
        elif json_schema is not None and self.json_mode == "json_object":
            payload["response_format"] = {"type": "json_object"}
        last: Exception | None = None
        for attempt in range(self.retries):
            try:
                r = self.http.post(f"{self.base_url}/chat/completions", json=payload)
                if r.status_code == 429 or r.status_code >= 500:
                    last = ProviderError(f"HTTP {r.status_code}: {r.text[:200]}")
                else:
                    r.raise_for_status()
                    data = r.json()
                    choice = data["choices"][0]
                    if choice.get("finish_reason") == "length":
                        raise ProviderError("completion truncated (finish_reason=length); raise max_tokens")
                    usage = data.get("usage") or {}
                    return Completion(
                        choice["message"]["content"] or "",
                        int(usage.get("prompt_tokens", 0)),
                        int(usage.get("completion_tokens", 0)),
                    )
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last = exc
            time.sleep(2**attempt)
        raise ProviderError(f"request to {self.base_url} failed after {self.retries} attempts: {last}")
