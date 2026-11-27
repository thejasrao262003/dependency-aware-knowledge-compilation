"""Claude through the Claude Code CLI: no API key, the person's own Claude Code login.

``provider = "claude-code"`` runs ``claude -p`` once per model call, with the model
chosen in ``[provider.claude-code] model`` (``haiku``, ``sonnet``, ``opus`` or a full
model id). It uses whatever login Claude Code has: a Claude subscription, or an API
key it is configured with.

Each call runs in an empty scratch directory with tools, MCP servers and settings off
(``--tools ""``, ``--strict-mcp-config``, ``--setting-sources ""``), so no project's
CLAUDE.md, hooks or servers are loaded. DAKC never reads or stores credentials.

Measured with Claude Code 2.1.312 and ``haiku`` (late 2026): about 3 to 4 seconds per
call, of which 1 to 2 seconds is the model, and about 950 tokens of Claude Code's own
request overhead (cached between calls) on top of DAKC's prompt. Fine for incremental
compiles, which make a few to a few dozen calls. A first build of a large corpus makes
thousands, and Claude subscriptions' limits assume ordinary individual use: use an
API-key provider for that.

A failed call raises :class:`ProviderError`. Its ``transient`` is True when asking again
later may work: no answer within ``timeout``, or an API error that is a rate or usage
limit, a server error, a timeout or a lost connection. Claude Code reports the API's
HTTP status as ``api_error_status`` (2.1.312); without it, the error text is read.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from dakc.providers.llm import Completion, ProviderError, is_transient_status

LARGE_BUILD_CALLS = 200  # above this, a compile notes that an API-key provider suits it better
# What Claude Code says when the API call behind it failed in a way that may pass
_API_STATUS = re.compile(r"\bAPI Error: (\d{3})\b")
_TRANSIENT_TEXT = re.compile(r"rate.?limit|usage limit reached|overloaded|timed out|connection error|"
                             r"connection closed|no response from api", re.I)


class ClaudeCodeClient:
    def __init__(self, model: str = "haiku", binary: str = "claude", timeout: float = 300.0, retries: int = 2):
        self.model = model
        self.binary = binary
        self.timeout = timeout
        self.retries = retries
        self.fingerprint = f"claude-code:{model}"
        self._workdir: str | None = None

    def _command(self, system: str, json_schema: dict | None) -> list[str]:
        exe = shutil.which(self.binary) or (self.binary if Path(self.binary).is_file() else None)
        if exe is None:
            raise ProviderError(f"Claude Code CLI not found ({self.binary!r}): install Claude Code and log in, "
                                "or set [provider.claude-code] binary = \"/path/to/claude\"")
        cmd = [exe, "-p", "--model", self.model, "--system-prompt", system, "--tools", "", "--output-format", "json",
               "--no-session-persistence", "--strict-mcp-config", "--setting-sources", ""]
        if json_schema is not None:
            cmd += ["--json-schema", json.dumps(json_schema, separators=(",", ":"))]
        return cmd

    def complete(self, system: str, user: str, json_schema: dict | None = None) -> Completion:
        if self._workdir is None:  # an empty directory: no project settings, CLAUDE.md, hooks or MCP servers
            self._workdir = tempfile.mkdtemp(prefix="dakc-claude-code-")
        cmd = self._command(system, json_schema)
        last, transient = "", False
        for attempt in range(self.retries + 1):
            try:
                proc = subprocess.run(cmd, input=user, capture_output=True, text=True, cwd=self._workdir,
                                      timeout=self.timeout)
            except subprocess.TimeoutExpired:
                last, transient = f"no answer within {self.timeout:.0f} s", True
            else:
                try:
                    data = json.loads(proc.stdout)
                except json.JSONDecodeError:
                    data = None
                if isinstance(data, dict) and not data.get("is_error") and data.get("subtype") == "success":
                    usage = data.get("usage") or {}
                    tokens_in = sum(int(usage.get(k) or 0) for k in
                                    ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
                    structured = data.get("structured_output")
                    text = json.dumps(structured) if json_schema is not None and structured is not None else str(data.get("result") or "")
                    return Completion(text, tokens_in, int(usage.get("output_tokens") or 0))
                detail = (data or {}).get("result") if isinstance(data, dict) else None
                full = str(detail or proc.stderr.strip() or proc.stdout.strip() or f"exit status {proc.returncode}")
                last, transient = full[:300], _transient(data, full)
                if isinstance(data, dict) and data.get("subtype") not in (None, "success", "error_during_execution"):
                    transient = False
                    break  # e.g. error_max_structured_output_retries: asking again will not help
            if attempt < self.retries:
                time.sleep(2 ** attempt)
        raise ProviderError(f"Claude Code call failed: {last}", transient=transient)


def _transient(data: object, text: str) -> bool:
    """Whether a failed ``claude -p`` may succeed later: by the API error's HTTP status
    when Claude Code reports one, otherwise by what its message says."""
    status = data.get("api_error_status") if isinstance(data, dict) else None
    if isinstance(status, int) and not isinstance(status, bool):
        return is_transient_status(status)
    if m := _API_STATUS.search(text):
        return is_transient_status(int(m.group(1)))
    return bool(_TRANSIENT_TEXT.search(text))
