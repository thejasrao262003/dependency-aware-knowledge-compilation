"""Claude provider using the official Anthropic Python SDK (optional extra).

Install with ``pip install 'dakc[anthropic]'``. Credentials are resolved by the
SDK (``ANTHROPIC_API_KEY``, ``ANTHROPIC_AUTH_TOKEN`` or an ``ant auth login``
profile), unless ``api_key_env`` names another environment variable that is set:
then its value is the API key. The key is passed to the SDK and never stored,
logged or put in an error message.

Notes for current models:
* ``temperature`` is not sent: it is rejected on current models and API
  versions. Outputs are therefore not reproducible; DAKC relies on its
  content-addressed extraction cache and signatures, not on model determinism.
* Extraction uses structured outputs (``output_config.format``) so the
  response is valid JSON.
* Server-side refusal fallbacks are enabled by default (``fallbacks="default"``);
  a request the whole chain refuses raises :class:`ProviderError`.
* The SDK retries rate limits, server errors and dropped connections itself. What
  still fails is raised as :class:`ProviderError`, with ``transient`` set for a rate
  limit, a server error, a timeout or a connection failure.
"""

from __future__ import annotations

import os
import sys

from dakc.providers.llm import Completion, ProviderError, is_transient_status

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
        api_key_env: str | None = None,  # last, so positional calls of 0.7 keep their meaning
    ):
        if client is None:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover - depends on optional extra
                raise ProviderError("the Claude provider needs the 'anthropic' extra: pip install 'dakc[anthropic]'") from exc
            key = os.environ.get(api_key_env, "") if api_key_env else ""
            # unset or empty: the SDK's own resolution, exactly as without api_key_env
            client = anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()
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

        try:
            if self.fallbacks and self.model.startswith(FALLBACK_MODELS):
                response = self.client.beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **kwargs)
            else:
                response = self.client.messages.create(**kwargs)
        except Exception as exc:
            transient = _transient(exc)
            if transient is None:
                raise  # not an API error (a bug, an interrupted process): let it show as it is
            raise ProviderError(f"Claude API call failed: {str(exc)[:300]}", transient=transient) from exc

        if response.stop_reason == "refusal":
            category = getattr(getattr(response, "stop_details", None), "category", None)
            raise ProviderError(f"model declined the request (category={category})")
        if response.stop_reason == "max_tokens":
            raise ProviderError("response truncated at max_tokens; raise max_tokens in dakc.toml")
        text = "".join(block.text for block in response.content if block.type == "text")
        return Completion(text, response.usage.input_tokens, response.usage.output_tokens)


def _transient(exc: BaseException) -> bool | None:
    """Whether an exception of the ``anthropic`` SDK is worth retrying later, or None
    when it is not an SDK API error. The SDK has already retried what it could."""
    sdk = sys.modules.get("anthropic")  # imported by whoever built the SDK client
    connection, status, api = (getattr(sdk, n, ()) for n in ("APIConnectionError", "APIStatusError", "APIError"))
    if isinstance(exc, connection):  # includes APITimeoutError
        return True
    if isinstance(exc, status):
        return is_transient_status(int(exc.status_code))
    if isinstance(exc, api):  # e.g. a response that does not match the SDK's types
        return False
    return None
