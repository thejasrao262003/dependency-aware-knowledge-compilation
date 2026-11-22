# MCP server

`dakc mcp` serves one workspace to AI agents over the
[Model Context Protocol](https://modelcontextprotocol.io). It is built on the
official MCP Python SDK (2.x) and is a thin layer over the
[Python API](python-api.md).

## Setup

The server needs an existing workspace. Agents cannot create one yet. Build
it once from your documents:

```bash
pip install 'dakc[mcp]'
cd /path/to/workspace && dakc ingest ./docs   # creates dakc.toml and compiles ./docs
```

Then register it. In Claude Code:

```bash
claude mcp add dakc -- dakc mcp --workspace /absolute/path/to/workspace
# or, without installing anything first (uv fetches DAKC on demand):
claude mcp add dakc -- uvx --from 'dakc[mcp]' dakc mcp --workspace /absolute/path/to/workspace
```

Other clients take the usual JSON configuration:

```json
{ "mcpServers": { "dakc": { "command": "dakc", "args": ["mcp", "--workspace", "/absolute/path/to/workspace"] } } }
```

Options:

```bash
dakc mcp --workspace ... --read-only                        # inspection tools only
dakc mcp --workspace ... --max-llm-calls 500                # raise the cost guard (default 100)
dakc mcp --workspace ... --transport streamable-http        # HTTP on 127.0.0.1:8766/mcp (this machine only)
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
- **Local-only HTTP.** There is no authentication yet, so `--transport
  streamable-http` refuses to bind to anything but 127.0.0.1, localhost or
  ::1. Requests whose `Host` header is not local are rejected (HTTP 421),
  which blocks DNS-rebinding attacks from web pages. A read-only server
  still exposes everything in the knowledge base. The stdio transport has
  no network surface at all.
- **Same locking as everywhere else.** A compile waits up to 5 s for a
  running build (from the CLI, `dakc watch` or another agent), then fails with
  a clear message. Read tools never block on a build.

## Tested with real agents

The server was exercised by headless Claude Code agents (Sonnet) that could see only the DAKC tools. They worked against
a knowledge base built from this repository's `research/` folder. Each task was phrased as a user would phrase it:

- a concept question with no page of its own;
- a question about one audited project, with citations;
- the impact of editing a file, named by its repository path;
- whether a fact is still accurate after its document was edited;
- "rebuild after edits".

Every task succeeded. The traces exposed five problems, all fixed:

| Problem | Fix |
|---|---|
| A path such as `research/TECHNICAL_GAP.md` did not resolve: the source is `TECHNICAL_GAP.md` | Unambiguous suffix paths resolve; errors suggest the closest paths and titles |
| `read_artifact` on a source returned no text, so an agent could not check an edited document and repeated an outdated fact | Sources return their current text on disk, with `changed_since_build` |
| Passages matched a query only in their first 80 characters | Claims and passages match on their full text |
| `status` said about 160 pages were stale; the rebuild changed none (the edit touched no extracted fact) | `stale_note`: the count is an upper bound, and a compile preview gives the exact rebuild |
| Markdown tables were parsed as one paragraph, so a claim taken from a table row lost its column headers, and an agent attributed a value to the wrong project | Tables are parsed into one block per row, each value labelled with its column header (parser version `parse/2`) |

After the fixes:

- the impact task took 2 turns instead of 4;
- the "still accurate?" task found the exact outdated value (2 s in the knowledge base, 5 s in the edited document) where before it could only guess;
- the project question gave the correct table value with its source.

## Known limitations

| Limitation | Effect | Workaround |
|---|---|---|
| No workspace creation | An agent cannot start a knowledge base from a folder | Run `dakc ingest DIR` once, then register the server |
| No progress reporting | A long `compile(apply=true)` with an LLM provider is a single call that can take minutes; some clients time out | Preview first. Run large first builds with `dakc compile` in a terminal, and leave incremental builds to the agent. |
| Search is substring, not semantic | Pages match on their title; claims and passages on their full text. Synonyms and paraphrases are not found. | Agents should try several phrasings, and search claims and passages for concepts |
| "Stale" is an upper bound | `status` and `explain` count everything downstream of a changed document | `compile` without `apply` gives the exact rebuild |
| No authentication on HTTP | HTTP is local-only (see *Safety rules*) | Use stdio, which is what local agents use anyway |
| Compile results are not yet a structured change list | The report lists operations and ids; an agent has to interpret them to explain "what changed" | Use `diff_builds` after a build |
| No MCP resources or prompts | Clients cannot browse pages as resources | Use `list_artifacts` and `read_artifact` |
| Quality depends on the extractor | The default heuristic extractor makes noisy entity pages (many single-fact pages, some junk names). It quotes source sentences verbatim, so its claims stay grounded. | Configure an LLM provider for cleaner pages |

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
