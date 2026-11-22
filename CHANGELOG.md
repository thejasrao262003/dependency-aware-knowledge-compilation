# Changelog

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
