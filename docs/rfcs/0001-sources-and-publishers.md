# RFC 0001: Sources and publishers

- **Status:** draft for review. Phase 1 is implemented: the source interface,
  multiple sources per workspace, the publisher interface, the publication
  ledger and plugin discovery. Phases 2 and 3 are open.
- **Scope:** how DAKC gets content (sources) and where compiled knowledge goes
  (publishers), without changing how the compiler works.

## Why

DAKC assumed one local directory in and one Markdown folder out. Teams keep
knowledge in many systems, such as Drive, SharePoint, GitHub and databases.
They consume it in many places too: vector databases, graph databases, and
agent memory. Three concerns are independent and should stay that way:

| Concept | Responsibility | Extended by |
|---|---|---|
| **Source** | Where content comes from, and how it is identified and versioned | source connectors |
| **Compiler** | How content becomes passages, claims, entities, pages, summaries and an index | (not extended) |
| **Publisher** | Where compiled knowledge is delivered | publishers |

The compiler never learns which connector supplied a document. Equally, a
publisher never sees the build machinery, only the current state and what
changed.

## Design principles

1. **Reconcile, don't trust events.** Every build compares a complete listing
   of each source with the source registry. A missed webhook or a lost cursor
   can delay an update but cannot corrupt state. Change feeds, when added, are
   an optimization for finding what to list, never the source of truth.
2. **Fail closed on sources.** If any source cannot be listed or read, the
   build fails before anything is committed. A partial listing would look
   like deletions and retract every fact from the missing source.
3. **Publishers converge on state, not on a log.** DAKC records which version
   of each artifact each publisher has acknowledged, and sends only the
   difference. Retries, crashes, rollbacks and new publishers are all the same
   operation.
4. **Nothing existing moves.** A workspace with today's `source_dir`
   configuration gets the same source ids, paths and artifacts. Upgrading
   rebuilds nothing.

## Sources

### Items

A connector lists **items**: versioned content objects. A file is one kind of
item; a database row or an API object could be another.

```python
@dataclass(frozen=True)
class Item:
    path: str               # where it is, relative to the source, POSIX style; may change
    source_type: str        # how DAKC parses it: markdown, html, pdf, text, json
    revision: str           # opaque version marker; a content hash when the connector has one
    size: int = 0
    key: str | None = None  # stable native id (a Drive file id), if the system has one
    metadata: dict = {}     # free-form: owner, url, modified time, ...
```

### The connector interface

```python
class SourceConnector(Protocol):
    kind: str                       # "filesystem"
    capabilities: frozenset[str]    # see below

    def list(self) -> Iterable[Item]: ...      # a complete listing, every time
    def read(self, item: Item) -> bytes: ...   # the bytes of that item at that revision
```

A connector is created by a factory, `factory(name, options, root)`, where
`root` is the workspace directory for resolving relative paths. Two
capabilities are defined:

- **`native_ids`.** Items carry a stable `key`. DAKC then matches items to
  sources by key, so a document that was moved *and* edited is still the same
  source. Without it, DAKC matches by path, and detects renames by identical
  content, as it does for files today.
- **`content_hash`.** `revision` is a hash of the bytes, so an unchanged
  revision means unchanged content and the item is not read at all. The
  filesystem connector has this capability. Without it, an item whose
  revision changed is read and hashed, and is treated as modified only if its
  bytes changed.

`change_feed` (a cursor-based `changes()`), `permissions` and `revisions`
(historical versions) are reserved names for phase 2.

`read()` must return the bytes of the revision it was given, or raise. The
compiler re-hashes what it reads, and fails the build if the content moved on
mid-build. Today's filesystem check generalizes to every connector.

### Identity

- **Source ids are unchanged.** A source id is `src:` plus a slug of the path
  where DAKC first saw the item. It is assigned once and kept across renames
  and moves. Ids are readable because they appear in URLs, the UI and trace
  output.
- **The native key is stored, not exposed.** It is a new nullable `key`
  column in the source registry, used only to match items across builds.
  Filesystem sources leave it empty.
- **Revision and content hash are different things.** The registry keeps the
  raw content hash, as it does today. A connector's revision is used only to
  decide whether to read.

### Several sources in one workspace

```toml
[[sources]]
name = "docs"
type = "filesystem"
path = "knowledge"
prefix = ""                  # files appear at the top level, as with source_dir

[[sources]]
name = "policies"
type = "filesystem"          # or any installed connector
path = "/mnt/policies"
# prefix defaults to the name: these files appear under policies/
```

- **Namespacing.** Each source has a **prefix**, and the paths DAKC records
  are `prefix/path`. At most one source may have an empty prefix. A non-empty
  prefix may not also be a top-level directory of the empty-prefix source;
  the scan fails with a clear error rather than mixing them. Topics follow
  paths, so each prefixed source becomes its own top-level topic.
- **Ownership.** A registered path belongs to the source whose prefix it
  starts with. Renames are detected only within one source. The same bytes in
  two sources are two sources, reported as duplicates, as they are today.
- **Support across sources.** Nothing new is needed. A claim stated in
  passages from two sources is supported by both. Deleting one source
  retracts only facts that no other passage supports. Contradictions stay
  side by side, each with its own sources.
- **Full-rebuild equality across sources.** `verify --against-full` rebuilds
  from the same set of connectors. A mixed-source workspace is checked
  exactly like a single directory.
- **Legacy configuration.** Without `[[sources]]`, `[dakc] source_dir`
  defines one filesystem source with an empty prefix: exactly today's
  behaviour.

### Failure semantics

| Situation | Behaviour |
|---|---|
| A source cannot be listed (network, auth) | The build fails; nothing is committed; HEAD and the published state are unchanged |
| An item cannot be read, or changes while read | The build fails, as a file changing mid-build does today |
| A connector returns the same revision | Nothing is read or extracted |
| A connector reports a move with the same key | Rename: the source id is kept, and its passages are reused where the text is unchanged |

One planned option, **`on_error = "keep"`**, would carry an unavailable
source's last committed state forward instead of failing. It is safe for the
store, but full-rebuild verification cannot check that source until it is
reachable again. Phase 2.

## Publishers

### Contract

```python
@dataclass(frozen=True)
class Record:
    id: str                 # DAKC artifact id: stable, content-addressed for claims
    kind: str               # claim | page | summary | index | passage
    state_hash: str         # changes whenever anything a consumer can see changes
    title: str
    text: str               # rendered Markdown for pages, summaries and the index; text otherwise
    sources: list[str]      # paths of the source documents it derives from
    build: int              # the build that wrote this version
    metadata: dict          # kind-specific: claim ids, mentions, topic, ...

@dataclass(frozen=True)
class PublishBatch:
    upserts: list[Record]
    deletes: list[str]      # artifact ids no longer current

class Publisher(Protocol):
    kind: str
    kinds: frozenset[str]   # which artifact kinds it wants, e.g. {"claim", "page"}
    def publish(self, batch: PublishBatch) -> None: ...   # idempotent; raise on failure
```

### The publication ledger

DAKC stores, per publisher, the `state_hash` of every record that publisher
has acknowledged, in a `published` table.

- **Pending work** is the current artifacts of the wanted kinds whose
  `state_hash` differs from the ledger, plus ledger ids that are no longer
  current (deletes). It is computed with one query.
- **Batches.** DAKC sends batches in a deterministic order. After each one
  that returns, it records the batch in the ledger in a single transaction.
  If a batch raises, publishing stops: that batch and everything after it
  stay pending, and the error is reported. The next `dakc compile` or
  `dakc publish` resumes.
- **Idempotency.** Upserts are keyed by artifact id and deletes may repeat,
  so resending a batch that partly landed is harmless. Publishers must be
  written that way.
- **Rollbacks, rebuilds and new publishers need no special case.** The ledger
  diff against the current state is always the right set of operations.
- **Locking.** Publishing runs after the build commits, under the workspace
  build lock, so HEAD does not move while a publisher is catching up.

### Consistency

During a publish, a reader of the target can see some records from the new
build and some from the old one. Every record carries `build`. A target that
needs an atomic switch can publish into a new collection and swap an alias;
that is the publisher's choice, not the contract's. The ledger tells a
publisher exactly when it has caught up.

### Built-in publishers

- **`markdown`.** The wiki folder. It stays the existing projection, which
  already reconciles against the store with its own manifest. It is always
  on, and configured by `[dakc] output_dir`.
- **`jsonl`.** A change feed: one JSON line per upsert or delete. It is the
  integration point for anything without a plugin: a script can read new
  lines and update a vector database.

Vector-database publishers (Qdrant, Chroma, Weaviate, Milvus) are plugins.
They need an embedding model, which DAKC does not choose for the user.

## Plugins

Connectors and publishers are found the same way providers are:

1. **Built-ins:** `filesystem` for sources; `jsonl` (and the implicit
   `markdown`) for publishers.
2. **Installed packages** register Python entry points:

   ```toml
   [project.entry-points."dakc.sources"]
   google-drive = "dakc_google_drive:make"

   [project.entry-points."dakc.publishers"]
   qdrant = "dakc_qdrant:make"
   ```

3. **Local code**, without packaging: `type = "custom"`, `factory =
   "module:attr"` and `plugin_path = "plugins"` (the directory to import it
   from, relative to the workspace). Every other key in the table is passed
   to the factory, so `path` stays free for the connector's own use.

A factory is called as `factory(name, options, root)` and returns an object
satisfying the protocol. `dakc sources` lists the configured sources, and
`dakc publish` shows each publisher's pending work and runs it.

## Permissions (phase 3, not implemented)

Access control has to propagate through derived artifacts, so it is a
compiler concern, not only a connector concern. This is the proposed model.

- **Items carry labels.** Connectors with the `permissions` capability attach
  an `acl` label set to each item, for example the groups that may read it.
- **Claims are visible if any supporting source is.** A user who may read
  either of two documents stating a fact may see the fact, but only the
  citations they may read.
- **Pages and summaries are the hard part.** An LLM wrote them from claims of
  mixed visibility, so text cannot be filtered after the fact. The options:
  - build pages per label set, which is correct but multiplies cost;
  - label a page with the *intersection* of its claims' labels, which is
    correct but hides pages that mix sources;
  - publish only claims to permission-aware targets.

  The third is the recommended starting point. Claims are the precise unit,
  and every record already lists its sources, so a retrieval layer can filter
  them.
- **Labels must be part of signatures.** An ACL change must re-label
  artifacts without rewriting text.

## Phases

1. **Done:** the source interface; the filesystem connector; multiple sources
   with prefixes; native-key matching; the publisher interface; the ledger;
   the `jsonl` publisher; plugin discovery; `dakc sources` and
   `dakc publish`.
2. **Remote connectors and feeds:** Google Drive, SharePoint and GitHub as
   separate packages; `change_feed`; `on_error = "keep"`; connector health
   in `status`.
3. **Permissions,** as above, and permission-aware publishers.

## Tests that hold the abstraction to account

- Existing workspaces upgrade with identical ids and no rebuild. The
  published benchmark still reproduces exactly (`--verify-against`).
- Two sources support one claim; deleting either keeps the claim.
- The same document in two sources has two identities.
- A rename within a source keeps the id. With native ids, a move plus an
  edit keeps it too.
- A source whose listing fails leaves HEAD, the wiki and every publisher
  untouched.
- A connector that returns the same revision triggers no reads.
- A mixed-source workspace equals a full rebuild.
- A publisher that fails mid-way resumes without losing or duplicating work.
  After a rollback it converges, deleting what is no longer current.
