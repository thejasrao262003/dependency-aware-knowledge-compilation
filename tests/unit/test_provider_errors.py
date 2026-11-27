"""ProviderError.transient: True when asking again later may succeed (a rate limit or server error
that outlasted the retries, a timeout, a failed connection), False otherwise.

The OpenAI-compatible client is checked against a real HTTP server on the loopback interface, so
timeouts and refused connections are real ones; the anthropic SDK and `claude -p` are fakes.
Nothing here reaches a network or a model.
"""

from __future__ import annotations

import json
import pickle
import socket
import subprocess
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from dakc.providers import ProviderError, make_client
from dakc.providers.anthropic_provider import AnthropicClient
from dakc.providers.claude_code import ClaudeCodeClient
from dakc.providers.llm import OpenAICompatibleClient, is_transient_status

OK = {"choices": [{"message": {"content": "fine"}, "finish_reason": "stop"}],
      "usage": {"prompt_tokens": 3, "completion_tokens": 2}}
HANG = "hang"  # a scripted answer that never comes


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)  # the stub server waits on events, not sleep


def test_the_flag_is_keyword_only_false_by_default_and_survives_pickling():
    assert ProviderError("x").transient is False
    assert ProviderError("x", "detail").transient is False  # positional arguments mean what they always did
    err = ProviderError("rate limited", transient=True)
    assert str(err) == "rate limited" and isinstance(err, RuntimeError)
    assert pickle.loads(pickle.dumps(err)).transient is True


@pytest.mark.parametrize("status,transient", [(408, True), (429, True), (500, True), (503, True), (529, True),
                                              (400, False), (401, False), (403, False), (404, False), (409, False),
                                              (422, False)])
def test_which_http_statuses_are_transient(status, transient):
    assert is_transient_status(status) is transient


# --------------------------------------------------------------------------- #
# OpenAI-compatible client against a loopback stub server
# --------------------------------------------------------------------------- #


class StubServer:
    """``/v1/chat/completions`` on 127.0.0.1, answering POSTs from a script of (status, body)
    or HANG; the last entry repeats."""

    def __init__(self, *script):
        self.script, self.requests = list(script), 0
        self.release = threading.Event()
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                answer = stub.script[min(stub.requests, len(stub.script) - 1)]
                stub.requests += 1
                if answer == HANG:
                    stub.release.wait(10)  # the client gives up first
                    return
                status, body = answer
                data = (body if isinstance(body, str) else json.dumps(body)).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def __enter__(self):
        threading.Thread(target=self.server.serve_forever, args=(0.02,), daemon=True).start()  # quick shutdown
        return self

    def __exit__(self, *exc):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()


def client(base_url: str, **kw) -> OpenAICompatibleClient:
    return OpenAICompatibleClient(base_url=base_url, model="m", retries=3, **kw)


@pytest.mark.parametrize("status", [429, 500, 503, 408])
def test_a_rate_limit_or_server_error_that_outlasts_the_retries_is_transient(status):
    with StubServer((status, "try later")) as stub, pytest.raises(ProviderError, match="failed after 3 attempts") as err:
        client(stub.base_url).complete("s", "u")
    assert err.value.transient is True and stub.requests == 3
    assert f"HTTP {status}" in str(err.value)


def test_a_server_error_that_passes_is_retried_away():
    with StubServer((503, "busy"), (429, "slow down"), (200, OK)) as stub:
        assert client(stub.base_url).complete("s", "u").text == "fine"
    assert stub.requests == 3


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_a_rejected_request_is_a_provider_error_and_is_not_retried(status):
    """Before 0.8, httpx.HTTPStatusError escaped; pytest.raises(ProviderError) would not catch it."""
    with StubServer((status, '{"error": "no"}')) as stub, pytest.raises(ProviderError, match=f"HTTP {status} from") as err:
        client(stub.base_url).complete("s", "u")
    assert err.value.transient is False and stub.requests == 1


def test_a_timeout_is_transient():
    with StubServer(HANG) as stub, pytest.raises(ProviderError, match="failed after 2 attempts") as err:
        OpenAICompatibleClient(base_url=stub.base_url, model="m", retries=2, timeout=0.2).complete("s", "u")
    assert err.value.transient is True and stub.requests == 2
    assert "timed out" in str(err.value).lower()


def test_a_refused_connection_is_transient():
    with socket.socket() as s:  # a loopback port with nothing listening
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with pytest.raises(ProviderError, match="failed after 3 attempts") as err:
        client(f"http://127.0.0.1:{port}/v1").complete("s", "u")
    assert err.value.transient is True


@pytest.mark.parametrize("body", ["<html>gateway</html>", {"choices": []}, {"choices": [{"message": {}}]}])
def test_a_malformed_answer_is_a_provider_error(body):
    with StubServer((200, body)) as stub, pytest.raises(ProviderError, match="unexpected response") as err:
        client(stub.base_url).complete("s", "u")
    assert err.value.transient is False and stub.requests == 1


def test_truncation_a_missing_key_and_a_bad_url_are_not_transient(monkeypatch):
    length = {"choices": [{"message": {"content": "part"}, "finish_reason": "length"}], "usage": {}}
    with StubServer((200, length)) as stub, pytest.raises(ProviderError, match="truncated") as err:
        client(stub.base_url).complete("s", "u")
    assert err.value.transient is False
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(ProviderError, match="GEMINI_API_KEY is not set") as err:
        make_client("gemini").complete("s", "u")
    assert err.value.transient is False
    with pytest.raises(ProviderError, match="cannot send a request") as err:
        client("ftp://127.0.0.1/v1").complete("s", "u")
    assert err.value.transient is False


# --------------------------------------------------------------------------- #
# anthropic: a fake SDK with the SDK's exception hierarchy
# --------------------------------------------------------------------------- #


def fake_sdk() -> types.ModuleType:
    sdk = types.ModuleType("anthropic")

    class APIError(Exception):
        pass

    class APIConnectionError(APIError):
        pass

    class APITimeoutError(APIConnectionError):
        pass

    class APIStatusError(APIError):
        def __init__(self, message, status_code):
            super().__init__(message)
            self.status_code = status_code

    for cls in (APIError, APIConnectionError, APITimeoutError, APIStatusError):
        setattr(sdk, cls.__name__, cls)
    return sdk


class Raising:
    """A fake SDK client whose every call raises ``exc``."""

    def __init__(self, exc: BaseException):
        def create(**kw):
            raise exc

        self.messages = types.SimpleNamespace(create=create)
        self.beta = types.SimpleNamespace(messages=self.messages)


SDK = fake_sdk()


@pytest.mark.parametrize("exc,transient", [
    (SDK.APIStatusError("Error code: 429 - rate_limit_error", 429), True),
    (SDK.APIStatusError("Error code: 500 - api_error", 500), True),
    (SDK.APIStatusError("Error code: 529 - overloaded_error", 529), True),
    (SDK.APITimeoutError("Request timed out."), True),
    (SDK.APIConnectionError("Connection error."), True),
    (SDK.APIStatusError("Error code: 400 - invalid_request_error", 400), False),
    (SDK.APIStatusError("Error code: 401 - authentication_error", 401), False),
    (SDK.APIError("response did not match the schema"), False),
])
@pytest.mark.parametrize("model", ["claude-opus-5-5", "claude-haiku-5-5"])  # with and without refusal fallbacks
def test_anthropic_errors_are_classified(exc, transient, model, monkeypatch):
    monkeypatch.setitem(sys.modules, "anthropic", SDK)
    with pytest.raises(ProviderError, match="Claude API call failed") as err:
        AnthropicClient(model=model, client=Raising(exc)).complete("s", "u")
    assert err.value.transient is transient and err.value.__cause__ is exc


def test_anthropic_errors_that_are_not_the_apis_pass_through(monkeypatch):
    monkeypatch.setitem(sys.modules, "anthropic", SDK)
    with pytest.raises(KeyError):
        AnthropicClient(client=Raising(KeyError("a bug"))).complete("s", "u")


def test_anthropic_refusal_and_truncation_are_not_transient():
    from tests.unit.test_providers import FakeAnthropic

    for stop in ("refusal", "max_tokens"):
        with pytest.raises(ProviderError) as err:
            AnthropicClient(client=FakeAnthropic(stop_reason=stop)).complete("s", "u")
        assert err.value.transient is False


# --------------------------------------------------------------------------- #
# claude-code: a fake `claude -p`
# --------------------------------------------------------------------------- #


def fake_claude(monkeypatch, *answers) -> list[list[str]]:
    """Replace ``subprocess.run`` for ClaudeCodeClient: each answer is a JSON result, a
    (returncode, stdout, stderr) tuple, or HANG; the last repeats. Returns the commands run."""
    commands: list[list[str]] = []

    def run(cmd, **kw):
        commands.append(cmd)
        answer = answers[min(len(commands), len(answers)) - 1]
        if answer == HANG:
            raise subprocess.TimeoutExpired(cmd, kw["timeout"])
        if isinstance(answer, tuple):
            return subprocess.CompletedProcess(cmd, answer[0], answer[1], answer[2])
        return subprocess.CompletedProcess(cmd, 0 if not answer.get("is_error") else 1, json.dumps(answer), "")

    monkeypatch.setattr("dakc.providers.claude_code.subprocess.run", run)
    return commands


def cc(**kw) -> ClaudeCodeClient:
    return ClaudeCodeClient(binary=sys.executable, **kw)  # any existing file passes the "is it installed" check


def api_error(text: str, status: int | None) -> dict:
    """What Claude Code 2.1.312 prints when the API call behind `claude -p` fails."""
    return {"type": "result", "subtype": "success", "is_error": True, "result": text, "api_error_status": status}


SUCCESS = {"type": "result", "subtype": "success", "is_error": False, "result": "fine",
           "usage": {"input_tokens": 1, "output_tokens": 1}}


def test_claude_code_timing_out_is_transient(monkeypatch):
    commands = fake_claude(monkeypatch, HANG)
    with pytest.raises(ProviderError, match="no answer within 5 s") as err:
        cc(timeout=5, retries=1).complete("s", "u")
    assert err.value.transient is True and len(commands) == 2


@pytest.mark.parametrize("answer,transient", [
    (api_error('API Error: 529 {"type":"error","error":{"type":"overloaded_error"}}', 529), True),
    (api_error('API Error: 429 {"type":"error","error":{"type":"rate_limit_error"}}', 429), True),
    (api_error("API Error: 500 Internal server error", 500), True),
    (api_error("API Error: Request timed out.", None), True),
    (api_error("API Error: Connection error.", None), True),
    (api_error("Claude AI usage limit reached|1764000000", None), True),
    (api_error("API Error: 401 Invalid API key · Please run /login", 401), False),
    (api_error("API Error: 400 messages: text content blocks must be non-empty", 400), False),
    ({"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "rate limited"}, True),
    ({"type": "result", "subtype": "error_during_execution", "is_error": True, "errors": ["unexpected state"]}, False),
    ((1, "", "Error: unknown option '--json-schema'"), False),
])
def test_claude_code_api_errors_are_classified(answer, transient, monkeypatch):
    commands = fake_claude(monkeypatch, answer)
    with pytest.raises(ProviderError, match="Claude Code call failed") as err:
        cc(retries=1).complete("s", "u")
    assert err.value.transient is transient
    assert len(commands) == 2  # retried as before: the classification changes what is raised, not the retries


def test_claude_code_the_status_wins_over_the_words(monkeypatch):
    fake_claude(monkeypatch, api_error("API Error: Request timed out.", 400))
    with pytest.raises(ProviderError) as err:
        cc(retries=0).complete("s", "u")
    assert err.value.transient is False


def test_claude_code_a_final_error_is_not_transient_and_not_retried(monkeypatch):
    commands = fake_claude(monkeypatch, {"type": "result", "subtype": "error_max_structured_output_retries",
                                         "is_error": True, "errors": ["the API timed out once"]})
    with pytest.raises(ProviderError) as err:
        cc(retries=2).complete("s", "u")
    assert err.value.transient is False and len(commands) == 1


def test_claude_code_the_last_failure_decides(monkeypatch):
    fake_claude(monkeypatch, HANG, api_error("API Error: 401 Invalid API key", 401))
    with pytest.raises(ProviderError, match="401") as err:
        cc(retries=1).complete("s", "u")
    assert err.value.transient is False
    fake_claude(monkeypatch, api_error("API Error: 529 overloaded", 529), SUCCESS)
    assert cc(retries=1).complete("s", "u").text == "fine"


def test_claude_code_not_installed_is_not_transient(tmp_path):
    with pytest.raises(ProviderError, match="CLI not found") as err:
        ClaudeCodeClient(binary=str(tmp_path / "no-claude")).complete("s", "u")
    assert err.value.transient is False
