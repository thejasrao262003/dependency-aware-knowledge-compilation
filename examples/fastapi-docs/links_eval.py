"""Broken-link checking on FastAPI's documentation.

    python examples/fastapi-docs/links_eval.py      # writes results/links.json and links.md

1. **The docs as they are** at the pinned commit: links, how many point inside the docs,
   anchors, and broken links found (each listed, so a false alarm can be seen).
2. **Planted renames.** For every link to a heading in another document, rename that
   heading (text and explicit id) in a copy; and for every linked document, move it. The
   check must report the link and name the new target.
3. **Real history.** Compile the last 50 commits that changed the docs, incrementally, and
   count the links each commit broke.

No model is called.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from replay import DOCS, END, ensure_clone, git  # noqa: E402

import dakc  # noqa: E402
from dakc.claims import segment  # noqa: E402
from dakc.compiler import Compiler  # noqa: E402
from dakc.links import anchor_renames, check, extract, heading_anchors, resolve  # noqa: E402
from dakc.parsing import parse_bytes  # noqa: E402
from dakc.providers import make_provider  # noqa: E402
from dakc.storage import Store  # noqa: E402

HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*$")


def read_docs(repo: Path, sha: str) -> dict[str, bytes]:
    files = [f for f in git(repo, "ls-tree", "-r", "--name-only", sha, DOCS).split() if f.endswith(".md")]
    return {f[len(DOCS) + 1:]: subprocess.run(["git", "-C", str(repo), "show", f"{sha}:{f}"], capture_output=True,
                                               check=True).stdout for f in files}


def rename_heading(raw: bytes, anchor: str) -> tuple[bytes, str] | None:
    """The document with the heading answering to ``anchor`` renamed (text and explicit id)."""
    lines = raw.decode("utf-8").split("\n")
    for i, line in enumerate(lines):
        m = HEADING.match(line)
        if m and anchor.lower() in {a.lower() for a in heading_anchors(m.group(2))}:
            text = re.sub(r"\{\s*#([^\s}]+)", lambda mm: "{ #" + mm.group(1) + "-renamed", m.group(2))
            text = re.sub(r"^([^{]*?)(\s*\{|$)", lambda mm: mm.group(1) + " (renamed)" + mm.group(2), text, count=1)
            lines[i] = f"{m.group(1)} {text}"
            return "\n".join(lines).encode("utf-8"), heading_anchors(text)[0]
    return None


def headings(raw: bytes) -> dict[str, str]:
    return {p.id: p.heading for p in segment("s", parse_bytes(raw, "markdown"))}


def planted(raws: dict[str, bytes]) -> dict:
    docs = {p: extract(r, "markdown") for p, r in raws.items()}
    paths = {p: p for p in raws}
    targets = []
    for src, d in docs.items():
        for link in d.links:
            r = resolve(src, link.href)
            if r and r[1]:
                target = next((c for c in r[0] if c in raws), None)
                if target and target != src:
                    targets.append((src, link, target, r[1]))
    res = {"heading": {"links": 0, "caught": 0, "fix_correct": 0, "not_renamable": 0},
           "document": {"links": 0, "caught": 0, "fix_correct": 0}}
    for src, link, target, anchor in targets:
        renamed = rename_heading(raws[target], anchor)
        if renamed is None:  # an anchor from inline HTML, not a heading
            res["heading"]["not_renamable"] += 1
            continue
        new_raw, new_anchor = renamed
        trial = dict(docs, **{target: extract(new_raw, "markdown")})
        old_h, new_h = headings(raws[target]), headings(new_raw)
        hint = anchor_renames({pid: [h] for pid, h in old_h.items()}, new_h)
        found = [b for b in check(trial, paths, None, lambda sid, a: hint(a) if sid == target else None).values()
                 if b.path == src and b.href == link.href]
        res["heading"]["links"] += 1
        res["heading"]["caught"] += bool(found)
        res["heading"]["fix_correct"] += bool(found) and found[0].suggestion.endswith("#" + new_anchor)
    for target in sorted({t for _, _, t, _ in targets}):
        moved_to = "moved/" + target
        trial = {(moved_to if p == target else p): d for p, d in docs.items()}
        found = check(trial, {p: p for p in trial}, {target: moved_to})
        incoming = [(s, link) for s, link, t, _ in targets if t == target]
        for src, link in incoming:
            hits = [b for b in found.values() if b.path == src and b.href == link.href]
            res["document"]["links"] += 1
            res["document"]["caught"] += bool(hits)
            res["document"]["fix_correct"] += bool(hits) and moved_to.split("/")[-1] in hits[0].suggestion
    return res


def replay(repo: Path, n: int) -> list[dict]:
    commits = git(repo, "rev-list", "--first-parent", "-n", str(n), END, "--", DOCS).split()[::-1]
    base = git(repo, "rev-parse", f"{commits[0]}^").strip()
    rows = []
    with tempfile.TemporaryDirectory(prefix="dakc-links-") as tmp:
        src = Path(tmp) / "docs"

        def checkout(sha: str) -> None:
            shutil.rmtree(src, ignore_errors=True)
            src.mkdir()
            arch = subprocess.run(["git", "-C", str(repo), "archive", sha, DOCS], check=True, capture_output=True).stdout
            subprocess.run(["tar", "-x", "-C", str(src)], input=arch, check=True)

        store = Store(Path(tmp) / "kb.db")
        checkout(base)
        Compiler(store, make_provider("mock"), src / DOCS, jobs=8, acquire_lock=False).run()
        for sha in commits:
            checkout(sha)
            rep, _ = Compiler(store, make_provider("mock"), src / DOCS, jobs=8, acquire_lock=False).run()
            rows.append({"commit": sha[:10], "subject": git(repo, "log", "-1", "--format=%s", sha).strip()[:90],
                         "broken_now": rep.counts.get("links_broken", 0), "newly_broken": rep.n("link_broken"),
                         "fixed": rep.n("link_fixed")})
        store.close()
    return rows


def main() -> None:
    repo = ensure_clone(HERE / ".work")
    raws = read_docs(repo, END)
    t = time.perf_counter()
    docs = {p: extract(r, "markdown") for p, r in raws.items()}
    t_read = time.perf_counter() - t
    t = time.perf_counter()
    found = check(docs, {p: p for p in raws})
    t_check = time.perf_counter() - t
    internal = sum(1 for p, d in docs.items() for link in d.links if resolve(p, link.href))
    out = {"generated": datetime.now(UTC).isoformat(timespec="seconds"), "dakc_version": dakc.__version__,
           "source": {"repo": "fastapi/fastapi", "commit": END},
           "as_is": {"documents": len(docs), "links": sum(len(d.links) for d in docs.values()), "internal_links": internal,
                     "anchors": sum(len(d.anchors) for d in docs.values()), "broken": [b.payload() for b in found.values()],
                     "read_seconds": round(t_read, 3), "check_ms": round(t_check * 1000, 2)}}
    print("as is:", {k: v for k, v in out["as_is"].items() if k != "broken"}, "broken:", len(found), flush=True)
    out["planted"] = planted(raws)
    print("planted:", out["planted"], flush=True)
    out["replay"] = replay(repo, 50)
    broke = [r for r in out["replay"] if r["newly_broken"]]
    print(f"replay: {len(broke)} of {len(out['replay'])} commits broke a link", flush=True)

    res = HERE / "results" / "links.json"
    res.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    a, p = out["as_is"], out["planted"]
    md = [
        "# Broken links on FastAPI's documentation",
        "",
        f"_Generated by `examples/fastapi-docs/links_eval.py` ({out['generated']}, DAKC {out['dakc_version']}). FastAPI's English "
        f"docs at [`{END[:8]}`](https://github.com/fastapi/fastapi/commit/{END}). No model is called._",
        "",
        "## The docs as they are",
        "",
        f"{a['documents']} documents, {a['links']:,} links ({a['internal_links']} pointing inside the docs; the rest are external "
        f"and not checked), {a['anchors']:,} anchors. **{len(a['broken'])} broken links.** Checking every link took "
        f"{a['check_ms']} ms.",
        "",
        *[f"- `{b['path']}:{b['line']}` {b['href']}: {b['reason']}" for b in a["broken"]],
        "",
        "## Planted renames",
        "",
        "| Rename | Links affected | Reported | Fix named correctly |",
        "|---|---:|---:|---:|",
        f"| the heading a link points at (text and explicit id) | {p['heading']['links']} | {p['heading']['caught']} | "
        f"{p['heading']['fix_correct']} |",
        f"| the document a link points at (moved to another folder) | {p['document']['links']} | {p['document']['caught']} | "
        f"{p['document']['fix_correct']} |",
        "",
        "## Real history",
        "",
        f"Over the last {len(out['replay'])} commits that changed the docs, compiled incrementally: "
        f"**{len(broke)} broke a link**." + ("" if not broke else " They were:"),
        "",
        *[f"- `{r['commit']}` {r['subject']}: {r['newly_broken']} broken" for r in broke],
    ]
    res.with_suffix(".md").write_text("\n".join(md).rstrip() + "\n", encoding="utf-8")
    print(f"wrote {res} and {res.with_suffix('.md')}")


if __name__ == "__main__":
    main()
