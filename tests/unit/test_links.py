"""Reading, resolving and judging links between documents (no store)."""

from __future__ import annotations

import pytest

from dakc.links import DocLinks, Link, anchor_renames, check, extract, heading_anchors, resolve


@pytest.mark.parametrize("heading,anchors", [
    ("Redis setup", ("redis-setup",)),
    ("Install `pwdlib` { #install-pwdlib }", ("install-pwdlib",)),  # an explicit id replaces the slug
    ("What's new in 2.0?", ("whats-new-in-20",)),
    ("Form  -  Data", ("form-----data", "form-data")),  # GitHub: a hyphen per space, the dash kept; MkDocs collapses them
    ("Überblick", ("überblick", "uberblick")),
])
def test_heading_anchors(heading, anchors):
    assert heading_anchors(heading) == anchors


def test_markdown_links_lines_and_anchors():
    md = (b"---\ntitle: x\n---\n# Caching\n\n## Redis setup\n\n"
          b"See [the gateway](gateway.md#limits) and\n[home](../index.md).\n\n"
          b"```\n[not a link](inside.md)\n```\n\n## Redis setup\n\n<a id=\"legacy\"></a> [ext](https://x.y)\n")
    d = extract(md, "markdown")
    assert [(link.line, link.text, link.href) for link in d.links] == [
        (8, "the gateway", "gateway.md#limits"), (9, "home", "../index.md"), (17, "ext", "https://x.y")]
    assert {"caching", "redis-setup", "redis-setup-1", "redis-setup_1", "legacy"} <= set(d.anchors)


def test_html_links_and_ids():
    d = extract(b'<h2 id="setup">Setup</h2>\n<p>See <a href="other.html#x">the other page</a>.</p>', "html")
    assert d.links == [Link(2, "the other page", "other.html#x")] and "setup" in d.anchors


@pytest.mark.parametrize("src,href,expected", [
    ("a/b.md", "c.md#x", (["a/c.md"], "x", True)),
    ("a/b.md", "#local", (["a/b.md"], "local", True)),
    ("a/b.md", "../index.md", (["index.md"], "", True)),
    ("a/b.md", "c.html#y", (["a/c.html", "a/c.md"], "y", True)),  # a built page: its Markdown source counts
    ("a/b.md", "https://example.com/x.md", None),
    ("a/b.md", "mailto:me@example.com", None),
    ("a/b.md", "../../other-repo/x.md", None),  # outside the knowledge base
    ("a/b.md", "img/diagram.png", None),  # not a document
])
def test_resolve(src, href, expected):
    assert resolve(src, href) == expected


def test_directory_links_resolve_both_ways_and_are_never_reported_when_unresolved():
    cands, anchor, explicit = resolve("tutorial/body.md", "../first-steps/#run")
    assert "first-steps.md" in cands and "tutorial/first-steps.md" in cands and anchor == "run" and not explicit
    docs = {"s": DocLinks([Link(1, "x", "../nowhere/")])}
    assert check(docs, {"s": "tutorial/body.md"}) == {}


def test_check_reports_missing_documents_and_anchors_with_stable_ids():
    docs = {"a": DocLinks([Link(3, "setup", "b.md#redis-setup"), Link(4, "gone", "c.md"), Link(5, "ok", "b.md#Eviction"),
                           Link(9, "setup again", "b.md#redis-setup")]),
            "b": DocLinks([], ["eviction"])}
    found = check(docs, {"a": "a.md", "b": "b.md"})
    kinds = sorted((b.line, b.kind) for b in found.values())
    assert kinds == [(3, "missing_anchor"), (4, "missing_document"), (9, "missing_anchor")]  # anchors match case-insensitively
    moved_lines = {"a": DocLinks([Link(30, "setup", "b.md#redis-setup"), Link(4, "gone", "c.md"),
                                  Link(5, "ok", "b.md#Eviction"), Link(90, "setup again", "b.md#redis-setup")]), "b": docs["b"]}
    assert set(check(moved_lines, {"a": "a.md", "b": "b.md"})) == set(found)  # ids do not depend on line numbers


def test_suggestions_for_a_moved_document_and_a_renamed_heading():
    docs = {"a": DocLinks([Link(1, "x", "old/b.md#redis-setup")]), "b": DocLinks([], ["rate-limit-store"])}
    rename = anchor_renames({"p1": ["Caching > Redis setup", "Caching > Rate-limit store"]}, {"p1": "Caching > Rate-limit store"})
    (b,) = check(docs, {"a": "a.md", "b": "new/b.md"}, {"old/b.md": "new/b.md"}, lambda sid, anchor: rename(anchor)).values()
    assert b.kind == "missing_document" and b.suggestion == "new/b.md#rate-limit-store"
    docs["a"] = DocLinks([Link(1, "x", "new/b.md#redis-setup")])
    (b,) = check(docs, {"a": "a.md", "b": "new/b.md"}, {}, lambda sid, anchor: rename(anchor)).values()
    assert b.kind == "missing_anchor" and b.suggestion == "new/b.md#rate-limit-store"
    assert '"Redis setup" is now "Rate-limit store"' in b.reason


def test_a_renamed_heading_with_only_subsections_under_it():
    rename = anchor_renames({"p": ["Models > About dumps > Details"]}, {"p": "Models > About serialization > Details"})
    assert rename("about-dumps") == ("About dumps", "About serialization", "about-serialization")


def test_a_directory_link_to_a_document_that_moved_is_reported():
    docs = {"a": DocLinks([Link(1, "x", "tutorial/#install")]), "b": DocLinks([], ["install"])}
    (b,) = check(docs, {"a": "index.md", "b": "moved/tutorial/index.md"}, {"tutorial/index.md": "moved/tutorial/index.md"}).values()
    assert b.kind == "missing_document" and b.suggestion == "moved/tutorial/index.md#install"
