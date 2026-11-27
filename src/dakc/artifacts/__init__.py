"""Artifact records.

Every derived artifact is an immutable record with:

* ``content_hash``: hash of its *knowledge content* (what a reader sees);
* ``state_hash``: hash of its full payload, including provenance. A new
  version row is written iff this changes;
* ``deps()``: the ids it depends on, with an edge kind. Edges are derived from
  content, never maintained by hand, so they cannot drift from it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property
from typing import Any, ClassVar

from dakc.hashing import h_obj

LAYER: dict[str, int] = {
    "source": 0,
    "passage": 1,
    "claim": 2,
    "entity": 3,
    "relationship": 3,
    "page": 4,
    "summary": 5,
    "index": 6,
}
KINDS = tuple(LAYER)
INDEX_ID = "index:main"


@dataclass(frozen=True)
class Artifact:
    id: str
    kind: ClassVar[str] = ""

    def content(self) -> dict[str, Any]:  # pragma: no cover - overridden
        raise NotImplementedError

    def payload(self) -> dict[str, Any]:  # pragma: no cover - overridden
        raise NotImplementedError

    def deps(self) -> list[tuple[str, str]]:  # (dependency id, edge kind)
        return []

    @cached_property
    def content_hash(self) -> str:
        return h_obj(self.content())

    @cached_property
    def state_hash(self) -> str:
        return h_obj(self.payload())

    @property
    def title(self) -> str:
        return self.id


@dataclass(frozen=True)
class PassageArt(Artifact):
    source_id: str = ""
    text: str = ""
    text_hash: str = ""
    heading: str = ""
    occurrence: int = 0
    extracted_by: str = ""  # fingerprint of the extractor whose claims this passage supports
    # Per sentence (split_sentences(text)), the bodies of the HTML comments written after it
    # in the source; empty when there are none. Metadata only: it is outside the id, the
    # text hash and the extraction cache key, and nothing downstream reads it, so a
    # comment-only edit writes a new passage version and nothing else (trace reads it).
    refs: tuple[tuple[str, ...], ...] = ()
    kind: ClassVar[str] = "passage"

    def content(self):
        return {"text": self.text}

    def payload(self):
        return {"source_id": self.source_id, "text": self.text, "text_hash": self.text_hash, "heading": self.heading, "occurrence": self.occurrence, "extracted_by": self.extracted_by,
                **({"refs": [list(r) for r in self.refs]} if self.refs else {})}

    def sentence_refs(self, i: int) -> tuple[str, ...]:
        """The comment bodies written after sentence ``i``."""
        return self.refs[i] if 0 <= i < len(self.refs) else ()

    def deps(self):
        return [(self.source_id, "from_source")]


@dataclass(frozen=True)
class ClaimArt(Artifact):
    # (passage id, surface text, mentions, evidence sentence indices in that passage), sorted by passage id
    supports: tuple[tuple[str, str, tuple[str, ...], tuple[int, ...]], ...] = ()
    kind: ClassVar[str] = "claim"

    @cached_property
    def text(self) -> str:
        return min(s[1] for s in self.supports) if self.supports else ""

    @cached_property
    def mentions(self) -> tuple[str, ...]:
        return tuple(sorted({m for s in self.supports for m in s[2]}))

    @property
    def passage_ids(self) -> tuple[str, ...]:
        return tuple(s[0] for s in self.supports)

    def content(self):
        return {"text": self.text, "mentions": list(self.mentions)}

    def payload(self):
        return {"supports": [[p, t, list(m), list(e)] for p, t, m, e in self.supports]}

    def deps(self):
        return [(p, "supported_by") for p in self.passage_ids]

    @property
    def title(self) -> str:
        return self.text


@dataclass(frozen=True)
class EntityArt(Artifact):
    name: str = ""
    claim_ids: tuple[str, ...] = ()
    kind: ClassVar[str] = "entity"

    def content(self):
        return {"name": self.name, "claim_ids": list(self.claim_ids)}

    payload = content

    def deps(self):
        return [(c, "mentioned_in") for c in self.claim_ids]

    @property
    def title(self) -> str:
        return self.name


@dataclass(frozen=True)
class RelationshipArt(Artifact):
    a: str = ""
    b: str = ""
    claim_ids: tuple[str, ...] = ()
    kind: ClassVar[str] = "relationship"

    def content(self):
        return {"a": self.a, "b": self.b, "claim_ids": list(self.claim_ids)}

    payload = content

    def deps(self):
        return [(c, "evidenced_by") for c in self.claim_ids]


@dataclass(frozen=True)
class PageArt(Artifact):
    entity_id: str = ""
    title_: str = ""
    body: str = ""
    lead: str = ""
    rendered: str = ""
    claim_ids: tuple[str, ...] = ()
    rel_ids: tuple[str, ...] = ()
    topic: str = ""
    gen_sig: str = ""
    render_sig: str = ""
    generator: str = ""
    dropped: tuple[str, ...] = ()  # generated sentences removed by strict provenance (audit trail)
    # Hub pages only: (signature, label, text with section-local citations, claim ids) of
    # every section, in order. Kept so unchanged sections are reused instead of rewritten.
    sections: tuple[tuple[str, str, str, tuple[str, ...]], ...] = ()
    # The claims listed under History, the tail of claim_ids: every document supporting them
    # is declared past by a [[conflicts.scope]] rule. Absent from the payload when empty.
    history: tuple[str, ...] = ()
    kind: ClassVar[str] = "page"

    def content(self):
        return {"rendered": self.rendered}

    def payload(self):
        return {
            "entity_id": self.entity_id,
            "title": self.title_,
            "body": self.body,
            "lead": self.lead,
            "rendered": self.rendered,
            "claim_ids": list(self.claim_ids),
            "rel_ids": list(self.rel_ids),
            "topic": self.topic,
            "gen_sig": self.gen_sig,
            "render_sig": self.render_sig,
            "generator": self.generator,
            "dropped": list(self.dropped),
            **({"sections": [[s[0], s[1], s[2], list(s[3])] for s in self.sections]} if self.sections else {}),
            **({"history": list(self.history)} if self.history else {}),
        }

    def deps(self):
        return [(self.entity_id, "describes")] + [(r, "related_via") for r in self.rel_ids]

    @property
    def title(self) -> str:
        return self.title_


@dataclass(frozen=True)
class SummaryArt(Artifact):
    topic: str = ""
    title_: str = ""
    rendered: str = ""
    page_ids: tuple[str, ...] = ()
    sig: str = ""
    generator: str = ""
    body: str = ""
    render_sig: str = ""
    # Hierarchical summaries only: (signature, title, overview) of every section, all
    # levels, in order. Kept so unchanged sections are reused instead of regenerated.
    sections: tuple[tuple[str, str, str], ...] = ()
    kind: ClassVar[str] = "summary"

    def content(self):
        return {"rendered": self.rendered}

    def payload(self):
        return {
            "topic": self.topic,
            "title": self.title_,
            "rendered": self.rendered,
            "page_ids": list(self.page_ids),
            "sig": self.sig,
            "generator": self.generator,
            "body": self.body,
            "render_sig": self.render_sig,
            **({"sections": [list(s) for s in self.sections]} if self.sections else {}),
        }

    def deps(self):
        return [(p, "summarizes") for p in self.page_ids]

    @property
    def title(self) -> str:
        return self.title_


@dataclass(frozen=True)
class IndexArt(Artifact):
    rendered: str = ""
    page_ids: tuple[str, ...] = ()
    summary_ids: tuple[str, ...] = ()
    sig: str = ""
    kind: ClassVar[str] = "index"

    def content(self):
        return {"rendered": self.rendered}

    def payload(self):
        return {"rendered": self.rendered, "page_ids": list(self.page_ids), "summary_ids": list(self.summary_ids), "sig": self.sig}

    def deps(self):
        return [(p, "lists") for p in self.page_ids] + [(s, "lists") for s in self.summary_ids]

    @property
    def title(self) -> str:
        return "Index"


def from_payload(kind: str, id_: str, p: dict[str, Any]) -> Artifact:
    if kind == "passage":
        return PassageArt(id_, p["source_id"], p["text"], p["text_hash"], p.get("heading", ""), p.get("occurrence", 0), p.get("extracted_by", ""),
                          tuple(tuple(r) for r in p.get("refs", ())))
    if kind == "claim":
        return ClaimArt(id_, tuple((s[0], s[1], tuple(s[2]), tuple(s[3]) if len(s) > 3 else ()) for s in p["supports"]))
    if kind == "entity":
        return EntityArt(id_, p["name"], tuple(p["claim_ids"]))
    if kind == "relationship":
        return RelationshipArt(id_, p["a"], p["b"], tuple(p["claim_ids"]))
    if kind == "page":
        return PageArt(
            id_, p["entity_id"], p["title"], p["body"], p["lead"], p["rendered"], tuple(p["claim_ids"]),
            tuple(p["rel_ids"]), p["topic"], p["gen_sig"], p["render_sig"], p.get("generator", ""), tuple(p.get("dropped", ())),
            tuple((s[0], s[1], s[2], tuple(s[3])) for s in p.get("sections", ())), tuple(p.get("history", ())),
        )
    if kind == "summary":
        return SummaryArt(id_, p["topic"], p["title"], p["rendered"], tuple(p["page_ids"]), p["sig"], p.get("generator", ""),
                          p.get("body", ""), p.get("render_sig", ""), tuple(tuple(s) for s in p.get("sections", ())))
    if kind == "index":
        return IndexArt(id_, p["rendered"], tuple(p["page_ids"]), tuple(p["summary_ids"]), p["sig"])
    raise KeyError(kind)


def kind_of(artifact_id: str) -> str:
    prefix = artifact_id.split(":", 1)[0]
    return {
        "src": "source",
        "psg": "passage",
        "clm": "claim",
        "ent": "entity",
        "rel": "relationship",
        "page": "page",
        "sum": "summary",
        "index": "index",
    }.get(prefix, "unknown")


@dataclass
class Usage:
    """Provider usage counters for one build."""

    calls: int = 0
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_hits: int = 0
    by_stage: dict[str, int] = field(default_factory=dict)

    def add(self, stage: str, *, llm: bool, input_tokens: int = 0, output_tokens: int = 0, calls: int = 1) -> None:
        self.calls += calls
        if llm:
            self.llm_calls += calls
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.by_stage[stage] = self.by_stage.get(stage, 0) + 1
        if calls > 1:
            self.by_stage[f"{stage}_retry"] = self.by_stage.get(f"{stage}_retry", 0) + calls - 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider_calls": self.calls,
            "llm_calls": self.llm_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "extraction_cache_hits": self.cache_hits,
            "calls_by_stage": dict(sorted(self.by_stage.items())),
        }
