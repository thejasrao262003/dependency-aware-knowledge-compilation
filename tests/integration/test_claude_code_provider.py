"""provider = "claude-code": DAKC's model calls through the Claude Code CLI, here a fake `claude`
that answers like `claude -p --output-format json`. Checks what DAKC sends (tools, MCP servers
and settings off, an empty working directory), that a whole compile works and is cached like
any other provider's, and that failures and large builds are reported plainly."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

import dakc
from dakc.errors import CompileFailed

from tests.conftest import write_files

FAKE = r'''#!{python}
import json, os, re, sys
args = sys.argv[1:]
prompt = sys.stdin.read()
with open(os.environ["FAKE_CLAUDE_LOG"], "a") as log:
    log.write(json.dumps({{"args": args, "cwd": os.getcwd(), "cwd_files": os.listdir(".")}}) + "\n")
if os.environ.get("FAKE_CLAUDE_FAIL"):
    print(json.dumps({{"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "rate limited"}}))
    sys.exit(1)
schema = json.loads(args[args.index("--json-schema") + 1]) if "--json-schema" in args else None
out = {{"type": "result", "subtype": "success", "is_error": False,
        "usage": {{"input_tokens": 3, "cache_creation_input_tokens": 950, "cache_read_input_tokens": 0, "output_tokens": 40}}}}
if schema and "claims" in schema["properties"]:
    want = re.search(r"by sentence\(s\) ([\d, ]+)", prompt)
    want = {{int(x) for x in want.group(1).split(",")}} if want else None
    claims = []
    for n, sentence in re.findall(r"^\[(\d+)\] (.+)$", prompt, re.M):
        names = re.findall(r"\b[A-Z][A-Za-z0-9]+", sentence)
        if names and (want is None or int(n) in want):
            claims.append({{"text": sentence, "entities": names, "sentences": [int(n)]}})
    out["structured_output"] = {{"claims": claims}}
    out["result"] = json.dumps(out["structured_output"])
elif schema:
    out["structured_output"] = {{"verdicts": []}}
    out["result"] = "{{}}"
else:
    refs = re.findall(r"^\[(C\d+)\] (.+)$", prompt, re.M)
    out["result"] = " ".join(f"{{text}} [^{{ref}}]" for ref, text in refs) or "An overview."
print(json.dumps(out))
'''


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    path = tmp_path / "bin" / "claude"
    path.parent.mkdir()
    path.write_text(FAKE.format(python=sys.executable), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    return path, log


def workspace(root: Path, binary: Path, extra: str = "") -> Path:
    write_files(root / "knowledge", {"db.md": "# Database\n\nPostgreSQL supports JSONB. Redis caches Sessions.\n",
                                     "ops.md": "# Ops\n\nKafka runs on three nodes.\n"})
    (root / "dakc.toml").write_text(
        '[dakc]\nsource_dir = "knowledge"\nprovider = "claude-code"\njobs = 2\n\n'
        f'[provider.claude-code]\nmodel = "sonnet"\nbinary = "{binary}"\nretries = 0\n{extra}', encoding="utf-8")
    return root


def calls(log: Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def test_a_compile_through_claude_code(tmp_path, fake_claude):
    binary, log = fake_claude
    ws = workspace(tmp_path / "ws", binary)
    with dakc.open(ws) as kb:
        rep = kb.compile()
        assert rep.status == "committed" and rep.usage.llm_calls == len(calls(log)) > 0
        assert rep.usage.input_tokens >= 953 * rep.usage.llm_calls  # Claude Code's own overhead is counted
        assert {"page:postgresql", "page:kafka"} <= {a.id for a in kb.artifacts("page")}
        assert kb.verify().ok
        n = len(calls(log))
        assert kb.compile().status == "noop" and len(calls(log)) == n  # cached like any provider
    for c in calls(log):
        args = c["args"]
        assert args[args.index("--model") + 1] == "sonnet"
        for flag, value in (("--tools", ""), ("--setting-sources", ""), ("--output-format", "json")):
            assert args[args.index(flag) + 1] == value
        assert "--strict-mcp-config" in args and "--no-session-persistence" in args
        assert c["cwd_files"] == [] and "dakc-claude-code-" in c["cwd"]  # no project's CLAUDE.md, hooks or servers


def test_a_failed_call_says_what_happened(tmp_path, fake_claude, monkeypatch):
    binary, _ = fake_claude
    monkeypatch.setenv("FAKE_CLAUDE_FAIL", "1")
    ws = workspace(tmp_path / "ws", binary)
    with dakc.open(ws) as kb, pytest.raises(CompileFailed, match="Claude Code call failed: rate limited") as err:
        kb.compile()
    assert err.value.preserved  # nothing was committed


def test_no_claude_code_installed(tmp_path):
    ws = workspace(tmp_path / "ws", tmp_path / "missing" / "claude")
    with dakc.open(ws) as kb, pytest.raises(CompileFailed, match="Claude Code CLI not found"):
        kb.compile()


def test_a_large_build_suggests_an_api_key(tmp_path, fake_claude, monkeypatch):
    import dakc.providers.claude_code as cc

    binary, _ = fake_claude
    monkeypatch.setattr(cc, "LARGE_BUILD_CALLS", 1)
    ws = workspace(tmp_path / "ws", binary)
    with dakc.open(ws) as kb:
        plan = kb.plan()
        assert any("calls through Claude Code" in n for n in plan.notes)


def test_the_default_model_is_haiku():
    from dakc.providers import make_provider

    assert make_provider("claude-code").extractor.client.fingerprint == "claude-code:haiku"
    assert os.environ.get("FAKE_CLAUDE_FAIL") is None
