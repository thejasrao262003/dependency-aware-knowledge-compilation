# inkeep/open-knowledge ("OpenKnowledge" / `ok`) — competitive audit

Audited: shallow clone at HEAD `228ac2b` ("Delete released Open Knowledge changesets (#11372)"), 7,661 tracked files. Paths below are relative to the repo root. I only read and grepped the code; nothing was built or run. Issues were checked with `gh` on 2026-11-21.

## TL;DR

OpenKnowledge is a markdown IDE: a WYSIWYG editor (Electron/web) over a Hocuspocus/Yjs CRDT server. It exposes an MCP tool surface (`write`, `edit`, `exec`, `search`, `links`, `audit`, `delete`, `move`, `checkpoint`, `restore_version`, …) to **external** agent harnesses such as Claude Code, Codex, Cursor and OpenCode. **The codebase contains no LLM generation pipeline.** "Ingest", "research", "consolidate" and "codebase-wiki refresh" are **natural-language procedures in bundled SKILL.md files**. The host agent reads them and carries them out with its own tools: shell `curl`/`sha256sum`, native `Read`, plus OK's MCP `write`. All provenance (`sources:`, `supersedes:`, `sha256:`, `source_commit:`) lives in agent-written YAML frontmatter, and the server does not interpret it beyond JSON-schema shape linting.

The real deterministic, hash-based incremental machinery covers **derived indexes**, not wiki content:
- an embedding vector cache keyed by sha256 (content-addressed, with a mtime fast path and invalidation on model/chunking identity changes);
- incremental backlink, local-target and tag indexes with reverse maps;
- a lint cache keyed by mtime+size+config;
- an incremental search corpus.

None of these regenerate pages or flag pages as stale when a source changes.

---

## 1. Source ingestion pipeline

**No code pipeline.** `grep` finds no LLM SDK imports (`@anthropic-ai/*`, `openai`, `ai`, `@ai-sdk/*`) anywhere under `packages/`. It also finds no `chat/completions`, `messages.create`, `generateText` or `streamText` call in `packages/server/src`, `packages/core/src` or `packages/cli/src`. The only outbound model call in the server is the OpenAI-compatible **embeddings** client: `packages/server/src/embeddings/embedder.ts:150` (`createOpenAiEmbedder`), which POSTs to `${baseUrl}/embeddings` (`embedder.ts:165`).

Ingestion is a prompt procedure shipped as skill text:
- `packages/server/assets/skills/project/references/ingest-and-sources.md`. This is "Ingest — capture an external source as raw reference material … Raw preservation only — no summary, no analysis" (`:1-3`). Its steps are:
  - Step 0: scope and "already ingested?" checks (`:9-17`).
  - Step 1a: binary-vs-text classification via `curl -IL`, plus STOP gates (`:23-41`).
  - Step 1b: binary download via `curl` with SSRF-safe flags (`:43-101`).
  - Step 1c: text fetch via the host's WebFetch (`:105-111`).
  - Steps 2a/2b: write a markdown "wrapper" in `external-sources/<slug>.md` with frontmatter (`:113-181`).
  - Step 4: verify (`:193-198`).
  - Step 5: re-ingest semantics (`:200-213`).
  - Step 6: chat-only takeaways (`:215-223`).
  - Step 7: optionally link from 1–3 neighbour docs (`:225-231`).
- The procedure itself makes no LLM call; the *agent* is the LLM. The ingest step deliberately does **no** synthesis ("No analysis … No promotion … No silent chaining into research", `:233-241`).
- Downstream stages are separate skills in the `knowledge-base` starter pack, and each must be explicitly invoked by the user ("Don't chain silently", `packages/server/assets/skills/packs/knowledge-base/SKILL.md:47`):
  - **research** (`packs/knowledge-base/research/SKILL.md`): the agent reads ingested sources and writes `research/<slug>.md` with `status: draft` and an object-shaped `sources:` list (`:21`, `:253-255`). Every claim is cited inline (`:224`). The skill says "Persist as you go" and edits each source's findings into the article right after analysing it (`:71-72`, `:220`).
  - **consolidate** (`packs/knowledge-base/consolidate/SKILL.md`): promotes research to `articles/` with `status: stable` and `supersedes:`, and marks the research `status: deprecated` with `superseded_by:` (`:143-159`).
- Layer model: `external-sources/ → research/ → articles/` (`packs/knowledge-base/SKILL.md:21-31`). The seeded templates are in `packages/server/src/seed/starter.ts:36-63` (folders), `:77-99` (`research-log` template with `sources: []`) and `:105-120` (`article` with `supersedes: []`).
- Codebase-wiki variant: `packs/codebase-wiki/references/generate-and-refresh.md`. The agent reads repo source with native tools and authors `wiki/{architecture,modules,flows,concepts,guides}` through phases 0–7 with STOP gates (`:47-110`).
- Entity variant (`entity-vault`, GBrain-compatible): `packs/entity-vault/SKILL.md`. Dossiers have a rewritable "compiled truth" above `--- timeline ---` and append-only dated evidence bullets below it (`:18-23`). After a meeting note lands, the agent "extract[s] entity mentions and append[s] timeline bullets to each referenced dossier" (`:44`).
- The app's chat composer `ingestFiles` (`packages/app/src/components/acp/ThreadView.tsx:493`) only attaches files to an ACP agent prompt. It is not a pipeline.

## 2. Source change detection

- **Knowledge sources: agent-run, by convention only.** For binary sources the agent runs `sha256sum`/`shasum -a 256` after download (`ingest-and-sources.md:96-101`) and records `sha256:` and `bytes:` in the wrapper frontmatter (`:125-126`). Re-ingest (`:200-213`): if `<slug>.<ext>` exists, the agent compares sha256 values. If they match it STOPs as a no-op; if they differ it saves a **dated sibling** `<slug>.YYYY-MM-DD.<ext>` plus a new wrapper with `supersedes: [<slug>.md]`, and never mutates the old one.
  - Granularity: whole file. **Text sources get no hash at all.** The text wrapper frontmatter (`:150-170`) has no `sha256`, so change detection for HTML/text sources depends on the agent's judgement. The Step-0 "already ingested?" check is `grep -rln <source-url-or-title-slug>` (`:14`).
- **Codebase wiki: one global git anchor.** `wiki/OVERVIEW.md` frontmatter has `source_commit` (`generate-and-refresh.md:84`; seeded at `starter.ts:538-539`). Refresh runs `git diff --stat <source_commit>..HEAD` (`:119`). Individual pages carry no per-page commit or per-file hash; the page templates have only `type`/`tags` (`starter.ts:440-521`).
- **Server-side (platform, not provenance):**
  - The file watcher computes sha256 of markdown content (`packages/server/src/version-hash.ts:3-5`, used in `file-watcher.ts:378,387,1186,1262`). It uses this for own-write echo suppression and for disk-change intake into the CRDT.
  - The polled watcher uses a sha256 file digest (`polled-path-watcher.ts:47-56`) plus a stat signature (`stat-signature.ts:9-10`).
  - These detect that *a file changed*; nothing downstream of them invalidates derived wiki pages.
  - The embedding cache (see §11) is the one place where content hashes gate recomputation.

## 3. How derived pages are associated with sources

Only in frontmatter and body links, written by the agent:
- `sources:` is an OKF v0.2 list of `{resource, id?, title?, author?, usage_count?, last_modified?}` (`packages/core/src/markdown/lint/okf-frontmatter/schemas.ts:65-124`). The schema's own description says: "Lineage is expressed through links rather than a dedicated field … a consumer may recurse into that source's own `sources`" (`:68`). `sources[].id` is meant to match a markdown footnote label for **claim-level attribution** (`:78-82`), but this is only a documented convention; no code reads footnote↔source ids. Nothing outside the lint schemas reads `sources:` (grep for `'sources'` in non-test `.ts` returns nothing relevant).
- `supersedes:`/`superseded_by:` chains between article/research versions and between dated source snapshots (`ingest-and-sources.md:130-131,206-213`; `consolidate/SKILL.md:143-159`). The only code mention is template text in `seed/starter.ts`.
- Inline body links (`[Source](../external-sources/x.md)`) are required by the research skill (`research/SKILL.md:316`). These links **are** indexed by the server.
- Codebase-wiki pages reference code via relative links or GitHub URLs in the body (`generate-and-refresh.md:28-34`).
- There is no DB table or manifest of page→source derivations.

## 4. Explicit dependency graph / reverse traversal

What exists is a **navigational link graph**, not a derivation graph:
- `BacklinkIndex` keeps `forward: Map<doc, Set<target>>` and `backward: Map<target, Map<source, meta>>` per git branch (`packages/server/src/backlink-index.ts:165-173`). It is updated incrementally per document (`updateDocumentFromMarkdown`, `:1469`; `deleteDocument`, `:1513`) and persisted as a snapshot (`saveToDisk`/`loadFromDisk`, `:2151-2164`). **It strips frontmatter before extracting links** (`:1471`), so `sources:` entries are *not* edges. Only body links are. Open issue #2286 confirms from the user side that frontmatter link fields aren't validated or counted.
- `LocalTargetIndex` keeps reverse maps `reverseByDoc` and `reverseByFile` (`packages/server/src/local-target-index.ts:268-269`), with `getDocumentDependents` and `getFileDependents` (`:346-358`). When a target *appears or disappears*, dependents are re-assessed (`setFileTarget` → `reassessFileDependents`, `:462-477,818-824`). However:
  - it returns early if existence is unchanged (`:463`), so a **content change to a target is a no-op**;
  - it only recomputes link-resolution status (broken/resolved), never content;
  - it is one hop;
  - it also strips frontmatter (`local-target-occurrences.ts:390-391`);
  - `getLocalTarget*Dependents` have no non-test callers outside `derived-document-index.ts:675-682`.
- MCP `links` exposes `backlinks`, `forward`, `dead`, `orphans`, `hubs` and `suggest` (`packages/server/src/mcp/tools/links.ts:24,82-133`), all **1-hop**. `getLinkGraphNeighborhood(center, maxDegrees)` does an *undirected* multi-hop BFS (forward + backward + external) for the graph UI (`backlink-index.ts:2046-2090`). That is reachability, not "what depends on X".
- **No transitive dependency invalidation.** The agent can do it by hand: `links({kind:'backlinks'})` on a source wrapper, then backlinks of those docs, and so on.

## 5. Behaviour on source update

- **knowledge-base pack:** sources are "immutable after capture — update only to refresh a stale fetch" (`packs/knowledge-base/SKILL.md:53`). A changed binary becomes a new dated snapshot with `supersedes` (`ingest-and-sources.md:206-213`). **Nothing propagates the change.** No step finds the research or articles citing the old snapshot. The research skill has a manual "Path C — Update existing research" that the user triggers by saying "update/refresh/extend" (`research/SKILL.md:23,111-142`), and the agent then "diff[s] deltas" (`:99`) by reading.
- **codebase-wiki:** "Refresh mode … Incremental by default". The agent runs `git diff --stat <source_commit>..HEAD`, then "**Map changed code → affected pages**. For each changed package/area, update its module/architecture/flow pages". It falls back to a full GENERATE "if the diff is large or structural … or `source_commit` is missing", then re-stamps (`generate-and-refresh.md:114-123`; also `codebase-wiki/SKILL.md:64-66`). This is the closest thing to selective regeneration, but **the changed-file → page mapping is LLM judgement**, with no recorded per-page dependency set, and the granularity is the whole commit diff.
- **entity-vault:** re-sync of a meeting is keyed by `meetings/<source>-<source_meeting_id>` "so re-syncing a meeting updates it in place instead of creating a duplicate" (`entity-vault/SKILL.md:28,43`). Dossier updates are append-only timeline bullets, with "compiled truth" rewritten by the agent (`:20-23`). Nothing retracts timeline bullets derived from a meeting note that later changed.
- **Claim/fact-level granularity:** none in code. The closest are the OKF `sources[].id` ↔ footnote convention (`schemas.ts:78-82`) and entity-vault timeline bullets of the form `- **YYYY-MM-DD** | source | @author — evidence. Confidence: …` (`entity-vault/SKILL.md:21`). Both are formats only, with no tracking.

## 6. Source deletion

- MCP `delete`: "Irreversible; deleting a doc leaves inbound redlinks. Call links({kind:'backlinks',document}) BEFORE deleting." (`packages/server/src/mcp/tools/delete.ts:31`, and `:101` "Inbound links become redlinks").
- Derived pages are **left orphaned with dead links**. Nothing cascades, and pages are not marked stale. Detection is after the fact via `links({kind:'dead'})` or `audit` (broken-link family, `mcp/tools/audit.ts:28-30`). Every `write`/`edit` also returns `brokenLinks` for the doc just written. A `sources:` entry that points at a deleted wrapper is *not* reported, because frontmatter is not link-indexed (§4).
- Index cleanup is handled. `BacklinkIndex.deleteDocument` removes edges (`backlink-index.ts:1513+`), and `LocalTargetIndex.removeSource` re-assesses dependents so their links flip to broken (`local-target-index.ts:426-454`). The embedding cache drops vectors of docs that are no longer active (`vector-cache.ts:230-242`).
- The knowledge-base convention is to *not* delete research but deprecate it (`consolidate/SKILL.md:165`).
- Open issue #1679 ("safe project-level delete and rename with complete state cleanup") is about project directories, not derived content.

## 7. Duplicate detection

- **Binary assets uploaded through OK** (MCP `write({asset})` or UI upload): sha256 dedup within the **same directory only**. The candidate scan is restricted to same-size files and capped at 1000 candidates, and a match returns the existing file (`packages/server/src/services/assets.ts:152-213,370-387`; mode `same-dir` at `packages/core/src/constants/upload.ts:262`; surfaced at `mcp/tools/write.ts:650-652`). The ingest procedure downloads with shell `curl` straight to disk (`ingest-and-sources.md:67-77`), which **bypasses** this upload dedup, even though the skill credits OK with "sha256 dedup" (`:5`).
- **Sources:** an agent-run `grep -rln <url-or-slug>` (`ingest-and-sources.md:14`) and sha256 equality on the same slug (`:204-205`). The same bytes under a different slug, near-duplicates, and the same URL with different content are not detected by code.
- **Q&A pages:** the agent should "scan first (`search`, `exec("grep …")`); if one does [answer it], point the user to it instead of writing a near-duplicate" (`project/references/corpus-qa.md:9`). Optional semantic search (embeddings) can help the agent find near-duplicates, but no dedup logic uses it.
- **Renames (on disk):** the file watcher pairs a delete with a create as a `rename` when the last-known sha256 or inode matches (`packages/server/src/file-watcher.ts:1167-1211`). That preserves document identity. Managed moves (`move` tool) rewrite inbound inline links (`mcp/tools/move.ts:46-47`; `managed-rename-rewrite.ts`, invoked from `api-extension.ts`/`apply-managed-rename.ts`). The rewriter has no frontmatter handling (no `frontmatter` match in `managed-rename-rewrite.ts`), so `sources: - resource: ../x.md` is not updated on rename.
- **Skill packages** (not knowledge): "identical content-hash re-import is a no-op" (`mcp/tools/import.ts:38`).

## 8. Failure recovery, atomicity, idempotency

- **File-level atomicity:** `atomicWriteFile` writes a tmp file then renames it, and sweeps stale tmps (`packages/core/src/util/atomic-yaml-write.ts:74-92`). Document persistence also writes tmp→rename (`packages/server/src/persistence.ts:2028+`). The vector cache manifest is written tmp→rename (`vector-cache.ts:278-280`).
- **Multi-document operations are not transactional.** `write({documents:[…]})` runs `writeOneDoc` sequentially and returns per-doc ok/error (`mcp/tools/write.ts:813-825`), so partial success is possible. Cross-scope skill moves explicitly "can leave partial state" (`move.ts:49`).
- **Rollback:** `checkpoint` saves a project-wide snapshot to a shadow git repo and returns a 40-char SHA (`mcp/tools/checkpoint.ts:19-20`). `restore_version` restores **one** document or skill to a SHA (`mcp/tools/restore-version.ts:38`). There is no "roll back this ingest run".
- **Crash resumption is by convention:** "Ingest each source the moment you fetch it … Create the article skeleton early and fill it section-by-section … you resume by reading the partial article back" (`research/SKILL.md:71-72,230`). For the codebase wiki: "resuming keeps already-written pages" (`generate-and-refresh.md:158-159`).
- **Idempotency:** re-ingest with a matching sha256 is a no-op (`ingest-and-sources.md:205`, binary only). Meeting re-sync is keyed by `source_meeting_id` (`entity-vault/SKILL.md:28,43`). Embedding passes are idempotent through the content-hash cache.
- A large body of CRDT/disk-reconciliation machinery (`persistence-*`, `stale-*` tests, `doc-lineage-guard.ts`, `persistence-staleness-watchdog.ts`) concerns **editor/disk consistency**, not knowledge provenance. The "lineage" and "stale" file names refer to CRDT document identity, not source lineage.

## 9. Full-rebuild operations

- **Content:** codebase-wiki "Full-regen fallback" (`generate-and-refresh.md:121`). This is triggered when the diff is large or structural, or when `source_commit` or git is unavailable. The knowledge-base pack has no "rebuild wiki" operation (it isn't a compiler).
- **Indexes:** `BacklinkIndex.rebuildFromDisk` (`backlink-index.ts:2225`, called from `derived-document-index.ts:354,718,747,862`) and `LocalTargetIndex.rebuildFromDisk` (`local-target-index.ts:635`; `derived-document-index.ts:893`). The embedding cache is wiped and rebuilt when provider, model, dims, chunk config or schema version changes (`vector-cache.ts:145-164`), or when vector dims drift (`semantic-search-service.ts:161-196`). The search corpus has cold, incremental and rebuild modes (`services/search.ts:47,500-503`).
- The CLI has no `ingest`, `compile` or `rebuild` command (`packages/cli/src/commands/`: `seed`, `lint`, `audit`, `embeddings`, `sync`, …).

## 10. Open issues / roadmap

- **#1681 (OPEN, 2026-09-14, external author `mhingston`, labels `feature, ok:platform`, 0 comments): "Deterministic source-ingestion hooks for external knowledge producers"**. It asks for a versioned producer/import contract with "source identity, revision/version, extraction time", "deterministic transform, validation, and diff", and "generated versus hand-curated" ownership so that "regeneration cannot overwrite manual content". It also says "the default path should not silently rewrite facts with an LLM". This is a request, not a plan. There is no milestone and no linked PR.
- **#1680 (OPEN, same author): "Configurable provenance and freshness validation"**. It asks for required provenance fields, freshness policies and CI JSON output. The maintainer reply was "We will add this to our backlog and determine prioritization." Again no milestone or PR.
- **#2286 (OPEN): "Typed link constraints in frontmatter schemas"** (claims→sources integrity for an investigative-journalism KB). It says frontmatter links aren't validated and asks for "x-ok-link". This confirms that frontmatter provenance links are not first-class.
- #1444 (CLOSED) fixed datetime parsing for `stale_after` and `sources.last_modified` in the OKF provenance lint (shape only).
- #1174 Web Clipper and #1172 standalone-markdown import (OPEN) are capture features.
- #2205 (CLOSED) was a perf fix: the backlinks.json cache was rewritten on every save.
- No PRs were found for incremental/dependency/provenance work beyond the OKF lint datetime fix (#1454) and incremental *search corpus* maintenance (#752).

## 11. Other notable items

- **Stale-artifact detection:** none computed. `stale_after` is schema-validated, and its description says an instant in the past "is a freshness signal for consumers, not a violation" (`schemas.ts:212-217`). The settings UI says "absence is never an error" (`packages/app/src/components/settings/LintingSection.tsx:394-399`). The OKF document rules are only `no-wiki-links`, `log-shape`, `index-shape` and `reserved-casing` (`packages/core/src/markdown/lint/okf-rules.ts:14-17`), and the only project rule is `project-no-mdx` (`okf-project/rules.ts:44-46`). "Find stale claims" is a monthly *prompt* to the agent using orphans, dead links and backlinks (`docs/content/workflows/karpathy-llm-wiki.mdx:246`). `text-only` fallbacks are greppable for upgrade (`ingest-and-sources.md:181`).
- **Caching of model calls:** the embedding vector cache (`packages/server/src/embeddings/vector-cache.ts`) is a well-designed content-addressed incremental cache:
  - `hashContent` = sha256(utf-8 content) (`:53-55`);
  - a per-doc manifest `{contentHash, mtimeMs}` (`:20-23`);
  - an mtime fast path `isFresh` (`:194-199`);
  - hash reuse across docs or renames via `link()` (`:201-207`), so identical content is never re-embedded;
  - a cache identity of `providerId/modelId/dims/chunkConfigId` with full invalidation on change (`:112-119,145-164`; `chunking.ts:7`);
  - GC of unreferenced blobs and deleted docs (`:230-242,282-289`);
  - an atomic manifest write (`:278-280`).

  It is driven by `runEmbedPass` (`semantic-search-service.ts:290-296,392-393`). Granularity is whole-document: any edit re-embeds all chunks of that doc, with chunks fixed at 8000 chars and 400 overlap (`chunking.ts:1-7`). There is **no caching of LLM generation**, because OK makes no generation calls.
- **Lint cache:** keyed by `contentDir, docRelPath, mtimeMs, size, sha1(config)` (`packages/server/src/lint/audit-cache.ts:38-56`), with an LRU bound.
- **Cost/token accounting:** none for ingestion. The UI shows ACP `usage_update` context-window usage `{used,size}` (`packages/app/src/lib/acp/thread-event-model.ts:164,652-660`). Embedding telemetry counts requests and outcomes (`embeddings-telemetry.ts:51`).
- **Determinism:** the outputs are agent/LLM-authored and non-deterministic. Write provenance (who edited what) is recorded through CRDT attribution and shadow-git history (`exec` returns "shadow-repo activity and git history with human/agent attribution", `mcp/tools/exec.ts:64`). Input provenance (what was read to produce a page) is **not** recorded by code.
- **Audit trail:** `log.md` per project/wiki is append-only by agent convention (`packs/knowledge-base/SKILL.md:72-91`; `codebase-wiki/SKILL.md:68-79`).

---

## Capability matrix vs. the proposed dependency-aware incremental compiler

| Capability | OpenKnowledge | Gap |
|---|---|---|
| Source change detection | Agent-run sha256 on binary sources in frontmatter (`ingest-and-sources.md:96-101,200-213`); global `source_commit` git anchor for codebase wiki (`generate-and-refresh.md:84,119`) | Prompt convention, not code; no hash for text sources; no per-page anchor |
| Dependency tracking | Body-link backlink graph (`backlink-index.ts:165-173`), reverse maps in `local-target-index.ts:268-269,346-358` | Navigational, not derivational; frontmatter `sources:` not indexed (`backlink-index.ts:1471`) |
| Claim-level lineage | OKF `sources[].id` ↔ footnote convention (`schemas.ts:78-82`); entity timeline bullets | Format only; nothing parses or tracks it |
| Incremental recompilation | Codebase-wiki "refresh" diff → LLM maps to pages (`generate-and-refresh.md:114-123`) | LLM judgement, no recorded deps, whole-commit granularity |
| Source deletion handling | Leaves redlinks; detected via `links dead`/`audit` (`delete.ts:31`) | No cascade, no stale flag; frontmatter refs not checked |
| Stale artifact detection | `stale_after` shape-lint only (`schemas.ts:212-217`) | Not evaluated; issue #1680 open |
| Transitive invalidation | Undirected k-hop BFS for graph UI (`backlink-index.ts:2046`) | No transitive dependents traversal |
| Selective regeneration | Same as incremental recompilation (agent judgement) | No mechanism |
| Failure recovery | tmp+rename atomic writes, checkpoint/restore_version, "persist as you go" | Batches non-atomic; restore is per-doc |
| Deterministic compilation | Deterministic indexes only | Content generation is LLM-agent; #1681 asks for determinism |
| Cost optimization | sha256 content-addressed embedding cache (`vector-cache.ts`) | No LLM-call caching; no token accounting for ingest |

**Verdict:** NO. OpenKnowledge is an editor + MCP surface + prompt-skills for LLM wikis. Its only hash-gated incremental recomputation is for embeddings and indexes. Source→page derivation lives in agent-written frontmatter that the server doesn't track. Open issues #1680/#1681 show users asking for exactly the deterministic provenance/regeneration layer the proposed project would provide.
