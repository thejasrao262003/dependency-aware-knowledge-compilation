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

All three strategies write hub pages (more than 64 claims) and large topic
summaries (more than 64 pages) in sections, because the output must be
identical. So when `file` or `full` rebuilds a hub, it makes one call per
section, where a real file-level tool would make one larger call. Call counts
therefore overstate the gap between `file` and `dakc` on scenarios that touch a
hub; **input tokens are the fair comparison**.

## Corpus generator

`benchmarks/corpus.py`, fully seeded (`--seed`, default 7), so every run of a
given size produces byte-identical files.

- Sizes: **100, 1,000, 5,000** documents (uniform profile), plus a **20,000**
  document scale run with the *realistic* profile: heavy-tailed document lengths
  (most short, a few very long) and uneven topic sizes.
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
| S9 | Contradiction, then original deleted | Add a document asserting a different value for a claim stated by exactly one document, and delete that document. Only the new value may remain. |

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
- **Concurrency**: a stale build (HEAD moved while it computed) must refuse
  to commit. A second process must be refused, or must queue with `--wait`,
  while readers keep working.
- **Drift**: a different extractor, page writer or summarizer, and each DAKC
  logic version bump, must invalidate exactly its layer and match a full
  rebuild under the new configuration.
- **Reproducibility**: `python -m benchmarks.run --sizes 100 --verify-against
  benchmarks/results/latest.json` re-runs the benchmark and fails unless
  every non-timing metric matches the published file. CI runs it.

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

- Date: 2026-11-22T15:55:19Z · macOS-27.2-arm64-arm-64bit · Apple M5 (10 cores) · Python 3.12.13
- Provider: mock (deterministic; tokens estimated as ceil(chars/4))
- Strategies are run from a copy of the same initial store on the same mutated corpus; the oracle is an independent full build of the mutated corpus.
- Timing is the median of the stated number of runs. Mock providers have ~0 latency, so times measure engine overhead (scanning, hashing, SQLite), not LLM latency; LLM calls and tokens are the cost proxy.

### 100 documents

Initial full build: 0.272 s, 683 LLM calls; 608 passages, 1,085 claims, 53 entities, 341 relationships, 53 pages, 2 summaries, 5,463 dependency edges. Timing runs per strategy: 3.

**Cost: LLM calls and estimated tokens** (full / file-level / DAKC)

| Scenario | LLM calls | DAKC saving vs full | DAKC saving vs file-level | Input tokens (est.) | Output tokens (est.) |
|---|---|---|---|---|---|
| S0 no change | 683 / 0 / 0 | 100.0% | — | 73,225 / 0 / 0 | 41,815 / 0 / 0 |
| S1 one source modified | 683 / 36 / 5 | 99.3% | 86.1% | 73,255 / 11,763 / 1,707 | 41,855 / 10,162 / 546 |
| S2 five sources modified | 686 / 93 / 33 | 95.2% | 64.5% | 73,388 / 23,430 / 11,304 | 41,847 / 20,114 / 9,772 |
| S3 one source deleted | 678 / 29 / 6 | 99.1% | 79.3% | 72,773 / 9,891 / 2,620 | 41,609 / 8,453 / 2,038 |
| S4 one source renamed | 683 / 33 / 0 | 100.0% | 100.0% | 73,225 / 10,984 / 0 | 41,815 / 9,435 / 0 |
| S5 one claim modified | 683 / 32 / 2 | 99.7% | 93.8% | 73,225 / 10,402 / 1,252 | 41,815 / 9,410 / 1,244 |
| S6 new source → popular entity | 686 / 26 / 7 | 99.0% | 73.1% | 73,513 / 8,898 / 1,843 | 41,965 / 7,475 / 578 |
| S7 contradicting source | 684 / 5 / 4 | 99.4% | 20.0% | 73,310 / 2,367 / 1,520 | 41,845 / 1,258 / 372 |
| S8 edit touching 7 entities | 683 / 40 / 15 | 97.8% | 62.5% | 73,369 / 13,215 / 6,298 | 41,984 / 11,577 / 5,149 |
| S9 contradiction, then original deleted | 679 / 35 / 16 | 97.6% | 54.3% | 72,741 / 12,639 / 6,834 | 41,497 / 11,197 / 5,667 |

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
| S9 contradiction, then original deleted | 100 / 1 / 1 | 604 / 1 / 1 | 1480 / 1 / 1 | 53 / 13 / 11 | 2 | 2 / 2 / 2 |

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
| S9 contradiction, then original deleted | 55 | 0 / 0 / 0 | 40 / 0 / 0 | 0 / 0 / 0 | yes / yes / yes |

**Execution time, seconds (median)** (full / file-level / DAKC)

| Scenario | Time |
|---|---|
| S0 no change | 0.259 / 0.037 / 0.035 |
| S1 one source modified | 0.213 / 0.060 / 0.052 |
| S2 five sources modified | 0.270 / 0.102 / 0.090 |
| S3 one source deleted | 0.286 / 0.065 / 0.060 |
| S4 one source renamed | 0.207 / 0.055 / 0.048 |
| S5 one claim modified | 0.212 / 0.053 / 0.048 |
| S6 new source → popular entity | 0.213 / 0.053 / 0.052 |
| S7 contradicting source | 0.201 / 0.036 / 0.035 |
| S8 edit touching 7 entities | 0.208 / 0.057 / 0.054 |
| S9 contradiction, then original deleted | 0.196 / 0.096 / 0.075 |

**PRD comparison table, scenario S1 (one source modified), 100 documents**

| Metric | Full rebuild | File-level invalidation | Incremental (DAKC) |
|---|---|---|---|
| Number of processed sources | 100 | 1 | 1 |
| Number of regenerated claims | 1,494 | 10 | 3 |
| Number of regenerated pages | 53 | 11 | 2 |
| LLM calls | 683 | 36 | 5 |
| Input tokens (estimated) | 73,255 | 11,763 | 1,707 |
| Output tokens (estimated) | 41,855 | 10,162 | 546 |
| Execution time (s, median) | 0.213 | 0.060 | 0.052 |
| Unaffected artifacts modified | 0 | 0 | 0 |
| Incorrectly retained stale artifacts | 0 | 0 | 0 |

### 1,000 documents

Initial full build: 2.733 s, 6,710 LLM calls; 5,868 passages, 10,107 claims, 503 entities, 4,296 relationships, 503 pages, 20 summaries, 55,078 dependency edges. Timing runs per strategy: 3.

**Cost: LLM calls and estimated tokens** (full / file-level / DAKC)

| Scenario | LLM calls | DAKC saving vs full | DAKC saving vs file-level | Input tokens (est.) | Output tokens (est.) |
|---|---|---|---|---|---|
| S0 no change | 6,710 / 0 / 0 | 100.0% | — | 717,825 / 0 / 0 | 412,330 / 0 / 0 |
| S1 one source modified | 6,710 / 228 / 15 | 99.8% | 93.4% | 717,872 / 81,665 / 7,136 | 412,386 / 74,891 / 4,366 |
| S2 five sources modified | 6,710 / 416 / 36 | 99.5% | 91.3% | 717,913 / 136,839 / 14,798 | 412,442 / 122,250 / 7,951 |
| S3 one source deleted | 6,701 / 192 / 36 | 99.5% | 81.2% | 716,898 / 71,084 / 17,493 | 411,827 / 61,871 / 11,680 |
| S4 one source renamed | 6,710 / 205 / 0 | 100.0% | 100.0% | 717,825 / 76,238 / 0 | 412,330 / 66,671 / 0 |
| S5 one claim modified | 6,710 / 193 / 2 | 100.0% | 99.0% | 717,825 / 69,827 / 268 | 412,330 / 66,373 / 164 |
| S6 new source → popular entity | 6,713 / 110 / 5 | 99.9% | 95.5% | 718,101 / 37,828 / 2,344 | 412,469 / 33,419 / 1,489 |
| S7 contradicting source | 6,711 / 8 / 3 | 100.0% | 62.5% | 717,909 / 4,010 / 1,286 | 412,360 / 3,343 / 575 |
| S8 edit touching 7 entities | 6,711 / 231 / 26 | 99.6% | 88.7% | 717,994 / 82,078 / 10,979 | 412,485 / 75,148 / 6,743 |
| S9 contradiction, then original deleted | 6,704 / 186 / 29 | 99.6% | 84.4% | 717,148 / 63,785 / 11,055 | 411,904 / 53,980 / 5,948 |

**Work performed** (full / file-level / DAKC)

| Scenario | Sources processed | Passages re-extracted | Claims regenerated | Pages regenerated (LLM) | Pages re-rendered (no LLM, DAKC) | Summaries regenerated |
|---|---|---|---|---|---|---|
| S0 no change | 1000 / 0 / 0 | 5868 / 0 / 0 | 14782 / 0 / 0 | 503 / 0 / 0 | 0 | 20 / 0 / 0 |
| S1 one source modified | 1000 / 1 / 1 | 5868 / 7 / 1 | 14783 / 18 / 3 | 503 / 26 / 7 | 2 | 20 / 7 / 5 |
| S2 five sources modified | 1000 / 5 / 5 | 5868 / 34 / 5 | 14787 / 88 / 15 | 503 / 85 / 16 | 13 | 20 / 15 / 10 |
| S3 one source deleted | 999 / 0 / 0 | 5859 / 0 / 0 | 14763 / 0 / 0 | 503 / 22 / 19 | 3 | 20 / 14 / 12 |
| S4 one source renamed | 1000 / 1 / 0 | 5868 / 7 / 0 | 14782 / 17 / 0 | 503 / 23 / 0 | 23 | 20 / 13 / 0 |
| S5 one claim modified | 1000 / 1 / 1 | 5868 / 7 / 1 | 14782 / 17 / 1 | 503 / 23 / 1 | 0 | 20 / 1 / 0 |
| S6 new source → popular entity | 1001 / 1 / 1 | 5871 / 3 / 3 | 14791 / 9 / 9 | 503 / 5 / 1 | 4 | 20 / 4 / 1 |
| S7 contradicting source | 1001 / 1 / 1 | 5869 / 1 / 1 | 14783 / 1 / 1 | 503 / 1 / 1 | 0 | 20 / 1 / 1 |
| S8 edit touching 7 entities | 1000 / 1 / 1 | 5868 / 7 / 1 | 14786 / 21 / 7 | 503 / 31 / 15 | 0 | 20 / 7 / 7 |
| S9 contradiction, then original deleted | 1000 / 1 / 1 | 5863 / 1 / 1 | 14766 / 1 / 1 | 503 / 23 / 16 | 7 | 20 / 14 / 10 |

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
| S9 contradiction, then original deleted | 83 | 0 / 0 / 0 | 490 / 4 / 0 | 0 / 0 / 0 | yes / yes / yes |

**Execution time, seconds (median)** (full / file-level / DAKC)

| Scenario | Time |
|---|---|
| S0 no change | 2.293 / 0.305 / 0.282 |
| S1 one source modified | 2.526 / 0.721 / 0.457 |
| S2 five sources modified | 1.925 / 0.565 / 0.660 |
| S3 one source deleted | 2.294 / 0.468 / 0.617 |
| S4 one source renamed | 4.031 / 0.636 / 0.707 |
| S5 one claim modified | 1.469 / 0.291 / 0.249 |
| S6 new source → popular entity | 2.232 / 0.499 / 0.322 |
| S7 contradicting source | 2.007 / 0.204 / 0.167 |
| S8 edit touching 7 entities | 1.819 / 0.411 / 0.222 |
| S9 contradiction, then original deleted | 1.307 / 0.288 / 0.304 |

**PRD comparison table, scenario S1 (one source modified), 1,000 documents**

| Metric | Full rebuild | File-level invalidation | Incremental (DAKC) |
|---|---|---|---|
| Number of processed sources | 1,000 | 1 | 1 |
| Number of regenerated claims | 14,783 | 18 | 3 |
| Number of regenerated pages | 503 | 26 | 7 |
| LLM calls | 6,710 | 228 | 15 |
| Input tokens (estimated) | 717,872 | 81,665 | 7,136 |
| Output tokens (estimated) | 412,386 | 74,891 | 4,366 |
| Execution time (s, median) | 2.526 | 0.721 | 0.457 |
| Unaffected artifacts modified | 0 | 0 | 0 |
| Incorrectly retained stale artifacts | 0 | 0 | 0 |

### 5,000 documents

Initial full build: 14.832 s, 34,767 LLM calls; 30,517 passages, 48,933 claims, 2,501 entities, 22,910 relationships, 2,501 pages, 100 summaries, 279,406 dependency edges. Timing runs per strategy: 1.

**Cost: LLM calls and estimated tokens** (full / file-level / DAKC)

| Scenario | LLM calls | DAKC saving vs full | DAKC saving vs file-level | Input tokens (est.) | Output tokens (est.) |
|---|---|---|---|---|---|
| S0 no change | 34,767 / 0 / 0 | 100.0% | — | 3,663,730 / 0 / 0 | 2,075,862 / 0 / 0 |
| S1 one source modified | 34,767 / 766 / 13 | 100.0% | 98.3% | 3,663,729 / 289,027 / 6,036 | 2,075,867 / 272,551 / 2,893 |
| S2 five sources modified | 34,767 / 1,195 / 38 | 99.9% | 96.8% | 3,663,706 / 439,248 / 16,893 | 2,075,847 / 411,105 / 10,343 |
| S3 one source deleted | 34,759 / 542 / 27 | 99.9% | 95.0% | 3,662,976 / 212,492 / 14,692 | 2,075,472 / 192,086 / 6,780 |
| S4 one source renamed | 34,767 / 746 / 0 | 100.0% | 100.0% | 3,663,730 / 283,334 / 0 | 2,075,862 / 256,385 / 0 |
| S5 one claim modified | 34,767 / 702 / 3 | 100.0% | 99.6% | 3,663,740 / 263,682 / 726 | 2,075,874 / 255,207 / 312 |
| S6 new source → popular entity | 34,770 / 300 / 8 | 100.0% | 97.3% | 3,664,029 / 115,932 / 3,838 | 2,076,025 / 108,487 / 1,843 |
| S7 contradicting source | 34,768 / 196 / 3 | 100.0% | 98.5% | 3,663,815 / 79,867 / 854 | 2,075,891 / 76,419 / 572 |
| S8 edit touching 7 entities | 34,767 / 824 / 22 | 99.9% | 97.3% | 3,663,892 / 310,954 / 11,102 | 2,076,065 / 291,811 / 5,178 |
| S9 contradiction, then original deleted | 34,766 / 217 / 9 | 100.0% | 95.9% | 3,663,617 / 86,242 / 4,410 | 2,075,791 / 79,280 / 3,118 |

**Work performed** (full / file-level / DAKC)

| Scenario | Sources processed | Passages re-extracted | Claims regenerated | Pages regenerated (LLM) | Pages re-rendered (no LLM, DAKC) | Summaries regenerated |
|---|---|---|---|---|---|---|
| S0 no change | 5000 / 0 / 0 | 30517 / 0 / 0 | 76361 / 0 / 0 | 2501 / 0 / 0 | 0 | 100 / 0 / 0 |
| S1 one source modified | 5000 / 1 / 1 | 30517 / 10 / 1 | 76361 / 27 / 3 | 2501 / 43 / 5 | 3 | 100 / 7 / 3 |
| S2 five sources modified | 5000 / 5 / 5 | 30517 / 35 / 5 | 76363 / 84 / 15 | 2501 / 90 / 15 | 9 | 100 / 21 / 9 |
| S3 one source deleted | 4999 / 0 / 0 | 30510 / 0 / 0 | 76344 / 0 / 0 | 2501 / 22 / 11 | 11 | 100 / 21 / 10 |
| S4 one source renamed | 5000 / 1 / 0 | 30517 / 10 / 0 | 76361 / 27 / 0 | 2501 / 39 / 0 | 39 | 100 / 31 / 0 |
| S5 one claim modified | 5000 / 1 / 1 | 30517 / 10 / 1 | 76361 / 27 / 1 | 2501 / 39 / 1 | 0 | 100 / 1 / 0 |
| S6 new source → popular entity | 5001 / 1 / 1 | 30520 / 3 / 3 | 76370 / 9 / 9 | 2501 / 5 / 2 | 3 | 100 / 5 / 0 |
| S7 contradicting source | 5001 / 1 / 1 | 30518 / 1 / 1 | 76362 / 1 / 1 | 2501 / 1 / 1 | 0 | 100 / 1 / 0 |
| S8 edit touching 7 entities | 5000 / 1 / 1 | 30517 / 10 / 1 | 76366 / 32 / 7 | 2501 / 50 / 11 | 3 | 100 / 13 / 9 |
| S9 contradiction, then original deleted | 5000 / 1 / 1 | 30516 / 1 / 1 | 76358 / 1 / 1 | 2501 / 6 / 4 | 2 | 100 / 6 / 3 |

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
| S9 contradiction, then original deleted | 24 | 0 / 0 / 0 | 2592 / 3 / 0 | 0 / 0 / 0 | yes / yes / yes |

**Execution time, seconds (median)** (full / file-level / DAKC)

| Scenario | Time |
|---|---|
| S0 no change | 8.460 / 1.180 / 1.463 |
| S1 one source modified | 8.174 / 1.549 / 1.081 |
| S2 five sources modified | 10.124 / 2.701 / 1.694 |
| S3 one source deleted | 7.934 / 2.181 / 1.518 |
| S4 one source renamed | 8.051 / 1.598 / 1.712 |
| S5 one claim modified | 9.213 / 1.570 / 0.927 |
| S6 new source → popular entity | 8.502 / 2.393 / 1.226 |
| S7 contradicting source | 10.285 / 2.437 / 1.728 |
| S8 edit touching 7 entities | 11.046 / 2.247 / 1.588 |
| S9 contradiction, then original deleted | 9.364 / 3.127 / 1.924 |

**PRD comparison table, scenario S1 (one source modified), 5,000 documents**

| Metric | Full rebuild | File-level invalidation | Incremental (DAKC) |
|---|---|---|---|
| Number of processed sources | 5,000 | 1 | 1 |
| Number of regenerated claims | 76,361 | 27 | 3 |
| Number of regenerated pages | 2,501 | 43 | 5 |
| LLM calls | 34,767 | 766 | 13 |
| Input tokens (estimated) | 3,663,729 | 289,027 | 6,036 |
| Output tokens (estimated) | 2,075,867 | 272,551 | 2,893 |
| Execution time (s, median) | 8.174 | 1.549 | 1.081 |
| Unaffected artifacts modified | 0 | 0 | 0 |
| Incorrectly retained stale artifacts | 0 | 0 | 0 |

### Reading these results

- **Unaffected artifacts modified is 0 for every strategy, by construction.** A new version is written only when an artifact's state hash changes, and the mock providers are deterministic, so a needless regeneration reproduces identical bytes. The column that tells the strategies apart is **unnecessary regenerations**: work whose result was identical to the previous build. With a real LLM, each of those would also rewrite an unaffected page with different wording.
- **Stale artifacts retained is 0 for all three strategies.** Inside this engine even the file-level strategy is correct, because it retracts claims through the same support sets. The audited tools that use file-level invalidation additionally *merge* LLM output, which keeps retracted facts (research/COMPETITIVE_ANALYSIS.md). That failure mode is not reproduced here and these numbers do not measure it.
- **Savings depend on corpus structure.** Hub entities (Zipf-distributed mentions) make some single edits touch many pages. Scenario S8 is the adversarial case where one paragraph mentions seven entities.
- Token counts are **estimates** (mock provider, ceil(chars/4)). No real LLM was called. See *Real LLM providers* above.

## Scale run: realistic document distributions

_Generated by `python -m benchmarks.report` from `benchmarks/results/scale-realistic.json`._

- Date: 2026-11-22T16:04:13Z · macOS-27.2-arm64-arm-64bit · Apple M5 (10 cores) · Python 3.12.13
- Provider: mock (deterministic; tokens estimated as ceil(chars/4))
- Strategies are run from a copy of the same initial store on the same mutated corpus; the oracle is an independent full build of the mutated corpus.
- Timing is the median of the stated number of runs. Mock providers have ~0 latency, so times measure engine overhead (scanning, hashing, SQLite), not LLM latency; LLM calls and tokens are the cost proxy.

### 20,000 documents

Initial full build: 146.909 s, 190,964 LLM calls; 167,317 passages, 329,123 claims, 10,002 entities, 156,920 relationships, 10,002 pages, 227 summaries, 1,906,814 dependency edges. Timing runs per strategy: 1.

**Cost: LLM calls and estimated tokens** (full / file-level / DAKC)

| Scenario | LLM calls | DAKC saving vs full | DAKC saving vs file-level | Input tokens (est.) | Output tokens (est.) |
|---|---|---|---|---|---|
| S0 no change | 190,964 / 0 / 0 | 100.0% | — | 23,034,057 / 0 / 0 | 15,287,777 / 0 / 0 |
| S1 one source modified | 190,964 / 4,273 / 6 | 100.0% | 99.9% | 23,034,010 / 1,690,271 / 5,019 | 15,287,728 / 1,504,032 / 1,904 |
| S3 one source deleted | 190,940 / 5,394 / 81 | 100.0% | 98.5% | 23,031,474 / 2,110,963 / 81,272 | 15,286,220 / 1,906,845 / 27,587 |
| S4 one source renamed | 190,964 / 4,230 / 0 | 100.0% | 100.0% | 23,034,057 / 1,675,496 / 0 | 15,287,777 / 1,486,936 / 0 |
| S5 one claim modified | 190,964 / 4,226 / 2 | 100.0% | 100.0% | 23,034,057 / 1,672,589 / 498 | 15,287,777 / 1,486,801 / 385 |
| S9 contradiction, then original deleted | 190,964 / 1,939 / 15 | 100.0% | 99.2% | 23,033,954 / 821,693 / 13,441 | 15,287,638 / 648,020 / 2,967 |

**Work performed** (full / file-level / DAKC)

| Scenario | Sources processed | Passages re-extracted | Claims regenerated | Pages regenerated (LLM) | Pages re-rendered (no LLM, DAKC) | Summaries regenerated |
|---|---|---|---|---|---|---|
| S0 no change | 20000 / 0 / 0 | 167317 / 0 / 0 | 585501 / 0 / 0 | 10002 / 0 / 0 | 0 | 227 / 0 / 0 |
| S1 one source modified | 20000 / 1 / 1 | 167317 / 7 / 1 | 585500 / 27 / 3 | 10002 / 37 / 3 | 8 | 227 / 1 / 0 |
| S3 one source deleted | 19999 / 0 / 0 | 167293 / 0 / 0 | 585420 / 0 / 0 | 10002 / 74 / 35 | 39 | 227 / 4 / 4 |
| S4 one source renamed | 20000 / 1 / 0 | 167317 / 7 / 0 | 585501 / 28 / 0 | 10002 / 34 / 0 | 34 | 227 / 3 / 0 |
| S5 one claim modified | 20000 / 1 / 1 | 167317 / 7 / 1 | 585501 / 28 / 1 | 10002 / 34 / 1 | 0 | 227 / 1 / 0 |
| S9 contradiction, then original deleted | 20000 / 1 / 1 | 167317 / 1 / 1 | 585496 / 1 / 1 | 10002 / 10 / 7 | 3 | 227 / 3 / 0 |

**Correctness and precision against the oracle** (full / file-level / DAKC)

| Scenario | Artifacts that truly changed (Δ) | Stale artifacts retained | Unnecessary regenerations | Unaffected artifacts modified | Equal to full rebuild |
|---|---|---|---|---|---|
| S0 no change | 0 | 0 / 0 / 0 | 10229 / 0 / 0 | 0 / 0 / 0 | yes / yes / yes |
| S1 one source modified | 26 | 0 / 0 / 0 | 10217 / 26 / 0 | 0 / 0 / 0 | yes / yes / yes |
| S3 one source deleted | 235 | 0 / 0 / 0 | 10151 / 0 / 0 | 0 / 0 / 0 | yes / yes / yes |
| S4 one source renamed | 78 | 0 / 0 / 0 | 10195 / 3 / 0 | 0 / 0 / 0 | yes / yes / yes |
| S5 one claim modified | 12 | 0 / 0 / 0 | 10228 / 34 / 0 | 0 / 0 / 0 | yes / yes / yes |
| S9 contradiction, then original deleted | 34 | 0 / 0 / 0 | 10216 / 0 / 0 | 0 / 0 / 0 | yes / yes / yes |

**Execution time, seconds (median)** (full / file-level / DAKC)

| Scenario | Time |
|---|---|
| S0 no change | 59.928 / 7.235 / 7.305 |
| S1 one source modified | 59.901 / 11.567 / 8.919 |
| S3 one source deleted | 55.648 / 12.715 / 15.366 |
| S4 one source renamed | 82.140 / 16.771 / 15.658 |
| S5 one claim modified | 82.649 / 15.569 / 12.874 |
| S9 contradiction, then original deleted | 81.521 / 13.475 / 13.330 |

**PRD comparison table, scenario S1 (one source modified), 20,000 documents**

| Metric | Full rebuild | File-level invalidation | Incremental (DAKC) |
|---|---|---|---|
| Number of processed sources | 20,000 | 1 | 1 |
| Number of regenerated claims | 585,500 | 27 | 3 |
| Number of regenerated pages | 10,002 | 37 | 3 |
| LLM calls | 190,964 | 4,273 | 6 |
| Input tokens (estimated) | 23,034,010 | 1,690,271 | 5,019 |
| Output tokens (estimated) | 15,287,728 | 1,504,032 | 1,904 |
| Execution time (s, median) | 59.901 | 11.567 | 8.919 |
| Unaffected artifacts modified | 0 | 0 | 0 |
| Incorrectly retained stale artifacts | 0 | 0 | 0 |


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

Full build with the real model: 232 LLM calls, 176,260 input / 22,832 output tokens, 69.97 s. Each scenario then starts from a copy of that store. Provenance of the LLM-written pages: 297 of 412 generated sentences (72.1%) cite at least one claim, with 0 invalid citations; `provenance = "strict"` renders only the cited ones.

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

## Claim equivalence: can paraphrases be matched without false merges?

_Generated from `benchmarks/results/equivalence.json` (`python -m benchmarks.equivalence`)._

262 labelled pairs: 60 paraphrases of the same fact, and hard negatives where the value changed, the subject or object was swapped, the direction was reversed, the relation changed, or the statement was negated. A false merge would silently turn two different facts into one, so precision matters most.

| Matcher | Precision | Recall | False merges |
|---|---|---|---|
| exact (DAKC claim identity) | 100.0% | 0.0% | 0 |
| naive fuzzy (token_sort_ratio ≥ 85) | 22.9% | 31.7% | 64 |
| guarded deterministic (`dakc duplicates`) | 100.0% | 33.3% | 0 |
| LLM judge (`hosted-agent:gpt-4o-mini:t0.0`) | 100.0% | 100.0% | 0 |

Naive fuzzy matching merged: direction reversed 14/22, negated 33/60, object swapped 1/30, subject swapped 6/30, value changed 10/30.

**Policy.** Claim identity stays exact (normalized text), because dependency correctness cannot rest on a similarity score or a model's judgement. The guarded matcher is shipped only as an advisory report (`dakc duplicates`); nothing is merged. The pairs are templated, so these numbers are an optimistic bound for real text, particularly for the LLM judge.
