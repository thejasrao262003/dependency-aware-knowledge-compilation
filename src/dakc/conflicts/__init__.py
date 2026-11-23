"""Conflict detection (RFC 0002, ``docs/rfcs/0002-conflicts.md``).

Advisory and deterministic. It flags pairs of claims, from any sources, that
cannot both be true in the same scope. It never changes a claim or a page: the
sources stay the truth, and a conflict disappears when the sources agree.

A pair is a **conflict** when all of these hold:

1. **Same frame.** Both claims say the same thing about the same subject, apart
   from one value or a negation, after normalization: contractions are
   expanded, past tense is mapped to present, and plurals are stemmed crudely.
   "X is the Y" is also rewritten as "the Y is X".
2. **Incompatible.** Either one negates the other ("supports" against "does not
   support"), or they give different values for a single-valued property: a
   definite role ("the primary database is …"), a number ("runs on three
   nodes"), or a functional attribute ("default port", "licensed under",
   "version").
3. **Same scope.** Same status (current, past, proposed, conditional), same time
   or release markers, and same environment markers. Scope is read from the
   sentence, its heading path, its file path, and the ``[[conflicts.scope]]``
   rules in ``dakc.toml``.

Pairs that meet 1 and 2 but differ in scope are recorded as **scoped**: a
difference explained by time, release, environment or status, such as MongoDB
in release 1 and PostgreSQL in production. Everything else is not reported.
This includes paraphrases ("JSONB is available in PostgreSQL"), different
values of a multi-valued property ("supports JSONB", "supports Indexing"), and
different subjects sharing a value.

The rules favour precision. A contradiction phrased with different words, such
as "JSONB is unavailable in PostgreSQL", is not caught; that is the job of an
optional verifier (RFC 0002, phase 2).
"""

from __future__ import annotations

import fnmatch
import functools
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from dakc.hashing import entity_key, h

CONTRACTIONS = {
    "doesn't": "does not", "don't": "do not", "didn't": "did not", "isn't": "is not", "aren't": "are not",
    "wasn't": "was not", "weren't": "were not", "can't": "can not", "cannot": "can not", "won't": "will not",
    "hasn't": "has not", "haven't": "have not", "hadn't": "had not", "shouldn't": "should not",
    "couldn't": "could not", "wouldn't": "would not", "mustn't": "must not",
}
NEGATIONS = {"not", "no", "never", "without", "none"}
AUX_BEFORE_NOT = {"does", "do", "did"}
PAST_TO_PRESENT = {"was": "is", "were": "is", "are": "is", "had": "has"}
# A word that states the opposite of another, read as "not <other>" when it is the predicate
# ("caching is disabled" = "caching is not enabled"). Only after a copula: "a private key"
# is not "not a public key".
ANTONYMS = {"disabled": "enabled", "optional": "required", "asynchronous": "synchronous", "async": "synchronous",
            "private": "public", "stateful": "stateless", "immutable": "mutable", "unencrypted": "encrypted",
            "unavailable": "available", "unsupported": "supported", "incompatible": "compatible",
            "forbidden": "allowed", "prohibited": "allowed", "disallowed": "allowed", "inactive": "active",
            "insecure": "secure", "read-only": "writable", "readonly": "writable", "non-blocking": "blocking",
            "nonblocking": "blocking", "manual": "automatic", "offline": "online", "unauthenticated": "authenticated",
            "unsigned": "signed", "untrusted": "trusted", "invalid": "valid"}
PREDICATE_BEFORE = {"is", "be", "been", "being", "remain", "stay", "become"}
# Words and names written more than one way, compared as one. ``[aliases]`` in dakc.toml adds more.
SYNONYMS = {"mandatory": "required", "sync": "synchronous", "db": "database", "datastore": "database",
            "postgres": "postgresql", "k8s": "kubernetes", "mongo": "mongodb", "golang": "go"}
COPULAS = {"is", "are"}
# "used" ("is used by"), "legacy" ("legacy clients") and "deprecated" ("X is deprecated") describe
# things, not the time of a statement. A heading or path that says "Deprecated" still marks a section.
PAST_MARKERS = {"was", "were", "had", "previously", "formerly", "originally", "initially", "earlier", "until",
                "former", "old"}
# "X was deprecated / removed / replaced": a change whose result still holds, so the statement is current.
_LASTING_CHANGE = re.compile(
    r"\b(?:was|were|has been|have been)\s+(?:(?:also|then|later|eventually|fully|officially|formally|since)\s+)?"
    r"(?:deprecated|removed|replaced|dropped|discontinued|retired|renamed|superseded|sunset|archived|disabled|deleted"
    r"|migrated|moved|merged)\b")
# "used to read" is past; "is used to store" is not.
_USED_TO = re.compile(r"(?<!is )(?<!are )(?<!was )(?<!were )(?<!be )(?<!been )(?<!being )\bused to\b")
# "may" that grants a limit is permission, not a proposal: "each tenant may send up to 500 requests".
_PERMISSION = re.compile(r"\bmay\s+(?:not\s+)?(?:[a-z\-]+\s+){0,3}?(?:up to|at most|no more than|a maximum of|not exceed)\b")
PROPOSED_MARKERS = {"should", "could", "may", "might", "would", "plan", "plans", "planned", "planning",
                    "propose", "proposed", "proposal", "consider", "considering", "considered",
                    "option", "optionally", "intend", "intends", "roadmap", "eventually", "future"}
# "The recommended algorithm is Argon2" states current guidance: two documents recommending
# different algorithms is a conflict worth flagging, so "recommended" is not a proposal marker.
CONDITIONAL_MARKERS = {"if", "unless", "when", "whenever", "assuming"}
FUNCTIONAL = {"default", "primary", "main", "maximum", "max", "minimum", "min", "limit", "version", "licensed",
              "license", "released", "port", "timeout", "owner", "located", "headquartered", "founded", "capacity", "size", "only", "sole", "official", "canonical", "current",
              "latest", "language", "runtime", "engine", "database", "datastore", "backend", "frontend", "host",
              "hosted", "region", "ttl", "interval", "retention", "quota", "price", "cost", "deadline", "date"}
FUNCTIONAL_PHRASES = ("maintained by", "owned by", "written in", "licensed under", "hosted on", "deployed to",
                      "deployed on", "located in", "managed by", "built with", "implemented in")
MULTI_VALUED_VERBS = {"support", "use", "include", "integrate", "provide", "offer", "depend", "contain", "enable",
                      "allow", "connect", "publish", "consume", "read", "write", "accept", "handle", "expose",
                      "call", "emit", "produce", "send", "receive", "work", "mention", "list"}
IDENTIFIER_BEFORE = {"pr", "issue", "ticket", "id", "commit", "rfc", "adr", "section", "chapter", "page", "step",
                     "line", "item", "no", "number", "python", "node", "java", "go", "version", "v", "release", "phase"}
LIMIT_BEFORE = {"to", "most", "least", "exactly", "max", "maximum", "minimum", "min", "of", "under", "over", "every"}
UNIT_AFTER = {"node", "replica", "connection", "writer", "reader", "worker", "thread", "core", "cpu", "gb", "mb", "kb",
              "tb", "ms", "second", "minute", "hour", "day", "week", "month", "year", "request", "user", "instance",
              "shard", "partition", "server", "machine", "pod", "container", "retry", "attempt", "byte", "row",
              "item", "char", "character", "token", "percent", "%", "time", "copy", "zone", "region", "slot"}
# Sentences that start with these record an event or are fragments without a subject
# ("Update X to 0.7.0", "Bump X from 1 to 2", "Defaults to 9"): not statements of a property.
IMPERATIVE_START = {"add", "update", "bump", "fix", "remove", "refactor", "rename", "upgrade", "downgrade", "improve",
                    "use", "set", "return", "default", "allow", "make", "move", "deprecate", "drop", "enable",
                    "disable", "install", "run", "create", "delete", "see", "note", "check", "read", "learn", "get",
                    "call", "pass", "import", "replace", "revert", "merge", "change", "tweak", "simplify", "document",
                    "translate", "clarify", "release", "publish", "configure", "ensure", "avoid", "try", "pin", "unpin",
                    "restore", "introduce", "extend", "optimize", "reduce", "increase", "decrease", "switch"}
MAX_VALUES_PER_FRAME = 12  # more distinct values than this: an enumeration ("PR #… by …"), not a property
NUMBER_WORDS = {"zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven",
                "twelve", "twenty", "thirty", "forty", "fifty", "hundred", "thousand", "million", "billion"}

_TIME = re.compile(
    r"\b(?:v?\d+\.\d+(?:\.\d+)*|(?:release|version|v|r|phase|sprint|milestone|iteration|stage)\s*\d+[a-z]?"
    r"|(?:19|20)\d{2}|q[1-4]|h[12]|mvp|beta|alpha|ga|preview|pilot|poc|prototype|legacy|deprecated|archived?"
    r"|history|historical|changelog|release notes|migration)\b")
_ENV = re.compile(
    r"\b(?:production|prod|staging|stage|development|dev|test|testing|qa|uat|sandbox|local|on-?prem(?:ises)?"
    r"|self-hosted|cloud|linux|windows|macos|ios|android|enterprise edition|community edition"
    r"|(?:us|eu|ap|sa|ca|me|af)-[a-z]+-\d)\b")
_STATUS_HEADING = {
    "past": re.compile(r"\b(?:history|historical|legacy|deprecated|archived?|old|previous|former|changelog|release notes|migration)\b"),
    "proposed": re.compile(r"\b(?:proposals?|proposed|drafts?|rfcs?|rejected|alternatives?|options considered|considered|future|roadmap|ideas?|wishlist|plans?)\b"),
    "quoted": re.compile(r"\b(?:faq|myths?|misconceptions?|examples?|quotes?|anti-?patterns?)\b"),
}
# scope phrases removed from the sentence before frames are compared (their markers are kept as scope)
_SCOPE_PHRASE = re.compile(
    r"(?:^|(?<=[\s,(]))(?:in|for|on|during|since|as of|until|before|after|from|with)\s+(?:the\s+)?"
    r"(?:[a-z0-9.\-]+\s+){0,3}?(?:" + _TIME.pattern[2:-2] + "|" + _ENV.pattern[2:-2] + r")\b(?:\s+[a-z0-9.\-]+){0,2}?"
    r"(?:\s*\([^)]*\))?\s*,?")
_PAREN = re.compile(r"\([^)]*\)")
_REGION = re.compile(r"(?:us|eu|ap|sa|ca|me|af)-[a-z]+-\d")
_PRODUCT_VERSION = re.compile(r"([a-z][a-z0-9+#\-]*)\s+(v?\d+\.\d+(?:\.\d+)*)\b")
_NOT_PRODUCTS = {"release", "version", "v", "r", "phase", "sprint", "milestone", "iteration", "stage", "the", "a", "an",
                 "in", "on", "for", "with", "from", "to", "of", "as", "since", "until", "before", "after", "during"}


def _scope_phrases(s: str) -> list[re.Match]:
    """The phrases of a sentence that limit its scope ("in release 1", "in production").

    A product's version or a region after the verb is what the sentence states, not where it
    applies: "Catalog runs on Python 3.11", "Atlas lives in the eu-west-1 region". At the
    start of a sentence, set off by a comma, or after a word of time ("since 3.11"), a
    phrase stays a scope."""
    out = []
    for m in _SCOPE_PHRASE.finditer(s):
        before, phrase = s[:m.start()].rstrip(), m.group(0).strip()
        lead = phrase.split()[0] if phrase else ""
        if before and not before.endswith((",", "(")) and lead not in {"since", "until", "before", "after", "during", "from", "as"}:
            markers = _TIME.findall(phrase) + _ENV.findall(phrase)
            values = {x for x in markers if _REGION.fullmatch(x)}
            if lead in {"on", "with", "for"}:
                values |= {v for w, v in _PRODUCT_VERSION.findall(phrase) if w not in _NOT_PRODUCTS}
            if markers and all(x in values for x in markers):
                continue  # values, not a scope
        out.append(m)
    return out
# A file name that carries a revision number is a snapshot of that revision: techstack001.md
# beside techstack.md, or decision record 0002 superseding 0001. Zero-padded, so that oauth2.md,
# http2.md or python3.md are not read as revisions.
_REVISION_SUFFIX = re.compile(r"(?:^|[a-z][-_ ]?)(0\d{2,})$")
_RECORD_PREFIX = re.compile(r"^(\d{3,5})[-_ ]")
_RECORD_DIRS = re.compile(r"(?:^|/)(?:adrs?|decisions?|decision-records?|architecture-decisions?)(?:/|$)")
_TOKEN = re.compile(r"[a-z0-9][a-z0-9.\-+#]*|_")
_NUMBER = re.compile(r"^\d+(?:[.,]\d+)*(?:%|[a-z]{1,3})?$")


@dataclass(frozen=True)
class Scope:
    status: str  # current | past | proposed | conditional | quoted
    time: frozenset[str] = frozenset()
    env: frozenset[str] = frozenset()

    def label(self) -> str:
        parts = [] if self.status == "current" else [self.status]
        parts += sorted(self.time) + sorted(self.env)
        return ", ".join(parts) or "current"


@dataclass(frozen=True)
class Occurrence:
    claim_id: str
    passage_id: str
    path: str
    heading: str
    sentence: str


@dataclass
class Parsed:
    occ: Occurrence
    scope: Scope
    polarity: int
    tokens: list[str]
    frames: list[tuple[str, str, bool]] = field(default_factory=list)  # (frame key, value, functional)

    @property
    def negation_key(self) -> str:
        return " ".join(self.tokens)


@dataclass
class ScopeRule:
    pattern: str
    status: str | None = None
    label: str | None = None

    def applies(self, path: str) -> bool:
        return fnmatch.fnmatch(path, self.pattern) or fnmatch.fnmatch(path, self.pattern.rstrip("/") + "/*")


def _stem(tok: str) -> str:
    if len(tok) > 4 and tok.endswith("ies"):
        return tok[:-3] + "y"  # retries -> retry, policies -> policy
    if len(tok) > 4 and tok.endswith(("sses", "xes", "shes")):
        return tok[:-2]  # processes -> process, indexes -> index (not "-ches": caches, matches)
    if len(tok) > 3 and tok.endswith("s") and not tok.endswith(("ss", "us", "is")):
        return tok[:-1]
    return tok


def _expand(text: str) -> str:
    t = text.lower().replace("’", "'")
    t = re.sub(r"\bno longer\b", "not", t)
    for k, v in CONTRACTIONS.items():
        t = t.replace(k, v)
    return re.sub(r"'s\b", "", t)


def scope_of(sentence: str, heading: str, path: str, rules: list[ScopeRule] | None = None) -> Scope:
    s, hd, p = _expand(sentence), heading.lower(), path.lower().replace("_", " ").replace("/", " / ")
    words = set(_TOKEN.findall(s))
    status = "current"
    for name, rx in _STATUS_HEADING.items():
        if rx.search(hd) or rx.search(p):
            status = name
            break
    if status == "current":
        lead = s.split(",")[0].split()[:2]
        if CONDITIONAL_MARKERS & set(lead):
            status = "conditional"
        elif PROPOSED_MARKERS & words and not (PROPOSED_MARKERS & words == {"may"} and _PERMISSION.search(s)):
            status = "proposed"
        elif PAST_MARKERS & set(_TOKEN.findall(_LASTING_CHANGE.sub(" ", s))) or _USED_TO.search(s):
            status = "past"
    phrases = " ".join(m.group(0) for m in _scope_phrases(s))
    time = set(_TIME.findall(" ".join(_PAREN.findall(s)) + " " + phrases))
    time |= set(_TIME.findall(hd)) | set(_TIME.findall(p))
    env = set(_ENV.findall(phrases)) | set(_ENV.findall(hd)) | set(_ENV.findall(p))
    stem = PurePosixPath(path.lower()).stem
    m = _REVISION_SUFFIX.search(stem)
    if m:
        time.add(f"revision {m.group(1)}")
    m = _RECORD_PREFIX.match(stem)
    if m and _RECORD_DIRS.search(path.lower()):
        time.add(f"record {m.group(1)}")
    for r in rules or ():
        if r.applies(path):
            if r.status:
                status = r.status
            if r.label:
                time.add(r.label.lower())
    time -= {"legacy", "deprecated", "archived", "archive", "history", "historical", "changelog", "release notes"}
    return Scope(status, frozenset(time), frozenset(env))


@functools.lru_cache(maxsize=1 << 16)
def _ekey(text: str) -> str:
    return entity_key(text)


def _apply_aliases(raw: list[str], aliases: dict[str, str]) -> list[str]:
    """Replace every name written as an alias (one to three words) with its canonical name."""
    if not aliases:
        return raw
    out, i = [], 0
    while i < len(raw):
        for n in (3, 2, 1):
            canon = aliases.get(_ekey(" ".join(raw[i:i + n]))) if i + n <= len(raw) else None
            if canon is not None:
                out += _TOKEN.findall(canon.lower())
                i += n
                break
        else:
            out.append(raw[i])
            i += 1
    return out


def _word(t: str) -> str:
    t = SYNONYMS.get(t, t)
    return _stem(PAST_TO_PRESENT.get(t, t))


def _canonical_phrases(toks: list[str]) -> tuple[list[str], int]:
    """Write equivalent phrasings of support one way. Returns the tokens and -1 when the
    rewrite negated the statement ("lacks X", "dropped support for X")."""
    sign, out, i = 1, [], 0
    while i < len(toks):
        t, n1, n2 = toks[i], toks[i + 1] if i + 1 < len(toks) else "", toks[i + 2] if i + 2 < len(toks) else ""
        if n1 == "support" and n2 == "for" and t in {"has", "have", "offer", "provide", "dropped", "removed", "ended",
                                                     "discontinued", "deprecated"}:
            out.append("support")
            sign = -sign if t in {"dropped", "removed", "ended", "discontinued", "deprecated"} else sign
            i += 3
        elif t in {"lack", "lacking"}:
            out.append("support")
            sign = -sign
            i += 1
        elif t == "work" and i > 0:  # "works on X", or "works" when "on Python 3.8" was read as scope
            out.append("support")
            i += 2 if n1 in {"on", "with"} else 1
        elif t == "is" and n1 == "compatible":
            out.append("support")
            i += 3 if n2 == "with" else 2
        else:
            out.append(t)
            i += 1
    # passive and availability: "B is supported by A", "B is available in A" -> "A support B"
    for i in range(1, len(out) - 3):
        if out[i] == "is" and out[i + 1] in {"supported", "available"} and out[i + 2] in {"by", "in", "on", "for"}:
            return out[i + 3:] + ["support"] + out[:i], sign
    return out, sign


def parse(occ: Occurrence, mentions: tuple[str, ...], rules: list[ScopeRule] | None = None,
          aliases: dict[str, str] | None = None) -> Parsed:
    """``aliases``: normalized alias key -> canonical name (the workspace's ``[aliases]``)."""
    aliases = aliases or {}
    scope = scope_of(occ.sentence, occ.heading, occ.path, rules)
    text = _expand(occ.sentence)
    for m in reversed(_scope_phrases(text)):  # kept as scope; values stay in the statement
        text = text[:m.start()] + " " + text[m.end():]
    text = _PAREN.sub(" ", text)
    text = re.sub(r"\b(?:until|since|as of|before|after)\b.*$", " ", text)  # trailing time clause
    text = re.sub(r"^\s*(?:if|unless|when|whenever|assuming)\b[^,]*,", " ", text)  # leading condition (kept as scope)
    raw = [t.rstrip(".-,") for t in _TOKEN.findall(text)]
    raw = _apply_aliases([t for t in raw if t], aliases)
    polarity, toks = 1, []
    for i, t in enumerate(raw):
        if t in NEGATIONS:
            polarity = -polarity
            if i and raw[i - 1] in AUX_BEFORE_NOT and toks:
                toks.pop()
            continue
        if t in {"previously", "formerly", "originally", "initially", "still", "currently", "now", "also", "only"}:
            continue
        if t in ANTONYMS and toks and toks[-1] in PREDICATE_BEFORE:
            polarity = -polarity
            t = ANTONYMS[t]
        toks.append(_word(t))
    if toks and toks[0] in IMPERATIVE_START:
        return Parsed(occ, scope, polarity, [])  # an event or a fragment: nothing to compare
    toks, sign = _canonical_phrases(toks)
    polarity *= sign
    # "X is the Y" -> "the Y is X", so both word orders share a frame
    if len(toks) > 3 and toks[0] != "the" and toks[1] in COPULAS and toks[2] == "the":
        toks = toks[2:] + [toks[1]] + toks[:1]
    parsed = Parsed(occ, scope, polarity, toks)
    ments = [aliases.get(_ekey(m), m).lower().removesuffix("'s") for m in mentions]
    spans: list[tuple[int, int]] = []
    for m in sorted(ments, key=len, reverse=True):
        mt = [_word(x) for x in _TOKEN.findall(m)]
        for i in range(len(toks) - len(mt) + 1):
            if mt and toks[i:i + len(mt)] == mt and not any(a <= i < b for a, b in spans):
                spans.append((i, i + len(mt)))
    spans += [(i, i + 1) for i, t in enumerate(toks) if (_NUMBER.match(t) or t in NUMBER_WORDS) and not any(a <= i < b for a, b in spans)]
    verbs = {i for i, t in enumerate(toks) if t in COPULAS or t in MULTI_VALUED_VERBS or t in {"has", "run", "store", "require", "listen", "cost", "take", "support"}}
    first_verb = min(verbs) if verbs else None
    for a, b in spans:
        value = " ".join(toks[a:b])
        is_number = b - a == 1 and (bool(_NUMBER.match(toks[a])) or toks[a] in NUMBER_WORDS)
        if is_number:
            before = toks[a - 1] if a else ""
            after = toks[b] if b < len(toks) else ""
            if before in IDENTIFIER_BEFORE:
                continue  # "PR #10842", "Python 3.15": an identifier, not a measurement
            measured = before in LIMIT_BEFORE or after in UNIT_AFTER or bool(set(toks[:a]) & FUNCTIONAL)
            if not measured:
                continue
        after_verb = first_verb is not None and a > first_verb
        at_end = b >= len(toks) - 1
        if not (is_number or (after_verb and at_end)):
            continue  # only values in object position (or numbers) are slots; subjects are never values
        if not is_number:  # an object value runs to the end of the sentence ("the MIT license")
            b = len(toks)
            value = " ".join(toks[a:b])
        frame_toks = toks[:a] + ["_"] + toks[b:]
        pred = set(frame_toks[: frame_toks.index("_")])
        definite_role = toks[0] == "the" and first_verb is not None and toks[first_verb] in COPULAS
        prefix = " ".join(frame_toks[: frame_toks.index("_")])
        phrase = any(ph in prefix for ph in FUNCTIONAL_PHRASES)
        multi = bool(pred & MULTI_VALUED_VERBS) and not (pred & FUNCTIONAL) and not phrase
        functional = is_number or ((definite_role or bool(pred & FUNCTIONAL) or phrase) and not multi)
        parsed.frames.append((" ".join(frame_toks), value, functional))
    return parsed


@dataclass
class Finding:
    """One property whose sources disagree: every value, with every place that states it."""

    id: str
    kind: str  # conflict | scoped
    property: str  # the shared frame, readable: "the database is …"
    reason: str
    values: list[dict[str, Any]]  # [{value, polarity, occurrences: [{claim_id, passage_id, path, heading, sentence, scope}]}]

    def payload(self) -> dict[str, Any]:
        return {"kind": self.kind, "property": self.property, "reason": self.reason, "values": self.values}

    @property
    def claim_ids(self) -> list[str]:
        return sorted({o["claim_id"] for v in self.values for o in v["occurrences"]})


def _occ(p: Parsed) -> dict[str, Any]:
    return {"claim_id": p.occ.claim_id, "passage_id": p.occ.passage_id, "path": p.occ.path,
            "heading": p.occ.heading, "sentence": p.occ.sentence, "scope": p.scope.label()}


def _values(members: list[tuple[Parsed, str]]) -> list[dict[str, Any]]:
    by_value: dict[str, list[Parsed]] = defaultdict(list)
    for p, v in members:
        by_value[v].append(p)
    out = []
    for v in sorted(by_value):
        ps = sorted(by_value[v], key=lambda p: (p.occ.path, p.occ.claim_id, p.occ.passage_id))
        seen, occs = set(), []
        for p in ps:
            if (p.occ.claim_id, p.occ.passage_id) not in seen:
                seen.add((p.occ.claim_id, p.occ.passage_id))
                occs.append(_occ(p))
        out.append({"value": _as_written(v, [p.occ.sentence for p in ps]), "occurrences": occs})
    return out


def _as_written(value: str, sentences: list[str]) -> str:
    """The value as a document writes it ("MongoDB", not "mongodb"), when it can be found."""
    rx = re.compile(r"(?<![\w])" + re.escape(value).replace("\\ ", r"\s+") + r"(?![\w])", re.IGNORECASE)
    for sentence in sentences:
        m = rx.search(sentence)
        if m:
            return re.sub(r"\s+", " ", m.group(0))
    return value


def _judge(key: str, members: list[tuple[Parsed, str]], readable: str, why: str) -> Finding | None:
    """members: (parsed occurrence, value) sharing one property. A conflict if some scope holds
    more than one value; scoped if values differ only across scopes; nothing otherwise."""
    if len({v for _, v in members}) < 2 or len({p.occ.claim_id for p, _ in members}) < 2:
        return None
    buckets: dict[Scope, list[tuple[Parsed, str]]] = defaultdict(list)
    for p, v in members:
        buckets[p.scope].append((p, v))
    clashing = [b for sc, b in buckets.items() if sc.status in ("current", "past") and len({v for _, v in b}) > 1
                and len({p.occ.claim_id for p, _ in b}) > 1]
    fid = "cfl:" + h(key)[:10]
    if clashing:
        members_c = [m for b in clashing for m in b]
        scopes = sorted({p.scope.label() for p, _ in members_c})
        plain = all(p.scope == Scope("current") for p, _ in members_c)
        reason = (f"{why}, and none of them is limited to a release, time or environment" if plain
                  else f"{why}, in the same scope ({'; '.join(scopes)})")
        return Finding(fid, "conflict", readable, reason, _values(members_c))
    labels = sorted({f"{_as_written(v, [p.occ.sentence])}: {p.scope.label()}" for p, v in members}, key=str.casefold)
    statuses = {p.scope.status for p, _ in members}
    if len({p.scope for p, _ in members}) == 1:  # one scope, but not a statement of fact
        why = f"{why}, but only in {statuses.pop()} statements"
    else:
        why = f"{why}, but in different scopes"
    return Finding(fid, "scoped", readable, f"{why} ({'; '.join(labels)})", _values(members))


DETECTOR_VERSION = "conflicts/7"  # bump on any change to parsing, scope or judging: stores re-judge everything

# One index row per comparable key of a claim occurrence:
# (key, claim_id, passage_id, value, polarity, scope_json, path, heading, sentence)
Row = tuple[str, str, str, str, int, str, str, str, str]


def _scope_json(sc: Scope) -> str:
    import json

    return json.dumps({"status": sc.status, "time": sorted(sc.time), "env": sorted(sc.env)}, sort_keys=True)


def index_rows(occurrences: list[tuple[Occurrence, tuple[str, ...], str]], rules: list[ScopeRule] | None = None,
               aliases: dict[str, str] | None = None) -> list[Row]:
    """Parse claim occurrences into the comparable keys conflicts are found by."""
    rows: list[Row] = []
    for occ, mentions, _text in occurrences:
        p = parse(occ, mentions, rules, aliases)
        sj = _scope_json(p.scope)
        base = (occ.claim_id, occ.passage_id)
        if len(p.tokens) >= 2:
            rows.append(("neg|" + p.negation_key, *base, "is stated" if p.polarity > 0 else "is negated", p.polarity, sj,
                         occ.path, occ.heading, occ.sentence))
        for key, value, functional in p.frames:
            if functional:
                rows.append((f"val|{key}|{p.polarity}", *base, value, p.polarity, sj, occ.path, occ.heading, occ.sentence))
    return rows


def judge(key: str, rows: list[Row]) -> Finding | None:
    """The finding (if any) for every index row sharing one key."""
    import json

    members: list[tuple[Parsed, str]] = []
    for _k, cid, pid, value, polarity, sj, path, heading, sentence in rows:
        sc = json.loads(sj)
        scope = Scope(sc["status"], frozenset(sc["time"]), frozenset(sc["env"]))
        members.append((Parsed(Occurrence(cid, pid, path, heading, sentence), scope, polarity, []), value))
    if key.startswith("neg|"):
        return _judge(key, members, _readable_negation(key, members), "one source states what another negates")
    if len({v for _, v in members}) > MAX_VALUES_PER_FRAME:
        return None  # an enumeration, not a property
    return _judge(key, members, _readable_value(key, members), "sources give different values for a single-valued property")


def _clean(sentence: str) -> str:
    return re.sub(r"\s+", " ", sentence).strip().rstrip(".")


def _readable_negation(key: str, members: list[tuple[Parsed, str]]) -> str:
    """The property in the documents' own words: the shortest sentence that states it."""
    stated = sorted((_clean(p.occ.sentence) for p, v in members if v == "is stated"), key=lambda t: (len(t), t))
    return stated[0] if stated else key[4:]


def _readable_value(key: str, members: list[tuple[Parsed, str]]) -> str:
    """A sentence that states the property, with its value replaced by an ellipsis."""
    for p, value in sorted(members, key=lambda m: (len(m[0].occ.sentence), m[0].occ.sentence)):
        text = _clean(p.occ.sentence)
        m = re.search(re.escape(value).replace("\\ ", r"\s+"), text, flags=re.IGNORECASE)
        if m:
            return (text[:m.start()] + "…" + text[m.end():]).strip()
    return key[4:].rsplit("|", 1)[0].replace("_", "…")


def detect(occurrences: list[tuple[Occurrence, tuple[str, ...], str]], rules: list[ScopeRule] | None = None,
           aliases: dict[str, str] | None = None) -> dict[str, Finding]:
    """Every finding for a set of claim occurrences (the full, non-incremental computation)."""
    groups: dict[str, list[Row]] = defaultdict(list)
    for row in index_rows(occurrences, rules, aliases):
        groups[row[0]].append(row)
    out: dict[str, Finding] = {}
    for key, rows in groups.items():
        f = judge(key, rows)
        if f:
            out[f.id] = f
    return out


def detect_in_store(store, rules: list[ScopeRule] | None = None, aliases: dict[str, str] | None = None) -> dict[str, Finding]:
    """Every finding over the claims at HEAD, computed from scratch (what the incremental
    index must equal; ``dakc verify`` checks it)."""
    st = store.load_state()
    passages, sources = st.of("passage"), st.sources
    occ = []
    for c in st.of("claim").values():
        for pid, surface, mentions, _ev in c.supports:
            p = passages[pid]
            occ.append((Occurrence(c.id, pid, sources[p.source_id].path, p.heading, surface), tuple(mentions), c.text))
    return detect(occ, rules, aliases)


def rules_from_config(table: dict[str, Any] | None) -> tuple[list[ScopeRule], dict[str, str]]:
    """``[conflicts]`` in dakc.toml -> (scope rules, accepted conflict id -> reason)."""
    table = table or {}
    rules = [ScopeRule(r["path"], r.get("status"), r.get("label")) for r in table.get("scope", []) if r.get("path")]
    accepted = {a["id"]: a.get("reason", "") for a in table.get("accept", []) if a.get("id")}
    return rules, accepted


def fingerprint(rules: list[ScopeRule], aliases: dict[str, str] | None = None) -> str:
    """Changes whenever the detector, the scope rules or the aliases change: the index is then rebuilt."""
    from dakc.hashing import h_obj

    return h_obj([DETECTOR_VERSION, [(r.pattern, r.status, r.label) for r in rules], sorted((aliases or {}).items())])
