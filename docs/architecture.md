# Architecture

DAKC is a local, single-process build engine for LLM-maintained knowledge
bases. It keeps every derived artifact in SQLite together with its recorded
inputs. A Markdown "wiki" directory is a projection of that store.

```text
            ┌──────────────┐   raw bytes + BLAKE3
 knowledge/ │ Source       │──────────────────────────┐
 (inputs) ─▶│ Registry     │ new/modified/deleted/     │
            └──────┬───────┘ renamed/unchanged         │
                   │ changed sources                   │
            ┌──────▼───────┐                           │
            │ Parsing +    │ md / txt / html / json / pdf
            │ Segmentation │ → passages (content-addressed)
            └──────┬───────┘                           │
                   │ passages new to the cache          │
            ┌──────▼───────┐   extraction cache         │
            │ Claim        │◀─ H(extractor fp, passage) │
            │ Extraction   │   (LLM or heuristic)       │
            └──────┬───────┘                           │
                   │ claim support deltas               │
            ┌──────▼───────┐                           │
            │ Entity &     │ deterministic resolution   │
            │ Relationship │ (normalization + alias map)│
            └──────┬───────┘                           │
                   │ changed signatures only            │
            ┌──────▼───────┐                           │
            │ Pages,       │ LLM (regenerate) or         │
            │ Summaries,   │ code (rerender / index)     │
            │ Index        │                           │
            └──────┬───────┘                           │
                   │ ChangeSet (in memory)              │
            ┌──────▼───────┐                           │
            │ Transaction  │ one SQLite transaction:    │
            │ Manager      │ versions + edges + sources │
            └──────┬───────┘                           │
                   │ committed build                    │
            ┌──────▼───────┐                           │
            │ Projection   │ wiki/*.md (atomic per file,│
            │              │ re-derivable from store)   │
            └──────────────┘                           │
```

## Modules (`src/dakc/`)

| Module | Responsibility |
|---|---|
| `hashing` | BLAKE3 helpers, canonical JSON, text normalization, id derivation. |
| `parsing` | Format detection and text-block extraction (Markdown via markdown-it-py, HTML via BeautifulSoup, PDF via PyMuPDF, JSON, TXT). |
| `sources` | Source connectors (RFC 0001): the connector protocol, the filesystem connector, and `SourceSet`, which lists every configured source into one prefixed namespace and classifies changes (native-key matching, rename pairing within a source). Plugin loading. |
| `ingestion` | Source records, file discovery, and the scan result the compiler consumes. |
| `publishers` | Publishers (RFC 0001): the publication ledger, resumable batches, records, and the built-in `jsonl` change feed. |
| `claims` | Segmentation into passages and sentences, the heuristic extractor, sentence-grounded reuse planning (`plan_reuse`), claim normalization and deterministic claim matching. |
| `artifacts` | Pydantic models for every artifact kind; content hashing; dependency derivation from content. |
| `graph` | Dependency-edge queries, reverse/transitive traversal, cycle detection and export (JSON, DOT, React Flow) on NetworkX. |
| `invalidation` | Builds the plan: the affected set, rebuild categories and LLM-call estimates. |
| `compiler` | Executes the plan layer by layer. Runs provider calls concurrently within a layer, applies signatures and early cutoff, and emits a ChangeSet. Strategies: `dakc`, `file`, `full`. |
| `providers` | Extractor, page-writer and summarizer protocols. Deterministic heuristic and mock implementations. LLM components over one client protocol: an OpenAI-compatible `httpx` client (OpenAI, Ollama, vLLM, LM Studio) and a Claude client on the official `anthropic` SDK (optional extra). Token accounting. |
| `transactions` | Build records, atomic commit with a HEAD check, workspace build lock, failure injection, crash recovery, rollback, state digest. |
| `storage` | SQLite schema and temporal (versioned) tables. |
| `provenance` | Explain queries, sentence-level trace (`trace`), strict grounding filter, canonical comparison and invariant checks (`verify`). |
| `cli` | Typer application (`dakc`). |
| `api` | FastAPI read-only service over the same queries. |

## Storage

One SQLite database (`.dakc/dakc.db`, WAL mode). Every mutable table is
**temporal**: rows carry `from_build` / `to_build` validity, and the current
state is `to_build IS NULL`. This gives the following for little extra code:

- **history**: every artifact version that was ever committed;
- **snapshots**: the state at build *N* is `from_build ≤ N < coalesce(to_build, ∞)`;
- **rollback**: deleting rows created after *N* and reopening rows closed
  after *N*, in one transaction (O(changes since N));
- **diff**: `dakc diff A B` is a query over validity ranges.

| Table | Purpose |
|---|---|
| `builds` | id, parent, mode/strategy, status (`running`, `committed`, `failed`, `aborted`, `rolled_back`), timings, stats JSON, error |
| `sources` | temporal source registry (path, raw hash, text hash, type, size; a connector's native key and revision when it has them) |
| `artifacts` | temporal artifact versions (id, kind, content, content hash, signatures, meta) |
| `edges` | temporal dependency edges (dependent → dependency) |
| `extraction_cache` | content-addressed provider results. It is not knowledge, so it is never rolled back. |
| `published` | per publisher, the state hash of every artifact it has acknowledged: its ledger. Not knowledge, so it is never rolled back. |
| `meta` | schema version, head build, config fingerprint |

Original sources are never written to. Derived Markdown goes to a separate
output directory.

## Build lifecycle

1. **Open**: any build left `running` by a crashed process is marked
   `aborted`. Its compute phase never wrote knowledge rows, so nothing needs
   undoing.
2. **Plan**: scan sources and load the current state from `HEAD`.
3. **Compute**: run the layers. Provider calls happen here, concurrently
   within a layer. Results go into an in-memory ChangeSet and the
   content-addressed extraction cache, so a retry after a failure re-uses
   paid work.
4. **Commit**: one `BEGIN IMMEDIATE … COMMIT` closes superseded rows, inserts
   new versions and edges, updates the source registry and marks the build
   `committed`. An exception, injected fault or process kill before `COMMIT`
   leaves the previous build intact (SQLite atomicity).
5. **Project**: write changed pages, summaries and index to `wiki/`
   (temp file + `os.replace`), delete removed ones and record the projected
   build. A projection interrupted after commit is detected and repaired on
   the next run, because it is a pure function of the store.
6. **Publish**: each configured publisher receives the difference between
   the current state and its ledger, in batches recorded after each
   succeeds. A failing target leaves the rest pending for the next run, and
   does not undo the build.

## Concurrency

**Within a build.** Within a layer, artifacts are independent by
construction: an artifact depends only on lower layers. Provider calls in a
layer run on a `ThreadPoolExecutor` (`--jobs`). Results are collected and
applied in sorted id order, so output does not depend on scheduling.

**Between processes.** A workspace build lock (`.dakc/build.lock`, an OS file
lock holding the owner's PID) covers a whole build:

- recovery of crashed builds;
- loading state, compute and commit;
- wiki projection.

Behaviour of other processes:
- A second writer fails immediately (exit code 3), or waits with
  `--wait SECONDS`.
- The commit transaction also checks that HEAD has not moved since the state
  was loaded, so even a writer that bypasses the lock cannot cause a lost
  update.
- Readers never take the lock; SQLite WAL gives them a consistent snapshot
  during a build.

## What is deliberately not here

No message queue, no distributed workers and no vector database. The web UI
(`dakc serve`, ADR-26) is static HTML, CSS and JavaScript over the read-only
REST API, with no framework or build step. Compiling stays in the CLI, the
Python API and MCP.
