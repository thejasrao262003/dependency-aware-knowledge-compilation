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


_TURN_REF = r" ?<!-- t:\w+ -->"


def _comment_edit(corpus, rng: random.Random, step: int) -> str:
    """Add, rewrite, remove or move an HTML comment after a sentence (a turn reference),
    or edit a word next to one. Comments are passage metadata (PARSER parse/4)."""
    import re

    files = sorted(corpus.rglob("*.md"))
    commented = [(f, i) for f in files for i, line in enumerate(f.read_text().split("\n")) if "<!-- t:" in line]
    op = rng.choice(["add", "add", "change", "remove", "move", "text"])
    if op == "add" or not commented:
        target = rng.choice(files)
        body = [i for i, line in enumerate(target.read_text().split("\n")) if line and not line.startswith("#")]
        if not body:
            return "none"
        i, op = rng.choice(body), "text" if op == "text" else "add"
    else:
        target, i = rng.choice(commented)
    lines = target.read_text().split("\n")
    line = lines[i]
    ref = f" <!-- t:{step:02d}{rng.randrange(1000):03d} -->"

    def insert(text: str) -> str:
        ends = [m.end() for m in re.finditer(r"[.!?](?= |$)", text)]
        if not ends:
            return text
        pos = rng.choice(ends)
        return text[:pos] + ref + text[pos:]

    found = list(re.finditer(_TURN_REF, line))
    if op == "add":
        line = insert(line)
    elif op == "change" and found:
        m = rng.choice(found)
        line = line[:m.start()] + m.group(0).replace(" -->", "x -->") + line[m.end():]
    elif op in ("remove", "move") and found:
        m = rng.choice(found)
        line = line[:m.start()] + line[m.end():]
        if op == "move":
            ref = m.group(0) if m.group(0).startswith(" ") else " " + m.group(0)
            line = insert(line)
    elif op == "text":  # a word outside the comments
        parts = re.split(f"({_TURN_REF})", line)
        k = rng.choice([j for j in range(0, len(parts), 2) if parts[j].strip()])
        words = parts[k].split(" ")
        w = rng.choice([j for j, x in enumerate(words) if x])
        words[w] = rng.choice(["Kelitron", "Zanbex", "2042"])
        parts[k] = " ".join(words)
        line = "".join(parts)
    lines[i] = line
    target.write_text("\n".join(lines))
    return op


def test_comment_edit_sequence_stays_equivalent(base, tmp_path):
    root, _ = base
    corpus = tmp_path / "corpus"
    shutil.copytree(root / "corpus", corpus)
    shutil.copy(root / "A.db", tmp_path / "I.db")
    inc = Store(tmp_path / "I.db")
    rng = random.Random(4321)
    for step in range(8):
        ops = [_comment_edit(corpus, rng, step) for _ in range(rng.randint(2, 6))]
        if step % 3 == 2:
            ops.append(_random_edit(corpus, rng, step))  # rename, duplicate, swap... next to comments
        rep = compile_once(inc, corpus)
        if set(ops) <= {"add", "change", "remove", "move", "none"}:  # comments only: nothing to extract or render
            assert rep.usage.calls == 0 and rep.n("reextract") == 0, (step, ops)
            assert not any(rep.n(op) for op in ("regenerate_page", "rerender_page", "new_claim", "retract_claim")), (step, ops)
        ref = Store(tmp_path / f"B{step}.db")
        compile_once(ref, corpus, strategy="full")
        d = diff_states(canonical_state(ref), canonical_state(inc))
        ref.close()
        assert d.equal, (step, ops, d.by_kind(), d.different[:3], d.missing[:3], d.extra[:3])
    refs = [json.loads(p) for (p,) in inc.conn.execute(
        "SELECT payload FROM artifacts WHERE kind = 'passage' AND to_build IS NULL AND payload LIKE '%\"refs\"%'")]
    assert refs, "the sequence left passages with comment refs"
