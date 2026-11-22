"""Turn source files into ordered text blocks.

Supported: Markdown, plain text, HTML, JSON and PDF (PyMuPDF, optional extra).
Each parser returns blocks in document order. Headings are kept as context but
are not passages; code blocks are dropped (they are not prose claims). Markdown
table rows become one block each, with every cell labelled by its column header
("Row — Header: value; Header: value."), so a fact taken from a table still says
which column it belongs to.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from dakc.hashing import h, normalize_text

SUFFIX_TYPES = {
    ".md": "markdown",
    ".markdown": "markdown",
    ".txt": "text",
    ".text": "text",
    ".html": "html",
    ".htm": "html",
    ".json": "json",
    ".pdf": "pdf",
}


class ParseError(Exception):
    pass


@dataclass(frozen=True)
class Block:
    text: str
    kind: str  # paragraph | list_item | heading | pdf_block | json_field
    heading: str = ""


@dataclass(frozen=True)
class ParsedDocument:
    source_type: str
    blocks: tuple[Block, ...]

    @property
    def body_blocks(self) -> tuple[Block, ...]:
        return tuple(b for b in self.blocks if b.kind != "heading")

    @property
    def text_hash(self) -> str:
        """Hash of the extracted text (independent of formatting-only changes)."""
        return h("\n\n".join(f"{b.kind}\x1f{b.text}" for b in self.blocks))


def source_type_for(path: Path) -> str | None:
    return SUFFIX_TYPES.get(path.suffix.lower())


def parse_bytes(raw: bytes, source_type: str) -> ParsedDocument:
    try:
        parser = _PARSERS[source_type]
    except KeyError as exc:
        raise ParseError(f"unsupported source type {source_type!r}") from exc
    blocks = [Block(normalize_text(b.text), b.kind, b.heading) for b in parser(raw)]
    return ParsedDocument(source_type, tuple(b for b in blocks if b.text))


# --------------------------------------------------------------------------- #


def _decode(raw: bytes) -> str:
    return raw.decode("utf-8-sig", errors="replace")


def _strip_front_matter(text: str) -> str:
    if text.startswith("---\n") or text.startswith("---\r\n"):
        end = text.find("\n---", 4)
        if end != -1:
            nl = text.find("\n", end + 4)
            return text[nl + 1 :] if nl != -1 else ""
    return text


def _parse_markdown(raw: bytes) -> list[Block]:
    from markdown_it import MarkdownIt

    tokens = MarkdownIt("commonmark").enable("table").parse(_strip_front_matter(_decode(raw)))
    blocks: list[Block] = []
    headings: dict[int, str] = {}
    in_list = 0
    pending_heading: int | None = None
    header: list[str] = []  # current table's column headers
    row: list[str] | None = None  # cells of the table row being read
    in_thead = False
    for i, tok in enumerate(tokens):
        if tok.type == "thead_open":
            in_thead, header = True, []
        elif tok.type == "thead_close":
            in_thead = False
        elif tok.type == "tr_open":
            row = []
        elif tok.type == "tr_close":
            if in_thead:
                header = row or []
            else:
                text = _table_row_text(header, row or [])
                if text:
                    blocks.append(Block(text, "table_row", _heading_path(headings, 99)))
            row = None
        elif tok.type == "inline" and row is not None:
            row.append(_inline_text(tok).strip())
        elif tok.type in ("bullet_list_open", "ordered_list_open"):
            in_list += 1
        elif tok.type in ("bullet_list_close", "ordered_list_close"):
            in_list -= 1
        elif tok.type == "heading_open":
            pending_heading = int(tok.tag[1:])
        elif tok.type == "inline":
            text = _inline_text(tok)
            if pending_heading is not None:
                level = pending_heading
                headings = {k: v for k, v in headings.items() if k < level}
                headings[level] = text
                blocks.append(Block(text, "heading", _heading_path(headings, level - 1)))
                pending_heading = None
            else:
                kind = "list_item" if in_list else "paragraph"
                blocks.append(Block(text, kind, _heading_path(headings, 99)))
        elif tok.type == "heading_close":
            pending_heading = None
    return blocks


def _table_row_text(header: list[str], cells: list[str]) -> str:
    """One table row as a self-describing statement. An unlabelled first column is
    the row's label: ``| | A | B |`` / ``| x | 1 | 2 |`` gives ``"x — A: 1; B: 2."``."""
    label, parts = "", []
    for i, cell in enumerate(cells):
        if not cell:
            continue
        head = header[i] if i < len(header) else ""
        if i == 0 and not head:
            label = cell
        else:
            parts.append(f"{head}: {cell}" if head else cell)
    body = "; ".join(parts)
    text = f"{label} — {body}" if label and body else (label or body)
    return text if not text or text.endswith((".", "!", "?")) else text + "."


def _inline_text(tok) -> str:  # markdown_it Token
    if not tok.children:
        return tok.content
    parts: list[str] = []
    for child in tok.children:
        if child.type in ("text", "code_inline"):
            parts.append(child.content)
        elif child.type in ("softbreak", "hardbreak"):
            parts.append(" ")
        elif child.type == "image":
            parts.append(child.content)
    return "".join(parts)


def _heading_path(headings: dict[int, str], upto: int) -> str:
    return " > ".join(v for k, v in sorted(headings.items()) if k <= upto)


def _parse_text(raw: bytes) -> list[Block]:
    text = _decode(raw).replace("\r\n", "\n")
    return [Block(chunk, "paragraph") for chunk in text.split("\n\n") if chunk.strip()]


def _parse_html(raw: bytes) -> list[Block]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(raw, "html.parser")
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    blocks: list[Block] = []
    current = ""
    for el in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "blockquote", "dd", "dt"]):
        if el.name in ("p",) and el.find_parent(["li", "blockquote"]):
            continue
        text = el.get_text(" ", strip=True)
        if not text:
            continue
        if el.name.startswith("h"):
            current = text
            blocks.append(Block(text, "heading"))
        else:
            blocks.append(Block(text, "list_item" if el.name == "li" else "paragraph", current))
    return blocks


def _parse_json(raw: bytes) -> list[Block]:
    try:
        data = json.loads(_decode(raw))
    except json.JSONDecodeError as exc:
        raise ParseError(f"invalid JSON: {exc}") from exc
    blocks: list[Block] = []

    def walk(node, path: str) -> None:
        if isinstance(node, dict):
            for key in sorted(node):
                walk(node[key], f"{path}.{key}" if path else str(key))
        elif isinstance(node, list):
            for i, item in enumerate(node):
                walk(item, f"{path}[{i}]")
        elif node is not None:
            blocks.append(Block(f"{path}: {node}", "json_field", path.split(".")[0]))

    walk(data, "")
    return blocks


def _parse_pdf(raw: bytes) -> list[Block]:
    try:
        import pymupdf  # PyMuPDF >= 1.24
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise ParseError("PDF support requires the 'pdf' extra: pip install 'dakc[pdf]'") from exc
    blocks: list[Block] = []
    with pymupdf.open(stream=raw, filetype="pdf") as doc:
        for page_no, page in enumerate(doc, start=1):
            for b in page.get_text("blocks", sort=True):
                if b[6] == 0 and b[4].strip():  # text block
                    blocks.append(Block(b[4], "pdf_block", f"page {page_no}"))
    return blocks


_PARSERS = {
    "markdown": _parse_markdown,
    "text": _parse_text,
    "html": _parse_html,
    "json": _parse_json,
    "pdf": _parse_pdf,
}
