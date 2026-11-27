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
- **The cost guard** uses the plan's estimate. Extraction calls are exact,
  and the pages and summaries new facts would touch are estimated with the
  heuristic extractor, so it catches unexpectedly large builds. It is not a
  hard ceiling: an earlier version of this ADR said "upper bound", which was
  wrong for edits that add facts.
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

## ADR-26: A read-only web UI with no build step

**Decision.** `dakc serve` serves a single-page UI as static files from the
package (`dakc/api/static`): plain HTML, CSS and ES-module JavaScript, with no
framework, bundler or CDN.

- **Read-only.** It reads the existing REST API, plus a few endpoints added
  for it (`read`, `neighborhood`, `titles`, titled diffs). Compiling stays in
  the CLI, the Python API and MCP.
- **Local-only.** The same loopback and `Host` checks as the API.
- **The overview** draws the knowledge base as layers, sources at the bottom
  and the index at the top. Each layer is sized by its artifact count, and
  the next (or last) compile's changes are marked in a single accent colour.
  The point is to make "how little was rebuilt" visible at a glance.

**Why.** The review advised against a heavy React frontend until users ask
for one, and the user asked for a UI.

- *No build step* means nothing to install, nothing to keep patched, and the
  UI works offline in the wheel.
- *Read-only* means the UI cannot spend money or rewrite the knowledge base.

The UI was checked in a real browser with Playwright: every view on desktop,
in dark mode and at 390 px wide, with no console errors and no horizontal
overflow.

## ADR-27: Bound every artifact's input with content-defined sections

**Decision.** Hub pages (more than 64 claims) and large topic summaries (more
than 64 pages) are built in sections.

- Section boundaries are content-defined: an item ends a section when its
  key's hash is 0 modulo 32, with a cap of 128 items.
- Each section is cached by a signature over its inputs.
- The parent is assembled (pages) or summarized (topics) from the sections.
- Hub pages number citations per section and renumber them, page-wide, at
  render time. They have a deterministic lead.
- Smaller artifacts are unchanged. Versions `page/2` and `summary/2` record
  the change, so existing knowledge bases migrate on their next build.

**Why.** FastAPI's replay showed where the remaining cost went (measured per
stage): one topic summary over about 1,660 pages and one hub page over 3,491
claims were rewritten in full on almost every commit. An external review
suggested hierarchical summaries; the measurement showed hub pages needed the
same fix.

- **Content-defined boundaries** were chosen over fixed-size chunks, which
  shift on every insertion, and over semantic clustering, which is
  nondeterministic and would break full-rebuild equality.
- **Hub claims are ordered by claim text**, as on small pages, so a section's
  membership depends only on its claims. Ordering by source and heading was
  tried first, because it keeps each section about one document region. But
  it made sections depend on provenance: a fact whose citations moved could
  change sections and rewrite two of them. That broke the rule that moved
  citations only re-render. The 100-document benchmark caught it: a
  one-paragraph edit cost twice as much as before sections.

**Trade-offs.**

- A full build makes 3–5% more calls.
- A hub page's lead is no longer LLM-written.
- An edited fact can move between sections, because its claim id changes, so
  it may rewrite two sections.
- A hub section is not one document region. Its heading names the
  documents it draws on instead.
- Dry runs with an LLM provider now estimate the facts in new text with the
  heuristic extractor. Before this change they assumed none, which made their
  page and summary counts too low. The docs had called those counts an upper
  bound. The plan is now labelled an estimate.

## ADR-28: Sources and publishers; publishers converge on state

**Decision.** Where content comes from (source connectors) and where compiled
knowledge goes (publishers) are plugin interfaces, separate from the
compiler. The full contract is in `docs/rfcs/0001-sources-and-publishers.md`.

- **Connectors.** A connector returns a complete listing of versioned items
  on every build, and reads them. The filesystem is the first connector.
- **Namespacing.** Several sources share one namespace, each under its own
  path prefix.
- **Identity.** Source identity follows native keys where a system has them,
  and paths plus content-hash renames where it does not.
- **The publication ledger.** A publisher's work is the difference between
  the current state and a ledger of what it has acknowledged. It is not a log
  of events.

**Why.** Teams keep knowledge in several systems and consume it in several
places. The compiler's correctness argument depends on none of those
details, so they should not be inside it.

- **Reconcile against a full listing, instead of trusting change
  notifications.** A missed event then delays an update but cannot corrupt
  state.
- **A ledger, instead of an outbox of events.** Retries, crashes, rollbacks
  and new publishers become one operation: send the difference. The Markdown
  projection already worked this way with its manifest.

**Trade-offs.**

- Listing a large remote source on every build costs API calls. Change feeds
  (a phase 2 capability) can narrow what is listed, but the listing remains
  the source of truth.
- Records are per artifact, so a publisher sees a page's whole text when any
  part of it changes, not a text diff.
- The ledger grows with the number of published artifacts: one row each, per
  publisher.
- Permissions are designed but not implemented. They must propagate through
  claims, pages and summaries, which is a compiler concern (RFC 0001,
  phase 3).

## ADR-29: Claims that only past documents state are rendered as History

**Decision.** A claim whose every supporting document a `[[conflicts.scope]]`
rule declares `status = "past"` is listed in a deterministic `## History`
section of each page that shows it, after the body, with its citation. The
page writer is given the current claims only. Any current support makes a
claim current. A page whose every claim is history gets a one-sentence
deterministic body and no writer call.

- **Declared status only.** History follows the rules (`rule_status`), not
  the scope RFC 0002 infers from tense, headings and paths.
- **Signatures.** The generation signature covers the current claims, the
  render signature the History list. The status part of the rules (pattern
  and status, in order) is in the config fingerprint, added only when some
  rule has a status.
- **Version.** `RENDER` goes to `render/2`. `PAGE` is unchanged, since
  bumping it would rewrite every hub section.

**Why.** Session notes (one per working session, under `sessions/`) record
decisions that later notes or current documents reverse. Showing "the
password hashing algorithm is Bcrypt" beside "… is Argon2" as two current
facts is wrong, and dropping the old one loses history the notes exist to
keep. The scope rules already say which documents describe the past (RFC
0002), so pages can follow them.

- **Why not inferred scope.** Inference is tuned for conflicts: a wrong
  "past" only turns a finding into a scoped one. On FastAPI's documentation
  it would have moved 68% of the claims to History, on 70% of the pages,
  including present-tense facts under headings such as "Previous tools".
  A declared status is explicit, reviewable, and changes nothing for a
  workspace without one.
- **Why the writer does not see history claims.** Given the old value as a
  fact, a model writes it as one. Filtering its sentences afterwards (as
  strict provenance does) cannot fix a sentence that merges both values.
- **Why it is in the signatures.** Whether a claim is history depends on
  the paths of all its supporting documents and on the rules, so it is a
  page input. With the split in both signatures and the rules in the config
  fingerprint, a claim that moves rebuilds exactly the pages that show it,
  and a randomized test (moves across the boundary, rule changes) keeps
  incremental equal to full.

**Trade-offs.**

- A claim that moves between current and history regenerates the bodies of
  its pages (writer calls), not only their History sections, because the
  writer's input changed.
- Changing a rule's status re-checks every page. Only the affected ones are
  rebuilt, but the check reads every claim once.
- A heading such as "History" in a current document does not make its facts
  history. Put such documents under a folder a rule declares past.

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
