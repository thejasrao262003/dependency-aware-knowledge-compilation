# Implementation log

A running record of what was done, in order, and why. Dates are 2026-11-21
unless noted.

## Phase 0: research and validation

1. **Cloned and audited the seven PRD repositories** at the commits listed
   in `research/COMPETITIVE_ANALYSIS.md`. Each was read against the PRD's ten
   research questions, with file:line evidence (`research/audits/`). Nothing
   from them was executed.
2. **Searched for further prior art.** The search surfaced
   atomicstrata/llm-wiki-compiler (the most complete incremental LLM wiki
   found), trickle-labs/riverbank (documents a design close to DAKC's) and
   PrefectHQ/colin (a dbt-style LLM document compiler). Microsoft GraphRAG and
   LightRAG were also inspected as graph-RAG systems with update or delete
   paths.
3. **Re-verified load-bearing claims directly**, especially any claim that a
   competitor *does* implement something:
   - Riverbank: `record_artifact_dep` argument-order bug; `mark_pages_stale`
     is never called.
   - GraphRAG: `deleted_inputs` is never used.
   - llm-wiki-compiler: deletion disables extraction reuse; seed pages call
     the LLM on every compile.
   - llm_wiki: the merge prompt preserves "every factual claim".
   - LLM-WIKI: deletion is forbidden; incremental recompile is on the roadmap.
   - LightRAG: the maintainer's delete-then-reinsert update path.
4. **Research gate** (`research/TECHNICAL_GAP.md`): no audited project
   implements claim-level, multi-layer, signature-based incremental
   compilation with early cutoff, and none tests incremental against full.
   Decision: proceed. An upstream contribution was considered and deferred:
   the target projects would need a new artifact layer, not a patch.
5. Wrote the dependency model, architecture, ADRs and benchmark design before
   implementation code.

## Phases 1–3: core engine, compiler, reliability

6. Implemented hashing/normalization, parsing (Markdown, TXT, HTML, JSON,
   PDF), source scan with rename pairing, segmentation into content-addressed
   passages, and the heuristic extractor.
7. Implemented the artifact model (content hash vs state hash, edges derived
   from content) and the SQLite temporal store (`from_build` / `to_build`).
8. Implemented the compiler as layered stages with signatures and early
   cutoff, plus the `file` and `full` strategies inside the same engine so
   they can be compared fairly.
9. **First end-to-end check (100 docs):** incremental equal to full;
   one-paragraph edit = 5 provider calls vs 663 for a full build; recompile is
   a no-op.
10. Added the transaction manager: single-transaction commit, fault injection
    (`extract`, `page`, `summary`, `commit:n`), rollback through the temporal
    tables, crashed-build recovery, and state digests.
11. **Design change:** passage payloads now store their occurrence index, so
    two stores can be compared canonically when a rename has given the same
    file different source ids in each.
12. **Design change:** extraction-cache writes are batched and flushed in a
    `finally` block. Paid provider work survives a failed build and is
    reused on retry, without writing one row per call.

## Phases 4–5: providers, CLI, API

13. Added LLM providers behind one small client protocol: OpenAI-compatible
    (`httpx`; OpenAI/Ollama/vLLM/LM Studio) and Claude via the official
    `anthropic` SDK (optional extra).
    - The Claude client sends no `temperature`, uses structured outputs for
      extraction, and enables refusal fallbacks.
    - ADR-12 was revised from "httpx for everything" to "official SDK for
      Claude".
    - Anchored re-extraction: previous claims of an edited passage are passed
      to the LLM so unchanged facts keep their wording. **Not measured**: no
      LLM endpoint was available in this environment.
14. CLI (`ingest, check, compile [--dry-run|--full|--strategy], graph,
    explain, status, builds, diff, rollback, verify [--against-full], serve,
    watch`) and a read-only FastAPI service.
15. Fixed during manual end-to-end use:
    - the renamed-sources count was overwritten in reports;
    - an API route-order bug made `/artifacts/{id}/content` unreachable;
    - the console was too narrow for non-TTY output.
16. Heuristic extractor: the minimum sentence length was lowered from three
    words to two so JSON fields (`producer: Ledger`) yield claims. The
    fingerprint was bumped to `heuristic-extractor/2` so cached extractions
    are invalidated.

## Tests and benchmark

17. Test suites:
    - unit: hashing, parsing, claims, ingestion, graph, transactions;
    - integration: compiler semantics, CLI Definition-of-Done flow, REST API;
    - regression: every scenario × {dakc, file} equals full, plus a seeded
      12-step random edit sequence with renames, duplicates, deletions and
      paragraph swaps;
    - benchmark properties.
18. Benchmark harness (`benchmarks/run.py`) and report generator
    (`benchmarks/report.py`). The first full run was stopped and repeated
    after the extractor fingerprint change, so the published results come from
    the final code.
19. **Benchmark scenario bug found and fixed.** In the second run, S7
    ("contradicting source") cost DAKC 0 LLM calls at 1,000 and 5,000
    documents. Investigation showed the generated "contradicting" sentence
    already existed verbatim in 3 and 122 documents, because the attribute value
    space is small. The scenario was therefore measuring "a new source repeats
    a known claim". S7 now asserts a value outside the generator's ranges, and
    the benchmark was re-run. The engine was not at fault: it correctly
    treated the restatement as provenance-only.
20. Added a real-crash test: an `exit` fault mode calls `os._exit` inside the
    commit transaction in a subprocess. The previous build survives, and the
    orphaned build is marked `aborted` on the next run.
21. Added provider tests against fake transports: OpenAI-compatible over
    `httpx.MockTransport`, and a fake Anthropic SDK client through an
    injection seam. They include a full compile through the LLM components.
    They verify DAKC's request and response handling only, not any real
    model.

## Real-LLM phase (2026-11-22)

22. Added a vendor-neutral `custom` provider (`factory = "module:callable"`)
    so any LLM client can be plugged in without code changes in DAKC.
23. A first live extraction call showed anchored re-extraction keeping a
    stale fact (ADR-15). Anchoring was made configurable (`off` / `strict` /
    `loose`), default `off`, and an experiment was designed to measure the
    benefit (unchanged facts keep their claim ids) against the harm (edited
    facts survive, new facts are missed).
24. While designing that experiment, found that an extractor change did not
    re-extract unchanged passages, contradicting the README. Fixed with
    per-passage `extracted_by` (ADR-14).
25. The first real-LLM run aborted on one invalid JSON response. Added one
    bounded repair retry (ADR-16). The experiment now records per-item
    failures instead of crashing.

## Making the real-LLM path work (2026-11-22)

26. **Diagnosis of the poor real-LLM numbers.**
    - Claim identity was a hash of the LLM's wording.
    - Re-extracting a whole edited paragraph regenerated, and reworded, its
      unchanged facts.
    - Anchoring asked the model to do a comparison inside generation.
    - The measurement itself had two flaws: the code-dense test text was a
      worst case for verbatim reproduction, and the stale metric counted
      edits whose target appeared twice in a sentence.
27. **Sentence-grounded extraction (ADR-17).**
    - Every claim carries its evidence sentences.
    - Changed passages reuse the claims of unchanged sentences verbatim, and
      only changed or context-dependent sentences are sent to the model.
    - Deletions and reorderings cost zero calls.
    - Anchoring was removed.
28. **A live trace exposed two more extraction issues (ADR-18).**
    - Correct claims with no named entity were being discarded. The prompt now
      also asks for technical concepts.
    - Quote and backtick variation was minting new claim ids. Identity
      normalization now ignores them.
29. **Measurement fixes.**
    - The private client now uses server-enforced JSON schemas. The second
      experiment had zero provider failures, down from 5 of 36 paragraphs.
    - Edits now only target entities that occur exactly once in a paragraph.
30. **Three-way experiment: passage vs sentence vs evidence-only.** Evidence-only
    is the most efficient, but left stale facts in pronoun cases, so the
    conservative context rule remains the default.
31. The deterministic benchmark was re-run on the final code, because
    sentence reuse changes extraction call counts. A deleted sentence no
    longer costs a call.

## Release hardening (2026-11-22)

An external review asked five questions before going public: claim identity,
contradictions, provenance, concurrency, model drift. It also asked for a
reproducible benchmark command and a clean CI run.

32. **Concurrency (two real bugs).** Concurrent builds could both commit from
    the same HEAD (a lost update), and crash recovery could mark a live build
    as aborted. Fixed with the workspace build lock and the HEAD check
    (ADR-19). Tests cover interleaving and two real processes.
33. **Model drift (two real bugs).** A page-writer or summarizer change
    regenerated nothing unless a source changed. DAKC's own logic (parser,
    claim normalization, rendering) was not versioned. Fixed with per-artifact
    generator checks and per-layer logic versions (ADR-20). Seven drift tests.
34. **Provenance.**
    - Evidence sentence indices are now stored on every claim support.
    - New `dakc trace`.
    - `verify` checks citation markers and evidence ranges, and reports
      citation coverage.
    - Real-model finding: about 72% of LLM-written page sentences cite a
      claim.
    - Added the `strict` provenance mode (ADR-21).
35. **Contradictions.** Explicit tests, plus benchmark scenario S9
    (contradiction, then the original source deleted).
36. **Claim identity.** A labelled equivalence evaluation with hard negatives,
    covering exact, naive fuzzy, guarded and LLM-judge matchers. Policy:
    identity stays exact, and duplicates are advisory (ADR-22, `dakc
    duplicates`).
37. **Reproducibility.**
    - `uv.lock` and a Makefile.
    - `benchmarks.run --verify-against`, which CI uses to require the 100-doc
      benchmark to reproduce every non-timing metric.
    - A realistic corpus profile and a 20,000-document scale run.
38. **Wording.** The competitive conclusion is now stated as "not found in
    the twelve projects audited".
39. **Corpus generator at 20,000 documents.**
    - The first scale run never started compiling. A 20,000-document corpus
      needs 10,000 distinct entity names, but three-part names can only make
      4,800, so the rejection loop could not finish.
    - Fix: past three quarters of the three-part name space, the generator
      switches to five-part names.
    - Entity sampling also rebuilt its cumulative weights on every draw
      (O(entities) per call). They are now computed once; the random stream
      is unchanged.
    - Every published corpus (100, 1,000 and 5,000 documents, both profiles)
      was checked byte for byte against the old generator and is identical.
      20,000 documents now generate in about 4 s.
    - A regression test covers this.
40. **Python API (v0.2).**
    - Added `dakc.kb.KnowledgeBase` and `dakc.open` / `init` / `ingest`,
      with typed results and the `dakc.DakcError` hierarchy (ADR-23).
    - The CLI and the REST API were rebuilt on top of it. The REST API now
      opens one connection per request and closes it; before, it leaked one
      per request. It also gained `/artifacts`, `/trace`, `/downstream` and
      `/duplicates`.
    - `dakc status` and `dakc builds` gained `--json`.
    - `BuildReport.has_work()` separates real work from cutoffs (checks that
      found nothing to do).
    - While writing the API docstring, I found that `rollback` discards the
      artifact versions of later builds rather than keeping them. The API and
      MCP descriptions now say so, and MCP marks it destructive.
41. **MCP server (v0.2).**
    - `dakc mcp` is built on the official MCP Python SDK 2.x. Version 2
      renamed `FastMCP` to `MCPServer` and changed other APIs. Rather than
      writing from memory of 1.x, I read the API from the installed package
      and probed its behaviour (structured results, `ToolError`, annotations,
      the in-process client) before writing any server code.
    - 12 read-only tools, plus `compile` (preview by default, with an LLM cost
      guard) and `rollback` (explicit, destructive). It also has a read-only
      mode, bounded outputs, and a connection per call (ADR-24).
    - Tests use the official client, both in-process and over a real stdio
      subprocess, which catches any stray output on stdout. The
      streamable-HTTP transport was smoke-tested by hand.
42. **Clean-up.** Removed five unused imports and variables flagged by
    pyflakes, including a stale comment in the real-LLM experiment that
    contradicted the code below it. Behaviour is unchanged, and the benchmark
    still reproduces exactly.

## Not done (deliberately or for lack of resources)

- No React frontend. The API exposes React-Flow-shaped graph JSON for one.
- Real-LLM results cover one hosted model (`gpt-4o-mini`) on small corpora
  (see docs/benchmark.md). Other models were not measured.
- Semantic (embedding) claim matching was not added. Deterministic matching
  plus content addressing was sufficient for the benchmark scenarios, and the
  PRD asks for deterministic-first.
