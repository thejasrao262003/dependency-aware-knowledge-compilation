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
| `status()` | `Status` | `head`, `counts`, `up_to_date`, `source_changes`, `stale` (artifacts per kind downstream of changed sources), `conflicts_open`, and `outdated`: reasons a compile has work besides source changes (DAKC's logic, the configuration, the conflict scope rules or the provider changed since HEAD, or the conflict verifier has pairs left to judge). `up_to_date` is true only when there are neither. |
| `check(root=None)` | `SourceChanges` | new / modified / deleted / renamed / unchanged, by content hash |
| `plan(full=False)` | `BuildReport` | Dry run. Nothing is written or spent. `has_work()` and `llm_calls()` tell you whether a compile is needed and what it costs. Exact for free providers. For LLM providers, extraction calls are exact and page and summary counts are an estimate (`report.exact` is false). |
| `compile(full=False, wait=0.0)` | `BuildReport` | Takes the build lock, commits atomically, updates the wiki files, then runs the configured publishers. `report.publications` has one result per publisher; a failing publisher does not fail the build. Raises `BuildLocked` or `CompileFailed`. |
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
| `links(path=None)` | `list[BrokenLink]` | Links between documents that point at a document or heading that no longer exists, each with `path`, `line`, `href`, `reason` and, when DAKC saw the rename that broke it, a `suggestion`. With `path`, only links in that document or pointing at it. |
| `linked_from(path)` | `list[dict]` | Every link in another document that points at `path`, working or not: who a rename of it, or of its headings, affects. |
| `conflicts(scoped=False, accepted=False)` | `list[Conflict]` | Open conflicts between sources at HEAD (RFC 0002): `kind` is `conflict` (found by the rules) or `possible` (judged by the optional verifier). `scoped=True` adds differences explained by time, release, environment or status. `accepted=True` adds those accepted in `dakc.toml`. Advisory: nothing is changed. |
| `conflict(id)` | `Conflict` | One finding: each value with its `locations` (path, `line` in the source on disk, heading, scope, claim and the source's own `evidence` sentence), and `affected` pages and summaries. `id` is `cfl:…`, a unique prefix, or `conflict:…`. |
| `verify(against_full=False, allow_cost=False)` | `VerifyReport` | Invariants. `against_full` also compares with a full rebuild; with an LLM provider it needs `allow_cost=True` (`CostNotAllowed` otherwise). |
| `rollback(to=None, wait=0.0)` | `RollbackResult` | Takes the build lock. Destructive: later artifact versions are discarded, though their build records remain. Publishers are brought back too (`.publications`). |
| `sources()` | `list[SourceInfo]` | The configured sources: name, type, path prefix, location, capabilities, documents at HEAD |
| `publish(name=None, dry_run=False, reset=False, wait=0.0)` | `list[PublishResult]` | Sends each publisher what its ledger has not acknowledged. `dry_run` only counts; `reset` sends everything again. A failure is returned (`.error`, `.pending`), not raised. |

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
| `SourceError` | `RuntimeError` | a source is misconfigured, could not be listed or read, or its plugin could not be loaded. During a compile it surfaces as `CompileFailed` and nothing is committed. |
| `PublishError` | `RuntimeError` | a publisher is misconfigured or its plugin could not be loaded. It is raised before a compile starts. |

## Concurrency

`compile` and `rollback` hold the workspace build lock (`.dakc/build.lock`)
for the whole build, so builds from several processes, threads or tools are
serialized.

Read methods never take the lock. SQLite's WAL mode gives them a consistent
snapshot while a build runs. This is what lets an agent or the REST API keep
answering during a compile.

## Extending: connectors and publishers

The contracts are in
[RFC 0001](rfcs/0001-sources-and-publishers.md). A connector lists items and
reads them:

```python
from dakc import Item

class NotesConnector:
    kind = "notes"
    capabilities = frozenset({"native_ids"})   # items carry a stable key

    def __init__(self, client):
        self.client = client

    def list(self):                              # a complete listing, every time
        for n in self.client.all_notes():
            yield Item(path=f"{n.folder}/{n.slug}.md", source_type="markdown",
                       revision=n.version, size=n.size, key=n.id)

    def read(self, item):                        # the bytes of exactly that revision
        return self.client.export(item.key, version=item.revision)

def make(name, options, root):                   # the factory DAKC calls
    return NotesConnector(connect(options["url"]))
```

A publisher receives batches of `Record`s (`id`, `kind`, `state_hash`,
`title`, `text`, `sources`, `build`, `metadata`) and deletions. It must be
idempotent:

```python
class VectorPublisher:
    kind = "vectors"
    kinds = frozenset({"claim"})

    def __init__(self, index):
        self.index = index

    def publish(self, batch):
        if batch.deletes:
            self.index.delete(ids=batch.deletes)
        if batch.upserts:
            self.index.upsert([(r.id, embed(r.text), {"sources": r.sources, "build": r.build})
                               for r in batch.upserts])
```

Register either one with an entry point in the `dakc.sources` or
`dakc.publishers` group, or load a local module with `type = "custom"`,
`factory = "module:attr"` and `plugin_path = "plugins"`. Every other key in
its `[[sources]]` or `[[publishers]]` table is passed to the factory as
`options`.
