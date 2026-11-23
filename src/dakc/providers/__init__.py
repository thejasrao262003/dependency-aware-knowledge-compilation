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

PROVIDERS = ("heuristic", "mock", "openai", "anthropic", "custom")


def make_provider(name: str, options: dict[str, Any] | None = None) -> ProviderBundle:
    options = dict(options or {})
    from dakc.conflicts.verifier import HeuristicVerifier, LLMVerifier

    if name in ("heuristic", "mock"):
        as_llm = name == "mock"
        return ProviderBundle(name, HeuristicExtractor(as_llm), TemplatePageWriter(as_llm), TemplateSummarizer(as_llm),
                              verifier=HeuristicVerifier(as_llm))
    if name == "openai":
        client = OpenAICompatibleClient(**options)
    elif name == "anthropic":
        from dakc.providers.anthropic_provider import AnthropicClient

        client = AnthropicClient(**options)
    elif name == "custom":
        client = load_custom_client(options)
    else:
        raise ProviderError(f"unknown provider {name!r}; choose one of {', '.join(PROVIDERS)}")
    return ProviderBundle(name, LLMExtractor(client), LLMPageWriter(client), LLMSummarizer(client),
                          verifier=LLMVerifier(client))


def load_custom_client(options: dict[str, Any]):
    """Bring-your-own LLM client.

    ``factory = "module.path:callable"`` must return an object with a
    ``fingerprint`` attribute and ``complete(system, user, json_schema=None)``
    returning :class:`dakc.providers.llm.Completion`. Optional ``path`` is a
    directory prepended to ``sys.path`` (resolved by the workspace relative to
    itself), and ``options`` is a table of keyword arguments for the factory.
    """
    import importlib
    import sys

    spec = options.get("factory")
    if not spec or ":" not in spec:
        raise ProviderError('custom provider needs factory = "module.path:callable" in [provider.custom]')
    path = options.get("path")
    if path and path not in sys.path:
        sys.path.insert(0, path)
    module_name, _, attr = spec.partition(":")
    try:
        factory = getattr(importlib.import_module(module_name), attr)
    except (ImportError, AttributeError) as exc:
        raise ProviderError(f"cannot load custom provider factory {spec!r}: {exc}") from exc
    client = factory(**dict(options.get("options", {})))
    if not hasattr(client, "complete") or not getattr(client, "fingerprint", ""):
        raise ProviderError(f"{spec!r} must return an object with .fingerprint and .complete(system, user, json_schema=None)")
    return client
