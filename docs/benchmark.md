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
key or GPU. The same harness accepts `--provider openai-compatible` (OpenAI,
Ollama, vLLM, LM Studio) or `--provider anthropic`. With non-deterministic
generators, content-hash equality with `B` is not expected; only structural
equivalence (same artifact ids, same dependency edges, same claim support) is
checked, and semantic equivalence is **not** claimed.

## Results

_Populated by the benchmark run. No numbers are written here by hand._
