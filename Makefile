# Reproducible entry points. Every published number comes from one of these targets.
.PHONY: install test bench bench-verify scale equivalence report llm-bench

install:          ## locked environment (uv.lock)
	uv sync --locked --extra dev

test:
	uv run pytest

bench:            ## deterministic benchmark, 100/1,000/5,000 docs, then regenerate the docs
	uv run python -m benchmarks.run --sizes 100,1000,5000 --reps 3
	uv run python -m benchmarks.report

bench-verify:     ## re-run 100 docs; every non-timing metric must match the published results
	uv run python -m benchmarks.run --sizes 100 --reps 1 --verify-against benchmarks/results/latest.json

scale:            ## 20,000 docs, realistic length/topic distributions, subset of scenarios
	uv run python -m benchmarks.run --sizes 20000 --profile realistic \
		--scenarios S0_no_change,S1_one_modified,S3_one_deleted,S4_one_renamed,S5_one_claim_modified,S9_contradiction_then_delete \
		--out benchmarks/results/scale-realistic.json
	uv run python -m benchmarks.report

equivalence:      ## claim-equivalence evaluation (set PROVIDER_CONFIG=... to add the LLM judge)
	uv run python -m benchmarks.equivalence $(if $(PROVIDER_CONFIG),--provider-config $(PROVIDER_CONFIG),)

llm-bench:        ## real-LLM experiment; needs PROVIDER_CONFIG=path/to/provider.toml (costs provider calls)
	@test -n "$(PROVIDER_CONFIG)" || (echo "set PROVIDER_CONFIG=path/to/provider.toml" && exit 2)
	uv run python -m benchmarks.llm_experiment --provider-config $(PROVIDER_CONFIG) --out benchmarks/results/llm-experiment.json

report:
	uv run python -m benchmarks.report
