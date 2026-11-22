"""Conflicting sources: both claims are kept with their own evidence; deleting a
source removes exactly the claims only it supported."""

from tests.conftest import compile_once, write_files
from tests.integration.test_compile import assert_equals_full_rebuild


def facts(store, page_id):
    return store.current_artifact(page_id)[0].rendered


def test_contradiction_then_original_source_deleted(kb, tmp_path):
    src, store = kb
    write_files(src, {
        "ops/kafka-a.md": "Kafka retains Events for 7 days.\n",
        "ops/kafka-b.md": "Kafka retains Events for 3 days.\n",
    })
    compile_once(store, src)
    page = facts(store, "page:kafka")
    assert "7 days" in page and "3 days" in page, "both conflicting claims are shown"
    assert "[^C1]: ops/kafka-b.md" in page and "[^C2]: ops/kafka-a.md" in page, "each with its own source"

    (src / "ops/kafka-a.md").unlink()
    rep = compile_once(store, src)
    page = facts(store, "page:kafka")
    assert "7 days" not in page and "3 days" in page
    assert "ops/kafka-a.md" not in page
    assert len(rep.ops["retract_claim"]) == 1
    assert_equals_full_rebuild(src, store, tmp_path)


def test_shared_claim_survives_when_one_supporting_source_is_deleted(kb, tmp_path):
    src, store = kb
    write_files(src, {
        "a.md": "Kafka retains Events for 7 days.\n",
        "b.md": "Kafka retains Events for 7 days.\n\nKafka uses ZooKeeper.\n",
        "c.md": "Kafka retains Events for 3 days.\n",
    })
    compile_once(store, src)
    (src / "a.md").unlink()
    rep = compile_once(store, src)
    page = facts(store, "page:kafka")
    assert "7 days" in page and "3 days" in page, "still supported by b.md"
    assert "a.md" not in page.split("## Sources")[1]
    assert "retract_claim" not in rep.ops and rep.n("regenerate_page") == 0, "provenance-only change: re-render"
    assert_equals_full_rebuild(src, store, tmp_path)
