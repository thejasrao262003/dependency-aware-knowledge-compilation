# GitHub Action: a knowledge diff on every pull request

The action comments on each pull request with what the change does to your
compiled knowledge base, before anything is compiled or spent:

> **1 document changed.** Compiling this change would:
>
> - re-extract **1 passage** of 21
> - add **1 fact** and retract **1 fact** (3 facts keep their text but change sources)
> - regenerate **2 pages** of 25
> - cost **3 LLM calls**, about 356 input and 202 output tokens (≈ $0.0005 with gpt-5-mini)
> - leave everything else untouched: 23 pages
>
> ▸ Facts added (1): PostgreSQL stores JSONB in a binary format. <sub>databases/postgresql.md</sub>
>
> ▸ Facts retracted (1): ~~PostgreSQL supports JSONB.~~ <sub>databases/postgresql.md</sub>
>
> ▸ Pages regenerated (2) · ▸ Documents (1)

That is the real output for a one-phrase edit to the example corpus
(`examples/sample-knowledge-base`). The lists are collapsible sections in the
comment. A reviewer sees exactly which facts change, and the old wording is
struck through. A text diff spread across several files never makes that
obvious.

When a change makes two documents disagree, the comment says so and expands
the list (RFC 0002):

> - **introduce 1 conflict between documents** (below)
>
> ▾ Conflicts introduced (1)
> - PostgreSQL supports JSONB: **is negated** <sub>architecture.md</sub> vs **is stated** <sub>databases/postgresql.md</sub> <sub>`cfl:7e6b362bd2`</sub>

That is the real output when `architecture.md`, saying "PostgreSQL does not
support JSONB.", is added to the example corpus.

A later change that makes them agree again is reported as "resolve 1
conflict". Differences explained by release, time or environment are not
reported, and neither are conflicts accepted in `dakc.toml`.

## Setup

```yaml
# .github/workflows/knowledge-diff.yml
name: knowledge diff
on: pull_request

permissions:
  contents: read
  pull-requests: write       # to post the comment

jobs:
  knowledge-diff:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          fetch-depth: 0     # the base commit must be available
      - uses: thejasrao262003/dependency-aware-knowledge-compilation@v0.7.0
        with:
          workspace: .       # the directory holding dakc.toml
          price: |           # optional: adds a dollar estimate per model
            gpt-5-mini:0.25:2.00
```

That is all. The workspace is your existing `dakc.toml`: same sources,
aliases and provenance mode. With the MkDocs plugin, point `workspace` at the
plugin's workspace directory (`.dakc-kb` by default). This repository reviews
its own pull requests this way: see `.github/workflows/knowledge-diff.yml`.

## Inputs and outputs

| Input | Default | Meaning |
|---|---|---|
| `workspace` | `.` | Directory holding `dakc.toml` |
| `base` | the PR's base commit | Git ref to compare with (needed outside `pull_request` events) |
| `price` | none | `LABEL:INPUT:OUTPUT` per line, in USD per million tokens |
| `comment` | `true` | Post the report as a PR comment, updated in place on later pushes |
| `limit` | `25` | Items listed per section |
| `dakc-version` | the action's own ref | A PyPI version to run instead |
| `github-token` | `github.token` | Token for the comment |

The `has-changes` and `llm-calls` outputs let later steps act on the result,
for example requiring a label when `llm-calls` exceeds a budget. The report is
always written to the job summary too.

## How it works, and what it costs

1. **The base build.** The action compiles the sources as of the base commit
   into a scratch store, kept in the Actions cache. After the first run this
   is incremental and takes seconds.
2. **The head build.** It compiles the pull request's version into a copy of
   that store.
3. **The report.** It diffs the two builds: facts by claim id, pages by
   regeneration, documents by content hash.

Both builds use DAKC's **deterministic mock provider**. The review never calls
or pays a model, even when your workspace is configured with one. The call
count is what an LLM provider would make for the facts the deterministic
extractor finds. An LLM extracts somewhat different facts, so read the calls
and dollars as an estimate of scale, not an invoice. Your own knowledge base
(`.dakc/dakc.db`) is never touched.

Locally, the same report:

```bash
dakc review --base origin/main                  # Markdown to stdout
dakc review --base origin/main --format json    # for scripts
```

## Limits

- **Only sources inside the repository are compared.** Filesystem sources
  under the repository are read at both commits. Sources elsewhere (another
  directory, or a remote connector) are identical on both sides, so they
  never show as changed.
- **Fork pull requests** get a read-only token. The action then writes the
  report to the job summary and logs a warning instead of failing.
