"""Differential testing: incremental compilation must equal a full rebuild.

Each benchmark scenario, and a seeded sequence of random edits, is applied to a
synthetic corpus. After every step the incremental store (both the ``dakc``
and ``file`` strategies) must be identical (canonical state, including content
hashes and dependency edges) to a from-scratch full build of the same corpus.
"""

import json
import random
import shutil

import pytest

from benchmarks.corpus import SCENARIOS, apply_scenario, generate
from dakc.provenance.compare import canonical_state, diff_states
from dakc.storage import Store

from tests.conftest import compile_once


@pytest.fixture(scope="module")
def base(tmp_path_factory):
    root = tmp_path_factory.mktemp("eq")
    generate(root / "corpus", 120)
    manifest = json.loads((root / "corpus.manifest.json").read_text())
    store = Store(root / "A.db")
    compile_once(store, root / "corpus", strategy="full")
    store.close()
    return root, manifest


@pytest.mark.parametrize("scenario", list(SCENARIOS))
@pytest.mark.parametrize("strategy", ["dakc", "file"])
def test_scenario_equals_full_rebuild(base, tmp_path, scenario, strategy):
    root, manifest = base
    corpus = tmp_path / "corpus"
    shutil.copytree(root / "corpus", corpus)
    apply_scenario(corpus, manifest, scenario)
    shutil.copy(root / "A.db", tmp_path / "I.db")
    inc = Store(tmp_path / "I.db")
    rep = compile_once(inc, corpus, strategy=strategy)
    ref = Store(tmp_path / "B.db")
    compile_once(ref, corpus, strategy="full")
    d = diff_states(canonical_state(ref), canonical_state(inc))
    assert d.equal, (scenario, strategy, d.by_kind(), d.different[:5], d.missing[:5], d.extra[:5])
    if scenario == "S0_no_change":
        assert rep.status == "noop"


def _random_edit(corpus, rng: random.Random, step: int) -> str:
    files = sorted(p for p in corpus.rglob("*.md"))
    op = rng.choice(["edit", "edit", "delete", "add", "rename", "duplicate", "swap"])
    target = rng.choice(files)
    if op == "edit":
        lines = target.read_text().split("\n")
        body = [i for i, l in enumerate(lines) if l and not l.startswith("#")]
        if body:
            i = rng.choice(body)
            words = lines[i].split(" ")
            words[rng.randrange(len(words))] = rng.choice(["Kelitron", "Zanbex", "2042", "Vortex", "the"])
            lines[i] = " ".join(words)
            target.write_text("\n".join(lines))
    elif op == "delete" and len(files) > 5:
        target.unlink()
    elif op == "add":
        (corpus / "topic-000" / f"added-{step}.md").write_text(f"# Added\n\nZanbex integrates with Kelitron. Step{step} extends Zanbex.\n")
    elif op == "rename":
        target.rename(target.with_name(target.stem + f"-r{step}.md"))
    elif op == "duplicate":
        shutil.copy(target, target.with_name(target.stem + f"-dup{step}.md"))
    elif op == "swap":
        text = target.read_text().split("\n\n")
        if len(text) > 3:
            text[1], text[-1] = text[-1], text[1]
            target.write_text("\n\n".join(text))
    return op


def test_random_edit_sequence_stays_equivalent(base, tmp_path):
    root, _ = base
    corpus = tmp_path / "corpus"
    shutil.copytree(root / "corpus", corpus)
    shutil.copy(root / "A.db", tmp_path / "I.db")
    inc = Store(tmp_path / "I.db")
    rng = random.Random(1234)
    for step in range(12):
        ops = [_random_edit(corpus, rng, step) for _ in range(rng.randint(1, 3))]
        compile_once(inc, corpus)
        ref = Store(tmp_path / f"B{step}.db")
        compile_once(ref, corpus, strategy="full")
        d = diff_states(canonical_state(ref), canonical_state(inc))
        ref.close()
        assert d.equal, (step, ops, d.by_kind(), d.different[:3], d.missing[:3], d.extra[:3])


def test_past_scope_rules_stay_equivalent(base, tmp_path):
    """Pages list claims that only past-scoped documents support under History (versions.RENDER
    = render/2). Which claims those are depends on every supporting document's path and on the
    rules, so documents are also moved between a past and a current folder, and the rules
    change between steps. After each step, incremental must equal full with the same rules."""
    from dakc.conflicts import ScopeRule

    root, _ = base
    corpus = tmp_path / "corpus"
    shutil.copytree(root / "corpus", corpus)
    rules_by_step = {0: [ScopeRule("topic-001/**", status="past", label="{path}")],
                     5: [ScopeRule("topic-000/**", status="past")],
                     8: [ScopeRule("topic-*/**", status="past"), ScopeRule("topic-000/doc-000[0-4]*", status="current")],
                     10: []}
    inc = Store(tmp_path / "I.db")
    rng = random.Random(99)
    rules: list = []
    history_seen = 0
    for step in range(12):
        rules = rules_by_step.get(step, rules)
        ops = []
        if step:
            ops = [_random_edit(corpus, rng, step) for _ in range(rng.randint(1, 2))]
            files = sorted(corpus.rglob("*.md"))
            moved = rng.choice(files)  # across the past/current boundary
            other = "topic-000" if moved.parent.name != "topic-000" else "topic-001"
            moved.rename(corpus / other / moved.name)
            ops.append(f"move {moved.name} -> {other}")
        compile_once(inc, corpus, conflict_rules=rules)
        ref = Store(tmp_path / f"B{step}.db")
        compile_once(ref, corpus, strategy="full", conflict_rules=rules)
        d = diff_states(canonical_state(ref), canonical_state(inc))
        history_seen += sum(1 for p in ref.load_state().of("page").values() if p.history)
        ref.close()
        assert d.equal, (step, ops, d.by_kind(), d.different[:3], d.missing[:3], d.extra[:3])
    assert history_seen > 0, "the sequence must exercise pages with history"
