"""Invalidation planning and plan/report rendering.

A plan is a dry run of the compiler: every deterministic layer (scan,
segmentation, cache lookup, entity resolution, signatures) is evaluated
exactly. Provider calls are executed only for providers that are free and
deterministic. For paid/LLM providers the plan counts the calls it *would*
make and treats their outputs as unknown, so downstream numbers become an
upper bound derived from the current graph. Such plans are labelled
``exact=False`` and are never presented as exact.
"""

from __future__ import annotations

from dakc.compiler import BuildReport


def render(report: BuildReport) -> str:
    s = report.scan
    c = report.counts
    n = report.n
    title = "Compilation Plan" if report.dry_run else f"Compilation Report (build {report.build_id}, {report.status})"
    pages_total = c.get("pages_total", 0)
    affected_pages = n("regenerate_page") + n("rerender_page") + n("delete_page")
    lines = [
        title,
        "-" * len(title),
        f"Strategy: {report.strategy}",
        "",
        f"New Sources:        {s.get('new', 0)}",
        f"Modified Sources:   {s.get('modified', 0)}",
        f"Deleted Sources:    {s.get('deleted', 0)}",
        f"Renamed Sources:    {s.get('renamed', 0)}",
        f"Unchanged Sources:  {s.get('unchanged', 0)}",
        "",
        f"Passages to (re-)extract:   {n('reextract')}   (extraction cache hits: {report.usage.cache_hits})",
        f"Affected Claims:            {c.get('claims_touched', 0)}"
        f"   (new {n('new_claim')}, retracted {n('retract_claim')}, changed {n('claim_changed')},"
        f" provenance-only {n('claim_provenance_changed')}, reproduced unchanged {n('claim_reproduced')})",
        f"Affected Entities:          {n('reconcile_entity') + n('delete_entity')}",
        f"Affected Knowledge Pages:   {affected_pages}"
        f"   (regenerate {n('regenerate_page')}, re-render citations only {n('rerender_page')},"
        f" delete {n('delete_page')}, checked & unchanged {n('cutoff_page')})",
        f"Unaffected Knowledge Pages: {max(0, pages_total - affected_pages - n('cutoff_page'))}",
        f"Summaries:                  regenerate {n('regenerate_summary')}, delete {n('delete_summary')},"
        f" checked & unchanged {n('cutoff_summary')}",
        f"Index:                      {'re-render' if n('rerender_index') else 'unchanged'}",
        "",
        "Planned Operations:" if report.dry_run else "Operations:",
        f"- Re-extract {n('reextract')} passage(s)",
        f"- Reconcile {n('reconcile_entity')} entit{'y' if n('reconcile_entity') == 1 else 'ies'}",
        f"- Regenerate {n('regenerate_page')} page(s), re-render {n('rerender_page')}",
        f"- Update {n('regenerate_summary')} summar{'y' if n('regenerate_summary') == 1 else 'ies'}",
    ]
    if report.revisions:
        lines.append("")
        lines.append("Claim revisions (matched for history; never used to skip work):")
        for old, new, score in report.revisions[:10]:
            lines.append(f"  ~ {old!r} -> {new!r} ({score:.0f}%)")
    lines.append("")
    u = report.usage
    if report.dry_run:
        planned = sum(report.planned_llm_calls.values())
        est = u.llm_calls + planned
        qual = "exact" if report.exact else "upper bound"
        lines.append(f"Estimated LLM Calls: {est} ({qual})")
    else:
        lines.append(f"LLM Calls: {u.llm_calls}   Provider calls: {u.calls}   Tokens in/out: {u.input_tokens}/{u.output_tokens}")
        if report.changes:
            ch = report.changes
            lines.append(
                f"Artifact versions written: {ch.get('artifact_versions_written', 0)}, deleted: {ch.get('artifacts_deleted', 0)}"
            )
        if report.projection:
            p = report.projection
            lines.append(f"Wiki files written: {p.get('written', 0)}, deleted: {p.get('deleted', 0)}")
    for note in report.notes:
        lines.append(f"note: {note}")
    if report.dry_run:
        lines += ["", "No files modified."]
    return "\n".join(lines)
