# Architecture decisions

Short ADRs. Each one records the decision, the reason, and what was rejected.

## ADR-1: Layered artifact model with content-addressed claims

**Decision.** Seven layers: source, passage, claim, entity/relationship, page,
summary, index. Edges only point to lower layers. Claim id = H(normalized
text).

**Why.**
- Layering makes execution order a topological order by construction and rules
  out cycles.
- Content-addressed claims give deduplication across sources, support
  counting, and claim-level early cutoff for free.

**Rejected.**
- *Per-source claim ids* (`source#n`): breaks deduplication, and any
  reordering invalidates everything.
- *An LLM-judged "same claim?" step*: costly and non-deterministic.

## ADR-2: Verifying-trace signatures plus early cutoff (from build systems)

**Decision.** Each derived artifact stores the signature of the inputs it was
built from, including the provider fingerprint. It is rebuilt iff the
signature changes.

**Why.** This is the minimal-recomputation rule of Shake, Bazel and Salsa. It
makes "affected" and "rebuilt" different sets, as PRD §4.3 requires.

**Rejected.** Dirty-bit propagation without signatures, which
over-invalidates. Colin's "upstream recompiled ⇒ rebuild" behaves this way.

## ADR-3: Split page work into *regenerate* (LLM) and *rerender* (code)

**Decision.**
- The page body depends on claim content and is LLM-generated.
- The citations section depends on claim support sets and is rendered
  deterministically.
- Pages cite source **paths**.

**Why.** Adding a duplicate source, deleting one of several supporting sources,
or renaming a file changes provenance but not knowledge. Those cases should
cost zero LLM calls (PRD §4.5: "do not require an LLM for operations that can
be performed deterministically").

## ADR-4: Summaries depend on page *leads*

**Decision.** A topic summary's signature covers each member page's title and
lead paragraph, not the full body.

**Why.** It is an explicit, inspectable dependency narrowing. Without it, any
edit to a hub page regenerates every summary containing it (the WeKnora #4016
write-amplification pattern).

## ADR-5: SQLite with temporal tables as the single source of truth

**Decision.**
- Tables: `sources`, `artifacts` and `edges`, each with
  `from_build`/`to_build` validity.
- Commit: one transaction per build.
- Markdown output is a projection.

**Why.**
- Atomic commit comes from SQLite.
- Snapshots, history, diff and rollback are queries.
- No server is needed (local-first).

**Rejected.**
- *Copy-on-write snapshot directories* (GraphRAG, OpenKB): O(KB) per build.
- *Postgres*: an unnecessary dependency for the MVP. The schema is plain SQL
  and portable later.

## ADR-6: Compute phase is side-effect free (except the content-addressed cache)

**Decision.**
- All provider calls happen before the commit transaction, into an in-memory
  ChangeSet.
- The extraction cache is written as results arrive and is never rolled
  back.

**Why.**
- A failure at any point leaves knowledge untouched (PRD §4.6).
- Retries reuse paid work.
- Cache entries are true for their key regardless of build outcome.

## ADR-7: NetworkX for analysis, SQL for the hot path

**Decision.**
- Reverse-dependency traversal in the compiler runs over indexed SQL and
  in-memory maps.
- NetworkX is used for `dakc graph` and `dakc explain`, cycle detection,
  export (JSON, DOT, React Flow) and tests.

**Why.** At 5,000 documents the graph has hundreds of thousands of edges.
Building a NetworkX graph on every compile would dominate no-op compiles. The
PRD asks for NetworkX "initially". It is used where its algorithms add value.

## ADR-8: BLAKE3 for hashing, 64-bit id prefixes

**Decision.**
- BLAKE3 (`blake3` package) for all content hashes, full 256-bit stored.
- Ids use the first 16 hex digits.

**Why.**
- BLAKE3 is fast, with wheels for all platforms.
- At 10⁶ claims the birthday collision probability is ≈ 3·10⁻⁸.

## ADR-9: Sources are hashed every scan (no mtime shortcut)

**Decision.** `dakc check` and `dakc compile` read and hash every file.

**Why.**
- The PRD asks for content hashing "rather than modification timestamps
  alone".
- BLAKE3 on 5,000 small files takes well under a second.

An mtime/size pre-filter (Git's index approach) is a possible later
optimization. It would need racy-timestamp handling.

## ADR-10: Stable source ids survive renames; comparisons canonicalize

**Decision.**
- Source id = `src:` + slug of the first-seen path.
- A rename (same hash, old path gone) keeps the id.
- The full-vs-incremental equality check compares sources and passages
  through their current paths.

**Why.**
- History continuity across renames, as the PRD requires.
- Zero recomputation besides citation rerender.

## ADR-11: Deterministic providers are first-class

**Decision.** Ship a heuristic extractor (sentence claims, capitalized-term
entities) and template page and summary writers. These are the defaults.

**Why.**
- The core benchmark and tests run with no network, keys or GPU (PRD §9, §12).
- Byte-level equality with a full rebuild becomes a testable invariant.

LLM providers implement the same protocols.

## ADR-12: One small client protocol; generic HTTP for OpenAI-compatible servers, official SDK for Claude

**Decision.**
- `OpenAICompatibleClient` is a thin `httpx` client for `/v1/chat/completions`.
  It covers OpenAI, Ollama, vLLM and LM Studio.
- `AnthropicClient` uses the official `anthropic` SDK, installed as an
  optional extra (`dakc[anthropic]`).
- The Claude client does not send `temperature`, which current models reject.
  It uses structured outputs for extraction and enables server-side refusal
  fallbacks.

**Why.**
- One generic HTTP path serves every OpenAI-compatible local server without
  vendor SDKs.
- For Claude, the official SDK gives typed responses, retries and current
  request features, and keeps core dependencies small.
- Model output is not reproducible. Correctness rests on DAKC's
  content-addressed cache and signatures, not on model determinism.

## ADR-13: Fuzzy claim matching never suppresses a rebuild

**Decision.** RapidFuzz `token_sort_ratio` (≥ 85, one-to-one) pairs a
retracted claim with the new claim that most likely revised it. The pairs are
used for history and reporting only, and are stored in the build record.
Invalidation is driven by content hashes alone.

**Why.**
- "Version 15" → "version 16" is a real change.
- Using similarity to skip work would trade correctness for cost, which the
  PRD forbids.

## ADR-14: Each passage records the extractor that produced its claims

**Decision.** A passage stores `extracted_by`, the extractor fingerprint
(algorithm, model, prompt). A passage whose `extracted_by` differs from the
current extractor is re-extracted even though its text is unchanged.

**Why.** Without this, changing the extraction model or prompt left every
unchanged passage with claims from the old extractor, while the README
claimed that a model change invalidates what that model built. Found while
designing the real-LLM experiment; fixed and covered by
`test_extractor_change_reextracts_unchanged_passages`.

## ADR-15: Anchored re-extraction (superseded by ADR-17, removed)

**Decision (historical).** Showing the model the previous claims of an
edited passage ("anchoring") was briefly available as `strict` or `loose`,
off by default. It was removed in favour of deterministic sentence reuse
(ADR-17).

**Why.** A live call showed the risk. The passage changed "PostgreSQL supports
JSONB" to "PostgreSQL stores JSONB in a binary format", and the anchored model
returned the old claim, a stale fact. Anchoring trades correctness risk for
fewer regenerated pages, so it must be justified by measurement, not assumed.
The first real-LLM experiment measured both sides (raw results:
`benchmarks/results/archive/llm-gpt-4o-mini-anchoring.json`).

**Outcome (gpt-4o-mini).** `off` was best on synthetic text: unchanged facts
reproduced verbatim, no stale claims, every new fact captured. On natural
prose, `strict` raised unchanged-fact reproduction by about 5 points but
captured the new fact in far fewer paragraphs. `loose` kept stale facts and,
end to end, lost every new fact of a rewritten paragraph. Anchoring was
therefore never enabled by default, and was later removed.

## ADR-16: One JSON repair retry for LLM extraction

**Decision.** If an extraction response is not valid JSON, the extractor asks
once more for strictly valid JSON. If that also fails, the error propagates
and the build fails without committing anything. Both calls are counted in
usage.

**Why.** Technical prose with quotes and code identifiers produced an invalid
JSON response in the first real-LLM run, which aborted the run. A single
bounded retry fixes the common case without hiding persistent failures.

## ADR-17: Sentence-grounded extraction with verbatim reuse

**Decision.** Claims carry sentence-level evidence. When a passage changes,
claims whose evidence sentences are unchanged are reused verbatim, and only
changed sentences are extracted, with the passage as context. A sentence that
may depend on earlier context (pronoun, "the X", continuation word) is
re-extracted whenever an earlier sentence changed. The anchoring feature
(ADR-15) was removed.

**Why.** The first real-LLM run showed that re-extracting a whole edited
paragraph rewords its unchanged facts. Only about 40% of claims reproduced
verbatim, even with no edit at all, so claim-level cutoff barely worked on
real prose. Anchoring tried to fix this by asking the model to reuse
wording, and was measured to keep stale facts and miss new ones (raw results:
`benchmarks/results/archive/llm-gpt-4o-mini-anchoring.json`). Reuse decided
deterministically, by sentence hashes, does not depend on the model repeating
itself. The LLM is only asked about sentences that actually changed.

**Measured (gpt-4o-mini, docs/benchmark.md).**
- Unchanged facts kept with identical ids went from about 50% (whole-paragraph
  re-extraction) to about 80–90% on technical and explanatory prose, with zero
  stale claims and fewer output tokens.
- A variant trusting only the model's declared dependencies (no pattern rule)
  reached about 96–100%, but in the coreference set left 5 stale claims (10% of those that should have been retracted).
  The pattern rule therefore stays.

**Rejected.**
- *Per-sentence extraction without context*: pronouns and abbreviations
  could not be resolved.
- *Semantic (embedding) matching of old and new claims*: it would have to
  decide "same fact?" by similarity, which is the anchoring failure mode in
  another form.

## ADR-18: Claim identity ignores cosmetic wording; extraction asks for concepts too

**Decision.**
- Claim identity normalization drops quote, backtick and markdown emphasis
  characters (`"MD5"`, `'MD5'` and `` `MD5` `` are one claim).
- The LLM extraction prompt asks for at least one entity per claim, allowing
  specific technical concepts ("file watcher") as well as named entities.

**Why.** A live trace showed the model correctly extracting "The watcher
checks size and mtime." with an empty entity list. DAKC then discarded it
because it had no page to attach to. Quote style also varied between calls.

## ADR-19: Workspace build lock plus a HEAD check at commit

**Decision.**
- An OS file lock (`.dakc/build.lock`) covers each whole build: recovery,
  load, compute, commit and projection.
- The commit transaction also refuses to commit if HEAD moved since the state
  was loaded.
- Readers never lock.

**Why.** SQLite serialized only the commit. Two builds computed from the same
HEAD would both commit, losing an update. Crash recovery could also have
marked another process's live build as aborted. The HEAD check is defence in
depth for writers that bypass the lock.

**Rejected.** Database-level advisory locks: SQLite has none, and holding a
write transaction during LLM calls would block readers for minutes.

## ADR-20: Version DAKC's own logic, per layer

**Decision.** `dakc/versions.py` holds versions for the parser, claim
identity, entity resolution and rendering. They are recorded with every build
and restored by rollback. A mismatch re-runs only that layer. Page-writer and
summarizer drift is detected per artifact (`generator` field), like the
extractor's `extracted_by`.

**Why.** Provider fingerprints covered models and prompts, but not DAKC's
own deterministic code. A renderer or normalizer change would have left old
artifacts marked current, and a page-writer model change regenerated nothing
unless a source changed. Both are covered by `test_model_drift.py`.

## ADR-21: Strict provenance is a rendering mode, not a generation constraint

**Decision.** With `provenance = "strict"`, rendering drops any generated
sentence that cites none of the page's claims, and keeps the dropped list for
audit. The default is `report`: the generated text is shown as written, and
coverage is reported by `dakc verify`.

**Why.** With the measured model, about 28% of LLM-written page sentences
cited no claim, often embellishments no source supports. Filtering
deterministically at render time makes "every displayed sentence is
traceable" hold by construction, at no LLM cost. It is not the default
because it removes text. That is a product decision for the user.

## ADR-22: Claim identity stays exact; equivalence is advisory only

**Decision.** Claims are never merged by similarity. `dakc duplicates`
reports likely paraphrase pairs using a conservative deterministic matcher.

**Why.** On a labelled set with hard negatives (`benchmarks/equivalence.py`),
naive fuzzy matching merged negations, value changes and reversed relations
(64 false merges). The guarded matcher made no false merges, but found only a
third of the true paraphrases. An LLM judge did well on the templated set,
but real text is harder, and a false merge silently corrupts knowledge.
Redundant claims are the safer failure.

## ADR-23: One library API; the CLI, REST and MCP layers are thin

**Decision.** `dakc.kb.KnowledgeBase` is the only place where operations
are orchestrated: taking the lock, compiling, writing the wiki projection,
mapping failures to errors. Every method returns a dataclass with `to_dict()`.
The CLI, the REST API and the MCP server only parse arguments and present the
results. Errors form one hierarchy under `dakc.DakcError`, and each error also
derives from the matching built-in.

**Why.** Before v0.2 the orchestration lived inside CLI command functions.
The REST API duplicated parts of it and leaked a SQLite connection per
request. A second and third front end would have meant a second and third
copy of the lock and projection logic, which is exactly where concurrency
bugs hide. With one code path, a test of the library covers every front end.

**Trade-off.** `CompileFailed.preserved` is computed from HEAD (did it
move?), not from a full state digest. Hashing the whole store would cost
seconds per build at 20,000 documents. The transactional commit is what
guarantees preservation, and fault-injection tests check it with full
digests.

## ADR-24: The MCP server previews by default and is bound to one workspace

**Decision.** `dakc mcp` serves exactly one workspace, fixed at start-up.

- **Inspection tools** are annotated read-only.
- **`compile`** returns a plan unless called with `apply=true`. With an LLM
  provider it refuses a build planned to exceed `--max-llm-calls`.
- **`rollback`** needs an explicit build id and is annotated destructive.
- **`--read-only`** drops both mutating tools.
- **Every list output is capped**, and totals are always reported.
- **Each tool call opens its own `KnowledgeBase`.**

**Why.** An agent calling tools is an untrusted, cost-unaware caller.

- **Previewing first** costs nothing, because a dry run is free and exact for
  free providers. It turns an accidental full rebuild into a visible plan.
- **The cost guard** uses the plan's upper bound, so it can only refuse too
  much, never too little.
- **Bounding outputs** keeps a hub page or a big build from flooding the
  model's context.
- **Binding to one workspace** stops an agent from compiling arbitrary
  directories.
- **A connection per call**, because the SDK runs synchronous tools on
  worker threads.

**Not done.** There are no MCP resources or prompts yet; the tools cover the
use cases. There is no authentication for HTTP, so (since v0.3.0) both HTTP
servers refuse non-loopback binds and reject non-local `Host` headers.

## ADR-25: Integrate with existing docs tools as a plugin

**Decision.** The first integration is a MkDocs plugin, not a new site
generator or a hosted service.

- **One line in `mkdocs.yml`.** DAKC compiles the `docs_dir` incrementally in
  `on_pre_build`, so this runs for every build and every `mkdocs serve`
  reload.
- **Virtual files.** The generated pages are added as virtual files
  (`File.generated`) under a prefix. Nothing is written into `docs_dir`.
- **Separate state.** The workspace lives outside `docs_dir`, which makes it
  easy to cache in CI.
- **Links back.** Citations link to the source docs pages.
- **Page filter.** Pages backed by fewer than `min_claims` facts are not
  published. References to them are dropped or unlinked, so strict builds
  pass.

**Why.**

- *Adoption.* The question to answer was whether a developer can add DAKC to
  an existing docs workflow without replacing their tools. A plugin changes
  one line.
- *Language.* MkDocs is Python, so the plugin calls the library directly.
- *Correctness.* Virtual files avoid a feedback loop, where DAKC would ingest
  its own output, and avoid polluting the docs repository.
- *Build time.* `min_claims` exists because MkDocs renders every page on
  every build. On FastAPI's docs, publishing all 2,336 entity pages made
  builds 6 times slower, and most of those pages held one fact.

## Libraries reused rather than reimplemented

| Need | Library |
|---|---|
| Graph algorithms, export | `networkx` |
| Hashing | `blake3` |
| Models / validation | `pydantic` v2 |
| CLI | `typer` (+ `rich` for tables) |
| Markdown parsing | `markdown-it-py` |
| HTML parsing | `beautifulsoup4` |
| PDF text | `pymupdf` (optional extra) |
| Fuzzy claim matching | `rapidfuzz` |
| File watching | `watchdog` (optional extra) |
| HTTP API / clients | `fastapi`, `httpx` |
| MCP server | `mcp` (official Python SDK, v2; optional extra) |
| MkDocs integration | `mkdocs` plugin API (optional extra) |
| Tests | `pytest` |
