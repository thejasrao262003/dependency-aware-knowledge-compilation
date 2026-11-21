# microsoft/llmwiki: competitive audit (dependency-aware incremental compilation)

- Repo: https://github.com/microsoft/llmwiki (shallow clone, HEAD `32da49b`, 2026-11-13)
- Versions: VS Code extension `1.1.0` (`packages/vscode/package.json:5`), `@llmwiki/core` `0.1.2` (`packages/core/package.json:3`)
- Size: about 6k lines of TypeScript in `packages/core/src` (wiki logic + MCP server) and `packages/vscode/src` (UI, file watchers, the one direct LLM call).
- Method: I read every core and extension source file. All behaviour below comes from the implementation; README and ARCHITECTURE claims were checked against the code. Nothing was built or run.

---

## TL;DR

llmwiki is a **Karpathy-style "LLM wiki" maintainer**: markdown files with YAML frontmatter, a hand-maintained `index.md` catalog, and an append-only `log.md`. It works **per source and is additive**. A new file in `raw/` triggers one LLM call that returns a summary plus entity/concept pages. Those pages are created, or appended to if they already exist. The only dependency data is an optional frontmatter list, `sources: ["sources/<slug>-summary.md"]`, on entity/concept pages. It is used for exactly one thing: **deleting derived pages once all of their summary-page sources are gone.**

It has **no content hashing, no change detection for modified sources, no dependency graph, no staleness marking, no selective regeneration, no claim-level lineage, no transitive invalidation, no LLM caching and no token accounting.** When a source file is modified, the wiki silently goes stale: the watcher's `onDidChange` handler only refreshes a tree view. "Incrementally builds" in the README (`README.md:8`) means "accumulates knowledge over time", not incremental recompilation.

---

## 1. Source ingestion pipeline

There are two parallel paths. Only the VS Code path calls an LLM itself. In the MCP path, the calling agent (Copilot, Claude, etc.) does the LLM work by following skill/prompt instructions.

### 1a. Mechanical core ingest (`@llmwiki/core`)
- `ingestSource()` at `packages/core/src/ingest.ts:35-44` wraps `ingestSourceFile` in the wiki lock.
- Stages in `ingestSourceFile` (`ingest.ts:46-197`):
  1. Check that the wiki is initialized (`:58`) and guard against path traversal (`:71-84`).
  2. Read the file as UTF-8 and stat it (`:105-106`). PDFs are read as UTF-8 too, which gives a garbage preview.
  3. Key the source by **basename slug**: `summaryRelPath = sources/${slugify(basename)}-summary.md` (`:107-111`). `slugify` strips the extension and lowercases (`packages/core/src/utils.ts:5-11`).
  4. Duplicate check = "does the summary file exist?" (`:113-134`). If it exists and `force` is not set, return `status: 'skipped'`.
  5. Write a stub summary page: the first 500 chars of the source (`:136-139`) plus frontmatter `{type:'source', title, source_path, ingested, created, tags}` (`:151-161`).
  6. Update the index: `removeEntry` (only when forcing over an existing summary) then `addEntry` (`:164-174`). Append to the log (`:178-182`).
- No LLM call.
- `ingestWithContext()` (`packages/core/src/ingest-context.ts:148-224`) calls `ingestSource`, then runs a **keyword-overlap search** (first 20 non-stopword tokens, `:189-193`) to return `related_pages` and canned `suggested_actions` (`:95-135`). This is what the MCP tool `wiki_ingest_with_context` exposes (`packages/core/src/mcp/write-tools.ts:710-720`).
- `bulkIngest()` (`packages/core/src/bulk-ingest.ts:33-107`) loops over `listSources(raw/)` and skips any file whose summary file exists (`:72-89`). It is exported but **not used by the extension or the MCP server**: grep finds no callers in `packages/*/src`.

### 1b. VS Code LLM ingest (`packages/vscode/src/llmIngest.ts`)
- Entry points:
  - the `raw/**` watcher's `onDidCreate` calls `autoIngest` (`packages/vscode/src/extension.ts:121-126`, `:276-323`);
  - a startup scan and the `llmwiki.scanRaw` command call `scanRawForUningested` (`extension.ts:148-156`, `:165-270`);
  - the `llmwiki.ingest` command, a file/folder picker (`packages/vscode/src/commands.ts:206-316`).
  
  **All three call `llmIngest(..., force=false)`** (`commands.ts:296`, `extension.ts:251`, `extension.ts:305`). The UI has no re-ingest action.
- Stages (`llmIngest.ts:33-157`):
  1. Mechanical `ingestSource` (`:49`). If it returns `skipped`, throw "already ingested" (`:54-56`).
  2. Snapshot the stub summary body for an optimistic-concurrency check (`:60-70`).
  3. `extractText` (pdfjs for .pdf, mammoth for .docx, else UTF-8; `packages/vscode/src/extractText.ts:10-22`).
  4. Build `wikiContext` = **the entire index.md as a bullet list** (`:83-85`).
  5. **One LLM call** (`callLlm`, `:161-228`). The source is truncated to 60,000 chars (`:176-179`). The system prompt asks for JSON `{summary, entities[], concepts[], crosslinks[]}` (`:181-195`). The call is `model.sendRequest(messages, {}, token)` with **no model options** (`:209`). The model comes from VS Code's Copilot LM API (`packages/vscode/src/modelSelection.ts:26-58`).
  6. Parse and validate the JSON shape (`:230-291`).
  7. Rewrite the summary body under the lock, unless the stub was edited meanwhile (`:97-115`, check at `:103`).
  8. For each entity/concept, call `upsertDerivedPage(..., sourcePage=summaryRelPath)` inside its own try/catch (`:122-135`).
  9. Call `addCrosslinks` for each LLM-proposed link (`:138-147`).
  10. Append an `enriched` entry to the log (`:150-154`).
- What the LLM is used for: summarising the source; extracting entities/concepts with their page bodies; proposing crosslinks. It is never used for change impact, re-summarisation of dependents, or merging/reconciliation. New content is simply appended (see 5).

### 1c. MCP/agent path
- The MCP server exposes write tools (`write-tools.ts:48-311`): `wiki_write_page`, `wiki_create_entity`, `wiki_create_concept`, `wiki_update_page`, `wiki_add_crosslinks`, `wiki_update_index`, `wiki_ingest_with_context`, `wiki_fix`, `wiki_delete_page`.
- The agent workflow is prose in `plugin/skills/wiki-ingest/SKILL.md:9-18` and the `ingest-and-integrate` MCP prompt (`packages/core/src/mcp/prompts.ts:47-81`). Recording provenance (`sources` / `addSources`) is **optional and relies on the agent following instructions** (`write-tools.ts:40-45`, `SKILL.md:14-15`).

## 2. Source change detection

- **New vs. already-ingested:** decided only by **whether `wiki/sources/<basename-slug>-summary.md` exists** (`ingest.ts:113-134`, `bulk-ingest.ts:72-89`, `extension.ts:200-214`).
- **Modified:** **not detected at all.**
  - `rawWatcher.onDidChange` only logs and refreshes the tree view (`extension.ts:117-120`).
  - The startup scan only looks for files without a summary (`extension.ts:211-214`).
- **Hashing:** none. A grep for `hash|sha|crypto|checksum|digest|etag` across `packages/` and docs finds only "hashtags" in `index-ops.ts:54`.
- **mtime:** collected by `listSources` (`packages/core/src/sources.ts:42`) but used **only for display** (`packages/vscode/src/rawSourcesTree.ts:52`) and in the `wiki_list_sources` MCP output. It is never compared to anything. The summary's `ingested: <date>` frontmatter (`ingest.ts:156`) is written and **never read back**. `source_path` (`ingest.ts:155`) is also write-only: its only other mentions are in the AGENTS.md template text (`init.ts:72,82`).
- **Granularity:** whole file, keyed by basename slug. There is no chunk or section granularity.
- Concrete staleness scenario: dropping a file whose name already exists in `raw/` overwrites it (`rawSourcesTree.ts:140-141` `copyFile`, and `commands.ts:47-49` `ensureInRaw` `copyFile`; Node's `copyFile` overwrites by default). That fires `onDidChange`, not `onDidCreate`, so nothing is re-ingested and the wiki silently stays stale. Even an explicit Ingest of that file reports "already ingested" (`llmIngest.ts:54-56`).

## 3. How derived artifacts are associated with sources

- Summary page → raw file: frontmatter `source_path: <relative path>` (`ingest.ts:155`). **No code reads it.**
- Entity/concept page → summary page: optional frontmatter `sources: string[]` of wiki-relative summary paths (`packages/core/src/wiki.ts:12-20`).
  - Set on creation by `upsertDerivedPage` (`wiki.ts:226-230`).
  - Appended when an existing page is extended, **but only if the page already has a non-empty `sources` list** (`wiki.ts:237-240`). Hand-written pages never acquire provenance.
  - MCP: `sources` on create (`write-tools.ts:505,518`), `addSources` on update (`write-tools.ts:604-616`). Each entry must be an existing `sources/*.md` file (`write-tools.ts:365-397`).
  - Only `sources/<x>.md` refs count. Raw paths and URLs are ignored for provenance (`packages/core/src/fix.ts:56-64`).
- Summary → entities: nothing is stored on the summary side. There is no forward list.
- Crosslinks are plain markdown links in a "## See also" section (`wiki.ts:273-373`). They are not dependency edges.
- Index (`index.md`) and log (`log.md`) are flat markdown. There is no DB or sidecar manifest.

## 4. Explicit, reverse-traversable dependency graph?

**No.**
- Reverse lookup "summary → pages that depend on it" is done by a **full scan** of every content page's frontmatter (`fix.ts:122-124` in `removeSourcePages`; `fix.ts:93-102` in `findOrphanedDerivedPages`).
- Reverse link lookup ("backlinks") is also a full scan that parses every page's markdown links (`packages/core/src/backlinks.ts:23-54`).
- The effective graph is **one hop**: raw →(by naming convention, not data) summary →(frontmatter) entity/concept. Nothing is **transitive**:
  - query/synthesis pages (`queries/*.md`, written by `wiki-save`) have no `sources` and are never invalidated;
  - crosslinked pages are never re-examined;
  - raw → summary is not checked at all (see 6).

## 5. On a source update: selective regeneration?

- **No regeneration is triggered by an update** (see 2).
- The only way to reprocess is `force: true`. That is reachable only through MCP `wiki_ingest_with_context` (`write-tools.ts:262-266`, `:717-718`), or `bulkIngest({force})` / `ingestSource(..., force)` called programmatically. Agent guidance discourages it ("re-ingest with force only if the user asks", `ingest.ts:131`, `SKILL.md:11-12`).
- What force does:
  - The **summary page is overwritten wholesale** with a new stub (`ingest.ts:151-161`), and its `created` date is reset.
  - In the VS Code path (if force were ever passed) the LLM re-extracts everything, and `upsertDerivedPage` **appends** the new paragraph to an existing entity/concept page unless the identical string is already present (`wiki.ts:242-250`). Because LLM output is non-deterministic, re-ingest accumulates near-duplicate paragraphs.
  - Old content derived from the previous version is **never removed**.
  - Entities that the new version no longer mentions keep their stale content and still list the (still-existing) summary in `sources`, so they are never cleaned up.
- In the MCP path, what changes after a forced ingest is entirely up to the agent.
- **There is no claim/fact-level granularity anywhere.** Entity bodies are opaque markdown blobs, merged by string append.

## 6. Source deletion handling

This is the one place with real dependency logic.
- VS Code `raw/**` `onDidDelete` → `autoCleanup` (`extension.ts:127-132`, `:327-372`) → `removeSourcePages(wikiDir, "sources/<slug>-summary.md")` (`fix.ts:112-135`). Under the wiki lock, it:
  - deletes the summary page;
  - then deletes every content page that lists that summary in `sources` **and** whose listed summary sources are all now missing (`ENOENT` only; `fix.ts:71-86`). Multi-source pages survive while any source remains.
- The `llmwiki.removeSource` command (raw-tree context menu) unlinks the raw file and then calls the same function (`commands.ts:319-360`).
- `fixWiki` / `wiki_fix` / Refresh / Fix Issues: `findOrphanedDerivedPages` deletes entity/concept pages whose `sources` are all missing (`fix.ts:93-102`, `:149-201`). It then runs `lintFix` (stale index entries, unindexed pages, frontmatter defaults). There is a **dry-run preview** that lints the wiki "as if" the deletions had happened (`assumeDeleted`, `fix.ts:172-178`; `packages/core/src/lint.ts:43-50`). MCP prompt and skill require user confirmation before deleting (`prompts.ts:101-104`, `plugin/skills/wiki-fix/SKILL.md:9-11`).
- Deletion is hard delete. There is no "stale" or "tombstone" state. Backlinks to deleted pages become `broken-links` lint errors (`lint.ts:113-128`). `deletePage` returns `backlinkWarnings` but doesn't fix them (`wiki.ts:462-476`).
- Gaps:
  - **A multi-source page that loses one source keeps that source's content and keeps the dangling path in `sources`.** No content is retracted and the frontmatter is not pruned (`fix.ts:125-132` only skips or deletes).
  - **Deletions made while VS Code isn't running are never cleaned up.** Nothing checks summary → raw existence: there is no lint rule for it, `fixWiki` only checks entity → summary, and the startup scan only finds un-ingested files (`extension.ts:165-214`). Because the summary survives, its derived pages are never orphaned either.
  - Deletion is keyed by **basename slug**. Deleting `raw/a/notes.md` removes `sources/notes-summary.md` even if that summary was produced from `raw/b/notes.md`.
  - `removeSourcePages` scans *all* content pages (`fix.ts:122`), but `findOrphanedDerivedPages` scans only `entities/` and `concepts/` (`fix.ts:96`). The two cleanup paths are inconsistent.
  - The MCP path has no automatic raw-deletion hook. The agent must run `wiki_fix`.

## 7. Duplicate detection

- Only the "summary page with the same basename slug exists" check (`ingest.ts:113-134`; added in PR #37, "prevent silent data corruption when re-ingesting").
- **False positives:** `raw/papers/a.pdf` and `raw/notes/a.md` collide (directory ignored, extension stripped), so the second is skipped as a duplicate and never ingested.
- **False negatives:** identical content under two names is ingested twice. There is no content hash and no near-duplicate detection.
- **Renames:** a rename in `raw/` fires delete + create. `autoCleanup` deletes the summary and its sole-source pages, then `autoIngest` (after a 500 ms delay, `extension.ts:294`) does a **full new LLM ingest**. That costs tokens and gives non-deterministic output, and multi-source pages accumulate duplicate content.
- Entity-level dedup: entities with the same slug are merged by appending (`wiki.ts:210`, `:242-250`). Different spellings of one entity give separate pages. There is no entity resolution.
- `renamePage` (`wiki.ts:501-621`) rewrites body links and the index but **not the `sources` frontmatter**. Renaming a summary page would make its derived pages look orphaned to `wiki_fix`. It is currently exported but not wired to any UI command or MCP tool (no callers in `packages/*/src`).

## 8. Failure recovery, atomicity, idempotency

- **Concurrency:** this part is solid. `withWikiLock` (`packages/core/src/lock.ts:38-58`) is a cross-process lock file `wiki/.llmwiki.lock` (`lock.ts:7`), created with `open(..., 'wx')` (`:65`). It is re-entrant via AsyncLocalStorage (`:15`, `:40-41`). A heartbeat `utimes` runs every 2.5 s (`:47-51`), stale locks are broken after 10 s (`:10`, `:79-82`), and acquisition times out after 30 s (`:12`). All read-modify-writes of pages and the index take it. The LLM call is deliberately *outside* the lock, and an optimistic check on the summary body prevents clobbering edits made while the model runs (`llmIngest.ts:60-70`, `:103`).
- **Atomic writes:** none.
  - `writePage` is a plain `writeFile` (`wiki.ts:39-43`), with no temp file + rename and no fsync. `writeIndex` rewrites the whole index file the same way (`packages/core/src/index-ops.ts:128-152`). A crash mid-write can truncate a page or `index.md`.
  - `log.md` uses `appendFile` (`packages/core/src/log.ts:17-26`).
- **Transactions:** none. An ingest is a sequence of independently committed writes (summary → index → log → [LLM] → summary rewrite → N page upserts → crosslinks → log). Each upsert and crosslink is try/caught and logged, and the loop continues (`llmIngest.ts:128-134`, `:140-146`). There is no rollback.
- **Partial-failure trap:** if the LLM is unavailable, errors, is cancelled, or returns unparseable JSON, `callLlm` returns `null` and `llmIngest` returns early (`llmIngest.ts:91-94`, `:168-171`, `:220-227`, `:287-290`). **The stub summary stays on disk, so every later scan treats the source as ingested** (`extension.ts:211-214`). The source is permanently stuck as a 500-char preview with no entities, and there is no retry queue or "failed" marker. The UI only recovers via a forced MCP ingest.
- **Ordering choices:**
  - `deletePage` removes the index entry *before* unlinking the file ("safer to have orphan file than orphan index entry", `wiki.ts:441-442`, `:465-470`); lint-fix re-indexes orphans.
  - `renamePage` writes the new file, rewrites links, updates the index, then deletes the old file (`wiki.ts:561-617`). A crash leaves both.
- **Idempotency:**
  - Non-forced ingest is idempotent (skip).
  - `upsertDerivedPage` is idempotent only for byte-identical content (`wiki.ts:244`).
  - `addCrosslinks` dedups identical link lines (`wiki.ts:350-358`).
  - `upsertEntry` prevents duplicate index rows (`index-ops.ts:173-187`).
  - In-process dedup of watcher events uses a `_pendingIngests` set (`extension.ts:274-290`).
  - MCP tool annotations declare idempotency hints (`write-tools.ts:28-37`).
- No git integration: ARCHITECTURE says "Every wiki change is a git commit" (`ARCHITECTURE.md:600`), but there are no git calls in `packages/*/src`. That is left to the user.

## 9. Full-rebuild operations

- There is **no "rebuild wiki" or "recompile all" operation.**
- The closest things:
  - `bulkIngest({force:true})` (`bulk-ingest.ts:33-107`). It re-runs only the *mechanical* summary stubs, has no LLM step, and is not wired to the UI or MCP.
  - `fixWiki`, which can "rebuild the index" from pages on disk via lint-fix index-completeness (`ARCHITECTURE.md:361`; `lint.ts:372-401`).
- In practice a rebuild means deleting `wiki/` and re-dropping sources, or asking an agent to re-ingest with force. Because the system cannot regenerate selectively, any correction after a source edit is effectively manual or full.

## 10. Issues / roadmap

- `gh issue list --state all`: there are only 4 issues in the repo (#1, #3, #6, #70), all housekeeping. Searches for incremental, stale, re-ingest, reingest, delete, update, dependency, rebuild, hash, duplicate, modified, changed and provenance returned nothing relevant.
- PRs: development runs in planned "cycles" (`plan: cycle-3/4/5`, PRs #35, #46, #58). None of those plans mention change detection, staleness, hashing or dependency tracking. Cycle-5 (#58) covers delete/rename CRUD, MCP resources/prompts, lint auto-fix, scheduled lint and PR-triggered validation.
  - PR #37 added the slug-based duplicate check.
  - PR #124 (v1.1.0) added `sources`/`addSources` provenance, `removeSourcePages` (keep multi-source pages), `upsertDerivedPage` (fixing a data-loss bug where ingest overwrote entity pages and reset `sources`) and the cross-process lock. Its description frames provenance purely as **cleanup on removal**.
- **No open roadmap item addresses modified-source handling, invalidation, or incremental recompilation.** The niche appears unclaimed in this project.

## 11. Other notable points

- **Stale-artifact lint:** the lint categories are broken-links, orphan-pages, index-completeness, `stale-entries` (index rows pointing at missing files), missing-pages and frontmatter-validation (`lint.ts:113-243`). "stale" here means *dangling index entry*, not *content older than its source*. No rule compares a source's mtime or hash with the derived page, and none checks summary → raw existence.
- **Cost/tokens:** no token counting or cost accounting anywhere. `modelSelection.ts:109` only displays `maxInputTokens` in a picker.
  - Each ingest makes one LLM call that includes the **whole index** as context (`llmIngest.ts:83-85`), so prompt size grows linearly with the wiki.
  - Sources over 60k chars are silently truncated (`:176-179`). There is no chunking or map-reduce.
  - No LLM response caching, and nothing is skipped for unchanged content: there is no content identity.
- **Determinism:** low.
  - No temperature or seed (`llmIngest.ts:209`). The model falls back to "any Copilot model" if the configured family is unavailable (`modelSelection.ts:46-51`).
  - Timestamps go into frontmatter: entity pages get a full ISO timestamp (`wiki.ts:117`), summaries a date (`ingest.ts:144`).
  - Merge is by string append, and index order is insertion order.
  - The committed MCP bundle is reproducible, but that is about the build, not the wiki.
- **Status:** `getWikiStatus` derives the last ingest/lint dates by parsing `log.md` (`packages/core/src/status.ts:52-69`). The log is human-readable history, not a machine-usable manifest.
- **Query:** keyword-weighted search (titles 3×, summaries 2×, bodies 1×; `ARCHITECTURE.md:590`). No embeddings.
- **Strengths worth borrowing:**
  - a cross-process lock with a heartbeat;
  - the optimistic-concurrency check around the long-running LLM call;
  - a "provenance must reference an existing summary page" validation (`write-tools.ts:365-397`);
  - deletion that keeps multi-source pages;
  - a dry-run planner that lints the post-deletion state (`assumeDeleted`);
  - destructive-operation confirmations at the MCP layer;
  - an extensive test suite (`tests/shared/fix.test.ts`, `wiki-create-pages.test.ts` including the concurrent-upsert test at `:196`).

---

## Capability matrix vs. proposed engine

| Capability | llmwiki | Evidence | Limitation |
|---|---|---|---|
| Source change detection | New-file only (summary-file existence by basename slug) | `ingest.ts:113-134`; `extension.ts:117-126,211-214` | No hash; mtime displayed but unused (`sources.ts:42`); modifications are ignored |
| Dependency tracking | Optional `sources:` frontmatter on entity/concept pages (one hop to the summary) | `wiki.ts:226-240`; `write-tools.ts:40-45,365-407` | Raw→summary link is a naming convention only (`source_path` never read); no forward edges; agent-dependent in MCP mode |
| Claim-level lineage | None | `llmIngest.ts:18-23` (opaque `content` strings); `wiki.ts:242-250` | Page-level only; merged content has no per-paragraph attribution |
| Incremental recompilation | None | `extension.ts:117-120` | Update means a manual `force` re-ingest, which is additive |
| Source deletion handling | Yes: delete summary + sole-source derived pages; keep multi-source pages | `fix.ts:112-135`; `extension.ts:127-132,327-372`; `commands.ts:319-360` | Hard delete; residual content stays on multi-source pages; offline deletions missed; slug collisions |
| Stale artifact detection | Lint `stale-entries` = index rows for missing files; `findOrphanedDerivedPages` | `lint.ts:158-172`; `fix.ts:93-102` | No content-staleness concept |
| Dependency invalidation (transitive) | None | `fix.ts:93-135` (single hop) | Query pages and crosslinked pages are never invalidated |
| Selective regeneration | None | `llmIngest.ts:122-135` (append-only upsert) | Cannot regenerate only affected pages or claims |
| Failure recovery | Cross-process lock; per-item try/catch; optimistic check | `lock.ts:38-116`; `llmIngest.ts:103,128-146` | Non-atomic `writeFile`; no rollback; failed LLM leaves the source permanently "ingested" as a stub |
| Deterministic compilation | No | `llmIngest.ts:209`; `modelSelection.ts:46-51`; `wiki.ts:117` | No temperature/seed, no cache, timestamps in output |
| Cost optimization | Minimal: skip already-ingested files; 60k char truncation | `ingest.ts:123-134`; `llmIngest.ts:176-179` | No token accounting or caching; whole index sent every call |

**Verdict:** NO. llmwiki is an additive, per-source LLM wiki builder. Its only provenance mechanism is one-hop `sources:` frontmatter used to garbage-collect pages after a source is deleted. It has no content hashing, no change detection for modified sources, no dependency graph, no transitive invalidation and no selective regeneration.
