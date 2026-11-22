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
| 1 | `passage` | `psg:` + H(source id, normalized text hash, occurrence #) | normalized text of one block (paragraph, list item, PDF block…) | its source |
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

## Dry-run precision

Deterministic layers (scan, segmentation, sentence reuse planning, cache
lookup, entity resolution, signatures) are computed exactly in a dry run, so
the number of extraction calls is exact. The claims those calls would
return cannot be known without making them, so with a *paid* extractor the
dry run reports downstream counts as an **upper bound derived from the
current graph**. It labels them that way and does not present them as exact.
With the free heuristic extractor the whole dry run is exact.

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
