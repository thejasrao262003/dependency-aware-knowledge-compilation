# Competitive analysis

Audit date: 2026-11-21. Every project was shallow-cloned and read at the
commit listed below. Conclusions come from the implementation, not the README.
Nothing was built or executed. Per-repository notes with file:line citations
are in [`audits/`](audits/).

**Method.** The seven repositories named in the PRD, plus five closely related
projects found while searching, were each audited against the PRD's ten
research questions. The most load-bearing claims were then re-checked
directly against the source (marked ✔ below), including every claim that a
feature *is* implemented in a way that would overlap with DAKC.

## Projects

| Project | Commit | Stack | What it is |
|---|---|---|---|
| [nashsu/llm_wiki](https://github.com/nashsu/llm_wiki) | `e08cfce` | Tauri, TS + Rust | Desktop LLM wiki; ingest queue, LLM page merge |
| [VectifyAI/OpenKB](https://github.com/VectifyAI/OpenKB) | `ff54396` | Python | CLI knowledge base; per-document LLM compile |
| [Tencent/WeKnora](https://github.com/Tencent/WeKnora) | `1ef55d4` | Go + Postgres | RAG platform with a "self-maintaining wiki" |
| [microsoft/llmwiki](https://github.com/microsoft/llmwiki) | `32da49b` | VS Code ext., TS | Additive per-source wiki builder |
| [inkeep/open-knowledge](https://github.com/inkeep/open-knowledge) | `228ac2b` | TS, CRDT server | Markdown IDE + MCP tools; LLM work is done by the host agent |
| [Raymonda/LLM-WIKI](https://github.com/Raymonda/LLM-WIKI) | `7388b6e` | Java/Spring, MySQL | Multi-stage ingest with lint |
| [okforge/okforge](https://github.com/okforge/okforge) | `3031b7b` | Python | Hard fork of OpenKB emitting OKF bundles |
| *Additional prior art found during research* | | | |
| [atomicstrata/llm-wiki-compiler](https://github.com/atomicstrata/llm-wiki-compiler) | `b8e28c6` | TS | "The knowledge compiler"; the most complete incremental LLM wiki found |
| [HKUDS/LightRAG](https://github.com/HKUDS/LightRAG) | `2537e81` | Python | Graph RAG with provenance-aware deletion |
| [microsoft/graphrag](https://github.com/microsoft/graphrag) | `34b2b67` | Python | Graph RAG with an `update` mode |
| [trickle-labs/riverbank](https://github.com/trickle-labs/riverbank) | `38559dd` | Python + Postgres | "Knowledge compiler"; documents a design very close to DAKC |
| [PrefectHQ/colin](https://github.com/PrefectHQ/colin) | `827969f` | Python | dbt-style compiler for LLM-templated documents |

## Comparison matrix (PRD format)

"Existing implementation" names the **strongest** implementation found for
each capability.

| Capability | Existing implementation | Limitations | Our proposed improvement |
|---|---|---|---|
| **Source change detection** | Whole-file SHA-256 (OpenKB/okforge `state.py:120-127` ✔, llm-wiki-compiler `hasher.ts:33-143`, Raymonda); MD5 (WeKnora, LightRAG doc id); MD5+size+mtime watcher (llm_wiki `file_sync.rs:771-808`); **per-fragment xxh3** (Riverbank `pipeline/__init__.py:803-845` ✔). GraphRAG uses **titles only** (`incremental_index.py:49-56` ✔); microsoft/llmwiki has **no hashing** ✔. | File granularity everywhere except Riverbank. Edits in place are invisible to GraphRAG and microsoft/llmwiki (`extension.ts:117-120` ✔ only refreshes a tree view). Prompt, model or config changes invalidate nothing in most tools. | BLAKE3 per file, then per **passage** (paragraph, list item or PDF block). Provider fingerprints (model, prompt hash, version) are part of every signature. Rename detection by hash pairing keeps source identity. |
| **Dependency tracking** | One-hop `sources:` frontmatter (llm_wiki, OpenKB, okforge, microsoft/llmwiki), found by scanning every page; a `wiki_page_source` table (Raymonda); `source_refs` + GIN index (WeKnora); a source→concept ownership map with reverse index (llm-wiki-compiler `deps.ts:39-157` ✔); chunk ids on entities and relations (LightRAG `source_id`). | No project records dependencies *between derived artifacts* (page→page, summary→page, index→page). Riverbank's `artifact_deps` is written with the wrong argument order and errors are swallowed (`pipeline/__init__.py:1543,1606,1618` vs `catalog/graph.py:331` ✔), so it never matches. | An explicit, temporal edge table across six layers (source → passage → claim → entity/relationship → page → summary → index). Edges are derived from artifact content and verified against it (`dakc verify`). |
| **Claim-level lineage** | LLM-written citations such as `^[file.md:L-R]` (llm-wiki-compiler), `(p. N)` (okforge), `（来源：name）` bullets parsed by regex (Raymonda); chunk-id sets per page (WeKnora). OpenKB issue #222 proposes claim objects (PR #223 closed unmerged ✔). | Citations are annotations. Nothing is *invalidated* through them, and none are content-addressed. | Claims are first-class artifacts with content-addressed ids and **support sets** (the passages that yield them). Pages depend on exact claim ids and content hashes. |
| **Incremental recompilation** | Changed source → its pages regenerated, with unchanged co-owners' extractions reused (llm-wiki-compiler `extraction-merge.ts:27-35`). Per-document re-run elsewhere. Append-only delta (GraphRAG). | The unit is always the whole file and whole page. A one-sentence edit regenerates every page the file touches. Unchanged co-owners' full text is re-sent. Any deletion disables extraction reuse for the whole run (llm-wiki-compiler `extraction-snapshot.ts:76` ✔). | Re-extract only passages whose text is new to the content-addressed cache. Unchanged sentences in an edited paragraph reproduce identical claim ids. |
| **Source deletion handling** | Strongest: LightRAG classifies each affected entity/relation as delete-outright or rebuild-from-surviving-chunks and rebuilds from cached extractions with a durable purge journal (`lightrag.py:7712-7923`, `operate.py:1169` ✔). Also llm-wiki-compiler (orphan flag + rebuild shared pages), WeKnora (tombstone + LLM rewrite of shared pages). | LLM wikis strip the source from `sources:` but leave its *facts* in shared pages (llm_wiki, OpenKB, okforge, microsoft/llmwiki). Raymonda forbids deletion (`SourceService.java:481-483` ✔). GraphRAG computes deleted docs and never uses them ✔. Riverbank and OpenKB/okforge watchers ignore deletes. | Support counting: a claim is retracted exactly when its support set becomes empty. Pages whose claims all survive are only *re-rendered* (citations), with no LLM call. |
| **Stale artifact detection** | Per-page fresh/stale/orphaned/unverified from source hashes (llm-wiki-compiler `freshness/index.ts:45-60`). Timestamp SQL (Raymonda). LLM lint over page text (llm_wiki, OpenKB). | Only direct sources are considered. Summaries, overviews and linking pages are never marked stale. LLM lint is a guess. | Live staleness for every artifact: `dakc explain`/`status` walks the graph from changed inputs, with the reason ("source X modified"). |
| **Dependency invalidation (transitive)** | Source↔source closure over shared concepts (llm-wiki-compiler `deps.ts:86-110` ✔). A k-hop link search for the UI (open-knowledge). | None propagate page → summary → index. llm-wiki-compiler's closure *over*-invalidates: a whole connected group of sources is re-extracted when reuse is off. | Transitive invalidation through all layers, bounded by signatures and **early cutoff**: propagation stops wherever a rebuilt artifact's output hash is unchanged. |
| **Selective regeneration** | `refresh --stale [--dry-run]` selects by source (llm-wiki-compiler); an LLM plan picks pages to touch (OpenKB, okforge); WeKnora rewrites touched pages in full. | Selection is by source or by LLM judgement, never by a dependency diff. Hub pages are fully rewritten on every contribution (WeKnora #4016, open ✔). Seed/overview pages call the LLM on *every* compile, even with no changes (llm-wiki-compiler `seed-pages.ts:183` ✔). | Two-level decision per page: **regenerate** (LLM) only when the claims it is built from change; **rerender** (code) when only provenance changes. |
| **Failure recovery** | Journal + hard-link snapshot + crash rollback for `add` (OpenKB/okforge `mutation.py`); per-batch intent journal (llm-wiki-compiler); durable queue + page/revision transactions (WeKnora); purge journal (LightRAG). | Partial: `remove`/`recompile` are not journaled (OpenKB/okforge). Failed page generations are swallowed and the add still commits (OpenKB #239). No project offers all-or-nothing publication of a whole compile (WeKnora #2325). | Compute phase writes nothing. One SQLite transaction commits every version, edge and source row. Temporal tables give rollback to any previous build. The extraction cache survives failures, so retries don't re-pay. |
| **Deterministic compilation** | Deterministic index/log steps; provenance stamps (llm-wiki-compiler `provenance.ts:31-36`). | No project sets temperature or seed. Results depend on ingest order and on the current wiki state passed to the prompt. | Deterministic everywhere except the provider call. Inputs are sorted and canonical. Deterministic mock providers make full vs incremental **byte-comparable**, which is how correctness is tested. |
| **Cost optimization** | Extraction snapshot keyed by source hash + model + prompt (llm-wiki-compiler); LLM response cache keyed by request hash (GraphRAG `with_cache.py:67-102` ✔, Colin `cache.py:109-183` ✔); Anthropic prompt caching (llm_wiki, OpenKB, okforge); per-scope token budgets (Raymonda). | Caches are keyed by whole requests or whole files, so an edited file misses entirely. Planning prompts grow with wiki size (OpenKB #203/#226; llm-wiki-compiler #267 is an open question about scale ✔). Token use mostly logged, not reported. | Work avoided **structurally** (fewer calls), not just memoized. A per-build token ledger, and dry-run plans that state the LLM-call count before spending it. |

## Capability grid

✓ = implemented and verified in code · ◐ = partial / coarse · ✗ = absent ·
📄 = documented but not working in code

| | llm_wiki | OpenKB | okforge | WeKnora | ms/llmwiki | open-knowledge | LLM-WIKI | llm-wiki-compiler | LightRAG | GraphRAG | Riverbank | Colin |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Content-hash change detection | ◐ file | ◐ file | ◐ file | ◐ file | ✗ | ◐ agent convention | ◐ file | ◐ file | ◐ doc | ✗ title | ✓ fragment | ◐ file |
| Explicit dependency store | ◐ 1-hop | ◐ 1-hop | ◐ 1-hop | ◐ 1-hop | ◐ 1-hop | ✗ | ◐ 1-hop | ◐ 1-layer | ◐ chunk→KG | ✗ | 📄 | ✓ hand-declared |
| Claim-level lineage | ✗ | ✗ | ✗ | ◐ chunk sets | ✗ | ✗ | ◐ by name | ◐ citations | ◐ chunk | ✗ | 📄 triples | ✗ |
| Sub-file incremental extraction | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✓ | n/a |
| Transitive invalidation (pages → summaries) | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | n/a | ✗ | 📄 | ◐ no cutoff |
| Early cutoff | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |
| Deletion retracts unsupported facts | ✗ | ✗ | ✗ | ◐ LLM | ✗ | ✗ | ✗ | ◐ | ✓ | ✗ | ✗ | n/a |
| Modified-source update path | ◐ full re-ingest | ◐ additive | ◐ additive | ◐ full doc | ✗ | ✗ | ✗ | ◐ per source | ✗ delete+insert | ✗ | ◐ | ✓ |
| Dry-run plan | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ◐ refresh only | ✗ | ✗ | ✗ | ✗ |
| All-or-nothing commit / rollback | ✗ | ◐ add only | ◐ add only | ◐ per page | ✗ | ◐ checkpoints | ✗ | ◐ per batch | ◐ journal | ◐ backup copy | ✗ | ✗ |
| Equivalence check vs full rebuild | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |

## Notable failure reports in the wild

These are users and maintainers describing the problem DAKC targets:

- **llm_wiki #837 (open)**: "Re-ingest shortens single-source pages and creates
  duplicates". The merge prompt tells the model to "Preserve every factual
  claim from both inputs" (`ingest.ts:3461` ✔), so **retracted claims
  survive a source edit**.
- **WeKnora #4016 (open)**: "Wiki hub pages are fully re-synthesized on
  every contribution update, causing unbounded write amplification" ✔.
- **WeKnora #3191 comment (unanswered)**: "supersede one fact X; 2 Wiki pages
  cited X; 500 did not; expected: the 2 dependent pages are regenerated, the
  other 500 require no work" ✔. This is DAKC's core acceptance test, asked
  for by a user.
- **WeKnora #1679 (open)**: reuse caches when re-parsing instead of
  recomputing everything ✔.
- **LightRAG #2219**: maintainer: "After updating the file, the old document
  must be deleted first, then upload the new version" ✔.
- **LLM-WIKI README roadmap**: "Incremental re-compilation when a source is
  updated or removed" listed as future work ✔.
- **llm-wiki-compiler #267 (open)**: scaling to tens of thousands of documents
  is an open question ✔.
- **open-knowledge #1680 / #1681 / #2286**: requests for provenance/freshness
  validation and deterministic regeneration hooks.

## Adjacent technology (reused, not reinvented)

See [`audits/adjacent_prior_art.md`](audits/adjacent_prior_art.md).

| Idea | Source | Use in DAKC |
|---|---|---|
| Verifying traces, early cutoff | *Build Systems à la Carte* (Mokhov, Mitchell, Peyton Jones, ICFP 2018); Shake; Bazel change pruning; Salsa "backdating" | Signature-based rebuild rule; cutoff at claim, page-lead and index layers |
| Content addressing / Merkle DAG | Git, Nix | Content-addressed claim ids and extraction cache; source→passage hashing |
| Support counting under deletion | Incremental view maintenance (pg_ivm, Differential Dataflow) | A claim survives while ≥ 1 supporting passage exists |
| State-based selection (`state:modified+`), asset versions | dbt, Dagster `code_version`/`data_version` | Provider fingerprints in signatures; downstream selection by graph traversal |
| Temporal tables | SQL:2011 system-versioned tables | `from_build`/`to_build` validity for snapshots, diff and rollback |
| Provenance vocabulary | W3C PROV-O, OpenLineage | Edge kinds map to `wasDerivedFrom`; export-friendly |

## What existing projects do *better* than the plan, and we should borrow

- **LightRAG's** delete-vs-rebuild classification and destructive ordering
  (repair dependents first, delete inputs last).
- **llm-wiki-compiler's** inclusion of model + prompt version in extraction
  cache keys, and its stale/orphaned freshness vocabulary.
- **OpenKB/okforge's** "record the source hash last" rule: a failed add is
  automatically retried.
- **microsoft/llmwiki's** heartbeat lock file for multi-process safety (not
  in the MVP; SQLite's `BEGIN IMMEDIATE` covers single-host writers).
- **Colin's** skip-dependents-of-failed-upstream behaviour.
