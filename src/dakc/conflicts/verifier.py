"""The optional conflict verifier (RFC 0002, phase 2).

The rules in :mod:`dakc.conflicts` only compare statements written in the same shape.
The verifier asks a model about pairs of claims that are likely to be about the same
thing, however they are worded:

1. **Candidates (no model).** For each claim that is new in this build, and for each
   entity it mentions, the few most similar claims about that entity in *other*
   documents. Pairs the rules already compare (they share a property key) are skipped.
2. **Verdicts.** The model judges each pair ``contradiction``, ``different_scope`` or
   ``compatible``, several pairs per call. A verdict is a fact about two claim texts, so
   it is cached by the pair of claim ids and the verifier's fingerprint, like extraction:
   an unchanged pair is never judged twice.
3. **Findings.** Every pair of current claims with a ``contradiction`` verdict becomes a
   ``possible`` finding, unless DAKC's own scope reading (heading, path, ``[[conflicts.scope]]``)
   puts the two statements in different scopes, in which case it is ``scoped``. Findings
   are a function of the current claims and the verdicts on record, so they equal a
   detection from scratch and close when either statement changes.

Advisory, like the rules: nothing here changes a claim or a page.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from dakc.conflicts import Finding, Occurrence, Parsed, ScopeRule, _judge, _values, index_rows, parse
from dakc.hashing import h

VERDICTS = ("contradiction", "different_scope", "compatible")
VERDICT_PREFIX = "verdict:"  # extraction_cache keys holding verdicts
PAIR_PREFIX = "pair|"  # conflicts.key of findings made from verdicts

SYSTEM = """You check statements taken from a team's documentation for contradictions.
You get numbered pairs of statements. The two statements of a pair come from different documents.
For each pair, decide:
- "contradiction": both cannot be true at the same time about the same system.
- "different_scope": they differ, but the statements themselves say they apply to different versions, dates, environments or plans (for example "in release 1" against "in production").
- "compatible": both can be true: they agree, are about different things, or one adds detail to the other.
Judge only what the statements say, not which one is right. Treat names that clearly refer to the same thing (Postgres and PostgreSQL) as the same.
For a pair that is not "compatible", give a one-sentence reason naming what disagrees; otherwise leave the reason empty.
Respond only with JSON of the form {"verdicts": [{"pair": 1, "verdict": "contradiction", "reason": "..."}]}."""

SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "pair": {"type": "integer"},
                    "verdict": {"type": "string", "enum": list(VERDICTS)},
                    "reason": {"type": "string"},
                },
                "required": ["pair", "verdict", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["verdicts"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class Settings:
    """``[conflicts]`` verifier settings in dakc.toml."""

    max_checks: int = 500  # pairs judged per build; more are left for the next build
    per_entity: int = 5  # for each new claim and entity it mentions: the most similar claims compared
    pairs_per_call: int = 10

    @classmethod
    def from_config(cls, table: dict[str, Any] | None) -> Settings | None:
        """None when the verifier is off (the default)."""
        table = table or {}
        mode = str(table.get("verifier", "none")).lower()
        if mode in ("none", "off", "false", ""):
            return None
        if mode != "llm":
            raise ValueError(f'[conflicts] verifier must be "llm" or "none", not {mode!r}')
        return cls(max_checks=max(0, int(table.get("max_checks", cls.max_checks))),
                   per_entity=max(1, int(table.get("candidates_per_entity", cls.per_entity))),
                   pairs_per_call=max(1, int(table.get("pairs_per_call", cls.pairs_per_call))))


@dataclass
class Judged:
    verdicts: list[tuple[str, str]]  # (verdict, reason) per pair, in order; ("", "") when the model gave none
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 1


def user_prompt(pairs: list[tuple[str, str]]) -> str:
    return "Pairs:\n\n" + "\n\n".join(f"[{i + 1}]\nA: {a}\nB: {b}" for i, (a, b) in enumerate(pairs))


def parse_verdicts(text: str, n: int) -> list[tuple[str, str]]:
    from dakc.providers.llm import ProviderError, _parse_json_object

    try:
        data = _parse_json_object(text)
    except ProviderError:
        return [("", "")] * n
    out = [("", "")] * n
    for item in data.get("verdicts", []) if isinstance(data, dict) else []:
        try:
            i = int(item.get("pair")) - 1
        except (TypeError, ValueError):
            continue
        verdict = str(item.get("verdict", "")).strip().lower().replace(" ", "_")
        if 0 <= i < n and verdict in VERDICTS:
            out[i] = (verdict, re.sub(r"\s+", " ", str(item.get("reason", ""))).strip()[:300])
    return out


class LLMVerifier:
    is_llm = True
    free = False

    def __init__(self, client):
        self.client = client
        self.fingerprint = f"llm-verifier/1:{client.fingerprint}:{h(SYSTEM + json.dumps(SCHEMA, sort_keys=True))[:10]}"

    def judge(self, pairs: list[tuple[str, str]]) -> Judged:
        c = self.client.complete(SYSTEM, user_prompt(pairs), json_schema=SCHEMA)
        return Judged(parse_verdicts(c.text, len(pairs)), c.input_tokens, c.output_tokens)


class HeuristicVerifier:
    """Free and deterministic: a pair is a contradiction only when the rules say so, which
    for candidates (pairs the rules do not compare) is never. It exists so that the mock
    provider can account for verifier calls and tokens; it finds nothing new."""

    fingerprint = "heuristic-verifier/1"
    free = True

    def __init__(self, as_llm: bool = False):
        self.is_llm = as_llm

    def judge(self, pairs: list[tuple[str, str]]) -> Judged:
        from dakc.conflicts import detect
        from dakc.providers.base import estimate_tokens

        verdicts = []
        for a, b in pairs:
            occ = [(Occurrence("a", "a", "a.md", "", a), _mentions(a), a), (Occurrence("b", "b", "b.md", "", b), _mentions(b), b)]
            hit = any(f.kind == "conflict" for f in detect(occ).values())
            verdicts.append(("contradiction", "the rules compare these statements") if hit else ("compatible", ""))
        out = json.dumps({"verdicts": [{"pair": i + 1, "verdict": v, "reason": r} for i, (v, r) in enumerate(verdicts)]})
        return Judged(verdicts, estimate_tokens(SYSTEM + user_prompt(pairs)), estimate_tokens(out))


def _mentions(text: str) -> tuple[str, ...]:
    from dakc.claims import extract_mentions

    return extract_mentions(text)


def cache_key(fingerprint: str, a: str, b: str) -> str:
    a, b = sorted((a, b))
    return VERDICT_PREFIX + h(f"{fingerprint}|{a}|{b}")


# --------------------------------------------------------------------------- candidates

_STOP = set("""a an the this that these those it its is are was were be been being has have had do does did not no
and or but of in on at by to from with for as can will may should would could if then than so you your we our
they their there here when which who what how also only all any each more most other such into over under""".split())
_WORD = re.compile(r"[a-z][a-z0-9+#.\-]*")


def words(text: str) -> frozenset[str]:
    return frozenset(w for w in _WORD.findall(text.lower()) if w not in _STOP and len(w) > 2)


@dataclass
class ClaimInfo:
    """What candidate selection needs to know about one current claim."""

    text: str
    paths: frozenset[str]
    entities: tuple[str, ...]
    words: frozenset[str] = field(default_factory=frozenset)
    keys: frozenset[str] = field(default_factory=frozenset)  # rule keys (conflict_index) of its occurrences


_POSSESSIVE = re.compile(r"['\u2019]s\b")


def subject_key(name: str, aliases: dict[str, str] | None = None) -> str:
    """What the verifier compares entities by: the name without a possessive, aliases applied."""
    from dakc.hashing import entity_key

    bare = _POSSESSIVE.sub("", name).strip() or name
    return entity_key((aliases or {}).get(entity_key(bare), bare))


@dataclass
class Subjects:
    """Entities grouped the way the verifier compares claims: by name without a possessive
    ("Ledger's" and "Ledger" are one subject), with the workspace's aliases applied."""

    of_claim: dict[str, set[str]]  # claim id -> its subjects
    members: dict[str, list[str]]  # subject -> claim ids
    name: dict[str, str]  # subject -> a display name

    @classmethod
    def of(cls, entities: dict[str, Any], aliases: dict[str, str] | None = None) -> Subjects:
        of_claim: dict[str, set[str]] = {}
        members: dict[str, list[str]] = {}
        names: dict[str, list[str]] = {}
        for eid in sorted(entities):
            e = entities[eid]
            g = subject_key(e.name, aliases)
            names.setdefault(g, []).append(_POSSESSIVE.sub("", e.name).strip() or e.name)
            for c in e.claim_ids:
                subjects = of_claim.setdefault(c, set())
                if g not in subjects:
                    subjects.add(g)
                    members.setdefault(g, []).append(c)
        return cls(of_claim, members, {g: min(v, key=lambda n: (len(n), n.casefold(), n)) for g, v in names.items()})

    def shared(self, a: str, b: str) -> list[str]:
        return sorted({self.name[g] for g in self.of_claim.get(a, set()) & self.of_claim.get(b, set())}, key=str.casefold)


def candidates(new: Iterable[str], info: dict[str, ClaimInfo], members: dict[str, list[str]],
               per_entity: int) -> list[tuple[int, str, str]]:
    """(similarity, a, b) for every candidate pair involving a new claim, most similar first.

    For each new claim and each entity it mentions, the ``per_entity`` claims about that
    entity from other documents that share the most words with it. Pairs the rules already
    compare (a shared property key) are left to the rules."""
    best: dict[tuple[str, str], int] = {}
    for c in sorted(new):
        ci = info.get(c)
        if ci is None:
            continue
        for e in ci.entities:
            scored = []
            for d in members.get(e, ()):
                di = info.get(d)
                if d == c or di is None or di.paths & ci.paths or di.keys & ci.keys:
                    continue
                scored.append((-len(ci.words & di.words), d))
            scored.sort()
            for neg, d in scored[:per_entity]:
                pair = (c, d) if c < d else (d, c)
                best[pair] = max(best.get(pair, 0), -neg)
    return sorted(((s, a, b) for (a, b), s in best.items()), key=lambda t: (-t[0], t[1], t[2]))


# --------------------------------------------------------------------------- findings

def finding_id(a: str, b: str) -> str:
    a, b = sorted((a, b))
    return "cfl:" + h(PAIR_PREFIX + a + "|" + b)[:10]


def findings(verdicts: Iterable[dict[str, Any]], occurrences: Callable[[str], list[tuple[Occurrence, tuple[str, ...], str]]],
             names: Callable[[str, str], list[str]], rules: list[ScopeRule] | None = None,
             aliases: dict[str, str] | None = None) -> dict[str, tuple[str, Finding]]:
    """{finding id: (key, finding)} for verdicts on pairs of current claims.

    ``occurrences(claim_id)`` returns the claim's occurrences ([] when it is no longer
    current); ``names(a, b)`` the entities the two claims share, for the label."""
    out: dict[str, tuple[str, Finding]] = {}
    for v in verdicts:
        if v.get("verdict") not in ("contradiction", "different_scope"):
            continue
        a, b = sorted((v["a"], v["b"]))
        occ_a, occ_b = occurrences(a), occurrences(b)
        if not occ_a or not occ_b:
            continue
        keys_a = {r[0] for r in index_rows(occ_a, rules, aliases)}
        keys_b = {r[0] for r in index_rows(occ_b, rules, aliases)}
        if keys_a & keys_b:
            continue  # the rules compare these two: their finding (if any) is the rules'
        members = [(Parsed(p.occ, p.scope, p.polarity, []), text)
                   for side in (occ_a, occ_b) for p, text in ((parse(o, m, rules, aliases), t) for o, m, t in side)]
        reason = v.get("reason") or "the verifier judged that these statements cannot both be true"
        key = PAIR_PREFIX + a + "|" + b
        label = ", ".join(names(a, b)) or "two statements"
        if v["verdict"] == "different_scope":
            f = Finding(finding_id(a, b), "scoped", label, f"{reason} (the verifier read a difference of scope)",
                        _values(members))
        else:
            f = _judge(key, members, label, reason)
            if f is None:
                continue
            f.id = finding_id(a, b)
            if f.kind == "conflict":
                f.kind = "possible"
                f.reason = reason
        out[f.id] = (key, f)
    return out


def load_verdicts(conn, fingerprint: str) -> list[dict[str, Any]]:
    """Every contradiction or difference of scope on record for this verifier."""
    rows = conn.execute(
        "SELECT result FROM extraction_cache WHERE fingerprint=? AND key LIKE ? AND result NOT LIKE ?",
        (fingerprint, VERDICT_PREFIX + "%", '%"verdict": "compatible"%'))
    return [json.loads(r) for (r,) in rows]


def verified_in_store(store, rules: list[ScopeRule] | None = None, aliases: dict[str, str] | None = None
                      ) -> dict[str, tuple[str, dict[str, Any]]]:
    """{id: (kind, payload)} of the verifier findings the claims at HEAD and the verdicts on
    record imply: what the stored ones must equal (``dakc verify`` checks it)."""
    fp = store.get_meta("verifier_fp") or ""
    if not fp:
        return {}
    st = store.load_state()
    claims, passages, sources = st.of("claim"), st.of("passage"), st.sources
    subjects = Subjects.of(st.of("entity"), aliases)

    def occurrences(cid: str):
        c = claims.get(cid)
        if c is None:
            return []
        return [(Occurrence(cid, pid, sources[passages[pid].source_id].path, passages[pid].heading, surface),  # type: ignore[attr-defined]
                 tuple(m), c.text) for pid, surface, m, _ in c.supports]  # type: ignore[attr-defined]

    return {fid: (f.kind, f.payload()) for fid, (_k, f) in findings(load_verdicts(store.conn, fp), occurrences,
                                                                      subjects.shared, rules, aliases).items()}


__all__ = ["Settings", "LLMVerifier", "HeuristicVerifier", "Judged", "ClaimInfo", "Subjects", "subject_key", "candidates", "findings",
           "load_verdicts", "verified_in_store", "cache_key", "finding_id", "words", "VERDICTS", "PAIR_PREFIX"]
