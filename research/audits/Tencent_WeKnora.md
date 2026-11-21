# Tencent/WeKnora: competitive audit (self-maintaining Wiki, ingestion, update/delete, graph)

- Snapshot: shallow clone at `1ef55d4` (merge of PR #5712), audited 2026-11-21. Go backend under `internal/`.
- Method: I read and grepped the code only. Nothing was run. Citations are `path:line` relative to the repo root. GitHub issues and PRs were read with `gh`.
- Scope: Wiki ingest (`internal/application/service/wiki_ingest*.go`), the knowledge (document) lifecycle (`knowledge_create.go`, `knowledge_replace.go`, `knowledge_process.go`, `knowledge_delete.go`, `knowledge_datasource_replace.go`, `datasource_service.go`), the Neo4j graph (`repository/retriever/neo4j/repository.go`, `extract.go`), lint (`wiki_lint.go`), and the schema (`migrations/versioned/000037,000041,000061,000075,000077`).

## TL;DR

WeKnora ships a mature, production-hardened LLM wiki pipeline: a durable per-KB op queue, map/reduce batches, per-slug locks, optimistic write snapshots, revision history, dead letters and crash recovery. It has a document-level reverse index (`wiki_pages.source_refs` JSONB with a GIN index). Deletes and reparses use that index to find the pages a source touched.

It is not a dependency-aware incremental compiler:

- **Change detection is file-level only.** Duplicates are caught by an MD5 of the uploaded bytes. Connectors use the source's mtime cursor. Nothing is hashed at chunk, section or claim level for documents.
- **Any change reprocesses the whole document.** All chunks are deleted and re-created with new UUIDs, and all embeddings and the document's graph namespace are rebuilt. The Wiki "map" stage (3+ LLM calls) runs again over the whole document. Every page the document touches then gets a full-page LLM rewrite.
- **Lineage stops at (page → document) and (page → chunk-ID set).** No claim or fact store exists. Retraction asks an LLM to remove claims "ONLY sourced from the deleted document", judged against the other documents' *summaries*.
- **No transitive invalidation.** Synthesis and comparison pages built from other wiki pages are not invalidated when those pages change. The index intro is updated from a change log, not recomputed from its inputs. Cross-links and dead links are repaired by text passes.
- **Maintainers know about the gap and have not fixed it.** Open issues #1679 (reuse caches / content-addressed chunk IDs), #4016 (store per-source contributions instead of full-page rewrites) and #3191 (supersede instead of delete), plus several unmerged PRs (#5084, #2844, #5522), describe parts of our proposal. Issue #2232 records whole-page reparse instead of fact-level incremental as a deliberate trade-off.

---

## 1. Source ingestion pipeline

### Entry points
- **File upload:** `knowledge_create.go:36` `createKnowledgeFromFile` → MD5 hash and duplicate check (`:98-134`) → save file → insert the `knowledges` row → enqueue `TypeDocumentProcess` (asynq).
- **URL / file URL:** `knowledge_create.go:380` (URL hash = `md5(url)`), `:620`.
- **Manual (markdown) knowledge:** `knowledge_create.go:1042` `UpdateManualKnowledge` → `enqueueManualProcessing(..., needCleanup=true)`.
- **Datasource sync** (Feishu, Confluence, Notion, GitLab and others): `datasource_service.go` `ingestItem` (~`:1800-1967`) → `CreateKnowledgeFromFile` / `CreateKnowledgeFromURL`, or `ReplaceKnowledgeFromDataSource` for an existing `external_id` (`:1850-1883`).
- **Reparse / batch reparse:** `knowledge_process.go:3124` `ReparseKnowledge`; route `POST /knowledge/batch-reparse` (`router/routes_knowledge.go:157`).
- **In-place file replacement:** `knowledge_replace.go:41` `ReplaceKnowledgeFile`.

### Stages (document → chunks → enrichments)
1. Parse (docreader) → chunks. `processChunks` (`knowledge_process.go:530`) deletes all old chunks, vectors and the graph namespace first (`:622-663`), then creates new chunks with `uuid.New()` IDs (`:732`, `:772`) and embeds them.
2. Post-process fan-out (`knowledge_post_process.go:191-453`) computes the subtask count and spawns summary, question generation (batched), Graph RAG extraction (one task per chunk, `:420-436`) and **one Wiki op per document** (`:219`, `:320-334`, `:442-453`). The Wiki op is written atomically with the `processing→finalizing` transition (`SeedKnowledgeFinalizingWithPendingOp`, `repository/task_queue.go:148`).

### Wiki pipeline (KB-scoped, debounced, durable)
- **Enqueue.** A row goes into `task_pending_ops` (task_type `wiki:ingest`, scope = KB, `dedup_key = knowledge_id`) plus a debounced asynq trigger (`ProcessIn(30s)`) (`wiki_ingest.go:530-647`, delay at `:130`). Several ops for the same document collapse last-write-wins at consume time (`wiki_ingest.go:1206-1258`).
- **Batch** (`ProcessWikiIngest`, `wiki_ingest_batch.go:260`). Each batch claims up to `IngestBatchSize` (default 5) ops via `SELECT … FOR UPDATE SKIP LOCKED` (`repository/task_queue.go:277-342`). A per-KB in-flight cap applies (`wiki_ingest_batch.go:461-470`). Lite mode uses a process-local lock (`:370-378`).
- **Map**, per document, in parallel (`mapOneDocument`, `wiki_ingest_batch.go:1381-1836`):
  - Liveness guard: the Redis tombstone, then the DB (`:1407-1418`, `wiki_ingest.go:3056`).
  - Load text and OCR chunks, then rebuild the content and **truncate it to 32,768 runes** (`:1423-1443`; const `wiki_ingest.go:42`).
  - Read `oldPageSlugs` = pages whose `source_refs` contain this document (`:1479`, `wiki_ingest.go:2131`).
  - **LLM #1, Pass 0 candidate extraction** (`extractCandidateSlugs`, `wiki_ingest_cite.go:73`). Includes trigram prefiltering plus an **LLM dedup** call against existing pages (`wiki_ingest_cite.go:118` → `deduplicateExtractedBatch`, `wiki_ingest.go:2433`). If it fails, the code falls back to a legacy single-shot extractor (`:1495-1506`).
  - **LLM #2, document summary** (`WikiSummaryPrompt`, `:1569`) and **LLM #3..N, chunk-citation classification**, one call per chunk batch (`classifyChunkCitations`, `:1597`; `wiki_ingest_cite.go:257`). These run in parallel.
  - Identity stabilisation through Redis identity claims (`:1614`, `wiki_ingest_dedup.go:287`).
  - Emit `SlugUpdate`s: one summary, plus entity and concept additions that carry `SourceChunks` (`:1691-1735`). Reparse reconciliation adds more (see §5).
- **Remap identities and plan taxonomy.** One LLM call for the batch's folder tree (`wiki_ingest_batch.go:714-722`, `wiki_ingest_taxonomy.go:32`).
- **Reduce**, per slug, in parallel, under a Redis per-slug lock (`wiki_ingest_batch.go:771-855`, `withSlugLock` `wiki_ingest.go:999`):
  - Summary pages are **overwritten wholesale** with no LLM call in reduce (`:2132-2160`).
  - Entity and concept pages go through **LLM #k, full-page rewrite** (`WikiPageModifyUserPrompt`). The prompt contains the existing page, new verbatim cited chunks, the deleted documents' summaries and the remaining sources' summaries (`:2162-2392`; prompt `agent/prompts_wiki.go:321-411`).
  - Writes are guarded by an optimistic snapshot (`:2111`) and a "dropped table rows" truncation guard (`wiki_page.go:143-227`).
- **Publish drafts.** A status flip (`wiki_ingest_batch.go:892-895`).
- **Finalize** (debounced per KB, `ProcessWikiFinalize`, `wiki_ingest_batch.go:1102-1356`):
  - LLM index-intro update from a change description (`rebuildIndexPage`, `wiki_ingest.go:2235`).
  - Dead-link cleanup (`:1843`) and cross-link injection (`:1939`), both text-only, over the affected slugs only.
  - Pruning of empty folders.

**LLM calls per ingested document:** 1–2 extraction, 1 dedup, 1 summary, ⌈chunks/batch⌉ citation calls, 1 full-page rewrite per touched entity or concept page, a share of the taxonomy and index-intro calls, and, if graph is on, 1 call per chunk for Graph RAG.

## 2. Source change detection

| Path | Mechanism | Granularity | Evidence |
|---|---|---|---|
| File upload | **MD5** of the uploaded bytes, matched with the same `file_type` in the KB. Falls back to (name, size, type) | whole file | `knowledge_util.go:130-149`; `repository/knowledge.go:420-484`; `knowledge_create.go:98-134` |
| URL | `md5(url)`, which is identity, not content | URL string | `knowledge_create.go:380,620` |
| In-place file replace | Skip if `hash == existing.FileHash && same name && same folder`; otherwise do a full reparse | whole file | `knowledge_replace.go:106-127` |
| Manual knowledge edit | **None**. Every publish re-processes | n/a | `knowledge_create.go:1042-1160` |
| Datasource connectors | Source **mtime / edit-time cursor** per node (Feishu `obj_edit_time` / `modified_time`, Notion `PageEditTimes`, …) skips unchanged items | per remote item | `datasource/connector/feishu/core/engine.go:215-225`; `feishu/drive/connector.go:285-290`; `datasource_service.go:1838-1840` |
| Datasource update | **No content-hash comparison.** A changed (or force-full) item goes to `ReplaceKnowledgeFromDataSource`, which creates a *new* knowledge row with a new ID and runs the full delete cascade on the old one | whole item | `knowledge_datasource_replace.go:73-130`; issue #4237 (open) |
| Chunks | `chunks.content_hash` exists, but is used only for **FAQ** diffing and manual chunk edits. Document chunks are regenerated with fresh UUIDs on every parse | n/a for docs | `types/chunk.go:184-185`; `repository/chunk.go:1247-1320` (FAQ); `knowledge_process.go:626,732,772` |
| Wiki map | **None.** `mapOneDocument` always re-runs the LLM | n/a | `wiki_ingest_batch.go:1381-1836` |

## 3. How derived artifacts are associated with sources

- `wiki_pages.source_refs JSONB` holds document-level refs, either `"<knowledge_id>"` or legacy `"<knowledge_id>|<title>"` (`migrations/versioned/000037_wiki_and_indexing.up.sql:35`; `types/wiki_page.go:235-239`).
- `wiki_pages.chunk_refs JSONB` is the union of chunk UUIDs cited by additions (`types/wiki_page.go:240-247`; written by `mergeChunkRefs`, `wiki_ingest_batch.go:2413,2471-2491`).
- `wiki_pages.in_links / out_links JSONB` hold slug arrays. Out-links are parsed from `[[slug]]` in the content (`wiki_page.go:267`), and in-links are maintained atomically (`repository/wiki_page_links.go:28-60`).
- Summary page slug = `summary/<knowledge_id>` (`wiki_ingest_batch.go:1525`).
- GIN indexes on `source_refs`, both `jsonb_path_ops` containment and text (`migrations/versioned/000041_task_queue_and_wiki_indexes.up.sql:139-143`). They back `ListBySourceRef` (`repository/wiki_page.go:540-570`).
- Agent-written `synthesis` / `comparison` pages carry model-supplied `source_refs` resolved against knowledge rows (`agent/tools/wiki_source_refs.go:29-78`; `wiki_write_page.go:132-208`). They do **not** record which wiki pages they were synthesised from.
- Graph nodes are keyed `{name, kg: knowledge_id}` under per-KB and per-knowledge labels, with a `chunks` list (`neo4j/repository.go:76-80`; `extract.go:379-382`). Entities are **not shared across documents** in the graph.
- Revisions: `wiki_page_revisions` stores a snapshot per superseded version, with edit source pipeline / agent / user / revert (`000075_wiki_page_revisions.up.sql`; `types/wiki_page.go:318-344`).
- **No frontmatter. No claim, fact or contribution table.**

## 4. Is there an explicit, reverse-traversable dependency graph?

**Partially, one hop only.**

- **Source → pages:** yes. `ListBySourceRef` / `ListSlugsBySourceRef` run a reverse lookup over the GIN-indexed JSONB (`repository/wiki_page.go:540-570`). Delete, retract and reparse all use it (`knowledge_delete.go:257`, `wiki_ingest_batch.go:611`, `wiki_ingest.go:2131`).
- **Source → chunks → graph nodes:** by knowledge namespace (`types/extract_graph.go:45-58`).
- **Page → page:** `out_links` / `in_links` exist, but only as navigation links. They are never used as invalidation edges. When an entity page changes, nothing re-derives the synthesis pages, summaries or the index that link to it. Only dead links get stripped (`cleanDeadLinks`, `wiki_ingest.go:1843-1917`).
- **Transitive:** no. There is no artifact → artifact derivation edge such as "synthesis page X was built from entity pages A and B", no topological ordering, and no fixpoint propagation.
- The index page is an incremental LLM update from a change description (`wiki_ingest.go:2217-2234`). It is not recomputed from its dependencies.

## 5. What happens on a source update

**The document is fully re-ingested. Selective regeneration happens only at page granularity, decided by LLM extraction overlap.**

1. **Upstream (deterministic, total):** `ReplaceKnowledgeFile` / `ReparseKnowledge` → `processChunks` deletes **all** chunks (new UUIDs), all vectors and the document's whole graph namespace, then rebuilds (`knowledge_process.go:3136-3349`, `:614-663`). Every chunk then gets per-chunk Graph RAG LLM calls again. An in-place reparse keeps the old content searchable until the swap (`:3204-3206`, #5002).
2. **Wiki side:** `prepareWikiForReparse` only scrubs queued ingest ops (`knowledge_delete.go:367-393`). The re-ingest then runs the **whole map stage again** over the new content. Reconciliation against `oldPageSlugs` (`wiki_ingest_batch.go:1737-1792`) has three cases:
   - (a) The old slug is **not** produced by the new extraction. The page and its source ref are **kept unchanged** ("Extraction can omit previously produced slugs even when the document still supports them", `:1741-1744`). **Stale content from the old version survives indefinitely.** This was a deliberate change in PR #5186, made after issue #5182 found that reparse blanked pages. The old `retractStalePages` survives only as comments (`wiki_ingest.go:2152-2165`), and the `"retractStale"` type is still accepted but no longer emitted (`wiki_ingest_batch.go:2123`).
   - (b) The old slug is produced again (entity or concept page): emit `retract` (with the document's *prior summary body* as the "old version") plus the addition. The LLM is asked to "swap" the document's contribution in one full-page rewrite (`:1746-1753`, `:1780-1789`).
   - (c) Summary page: overwritten wholesale (`:2132-2160`).
3. **New slugs:** created.
4. **`chunk_refs` are never pruned on reparse.** The union keeps the dead UUIDs (`wiki_ingest_batch.go:2462-2470`). Open PR #2844 addresses this.
5. **Granularity:** whole document in, whole page out. There are no claim or fact units. Retraction relies on LLM instruction-following: "REMOVE facts/claims that were ONLY sourced from the `<deleted_documents>` and are NOT present in any `<remaining_source_documents>`" (`agent/prompts_wiki.go:387`). The remaining sources are represented by their **summary pages**, not their evidence (`wiki_ingest_batch.go:2181-2201`).
6. **Hub pages:** every contribution change causes a full-page re-synthesis. Issue #4016 (open) reports a 147K-char page with 472 source refs being regenerated on each small update.
7. **Datasource "update"** = new knowledge ID plus a full delete cascade of the old ID, then a fresh ingest (`knowledge_datasource_replace.go:116-128`). This is a retract plus an add, not an in-place update.

## 6. Source deletion

`executeKnowledgeDelete` (`knowledge_delete.go:477-682`):

- Marks rows `deleting` and dequeues in-flight tasks (`:485-512`).
- **Wiki** (`cleanupWikiReferences`, `:208-323`), which runs before the chunks are deleted so chunk IDs are still resolvable (`:575-582`):
  - Writes a Redis tombstone `wiki:deleted:{kb}:{kid}` with a 1h TTL and scrubs pending ingest ops (`:227-238`).
  - `ListBySourceRef` → **persists a retract op first** so a retry can rediscover the pages (`:257-290`).
  - For each page: if the deleted document was the **only** source, the page is **hard-deleted** (soft-delete row, revisions dropped, in-links removed; `wiki_page.go:500-525`). Otherwise its `source_refs` entry and that document's `chunk_refs` are stripped (`:298-314`).
  - The async retract batch then **re-queries** `ListBySourceRef` at run time to catch race-born pages (`wiki_ingest_batch.go:582-653`). It does an LLM retract-rewrite of each multi-source page (`:2171-2213`). Empty folders are pruned only once the ingest lane has drained (`:1287-1311`).
- **Chunks:** soft-deleted (`:584-591`). **Vectors:** deleted per (store, model, type) group (`:537-573`). **Graph:** `DelGraph` over the document's namespace via APOC batches (`:593-607`; `neo4j/repository.go:138-182`).
- **Orphan leftovers:**
  - Dead `[[links]]` in non-affected pages are cleaned only on pages in the finalize "affected" set or via lint auto-fix.
  - Auto-created folders can linger (issue #5093, open).
  - Synthesis pages that cite a deleted page but not the document in `source_refs` are untouched except for dead-link stripping.
  - Lint `stale_ref` detects refs to deleted knowledge (`wiki_lint.go:194-221`), and AutoFix strips them or deletes the page (`:404-430`) **without** an LLM content rewrite.

## 7. Duplicate detection

- **Same bytes, different path:** caught for file uploads. The MD5 plus file-type match is KB-wide, not path-scoped (`repository/knowledge.go:447-464`). Issue #5005 asks to scope it per folder. Connector items are scoped by `(datasource_id, external_id)` (`knowledge_create.go:114-120`).
- **Renames:** `ReplaceKnowledgeFile` treats the same hash with a different name or folder as a change and **reparses fully** (`knowledge_replace.go:115`). No rename-without-recompile exists.
- **Near-duplicate documents:** none.
- **Wiki entity dedup** (cross-document identity; this is the strong part):
  - pg_trgm title prefilter plus char-bigram Jaccard (`wiki_ingest_dedup.go:15-176`, `:723-803`).
  - An LLM dedup call (`wiki_ingest.go:2433`).
  - Exact normalised-title identity binding and Redis identity claims across concurrent batches (`wiki_ingest_dedup.go:209-340`, `:481-720`).
  - Cross-type twin reuse, concept vs entity (PR #3547 / #3526).
  - Gaps: lint `duplicate_slug` is defined but **never emitted** (`wiki_lint.go:22`; noted in #2232). A manual merge is undone by the next reparse (#5199, open).
- **Chunk-level dedup:** only for FAQ entries (`knowledge_faq_import.go:1083-1245`).

## 8. Failure recovery, atomicity, idempotency, retries

Strong, but at operation and page level, not as a compile transaction.

- **Durable queue:** `task_pending_ops` in Postgres, with claim and stale-claim recovery after 90 min (`wiki_ingest.go:52-58`; `000041…up.sql:51-70`). Claims are released on abnormal exit (`wiki_ingest_batch.go:536-550`). Triggers are re-armed at startup from durable rows (`container/recover_pending_wiki_tasks.go:29-…`). Housekeeping re-arms them too (`knowledge_housekeeping.go:872`).
- **Retries:**
  - LLM call: 3 attempts with exponential backoff, transient errors only (`wiki_ingest.go:2775-2803`, `:2989-3044`).
  - Per document: `fail_count`, at most 5 batch cycles before dead-lettering to `task_dead_letters` (`wiki_ingest.go:1317-1390`).
  - asynq: `MaxRetry` 10 for the wiki trigger (`:163`).
  - Rate-limit-aware backoff (`wiki_ingest_batch.go:1081-1087`).
- **Atomicity:**
  - A page write plus its revision snapshot is one DB transaction (`repository/wiki_page.go:133-150`, `ON CONFLICT DO NOTHING` for idempotent snapshots).
  - Optimistic snapshot conflict detection with retry (`wiki_ingest_batch.go:2038-2064`, `:2111`).
  - Per-slug Redis lock with TTL (`wiki_ingest.go:94-102`).
  - Atomic finalizing handoff with the pending op (`knowledge_post_process.go:320-334`).
- **Partial failure:** **no cross-page atomicity per document or batch.** If some of a document's slugs land and others fail (lock timeout, LLM error, truncation refusal), the document op is re-queued and the **whole map stage runs again** on retry. Pages that already landed are re-touched through the retract+add swap (`wiki_ingest_batch.go:1017-1038`). Readers can see a half-applied document between batches. Issue #2325 (open) proposes atomic release namespaces.
- **Idempotency:**
  - Dedup by `knowledge_id` within a batch (last op wins).
  - The tombstone guards against ingest/delete races (`isKnowledgeGone`, `filterLiveUpdates` `wiki_ingest_batch.go:1958`).
  - Re-running ingest is *semantically* idempotent only to the extent the LLM honours "replace-not-append". It is not byte-idempotent.
- **Truncation guards:** completion-budget continuation up to 3 rounds (`wiki_ingest.go:2806-2871`). Refusal when a rewrite drops table rows (`wiki_page.go:143-227`).

## 9. Full-rebuild operations

- **Per-document reparse** (`POST /knowledge/:id/reparse`) and **batch reparse** (`POST /knowledge/batch-reparse`, `routes_knowledge.go:157`). Each re-parses, re-embeds, re-extracts the graph and re-ingests the Wiki for the selected documents.
- **Enabling the Wiki on an existing KB does not backfill.** It only creates `WikiConfig` (`knowledgebase.go:669-671`), so a batch reparse is needed. Issue #2294 (closed) complained there was no wiki rebuild.
- `POST /wiki/rebuild-links`: deterministic re-parse of `[[links]]` across all pages (`wiki_page.go:1112-1165`).
- `GET /wiki/lint` and `POST /wiki/auto-fix` (`routes_knowledge.go:385-387`).
- Admin `/system/admin/runtime/documents/wiki-start` only re-arms existing pending ops (`document_progress_start.go:39-90`).
- `RebuildIndexPage` on the service is a **no-op stub** (`wiki_page.go:2255-2259`).
- No "drop all wiki pages and regenerate from sources" operation exists.
- Rebuilds are needed after a model or prompt change, after enabling the Wiki, after config changes (#5591 open), and to recover from bad merges.

## 10. Open issues, PRs and roadmap

| # | State | Relevance |
|---|---|---|
| #1679 | OPEN | Reparse = "delete all and recompute everything"; asks for content-addressed stable chunk IDs, plus VLM/embedding/Wiki-map caches keyed by content+model+prompt version. Explicitly notes Wiki reduce is not cacheable. **Our proposal's caching half.** |
| PR #5084 | OPEN | Persistent processing cache (parse, OCR, embedding, summary, graph, Wiki map). Keeps random chunk UUIDs and **no diff indexing or atomic publish** ("仍按现有流程重写 chunk 和索引，未引入差异索引或原子发布"). Default off. |
| PRs #1855, #2219, #2450, #1918, #2127, #2131, #2177 | CLOSED (unmerged) | Repeated attempts at content-addressed chunk IDs / reparse caches |
| #4016 | OPEN | Hub pages fully re-synthesised on every contribution; proposes persisting contributions keyed `(kb, slug, knowledge_id)` and assembling pages deterministically. **Our proposal's contribution-level lineage half.** A comment notes `updated_at` is bumped by non-content writes, so it cannot serve as a freshness signal. |
| #3191 | OPEN | Graph has no time axis; reparse deletes and rebuilds; proposes supersede-not-delete with a normalised topic key. A commenter explicitly asks for **artifact → facts provenance so supersede(X) finds dependent wiki pages** ("2 pages cited X, 500 did not → only 2 regenerated"). No maintainer answer. |
| #3192 | OPEN | Carry the source version (git commit, Feishu revision) into the graph |
| #4237 | OPEN | Datasource overwrite deletes and recreates unchanged documents (no content check), cascading slow Wiki cleanup |
| #2232 | OPEN | Wiki quality / self-heal; documents the **deliberate choice of whole-page reparse over fact-level incremental**, LLM-dependent replace semantics, and the unimplemented duplicate-slug lint |
| #5182 → PR #5186 | CLOSED / MERGED | Reparse blanked every sourced page; the fix now **preserves** omitted slugs (stale-content risk) |
| PR #2844 | OPEN | Remove stale `chunk_refs` before reparse |
| PR #5522 / #4951 | OPEN | Resume long-document ingestion without rewriting completed pages (per-segment checkpoints) |
| #2325 | OPEN | Versioned wiki release namespaces with atomic activation and manifest hashes |
| #3258 | OPEN | No token accounting, cost estimate or budget for the ingest/Wiki pipeline; one incident burned ~400–500M tokens, with one hub page rewritten 270× |
| #4826, #5199 | OPEN | Pipeline overwrites manual edits; manual merges are undone by the next reparse (no slug suppression) |
| #5093 | OPEN | Deleting a document leaves auto-created wiki folders behind |
| #3479 | OPEN | User request for stale detection of agent-generated content |

## 11. Other notable points

- **Stale detection:** lint checks orphans, broken links, empty content, `stale_ref` (ref to *deleted* knowledge only) and missing cross-refs (`wiki_lint.go:94-345`). Nothing detects pages derived from a *changed* source or an outdated input hash. Lint is on-demand only, with no scheduler. `wiki_page_issues` plus the agent tool `wiki_flag_issue` let an LLM or user flag `out_of_date` pages manually (`agent/tools/wiki_flag_issue.go`).
- **Determinism:**
  - Temperature 0.3, thinking off (`wiki_ingest.go:2734`).
  - Slugs come from LLM output (the summary slug is derived from the document ID).
  - The source-context ordering is sorted for prefix-cache stability (`wiki_ingest_batch.go:2273-2280`).
  - Output is not reproducible, and nothing records input hashes per artifact.
  - `version` bumps only on user-visible content change (`wiki_page.go:229-290`).
- **LLM caching:**
  - In-process `singleflight` coalescing of *byte-identical concurrent* prompts only (`wiki_ingest.go:417-419`, `:2749-2757`, `:2888`).
  - Prompt-prefix "warmup" serialisation so provider-side prefix caches hit during the parallel reduce (`:2735-2746`, `:2926-2949`).
  - **No persistent response cache** (PR #5084 would add one).
- **Cost accounting:** token usage, including cached tokens, is **logged only** (`models/api/usage_log.go:14-30`) with a purpose label (`wiki_ingest.go:2747`, `:2903-2924`). Nothing is persisted per document or page (#3258).
- **Truncated input:** Wiki extraction and summary see only the first 32,768 runes of a document (`wiki_ingest_batch.go:1440-1443`). Citation classification sees all chunks.
- **Scale engineering:** the system targets 40k-document KBs with per-KB in-flight caps, a debounced finalize lane, a cursor-streamed lint, and no full-corpus loads in cross-linking (`wiki_ingest.go:1919-1937`).
- **Bi-temporal supersede exists only for conversational memory** (`types/memory.go`, per #3191), not for documents or wiki pages.

---

## Capability matrix vs. proposed engine

| Capability | WeKnora | Limitation |
|---|---|---|
| Source change detection | MD5 of whole file (`knowledge_util.go:130`; `knowledge_replace.go:115`); connector mtime cursors (`feishu/core/engine.go:218-224`) | File-level only; none for manual edits; datasource update path has no hash check (#4237); chunks get new UUIDs every parse |
| Dependency tracking | `wiki_pages.source_refs` JSONB + GIN reverse lookup (`repository/wiki_page.go:540`); `chunk_refs`; graph namespace per document | One hop (doc → page); page → page links are navigation only, not derivation edges |
| Claim-level lineage | Chunk-citation pass binds chunk IDs to entity/concept candidates (`wiki_ingest_cite.go:257`; `wiki_ingest_batch.go:2413`) | Page-level union of chunk IDs; prose is free-form LLM output; refs go stale on reparse (PR #2844) |
| Incremental recompilation | Only new or changed documents are processed; per-slug reduce touches only pages the document maps to (`wiki_ingest_batch.go:1737-1792`) | Whole-document re-map (3+ LLM calls) and full-page rewrites; no caching of unchanged work (#1679) |
| Source deletion handling | Tombstone, single-source pages deleted, multi-source pages stripped and LLM-retracted, vectors/chunks/graph deleted (`knowledge_delete.go:208-323, 477-682`) | Retraction is LLM judgment over summaries; dead folders and links linger (#5093) |
| Stale artifact detection | Lint `stale_ref` for deleted sources (`wiki_lint.go:194-221`); manual `wiki_flag_issue` | No detection of changed-source staleness; on-demand only; reparse deliberately *keeps* omitted slugs (#5186) |
| Dependency invalidation (transitive) | None | Synthesis pages, index intro and linking pages are never invalidated by upstream page changes |
| Selective regeneration | Reduce only on slugs in the update set; finalize only on affected slugs (`wiki_ingest_batch.go:1282-1285`) | Selection = LLM extraction overlap, not a dependency diff; hub pages fully re-synthesised (#4016) |
| Failure recovery | Durable PG queue, SKIP LOCKED claims, dead letters, revision snapshots in a transaction, startup re-arm (`task_queue.go:277`; `wiki_ingest.go:1317`; `recover_pending_wiki_tasks.go`) | No cross-page atomic publish (#2325); retries redo the whole map stage |
| Deterministic compilation | Sorted prompt context, version-bump-on-content-change only | Temperature 0.3, LLM-chosen slugs, no input-hash manifest |
| Cost optimization | Debounced batching, singleflight, prefix-cache warmup, finalize coalescing, granularity knob | No persistent LLM cache; no token accounting or budget (#3258) |

**Verdict:** PARTIAL. WeKnora has a production-grade *incremental-by-document* LLM wiki with a document → page reverse index and solid queue and rollback machinery. It lacks content-hash diffing below the file, claim or contribution-level lineage, transitive invalidation and output caching. Its maintainers and contributors have filed exactly these as open issues and PRs (#1679, #4016, #3191, #5084) without landing them.

## Takeaways for our project

- **Borrow:**
  - A durable op queue keyed by source, with last-write-wins coalescing.
  - Tombstones against ingest/delete races.
  - A "persist the retract set before mutating" ordering.
  - Revision snapshots in the same transaction.
  - A truncation guard on LLM rewrites.
  - A per-artifact optimistic snapshot.
  - Debounced KB-global "finalize" for index-like aggregates.
- **Differentiate on:**
  1. Content-addressed chunk and section IDs, so lineage survives reparse.
  2. A persisted contribution/claim table `(artifact, source_span_hash)` that makes retraction deterministic rather than LLM-judged (#4016's ask).
  3. Artifact → artifact derivation edges with transitive invalidation (the question asked in #3191).
  4. An input-hash manifest per artifact, giving stale detection on *changed* sources and not just deleted ones.
  5. A persistent LLM cache keyed by input hash, model and prompt version (#1679).
  6. Atomic multi-artifact publish (#2325).
  7. Per-artifact token accounting (#3258).
- **Watch the trap:** WeKnora retreated from "retract stale slugs on reparse" to "preserve them" (#5182 → #5186) because LLM retraction was destructive. Claim-level provenance is the thing that would make safe retraction possible.
