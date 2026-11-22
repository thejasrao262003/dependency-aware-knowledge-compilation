# Changelog

## v0.4.0: web UI, MkDocs plugin, and FastAPI's real history

### Upgrading from 0.3

- **Nothing to rebuild.** The parser, claim-identity and rendering versions
  are unchanged, so existing knowledge bases are reused as they are.
- **`dakc serve` now also serves the web UI.** `/` redirects to `/ui/`, every
  existing REST endpoint is unchanged, and new ones were added (below). It
  stays local-only.
- **New optional extra: `mkdocs`.** Install it with
  `pip install 'dakc[mkdocs]'`.

### Added

- **Web UI: `dakc serve`.** A local, read-only UI. It has these views:
  - an overview drawing the knowledge base as layers, with what the next or
    last compile changes;
  - the pending compile plan;
  - search across pages, claims, passages and sources;
  - pages whose citations open the claim, the source version and the exact
    sentence;
  - dependencies, impact and a layered dependency graph per artifact;
  - build history with titled diffs.

  It is plain HTML, CSS and JavaScript inside the package, with no build step
  or CDN. It has light and dark themes and works on mobile. New REST
  endpoints: `/artifacts/{id}/read`, `/artifacts/{id}/neighborhood`,
  `/titles`, and `?titles=true` on `/diff`. New library method:
  `KnowledgeBase.neighborhood()`.
- **`examples/fastapi-docs/ui_demo.py`.** The UI on FastAPI's docs, with
  history and one pending real commit.

- **MkDocs plugin** (`pip install 'dakc[mkdocs]'`, then add `- dakc` under
  `plugins`).
  - Every `mkdocs build` and every `mkdocs serve` reload compiles the docs
    incrementally.
  - The knowledge base is published under `/knowledge/`, with every citation
    linking back to its docs page.
  - Options: `workspace`, `provider`, `publish`, `prefix`, `min_claims`
    (publish only pages backed by at least N facts; default 2),
    `max_llm_calls` (fails the build instead of overspending), `verify` and
    `enabled`.
  - Works with `mkdocs build --strict`.
  - See `docs/integrations/mkdocs.md`, which includes a GitHub Actions
    workflow that caches DAKC's state between runs.
- **`examples/mkdocs-site`**: an ordinary MkDocs site with the plugin added.
  CI builds it in strict mode and checks that the second build is a no-op.
- **`examples/fastapi-docs`**: FastAPI's real documentation and history.
  - `replay.py` replays the last 50 commits that changed the docs, measuring
    full, file-level and DAKC rebuild costs at every commit. It also checks
    that DAKC's result equals a full rebuild each time.
  - `walkthrough.py` captures the edit, dry-run, compile, trace and verify
    flow on one real commit.
  - `agent_session.py` records a real agent session over MCP, before and
    after that commit.
  - `RECORDING.md` is a tested script for a demo of under 3 minutes.
  - Findings, now documented as limitations: large flat folders make
    expensive topic summaries, and the heuristic extractor misses sentences
    with no capitalized term.

## v0.3.0: fixes from real-agent testing, local-only HTTP

Most changes were found by testing the MCP server with real agents (see
`docs/mcp.md`).

### Upgrading from 0.2

- **The parser version changes (`parse/2`).** The first `dakc compile`
  after upgrading re-parses every source. Only passages whose text changed
  are re-extracted: in practice, paragraphs containing Markdown tables.
  With the free default provider this costs nothing. **With an LLM
  provider, run `dakc compile --dry-run` first**: it shows how many
  passages and LLM calls the upgrade needs. On this repository's
  `research/` folder, the 19 tables became 193 row passages, and every
  other passage was reused.
- **`dakc serve` and `dakc mcp --transport streamable-http` refuse
  non-loopback `--host` values.** Requests with a non-local `Host` header
  are rejected. If you exposed the REST API on a network, that now fails on
  purpose. There is no authentication yet.
- **No API, CLI or JSON fields were removed.**

### Security

- **Local-only HTTP.** Both HTTP servers bind only to 127.0.0.1, localhost
  or ::1, and reject requests for other hosts. This blocks DNS rebinding
  from web pages. The MCP HTTP transport uses the SDK's host check, and the
  REST API adds the same check.

### Fixed

- **Markdown tables** were parsed as a single paragraph, so facts taken
  from a table row lost their column headers. Each row is now one block,
  with every value labelled by its column ("Row - Header: value;
  Header: value."). The parser version moves to `parse/2`, so existing
  knowledge bases re-parse on their next build. Only passages whose text
  changed are re-extracted.

### Added

- **`read` / `read_artifact` on a source document** return its current text
  on disk, with `changed_since_build` (and `on_disk: missing` when it is
  gone).
- **Forgiving references.** A path relative to another directory
  (`research/notes.md` for `notes.md`) resolves when exactly one source
  matches. `ArtifactNotFound` suggests the closest paths and page titles,
  and explains content-addressed ids taken from a compile preview.
- **Full-text search.** `artifacts(query=...)` matches claims and passages
  on their full text.
- **MCP stale notes.** `status` and `explain` include a `stale_note` saying
  that "stale" is an upper bound and that a compile preview gives the exact
  rebuild. The tool descriptions and server instructions now say how to
  search concepts and read source documents.
- **Docs.** MCP setup, including registration with `uvx` and no install,
  plus a *Known limitations* table (`docs/mcp.md`). GitHub issue templates
  for bugs, feature requests and integration reports.

## v0.2.1: first PyPI release

There are no code changes from v0.2.0. This release is packaging only:

- **Published to PyPI** as `dakc`:
  `pip install dakc`, `pip install 'dakc[mcp]'`.
- **Package metadata:**
  - project URLs;
  - author, keywords and classifiers;
  - an SPDX license expression (`Apache-2.0`) with the license file.
- **PyPI page links:** repository-relative README links are rewritten to
  absolute GitHub URLs at build time, so they work there.
- **Install instructions** in the README and `docs/mcp.md` now use PyPI.

## v0.2.0: Python API and MCP server

### Added

- **Python API.**
  - `dakc.open(path)`, `dakc.init(...)` and `dakc.ingest(...)`, plus a
    `KnowledgeBase` with these methods: `status`, `check`, `plan`, `compile`,
    `artifacts`, `read`, `explain`, `trace`, `downstream`, `graph_stats`,
    `export_graph`, `builds`, `diff`, `duplicates`, `verify` and `rollback`.
  - Typed results with `to_dict()`.
  - One error hierarchy under `dakc.DakcError`.
  - See [docs/python-api.md](docs/python-api.md).
- **MCP server: `dakc mcp`**, on the official MCP Python SDK 2.x, over stdio
  or streamable HTTP.
  - 12 read-only tools, plus `compile` (previews by default and builds only
    with `apply=true`) and `rollback` (an explicit build id, marked
    destructive).
  - A cost guard for LLM providers (`--max-llm-calls`), `--read-only` mode,
    and bounded outputs.
  - See [docs/mcp.md](docs/mcp.md).
- **REST API:** `GET /artifacts`, `/artifacts/{id}/trace`,
  `/artifacts/{id}/downstream`, `/duplicates` and `/health`, plus `?full=` on
  `/plan`.
- **CLI:** `--json` for `status` and `builds`.
- **Build reports:** `BuildReport.has_work()`, and `has_work` in report JSON.
- **Packaging:** an `mcp` extra.

### Changed

- **The CLI and the REST API are now thin layers over the Python API.** The
  REST API opens and closes a connection per request; before, it leaked one
  per request.
- **REST `/status`** returns the full status object. It now has `stale`
  (artifact counts per kind) instead of `stale_sources`.
- **`--json` output** of `check` and `verify` gained fields (`root`,
  `has_work`, `ok`). Existing fields are unchanged.
- **A failed compile** is reported as
  `compilation failed: <ErrorType>: <message>`.

### Fixed

- **The README was imprecise about `rollback`** ("rollback to any build").
  Rolling back to build N discards the artifact versions written by later
  builds, and their records stay in the history as `rolled_back`. The README,
  the API and the MCP tool now say so, and MCP marks the tool destructive.
  `docs/architecture.md` already described this correctly.

## v0.1.0: first public release

DAKC is an incremental compiler for AI-generated knowledge bases. It turns a
directory of documents into claims, entity pages, topic summaries and an
index. When sources change, it rebuilds only the artifacts whose inputs
changed, and it can check the result against an independent full rebuild.

### Compiler

- **Dependency model.** Dependencies are tracked at the level of claims, not
  files: source → passage → claim → entity/relationship → page → summary →
  index.
- **Content addressing.** Paragraphs are hashed, and claims are identified by
  their content.
- **Early cutoff.** A rebuild stops as soon as a recomputed artifact comes out
  unchanged.
- **Sentence-grounded extraction.** Each claim records the sentences it came
  from. After an edit, claims whose evidence sentences are unchanged keep their
  identity. Sentences that may refer back to an edited one are re-extracted.
- **Transactional builds.** Every build is committed atomically, with full
  history, `rollback`, and fault injection for testing.
- **Concurrency.** A workspace build lock, plus a HEAD check at commit, so one
  build never overwrites another.

### Correctness and provenance

- **Full-rebuild check.** `dakc verify --against-full` compares the
  incremental result with an independent full rebuild.
- **Version tracking.** Every artifact records the extractor, writer and
  summarizer fingerprints. The parser, claim-identity, entity-resolution and
  rendering logic are versioned, and changing one re-derives exactly the
  affected layer.
- **Tracing.** `dakc trace` maps each page sentence to its claim, passage,
  sentence and source version. An optional strict mode renders only sentences
  that cite a claim.
- **Claim identity.** Matching is exact. Possible duplicates are reported by
  `dakc duplicates` but never merged. The labelled equivalence evaluation is
  in `benchmarks/equivalence.py`.
- **Contradictions.** Conflicting claims are kept side by side, each with its
  own sources.

### Interfaces

- **CLI:** `init`, `ingest`, `check`, `compile` (`--incremental`,
  `--dry-run`, `--full`), `graph`, `explain`, `trace`, `duplicates`, `status`,
  `builds`, `diff`, `rollback`, `verify` and `watch`.
- **Read-only REST API:** `dakc serve`. Its graph JSON is in React Flow
  format.
- **Providers:** `heuristic` (default, no network), `mock` (benchmarking),
  `openai` (any OpenAI-compatible endpoint, including Ollama), `anthropic`, and
  `custom` (bring your own client).

### Evidence

- **Deterministic benchmark.** Mock provider, 100 / 1,000 / 5,000 documents,
  ten change scenarios, plus a 20,000-document run with realistic document
  lengths and topic sizes. In every run, every strategy's result was
  identical to a full rebuild. CI reproduces the published 100-document
  numbers exactly.
- **Real-model measurements.** One hosted model on small corpora. These are
  indicative, not exactly reproducible.
- See [docs/benchmark.md](docs/benchmark.md) for the method, the full results
  and the caveats.

### Known limitations

See [Limitations](README.md#limitations). In particular:

- Real-model evidence covers one model.
- There is no frontend yet.
- DAKC runs on a single host.
