"""Provider factory.

``heuristic`` (default): deterministic, free, no network, not counted as LLM.
``mock``: same algorithms, but every call is accounted as an LLM call with
estimated tokens. Used by the benchmark to model LLM cost.
``openai``: any OpenAI-compatible endpoint (OpenAI, Ollama, vLLM, LM Studio).
``anthropic``: Claude via the official SDK (``dakc[anthropic]`` extra).
"""

from __future__ import annotations

from typing import Any

from dakc.providers.base import (
    ClaimView,
    ExtractResult,
    GenResult,
    PageLead,
    ProviderBundle,
)
from dakc.providers.deterministic import HeuristicExtractor, TemplatePageWriter, TemplateSummarizer
from dakc.providers.llm import LLMExtractor, LLMPageWriter, LLMSummarizer, OpenAICompatibleClient, ProviderError

__all__ = [
    "ClaimView",
    "ExtractResult",
    "GenResult",
    "PageLead",
    "ProviderBundle",
    "ProviderError",
    "make_provider",
    "PROVIDERS",
]

PROVIDERS = ("heuristic", "mock", "openai", "anthropic")


def make_provider(name: str, options: dict[str, Any] | None = None) -> ProviderBundle:
    options = options or {}
    if name in ("heuristic", "mock"):
        as_llm = name == "mock"
        return ProviderBundle(name, HeuristicExtractor(as_llm), TemplatePageWriter(as_llm), TemplateSummarizer(as_llm))
    if name == "openai":
        client = OpenAICompatibleClient(**options)
        return ProviderBundle(name, LLMExtractor(client), LLMPageWriter(client), LLMSummarizer(client))
    if name == "anthropic":
        from dakc.providers.anthropic_provider import AnthropicClient

        client = AnthropicClient(**options)
        return ProviderBundle(name, LLMExtractor(client), LLMPageWriter(client), LLMSummarizer(client))
    raise ProviderError(f"unknown provider {name!r}; choose one of {', '.join(PROVIDERS)}")
