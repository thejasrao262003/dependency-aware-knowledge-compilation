import json

import pytest

from dakc.hashing import canonical_json, claim_id, entity_key, h, normalize_claim, normalize_text, slugify
from dakc.parsing import ParseError, parse_bytes, source_type_for


def test_normalization_is_stable_and_insensitive_to_cosmetics():
    assert normalize_text("  PostgreSQL  supports\n“JSONB” ") == 'PostgreSQL supports "JSONB"'
    assert normalize_claim("PostgreSQL supports JSONB.") == normalize_claim("postgresql   SUPPORTS jsonb")
    assert claim_id("PostgreSQL supports JSONB.") == claim_id("PostgreSQL supports JSONB")
    assert claim_id("PostgreSQL 15 supports JSONB") != claim_id("PostgreSQL 16 supports JSONB")


def test_entity_key_and_slug():
    assert entity_key("PostgreSQL") == entity_key("postgre-sql") == "postgresql"
    assert entity_key("C++") == "c"
    assert entity_key("***").startswith("x")
    assert slugify("topic-01/Doc A.md") == "topic-01-doc-a-md"


def test_canonical_json_is_order_independent():
    assert canonical_json({"b": 1, "a": [1, {"d": 2, "c": 3}]}) == canonical_json({"a": [1, {"c": 3, "d": 2}], "b": 1})
    assert h("x") == h(b"x") and len(h("x")) == 64


def test_markdown_blocks_frontmatter_headings_lists_and_code():
    md = b"""---
title: ignored
---
# Title

Intro paragraph with **bold** and `code`.

## Section

- First item mentions Redis.
- Second item

```
code block is ignored
```
"""
    doc = parse_bytes(md, "markdown")
    kinds = [(b.kind, b.text) for b in doc.blocks]
    assert ("heading", "Title") in kinds
    assert ("paragraph", "Intro paragraph with bold and code.") in kinds
    assert ("list_item", "First item mentions Redis.") in kinds
    assert not any("ignored" in t for _, t in kinds)
    item = next(b for b in doc.blocks if b.text.startswith("First"))
    assert item.heading == "Title > Section"


def test_text_hash_ignores_formatting_only_changes():
    a = parse_bytes(b"# T\n\nRedis is fast.\n", "markdown")
    b = parse_bytes(b"# T\n\n\nRedis   is fast.\n\n", "markdown")
    assert a.text_hash == b.text_hash


def test_html_json_text():
    html = b"<html><head><style>x{}</style></head><body><h1>Kafka</h1><p>Kafka streams events.</p><ul><li><p>Kafka uses Zookeeper.</p></li></ul><script>bad()</script></body></html>"
    doc = parse_bytes(html, "html")
    texts = [b.text for b in doc.body_blocks]
    assert texts == ["Kafka streams events.", "Kafka uses Zookeeper."]
    j = parse_bytes(json.dumps({"service": {"name": "Billing", "db": "PostgreSQL"}, "tags": ["a"]}).encode(), "json")
    assert [b.text for b in j.blocks] == ["service.db: PostgreSQL", "service.name: Billing", "tags[0]: a"]
    t = parse_bytes(b"One para.\n\nTwo para.\n", "text")
    assert [b.text for b in t.blocks] == ["One para.", "Two para."]
    with pytest.raises(ParseError):
        parse_bytes(b"{not json", "json")


def test_pdf(tmp_path):
    pymupdf = pytest.importorskip("pymupdf")
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "PostgreSQL supports JSONB.")
    data = doc.tobytes()
    parsed = parse_bytes(data, "pdf")
    assert any("PostgreSQL supports JSONB." in b.text for b in parsed.blocks)


def test_source_types(tmp_path):
    assert source_type_for(tmp_path / "a.MD") == "markdown"
    assert source_type_for(tmp_path / "a.htm") == "html"
    assert source_type_for(tmp_path / "a.png") is None


def test_markdown_table_rows_keep_their_column_headers():
    from dakc.parsing import parse_bytes

    md = (
        "# Grid\n\nIntro.\n\n"
        "| | llm_wiki | OpenKB |\n|---|---|---|\n"
        "| Update path | full re-ingest | **additive** |\n"
        "| Cutoff | none | |\n\n"
        "| Capability | Status |\n|---|---|\n| Hashing | file-level |\n"
    )
    blocks = parse_bytes(md.encode(), "markdown").blocks
    rows = [b for b in blocks if b.kind == "table_row"]
    assert [b.text for b in rows] == [
        "Update path - llm_wiki: full re-ingest; OpenKB: additive.",  # unlabelled first column = row label
        "Cutoff - llm_wiki: none.",  # empty cells are skipped
        "Capability: Hashing; Status: file-level.",
    ]
    assert all(b.heading == "Grid" for b in rows)
    assert not any("|" in b.text for b in blocks)


# A session note as Reckon writes it: YAML front matter, then sentences that end with
# HTML-comment receipts (a turn reference, or a source reference with its turn).
RECKON_NOTE = b"""---
session: 3f9a12c4
date: 2026-03-01
tags: [auth, redis]
---
# Session 3

- The refresh endpoint takes a per-user Redis lock with a 5-second TTL. <!-- t:3f9a12c4 -->
- Use Argon2 for password hashing. <!-- rs_01JDQ8X4Z7K3M9P2Q5R8S1T4V6#t:3f9a12c4 -->

Redis holds sessions. <!-- t:aa11 --> Postgres holds users. <!-- t:bb22 -->
"""


def _without_comments_or_front_matter(md: bytes) -> bytes:
    import re

    return re.sub(rb" ?<!--.*?-->", b"", md.split(b"---\n", 2)[2])


def test_turn_reference_comments_and_front_matter_never_reach_passages_or_claims():
    from dakc.claims import heuristic_extract, segment

    doc = parse_bytes(RECKON_NOTE, "markdown")
    passages = segment("src:s", doc)
    assert [p.text for p in passages] == [
        "The refresh endpoint takes a per-user Redis lock with a 5-second TTL.",
        "Use Argon2 for password hashing.",
        "Redis holds sessions. Postgres holds users.",
    ]
    claims = [c for p in passages for c in heuristic_extract(p.text)]
    assert claims
    leaked = ("<!--", "-->", "3f9a12c4", "rs_01", "aa11", "session:", "date:", "tags:", "2026-03-01")
    for text in [b.text for b in doc.blocks] + [c.text for c in claims] + [m for c in claims for m in c.mentions]:
        assert not any(x in text for x in leaked), text
    # The comments and the front matter change no text: same blocks, same hashes, as the plain document.
    plain = parse_bytes(_without_comments_or_front_matter(RECKON_NOTE), "markdown")
    assert [(b.kind, b.text, b.heading) for b in plain.blocks] == [(b.kind, b.text, b.heading) for b in doc.blocks]
    assert plain.text_hash == doc.text_hash
    assert parse_bytes(RECKON_NOTE.replace(b"\n", b"\r\n"), "markdown").text_hash == doc.text_hash


def test_trailing_comments_are_kept_as_refs_of_the_sentence_they_end():
    from dakc.claims import segment
    from dakc.parsing import split_sentences

    doc = parse_bytes(RECKON_NOTE, "markdown")
    assert [b.refs for b in doc.body_blocks] == [
        (("t:3f9a12c4",),),
        (("rs_01JDQ8X4Z7K3M9P2Q5R8S1T4V6#t:3f9a12c4",),),
        (("t:aa11",), ("t:bb22",)),
    ]
    for b in doc.body_blocks:  # one entry per sentence, in sentence order
        assert len(b.refs) == len(split_sentences(b.text))
    passages = segment("src:s", doc)
    assert [p.refs for p in passages] == [b.refs for b in doc.body_blocks]
    # refs are metadata: the passage ids are those of the same text without comments
    plain = segment("src:s", parse_bytes(_without_comments_or_front_matter(RECKON_NOTE), "markdown"))
    assert [p.id for p in plain] == [p.id for p in passages] and all(p.refs == () for p in plain)


def test_comment_placement_rules():
    def refs(md: str):
        (block,) = parse_bytes(md.encode(), "markdown").body_blocks
        return block.text, block.refs

    # several comments after one sentence, and a sentence without any
    assert refs("Redis holds sessions. <!-- t:1 --><!-- t:2 --> Redis holds locks. Kafka streams events. <!-- t:3 -->\n") == (
        "Redis holds sessions. Redis holds locks. Kafka streams events.", (("t:1", "t:2"), (), ("t:3",)))
    # a soft line break between sentences; a comment glued to the full stop
    assert refs("Redis holds sessions.<!-- t:1 -->\nKafka streams events. <!-- t:2 -->\n")[1] == (("t:1",), ("t:2",))
    # inside a sentence it belongs to that sentence
    assert refs("Redis holds <!-- t:1 --> sessions. Kafka streams events.\n") == (
        "Redis holds sessions. Kafka streams events.", (("t:1",), ()))
    # comment bodies are trimmed; empty comments and other inline HTML are dropped
    assert refs("Redis holds sessions. <!--   t:1\n  --> <!-- --> <br>\n") == ("Redis holds sessions.", (("t:1",),))
    # a block without comments has no refs
    assert refs("Redis holds sessions.\n")[1] == ()


def test_comments_outside_sentences_are_dropped():
    md = (b"# Auth <!-- t:h -->\n\n<!-- a standalone comment -->\n\n"
          b"| Store | Holds |\n|---|---|\n| Redis <!-- t:c --> | sessions |\n\n- <!-- t:only -->\n\n"
          b"<!-- t:x --> A line that starts with a comment is an HTML block, whose text is not read (as before).\n")
    doc = parse_bytes(md, "markdown")
    assert [(b.kind, b.text, b.refs) for b in doc.blocks] == [
        ("heading", "Auth", ()),
        ("table_row", "Store: Redis; Holds: sessions.", ()),  # table cells keep their text, as before, without refs
    ]
