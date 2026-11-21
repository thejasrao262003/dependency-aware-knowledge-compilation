# nashsu/llm_wiki: incremental and dependency audit

- Repo: `nashsu/llm_wiki`, shallow clone at HEAD `e08cfce` ("release: v0.6.13"). Audited 2026-11-21.
- Stack: Tauri desktop app. The TypeScript front end (`src/lib/*`) holds all of the ingest, merge and lifecycle logic. The Rust back end (`src-tauri/src/commands/*`) handles the file watcher, file I/O, document extraction, vector store and API server. There is also an MCP server (`mcp-server/`).
- Method: I only read the code and checked GitHub issues and PRs with `gh`. Nothing was built or run. All paths are relative to the repo root.

---

## TL;DR

llm_wiki is a mature, heavily patched "LLM wiki" desktop app. Its incremental support is coarse-grained:

- **Source-level skip cache.** It stores a SHA-256 of the extracted source text and a list of files the last ingest wrote.
- **File watcher.** A Rust watcher keeps an MD5+size+mtime snapshot and detects created, modified and deleted files. Renames are detected only when the content is unchanged.
- **Delete cascade.** When a source is deleted, every wiki page is scanned and its frontmatter `sources:` list is checked.
- **Merge on write.** When an ingest writes over an existing page, the old and new bodies are combined by an LLM.

There is **no dependency graph and no claim-level lineage**. Nothing traverses invalidations transitively. A modified source always goes through the **full LLM pipeline (analysis → generation → per-page LLM merge)**. Pages from the old version that the new generation no longer produces are **left in place** with their old content and `sources:` entry. Shared pages keep the retracted claims, because the merge prompt tells the model to "preserve every factual claim from both inputs". Downstream pages that link to changed pages (and `overview.md`) are never re-derived. The proposed engine is **not** already implemented here. The parts that do exist (hash skip, watcher, delete cascade by `sources:` scan, resume checkpoints) are useful prior art and show the pain points.

---

## 1. Source ingestion pipeline

**Entry points that enqueue a source for ingest:**

| Entry | Code |
|---|---|
| Import files or folder (UI) | `src/lib/source-lifecycle.ts:315-374` (`importSourceFiles`), `:376-446` (`importSourceFolder`). Both copy into `raw/sources/` and then call `enqueueSourceIngest` (`:258-286`) |
| Re-ingest button (Sources view) | `src/components/sources/sources-view.tsx:356-373`. It goes through the same queue path, so the cache still applies and there is no "force" option |
| Watcher-detected create or modify | `src/lib/project-file-sync.ts:337-381` (`enqueueRawSourceChanges`) |
| Scheduled import (mirror an external folder) | `src/lib/scheduled-import.ts:447-639` |
| Interactive chat ingest (older path) | `src/lib/ingest.ts:3629` (`startIngest`) and `:3719` (`executeIngestWrites`) |

**Queue:** `src/lib/ingest-queue.ts`.
- Each task has the fields `{id, projectId, sourcePath, folderContext, status, retryCount}` (`:12-24`).
- The queue is persisted to `.llm-wiki/ingest-queue.json` on a best-effort basis (`:121-158`).
- Tasks for the same path are deduplicated (`upsertQueuedIngestTask`, `:206-259`).
- There is a worker pool (`processNext`, `:1239-1286`).
- Commits are serialised in task-start order by `IngestCommitCoordinator` (`src/lib/ingest-commit-coordinator.ts:8-41`) plus a per-project mutex (`src/lib/ingest.ts:724-726`).

**Stages in `autoIngestImpl`** (`src/lib/ingest.ts:707-1597`):

0. **MinerU PDF parse (optional).** `:736-777`. The output is cached as `raw/sources/.cache/<file>.txt`.
1. **Read the source text and the global context:** `schema.md`, `purpose.md`, `wiki/index.md`, `wiki/overview.md` (`:779-785`). Extraction itself happens in Rust; its cache is fresh if the cache mtime ≥ the source mtime (`src-tauri/src/commands/fs.rs:186-203`).
2. **Ingest cache check.** `:816-908`. On a hit, only the image extraction and caption pass is redone, then the function returns.
3. **Image extraction (`:910-942`) and vision captioning (`:944-1034`).** Captions are cached by image SHA-256.
4. **Long-source chunked analysis** when the text exceeds the context budget. `:1036-1062` → `analyzeLongSourceInChunks` (`:3240-3377`). This makes sequential LLM calls per semantic chunk with a rolling "global digest".
5. **LLM call #1, analysis** (`:1064-1109`, prompt `buildAnalysisPrompt` `:2434`). It extracts entities, concepts, claims, connections and contradictions as prose. Temperature 0.1.
6. **LLM call #2, generation** (`:1111-1208`, prompt `buildGenerationPrompt` `:2498`). It emits `---FILE: path---…---END FILE---` blocks, parsed by `parseFileBlocks` (`:504`). It may resume from a checkpoint.
7. **LLM call #3, optional review-suggestion stage** (`:1210-1255`).
8. **Commit, under the ordered commit lock** (`:1258-1596`):
   - `writeFileBlocks` (`:2095-2315`). For each block it sanitises the content, stamps dates, canonicalises `sources:`, applies schema routing, runs a language guard, and then **merges with the existing page through LLM call #4 per pre-existing page** (`:2250-2290`).
   - **LLM call #5, truncated-block repair** if needed (`:1286-1385`).
   - Deterministic index/overview "recent" sections (`:1387-1400`, `:1700-1774`) and a deterministic `log.md` append (`:1406-1418`).
   - A fallback source summary if the model omitted one (`:1443-1453`).
   - Image injection into the summary (`:1460-1468`).
   - A completeness gate that throws on partial output (`:1505-1526`).
   - Review items (`:1532-1538`).
   - **Save the ingest cache** (`:1543-1557`).
   - **Re-embed every written page** (`:1560-1580`).
9. **After the queue drains:** an LLM "sweep resolved reviews" pass (`src/lib/ingest-queue.ts:1056-1077`).

**What the LLM is called for:** image captions, chunk analysis, source analysis, page generation, review suggestions, truncated-file repair, a **per-page body merge** for each existing page touched, and the post-queue review sweep. Separately, users can run duplicate-entity detection and merge, and semantic lint.

---

## 2. Source change detection

There are two independent layers, and both work at **whole-file** granularity.

### (a) Rust file watcher and snapshot (`src-tauri/src/commands/file_sync.rs`)

- **Snapshot.** Stored in `.llm-wiki/file-snapshot.json` (`:20`). Each file has a `FileMeta {hash: Option<String>, size, mtime_ms}` (`:53-59`).
- **Hash.** MD5 of the raw bytes, streamed in 64 KiB reads (`md5_file`, `:1085-1100`). It is computed only when size ≤ 32 MiB (`MAX_HASH_BYTES`, `:24`; `read_meta`, `:1050-1054`). Larger files have `hash: None`, so they rely on size alone. The comparison at `:784` is `(hash, size)`, so two `None` hashes with equal size count as unchanged even if the mtime differs.
- **Fast prefilter.** A file becomes a candidate when (size, mtime) differs from the snapshot (`:722-727`, `:744-747`). The decision is then confirmed with (hash, size) (`enqueue_paths`, `:771-808`).
- **Events.** It emits `Created`, `Modified` and `Deleted` tasks with `hash_before` and `hash_after` (`:69-104`, `:848-865`).
- **Coverage.** It watches `raw/sources` and `wiki` (`:298-308`). Periodic rescans run every 10 s on Linux and 60 s elsewhere (`:32-33`).
- **Snapshot advance.** The snapshot moves forward and the task is marked **Done as soon as the metadata is recorded** (`:944-951`). This happens before the TypeScript side has finished, or even started, the ingest.

### (b) Ingest cache (`src/lib/ingest-cache.ts`)

- **Key.** The source identity, which is the path relative to `raw/sources/` (`src/lib/source-identity.ts:11-27`). It is not keyed by content.
- **Value.** `{hash, timestamp, filesWritten[]}` (`:10-18`).
- **Hash.** SHA-256 of the **extracted text** (`sourceContent`), not of the raw bytes (`:20-26`, `:76`; called at `src/lib/ingest.ts:816`). A byte-level change that doesn't change the extracted text is therefore still a cache hit.
- **Hit condition.** The hash must match and **every file in `filesWritten` must still exist** (`:67-99`). If any output was deleted, the whole source is re-ingested.
- **What the key leaves out.** It does **not** include the model, prompt version, `schema.md`, `purpose.md`, output language or wiki state. Changing any of these does not invalidate anything.

### Other hashes and fingerprints (resume only, not change detection)

- **Generation checkpoint fingerprint.** 64-bit FNV-1a over `content\0mtime\0size` (`src/lib/ingest.ts:3030-3041`, `:3059-3065`).
- **Long-source chunk checkpoint.** Keyed by an FNV hash of the whole text plus the chunk shape (`:3110-3134`, `:3263-3264`).
- **Scheduled import.** Per-file MD5 with a (size, mtime) stat fast path (`src/lib/scheduled-import.ts:518-543`).

**There is no chunk-level or section-level change detection.** Chunk checkpoints are keyed by the hash of the whole source. Chunk *k*'s analysis depends on the rolling digest of chunks 1..k-1 (`:3286-3352`). So a one-line edit invalidates every chunk.

---

## 3. Associating derived artifacts with sources

**Only YAML frontmatter `sources: [...]` on each wiki page.** No database table or sidecar index holds this information.

- **What the model is told.** The generation prompt orders it to include the source filename in `sources` (`src/lib/ingest.ts:2520`, `:2568`).
- **Canonicalisation.** `canonicalizeSourcesField` (`:1835-1863`) **always appends the current source identity** to every non-log, non-listing page the ingest writes (`:1850-1852`). Any page *touched* by source X is therefore tagged X, whether or not X actually supports its content.
- **Merge.** On merge, `sources`, `tags` and `related` are **union-merged** (`src/lib/page-merge.ts:31`, `:110-114`, `:203`). Entries are never removed on re-ingest.
- **Other per-source records:**
  - `ingest-cache.json` holds `filesWritten` (a forward list from source to outputs). It is used only as a cache-validity check, never for invalidation or cleanup.
  - The source summary page at `wiki/sources/<slug>.md` and the media folder `wiki/media/<slug>/` are tied to the source by naming convention (`src/lib/source-identity.ts:42-90`).
  - Review items carry a `sourcePath` (`src/lib/ingest.ts:1533-1534`).
- **Page-to-page edges.** These are `[[wikilinks]]` and `related:` entries, used for the graph view and retrieval. Shared `sources` add a relevance boost (`src/lib/graph-relevance.ts:35`, `:321-326`), but they never drive invalidation.

---

## 4. Explicit dependency graph or reverse traversal?

**No.**

- **Reverse lookup.** "What depends on source S" is answered by a **full scan of every `wiki/**/*.md`**: read each file and parse its frontmatter `sources` (`src/lib/source-lifecycle.ts:517-556` for delete; `:149-182` for move).
- **Wikilink cleanup.** References to deleted pages are found by the same kind of full scan (`src/lib/wiki-page-delete.ts:240-280`; `source-lifecycle.ts:629-662`).
- **Depth.** Every traversal is **one hop**: source → pages that list it. Pages that link to those pages, or summarise them (`overview.md`, `index.md`, synthesis or query pages), are never re-derived. Only their dangling wikilinks are de-linked.
- **No stored graph.** Nothing stores claim → page, entity → page or page → summary edges, and nothing traverses dependencies transitively.

---

## 5. Behaviour when a source is updated

**The whole source is re-ingested.** It is never "only the invalidated artifacts".

- **Detection.** The watcher sees `Modified` → `enqueueSourceIngest` → `autoIngest`. The cache misses because the hash differs, so the **full pipeline reruns**: all chunk analyses (the old chunk checkpoint has a different hash path), analysis, generation and review (`src/lib/ingest.ts:1036-1255`).
- **Per page the new generation emits:**
  - **Page owned only by this source** (`isOwnedOnlyBySource`, `:2317-2324`) → `replaceExistingBody` (`:2271-2273`, `src/lib/page-merge.ts:121-135`). The body is replaced wholesale. Locked fields and the union arrays are kept, and a backup goes to `.llm-wiki/page-history/` (`src/lib/ingest.ts:3489-3498`). This was added in v0.6.3 in response to issue #514, where corrected wording kept merging back in.
  - **Shared page** (other sources in `sources:`) → an LLM merge of the old and new bodies (`src/lib/ingest.ts:3385-3452`). The system prompt says **"Preserves every factual claim from both inputs (do not drop content)"** (`:3461`). Retracted or changed claims from the old version of *this* source therefore **survive** on shared pages. Nothing can tell which sentences came from which source.
  - **Fallbacks:** if the merge output is >30% shorter or has no frontmatter, it falls back to "incoming body + array union" with a backup (`src/lib/page-merge.ts:54`, `:150-180`).
- **Pages the old version produced but the new version does not:** **nothing happens**. No code compares the old `filesWritten` with the new one, and `saveIngestCache` simply overwrites the entry (`src/lib/ingest-cache.ts:104-119`). Those pages keep their old content and `sources: [S]`. They are silent stale orphans.
- **Downstream pages** (other pages that cite or link to changed pages, and `overview.md`) are not revisited. The overview update only adds a bounded "recent" bullet list (`src/lib/ingest.ts:1737-1799`).
- **No claim-level granularity.** Claims exist only as prose inside the analysis and page bodies. No claim IDs, spans or claim → source mapping exist. The only "provenance" given to the merge LLM is the comma-joined `sources` list (`:3395-3412`).
- **Side effects of the coarse approach (open issue #837, v0.6.13):**
  - Re-ingesting an *unchanged* source, for example when a cache miss is caused by one missing output file, replaces single-source pages with fresh and often shorter generations. The issue reports 26–39% body loss on 5 pages.
  - The bounded index (200 recent entries, 32k chars, `:66`, `:1819`) hides most existing pages from the model, which led to 9 near-duplicate pages.
  - Open PR #838 proposes "replace only if the recorded source hash differs". Open PR #839 proposes showing the model the pages already citing this source.

---

## 6. Handling source deletion

`deleteSourceFiles` (`src/lib/source-lifecycle.ts:460-578`) is triggered by:
- UI delete,
- watcher `Deleted` events (`src/lib/project-file-sync.ts:389-424`),
- scheduled-import removal or exclusion (`src/lib/scheduled-import.ts:330-361`, added for issue #642 in v0.6.9).

What it does:
- **Cleans up the source's own records.** It removes the parsed-markdown copy, the extraction cache files and the ingest-cache entry, under both the identity key and the basename key (`:492-511`).
  - Minor bug: it deletes `raw/sources/.cache/<basename>.txt` (`:495`), but the Rust extractor stores its cache in `<parent>/.cache/` (`src-tauri/src/commands/fs.rs:163-167`). Caches of nested sources are therefore left behind, and the call could remove the cache of a different top-level file with the same basename.
- **Scans all wiki pages.**
  - If every entry in `sources:` was deleted, the page is **deleted** (`:546-547`).
  - Otherwise it **rewrites `sources:` to the surviving entries** (`:548-555`). The **body is kept unchanged**, so content derived from the deleted source stays in shared pages with no attribution left.
- **Cascades page deletes** (`src/lib/wiki-page-delete.ts:202-280`). It deletes the file, its embeddings and its media directory. It then strips wikilinks to the deleted pages from all other pages (replacing them with plain text), filters `related:`, cleans `index.md` entries, and prunes review items.
- **Appends a log entry** (`:565-569`, `:691-708`).
- **Pure decision helper** `decidePageFate` (`src/lib/source-delete-decision.ts:33-58`). It returns keep, delete or skip.
- **Rename and move.**
  - A delete+create pair in the same batch with **identical MD5, unique on both sides, and size ≥ 32 bytes** is treated as a move (`src/lib/project-file-sync.ts:269-312`).
  - `migrateSourcePath` rewrites `sources:` references, moves the summary page and the cache entry, and rolls back on failure (`src/lib/source-lifecycle.ts:137-233`).
  - A rename *with* edits becomes a delete (cascade) plus a fresh full ingest.

Summary: deletion does real cleanup, but only at page level, one hop deep, with **no body regeneration** for shared pages and no handling for pages that merely *summarised* the deleted pages.

---

## 7. Duplicate detection

- **Same content under a different path:** **none at source level.**
  - The ingest cache is keyed by path (identity), so a copy of a file under a new name is fully re-ingested.
  - Name collisions on import produce `name-YYYYMMDD[-n].ext` (`src/lib/source-lifecycle.ts:665-689`) rather than a content check.
  - The only content-identity logic is move detection (hash pairs within one watcher batch, see §6).
- **Near-duplicate sources:** none.
- **Duplicate entities/concepts (wiki side):** this is a manual Maintenance action, not part of ingest (`src/lib/dedup.ts:1-29`).
  - An optional embedding prefilter uses cosine similarity (`src/lib/dedup_embedding.ts:52`, `:110`).
  - An LLM then groups slugs that look like duplicates (`dedup.ts:211`).
  - An LLM merges the bodies and cross-references are rewritten (`dedup.ts:348`, `:456`).
  - A persisted "not duplicates" whitelist exists (`src/lib/dedup-storage.ts`).
  - The queue lives in `dedup-queue.ts`.
- **Exact slug collisions during ingest** are handled by the page merge (§5).
- **Image duplicates:** captions are cached by image SHA-256 plus language (`src/lib/image-caption-pipeline.ts:18`, `:116`, `:519-545`).

---

## 8. Failure recovery, rollback, atomicity and idempotency

- **Wiki page writes are not atomic.**
  - Ingest uses `writeFile`, which is `fs::write` directly (`src-tauri/src/commands/fs.rs:1243-1267`).
  - A tmp+rename `write_file_atomic` exists (`:1296-1340`) but is used only for some metadata, such as the scheduled-import DB (`src/lib/scheduled-import.ts:421`).
  - The Rust snapshot and queue JSON files are written atomically (`file_sync.rs:1347-1381`).
- **No transaction across the multi-page writes of one source.**
  - Pages are written one by one (`src/lib/ingest.ts:2134-2301`).
  - On partial failure (write errors, truncated blocks, a generation stream error), the files already written **stay on disk**. A **generation checkpoint** records `completedPaths` (`:1483-1503`; path `.llm-wiki/ingest-generation/<slug>-<fp>.json`, `:3051-3057`). The ingest throws `Ingest incomplete` (`:1505-1526`) and the cache is **not** saved (`:1543-1557`).
  - On retry, the model is told to skip the completed paths (`:1145-1154`).
- **Long-source analysis.** It checkpoints after every chunk and resumes from the last completed one (`:3274-3283`, `:3344-3351`).
- **Retries.** The queue retries up to 3 times (`src/lib/ingest-queue.ts:1030`, `:1166-1174`). Errors that look like rate or usage limits pause the queue and auto-resume after 15 min (`:1146-1164`, `:1031`). Tasks are restored after restart (`restoreQueue`, `:955`).
- **Cancellation.** It deliberately **keeps** pages already written, since they may have been merged into pre-existing pages and "deleting them without snapshots would destroy user data" (`:1107-1113`, `:261-272`).
- **Backups:**
  - `.llm-wiki/page-history/<path>-<ts>` snapshots are taken on replace and merge fallback (`src/lib/ingest.ts:3489-3498`; `page-merge.ts:122`, `:154`, `:165`, `:178`).
  - An optional per-file version history (`file_history.rs`) records before and after every `write_file` (`fs.rs:1253-1260`). It is **disabled by default** (`file_history.rs:42-48`) and limited to 10 versions per file, 512 KiB, 128 MiB total (`:9-13`).
  - It allows per-file restore only, not a whole-ingest rollback.
- **Move migration** has a real rollback (`src/lib/source-lifecycle.ts:194-232`).
- **Idempotency:**
  - An unchanged source is a cache hit and makes no LLM calls; the image pass is idempotent (`:806-815`).
  - A re-ingest without a hit is **not idempotent**: it regenerates, then replaces or LLM-merges (issue #837).
  - Review items are deferred until after the completeness gate to avoid duplicates on retry (`:1528-1538`).
- **Crash-window gap.** The Rust file-change task is marked Done once the snapshot is updated (`file_sync.rs:944-951`), and the TypeScript ingest queue is saved best-effort (`ingest-queue.ts:148-150`). If enqueueing fails for the active project, nothing re-invalidates the snapshot. That re-invalidation exists only for inactive projects (`src/lib/project-file-sync.ts:373-378`). A crash between the two steps could lose a change. The Sources view does show un-ingested sources via `listIngestedSourceIdentities` (`src/components/sources/sources-view.tsx:112`).

---

## 9. Full-rebuild operations

- **Rebuild index.** Deterministic, with no LLM: it walks the wiki, groups pages by frontmatter `type`, and writes `index.md` atomically (`src-tauri/src/commands/project_maintenance.rs:288-355`; UI in `src/components/settings/sections/maintenance-section.tsx:197-201`).
- **Re-embed all pages** (`src/lib/embedding.ts:578`, `embedAllPages`). It is needed after changing the embedding model or dimension.
- **Project archive export/import** (`project_maintenance.rs:123-275`). It includes the hidden `.llm-wiki` state.
- **There is no "recompile the wiki from sources" command and no force-reingest flag.** To rebuild knowledge you have to delete the generated pages, the cache or the sources and re-ingest. Changing the model, prompt or schema never triggers a rebuild, because the cache key is the text hash only.

---

## 10. Relevant issues and roadmap (GitHub, checked 2026-11-21)

| # | State | Relevance |
|---|---|---|
| #837 | OPEN (v0.6.13) | "Re-ingest shortens single-source pages and creates duplicates". Explains why the coarse re-ingest damages pages (body replacement on cache miss even when the source is unchanged; a bounded index hides pages → duplicates). |
| PR #838 | OPEN | "merge instead of replacing pages when the source is unchanged". Adds `hasSourceChanged` (cache-hash comparison). This is still per source, not per artifact. |
| PR #839 | OPEN | Shows the model the pages already citing this source and matching pages from other sources (reuse paths, fewer duplicates). |
| PR #846 | OPEN | Generate long sources section by section (depends on #838/#839). Generation per section, still not incremental. |
| PR #813 | OPEN | Scheduled import: reuse the content hash for unchanged files. |
| PR #516 | OPEN | "Ingest prematch": LLM-filter the index and overview down to relevant entries. |
| #533 | OPEN | Asks for a project-level ingest contract with pre-write validation, staging and quarantine, and source references that must resolve. |
| #514 | CLOSED | "How to correct imported knowledge": old wording mixed with new. Fixed in v0.6.3 by body replacement for single-source pages only. |
| #642 | CLOSED | Exclusions and archive deletions left stale mirrors and orphaned wiki pages. Fixed in v0.6.9 with scheduled-import reconciliation. |
| #139 / #138 | CLOSED | Requested "Delta ingest" and a file watcher. Implemented as the watcher plus the SHA-256 cache. |
| #737 | OPEN | The README says `overview.md` is regenerated on every ingest. In v0.6.11 it was never updated. Current code only appends a bounded "recent" list (`ingest.ts:1737`). |
| #476 | OPEN | Repeated batch ingests gradually lose `log.md`, `overview.md` and entity pages (output truncation). |
| #458 | CLOSED | Fidelity and hallucination concerns. The answer was a "raw sources only" retrieval mode, not provenance tracking. |
| #110, #109 | OPEN | Entity merge and duplicate-detection scaling. |

No issue or PR proposes a dependency graph, claim-level provenance, or selective invalidation of downstream artifacts.

---

## 11. Other notable points

- **Stale-artifact detection or lint.**
  - Structural lint covers orphan pages (no inbound links), broken links and missing outlinks (`src/lib/lint-structural-core.ts:189`).
  - **Semantic lint** sends the **first 500 characters of every page** to an LLM and asks it to flag `contradiction`, `stale` ("information that appears outdated"), `missing-page` and `suggestion` (`src/lib/lint.ts:205-356`, `:279`).
  - Staleness is an LLM guess from content. It is **not** derived from provenance or hashes. Nothing marks a page stale because its source changed.
- **Cost and token accounting.** None found: there is no parsing of usage fields and no cost totals (a grep for `prompt_tokens`, `usage.` and similar finds only `max_completion_tokens` plumbing). The cost-saving measures are:
  - the source-level skip cache,
  - the image caption cache,
  - Anthropic `cache_control: ephemeral` on the system prompt (`src/lib/llm-providers.ts:591-603`, issue #276),
  - resume checkpoints,
  - a bounded index in prompts.
  
  Re-embedding happens for every written page, with no content-hash skip (`src/lib/ingest.ts:1560-1580`).
- **Determinism.**
  - LLM output at temperature 0.1 with no seed.
  - The output depends on mutable global context (the current `index.md`, bounded to 200 recent entries and 32k chars, and `overview.md`), which is read **before** generation and with commits running concurrently (`:779-785` vs `:1258`). The result therefore depends on ingest order and concurrency.
  - Commit order is fixed to task-start order (`ingest-commit-coordinator.ts`).
  - Dates are stamped deterministically (`:2049-2069`). The index and log updates are deterministic (`:1387-1418`).
  - Merge sanity checks (`page-merge.ts:54`, `:158-180`) and post-merge scaffolding stripping (`:298-345`) handle merge quality.
- **LLM call caching.** There is no generic prompt → response cache. The only memoised calls are image captions (by image hash) and resume checkpoints (chunk analyses and completed FILE paths).
- **Architecture risk noted in issue #777:** the core orchestration lives in UI-layer TypeScript with Zustand stores (`useWikiStore`, `useActivityStore`) mixed into `ingest.ts`.

---

## Bottom line for the proposed project

| Proposed capability | llm_wiki status |
|---|---|
| Content hash per source | **Yes**, file-level: SHA-256 of the extracted text plus MD5 of the raw bytes in the watcher |
| Chunk or section-level hashing | No; chunk checkpoints are whole-source keyed and order-dependent |
| source → claim lineage | **No**; claims exist only as prose |
| source → page mapping | Yes, through frontmatter `sources:` (unioned, sticky, auto-appended to any touched page) |
| Reverse traversal | One hop, by scanning all pages; no index |
| Transitive invalidation (page → summary/overview/linked pages) | **No** |
| Selective regeneration | **No**; full re-ingest per changed source, then a per-page LLM merge or replace |
| Removal of artifacts the new version no longer produces | **No** (stale orphans persist) |
| Retraction of claims from shared pages on update or delete | **No** (the merge prompt preserves all claims; on delete only `sources:` is edited) |
| Stale marking | **No** (only an LLM-guessed "stale" lint) |
| Atomic multi-artifact commit or rollback | **No**; resume checkpoints and per-page backups instead |
| Cost accounting | **No** |

Verdict: **PARTIAL overlap at the source-hash and watcher level only. The dependency-aware, fine-grained incremental recompilation engine does not exist.** The open issue #837 and PRs #838/#839 show the maintainers are hitting exactly the problems a dependency-aware compiler would solve, still using per-source heuristics.
