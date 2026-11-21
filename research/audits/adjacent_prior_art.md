# Adjacent prior art (inspected directly, 2026-11-21)

## microsoft/graphrag @ 34b2b67 (2026-11-01)
- `packages/graphrag/graphrag/index/update/incremental_index.py:29-58` get_delta_docs: delta = titles not in previous `documents` table (new) and previous titles not in input (deleted). Identity = **title**, not content hash. A modified document with the same title is NOT detected as changed.
- `deleted_inputs` is computed but never consumed anywhere (grep: only referenced in incremental_index.py). Deleted documents' entities/relationships/text units remain.
- `index/workflows/update_entities_relationships.py:61-119`: merges delta entities into old ones (`_group_and_resolve_entities`), then re-summarizes *all* merged descriptions via `get_summarized_entities_relationships` — relies on LLM cache to avoid repeat cost.
- `index/run/run_pipeline.py:54-86`: update run copies previous output to timestamped `previous/` backup, writes delta to `delta/`, merges — a snapshot-ish mechanism, but no rollback command.
- `graphrag-llm/.../middleware/with_cache.py:67-102`: LLM response cache keyed by hash of request kwargs → cost optimization via memoization, not dependency analysis.
- Verdict: append-only incremental; no modification detection, no deletion, no dependency graph, no claim lineage.

## HKUDS/LightRAG @ 2537e81 (2026-11-22)
- Doc id = md5 of full text (`lightrag/utils.py:1055` compute_mdhash_id; `lightrag.py:3354`).
- Entities/relations carry `source_id` = chunk-id list (`GRAPH_FIELD_SEP`-joined), plus `entity_chunks` / `relation_chunks` tracking stores (`lightrag.py:2239-2245`) and per-doc `full_entities`/`full_relations` recovery rows.
- Deletion (`lightrag.py:7712-7923`, `adelete_by_doc_id` at 8136): classifies each affected entity/relation as delete-outright (no remaining sources) vs rebuild (remaining chunks); `operate.py:1169 rebuild_knowledge_from_chunks` rebuilds from **cached extraction results** of surviving chunks (no LLM call unless cache missing). Durable purge journal in doc_status metadata; destructive ordering documented (`docs/design/PurgeRecoveryContract.md`). This is the most serious real implementation of provenance-aware invalidation found.
- No update path: maintainer confirms in issue #2219 "the old document must be deleted first, then upload the new version… File uploads are not permitted if the filename conflicts with an existing file."
- Granularity is chunk → entity/relation. No claims, no generated pages/summaries tier, no dry-run plan, no full-rebuild-equivalence check. One tier of dependency (chunk→KG object), not a transitive graph through pages/summaries.

## LlamaIndex IngestionPipeline (run-llama/llama_index, main)
- `llama-index-core/llama_index/core/ingestion/pipeline.py:242-259` DocstoreStrategy UPSERTS / DUPLICATES_ONLY / UPSERTS_AND_DELETE; `_handle_upserts` (469-495) compares per-`ref_doc_id` document hash; on change deletes the ref doc's nodes and re-runs all transformations for that doc.
- `get_transformation_hash` (58): transformation cache keyed by hash(nodes + transformation) — memoization.
- Granularity is document → nodes; no derived pages, no entity reconciliation, no transitive invalidation.

## trickle-labs/riverbank @ 38559dd (2026-05-12) — closest *documented* design
- Docs (`docs/concepts/incremental-compilation.md`) describe exactly our idea: per-fragment xxh3_128 hash skip, `_riverbank.artifact_deps` table, invalidation of dependent artifacts, rendered pages marked stale. ROADMAP.md:31,937-946 marks v0.4.0 "Incremental compilation core — Done".
- Implemented: fragment hash skip (`src/riverbank/pipeline/__init__.py:803-845`) — real.
- **Broken in code**: `catalog/graph.py:331 record_artifact_dep(conn, artifact_iri, dep_kind, dep_ref)` is called from the pipeline as `record_artifact_dep(conn, vocab_triples, frag_iri, profile)` (`pipeline/__init__.py:1543,1606,1618`) — a list of triples as artifact_iri, the fragment IRI as dep_kind, the profile object as dep_ref. Exceptions are swallowed (`except Exception: logger.debug`, graph.py:357). Since `get_artifacts_depending_on_fragment` queries `dep_kind = 'fragment' AND dep_ref = :frag` (graph.py:384-400), it can never match rows written by the pipeline → invalidation is a no-op in practice. Unit test `tests/unit/test_artifact_deps.py:44` calls the function directly with correct args, so the bug is not caught.
- Even if wired, invalidation only deletes dependency rows (`pipeline/__init__.py:815-817` → `delete_artifact_deps`), not the stale triples themselves.
- `rendering/__init__.py:367 mark_pages_stale` is defined but **never called** anywhere.
- No source-deletion handling in `connectors/fs.py` or pipeline. Removed fragments within a modified source are not retracted.
- Requires PostgreSQL + pg_ripple + pg-trickle extensions (not lightweight/local-first).
- Verdict: same idea on paper; implementation does not deliver invalidation. Validates that the gap is real and that *correctness testing against full rebuild* is the missing discipline.

## PrefectHQ/colin @ 827969f (2026-02-02) — dbt-style compiler for LLM-templated docs
- `src/colin/compiler/engine.py:133-206 _is_document_stale`: source sha256 (16 hex) vs manifest, upstream-recompiled-this-run → stale, per-ref version check (`_check_ref_staleness` 208-260), provider config hash, TTL expiry.
- `engine.py:522 _build_dependency_graph` from explicit Jinja `ref()` calls (`_extract_refs_from_ast` 609-640); `allow_stale=True` breaks cycles.
- `compiler/cache.py:109-183` per-LLM-call memo cache keyed by doc_uri + position + input hash + config hash.
- Failed upstream → dependents skipped (`engine.py:307-334`).
- **No early cutoff**: `engine.py:192-197` — any upstream recompiled in this run makes the document stale, even if upstream output_hash is unchanged.
- Dependencies are **hand-declared** in templates; there is no extraction of claims/entities and no automatic provenance discovery. It's a build system for prompts, not a knowledge compiler.

## Build-system / incremental-computation theory to reuse (not reinvent)
- Mokhov, Mitchell, Peyton Jones, "Build Systems à la Carte" (ICFP 2018; JFP 2020): vocabulary of *verifying traces* (store hashes of inputs per key; rebuild iff any dep hash differs) and *early cutoff* (if a rebuilt key's output hash is unchanged, dependents are not rebuilt). Shake, Bazel/Skyframe ("change pruning"), Salsa ("backdating") all implement early cutoff.
- Salsa (rust-analyzer) red-green algorithm: on input change, re-validate dependents lazily; a re-executed query whose value is unchanged is "backdated" so its dependents are reused.
- Adapton / self-adjusting computation (Acar): demand-driven change propagation; dirty-marking then re-evaluation.
- dbt `state:modified+` / Dagster asset `code_version`+`data_version`: content/version-based staleness for derived tables; downstream selection via graph traversal.
- Git / Merkle DAG: content-addressed objects; tree hash summarises children → O(changed path) comparison.
- Incremental view maintenance (pg_ivm, Materialize/Differential Dataflow): maintain derived results under insert/delete deltas; deletions require support counting — directly analogous to "a claim survives deletion of source A if source B still supports it".
- W3C PROV-O / OpenLineage: provenance vocabulary (wasDerivedFrom, wasGeneratedBy) for exportable lineage.
