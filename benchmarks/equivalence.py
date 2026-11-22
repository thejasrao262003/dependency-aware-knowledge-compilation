"""Can differently worded claims be matched as the same fact without false merges?

A labelled set is generated: for each fact, paraphrase pairs (same fact) and
hard negatives (value changed, object swapped, direction reversed, relation
changed, negated). Each matcher decides "same fact?" for every pair:

* exact      DAKC's claim identity (normalized text hash)
* fuzzy      RapidFuzz token_sort_ratio >= 85 (a typical naive choice)
* guarded    deterministic: same entities in the same order, same numbers, same
             negation polarity, and similar remaining words
* llm        an LLM judge (optional; needs --provider-config)

A false merge would silently corrupt knowledge (two different facts become
one claim), so precision is the primary metric.

Usage: python -m benchmarks.equivalence [--provider-config FILE] [--facts 60]
"""

from __future__ import annotations

import argparse
import json
import random
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from rapidfuzz import fuzz

from dakc.claims import likely_same_fact
from dakc.hashing import claim_id, normalize_claim

ROOT = Path(__file__).resolve().parents[1]
ENTITIES = ["Nexiodyn", "Kelitron", "Draildock", "Sylabase", "Halecast", "Morogrid", "Quorlite", "Ulosql", "Feniovault", "Jorohive", "Pyrabex", "Wexoport"]

# (relation key, asymmetric, paraphrase templates)
RELATIONS = [
    ("integrates", False, ["{a} integrates with {b}.", "{a} is integrated with {b}.", "{a} has an integration with {b}."]),
    ("depends", True, ["{a} depends on {b}.", "{a} relies on {b}.", "{a} is dependent on {b}."]),
    ("replaces", True, ["{a} replaces {b}.", "{a} is a replacement for {b}.", "{b} is replaced by {a}."]),
    ("streams", True, ["{a} streams data to {b}.", "{a} sends a data stream to {b}.", "Data is streamed from {a} to {b}."]),
]
ATTRIBUTES = [
    ("released", lambda r: r.randint(1995, 2024), lambda v: v + 1,
     ["{a} was first released in {v}.", "{a} was initially released in {v}.", "The first release of {a} was in {v}."]),
    ("port", lambda r: r.randint(1024, 60000), lambda v: v + 1,
     ["{a} has a default port of {v}.", "The default port of {a} is {v}.", "{a} listens on port {v} by default."]),
    ("writers", lambda r: r.choice([8, 16, 32, 64]), lambda v: v * 2,
     ["{a} supports up to {v} concurrent writers.", "{a} allows at most {v} concurrent writers.", "Up to {v} writers can write to {a} concurrently."]),
]
NEGATIONS = {
    "integrates": "{a} does not integrate with {b}.", "depends": "{a} does not depend on {b}.",
    "replaces": "{a} does not replace {b}.", "streams": "{a} does not stream data to {b}.",
    "released": "{a} was not released in {v}.", "port": "{a} does not use port {v} by default.",
    "writers": "{a} does not support {v} concurrent writers.",
}


def labelled_pairs(n_facts: int, seed: int = 4) -> list[dict]:
    rng = random.Random(seed)
    pairs: list[dict] = []
    for i in range(n_facts):
        a, b, c = rng.sample(ENTITIES, 3)
        if i % 2 == 0:
            key, asym, tpls = RELATIONS[(i // 2) % len(RELATIONS)]
            t1, t2 = rng.sample(tpls, 2)
            base = tpls[0].format(a=a, b=b)
            pairs.append({"a": t1.format(a=a, b=b), "b": t2.format(a=a, b=b), "same": True, "type": "paraphrase"})
            pairs.append({"a": base, "b": tpls[0].format(a=a, b=c), "same": False, "type": "object swapped"})
            other = RELATIONS[((i // 2) + 1) % len(RELATIONS)][2][0]
            pairs.append({"a": base, "b": other.format(a=a, b=b), "same": False, "type": "relation changed"})
            pairs.append({"a": base, "b": NEGATIONS[key].format(a=a, b=b), "same": False, "type": "negated"})
            if asym:
                pairs.append({"a": base, "b": rng.choice(tpls).format(a=b, b=a), "same": False, "type": "direction reversed"})
        else:
            key, gen, change, tpls = ATTRIBUTES[(i // 2) % len(ATTRIBUTES)]
            v = gen(rng)
            t1, t2 = rng.sample(tpls, 2)
            base = tpls[0].format(a=a, v=v)
            pairs.append({"a": t1.format(a=a, v=v), "b": t2.format(a=a, v=v), "same": True, "type": "paraphrase"})
            pairs.append({"a": base, "b": rng.choice(tpls).format(a=a, v=change(v)), "same": False, "type": "value changed"})
            pairs.append({"a": base, "b": tpls[0].format(a=b, v=v), "same": False, "type": "subject swapped"})
            pairs.append({"a": base, "b": NEGATIONS[key].format(a=a, v=v), "same": False, "type": "negated"})
    return pairs


# ------------------------------------------------------------------ matchers
def exact(a: str, b: str) -> bool:
    return claim_id(a) == claim_id(b)


def naive_fuzzy(a: str, b: str) -> bool:
    return fuzz.token_sort_ratio(normalize_claim(a), normalize_claim(b)) >= 85


def guarded(a: str, b: str) -> bool:
    """The matcher DAKC ships for advisory duplicate reports (dakc.claims.likely_same_fact)."""
    return likely_same_fact(a, b)


JUDGE_SYSTEM = """You decide whether two statements assert exactly the same fact.
They are the same only if they name the same entities in the same roles, the same relation and direction, the same values and the same polarity. Wording may differ.
Answer with exactly one word: yes or no."""


def llm_judge(config: Path, pairs: list[dict], jobs: int) -> list[bool]:
    from benchmarks.llm_experiment import load_bundle

    client = load_bundle(config).extractor.client

    def ask(p: dict) -> bool:
        c = client.complete(JUDGE_SYSTEM, f"Statement A: {p['a']}\nStatement B: {p['b']}\nSame fact?")
        return c.text.strip().lower().startswith("yes")

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        return list(pool.map(ask, pairs))


def score(pairs: list[dict], decisions: list[bool]) -> dict:
    tp = sum(1 for p, d in zip(pairs, decisions) if d and p["same"])
    fp = sum(1 for p, d in zip(pairs, decisions) if d and not p["same"])
    fn = sum(1 for p, d in zip(pairs, decisions) if not d and p["same"])
    by_type: dict[str, dict[str, int]] = {}
    for p, d in zip(pairs, decisions):
        t = by_type.setdefault(p["type"], {"pairs": 0, "matched": 0})
        t["pairs"] += 1
        t["matched"] += int(d)
    return {
        "precision": round(tp / (tp + fp), 4) if tp + fp else 1.0,
        "recall": round(tp / (tp + fn), 4) if tp + fn else 0.0,
        "false_merges": fp,
        "by_type": by_type,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--facts", type=int, default=60)
    ap.add_argument("--provider-config", type=Path)
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--out", type=Path, default=ROOT / "benchmarks" / "results" / "equivalence.json")
    args = ap.parse_args()
    pairs = labelled_pairs(args.facts)
    result = {"pairs": len(pairs), "positives": sum(p["same"] for p in pairs), "matchers": {}}
    for name, fn in (("exact", exact), ("fuzzy", naive_fuzzy), ("guarded", guarded)):
        result["matchers"][name] = score(pairs, [fn(p["a"], p["b"]) for p in pairs])
    if args.provider_config:
        from benchmarks.llm_experiment import load_bundle

        result["matchers"]["llm"] = score(pairs, llm_judge(args.provider_config, pairs, args.jobs))
        result["llm_model"] = load_bundle(args.provider_config).extractor.client.fingerprint
    result["false_merge_examples"] = {
        name: [p for p, d in zip(pairs, [fn(p["a"], p["b"]) for p in pairs]) if d and not p["same"]][:3]
        for name, fn in (("fuzzy", naive_fuzzy), ("guarded", guarded))
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=1, sort_keys=True), encoding="utf-8")
    for name, m in result["matchers"].items():
        print(f"{name:8s} precision {m['precision']:.3f}  recall {m['recall']:.3f}  false merges {m['false_merges']}")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
