# Changelog

## v0.6.0: conflicts between sources

### Added

- **Conflicts between sources** (RFC 0002, `docs/rfcs/0002-conflicts.md`).
  - What it does: every compile reports statements in different documents
    that cannot both be true. That means a negation ("supports" / "does not
    support"), or two values for a single-valued property (the primary
    database, a default port, a node count, a license).
  - Where to see them: `dakc conflicts` lists them. `dakc conflicts ID` or
    `dakc explain cfl:…` shows each sentence with its file and line, and the
    pages and summaries built from it.
  - It is advisory: no claim or page changes, and no model is called.
- **Scope decides what is a conflict.** It is read from the sentence, the
  heading and the path. Release markers, past tense, environments,
  `archive/`, revision-numbered files (`techstack001.md`), decision records,
  proposals, FAQs and conditionals all count. So an MVP on MongoDB and
  production on PostgreSQL is reported as *scoped* (`--all`), not as a
  conflict.
- **`[conflicts]` in `dakc.toml`.**
  - `[[conflicts.scope]]` marks what a path describes.
  - `[[conflicts.accept]]` keeps a reviewed conflict, with a reason.
- **Everywhere you already look.**
  - The compile report and `status` count open conflicts.
  - `--check` exits 1 when one is open, for CI.
  - Python: `kb.conflicts()` and `kb.conflict(id)`. REST: `/conflicts` and
    `/conflicts/{id}`. MCP: a `conflicts` tool.
  - The `dakc review` PR comment lists conflicts a change introduces and
    resolves.
- **Kept current incrementally, and checked.**
  - A build re-judges only the properties whose claims changed: 0.3 ms for
    a one-file edit of FastAPI's docs.
  - `dakc verify` requires the stored findings to equal a detection from
    scratch.
  - `rollback` restores the findings of the build it returns to.
  - Measured on FastAPI's docs by `examples/fastapi-docs/conflicts_eval.py`:
    0 findings over 11,067 claims; 90% of planted negations and 78% of
    planted value changes caught in plain statements.
- **Scope readings fixed**, found by the verifier's evaluation:
  - a word starting with a scope marker ("from **Ga**teway") was read as
    the GA release;
  - "used" or "legacy" in a sentence marked it as past;
  - a product's version or a region after the verb ("runs on Python 3.11",
    "lives in the eu-west-1 region") was read as a scope; it is now a value
    of the statement. In a leading phrase ("In eu-west-1, …") it is still
    a scope.
- **The same statement worded differently.**
  - "lacks", "has no support for", "dropped support for", "is unavailable
    in" and "is not supported by" read as negations of "supports".
  - Antonyms used as the predicate ("is disabled", "is optional", "is
    asynchronous" and about twenty more) read as negations of their
    counterpart.
  - A few synonyms (DB and datastore for database, Postgres for
    PostgreSQL) and the workspace's `[aliases]` apply before statements are
    compared.
- **An optional conflict verifier**, off by default:
  `[conflicts] verifier = "llm"`.
  - What it does: the workspace's model judges pairs of related claims that
    the rules cannot compare, such as "written in Go" against "a Python
    service". For each new claim, the pairs are the few most similar claims
    about the same entity in other documents.
  - Its findings are marked **possible**, and scope still applies to them.
  - It compares entities by name without a possessive, so "Ledger's" and
    "Ledger" are one subject.
  - Cost: verdicts are cached by the pair of claims, so each pair is paid
    for once. `max_checks` (default 500) caps the pairs judged per compile;
    the rest wait for the next one. `--dry-run` and the MCP call cap count
    its calls.
  - Checked: its findings are recomputed from the verdicts on record, and
    `dakc verify` requires them to equal that computation.
  - Quality: with gpt-5-mini, rules plus verifier flag 50 of 55 held-out
    contradictions, with no false alarm in 55 other pairs (rules alone: 4
    of 55, no false alarm).
  - On FastAPI's docs, at gpt-5-mini's measured token use: turning it on
    judges about 23,000 pairs (about $6, once); 50 real commits then judged
    291 pairs (about 8 cents). `examples/fastapi-docs/verifier_volume.py`
    measures this.
- **A labelled evaluation set** in `benchmarks/conflicts/`: 81 development
  pairs and 110 held-out pairs written without access to the detector.
  `evaluate.py` reports precision, recall, and how many contradictions the
  verifier would be shown. On the held-out set, the rules catch 4 of 55
  contradictions with no false alarm in 55 other pairs, and 53 of 55 reach
  the verifier. `--verdicts` keeps a model's verdicts, so evaluating again
  costs nothing.

### Upgrading from 0.5

- **The store schema moves to version 3** (the `conflicts` and
  `conflict_index` tables). This happens automatically when the store is
  opened.
- **The first compile after upgrading runs detection over every claim.** It
  commits a build even if no document changed. On FastAPI's docs that takes
  under a second.

### Fixed

- **A failed provider call during extraction threw away the calls that had
  succeeded.**
  - What happened: results were collected for the whole extraction stage
    before any was cached. One call failing, after the client's own retries,
    lost every paid extraction in that build, and the next attempt paid for
    all of them again.
  - The fix: results are now cached as they complete. When a call fails,
    calls that have not started are cancelled, every completed result is
    still cached, and then the build fails.
  - Effect: a retry pays only for what never succeeded, and a failing build
    stops spending.
  - Found while measuring a 14,000-call real-model build; covered by
    `tests/integration/test_paid_work_survives.py`.

## v0.5.0: bounded fan-in, sources and publishers, and a knowledge diff for pull requests

### Upgrading from 0.4

- **The first build after upgrading migrates large artifacts.** New
  versions `page/2` and `summary/2` are recorded. Hub pages (more than 64
  claims) and topic summaries of more than 64 pages are rebuilt once, in
  sections. Smaller pages and topics keep their signatures and are not
  regenerated.
- **Every source is re-parsed once (`parse/3`).** Passage text is
  unchanged, so nothing is re-extracted and no provider is called. The
  re-parse repairs passage headings left stale by earlier versions (see
  *Fixed*).
- **The store's schema moves to version 2.** Two nullable columns
  (`sources.key`, `sources.revision`) and a `published` table are added on
  first open. The change is additive, so 0.4.0 can still open and compile
  the store (tested). Whatever it writes is migrated by the next compile
  with this version.
  Workspaces without `[[sources]]` behave exactly as before: same source ids,
  same paths.
- **With an LLM provider, preview the migration first.** Run
  `dakc compile --dry-run` to see its cost before running it.
- **Heuristic and mock summaries are regenerated (free).** The template
  summarizer now writes a short overview (`template-summarizer/2`).

### Added

- **Sources and publishers** ([RFC 0001](docs/rfcs/0001-sources-and-publishers.md)).
  The compiler no longer knows where content comes from or where it goes.
  - **Source connectors** list versioned items and read them. The filesystem
    is now the first connector. A connector may declare `native_ids` (a
    moved and edited document keeps its identity) and `content_hash` (an
    unchanged revision is never read).
  - **Several sources per workspace**, configured as `[[sources]]` tables,
    each under its own path prefix. A fact stated in two sources survives
    the deletion of either. Renames are detected only within one source.
  - **Fail closed.** A source that cannot be listed fails the build before
    anything is committed. So does a source directory that went missing
    while the store still holds its documents.
  - **Publishers**, configured as `[[publishers]]` tables, receive only what
    changed, tracked by a ledger of what each one has acknowledged. A
    publisher that fails leaves its changes pending without failing the
    build, and the next run resumes. Rollbacks converge.
  - **A built-in `jsonl` publisher**: a change feed for anything without a
    plugin.
  - **Plugins** via the `dakc.sources` and `dakc.publishers` entry-point
    groups, or local modules (`type = "custom"`, `factory`, `plugin_path`).
  - **Interfaces.**
    - CLI: `dakc sources`, and `dakc publish [NAME] [--dry-run] [--reset]`.
    - Python: `kb.sources()`, `kb.publish()`, `report.publications`, and
      `dakc.Item`, `SourceConnector`, `Publisher`, `Record`,
      `SourceError`, `PublishError`.
    - `dakc watch` watches every filesystem source, and refuses to start
      when there is none.

- **A knowledge diff for pull requests.**
  - `dakc review --base REF` compiles the base and the head with the free
    deterministic provider. It reports the documents changed, the facts added
    and retracted (with their text and sources), the pages and summaries
    affected, and the LLM calls and estimated cost of applying the change.
  - A GitHub Action (`action.yml`) posts it as a pull request comment,
    updated in place, and caches the base build between runs. See
    `docs/integrations/github-action.md`.
  - This repository reviews its own pull requests with it.

### Changed

- **Bounded fan-in.** Hub pages and large topic summaries are built in
  content-defined sections, each cached by a signature over its inputs. An
  edit rewrites the sections it touched, then their parent, and no prompt
  reads a whole hub or a whole large topic.
  - On a FastAPI release-notes commit: 4,699 input tokens instead of 58,689,
    in 6 calls instead of 4.
  - Over FastAPI's last 50 documentation commits: 148,030 input tokens
    instead of 2,460,770, in 317 calls instead of 259. The result still
    equals a full rebuild at every commit.
  - On the synthetic benchmark, DAKC's input tokens across all ten scenarios
    fall by 26% at 100 documents, 69% at 1,000 and 89% at 5,000. It
    regenerates the same pages as before in every scenario.
  - A full build makes 3–5% more calls.
  - See *Bounded fan-in* in `docs/dependency-model.md` and ADR-27.
- **The template summarizer's output is bounded.** It writes a short overview
  instead of repeating every page, which the rendered summary already lists.

### Fixed

- **`status` said "up to date" when a compile still had work.** It compared
  only the sources. After an upgrade that changes DAKC's logic, or a change
  to aliases, the provenance mode or the provider's model, it reported up to
  date. An agent that trusted it would skip the compile. `Status` now has
  `outdated`, the reasons other than source changes, and `up_to_date`
  accounts for them. This shows in the CLI, REST, MCP and the web UI. After a
  rollback, it reads the fingerprints of the build that was restored.
- **Passages kept the heading they were first seen under.** A passage's id
  covers its text, not its heading. So when a heading was inserted or renamed
  above unchanged paragraphs, the passages were rightly reused, but kept their
  old heading. `dakc trace` and the web UI then named the wrong section, and
  `verify --against-full` missed it because it compared passage text only.
  Present since v0.1. Re-parsing now updates the heading in place
  (`passage_heading_changed`), without re-extracting, and the full-rebuild
  comparison includes headings. Found by replaying FastAPI's history with an
  early version of sectioned hub pages that grouped facts by heading.
- **A summary could be reported twice.** When its text was reused but its
  rendered listing changed, it was reported as both *checked and unchanged*
  and *re-rendered*, so the web UI counted it twice and showed too few
  untouched summaries. Each summary now gets exactly one outcome, as pages
  already did. The CLI plan lists re-rendered summaries
  (`Summaries: regenerate N, re-render N, …` and
  `- Regenerate N summaries, re-render N`).
- **Dry runs with an LLM provider undercounted.** For edits that add facts,
  they planned the extraction call but none of the pages and summaries the
  new facts would touch. The plan now estimates new facts with the heuristic
  extractor. The docs had called these counts an "upper bound"; they are now
  labelled an estimate, and the MCP and MkDocs cost guards are described
  accordingly.

### Corrected

- **The cost of a FastAPI release-notes commit was misattributed.** The
  v0.4.0 docs said nearly all of it went to a ~55,000-token prompt for the
  root topic summary. Measured stage by stage, that summary is about 30,000
  tokens. The other ~28,600 regenerate one hub page with 3,491 claims ("PR",
  created by the heuristic extractor). The README, the replay report and the
  example README now give the measured split.
- **The README no longer says there is no frontend.**

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
