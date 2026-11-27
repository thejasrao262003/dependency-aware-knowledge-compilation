"""``dakc`` command-line interface: a thin presentation layer over :mod:`dakc.kb`."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table
from rich.tree import Tree

from dakc import kb as api
from dakc.artifacts import LAYER
from dakc.compiler import STRATEGIES
from dakc.errors import BuildLocked, CompileFailed, CostNotAllowed, DakcError
from dakc.invalidation import projection_lines, render
from dakc.kb import KnowledgeBase
from dakc.workspace import CONFIG_NAME, Workspace

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Dependency-Aware Knowledge Compiler: recompile only what has become invalid.",
)
_WIDTH = None if sys.stdout.isatty() else 160
console = Console(highlight=False, soft_wrap=True, width=_WIDTH)
err = Console(stderr=True, highlight=False, soft_wrap=True, width=_WIDTH)

WS = Annotated[Path, typer.Option("--workspace", "-w", help="Workspace directory (holds dakc.toml).", envvar="DAKC_WORKSPACE")]
JSON = Annotated[bool, typer.Option("--json", help="Machine-readable output.")]
WAIT = Annotated[float, typer.Option("--wait", help="Seconds to wait for another running build on this workspace.")]
LOOPBACK = ("127.0.0.1", "localhost", "::1")


def _require_loopback(host: str) -> None:
    """The HTTP servers have no authentication yet, so they listen only on this machine."""
    if host not in LOOPBACK:
        err.print(f"[red]error:[/red] refusing to listen on {host!r}: DAKC's HTTP servers have no authentication yet, "
                  "so they only bind to this machine (127.0.0.1, localhost or ::1). A read-only API still exposes "
                  "everything in the knowledge base.")
        raise typer.Exit(2)


@contextmanager
def _errors() -> Iterator[None]:
    """Map library errors to messages and exit codes: 1 failed build, 2 usage, 3 locked."""
    try:
        yield
    except CompileFailed as exc:
        err.print(f"[red]compilation failed:[/red] {exc}")
        err.print(f"previous valid compilation preserved: {'yes' if exc.preserved else '[red]NO[/red]'} (HEAD = build {exc.head})")
        if exc.projection and exc.projection.get("repaired"):
            for line in projection_lines(exc.projection):
                err.print(line, markup=False, highlight=False)
        raise typer.Exit(1) from exc
    except BuildLocked as exc:
        err.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(3) from exc
    except DakcError as exc:
        err.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(2) from exc


@contextmanager
def _open(path: Path) -> Iterator[KnowledgeBase]:
    with _errors():
        kb = KnowledgeBase.open(path)
        with kb:
            yield kb


def _emit_json(obj) -> None:
    sys.stdout.write(json.dumps(obj, indent=1, sort_keys=True, default=str) + "\n")


def _print_publications(pubs: list[dict]) -> None:
    for p in pubs:
        if p.get("error"):
            console.print(f"[red]Publisher {p['name']} ({p['kind']}) failed[/red]: {p['error']}. "
                          f"{p['pending']} change(s) still pending; `dakc publish` retries.")
        elif p.get("dry_run"):
            console.print(f"Publisher {p['name']} ({p['kind']}): {p['pending']} change(s) pending")
        elif p["upserted"] or p["deleted"]:
            console.print(f"Published to {p['name']} ({p['kind']}): {p['upserted']} upserted, {p['deleted']} deleted")
        else:
            console.print(f"Publisher {p['name']} ({p['kind']}): up to date")


def _print_report(report, as_json: bool) -> None:
    if as_json:
        _emit_json(report.to_dict())
        return
    console.print(render(report), markup=False)  # plain text: notes quote "[[conflicts.scope]]", "[conflicts]"
    _print_publications(report.publications)
    if report.status == "noop":
        console.print("\nNothing changed. Knowledge base is up to date.")


def _layer(kind: str) -> int:
    return LAYER.get(kind, 9)


# --------------------------------------------------------------------------- #
def _version(value: bool) -> None:
    if value:
        from dakc import __version__

        console.print(f"dakc {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    version: Annotated[bool, typer.Option("--version", callback=_version, is_eager=True, help="Show the version and exit.")] = False,
) -> None:
    """Dependency-Aware Knowledge Compiler: recompile only what has become invalid."""


@app.command()
def init(
    workspace: WS = Path("."),
    source: Annotated[str, typer.Option(help="Source directory, relative to the workspace.")] = "knowledge",
    output: Annotated[str, typer.Option(help="Output (wiki) directory.")] = "wiki",
    provider: Annotated[str, typer.Option(help="heuristic | mock | claude-code | anthropic (claude) | openai | gemini | grok | "
                                               "deepseek | mistral | groq | openrouter | together | ollama | lmstudio | custom")] = "heuristic",
) -> None:
    """Create dakc.toml in a workspace."""
    existed = (workspace / CONFIG_NAME).exists()
    ws = Workspace.init(workspace, source_dir=source, provider=provider, output_dir=output)
    console.print(f"{'Using existing' if existed else 'Created'} {ws.root / CONFIG_NAME}")
    console.print(f"  sources: {ws.source_root}\n  output:  {ws.output_root}\n  provider: {ws.config.provider}")


@app.command()
def ingest(
    directory: Annotated[Path, typer.Argument(help="Directory of source documents.")],
    workspace: WS = Path("."),
    provider: Annotated[Optional[str], typer.Option(help="Override the configured provider.")] = None,
    jobs: Annotated[Optional[int], typer.Option(help="Concurrent provider calls.")] = None,
    as_json: JSON = False,
    wait: WAIT = 0.0,
) -> None:
    """Register a corpus and compile it (incrementally if already compiled)."""
    with _errors():
        report = api.ingest(directory, workspace, provider=provider, jobs=jobs, wait=wait)
    _print_report(report, as_json)


@app.command()
def check(
    directory: Annotated[Optional[Path], typer.Argument(help="Source directory (default: configured).")] = None,
    workspace: WS = Path("."),
    as_json: JSON = False,
) -> None:
    """Detect new, modified, deleted, renamed and unchanged sources (content hashes)."""
    with _open(workspace) as kb:
        ch = kb.check(directory)
    if as_json:
        _emit_json(ch.to_dict())
        return
    console.print(f"Source root: {ch.root}")
    console.print(f"New sources:       {len(ch.new)}")
    for p in ch.new:
        console.print(f"  [green]+[/green] {p}")
    console.print(f"Modified sources:  {len(ch.modified)}")
    for p in ch.modified:
        console.print(f"  [yellow]~[/yellow] {p}")
    console.print(f"Deleted sources:   {len(ch.deleted)}")
    for p in ch.deleted:
        console.print(f"  [red]-[/red] {p}")
    console.print(f"Renamed sources:   {len(ch.renamed)}")
    for o, n in ch.renamed:
        console.print(f"  [cyan]>[/cyan] {o} -> {n}")
    console.print(f"Unchanged sources: {ch.unchanged}")
    if ch.duplicates:
        console.print(f"Identical content at several paths: {len(ch.duplicates)} group(s) (extraction is shared)")
    if ch.has_changes:
        console.print("\nRun [bold]dakc compile --dry-run[/bold] to see the affected artifacts.")


@app.command()
def compile(  # noqa: A001 - CLI verb
    workspace: WS = Path("."),
    incremental: Annotated[bool, typer.Option("--incremental/--full", help="Incremental (default) or full rebuild.")] = True,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Plan only; write nothing.")] = False,
    strategy: Annotated[Optional[str], typer.Option(help="dakc | file | full (benchmark baselines).")] = None,
    provider: Annotated[Optional[str], typer.Option(help="Override the configured provider.")] = None,
    jobs: Annotated[Optional[int], typer.Option(help="Concurrent provider calls.")] = None,
    as_json: JSON = False,
    fault: Annotated[Optional[str], typer.Option(hidden=True, help="Inject a failure, e.g. commit:100 (testing).")] = None,
    wait: WAIT = 0.0,
) -> None:
    """Compile the knowledge base: only invalidated artifacts are rebuilt."""
    if strategy is not None and strategy not in STRATEGIES:
        err.print(f"[red]error:[/red] strategy must be one of {', '.join(STRATEGIES)}")
        raise typer.Exit(2)
    with _open(workspace) as kb:
        if dry_run:
            report = kb.plan(full=not incremental, strategy=strategy, provider=provider)
        else:
            report = kb.compile(full=not incremental, strategy=strategy, provider=provider, jobs=jobs, wait=wait, fault=fault)
        _print_report(report, as_json)


@app.command()
def graph(
    workspace: WS = Path("."),
    source: Annotated[Optional[str], typer.Option("--source", "-s", help="Show everything downstream of this source (id or path) or artifact.")] = None,
    fmt: Annotated[str, typer.Option("--format", "-f", help="text | json | dot | reactflow")] = "text",
    output: Annotated[Optional[Path], typer.Option("--output", "-o", help="Write export to a file.")] = None,
    limit: Annotated[int, typer.Option(help="Max items listed per kind in text mode.")] = 25,
) -> None:
    """Inspect the dependency graph (whole graph statistics, or downstream of one node)."""
    with _open(workspace) as kb:
        if fmt == "text":
            if source is None:
                s = kb.graph_stats()
                t = Table(title="Dependency graph (HEAD)")
                t.add_column("layer")
                t.add_column("artifacts", justify="right")
                for kind, n in s["nodes"].items():
                    t.add_row(f"{LAYER.get(kind, '?')} {kind}", str(n))
                console.print(t)
                e = Table(title="Dependency edges (dependent -> dependency)")
                e.add_column("edge kind")
                e.add_column("count", justify="right")
                for kind, n in s["edges"].items():
                    e.add_row(kind, str(n))
                console.print(e)
                return
            d = kb.downstream(source)
            tree = Tree(f"[bold]{d.root}[/bold]  {d.title}")
            for kind, items in d.by_kind.items():
                branch = tree.add(f"{kind} ({len(items)})")
                for a in items[:limit]:
                    branch.add(f"{a.id}  [dim]{a.title[:70]}[/dim]")
                if len(items) > limit:
                    branch.add(f"[dim]… {len(items) - limit} more[/dim]")
            console.print(tree)
            return
        text = kb.export_graph(fmt, around=source)
    if output:
        output.write_text(text, encoding="utf-8")
        console.print(f"wrote {output}")
    else:
        sys.stdout.write(text)


@app.command(name="explain")
def explain_cmd(
    artifact: Annotated[str, typer.Argument(help="e.g. page:postgresql, entity_postgresql, a source path, a page title")],
    workspace: WS = Path("."),
    as_json: JSON = False,
    limit: Annotated[int, typer.Option(help="Max dependencies listed.")] = 30,
) -> None:
    """Explain an artifact's dependencies, dependents and staleness (or a conflict: cfl:…)."""
    if artifact.strip().startswith(("cfl:", "conflict:")):
        with _open(workspace) as kb:
            _show_conflict(kb.conflict(artifact), as_json)
        return
    with _open(workspace) as kb:
        exp = kb.explain(artifact)
    if as_json:
        _emit_json(exp.to_dict())
        return
    console.print(f"Artifact: [bold]{exp.id}[/bold]  ({exp.kind})")
    console.print(f"Title:    {exp.title}")
    if exp.version:
        console.print(f"Version:  {exp.version}")
    console.print("\nDepends on:")
    for d in exp.depends_on[:limit]:
        console.print(f"  - {d['id']}  [dim]{d['edge']}: {d['title'][:70]}[/dim]")
    if len(exp.depends_on) > limit:
        console.print(f"  … {len(exp.depends_on) - limit} more")
    if exp.upstream_sources and exp.kind != "source":
        console.print("\nUpstream sources:")
        for s in exp.upstream_sources[:limit]:
            console.print(f"  - {s['id']}  [dim]{s['path']}[/dim]")
        if len(exp.upstream_sources) > limit:
            console.print(f"  … {len(exp.upstream_sources) - limit} more")
    if exp.upstream_counts:
        console.print("  upstream totals: " + ", ".join(f"{k} {v}" for k, v in sorted(exp.upstream_counts.items(), key=lambda kv: _layer(kv[0]))))
    console.print("\nDependents:")
    for d in exp.dependents[:limit]:
        console.print(f"  - {d['id']}  [dim]{d['edge']}[/dim]")
    if exp.downstream_counts:
        console.print("  downstream totals: " + ", ".join(f"{k} {v}" for k, v in sorted(exp.downstream_counts.items(), key=lambda kv: _layer(kv[0]))))
    if exp.last_changed_build:
        console.print(f"\nLast compiled:\n  build {exp.last_changed_build}" + (f" at {exp.last_changed_at}" if exp.last_changed_at else ""))
    if exp.signatures:
        console.print("Signatures: " + ", ".join(f"{k}={v[:12]}" for k, v in exp.signatures.items()))
    colour = {"VALID": "green", "STALE": "yellow", "DELETED": "red"}.get(exp.status, "white")
    console.print(f"\nStatus:\n  [{colour}]{exp.status}[/{colour}]")
    for r in exp.reasons:
        console.print(f"  - {r}")


def _refs_note(refs: list[str]) -> str:
    """The comments written after an evidence sentence, e.g. "  refs: t:3f9a12c4"."""
    return f"  [cyan]refs: {escape(', '.join(refs))}[/cyan]" if refs else ""


@app.command()
def trace(
    artifact: Annotated[str, typer.Argument(help="A page or claim, e.g. page:postgresql or a page title")],
    workspace: WS = Path("."),
    as_json: JSON = False,
) -> None:
    """Sentence-level provenance: generated sentence -> claim -> passage -> source version -> evidence sentence."""
    from dakc.provenance.trace import ClaimTrace

    with _open(workspace) as kb:
        t = kb.trace(artifact)
    if as_json:
        _emit_json(t.to_dict())
        return
    if isinstance(t, ClaimTrace):
        console.print(f"Claim: [bold]{t.claim_id}[/bold]\n  {t.text}")
        for e in t.evidence:
            console.print(f"  <- {e.source_path} @ {e.source_raw_hash} (registered in build {e.source_version_build}), {e.passage_id}")
            for s, refs in zip(e.sentences, e.refs or [[]] * len(e.sentences)):
                console.print(f"       [dim]\"{s}\"[/dim]" + _refs_note(refs))
        return
    console.print(f"Page: [bold]{t.page_id}[/bold]  ({t.title})\n\nGenerated sentences:")
    for i, s in enumerate(t.sentences, 1):
        tag = ("-> " + ", ".join(s.cites)) if s.cites else "[yellow]uncited[/yellow]"
        bad = f"  [red]invalid: {', '.join(s.invalid)}[/red]" if s.invalid else ""
        console.print(f"  {i}. {s.sentence[:110]}  {tag}{bad}")
    console.print("\nClaims:")
    for c in t.claims:
        console.print(f"  {c.ref}  {c.claim_id}  {c.text[:100]}")
        for e in c.evidence:
            console.print(f"       <- {e.source_path} @ {e.source_raw_hash} (build {e.source_version_build}), {e.passage_id}"
                          + (f", section '{e.heading}'" if e.heading else ""))
            for s, refs in zip(e.sentences, e.refs or [[]] * len(e.sentences)):
                console.print(f"          [dim]\"{s[:140]}\"[/dim]" + _refs_note(refs))
    n = len(t.sentences)
    console.print(f"\n{n - len(t.uncited)} of {n} displayed sentences cite a claim; {len(t.invalid_markers)} invalid citation marker(s).")
    if t.dropped:
        console.print(f"Removed by strict provenance ({len(t.dropped)}):")
        for d in t.dropped[:10]:
            console.print(f"  [dim]- {d[:140]}[/dim]")


@app.command()
def duplicates(
    workspace: WS = Path("."),
    limit: Annotated[int, typer.Option(help="Max pairs listed.")] = 30,
    as_json: JSON = False,
) -> None:
    """Report claims that are likely the same fact in different words (advisory; nothing is merged)."""
    with _open(workspace) as kb:
        pairs = kb.duplicates()
        n_claims = kb.store.counts().get("claim", 0)
    if as_json:
        _emit_json([p.to_dict() for p in pairs])
        return
    console.print(f"{len(pairs)} likely duplicate pair(s) among {n_claims} claims (deterministic, conservative; nothing is merged):")
    for p in pairs[:limit]:
        console.print(f"  - {p.a_text}\n    {p.b_text}")


@app.command()
def conflicts(
    conflict: Annotated[Optional[str], typer.Argument(help="Show one finding in full: its id (cfl:…) or a unique prefix.")] = None,
    workspace: WS = Path("."),
    show_all: Annotated[bool, typer.Option("--all", help="Also list differences explained by scope, and accepted conflicts.")] = False,
    check: Annotated[bool, typer.Option("--check", help="Exit with status 1 if a conflict is open (for CI).")] = False,
    as_json: JSON = False,
) -> None:
    """Statements in different sources that cannot both be true. Advisory: nothing is changed."""
    with _open(workspace) as kb:
        if conflict:
            _show_conflict(kb.conflict(conflict), as_json)
            return
        found = kb.conflicts(scoped=show_all, accepted=show_all)
    open_ = [c for c in found if c.kind in ("conflict", "possible") and c.accepted is None]
    if as_json:
        _emit_json({"open": len(open_), "conflicts": [c.to_dict() for c in found]})
    elif not found:
        console.print("[green]No conflicts[/green] between sources at HEAD." + ("" if show_all else "  (`--all` adds scoped differences)"))
    else:
        for c in found:
            tag = ("[green]accepted[/green]" if c.accepted is not None
                   else "[red]conflict[/red]" if c.kind == "conflict"
                   else "[yellow]possible[/yellow]" if c.kind == "possible" else "[cyan]scoped[/cyan]")
            console.print(f"{tag}  [bold]{c.id}[/bold]  {escape(c.property)}")
            for v in c.values:
                where = ", ".join(sorted({loc.path for loc in v.locations}))
                console.print(f"    {escape(repr(v.value)):28}  [dim]{escape(where[:90])}[/dim]")
        possible = sum(1 for c in open_ if c.kind == "possible")
        console.print(f"\n{len(open_)} open conflict(s)" + (f", {possible} of them possible (judged by the verifier)" if possible else "")
                      + ". `dakc conflicts <id>` shows the sentences, lines and affected pages.")
    if check and open_:
        raise typer.Exit(1)


@app.command()
def links(
    path: Annotated[Optional[str], typer.Argument(help="Only links in this document or pointing at it.")] = None,
    workspace: WS = Path("."),
    to: Annotated[bool, typer.Option("--to", help="List every link pointing at PATH, working or not.")] = False,
    check: Annotated[bool, typer.Option("--check", help="Exit with status 1 if a link is broken (for CI).")] = False,
    as_json: JSON = False,
) -> None:
    """Links between documents that point at a document or heading that no longer exists.
    Advisory: nothing is changed."""
    with _open(workspace) as kb:
        if to:
            if not path:
                raise typer.BadParameter("--to needs a document path")
            into = kb.linked_from(path)
            if as_json:
                _emit_json({"path": path, "linked_from": into})
                return
            for x in into:
                console.print(f"{escape(x['path'])}:{x['line']}  [dim]{escape(x['href'])}[/dim]  {escape(x['text'])}")
            console.print(f"\n{len(into)} link(s) point at {escape(path)}.")
            return
        found = kb.links(path)
    if as_json:
        _emit_json({"broken": len(found), "links": [b.to_dict() for b in found]})
    elif not found:
        console.print("[green]No broken links[/green] between documents at HEAD.")
    else:
        for b in found:
            console.print(f"[red]broken[/red]  [bold]{escape(b.path)}:{b.line}[/bold]  [{escape(b.text)}]({escape(b.href)})")
            console.print(f"        {escape(b.reason)}")
            if b.suggestion:
                console.print(f"        [green]fix:[/green] {escape(b.suggestion)}")
        console.print(f"\n{len(found)} broken link(s). Advisory: nothing was changed.")
    if check and found:
        raise typer.Exit(1)


def _show_conflict(c, as_json: bool) -> None:
    if as_json:
        _emit_json(c.to_dict())
        return
    label = ("Accepted conflict" if c.accepted is not None else "Conflict" if c.kind == "conflict"
             else "Possible conflict" if c.kind == "possible" else "Scoped difference")
    console.print(f"[bold]{label} {c.id}[/bold]  (since build {c.since_build})")
    console.print(f"{'About:   ' if c.kind == 'possible' else 'Property:'} {escape(c.property)}")
    console.print(f"Why:      {escape(c.reason)}")
    for v in c.values:
        console.print(f"\n[bold]{v.value}[/bold]")
        for loc in v.locations:
            heading = re.sub(r"\s*\{\s*#[^}]*\}?", "", loc.heading)  # mkdocs anchors: "Title { #id }"
            heading = heading if len(heading) <= 70 else "…" + heading[-69:]
            console.print(f"  {loc.path}" + (f":{loc.line}" if loc.line else "")
                          + f"  [dim]{escape(heading)}  ({escape(loc.scope)})[/dim]")
            console.print(f"    \"{escape(loc.evidence or loc.sentence)}\"")
            if loc.evidence and loc.evidence.strip() != loc.sentence.strip():
                console.print(f"    [dim]claim: {escape(loc.sentence)}[/dim]")
    if c.affected:
        console.print("\nBuilt from these claims:")
        for kind, items in c.affected.items():
            names = ", ".join(a.title for a in items[:8]) + (f" … {len(items) - 8} more" if len(items) > 8 else "")
            console.print(f"  {kind} ({len(items)}): {names}")
    if c.accepted is not None:
        console.print(f"\nAccepted in dakc.toml: {escape(c.accepted or '(no reason given)')}")
    elif c.kind in ("conflict", "possible"):
        if c.kind == "possible":
            console.print("\n[dim]Judged by the conflict verifier (a model), not by DAKC's rules: check both statements.[/dim]")
        console.print(escape("\nResolution: edit one of the sources and compile; the conflict closes when they agree."
                             " If it is a difference of time, release or environment, say so in the text or with a"
                             " [[conflicts.scope]] rule. To keep it as is, add it to [[conflicts.accept]] in dakc.toml."))


@app.command()
def status(workspace: WS = Path("."), as_json: JSON = False) -> None:
    """Head build, artifact counts and staleness against the current sources."""
    with _open(workspace) as kb:
        st = kb.status()
    if as_json:
        _emit_json(st.to_dict())
        return
    console.print(f"Workspace: {st.workspace}\nSources:   {st.source_root}\nProvider:  {st.provider}\nHEAD:      build {st.head or '-'}")
    console.print("Artifacts: " + ", ".join(f"{k} {v}" for k, v in sorted(st.counts.items(), key=lambda kv: _layer(kv[0]))))
    if st.conflicts_open:
        console.print(f"[red]Conflicts[/red]: {st.conflicts_open} open (`dakc conflicts`)")
    if st.links_broken:
        console.print(f"[red]Links[/red]: {st.links_broken} broken (`dakc links`)")
    if st.up_to_date:
        console.print("[green]Up to date[/green]: no source changes since HEAD.")
        return
    if any(st.source_changes[k] for k in ("new", "modified", "deleted", "renamed")):
        console.print(f"[yellow]Stale[/yellow]: {st.source_changes}")
        if st.stale:
            console.print("  artifacts downstream of changed sources: " + ", ".join(f"{k} {v}" for k, v in st.stale.items()))
        if st.new_sources:
            console.print(f"  plus {st.new_sources} new source(s) not yet compiled")
    for reason in st.outdated:
        console.print(f"[yellow]Outdated[/yellow]: {escape(reason)}")
    console.print("  `dakc compile --dry-run` shows exactly what a compile would redo.")


@app.command()
def builds(workspace: WS = Path("."), limit: Annotated[int, typer.Option()] = 20, as_json: JSON = False) -> None:
    """List compilation builds (audit log)."""
    with _open(workspace) as kb:
        records, head = kb.builds(limit), kb.head
    if as_json:
        _emit_json([b.to_dict() for b in records])
        return
    t = Table()
    for col in ("build", "strategy", "status", "finished", "llm calls", "versions", "error"):
        t.add_column(col)
    for b in records:
        s = b.stats or {}
        t.add_row(
            f"{b.id}{' (HEAD)' if b.id == head else ''}", b.strategy, b.status, b.finished_at or "-",
            str(s.get("usage", {}).get("llm_calls", "")), str(s.get("changes", {}).get("artifact_versions_written", "")),
            (b.error or "").split(": ", 1)[-1][:40],
        )
    console.print(t)


@app.command()
def diff(
    build_a: Annotated[int, typer.Argument(help="Older build")],
    build_b: Annotated[Optional[int], typer.Argument(help="Newer build (default HEAD)")] = None,
    workspace: WS = Path("."),
    limit: Annotated[int, typer.Option()] = 20,
    as_json: JSON = False,
) -> None:
    """Show which artifacts differ between two builds."""
    with _open(workspace) as kb:
        d = kb.diff(build_a, build_b)
    if as_json:
        _emit_json(d.changes)
        return
    for kind, items in d.changes.items():
        console.print(f"[bold]{kind}[/bold]: {len(items)}")
        for aid, change in items[:limit]:
            mark = {"created": "[green]+[/green]", "deleted": "[red]-[/red]", "updated": "[yellow]~[/yellow]"}[change]
            console.print(f"  {mark} {aid}")
        if len(items) > limit:
            console.print(f"  … {len(items) - limit} more")
    if not d.changes:
        console.print("No differences.")


@app.command()
def sources(workspace: WS = Path("."), as_json: JSON = False) -> None:
    """The configured sources: type, path prefix, location and documents at HEAD."""
    with _open(workspace) as kb:
        infos = kb.sources()
    if as_json:
        _emit_json([i.to_dict() for i in infos])
        return
    table = Table("Source", "Type", "Prefix", "Location", "Documents", "Capabilities")
    for i in infos:
        table.add_row(i.name, i.type, i.prefix or "(none)", i.location, str(i.documents), ", ".join(i.capabilities) or "-")
    console.print(table)


@app.command()
def publish(
    workspace: WS = Path("."),
    name: Annotated[Optional[str], typer.Argument(help="One publisher (default: all configured).")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Only count pending work.")] = False,
    reset: Annotated[bool, typer.Option("--reset", help="Forget what was published and send everything again.")] = False,
    as_json: JSON = False,
    wait: WAIT = 0.0,
) -> None:
    """Send what changed since each publisher's last acknowledged state (retries failures)."""
    with _open(workspace) as kb:
        results = kb.publish(name, dry_run=dry_run, reset=reset, wait=wait)
    if as_json:
        _emit_json([r.to_dict() for r in results])
        return
    if not results:
        console.print("No publishers configured: add a [[publishers]] table to dakc.toml.")
    _print_publications([r.to_dict() for r in results])
    if any(not r.ok for r in results):
        raise typer.Exit(1)


@app.command()
def review(
    workspace: WS = Path("."),
    base: Annotated[str, typer.Option("--base", help="Git ref to compare with, e.g. origin/main or a commit sha.")] = "origin/main",
    state_dir: Annotated[Optional[Path], typer.Option(help="Where the base build is kept between runs (cache it in CI).")] = None,
    price: Annotated[Optional[list[str]], typer.Option("--price", help="LABEL:INPUT:OUTPUT in USD per million tokens; repeatable.")] = None,
    fmt: Annotated[str, typer.Option("--format", help="markdown | json")] = "markdown",
    output: Annotated[Optional[Path], typer.Option("--output", "-o", help="Write to this file instead of stdout.")] = None,
    json_output: Annotated[Optional[Path], typer.Option("--json-output", help="Also write the result as JSON to this file.")] = None,
    limit: Annotated[int, typer.Option(help="Items listed per section.")] = 25,
) -> None:
    """Knowledge diff against a git ref, for pull requests: facts added and retracted, pages affected, LLM calls and cost.
    Uses the free deterministic provider, so it never calls or pays a model."""
    from dakc.review import run

    with _errors():
        r, text = run(workspace, base, state_dir=state_dir, prices=price, fmt=fmt, limit=limit)
    if json_output is not None:
        json_output.write_text(json.dumps(r.to_dict(), indent=1, ensure_ascii=False), encoding="utf-8")
    if output is not None:
        output.write_text(text, encoding="utf-8")
    else:
        typer.echo(text)


@app.command(name="rollback")
def rollback_cmd(
    workspace: WS = Path("."),
    to: Annotated[Optional[int], typer.Option("--to", help="Committed build to restore (default: the one before HEAD).")] = None,
    wait: WAIT = 0.0,
) -> None:
    """Restore the knowledge base to an earlier committed build (sources are untouched)."""
    with _open(workspace) as kb:
        r = kb.rollback(to, wait=wait)
    console.print(f"Restored state of build {r.restored_build} as build {r.new_build}.")
    for line in projection_lines(r.projection):
        console.print(line, markup=False, highlight=False)
    _print_publications(r.publications)
    console.print("Note: sources on disk are unchanged; `dakc status` shows what differs from them.")


@app.command()
def verify(
    workspace: WS = Path("."),
    against_full: Annotated[bool, typer.Option("--against-full", help="Rebuild from scratch in a temp store and compare.")] = False,
    allow_cost: Annotated[bool, typer.Option("--allow-cost", help="Permit a full rebuild with a paid provider.")] = False,
    as_json: JSON = False,
) -> None:
    """Check invariants: edges, acyclicity, provenance, projection; optionally equality with a full rebuild."""
    with _open(workspace) as kb:
        try:
            res = kb.verify(against_full=against_full, allow_cost=allow_cost)
        except CostNotAllowed as exc:
            err.print("[red]error:[/red] --against-full with an LLM provider re-pays every call; add --allow-cost")
            raise typer.Exit(2) from exc
    if as_json:
        _emit_json(res.to_dict())
        raise typer.Exit(0 if res.ok else 1)
    for name, passed in res.checks.items():
        console.print(f"{'[green]PASS[/green]' if passed else '[red]FAIL[/red]'}  {name}")
        for p in res.problems.get(name, [])[:5]:
            console.print(f"        {p}")
    cov = res.metrics.get("citation_coverage")
    if cov:
        console.print(f"INFO  {cov['cited']} of {cov['sentences']} generated page sentences cite a claim "
                      f"({100 * cov['coverage']:.1f}%); `dakc trace PAGE` shows which do not")
    if res.equal_to_full_rebuild is not None:
        detail = res.full_rebuild_diff or {}
        what = "content and structure" if detail.get("content_compared") else "structure (sources/passages)"
        console.print(f"{'[green]PASS[/green]' if res.equal_to_full_rebuild else '[red]FAIL[/red]'}  equal to a full rebuild ({what})")
        if not res.equal_to_full_rebuild:
            console.print(f"        {detail.get('by_kind')}")
    raise typer.Exit(0 if res.ok else 1)


@app.command()
def serve(
    workspace: WS = Path("."),
    host: Annotated[str, typer.Option()] = "127.0.0.1",
    port: Annotated[int, typer.Option()] = 8765,
) -> None:
    """Serve the web UI and the read-only REST API on this machine (requires the 'api' extra)."""
    _require_loopback(host)
    try:
        import uvicorn
    except ImportError as exc:
        err.print("[red]error:[/red] install the API extra: pip install 'dakc\\[api]'")
        raise typer.Exit(2) from exc
    from dakc.api import create_app

    with _errors():
        app_ = create_app(workspace.resolve())
    shown = f"[{host}]" if ":" in host else host
    console.print(f"DAKC UI: http://{shown}:{port}/   (read-only; Ctrl-C to stop)")
    uvicorn.run(app_, host=host, port=port, log_level="warning")


@app.command(name="mcp")
def mcp_cmd(
    workspace: WS = Path("."),
    transport: Annotated[str, typer.Option(help="stdio (local agents) | streamable-http")] = "stdio",
    host: Annotated[str, typer.Option(help="Bind address for streamable-http (loopback only).")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port for streamable-http.")] = 8766,
    read_only: Annotated[bool, typer.Option("--read-only", help="Expose inspection tools only (no compile or rollback).")] = False,
    max_llm_calls: Annotated[int, typer.Option(help="Refuse an LLM-provider compile planned to exceed this many calls.")] = 100,
) -> None:
    """Serve the knowledge base to AI agents over MCP (requires the 'mcp' extra)."""
    if importlib.util.find_spec("mcp") is None:
        err.print("[red]error:[/red] install the MCP extra: pip install 'dakc\\[mcp]'")
        raise typer.Exit(2)
    if transport not in ("stdio", "streamable-http"):
        err.print("[red]error:[/red] transport must be stdio or streamable-http")
        raise typer.Exit(2)
    if transport == "streamable-http":
        _require_loopback(host)  # the SDK then also rejects requests whose Host header is not local
    from dakc.mcp_server import create_server

    with _errors():
        server = create_server(workspace, read_only=read_only, max_llm_calls=max_llm_calls)
    # stdio: stdout carries the protocol, so nothing else may be printed to it.
    if transport == "stdio":
        server.run("stdio")
    else:
        server.run("streamable-http", host=host, port=port)


@app.command()
def watch(
    workspace: WS = Path("."),
    debounce: Annotated[float, typer.Option(help="Seconds of quiet before compiling.")] = 1.0,
) -> None:
    """Recompile incrementally whenever sources change (requires the 'watch' extra)."""
    try:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer
    except ImportError as exc:
        err.print("[red]error:[/red] install the watch extra: pip install 'dakc\\[watch]'")
        raise typer.Exit(2) from exc
    import threading
    import time

    dirty = threading.Event()

    class Handler(FileSystemEventHandler):
        def on_any_event(self, event):  # noqa: ANN001
            if not event.is_directory:
                dirty.set()

    def build(kb: KnowledgeBase) -> None:
        try:
            with _errors():
                _print_report(kb.compile(), as_json=False)
        except typer.Exit:
            pass  # reported; keep watching

    with _open(workspace) as kb:
        obs = Observer()
        roots = kb.workspace.source_set().filesystem_roots()
        if not roots:
            err.print("[red]error:[/red] no filesystem source to watch; compile on a schedule instead (e.g. cron or CI)")
            raise typer.Exit(2)
        for root in roots:
            obs.schedule(Handler(), str(root), recursive=True)
        obs.start()
        console.print(f"Watching {', '.join(map(str, roots))} (Ctrl-C to stop). "
                      "Other kinds of source are checked whenever a compile runs.")
        build(kb)
        try:
            while True:
                if dirty.wait(0.5):
                    time.sleep(debounce)
                    dirty.clear()
                    build(kb)
        except KeyboardInterrupt:
            pass
        finally:
            obs.stop()
            obs.join()


def main() -> None:  # pragma: no cover
    app()
