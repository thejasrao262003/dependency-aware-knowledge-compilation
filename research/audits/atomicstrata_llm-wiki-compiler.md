# atomicstrata/llm-wiki-compiler: code audit (competitive research)

Repo: `atomicstrata/llm-wiki-compiler` (v1.5.0, HEAD `b8e28c6`), TypeScript, ~1,266 src files. I only read and grepped the shallow clone; nothing was run. Path alias: `@atomicstrata/llmwiki-core/modules/*` resolves to `src/*` (`tsconfig.json:23`).

Everything below comes from reading the implementation. I checked that each cited function is reached from the CLI (`src/cli.ts:31,33,35,39,47,51` lazily register compile, rm, refresh, state, watch and lint).

## Verdict on the web-summary claim

> "Its lint step flags pages whose sources changed (stale) or were deleted (orphaned) since the last compile, and a `refresh --stale` command repairs them. Compilation is incremental, so unchanged sources do not flow back through the model."

- **Lint stale/orphaned: TRUE.** `checkStalePagesFrom` (`src/linter/rules.ts:163-189`) is registered in `RULES_WITH_FRESHNESS` (`src/linter/index.ts:95-97`) and run on every lint (`src/linter/index.ts:155-160`, called from `src/commands/lint.ts:215,238`). Classification is in `src/freshness/index.ts:45-60`.
- **`refresh --stale` repairs them: TRUE, with a caveat.** It is the normal compile with a `changeFilter` restricted to changed or deleted *owner sources*, plus `skipSeedPages: true` (`src/commands/refresh.ts:80-85`, `src/compiler/refresh-plan.ts:233-239`). The repair works per source, not per page.
- **"Unchanged sources do not flow back through the model": MOSTLY TRUE, with real exceptions.**
  - If nothing changed, the concept pipeline makes no LLM call (`src/compiler/index.ts:371-372`). Seed pages (overview/comparison/entity) are the exception: they are LLM-regenerated on every compile, including no-change compiles (`src/compiler/compile-finalize.ts:163-179`, `src/compiler/seed-pages.ts:183`).
  - The **full text** of unchanged co-owner sources *is* sent to the model again inside the page-generation prompt for every shared concept that is rebuilt (`src/compiler/extraction-merge.ts:61-64,147-152`). This is by design.
  - An unchanged co-owner normally reuses its saved extraction (no extraction call). Reuse is turned off for the **whole run** whenever any deletion is detected or any frozen slug persists (`src/compiler/extraction-snapshot.ts:76`). In that case every co-owner in the transitive closure is re-extracted by the LLM, and all of its pages are regenerated (see Q5).
  - Changing a prompt modifier (`--lang`, `--no-sources-section`, `--no-pages-limit`, or an instructions file) promotes **every** unchanged source to changed (`src/compiler/prompt-modifiers.ts:189-194`).

## 1. Ingestion / compile pipeline

**Ingest** (`src/commands/ingest.ts:356-415`) fetches or reads URL, PDF, image, transcript or file content and writes one Markdown file with YAML frontmatter into `sources/` through `saveSource` (`src/utils/source-writer.ts:238-298`). Ingest makes no LLM call for text sources.

**Compile entry:** `llmwiki compile` → `src/cli/compile-command.ts:48-57` → `src/commands/compile.ts:18-28` → `compile()` → `compileAndReportWith(localCompileRunSteps, …)` (`src/compiler/index.ts:136-203`).

Stages, in order, from `src/compiler/index.ts` and `src/compiler/compile-phases.ts`:

1. **Acquire the lock.** This is a PID lock file with stale-lock reclamation (`src/compiler/index.ts:175`, `src/utils/lock.ts:1-30`).
2. **Journal recovery.** Any pending page-batch journal is reverted to its pre-state; an unsafe journal aborts the run (`src/compiler/index.ts:190-193`).
3. **Prepare** (`src/compiler/compile-phases.ts:144-166`):
   - load the schema and review policy;
   - `detectChanges`: SHA-256 per source file;
   - `applyChangeFilter`, used only by refresh;
   - `promoteForPromptModifiers`;
   - `markUnchangedPendingSources`, which avoids re-proposing sources whose review candidates are still pending;
   - `reusableSourceFiles`;
   - `findAffectedSources`, the co-owner closure;
   - `findReconciliationSlugs`;
   - bucket the results into `toCompile`, `deleted` and `unchanged`.
4. **No-change short-circuit** to `finalizeUnchanged` (`src/compiler/index.ts:371-372`).
5. **Orphan marking for deleted sources.** Runs before any generation (`src/compiler/index.ts:376,433-442`).
6. **Extraction (LLM call #1 per source).** `runExtractionPhases` (`src/compiler/extraction-phase.ts:92-118`) makes one tool-call per source. The prompt contains the whole source text plus the entire `wiki/index.md` (`src/compiler/extraction-phase.ts:143-144,161-175`). After that, a fixed-point loop pulls in late-affected co-owners (`src/compiler/extraction-phase.ts:104-115`).
7. **Freeze failed extractions.** A failed extraction gets a blank hash, which forces a retry (`src/compiler/deps.ts:382-400`).
8. **Page generation (LLM call #2, one per affected page).**
   - `generatePagesPhase` merges extractions by slug (`src/compiler/compile-phases.ts:197-235`, `src/compiler/extraction-merge.ts:127-160`).
   - `renderMergedPageContent` (`src/compiler/page-renderer.ts:45-81`) builds the prompt from: the budgeted combined source text, the **existing page body** (left out only when `rebuild`), up to 5 "related" concept pages (the first 5 directory entries, not chosen semantically: `src/compiler/page-renderer.ts:25,121-137`), and every link-target title.
9. **Policy, lint and write.**
   - The review policy may hold a page as a candidate (`src/compiler/review-pipeline.ts:58-96`).
   - Otherwise every live page is written in **one journalled batch** (`src/compiler/index.ts:217-235`, `src/compiler/compile-write.ts:172-208`).
10. **Persist the per-source state into the in-memory draft** (`src/compiler/index.ts:244-275`), then orphan unowned frozen pages and persist frozen slugs (`src/compiler/index.ts:414-421`).
11. **Finalize** (`src/compiler/compile-finalize.ts:124-139,215-268`):
    - seed pages (LLM);
    - interlink resolution (rule-based, no LLM; `src/compiler/resolver.ts:1-10`);
    - link repair;
    - **one durable `state.json` flush** (`src/compiler/compile-finalize.ts:260-263`);
    - regenerate `index.md` and the MOC (deterministic, always the whole thing);
    - refresh embeddings for changed slugs.
12. **Write `log.md`** (`src/compiler/index.ts:427`).

**LLM call sites in the compile path:** `src/compiler/extraction-phase.ts:166`, `src/compiler/page-renderer.ts:64`, `src/compiler/seed-pages.ts:183` (through `steps.complete`). All three go through `callClaude` (`src/utils/llm.ts:75-103`), which only adds retry with exponential backoff.

## 2. Source change detection

- **Algorithm.** SHA-256 hex over the full decoded text of each selected source file (`src/compiler/hasher.ts:33-36,56-59`). The hash covers the frontmatter, but ingest avoids rewriting a file when only the volatile `ingestedAt` would change (`src/utils/source-writer.ts:209-217,293-295`).
- **Classification.** `new`, `changed`, `unchanged` or `deleted`, by comparing with `state.sources[file].hash` (`src/compiler/hasher.ts:79-124`). A source the selection no longer includes is treated as `deleted` with `reason: "deselected"` (`src/compiler/hasher.ts:132-143`).
- **Granularity: whole file only.** Sections, chunks and claims are never hashed. `state.indexHash` exists in the schema but is vestigial: it is always `""` and never computed (`src/utils/state.ts:67`, `src/utils/types.ts:103`).
- **Configuration changes.** These invalidate everything through a prompt-modifier digest stored in state: lang, sourcesSection, pagesLimit, and the sha256 of the instructions policy (`src/compiler/prompt-modifiers.ts:41-50,131-135,170-194`). A change of **model, provider or `PROMPT_VERSION` does not trigger a recompile.** It is only stamped into page frontmatter (`src/compiler/provenance.ts:31-36`) and invalidates the extraction snapshot the next time that source participates (`src/compiler/extraction-snapshot.ts:53-58,89`).
- **Hashes are computed up to three times per run:** at detection, again at persist (`src/compiler/index.ts:269`, which re-reads disk through `src/compiler/compile-access.ts:166`), and again in the freshness snapshot for lint and status.
- **Potential correctness gap: the persisted hash is not the hash of the bytes that were compiled.**
  - Extraction reads the content at `src/compiler/extraction-phase.ts:132`.
  - `persistExtractionStates` later records `await access.hashSource(file)`, a **fresh disk read** taken after the whole page-generation phase (`src/compiler/index.ts:268-270`).
  - If a source is edited during a compile, state therefore records the post-edit hash while the pages reflect pre-edit content. The next compile, including the queued recompile in `watch` (`src/commands/watch.ts:57-68`), sees "unchanged", and freshness reports "fresh".
  - The extraction snapshot deliberately avoids this mistake ("Hash the bytes actually supplied to extraction, not a later disk read", `src/compiler/extraction-snapshot.ts:47-50`), but the main state hash does not.
  - The review path has the same issue (`src/compiler/source-state.ts:56`).
  - The lock (`.llmwiki/lock`) does not stop editors from writing to `sources/`.
  - I found this by reading the code; I did not run it.

## 3. How pages are associated with sources

There are two parallel records, and neither is a separate lockfile or database:

- **`.llmwiki/state.json`:** `sources[file] = { hash, concepts: slug[], compiledAt, extraction?: {fingerprint, sourceHash, concepts}, entities? }`, plus `frozenSlugs` and a `promptModifiers` digest (`src/utils/types.ts:85-121`). This forward map from source to concept slugs **is the authoritative ownership graph**. Freshness, refresh, orphaning and rm all read it.
- **Page frontmatter:** `sources: [file…]`, `title`, `summary`, `kind`, `createdAt`/`updatedAt`, `confidence`/`provenanceState`/`contradictedBy`, `modelId`, `promptVersion`, `promptModifiers` (`src/compiler/page-renderer.ts:87-109`, `src/compiler/provenance.ts:31-58`). Frontmatter is descriptive. The freshness code explicitly treats state as authoritative ("state is authoritative", `src/freshness/index.ts:68`).
- **Typed entity pages** from non-default profiles (`papers/`, `ideas/`, …) have **no ownership entry** and are always classified `unverified`. The code calls this an acknowledged limitation (`src/freshness/index.ts:37-43`).

## 4. Dependency graph: explicit? reverse-traversable? transitive?

- **Explicit edges stored:** only source → concept slug (`state.sources[*].concepts`).
- **Reverse index:** built on the fly as concept → owning sources (`buildConceptToSourcesMap`, `src/compiler/deps.ts:39-56`; `buildOwnersIndex`, `src/freshness/index.ts:91-101`). So the graph can be traversed in reverse, but only within this one bipartite layer.
- **Transitive closure:** only source ↔ source through shared concepts. `expandSharedOwnerClosure` walks co-ownership to a fixed point, i.e. the whole connected component of the source/concept bipartite graph (`src/compiler/deps.ts:86-110`). It is called from `findAffectedSources` (`src/compiler/deps.ts:129-157`) and `findLateAffectedSources` (`src/compiler/deps.ts:322-342`). Its job is to gather evidence so that shared pages are rebuilt from all owners.
- **Not tracked as dependencies:**
  - page → page, whether through wikilinks or the 5 "related pages" placed in the prompt (`src/compiler/page-renderer.ts:121-137`);
  - page → its own previous body, which is fed back into the prompt (`src/compiler/page-renderer.ts:59`);
  - extraction → `wiki/index.md`, which is in the extraction prompt but deliberately excluded from snapshot validity (`src/compiler/extraction-snapshot.ts:3-6`);
  - page → seed/summary pages: the schema declares `relatedSlugs`, but they only supply prompt content and never drive invalidation (`src/compiler/seed-pages.ts:180,221-231`).
- **Downstream artifacts:**
  - `index.md` and the MOC are fully regenerated on every compile, with no LLM (`src/compiler/compile-finalize.ts:265-266`).
  - Interlink resolution is incremental, O(changed × total) (`src/compiler/resolver.ts:9-10`).
  - Embeddings have chunk-level content-hash reuse (`src/utils/embeddings-chunks.ts:29-56,95-106`).
- **Reverse wikilink scan, report only:** `rm` scans for surviving pages whose wikilinks would break (`src/sources/removal-plan.ts:1-31`). This only produces a report; nothing is invalidated.

## 5. On source change: what gets recompiled?

Granularity is **per source for extraction and per concept-page for generation**. There are no claims as a unit.

- **The changed source** A is fully re-extracted (one LLM call) against the current index.
- **Pages regenerated** = every concept in A's fresh extraction ∪ A's previous concepts ∪ reconciliation slugs (`affectedPageSlugs`, `src/compiler/extraction-merge.ts:27-35`; filtering at `src/compiler/extraction-merge.ts:141`). Each affected page costs one LLM call, and its prompt includes the full text of **every** owning source.
- **So yes, unchanged pages that share the source are re-run.** If A owns X, Y and Z and the edit touches only X's material, Y and Z are still regenerated.
- **Unchanged co-owners** B, C, … in the closure:
  - If their snapshot is valid, they reuse it with no LLM call (`src/compiler/extraction-phase.ts:133-137`, `src/compiler/extraction-snapshot.ts:82-96`), and their *other* pages are not regenerated ("Reused contributors participate only in affected concepts", `src/compiler/extraction-merge.ts:124-125`). This is reasonably precise.
  - **Cascade case:** if any deletion is detected, or any frozen slug persists, `reusableSourceFiles` returns an empty set (`src/compiler/extraction-snapshot.ts:76`). Every closure member is then LLM-re-extracted, counts as "fresh", and **all of its pages regenerate** (`src/compiler/extraction-merge.ts:30`). Deleting one source can therefore rebuild an entire connected component.
- **Early cutoff: NONE.**
  - Nothing compares new extraction output with the previous extraction.
  - Nothing hashes the merged page inputs (combined content plus prompt).
  - Nothing compares a regenerated page with the old body before writing.
  - Every affected page is unconditionally regenerated and rewritten with a new `updatedAt` (`src/compiler/review-pipeline.ts:90-95`, `src/compiler/page-renderer.ts:92-104`).
  - The only content-hash short-circuit downstream is embedding chunk reuse.

## 6. Source deletion

Detected as `deleted` by `src/compiler/hasher.ts:132-143`.

- **Exclusively owned pages** are **not deleted**. They are rewritten with `orphaned: true` injected into the frontmatter (`src/compiler/orphan.ts:79-106,172-197`). The source's state entry is then removed (`src/compiler/orphan.ts:105`).
- **Shared pages** (a deleted owner plus at least one live owner) become reconciliation slugs (`src/compiler/deps.ts:167-187`). They are rebuilt with `rebuild=true`, meaning the old body is **not** fed back (`src/compiler/page-renderer.ts:59`), from the surviving owners only. They stay frozen until a replacement page is committed (`src/compiler/deps.ts:214-262`), and any that lose every owner are orphaned afterwards (`src/compiler/orphan.ts:127-150`).
- **`llmwiki rm <source> [--dry-run]`** (`src/sources/removal.ts:1-50`) is an explicit delete. It removes the source file first, then the exclusively owned pages, then the state entry, keeps shared pages, and reports broken inbound wikilinks.
- **Orphaned pages** are reported by lint (`src/linter/rules.ts:129-146,179-185`) and by `status` (`src/status/collect.ts:139-155`).
- **`compile --review`** defers orphan-marking (`src/cli/compile-command.ts:33`).

## 7. Duplicate and rename detection

- **On ingest, identity is the frontmatter `source` field** (a URL or path), not the content.
  - Re-ingesting the same identity updates the file in place, or reports `unchanged` if the stable content matches (`src/utils/source-writer.ts:188-201,248-298`).
  - Two different identities whose titles slugify to the same name are kept apart by a hash suffix (`src/utils/source-writer.ts:157-173`).
  - **The same content under two different identities is not deduplicated.**
  - `ingestText` derives `manual:<sha256(title,text)>`, so identical manual text is idempotent (`src/commands/ingest.ts:383-393`).
- **On compile, there is no rename detection.** Moving or renaming a file in `sources/` shows up as one `deleted` plus one `new`. The old owner's exclusive pages are orphaned, the source is re-extracted, and the pages are regenerated.
- **Concept/entity deduplication** is an exact `slugify(concept)` match, plus a hint in the extraction prompt to avoid concepts already listed in the index (`src/compiler/extraction-merge.ts:45`, `src/compiler/prompts.ts:166-167`). There is no semantic merging; open issue #281 asks for it.

## 8. Failure recovery

- **Lock.** A PID lock is held for the whole run (`src/utils/lock.ts`).
- **Per-batch atomicity.** Each page batch writes an intent journal that records pre-state bytes, and replay reverts pending batches (`src/trust/journal.ts:1-27`). One compile has several separate batches: orphan, generation, seed, link-resolve and link-repair. The journal comment says it is crash-consistent but **not power-loss durable (no fsync)**.
- **State.** `state.json` is written atomically by temp file plus rename (`src/utils/state.ts:252-262`), exactly **once**, after link resolution (`src/compiler/compile-finalize.ts:243-263`). A crash before that point re-runs the whole compile from the old state, so all LLM work is redone. Page batches committed before the crash stay on disk; they are not rolled back across batches. A corrupt state file is backed up to `.bak` and treated as empty, which forces a full recompile (`src/utils/state.ts:243`).
- **Partial failure.** There are two cases:
  - **Extraction parse failure.** The source is frozen with a blank hash and retried next run, and its old pages are kept (`src/compiler/deps.ts:382-400`).
  - **Provider exception, after retries.** Extraction short-circuits the remaining queue (`src/compiler/extraction-phase.ts:49-65`). Page generation has no try/catch (`src/compiler/compile-phases.ts:212-217`, `src/compiler/review-pipeline.ts:58-96`), so one throw rejects `Promise.all` and **discards every page already generated in that run**. There is no per-page checkpoint and no response cache, so a retry pays again.
- **Invalid pages** (validation or floor checks) are skipped rather than aborting the run (`src/compiler/compile-write.ts:16-21`).
- **Idempotency.** Logically yes: re-running converges, orphan-marking is idempotent (`src/compiler/orphan.ts:192`), and replay is idempotent. Byte-for-byte no, because outputs depend on LLM sampling and timestamps.

## 9. Full rebuild

There is no `compile --force` or `--full` flag (`src/cli/compile-command.ts:28-47`).

- **`llmwiki state reset --yes`** backs up and removes `state.json`, so the next compile treats every source as new (`src/commands/state-reset.ts:1-14`). Existing page bodies are still fed back as "existing page" context unless `--scope wiki|all` also deletes the wiki (`src/cli/state-commands.ts:19`).
- **Changing any prompt modifier** also forces every source to recompile.

## 10. Issues and roadmap

- **#267 (open, scale).** The maintainer writes: "each source needs one model call to extract concepts plus one per page it creates or updates, and the extraction prompt includes the full wiki index, so each document costs more as the wiki grows… Scale is a high priority that we're actively researching… it is gonna take some core computer science analysis and algorithmic implementation." **This is the most direct sign that finer-grained incremental compilation is a stated, unsolved priority.**
- **#59 (closed).** The maintainer writes: "Freshness checks flag source drift, not whether every affected claim is still true." That confirms there is no claim-level staleness. Candidate verification is tracked in #221.
- **#281 (open).** Asks for semantic concept matching (entity resolution); merging today is by slug only.
- **#194 (closed).** A shared page frozen by `rm` could never be rebuilt. It was fixed through the reconciliation and frozen-slug machinery.
- **#99 (closed).** Embedding batching and binary storage for 900+ pages.
- **CHANGELOG.** It describes reuse of extraction metadata for unchanged co-owners and prompt ordering that puts shared content first, so prefix caching can help (CHANGELOG lines ~299 and 448-458). Neither is LLM response caching.

## 11. Claim lineage, cost accounting, dry-run, caching

- **Claim-level lineage: NO (citations only).**
  - The page prompt asks the LLM to finish each paragraph with `^[file.md:START-END]` (`src/compiler/prompts.ts:198-205`). A post-processor repairs bare line numbers (`src/compiler/citation-normalize.ts`).
  - Lint and eval check these markers (broken source or out-of-bounds span; the docs call it a "claim-level citation rate").
  - These are LLM-written annotations inside the page body. They are not compiler-tracked claim nodes, and changes are never invalidated by citation span.
- **Cost and token accounting: opt-in receipts only.**
  - Setting `LLMWIKI_USAGE_DIRECTORY` and `LLMWIKI_USAGE_EXECUTION_ID` publishes one content-free JSON receipt per provider call, with input, output and cache token counts (`src/providers/usage.ts:41-66`; Anthropic at `src/providers/anthropic.ts:88,120,148,160-162`).
  - There is no cost estimate, budget, or per-page or per-source cost attribution.
  - `timeStage` records per-stage timings.
- **Dry-run and plan output: PARTIAL.**
  - `refresh --stale --dry-run` prints the recompiled pages, orphaned pages, changed owners, known-affected sources and skipped new sources, without needing an API key (`src/commands/refresh.ts:73-77,177-210`). It notes that "more may be found during the run" because of late discovery.
  - `rm --dry-run` and `status` (changed sources plus stale page counts, `src/status/collect.ts:182,322-355`) also exist.
  - **`compile` has no dry-run**, and no estimate of LLM call or token counts is printed anywhere.
- **LLM response caching: NO general cache.**
  - The only memoization is the **extraction snapshot**: per source, keyed by source-bytes hash plus a fingerprint of (PROMPT_VERSION, provider, model, prompt template, tool schema) (`src/compiler/extraction-snapshot.ts:47-96`). It keeps only the latest snapshot, is used only for *unchanged co-owners*, is turned off by any deletion or frozen slug, and the code says outright that it "is not a prompt-response cache" (`src/compiler/extraction-snapshot.ts:4`).
  - Page generation is never cached.
  - Provider prompt caching is not used: no `cache_control` in `src/providers/anthropic.ts`. The prompts are only ordered so that implicit prefix caching can help.
  - No temperature or seed is set for Anthropic or OpenAI; only Ollama uses `temperature: 0` (`src/providers/ollama.ts:89`). Output is therefore non-deterministic.
  - Embeddings do have chunk content-hash reuse (`src/utils/embeddings-chunks.ts:95-106`).

## Other notable gaps relevant to our design

1. **Seed and summary pages are always regenerated.** Overview, comparison and entity pages make one LLM call each per compile, even when nothing changed. Their declared dependency on `relatedSlugs` pages is never used for invalidation (`src/compiler/compile-finalize.ts:163-179`, `src/compiler/seed-pages.ts:81-110,173-212`). The code comment there says "Seed pages are cheap deterministic writes", but they call the LLM.
2. **Hidden prompt inputs are not part of any dependency key:** the full `index.md` in extraction prompts, the previous page body, the first 5 related pages, and the global link-target list. Reruns are path-dependent and outputs cannot be reproduced from tracked inputs.
3. **The co-owner closure is a whole connected component.** It is cheap only while snapshot reuse is enabled; any deletion disables reuse globally (see Q5).
4. **State-hash TOCTOU** (see Q2).
5. **Model or prompt-version changes do not mark pages stale.** They are recorded in frontmatter only, and freshness ignores them (`src/freshness/index.ts:45-60`).

## Bottom line for our proposal

llmwiki-compiler already ships the **source-hash → page ownership → stale/orphan → scoped recompile** loop, and does it carefully: a journalled, atomic, lock-protected pipeline with deletion reconciliation and reuse of saved extractions for unchanged co-owners. It does **not** have:

- claims or entities as tracked intermediate nodes;
- sub-file change detection;
- a multi-layer DAG (page → page or page → summary invalidation);
- input hashes per artifact;
- early cutoff;
- a general LLM response cache;
- a compile plan or cost estimate.

Its invalidation unit is "a source changed, so re-extract the whole source and regenerate every page it owns, with the full text of every co-owner". Our proposed claim-granular, content-addressed DAG with early cutoff would be a real step beyond it, not a re-implementation.
