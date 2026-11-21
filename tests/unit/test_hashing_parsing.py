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
