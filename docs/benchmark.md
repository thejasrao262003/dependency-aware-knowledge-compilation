# Benchmark design

This document defines the benchmark *before* any results exist. Measured results
are appended in the "Results" section by `python -m benchmarks.run`; nothing in
this document is an expected outcome.

## Questions the benchmark must answer

1. Does incremental compilation produce **the same knowledge base as a full
   rebuild** of the changed corpus? (correctness)
2. How much work does it avoid compared with (a) a full rebuild and (b) the
   **file-level invalidation** strategy used by existing LLM-wiki tools?
   (efficiency)
3. Does it ever regenerate an artifact whose inputs did not change, or keep an
   artifact whose inputs did? (precision / staleness)

## Strategies compared

All strategies run inside the same engine, on the same corpus, with the same
providers, so the only variable is the invalidation policy.

| Strategy | What it does | Models |
|---|---|---|
| `full` | Discard derived state and extraction cache, recompute every artifact. | Correctness reference (PRD §8 invariant 8). |
| `file` | A changed source is treated as wholly dirty: every passage is re-extracted without cache, and every artifact transitively downstream of the source's old *or* new claims is regenerated. No early cutoff. Renames are delete + add. | The strategy found in llm_wiki, okforge, LightRAG (delete + re-insert) and Riverbank's documented design — see `research/COMPETITIVE_ANALYSIS.md`. This is an *emulation of the strategy*, not those projects' code. |
| `dakc` | Passage-level verifying traces, content-addressed claims with support counting, signature-based rebuild decisions and early cutoff at every layer. | The proposed engine. |

## Corpus generator

`benchmarks/corpus.py`, fully seeded (`--seed`, default 7), so every run of a
given size produces byte-identical files.

- Sizes: **100, 1,000, 5,000** documents.
- Documents are Markdown, grouped into topic folders (`topic-00/ … topic-NN/`,
  ~50 docs per topic), each with 2–4 sections of 1–3 paragraphs.
- Each paragraph holds 1–4 factual sentences of the form
  `<Entity> <relation> <Entity|value>.` drawn from a fixed entity vocabulary
  whose size scales with the corpus (`max(40, n_docs // 2)`), with Zipf-like
  popularity so some entities are shared by hundreds of documents.
- ~5 % of paragraphs are copied verbatim into a second document, so some claims
  have multiple independent sources (exercises support counting on deletion).

## Change scenarios

Each scenario is a deterministic mutation applied to a copy of the corpus.

| # | Scenario | Mutation |
|---|---|---|
| S0 | No change | Nothing (idempotence check). |
| S1 | One source modified | Rewrite one paragraph of one document. |
| S2 | Five unrelated sources modified | S1 applied to five documents in five different topics. |
| S3 | One source deleted | Delete one document that shares at least one claim with another document. |
| S4 | One source renamed | Move one document to a new path, content unchanged. |
| S5 | One claim modified | Change one word in one sentence of one document. |
| S6 | Multiple sources → same entity | Add a new document whose claims all mention the most popular entity. |
| S7 | Contradicting source | Add a document asserting a different value for an existing (entity, relation) claim. |
| S8 | Source affecting many pages | Rewrite a paragraph whose sentences mention ≥ 6 distinct entities. |

## Oracle

For each (size, scenario):

1. `A` = `full` build of the original corpus.
2. `B` = `full` build of the mutated corpus in a fresh workspace.
3. `Δ` = the set of artifact ids whose content hash differs between `A` and
   `B`, or that exist in only one of them. Δ is the **minimum** set of
   artifacts any correct incremental strategy must change.
4. `I` = start from a copy of `A`'s workspace, apply the mutation, run the
   strategy under test.

Artifact identity for comparison is canonical: claim ids are content
addresses; entity/page/summary ids are derived from normalized names; source
ids are compared through their current path (so a rename does not count as a
difference by itself).

## Metrics

| Metric | Definition |
|---|---|
| Processed sources | Sources read, parsed and segmented. |
| Passages re-extracted | Extraction-provider invocations. |
| Claims regenerated | Claims returned by those invocations. |
| Pages regenerated | Page-writer invocations. |
| Summaries regenerated | Summarizer invocations. |
| LLM calls | Sum of provider invocations (extract + page + summary). |
| Input / output tokens | Reported by the provider. The deterministic mock *estimates* tokens as `ceil(chars / 4)`; results are labelled as estimates. |
| Execution time | Wall-clock seconds, median of 3 runs. Mock providers have ~0 latency, so this measures engine overhead, not LLM latency. |
| Unaffected artifacts modified | Artifacts ∉ Δ whose stored content hash changed in `I`. **Target 0.** |
| Unnecessary regenerations | Pages + summaries regenerated in `I` whose id ∉ Δ (work whose result was identical). |
| Incorrectly retained stale artifacts | Artifacts in `I` that are missing from, extra to, or differ in content hash from `B`. **Target 0.** |
| Equivalent to full rebuild | `I` and `B` have identical canonical artifact sets, content hashes and dependency edges. |

## Reliability checks (pass/fail, not timed)

- **Idempotence**: compiling twice in a row; the second run performs 0
  provider calls and writes 0 artifact versions.
- **Failure injection**: an exception injected (a) mid-extraction, (b)
  mid-page-generation and (c) inside the commit transaction after partial
  writes. After each, the store's state digest must equal the pre-build
  digest, and a retry must reach the same state as `B`.
- **Rollback**: after a committed build, `dakc rollback` must restore the
  previous build's state digest exactly.

## Real LLM providers

The core benchmark uses only deterministic mocks and needs no network, API
key or GPU. Real-LLM runs use a separate harness,
`python -m benchmarks.llm_experiment --provider-config <file>`, which works
with any DAKC LLM provider (`openai`, `anthropic` or `custom`). With
non-deterministic generators, content-hash equality with a full rebuild is
not expected, so that harness checks invariants and specific facts instead
(for example, did the edited value replace the old one). Semantic
equivalence is **not** claimed.

## Results

_Generated by `python -m benchmarks.report` from `benchmarks/results/latest.json`._

- Date: 2026-11-22T06:27:22Z · macOS-27.2-arm64-arm-64bit · Apple M5 (10 cores) · Python 3.12.13
- Provider: mock (deterministic; tokens estimated as ceil(chars/4))
- Strategies are run from a copy of the same initial store on the same mutated corpus; the oracle is an independent full build of the mutated corpus.
- Timing is the median of the stated number of runs. Mock providers have ~0 latency, so times measure engine overhead (scanning, hashing, SQLite), not LLM latency; LLM calls and tokens are the cost proxy.

### 100 documents

Initial full build: 0.135 s, 663 LLM calls; 608 passages, 1,085 claims, 53 entities, 341 relationships, 53 pages, 2 summaries, 5,463 dependency edges. Timing runs per strategy: 3.

**Cost: LLM calls and estimated tokens** (full / file-level / DAKC)

| Scenario | LLM calls | DAKC saving vs full | DAKC saving vs file-level | Input tokens (est.) | Output tokens (est.) |
|---|---|---|---|---|---|
| S0 no change | 663 / 0 / 0 | 100.0% | — | 72,121 / 0 / 0 | 42,869 / 0 / 0 |
| S1 one source modified | 663 / 17 / 5 | 99.2% | 70.6% | 72,151 / 10,715 / 1,654 | 42,909 / 11,232 / 1,855 |
| S2 five sources modified | 663 / 70 / 26 | 96.1% | 62.9% | 72,100 / 22,142 / 14,558 | 42,849 / 21,116 / 15,048 |
| S3 one source deleted | 658 / 13 / 6 | 99.1% | 53.8% | 71,669 / 8,992 / 3,100 | 42,662 / 9,555 / 3,276 |
| S4 one source renamed | 663 / 14 / 0 | 100.0% | 100.0% | 72,121 / 9,936 / 0 | 42,869 / 10,505 / 0 |
| S5 one claim modified | 663 / 13 / 2 | 99.7% | 84.6% | 72,121 / 9,372 / 1,924 | 42,869 / 9,789 / 1,985 |
| S6 new source → popular entity | 666 / 10 / 6 | 99.1% | 40.0% | 72,410 / 8,013 / 4,497 | 43,019 / 8,589 / 4,904 |
| S7 contradicting source | 664 / 4 / 4 | 99.4% | 0.0% | 72,205 / 2,258 / 2,258 | 42,899 / 2,551 / 2,551 |
| S8 edit touching 7 entities | 663 / 21 / 13 | 98.0% | 38.1% | 72,267 / 12,169 / 7,664 | 43,038 / 12,647 / 8,107 |

**Work performed** (full / file-level / DAKC)

| Scenario | Sources processed | Passages re-extracted | Claims regenerated | Pages regenerated (LLM) | Pages re-rendered (no LLM, DAKC) | Summaries regenerated |
|---|---|---|---|---|---|---|
| S0 no change | 100 / 0 / 0 | 608 / 0 / 0 | 1493 / 0 / 0 | 53 / 0 / 0 | 0 | 2 / 0 / 0 |
| S1 one source modified | 100 / 1 / 1 | 608 / 4 / 1 | 1494 / 10 / 3 | 53 / 11 / 2 | 4 | 2 / 2 / 2 |
| S2 five sources modified | 100 / 5 / 5 | 608 / 29 / 5 | 1493 / 71 / 15 | 53 / 39 / 19 | 4 | 2 / 2 / 2 |
| S3 one source deleted | 99 / 0 / 0 | 603 / 0 / 0 | 1483 / 0 / 0 | 53 / 11 / 5 | 6 | 2 / 2 / 1 |
| S4 one source renamed | 100 / 1 / 0 | 608 / 4 / 0 | 1493 / 9 / 0 | 53 / 8 / 0 | 8 | 2 / 2 / 0 |
| S5 one claim modified | 100 / 1 / 1 | 608 / 4 / 1 | 1493 / 9 / 1 | 53 / 8 / 1 | 0 | 2 / 1 / 0 |
| S6 new source → popular entity | 101 / 1 / 1 | 611 / 3 / 3 | 1502 / 9 / 9 | 53 / 5 / 1 | 4 | 2 / 2 / 2 |
| S7 contradicting source | 101 / 1 / 1 | 609 / 1 / 1 | 1494 / 1 / 1 | 53 / 1 / 1 | 0 | 2 / 2 / 2 |
| S8 edit touching 7 entities | 100 / 1 / 1 | 608 / 4 / 1 | 1496 / 12 / 7 | 53 / 15 / 10 | 2 | 2 / 2 / 2 |

**Correctness and precision against the oracle** (full / file-level / DAKC)

| Scenario | Artifacts that truly changed (Δ) | Stale artifacts retained | Unnecessary regenerations | Unaffected artifacts modified | Equal to full rebuild |
|---|---|---|---|---|---|
| S0 no change | 0 | 0 / 0 / 0 | 55 / 0 / 0 | 0 / 0 / 0 | yes / yes / yes |
| S1 one source modified | 19 | 0 / 0 / 0 | 47 / 5 / 0 | 0 / 0 / 0 | yes / yes / yes |
| S2 five sources modified | 99 | 0 / 0 / 0 | 30 / 16 / 0 | 0 / 0 / 0 | yes / yes / yes |
| S3 one source deleted | 34 | 0 / 0 / 0 | 43 / 1 / 0 | 0 / 0 / 0 | yes / yes / yes |
| S4 one source renamed | 27 | 0 / 0 / 0 | 47 / 2 / 0 | 0 / 0 / 0 | yes / yes / yes |
| S5 one claim modified | 10 | 0 / 0 / 0 | 54 / 8 / 0 | 0 / 0 / 0 | yes / yes / yes |
| S6 new source → popular entity | 20 | 0 / 0 / 0 | 48 / 0 / 0 | 0 / 0 / 0 | yes / yes / yes |
| S7 contradicting source | 7 | 0 / 0 / 0 | 52 / 0 / 0 | 0 / 0 / 0 | yes / yes / yes |
| S8 edit touching 7 entities | 43 | 0 / 0 / 0 | 41 / 3 / 0 | 0 / 0 / 0 | yes / yes / yes |

**Execution time, seconds (median)** (full / file-level / DAKC)

| Scenario | Time |
|---|---|
| S0 no change | 0.093 / 0.021 / 0.014 |
| S1 one source modified | 0.093 / 0.025 / 0.023 |
| S2 five sources modified | 0.098 / 0.040 / 0.036 |
| S3 one source deleted | 0.098 / 0.024 / 0.023 |
| S4 one source renamed | 0.096 / 0.026 / 0.022 |
| S5 one claim modified | 0.094 / 0.035 / 0.024 |
| S6 new source → popular entity | 0.099 / 0.025 / 0.024 |
| S7 contradicting source | 0.094 / 0.018 / 0.018 |
| S8 edit touching 7 entities | 0.097 / 0.028 / 0.029 |

**PRD comparison table, scenario S1 (one source modified), 100 documents**

| Metric | Full rebuild | File-level invalidation | Incremental (DAKC) |
|---|---|---|---|
| Number of processed sources | 100 | 1 | 1 |
| Number of regenerated claims | 1,494 | 10 | 3 |
| Number of regenerated pages | 53 | 11 | 2 |
| LLM calls | 663 | 17 | 5 |
| Input tokens (estimated) | 72,151 | 10,715 | 1,654 |
| Output tokens (estimated) | 42,909 | 11,232 | 1,855 |
| Execution time (s, median) | 0.093 | 0.025 | 0.023 |
| Unaffected artifacts modified | 0 | 0 | 0 |
| Incorrectly retained stale artifacts | 0 | 0 | 0 |

### 1,000 documents

Initial full build: 1.277 s, 6,391 LLM calls; 5,868 passages, 10,107 claims, 503 entities, 4,296 relationships, 503 pages, 20 summaries, 55,078 dependency edges. Timing runs per strategy: 3.

**Cost: LLM calls and estimated tokens** (full / file-level / DAKC)

| Scenario | LLM calls | DAKC saving vs full | DAKC saving vs file-level | Input tokens (est.) | Output tokens (est.) |
|---|---|---|---|---|---|
| S0 no change | 6,391 / 0 / 0 | 100.0% | — | 700,920 / 0 / 0 | 421,028 / 0 / 0 |
| S1 one source modified | 6,391 / 40 / 14 | 99.8% | 65.0% | 700,970 / 72,044 / 32,902 | 421,086 / 78,187 / 36,379 |
| S2 five sources modified | 6,391 / 134 / 31 | 99.5% | 76.9% | 701,008 / 122,040 / 39,477 | 421,141 / 129,568 / 43,454 |
| S3 one source deleted | 6,382 / 36 / 32 | 99.5% | 11.1% | 699,990 / 63,085 / 49,428 | 420,522 / 69,525 / 54,460 |
| S4 one source renamed | 6,391 / 43 / 0 | 100.0% | 100.0% | 700,920 / 67,796 / 0 | 421,028 / 74,211 / 0 |
| S5 one claim modified | 6,391 / 31 / 2 | 100.0% | 93.5% | 700,920 / 61,692 / 268 | 421,028 / 66,500 / 164 |
| S6 new source → popular entity | 6,394 / 12 / 5 | 99.9% | 58.3% | 701,197 / 32,854 / 19,421 | 421,167 / 36,170 / 21,229 |
| S7 contradicting source | 6,392 / 3 / 3 | 100.0% | 0.0% | 701,005 / 3,748 / 3,748 | 421,058 / 4,126 / 4,126 |
| S8 edit touching 7 entities | 6,391 / 45 / 23 | 99.6% | 48.9% | 701,033 / 72,565 / 23,544 | 421,166 / 78,571 / 25,892 |

**Work performed** (full / file-level / DAKC)

| Scenario | Sources processed | Passages re-extracted | Claims regenerated | Pages regenerated (LLM) | Pages re-rendered (no LLM, DAKC) | Summaries regenerated |
|---|---|---|---|---|---|---|
| S0 no change | 1000 / 0 / 0 | 5868 / 0 / 0 | 14782 / 0 / 0 | 503 / 0 / 0 | 0 | 20 / 0 / 0 |
| S1 one source modified | 1000 / 1 / 1 | 5868 / 7 / 1 | 14783 / 18 / 3 | 503 / 26 / 7 | 2 | 20 / 7 / 6 |
| S2 five sources modified | 1000 / 5 / 5 | 5868 / 34 / 5 | 14787 / 88 / 15 | 503 / 85 / 16 | 13 | 20 / 15 / 10 |
| S3 one source deleted | 999 / 0 / 0 | 5859 / 0 / 0 | 14763 / 0 / 0 | 503 / 22 / 19 | 3 | 20 / 14 / 13 |
| S4 one source renamed | 1000 / 1 / 0 | 5868 / 7 / 0 | 14782 / 17 / 0 | 503 / 23 / 0 | 23 | 20 / 13 / 0 |
| S5 one claim modified | 1000 / 1 / 1 | 5868 / 7 / 1 | 14782 / 17 / 1 | 503 / 23 / 1 | 0 | 20 / 1 / 0 |
| S6 new source → popular entity | 1001 / 1 / 1 | 5871 / 3 / 3 | 14791 / 9 / 9 | 503 / 5 / 1 | 4 | 20 / 4 / 1 |
| S7 contradicting source | 1001 / 1 / 1 | 5869 / 1 / 1 | 14783 / 1 / 1 | 503 / 1 / 1 | 0 | 20 / 1 / 1 |
| S8 edit touching 7 entities | 1000 / 1 / 1 | 5868 / 7 / 1 | 14786 / 21 / 7 | 503 / 31 / 15 | 0 | 20 / 7 / 7 |

**Correctness and precision against the oracle** (full / file-level / DAKC)

| Scenario | Artifacts that truly changed (Δ) | Stale artifacts retained | Unnecessary regenerations | Unaffected artifacts modified | Equal to full rebuild |
|---|---|---|---|---|---|
| S0 no change | 0 | 0 / 0 / 0 | 523 / 0 / 0 | 0 / 0 / 0 | yes / yes / yes |
| S1 one source modified | 34 | 0 / 0 / 0 | 508 / 18 / 0 | 0 / 0 / 0 | yes / yes / yes |
| S2 five sources modified | 102 | 0 / 0 / 0 | 484 / 61 / 0 | 0 / 0 / 0 | yes / yes / yes |
| S3 one source deleted | 91 | 0 / 0 / 0 | 488 / 1 / 0 | 0 / 0 / 0 | yes / yes / yes |
| S4 one source renamed | 56 | 0 / 0 / 0 | 500 / 13 / 0 | 0 / 0 / 0 | yes / yes / yes |
| S5 one claim modified | 9 | 0 / 0 / 0 | 522 / 23 / 0 | 0 / 0 / 0 | yes / yes / yes |
| S6 new source → popular entity | 19 | 0 / 0 / 0 | 517 / 3 / 0 | 0 / 0 / 0 | yes / yes / yes |
| S7 contradicting source | 6 | 0 / 0 / 0 | 521 / 0 / 0 | 0 / 0 / 0 | yes / yes / yes |
| S8 edit touching 7 entities | 56 | 0 / 0 / 0 | 501 / 16 / 0 | 0 / 0 / 0 | yes / yes / yes |

**Execution time, seconds (median)** (full / file-level / DAKC)

| Scenario | Time |
|---|---|
| S0 no change | 0.915 / 0.128 / 0.129 |
| S1 one source modified | 0.956 / 0.212 / 0.178 |
| S2 five sources modified | 0.953 / 0.283 / 0.237 |
| S3 one source deleted | 0.946 / 0.225 / 0.219 |
| S4 one source renamed | 0.918 / 0.199 / 0.187 |
| S5 one claim modified | 0.901 / 0.198 / 0.134 |
| S6 new source → popular entity | 0.920 / 0.175 / 0.175 |
| S7 contradicting source | 0.908 / 0.140 / 0.141 |
| S8 edit touching 7 entities | 0.942 / 0.209 / 0.169 |

**PRD comparison table, scenario S1 (one source modified), 1,000 documents**

| Metric | Full rebuild | File-level invalidation | Incremental (DAKC) |
|---|---|---|---|
| Number of processed sources | 1,000 | 1 | 1 |
| Number of regenerated claims | 14,783 | 18 | 3 |
| Number of regenerated pages | 503 | 26 | 7 |
| LLM calls | 6,391 | 40 | 14 |
| Input tokens (estimated) | 700,970 | 72,044 | 32,902 |
| Output tokens (estimated) | 421,086 | 78,187 | 36,379 |
| Execution time (s, median) | 0.956 | 0.212 | 0.178 |
| Unaffected artifacts modified | 0 | 0 | 0 |
| Incorrectly retained stale artifacts | 0 | 0 | 0 |

### 5,000 documents

Initial full build: 6.456 s, 33,118 LLM calls; 30,517 passages, 48,933 claims, 2,501 entities, 22,910 relationships, 2,501 pages, 100 summaries, 279,406 dependency edges. Timing runs per strategy: 1.

**Cost: LLM calls and estimated tokens** (full / file-level / DAKC)

| Scenario | LLM calls | DAKC saving vs full | DAKC saving vs file-level | Input tokens (est.) | Output tokens (est.) |
|---|---|---|---|---|---|
| S0 no change | 33,118 / 0 / 0 | 100.0% | — | 3,579,640 / 0 / 0 | 2,121,526 / 0 / 0 |
| S1 one source modified | 33,118 / 60 / 11 | 100.0% | 81.7% | 3,579,639 / 255,954 / 77,802 | 2,121,532 / 279,922 / 86,382 |
| S2 five sources modified | 33,118 / 146 / 32 | 99.9% | 78.1% | 3,579,616 / 388,938 / 175,160 | 2,121,508 / 423,140 / 193,422 |
| S3 one source deleted | 33,111 / 43 / 24 | 99.9% | 44.2% | 3,578,947 / 189,601 / 86,535 | 2,121,150 / 209,791 / 96,350 |
| S4 one source renamed | 33,118 / 80 / 0 | 100.0% | 100.0% | 3,579,640 / 251,736 / 0 | 2,121,526 / 277,164 / 0 |
| S5 one claim modified | 33,118 / 50 / 3 | 100.0% | 94.0% | 3,579,650 / 233,915 / 2,341 | 2,121,538 / 254,416 / 2,916 |
| S6 new source → popular entity | 33,121 / 13 / 7 | 100.0% | 46.2% | 3,579,940 / 103,043 / 74,917 | 2,121,690 / 113,353 / 82,663 |
| S7 contradicting source | 33,119 / 3 / 3 | 100.0% | 0.0% | 3,579,725 / 71,713 / 71,713 | 2,121,556 / 78,865 / 78,865 |
| S8 edit touching 7 entities | 33,118 / 73 / 22 | 99.9% | 69.9% | 3,579,804 / 275,721 / 34,618 | 2,121,728 / 301,633 / 38,559 |

**Work performed** (full / file-level / DAKC)

| Scenario | Sources processed | Passages re-extracted | Claims regenerated | Pages regenerated (LLM) | Pages re-rendered (no LLM, DAKC) | Summaries regenerated |
|---|---|---|---|---|---|---|
| S0 no change | 5000 / 0 / 0 | 30517 / 0 / 0 | 76361 / 0 / 0 | 2501 / 0 / 0 | 0 | 100 / 0 / 0 |
| S1 one source modified | 5000 / 1 / 1 | 30517 / 10 / 1 | 76361 / 27 / 3 | 2501 / 43 / 5 | 3 | 100 / 7 / 5 |
| S2 five sources modified | 5000 / 5 / 5 | 30517 / 35 / 5 | 76363 / 84 / 15 | 2501 / 90 / 15 | 9 | 100 / 21 / 12 |
| S3 one source deleted | 4999 / 0 / 0 | 30510 / 0 / 0 | 76344 / 0 / 0 | 2501 / 22 / 11 | 11 | 100 / 21 / 13 |
| S4 one source renamed | 5000 / 1 / 0 | 30517 / 10 / 0 | 76361 / 27 / 0 | 2501 / 39 / 0 | 39 | 100 / 31 / 0 |
| S5 one claim modified | 5000 / 1 / 1 | 30517 / 10 / 1 | 76361 / 27 / 1 | 2501 / 39 / 1 | 0 | 100 / 1 / 1 |
| S6 new source → popular entity | 5001 / 1 / 1 | 30520 / 3 / 3 | 76370 / 9 / 9 | 2501 / 5 / 2 | 3 | 100 / 5 / 2 |
| S7 contradicting source | 5001 / 1 / 1 | 30518 / 1 / 1 | 76362 / 1 / 1 | 2501 / 1 / 1 | 0 | 100 / 1 / 1 |
| S8 edit touching 7 entities | 5000 / 1 / 1 | 30517 / 10 / 1 | 76366 / 32 / 7 | 2501 / 50 / 11 | 3 | 100 / 13 / 10 |

**Correctness and precision against the oracle** (full / file-level / DAKC)

| Scenario | Artifacts that truly changed (Δ) | Stale artifacts retained | Unnecessary regenerations | Unaffected artifacts modified | Equal to full rebuild |
|---|---|---|---|---|---|
| S0 no change | 0 | 0 / 0 / 0 | 2601 / 0 / 0 | 0 / 0 / 0 | yes / yes / yes |
| S1 one source modified | 30 | 0 / 0 / 0 | 2588 / 37 / 0 | 0 / 0 / 0 | yes / yes / yes |
| S2 five sources modified | 103 | 0 / 0 / 0 | 2565 / 75 / 0 | 0 / 0 / 0 | yes / yes / yes |
| S3 one source deleted | 77 | 0 / 0 / 0 | 2566 / 8 / 0 | 0 / 0 / 0 | yes / yes / yes |
| S4 one source renamed | 88 | 0 / 0 / 0 | 2562 / 31 / 0 | 0 / 0 / 0 | yes / yes / yes |
| S5 one claim modified | 9 | 0 / 0 / 0 | 2599 / 38 / 0 | 0 / 0 / 0 | yes / yes / yes |
| S6 new source → popular entity | 22 | 0 / 0 / 0 | 2594 / 3 / 0 | 0 / 0 / 0 | yes / yes / yes |
| S7 contradicting source | 6 | 0 / 0 / 0 | 2599 / 0 / 0 | 0 / 0 / 0 | yes / yes / yes |
| S8 edit touching 7 entities | 52 | 0 / 0 / 0 | 2577 / 39 / 0 | 0 / 0 / 0 | yes / yes / yes |

**Execution time, seconds (median)** (full / file-level / DAKC)

| Scenario | Time |
|---|---|
| S0 no change | 5.349 / 0.659 / 0.650 |
| S1 one source modified | 5.277 / 1.047 / 0.826 |
| S2 five sources modified | 5.369 / 1.287 / 1.072 |
| S3 one source deleted | 5.272 / 1.026 / 1.004 |
| S4 one source renamed | 5.318 / 0.956 / 0.937 |
| S5 one claim modified | 5.382 / 0.976 / 0.707 |
| S6 new source → popular entity | 5.531 / 0.990 / 0.950 |
| S7 contradicting source | 5.661 / 0.830 / 0.896 |
| S8 edit touching 7 entities | 5.698 / 1.045 / 0.838 |

**PRD comparison table, scenario S1 (one source modified), 5,000 documents**

| Metric | Full rebuild | File-level invalidation | Incremental (DAKC) |
|---|---|---|---|
| Number of processed sources | 5,000 | 1 | 1 |
| Number of regenerated claims | 76,361 | 27 | 3 |
| Number of regenerated pages | 2,501 | 43 | 5 |
| LLM calls | 33,118 | 60 | 11 |
| Input tokens (estimated) | 3,579,639 | 255,954 | 77,802 |
| Output tokens (estimated) | 2,121,532 | 279,922 | 86,382 |
| Execution time (s, median) | 5.277 | 1.047 | 0.826 |
| Unaffected artifacts modified | 0 | 0 | 0 |
| Incorrectly retained stale artifacts | 0 | 0 | 0 |

### Reading these results

- **Unaffected artifacts modified is 0 for every strategy, by construction.** A new version is written only when an artifact's state hash changes, and the mock providers are deterministic, so a needless regeneration reproduces identical bytes. The column that tells the strategies apart is **unnecessary regenerations**: work whose result was identical to the previous build. With a real LLM, each of those would also rewrite an unaffected page with different wording.
- **Stale artifacts retained is 0 for all three strategies.** Inside this engine even the file-level strategy is correct, because it retracts claims through the same support sets. The audited tools that use file-level invalidation additionally *merge* LLM output, which keeps retracted facts (research/COMPETITIVE_ANALYSIS.md). That failure mode is not reproduced here and these numbers do not measure it.
- **Savings depend on corpus structure.** Hub entities (Zipf-distributed mentions) make some single edits touch many pages. Scenario S8 is the adversarial case where one paragraph mentions seven entities.
- Token counts are **estimates** (mock provider, ceil(chars/4)). No real LLM was called. See *Real LLM providers* above.

## Real-LLM experiment (llm-gpt-4o-mini)

_Generated from `benchmarks/results/llm-gpt-4o-mini.json` by `python -m benchmarks.report`._

- Model: `custom: hosted-agent:gpt-4o-mini:t0.0` (temperature 0), date 2026-11-22T06:23:43Z. Token counts are **reported by the provider**, not estimated.
- Harness: `benchmarks/llm_experiment.py`. One model, small corpora: indicative, not a general result about LLMs.

### A. Does editing one sentence disturb the other facts of the paragraph?

Each paragraph (3+ sentences) is extracted, extracted again unchanged (*repeat stability*), then one fact is edited (an entity in one sentence replaced by an unseen one) and the new version is processed two ways. **passage**: the whole paragraph is re-extracted (DAKC's original behaviour). **sentence**: claims of unchanged sentences are reused verbatim and only changed or context-dependent sentences are sent to the model (current behaviour), also re-extracting any sentence that may refer back to a changed one (pronouns, "the X"). **sentence-evidence-only**: the same, but trusting only the model's declared sentence dependencies instead of that pattern rule. *Unchanged-fact reproduction*: claims of untouched sentences kept with identical ids, which is what lets DAKC skip their pages. *Stale retention*: old claims still stating the replaced detail (should be 0). *New-fact capture*: paragraphs whose output states the edited fact. Paragraph sets: synthetic sentences; code-heavy technical notes (`research/audits`); explanatory prose (this repository's design docs); a coreference set in which the edited sentence is the antecedent of a pronoun in the next sentence.

| Paragraph set | Paragraphs (failed) | Repeat stability | Mode | Unchanged-fact reproduction ↑ | Stale retention ↓ | New-fact capture ↑ | Output tokens |
|---|---|---|---|---|---|---|---|
| coreference | 40 (0) | 89.2% | passage | 93.4% | 0.0% (0 of 50) | 100.0% | 3,345 |
|  | |  | sentence | 96.7% | 0.0% (0 of 50) | 100.0% | 1,983 |
|  | |  | sentence-evidence-only | 100.0% | 10.0% (5 of 50) | 100.0% | 1,278 |
| prose | 40 (0) | 57.7% | passage | 51.8% | 0.0% (0 of 31) | 77.5% | 4,946 |
|  | |  | sentence | 87.6% | 0.0% (0 of 31) | 77.5% | 2,945 |
|  | |  | sentence-evidence-only | 100.0% | 0.0% (0 of 31) | 85.0% | 2,178 |
| synthetic | 38 (0) | 88.1% | passage | 100.0% | 0.0% (0 of 35) | 100.0% | 4,026 |
|  | |  | sentence | 100.0% | 0.0% (0 of 35) | 100.0% | 1,221 |
|  | |  | sentence-evidence-only | 100.0% | 0.0% (0 of 35) | 100.0% | 1,221 |
| technical | 35 (0) | 59.2% | passage | 50.6% | 0.0% (0 of 24) | 77.1% | 4,662 |
|  | |  | sentence | 77.6% | 0.0% (0 of 24) | 80.0% | 2,719 |
|  | |  | sentence-evidence-only | 96.5% | 0.0% (0 of 24) | 82.9% | 1,890 |

In `sentence` mode, 43.4% of all sentences were sent to the model; the rest of each paragraph was reused.

In `sentence-evidence-only` mode, 28.9% of all sentences were sent to the model; the rest of each paragraph was reused.

Stale claims observed (old detail kept after the edit):

- `sentence-evidence-only`: edited *Atlasbridge is the main queue behind the Billing service.* → kept *Ulosql authenticates against Morogrid.*
- `sentence-evidence-only`: edited *Atlasbridge is the main database behind the Identity service.* → kept *Ulosql streams data to Morogrid.*
- `sentence-evidence-only`: edited *Atlasbridge is the main queue behind the Search service.* → kept *Draildock replicates to Feniovault.*
- `sentence-evidence-only`: edited *Atlasbridge is the main database behind the Billing service.* → kept *Ulosql depends on Draildock.*
- `sentence-evidence-only`: edited *Atlasbridge is the main scheduler behind the Billing service.* → kept *Halecast authenticates against Nexiodyn.*

### B. End-to-end incremental compilation, 30 synthetic documents

Full build with the real model: 232 LLM calls, 176,260 input / 22,832 output tokens, 69.97 s. Each scenario then starts from a copy of that store.

| Scenario | Run | LLM calls | Tokens in / out | Claims new / retracted | Pages regenerated | Re-rendered (no LLM) | Seconds | Invariants |
|---|---|---|---|---|---|---|---|---|
| S0 no change | file | 0 | 0 / 0 | 0 / 0 | 0 | 0 | 0.04 | pass |
| S0 no change | dakc-passage | 0 | 0 / 0 | 0 / 0 | 0 | 0 | 0.02 | pass |
| S0 no change | dakc | 0 | 0 / 0 | 0 / 0 | 0 | 0 | 0.02 | pass |
| S1 one source modified | file | 27 | 23,329 / 5,810 | 2 / 1 | 18 | 0 | 25.07 | pass |
| S1 one source modified | dakc-passage | 6 | 8,168 / 1,687 | 2 / 1 | 3 | 2 | 19.64 | pass |
| S1 one source modified | dakc | 6 | 8,165 / 1,802 | 2 / 1 | 3 | 2 | 28.9 | pass |
| S3 one source deleted | file | 19 | 17,003 / 4,900 | 0 / 12 | 17 | 0 | 17.51 | pass |
| S3 one source deleted | dakc-passage | 16 | 15,155 / 4,911 | 0 / 12 | 14 | 3 | 15.94 | pass |
| S3 one source deleted | dakc | 16 | 15,119 / 4,458 | 0 / 12 | 14 | 3 | 20.83 | pass |
| S4 one source renamed | file | 25 | 22,191 / 5,318 | 0 / 0 | 16 | 0 | 18.85 | pass |
| S4 one source renamed | dakc-passage | 0 | 0 / 0 | 0 / 0 | 0 | 17 | 0.03 | pass |
| S4 one source renamed | dakc | 0 | 0 / 0 | 0 / 0 | 0 | 17 | 0.02 | pass |
| S5 one claim modified | file | 25 | 22,162 / 5,225 | 1 / 1 | 16 | 0 | 16.68 | pass |
| S5 one claim modified | dakc-passage | 5 | 6,991 / 1,128 | 1 / 1 | 2 | 0 | 16.19 | pass |
| S5 one claim modified | dakc | 4 | 6,456 / 1,082 | 1 / 1 | 1 | 0 | 13.62 | pass |
| S8 edit touching 7 entities | file | 30 | 25,157 / 6,047 | 7 / 2 | 21 | 0 | 17.7 | pass |
| S8 edit touching 7 entities | dakc-passage | 15 | 14,231 / 3,964 | 7 / 2 | 12 | 2 | 18.47 | pass |
| S8 edit touching 7 entities | dakc | 15 | 14,300 / 3,959 | 7 / 2 | 12 | 2 | 18.16 | pass |

**Did the edited value actually change?** Scenario S5 edits `has a default port of: 36248 -> 36249`.

| Run | Old value still claimed by the edited document ↓ | New value present ↑ |
|---|---|---|
| file | no | yes |
| dakc-passage | no | yes |
| dakc | no | yes |
