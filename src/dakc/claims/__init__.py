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


def heuristic_extract(passage_text: str) -> list[ExtractedClaim]:
    """Deterministic claim extraction: sentences with >= 2 words and >= 1 entity.

    A baseline, not an NLP pipeline: any capitalized or camel-case term is an
    entity candidate (so sentence-initial words such as "Nightly" can become
    entities). Use an LLM extractor for extraction quality; DAKC's invalidation
    guarantees do not depend on which extractor is used.
    """
    claims: list[ExtractedClaim] = []
    for sentence in split_sentences(passage_text):
        if len(_TOKEN.findall(sentence)) < 2:
            continue
        mentions = extract_mentions(sentence)
        if mentions:
            claims.append(ExtractedClaim(sentence, mentions))
    return claims


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
