"""Knowledge diff for a code review: what a change does to the compiled knowledge base.

``dakc review --base REF`` compiles the workspace's sources as of a git ref (the
pull request's base) and as they are now (the head), and reports the
difference:

* the documents changed;
* the facts added and retracted, with their text;
* the pages and summaries affected;
* the LLM calls and estimated cost of applying it.

Both compiles use DAKC's deterministic mock provider, so a review never calls
or pays a model: the call counts are what an LLM provider would make, given the
facts the deterministic extractor finds. An LLM extracts somewhat different
facts, so treat the counts and prices as estimates of scale.

The base build is kept in a state directory (cache it in CI) and updated
incrementally, so reviews after the first are fast.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from dakc.compiler import Compiler
from dakc.errors import InvalidArgument, WorkspaceNotFound
from dakc.providers import make_provider
from dakc.sources import FilesystemSource, Source, SourceSet
from dakc.storage import Store
from dakc.workspace import Workspace

MARKER = "<!-- dakc-knowledge-diff -->"


@dataclass
class Price:
    label: str
    input: float  # USD per million input tokens
    output: float  # USD per million output tokens

    @classmethod
    def parse(cls, spec: str) -> Price:
        try:
            label, pin, pout = spec.rsplit(":", 2)
            return cls(label, float(pin), float(pout))
        except ValueError as exc:
            raise InvalidArgument(f"price must be LABEL:INPUT:OUTPUT in USD per million tokens, got {spec!r}") from exc


@dataclass
class Review:
    base: str
    head: str
    documents: dict[str, list] = field(default_factory=dict)  # new, modified, deleted, renamed
    facts_added: list[dict[str, Any]] = field(default_factory=list)  # {id, text, sources}
    facts_retracted: list[dict[str, Any]] = field(default_factory=list)
    facts_changed_sources: int = 0
    pages: dict[str, list[str]] = field(default_factory=dict)  # regenerated, rerendered, new, deleted (titles)
    summaries: dict[str, int] = field(default_factory=dict)
    passages_reextracted: int = 0
    sections_rewritten: int = 0
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    totals: dict[str, int] = field(default_factory=dict)  # corpus size at head
    costs: list[dict[str, Any]] = field(default_factory=list)  # {label, usd}

    @property
    def has_changes(self) -> bool:
        return bool(self.facts_added or self.facts_retracted or any(self.pages.values())
                    or any(self.documents.values()) or any(self.summaries.values()))

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "has_changes": self.has_changes}


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise InvalidArgument(f"git {' '.join(args)} failed: {r.stderr.strip()[:300]}")
    return r.stdout.strip()


def _base_sources(head: SourceSet, repo_root: Path, base_tree: Path) -> SourceSet:
    """The same sources as of the base commit: filesystem sources inside the repository
    are read from the base checkout; anything else is the same on both sides."""
    out = []
    for s in head.sources:
        conn = s.connector
        if isinstance(conn, FilesystemSource) and conn.root.is_relative_to(repo_root):
            mapped = tuple(base_tree / p.resolve().relative_to(repo_root) if p.resolve().is_relative_to(repo_root) else p
                           for p in conn.exclude)
            conn = FilesystemSource(base_tree / conn.root.relative_to(repo_root), mapped)
        out.append(Source(s.name, conn, s.prefix, s.options))
    return SourceSet(out)


def _compile(store: Store, ws: Workspace, sources: SourceSet):
    return Compiler(store, make_provider("mock"), sources, aliases=ws.config.alias_map,
                    config_fingerprint=ws.config.fingerprint, provenance=ws.config.provenance,
                    jobs=1, acquire_lock=False).run()[0]


def _claim_text(store: Store, cid: str, at_build: int) -> tuple[str, list[str]]:
    row = store.conn.execute(
        "SELECT payload FROM artifacts WHERE id=? AND from_build<=? AND (to_build IS NULL OR to_build>?)",
        (cid, at_build, at_build)).fetchone()
    if row is None:
        return cid, []
    sup = json.loads(row[0])["supports"]
    pids = [s[0] for s in sup]
    paths: set[str] = set()
    for pid in pids:
        p = store.conn.execute(
            "SELECT json_extract(payload,'$.source_id') FROM artifacts WHERE id=? AND from_build<=? AND (to_build IS NULL OR to_build>?)",
            (pid, at_build, at_build)).fetchone()
        if p:
            src = store.conn.execute(
                "SELECT path FROM sources WHERE id=? AND from_build<=? AND (to_build IS NULL OR to_build>?)",
                (p[0], at_build, at_build)).fetchone()
            if src:
                paths.add(src[0])
    return (min(s[1] for s in sup) if sup else cid), sorted(paths)


def _title(store: Store, aid: str, at_build: int) -> str:
    row = store.conn.execute(
        "SELECT json_extract(payload,'$.title') FROM artifacts WHERE id=? AND from_build<=? AND (to_build IS NULL OR to_build>?)",
        (aid, at_build, at_build)).fetchone()
    return (row[0] if row and row[0] else aid.split(":", 1)[-1])


def review(workspace: Path, base_ref: str, *, state_dir: Path | None = None, prices: list[Price] | None = None) -> Review:
    try:
        ws = Workspace.load(workspace)
    except FileNotFoundError as exc:
        raise WorkspaceNotFound(str(exc)) from exc
    repo_root = Path(_git(ws.root, "rev-parse", "--show-toplevel")).resolve()
    base_sha = _git(repo_root, "rev-parse", f"{base_ref}^{{commit}}")
    head_ref = _git(repo_root, "rev-parse", "--short=10", "HEAD")
    if _git(repo_root, "status", "--porcelain", "--untracked-files=normal"):
        head_ref += " with uncommitted changes"
    state = (state_dir or ws.state_dir / "review").resolve()
    state.mkdir(parents=True, exist_ok=True)
    head_sources = ws.source_set()

    with tempfile.TemporaryDirectory(prefix="dakc-review-") as tmp:
        tree = Path(tmp) / "base"
        _git(repo_root, "worktree", "add", "--detach", "--force", str(tree), base_sha)
        try:
            base_db = state / "base.db"
            store = Store(base_db)
            try:
                _compile(store, ws, _base_sources(head_sources, repo_root, tree))
                store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                store.close()
        finally:
            _git(repo_root, "worktree", "remove", "--force", str(tree))
        head_db = Path(tmp) / "head.db"
        shutil.copy(base_db, head_db)
        store = Store(head_db)
        try:
            a = store.head
            base_paths = {r.id: r.path for r in store.load_sources().values()}
            rep = _compile(store, ws, head_sources)
            b = store.head
            out = Review(base_sha[:10], head_ref)
            sc = rep.scan
            out.documents = {"new": sc.get("new_paths", []), "modified": sc.get("modified_paths", []),
                             "deleted": [base_paths.get(i, i) for i in sc.get("deleted_ids", [])],
                             "renamed": sc.get("renamed_paths", [])}
            diff = store.diff_builds(a, b) if b != a else {}
            for cid, change in diff.get("claim", []):
                if change == "created":
                    text, paths = _claim_text(store, cid, b)
                    out.facts_added.append({"id": cid, "text": text, "sources": paths})
                elif change == "deleted":
                    text, paths = _claim_text(store, cid, a)
                    out.facts_retracted.append({"id": cid, "text": text, "sources": paths})
                else:
                    out.facts_changed_sources += 1
            ops = rep.ops
            new_pages = {i for i, c in diff.get("page", []) if c == "created"}
            out.pages = {
                "regenerated": sorted(_title(store, p, b) for p in ops.get("regenerate_page", []) if p not in new_pages),
                "new": sorted(_title(store, p, b) for p in new_pages),
                "rerendered": sorted(_title(store, p, b) for p in ops.get("rerender_page", [])),
                "deleted": sorted(_title(store, p, a) for p in ops.get("delete_page", [])),
            }
            out.summaries = {k: len(ops.get(f"{k}_summary", [])) for k in ("regenerate", "rerender", "delete")}
            out.passages_reextracted = rep.usage.by_stage.get("extract", 0)
            out.sections_rewritten = len(ops.get("regenerate_section", []))
            out.llm_calls, out.input_tokens, out.output_tokens = rep.usage.llm_calls, rep.usage.input_tokens, rep.usage.output_tokens
            out.totals = {k: v for k, v in store.counts().items() if k in ("source", "passage", "claim", "page", "summary")}
        finally:
            store.close()
    for p in prices or []:
        out.costs.append({"label": p.label, "usd": round((out.input_tokens * p.input + out.output_tokens * p.output) / 1e6, 4)})
    return out


# ------------------------------------------------------------------- rendering
def _n(x: int, one: str, many: str | None = None) -> str:
    return f"{x:,} {one if x == 1 else (many or one + 's')}"


def _details(title: str, items: list[str], limit: int) -> str:
    if not items:
        return ""
    body = "\n".join(f"- {i}" for i in items[:limit]) + (f"\n- … and {len(items) - limit:,} more" if len(items) > limit else "")
    return f"<details><summary>{title}</summary>\n\n{body}\n\n</details>\n"


def render_markdown(r: Review, limit: int = 25) -> str:
    def esc(s: str) -> str:
        return s.replace("|", "\\|").replace("<", "&lt;").replace(">", "&gt;")

    lines = [MARKER, "### Knowledge diff (DAKC)", ""]
    docs = r.documents
    changed = sum(len(docs.get(k, [])) for k in ("new", "modified", "deleted", "renamed"))
    if not r.has_changes:
        lines += [f"This change does not affect the knowledge base ({_n(r.totals.get('source', 0), 'source document')} checked).", ""]
    else:
        pages_total = r.totals.get("page", 0)
        touched = len(r.pages.get("regenerated", [])) + len(r.pages.get("new", []))
        cost = "" if not r.costs else " (" + ", ".join(f"≈ ${c['usd']:.4f} with {c['label']}" for c in r.costs) + ")"
        summaries = r.summaries.get("regenerate", 0)
        lines += [
            f"**{_n(changed, 'document')} changed.** Compiling this change would:",
            "",
            f"- re-extract **{_n(r.passages_reextracted, 'passage')}** of {r.totals.get('passage', 0):,}",
            f"- add **{_n(len(r.facts_added), 'fact')}** and retract **{_n(len(r.facts_retracted), 'fact')}**"
            + (f" ({_n(r.facts_changed_sources, 'fact')} keep their text but change sources)" if r.facts_changed_sources else ""),
            f"- regenerate **{_n(touched, 'page')}** of {pages_total:,}"
            + (f" ({_n(len(r.pages['new']), 'new page')})" if r.pages.get("new") else "")
            + (f", re-render {len(r.pages['rerendered'])} without an LLM call" if r.pages.get("rerendered") else "")
            + (f", delete {len(r.pages['deleted'])}" if r.pages.get("deleted") else ""),
            (f"- regenerate {_n(summaries, 'topic summary', 'topic summaries')}"
             + (f" ({r.sections_rewritten} hub or topic sections rewritten)" if r.sections_rewritten else ""))
            if summaries or r.sections_rewritten else None,
            f"- cost **{_n(r.llm_calls, 'LLM call')}**, about {r.input_tokens:,} input and {r.output_tokens:,} output tokens{cost}",
            f"- leave everything else untouched: {max(pages_total - touched, 0):,} pages",
            "",
        ]
        lines.append(_details(f"Facts added ({len(r.facts_added)})",
                              [f"{esc(f['text'])} <sub>{esc(', '.join(f['sources']))}</sub>" for f in r.facts_added], limit))
        lines.append(_details(f"Facts retracted ({len(r.facts_retracted)})",
                              [f"~~{esc(f['text'])}~~ <sub>{esc(', '.join(f['sources']))}</sub>" for f in r.facts_retracted], limit))
        regenerated = [f"{esc(t)} (new)" for t in r.pages.get("new", [])] + [esc(t) for t in r.pages.get("regenerated", [])]
        lines.append(_details(f"Pages regenerated ({len(regenerated)})", regenerated, limit))
        lines.append(_details(f"Documents ({changed})",
                              [f"{k}: `{p if isinstance(p, str) else ' → '.join(p)}`" for k in ("new", "modified", "deleted", "renamed")
                               for p in docs.get(k, [])], limit))
    lines += [
        f"<sub>Base `{r.base}` → head `{r.head.split(' ')[0]}`{r.head[len(r.head.split(' ')[0]):]}. Computed with DAKC's deterministic extractor, so nothing was spent; "
        "an LLM extracts somewhat different facts, so calls and cost are estimates of scale. "
        "[What is this?](https://github.com/thejasrao262003/dependency-aware-knowledge-compilation)</sub>",
    ]
    return "\n".join(x for x in lines if x is not None).replace("\n\n\n", "\n\n") + "\n"


def run(workspace: Path, base_ref: str, *, state_dir: Path | None = None, prices: list[str] | None = None,
        fmt: str = "markdown", limit: int = 25) -> tuple[Review, str]:
    if fmt not in ("markdown", "json"):
        raise InvalidArgument("format must be markdown or json")
    r = review(workspace, base_ref, state_dir=state_dir, prices=[Price.parse(p) for p in prices or []])
    text = render_markdown(r, limit) if fmt == "markdown" else json.dumps(r.to_dict(), indent=1, ensure_ascii=False)
    return r, text
