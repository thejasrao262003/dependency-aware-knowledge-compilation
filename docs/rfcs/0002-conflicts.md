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
| two contradicting sentences in one note under a `label = "{path}"` rule | conflict | one document, one scope |
| two notes under a rule with `status = "past"` and no label | conflict | both are past, and nothing else tells them apart |
| `techstack001.md` MongoDB / `techstack.md` PostgreSQL | scoped | a zero-padded number in a file name marks a revision snapshot |
| heading "Release 1 (MVP)" / heading "Production" | scoped | scope is read from the heading path |
| "In release 1 (MVP), MongoDB is the database" / "PostgreSQL is the database" | scoped | scope is read from the sentence |
| "MongoDB was the database until the migration" / "PostgreSQL is the database" | scoped | past tense |
| "In production, …" / "In development, …" | scoped | environments |
| `archive/techstack.md`, `docs/adr/0001-*.md` vs `0004-*.md`, release notes under version headings | scoped | the path or heading marks history or a decision record |
| "Rejected alternatives", "FAQ > Myths", "If caching is enabled, …" | scoped | proposed, quoted and conditional statements are not statements of fact |
| a path matched by a `[[conflicts.scope]]` rule | scoped | the team said what the document describes |
| two session notes under a rule with `label = "{path}"`: "… is Bcrypt" in March, "… is Argon2" in May | scoped | each document under the rule is its own scope |
| a note under that rule (`status = "past"`) / a current document | scoped | past against current, as without the label |
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

### Per-document scope

A rule's `status` and `label` are shared by every document it matches. That
suits an `archive/` folder, but not a folder of notes written one per session
or per meeting: two notes that record a decision and, later, its reversal
("the password hashing algorithm is Bcrypt" in March, "… is Argon2" in May)
are both past, share one scope, and would be reported as a conflict.

A label can hold a placeholder, expanded for each document:

```toml
[[conflicts.scope]]
path = "sessions/**"
status = "past"
label = "{path}"      # every note its own scope; "{dir}": every folder
```

- **Between documents under the rule**, a difference is explained by scope:
  *scoped*, listed with `--all`.
- **Within one document** the scope is shared, so two statements that
  disagree are still a conflict.
- **Against a document outside the rule** nothing changes. A past note
  against a current document was already scoped (two statuses), and stays
  scoped, with every value listed. Two current documents that disagree are
  a conflict whatever the notes say.
- Only `{path}` and `{dir}` are placeholders. Another `{name}` in a label is
  an error when the configuration is read, so a typo cannot silently put
  every document in one scope.

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
    change: their fingerprint is stored with the index. A rule enters the
    fingerprint as written: a `{path}` label is hashed as the template, not
    its expansions, so adding a document under the rule re-judges only that
    document's keys. A renamed document's claims are re-parsed (above), which
    picks up its new label.
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

### Past-scoped claims on pages

The status a `[[conflicts.scope]]` rule declares is also read by page
rendering, which is separate from detection. A claim whose every supporting
document a rule declares past is listed under a deterministic `## History`
section of its page, not among its current facts. With the session-notes rule
above, Bcrypt (stated only in a March note) is History on the Auth page and
Argon2 (stated in `current/auth.md`) is current. The finding between them
stays *scoped*.

Only declared status moves a claim. Scope inferred from a sentence, heading or
path stays a conflict-detection matter: it is tuned so that a wrong "past"
costs a finding, not a fact on a page. Design, invalidation and measurements:
"History" in [docs/dependency-model.md](../dependency-model.md) and ADR-29.

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
| The web UI (`dakc serve`) | a Conflicts view: the values side by side, each with its sentence, file and line, the pages built on them, and how to resolve it; notices on the claims, pages and documents involved |
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
  | negation ("is" → "is not", "supports" → "does not support") | 108 / 150 (72%) | 90 / 100 (90%) |
  | a different value for a single-valued property | 70 / 127 (55%) | 62 / 80 (78%) |

  "Plain statements" excludes list fragments ("Editor support (obviously)"),
  instructions ("You can …", "If …") and notes. These contradictions are
  written in the shape the rules match, so they overstate recall on real
  writing; the next measurement does not.
- **Cost:**
  - **incremental compile:** the conflict step took **0.4 ms** for a
    one-file edit;
  - **re-judging all 11,067 claims** (after a change to the scope rules):
    0.74 s.

### Labelled pairs, written the way people write

`benchmarks/conflicts/evaluate.py` runs detection over pairs of statements
labelled `contradiction`, `scoped` or `compatible`. There are two sets:

- `pairs-dev.jsonl` (81 pairs) was written while the phase 2 rules were
  designed.
- `pairs-test.jsonl` (110 pairs) was written separately, from a brief, by
  someone with no access to the detector. Its numbers are the ones to
  quote.

With `--provider-config`, the pairs the rules do not flag, and that a
compile would show the verifier, are judged by that provider's verifier,
ten pairs per call. Two models were measured on the held-out set: gpt-5-mini,
which reasons before it answers, and gpt-4o-mini, which does not.

| Held-out set (110 pairs) | contradictions caught | false alarms | output tokens per pair |
|---|---|---|---|
| rules alone | 4 / 55 (7%) | 0 / 55 | none |
| rules + gpt-5-mini | **52 / 55 (95%)** | **0 / 55** | about 121 |
| rules + gpt-4o-mini | **53 / 55 (96%)** | 2 / 55 | about 25 |

By kind of contradiction, the verifier rows are the same for both models
except one pair, noted below:

| Kind | rules | rules + verifier |
|---|---|---|
| same wording, one word negated or changed | 4 / 5 | 5 / 5 |
| negation in other words | 0 / 10 | 10 / 10 |
| antonym | 0 / 10 | 10 / 10 |
| synonym or alias | 0 / 8 | 7 / 8 |
| the same property, worded differently | 0 / 12 | 10 / 12 (gpt-4o-mini 11 / 12) |
| implied ("uses only X" / "also writes to Y") | 0 / 10 | 10 / 10 |

Results, and each model's verdicts with its reasons, are in
`benchmarks/conflicts/` (`results-verifier-*.json`, `verdicts-*.json`). The
dev set was judged once, by gpt-5-mini, before the fixes below: 35 of 41,
with no false alarm (`results-verifier-gpt-5-mini-before-fixes.json`).

What this says:

- **The rules do not invent conflicts, and do not generalize.** No false
  alarm in 95 scoped or compatible pairs. But the phase 2 wording rules
  lift the dev set from 5 to 19 of 41, and the held-out set only from 3 to
  4 of 55. In the held-out set an antonym is rarely the only difference
  ("logins require two-factor authentication" against "two-factor
  authentication is optional when signing in").
- **With a verifier, almost everything that reaches the model is caught.**
  - Two contradictions never reach it: the free extractor groups "Billing's
    DB" and "Inventory's HTTP API" into entities of their own. An LLM
    extractor would not.
  - gpt-5-mini misses one: it called "requests from Gateway to Ledger time
    out after 30 seconds" and "Gateway gives up on a Ledger call after 5
    seconds" compatible.
  - gpt-4o-mini's two false alarms: it read a connect timeout and a response
    timeout as one property, and missed that "Ledger 2.0 uses PostgreSQL"
    and "Ledger 3.0 moves the primary database to CockroachDB" are two
    releases.
- **A model that does not reason is good enough, and much cheaper.** gpt-4o-mini
  catches as much, with two false alarms in 55, and writes a fifth of the
  output tokens. Findings are advisory and labelled `possible`, so it is a
  reasonable default; gpt-5-mini is the choice when false alarms cost more
  than tokens.
- **How the numbers got here.** The first run (gpt-5-mini, 43 of 55, one
  false alarm) missed 12. Reading those misses showed that most were
  DAKC's, not the model's, and led to these fixes, each general rather
  than a word from the set:
  - two scope-reading bugs: "from **Ga**teway" read as the GA release
    marker, and "used" or "legacy" in a sentence marking it as past;
  - a product's version or a region after the verb ("runs on Python
    3.11", "lives in the eu-west-1 region") is a value, not a scope;
  - "deprecated", and a lasting change ("was removed", "was replaced"),
    describe the present, not the past;
  - "may" that grants a limit ("each tenant may send up to 500 requests")
    is permission, not a proposal;
  - the verifier compares entities by name without a possessive
    ("Ledger's" is Ledger);
  - "used to read" is past again, after the "used" fix had broken it.

  The rules' stemming bug ("retries" against "retry") was found the same
  way. Because of all this, **the held-out set is no longer held out**: its
  numbers are now an optimistic estimate. A fresh set, from real
  documentation, is the next measurement.
- **Caveats.** The sets are small, and were written by a model from a
  brief. The models are not fully repeatable: gpt-5-mini's one false alarm
  in the first run did not recur in the second. In a compile each pair is
  judged once and its verdict cached, so a knowledge base sees one answer
  per pair.
- **What the runs cost:** gpt-5-mini, 23 calls over two runs, about 5.8
  cents at $0.25 / $2.00 per million tokens; gpt-4o-mini, 9 calls, 8,612
  input and 2,192 output tokens, about a quarter of a cent at $0.15 / $0.60.
  Re-scoring after the fixes used the saved verdicts and cost nothing.

`--verdicts verdicts.json` keeps the model's verdicts, so evaluating again
after a change to DAKC's rules or scope reading costs nothing.

### The verifier's volume on FastAPI's documentation

`examples/fastapi-docs/verifier_volume.py` compiles FastAPI's docs with the
verifier on and unbounded, then keeps it on through the last 50 commits
that changed them. It counts the pairs and calls a compile makes, with the
mock provider, and prices the pairs at the token rates measured above
(gpt-5-mini: about 101 input and 124 output tokens per pair, at $0.25 /
$2.00 per million; gpt-4o-mini: about 98 and 25, at $0.15 / $0.60, with
`--price 0.15:0.60 --tokens-per-pair 98:25`). Results are in
[examples/fastapi-docs/results/verifier-volume.md](../../examples/fastapi-docs/results/verifier-volume.md).

| | Pairs judged | Model calls | Cost at measured rates |
|---|---:|---:|---:|
| Turning the verifier on (11,067 claims) | 23,280 | 2,328 | gpt-5-mini about $6.36, gpt-4o-mini about $0.69, once |
| Keeping it on through 50 commits | 291 | 58 | gpt-5-mini about 8 cents, gpt-4o-mini under 1 cent, in all |

- 10 of the 50 commits needed no call, and the busiest judged 90 pairs.
- Output is most of a reasoning model's cost. gpt-4o-mini writes short
  answers and costs about a ninth as much, with the quality measured above.
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
  paragraphs up, is not seen. Versions are read by position:
  - in a leading phrase a version or region is a scope ("In eu-west-1, …"),
    and after the verb a value ("lives in eu-west-1"). Only "on", "with"
    and "for" make a version a value, so "available in PostgreSQL 9.4" is
    still read as a release, and a real contradiction can come out
    *scoped*;
  - a version in the subject ("Ledger 2.0 uses PostgreSQL") is not read as
    a release, so two releases can come out as a conflict.
- **What the extractor records limits recall.** The free heuristic
  extractor needs a capitalised name in a sentence. An LLM extractor
  records more claims, and normalizes their wording, which helps matching.
- **Single-valued is a vocabulary, not a schema.** A property DAKC does not
  know to be single-valued ("the on-call team is …" is, via "the";
  "Ledger stores data in …" is not) is not compared by the rules.

## Next

1. **A fresh held-out set from real documentation.** The current one has
   been read to fix DAKC, so its numbers are optimistic.
2. **A local NLI model as a second verifier**, behind the same
   `verifier` setting: free and deterministic, weaker on implied
   contradictions.
3. **Versions in the subject:** "Ledger 2.0 uses …" against "Ledger 3.0
   …" are two releases.
4. **Declared functional properties:**
   `[conflicts] single_valued = ["on-call team", "owner"]`.
5. **An opt-in page marker.** Pages would stay unchanged by default, so
   detection stays advisory.
