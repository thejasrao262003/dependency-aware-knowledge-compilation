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

from dakc.claims import ExtractedClaim, split_sentences
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
The passage is given as numbered sentences.
Rules:
- Each claim is one self-contained declarative sentence stated by the passage. Prefer the passage's own wording.
- Replace pronouns with the named subject so each claim stands alone.
- For each claim, list at least one entity it is about: named entities (people, organizations, products, technologies, places) and specific technical concepts (a component, feature, protocol or format, e.g. "file watcher"), spelled as in the passage. Do not list generic words such as "size" or "data".
- For each claim, list in "sentences" the numbers of every sentence needed to state it, including any sentence that identifies what a pronoun or a phrase such as "the service" refers to.
- Skip opinions, questions, instructions, navigation text and boilerplate.
- If asked for claims of specific sentences only, return only claims that those sentences state at least in part; use the other sentences only as context.
- If there are no such facts, return an empty list.
Respond only with JSON of the form {"claims": [{"text": "...", "entities": ["..."], "sentences": [1]}]}."""

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
                    "sentences": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["text", "entities", "sentences"],
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


JSON_REPAIR = (
    "\n\nYour previous answer was not valid JSON. Answer again with only a valid JSON object: "
    "escape every double quote and backslash inside strings, no comments, no trailing commas."
)


class LLMExtractor:
    """LLM claim extraction over numbered sentences, with per-claim sentence evidence.

    With ``targets``, the model sees the whole passage but extracts only claims
    stated by the target sentences; DAKC reuses the cached claims of every other
    (unchanged) sentence verbatim, so their ids cannot drift (see
    ``dakc.claims.plan_reuse``).
    """

    is_llm = True
    free = False

    def __init__(self, client: LLMClient):
        self.client = client
        self.fingerprint = _fp("llm-extractor", client, EXTRACT_SYSTEM + json.dumps(EXTRACT_SCHEMA, sort_keys=True))

    def extract(self, passage_text: str, targets: tuple[int, ...] | None = None) -> ExtractResult:
        sents = split_sentences(passage_text)
        numbered = "\n".join(f"[{i + 1}] {x}" for i, x in enumerate(sents))
        user = f"Passage (numbered sentences):\n<<<\n{numbered}\n>>>\n\n"
        wanted = sorted(set(targets)) if targets is not None else list(range(len(sents)))
        if targets is not None:
            user += (
                f"Extract only the claims stated, at least in part, by sentence(s) {', '.join(str(i + 1) for i in wanted)}. "
                "The other sentences are context only."
            )
        else:
            user += "Extract all claims."
        tokens_in = tokens_out = calls = 0
        for attempt in range(2):
            prompt = user if attempt == 0 else user + JSON_REPAIR
            c = self.client.complete(EXTRACT_SYSTEM, prompt, json_schema=EXTRACT_SCHEMA)
            tokens_in, tokens_out, calls = tokens_in + c.input_tokens, tokens_out + c.output_tokens, calls + 1
            try:
                data = _parse_json_object(c.text)
                break
            except ProviderError:
                if attempt == 1:
                    raise
        claims: list[ExtractedClaim] = []
        for item in data.get("claims", []):
            text = str(item.get("text", "")).strip()
            ents = tuple(sorted({str(e).strip() for e in item.get("entities", []) if str(e).strip()}))
            evidence = tuple(sorted({int(n) - 1 for n in item.get("sentences", []) if str(n).lstrip("-").isdigit() and 1 <= int(n) <= len(sents)}))
            if not evidence:
                evidence = tuple(wanted)  # unknown provenance: conservatively depend on every target sentence
            if targets is not None and not set(evidence) & set(wanted):
                continue  # states only context sentences, whose claims DAKC reuses
            if text and ents:
                claims.append(ExtractedClaim(text, ents, evidence))
        return ExtractResult(claims, tokens_in, tokens_out, calls)


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
    """Minimal ``/v1/chat/completions`` client (OpenAI, Gemini, Grok, DeepSeek, Mistral,
    Groq, OpenRouter, Together, Ollama, vLLM, LM Studio: see ``dakc.providers.PRESETS``).

    ``json_mode`` controls structured output: ``"none"`` (prompt + lenient
    parsing, works everywhere), ``"json_object"`` or ``"json_schema"`` (server
    must support it). ``temperature`` and ``seed`` set to None are left out, for
    models that reject them. ``token_param`` names the output limit:
    ``max_tokens``, or ``max_completion_tokens`` for OpenAI's reasoning models.
    """

    def __init__(
        self,
        base_url: str = "http://localhost:11434/v1",
        model: str = "llama3.1",
        api_key_env: str = "OPENAI_API_KEY",
        timeout: float = 180.0,
        json_mode: str = "none",
        temperature: float | None = 0.0,
        seed: int | None = 0,
        max_tokens: int = 4096,
        retries: int = 3,
        token_param: str = "max_tokens",
        require_key: bool = False,
        name: str = "openai-compat",
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.json_mode = json_mode
        self.temperature = temperature
        self.seed = seed
        self.max_tokens = max_tokens
        self.retries = retries
        self.token_param = token_param
        headers = {"Content-Type": "application/json"}
        key = os.environ.get(api_key_env, "") if api_key_env else ""
        if key:
            headers["Authorization"] = f"Bearer {key}"
        self.missing_key = api_key_env if require_key and not key else None  # reported at the first call
        self.http = httpx.Client(timeout=timeout, headers=headers)
        self.fingerprint = f"{name}:{model}"

    def complete(self, system: str, user: str, json_schema: dict | None = None) -> Completion:
        if self.missing_key:
            raise ProviderError(f"{self.missing_key} is not set: export the provider's API key as {self.missing_key} "
                                "(or name another variable with api_key_env)")
        payload: dict = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            self.token_param: self.max_tokens,
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature
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
