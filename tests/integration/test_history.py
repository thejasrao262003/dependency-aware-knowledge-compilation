"""Past-scoped claims on pages: a claim whose every supporting document a [[conflicts.scope]]
rule declares past is listed, deterministically, under the page's History section instead of
among its current facts. Which claims are history is an input of each page's signatures, so a
claim moving between current and history rebuilds exactly the pages that show it, and
incremental builds stay equal to full ones."""

from __future__ import annotations

from pathlib import Path

import pytest

import dakc
from dakc import versions
from dakc.compiler import PAGE_FLAT, Compiler
from dakc.conflicts import ScopeRule
from dakc.providers import make_provider
from dakc.provenance.compare import canonical_state, diff_states
from dakc.provenance.trace import history_sentences, trace_page
from dakc.provenance.verify import verify_against_full, verify_store
from dakc.storage import Store

from tests.conftest import POSTGRES_DOCS, write_files

SESSIONS = [ScopeRule("sessions/**", status="past", label="{path}")]
RULE_TOML = '[[conflicts.scope]]\npath = "sessions/**"\nstatus = "past"\nlabel = "{path}"\n'
MARCH = "# Session 2026-03-02\n\nThe password hashing algorithm of Auth is Bcrypt. Auth runs on Kubernetes.\n"
MAY = "# Session 2026-05-14\n\nThe password hashing algorithm of Auth is Argon2.\n"
CURRENT = "# Auth\n\nThe password hashing algorithm of Auth is Argon2. Auth runs on Kubernetes.\n"
OTHER = "# Billing\n\nBilling runs on Nomad.\n"


class RecordingWriter:
    """The mock page writer, remembering the claims it was given per page."""

    def __init__(self, inner):
        self.inner, self.seen = inner, {}
        self.fingerprint, self.free, self.is_llm = inner.fingerprint, inner.free, inner.is_llm

    def write(self, name, claims):
        self.seen[name] = [c.text for c in claims]
        return self.inner.write(name, claims)


def build(store: Store, src: Path, rules=SESSIONS, strategy: str = "dakc"):
    bundle = make_provider("mock")
    bundle.writer = RecordingWriter(bundle.writer)
    report, _ = Compiler(store, bundle, src, conflict_rules=rules, strategy=strategy, jobs=1).run()
    return report, bundle.writer


def page(store: Store, pid: str):
    return store.load_state().of("page")[pid]


def sections(rendered: str) -> dict[str, str]:
    """Each '## ' section of a rendered page, by heading (the mock writer lists the current
    facts under "Facts"); the lead is under ''."""
    out, cur = {"": []}, ""
    for line in rendered.split("\n")[1:]:
        if line.startswith("## "):
            cur = line[3:]
            out[cur] = []
        else:
            out[cur].append(line)
    return {k: "\n".join(v).strip() for k, v in out.items()}


def equals_full(store: Store, src: Path, tmp_path: Path, rules=SESSIONS, name: str = "ref.db") -> bool:
    ref = Store(tmp_path / name)
    build(ref, src, rules, strategy="full")
    d = diff_states(canonical_state(ref), canonical_state(store))
    ref.close()
    return d.equal


@pytest.fixture
def kb(tmp_path):
    src = tmp_path / "knowledge"
    write_files(src, {"sessions/2026-03-02.md": MARCH, "current/auth.md": CURRENT, "billing/billing.md": OTHER})
    store = Store(tmp_path / ".dakc" / "dakc.db")
    yield src, store
    store.close()


def test_a_reversal_shows_the_new_value_as_current_and_the_old_under_history(kb, tmp_path):
    src, store = kb
    _, writer = build(store, src)
    auth = page(store, "page:auth")
    s = sections(auth.rendered)
    # the old value is only in a past note: History, cited, and never given to the writer
    assert s["History"] == "- The password hashing algorithm of Auth is Bcrypt. [^C3]"
    assert "Bcrypt" not in s["Facts"] and "Bcrypt" not in " ".join(writer.seen["Auth"])
    # the new value is in a current document: a current fact
    assert "The password hashing algorithm of Auth is Argon2. [^C2]" in s["Facts"]
    # stated in the past note and in a current document: current, cited by both
    assert "Auth runs on Kubernetes. [^C1]" in s["Facts"] and "Kubernetes" not in s["History"]
    assert "[^C1]: current/auth.md, sessions/2026-03-02.md" in s["Sources"]
    assert "[^C3]: sessions/2026-03-02.md" in s["Sources"]
    assert list(auth.history) == [auth.claim_ids[2]] and len(auth.claim_ids) == 3
    # History comes before Related; every History line traces to its claim and evidence
    assert auth.rendered.index("## History") < auth.rendered.index("## Related")
    t = trace_page(store, auth)
    assert not t.invalid_markers
    hist = next(x for x in t.sentences if "Bcrypt" in x.sentence)
    assert hist.cites == ["C3"] and t.claims[2].evidence[0].source_path == "sessions/2026-03-02.md"
    res = verify_store(store)
    assert res.ok
    # History lines are traced, but citation coverage counts generated sentences only
    pages = store.load_state().of("page").values()
    assert history_sentences(auth) == 1 and history_sentences(page(store, "page:argon2")) == 0
    assert res.metrics["citation_coverage"]["sentences"] == sum(
        len(trace_page(store, p).sentences) - history_sentences(p) for p in pages)
    assert equals_full(store, src, tmp_path)


def test_a_page_with_only_past_claims_is_written_without_a_writer_call(kb):
    src, store = kb
    rep, writer = build(store, src)
    bcrypt = page(store, "page:bcrypt")
    assert "Bcrypt" not in writer.seen and "page:bcrypt" not in rep.ops["regenerate_page"]
    assert bcrypt.body == "No current source describes Bcrypt. What earlier sources recorded is listed under History."
    assert bcrypt.lead == bcrypt.body  # what the topic summary reads
    assert sections(bcrypt.rendered)["History"] == "- The password hashing algorithm of Auth is Bcrypt. [^C1]"
    with dakc.open(_workspace(src.parent, rules=True)) as kb_:
        assert kb_.read("page:bcrypt").metadata["history"] == list(bcrypt.history)


def test_without_a_past_rule_nothing_is_history(kb, tmp_path):
    src, store = kb
    build(store, src, rules=[ScopeRule("sessions/**", label="{path}")])  # a label, no status
    auth = page(store, "page:auth")
    assert auth.history == () and "History" not in sections(auth.rendered) and "Bcrypt" in sections(auth.rendered)["Facts"]
    # nor is anything inferred from tense or headings: "was" and a History heading stay current
    write_files(src, {"current/history.md": "# History\n\nThe password hashing algorithm of Auth was MD5.\n"})
    build(store, src)
    assert "History" not in sections(page(store, "page:md5").rendered)


def test_history_lists_claims_by_their_earliest_document(kb):
    src, store = kb
    write_files(src, {"sessions/2026-01-10.md": "# Session\n\nAuth uses Vault.\n",
                      "sessions/2026-04-01.md": "# Session\n\nAuth uses Consul.\n"})
    build(store, src)
    lines = sections(page(store, "page:auth").rendered)["History"].split("\n")
    assert [line.split(" [^")[0] for line in lines] == [
        "- Auth uses Vault.",  # sessions/2026-01-10.md
        "- The password hashing algorithm of Auth is Bcrypt.",  # sessions/2026-03-02.md
        "- Auth uses Consul.",  # sessions/2026-04-01.md
    ]


def _workspace(root: Path, rules: bool) -> Path:
    (root / "dakc.toml").write_text('[dakc]\nsource_dir = "knowledge"\nprovider = "mock"\n\n' + (RULE_TOML if rules else ""),
                                    encoding="utf-8")
    return root


def _touched(rep) -> set[str]:
    return {p for k in ("regenerate_page", "rerender_page") for p in rep.ops.get(k, [])}


def test_a_scope_change_in_the_config_rebuilds_exactly_the_pages_that_show_its_claims(tmp_path):
    """sessions/ stops being past: the claims it alone supports become current, and only the
    pages that show them are rebuilt. The result equals a full build, and back again."""
    ws = _workspace(tmp_path, rules=True)
    write_files(ws / "knowledge", {"sessions/2026-03-02.md": MARCH, "current/auth.md": CURRENT, "billing/billing.md": OTHER,
                                   "sessions/2026-05-14.md": MAY})
    with dakc.open(ws) as kb:
        kb.compile()
        assert "Bcrypt" in sections(kb.read("page:auth").text)["History"]
        n_pages = len(kb.store.load_state().of("page"))

    _workspace(tmp_path, rules=False)
    with dakc.open(ws) as kb:
        assert any("[[conflicts.scope]]" in r for r in kb.status().outdated)
        rep = kb.compile()
        # "Bcrypt" is now current: the pages that show it, and nothing else. "Argon2" and
        # "Kubernetes" were current already (a current document states them).
        assert _touched(rep) == {"page:auth", "page:bcrypt"}
        assert len(rep.ops["cutoff_page"]) == n_pages - 2
        assert rep.usage.by_stage.get("extract", 0) == 0
        assert "History" not in sections(kb.read("page:auth").text)
        assert kb.verify(against_full=True).equal_to_full_rebuild

    _workspace(tmp_path, rules=True)
    with dakc.open(ws) as kb:
        rep = kb.compile()
        assert _touched(rep) == {"page:auth", "page:bcrypt"}
        assert "Bcrypt" in sections(kb.read("page:auth").text)["History"]
        assert kb.verify(against_full=True).equal_to_full_rebuild
        assert kb.compile().status == "noop"


def test_moving_a_document_into_or_out_of_scope_rebuilds_exactly_the_affected_pages(kb, tmp_path):
    src, store = kb
    build(store, src)
    # the March note is moved out of sessions/: current now, so Bcrypt is a current fact
    (src / "current" / "march.md").write_bytes((src / "sessions" / "2026-03-02.md").read_bytes())
    (src / "sessions" / "2026-03-02.md").unlink()
    rep, _ = build(store, src)
    assert rep.ops["source_renamed"] and _touched(rep) == {"page:auth", "page:bcrypt", "page:kubernetes"}
    assert page(store, "page:auth").history == () and page(store, "page:bcrypt").history == ()
    assert equals_full(store, src, tmp_path, name="ref1.db")
    # and back: the claims it alone supports return to History
    (src / "sessions" / "2026-03-02.md").write_bytes((src / "current" / "march.md").read_bytes())
    (src / "current" / "march.md").unlink()
    rep, _ = build(store, src)
    assert "Bcrypt" in sections(page(store, "page:auth").rendered)["History"]
    assert equals_full(store, src, tmp_path, name="ref2.db")


def test_support_from_a_current_document_moves_a_claim_out_of_history_and_back(kb, tmp_path):
    src, store = kb
    build(store, src)
    write_files(src, {"current/security.md": "# Security\n\nThe password hashing algorithm of Auth is Bcrypt.\n"})
    rep, _ = build(store, src)
    assert page(store, "page:bcrypt").history == () and "page:billing" not in _touched(rep)
    assert equals_full(store, src, tmp_path, name="ref1.db")
    (src / "current" / "security.md").unlink()
    rep, _ = build(store, src)
    assert page(store, "page:bcrypt").history != () and "page:billing" not in _touched(rep)
    assert equals_full(store, src, tmp_path, name="ref2.db")


def test_strict_provenance_keeps_history(kb):
    src, store = kb
    bundle = make_provider("mock")
    Compiler(store, bundle, src, conflict_rules=SESSIONS, provenance="strict").run()
    auth = page(store, "page:auth")
    assert sections(auth.rendered)["History"] == "- The password hashing algorithm of Auth is Bcrypt. [^C3]"
    assert not trace_page(store, auth).invalid_markers


def test_a_hub_page_writes_only_its_current_claims_in_sections(tmp_path):
    src = tmp_path / "knowledge"
    n = PAGE_FLAT + 10
    write_files(src, {
        "guide/a.md": "# Guide\n\n" + "\n\n".join(f"Hub supports Feature{i:03d}." for i in range(n)) + "\n",
        "sessions/2026-03-02.md": "# Session\n\n" + "\n\n".join(f"Hub supports Legacy{i:03d}." for i in range(20)) + "\n",
    })
    store = Store(tmp_path / "s.db")
    build(store, src)
    hub = page(store, "page:hub")
    assert len(hub.history) == 20 and len(hub.claim_ids) == n + 20
    assert sum(len(sec[3]) for sec in hub.sections) == n and not set(hub.history) & {c for sec in hub.sections for c in sec[3]}
    assert hub.body.startswith(f"Hub has {n} documented facts")
    assert f"Hub supports Legacy000. [^C{n + 1}]" in sections(hub.rendered)["History"]
    assert not trace_page(store, hub).invalid_markers
    equal, detail = verify_against_full(store, src, make_provider("mock"), {}, conflict_rules=SESSIONS)
    assert equal, detail
    store.close()


def test_the_render_version_bump_rerenders_without_calls_and_changes_no_page_without_history(tmp_path, monkeypatch):
    """render/2 is a rendering change: a knowledge base built by render/1 is re-rendered with no
    provider call, and a workspace with no past rule gets back the same page text."""
    src = tmp_path / "knowledge"
    write_files(src, POSTGRES_DOCS)
    store = Store(tmp_path / "s.db")
    monkeypatch.setattr(versions, "RENDER", "render/1")
    Compiler(store, make_provider("mock"), src).run()
    before = {p.id: p.rendered for p in store.load_state().of("page").values()}
    monkeypatch.undo()
    rep, _ = Compiler(store, make_provider("mock"), src).run()
    assert rep.usage.calls == 0 and rep.n("rerender_page") == len(before) and rep.n("regenerate_page") == 0
    assert {p.id: p.rendered for p in store.load_state().of("page").values()} == before
    assert store.load_state().compiler_versions["render"] == versions.RENDER
    store.close()


def test_the_cli_says_which_configuration_changed(tmp_path):
    """Notes and status quote "[[conflicts.scope]]" literally (not as Rich markup)."""
    from typer.testing import CliRunner

    from dakc.cli import app

    ws = _workspace(tmp_path, rules=True)
    write_files(ws / "knowledge", {"sessions/2026-03-02.md": MARCH, "current/auth.md": CURRENT})
    run = CliRunner().invoke
    assert run(app, ["compile", "-w", str(ws)]).exit_code == 0
    _workspace(tmp_path, rules=False)
    status = run(app, ["status", "-w", str(ws)])
    assert "the status of [[conflicts.scope]] rules" in " ".join(status.output.split())
    out = run(app, ["compile", "-w", str(ws)])
    assert out.exit_code == 0 and "the status of [[conflicts.scope]] rules" in " ".join(out.output.split())
    assert "Bcrypt" in (ws / "wiki" / "pages" / "auth.md").read_text().split("## Related")[0].split("## Facts")[1]
