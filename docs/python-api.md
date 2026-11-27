# Python API

`import dakc` gives the whole compiler as a library. The CLI (`dakc ...`), the
REST API (`dakc serve`) and the MCP server (`dakc mcp`) are thin layers over
the same module, `dakc.kb`. A behaviour reachable from one is reachable from
all of them.

## Opening a knowledge base

```python
import dakc

kb = dakc.open("path/to/workspace")       # needs dakc.toml; raises dakc.WorkspaceNotFound
kb = dakc.init("path", source_dir="docs") # creates dakc.toml if missing (an existing one is kept)
report = dakc.ingest("path/docs", "path") # init for that directory if new, then compile
```

`KnowledgeBase` is a context manager. It holds one SQLite connection, which
`close()` or the end of a `with` block releases. Use one instance per thread.
Instances are cheap to open.

## Methods

| Method | Returns | Notes |
|---|---|---|
| `status()` | `Status` | `head`, `counts`, `up_to_date`, `source_changes`, `stale` (artifacts per kind downstream of changed sources), `conflicts_open`, and `outdated`: reasons a compile has work besides source changes (DAKC's logic, the configuration, the conflict scope rules or the provider changed since HEAD, or the conflict verifier has pairs left to judge). `up_to_date` is true only when there are neither. |
| `check(root=None)` | `SourceChanges` | new / modified / deleted / renamed / unchanged, by content hash |
| `plan(full=False)` | `BuildReport` | Dry run. Nothing is written or spent. `has_work()` and `llm_calls()` tell you whether a compile is needed and what it costs. Exact for free providers. For LLM providers, extraction calls are exact and page and summary counts are an estimate (`report.exact` is false). |
| `compile(full=False, wait=0.0)` | `BuildReport` | Takes the build lock, commits atomically, updates the wiki files, then runs the configured publishers. `report.publications` has one result per publisher; a failing publisher does not fail the build. `report.projection` counts the wiki file operations (see [Wiki files](#wiki-files)). Raises `BuildLocked` or `CompileFailed`. |
| `artifacts(kind=None, query=None, limit=100)` | `list[ArtifactSummary]` | Ordered by layer, then id. `query` is a case-insensitive substring of the title, or of the full text for claims and passages |
| `read(artifact)` | `ArtifactContent` | Rendered Markdown for pages, summaries and the index. Text for claims and passages. For a source document, its current text on disk, with `metadata['changed_since_build']`. For a page, `metadata['claim_ids']` follows its citation numbers, and `metadata['history']` (present when not empty) lists the claims shown under History: those whose every document a `[[conflicts.scope]]` rule declares past. A claim's `metadata['supports']` give each supporting passage with its `evidence_sentences` and their `refs`; a passage's `metadata['refs']` are those of every sentence (see *Comments after a sentence*). |
| `explain(artifact)` | `Explanation` | Dependencies, dependents, upstream sources, signatures, history, `VALID` / `STALE` / `DELETED` with reasons. `STALE` means a source upstream changed. That is an upper bound: `plan()` shows whether the artifact actually changes. |
| `trace(artifact)` | `PageTrace` or `ClaimTrace` | Each page sentence, the claims it cites, and each claim's passage, source version and evidence sentences. Each evidence entry's `refs[i]` lists the comments written after `sentences[i]` |
| `downstream(artifact)` | `Downstream` | Everything an edit to this source or artifact would invalidate, by kind |
| `graph_stats()` | `dict` | Artifact and edge counts |
| `export_graph(fmt="json", around=None)` | `str` | `json` (node-link), `dot`, or `reactflow` |
| `builds(limit=20)` | `list[BuildRecord]` | Newest first: committed, noop, failed, rolled_back |
| `diff(a, b=None)` | `BuildDiff` | Artifacts created / updated / deleted between two builds (`b` defaults to HEAD) |
| `duplicates(limit=None)` | `list[DuplicatePair]` | Advisory. Claims are never merged. |
| `links(path=None)` | `list[BrokenLink]` | Links between documents that point at a document or heading that no longer exists, each with `path`, `line`, `href`, `reason` and, when DAKC saw the rename that broke it, a `suggestion`. With `path`, only links in that document or pointing at it. |
| `linked_from(path)` | `list[dict]` | Every link in another document that points at `path`, working or not: who a rename of it, or of its headings, affects. |
| `conflicts(scoped=False, accepted=False)` | `list[Conflict]` | Open conflicts between sources at HEAD (RFC 0002): `kind` is `conflict` (found by the rules) or `possible` (judged by the optional verifier). `scoped=True` adds differences explained by time, release, environment or status. `accepted=True` adds those accepted in `dakc.toml`. Advisory: nothing is changed. |
| `conflict(id)` | `Conflict` | One finding: each value with its `locations` (path, `line` in the source on disk, heading, scope, claim and the source's own `evidence` sentence), and `affected` pages and summaries. `id` is `cfl:…`, a unique prefix, or `conflict:…`. |
| `verify(against_full=False, allow_cost=False)` | `VerifyReport` | Invariants. `against_full` also compares with a full rebuild; with an LLM provider it needs `allow_cost=True` (`CostNotAllowed` otherwise). The check `wiki files match the store` reads every wiki file and lists drifted ones (`metrics['wiki_files_drifted']`) without repairing them. |
| `rollback(to=None, wait=0.0)` | `RollbackResult` | Takes the build lock. Destructive: later artifact versions are discarded, though their build records remain. Publishers are brought back too (`.publications`), and the wiki is repaired like a compile's (`.projection`). |
| `sources()` | `list[SourceInfo]` | The configured sources: name, type, path prefix, location, capabilities, documents at HEAD |
| `publish(name=None, dry_run=False, reset=False, wait=0.0)` | `list[PublishResult]` | Sends each publisher what its ledger has not acknowledged. `dry_run` only counts; `reset` sends everything again. A failure is returned (`.error`, `.pending`), not raised. |

`artifact` may be:

- an id (`page:postgresql`, `clm:…`, `sum:databases`);
- a friendly form (`page:PostgreSQL`, `entity_postgresql`);
- a source path (`databases/postgres.md`);
- a page title (`PostgreSQL`).

A path written relative to another directory (`research/notes.md` for the source `notes.md`) resolves when exactly one source matches it. When nothing matches, `ArtifactNotFound` suggests the closest source paths and page titles.

Every result has `to_dict()`, which returns JSON-safe data. That is what
`--json`, the REST API and the MCP tools emit. `Downstream.to_dict` and
`BuildDiff.to_dict` take a `limit`.

## Wiki files

The wiki (`output_dir`, `wiki/` by default) is build output. Each compile
brings it in line with HEAD, and so does a no-change compile, a failed
compile and a rollback. `projection` (on `BuildReport`, `RollbackResult` and
`CompileFailed`) has:

| Key | Meaning |
|---|---|
| `written` | files written because their page, summary or index changed |
| `repaired` | files restored because they were edited or deleted by hand although their artifact did not change |
| `repaired_paths` | those files, sorted, relative to the wiki directory (`pages/jsonb.md`) |
| `deleted` | files removed because their artifact no longer exists |
| `unchanged` | files left as they were |

`written + repaired + unchanged` is the number of wiki files. A file is read
only when its size or mtime differs from what DAKC recorded when it last wrote
or checked it, so a repair pass over an unchanged wiki costs one `stat` per
file. Only files listed in `.dakc/projection.json` (the files DAKC wrote) are
repaired or deleted; your own files in the wiki directory are left alone. To
change a page, change its sources: a hand edit is overwritten by the next
compile.

## Comments after a sentence

In Markdown paragraphs and list items, the HTML comments written after a
sentence are kept as that sentence's metadata, its `refs`. Tools that write
receipts into their notes use them, Reckon for example:

```markdown
- The refresh endpoint takes a per-user Redis lock with a 5-second TTL. <!-- t:3f9a12c4 -->
- Use Argon2 for password hashing. <!-- rs_01JDQ8X4Z7K3M9P2Q5R8S1T4V6#t:3f9a12c4 -->
```

```python
claim = kb.trace("clm:…")                 # or a page: kb.trace("page:argon2").claims
for ev in claim.evidence:
    for sentence, refs in zip(ev.sentences, ev.refs):
        print(ev.source_path, sentence, refs)   # ... "Use Argon2 for password hashing." ['rs_01JDQ8X4Z7K3M9P2Q5R8S1T4V6#t:3f9a12c4']

kb.read("clm:…").metadata["supports"]     # [{"passage_id", "text", "evidence_sentences": [1], "refs": [["t:3f9a12c4"]]}, ...]
```

- `refs` holds comment bodies, trimmed: `<!-- t:3f9a12c4 -->` gives `"t:3f9a12c4"`.
  A sentence without comments has `[]`.
- A comment belongs to the sentence its preceding text ends in: the one it
  follows, or the one it sits inside. Several comments after one sentence are
  kept in order.
- Comments in headings and table cells are dropped. So is a line that starts
  with `<!--`: CommonMark reads it as an HTML block, and DAKC never reads the
  text of HTML blocks. Write the comment after the sentence, on its line.
- YAML front matter (`---` ... `---` at the top of a Markdown file) is skipped.
- Comments are not text. They are outside passage ids, text hashes and the
  extraction cache, and no claim, page or summary is built from them. Adding,
  rewriting, moving or removing a comment makes no provider call and rewrites
  no page: the compile writes new passage versions (`passage_refs_changed` in
  the report) and nothing else.

## Splitting text into sentences

`dakc.split_sentences(text) -> list[str]` is the splitter DAKC itself uses.
Claim evidence (`evidence_sentences`), sentence reuse and comment `refs` are
all indices into its result for a passage's text, so use it to map them back
to sentences:

```python
passage = kb.read("psg:…")
dakc.split_sentences(passage.text)        # ["Redis holds the Sessions.", "Postgres holds the Users."]
```

It splits after `.`, `!` or `?` and whitespace, before an uppercase letter, a
digit, a quote or an opening bracket. Part of the public API since 0.8; the
splitting rules are versioned by PARSER (`dakc.versions.PARSER`), so a change
to them re-parses every source.

## Errors

All errors derive from `dakc.DakcError`. Each one also derives from the
matching built-in, so existing `except` clauses keep working.

| Error | Also a | Raised when |
|---|---|---|
| `WorkspaceNotFound` | `FileNotFoundError` | no `dakc.toml` |
| `ArtifactNotFound` | `LookupError` | no artifact or source matches |
| `InvalidArgument` | `ValueError` | unknown strategy, kind or format; `trace` on something that is not a page or claim; a missing build |
| `BuildLocked` | `RuntimeError` | another process is compiling or rolling back (pass `wait=` to queue) |
| `CompileFailed` | `RuntimeError` | a build failed. Nothing was committed. `.preserved` is True when HEAD still points at the previous build, and `.head` is that build. The wiki is repaired to HEAD; `.projection` has its file operations, or is None if the repair itself could not run (the next compile does it). |
| `CostNotAllowed` | `PermissionError` | a full-rebuild comparison with a paid provider without `allow_cost=True` |
| `RollbackError` | `ValueError` | the target is not a committed build, or is already HEAD |
| `SourceError` | `RuntimeError` | a source is misconfigured, could not be listed or read, or its plugin could not be loaded. During a compile it surfaces as `CompileFailed` and nothing is committed. |
| `PublishError` | `RuntimeError` | a publisher is misconfigured or its plugin could not be loaded. It is raised before a compile starts. |

## Concurrency

`compile` and `rollback` hold the workspace build lock (`.dakc/build.lock`)
for the whole build, so builds from several processes, threads or tools are
serialized.

Read methods never take the lock. SQLite's WAL mode gives them a consistent
snapshot while a build runs. This is what lets an agent or the REST API keep
answering during a compile.

## Using DAKC's clients from your own code

`dakc.providers.make_client(name, options=None)` builds the model client of a
provider, and nothing else: no workspace, no cache. Use it when your application
calls the same model as its knowledge base, configured the same way.

```python
from dakc.providers import ProviderError, make_client

client = make_client("anthropic", {"model": "claude-haiku-5-5", "api_key_env": "MYAPP_ANTHROPIC_KEY"})
try:
    reply = client.complete("Answer in one sentence.", "What does a write-ahead log protect against?")
except ProviderError as exc:
    if not exc.transient:   # a rejected request, a missing key, a refusal: asking again will not help
        raise
    ...                     # a rate limit, server error, timeout or lost connection: try again later
else:
    print(reply.text, reply.input_tokens, reply.output_tokens)
```

- **`name`**: `claude-code`, `anthropic` (or `claude`), an OpenAI-compatible
  preset (`openai`, `gemini`, `grok`, `deepseek`, `mistral`, `groq`,
  `openrouter`, `together`, `ollama`, `lmstudio`, or an alias such as `xai`),
  or `custom`. `heuristic` and `mock` have no model client. Any other name raises
  `ProviderError` listing the valid ones.
- **`options`**: the keys of the `[provider.<name>]` table in `dakc.toml`, with the
  same names and defaults (`model`, `api_key_env`, `base_url`, `max_tokens`,
  `binary`, `timeout`, `factory`, ...). For `custom`, `path` is used as given; a
  workspace resolves it relative to itself.
- **`client.complete(system, user, json_schema=None)`** returns a `Completion`
  (`text`, `input_tokens`, `output_tokens`). With `json_schema`, providers that
  support structured output are asked for JSON matching it.
- **`client.fingerprint`** is the one a compile with the same configuration
  records, for example `anthropic:claude-haiku-5-5:default`. `make_provider`
  builds its extractor, page writer, summarizer and conflict verifier on this same
  client.
- **Calls are yours.** DAKC does not cache, count or log what you send through
  the client.
- **Keys.** Hosted providers read their key from the variable `api_key_env`
  names. For `anthropic`, when `api_key_env` is not given or the variable is
  unset, the SDK finds the key as usual (`ANTHROPIC_API_KEY`,
  `ANTHROPIC_AUTH_TOKEN` or a login profile). DAKC never logs a key or puts it in
  an error message.
- **Errors.** A failed call raises `ProviderError` (a `custom` client raises
  whatever it raises). Its `transient` is True for a rate limit (429), request
  timeout (408) or server error (5xx) that outlasted the client's own retries, a
  timeout, a failed connection, and a `claude -p` that did not answer within
  `timeout`. It is False otherwise (a rejected request, a missing key, a refusal,
  a truncated answer), where asking again gets the same result.

## Extending: connectors and publishers

The contracts are in
[RFC 0001](rfcs/0001-sources-and-publishers.md). A connector lists items and
reads them:

```python
from dakc import Item

class NotesConnector:
    kind = "notes"
    capabilities = frozenset({"native_ids"})   # items carry a stable key

    def __init__(self, client):
        self.client = client

    def list(self):                              # a complete listing, every time
        for n in self.client.all_notes():
            yield Item(path=f"{n.folder}/{n.slug}.md", source_type="markdown",
                       revision=n.version, size=n.size, key=n.id)

    def read(self, item):                        # the bytes of exactly that revision
        return self.client.export(item.key, version=item.revision)

def make(name, options, root):                   # the factory DAKC calls
    return NotesConnector(connect(options["url"]))
```

A publisher receives batches of `Record`s (`id`, `kind`, `state_hash`,
`title`, `text`, `sources`, `build`, `metadata`) and deletions. It must be
idempotent:

```python
class VectorPublisher:
    kind = "vectors"
    kinds = frozenset({"claim"})

    def __init__(self, index):
        self.index = index

    def publish(self, batch):
        if batch.deletes:
            self.index.delete(ids=batch.deletes)
        if batch.upserts:
            self.index.upsert([(r.id, embed(r.text), {"sources": r.sources, "build": r.build})
                               for r in batch.upserts])
```

Register either one with an entry point in the `dakc.sources` or
`dakc.publishers` group, or load a local module with `type = "custom"`,
`factory = "module:attr"` and `plugin_path = "plugins"`. Every other key in
its `[[sources]]` or `[[publishers]]` table is passed to the factory as
`options`.
