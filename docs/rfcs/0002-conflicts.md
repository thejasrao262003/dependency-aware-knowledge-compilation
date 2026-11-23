# RFC 0002: Conflicts between sources

- **Status:** implemented.
  - Phase 1: deterministic detection, kept current incrementally, with a
    CLI, Python API, REST, MCP and the pull-request comment.
  - Phase 2: rules for the same statement worded differently, aliases in
    keys, and an optional model-based verifier (off by default). See
    [The verifier](#the-verifier-optional).
- **Scope:** telling people which documents disagree, and where. DAKC never
  decides which one is right.

## Why

DAKC already keeps contradicting claims: both appear on the page, each with
its own sources (`tests/integration/test_contradictions.py`). That is
correct, but silent. A reader of the PostgreSQL page sees "supports JSONB"
and "does not support JSONB" and has to work out on their own that two
documents disagree, and which two.

Documentation drifts this way all the time. Someone updates the database
page, and the architecture overview still says the old thing. A compiler
that already knows every claim, its exact sentence and its source is in the
right place to notice.

## The hard part: what is a conflict?

Most differences between documents are not conflicts. The question that
shaped this design:

> Release 1 was an MVP on MongoDB. In production the team moved to
> PostgreSQL. `techstack001.md` says MongoDB is the DB and `techstack.md`
> says PostgreSQL is the DB. That is history, not a conflict.

So detection has two halves. The first finds pairs of statements that are
incompatible on their face. The second decides whether something in the
text explains the difference. DAKC reports three outcomes:

- **conflict:** the same property, the same scope, incompatible values. Someone
  should look.
- **scoped:** incompatible values, but in different scopes (time, release,
  environment or status). Listed with `--all`, not counted as open.
- **nothing:** compatible statements. Not reported.

### Taxonomy

Each row is a test case in `tests/unit/test_conflicts.py`.

| Statements | Outcome | Why |
|---|---|---|
| "PostgreSQL supports JSONB" / "PostgreSQL does not support JSONB" | conflict | a negation |
| "Redis supports X" / "Redis doesn't support X" | conflict | contractions are expanded |
| "PostgreSQL supports Python 2" / "PostgreSQL no longer supports Python 2" | conflict | one document is stale; "no longer" is a negation |
| `database.md` "MongoDB is the database" / `architecture.md` "PostgreSQL is the database" | conflict | a definite role ("the database") holds one value |
| "The primary database is MongoDB" / "PostgreSQL is the primary database" | conflict | word order is normalized |
| default port 5432 / 5433; three nodes / five nodes; up to 100 / 500 connections | conflict | measured numbers are single-valued |
| licensed under PostgreSQL License / MIT | conflict | "licensed under" is single-valued |
| "The recommended algorithm is Argon2" / "… is Bcrypt" | conflict | current guidance, not a proposal |
| both "in release 1", or both "in production", with different values | conflict | same scope, so the scope does not explain it |
| two contradicting sentences in one document | conflict | |
| `techstack001.md` MongoDB / `techstack.md` PostgreSQL | scoped | a zero-padded number in a file name marks a revision snapshot |
| heading "Release 1 (MVP)" / heading "Production" | scoped | scope is read from the heading path |
| "In release 1 (MVP), MongoDB is the database" / "PostgreSQL is the database" | scoped | scope is read from the sentence |
| "MongoDB was the database until the migration" / "PostgreSQL is the database" | scoped | past tense |
| "In production, …" / "In development, …" | scoped | environments |
| `archive/techstack.md`, `docs/adr/0001-*.md` vs `0004-*.md`, release notes under version headings | scoped | the path or heading marks history or a decision record |
| "Rejected alternatives", "FAQ > Myths", "If caching is enabled, …" | scoped | proposed, quoted and conditional statements are not statements of fact |
| a path matched by a `[[conflicts.scope]]` rule | scoped | the team said what the document describes |
| "PostgreSQL supports JSONB" / "PostgreSQL supports Indexing" | nothing | "supports" is multi-valued |
| "Ledger uses PostgreSQL" / "Ledger uses Redis"; "stores data in" | nothing | multi-valued |
| "Redis is a cache" / "Memcached is a cache" | nothing | indefinite: different subjects can share a description |
| default port of PostgreSQL / default port of Redis | nothing | different subjects |
| "JSONB is available in PostgreSQL" | nothing | a paraphrase that agrees |
| "We plan to make CockroachDB the database" | not a conflict | proposed |
| the same file edited from MongoDB to PostgreSQL | nothing | the old claim is retracted; builds keep the history |

That last row is the commonest case and needs no rule at all: DAKC
compares the claims at HEAD, and an edit retracts the old one. History lives
in `dakc builds` and `dakc diff`, not in conflicts.

**What DAKC cannot know.** Two documents with no dates, no release markers
and file names like `stack-a.md` and `stack-b.md` give no hint that one is
older. DAKC reports a conflict. The fix is cheap: say "in release 1" in the
text, put old documents under `archive/`, or add a rule:

```toml
[[conflicts.scope]]
path = "docs/mvp/**"
label = "release 1"        # or status = "past" | "proposed"

[[conflicts.accept]]       # reviewed, and kept as is
id = "cfl:0123456789"
reason = "the two services really do use different ports"
```

## How it works

### Comparable keys

Every claim occurrence (a claim, in one passage, of one source) is parsed
into normalized tokens:

- contractions expanded, "no longer" turned into a negation;
- possessives, parentheticals and scope phrases ("in release 1",
  "in production") removed, so they don't stop two statements matching;
- past tense mapped to present, plurals stemmed crudely;
- "X is the Y" rewritten as "the Y is X".

The same statement worded differently is written one way (phase 2):

- "lacks X", "has no support for X", "dropped support for X",
  "X is unavailable in Y", "X is not supported by Y" and "is not
  compatible with" all become a negation of "Y supports X";
- an antonym used as the predicate ("is disabled", "is optional", "is
  asynchronous", "is private", "is stateful" and about twenty more) reads
  as the negation of its counterpart. Only after "is" or a similar verb:
  "a private key" is not "not a public key";
- a few synonyms (DB and datastore for database, Postgres for PostgreSQL,
  k8s for Kubernetes) and the workspace's `[aliases]` are applied before
  keys are made. Changing the aliases re-judges everything.

Each occurrence then yields up to two kinds of key:

- **a negation key:** the tokens without the negation. "supports JSONB" and
  "does not support JSONB" share one; the value is "is stated" or "is
  negated".
- **value keys:** the sentence with one named value (an entity mention or a
  measured number) replaced by a slot, such as `the database is _`.
  Only single-valued frames get one:
  - a definite role ("the database is _");
  - a functional word or phrase ("default", "version", "port",
    "licensed under", "maintained by", "written in");
  - a measured number ("runs on _ nodes", "up to _ connections").

  Verbs such as "supports", "uses" and "includes" never do. Neither do
  imperative changelog lines ("Update X to 0.7.0") or identifier numbers
  ("PR #123").

Two occurrences can only disagree if they share a key. Most keys have one
claim, so judging is cheap.

### Scope

A scope is a status (current, past, proposed, conditional or quoted), plus
time markers and environment markers. It is read from four places:

1. **The sentence:** "In release 1", "(MVP)", "was", "plan to", "If …".
2. **The heading path:** "Release 1 (MVP)", "Production", "History",
   "Rejected alternatives", "FAQ > Myths".
3. **The file path:** `archive/`, `legacy/`, `v1/`, `2023`,
   `techstack001.md` (revision 001), `docs/adr/0004-*.md` (record 0004).
   Only zero-padded numbers count as revisions, so `oauth2.md` and
   `http2.md` are not.
4. **`[[conflicts.scope]]` rules** in `dakc.toml`.

### Judging

All occurrences that share a key are grouped by scope.

- **conflict:** some current or past scope holds two values, from two
  claims.
- **scoped:** the values differ only across scopes, or only in proposed,
  conditional or quoted statements.
- A frame with more than 12 values is an enumeration, not a property, and
  is skipped.

A finding is about a **property**, not a pair. "The database is …" with
three values is one finding listing every value and every sentence that
states it. It is labelled in the documents' own words, with the value
replaced by "…". Its id (`cfl:` plus ten hex characters) is a hash of the
property's key. It therefore survives edits to the values, and a conflict
that is fixed and later reintroduced comes back with the same id.

### Kept current incrementally

- **What a build re-parses.** A build re-parses only:
  - claims it touched;
  - claims of passages whose heading changed;
  - claims of renamed sources (their path may now mark a scope).
- **What a build re-judges.** It re-judges only the keys those claims had
  before or have now. The index (`conflict_index`) holds one row per
  comparable key per occurrence.
- **Findings are versioned like artifacts.** They live in `conflicts`, with
  `from_build` and `to_build`, so `rollback` restores the findings of the
  build it returns to.
- **When everything is rebuilt.**
  - Everything is re-judged when the detector version or the scope rules
    change: their fingerprint is stored with the index.
  - The index is rebuilt after a rollback.
  - The first build after an upgrade builds it from scratch.
- **Checking it.** `dakc verify` recomputes every finding from scratch and
  requires the stored ones to be identical. The integration tests do the
  same after every step of an edit sequence that:
  - resolves a conflict;
  - adds a source;
  - changes a heading;
  - renames a file into `archive/`;
  - deletes a source.

Detection never changes a claim, a page or a signature. It is not part of
the canonical state, so it cannot affect `verify --against-full` or the
benchmark.

### The verifier (optional)

The rules compare statements of the same shape. "Ledger is written in Go"
and "Ledger is a Python service" share no key, and no rule will ever cover
every way people write. The verifier asks a model instead, about a few
pairs per edit. It is off by default:

```toml
[conflicts]
verifier = "llm"          # the workspace's provider judges; "none" turns it off
max_checks = 500          # pairs judged per compile; the rest wait for the next one
candidates_per_entity = 5
pairs_per_call = 10
```

1. **Candidates, without a model.** For each claim that is new in this
   build, and each entity it mentions, the five claims about that entity in
   *other* documents that share the most words with it. Pairs the rules
   already compare (a shared key) are left to the rules. A hub entity such
   as "FastAPI", with thousands of claims, still yields five.
2. **Verdicts.** The model judges each pair `contradiction`,
   `different_scope` or `compatible`, ten pairs per call, with a
   one-sentence reason. A verdict is a fact about two claim texts. It is
   cached by the pair of claim ids and the verifier's fingerprint (model
   and prompt), as extraction is, so an unchanged pair is never judged
   twice, and a build that fails keeps the verdicts it paid for.
3. **Findings.** Every pair of current claims with a `contradiction`
   verdict is a **possible** conflict, labelled with the entities the two
   statements share and the model's reason. DAKC's own scope reading still
   applies: if the heading, path or a `[[conflicts.scope]]` rule puts the
   two in different scopes, or the model said `different_scope`, it is
   *scoped*. A possible conflict closes when either statement changes, and
   can be accepted like any other.
4. **Cost is bounded and visible.**
   - `dakc compile --dry-run` counts the verifier's calls in its LLM call
     estimate, and the MCP `compile` tool's call cap includes them.
   - Each compile judges at most `max_checks` pairs, most similar first.
     The rest are reported and judged by the next compile (`status` says
     so), so nothing is skipped for good.
   - Turning the verifier on, or changing its model, makes the next compiles
     look at every claim, not only new ones. Verdicts already on record are
     reused.
5. **Checked like the rules.** Findings from the verifier are recomputed
   from the current claims and the verdicts on record at every compile. So
   they equal a computation from scratch, and `dakc verify` checks that
   they do. Rolling back and compiling again gives the same findings.

With the free `heuristic` and `mock` providers, the verifier is a stand-in
that agrees with the rules, and so finds nothing new. It exists so that
the mock provider can count the calls and tokens a real model would cost.

## Interfaces

| Where | What |
|---|---|
| `dakc compile` | `Conflicts: 1 open (new 1, resolved 0)`; with the verifier, also `Conflict verifier: 3 pair(s) judged, 1 possible conflict(s) open` |
| `dakc status` | `Conflicts: 1 open` |
| `dakc conflicts [--all] [--json] [--check]` | open conflicts, the verifier's marked `possible`; `--all` adds scoped and accepted ones; `--check` exits 1 if one is open (for CI) |
| `dakc conflicts ID`, `dakc explain cfl:…` | every value, the file and line that states it, the heading and scope, the pages and summaries built from those claims, and how to resolve it |
| `kb.conflicts(scoped=False, accepted=False)`, `kb.conflict(id)` | `Conflict` objects (Python API) |
| `GET /conflicts`, `GET /conflicts/{id}` | REST (`dakc serve`) |
| MCP `conflicts` | agents are told to mention an open conflict when an answer relies on one of its claims |
| `dakc review` and the GitHub Action | the pull-request comment lists conflicts the change introduces (expanded) and resolves |

An example, from FastAPI's documentation with one planted file:

```text
$ dakc explain cfl:1f3a
Conflict cfl:1f3a356464  (since build 12)
Property: The recommended algorithm is "…"
Why:      sources give different values for a single-valued property, and none of them is limited to a release, time or environment

Argon2
  tutorial/security/oauth2-jwt.md:73  … with Password (and hashing), Bearer with JWT tokens > Install pwdlib  (current)
    "The recommended algorithm is "Argon2"."

Bcrypt
  team-notes.md:5  Team notes  (current)
    "The recommended algorithm is "Bcrypt"."

Built from these claims:
  page (2): Argon2, Bcrypt
  summary (2): Topic: _root, Topic: tutorial
```

Changing `team-notes.md` to agree and compiling prints
`Conflicts: 0 open (new 0, resolved 1)`.

## Evaluation

Four measurements. Only the verifier's quality needs a model; the rest give
the same numbers every time.

### The rules on FastAPI's documentation

Measured by `examples/fastapi-docs/conflicts_eval.py` (about 15 seconds).
The full output, including every key it lists, is in
[examples/fastapi-docs/results/conflicts.md](../../examples/fastapi-docs/results/conflicts.md).

- **The taxonomy:** every row above is a test, and all pass.
- **Precision on real documentation.** The corpus is FastAPI's English docs
  at [`05e6df7e`](https://github.com/fastapi/fastapi/commit/05e6df7e906c3decba377c37548019973f40bf15),
  the commit the other benchmarks pin:
  - 159 documents, 11,067 claims, 11,704 claim occurrences;
  - **0 findings**;
  - the index has 7,742 keys, and 10 of them are stated by more than one
    claim. Read by hand, all 10 are restatements that agree ("You can
    declare a parameter of type Response …" in three documents). So
    there were very few chances to be wrong: this shows the rules do not
    invent conflicts, not that they find them.
- **Recall on planted contradictions.** A sentence from the same corpus was
  negated, or given a different value for a single-valued property. It was
  extracted with the same extractor, and detection over the corpus had to
  flag it against the original. The seed is fixed:

  | Planted | All sentences | Plain statements |
  |---|---|---|
  | negation ("is" → "is not", "supports" → "does not support") | 107 / 150 (71%) | 90 / 100 (90%) |
  | a different value for a single-valued property | 70 / 127 (55%) | 62 / 80 (78%) |

  "Plain statements" excludes list fragments ("Editor support (obviously)"),
  instructions ("You can …", "If …") and notes. These contradictions are
  written in the shape the rules match, so they overstate recall on real
  writing; the next measurement does not.
- **Cost:**
  - **incremental compile:** the conflict step took **0.4 ms** for a
    one-file edit;
  - **re-judging all 11,067 claims** (after a change to the scope rules):
    0.70 s.

### Labelled pairs, written the way people write

`benchmarks/conflicts/evaluate.py` runs detection over pairs of statements
labelled `contradiction`, `scoped` or `compatible`. There are two sets:

- `pairs-dev.jsonl` (81 pairs) was written while the phase 2 rules were
  designed.
- `pairs-test.jsonl` (110 pairs) was written separately, from a brief, by
  someone with no access to the detector. Its numbers are the ones to
  quote.

With `--provider-config`, the pairs the rules do not flag, and that a
compile would show the verifier, are judged by that provider's verifier.
The verifier below is gpt-5-mini. It ran twice on the held-out set: before
and after the fixes described below.

| Held-out set (110 pairs) | rules | rules + verifier, first run | **rules + verifier, after the fixes** |
|---|---|---|---|
| contradictions flagged | 4 / 55 (7%) | 43 / 55 (78%) | **50 / 55 (91%)** |
| scoped or compatible pairs flagged (false alarms) | 0 / 55 | 1 / 55 | **0 / 55** |

By kind of contradiction:

| Kind | rules | first run | **after the fixes** |
|---|---|---|---|
| same wording, one word negated or changed | 4 / 5 | 5 / 5 | **5 / 5** |
| negation in other words | 0 / 10 | 7 / 10 | **10 / 10** |
| antonym | 0 / 10 | 10 / 10 | **10 / 10** |
| synonym or alias | 0 / 8 | 5 / 8 | **7 / 8** |
| the same property, worded differently | 0 / 12 | 7 / 12 | **9 / 12** |
| implied ("uses only X" / "also writes to Y") | 0 / 10 | 9 / 10 | **9 / 10** |

On the dev set, the first run flagged 35 of 41 (rules alone: 19), with no
false alarm in 40 pairs; it was not run again. Results:
`benchmarks/conflicts/results-verifier-gpt-5-mini-before-fixes.json`
(first run), `results-verifier-gpt-5-mini.json` (after the fixes), and the
model's verdicts with reasons in `verdicts-gpt-5-mini.json`.

What this says:

- **The rules do not invent conflicts, and do not generalize.** No false
  alarm in 95 scoped or compatible pairs. But the phase 2 wording rules
  lift the dev set from 5 to 19 of 41, and the held-out set only from 3 to
  4 of 55. In the held-out set an antonym is rarely the only difference
  ("logins require two-factor authentication" against "two-factor
  authentication is optional when signing in").
- **The verifier closes most of the gap.** The first run's 12 misses were
  all DAKC's: 5 pairs never reached the model, because the free extractor
  names their subject differently. DAKC's own scope reading made the
  other 7 *scoped*:
  - four came from two bugs: "from **Ga**teway" read as the GA release
    marker, and "used" ("are not used by") or "legacy" ("from legacy
    clients") marking a sentence as past;
  - two were values read as scopes: a region, and "runs on Python 3.11";
  - one was "was deprecated", read as past.
- **The fixes**, then the second run:
  - the two bugs are fixed;
  - a product's version or a region after the verb is now a value, not a
    scope;
  - the verifier compares entities by name without a possessive
    ("Ledger's" is Ledger).
- **The 5 contradictions still missed:**
  - **2 never reach the model:** the free extractor groups "Billing's DB"
    and "Inventory's HTTP API" into entities of their own. An LLM
    extractor would not.
  - **2 the model flagged, and DAKC's scope reading made scoped:** "each
    tenant may send up to 500 requests" ("may" read as a proposal) and
    "the v1 Orders API was deprecated" (read as past).
  - **1 the model called compatible:** "requests from Gateway to Ledger
    time out after 30 seconds" against "Gateway gives up on a Ledger call
    after 5 seconds".
- **The model is not fully repeatable.** The first run's false alarm ("Ledger
  is not a stateless service" against "Ledger is a stateful service") did
  not recur in the second run. In a compile each pair is judged once and
  its verdict cached, so a knowledge base sees one answer per pair.
- **What it cost:**
  - the first run: 14 calls on both sets, 12,891 input and 15,786 output
    tokens, about 3.5 cents at $0.25 / $2.00 per million tokens;
  - the second: 9 calls, 8,594 and 10,645 tokens, about 2.3 cents.

  gpt-5-mini's reasoning makes the output about 120 tokens per pair, ten
  times what a short answer needs.
- **Two caveats.** Both sets are small, and the held-out set was written by
  a model from a brief, not taken from real documentation. Read the
  percentages as indicative. The fixes were found by reading the held-out
  misses, so the second run is no longer fully held out. Each fix is
  general (a bug, or a class of value), and none adds wording from the set.
  A stemming bug ("retries" against "retry") was found the same way.

`--verdicts verdicts.json` keeps the model's verdicts, so evaluating again
after a change to DAKC's rules or scope reading costs nothing.

### The verifier's volume on FastAPI's documentation

`examples/fastapi-docs/verifier_volume.py` compiles FastAPI's docs with the
verifier on and unbounded, then keeps it on through the last 50 commits
that changed them. It counts the pairs and calls a compile makes, with the
mock provider, and prices the pairs at the token rates measured above
(about 101 input and 124 output tokens per pair, at $0.25 / $2.00 per
million). Results are in
[examples/fastapi-docs/results/verifier-volume.md](../../examples/fastapi-docs/results/verifier-volume.md).

| | Pairs judged | Model calls | Cost at measured rates |
|---|---:|---:|---:|
| Turning the verifier on (11,067 claims) | 23,280 | 2,328 | about $6.36, once |
| Keeping it on through 50 commits | 291 | 58 | about $0.08 in all |

- 10 of the 50 commits needed no call, and the busiest judged 90 pairs.
- Output is most of the cost. A model that does not reason writes a short
  answer and would cost a fraction of this, but its quality is not
  measured.
- With the default `max_checks = 500`, turning the verifier on is spread
  over many compiles. To check a whole knowledge base in one go, raise
  `max_checks` for one compile; `--dry-run` shows the calls first.
- Claims here come from the heuristic extractor. An LLM extractor yields
  different claims, so treat these as estimates.

## Limitations

- **The rules miss most contradictions written in different words**: 4 of
  55 in the held-out set. The verifier is the answer, and it costs model
  calls.
- **The verifier's quality is the model's.** It is advisory and labelled
  `possible` for that reason. Its verdicts are cached, so a wrong verdict
  stays until one of the statements changes, the model or prompt changes,
  or someone accepts the finding.
- **The verifier only sees pairs that share an entity and come from
  different documents.** Two names for one thing need an `[aliases]` entry,
  and the free extractor's entities are coarse (see above). A contradiction
  inside one document is left to the rules.
- **Scope must be written down, and is read literally.** It is read from
  the sentence, its heading and its path. "As of this release", three
  paragraphs up, is not seen. Some words mislead it, and each makes a real
  contradiction *scoped*:
  - "was deprecated" is read as past, though the deprecation still holds;
  - "may" is read as a proposal, also when it grants permission ("each
    tenant may send up to 500 requests");
  - a version or region in a leading phrase is a scope ("In eu-west-1, …"),
    and after the verb a value ("lives in eu-west-1"). Only "on", "with"
    and "for" make a version a value: "available in PostgreSQL 9.4" is
    still read as a release.
- **What the extractor records limits recall.** The free heuristic
  extractor needs a capitalised name in a sentence. An LLM extractor
  records more claims, and normalizes their wording, which helps matching.
- **Single-valued is a vocabulary, not a schema.** A property DAKC does not
  know to be single-valued ("the on-call team is …" is, via "the";
  "Ledger stores data in …" is not) is not compared by the rules.

## Next

1. **Measure a model that does not reason**, for a cheaper verifier, and
   build a held-out set from real documentation.
2. **A local NLI model as a second verifier**, behind the same
   `verifier` setting: free and deterministic, weaker on implied
   contradictions.
3. **Tense and modality:** "was deprecated" and "may send" should not
   change a statement's scope.
4. **Declared functional properties:**
   `[conflicts] single_valued = ["on-call team", "owner"]`.
5. **An opt-in page marker.** Pages would stay unchanged by default, so
   detection stays advisory.
6. **A conflicts view in the web UI.**
