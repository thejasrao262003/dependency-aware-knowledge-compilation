# DAKC in an existing MkDocs site

Add one line to `mkdocs.yml`. Nothing else in your docs workflow changes:
your Markdown, theme, nav and deployment stay as they are.

```bash
pip install 'dakc[mkdocs]'
```

```yaml
plugins:
  - search
  - dakc
```

On every `mkdocs build`, and on every live reload of `mkdocs serve`:

- DAKC compiles a knowledge base from your `docs_dir`, **incrementally**.
  Only what your edits invalidated is rebuilt.
- The knowledge base is published under `/knowledge/`: one page per entity,
  one summary per topic folder, and an index.
- **Every fact links back to the docs page it came from.**

What the build log shows (real output on `examples/sample-knowledge-base`):

```text
INFO    -  dakc: build 1: 21 passage(s) re-extracted, 25 page(s) regenerated, 0 checked and unchanged, 0 LLM call(s)
INFO    -  dakc: knowledge base up to date (nothing to rebuild)                      # next build, no edits
INFO    -  dakc: build 3: 1 passage(s) re-extracted, 2 page(s) regenerated, 3 checked and unchanged, 0 LLM call(s)
```

The last line follows a one-sentence edit. Two pages had to change, three
more were checked and left exactly as they were, and nothing else was touched.

## Options

```yaml
plugins:
  - dakc:
      workspace: .dakc-kb      # DAKC's state, next to mkdocs.yml (keep it out of docs_dir; cache it in CI)
      provider: heuristic      # used when the workspace is first created
      publish: true            # add the generated pages to the site
      prefix: knowledge        # where they appear
      min_claims: 2            # publish entity pages backed by at least this many facts
      max_llm_calls: 200       # with an LLM provider: fail the build if a compile is planned to exceed this
      verify: false            # fail the build if DAKC's invariant checks fail
      enabled: !ENV [DAKC_ENABLED, true]
```

- **Workspace.** On the first build the plugin creates `.dakc-kb/dakc.toml`,
  pointing at your `docs_dir`. Edit that file to configure a provider, for
  example an LLM for cleaner pages, or entity aliases. API keys come from
  environment variables, as with the CLI.
- **Which pages are published.** Entity pages backed by fewer than
  `min_claims` facts (default 2) stay in the knowledge base for agents and the
  CLI, but are not published. List entries that would link to them are dropped,
  and other links to them become plain text, so `--strict` still passes. Set
  `min_claims: 1` to publish everything.
- **Nav.** Generated pages are kept out of the nav by default, so MkDocs does
  not list thousands of them. Link the index from your nav if you want:
  `nav: [..., {Knowledge: knowledge/index.md}]`.
- **Footnotes.** Citations use Markdown footnotes. The plugin enables the
  `footnotes` extension if your config does not already have it.
- **Strict mode.** `mkdocs build --strict` passes: generated pages contain no
  broken links.

## In CI

The knowledge base state lives in the workspace directory. Cache it, and each
CI build is incremental: a typical docs PR costs a few re-extracted
paragraphs, not a full rebuild.

```yaml
name: docs
on: [push, pull_request]
permissions:
  contents: read
jobs:
  docs:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
      - uses: actions/setup-python@6d4fd22b20465ccc0f7354476588e8df7c9ca0e0 # v7.1.0
        with:
          python-version: "3.12"
      - run: pip install mkdocs-material 'dakc[mkdocs]'
      - uses: actions/cache@55cc8345863c7cc4c66a329aec7e433d2d1c52a9 # v6.1.0
        with:
          path: .dakc-kb
          key: dakc-${{ github.ref_name }}-${{ github.sha }}
          restore-keys: |
            dakc-${{ github.ref_name }}-
            dakc-main-
      - name: What this change does to the knowledge base
        run: |
          if [ -f .dakc-kb/dakc.toml ]; then
            { echo '```'; dakc compile --dry-run -w .dakc-kb; echo '```'; } >> "$GITHUB_STEP_SUMMARY"
          fi
      - run: mkdocs build --strict
```

Correctness does not depend on the cache. DAKC compares content hashes against
whatever state it restores and rebuilds everything that differs. A stale or
missing cache only means more work, never a wrong result. `dakc verify
--against-full` checks this, and the replay of FastAPI's history in
[examples/fastapi-docs](../../examples/fastapi-docs/README.md) checks it at
every commit.

## Build time

DAKC's own work in a build is small. Every published page, though, is one
more page for MkDocs to render. Measured on FastAPI's 159-page docs with
Material ([details](../../examples/fastapi-docs/README.md#fastapis-site-with-the-mkdocs-plugin)):

| Build | Time |
|---|---|
| No plugin | 3.4 s |
| `publish: false` (knowledge base compiled, not published) | 3.7 s |
| Default, `min_claims: 2` (945 pages published) | 12.6 s |
| `min_claims: 1` (2,336 pages) | 20.2 s |

For a fast `mkdocs serve` on a large site, raise `min_claims` or set
`publish: !ENV [DAKC_PUBLISH, false]` locally. Keep publishing on in CI.

## Agents read the same knowledge base

The workspace the plugin maintains is an ordinary DAKC workspace. Point the
MCP server at it, and agents answer from the same knowledge your site
publishes, with sentence-level provenance:

```bash
claude mcp add docs-kb -- dakc mcp --workspace /path/to/repo/.dakc-kb --read-only
```

## Cost

- **With the default heuristic provider**, a build makes no network calls and
  costs nothing.
- **With an LLM provider**, the first build extracts every paragraph. After
  that, each build pays only for changed paragraphs and the pages they affect.
  Set `max_llm_calls` so a large change, or a cold cache in CI, fails the build
  instead of spending unexpectedly. To preview the cost of a change, run
  `dakc compile --dry-run -w .dakc-kb`.
