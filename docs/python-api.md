# Python API

`import dakc` gives the whole compiler as a library. The CLI (`dakc ...`), the
REST API (`dakc serve`) and the MCP server (`dakc mcp`) are thin layers over
the same module, `dakc.kb`. A behaviour reachable from one is reachable from
all of them.

## Opening a knowledge base

```python
import dakc

kb = dakc.open("path/to/workspace")       # needs dakc.toml; raises dakc.WorkspaceNotFound
kb = dakc.init("path", source_dir="docs") # creates dakc.toml if missing (an existing one is kept)
report = dakc.ingest("path/docs", "path") # init for that directory if new, then compile
```

`KnowledgeBase` is a context manager. It holds one SQLite connection, which
`close()` or the end of a `with` block releases. Use one instance per thread.
Instances are cheap to open.

## Methods

| Method | Returns | Notes |
|---|---|---|
| `status()` | `Status` | `head`, `counts`, `up_to_date`, `source_changes`, `stale` (artifacts per kind downstream of changed sources) |
| `check(root=None)` | `SourceChanges` | new / modified / deleted / renamed / unchanged, by content hash |
| `plan(full=False)` | `BuildReport` | Dry run. Nothing is written or spent. `has_work()` and `llm_calls()` tell you whether a compile is needed and what it costs. Exact for free providers; an upper bound for LLM providers (`report.exact`). |
| `compile(full=False, wait=0.0)` | `BuildReport` | Takes the build lock, commits atomically, updates the wiki files. Raises `BuildLocked` or `CompileFailed`. |
| `artifacts(kind=None, query=None, limit=100)` | `list[ArtifactSummary]` | Ordered by layer, then id. `query` is a case-insensitive substring of the title, or of the full text for claims and passages |
| `read(artifact)` | `ArtifactContent` | Rendered Markdown for pages, summaries and the index. Text for claims and passages. For a source document, its current text on disk, with `metadata['changed_since_build']`. |
| `explain(artifact)` | `Explanation` | Dependencies, dependents, upstream sources, signatures, history, `VALID` / `STALE` / `DELETED` with reasons. `STALE` means a source upstream changed. That is an upper bound: `plan()` shows whether the artifact actually changes. |
| `trace(artifact)` | `PageTrace` or `ClaimTrace` | Each page sentence, the claims it cites, and each claim's passage, source version and evidence sentences |
| `downstream(artifact)` | `Downstream` | Everything an edit to this source or artifact would invalidate, by kind |
| `graph_stats()` | `dict` | Artifact and edge counts |
| `export_graph(fmt="json", around=None)` | `str` | `json` (node-link), `dot`, or `reactflow` |
| `builds(limit=20)` | `list[BuildRecord]` | Newest first: committed, noop, failed, rolled_back |
| `diff(a, b=None)` | `BuildDiff` | Artifacts created / updated / deleted between two builds (`b` defaults to HEAD) |
| `duplicates(limit=None)` | `list[DuplicatePair]` | Advisory. Claims are never merged. |
| `verify(against_full=False, allow_cost=False)` | `VerifyReport` | Invariants. `against_full` also compares with a full rebuild; with an LLM provider it needs `allow_cost=True` (`CostNotAllowed` otherwise). |
| `rollback(to=None, wait=0.0)` | `RollbackResult` | Takes the build lock. Destructive: later artifact versions are discarded, though their build records remain. |

`artifact` may be:

- an id (`page:postgresql`, `clm:…`, `sum:databases`);
- a friendly form (`page:PostgreSQL`, `entity_postgresql`);
- a source path (`databases/postgres.md`);
- a page title (`PostgreSQL`).

A path written relative to another directory (`research/notes.md` for the source `notes.md`) resolves when exactly one source matches it. When nothing matches, `ArtifactNotFound` suggests the closest source paths and page titles.

Every result has `to_dict()`, which returns JSON-safe data. That is what
`--json`, the REST API and the MCP tools emit. `Downstream.to_dict` and
`BuildDiff.to_dict` take a `limit`.

## Errors

All errors derive from `dakc.DakcError`. Each one also derives from the
matching built-in, so existing `except` clauses keep working.

| Error | Also a | Raised when |
|---|---|---|
| `WorkspaceNotFound` | `FileNotFoundError` | no `dakc.toml` |
| `ArtifactNotFound` | `LookupError` | no artifact or source matches |
| `InvalidArgument` | `ValueError` | unknown strategy, kind or format; `trace` on something that is not a page or claim; a missing build |
| `BuildLocked` | `RuntimeError` | another process is compiling or rolling back (pass `wait=` to queue) |
| `CompileFailed` | `RuntimeError` | a build failed. Nothing was committed. `.preserved` is True when HEAD still points at the previous build, and `.head` is that build. |
| `CostNotAllowed` | `PermissionError` | a full-rebuild comparison with a paid provider without `allow_cost=True` |
| `RollbackError` | `ValueError` | the target is not a committed build, or is already HEAD |

## Concurrency

`compile` and `rollback` hold the workspace build lock (`.dakc/build.lock`)
for the whole build, so builds from several processes, threads or tools are
serialized.

Read methods never take the lock. SQLite's WAL mode gives them a consistent
snapshot while a build runs. This is what lets an agent or the REST API keep
answering during a compile.
