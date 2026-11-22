"""Deterministic synthetic corpus generator and change scenarios.

Every output is a pure function of (n_docs, seed): running the generator twice
produces byte-identical files. Scenario mutations are likewise deterministic.

Documents are Markdown files grouped into topic folders. Each paragraph holds
factual sentences of the form ``<Entity> <relation> <Entity>.`` or
``<Entity> <attribute> <value>.`` drawn from a Zipf-weighted entity vocabulary,
so a few entities are shared by many documents (hub pages). About 5 % of
paragraphs are copied verbatim into a second document so that some claims have
several independent sources.
"""

from __future__ import annotations

import json
import random
import shutil
from dataclasses import dataclass, field
from itertools import accumulate
from pathlib import Path

PREFIXES = [
    "Ar", "Bel", "Cor", "Dra", "El", "Fen", "Gal", "Hal", "Ix", "Jor", "Kel", "Lum",
    "Mor", "Nex", "Or", "Pyr", "Quor", "Ros", "Syl", "Tor", "Ul", "Vor", "Wex", "Zan",
]
MIDDLES = ["a", "e", "i", "o", "u", "ae", "io", "ar", "en", "il"]
SUFFIXES = [
    "bex", "con", "dyn", "flux", "grid", "hive", "lite", "mesh", "nova", "port",
    "rix", "sql", "tron", "vault", "wire", "xis", "base", "cast", "dock", "forge",
]
RELATIONS = [
    "integrates with", "depends on", "replaces", "is compatible with",
    "is maintained alongside", "competes with", "extends", "is deployed next to",
    "streams data to", "authenticates against",
]
ATTRIBUTES = [
    ("has a default port of", lambda r: str(r.randint(1024, 65000))),
    ("was first released in", lambda r: str(r.randint(1995, 2025))),
    ("is licensed under", lambda r: r.choice(["the MIT license", "the Apache license", "a proprietary license", "the BSD license"])),
    ("stores data in", lambda r: r.choice(["columnar files", "a write-ahead log", "memory-mapped pages", "an append-only journal"])),
    ("supports up to", lambda r: f"{r.choice([8, 16, 32, 64, 128, 256])} concurrent writers"),
]
SECTION_TITLES = ["Overview", "Integration", "Operations", "History", "Deployment", "Notes"]
# Values outside the ranges above, used by the contradiction scenario.
CONTRADICTION_VALUES = {
    "has a default port of": "65101",
    "was first released in": "2026",
    "is licensed under": "the GPL license",
    "stores data in": "a log-structured merge tree",
    "supports up to": "1024 concurrent writers",
}


@dataclass
class Corpus:
    root: Path
    n_docs: int
    seed: int
    entities: list[str]
    docs: list[str] = field(default_factory=list)  # relative paths
    shared_paragraph_docs: list[list[str]] = field(default_factory=list)  # [orig, copy]

    def manifest(self) -> dict:
        return {
            "n_docs": self.n_docs,
            "seed": self.seed,
            "entities": self.entities,
            "docs": self.docs,
            "shared_paragraph_docs": self.shared_paragraph_docs,
        }


def _entity_names(n: int, rng: random.Random) -> list[str]:
    names: set[str] = set()
    out: list[str] = []

    def add(name: str) -> None:
        name = name[0].upper() + name[1:]
        if name not in names:
            names.add(name)
            out.append(name)

    # Three-part names: 4,800 combinations. Rejection sampling stalls as the space
    # fills (and never ends past it), so stop at three quarters of the space and
    # continue with five-part names. Corpora needing <= 3,600 names (up to 7,200
    # documents) are byte-identical to those generated before this limit existed.
    simple_limit = min(n, len(PREFIXES) * len(MIDDLES) * len(SUFFIXES) * 3 // 4)
    while len(out) < simple_limit:
        add(rng.choice(PREFIXES) + rng.choice(MIDDLES) + rng.choice(SUFFIXES))
    while len(out) < n:
        add(rng.choice(PREFIXES) + rng.choice(MIDDLES) + rng.choice(SUFFIXES) + rng.choice(MIDDLES) + rng.choice(SUFFIXES))
    return out


class _Sampler:
    def __init__(self, entities: list[str], rng: random.Random, exponent: float = 1.05):
        self.entities = entities
        self.rng = rng
        # Cumulative weights computed once: passing ``weights=`` would rebuild them on
        # every draw (O(entities) per call). Same random stream, same choices.
        self.cum_weights = list(accumulate(1.0 / (i + 1) ** exponent for i in range(len(entities))))

    def entity(self) -> str:
        return self.rng.choices(self.entities, cum_weights=self.cum_weights, k=1)[0]

    def other(self, a: str) -> str:
        while True:
            b = self.entity()
            if b != a:
                return b

    def sentence(self, subject: str | None = None) -> str:
        a = subject or self.entity()
        if self.rng.random() < 0.6:
            return f"{a} {self.rng.choice(RELATIONS)} {self.other(a)}."
        attr, value = self.rng.choice(ATTRIBUTES)
        return f"{a} {attr} {value(self.rng)}."

    def paragraph(self, k: int | None = None) -> str:
        k = k or self.rng.randint(1, 4)
        return " ".join(self.sentence() for _ in range(k))


def generate(root: Path, n_docs: int, seed: int = 7, profile: str = "uniform") -> Corpus:
    """``profile="uniform"``: every document has 2-4 sections of 1-3 paragraphs.
    ``profile="realistic"``: heavy-tailed document lengths (most short, a few very
    long), uneven topic sizes, and 1-6 sentences per paragraph."""
    """Write a corpus of ``n_docs`` Markdown files under ``root`` (recreated)."""
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    rng = random.Random(seed)
    entities = _entity_names(max(40, n_docs // 2), rng)
    sampler = _Sampler(entities, rng)
    n_topics = max(2, n_docs // 50)
    corpus = Corpus(root=root, n_docs=n_docs, seed=seed, entities=entities)
    paragraphs_so_far: list[tuple[str, str]] = []  # (doc, paragraph)

    for i in range(n_docs):
        if profile == "realistic":
            # Zipf-like topic sizes: a few big folders, many small ones
            topic = f"topic-{min(n_topics - 1, int(n_topics * rng.random() ** 2.5)):03d}"
        else:
            topic = f"topic-{i % n_topics:03d}"
        rel = f"{topic}/doc-{i:05d}.md"
        a, b = sampler.entity(), sampler.entity()
        lines = [f"# Notes on {a} and {b}", ""]
        n_sections = (min(30, max(1, int(rng.lognormvariate(1.0, 0.8)))) if profile == "realistic" else rng.randint(2, 4))
        for s in range(n_sections):
            lines += [f"## {SECTION_TITLES[(i + s) % len(SECTION_TITLES)]}", ""]
            for _ in range(rng.randint(1, 4) if profile == "realistic" else rng.randint(1, 3)):
                if paragraphs_so_far and rng.random() < 0.05:
                    src_doc, para = paragraphs_so_far[rng.randrange(len(paragraphs_so_far))]
                    if src_doc != rel:
                        corpus.shared_paragraph_docs.append([src_doc, rel])
                else:
                    para = sampler.paragraph(rng.randint(1, 6) if profile == "realistic" else None)
                paragraphs_so_far.append((rel, para))
                lines += [para, ""]
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines), encoding="utf-8")
        corpus.docs.append(rel)

    (root.parent / f"{root.name}.manifest.json").write_text(
        json.dumps(corpus.manifest(), indent=1), encoding="utf-8"
    )
    return corpus


# --------------------------------------------------------------------------- #
# Scenarios
# --------------------------------------------------------------------------- #


def _paragraph_spans(text: str) -> list[tuple[int, str]]:
    """Return (line index, line) for every body paragraph line (not headings)."""
    return [
        (i, line)
        for i, line in enumerate(text.split("\n"))
        if line.strip() and not line.startswith("#")
    ]


def _rewrite_paragraph(path: Path, rng: random.Random, sampler: _Sampler, *, many_entities: bool = False) -> None:
    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")
    spans = _paragraph_spans(text)
    idx, _ = spans[rng.randrange(len(spans))]
    if many_entities:
        subjects: list[str] = []
        while len(subjects) < 7:
            e = sampler.entity()
            if e not in subjects:
                subjects.append(e)
        lines[idx] = " ".join(sampler.sentence(subject=s) for s in subjects)
    else:
        lines[idx] = sampler.paragraph(k=3)
    path.write_text("\n".join(lines), encoding="utf-8")


SCENARIOS = {
    "S0_no_change": "No change (idempotence)",
    "S1_one_modified": "One source modified (one paragraph rewritten)",
    "S2_five_modified": "Five unrelated sources modified",
    "S3_one_deleted": "One source deleted (shares a paragraph with another source)",
    "S4_one_renamed": "One source renamed",
    "S5_one_claim_modified": "One claim modified (one value changed)",
    "S6_new_source_popular_entity": "New source contributing to the most popular entity",
    "S7_contradiction": "New source contradicting an existing claim",
    "S8_many_pages": "One paragraph rewritten to mention 7 entities",
    "S9_contradiction_then_delete": "A source contradicts a claim, then the original source is deleted",
}


def apply_scenario(root: Path, corpus_manifest: dict, scenario: str, seed: int = 11) -> dict:
    """Mutate the corpus at ``root`` in place. Returns a description of the change."""
    rng = random.Random(f"{seed}:{scenario}")
    entities: list[str] = corpus_manifest["entities"]
    docs: list[str] = corpus_manifest["docs"]
    sampler = _Sampler(entities, rng)
    n = len(docs)
    pick = docs[n // 3]

    if scenario == "S0_no_change":
        return {"changed": []}
    if scenario == "S1_one_modified":
        _rewrite_paragraph(root / pick, rng, sampler)
        return {"modified": [pick]}
    if scenario == "S2_five_modified":
        # Five documents spread evenly across the corpus (different topics
        # whenever the corpus has at least five).
        chosen = [docs[(n // 5) * k + (n // 10)] for k in range(5)]
        for d in chosen:
            _rewrite_paragraph(root / d, rng, sampler)
        return {"modified": chosen}
    if scenario == "S3_one_deleted":
        shared = corpus_manifest["shared_paragraph_docs"]
        victim = shared[len(shared) // 2][0] if shared else pick
        (root / victim).unlink()
        return {"deleted": [victim]}
    if scenario == "S4_one_renamed":
        dst = pick.replace(".md", "-renamed.md")
        (root / pick).rename(root / dst)
        return {"renamed": [[pick, dst]]}
    if scenario == "S5_one_claim_modified":
        path = root / pick
        text = path.read_text(encoding="utf-8")
        for attr, _ in ATTRIBUTES:
            if f" {attr} " in text:
                head, tail = text.split(f" {attr} ", 1)
                value_end = tail.index(".")
                old = tail[:value_end]
                if old.isdigit():
                    new = str(int(old) + 1)
                elif "concurrent writers" in old:
                    new = old.replace(old.split()[0], str(int(old.split()[0]) * 2), 1)
                else:
                    new = old + " (revised)"
                path.write_text(head + f" {attr} " + new + tail[value_end:], encoding="utf-8")
                return {"modified": [pick], "claim": f"{attr}: {old} -> {new}"}
        # fall back: change the last word of the first sentence
        _rewrite_paragraph(path, rng, sampler)
        return {"modified": [pick]}
    if scenario == "S6_new_source_popular_entity":
        top = entities[0]
        rel = f"{docs[0].split('/')[0]}/doc-new-popular.md"
        body = [f"# More about {top}", "", "## Overview", ""]
        body += [" ".join(sampler.sentence(subject=top) for _ in range(3)) for _ in range(3)]
        (root / rel).write_text("\n\n".join(body) + "\n", encoding="utf-8")
        return {"added": [rel]}
    if scenario == "S7_contradiction":
        # Find an attribute claim in an existing doc and assert a different value.
        # The new value lies outside the generator's value space, so the
        # contradicting claim is guaranteed not to exist anywhere in the corpus
        # already (a small value space would otherwise make it a mere restatement).
        for d in docs:
            text = (root / d).read_text(encoding="utf-8")
            for line in text.split("\n"):
                if line.startswith("#"):
                    continue
                for sent in line.split(". "):
                    for attr, _value_fn in ATTRIBUTES:
                        if f" {attr} " in sent:
                            subject = sent.split(f" {attr} ")[0].strip()
                            old = sent.split(f" {attr} ")[1].rstrip(".")
                            new = CONTRADICTION_VALUES[attr]
                            rel = f"{d.split('/')[0]}/doc-contradiction.md"
                            (root / rel).write_text(
                                f"# Correction\n\n{subject} {attr} {new}.\n", encoding="utf-8"
                            )
                            return {"added": [rel], "contradicts": d, "claim": f"{subject} {attr}: {old} vs {new}"}
        raise RuntimeError("no attribute claim found")
    if scenario == "S9_contradiction_then_delete":
        # Pick an attribute claim stated by exactly one document, contradict it in
        # a new document, then delete the original: only the new value may remain.
        texts = {d: (root / d).read_text(encoding="utf-8") for d in docs}
        corpus_text = "\n".join(texts.values())
        for d in docs:
            for line in texts[d].split("\n"):
                if line.startswith("#"):
                    continue
                for sent in line.split(". "):
                    sent = sent.rstrip(".")
                    for attr in CONTRADICTION_VALUES:
                        if f" {attr} " in sent and corpus_text.count(sent) == 1 and d not in {x for pair in corpus_manifest["shared_paragraph_docs"] for x in pair}:
                            subject = sent.split(f" {attr} ")[0].strip()
                            old = sent.split(f" {attr} ")[1]
                            new = CONTRADICTION_VALUES[attr]
                            rel = f"{d.split('/')[0]}/doc-correction.md"
                            (root / rel).write_text(f"# Correction\n\n{subject} {attr} {new}.\n", encoding="utf-8")
                            (root / d).unlink()
                            return {"added": [rel], "deleted": [d], "claim": f"{subject} {attr}: {old} -> {new}"}
        raise RuntimeError("no single-source attribute claim found")
    if scenario == "S8_many_pages":
        _rewrite_paragraph(root / pick, rng, sampler, many_entities=True)
        return {"modified": [pick]}
    raise KeyError(scenario)


if __name__ == "__main__":  # pragma: no cover - manual use
    import argparse

    ap = argparse.ArgumentParser(description="Generate a synthetic DAKC corpus")
    ap.add_argument("root", type=Path)
    ap.add_argument("--docs", type=int, default=100)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    c = generate(args.root, args.docs, args.seed)
    print(f"wrote {len(c.docs)} docs, {len(c.entities)} entities to {args.root}")
