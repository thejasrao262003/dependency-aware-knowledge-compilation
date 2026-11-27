"""Invalidation planning and plan/report rendering.

A plan is a dry run of the compiler: every deterministic layer (scan,
segmentation, cache lookup, entity resolution, signatures) is evaluated
exactly. Provider calls are executed only for providers that are free and
deterministic. For paid/LLM providers the plan counts the extraction calls
it *would* make exactly, and estimates the facts in new text with the free
heuristic extractor, so page and summary counts are an estimate. Such plans
are labelled ``exact=False`` and are never presented as exact.
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
        f"Summaries:                  regenerate {n('regenerate_summary')}, re-render {n('rerender_summary')},"
        f" delete {n('delete_summary')}, checked & unchanged {n('cutoff_summary')}"
        + (f"   (sections: regenerate {n('regenerate_section')}, reused {n('cutoff_section')})"
           if n("regenerate_section") or n("cutoff_section") else ""),
        f"Index:                      {'re-render' if n('rerender_index') else 'unchanged'}",
        f"Conflicts:                  {c.get('conflicts_open', 0)} open"
        + (f"   (new {n('conflict_found')}, resolved {n('conflict_resolved')})" if n("conflict_found") or n("conflict_resolved") else "")
        + ("   `dakc conflicts` lists them" if c.get("conflicts_open") else ""),
    ]
    if c.get("links_broken") or n("link_broken") or n("link_fixed"):
        lines.append(f"Links:                      {c.get('links_broken', 0)} broken"
                     + (f"   (new {n('link_broken')}, fixed {n('link_fixed')})" if n("link_broken") or n("link_fixed") else "")
                     + ("   `dakc links` lists them" if c.get("links_broken") else ""))
    if "conflict_candidates" in c:  # the conflict verifier is on (RFC 0002, phase 2)
        judged = (f"{report.planned_llm_calls.get('verify', 0)} call(s) planned" if report.dry_run and "verify" in report.planned_llm_calls
                  else f"{c.get('conflict_pairs_checked', 0)} pair(s) judged")
        lines.append(f"Conflict verifier:          {judged}, {c.get('possible_conflicts_open', 0)} possible conflict(s) open"
                     + (f", {c['conflict_pairs_unchecked']} pair(s) left for the next compile" if c.get("conflict_pairs_unchecked") else ""))
    lines += [
        "",
        "Planned Operations:" if report.dry_run else "Operations:",
        f"- Re-extract {n('reextract')} passage(s)",
        f"- Reconcile {n('reconcile_entity')} entit{'y' if n('reconcile_entity') == 1 else 'ies'}",
        f"- Regenerate {n('regenerate_page')} page(s), re-render {n('rerender_page')}",
        f"- Regenerate {n('regenerate_summary')} summar{'y' if n('regenerate_summary') == 1 else 'ies'},"
        f" re-render {n('rerender_summary')}",
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
        qual = "exact" if report.exact else "estimate"
        lines.append(f"Estimated LLM Calls: {est} ({qual})")
    else:
        lines.append(f"LLM Calls: {u.llm_calls}   Provider calls: {u.calls}   Tokens in/out: {u.input_tokens}/{u.output_tokens}")
        if report.changes:
            ch = report.changes
            lines.append(
                f"Artifact versions written: {ch.get('artifact_versions_written', 0)}, deleted: {ch.get('artifacts_deleted', 0)}"
            )
        if report.projection:
            lines += projection_lines(report.projection)
    for note in report.notes:
        lines.append(f"note: {note}")
    if report.dry_run:
        lines += ["", "No files modified."]
    return "\n".join(lines)


def projection_lines(p: dict, limit: int = 10) -> list[str]:
    """The wiki file operations of a compile or rollback, naming the repaired files."""
    lines = [f"Wiki files written: {p.get('written', 0)}, deleted: {p.get('deleted', 0)}"
             + (f", repaired: {p['repaired']} (edited or deleted by hand; restored from the store)" if p.get("repaired") else "")]
    paths = p.get("repaired_paths", [])
    lines += [f"  repaired {rel}" for rel in paths[:limit]]
    if len(paths) > limit:
        lines.append(f"  … {len(paths) - limit} more")
    return lines
