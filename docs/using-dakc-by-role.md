# Using DAKC in a team: who does what

DAKC compiles a set of documents into a knowledge base: claims, entity pages,
topic summaries and an index. It keeps that knowledge base current as the
documents change, rebuilding only what each change invalidates. Most people
around a knowledge base never run DAKC. They write documents, read answers,
or review changes, and each role touches a different part of it.

This guide covers the roles that use DAKC directly or indirectly, and what
each one does with it. It also says who doesn't need it.

| Role | What they do with DAKC | How often | Main tools |
|---|---|---|---|
| [Docs maintainers](#docs-maintainers-and-document-owners) | Write the sources; check the impact of large edits | Every edit (mostly invisibly) | Their editor, the web UI, `compile --dry-run` |
| [Developers](#developers) | Build RAG apps, agents and tools on the knowledge base | While building and debugging | Python API, MCP server, publishers, `trace` |
| [CI and platform owners](#ci-and-platform-owners) | Run compiles automatically, cap cost, keep it healthy | Set up once, then on alerts | MkDocs plugin or a CI step, `verify`, `status`, `rollback` |
| [Solution architects](#solution-architects) | Decide whether and where DAKC fits; choose providers, sources and outputs | At design time, then occasionally | `dakc.toml`, the RFC, benchmark results |
| [Knowledge owners and reviewers](#knowledge-owners-and-reviewers) | Audit answers, review what a release changed, spot conflicts | Before releases, on complaints | Web UI, published knowledge pages, agent questions |
| [AI agents](#ai-agents) | Answer questions with citations; keep the knowledge base current | Continuously | MCP tools |

## One document change, end to end

Here is how the roles meet when a single sentence changes. A support policy
goes from "20 days" to "24 days" of annual leave.

1. **A document owner** edits the policy and opens a pull request, as usual.
2. **CI** (set up by the platform owner) comments on the pull request with
   the knowledge diff, using the [GitHub Action](integrations/github-action.md)
   or `dakc review`. The comment shows the one modified document, the old fact retracted and its
   replacement added, the few pages that stated it, and the exact LLM-call
   count. If the plan exceeds the team's budget, CI fails here instead of
   spending.
3. **On merge,** CI runs `dakc compile`. Only the affected passage is
   re-extracted, and only pages and summaries that stated the old fact are
   rewritten.
   - The wiki or the MkDocs site is updated.
   - Each publisher receives only the changes: the "20 days" claim is
     deleted from the vector index and the "24 days" claim is added.
4. **An AI agent** asked "how many days of leave?" answers "24", citing the
   policy document, version and sentence.
5. **A reviewer** checking the release opens History in the web UI. They see
   which facts and pages changed, and can follow any citation to its source
   sentence.

Nobody had to rebuild the knowledge base, and nobody had to remember which
summaries mentioned the old number.

---

## Docs maintainers and document owners

Technical writers, engineers who own docs pages, and owners of policies,
runbooks or ADRs.

**What changes for you: almost nothing.** You write documents as before.
Where the MkDocs plugin or a CI step is set up, the knowledge base follows
every merge automatically.

**Use DAKC when an edit is large or risky.**
- **Before you edit, see the blast radius.** In the web UI, open the document
  and its *Impact* tab, or run `dakc graph --source path/to/doc.md`. It lists
  every claim, page and summary that depends on the document.
- **Before you merge, see what the edit actually changes.** With the
  [GitHub Action](integrations/github-action.md), every pull request gets a
  comment listing the facts added and retracted (with their text) and the
  pages affected. Locally, `dakc review --base origin/main` gives the same
  report, and `dakc compile --dry-run` lists the passages re-extracted, the
  facts added or retracted, and the pages regenerated. Pages whose facts came out unchanged
  are marked "checked, unchanged". That is usually most of them.
- **Move and rename freely.** Renames are detected by content, so
  reorganising folders rebuilds nothing.

**Write so facts extract well.** These matter most with the free heuristic
extractor; an LLM provider is more forgiving.
- **Name the subject in each factual sentence.** Write "PostgreSQL supports
  JSONB", not "It supports JSONB". A claim must name at least one entity or
  concept to attach to a page.
- **State a fact once, in its canonical place.** The same fact in different
  words across documents becomes several claims: redundant, never wrong.
  `dakc duplicates` lists likely paraphrases worth consolidating.
- **When two documents disagree,** DAKC keeps both claims side by side on the
  page, each with its source. Treat that as a docs bug to resolve.
- **Two names for the same thing?** Ask whoever owns `dakc.toml` to add an
  alias (`"Postgres" = "PostgreSQL"`) so they share one page.

---

## Developers

Engineers building RAG applications, agents or internal tools on top of
documents.

**Getting started**

```bash
pip install 'dakc[mcp]'
dakc ingest ./docs            # creates dakc.toml and compiles ./docs (free heuristic provider)
dakc compile --dry-run        # after any edit: the exact rebuild, before anything runs
```

Switch `provider` in `dakc.toml` to `openai` (any OpenAI-compatible endpoint,
including Ollama), `anthropic`, or `custom` for better extraction and
writing. Run `compile --dry-run` first to see the cost.

**Integration patterns**

| You are building | Use | Why |
|---|---|---|
| An agent that answers from your docs | `dakc mcp` registered in Claude Code, Cursor or any MCP client ([docs/mcp.md](mcp.md)) | Current, citable knowledge, a cost-guarded compile, a read-only mode |
| A RAG app over a vector database | A publisher: the `jsonl` change feed today, or a plugin ([RFC 0001](rfcs/0001-sources-and-publishers.md)) | Re-embed only changed facts, delete retracted ones, keep source citations |
| A feature inside your Python app | `dakc.open(...)`: `read`, `trace`, `artifacts`, `plan`, `compile` ([docs/python-api.md](python-api.md)) | Typed results, one error hierarchy, safe concurrent reads during a build |
| Ingestion from an internal system | A source connector: a `list()` and a `read()` | Your system becomes a source, with stable identity and full-rebuild checks |
| A docs site with a knowledge base | The MkDocs plugin ([docs/integrations/mkdocs.md](integrations/mkdocs.md)) | One line in `mkdocs.yml`; incremental on every build |

**When an answer looks wrong**
- `dakc trace <page>` shows each displayed sentence, the claim behind it, and
  that claim's passage, source version and evidence sentence. Text with no
  citation was written by the model; `provenance = "strict"` removes it.
- `dakc explain <id>` shows what an artifact depends on, what depends on it,
  and whether it is stale and why.
- `dakc verify --against-full` rebuilds from scratch in a temporary store and
  checks it matches the incremental result. Run it freely with the free
  provider. With a paid provider it re-pays every call, so it needs an
  explicit `--allow-cost`.

**While iterating on prompts or models,** a changed model or prompt changes
the component's fingerprint. `dakc status` then lists it under *outdated*,
and the next compile regenerates exactly the affected layer. If the new
output is worse, `dakc rollback` restores the previous build.

---

## CI and platform owners

Whoever runs builds and keeps them healthy: DevOps, platform teams, or the
docs-infrastructure owner.

**Set up once**
- **Choose an entry point.** Use the MkDocs plugin for an MkDocs site, or a
  plain `dakc compile` step for anything else. Add the
  [GitHub Action](integrations/github-action.md) for a knowledge diff on every
  pull request; it never spends on a model.
  [docs/integrations/mkdocs.md](integrations/mkdocs.md) has a GitHub Actions
  workflow.
- **Cache the workspace state** (`.dakc/` or the plugin's workspace directory)
  between CI runs. Without the cache every build starts from scratch.
  Correctness never depends on the cache, only cost does.
- **Cap the cost.**
  - In the MkDocs plugin, `max_llm_calls` fails the build instead of
    overspending.
  - In CI scripts, compare `dakc compile --dry-run --json` against a budget
    before compiling.
  - Over MCP, `--max-llm-calls` (default 100) refuses oversized agent builds.
- **Provide secrets as environment variables.** API keys never go in
  `dakc.toml`.
- **Add gates.**
  - `dakc verify` (invariants and provenance) on every build.
  - `dakc verify --against-full` on a schedule, with the free provider.

**Day to day**
- **Monitoring.** `dakc status --json` reports source changes and an
  `outdated` list: DAKC upgraded, configuration changed, or provider changed
  since the last build.
- **Upgrading DAKC.** Run `dakc compile --dry-run` first. A new version can
  change a layer's logic, and the plan shows what that costs. Upgrade notes
  in [CHANGELOG.md](../CHANGELOG.md) say what each release rebuilds.
- **Publishing failures.** A target that is down doesn't fail the build. Its
  changes stay pending, and `dakc publish` retries them (`--dry-run` shows
  what is pending).
- **Recovering.**
  - `dakc rollback --to N` restores an earlier build, the wiki and the
    publishers.
  - Builds are atomic, so a crash leaves the last good build intact.
  - Back up by copying `.dakc/dakc.db`.

**Limits to plan around**
- **One host, one writer.** Concurrent compiles on one machine are serialized
  by a lock (`--wait` queues instead of failing). There is no multi-host mode.
- **No authentication on HTTP.** `dakc serve` and HTTP MCP listen on this
  machine only. Don't put them behind a public proxy.
- **Sources fail closed.** If a source cannot be listed, or a directory goes
  missing, the build fails and changes nothing. That is deliberate, but it
  will page you.

---

## Solution architects

Whoever decides whether DAKC belongs in a system, and how it connects.

**Is it a fit?**

| Good fit | Poor fit |
|---|---|
| Documents change often, and derived knowledge (pages, summaries, extracted facts) must follow | A small or static corpus: a full rebuild is cheap enough |
| LLM cost of keeping knowledge current matters | Plain "search my PDFs": chunk-level RAG with a re-index is simpler |
| Answers must be traceable to a source sentence and version | No provenance or audit requirement |
| Several consumers (agents, a docs site, a vector database) need the same knowledge | Multi-host writes, or per-user permissions, needed today |

**Where it sits.** Sources go into DAKC, and compiled knowledge goes to its
consumers. DAKC does not replace your vector database or your retrieval: it
keeps what those consume correct.

```text
documents, repos, file shares ──► source connectors ──► DAKC ──► publishers ──► vector DB, change feed
                                                         │
                                                         ├──► Markdown wiki or MkDocs site
                                                         └──► MCP server, Python API, REST, web UI
```

**Decisions to make**
- **Provider.**
  - The free heuristic extractor costs nothing and has crude quality.
  - An LLM provider gives better pages at a cost you can predict per change
    (`--dry-run`).
  - Extraction results are cached by content, so re-running an unchanged
    passage is free.
- **Sources.** One or more `[[sources]]`, each under its own path prefix. The
  filesystem connector ships; other systems need a connector.
- **Outputs.** Which `[[publishers]]` receive which kinds of artifact. Claims
  suit retrieval, pages and summaries suit humans.
- **Provenance mode.** `provenance = "strict"` renders only sentences that
  cite a claim. Use it where unsupported model text is unacceptable.
- **Audiences.** Permissions don't yet propagate into derived pages. Keep one
  knowledge base per audience until they do (RFC 0001, phase 3).

**Non-functional facts**, from the [benchmark](benchmark.md) and the
[FastAPI replay](../examples/fastapi-docs/README.md):
- **Incremental cost grows with the change, not the corpus.** One edited
  document out of 20,000 is 6 LLM calls, against 190,964 for a full rebuild.
- **Scale tested:** 20,000 documents, 329,123 claims, 1.9 million dependency
  edges, in a single SQLite file.
- **Governance:**
  - every build is in history;
  - any two builds can be diffed;
  - any build can be restored;
  - every output has a provenance chain back to a source version;
  - incremental results can be proven equal to a full rebuild.

---

## Knowledge owners and reviewers

Product managers, support leads, team leads and compliance: people
responsible for *what the knowledge says*, who don't operate the tool.

You don't need the CLI. Your access points:
- **The published knowledge pages.** With the MkDocs plugin, these live on
  the docs site under `/knowledge/`, and every cited fact links back to its
  docs page.
- **The web UI.** It is read-only and local, so someone runs `dakc serve`;
  there is no hosted, shared instance yet.
  - *Changes*: what the next compile would redo.
  - *History*: what each build changed.
  - Any page: select a citation to see the exact source sentence.
- **An AI agent connected over MCP.** Ask "where does this answer come from?"
  and it traces the answer to its source.

**What you'd use it for**
- **Before a release:** check that the knowledge reflects the release. The
  History view lists the facts, pages and summaries the build changed.
- **When an AI answer is challenged:** follow the citation to the document,
  version and sentence. Then you know whether the document or the model is
  wrong.
- **Docs hygiene:**
  - Conflicting statements appear side by side on a page, each with its
    source.
  - Likely duplicates are listed by `dakc duplicates`; ask a developer.
- **Scope:** decide with the architect which document sets feed the knowledge
  base, and which outputs (assistant, site, vector index) receive it.
- **Cost:** every build records its LLM calls, shown in the *History* view.

---

## AI agents

Agents are direct users, through the MCP server. The server tells them how to
work on connect:

- **Answering a question:** `list_artifacts` → `read_artifact` → `trace`,
  citing sources.
- **Before editing a document:** `downstream` shows what the edit would
  invalidate.
- **After documents change:** `status` → `compile` (a preview) →
  `compile(apply=true)`. A build planned over the cost guard is refused.

Run it with `--read-only` for agents that should only read, which removes
`compile` and `rollback` entirely. `rollback` always needs an explicit build
id and is marked destructive, so clients that confirm destructive actions ask
first.

---

## Who doesn't need DAKC

- **End users of an assistant** built on DAKC. They see answers and
  citations, not the tool.
- **Teams with a small corpus that rarely changes.** A full rebuild now and
  then is simpler.
- **"Search my PDFs" chatbots** with no derived pages, summaries or
  provenance requirement. A plain RAG pipeline is enough.
- **Reviewers as operators.** PMs and compliance shouldn't have to learn the
  CLI. If they find themselves running it, set up the plugin, CI and the web
  UI for them instead.

## Quick reference

| Task | Who | How |
|---|---|---|
| See what an edit affects before making it | Docs maintainer | Web UI *Impact* tab, or `dakc graph --source <doc>` |
| See exactly what the next compile does and costs | Anyone | `dakc compile --dry-run`, or the web UI *Changes* view |
| See what a pull request does to the knowledge base | Reviewer, docs maintainer | The GitHub Action's comment, or `dakc review --base origin/main` |
| Rebuild after changes | CI, developer, agent | `dakc compile`, the MkDocs build, MCP `compile(apply=true)` |
| Find where a sentence came from | Developer, reviewer | `dakc trace <page>`, a citation in the web UI, MCP `trace` |
| Check the knowledge base is correct | CI, developer | `dakc verify`, `dakc verify --against-full` |
| See what changed between builds | Reviewer, CI | Web UI *History*, `dakc diff A B` |
| Undo a bad build | CI or platform owner | `dakc rollback --to N` |
| Find duplicate facts | Docs maintainer | `dakc duplicates` |
| Retry delivery to a vector DB or feed | CI or platform owner | `dakc publish` |
| Know whether a compile is needed | CI, agent | `dakc status` (source changes plus `outdated`) |
