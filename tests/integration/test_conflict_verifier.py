"""The optional conflict verifier (RFC 0002, phase 2), with a scripted model in place of an
LLM: candidates are judged once and cached, its findings close when a statement changes,
they are recomputed from the verdicts on record (so they equal a detection from scratch),
and ``max_checks`` bounds what one compile spends without losing the rest."""

from __future__ import annotations

from pathlib import Path

import dakc
from dakc.conflicts.verifier import Judged

from tests.conftest import write_files

GO = "# Ledger\n\nLedger is written in Go.\n"
PYTHON = "# Services\n\nLedger is a Python service.\n"
VERIFIER = '[conflicts]\nverifier = "llm"\n'


class ScriptedVerifier:
    """Says ``contradiction`` for the listed pairs of claim texts, ``compatible`` otherwise."""

    is_llm = True
    free = False

    def __init__(self, contradictions=(), scoped=(), fingerprint="scripted-verifier/1"):
        self.contradictions = {frozenset(p) for p in contradictions}
        self.scoped = {frozenset(p) for p in scoped}
        self.fingerprint = fingerprint
        self.judged: list[tuple[str, str]] = []

    def judge(self, pairs):
        self.judged += pairs
        out = []
        for a, b in pairs:
            key = frozenset((a, b))
            out.append(("contradiction", "one says Go, the other Python") if key in self.contradictions
                       else ("different_scope", "the versions differ") if key in self.scoped else ("compatible", ""))
        return Judged(out, input_tokens=60 * len(pairs), output_tokens=15 * len(pairs))


def workspace(root: Path, files: dict[str, str], toml_extra: str = VERIFIER) -> Path:
    write_files(root / "knowledge", files)
    (root / "dakc.toml").write_text('[dakc]\nsource_dir = "knowledge"\nprovider = "mock"\n\n' + toml_extra, encoding="utf-8")
    return root


def opened(ws: Path, verifier: ScriptedVerifier):
    kb = dakc.open(ws)
    kb.provider().verifier = verifier
    return kb


def test_a_contradiction_the_rules_cannot_see(tmp_path):
    ws = workspace(tmp_path, {"ledger.md": GO, "services.md": PYTHON})
    v = ScriptedVerifier([("Ledger is written in Go.", "Ledger is a Python service.")])
    with opened(ws, v) as kb:
        rep = kb.compile()
        assert sorted(v.judged[0]) == ["Ledger is a Python service.", "Ledger is written in Go."] and len(v.judged) == 1
        assert rep.usage.by_stage.get("verify") == 1 and rep.usage.llm_calls >= 1
        (c,) = kb.conflicts()
        assert c.kind == "possible" and c.property == "Ledger" and c.reason == "one says Go, the other Python"
        assert rep.ops["conflict_found"] == [c.id] and rep.counts["conflicts_open"] == 1
        assert rep.counts["possible_conflicts_open"] == 1 and kb.status().conflicts_open == 1
        full = kb.conflict(c.id)
        where = sorted((loc.path, loc.line) for val in full.values for loc in val.locations)
        assert where == [("ledger.md", 3), ("services.md", 3)]
        assert any(a.id == "page:ledger" for a in full.affected["page"])
        assert kb.verify().checks["conflict findings equal a detection from scratch"]

        # nothing changed: nothing is judged again
        assert kb.compile().status == "noop" and len(v.judged) == 1

        # edit one side: the pair is gone, the new pair is judged compatible
        (ws / "knowledge" / "services.md").write_text("# Services\n\nLedger is a Go service.\n", encoding="utf-8")
        rep = kb.compile()
        assert rep.ops["conflict_resolved"] == [c.id] and kb.conflicts() == []
        assert len(v.judged) == 2 and kb.verify().checks["conflict findings equal a detection from scratch"]


def test_scope_still_applies_to_the_verifiers_findings(tmp_path):
    ws = workspace(tmp_path, {"ledger.md": "# Ledger\n\n## Release 1 (MVP)\n\nLedger is written in Go.\n",
                              "services.md": PYTHON, "billing.md": "# Billing\n\nBilling is written in Rust.\n",
                              "billing-v2.md": "# Billing\n\nBilling is a Go service.\n"})
    v = ScriptedVerifier([("Ledger is written in Go.", "Ledger is a Python service.")],
                         scoped=[("Billing is written in Rust.", "Billing is a Go service.")])
    with opened(ws, v) as kb:
        kb.compile()
        assert kb.conflicts() == []
        found = kb.conflicts(scoped=True)
        assert sorted(c.kind for c in found) == ["scoped", "scoped"]
        assert any("release 1" in loc.scope for c in found for val in c.values for loc in val.locations)


def test_max_checks_spreads_the_work_over_compiles(tmp_path):
    files = {f"doc{i}.md": f"# Doc {i}\n\nLedger handles {name} payments.\n" for i, name in enumerate(["Card", "Bank", "Wire", "Cash"])}
    ws = workspace(tmp_path, files, VERIFIER + "max_checks = 2\n")
    v = ScriptedVerifier()
    with opened(ws, v) as kb:
        rep = kb.compile()
        assert rep.counts["conflict_pairs_checked"] == 2 and rep.counts["conflict_pairs_unchecked"] == 4
        assert any("left to check" in r for r in kb.status().outdated)
        while any("left to check" in r for r in kb.status().outdated):
            kb.compile()
        assert len(v.judged) == 6 and len(set(v.judged)) == 6  # every pair of the four documents, once
        assert kb.compile().status == "noop" and len(v.judged) == 6


def test_a_dry_run_counts_the_checks_and_makes_none(tmp_path):
    ws = workspace(tmp_path, {"ledger.md": GO, "services.md": PYTHON})
    v = ScriptedVerifier([("Ledger is written in Go.", "Ledger is a Python service.")])
    with opened(ws, v) as kb:
        plan = kb.plan()
        assert plan.planned_llm_calls["verify"] == 1 and v.judged == []


def test_turning_the_verifier_off_and_on(tmp_path):
    ws = workspace(tmp_path, {"ledger.md": GO, "services.md": PYTHON})
    pair = ("Ledger is written in Go.", "Ledger is a Python service.")
    v = ScriptedVerifier([pair])
    with opened(ws, v) as kb:
        kb.compile()
        assert len(kb.conflicts()) == 1
    (ws / "dakc.toml").write_text((ws / "dakc.toml").read_text().replace(VERIFIER, ""), encoding="utf-8")
    with opened(ws, v) as kb:
        assert any("turned off" in r for r in kb.status().outdated)
        kb.compile()
        assert kb.conflicts(scoped=True) == [] and kb.status().up_to_date
        assert kb.verify().checks["conflict findings equal a detection from scratch"]
    (ws / "dakc.toml").write_text((ws / "dakc.toml").read_text() + VERIFIER, encoding="utf-8")
    with opened(ws, v) as kb:
        assert any("turned on" in r for r in kb.status().outdated)
        kb.compile()
        assert len(kb.conflicts()) == 1 and len(v.judged) == 1  # the verdict on record: no new call
        assert kb.status().up_to_date


def test_a_new_verifier_judges_again(tmp_path):
    ws = workspace(tmp_path, {"ledger.md": GO, "services.md": PYTHON})
    pair = ("Ledger is written in Go.", "Ledger is a Python service.")
    with opened(ws, ScriptedVerifier([pair])) as kb:
        kb.compile()
        assert len(kb.conflicts()) == 1
    other = ScriptedVerifier([], fingerprint="scripted-verifier/2")
    with opened(ws, other) as kb:
        kb.compile()
        assert kb.conflicts() == [] and len(other.judged) == 1


def test_accepting_a_possible_conflict(tmp_path):
    ws = workspace(tmp_path, {"ledger.md": GO, "services.md": PYTHON})
    v = ScriptedVerifier([("Ledger is written in Go.", "Ledger is a Python service.")])
    with opened(ws, v) as kb:
        kb.compile()
        (c,) = kb.conflicts()
    (ws / "dakc.toml").write_text((ws / "dakc.toml").read_text()
                                  + f'\n[[conflicts.accept]]\nid = "{c.id}"\nreason = "two services"\n', encoding="utf-8")
    with opened(ws, v) as kb:
        rep = kb.compile()
        assert rep.counts["conflicts_open"] == 0 and kb.conflicts() == []
        assert kb.conflicts(accepted=True)[0].accepted == "two services"


def test_a_possessive_names_the_same_subject(tmp_path):
    """The free extractor reads "Ledger's" as its own entity; the verifier still compares the two."""
    ws = workspace(tmp_path, {"a.md": "# A\n\nLedger's database is MongoDB.\n", "b.md": "# B\n\nLedger runs on PostgreSQL.\n"})
    v = ScriptedVerifier([("Ledger's database is MongoDB.", "Ledger runs on PostgreSQL.")])
    with opened(ws, v) as kb:
        kb.compile()
        (c,) = kb.conflicts()
        assert c.kind == "possible" and c.property == "Ledger"
        assert kb.verify().checks["conflict findings equal a detection from scratch"]
