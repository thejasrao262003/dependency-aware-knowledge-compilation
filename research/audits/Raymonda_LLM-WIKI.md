# Raymonda/LLM-WIKI ("知了"): competitive audit

Audited commit `7388b6e` (2026-09-28, shallow clone, single commit). The repo has 4 stars, 0 issues (open or closed), and 1 merged PR ("Refactor/1.1.0", body only says "1.1.0"). Backend is Java 17 / Spring Boot 3 / MyBatis-Plus / MySQL / Elasticsearch, with optional RocketMQ. It is about 63.8k LOC of main Java plus a Vue 3 UI. Every finding below comes from the code; README claims were checked against it.

Path abbreviations used below:
- `D/` = `llmwiki/app/domain/service/src/main/java/org/cn/liuwt/llmwiki/domain/service/`
- `H/` = `D/harness/`
- `B/` = `llmwiki/app/biz/service/src/main/java/org/cn/liuwt/llmwiki/service/`
- `W/` = `llmwiki/app/web/src/main/java/org/cn/liuwt/llmwiki/web/`
- `DAL/` = `llmwiki/app/common/dal/src/main/java/org/cn/liuwt/llmwiki/common/dal/`
- `INT/` = `llmwiki/app/common/service/integration/src/main/java/org/cn/liuwt/llmwiki/integration/`
- `MIG/` = `llmwiki/app/bootstrap/src/main/resources/db/migration/`

---

## TL;DR

- "Incremental ingestion" means **additive integration of a *new* source into existing pages**: append or merge claims into entity pages that already exist. It does **not** mean change-driven recompilation. The README's own roadmap lists "Incremental re-compilation when a source is updated or removed" as **planned** (`README.md:147`, `README_zh.md:147`).
- Sources are **immutable and content-addressed** (SHA-256 CAS, `raw/{h0:2}/{h2:4}/{sha256}`). An "updated" document is simply a **new `source` row** with no link to the previous version. Sources can never be deleted, only marked `DEPRECATED`.
- Lineage is a flat **page↔source join table** (`wiki_page_source`, plus `execution_id` since V63). It is one hop only: there is no claim table, no entity table distinct from pages, no page→page derivation edges, and no transitive invalidation.
- Claim-level provenance does exist, but as **markdown text**: entity-page bullets end in `（来源：<source name>；…）`, which is parsed by regex. Merging is append-only (`new` / `duplicate_of:i` / `conflict_with:i`). Nothing ever removes a claim when its source changes or is deprecated.
- Staleness is a **lint-time heuristic**: an mtime-vs-`content_updated_at` SQL check plus an LLM probe, and a "page cites a DEPRECATED source" SQL check. The fixes are human-approved, whole-page LLM rewrites (`StaleRefreshService`) or advisory findings. Nothing is recompiled automatically.
- Other notable pieces: step-level resume of the ingest pipeline from persisted step outputs; exact-hash duplicate-source skip; delta lint that re-diagnoses only pages changed since the last lint; token accounting and monthly budgets per scope. There is **no LLM-response cache** and no temperature or seed control.

---

## 1. Source ingestion pipeline

**Entry points**
- `POST /api/source/upload` → `SourceService.uploadSource` (`D/wiki/SourceService.java:71-140`). Stores the file and inserts a `source` row with status `uploaded`. The MCP text path is `uploadTextSource` (`:146-206`).
- `POST /api/ingest/start` (`W/controller/IngestController.java:110-126`) → `IngestOrchestrationService.startIngest` (`B/ingest/IngestOrchestrationService.java:51-75`). It refuses only `DEPRECATED` sources (`:56-58`). It does **not** refuse already-`processed` sources, so the same source can be fully re-run.
- `POST /api/ingest/analyze` (`IngestController.java:128-144`) runs Phase 1 only, then waits at `awaiting_confirmation` / `awaiting_review` for a human. `POST /{id}/execute` runs Phase 2.
- Batches: `POST /api/ingest/batch` → `IngestBatchService.createBatch` (`B/ingest/IngestBatchService.java:99-169`). `IngestBatchScheduler` gates them: analysis runs concurrently (default 2), writing runs strictly 1 at a time (`ARCHITECTURE.md:132`).

**Stages** (`H/ingest/IngestOrchestrator.java:120-240`): `UPLOAD → ANALYZE → WRITE → COMPLETE`.

| Stage | Code | What it does | LLM calls |
|---|---|---|---|
| UPLOAD (parse) | `H/ingest/UploadAgent.java:27-52`, `H/ingest/ParserAgent.java:58-312` | Python subprocess parser for pdf/docx/pptx/xlsx (OCR, multimodal and diagram are optional, `ParserAgent.java:84-158`). Writes `parsed/{sourceId}.parsed.md` and `parsed/{sourceId}.index.json` (`:240-241`, `:356-372`). Heuristic chapter and structure analysis, chunking, strategy selection (`:280-311`). | One "parse-quality review" chat over a head/mid/tail sample (`UploadAgent.java:38-42`). OCR, diagram and multimodal LLMs run inside the parser if enabled. |
| ANALYZE | `H/ingest/AnalysisAgent.java:80-91`, `H/ingest/AnalyzerAgent.java:104-185` | Builds a compact "pages context" from `GlobalSummary` (DB-derived category skeleton, `AnalyzerAgent.java:287-290`). Extracts entities, facts, metadata and `affectedPages`. Resolves entity → existing page by **exact case-insensitive title** (`AnalysisAgent.java:226-277`). Builds the info catalog and entity dossiers (heuristic). | Single-pass `analyzeAndExtract` (`AnalyzerAgent.java:111-128`), or parallel per-chunk analysis followed by a merge/extract call (`:141`, `:172`, `:178`). Sub-document parallel analysis (`AnalysisAgent.java:93-178`). |
| WRITE | `H/ingest/WritingAgent.java:16-21` → `WriterOrchestrator.writeWithVerification` → `H/ingest/WriterAgent.java:174-432` | Plan-then-execute. Generates a WritingPlan (LLM), then writes the summary page, entity pages and "related" pages in parallel. Chapter-mode "reference" pages are used for structured docs (`:202-206`, `:608-890`). Afterwards: compliance check, LLM consistency reconciler (which can rewrite pages, `H/ingest/ConsistencyReconciler.java:270`, `:372`), and a quality verifier. | WritingPlan; one summary-page call (`WriterAgent.java:1708`); per-entity create call or `mergeEntityClaims` call (`:2243-2245`); a batched `batchMergeEntityClaims` prefetch for related pages (`:2142-2143`); reference-page summaries; reconciler. |
| COMPLETE | `H/ingest/CompletionAgent.java:93-138` | Cross-ref link generation (async, `H/ingest/IndexerAgent.java:23-28` → `CrossRefDomainService.generateLinksForIngestAsync`), ES re-sync, content-duplicate detection, conflict routing, schema-patch proposals. Sets `source.status='processed'` (`:120`, `:423-428`). | Link evaluation (`H/crossref/CrossRefDomainService.java:540`, `:696`), conflict routing, schema-patch proposal. |

When the run finishes, `globalSummaryService.invalidate(scopeId)` is called (`IngestOrchestrator.java:215`). This is a coarse invalidation of a whole-scope in-memory cache with a 5-minute TTL (`H/GlobalSummaryService.java:34-62`). That summary is DB-derived and involves no LLM.

---

## 2. Source change detection

- **Hash:** SHA-256 over the whole file, computed while the upload streams (`DigestInputStream`) into `raw/.tmp/{uuid}`, then atomically moved to the CAS path (`SourceService.java:79-86`, `:216-236`). Granularity is file only. There are no chunk or section hashes.
- **Storage:** column `source.content_hash` (`MIG/V27__add_source_content_hash.sql:1-3`). The comment calls it "for idempotent dedup". There is an index on `(scope_id, content_hash, status)`.
- **Identity:** each upload **inserts a new `source` row** (`SourceService.java:88-99`). No code path updates `file_path`, `content_hash` or `file_modified_at` on an existing source (grep: the only writers are the two `insert`s and the lifecycle `update`s at `:427-436` and `:460-469`). There is no notion of "the same logical document, new version": no parent or version column and no path-based identity (the CAS path is the hash). "Modified" is indistinguishable from "new".
- **New / modified / unchanged decision:**
  - *Unchanged* (same bytes): detected only as a **duplicate**. `findDuplicateSource` matches `content_hash` among `status='processed'` sources in the same scope (`SourceService.java:257-267`). On upload this only produces a warning (`DuplicateInfo`, `:102-134`). In batch ingest the duplicate is skipped unless `forceReingest=true` (`IngestBatchService.java:131-139`). Single `/api/ingest/start` only logs the duplicate (`IngestController.java:119`, `:607-608`).
  - *Modified*: not detected. It becomes a new source and a fresh ingest, which merges additively into existing pages.
- **mtime:** `source.file_modified_at` is set once from the CAS file's storage mtime at upload (`SourceService.java:98`; `MIG/V25__add_source_file_modified_at.sql`). The V25 comment says re-uploads would refresh it, but no code ever updates it. It is used only by the stale lint (§10).
- Parse output is keyed by `sourceId`, not by hash (`parsed/{sourceId}.parsed.md`, `ParserAgent.java:240`). Binary formats are re-parsed on every run (`:70-79`), even when the bytes are an exact duplicate.

---

## 3. How derived artifacts are associated with sources

- **DB join table `wiki_page_source`** (`MIG/V2__init_schema.sql:73-79`): `(id, page_id, source_id)` with FK `ON DELETE CASCADE`. V11 added `scope_id` and `created_at` (`MIG/V11__add_scope_id_to_relation_tables.sql:14-16`). V63 added `execution_id` (`MIG/V63__add_execution_id_to_wiki_page_source.sql:1-2`). Explicit indexes exist on `page_id`, `scope_id` and `execution_id` (`V2:141`, `V11:43`, `V63:2`). `source_id` has no explicit index, but InnoDB creates one automatically for its FK, so reverse lookup is not unindexed.
- Writes go through `WriterAgent.persistSourceRelation` (`H/ingest/WriterAgent.java:2409-2429`). It upserts one row per (page, source) and overwrites `execution_id` with the latest run. After writing, a compensating pass adds rows for every page in the run context (`verifySourceRelations`, `:1404-1444`).
- `wiki_page.source_count` is a counter that is **incremented on every write**, even when the same source is re-ingested or a run is resumed. Examples: summary page `:1778`, entity page `:2302`. It therefore drifts away from the true number of rows in `wiki_page_source`. Cancellation cleanup trusts this counter (`B/ingest/IngestService.java:332`).
- **Claim-level, in text:** entity pages are bullet lists whose lines end with `（来源：A；B）`. These are parsed by `EntityPageEntryParser` (`H/ingest/EntityPageEntryParser.java:10-16`, `:59-64`). The annotation holds the **source display name or document title**, not an id or hash (`WriterAgent.java:2079-2087` builds it from the metadata title or file name). The batch quality gate measures the share of bullets that carry the annotation (`low_source_coverage`, `B/lint/BatchQualityGateListener.java:49-56`).
- Page↔page links: `wiki_page_link` (from, to, type, `created_by`, confidence, `execution_id`; `MIG/V31__enhance_wiki_page_link.sql`). These are semantic cross-refs, not derivation edges.
- There are no frontmatter `sources:` lists. The DB is the source of truth, and pages live as markdown files under `wiki/pages/…`.
- Synthetic sources: user "modify page" instructions and page merges create pseudo-`source` rows with `file_path=""`, `format=USER_FEEDBACK|USER_MERGE` (`B/ingest/PageModifyService.java:45-54`, `B/ingest/MergeService.java:113-122`), and feed them through the same pipeline. Lineage from a merged page back to the raw source therefore goes through a synthetic source. `ConflictDomainService`/`ConflictReviewService` re-point `wiki_page_source.page_id` on merges (`H/conflict/ConflictDomainService.java:582-598`).

---

## 4. Explicit dependency graph / reverse traversal?

- **One hop, yes.** `wiki_page_source` is queried in reverse (source → pages) in several places:
  - `WriterAgent.findSourceRelatedPagePaths` (`:2609-2628`): on re-ingest of the **same source id**, pages already linked to that source are forced into `affectedPages` with action `更新` ("update") (`:2630-2651`, used at `:233-234`, `:328-329`).
  - Cancellation cleanup (`IngestService.java:319-382`).
  - Batch "deprecate outputs" by `execution_id` (`IngestBatchService.java:515-565`).
  - Batch-settled quality scan (`B/ingest/IngestBatchScheduler.java:402-428`).
  - Deprecated-source lint SQL (`DAL/mapper/WikiPageMapper.java:202-213`).
  - Undeprecate auto-resolution (`H/LintFindingService.java:1303-1342`).
- **Not a graph.** There are no claim nodes, no entity nodes separate from pages, and no summary→entity or page→page *derivation* edges. Nothing traverses transitively. For example, if a summary page was written using entity pages, or a merged page came from pages that came from source S, there is no path back to S other than via synthetic sources. The `GlobalSummary` used as analyzer context is invalidated wholesale, not per dependency.

---

## 5. Behaviour on source "update": selective or full?

There is no update operation, so the realistic scenarios are:

1. **New version uploaded as a new file** → new source id → **full ingest** of that file. The ANALYZE LLM chooses which existing pages to touch, from the global category summary, and by exact title match for entities (`AnalysisAgent.java:243-255`). The summary page path is **derived from the LLM-extracted title** (`WriterAgent.java:1718`, `:2488-2504`). If the title matches the old version's, the old summary page is **overwritten** and both sources stay linked (`:1755-1783`). Entity pages receive **additive** claim merges: new claims are appended, duplicates gain an extra source tag, and conflicts are inserted next to the old claim and raise a conflict finding (`H/ingest/EntityPageIncrementalApplier.java:21-107`, `WriterAgent.java:2264-2295`). **Old claims from the previous version are never removed.** No set of artifacts that "depend on the old version" is computed.
2. **Same source re-ingested** (`forceReingest`, or `/ingest/start` on a processed source) → a full pipeline rerun: re-parse, re-analyze, rewrite the summary, plus forced updates to pages previously linked to this source id (`:2609-2651`). Still additive. Exact duplicate claims are skipped (`EntityPageIncrementalApplier.java:73-88`), and `source_count` is incremented again.
3. **Lint "stale" repair** (`B/lint/LintService.java:303-334`, `:336-376`, `:386-432` → `H/StaleRefreshService.java:60-122`). This is selective at the **page** level. For a page flagged stale, it reads the whole page and the **first** linked source's parsed text (`LintService.java:318`, `sourceIds.get(0)`, which is not necessarily the newest source), makes one LLM `mergeIntoExistingPage` call (`StaleRefreshService.java:88-98`), snapshots the old file and overwrites it. It requires human approval or an explicit trigger. It does not propagate to dependents.
4. `IngestService.triggerAsyncIngest` (`B/ingest/IngestService.java:392-403`) is labelled "stale repair Ingest" and simply reruns the full pipeline for a source. No callers were found besides its declaration.

**Granularity:** an entity-page update is "claim-aware" in that the LLM returns candidate entries with a `relation` pointing to existing entry indices (`H/ingest/EntityCandidateParser.java:39-52`). The apply step is a deterministic line patch (`EntityPageIncrementalApplier`). This is claim-level *merge*, not claim-level *invalidation*. Summary and reference pages are regenerated wholesale. Related pages accept updates only if `page_type='entity'` (`WriterAgent.java:2029-2033`).

The concurrency guard is a per-page write lock (`H/PageWriteLockRegistry`, used at `WriterAgent.java:2192`), plus an optimistic content-equality check that discards prefetched LLM candidates if the page changed in the meantime (`:2232-2237`).

---

## 6. Source deletion handling

- **Deletion is forbidden.** `SourceService.deleteSource` always throws `SOURCE_DELETE_FORBIDDEN` (`D/wiki/SourceService.java:481-483`). `ARCHITECTURE.md:83` says the same: raw file and `wiki_page_source` rows are kept permanently.
- **Deprecation** (`SourceService.java:398-446`): sets `lifecycle_status='DEPRECATED'` plus category (OUTDATED / SUPERSEDED / ERRONEOUS / OTHER) and reason. It is blocked while an ingest for the source is in flight (`:416-424`). **No derived artifact is touched**: no page is marked stale, no claim is removed, no rebuild happens. Deprecated sources cannot be ingested again (`IngestOrchestrationService.java:56-58`; `IngestBatchService.java:127-129`).
- **Lazy detection:** the next lint run executes `selectDeprecatedSourcePageRows` (`DAL/mapper/WikiPageMapper.java:202-213`). It finds ACTIVE pages with a `wiki_page_source` row pointing at a DEPRECATED source and emits a `deprecated_source` finding per page. The finding is high priority for reference pages and medium otherwise, and says "suggest recompiling this page to strip deprecated content, or close after confirming no impact" (`H/LintProbeService.java:408-459`, text at `:445`). There is **no automated repair handler** for `deprecated_source`. `approveFinding` only special-cases `conflict` and `stale`; anything else just reverts to `open` (`LintService.java:386-432`).
- **Undeprecate** auto-resolves those findings (`H/LintFindingService.java:1303-1342`).
- Query time filters **deprecated pages**, not deprecated sources (`H/RetrievalService.java:173`, `:251-253`). Claims that came from a deprecated source keep being served from ACTIVE entity pages.
- Batch rollback: `deprecateBatchOutputs` (`IngestBatchService.java:515-565`) marks **every page** with a `wiki_page_source.execution_id` in the batch as DEPRECATED. That includes pre-existing multi-source entity pages that were merely appended to. The notification says "only marked deprecated, files retained" (`:572-575`). This is whole-page, not claim-level. A later ingest that touches a deprecated page silently **reactivates** it (`WriterAgent.java:1757-1761`, `:1872-1877`, `:2052-2057`).

---

## 7. Duplicate detection

- **Exact source duplicates:** same SHA-256 in the same scope (`SourceService.java:257-279`). The CAS layer also deduplicates the physical file (`:220-236`). Same content under different file names is caught, because the hash ignores the path. **Renames** are just new uploads, deduplicated by hash. Near-duplicate *sources* (one edited byte) are **not** detected.
- **Page-level near duplicates:** `ContentDuplicateDetector` uses 4-gram Jaccard ≥ 0.5 on stripped markdown, with a length-ratio guard (`H/conflict/ContentDuplicateDetector.java:27-33`). It compares new entity pages against existing pages after each ingest (`:54-70`, called from `CompletionAgent`). A scope-wide `detectAll` does metadata Jaccard screening plus optional LLM screening, then Jaccard confirmation (`:73-114`). A batch-level `duplicate_content` finding is also raised (`BatchQualityGateListener.java:57-63`).
- **Entity dedup:** exact case-insensitive title match (`AnalysisAgent.java:263-277`; `WriterAgent.findPageByTitleIgnoreCase`, `:2506-2515`). Unique key `(scope_id, title)` (`MIG/V50__add_wiki_page_unique_title.sql`). Aliases are captured but not used for page resolution.

---

## 8. Failure recovery, rollback, atomicity, idempotency

- **Raw upload:** two-phase temp file plus `ATOMIC_MOVE` (`SourceService.java:79-86`, `:220-236`; `INT/storage/LocalStorageProvider.java:250-258`). Stale temp files are swept after 24h (`SourceService.java:238-255`).
- **Wiki page writes are not atomic.** `LocalStorageProvider.write` is a plain `Files.write` (`:96-100`). The file is written first (`flushCompiledPage`, `WriterAgent.java:476-500`) and the DB row afterwards, with no transaction: there is no `@Transactional` in `H/ingest/*` or `PipelineOrchestrator`. A crash between the two leaves the file and DB inconsistent.
- **No per-ingest content rollback.** Pages modified by a failed or partial run keep their changes. Individual page writes that fail are logged and skipped. The writer harvests whatever finished within an adaptive timeout and continues (`WriterAgent.java:368-406`, `:434-447`). Per-page degradation is reported as an event and notification, with "page left unchanged" (`:2325-2342`).
- **Cancel cleanup** (`IngestService.java:285-382`): pages with `source_count <= 1` linked to the source are **hard-deleted** (file, tags, keywords, links, ES doc, row). Multi-source pages only lose the relation row, and their **appended content stays**. Because `source_count` is inflated (§3), this heuristic can misfire.
- **Failure:** the execution is marked failed and the source status rolls back `processing → uploaded` (`IngestService.java:226-245`).
- **Resume:** step-level checkpointing. Each step's output JSON goes into `execution_step.output_data`. `resumeIngestAnalysis` / `resumeIngestExecution` rebuild `IngestContext` from it (`IngestOrchestrator.java:467-726`; `H/ingest/IngestContext.java:496-540`). WRITE is re-executed **as a whole** on resume. It is idempotent only to the extent that exact duplicate claims and existing source tags are skipped. The summary is rewritten and `source_count` is incremented again.
- **Crash recovery:** `ExecutionRecoveryService` (`B/harness/ExecutionRecoveryService.java:42-77`) marks zombie executions failed on startup and every 60s (no step for 5 minutes or no heartbeat for 30 minutes). It only reclaims; it does not repair content.
- **Lint repair snapshot:** before a stale-refresh rewrite, the old page is copied to `lint/snapshots/{findingId}.md` and can be restored (`H/LintRepairSnapshotService.java:37-74`; `StaleRefreshService.java:109`). This is the only content-level undo for automated rewrites. The editor has separate edit-step undo.
- **Idempotency keys:** background tasks dedupe on `scopeId + taskType + idempotencyKey` (`ARCHITECTURE.md:220-241`). Post-save processing for user edits uses `wiki_page.content_hash` (SHA-256, `D/wiki/DraftService.java:135-141`) to skip stale or duplicate MQ messages (`B/wiki/PageSavePostService.java:245-260`). The ingest writer **does not maintain** `wiki_page.content_hash`.

---

## 9. Full-rebuild operations

- **Elasticsearch index rebuild only:** `POST /api/wiki/search/rebuild-index` → background task `index_rebuild` → `searchService.rebuildIndex(scopeId)` (`W/controller/WikiController.java:180-190`; `B/harness/task/handler/IndexRebuildTaskHandler.java:32-51`). It re-indexes existing pages and involves no LLM.
- **Lint full scan vs delta:** `POST /api/lint?fullScan=true` (`W/controller/LintController.java:106-129`). The default is a full scan, and scheduled runs exist.
- `rebuildWikiIndex` is deprecated and a no-op (`D/wiki/WikiFileServiceImpl.java:362-372`).
- **No "recompile the wiki from sources" operation exists.** No code path wipes or regenerates pages from the raw layer. Changing the schema bumps `schema_version` on newly written pages but does not trigger re-ingest.

---

## 10. Issues / roadmap

- GitHub: **0 issues** (open and closed) and 1 PR (the 1.1.0 refactor merge, empty description). `gh issue list --search` for incremental/stale/delete/dependency/hash/re-ingest/update/source all return nothing.
- README roadmap: **"Incremental re-compilation when a source is updated or removed"** is listed as a *planned direction* (`README.md:147`; `README_zh.md:147`: "源文档更新/删除时的增量重编译"). Also planned: "Token-cost optimization and lighter models for routine tasks" (`README.md:149`).
- `docs/refactor/REFACTOR-1.1.0.md:146` mentions a planned **LLM record/replay snapshot** with a `(promptVersion, provider, model, params)` fingerprint, but **for tests only**. It is not a production cache. `:134` mentions an explicit `ExecutionPlan` with step dependencies (pipeline steps, not data dependencies).
- `ARCHITECTURE.md:31` claims "增量编译 | Ingest 更新已有页面 | 只重编受影响的部分" ("only recompile the affected parts"). In the code, "affected" means LLM-nominated pages plus pages previously linked to the same source id, not a computed invalidation set.

---

## 11. Other notable items

- **Stale detection** (`DAL/mapper/WikiPageMapper.java:33-49`; `H/LintProbeService.java:322-343`): flags pages where `MAX(source.file_modified_at)` over linked sources is later than `page.content_updated_at`. `content_updated_at` was added in V19 so that metadata-only updates don't count (`MIG/V19…sql:4-14`). My inference, not tested: sources are immutable and `file_modified_at` is set at upload, while a page's `content_updated_at` is set when ingest writes it, so this check should rarely fire. It mostly catches relations added or re-pointed without a content rewrite, such as conflict or merge re-pointing in `ConflictDomainService.java:582-598`. An LLM probe also nominates "stale" pages from the GlobalSummary (`H/prompt/config/LintPrompts.java:73-95`, `:328`). The two are merged and deduplicated (`LintProbeService.java:468-560`). The finding cache treats a finding as valid while its `created_at` is later than the page's `content_updated_at`, and orphan findings for 7 days (`H/LintFindingService.java:1476-1505`).
- **Delta lint:** re-diagnoses only pages with `content_updated_at > lastLintCompletedAt`, new pages, and pages whose `wiki_page_source.created_at > lastLint` (`H/PipelineOrchestrator.java:779-871`; `H/DeltaLintContext.java:12-36`). This is the closest thing to incremental processing, and it applies to diagnostics, not compilation. It is timestamp-based, not hash-based.
- **Cost and tokens:** real provider usage is captured from Spring AI `Usage`, with a chars/2 fallback estimate (`INT/ai/LlmClient.java:267-293`). Usage is recorded through an AOP aspect into a per-scope monthly budget (`W/aspect/TokenUsageAspect.java:42-56`; `H/governance/TokenUsageMonitor.java:36-47`) and a daily table (`MIG/V35`). There is a `budget_exhausted` terminal status and per-step token counts. Cost savers: batched related-page claim prefetch (one call for N pages, `WriterAgent.java:2089-2182`); a `skipMerge` strategy that saves one analysis call; a global LLM concurrency barrier. There is **no caching of LLM responses** keyed by input or content hash.
- **Determinism:** no temperature, seed or top_p configuration anywhere (grep of Java and YAML). The provider defaults apply. Page paths, entity resolution and summary-page identity depend on LLM-extracted titles, so re-running the same source can produce different page sets. Deterministic parts: the claim applier, regex parsers, SQL lint checks, CAS storage.
- **Human gating:** Phase 1 results wait for confirmation (or an auto-confirm policy in "auto" mode, `H/ingest/AutoConfirmPolicy.java`). Schema-compliance violations route to `awaiting_review` (`IngestOrchestrator.java:242-300`).

---

## Capability matrix (vs proposed dependency-aware incremental compiler)

| Capability | LLM-WIKI | Gap vs proposal |
|---|---|---|
| Source change detection | SHA-256 per file, CAS storage, exact-dup skip (`SourceService.java:79-99, 257-267`; `IngestBatchService.java:131-139`) | There is no source identity across versions, so "changed" = new source. No chunk or section hashes. Detects "unchanged" only. |
| Dependency tracking | `wiki_page_source(page_id, source_id, execution_id)` (`V2:73-79`, `V63`) | Page↔source only, one hop. `source_count` drifts. |
| Claim-level lineage | Markdown bullets with `（来源：name）` suffix, regex-parsed (`EntityPageEntryParser.java:15`) | Source *name*, not id or hash. Entity pages only. Never used for removal or invalidation. |
| Incremental recompilation | Additive merge of a new source into existing entity pages (`EntityPageIncrementalApplier.java:21-107`) | No recompile on change. README lists it as planned (`README.md:147`). |
| Source deletion handling | Delete forbidden; deprecate flag; lint flags citing pages (`SourceService.java:398-483`; `LintProbeService.java:408-459`) | No cleanup and no claim stripping. Deprecated-source claims are still served. |
| Stale artifact detection | mtime-vs-content_updated_at SQL, LLM probe, deprecated-source SQL (`WikiPageMapper.java:33-49, 202-213`) | Timestamp heuristic at lint time that rarely fires with immutable sources. Not hash-based. |
| Dependency invalidation (transitive) | None | No derivation graph and no propagation. |
| Selective regeneration | Per-page LLM refresh for approved stale findings (`StaleRefreshService.java:60-122`); same-source re-ingest touches previously linked pages (`WriterAgent.java:2609-2651`) | Whole-page rewrite with `sourceIds.get(0)`. Manual or approval-gated. Not driven by a dependency diff. |
| Failure recovery | Step checkpoint and resume; zombie reaper; atomic raw upload; lint snapshot restore; cancel cleanup | Non-atomic page writes; no transaction; no rollback of partial ingest edits. |
| Deterministic compilation | Deterministic applier, parsers and SQL checks | No temperature or seed; LLM-derived titles drive page identity. |
| Cost optimization | Token accounting and budgets, batched prefetch, concurrency barrier, delta lint | No LLM-response cache, no hash-keyed parse cache, re-ingest reprocesses everything. |

**Verdict: NO.** LLM-WIKI is an additive, LLM-driven "integrate new source into the wiki" pipeline with flat page↔source provenance and lint-time staleness heuristics. Change-driven, hash-based, transitive invalidation and selective recompilation are absent, and the README explicitly lists "incremental re-compilation when a source is updated or removed" as future work.
