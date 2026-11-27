"""HTML comments written after a sentence (Reckon's turn references) are passage metadata.

They are kept per sentence, shown by ``trace`` and ``read`` (and the CLI, REST and MCP
outputs that mirror them), and left out of every hash that drives extraction or
rendering: a comment-only edit costs no provider call and changes no page.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import dakc
from dakc import versions
from dakc.hashing import claim_id
from dakc.provenance.compare import canonical_state, diff_states
from dakc.storage import Store

from tests.conftest import compile_once, write_files

SESSION = "sessions/2026-03-01.md"
NOTES = {
    SESSION: (
        "---\nsession: 3f9a12c4\ndate: 2026-03-01\n---\n# Auth session\n\n"
        "- The refresh endpoint takes a per-user Redis lock with a 5-second TTL. <!-- t:3f9a12c4 -->\n"
        "- Use Argon2 for password hashing. <!-- rs_01JDQ8X4Z7K3M9P2Q5R8S1T4V6#t:3f9a12c4 -->\n\n"
        "Redis holds the Sessions. <!-- t:aa11 --> Postgres holds the Users. <!-- t:bb22 -->\n"
    ),
    "current/auth.md": "# Auth\n\nArgon2 hashes every Password. Redis holds the Sessions.\n",
}
SHARED = claim_id("Redis holds the Sessions.")  # stated in both documents, with a comment in one


@pytest.fixture
def kb(tmp_path):
    write_files(tmp_path / "knowledge", NOTES)
    assert dakc.ingest(tmp_path / "knowledge", tmp_path, provider="mock").status == "committed"
    with dakc.open(tmp_path) as kb:
        yield kb


def wiki_bytes(kb) -> dict[str, bytes]:
    return {p.relative_to(kb.output_root).as_posix(): p.read_bytes() for p in sorted(kb.output_root.rglob("*.md"))}


def derived_state(kb) -> dict[str, str]:
    """State hash of every current artifact except passages."""
    return dict(kb.store.conn.execute("SELECT id, state_hash FROM artifacts WHERE to_build IS NULL AND kind != 'passage'"))


def equals_full(store: Store, src: Path, tmp_path: Path) -> bool:
    ref = Store(tmp_path / "full.db")
    compile_once(ref, src, strategy="full")
    d = diff_states(canonical_state(ref), canonical_state(store))
    ref.close()
    assert d.equal, (d.by_kind(), d.different[:3], d.missing[:3], d.extra[:3])
    return True


def edit(kb, rel: str, old: str, new: str) -> None:
    p = kb.source_root / rel
    text = p.read_text(encoding="utf-8")
    assert old in text
    p.write_text(text.replace(old, new), encoding="utf-8")


def test_comment_only_edit_costs_no_provider_call_and_changes_no_page(kb, tmp_path):
    pages, derived = wiki_bytes(kb), derived_state(kb)
    assert any(k.startswith("pages/") for k in pages)
    edit(kb, SESSION, "<!-- t:3f9a12c4 -->", "<!-- t:9e8d7c6b -->")  # a turn reference rewritten
    edit(kb, SESSION, " <!-- t:aa11 -->", "")  # one removed
    edit(kb, "current/auth.md", "every Password.", "every Password. <!-- t:new1 -->")  # one added to a plain document

    plan = kb.plan()
    assert plan.has_work() and plan.llm_calls() == 0 and plan.exact
    assert plan.n("passage_refs_changed") == 3 and plan.n("reextract") == 0

    rep = kb.compile()
    assert rep.status == "committed"
    assert rep.usage.llm_calls == 0 and rep.usage.calls == 0 and rep.usage.by_stage.get("extract", 0) == 0
    assert rep.n("passage_refs_changed") == 3 and rep.counts["passages_added"] == 0
    for op in ("reextract", "new_claim", "retract_claim", "claim_changed", "claim_provenance_changed",
               "regenerate_page", "rerender_page", "regenerate_summary", "rerender_summary", "rerender_index"):
        assert rep.n(op) == 0, op
    assert rep.projection.get("written", 0) == 0
    assert wiki_bytes(kb) == pages, "page bytes are unchanged"
    assert derived_state(kb) == derived, "only passages got new versions"

    ev = {e.source_path: e for e in kb.trace(SHARED).evidence}
    assert ev[SESSION].refs == [[]] and ev["current/auth.md"].refs == [[]]
    lock = kb.trace(claim_id("The refresh endpoint takes a per-user Redis lock with a 5-second TTL."))
    assert lock.evidence[0].refs == [["t:9e8d7c6b"]]
    assert equals_full(kb.store, kb.source_root, tmp_path)
    assert kb.compile().status == "noop"


def test_removing_every_comment_restores_the_passage_payload(kb):
    psg = next(p for p in kb.artifacts("passage", query="Postgres holds", limit=None))
    before = kb.read(psg.id)
    assert before.metadata["refs"] == [["t:aa11"], ["t:bb22"]]
    edit(kb, SESSION, " <!-- t:aa11 --> Postgres holds the Users. <!-- t:bb22 -->", " Postgres holds the Users.")
    assert kb.compile().n("passage_refs_changed") == 1
    after = kb.read(psg.id)
    assert after.text == before.text and "refs" not in after.metadata
    assert after.version == before.version + 1


def test_trace_and_read_carry_each_evidence_sentences_refs(kb):
    t = kb.trace("page:argon2")
    use = next(c for c in t.claims if c.text == "Use Argon2 for password hashing.")
    (ev,) = use.evidence
    assert ev.source_path == SESSION and ev.sentences == ["Use Argon2 for password hashing."]
    assert ev.refs == [["rs_01JDQ8X4Z7K3M9P2Q5R8S1T4V6#t:3f9a12c4"]]
    plain = next(c for c in t.claims if c.text == "Argon2 hashes every Password.")
    assert plain.evidence[0].refs == [[]], "a sentence without comments has an empty refs entry"
    d = t.to_dict()
    assert {"sentences", "refs"} <= d["claims"][0]["evidence"][0].keys()

    shared = kb.trace(SHARED)  # one claim, two supporting documents
    assert {e.source_path: e.refs for e in shared.evidence} == {SESSION: [["t:aa11"]], "current/auth.md": [[]]}

    claim = kb.read(SHARED)
    supports = {s["passage_id"]: s for s in claim.metadata["supports"]}
    by_path = {e.source_path: supports[e.passage_id] for e in shared.evidence}
    assert by_path[SESSION]["evidence_sentences"] == [0] and by_path[SESSION]["refs"] == [["t:aa11"]]
    assert by_path["current/auth.md"]["evidence_sentences"] == [1] and by_path["current/auth.md"]["refs"] == [[]]
    psg = kb.read(next(e.passage_id for e in shared.evidence if e.source_path == SESSION))
    assert psg.text == "Redis holds the Sessions. Postgres holds the Users."
    assert psg.metadata["refs"] == [["t:aa11"], ["t:bb22"]]
    json.dumps(claim.to_dict()), json.dumps(t.to_dict())  # JSON-safe


def test_text_edits_next_to_comments_reuse_sentences_as_usual(kb, tmp_path):
    """Comments never enter sentence keys: editing one sentence of a commented paragraph
    re-extracts only that sentence, and the untouched sentence keeps its claim and refs."""
    edit(kb, SESSION, "Postgres holds the Users. <!-- t:bb22 -->", "Postgres holds the Accounts. <!-- t:cc33 -->")
    rep = kb.compile()
    assert rep.n("reextract") == 1 and rep.counts["sentences_reused"] == 1 and rep.counts["sentences_extracted"] == 1
    assert rep.n("claim_changed") == 0 and SHARED not in rep.ops.get("new_claim", [])
    ev = {e.source_path: e.refs for e in kb.trace(SHARED).evidence}
    assert ev[SESSION] == [["t:aa11"]]
    accounts = kb.trace(claim_id("Postgres holds the Accounts."))
    assert accounts.evidence[0].refs == [["t:cc33"]]
    assert equals_full(kb.store, kb.source_root, tmp_path)


def test_upgrading_to_parse_4_fills_refs_and_runs_nothing_else(kb, tmp_path):
    """A store built by parse/3 has no refs. The PARSER bump re-parses every source and
    fills them in: passages only, with no provider call and no page change."""
    store = kb.store
    store.conn.execute("UPDATE artifacts SET payload = json_remove(payload, '$.refs') WHERE kind = 'passage' AND to_build IS NULL")
    stored = json.loads(store.get_meta("compiler_versions"))
    store.set_meta("compiler_versions", json.dumps({**stored, "parser": "parse/3"}, sort_keys=True))
    store.conn.commit()
    assert versions.PARSER == "parse/4"
    assert any("parser logic changed (parse/3 -> parse/4)" in r for r in kb.status().outdated)
    pages, derived = wiki_bytes(kb), derived_state(kb)
    rep = kb.compile()
    assert any("parser" in n for n in rep.notes) and rep.counts["sources_processed"] == 2
    assert rep.n("passage_refs_changed") == 3 and rep.counts["passages_added"] == 0
    assert rep.usage.calls == 0 and rep.n("regenerate_page") == 0 and rep.n("rerender_page") == 0
    assert wiki_bytes(kb) == pages and derived_state(kb) == derived
    assert kb.trace(SHARED).evidence and equals_full(kb.store, kb.source_root, tmp_path)
    assert kb.compile().status == "noop"
