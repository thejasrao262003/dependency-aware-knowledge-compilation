"""Provider factory.

``heuristic`` (default): deterministic, free, no network, not counted as LLM.
``mock``: same algorithms, but every call is accounted as an LLM call with
estimated tokens. Used by the benchmark to model LLM cost.
``claude-code``: Claude through the Claude Code CLI and its login, no API key.
``anthropic`` (alias ``claude``): Claude via the official SDK (``dakc[anthropic]`` extra).
``openai``, ``gemini``, ``grok``, ``deepseek``, ``mistral``, ``groq``, ``openrouter``,
``together``, ``ollama``, ``lmstudio``: OpenAI-compatible APIs, with the endpoint, key
variable and default model of each preset in :data:`PRESETS`. Anything set in
``[provider.<name>]`` overrides the preset.
``custom``: bring your own client.

:func:`make_provider` builds the bundle a compile uses; :func:`make_client` builds just the
model client underneath it, for code that wants to call the same model DAKC is
configured with. Both take the options of ``[provider.<name>]``.
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
from dakc.providers.llm import (
    Completion,
    LLMClient,
    LLMExtractor,
    LLMPageWriter,
    LLMSummarizer,
    OpenAICompatibleClient,
    ProviderError,
)

__all__ = [
    "ClaimView",
    "Completion",
    "ExtractResult",
    "GenResult",
    "LLMClient",
    "PageLead",
    "ProviderBundle",
    "ProviderError",
    "make_client",
    "make_provider",
    "compatible_client",
    "PROVIDERS",
    "PRESETS",
    "ALIASES",
]

# OpenAI-compatible APIs. Default models are the providers' own cheap, current choices or
# stable aliases (checked against each provider's docs, late 2026); `model = "..."` in
# [provider.<name>] picks another. Providers that host many vendors' models have no default.
_HOSTED = dict(temperature=None, seed=None, max_tokens=16000, require_key=True)  # leave sampling to the provider
PRESETS: dict[str, dict[str, Any]] = {
    "openai": dict(_HOSTED, base_url="https://api.openai.com/v1", api_key_env="OPENAI_API_KEY", model="gpt-5.4-mini",
                   json_mode="json_schema", token_param="max_completion_tokens",
                   models="https://developers.openai.com/api/docs/models"),
    "gemini": dict(_HOSTED, base_url="https://generativelanguage.googleapis.com/v1beta/openai", api_key_env="GEMINI_API_KEY",
                   model="gemini-flash-latest", json_mode="none", models="https://ai.google.dev/gemini-api/docs/models"),
    "grok": dict(_HOSTED, base_url="https://api.x.ai/v1", api_key_env="XAI_API_KEY", model="grok-4.3", json_mode="none",
                 models="https://docs.x.ai/developers/models"),
    "deepseek": dict(_HOSTED, base_url="https://api.deepseek.com", api_key_env="DEEPSEEK_API_KEY", model="deepseek-flash",
                     json_mode="json_object", models="https://api-docs.deepseek.com/quick_start/pricing"),
    "mistral": dict(_HOSTED, base_url="https://api.mistral.ai/v1", api_key_env="MISTRAL_API_KEY", model="mistral-small-latest",
                    json_mode="json_object", models="https://docs.mistral.ai/models"),
    "groq": dict(_HOSTED, base_url="https://api.groq.com/openai/v1", api_key_env="GROQ_API_KEY", model=None, json_mode="none",
                 models="https://console.groq.com/docs/models"),
    "openrouter": dict(_HOSTED, base_url="https://openrouter.ai/api/v1", api_key_env="OPENROUTER_API_KEY", model=None,
                       json_mode="none", models="https://openrouter.ai/models"),
    "together": dict(_HOSTED, base_url="https://api.together.ai/v1", api_key_env="TOGETHER_API_KEY", model=None,
                     json_mode="none", models="https://docs.together.ai/docs/serverless-models"),
    "ollama": dict(base_url="http://localhost:11434/v1", api_key_env="", model="llama3.1", json_mode="none",
                   models="the models you have pulled: `ollama list`"),
    "lmstudio": dict(base_url="http://localhost:1234/v1", api_key_env="", model=None, json_mode="none",
                     models="the model loaded in LM Studio"),
}
# `provider = "openai"` with a base_url of another server (vLLM, a gateway): the settings
# DAKC used before presets existed, which suit most OpenAI-compatible servers.
_COMPATIBLE = dict(model="llama3.1", json_mode="none", temperature=0.0, seed=0, max_tokens=4096, token_param="max_tokens",
                   require_key=False)
ALIASES = {"claude": "anthropic", "xai": "grok", "google": "gemini", "lm-studio": "lmstudio"}
_DETERMINISTIC = ("heuristic", "mock")
_WITH_CLIENT = ("claude-code", "anthropic", *PRESETS, "custom")  # the providers make_client builds
PROVIDERS = (*_DETERMINISTIC, *_WITH_CLIENT)


def _choices(names: tuple[str, ...]) -> str:
    aliases = ", ".join(f"{a} ({c})" for a, c in ALIASES.items() if c in names)
    return f"choose one of {', '.join(names)}" + (f"; or an alias: {aliases}" if aliases else "")


def compatible_client(name: str, options: dict[str, Any]) -> OpenAICompatibleClient:
    """An OpenAI-compatible client for a preset, with ``options`` from ``[provider.<name>]`` on top."""
    preset = dict(PRESETS[name])
    models = preset.pop("models")
    if name == "openai" and not str(options.get("base_url", preset["base_url"])).startswith("https://api.openai.com"):
        preset.update(_COMPATIBLE)
    opts = {**preset, **options}
    if not opts.get("model"):
        raise ProviderError(f'[provider.{name}] needs a model, e.g. model = "...": see {models}')
    # "openai" keeps the fingerprint it always had, so existing caches stay valid
    return OpenAICompatibleClient(name="openai-compat" if name == "openai" else name, **opts)


def make_client(name: str, options: dict[str, Any] | None = None) -> LLMClient:
    """The model client of provider ``name``, configured by ``options``: the keys of its
    ``[provider.<name>]`` table in ``dakc.toml``, with the same names and defaults.

    This is the client a compile with that configuration uses, so its ``fingerprint``
    is the one DAKC records. Call ``client.complete(system, user, json_schema=None)``
    to get a :class:`Completion`. Failures raise :class:`ProviderError`, whose
    ``transient`` says whether asking again later may succeed. Calls made this way are
    not cached or counted by DAKC.

    ``name`` is one of ``claude-code``, ``anthropic``, an OpenAI-compatible preset in
    :data:`PRESETS`, ``custom``, or an alias in :data:`ALIASES`. ``heuristic`` and
    ``mock`` have no model client. For ``custom``, ``path`` is used as given; a
    workspace resolves it relative to itself first.
    """
    options = dict(options or {})
    canonical = ALIASES.get(name, name)
    if canonical in PRESETS:
        return compatible_client(canonical, options)
    if canonical == "claude-code":
        from dakc.providers.claude_code import ClaudeCodeClient

        return ClaudeCodeClient(**options)
    if canonical == "anthropic":
        from dakc.providers.anthropic_provider import AnthropicClient

        return AnthropicClient(**options)
    if canonical == "custom":
        return load_custom_client(options)
    if canonical in _DETERMINISTIC:
        raise ProviderError(f"provider {name!r} is deterministic and has no model client; {_choices(_WITH_CLIENT)}")
    raise ProviderError(f"unknown provider {name!r}; {_choices(_WITH_CLIENT)}")


def make_provider(name: str, options: dict[str, Any] | None = None) -> ProviderBundle:
    """The extractor, page writer, summarizer and conflict verifier of provider ``name``.
    LLM providers share one client, built by :func:`make_client` from ``options``."""
    name = ALIASES.get(name, name)
    from dakc.conflicts.verifier import HeuristicVerifier, LLMVerifier

    if name in _DETERMINISTIC:
        as_llm = name == "mock"
        return ProviderBundle(name, HeuristicExtractor(as_llm), TemplatePageWriter(as_llm), TemplateSummarizer(as_llm),
                              verifier=HeuristicVerifier(as_llm))
    if name not in _WITH_CLIENT:
        raise ProviderError(f"unknown provider {name!r}; {_choices(PROVIDERS)}")
    client = make_client(name, options)
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
