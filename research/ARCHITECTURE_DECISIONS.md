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
| Tests | `pytest` |
