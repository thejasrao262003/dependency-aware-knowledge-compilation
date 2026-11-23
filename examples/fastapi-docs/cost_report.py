"""Render results/cost.json (from cost.py) as results/cost.md."""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def money(x: float) -> str:
    return f"${x:,.2f}" if x >= 0.995 else f"${x:,.3f}"


def main() -> None:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "results" / "cost.json"
    d = json.loads(src.read_text(encoding="utf-8"))
    w, t, base = d["window"], d["totals"], d["initial_build"]
    p0 = d["prices"][0]
    model = p0["label"]
    base_calls = sum(base["spent"]["calls"].values())
    lines = [
        "# What keeping FastAPI's docs knowledge current costs, with a real model",
        "",
        f"_Measured by `examples/fastapi-docs/cost.py` and rendered by `cost_report.py` from `results/cost.json` "
        f"({d['generated']}, DAKC {d['dakc_version']})._",
        "",
        f"- **Corpus:** FastAPI's English docs at [`{d['source']['base'][:8]}`](https://github.com/fastapi/fastapi/commit/{d['source']['base']}): "
        f"{base['counts'].get('source', 0)} documents, {base['counts'].get('passage', 0):,} passages.",
        f"- **History:** the {w['commits']} commits that changed the docs between {w['first']} and {w['last']} "
        f"({w['days']} days, about {w['commits_per_month']:.0f} a month at that pace). The same commits as `replay.md`.",
        f"- **Model:** `{model}`, every call real. List prices: "
        + "; ".join(f"`{p['label']}` ${p['input']:.2f} input and ${p['output']:.2f} output per million tokens" for p in d["prices"])
        + f" (the provider's published prices on {d['generated'][:10]}).",
        "",
        "## The answer",
        "",
        f"| With `{model}` | {w['commits']} commits ({w['days']} days) | Per month at FastAPI's pace | How it was obtained |",
        "|---|---|---|---|",
        f"| **DAKC, incremental** | **{money(p0['commits_usd']['dakc'])}** | **{money(p0['per_month_usd']['dakc'])}** | measured: {t['dakc_measured']['calls']:,} real calls |",
        f"| Re-process each changed file | {money(p0['commits_usd']['file'])} | {money(p0['per_month_usd']['file'])} | estimated: {t['file_estimated']['calls']:,} calls × measured tokens per call |",
        f"| Rebuild everything per commit | {money(p0['commits_usd']['full'])} | {money(p0['per_month_usd']['full'])} | estimated: {t['full_estimated']['calls']:,} calls × measured tokens per call |",
        "",
        f"Building the knowledge base the first time is a one-off: **{money(p0['initial_build_usd'])}** "
        f"({base_calls:,} real calls, {base['seconds'] / 3600:.1f} hours at {d['jobs']} concurrent calls).",
        "",
    ]
    if len(d["prices"]) > 1:
        lines += ["At other models' list prices, using the same token counts (an approximation: tokenizers and output "
                  "lengths differ between models):", "",
                  "| Model | DAKC per month | Re-process changed files per month | Full rebuilds per month | Initial build |",
                  "|---|---|---|---|---|"]
        for p in d["prices"]:
            lines.append(f"| `{p['label']}` | {money(p['per_month_usd']['dakc'])} | {money(p['per_month_usd']['file'])} | "
                         f"{money(p['per_month_usd']['full'])} | {money(p['initial_build_usd'])} |")
        lines.append("")
    tok = d["real_tokens_per_call"]
    lines += [
        "## Method",
        "",
        "1. **Initial build.** DAKC built the knowledge base at the base commit with the real model.",
        "2. **Incremental compiles.** It then compiled each of the commits incrementally, as CI or `mkdocs serve`"
        " would.",
        "3. **Real token usage, logged.** Every provider call's real input and output tokens were logged by stage,"
        " including attempts whose build later failed and was retried.",
        "4. **The other two strategies, estimated.** Re-processing changed files and full rebuilds were not run for"
        " real: they would cost the amounts above. Their exact call counts per stage come from the deterministic"
        " replay. Each count is multiplied by the real tokens per call this run measured for that stage:",
        "",
        "| Stage | Calls measured | Input tokens per call | Output tokens per call |",
        "|---|---|---|---|",
    ]
    calls_by_stage = {s: base["spent"]["calls"][s] + sum(r["real_dakc"]["spent"]["calls"][s] for r in d["commits"]) for s in tok}
    for s, v in tok.items():
        lines.append(f"| {s} | {calls_by_stage[s]:,} | {v['input']:,.0f} | {v['output']:,.0f} |")
    lines += [
        "",
        f"- **Output tokens include the model's reasoning tokens.** `{model}` reasons before answering, and those"
        " tokens are billed as output.",
    ]
    if d["per_call_overhead_subtracted"]:
        lines.append(f"- **The calls went through a gateway** that adds a fixed {d['per_call_overhead_subtracted']} input tokens"
                     " to every call (measured). Those tokens were subtracted, so the figures reflect calling the model's"
                     " API directly.")
    lines += [
        f"- **DAKC's calls with the real model** were {t['dakc_measured']['calls']:,}. The deterministic replay predicted"
        f" {t['dakc_mock_calls']:,}: a real extractor finds somewhat different facts, so the pages it touches differ.",
        f"- **Store checks:** the store's invariants (edges, acyclicity, provenance) held after every build:"
        f" {'yes' if d['invariants_ok'] else '**no**'}. With a real model, a full rebuild is not expected to"
        " reproduce the same text, so equality with a full rebuild is checked only by the deterministic replay.",
        "",
        "## Per commit (DAKC, measured)",
        "",
        f"| # | Commit | Date | Files | Calls | Input tokens | Output tokens | Cost ({model}) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    ov = d["per_call_overhead_subtracted"]
    for r in d["commits"]:
        sp = r["real_dakc"]["spent"]
        calls = sum(sp["calls"].values())
        tin = sum(sp["input"].values()) - ov * calls
        tout = sum(sp["output"].values())
        cost = (tin * p0["input"] + tout * p0["output"]) / 1e6
        files = ", ".join(r["files"][:2]) + (f" +{len(r['files']) - 2}" if len(r["files"]) > 2 else "")
        lines.append(f"| {r['n']} | [`{r['commit'][:8]}`](https://github.com/fastapi/fastapi/commit/{r['commit']}) | {r['date']} | "
                     f"{files} | {calls} | {tin:,} | {tout:,} | {money(cost)} |")
    out = src.with_suffix(".md")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
