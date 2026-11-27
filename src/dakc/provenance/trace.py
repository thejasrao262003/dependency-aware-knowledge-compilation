"""Sentence-level provenance: from a generated page sentence to the source text.

For a page, every body sentence is mapped to the claims it cites (``[^Cn]``).
Every claim is mapped to each supporting passage, that passage's source path
and source version (content hash and the build that registered it), and the
exact evidence sentences inside the passage, each with the bodies of the HTML
comments written after it in the source (``refs``).
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field

from dakc.artifacts import ClaimArt, PageArt
from dakc.claims import split_sentences
from dakc.storage import Store

CITATION = re.compile(r"\[\^C(\d+)\]")


@dataclass
class Evidence:
    passage_id: str
    source_id: str
    source_path: str
    source_raw_hash: str
    source_version_build: int | None
    heading: str
    sentences: list[str]
    # refs[i]: the bodies of the HTML comments written after sentences[i] in the source
    # ("<!-- t:3f9a12c4 -->" -> "t:3f9a12c4"); [] when there are none.
    refs: list[list[str]] = field(default_factory=list)


@dataclass
class ClaimTrace:
    ref: str
    claim_id: str
    text: str
    evidence: list[Evidence] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SentenceTrace:
    sentence: str
    cites: list[str]  # claim refs, e.g. ["C1", "C3"]
    invalid: list[str]  # refs that point to no claim of this page


@dataclass
class PageTrace:
    page_id: str
    title: str
    claims: list[ClaimTrace]
    sentences: list[SentenceTrace]
    dropped: list[str] = field(default_factory=list)  # removed by strict provenance (not shown to readers)

    @property
    def uncited(self) -> list[str]:
        return [s.sentence for s in self.sentences if not s.cites]

    @property
    def invalid_markers(self) -> list[str]:
        return [ref for s in self.sentences for ref in s.invalid]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["uncited_sentences"] = self.uncited
        d["invalid_markers"] = self.invalid_markers
        return d


def _load(store: Store, artifact_id: str):
    cur = store.current_artifact(artifact_id)
    return cur[0] if cur else None


def body_sentences(body: str) -> list[str]:
    out: list[str] = []
    for block in body.split("\n"):
        block = block.strip()
        if not block or block.startswith("#"):
            continue
        block = re.sub(r"^[-*]\s+|^\d+\.\s+", "", block)
        # "Fact one. [^C1] Fact two." -> keep each marker with the sentence it follows
        block = re.sub(r"([.!?])((?:\s*\[\^C\d+\])+)", r"\2\1", block)
        out.extend(x for x in split_sentences(block) if CITATION.sub("", x).strip(" .!?"))
    return out


def ground_body(body: str, n_claims: int) -> tuple[str, list[str]]:
    """Strict provenance: keep headings and only the sentences that cite at least
    one of the page's claims (``[^C1]`` .. ``[^Cn]``). Returns (body, dropped sentences)."""
    valid = {f"C{i + 1}" for i in range(n_claims)}
    kept_blocks: list[str] = []
    dropped: list[str] = []
    for block in body.split("\n\n"):
        lines_out = []
        for line in block.split("\n"):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                lines_out.append(line)
                continue
            prefix = re.match(r"^\s*(?:[-*]|\d+\.)\s+", line)
            text = line[prefix.end():] if prefix else stripped
            text = re.sub(r"([.!?])((?:\s*\[\^C\d+\])+)", r"\2\1", text)
            keep = []
            for sent in split_sentences(text):
                if any(f"C{n}" in valid for n in CITATION.findall(sent)):
                    keep.append(re.sub(r"((?:\s*\[\^C\d+\])+)([.!?])$", r"\2\1", sent))
                elif CITATION.sub("", sent).strip(" .!?"):
                    dropped.append(CITATION.sub("", sent).strip())
            if keep:
                lines_out.append((prefix.group(0) if prefix else "") + " ".join(keep))
        if any(x.strip() and not x.strip().startswith("#") for x in lines_out):
            kept_blocks.append("\n".join(lines_out))
        elif any(x.strip().startswith("#") for x in lines_out):
            kept_blocks.append("\n".join(x for x in lines_out if x.strip()))
    # drop headings left without content beneath them
    out: list[str] = []
    for i, b in enumerate(kept_blocks):
        if b.lstrip().startswith("#") and "\n" not in b.strip() and (i + 1 == len(kept_blocks) or kept_blocks[i + 1].lstrip().startswith("#")):
            continue
        out.append(b)
    return "\n\n".join(out), dropped


def trace_claim(store: Store, claim: ClaimArt, ref: str = "") -> ClaimTrace:
    ct = ClaimTrace(ref, claim.id, claim.text)
    for pid, _surface, _mentions, ev in claim.supports:
        passage = _load(store, pid)
        if passage is None:
            continue
        row = store.conn.execute(
            "SELECT path, raw_hash, from_build FROM sources WHERE id=? AND to_build IS NULL", (passage.source_id,)
        ).fetchone()
        sents = split_sentences(passage.text)
        picked = [i for i in ev if 0 <= i < len(sents)]
        # Unknown evidence falls back to the whole passage, with all of its comments.
        refs = [list(passage.sentence_refs(i)) for i in picked] or [[r for rs in passage.refs for r in rs]]
        ct.evidence.append(Evidence(
            pid, passage.source_id, row[0] if row else "?", row[1][:16] if row else "?", row[2] if row else None,
            passage.heading, [sents[i] for i in picked] or [passage.text], refs,
        ))
    return ct


def displayed_body(page: PageArt) -> str:
    """The body text readers see: the rendered page minus title, Related and Sources sections."""
    text = page.rendered
    for marker in ("\n## Sources\n", "\n## Related\n"):
        if marker in text:
            text = text.rsplit(marker, 1)[0]
    lines = text.split("\n")
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    return "\n".join(lines).strip()


def trace_page(store: Store, page: PageArt) -> PageTrace:
    refs = {f"C{i + 1}": cid for i, cid in enumerate(page.claim_ids)}
    claims = []
    for ref, cid in refs.items():
        c = _load(store, cid)
        if c is not None:
            claims.append(trace_claim(store, c, ref))
    sentences = []
    for s in body_sentences(displayed_body(page)):
        cited = [f"C{n}" for n in CITATION.findall(s)]
        text = re.sub(r"\s+([.!?,;:])", r"\1", CITATION.sub("", s)).strip()
        sentences.append(SentenceTrace(text, [r for r in cited if r in refs], [r for r in cited if r not in refs]))
    return PageTrace(page.id, page.title, claims, sentences, list(page.dropped))


def citation_coverage(store: Store) -> dict:
    """Share of generated page sentences that cite at least one valid claim."""
    total = cited = invalid = 0
    for aid, payload in store.conn.execute("SELECT id, payload FROM artifacts WHERE kind='page' AND to_build IS NULL"):
        from dakc.artifacts import from_payload

        t = trace_page(store, from_payload("page", aid, json.loads(payload)))  # type: ignore[arg-type]
        total += len(t.sentences)
        cited += sum(1 for s in t.sentences if s.cites)
        invalid += len(t.invalid_markers)
    return {"sentences": total, "cited": cited, "coverage": round(cited / total, 4) if total else 1.0, "invalid_markers": invalid}
