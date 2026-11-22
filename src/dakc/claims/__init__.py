"""Passages, claim extraction (heuristic) and claim-version matching.

* Segmentation turns a parsed document into content-addressed passages.
* The heuristic extractor is deterministic and free: one claim per sentence
  that mentions at least one entity-like term. LLM extractors implement the
  same contract in :mod:`dakc.providers`.
* Claim matching links a new claim to the claim it most likely revised. It is
  used for history and reporting only, never to skip a rebuild (ADR-13).
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from rapidfuzz import fuzz

from dakc.hashing import h, normalize_claim, normalize_text, short
from dakc.parsing import ParsedDocument


@dataclass(frozen=True)
class Passage:
    id: str
    source_id: str
    text: str
    text_hash: str
    heading: str = ""
    occurrence: int = 0


def passage_id(source_id: str, text_hash: str, occurrence: int) -> str:
    return "psg:" + short(f"{source_id}\x00{text_hash}\x00{occurrence}")


def segment(source_id: str, doc: ParsedDocument) -> list[Passage]:
    """One passage per body block. Identical blocks in a source get an occurrence index."""
    seen: Counter[str] = Counter()
    out: list[Passage] = []
    for block in doc.body_blocks:
        text = normalize_text(block.text)
        if not text:
            continue
        th = h(text)
        occ = seen[th]
        seen[th] += 1
        out.append(Passage(passage_id(source_id, th, occ), source_id, text, th, block.heading, occ))
    return out


# --------------------------------------------------------------------------- #
# Heuristic extraction
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ExtractedClaim:
    text: str
    mentions: tuple[str, ...]
    # Indices (into split_sentences(passage)) of every sentence needed to state
    # the claim. Empty = unknown, which makes the claim non-reusable.
    evidence: tuple[int, ...] = ()


_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[])")
_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9+#]*(?:[.\-'][A-Za-z0-9+#]+)*")
_STOP = frozenset(
    """a an the this that these those it its it's they them their there here he she we you i
    and or but nor so yet for of in on at by to from with without within into onto over under
    about above below after before during since until while when where which who whom whose why how
    what if then else than as is are was were be been being has have had do does did not no yes
    all any each every some many most more much few other such only own same also both either neither
    however therefore moreover furthermore meanwhile otherwise instead thus hence although though because
    overview introduction summary notes note example examples see also first second third finally
    january february march april may june july august september october november december
    monday tuesday wednesday thursday friday saturday sunday
    one two three four five six seven eight nine ten new old use using used can could should would will
    must might shall let lets our your his her my me us""".split()
)


def _is_entity_token(tok: str) -> bool:
    if len(tok) < 2 or tok.casefold() in _STOP:
        return False
    return tok[0].isupper() or any(c.isupper() for c in tok[1:])


def extract_mentions(sentence: str) -> tuple[str, ...]:
    mentions: list[str] = []
    current: list[str] = []
    last_end = -1
    for m in _TOKEN.finditer(sentence):
        tok = m.group(0)
        if _is_entity_token(tok):
            if current and m.start() == last_end + 1 and sentence[last_end] == " ":
                current.append(tok)
            else:
                if current:
                    mentions.append(" ".join(current))
                current = [tok]
            last_end = m.end()
        else:
            if current:
                mentions.append(" ".join(current))
            current = []
            last_end = m.end()
    if current:
        mentions.append(" ".join(current))
    return tuple(sorted(set(mentions)))


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT_SPLIT.split(text) if s.strip()]


def heuristic_extract(passage_text: str, targets: tuple[int, ...] | None = None) -> list[ExtractedClaim]:
    """Deterministic claim extraction: sentences with >= 2 words and >= 1 entity.

    A baseline, not an NLP pipeline: any capitalized or camel-case term is an
    entity candidate (so sentence-initial words such as "Nightly" can become
    entities). Use an LLM extractor for extraction quality; DAKC's invalidation
    guarantees do not depend on which extractor is used.
    """
    claims: list[ExtractedClaim] = []
    wanted = None if targets is None else set(targets)
    for i, sentence in enumerate(split_sentences(passage_text)):
        if wanted is not None and i not in wanted:
            continue
        if len(_TOKEN.findall(sentence)) < 2:
            continue
        mentions = extract_mentions(sentence)
        if mentions:
            claims.append(ExtractedClaim(sentence, mentions, (i,)))
    return claims


# --------------------------------------------------------------------------- #
# Sentence-grounded reuse
# --------------------------------------------------------------------------- #
#
# When a passage changes, claims whose evidence sentences are all unchanged are
# reused verbatim instead of being re-generated (an LLM would reword them and
# give them new ids). Only changed sentences are sent for extraction. A sentence
# whose meaning may depend on an earlier sentence (pronouns, "the X" references,
# continuation words) is treated as changed whenever an earlier sentence in the
# passage changed.

_ANAPHORA = re.compile(
    r"\b(it|its|itself|they|them|their|theirs|themselves|he|him|his|she|her|hers|this|that|these|those|"
    r"such|former|latter|there|here|both|each|either|neither|same|above|below|aforementioned)\b",
    re.IGNORECASE,
)
_DEFINITE_START = re.compile(r"^(?:the|this|that|these|those|its|their)\s+[a-z]", re.IGNORECASE)
_CONTINUATION_START = re.compile(r"^(?:also|however|instead|otherwise|then|therefore|thus|so|but|and|or|which|whereas)\b", re.IGNORECASE)


def sentence_key(sentence: str) -> str:
    return h(normalize_text(sentence))


def is_context_dependent(sentence: str) -> bool:
    """Conservative: may the sentence's meaning depend on an earlier sentence?"""
    return bool(_ANAPHORA.search(sentence) or _DEFINITE_START.match(sentence) or _CONTINUATION_START.match(sentence))


def plan_reuse(
    prior: list[tuple[str, tuple[str, ...], tuple[str, ...]]],
    prior_sentence_keys: set[str],
    passage_text: str,
    context_rule: bool = True,
) -> tuple[list[ExtractedClaim], list[int]]:
    """Decide which prior claims can be reused for a new passage version.

    ``prior``: (claim text, mentions, evidence sentence keys) from extractions of
    the passage versions being replaced. ``prior_sentence_keys``: keys of every
    sentence in those versions. Returns (reused claims with evidence remapped to
    the new sentence indices, indices of sentences that must be extracted).

    ``context_rule=False`` relies only on the extractor's declared evidence
    (a claim is reused only if every sentence it depends on is unchanged) and
    skips the pattern-based re-extraction of context-dependent sentences.
    """
    sents = split_sentences(passage_text)
    keys = [sentence_key(x) for x in sents]
    dirty: set[int] = {i for i, k in enumerate(keys) if k not in prior_sentence_keys}
    if context_rule:
        for i, sentence in enumerate(sents):
            if i not in dirty and is_context_dependent(sentence) and any(j < i for j in dirty):
                dirty.add(i)
    dirty_keys = {keys[i] for i in dirty}
    clean_pos: dict[str, int] = {}
    for i, k in enumerate(keys):
        if i not in dirty and k not in dirty_keys:
            clean_pos.setdefault(k, i)
    reused: list[ExtractedClaim] = []
    seen: set[str] = set()
    for text, mentions, ev_keys in prior:
        if not ev_keys or not all(k in clean_pos for k in ev_keys):
            continue
        ident = normalize_claim(text)
        if ident in seen:
            continue
        seen.add(ident)
        reused.append(ExtractedClaim(text, tuple(mentions), tuple(sorted({clean_pos[k] for k in ev_keys}))))
    return reused, sorted(dirty)


# --------------------------------------------------------------------------- #
# Claim-version matching (history only)
# --------------------------------------------------------------------------- #

_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def match_revisions(removed: dict[str, str], added: dict[str, str], threshold: float = 85.0) -> list[tuple[str, str, float]]:
    """Pair removed claims with added claims that look like revisions of them.

    ``removed``/``added`` map claim id -> text. Greedy best-first matching with
    RapidFuzz ``token_sort_ratio``. Pairs are reported as (old id, new id,
    score). The result is used to label changes ("claim modified") and to
    record ``supersedes`` links; it never decides whether work is skipped.
    """
    candidates: list[tuple[float, str, str]] = []
    for old_id, old_text in removed.items():
        a = normalize_claim(old_text)
        for new_id, new_text in added.items():
            b = normalize_claim(new_text)
            score = fuzz.token_sort_ratio(a, b)
            if score >= threshold:
                candidates.append((score, old_id, new_id))
    candidates.sort(key=lambda t: (-t[0], t[1], t[2]))
    used_old: set[str] = set()
    used_new: set[str] = set()
    pairs: list[tuple[str, str, float]] = []
    for score, old_id, new_id in candidates:
        if old_id in used_old or new_id in used_new:
            continue
        used_old.add(old_id)
        used_new.add(new_id)
        pairs.append((old_id, new_id, score))
    return sorted(pairs, key=lambda p: (p[0], p[1]))


def numbers_differ(a: str, b: str) -> bool:
    return _NUMBER.findall(a) != _NUMBER.findall(b)


# --------------------------------------------------------------------------- #
# Advisory duplicate detection (never used for identity)
# --------------------------------------------------------------------------- #

_NEGATION = re.compile(r"\b(not|no|never|without|n't)\b", re.IGNORECASE)


def likely_same_fact(a: str, b: str) -> bool:
    """Conservative deterministic paraphrase test: same entities in the same
    order, same numbers, same negation polarity, and similar remaining words.
    Measured on a labelled set (benchmarks/equivalence.py) with zero false
    merges but limited recall. DAKC uses it only to *report* likely duplicates;
    claim identity stays exact, so a mistake here cannot corrupt the graph."""
    if normalize_claim(a) == normalize_claim(b):
        return True
    order_a = sorted(extract_mentions(a), key=a.find)
    order_b = sorted(extract_mentions(b), key=b.find)
    if order_a != order_b or not order_a:
        return False
    if _NUMBER.findall(a) != _NUMBER.findall(b) or bool(_NEGATION.search(a)) != bool(_NEGATION.search(b)):
        return False
    drop = {e.casefold() for e in order_a}

    def rest(s: str) -> str:
        return " ".join(w for w in normalize_claim(s).split() if w not in drop)

    return fuzz.token_set_ratio(rest(a), rest(b)) >= 80


def duplicate_candidates(claims: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Pairs of claim ids that are likely the same fact. Claims are bucketed by
    entity set, numbers and polarity first, so cost stays near-linear."""
    buckets: dict[tuple, list[tuple[str, str]]] = {}
    for cid, text in claims:
        key = (tuple(sorted(m.casefold() for m in extract_mentions(text))), tuple(_NUMBER.findall(text)), bool(_NEGATION.search(text)))
        if key[0]:
            buckets.setdefault(key, []).append((cid, text))
    out: list[tuple[str, str]] = []
    for group in buckets.values():
        if len(group) > 200:  # pathological bucket: skip rather than go quadratic
            continue
        for i, (ca, ta) in enumerate(group):
            for cb, tb in group[i + 1:]:
                if likely_same_fact(ta, tb):
                    out.append(tuple(sorted((ca, cb))))
    return sorted(set(out))
