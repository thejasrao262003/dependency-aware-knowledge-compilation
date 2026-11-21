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
| `ingestion` | Source scan, raw hashing and change classification, including rename pairing. |
| `claims` | Segmentation into passages, the extractor protocol, the heuristic extractor, claim normalization and deterministic claim matching. |
| `artifacts` | Pydantic models for every artifact kind; content hashing; dependency derivation from content. |
| `graph` | Dependency-edge queries, reverse/transitive traversal, cycle detection and export (JSON, DOT, React Flow) on NetworkX. |
| `invalidation` | Builds the plan: the affected set, rebuild categories and LLM-call estimates. |
| `compiler` | Executes the plan layer by layer. Runs provider calls concurrently within a layer, applies signatures and early cutoff, and emits a ChangeSet. Strategies: `dakc`, `file`, `full`. |
| `providers` | Extractor, page-writer and summarizer protocols. Deterministic heuristic and mock implementations. LLM components over one client protocol: an OpenAI-compatible `httpx` client (OpenAI, Ollama, vLLM, LM Studio) and a Claude client on the official `anthropic` SDK (optional extra). Token accounting. |
| `transactions` | Build records, atomic commit, failure recording, crash recovery, rollback, state digest. |
| `storage` | SQLite schema and temporal (versioned) tables. |
| `provenance` | Explain/trace queries: "why does this artifact exist, what does it depend on, what depends on it, is it stale". |
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
| `sources` | temporal source registry (path, raw hash, text hash, type, size) |
| `artifacts` | temporal artifact versions (id, kind, content, content hash, signatures, meta) |
| `edges` | temporal dependency edges (dependent → dependency) |
| `extraction_cache` | content-addressed provider results. It is not knowledge, so it is never rolled back. |
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

## Concurrency

Within a layer, artifacts are independent by construction: an artifact
depends only on lower layers. Provider calls in a layer run on a
`ThreadPoolExecutor` (`--jobs`). Results are collected and applied in sorted
id order, so output does not depend on scheduling.

## What is deliberately not here

No message queue, no distributed workers, no vector database, no web
frontend in the MVP. The FastAPI service and React-Flow-shaped graph export
are the hooks a frontend would use.
