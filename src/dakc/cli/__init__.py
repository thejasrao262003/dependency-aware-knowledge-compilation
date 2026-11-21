"""``dakc`` command-line interface."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table
from rich.tree import Tree

from dakc.artifacts import LAYER, kind_of
from dakc.compiler import STRATEGIES, Compiler
from dakc.compiler.projection import sync_projection
from dakc.graph import build_graph, downstream, export, resolve_id, stats, titles
from dakc.invalidation import render
from dakc.provenance import changed_sources, explain
from dakc.transactions import FaultInjector, InjectedFault, RollbackError, rollback, state_digest
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


def _ws(path: Path) -> Workspace:
    try:
        return Workspace.load(path)
    except FileNotFoundError as exc:
        err.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(2) from exc


def _emit_json(obj) -> None:
    sys.stdout.write(json.dumps(obj, indent=1, sort_keys=True, default=str) + "\n")


def _compile(ws: Workspace, strategy: str, dry_run: bool, provider: str | None, jobs: int | None, as_json: bool, fault: str | None = None) -> int:
    store = ws.store()
    bundle = ws.provider(provider)
    comp = Compiler(
        store, bundle, ws.source_root,
        aliases=ws.config.alias_map, config_fingerprint=ws.config.fingerprint,
        strategy=strategy, jobs=jobs or ws.config.jobs, dry_run=dry_run,
        fault=FaultInjector(fault), exclude=ws.excluded,
    )
    before = state_digest(store)
    try:
        report, _ = comp.run()
    except InjectedFault as exc:
        after = state_digest(store)
        err.print(f"[red]compilation failed:[/red] {exc}")
        err.print(f"previous valid compilation preserved: {'yes' if before == after else '[red]NO[/red]'} (HEAD = build {store.head})")
        return 1
    except Exception as exc:  # provider/network/parse failures
        after = state_digest(store)
        err.print(f"[red]compilation failed:[/red] {type(exc).__name__}: {exc}")
        err.print(f"previous valid compilation preserved: {'yes' if before == after else '[red]NO[/red]'} (HEAD = build {store.head})")
        return 1
    if report.status in ("committed", "noop") or not ws.output_root.exists():
        if not dry_run:
            report.projection = sync_projection(store, ws.output_root, ws.state_dir)
    if as_json:
        _emit_json(report.to_dict())
    else:
        console.print(render(report))
        if report.status == "noop":
            console.print("\nNothing changed. Knowledge base is up to date.")
    return 0


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
    provider: Annotated[str, typer.Option(help="heuristic | mock | openai | anthropic")] = "heuristic",
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
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Register a corpus and compile it (incrementally if already compiled)."""
    if not directory.is_dir():
        err.print(f"[red]error:[/red] {directory} is not a directory")
        raise typer.Exit(2)
    root = workspace.resolve()
    if not (root / CONFIG_NAME).exists():
        try:
            rel = directory.resolve().relative_to(root).as_posix()
        except ValueError:
            rel = str(directory.resolve())
        Workspace.init(root, source_dir=rel, provider=provider or "heuristic")
    ws = _ws(root)
    if ws.source_root != directory.resolve():
        err.print(f"[yellow]note:[/yellow] workspace source_dir is {ws.source_root}; compiling that directory")
    raise typer.Exit(_compile(ws, "dakc", False, provider, jobs, as_json))


@app.command()
def check(
    directory: Annotated[Optional[Path], typer.Argument(help="Source directory (default: configured).")] = None,
    workspace: WS = Path("."),
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Detect new, modified, deleted, renamed and unchanged sources (content hashes)."""
    ws = _ws(workspace)
    store = ws.store()
    root = directory.resolve() if directory else ws.source_root
    sr, changed = changed_sources(store, root, ws.excluded)
    reg = store.load_sources()
    data = {
        "new": sr.new,
        "modified": [p for _, p in sr.modified],
        "deleted": [reg[s].path for s in sr.deleted],
        "renamed": [[o, n] for _, o, n in sr.renamed],
        "unchanged": len(sr.unchanged),
        "duplicates": sr.duplicates,
    }
    if as_json:
        _emit_json(data)
        return
    console.print(f"Source root: {root}")
    console.print(f"New sources:       {len(sr.new)}")
    for p in sr.new:
        console.print(f"  [green]+[/green] {p}")
    console.print(f"Modified sources:  {len(sr.modified)}")
    for _, p in sr.modified:
        console.print(f"  [yellow]~[/yellow] {p}")
    console.print(f"Deleted sources:   {len(sr.deleted)}")
    for s in sr.deleted:
        console.print(f"  [red]-[/red] {reg[s].path}")
    console.print(f"Renamed sources:   {len(sr.renamed)}")
    for _, o, n in sr.renamed:
        console.print(f"  [cyan]>[/cyan] {o} -> {n}")
    console.print(f"Unchanged sources: {len(sr.unchanged)}")
    if sr.duplicates:
        console.print(f"Identical content at several paths: {len(sr.duplicates)} group(s) (extraction is shared)")
    if changed:
        console.print("\nRun [bold]dakc compile --dry-run[/bold] to see the affected artifacts.")


@app.command()
def compile(  # noqa: A001 - CLI verb
    workspace: WS = Path("."),
    incremental: Annotated[bool, typer.Option("--incremental/--full", help="Incremental (default) or full rebuild.")] = True,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Plan only; write nothing.")] = False,
    strategy: Annotated[Optional[str], typer.Option(help="dakc | file | full (benchmark baselines).")] = None,
    provider: Annotated[Optional[str], typer.Option(help="Override the configured provider.")] = None,
    jobs: Annotated[Optional[int], typer.Option(help="Concurrent provider calls.")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
    fault: Annotated[Optional[str], typer.Option(hidden=True, help="Inject a failure, e.g. commit:100 (testing).")] = None,
) -> None:
    """Compile the knowledge base: only invalidated artifacts are rebuilt."""
    ws = _ws(workspace)
    strat = strategy or ("dakc" if incremental else "full")
    if strat not in STRATEGIES:
        err.print(f"[red]error:[/red] strategy must be one of {', '.join(STRATEGIES)}")
        raise typer.Exit(2)
    raise typer.Exit(_compile(ws, strat, dry_run, provider, jobs, as_json, fault))


@app.command()
def graph(
    workspace: WS = Path("."),
    source: Annotated[Optional[str], typer.Option("--source", "-s", help="Show everything downstream of this source (id or path) or artifact.")] = None,
    fmt: Annotated[str, typer.Option("--format", "-f", help="text | json | dot | reactflow")] = "text",
    output: Annotated[Optional[Path], typer.Option("--output", "-o", help="Write export to a file.")] = None,
    limit: Annotated[int, typer.Option(help="Max items listed per kind in text mode.")] = 25,
) -> None:
    """Inspect the dependency graph (whole graph statistics, or downstream of one node)."""
    ws = _ws(workspace)
    store = ws.store()
    if source is None:
        if fmt == "text":
            s = stats(store)
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
        text = export(build_graph(store, with_titles=True), fmt)
    else:
        root = resolve_id(store, source)
        if root is None:
            err.print(f"[red]error:[/red] no source or artifact matches {source!r}")
            raise typer.Exit(2)
        depth = downstream(store, root)
        if fmt == "text":
            names = titles(store, list(depth))
            label = names.get(root, root)
            tree = Tree(f"[bold]{root}[/bold]  {label}")
            by_kind: dict[str, list[str]] = {}
            for i in depth:
                if i != root:
                    by_kind.setdefault(kind_of(i), []).append(i)
            for kind in sorted(by_kind, key=lambda k: LAYER.get(k, 9)):
                ids = sorted(by_kind[kind])
                branch = tree.add(f"{kind} ({len(ids)})")
                for i in ids[:limit]:
                    branch.add(f"{i}  [dim]{names.get(i, '')[:70]}[/dim]")
                if len(ids) > limit:
                    branch.add(f"[dim]… {len(ids) - limit} more[/dim]")
            console.print(tree)
            return
        text = export(build_graph(store, set(depth), with_titles=True), fmt)
    if output:
        output.write_text(text, encoding="utf-8")
        console.print(f"wrote {output}")
    else:
        sys.stdout.write(text)


@app.command(name="explain")
def explain_cmd(
    artifact: Annotated[str, typer.Argument(help="e.g. page:postgresql, entity_postgresql, a source path, a page title")],
    workspace: WS = Path("."),
    as_json: Annotated[bool, typer.Option("--json")] = False,
    limit: Annotated[int, typer.Option(help="Max dependencies listed.")] = 30,
) -> None:
    """Explain an artifact's dependencies, dependents and staleness."""
    ws = _ws(workspace)
    store = ws.store()
    aid = resolve_id(store, artifact) or artifact
    _, changed = changed_sources(store, ws.source_root, ws.excluded)
    exp = explain(store, aid, changed)
    if as_json:
        _emit_json(exp.to_dict())
        return
    if exp.status == "UNKNOWN":
        err.print(f"[red]error:[/red] no artifact matches {artifact!r}")
        raise typer.Exit(2)
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
        console.print("  upstream totals: " + ", ".join(f"{k} {v}" for k, v in sorted(exp.upstream_counts.items(), key=lambda kv: LAYER.get(kv[0], 9))))
    console.print("\nDependents:")
    for d in exp.dependents[:limit]:
        console.print(f"  - {d['id']}  [dim]{d['edge']}[/dim]")
    if exp.downstream_counts:
        console.print("  downstream totals: " + ", ".join(f"{k} {v}" for k, v in sorted(exp.downstream_counts.items(), key=lambda kv: LAYER.get(kv[0], 9))))
    if exp.last_changed_build:
        console.print(f"\nLast compiled:\n  build {exp.last_changed_build}" + (f" at {exp.last_changed_at}" if exp.last_changed_at else ""))
    if exp.signatures:
        console.print("Signatures: " + ", ".join(f"{k}={v[:12]}" for k, v in exp.signatures.items()))
    colour = {"VALID": "green", "STALE": "yellow", "DELETED": "red"}.get(exp.status, "white")
    console.print(f"\nStatus:\n  [{colour}]{exp.status}[/{colour}]")
    for r in exp.reasons:
        console.print(f"  - {r}")


@app.command()
def status(workspace: WS = Path(".")) -> None:
    """Head build, artifact counts and staleness against the current sources."""
    ws = _ws(workspace)
    store = ws.store()
    counts = store.counts()
    console.print(f"Workspace: {ws.root}\nSources:   {ws.source_root}\nProvider:  {ws.config.provider}\nHEAD:      build {store.head or '-'}")
    console.print("Artifacts: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items(), key=lambda kv: LAYER.get(kv[0], 9))))
    sr, changed = changed_sources(store, ws.source_root, ws.excluded)
    if not sr.has_changes:
        console.print("[green]Up to date[/green]: no source changes since HEAD.")
    else:
        from dakc.provenance import stale_artifacts

        stale = stale_artifacts(store, changed)
        console.print(f"[yellow]Stale[/yellow]: {sr.summary()}")
        console.print("  artifacts downstream of changed sources: " + ", ".join(f"{k} {len(v)}" for k, v in sorted(stale.items(), key=lambda kv: LAYER.get(kv[0], 9))))
        if sr.new:
            console.print(f"  plus {len(sr.new)} new source(s) not yet compiled")


@app.command()
def builds(workspace: WS = Path("."), limit: Annotated[int, typer.Option()] = 20) -> None:
    """List compilation builds (audit log)."""
    ws = _ws(workspace)
    store = ws.store()
    t = Table()
    for col in ("build", "strategy", "status", "finished", "llm calls", "versions", "error"):
        t.add_column(col)
    head = store.head
    for b in store.builds(limit):
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
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show which artifacts differ between two builds."""
    ws = _ws(workspace)
    store = ws.store()
    d = store.diff_builds(build_a, build_b or store.head)
    if as_json:
        _emit_json(d)
        return
    for kind in sorted(d, key=lambda k: LAYER.get(k, 9)):
        items = d[kind]
        console.print(f"[bold]{kind}[/bold]: {len(items)}")
        for aid, change in items[:limit]:
            mark = {"created": "[green]+[/green]", "deleted": "[red]-[/red]", "updated": "[yellow]~[/yellow]"}[change]
            console.print(f"  {mark} {aid}")
        if len(items) > limit:
            console.print(f"  … {len(items) - limit} more")
    if not d:
        console.print("No differences.")


@app.command(name="rollback")
def rollback_cmd(
    workspace: WS = Path("."),
    to: Annotated[Optional[int], typer.Option("--to", help="Committed build to restore (default: the one before HEAD).")] = None,
) -> None:
    """Restore the knowledge base to an earlier committed build (sources are untouched)."""
    ws = _ws(workspace)
    store = ws.store()
    if to is None:
        committed = [b.id for b in store.builds() if b.status == "committed" and b.id < store.head]
        if not committed:
            err.print("[red]error:[/red] no earlier committed build to roll back to")
            raise typer.Exit(2)
        to = committed[0]
    try:
        new_id = rollback(store, to)
    except RollbackError as exc:
        err.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(2) from exc
    proj = sync_projection(store, ws.output_root, ws.state_dir)
    console.print(f"Restored state of build {to} as build {new_id}. Wiki files written: {proj['written']}, deleted: {proj['deleted']}.")
    console.print("Note: sources on disk are unchanged; `dakc status` shows what differs from them.")


@app.command()
def verify(
    workspace: WS = Path("."),
    against_full: Annotated[bool, typer.Option("--against-full", help="Rebuild from scratch in a temp store and compare.")] = False,
    allow_cost: Annotated[bool, typer.Option("--allow-cost", help="Permit a full rebuild with a paid provider.")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Check invariants: edges, acyclicity, provenance, projection; optionally equality with a full rebuild."""
    from dakc.provenance.verify import verify_against_full, verify_store

    ws = _ws(workspace)
    store = ws.store()
    res = verify_store(store, ws.output_root, ws.state_dir)
    out = {"checks": res.checks, "problems": res.problems}
    ok = res.ok
    if against_full:
        bundle = ws.provider()
        if not bundle.free and not allow_cost:
            err.print("[red]error:[/red] --against-full with an LLM provider re-pays every call; add --allow-cost")
            raise typer.Exit(2)
        equal, detail = verify_against_full(store, ws.source_root, bundle, ws.config.alias_map, ws.excluded)
        out["equal_to_full_rebuild"] = equal
        out["full_rebuild_diff"] = detail
        ok = ok and equal
    if as_json:
        _emit_json(out)
    else:
        for name, passed in res.checks.items():
            console.print(f"{'[green]PASS[/green]' if passed else '[red]FAIL[/red]'}  {name}")
            for p in res.problems.get(name, [])[:5]:
                console.print(f"        {p}")
        if against_full:
            what = "content and structure" if out["full_rebuild_diff"]["content_compared"] else "structure (sources/passages)"
            console.print(f"{'[green]PASS[/green]' if out['equal_to_full_rebuild'] else '[red]FAIL[/red]'}  equal to a full rebuild ({what})")
            if not out["equal_to_full_rebuild"]:
                console.print(f"        {out['full_rebuild_diff']['by_kind']}")
    raise typer.Exit(0 if ok else 1)


@app.command()
def serve(
    workspace: WS = Path("."),
    host: Annotated[str, typer.Option()] = "127.0.0.1",
    port: Annotated[int, typer.Option()] = 8765,
) -> None:
    """Serve the read-only REST API (requires the 'api' extra)."""
    try:
        import uvicorn
    except ImportError as exc:
        err.print("[red]error:[/red] install the API extra: pip install 'dakc[api]'")
        raise typer.Exit(2) from exc
    from dakc.api import create_app

    uvicorn.run(create_app(workspace.resolve()), host=host, port=port)


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
        err.print("[red]error:[/red] install the watch extra: pip install 'dakc[watch]'")
        raise typer.Exit(2) from exc
    import threading
    import time

    ws = _ws(workspace)
    dirty = threading.Event()

    class Handler(FileSystemEventHandler):
        def on_any_event(self, event):  # noqa: ANN001
            if not event.is_directory:
                dirty.set()

    obs = Observer()
    obs.schedule(Handler(), str(ws.source_root), recursive=True)
    obs.start()
    console.print(f"Watching {ws.source_root} (Ctrl-C to stop)")
    _compile(ws, "dakc", False, None, None, False)
    try:
        while True:
            if dirty.wait(0.5):
                time.sleep(debounce)
                dirty.clear()
                _compile(ws, "dakc", False, None, None, False)
    except KeyboardInterrupt:
        pass
    finally:
        obs.stop()
        obs.join()


def main() -> None:  # pragma: no cover
    app()
