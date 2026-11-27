# Dependency model

DAKC treats a knowledge base as the output of a build: sources are inputs,
everything else is a derived artifact with a recorded set of inputs. This
document defines the artifact layers, their identities, what each one depends
on, and the exact rule that decides whether an artifact is rebuilt.

## Layers

Edges always point from a dependent artifact to an artifact in a strictly
lower layer. Execution in layer order is therefore a valid topological order,
and the graph is acyclic by construction. The graph engine still runs cycle
detection, as a check on that invariant.

| Layer | Kind | Identity | Content | Depends on |
|---|---|---|---|---|
| 0 | `source` | `src:<slug of first-seen path>`, kept across renames | path, raw-bytes hash, extracted-text hash, type | (input) |
| 1 | `passage` | `psg:` + H(source id, normalized text hash, occurrence #) | normalized text of one block (paragraph, list item, PDF block…); as metadata, the comments written after each sentence (`refs`) | its source |
| 2 | `claim` | `clm:` + H(normalized claim text) | canonical text + sorted entity mentions | every passage that yields it (its **support set**) |
| 3 | `entity` | `ent:` + normalized key (after alias map) | display name + sorted claim ids | claims that mention it |
| 3 | `relationship` | `rel:<a>--<b>` (a < b) | sorted ids of claims in which a and b co-occur | those claims |
| 4 | `page` | `page:<entity key>` | generated body + deterministic "Sources" section | entity, its claims, its relationships |
| 5 | `summary` | `sum:<topic>` (topic = top-level folder) | generated topic overview | the *lead* (title + first paragraph) of each page in the topic |
| 6 | `index` | `index:main` | deterministic page and summary listing | titles of all pages and summaries |

H is BLAKE3. Ids use a 64-bit prefix. Content hashes are full-length.

**Topic membership.** Each page belongs to exactly one *home topic*: the
top-level folder that contributes the most supporting sources to its claims,
with ties broken alphabetically. A page change can therefore invalidate at
most two summaries, the old and the new home. It never invalidates every
topic in which a popular entity is mentioned.

### Why claims are content-addressed

A claim's id depends only on its normalized text, so:

- the same fact extracted from two documents is **one** claim with two
  supports. Deleting one document leaves it alive (support counting, as in
  incremental view maintenance).
- re-extracting a changed paragraph that still states an unchanged fact
  reproduces the **same** claim id. That is what stops invalidation at the
  claim layer (see *early cutoff* below).
- duplicate, renamed and reverted documents cost no extraction calls, because
  the extraction cache is keyed by `H(extractor fingerprint, passage text hash)`.

Normalization is deterministic: NFKC, case-fold, collapse whitespace, unify
quotes and dashes, strip trailing punctuation. No LLM compares claims.

### Claim versions (semantic matching, last resort)

When a passage changes, its new claims are matched against the claims of the
passage it replaced:

1. Exact normalized-hash match: same claim. Nothing changes downstream.
2. Otherwise, a deterministic fuzzy match (RapidFuzz `token_sort_ratio`
   ≥ 85, greedy best-first, one-to-one) pairs each retracted claim with the
   new claim that most likely revised it. The pairs are shown in the
   compile report ("claim revisions") and stored in the build record.

The fuzzy match **never suppresses a rebuild**. "PostgreSQL 15 supports X" →
"PostgreSQL 16 supports X" is paired as a revision, but it is still a real
change and invalidates everything downstream of both claims. Matching only
feeds history and reporting. Per-claim version history is also available
through the temporal store (`dakc explain clm:…`).

## Signatures: when is an artifact rebuilt?

Every derived artifact stores the **input signature** it was built from: a
hash of the content hashes of exactly the inputs it reads, plus the
fingerprint (name, version, prompt hash) of the provider that built it. The
rule is the *verifying trace* from build systems:

> Rebuild an artifact iff its stored signature differs from the signature of
> its current inputs, or it does not exist yet.

| Artifact | Generation signature (LLM work) | Render signature (deterministic work) |
|---|---|---|
| passage | — (deterministic segmentation) | — |
| claim (extraction) | H(extractor fp, passage text hash); each passage records the fp that produced its claims (`extracted_by`), and a passage whose fp differs from the current extractor is re-extracted even if its text is unchanged | — |
| page | H(writer fp, entity display name, [(claim id, claim content hash)], related entity names) | H(gen sig, [(claim id, [source paths])]) |
| summary | H(summarizer fp, topic, [(page id, title, lead hash)]) | — |
| index | — | H([(id, title)] of pages and summaries) |

A page has two signatures because its *body* comes from the LLM, but its
*citations* are rendered by code from the claims' support sets:

- Claims change: the body is regenerated (LLM call).
- Only provenance changes (a duplicate source added, a supporting source
  deleted or renamed): the stored body is reused and only the Sources section
  is re-rendered, with **zero** LLM calls.

## Early cutoff

When an artifact is rebuilt and its output content hash equals the previous
one, its dependents' signatures do not change, so propagation stops there.
DAKC gets this at every layer:

- **Claim layer.** A paragraph edit that leaves a sentence intact keeps that
  sentence's claims byte-for-byte: they are *reused* from the cache rather
  than re-generated (see *Sentence-grounded extraction* below). Pages that
  depend only on unchanged claims are not touched. This holds for LLM
  extractors too, which would otherwise reword unchanged facts and give them
  new ids.
- **Page → summary.** Summaries depend on page *leads*, not full bodies. A
  changed fact deep inside a page does not regenerate the topic summary.
- **Index.** It depends only on titles.

## Affected vs. rebuilt

Every plan and report separates:

| Category | Meaning | Cost |
|---|---|---|
| `reextract` | passage text is new to the extraction cache | 1 LLM call per passage |
| `regenerate` | generation signature changed | 1 LLM call |
| `rerender` | only the render signature changed | 0 LLM calls |
| `retract` / `delete` | support set or membership became empty | 0 LLM calls |
| `checked` | reachable from a change but signature unchanged (early cutoff) | 0 |

"Affected" is the union of all five. "Needs rebuild" is the first three.

## Bounded fan-in: hub pages and large topics

Early cutoff decides *which* artifacts an edit invalidates. It does not bound
*how much input* an invalidated artifact is regenerated from. Before v0.5,
two kinds of artifact had unbounded input:

- an entity page, written from every claim about its entity;
- a topic summary, written from the lead of every page in its folder.

So one edit near a hub re-sent everything. On FastAPI's docs, a one-line
release-notes commit cost about 59,000 input tokens: a 30,000-token summary
prompt over about 1,660 pages, and a 28,600-token page prompt over 3,491
claims. A large enough folder or hub would also exceed a model's context
window.

Since v0.5 (`versions.PAGE = "page/2"`, `versions.SUMMARY = "summary/2"`),
both are built hierarchically once they pass 64 inputs. Smaller pages and
topics are built exactly as before.

- **Content-defined sections.** Inputs are ordered by a stable key and split
  where a key's hash is 0 modulo 32 (or at 128 items). The split is a pure
  function of the set of keys, so an incremental build and a full rebuild
  produce the same sections. Adding or removing an input changes only the
  section that holds it. This is the same idea as rsync's rolling chunks and
  prolly trees.
- **Hub pages** (more than 64 claims). Claims are in the same order as on a
  small page, by claim text and then claim id, and split into sections.
  - Section membership depends only on the claims. A fact whose citations
    move, or whose heading changes, stays in its section, and the page is only
    re-rendered, as any page would be.
  - Each section's heading gives its page-wide fact numbers and the documents
    it draws on, for example "Facts 129–256, from release-notes.md". It is
    computed from the citations, so it too changes only on a re-render.
  - Each section is written by the page writer, from at most 128 claims, with
    its own citation numbers, and cached by a signature over its claims.
  - Page-wide citation numbers are assigned when the page is assembled, so a
    new claim renumbers later citations without rewriting any text.
  - The lead is deterministic ("X has N documented facts…"). No LLM call
    reads the whole hub.
- **Large topics** (more than 64 pages). Pages are split into sections by page
  id. Each section is summarized from its pages' leads and cached by a
  signature over them. The topic summary is written from the section
  overviews, recursively if there are more than 64 sections.
  - If a regenerated section's overview comes out unchanged, the topic is cut
    off: early cutoff one level up.
- **Cost of an edit.** An edit to one fact rewrites the section that lost the
  old claim and the section that gained the new one: usually one or two
  sections, never the whole artifact. Each section has its own signature, so
  provenance and full-rebuild equality hold exactly as for any other
  artifact.
- **Why not group a hub's facts by document?** That was the first design. It
  made a section depend on its facts' provenance: a fact whose earliest source
  moved changed position, and rewrote the section it left and the one it
  joined. That broke the rule that moved citations only re-render, and it
  doubled DAKC's cost on the 100-document benchmark's one-paragraph edit.

Measured on the same FastAPI commit: 4,699 input tokens instead of 58,689, in
6 calls instead of 4. Over the 50 replayed commits: 148,030 input tokens
instead of 2,460,770, in 317 calls instead of 259. A full build makes 3–5%
more calls, because large artifacts are written in more, smaller pieces.

## Source change classification

Each scan hashes every file's raw bytes. It does not rely on modification
times.

| Observation | Classification |
|---|---|
| path known, hash equal | unchanged |
| path known, hash differs | modified |
| path unknown, hash matches a path that disappeared | **renamed**: id kept, zero recomputation except citations |
| path unknown otherwise | new |
| known path missing, not explained by a rename | deleted |

When several vanished files share the hash of several new files, they are
paired in sorted path order, deterministically.

## Deletion semantics

Deleting a source removes its passages, which removes them from claims'
support sets. A claim with an empty support set is **retracted**. An entity or
relationship with no claims is deleted, along with its page. A topic with no
pages loses its summary. Claims still supported elsewhere survive, and their
pages are only re-rendered (citations), not regenerated.

## Sentence-grounded extraction

Passages are split into sentences deterministically (`split_sentences`).
Every extracted claim carries its *evidence*: the sentences needed to state
it, including sentences that identify what a pronoun refers to. LLM
extractors are shown numbered sentences and asked for them. The extraction
cache stores evidence as sentence content hashes.

When a passage version replaces another (same source):

1. A sentence is **dirty** if its text is not in the replaced versions.
2. A sentence that may depend on earlier context is also dirty when any
   earlier sentence of the passage is dirty. That covers pronouns, "this" or
   "the X" references, and continuation words such as "However".
3. Claims whose evidence sentences are all clean are **reused verbatim**.
4. Only dirty sentences are sent to the extractor, with the whole passage as
   context. If no sentence is dirty (deletions, reordering), no call is made.

Without this, an LLM re-extracting an edited paragraph rewords the
unchanged facts, gives them new claim ids, and turns every page they feed
into a "change" (measured in docs/benchmark.md). The context rule is
conservative: it re-extracts more than strictly necessary rather than reuse
a claim whose antecedent changed. The coreference experiment in the
benchmark tests it.

## What a signature covers besides inputs

- **Provider fingerprints** cover model, temperature, prompt and output
  schema. Every passage records the extractor that produced its claims, and
  every page and summary records the writer or summarizer that generated it.
  An artifact built by a different provider is rebuilt even if no source
  changed.
- **DAKC's own logic** is versioned per layer (`dakc/versions.py`: parser,
  claim identity, entity resolution, rendering) and recorded with every
  build. A version change re-runs exactly that layer:
  - re-parse the sources;
  - re-derive claims from the extraction cache;
  - re-resolve entities;
  - re-render, with no provider calls.

  A store whose history predates versioning is treated as outdated in every
  layer.
- **Configuration** (entity aliases, provenance mode) is part of the config
  fingerprint.

## Provenance

Each claim support records `(passage, surface text, mentions, evidence
sentence indices)`. `dakc trace PAGE` maps every displayed sentence to the
claims it cites (`[^Cn]`), and every claim to the exact evidence sentences, the
passage, the source path, and the source version (content hash and the
build that registered it). Each evidence sentence also carries the HTML
comments written after it in the source (`refs`, see below). Under `provenance = "strict"`, rendering keeps only
sentences that cite one of the page's own claims. Removed sentences stay in
the page record as an audit list, and switching modes needs no LLM calls.

## Comments after a sentence: metadata outside every hash

In Markdown paragraphs and list items, an HTML comment written after a
sentence is kept on the passage as that sentence's `refs`:

```markdown
- The refresh endpoint takes a per-user Redis lock with a 5-second TTL. <!-- t:3f9a12c4 -->
```

gives the passage text `The refresh endpoint ... 5-second TTL.` and the refs
`[["t:3f9a12c4"]]`, one list per sentence of `split_sentences(text)`. A
comment belongs to the sentence its preceding text ends in. Comments in
headings and table cells are dropped, and a line that starts with `<!--` is an
HTML block in CommonMark, whose text DAKC does not read.

Refs are not text. They are outside the passage id, the text hash, the
extraction cache key and the sentence keys, and no claim, page or summary is
built from them; `trace` and `read` load them from the passage. So:

| Change | Work |
|---|---|
| A comment added, rewritten, moved or removed | the passage's new version (`passage_refs_changed`); zero provider calls, no page re-rendered |
| A sentence edited next to its comment | as any sentence edit: only that sentence is re-extracted (sentence reuse) |

The source itself still counts as modified (its bytes changed), so it is
re-parsed. That is the only cost. See ADR-29.

## Dry-run precision

Deterministic layers (scan, segmentation, sentence reuse planning, cache
lookup, entity resolution, signatures) are computed exactly in a dry run, so
the number of extraction calls is exact. The claims those calls would
return cannot be known without making them. With a *paid* extractor, the dry
run therefore estimates them: it runs the free heuristic extractor over the
new or edited sentences, and plans the pages and summaries those estimated
facts would touch. Downstream counts are an **estimate**, labelled as such
(`exact: false`). They are not a bound: an LLM may find facts, or entities,
that the heuristic does not. With the free heuristic extractor the whole dry
run is exact.

Before v0.5 the paid-provider plan treated the new facts as absent, and the
docs called its downstream counts an upper bound. For edits that add facts
that was wrong: the plan counted the extraction call but none of the pages it
would update.

## Invariants (checked by tests and benchmarks)

1. Unchanged corpus → 0 provider calls and 0 new artifact versions.
2. Modified source → every artifact whose inputs changed is rebuilt; the
   result equals a full rebuild (deterministic providers: byte-identical;
   LLM providers: structurally identical).
3. Deleted source → no claim with an empty support set remains valid.
4. Artifacts outside the true change set keep their content hashes.
5. A failed build leaves the store's state digest unchanged.
6. Re-running the same compile is idempotent.
7. Stored dependency edges equal the edges implied by current artifact
   contents (`dakc verify`).
8. `dakc compile --full` is always available as the reference.
