"""Storage backend tests: versioning, time travel, partitioning, retention."""

from __future__ import annotations

import json

import pandas as pd
import pytest

from insilico_trial_mas.config import StorageConfig
from insilico_trial_mas.errors import StorageError
from insilico_trial_mas.storage.base import sanitise_for_parquet
from insilico_trial_mas.storage.factory import MemoryStore, create_store
from insilico_trial_mas.storage.local_store import LocalVersionedStore


@pytest.fixture
def store(tmp_path) -> LocalVersionedStore:
    return LocalVersionedStore(StorageConfig(local_root=str(tmp_path / "lake")))


def _frame(values: list[float], arm: str = "placebo") -> pd.DataFrame:
    return pd.DataFrame({"patient_id": [f"PT-{i}" for i in range(len(values))], "sbp": values, "arm_id": arm})


def test_write_read_and_history(store: LocalVersionedStore) -> None:
    first = store.write("silver/patient_states", _frame([120.0, 130.0]), run_id="RUN-1")
    second = store.write("silver/patient_states", _frame([140.0]), run_id="RUN-2")
    assert first.version == 1 and second.version == 2
    assert store.latest_version("silver/patient_states") == 2
    assert len(store.read("silver/patient_states")) == 3
    history = store.history("silver/patient_states")
    assert [entry["version"] for entry in history] == [2, 1]
    assert history[0]["run_id"] == "RUN-2"


def test_time_travel_reads_a_historical_version(store: LocalVersionedStore) -> None:
    store.write("silver/patient_states", _frame([120.0, 121.0]), run_id="RUN-1")
    store.write("silver/patient_states", _frame([200.0]), run_id="RUN-2")
    version_one = store.read("silver/patient_states", version=1)
    assert list(version_one["sbp"]) == [120.0, 121.0]
    assert len(store.read("silver/patient_states", version=2)) == 1
    with pytest.raises(StorageError, match="version 99 not found"):
        store.read("silver/patient_states", version=99)


def test_read_scoped_to_one_run(store: LocalVersionedStore) -> None:
    store.write("silver/patient_states", _frame([1.0]), run_id="RUN-A")
    store.write("silver/patient_states", _frame([2.0]), run_id="RUN-B")
    scoped = store.read("silver/patient_states", run_id="RUN-A")
    assert list(scoped["sbp"]) == [1.0]
    with pytest.raises(StorageError, match="no versions"):
        store.read("silver/patient_states", run_id="RUN-Z")


def test_missing_table_reads_empty(store: LocalVersionedStore) -> None:
    assert store.read("silver/unknown").empty
    assert store.history("silver/unknown") == []


def test_overwrite_still_keeps_history(store: LocalVersionedStore) -> None:
    store.write("gold/arm_summaries", _frame([1.0, 2.0]), run_id="RUN-1")
    store.write("gold/arm_summaries", _frame([9.0]), run_id="RUN-2", mode="overwrite")
    assert list(store.read("gold/arm_summaries")["sbp"]) == [9.0]
    assert list(store.read("gold/arm_summaries", version=1)["sbp"]) == [1.0, 2.0]


def test_partitioned_write_preserves_partition_columns(store: LocalVersionedStore) -> None:
    frame = pd.concat([_frame([120.0], "placebo"), _frame([130.0], "high_dose")], ignore_index=True)
    store.write("silver/patient_states", frame, run_id="RUN-1", partition_by=["arm_id"])
    restored = store.read("silver/patient_states")
    assert set(restored["arm_id"]) == {"placebo", "high_dose"}
    assert len(restored) == 2


def test_nested_values_are_json_encoded(store: LocalVersionedStore) -> None:
    frame = pd.DataFrame(
        {
            "patient_id": ["PT-1"],
            "grade_distribution": [{"1": 5, "2": 3}],
            "ci": [(0.1, 0.4)],
        }
    )
    store.write("gold/safety_summary", frame, run_id="RUN-1")
    restored = store.read("gold/safety_summary")
    assert json.loads(restored["grade_distribution"].iloc[0])["1"] == 5
    assert json.loads(restored["ci"].iloc[0]) == [0.1, 0.4]


def test_sanitise_for_parquet_leaves_scalars_alone() -> None:
    frame = pd.DataFrame({"a": [1, 2], "b": ["x", None]})
    assert sanitise_for_parquet(frame).equals(frame)


def test_vacuum_removes_old_versions(store: LocalVersionedStore) -> None:
    for index in range(4):
        store.write("silver/patient_states", _frame([float(index)]), run_id=f"RUN-{index}")
    removed = store.vacuum("silver/patient_states", keep=1)
    assert removed == 3
    assert store.latest_version("silver/patient_states") == 4
    assert store.read("silver/patient_states", version=4).empty is False


def test_list_tables(store: LocalVersionedStore) -> None:
    store.write("silver/patient_states", _frame([1.0]))
    store.write("gold/arm_summaries", _frame([2.0]))
    assert store.list_tables() == ["gold/arm_summaries", "silver/patient_states"]


def test_memory_store_behaves_like_the_local_store() -> None:
    store = MemoryStore(StorageConfig())
    store.write("silver/patient_states", _frame([1.0]), run_id="R1")
    store.write("silver/patient_states", _frame([2.0]), run_id="R2")
    assert len(store.read("silver/patient_states")) == 2
    assert list(store.read("silver/patient_states", version=1)["sbp"]) == [1.0]
    assert store.history("silver/patient_states")[0]["run_id"] == "R2"
    store.write("silver/patient_states", _frame([3.0]), run_id="R3", mode="overwrite")
    assert list(store.read("silver/patient_states")["sbp"]) == [3.0]


def test_factory_selects_backend(tmp_path) -> None:
    from insilico_trial_mas.config import load_config

    local = create_store(load_config(None, {"storage": {"backend": "local", "local_root": str(tmp_path)}}))
    assert local.backend == "local"
    memory = create_store(load_config(None, {"storage": {"backend": "memory"}}))
    assert memory.backend == "memory"
    with pytest.raises(Exception, match="unknown storage backend"):
        create_store(load_config(None, {"storage": {"backend": "memory"}}).__class__(
            **{**load_config(None).to_dict(), "storage": type(load_config(None).storage)(backend="nope")}
        ))
