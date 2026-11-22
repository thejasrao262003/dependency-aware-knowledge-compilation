# DAKC in action: keeping FastAPI's documentation compiled

This example uses FastAPI's real English documentation (159 pages, MIT
licensed) and its real Git history. It shows what it costs to keep an
AI-generated knowledge base of a busy docs repository current, and that the
incremental result stays correct.

Everything here is reproducible. The scripts clone FastAPI's docs at pinned
commits, so nothing of FastAPI's is copied into this repository.

## 50 real commits, replayed

The last 50 commits that changed `docs/en/docs` were replayed in order. At
every commit we measured three strategies, all running in the same engine:

- **Full rebuild.**
- **File-level re-processing**: re-process every changed file, which is what
  most LLM-wiki tools do.
- **DAKC.**

| | LLM calls | Pages regenerated | Equal to a full rebuild |
|---|---|---|---|
| Full rebuild at every commit | 688,168 | — | (the reference) |
| Re-process changed files | 267,896 | 72,272 | |
| **DAKC** | **259** | **100** | **yes, at all 50 commits** |

Read this table with its context. Full detail is in
[results/replay.md](results/replay.md).

- **Release notes.** 46 of the 50 commits only append a line or two to
  `release-notes.md`, an 8,000-line file.
  - Re-processing that file costs about 5,800 LLM calls each time.
  - DAKC re-extracts the new paragraph: 0 to 6 calls.
  - This pattern is common: changelogs, decision logs, meeting notes.
- **Content edits.** On the 4 commits that edit docs pages, DAKC made 127 calls
  where file-level re-processing made 658. A commit that is mostly new text
  costs about the same under any strategy (33 vs 34), because new text has to
  be read.
- **Tokens.** Calls are not the whole cost. DAKC used about 14 times fewer
  input tokens than file-level re-processing, not 1,000 times fewer. Most of
  its tokens go to regenerating one large topic summary that lists about
  1,660 pages (see Limitations in the main README).
- **Provider.** Counts come from DAKC's deterministic mock provider. Calls are
  exactly what each strategy would send to an LLM; tokens are estimated.

## One commit, step by step

[results/walkthrough.md](results/walkthrough.md) has every command and its
real output. It follows fastapi/fastapi@b0a616d9, which adds a section on form
parsing limits to one docs page:

1. **Compile the 159 pages.** That is 11,468 passages, 11,060 claims and
   2,334 entity pages; a full build is 13,808 LLM calls.
2. **Ask** what happens when a form exceeds FastAPI's parser limits. There is
   nothing yet.
3. **Apply the commit**: 1 file changed, 16 lines added.
4. **Preview with `dakc compile --dry-run`**, before anything runs:

   ```mermaid
   flowchart LR
     D["advanced/request-body-limits.md<br/>1 of 159 documents changed"] --> P["8 passages re-extracted<br/>of 11,468"]
     P --> C["5 new claims<br/>0 retracted"]
     C --> G["9 pages regenerated<br/>fastapi, starlette, http, form, file, uploadfile …"]
     G --> S["3 topic summaries"] --> I["index"]
     U["2,325 other pages<br/>never touched"]
   ```

   **20 LLM calls, against 13,808 for a full rebuild.**
5. **Compile.** Exactly that plan runs.
6. **Ask again.** "Exceeding a parser limit returns HTTP 400." `dakc trace`
   shows the exact sentence, document and version.
7. **`dakc verify --against-full`**: identical to a from-scratch rebuild.

## An agent doing it over MCP

[results/agent-session.md](results/agent-session.md) records two real sessions.
In each, Claude Code had only DAKC's MCP tools: no shell and no file access.

- **Before the commit**, the agent searched, found the body-size limit
  documentation, and said plainly that the knowledge base had nothing on
  limiting the number of files.
- **After pulling the commit**, it ran `status`, then `check_sources`, then
  `compile` to preview (20 LLM calls), then `compile(apply=true)`. Then it
  answered with `max_files` (default 1,000) and "exceeding a parser limit
  returns HTTP 400", citing `advanced/request-body-limits.md`.

Headless runs cannot pause for approval, so this agent previewed and then
applied. In an interactive session it shows you the preview first.

## FastAPI's site, with the MkDocs plugin

[mkdocs.yml](mkdocs.yml) builds FastAPI's real docs with one added plugin line.
The knowledge base is published under `/knowledge/`, and every cited fact
links back to its docs page. Measured on a laptop (Apple M5), with Material:

| Build | Time |
|---|---|
| No plugin | 3.4 s |
| Plugin, `publish: false` (knowledge base compiled for agents and CI, not published) | 3.7 s |
| Plugin, default (`min_claims: 2`, 945 knowledge pages published) | 12.6 s |
| Plugin, `min_claims: 1` (all 2,336 pages) | 20.2 s |

DAKC's own work in an up-to-date build is 0.27 s. The rest is MkDocs rendering
the extra pages. FastAPI's own `mkdocs.yml` adds their custom hooks, which this
minimal config leaves out, so some code-include directives show as plain
text. The knowledge pages are unaffected.

## Reproduce

```bash
pip install 'dakc[mcp,mkdocs]' mkdocs-material
python examples/fastapi-docs/replay.py          # 50 commits, several minutes; writes results/replay.json
python examples/fastapi-docs/report.py          # results/replay.md
python examples/fastapi-docs/walkthrough.py     # results/walkthrough.md
python examples/fastapi-docs/agent_session.py   # results/agent-session.md (needs the `claude` CLI; uses your Claude usage)
mkdocs serve -f examples/fastapi-docs/mkdocs.yml
```

To record a demo, see [RECORDING.md](RECORDING.md).

## Limitations of this example

- **The free extractor.** All runs use the free heuristic extractor, through
  the mock provider so that calls are counted. It makes noisy entity pages
  (`page:exceeding`), and it skips sentences that name no capitalized term:
  the `max_files` bullet stayed a passage, not a claim. An LLM provider gives
  cleaner pages. The invalidation behaviour and the correctness checks do not
  depend on the extractor.
- **One machine.** The timings come from a single laptop.

FastAPI and its documentation are © Sebastián Ramírez, under the MIT License.
