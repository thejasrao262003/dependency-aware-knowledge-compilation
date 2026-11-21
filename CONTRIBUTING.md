# Contributing

Thanks for helping. DAKC is small on purpose. Its value comes from the
correctness of incremental compilation, so changes are judged on that first.

## Setup

```bash
uv venv --python 3.12
uv pip install -e '.[dev]'
uv run pytest                       # all suites, about a minute
uv run python -m benchmarks.run --sizes 100 --reps 1 --out /tmp/bench.json
```

## The one rule: incremental must equal full

Any change to the compiler, artifacts, signatures or storage must keep
`tests/regression/test_equivalence.py` green. That suite checks that, for
every benchmark scenario and a seeded sequence of random edits, an
incremental build is identical to a from-scratch full build. "Identical"
covers canonical state, content hashes and dependency edges.

If you add a new artifact kind or dependency:

1. Derive its edges from its content (`Artifact.deps()`); never write edges by hand.
2. Put everything it reads into its signature, including the provider
   fingerprint.
3. Add a scenario or random edit that exercises it, and confirm the
   regression suite still passes.
4. Run `dakc verify --against-full` on the example corpus.

## Benchmarks

Do not edit numbers in `docs/benchmark.md` by hand. Run
`python -m benchmarks.run` and then `python -m benchmarks.report`, and commit
`benchmarks/results/latest.json` with the regenerated document.

## Style

- Python 3.12+, type hints, no new runtime dependencies without discussion.
- Deterministic first: an LLM call must be the last resort, never a shortcut.
- Keep modules independently testable. Add tests with every behaviour change.

## Reporting issues

Include `dakc --version` output (or the commit), the provider used, and, when
possible, a minimal source tree that reproduces the problem. For
incorrect-invalidation bugs, the output of `dakc verify --against-full --json`
is the most useful attachment.
