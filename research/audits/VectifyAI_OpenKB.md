# VectifyAI/OpenKB — audit vs. dependency-aware incremental knowledge compiler

- Clone: shallow, HEAD `ff54396` ("feat(workbench): configurable entity types ...", 2026-07-22).
- Method: read-only review of `openkb/` (≈19k LOC Python) plus tests and GitHub issues/PRs. No code was executed.
- All paths below are relative to the repo root.

## TL;DR

OpenKB is a Karpathy-style "LLM wiki" compiler with the pipeline **file → markdown source → summary → concept/entity pages → index.md**. It has three solid pieces: a **file-level SHA-256 skip registry**, **path-keyed stable doc identity** (so an edited file re-ingests in place), and a **journaled, crash-safe transaction around `add`** with snapshot/rollback. There is **no dependency graph, no per-artifact input hashes, no staleness detection, no transitive invalidation and no claim-level lineage**.

Provenance is a page-level `sources: [summaries/<doc>.md]` list in concept/entity frontmatter. Reverse lookup works by scanning every page's frontmatter, one hop deep. When a source changes, its whole per-document pipeline runs again and the result is **merged additively** into existing pages through LLM full-page rewrites. Nothing is subtracted, and pages the new version no longer touches keep the old facts. Deletion happens only through an explicit `openkb remove`. It strips frontmatter and link references but **does not regenerate prose**, so facts from a removed document stay in multi-source pages. Our proposed engine is **not** already built here. OpenKB is a strong baseline for the surrounding infrastructure: registry, identity and transactions.

---

## 1. Source ingestion pipeline

**Entry points**
- CLI `openkb add <file|dir|URL>`: `openkb/cli.py:1065-1154`. A directory is walked recursively and sorted, then each file is added serially (`cli.py:1133-1146`). URLs are fetched into `raw/` first (`cli.py:1115-1126`, `openkb/url_ingest.py`).
- `openkb add --from-pageindex-cloud <doc_id>`: `cli.py:706-820`.
- `openkb watch` (watchdog on `raw/`, 2 s debounce): `cli.py:2407-2433`, `openkb/watcher.py:17-77`.
- REST: `POST /api/v1/add` (`openkb/api.py:243`), which goes through `_add_saved_file` (`openkb/api_helpers.py:381-390`) and then `cli._add_for_api` (`cli.py:650-671`). There is also a server-managed watcher (`openkb/watch_service.py:84-157`).
- Every path converges on `add_single_file`, which runs `_add_single_file_locked` under `kb_ingest_lock` (`cli.py:447-632`).

**Stages** (`cli.py:455-632`)
1. **Convert** (`openkb/converter.py:141-254`). Under the KB lock, the file is hashed (`converter.py:170`) and skipped if the hash is already known (`:171-178`). Otherwise the stable `doc_name` is resolved (`:179-184`), the source is copied to `raw/` (`:189-196`), and PDFs with `page_count >= pageindex_threshold` (default 20) are routed to the long-doc path (`:201-215`). Other files are converted to markdown: `.md` passthrough, pymupdf for PDFs, markitdown for Office/HTML (`:225-244`). The result goes to `wiki/sources/<doc_name>.md` (`:246-247`). Output lands in a per-add staging dir (`cli.py:381-385`, `485-490`).
2. **Mutation transaction** (`cli.py:507-632`, `openkb/add_coordinator.py:91-134`). The live paths are snapshotted, the staged raw/source files are published (`openkb/mutation.py:447-458`), and then the following steps run:
   - **Long doc:** PageIndex indexing (`openkb/indexer.py:183-294`). This makes LLM calls inside the PageIndex library for TOC, node summaries and doc description, and writes `wiki/sources/<doc>.json` plus `wiki/summaries/<doc>.md` (`indexer.py:125-153`). Then `compile_long_doc` runs (`openkb/agent/compiler.py:2291-2372`).
   - **Short doc:** `compile_short_doc` (`compiler.py:2205-2288`).
   - The registry entry is written **last**, as the commit point (`cli.py:582-606`).
3. **Post-commit**: append to `wiki/log.md` (`cli.py:608-609`, `openkb/log.py:9-22`).

**LLM calls per document** (all through LiteLLM, `compiler.py:400-498`)
- Short doc, step 1: a `summary` call with the full text in a single message. The summary is held in memory (`compiler.py:2248-2266`). There is no chunking (issue #73).
- Long doc, step 1: an `overview` call over the PageIndex tree summary (`compiler.py:2350-2351`).
- `concepts-plan`: one JSON call that returns create/update/related lists for both concepts and entities. Its prompt contains a one-line brief of **every** existing concept and entity page (`compiler.py:1626-1651`, prompt at `:90-129`).
- Concurrent page generation, bounded by a semaphore with default concurrency 5:
  - Concept create and update (`compiler.py:1835-1896`).
  - Entity create and update (`:1898-1964`).
  - **Update = full-page rewrite**: the whole existing body is sent as `existing_content`, with the instruction "Rewrite the full page incorporating the new information" (`:161-182`, `:200-217`, `:1863-1896`, `:1928-1964`).
- `summary-rewrite`: rewrites the summary against the whitelist of valid wikilink targets. Short docs only (`compiler.py:2091-2153`).
- Code-only steps, no LLM:
  - Strip "ghost" wikilinks (`:2054-2082`).
  - Write pages (`:2155-2164`).
  - "Related" cross-links (`:2166-2185`).
  - Bidirectional backlinks (`:2171-2190`, helpers `:1233-1317`).
  - Index update (`:2192-2202`, `:1506-1576`).

## 2. Source change detection

- **Mechanism: a SHA-256 hash of the whole raw file**, computed in streaming 64 KiB blocks (`openkb/state.py:120-127`).
- The registry is `.openkb/hashes.json`, a JSON dict `sha256 -> {name, doc_name, type, path, raw_path, source_path, doc_id}` (`state.py:11-114`; metadata written at `cli.py:586-597`).
- Decision logic (`converter.py:170-184`): a **known hash means skip**. An unknown hash means ingest, and the ingest is identified by **path**:
  - `resolve_doc_name` looks up the registry by path key (`converter.py:74-109`, `state.py:38-53`). The key is the KB-relative posix path for files inside the KB, otherwise the absolute path (`converter.py:35-46`).
  - A known path with a new hash keeps the same `doc_name`, so the re-ingest overwrites in place (docstring at `converter.py:83-88`, test `tests/test_converter.py:236-243`).
  - On commit the old hash entry is replaced: `registry.remove_by_doc_name(doc_name)` (`cli.py:598-606`, test `tests/test_add_command.py:342-373`).
- **Granularity: whole file only.** There are no chunk, section or page hashes. Derived artifacts (summaries, concept/entity pages, index) carry **no hash and no record of the input version** that produced them.
- **There is no mtime logic.** mtime is used only for "last compile" display (`cli.py:2676-2691`, `3516-3525`; `api_kbs.py:40-47`).
- **Change detection is push-only.** It needs an explicit `add`, or the watcher's `on_created`/`on_modified` events (`watcher.py:71-77`). No scan compares `raw/` or the original paths against the registry. If a file is modified in place without re-adding, lint does not notice: `find_missing_entries` finds the path in the registry and the artifacts present, so the file is reported as fine (`lint.py:401-414`).
- **Other ingest paths have their own identity rules:**
  - Cloud imports use a synthetic hash `sha256("pageindex-cloud:<doc_id>")`. They are skipped forever once imported, even if the cloud document changes (`cli.py:723-730`).
  - Web uploads with an existing filename are saved as `raw/<stem>-1.<ext>` (`api_helpers.py:194-229`). That is a new path, so an edited re-upload becomes a **new document** instead of an update.
- **Long PDFs:** PageIndex content-hashes its own blobs internally (maintainer comment on PR #43). OpenKB's own long-doc re-add creates a new `doc_id`. The old PageIndex doc is not deleted on re-ingest: `delete_document` is called only by `remove` (`cli.py:1313`) and by the failure cleanup (`indexer.py:289`).

## 3. How derived artifacts are associated with sources

There is no database table or explicit lineage store. Associations are spread across:

- **Registry → per-doc artifacts, by naming convention.** `raw/<doc_name>.*`, `wiki/sources/<doc_name>.md|json`, `wiki/sources/images/<doc_name>/` and `wiki/summaries/<doc_name>.md` are all keyed by `doc_name` (`cli.py:398-428`, `1423-1437`).
- **Summary → source:** a `full_text: sources/<doc>.{md|json}` frontmatter field.
  - Short docs: `compiler.py:950-957`. Long docs: `tree_renderer.py:8-15`.
  - Summaries have **no** `sources:` field, even though `skills/openkb/references/wiki-schema.md:59` documents `sources: [raw/paper.pdf]`. That doc is out of date.
- **Concept/entity → summaries:** the frontmatter list `sources: [summaries/<doc>.md, ...]`. It is written on create (`compiler.py:1036-1043`, `1075-1082`) and prepended on update (`:987-990`, `:1084-1087`, `:1121-1159`).
  - It is also prepended when the page is only "related" cross-linked (`compiler.py:1225-1226`). So `sources:` mixes "content derived from" with "cross-referenced by".
- **Body back-references:**
  - A `## Related Documents` list on concept/entity pages (`compiler.py:1263-1285`).
  - `See also: [[summaries/<doc>]]` lines for related-only links (`:1202-1230`).
  - `## Related Concepts` / `## Entities` sections on summaries (`:1233-1260`).
  - `index.md` entries (`:1506-1576`).
- **Granularity is the whole page.** Once a page has multiple sources, its body is a single LLM-blended rewrite. Nothing records which sentence came from which source.

## 4. Explicit, reverse-traversable dependency graph?

**No.** Reverse lookup (source → dependents) is done by **scanning every page's frontmatter or text** each time:
- `scan_affected_pages` (`compiler.py:1403-1427`) is used for the remove preview.
- `_remove_doc_from_pages` (`compiler.py:1320-1400`) does a substring prefilter and then edits frontmatter and body.

It is **one hop and not transitive**: summary → concept/entity. Nothing models:
- concept → concept or entity → concept dependencies (cross-links are wikilinks only),
- index.md or summaries as dependents of concept pages,
- generated outputs (queries, explorations, skills, decks) as dependents of pages.

`visualize.build_graph` (`openkb/visualize.py:22-70`) builds a **wikilink** graph for display, with per-node `sources` from frontmatter (`:38-45`). It is not used for invalidation.

## 5. What happens on a source update

**The whole per-document pipeline runs again, with additive LLM merging. Nothing is selective and nothing is subtracted.**

- An edited file at a known path gets a new hash, keeps the same `doc_name`, and goes through the full convert → (index) → compile → register sequence (`cli.py:455-632`). Every LLM call in §1 runs again.
- **No cleanup of the previous version's contributions happens before recompiling.** `remove_doc_from_*` is called only by `remove` (`cli.py:1551-1558`) and by the failed-cloud-import cleanup (`cli.py:684-703`). It is never called on re-add or recompile (verified by grep).
- What happens to existing artifacts:
  - **Summary:** fully regenerated and overwritten (`compiler.py:957`).
  - **Concepts/entities the new plan marks "update":** the LLM rewrites the full page from `existing_content`, which still contains the old version's facts, plus the new document. The prompt says "integrate", not "retract" (`compiler.py:161-182`). **Facts removed in v2 are likely to survive.**
  - **Pages created from v1 that the v2 plan ignores:** untouched. They keep `summaries/<doc>.md` in `sources:` and keep v1-derived prose. This produces **silently stale pages**.
  - **`index.md` document description:** never refreshed on re-ingest. The doc entry is inserted only if missing and never replaced (`compiler.py:1538-1543`); concept and entity entries *are* replaced (`:1545-1574`). This is a concrete example of a stale derived artifact.
  - **Long docs:** a new PageIndex doc is created and the old one is orphaned (see §2).
- **Which pages get rewritten is decided by the LLM plan** (`concepts-plan`, `compiler.py:1634-1651`), not by any dependency computation.
- **There is no claim or fact granularity.**
  - Open issue **#222** and closed-unmerged PR **#223** proposed "temporal claims" as a frontmatter JSON array with `id`, `as_of`, `status`, `source_anchor` and `supersedes` fields. It is not in the code.
  - Open issue **#245** proposes an opt-in "append-note" update mode, keyed by source doc so a re-ingest replaces its own note line, plus an `openkb consolidate` command. That would be the first per-source fragment granularity. It is not implemented.
- **`openkb recompile <doc>|--all`** (`cli.py:1860-2056`) re-runs `compile_short_doc`/`compile_long_doc` on the existing `wiki/sources/` and the stored `doc_id`, skipping conversion and indexing. It is also additive. It is **not** wrapped in the mutation transaction, and the user chooses the target, not staleness.

## 6. Source deletion handling

- **Only an explicit `openkb remove <id>`** (`cli.py:1683-1797`) or `POST /api/v1/remove` (`cli.py:1614-1680`, `api.py:459`).
  - Deleting a file from `raw/` or from its original location is not detected. The watcher handles only created/modified events (`watcher.py:71-77`).
  - Historical issues #41 and #25 asked for exactly this. The maintainer confirmed "removing from raw does not clean up the knowledge base", and the outcome was the explicit `remove` command (PR #51).
  - S3 connector proposal #274 / PR #277 (open) explicitly specifies: "deleting an object in the bucket leaves compiled pages and files already in `raw/` in place."
- **What `remove` does.** It is plan-then-execute, with a `--dry-run` preview (`cli.py:1400-1611`):
  - Deletes the summary, source md/json, images dir and raw file.
  - Strips the doc from concept/entity `sources:`, `## Related Documents` and `See also:` lines.
  - Deletes pages whose `sources:` list becomes empty, unless `--keep-empty` is set (`compiler.py:1320-1400`).
  - Removes `index.md` entries for the doc and for deleted pages (`compiler.py:1470-1503`).
  - Runs `lint --fix` scoped to the touched pages only (`cli.py:1560-1565`; the scoping comes from issue #58).
  - Deletes the PageIndex doc (`cli.py:1275-1314`), then the registry entry, which is the commit point (`cli.py:1595`).
- **Limitations**
  - **The prose of multi-source pages is not regenerated.** Facts synthesized from the removed doc stay in concept/entity bodies; only the citation is removed. The removal code makes no LLM call.
  - Other docs' summaries, and unmodified concept pages that wikilink to deleted concept/entity pages, keep **dangling links** until a global `openkb lint --fix`, because the fix is scoped (`cli.py:1560-1565`).
  - It is not journaled. Recovery relies on the steps being idempotent with the registry write last (`cli.py:1524-1531`). A PageIndex failure returns `partial` and keeps the registry entry so the user can retry (`cli.py:1567-1593`).
- **Web UI page delete** (`openkb/page_ops.py:106-166`) removes a concept/entity page, demotes inbound links to plain text and drops the index entry. Summaries cannot be deleted directly (`page_ops.py:35-38`).

## 7. Duplicate detection

- **Exact-content duplicates under different paths:** skipped by the file-hash check and reported as "[SKIP] Already in knowledge base" (`converter.py:171-178`, `cli.py:497-500`). The registry keeps the first path; the duplicate's path is never recorded or backfilled (test `tests/test_converter.py:460-484`).
- **Same stem, different path:** gets a collision-resistant `doc_name` with a deterministic `-<sha256(path)[:8]>` suffix (`converter.py:112-132`, `58-71`; PR #96).
- **Renames:**
  - Unchanged content: the hash is known, so the file is **skipped**, and the registry still points at the old path.
  - Renamed and edited: treated as a **new document**. The old one stays, so duplicate knowledge accumulates because deletions are never detected.
- **Near-duplicates (documents):** none. There is no shingling, MinHash or embedding comparison.
- **Concept/entity duplicates:** handled only by LLM prompt rules: "Prefer update over create", "Do NOT create a concept/entity that overlaps an existing one" (`compiler.py:123-124`). Entities have an `aliases` field (`compiler.py:1053-1081`), but nothing populates it algorithmically. Issues #203 and #247 report duplicate and sprawling entities such as `foo-bar` vs `bar-foo`.

## 8. Failure recovery, rollback, atomicity, idempotency

This is the most mature part of the codebase.

- **Atomic file writes.** `atomic_write_bytes/text/json` use temp file, fsync, `os.replace` and a directory fsync (`openkb/locks.py:223-257`). The golden principle "All wiki writes go through locks.py / mutation.py" is in `docs/golden-principles.md:18-19`.
  - Exceptions: the indexer writes long-doc artifacts with plain `write_text` (`indexer.py:142-152`), and `log.md` is appended in place (`log.py:18-22`).
- **KB lock.** An advisory `portalocker` flock on `.openkb/ingest.lock`, plus an in-process RW lock. It is reentrant per thread, with exclusive and shared modes (`locks.py:128-180`).
- **Journaled transactions for `add` and cloud import**:
  - `snapshot_paths` backs up the touched paths, using hardlink backups for the `concepts/` and `entities/` dirs, and writes `.openkb/journal/<uuid>.json` with status `active` (`mutation.py:308-368`).
  - The body runs, `mark_committed` is written, and post-commit the snapshot is discarded (`add_coordinator.py:91-134`, `mutation.py:166-177`).
  - On exception there is an in-process rollback (`add_coordinator.py:105-121`). Rollback uses an O(touched) inode-diff restore (`mutation.py:203-305`).
  - New PageIndex blobs are tracked after the fact with `track_new` (`mutation.py:179-201`, `cli.py:543-550`).
  - **Crash recovery:** any process taking the exclusive lock first drains active journals and rolls them back (`locks.py:102-125`, `164-166`; `mutation.py:387-444`). Rollback is retried at most 5 times (`mutation.py:23`, `429-443`).
  - A failed rollback raises `DirtyRollbackError`, which stops the batch (`add_coordinator.py:20-40`, `106-111`).
- **Idempotency.** The registry hash is written only after a successful compile (`cli.py:582-606`), so a failed add is retried from scratch on the next `add`. A retry adopts its own leftover artifacts and keeps its clean name (`converter.py:58-71`).
- **Gaps**
  - **Partial page failures are swallowed.** Concept and entity tasks use `asyncio.gather(..., return_exceptions=True)`. Failures are logged as `[WARN] N planned but only M written`, but the document still commits as "added" (`compiler.py:2001-2052`). Open issue **#239** proposes a fail-fast or fail-at-end `insert_mode`.
  - **Malformed plans are tolerated.** An unparseable or empty plan writes only the summary and index, and still commits (`compiler.py:1674-1712`, `1783-1794`).
  - **Whole-compile retry is not reset.** On any exception the compile is re-run once after 2 s (`cli.py:431-444`). Writes from the first attempt are not rolled back between attempts, although everything sits inside the outer snapshot.
  - **`recompile` and `remove` are not journaled** (no `snapshot_paths` call; grep shows `run_add_mutation` is used only at `cli.py:629` and `cli.py:807`). A crash mid-`recompile --all` leaves a mixed state.
  - A "staging reaper" is mentioned in a comment (`add_coordinator.py:119`), but no reaper implementation exists in `openkb/`.

## 9. Full-rebuild operations

- **`openkb recompile --all`** (`cli.py:1860-2056`, REST `iter_recompile` at `cli.py:2058-2290`) is the only bulk operation. It runs once per registry document, serially, and reuses the converted sources and PageIndex `doc_id`.
  - It does **not** clear concept/entity pages or the index first, so it is an additive re-run over the existing wiki, not a clean rebuild. Results depend on order and on the existing page state.
  - It needs a confirmation prompt and warns that manual edits will be overwritten.
  - Intended uses: pick up new pipeline features such as the entity layer added in PR #78, or a schema refresh via `--refresh-schema` (`cli.py:1799-1820`).
- **There is no "clean rebuild from raw" command.** You would have to delete `wiki/` and `.openkb/hashes.json` and re-add everything.
- **Cost of a rebuild:** #203 reports per-document rewrite cost growing roughly O(N²) on dense corpora: ~1,100 docs extrapolate to "tens of hours", and `recompile` "doesn't help".

## 10. Open issues / roadmap relevant to us

| # | State | Relevance |
|---|---|---|
| #203 | open | Bulk ingest is roughly O(N²) because hot entity/concept pages are fully rewritten for every document. Proposes a map (extract mentions per doc) / reduce (synthesize each page once) mode. **Very close to our "claim → entity → page" layering idea, but proposed as a batch mode, not as dependency tracking.** |
| #245 | open | Opt-in `concept_update_mode: append`: per-source note lines under `## Notes`, keyed by source doc so a re-ingest replaces its own note, plus `openkb consolidate`. The first step toward per-source fragments inside pages. |
| #222 / PR #223 | issue open, PR closed unmerged | Temporal claims in frontmatter: `id`, `as_of`, `status`, `source_anchor`, `supersedes`/`superseded_by`, `authority`. Claim-level provenance was proposed, but nothing landed. |
| #226 | open | The `concepts-plan` prompt embeds an uncapped, uncached list of all pages; input grew 3.5× over 28 docs. |
| #239 | open | Partial compile failures are reported as "added"; proposes `insert_mode`. |
| #274 / PR #277 (open), #275, #276 | open | Remote connectors (S3 / Drive / OneDrive). Identity is `s3://bucket/key`; same key + same bytes is skipped; same key + new bytes is recompiled with the same wiki name; **source deletions are explicitly not propagated**. |
| #41, #25 | closed | Users asked how to purge knowledge from superseded or wrong documents. Resolved only by the manual `remove` (PR #51). |
| #58 | closed | `remove` bugs: legacy hash entries were not removed, and the lint sweep was too broad. Fixed with `remove_by_hash` and a scoped `fix_broken_links`. |
| PR #15 | open | SQLite-backed registry. It is the same hash→meta table, not a dependency graph. |
| PR #43 | closed | Checkpoint/resume for long-PDF ingest; rejected because PageIndex already reuses `doc_id` by content hash. |
| #244, #247 | open | Concept page size limits and child spawning; curbing entity sprawl. |

Searches for "provenance", "lineage", "consistency" and "outdated" returned no issues. "dependency" and "rebuild" returned nothing about dependency tracking.

## 11. Other notable points

- **Stale-artifact detection: none.**
  - Structural lint (`lint.py:571-649`) checks broken links, link-graph orphans (`:294-347`), raw files without a wiki entry (`:350-418`), index sync (`:421-463`), YAML validity and OKF fields.
  - Nothing compares a raw file's current hash to its registered hash, checks that a page's `sources:` entries resolve to live registry docs, or flags pages derived from an older source version.
  - The LLM "semantic lint" "Staleness" check means dates and "recent" wording in prose (`openkb/agent/linter.py:22-32`), not dependency staleness.
- **Determinism: none at the LLM layer.**
  - No `temperature` or `seed` is passed to LiteLLM. kwargs carry only `response_format`, `timeout`, headers and credentials (`compiler.py:410-428`, `453-469`).
  - Results depend on ingest order, because the plan sees the briefs of whatever pages already exist (`compiler.py:1627-1628`) and updates rewrite the accumulated prose.
  - Deterministic pieces: `doc_name` suffixes (`converter.py:128-131`), sorted directory walks (`cli.py:1134-1138`), sorted page scans.
- **LLM-call caching: provider prompt caching only.**
  - Anthropic `cache_control` breakpoints are placed on (1) the doc message, (2) the summary message and (3) the wikilink-whitelist message, so the N+M per-page calls reuse a cached prefix (`compiler.py:1-16`, `263-335`, `1630-1632`, `1814-1830`, `2228-2246`). Markers are stripped for non-Anthropic providers (`compiler.py:277-334`).
  - There is **no response cache or memoization** keyed by input hash, so a recompile pays for every call again.
- **Cost and token accounting.** Per-call prompt, completion and cached token counts are printed to stdout (`compiler.py:364-370`, `432`, `474`). They are not aggregated, persisted or budgeted. `LITELLM_LOCAL_MODEL_COST_MAP` only avoids fetching the cost map over the network (`cli.py:29-30`).
  - The main cost saving at the source level is the file-hash skip. The second is that `recompile` skips conversion and PageIndex re-indexing.
- **Manual edits.** Users can edit concept, entity and summary pages through the Web UI (`page_ops.py:31-38`, `194+`). Nothing records that a page was hand-edited, so the next update or recompile silently overwrites it (warning text at `cli.py:1897-1898`, `1948-1951`).
- **Wikilink integrity.** A whitelist prompt plus `strip_ghost_wikilinks` post-processing stops links to pages that don't exist (`compiler.py:1796-1830`, `2054-2082`). Related slugs are filtered to pages that exist on disk (`:1735-1751`).

## Bottom line for our project

| Capability | OpenKB |
|---|---|
| Source content hashing | Yes. Whole-file SHA-256, skip-if-known (`state.py:120-127`, `converter.py:170-178`). |
| Stable source identity across edits | Yes. Path-keyed `doc_name` (`converter.py:74-132`). |
| Artifact-level input hashes / fingerprints | **No.** |
| Explicit dependency graph | **No.** Page-level `sources:` frontmatter only, recovered by scanning all pages. |
| Transitive invalidation | **No.** One hop (summary → concept/entity) and only on `remove`. |
| Selective recompilation on change | **No.** Full per-doc pipeline re-run; the LLM plan decides which pages to rewrite; additive. |
| Retraction of old facts on update | **No.** |
| Claim-level lineage | **No.** Proposed in #222 / #223 and not merged. |
| Deletion propagation | Manual `remove` only. Frontmatter and links are cleaned, prose is not regenerated. |
| Stale-artifact detection | **No.** |
| Crash-safe transactions | **Yes**, for add and import. Not for remove or recompile. |
| Deterministic compilation | **No.** |
| LLM output caching | **No.** Provider prompt caching only. |

Our differentiators would be:
- per-artifact input fingerprints and a persisted reverse dependency graph;
- claim-level lineage, so a source change or deletion retracts exactly its claims and then re-synthesizes only the pages and summaries whose claim sets changed;
- staleness detection, both pull-based and lint-based;
- content-addressed caching of LLM steps.

This directly addresses OpenKB's documented pain points: #203 (O(N²) rewrites), #245 (full-page rewrites), #41/#25 (stale knowledge after a document is superseded) and #222 (claims).
