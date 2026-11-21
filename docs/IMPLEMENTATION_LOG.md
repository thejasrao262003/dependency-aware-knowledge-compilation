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

## Not done (deliberately or for lack of resources)

- No React frontend. The API exposes React-Flow-shaped graph JSON for one.
- No real-LLM benchmark. No local model or API key was available, so the
  harness supports it but it was not run.
- Semantic (embedding) claim matching was not added. Deterministic matching
  plus content addressing was sufficient for the benchmark scenarios, and the
  PRD asks for deterministic-first.
