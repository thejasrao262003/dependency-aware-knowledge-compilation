# okforge/okforge: competitive audit

- Repo: https://github.com/okforge/okforge (hard fork of VectifyAI/OpenKB)
- Snapshot audited: shallow clone at HEAD `3031b7b` ("build: pin the build backend so releases stop floating")
- Size: about 15.2k LOC Python in `okforge/`. Core files: `okforge/agent/compiler.py` (2487 lines), `okforge/cli.py` (3320), `okforge/mutation.py` (459), `okforge/state.py` (127), `okforge/converter.py` (265), `okforge/locks.py` (244), `okforge/lint.py` (695)
- Method: I only read and grepped the code; nothing was run. Every claim below is checked against the implementation. All paths are relative to the repo root.

---

## TL;DR

okforge does **file-level, content-hash-gated, per-document re-ingestion**, and its per-`add` crash safety (snapshot, journal, rollback) is genuinely solid. It has **no dependency graph and no invalidation**, and it never computes downstream impact.

- **Source identity.** Each source is keyed by path, and a whole-file SHA-256 says whether it changed. If the hash is unchanged the file is skipped. If it changed, the **whole document** goes back through the LLM pipeline.
- **What gets rebuilt.** On a change, the document's summary is always regenerated. Which concept and entity pages get touched is up to a **fresh LLM "plan" call**, not any recorded dependency. Content the old version contributed is never retracted.
- **Provenance.** It is only a `sources: [summaries/<doc>.md]` list in the frontmatter of concept and entity pages. Nothing is hashed, nothing records a version, and nothing exists at claim level. The only reverse traversal is a linear scan of frontmatter, and it is used **only by `remove`**.
- **Deletion.** `remove` is explicit only (the watcher ignores deletes). It drops the document from `sources:` lists and deletes pages left with no source. On pages that still have other sources, the removed document's claims **stay in the body**.

This is **not** the proposed engine. The closest overlaps are hash-based skip-unchanged at file level, a `sources:` back-reference that could seed reverse lookups, and a good transactional-write layer.

---

## 1. Source ingestion pipeline

**Entry points**
- `okforge add <path|dir|url>`: `okforge/cli.py:1041-1121`. For a directory it walks recursively (`rglob`) and filters by extension (`cli.py:1089-1112`). For a URL it fetches into `raw/` first (`cli.py:1069-1082`, `okforge/url_ingest.py:216`).
- `okforge watch`: `cli.py:1962-1988`. It calls `add_single_file` for every created or modified file in `raw/`.
- `okforge recompile [DOC|--all]`: `cli.py:1668-1845`. It re-runs the compile step on documents already in the registry.
- Per-document driver: `add_single_file` takes the KB lock, then calls `_add_single_file_locked` (`cli.py:451-590`).

**Stages for one document** (`_add_single_file_locked`, `cli.py:459-590`)
1. **Convert** (`okforge/converter.py:146-265`, run under the KB lock):
   - hash check, skip if known (`converter.py:177-185`)
   - resolve a stable `doc_name` (`converter.py:186-191`)
   - copy the input to `raw/` (`:196-203`)
   - reject PDFs at or above `pageindex_threshold` pages (`:208-215`)
   - read Markdown or text, or extract a PDF with pymupdf (`:225-245`)
   - copy a sibling `.pages.json` to `wiki/sources/<doc>.json` (`:233-235`)
   - write `wiki/sources/<doc>.md` with OKF frontmatter (`:247-258`)
   - All of this goes into a staging dir (`cli.py:486`, `_staging_dir_for` at `cli.py:379-383`).
2. **Snapshot**: back up the KB paths an add can touch, then write an active journal (`cli.py:507-521`, `_snapshot_add_paths` at `cli.py:401-432`).
3. **Publish staging**: rename the staged `raw/` and `wiki/sources/` files into the live KB (`cli.py:522`, `okforge/mutation.py:448-459`).
4. **Compile** with `compile_short_doc`, wrapped in one retry (`cli.py:532-535`, `_run_compile_with_retry` at `cli.py:435-448`).
5. **Register** the hash only after compile succeeds (`cli.py:537-559`). Then **commit**: `snapshot.mark_committed()` (`cli.py:561`), followed by `log.md` append and cleanup (`cli.py:580-590`).

**LLM calls per document** (`okforge/agent/compiler.py`; the module docstring at `:1-16` describes the plan)
1. `summary`: system message (schema plus language) and the full document text produce JSON `{description, content}` (`compiler.py:2369-2378`). If a `.pages.json` exists, the document is sent split by `[Page N]` markers and the prompt asks for `(p. N)` citations (`compiler.py:86-104`, `2322-2337`).
2. `concepts-plan`: full document, the v1 summary, **one-line briefs of every existing concept page** and **every entity page with its source count**. The output is a JSON plan of create, update and related, for both concepts and entities (`compiler.py:118-157`, `1716-1739`, briefs at `739-801`).
3. Each concept **create** gets one call, which writes a page (`compiler.py:1923-1948`).
4. Each concept **update** gets one call with the full current page plus the document. The prompt says "Rewrite the full page incorporating the new information naturally — do not just append" (`compiler.py:189-210`, `1950-1982`).
5. Entity create and entity update are the same pattern (`compiler.py:212-245`, `1984-2048`). Steps 3 to 5 run concurrently under a semaphore (`compile_concurrency`, default 5: `compiler.py:1671`, `1921`, `2083-2088`).
6. `summary-rewrite`: the summary is rewritten against a whitelist of valid wikilink targets (`compiler.py:255-272`, `2177-2238`).
7. "Related" items, backlinks and `index.md` updates are code only, with no LLM (`compiler.py:2252-2288`).

The legacy long-document path (`compile_long_doc`, `compiler.py:2402-2487`) is reachable **only from `recompile`** (`cli.py:1815`). The PageIndex ingest that fed it has been removed (`cli.py:1273-1279`).

## 2. Source change detection

- **Mechanism.** A whole-file **SHA-256** of the *input* bytes, read in 64 KB blocks: `HashRegistry.hash_file` (`okforge/state.py:120-127`). It is computed in `convert_document` (`converter.py:177`).
- **Store.** `.okforge/hashes.json`, a JSON map of `sha256 -> {name, doc_name, type, path, raw_path, source_path}` (`state.py:11-20`, metadata built at `cli.py:541-550`). It is persisted atomically (`state.py:113-114` calls `locks.atomic_write_json`).
- **Decision logic** (`converter.py:178-191`):
  - If the hash is already in the registry, return `skipped`. The whole document is skipped no matter what path it arrived from.
  - Otherwise `resolve_doc_name` looks the file up **by path** (`converter.py:95-101`, `state.py:38-53`). If the path is known, it reuses the old `doc_name`, so a re-ingest "overwrites in place" (docstring at `converter.py:86-94`).
  - After compiling, the old entry for that `doc_name` is dropped and the new hash added (`cli.py:551-559`). This is exercised by the test "An edited doc arrives with a new content hash; the stale entry ... must be replaced" (`tests/test_add_command.py:202-232`).
- **Granularity.** File only. There is no chunk, section or page hash anywhere; the only `hashlib` uses are `state.py:123` and the name-collision suffix at `converter.py:135`.
- **mtime.** Not used for change detection. It is used only for "Last compile" display (`cli.py:2308`, `2364-2368`).
- **Blind spots**
  - The sibling `.pages.json`, which drives page citations, is **not hashed**. If only it changes, nothing is detected.
  - Relative images referenced by the Markdown (`converter.py:227`) are not hashed either.
  - Config, model or prompt changes never invalidate anything. The only lever is a manual `recompile [--refresh-schema]` (`cli.py:1682-1689`, `1774-1775`).
  - Pages do not record which source hash they were built from. The only provenance fields are `generated: {by, at}` (`okforge/frontmatter.py:187-209`).

## 3. How derived artifacts are associated with sources

- **Summary to source.** The file name is the link: `summaries/<doc_name>.md`, and its frontmatter `full_text: sources/<doc_name>.md` (`compiler.py:967-994`, in particular `:991`).
- **Concept and entity pages to source.** A frontmatter `sources:` list of **summary page paths** (not raw files, and with no hash), rendered as OKF v0.2 `{id, resource}` mappings (`compiler.py:1012-1018`, `frontmatter.py:162-172`).
  - On create the list is `[source_file]` (`compiler.py:1117-1120`, `1202`).
  - On update the document is prepended (`compiler.py:1066-1069`, `1170-1173`, `_prepend_source_to_frontmatter` at `1208-1246`). `_add_related_link` also adds it (`1289-1317`).
  - The example output confirms the shape: `sources: ["summaries/attention-is-all-you-need.md"]` in `examples/commands/sample-wiki/concepts/attention-mechanisms.md:3`.
- **Body back-links.** `## Related Documents` and `See also: [[summaries/<doc>]]` lines, plus `## Related Concepts` and `## Entities` on the summary (`compiler.py:1320-1404`).
- **`index.md`** is a flat list of entries (`compiler.py:1594-1664`).
- **No database, edge table or manifest** links pages to sources. The registry is keyed by file hash and knows nothing about derived pages. `pageindex.db` is a frozen legacy SQLite `documents` table touched only by cleanup (`cli.py:1261-1320`).

## 4. Is there a traversable dependency graph?

**No.** Reverse lookup (which pages depend on source X) is done by a linear scan that reads every `concepts/*.md` and `entities/*.md` and checks frontmatter `sources:` membership:
- `scan_affected_pages` (`compiler.py:1490-1515`) is used only for the `remove` preview (`cli.py:1459-1479`).
- `_remove_doc_from_pages` (`compiler.py:1407-1487`) is used only by `remove`.

**No transitivity.** There is one level: page to summary to (implicitly) doc_name to hash. The scans use non-recursive `glob("*.md")` (`compiler.py:1444`, `1504`). Pages that *wikilink* to a page (as opposed to listing it in `sources:`) are not tracked as dependents. Nothing propagates summary to concept to other summaries, and the index is not modelled as a dependent. The "graph" in `okforge visualize` is only a wikilink visualisation (`okforge/visualize.py`, `templates/graph.html`).

Per-claim attribution is anticipated only in a comment: the `id` key is "a stable key for per-claim attribution" (`frontmatter.py:165-167`). Nothing implements it.

## 5. Selective regeneration on source update

- **Decision unit: one document.** If the hash changed, `compile_short_doc` runs over the whole document. Unchanged files are skipped, so `add dir/` again only processes new or changed files. That much is real file-level incrementality (`converter.py:178-185`).
- **What is rebuilt for the changed document**
  - **Summary.** Always regenerated and overwritten (`compiler.py:2238`, `994`).
  - **Concept and entity pages.** Chosen by **a new LLM plan call** that sees the current wiki briefs (`compiler.py:1716-1739`), **not** by the `sources:` back-references. Pages the old version created but the new plan skips are **not touched**: their stale content stays and they still list the document in `sources:`.
  - **Pages the plan marks "update".** These get an LLM full-page rewrite that is told to *integrate new information*, with no instruction to retract the previous version's claims (`compiler.py:189-210`). Nothing removes the old contribution first: `add` never calls `remove_doc_from_*` (`cli.py:459-590`).
  - **`index.md`.** The document's own entry is **insert-only**. If `[[summaries/<doc>]]` already exists the entry is left alone, so **the document's description in the index goes stale** after an edit (`compiler.py:1626-1631`). Concept and entity entries are replaced (`1638-1641`, `1659-1662`).
- **Granularity.** No claim-level or fact-level unit. Pages are always regenerated whole (`compiler.py:1072-1077`: "Replace body with LLM rewrite (prompt asks for full rewrite, not delta)"). Citations are free text `(p. N)`, only in summaries and only when `.pages.json` exists, and nothing validates them (`compiler.py:93-96`). Concept and entity prompts ask for no citations (`compiler.py:173-245`).
- **`recompile` is not idempotent.** It re-runs the same pipeline on every target without checking whether anything changed (`cli.py:1788-1842`). Each run asks the LLM to "integrate" the same document into pages that already contain it. `--all` runs documents one after another, and each plan sees the wiki the previous documents produced, so the result depends on order.

## 6. Source deletion handling

- **Watcher.** It handles only `on_created` and `on_modified` (`okforge/watcher.py:71-77`). **Deletes and moves are ignored.** Deleting a file from `raw/` changes nothing in the wiki, and the registry entry and every derived page are kept.
- **Explicit `okforge remove <doc>`** (`cli.py:1353-1642`):
  - It deletes the summary, `sources/<doc>.md` and `.json`, and the per-document images dir (`cli.py:1563-1567`).
  - For every concept and entity page whose `sources:` contains the document, it drops that source, strips `## Related Documents` and `See also:` links, and **deletes the page if `sources:` is now empty** (or keeps it with `sources: []` under `--keep-empty`) (`compiler.py:1407-1487`).
  - It removes index entries (`compiler.py:1558-1591`), runs scoped `fix_broken_links` **only on modified concept and entity pages plus `index.md`** (`cli.py:1595-1602`), removes the registry entry (the commit point, `cli.py:1636`) and deletes `raw/` (`cli.py:1638-1639`).
- **Gaps**
  - **Claims stay in multi-source pages.** On pages with other sources, the removed document's contributions remain in the LLM-blended body. There is no LLM rewrite on remove, and tests assert the body survives (`tests/test_remove.py:35-40`, `:924-926`).
  - **Dangling links elsewhere.** Other documents' summaries that wikilink to concept pages deleted here are *not* in the lint scope, so their links dangle. This is deliberate (comment citing issue #58 at `cli.py:1589-1594`); the fix is a manual `okforge lint --fix`.
  - **Topic-tree mode.** Concept pages in nested `concepts/<topic>/` directories are **missed**, because the scans use non-recursive `glob("*.md")` (`compiler.py:1444`, `1504`). Their `sources:` keep pointing at the removed summary.

## 7. Duplicate detection

- **Exact duplicates.** Identical bytes under any path are skipped as "already known", because the hash is global (`converter.py:178-185`). The new path is not recorded, so if the original is later removed, the copy is never ingested.
- **Renames.** A pure rename (same bytes) is skipped and the registry keeps the old path. A rename combined with an edit has an unknown path and an unknown hash, so it becomes a **new document** with a fresh `doc_name` (with a `-sha256(path)[:8]` suffix if the stem collides: `converter.py:117-137`). The old document's pages remain, which duplicates content.
- **Name collisions.** Different paths with the same stem get collision-resistant names (`converter.py:63-76`, `117-137`).
- **Near-duplicates.** No algorithmic detection: no MinHash or SimHash, no embeddings. The only guard against concept overlap is the prompt ("Do NOT create a concept/entity that overlaps an existing one — use \"update\"", `compiler.py:151-152`), plus the LLM semantic lint "Redundancy" check (`okforge/agent/linter.py:27-28`).

## 8. Failure recovery, rollback, atomicity, idempotency

This is the strongest part of the codebase and is worth studying.

- **Atomic file writes.** temp file, fsync, `os.replace`, fsync of the directory (`okforge/locks.py:210-244`).
- **KB lock.** An advisory lock (portalocker plus an in-process RW lock) on `.okforge/ingest.lock`: exclusive for mutations, shared for reads (`locks.py:129-181`).
- **Journaled `add` transaction** (`okforge/mutation.py`)
  - `snapshot_paths` backs up every path an add may touch (`hashes.json`, `pageindex.db*`, this document's summary, sources, images, **the whole `concepts/` and `entities/` dirs**, `index.md`, `log.md`, and raw and source). It then writes an `active` journal (`mutation.py:309-369`, path list at `cli.py:401-432`).
  - The concepts and entities dirs are backed up with **hardlinks**, which costs O(1) per file. Rollback then restores only the inodes that changed, so it costs O(touched) (`mutation.py:48-68`, `261-306`).
  - The commit point is `mark_committed()` after the registry write (`cli.py:561`, `mutation.py:167-178`). On an exception it calls `rollback_best_effort()` and then discards (`cli.py:562-576`).
  - **Crash recovery.** Any active journal is rolled back when the exclusive lock is next taken (`locks.py:102-126`, `166-167`, `mutation.py:388-445`). Rollback is retried at most `MAX_ROLLBACK_ATTEMPTS = 5` times (`mutation.py:24`, `430-444`), and unreadable journals are removed (`mutation.py:410-425`).
  - Staged conversion keeps half-converted files out of the live KB (`cli.py:486-497`, `mutation.py:101-126`).
- **Partial-failure semantics inside a compile**
  - A failure in any single page's generation is **swallowed** (`asyncio.gather(..., return_exceptions=True)`, `compiler.py:2085-2088`). It is reported as "[WARN] N planned but only M written" and the add still **commits**, so a partial result is accepted as success (`compiler.py:2090-2136`).
  - A truncated LLM output causes that page to be skipped rather than written in partial form (`compiler.py:502-510`, `423-462`).
  - The summary rewrite falls back to the v1 summary (`compiler.py:2208-2237`).
  - An unparseable plan writes only the summary and the index entry (`compiler.py:1762-1780`).
  - `_run_compile_with_retry` re-runs the whole document once after 2 s (`cli.py:435-448`).
- **Gaps**
  - **`recompile` has no snapshot or journal**: on failure it just skips that document and moves on (`cli.py:1814-1820`, `1834-1840`).
  - **`remove` has no journal.** It relies on idempotent steps ordered before a registry commit point (`cli.py:1554-1562`).
  - **Some writes are not atomic.** `log.md` uses plain `write_text` (`okforge/log.py:28,40,55,59`), and so do `write_kb_file` (`okforge/agent/tools.py:445`) and the lint report (`cli.py:2038`). All three contradict the project's own "all wiki writes go through locks/mutation" rule (`docs/golden-principles.md`, `AGENTS.md`).
- **Idempotency.** Re-adding an unchanged file is a no-op through the hash. Re-compiling is **not** idempotent: it makes fresh LLM calls, rewrites pages and gives them new `generated.at` stamps.

## 9. Full-rebuild operations

- `okforge recompile --all [--refresh-schema] [-y]` (`cli.py:1668-1845`) re-runs `compile_short_doc` (or `compile_long_doc`) for every registry entry, reusing the on-disk `wiki/sources/`. It **does not** reconvert raw files or wipe the wiki, so it is an in-place re-integration. Its docstring says it is for when the pipeline or schema changes ("pre-feature KBs gain the `entities/` layer", `cli.py:1695-1706`), and it warns that manual edits are overwritten (`cli.py:1764-1772`).
- There is no "clean rebuild from raw" command. The way to approximate one is `remove` plus `add`; `--keep-empty` exists for exactly that workflow (`cli.py:1358-1368`).
- `okforge reindex` (topic tree) re-clusters flat concepts into directories with LLM cluster and summarize calls (`cli.py:2071-2106`, `okforge/topic_tree.py:208-229`). New ingests are **not** placed into the tree: `place_concept` and `_write_concept(topic_dir=...)` exist (`topic_tree.py:89-123`, `compiler.py:1043-1052`) but `_compile_concepts` never calls them (`compiler.py:2241-2250`). Open issue #6 confirms it.
- `okforge okf-migrate` migrates the frontmatter format (`okforge/okf.py:256`).

## 10. Open issues and roadmap

- The GitHub tracker is tiny: three issues and four PRs in total (`gh issue list` and `gh pr list`, checked 2026-11-21). Searches for `incremental`, `stale`, `delete`, `dependency`, `hash`, `recompile`, `re-ingest` and `modified` return nothing relevant.
  - **#6 (OPEN)**, "topic_tree configuration option and new post-reindex concepts". After `reindex`, new ingests create root-level concepts, including **6 same-slug duplicates** of nested concepts. The cause is that the compiler scans only `concepts/*.md` and never passes `topic_dir`. This is a consistency and duplication bug in exactly the area our proposal covers.
  - #2 (closed) concerned the exit code on compile failure. #4 (closed) concerned concept-generation failures and asyncio errors.
- The README roadmap (`README.md`, "Roadmap") lists only **multiple LLM endpoints** and **parallel processing**. Nothing mentions incremental compilation, invalidation, staleness or dependency tracking.
- The maintainers' design docs are in `docs/internal/`, which is not in git (`AGENTS.md`), so any internal plans are invisible.

## 11. Other notable points

- **Staleness detection**
  - `okf-lint` only validates the *format* of an `stale_after` field if one is present (`okforge/okf.py:99-101`); okforge never writes `stale_after`, `status` or `verified`.
  - The LLM semantic lint asks a model to spot "references to 'recent' work, dates, or versions that may be outdated" (`okforge/agent/linter.py:25-26`). That is heuristic and does not compare against sources.
  - Structural lint (`okforge/lint.py:617-695`) checks broken links, orphans, raw files with no wiki entry (**existence only, no hash comparison**: `lint.py:396-464`), index sync, YAML validity and OKF fields.
  - Nothing detects "raw file changed since compile" or "page built from an older source version".
- **Cost and token accounting**
  - Per-call `in/out/cached` tokens are printed to stdout (`compiler.py:392-398`, `454`, `490`). They are not persisted or aggregated, and there is no budget.
  - Cost guidance in the docs is "check your OpenRouter dashboard" (`GETTING_STARTED.md:416-424`).
- **Caching**
  - Provider-side prompt caching only: Anthropic `cache_control` with three breakpoints (document, summary, link whitelist) (`compiler.py:1-16`, `291-302`, `1719-1721`, `1902-1918`). The markers are stripped for providers that reject them (`compiler.py:305-362`).
  - **No local LLM response cache** and no memoisation keyed on inputs (no litellm cache configured; `grep` finds none).
  - Prompt size grows with the KB: every plan call includes briefs for **all** concepts and entities (`compiler.py:739-801`), and every page call includes the full whitelist of link targets (`compiler.py:1894-1918`).
- **Determinism**
  - No `temperature` or `seed` is set anywhere (grep finds none).
  - Results depend on the order of ingests, because each plan sees the wiki so far.
  - `generated.at` is rewritten on every write (`frontmatter.py:187-209`). Byte-identical rebuilds are impossible.
- **Hallucination guards**
  - Ghost-wikilink stripping against a whitelist of targets on disk or planned (`compiler.py:1884-1918`, `2138-2168`, `okforge/lint.py:69`).
  - "Related" slugs that do not exist are dropped (`compiler.py:1823-1839`).
- **Other**
  - The README claim "each claim traceable back to a `(p. N)` citation" holds only for summaries built from `.pages.json`, and only as a prompt instruction. Nothing parses, stores or checks those citations.
  - Concurrency is bounded per document with a semaphore (`compiler.py:1921`). Across documents ingest is serial (one at a time, under the lock).

---

## Mapping to the proposed engine (source → claim → entity → page → summary, with content hashes)

| Proposed capability | okforge status |
|---|---|
| Content-hash change detection | **Yes, file-level only**: SHA-256 per input file (`state.py:120-127`, `converter.py:177-185`). No chunk or section hashes, `.pages.json` and images are not hashed, and pages carry no input hash. |
| Dependency graph | **No.** There is only a `sources:` frontmatter list on concept and entity pages, pointing at summaries (`compiler.py:1012-1018`). Reverse lookup is an O(N) scan used only by `remove`. |
| Claim-level lineage | **No.** Free-text `(p. N)` in summaries only, unvalidated. The `id` key for per-claim attribution is reserved in a comment (`frontmatter.py:165-167`). |
| Transitive invalidation | **No.** One level only. Wikilink dependents and the index are not tracked. |
| Selective regeneration | **Per document only.** The page set is chosen by an LLM plan, not by dependencies. Old contributions are not retracted. |
| Deletion | Explicit `remove` prunes `sources:` and deletes pages left with no source. Claims stay in multi-source bodies. The watcher ignores deletes. The topic-tree path misses nested pages. |
| Staleness detection | **No** mechanical detection. An LLM lint heuristic exists, and `stale_after` is validated but never written. |
| Failure recovery | **Strong for `add`** (journal, hardlink snapshot, crash rollback). Weaker for `recompile` and `remove`. |
| Determinism | **No.** No temperature or seed, order-dependent, timestamps on every page. |
| Cost optimisation | Provider prompt caching, skipping unchanged files and a concurrency cap. No response cache, no token ledger, no minimal recompute. |

**Verdict: PARTIAL, low overlap with the core idea.** okforge already does "hash the source and skip it if unchanged" and keeps per-page `sources:` provenance. Those are the table stakes our proposal would also need. It does **not** do dependency-aware invalidation, claim lineage, retraction of outdated contributions, transitive propagation or deterministic recompiles. A source edit means re-running the whole document through an LLM merge pipeline that can only add. The genuinely novel parts of our proposal stay open, and okforge's own failure modes are concrete motivating examples: the stale index description, residual claims after `remove` or edit, and the duplicate concepts in issue #6. Its journaled-mutation layer (`mutation.py`) is a good reference design for our transactional write path.
