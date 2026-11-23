"""Links between documents, checked at every compile.

Every compile reads the links in each document, resolves the ones that point inside the
knowledge base, and reports the broken ones: a document that does not exist, or a
``#anchor`` that the target document no longer has. When the break comes from a rename
DAKC saw (a section whose text is unchanged under a new heading, or a moved file), the
finding names the new target.

Advisory and deterministic, like conflict detection: nothing here changes a claim or a
page, and no model is called. External URLs are not checked.

Precision first:
- a link to a path outside the knowledge base (``../../other-repo/x.md``) is skipped;
- a link without a file extension (``../first-steps/``, a site URL) is checked only when
  it resolves to a document; if it does not, it is skipped rather than reported;
- anchors are compared case-insensitively, and a heading answers to its explicit id
  (``{ #id }``, ``<a id>``) or, without one, to both GitHub's and MkDocs' slug of its text.
"""

from __future__ import annotations

import html.parser
import json
import posixpath
import re
import unicodedata
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import unquote

from dakc.hashing import h

LINKS_VERSION = "links/1"  # bump on any change to reading, resolving or judging: stores re-check everything
DOC_EXTS = {".md", ".markdown", ".mdx", ".html", ".htm", ".txt", ".pdf", ".json", ".rst"}
_EXPLICIT_ID = re.compile(r"\{\s*#([^\s}]+)[^}]*\}\s*$")
_HTML_ID = re.compile(r"""<[a-zA-Z][^>]*?\s(?:id|name)\s*=\s*["']([^"']+)["']""")
_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")


@dataclass(frozen=True)
class Link:
    line: int
    text: str
    href: str


@dataclass
class DocLinks:
    """The links a document makes, and the anchors it offers."""

    links: list[Link] = field(default_factory=list)
    anchors: list[str] = field(default_factory=list)

    def to_json(self) -> tuple[str, str]:
        return (json.dumps([asdict(x) for x in self.links], ensure_ascii=False),
                json.dumps(sorted(set(self.anchors)), ensure_ascii=False))

    @classmethod
    def from_json(cls, links: str, anchors: str) -> DocLinks:
        return cls([Link(**x) for x in json.loads(links)], json.loads(anchors))


# --------------------------------------------------------------------------- reading

def heading_anchors(text: str) -> tuple[str, ...]:
    """The anchors a heading answers to: its explicit id, or both common slugs of its text."""
    m = _EXPLICIT_ID.search(text)
    if m:
        return (m.group(1),)
    base = re.sub(r"[`*_~]|\[([^\]]*)\]\([^)]*\)", lambda mm: mm.group(1) or "", text).strip()
    github = re.sub(r"[^\w\- ]", "", base.lower(), flags=re.UNICODE).replace(" ", "-")
    ascii_ = unicodedata.normalize("NFKD", base).encode("ascii", "ignore").decode("ascii")
    pymd = re.sub(r"[-\s]+", "-", re.sub(r"[^\w\s-]", "", ascii_).strip().lower())
    return tuple(dict.fromkeys(a for a in (github, pymd) if a))


def _front_matter_lines(text: str) -> int:
    if text.startswith("---\n") or text.startswith("---\r\n"):
        end = text.find("\n---", 4)
        if end != -1:
            nl = text.find("\n", end + 4)
            return text[: nl + 1 if nl != -1 else len(text)].count("\n")
    return 0


def _markdown(raw: bytes) -> DocLinks:
    from markdown_it import MarkdownIt

    text = raw.decode("utf-8-sig", errors="replace")
    skip = _front_matter_lines(text)
    body = "\n".join(text.splitlines()[skip:])
    tokens = MarkdownIt("commonmark").enable("table").parse(body)
    out = DocLinks()
    seen: Counter[str] = Counter()
    heading_next = False
    for tok in tokens:
        if tok.type == "heading_open":
            heading_next = True
            continue
        if tok.type in ("html_block", "html_inline"):
            out.anchors += _HTML_ID.findall(tok.content)
        if tok.type != "inline":
            continue
        if heading_next:
            heading_next = False
            for a in heading_anchors(tok.content):
                n = seen[a]
                seen[a] += 1
                out.anchors += [a] if not n else [f"{a}-{n}", f"{a}_{n}"]  # a repeated heading: GitHub -1, MkDocs _1
        lines = tok.content.split("\n")
        first = (tok.map[0] if tok.map else 0) + skip + 1
        children = tok.children or []
        for j, child in enumerate(children):
            if child.type == "html_inline":
                out.anchors += _HTML_ID.findall(child.content)
            if child.type != "link_open":
                continue
            href = str(child.attrGet("href") or "")
            label = []
            for nxt in children[j + 1:]:
                if nxt.type == "link_close":
                    break
                label.append(nxt.content)
            offset = next((k for k, ln in enumerate(lines) if href and href in ln), 0)
            out.links.append(Link(first + offset, "".join(label).strip(), href))
    return out


class _HTMLLinks(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = DocLinks()
        self._open: tuple[int, str] | None = None
        self._label: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        for key in ("id", "name"):
            if a.get(key) and (key == "id" or tag == "a"):
                self.out.anchors.append(str(a[key]))
        if tag == "a" and a.get("href"):
            self._open, self._label = (self.getpos()[0], str(a["href"])), []

    def handle_data(self, data):
        if self._open:
            self._label.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._open:
            line, href = self._open
            self.out.links.append(Link(line, " ".join("".join(self._label).split()), href))
            self._open = None


def extract(raw: bytes, source_type: str) -> DocLinks:
    """The links and anchors of one document. Plain text, JSON and PDF have neither."""
    if source_type == "markdown":
        return _markdown(raw)
    if source_type == "html":
        p = _HTMLLinks()
        p.feed(raw.decode("utf-8", errors="replace"))
        return p.out
    return DocLinks()


# --------------------------------------------------------------------------- resolving

def resolve(from_path: str, href: str) -> tuple[list[str], str, bool] | None:
    """(candidate target paths, anchor, explicit) for a link inside the knowledge base, or None
    for one this check leaves alone (external, outside the knowledge base, not a document).
    ``explicit``: the link names a document file, so a missing target is reported."""
    href = href.strip()
    if not href or _SCHEME.match(href) or href.startswith("//"):
        return None
    path, _, anchor = href.partition("#")
    path, anchor = unquote(path.split("?")[0]), unquote(anchor)
    if not path:
        return [from_path], anchor, True
    base = posixpath.dirname(from_path)
    target = posixpath.normpath(path.lstrip("/") if path.startswith("/") else posixpath.join(base, path))
    if target == ".." or target.startswith("../"):
        return None
    target = "" if target == "." else target
    ext = posixpath.splitext(target)[1].lower()
    if path.endswith("/") or not ext:
        # a directory or site URL: as written, and as MkDocs reads it from the page's own URL
        page_dir = posixpath.splitext(from_path)[0]
        alt = posixpath.normpath(posixpath.join(page_dir, path))
        cands = []
        for t in dict.fromkeys(x for x in (target, alt) if not x.startswith("..")):
            t = "" if t == "." else t
            cands += [f"{t}.md" if t else "index.md", posixpath.join(t, "index.md"), posixpath.join(t, "README.md"),
                      posixpath.join(t, "index.html")]
        return list(dict.fromkeys(cands)), anchor, False
    if ext in (".html", ".htm"):
        return [target, target[: -len(ext)] + ".md"], anchor, True
    if ext in DOC_EXTS:
        return [target], anchor, True
    return None


def relative_href(from_path: str, to_path: str, anchor: str) -> str:
    rel = posixpath.relpath(to_path, posixpath.dirname(from_path) or ".")
    return rel + (f"#{anchor}" if anchor else "")


# --------------------------------------------------------------------------- judging

@dataclass
class Broken:
    id: str
    kind: str  # missing_document | missing_anchor
    source_id: str
    path: str
    line: int
    text: str
    href: str
    target: str
    anchor: str
    reason: str
    suggestion: str = ""

    def payload(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("id")
        return d


def check(docs: dict[str, DocLinks], paths: dict[str, str],
          moved: dict[str, str] | None = None,
          renamed_anchor: Any = None) -> dict[str, Broken]:
    """Every broken link among ``docs`` (source id -> DocLinks; ``paths``: source id -> path).

    ``moved``: an old document path -> its current path (for suggestions).
    ``renamed_anchor(source_id, anchor)``: (old heading, new heading, new anchor) when a
    section of that document that answered to ``anchor`` now sits under another heading."""
    moved = moved or {}
    by_path = {p: sid for sid, p in paths.items()}
    anchors = {sid: {a.lower() for a in d.anchors} for sid, d in docs.items()}
    out: dict[str, Broken] = {}
    for sid in sorted(docs):
        src = paths.get(sid)
        if src is None:
            continue
        occurrence: Counter[str] = Counter()
        for link in docs[sid].links:
            n = occurrence[link.href]
            occurrence[link.href] += 1
            r = resolve(src, link.href)
            if r is None:
                continue
            cands, anchor, explicit = r
            target = next((c for c in cands if c in by_path), None)
            fid = "lnk:" + h(f"{sid}\x00{link.href}\x00{n}")[:10]
            if target is None:
                new = next((moved[c] for c in cands if c in moved and moved[c] in by_path), None)
                if not explicit and new is None:  # a site URL that never was a document here: not ours to judge
                    continue
                new_anchor = anchor
                if new and anchor and anchor.lower() not in anchors.get(by_path[new], ()) and renamed_anchor:
                    hint = renamed_anchor(by_path[new], anchor)  # moved, and the section renamed too
                    new_anchor = hint[2] if hint else anchor
                out[fid] = Broken(fid, "missing_document", sid, src, link.line, link.text, link.href, cands[0], anchor,
                                  f"{cands[0]} is not in the knowledge base" + (f": it moved to {new}" if new else ""),
                                  relative_href(src, new, new_anchor) if new else "")
                continue
            tid = by_path[target]
            if not anchor or anchor.lower() in anchors.get(tid, ()):
                continue
            if tid not in docs:  # a document type without anchors (PDF, text): nothing to check
                continue
            hint = renamed_anchor(tid, anchor) if renamed_anchor else None
            reason = f"{target} has no heading #{anchor}"
            if hint:
                old, new_heading, new_anchor = hint
                reason += f': "{old}" is now "{new_heading}"'
            sugg = relative_href(src, target, hint[2]) if hint and target != src else (f"#{hint[2]}" if hint else "")
            out[fid] = Broken(fid, "missing_anchor", sid, src, link.line, link.text, link.href, target, anchor, reason, sugg)
    return out


def last_heading(path: str) -> str:
    """The innermost heading of a passage's heading path, as written (an explicit id kept)."""
    return path.split(" > ")[-1].strip() if path else ""


def display_heading(text: str) -> str:
    return _EXPLICIT_ID.sub("", text).strip()


def anchor_renames(history: dict[str, list[str]], current: dict[str, str]):
    """A ``renamed_anchor`` for :func:`check`, from passage headings over time.

    ``history``: passage id -> every heading path it has had (oldest first);
    ``current``: passage id -> its heading path now. Both for one target document's passages."""
    def parts(path: str) -> list[str]:
        return [x.strip() for x in path.split(" > ")] if path else []

    def find(anchor: str):
        # Every level of the heading path counts: a renamed heading may have only subsections under it.
        wanted = anchor.lower()
        for pid in sorted(current):
            now_parts = parts(current[pid])
            for old_path in history.get(pid, []):
                for i, old in enumerate(parts(old_path)):
                    now = now_parts[i] if i < len(now_parts) else ""
                    if now and old != now and wanted in {a.lower() for a in heading_anchors(old)}:
                        return display_heading(old), display_heading(now), heading_anchors(now)[0]
        return None
    return find


def fingerprint() -> str:
    return LINKS_VERSION
