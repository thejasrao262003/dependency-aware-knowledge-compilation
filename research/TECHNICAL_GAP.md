# Technical gap report and research gate

## 1. Does an existing project already implement essentially the same engine?

**No.** Twelve projects were audited at the code level
(see [COMPETITIVE_ANALYSIS.md](COMPETITIVE_ANALYSIS.md)). Three come closest,
each in a different way:

| Closest project | What it has | Why it is not the same engine |
|---|---|---|
| **atomicstrata/llm-wiki-compiler** | Hashed source→page ownership, stale/orphan flags, scoped recompiles, an extraction cache keyed by source hash + model + prompt. | Granularity stops at file and page. There are no claim or entity artifacts and no page→summary edges, and nothing stops propagation (no early cutoff). Any deletion disables reuse for the whole run. Seed pages call the LLM on every compile. |
| **HKUDS/LightRAG** | Chunk-level provenance on graph entities and relations. Deletion rebuilds affected objects from cached extractions. A durable purge journal. | Deletion only: the maintainer's documented update path is "delete, then re-insert". No compiled pages or summaries, so no multi-layer invalidation. No plan, dry-run or equivalence check. |
| **trickle-labs/riverbank** | Documentation describes our design almost exactly: fragment hashes, an `artifact_deps` table, stale rendered pages. Fragment-hash skipping works. | The dependency edges are written with the wrong argument order and errors are swallowed, so invalidation never matches. Old facts are not retracted. `mark_pages_stale` is never called. No deletion handling. Requires Postgres plus two extensions. |

The other projects detect change per file (or not at all), record at most one
hop of `source → page` provenance, and handle an edited source by
re-ingesting the whole document through an *additive* LLM merge. As a result
they retain facts the source no longer states (llm_wiki #837, OpenKB/okforge
`integrate` rewrites, WeKnora's "keep slugs the new extraction dropped").

## 2. What exact architectural gap remains?

Five pieces, none of which exists together, and most of which exist nowhere
in this domain:

1. **Sub-document change detection that reaches the claim layer.** Passages
   are hashed, claims are content-addressed, and the extraction cache is keyed
   by passage. An edit to one sentence costs one extraction call for one
   paragraph, and unchanged sentences in that paragraph keep their claim ids.
   Riverbank has fragment hashing but no working claim layer. Nobody else goes
   below the file.

2. **A multi-layer, explicit dependency graph** (source → passage → claim →
   entity/relationship → page → summary → index). Its edges are derived from
   artifact content, stored temporally and verified. Existing tools record at
   most one layer.

3. **Signature-based rebuild decisions with early cutoff**, plus the
   regenerate-vs-rerender split. A page is regenerated (LLM) only if its claims
   changed, re-rendered (code) if only provenance changed, and left untouched
   otherwise. Summaries depend on page *leads*, not whole pages. No audited
   tool has early cutoff at any layer.

4. **Support-counted retraction.** Deleting or editing a source retracts
   exactly the claims left with no support, and nothing else. Only LightRAG
   does this (for graph objects, on deletion only).

5. **Correctness discipline.** A full rebuild serves as an oracle,
   deterministic providers make full vs incremental *byte-comparable*, and a
   benchmark measures stale artifacts and unnecessary regeneration rather than
   only speed. No audited project tests incremental against full.

## 3. Is the gap technically meaningful?

Yes, for correctness as well as cost:

- **Correctness.** Additive LLM merges keep retracted facts. That produces
  stale knowledge presented as current, the PRD's goal 5. It is reported in
  the wild (llm_wiki #837) and visible in the code of four of the seven
  primary projects.
- **Cost.** File/page granularity makes cost proportional to the *size of
  what a file touches*, not the size of the change. Hub pages are rewritten on
  every contribution (WeKnora #4016), and planning prompts grow with the wiki
  (OpenKB #203, #226; llm-wiki-compiler #267). Users ask for the exact
  acceptance case "2 dependent pages regenerated, 500 untouched" (WeKnora
  #3191).

### What is *not* novel

The techniques are established; we reuse them rather than claim them:

- verifying traces and early cutoff (Shake, Bazel, Salsa);
- content addressing (Git, Nix);
- support counting (incremental view maintenance);
- temporal tables (SQL:2011).

The contribution is applying them correctly to LLM knowledge compilation, at
claim granularity, with a test oracle. Riverbank shows the idea is easy to
describe and easy to get wrong.

### Known risk to the claim-level advantage

Claim-level early cutoff depends on the extractor reproducing the same
normalized text for unchanged facts when a paragraph is re-extracted. The
deterministic and heuristic extractors do this by construction. An LLM may
paraphrase. Mitigations:

- re-extract only *changed* passages (unchanged ones never reach the LLM);
- pass the previous claims of the edited passage as anchors in the prompt
  (`src/dakc/providers/llm.py`, `LLMExtractor`).

**This mitigation is implemented but not measured in this repository**,
because no LLM endpoint was available during development. The benchmark
reports the deterministic setting only and says so.

## 4. Can we demonstrate an improvement?

Yes, without depending on other projects' code paths. The benchmark
(`docs/benchmark.md`) runs three strategies in one engine on one seeded
corpus:

- `full` (oracle);
- `file`: file-level invalidation, emulating the strategy of the audited
  tools;
- `dakc`.

It reports provider calls, tokens, regenerated pages, unnecessary
regenerations and stale artifacts against the oracle across eight change
scenarios at 100, 1,000 and 5,000 documents.

## Gate decision

**Proceed with implementation.** The gap is real, verified in code rather
than inferred from documentation, and measurable.

### Upstream alternative considered

Contributing claim-level invalidation to llm-wiki-compiler (TypeScript) or
LightRAG (Python) was considered. Both would require a new artifact layer
(claims) and a page tier (LightRAG) or a graph store (llm-wiki-compiler),
which is a redesign rather than a patch. A standalone, library-style engine
with a clear dependency model is the better first step. Its benchmark harness
and the model in `docs/dependency-model.md` can later back an upstream
proposal. Specific upstream bugs found during the audit should be reported
separately:

- Riverbank's `record_artifact_dep` argument order;
- GraphRAG's unused `deleted_inputs`.
