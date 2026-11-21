"""Run the benchmark harness on a small corpus and assert the PRD success criteria.

These are properties, not performance numbers: they must hold at every size.
"""

import pytest

from benchmarks.run import run_size


@pytest.fixture(scope="module")
def results(tmp_path_factory):
    return run_size(80, reps=1, work=tmp_path_factory.mktemp("bench"), log=lambda m: None)


def test_zero_stale_artifacts_and_equivalence(results):
    for scenario, sc in results["scenarios"].items():
        for strategy, m in sc["strategies"].items():
            assert m["stale_artifacts_retained"] == 0, (scenario, strategy)
            assert m["equivalent_to_full_rebuild"], (scenario, strategy)


def test_dakc_never_regenerates_unchanged_branches(results):
    for scenario, sc in results["scenarios"].items():
        m = sc["strategies"]["dakc"]
        assert m["unnecessary_regenerations"] == 0, scenario
        assert m["unaffected_artifacts_modified"] == 0, scenario


def test_dakc_does_no_more_work_than_file_level_or_full(results):
    for scenario, sc in results["scenarios"].items():
        s = sc["strategies"]
        assert s["dakc"]["llm_calls"] <= s["file"]["llm_calls"] <= s["full"]["llm_calls"], scenario
        assert s["dakc"]["pages_regenerated"] <= s["file"]["pages_regenerated"], scenario


def test_idempotent_and_rename_free(results):
    assert results["scenarios"]["S0_no_change"]["strategies"]["dakc"]["status"] == "noop"
    assert results["scenarios"]["S0_no_change"]["strategies"]["dakc"]["llm_calls"] == 0
    assert results["scenarios"]["S4_one_renamed"]["strategies"]["dakc"]["llm_calls"] == 0
