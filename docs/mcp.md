# MCP server

`dakc mcp` serves one workspace to AI agents over the
[Model Context Protocol](https://modelcontextprotocol.io). It is built on the
official MCP Python SDK (2.x) and is a thin layer over the
[Python API](python-api.md).

```bash
pip install 'dakc[mcp]'
dakc mcp --workspace /absolute/path/to/workspace            # stdio (local agents)
dakc mcp --workspace ... --transport streamable-http        # HTTP on 127.0.0.1:8766/mcp
dakc mcp --workspace ... --read-only                        # inspection tools only
dakc mcp --workspace ... --max-llm-calls 500                # raise the cost guard
```

Claude Code: `claude mcp add dakc -- dakc mcp --workspace /absolute/path/to/workspace`.

Other clients:

```json
{ "mcpServers": { "dakc": { "command": "dakc", "args": ["mcp", "--workspace", "/absolute/path/to/workspace"] } } }
```

## Tools

| Tool | Annotations | What it returns |
|---|---|---|
| `status` | read-only | HEAD, counts, whether sources changed, stale artifacts per kind |
| `check_sources` | read-only | new / modified / deleted / renamed documents |
| `list_artifacts` | read-only | artifacts by kind (default `page`) and title substring |
| `read_artifact` | read-only | rendered Markdown of a page, summary or the index, with `[^Cn]` citation markers, or the text of a claim or passage; `max_chars` truncates |
| `explain` | read-only | dependencies, dependents, upstream sources, VALID / STALE with reasons |
| `trace` | read-only | each page sentence, the claims it cites, and each claim's source document, version and evidence sentences |
| `downstream` | read-only | everything an edit to a document would invalidate |
| `graph_stats` | read-only | artifact and edge counts |
| `builds` | read-only | build history with LLM calls and operation counts |
| `diff_builds` | read-only | artifacts created / updated / deleted between two builds |
| `duplicates` | read-only | likely duplicate claims (advisory) |
| `verify` | read-only | invariants. `against_full` (a full-rebuild comparison) is allowed only with a free provider. |
| `compile` | not read-only, not destructive, idempotent | a preview by default; builds with `apply=true` |
| `rollback` | destructive | restores an explicit earlier build |

The server sends instructions on connect. They tell the agent how to:

- answer a question (`list_artifacts` → `read_artifact` → `trace`, citing sources);
- see an edit's impact before making it (`downstream`);
- rebuild after documents change (`status` → `compile` preview → `compile(apply=true)`).

## Safety rules

- **Bound to one workspace.** Tools take artifact ids and paths *within* the
  knowledge base. An agent cannot point the server at another directory.
- **Preview before build.** `compile` without `apply=true` returns the plan:
  what would be re-extracted and regenerated, and how many LLM calls it would
  make. Nothing is written or spent.
- **Cost guard.** With an LLM provider, `compile(apply=true)` plans first and
  refuses a build planned to exceed `--max-llm-calls` (default 100). The plan
  is an upper bound, so the guard is conservative. `verify(against_full=true)`,
  which re-pays every call, is refused over MCP with a paid provider.
- **Explicit destruction.** `rollback` requires a build id and is annotated
  `destructiveHint`, so clients that confirm destructive tools will ask the
  user. `--read-only` removes `compile` and `rollback` entirely.
- **Bounded outputs.** Lists are capped by `limit` (and text by `max_chars`),
  and totals are always reported (`*_total`, `more_available`). A hub page or
  a large build cannot flood the agent's context.
- **Same locking as everywhere else.** A compile waits up to 5 s for a
  running build (from the CLI, `dakc watch` or another agent), then fails with
  a clear message. Read tools never block on a build.

## Errors

Anticipated failures reach the agent as tool errors with their message, for
example `no source or artifact matches 'page:nope'` or `build 7 is not a
committed build`. Anything unexpected is reported only as `Error executing
tool <name>`; the traceback goes to the server's log on stderr, not to the
model.

## Testing

`tests/integration/test_mcp_server.py` drives the server through the official
MCP client, in-process and as a real stdio subprocess. It covers:

- the tool list and annotations;
- the preview → apply → rollback flow;
- tool errors and bounded outputs;
- the LLM cost guard;
- read-only mode.
