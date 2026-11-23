"""Claude provider using the official Anthropic Python SDK (optional extra).

Install with ``pip install 'dakc[anthropic]'``. Credentials are resolved by the
SDK (``ANTHROPIC_API_KEY``, ``ANTHROPIC_AUTH_TOKEN`` or an ``ant auth login``
profile).

Notes for current models:
* ``temperature`` is not sent: it is rejected on current models and API
  versions. Outputs are therefore not reproducible; DAKC relies on its
  content-addressed extraction cache and signatures, not on model determinism.
* Extraction uses structured outputs (``output_config.format``) so the
  response is valid JSON.
* Server-side refusal fallbacks are enabled by default (``fallbacks="default"``);
  a request the whole chain refuses raises :class:`ProviderError`.
"""

from __future__ import annotations

from dakc.providers.llm import Completion, ProviderError

DEFAULT_MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Server-side refusal fallbacks exist for these models only (not Haiku); others are sent without them.
FALLBACK_MODELS = ("claude-fable-", "claude-mythos-", "claude-opus-5", "claude-sonnet-5-5")


class AnthropicClient:
    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        effort: str | None = None,
        max_tokens: int = 16000,
        fallbacks: bool = True,
        client: object | None = None,  # injection seam for tests
    ):
        if client is None:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover - depends on optional extra
                raise ProviderError("the Claude provider needs the 'anthropic' extra: pip install 'dakc[anthropic]'") from exc
            client = anthropic.Anthropic()
        self.client = client
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self.fallbacks = fallbacks
        self.fingerprint = f"anthropic:{model}:{effort or 'default'}"

    def complete(self, system: str, user: str, json_schema: dict | None = None) -> Completion:
        kwargs: dict = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        output_config: dict = {}
        if self.effort:
            output_config["effort"] = self.effort
        if json_schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": json_schema}
        if output_config:
            kwargs["output_config"] = output_config

        if self.fallbacks and self.model.startswith(FALLBACK_MODELS):
            response = self.client.beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **kwargs)
        else:
            response = self.client.messages.create(**kwargs)

        if response.stop_reason == "refusal":
            category = getattr(getattr(response, "stop_details", None), "category", None)
            raise ProviderError(f"model declined the request (category={category})")
        if response.stop_reason == "max_tokens":
            raise ProviderError("response truncated at max_tokens; raise max_tokens in dakc.toml")
        text = "".join(block.text for block in response.content if block.type == "text")
        return Completion(text, response.usage.input_tokens, response.usage.output_tokens)
