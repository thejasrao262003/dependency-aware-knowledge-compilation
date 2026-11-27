"""The conflict taxonomy (RFC 0002): what is a conflict, what is explained by scope, and
what is not reported at all. Each case is two statements from (path, heading, sentence),
extracted with the deterministic heuristic extractor, then judged."""

from __future__ import annotations

import pytest

from dakc.conflicts import Occurrence, ScopeRule, detect, scope_of
from dakc.hashing import claim_id
from dakc.providers.deterministic import HeuristicExtractor

EX = HeuristicExtractor()

# Session notes, one per session (Reckon's layout): every note is past, and its own scope.
SESSIONS = [ScopeRule("sessions/**", status="past", label="{path}")]
BCRYPT = "The password hashing algorithm of Auth is Bcrypt."
ARGON2 = "The password hashing algorithm of Auth is Argon2."

# (case, (path, heading, sentence), (path, heading, sentence) or None, expected, rules)
CASES = [
    # --- conflicts: the same property, the same scope, incompatible
    ("negation", ("database.md", "DB", "PostgreSQL supports JSONB."),
     ("architecture.md", "Arch", "PostgreSQL does not support JSONB."), "conflict", None),
    ("contraction", ("a.md", "A", "Redis supports JSONB."), ("b.md", "B", "Redis doesn't support JSONB."), "conflict", None),
    ("no longer: one doc is stale", ("a.md", "A", "PostgreSQL supports Python 2."),
     ("b.md", "B", "PostgreSQL no longer supports Python 2."), "conflict", None),
    ("two docs, two databases, no scope", ("database.md", "Tech stack", "MongoDB is the database."),
     ("architecture.md", "Tech stack", "PostgreSQL is the database."), "conflict", None),
    ("word order", ("a.md", "A", "The primary database is MongoDB."), ("b.md", "B", "PostgreSQL is the primary database."), "conflict", None),
    ("default port", ("a.md", "A", "PostgreSQL's default port is 5432."), ("b.md", "B", "PostgreSQL's default port is 5433."), "conflict", None),
    ("node count", ("a.md", "A", "Kafka runs on three nodes."), ("b.md", "B", "Kafka runs on five nodes."), "conflict", None),
    ("limit", ("a.md", "A", "PostgreSQL supports up to 100 connections."),
     ("b.md", "B", "PostgreSQL supports up to 500 connections."), "conflict", None),
    ("recommendation", ("security.md", "Hashing", 'The recommended algorithm is "Argon2".'),
     ("team-notes.md", "Notes", 'The recommended algorithm is "Bcrypt".'), "conflict", None),
    ("license", ("a.md", "A", "PostgreSQL is licensed under the PostgreSQL License."),
     ("b.md", "B", "PostgreSQL is licensed under the MIT license."), "conflict", None),
    ("inside one document", ("a.md", "A", "Kafka runs on three nodes. Kafka runs on five nodes."), None, "conflict", None),
    ("same release, two values", ("a.md", "A", "In release 1, MongoDB is the database."),
     ("b.md", "B", "In release 1, PostgreSQL is the database."), "conflict", None),
    ("both in production", ("a.md", "A", "In production, Kafka runs on five nodes."),
     ("b.md", "B", "In production, Kafka runs on three nodes."), "conflict", None),
    ("oauth2.md is not a revision", ("oauth2.md", "OAuth", "Keycloak is the identity provider."),
     ("oauth.md", "OAuth", "Auth0 is the identity provider."), "conflict", None),
    ("two past notes, one shared label", ("sessions/2026-03-02.md", "Auth", BCRYPT),
     ("sessions/2026-05-14.md", "Auth", ARGON2), "conflict", [ScopeRule("sessions/**", status="past")]),
    ("two statements in one note, a label per document", ("sessions/2026-03-02.md", "Auth", f"{BCRYPT} {ARGON2}"), None,
     "conflict", SESSIONS),
    ("label per folder, one folder", ("teams/payments/db.md", "DB", "MongoDB is the database."),
     ("teams/payments/stack.md", "Stack", "PostgreSQL is the database."), "conflict", [ScopeRule("teams/**", label="{dir}")]),

    # --- scoped: incompatible values, explained by time, release, environment or status
    ("revision file beside the current one", ("techstack001.md", "Tech stack", "MongoDB is the DB."),
     ("techstack.md", "Tech stack", "PostgreSQL is the DB."), "scoped", None),
    ("release in heading", ("techstack.md", "Tech stack > Release 1 (MVP)", "MongoDB is the database."),
     ("techstack.md", "Tech stack > Production", "PostgreSQL is the database."), "scoped", None),
    ("release in sentence", ("a.md", "A", "In release 1 (MVP), MongoDB is the database."),
     ("b.md", "B", "PostgreSQL is the database."), "scoped", None),
    ("past tense", ("a.md", "A", "MongoDB was the database until the migration."),
     ("b.md", "B", "PostgreSQL is the database."), "scoped", None),
    ("environments", ("a.md", "A", "In production, PostgreSQL is the database."),
     ("b.md", "B", "In development, SQLite is the database."), "scoped", None),
    ("scope rule in dakc.toml", ("docs/mvp/stack.md", "Stack", "MongoDB is the database."),
     ("docs/stack.md", "Stack", "PostgreSQL is the database."), "scoped", [ScopeRule("docs/mvp/**", status=None, label="release 1")]),
    ("a decision and its reversal, in two notes with a label per document", ("sessions/2026-03-02.md", "Auth", BCRYPT),
     ("sessions/2026-05-14.md", "Auth", ARGON2), "scoped", SESSIONS),
    ("a past note against a current document", ("sessions/2026-03-02.md", "Auth", BCRYPT),
     ("current/auth.md", "Auth", ARGON2), "scoped", SESSIONS),
    ("label per folder, two folders", ("teams/payments/db.md", "DB", "MongoDB is the database."),
     ("teams/search/db.md", "DB", "PostgreSQL is the database."), "scoped", [ScopeRule("teams/**", label="{dir}")]),
    ("archive path", ("archive/techstack.md", "Tech stack", "MongoDB is the database."),
     ("techstack.md", "Tech stack", "PostgreSQL is the database."), "scoped", None),
    ("decision records", ("docs/adr/0001-database.md", "Decision", "MongoDB is the database."),
     ("docs/adr/0004-database.md", "Decision", "PostgreSQL is the database."), "scoped", None),
    ("node count by environment", ("a.md", "A", "In production, Kafka runs on five nodes."),
     ("b.md", "B", "In staging, Kafka runs on one node."), "scoped", None),
    ("rejected option", ("adr.md", "ADR-3 > Rejected alternatives", "MongoDB is the database."),
     ("b.md", "B", "PostgreSQL is the database."), "scoped", None),
    ("FAQ myth", ("faq.md", "FAQ > Myths", "PostgreSQL does not support JSONB."),
     ("b.md", "B", "PostgreSQL supports JSONB."), "scoped", None),
    ("conditional", ("a.md", "A", "If caching is enabled, Ledger uses Redis."),
     ("b.md", "B", "Ledger does not use Redis."), "scoped", None),
    ("release notes", ("release-notes.md", "Release Notes > 0.35.0", "FastAPI's default timeout is 30 seconds."),
     ("release-notes.md", "Release Notes > 0.40.0", "FastAPI's default timeout is 60 seconds."), "scoped", None),

    # --- not reported: compatible statements
    ("paraphrase", ("a.md", "A", "PostgreSQL supports JSONB."), ("b.md", "B", "JSONB is available in PostgreSQL."), "none", None),
    ("multi-valued: supports", ("a.md", "A", "PostgreSQL supports JSONB."), ("b.md", "B", "PostgreSQL supports Indexing."), "none", None),
    ("multi-valued: uses", ("a.md", "A", "Ledger uses PostgreSQL."), ("b.md", "B", "Ledger uses Redis."), "none", None),
    ("different subjects", ("a.md", "A", "PostgreSQL has a default port of 5432."),
     ("b.md", "B", "Redis has a default port of 6379."), "none", None),
    ("indefinite article", ("a.md", "A", "Redis is a cache."), ("b.md", "B", "Memcached is a cache."), "none", None),
    ("two plans", ("a.md", "A", "We plan to make CockroachDB the database."),
     ("b.md", "B", "We plan to make Spanner the database."), "not-conflict", None),
    ("plan", ("a.md", "A", "We plan to make CockroachDB the database."), ("b.md", "B", "PostgreSQL is the database."), "not-conflict", None),
    ("stores data in two places", ("a.md", "A", "Ledger stores data in PostgreSQL."),
     ("b.md", "B", "Ledger stores data in MongoDB."), "none", None),
    ("agreement", ("a.md", "A", "PostgreSQL is the database."), ("b.md", "B", "PostgreSQL is the database."), "none", None),
]


def occurrences(spec, tag):
    path, heading, text = spec
    return [(Occurrence(claim_id(c.text), f"psg:{tag}", path, heading, c.text), tuple(c.mentions), c.text)
            for c in EX.extract(text).claims]


@pytest.mark.parametrize("name,a,b,expected,rules", CASES, ids=[c[0] for c in CASES])
def test_taxonomy(name, a, b, expected, rules):
    found = detect(occurrences(a, "a") + (occurrences(b, "b") if b else []), rules)
    kinds = {f.kind for f in found.values()}
    got = "conflict" if "conflict" in kinds else ("scoped" if "scoped" in kinds else "none")
    if expected == "not-conflict":
        assert got != "conflict", found
    else:
        assert got == expected, found


def test_a_finding_names_every_place_that_states_each_value():
    found = detect(occurrences(("database.md", "DB", "PostgreSQL supports JSONB."), "a")
                   + occurrences(("architecture.md", "Arch", "PostgreSQL does not support JSONB."), "b"))
    (f,) = found.values()
    assert f.kind == "conflict" and f.id.startswith("cfl:")
    paths = {v["value"]: [o["path"] for o in v["occurrences"]] for v in f.values}
    assert paths == {"is negated": ["architecture.md"], "is stated": ["database.md"]}


def test_the_id_is_stable_across_wording_of_the_values():
    """The id is the property's, so editing one side to agree and back keeps one identity."""
    a = detect(occurrences(("a.md", "A", "Kafka runs on three nodes."), "a") + occurrences(("b.md", "B", "Kafka runs on five nodes."), "b"))
    b = detect(occurrences(("a.md", "A", "Kafka runs on seven nodes."), "a") + occurrences(("b.md", "B", "Kafka runs on five nodes."), "b"))
    assert set(a) == set(b)


@pytest.mark.parametrize("path,token", [
    ("techstack001.md", "revision 001"),
    ("docs/stack_0002.md", "revision 0002"),
    ("docs/adr/0003-use-postgres.md", "record 0003"),
    ("oauth2.md", None),
    ("http2.md", None),
    ("chapter01.md", None),
    ("guides/001-intro.md", None),  # a numbered chapter, not a decision record
])
def test_revision_numbers_in_file_names(path, token):
    sc = scope_of("MongoDB is the database.", "Stack", path)
    revs = {t for t in sc.time if t.startswith(("revision", "record"))}
    assert revs == ({token} if token else set())


# --- per-document scope labels: [[conflicts.scope]] label = "{path}" or "{dir}"
def test_a_label_with_a_placeholder_is_expanded_per_document():
    assert SESSIONS[0].label_for("sessions/2026-03-02.md") == "sessions/2026-03-02.md"
    assert ScopeRule("teams/**", label="team {dir}").label_for("teams/payments/db.md") == "team teams/payments"
    assert ScopeRule("docs/mvp/**", label="release 1").label_for("docs/mvp/a.md") == "release 1"  # as before
    sc = scope_of(BCRYPT, "Auth", "sessions/2026-03-02.md", SESSIONS)
    assert sc.status == "past" and sc.label() == "past, 2026, sessions/2026-03-02.md"


def _finding(occ, rules):
    (f,) = detect(occ, rules).values()
    return f.kind, {v["value"]: sorted(o["path"] for o in v["occurrences"]) for v in f.values}


def test_a_note_against_a_current_document_is_reported_as_before():
    """The labels separate the notes from one another and from nothing else. A past note
    against a current document is scoped, as without the label, and every value is listed."""
    occ = occurrences(("sessions/2026-03-02.md", "Auth", BCRYPT), "a") + occurrences(("current/auth.md", "Auth", ARGON2), "c")
    expected = ("scoped", {"Argon2": ["current/auth.md"], "Bcrypt": ["sessions/2026-03-02.md"]})
    assert _finding(occ, [ScopeRule("sessions/**", status="past")]) == expected
    assert _finding(occ, SESSIONS) == expected
    # a decision, its reversal in a later note, and the current document: one scoped finding with all three
    occ += occurrences(("sessions/2026-05-14.md", "Auth", ARGON2), "b")
    assert _finding(occ, SESSIONS) == ("scoped", {"Argon2": ["current/auth.md", "sessions/2026-05-14.md"],
                                                  "Bcrypt": ["sessions/2026-03-02.md"]})


def test_current_documents_still_conflict_when_notes_state_the_property_too():
    occ = (occurrences(("current/auth.md", "Auth", ARGON2), "a") + occurrences(("current/security.md", "Sec", BCRYPT), "b")
           + occurrences(("sessions/2026-03-02.md", "Auth", BCRYPT), "c"))
    assert _finding(occ, SESSIONS) == ("conflict", {"Argon2": ["current/auth.md"], "Bcrypt": ["current/security.md"]})


def test_labels_are_checked_when_the_rules_are_read():
    from dakc.conflicts import rules_from_config

    rules, _ = rules_from_config({"scope": [{"path": "sessions/**", "status": "past", "label": "{path}"}]})
    assert rules == SESSIONS
    with pytest.raises(ValueError, match=r"unknown placeholder \{paht\}"):
        rules_from_config({"scope": [{"path": "sessions/**", "label": "{paht}"}]})
    (rule,), _ = rules_from_config({"scope": [{"path": "v/**", "label": "release {1}"}]})  # not a placeholder
    assert rule.label_for("v/a.md") == "release {1}"


def test_the_fingerprint_holds_the_label_as_written():
    """Not its expansions: adding a document under the rule must not re-judge everything."""
    from dakc.conflicts import fingerprint

    assert fingerprint(SESSIONS) == fingerprint([ScopeRule("sessions/**", status="past", label="{path}")])
    assert fingerprint(SESSIONS) != fingerprint([ScopeRule("sessions/**", status="past")])


# --- phase 2 rules: the same statement worded differently (RFC 0002, "Rules: wording")
WORDING = [
    ("unavailable in", "PostgreSQL supports JSONB.", "JSONB is unavailable in PostgreSQL.", "conflict"),
    ("lacks", "PostgreSQL supports JSONB.", "PostgreSQL lacks JSONB.", "conflict"),
    ("has no support for", "PostgreSQL supports JSONB.", "PostgreSQL has no support for JSONB.", "conflict"),
    ("passive", "PostgreSQL supports JSONB.", "JSONB isn't supported by PostgreSQL.", "conflict"),
    ("dropped support", "Ledger supports Webhooks.", "Ledger dropped support for Webhooks.", "conflict"),
    ("compatible / works on", "Billing is compatible with Python 3.8.", "Billing does not work on Python 3.8.", "conflict"),
    ("enabled / disabled", "Caching is enabled by default in Gateway.", "Caching is disabled by default in Gateway.", "conflict"),
    ("required / optional", "The Region field is required in Orders.", "The Region field is optional in Orders.", "conflict"),
    ("sync / async", "The Ledger API is synchronous.", "The Ledger API is asynchronous.", "conflict"),
    ("stateless / stateful", "The Auth service is stateless.", "The Auth service is stateful.", "conflict"),
    ("public / private", "The Billing endpoint is public.", "The Billing endpoint is private.", "conflict"),
    ("datastore = database", "The datastore is MongoDB.", "The database is PostgreSQL.", "conflict"),
    ("Postgres = PostgreSQL", "Postgres supports JSONB.", "PostgreSQL does not support JSONB.", "conflict"),
    ("plural -ies", "The Billing service retries failed Stripe webhooks.",
     "The Billing service does not retry failed Stripe webhooks.", "conflict"),
    # compatible: the same rules must not invent a disagreement
    ("availability agrees", "PostgreSQL supports JSONB.", "JSONB is available in PostgreSQL.", "none"),
    ("double negation agrees", "TLS is not optional for Gateway.", "TLS is required for Gateway.", "none"),
    ("an adjective before a noun is not a predicate", "Billing has a private key.", "Billing has a public key.", "none"),
    ("different accounts", "Webhooks are disabled for Free accounts.", "Webhooks are enabled for Enterprise accounts.", "none"),
]


@pytest.mark.parametrize("name,a,b,expected", WORDING, ids=[w[0] for w in WORDING])
def test_wording(name, a, b, expected):
    found = detect(occurrences(("a.md", "A", a), "a") + occurrences(("b.md", "B", b), "b"))
    kinds = {f.kind for f in found.values()}
    assert ("conflict" if "conflict" in kinds else "none") == expected, found


def test_workspace_aliases_join_names():
    from dakc.hashing import entity_key

    occ = occurrences(("a.md", "A", "Pg supports JSONB."), "a") + occurrences(("b.md", "B", "PostgreSQL does not support JSONB."), "b")
    assert not any(f.kind == "conflict" for f in detect(occ).values())
    (f,) = detect(occ, aliases={entity_key("Pg"): "PostgreSQL"}).values()
    assert f.kind == "conflict"


# --- the verifier's pure parts (RFC 0002, phase 2)
def test_verdicts_are_parsed_leniently():
    from dakc.conflicts.verifier import parse_verdicts

    text = '```json\n{"verdicts": [{"pair": 2, "verdict": "Contradiction", "reason": "one says yes"}, {"pair": 9, "verdict": "compatible"}]}\n```'
    assert parse_verdicts(text, 3) == [("", ""), ("contradiction", "one says yes"), ("", "")]
    assert parse_verdicts("not json", 2) == [("", ""), ("", "")]


def test_candidates_skip_the_same_document_and_pairs_the_rules_compare():
    from dakc.conflicts.verifier import ClaimInfo, candidates, words

    def ci(text, path, keys=()):
        return ClaimInfo(text, frozenset({path}), ("ent:ledger",), words(text), frozenset(keys))

    info = {
        "c1": ci("Ledger is written in Go.", "a.md"),
        "c2": ci("Ledger is a Python service.", "b.md"),
        "c3": ci("Ledger is deployed with Helm.", "a.md"),  # same document as c1
        "c4": ci("Ledger is written in Go.", "c.md", ["neg|ledger is written in go"]),
        "c5": ci("Ledger is written in Rust.", "d.md"),
    }
    info["c1"].keys = frozenset({"neg|ledger is written in go"})
    members = {"ent:ledger": list(info)}
    pairs = candidates({"c1"}, info, members, per_entity=5)
    assert [(a, b) for _, a, b in pairs] == [("c1", "c5"), ("c1", "c2")]  # most similar first
    assert [(a, b) for _, a, b in candidates({"c1"}, info, members, per_entity=1)] == [("c1", "c5")]


def test_settings():
    from dakc.conflicts.verifier import Settings

    assert Settings.from_config({}) is None and Settings.from_config({"verifier": "none"}) is None
    s = Settings.from_config({"verifier": "llm", "max_checks": 50})
    assert s is not None and s.max_checks == 50 and s.per_entity == 5
    with pytest.raises(ValueError):
        Settings.from_config({"verifier": "nli"})


@pytest.mark.parametrize("sentence,label", [
    ("TLS termination has been removed from Gateway.", "current"),  # "Ga…" is not the GA release marker
    ("Requests from Gateway to Ledger time out after 30 seconds.", "current"),
    ("Dynamic credentials from Vault are not used by the Orders service.", "current"),  # "used by" is present tense
    ("Billing accepts SOAP requests from legacy clients.", "current"),  # legacy clients, not a legacy statement
    ("In GA, Ledger is the default.", "ga"),
    ("Kafka was the broker.", "past"),
])
def test_scope_words_inside_other_words_and_phrases(sentence, label):
    assert scope_of(sentence, "Overview", "b.md").label() == label


@pytest.mark.parametrize("sentence,label", [
    ("Catalog runs on Python 3.11.", "current"),  # the version is what the sentence states
    ("Billing is compatible with Python 3.8.", "current"),
    ("All Atlas infrastructure lives in the eu-west-1 region of AWS.", "current"),  # so is the region
    ("Since 3.11, Catalog is faster.", "3.11"),  # a word of time: a scope
    ("In eu-west-1, Ledger runs three replicas.", "eu-west-1"),  # at the start: a scope
    ("Kafka runs on five nodes in production.", "production"),  # an environment is always a scope
    ("Pydantic models behave differently in Pydantic v2.", "v2"),
])
def test_versions_and_regions_after_the_verb_are_values(sentence, label):
    assert scope_of(sentence, "Overview", "b.md").label() == label


def test_two_runtimes_are_a_conflict_not_two_releases():
    found = detect(occurrences(("a.md", "A", "Catalog runs on Python 3.11."), "a")
                   + occurrences(("b.md", "B", "Catalog does not run on Python 3.11."), "b"))
    assert [f.kind for f in found.values()] == ["conflict"]


@pytest.mark.parametrize("sentence,heading,label", [
    ("The v1 Orders API was deprecated.", "Overview", "current"),  # the deprecation still holds
    ("Gateway is deprecated.", "Overview", "current"),
    ("The Billing API has been removed.", "Overview", "current"),
    ("MongoDB was the database.", "Overview", "past"),  # a state that no longer holds
    ("Gateway accepts XML.", "Deprecated APIs", "past"),  # a heading still marks its section
    ("Each tenant may send up to 500 requests per second to the Ledger API.", "Overview", "current"),  # permission
    ("Clients may not exceed 100 connections.", "Overview", "current"),
    ("Ledger may move to CockroachDB.", "Overview", "proposed"),  # possibility
    ("The Reporting service used to read from the Ledger database.", "Overview", "past"),
    ("Redis is used to store sessions.", "Overview", "current"),
])
def test_tense_and_modality(sentence, heading, label):
    assert scope_of(sentence, heading, "b.md").label() == label
