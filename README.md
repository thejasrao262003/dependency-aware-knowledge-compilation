# DAKC: Dependency-Aware Knowledge Compiler

DAKC keeps an LLM-maintained knowledge base (claims, entity pages, topic
summaries, an index) up to date as its source documents change. It
**recomputes only what has become invalid** and proves that the result is the
same as a full rebuild.

It does this with a build-system model applied to knowledge:

- Every derived artifact records the exact inputs it was built from.
- Sources are hashed per paragraph.
- Claims are content-addressed.
- Rebuilds stop as soon as a recomputed artifact comes out unchanged
  (*early cutoff*).

```text
source ─▶ passage ─▶ claim ─▶ entity / relationship ─▶ page ─▶ topic summary ─▶ index
 (file)   (paragraph)  (fact)      (who / what)        (wiki)     (overview)
```

Edit one sentence in a 5,000-document corpus and DAKC re-extracts one
paragraph and regenerates only the pages whose facts changed. Pages whose
facts are unchanged but whose citations moved are re-rendered without an LLM
call. Everything else keeps its exact bytes. `dakc compile --dry-run` tells
you all of this before anything is spent.

> **Status:** research prototype / MVP, CLI-first, local-first. Python 3.12+,
> SQLite, no GPU, no network required (deterministic providers are the
> default). See [what is not done](#limitations).

## Why this exists

The research phase audited twelve open-source LLM-wiki and graph-RAG projects
at the code level ([competitive analysis](research/COMPETITIVE_ANALYSIS.md),
[gap report](research/TECHNICAL_GAP.md)). None does all of the following:

- tracks dependencies below the file level, or between derived artifacts
  (page → summary → index);
- stops propagation when a recomputed artifact is unchanged;
- tests incremental output against a full rebuild.

The usual pattern is "a file changed, re-ingest it, and let the LLM merge the
result into existing pages". That pattern is costly, and it keeps facts the
source no longer states (for example llm_wiki issue #837; see the analysis for
code citations). The techniques DAKC uses are not new: verifying traces and
early cutoff come from build systems (Shake, Bazel, Salsa), support counting
from incremental view maintenance, and content addressing from Git. The
contribution is applying them correctly at claim granularity, with a test
oracle.

## Quick start

```bash
git clone <this repo> && cd dependency_aware_knowledge_compilation
uv venv --python 3.12 && uv pip install -e '.[dev]'     # or: pip install -e '.[dev]'

mkdir demo && cp -r examples/sample-knowledge-base demo/knowledge && cd demo
dakc ingest ./knowledge          # discover, hash, extract, compile -> ./wiki
```

`ingest` creates `dakc.toml`, the store in `.dakc/dakc.db`, and Markdown
output in `wiki/` (`pages/`, `summaries/`, `index.md`). Your source files are
never modified.

## Walkthrough: the Definition of Done

```bash
dakc graph                                   # artifact and edge counts by layer
dakc graph --source databases/postgresql.md  # everything downstream of one source

# edit one claim in a source document...
perl -pi -e 's/supports JSONB/stores JSONB in a binary format/' knowledge/databases/postgresql.md

dakc check                       # new / modified / deleted / renamed / unchanged (content hashes)
dakc explain page:jsonb          # STALE, with the reason and the upstream sources
dakc compile --dry-run           # exactly which artifacts will be affected; writes nothing
dakc compile                     # incremental; only the plan above is executed
dakc diff 1                      # what changed between build 1 and HEAD
dakc verify --against-full       # invariants + identical to a from-scratch rebuild?
dakc rollback                    # restore the previous build (store + wiki files)
```

The dry run in this walkthrough prints the following (real output, unedited):

```text
Compilation Plan
----------------
Strategy: dakc

New Sources:        0
Modified Sources:   1
Deleted Sources:    0
Renamed Sources:    0
Unchanged Sources:  6

Passages to (re-)extract:   1   (extraction cache hits: 0)
Affected Claims:            5   (new 1, retracted 1, changed 0, provenance-only 3, reproduced unchanged 0)
Affected Entities:          2
Affected Knowledge Pages:   2   (regenerate 2, re-render citations only 0, delete 0, checked & unchanged 3)
Unaffected Knowledge Pages: 20
Summaries:                  regenerate 0, delete 0, checked & unchanged 1
Index:                      unchanged

Planned Operations:
- Re-extract 1 passage(s)
- Reconcile 2 entities
- Regenerate 2 page(s), re-render 0
- Update 0 summaries

Estimated LLM Calls: 0 (exact)

No files modified.
```

This is the PRD's example. The edited paragraph also states "PostgreSQL
supports Transactions" and "PostgreSQL supports Indexing". Re-extraction
reproduces those claims with the same ids, so the Transactions, Indexing and
Ledger pages are *checked and unchanged*. Only the JSONB and PostgreSQL pages
are regenerated.

With the default heuristic provider, LLM calls are 0 and the plan is exact.
With an LLM provider, the plan counts the calls it *would* make, does not
make them, and labels downstream numbers as an **upper bound**.

## Commands

| Command | What it does |
|---|---|
| `dakc init [--source DIR] [--provider P]` | Create `dakc.toml` |
| `dakc ingest DIR` | Register a corpus and compile it |
| `dakc check [DIR]` | Classify sources by content hash, including renames and duplicates |
| `dakc compile [--incremental \| --full] [--dry-run] [--json]` | Compile; `--full` is the correctness baseline |
| `dakc compile --strategy file` | The file-level baseline used in the benchmark |
| `dakc graph [--source X] [--format text\|json\|dot\|reactflow]` | Inspect or export the dependency graph |
| `dakc explain ID` | Dependencies, dependents, upstream sources, signatures, history, VALID/STALE |
| `dakc status` | HEAD, counts, and what is stale against the current sources |
| `dakc builds` / `dakc diff A [B]` | Audit log; artifact-level diff between builds |
| `dakc rollback [--to N]` | Restore any earlier committed build |
| `dakc verify [--against-full]` | Invariant checks; equality with a full rebuild |
| `dakc serve` | Read-only REST API (`/status /check /plan /graph /artifacts/{id} /builds /diff`) |
| `dakc watch` | Recompile incrementally on file changes |

IDs can be given in friendly form: `page:database-architecture`,
`entity_postgresql`, `source:databases/redis.md`, a source path, or a page
title.

## How it decides what to rebuild

Each artifact stores a **signature**: the hash of exactly the inputs it read,
plus the fingerprint (algorithm/model/prompt) of the provider that built it.
An artifact is rebuilt if and only if its signature changes. The full model,
including deletion semantics and dry-run precision, is in
[docs/dependency-model.md](docs/dependency-model.md).

| Change | What DAKC does |
|---|---|
| One sentence edited | Re-extract that paragraph. Unchanged sentences reproduce the same claim ids, so their pages are untouched. |
| Source deleted | Remove its passages from claims' support sets. Retract claims left unsupported. Pages whose claims survive elsewhere are only re-rendered (citations). |
| Source renamed | Same source id, **zero** LLM calls; citations re-rendered. |
| Duplicate document added | Extraction-cache hit, **zero** extraction calls. |
| Entity aliases changed (`[aliases]` in `dakc.toml`) | Entity resolution re-runs deterministically; only pages whose membership changed are rebuilt. |
| Prompt or model changed | The provider fingerprint changes, so exactly the artifacts that provider built are invalidated. |
| Build fails (provider error, crash, injected fault) | Nothing is committed. HEAD and the wiki are unchanged; paid extraction results are cached for the retry. |

## Providers

| `provider =` | Use | LLM calls |
|---|---|---|
| `heuristic` (default) | Deterministic sentence-level extraction and template writers; no network | 0 |
| `mock` | Same outputs, but every call is *accounted* as an LLM call with estimated tokens (benchmarking) | simulated |
| `openai` | Any OpenAI-compatible endpoint: OpenAI, **Ollama**, vLLM, LM Studio (`[provider.openai] base_url, model`) | real |
| `anthropic` | Claude via the official SDK (`pip install 'dakc[anthropic]'`, default model `claude-opus-5-5`) | real |

The heuristic extractor is a baseline: it treats capitalized and camel-case
terms as entities and each such sentence as a claim. Use an LLM provider for
extraction quality. The invalidation guarantees do not depend on the
extractor.

## Benchmark

BENCHMARK_SUMMARY_PLACEHOLDER

## Limitations

- **No real-LLM measurements yet.** All numbers come from deterministic mock
  providers with estimated tokens. With an LLM extractor, re-extracting an
  edited paragraph may paraphrase unchanged facts and weaken claim-level
  cutoff. Anchored re-extraction (previous claims passed to the model) is
  implemented to counter this, but has not been measured.
- **Heuristic extraction is crude** (see above).
- **Contradictions are kept, not resolved.** Both claims appear on the page
  with their own sources.
- **No frontend.** `dakc graph --format reactflow` and `GET /graph` provide
  React Flow data for one.
- **Single writer.** SQLite `BEGIN IMMEDIATE` serializes compiles; there is
  no distributed mode by design.

## Project layout

```text
src/dakc/      hashing · parsing · ingestion · claims · artifacts · graph · invalidation
               compiler (+ projection) · transactions · provenance (explain, compare, verify)
               providers (heuristic, mock, openai-compatible, anthropic) · storage · cli · api
research/      competitive analysis, technical gap, architecture decisions, per-repo audits
docs/          architecture, dependency model, benchmark (design + generated results), implementation log
benchmarks/    seeded corpus generator + scenarios, runner, report generator, results/latest.json
tests/         unit · integration · regression (incremental == full) · benchmark properties
examples/      sample-knowledge-base (Markdown, TXT, JSON, HTML)
```

## License

Apache-2.0. See [LICENSE](LICENSE). Contributions welcome; see
[CONTRIBUTING.md](CONTRIBUTING.md).
