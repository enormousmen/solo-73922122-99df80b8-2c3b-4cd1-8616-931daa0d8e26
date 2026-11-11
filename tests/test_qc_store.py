from __future__ import annotations

import threading
from decimal import Decimal

import pytest

from app.qc_store import (
    MAX_BATCHES_PER_SERIES,
    QcConflictError,
    QcNotFoundError,
    QcStore,
)


@pytest.fixture()
def store(tmp_path) -> QcStore:
    return QcStore(tmp_path / "qc.db")


def add(store: QcStore, seq_uid: str, measured: str, revision: int, **kwargs):
    return store.add_batch(
        instrument="ICP-01",
        test_item="lead",
        expected_revision=revision,
        batch_uid=seq_uid,
        water_result_ids=kwargs.get("ids", (f"W-{seq_uid}",)),
        measured=measured,
        target=kwargs.get("target", "10.00"),
        stddev=kwargs.get("stddev", "0.20"),
    )


def test_empty_series_starts_at_revision_zero(store: QcStore):
    snapshot = store.get_series("ICP-01", "lead")
    assert snapshot.revision == 0
    assert snapshot.batches == ()
    assert snapshot.conclusions == ()


def test_add_batch_assigns_seq_and_increments_revision(store: QcStore):
    outcome = add(store, "B1", "10.20", 0)
    assert outcome.revision == 1
    assert outcome.affected_seq == 1
    assert outcome.conclusion.status == "released"
    # 新增批次的水样结果以 before_status=None 出现在变化列表。
    change = outcome.changed[0]
    assert change.water_result_id == "W-B1"
    assert change.before_status is None
    assert change.after_status == "released"

    outcome2 = add(store, "B2", "10.82", 1)
    assert outcome2.affected_seq == 2
    assert outcome2.revision == 2
    assert outcome2.conclusion.status == "paused"  # z = 4.1


def test_stale_revision_is_rejected_and_nothing_changes(store: QcStore):
    add(store, "B1", "10.20", 0)
    snapshot_before = store.get_series("ICP-01", "lead")

    with pytest.raises(QcConflictError):
        add(store, "B2", "10.20", 0)

    snapshot_after = store.get_series("ICP-01", "lead")
    assert snapshot_after.revision == snapshot_before.revision == 1
    assert len(snapshot_after.batches) == 1


def test_correction_recomputes_and_reports_changed_water_results(store: QcStore):
    # B1: z=4.1 暂停；B2: z=2.5，与 B1 同号且都 >2 → 暂停。
    add(store, "B1", "10.82", 0, ids=("W1",))
    add(store, "B2", "10.50", 1, ids=("W2",))
    assert [c.status for c in store.get_series("ICP-01", "lead").conclusions] == [
        "paused",
        "paused",
    ]

    # 把 B1 实测值更正为 10.10（z=0.5）：两批都应变回放行。
    outcome = store.correct_measured(
        instrument="ICP-01",
        test_item="lead",
        expected_revision=2,
        seq=1,
        measured="10.10",
    )
    assert outcome.revision == 3
    assert outcome.conclusion.status == "released"
    changed = {(c.water_result_id, c.before_status, c.after_status) for c in outcome.changed}
    assert ("W1", "paused", "released") in changed
    assert ("W2", "paused", "released") in changed


def test_correction_unknown_seq_raises_not_found_and_rolls_back(store: QcStore):
    with pytest.raises(QcNotFoundError):
        store.correct_measured(
            instrument="ICP-01",
            test_item="lead",
            expected_revision=0,
            seq=99,
            measured="10.10",
        )
    assert store.get_series("ICP-01", "lead").revision == 0


def test_correction_with_stale_revision_rolls_back(store: QcStore):
    add(store, "B1", "10.82", 0)
    add(store, "B2", "10.50", 1)
    with pytest.raises(QcConflictError):
        store.correct_measured(
            instrument="ICP-01",
            test_item="lead",
            expected_revision=1,  # 已过期，当前为 2
            seq=1,
            measured="10.10",
        )
    snapshot = store.get_series("ICP-01", "lead")
    assert snapshot.revision == 2
    assert snapshot.batches[0].measured == "10.82"


def test_backfill_inserts_historical_seq_and_recomputes(store: QcStore):
    # 现有序号 2（先补录占住 2）：z=2.5，其前一批为空缺时放行。
    store.backfill_batch(
        instrument="ICP-01",
        test_item="lead",
        expected_revision=0,
        seq=2,
        batch_uid="B2",
        water_result_ids=("W2",),
        measured="10.50",
        target="10.00",
        stddev="0.20",
    )
    assert store.get_series("ICP-01", "lead").conclusions[0].status == "released"

    # 补录序号 1，z=2.6（同号且都 >2）→ 序号 2 应变为暂停。
    outcome = store.backfill_batch(
        instrument="ICP-01",
        test_item="lead",
        expected_revision=1,
        seq=1,
        batch_uid="B1",
        water_result_ids=("W1",),
        measured="10.52",
        target="10.00",
        stddev="0.20",
    )
    assert outcome.revision == 2
    assert outcome.conclusion.seq == 1
    statuses = {c.seq: c.status for c in store.get_series("ICP-01", "lead").conclusions}
    assert statuses == {1: "released", 2: "paused"}
    changed = {(c.water_result_id, c.before_status, c.after_status) for c in outcome.changed}
    assert ("W1", None, "released") in changed
    assert ("W2", "released", "paused") in changed


def test_backfill_rejects_occupied_seq_and_duplicate_uid(store: QcStore):
    add(store, "B1", "10.20", 0)
    with pytest.raises(QcConflictError):
        store.backfill_batch(
            instrument="ICP-01",
            test_item="lead",
            expected_revision=1,
            seq=1,
            batch_uid="B9",
            water_result_ids=("W9",),
            measured="10.20",
            target="10.00",
            stddev="0.20",
        )
    with pytest.raises(QcConflictError):
        store.backfill_batch(
            instrument="ICP-01",
            test_item="lead",
            expected_revision=1,
            seq=2,
            batch_uid="B1",
            water_result_ids=("W9",),
            measured="10.20",
            target="10.00",
            stddev="0.20",
        )
    assert store.get_series("ICP-01", "lead").revision == 1


def test_series_is_scoped_by_instrument_and_test_item(store: QcStore):
    add(store, "B1", "10.20", 0)
    # 另一仪器 / 另一项目是独立序列，修订号仍是全库共享。
    other = store.add_batch(
        instrument="ICP-02",
        test_item="lead",
        expected_revision=1,
        batch_uid="B1",  # 不同序列允许重复 ID
        water_result_ids=("X1",),
        measured="10.20",
        target="10.00",
        stddev="0.20",
    )
    assert other.affected_seq == 1
    assert store.get_series("ICP-01", "lead").revision == 2
    assert len(store.get_series("ICP-02", "lead").batches) == 1


def test_max_100_batches_enforced(store: QcStore):
    for i in range(MAX_BATCHES_PER_SERIES):
        outcome = add(store, f"B{i}", "10.00", i)
        assert outcome.revision == i + 1
    with pytest.raises(QcConflictError):
        add(store, "B-101", "10.00", MAX_BATCHES_PER_SERIES)


def test_decimal_strings_persisted_exactly_and_reopened(tmp_path):
    db_path = tmp_path / "qc.db"
    store = QcStore(db_path)
    store.add_batch(
        instrument="ICP-01",
        test_item="lead",
        expected_revision=0,
        batch_uid="B1",
        water_result_ids=("W1",),
        measured="10.8200",
        target="10.0000",
        stddev="0.2000",
    )
    # 重新打开数据库：重启后记录、结论与修订号都保留。
    reopened = QcStore(db_path)
    snapshot = reopened.get_series("ICP-01", "lead")
    assert snapshot.revision == 1
    assert snapshot.batches[0].measured == "10.8200"
    assert snapshot.conclusions[0].z.__class__.__name__ == "Decimal"
    # Decimal 除法结果系数按最简形式归一化，但数值精确等于 4.1。
    assert snapshot.conclusions[0].z == Decimal("4.1")


def test_concurrent_writes_with_same_revision_only_one_succeeds(store: QcStore):
    errors: list[Exception] = []

    def worker(uid: str) -> None:
        try:
            add(store, uid, "10.20", 0)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(f"B{i}",)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    snapshot = store.get_series("ICP-01", "lead")
    assert len(snapshot.batches) == 1
    assert snapshot.revision == 1
    assert len(errors) == 7
    assert all(isinstance(err, QcConflictError) for err in errors)


def test_concurrent_writes_with_fresh_revisions_all_commit_serially(store: QcStore):
    barrier = threading.Barrier(8)

    def worker(uid: str) -> None:
        barrier.wait()
        for _ in range(5):
            while True:
                revision = store.get_series("ICP-01", "lead").revision
                try:
                    add(store, f"{uid}-{revision}", "10.00", revision)
                    break
                except QcConflictError:
                    continue

    threads = [threading.Thread(target=worker, args=(f"B{i}",)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    snapshot = store.get_series("ICP-01", "lead")
    assert len(snapshot.batches) == 40
    assert snapshot.revision == 40
    assert [b.seq for b in snapshot.batches] == list(range(1, 41))
    assert len({b.batch_uid for b in snapshot.batches}) == 40


def test_failed_write_does_not_increment_revision(store: QcStore):
    with pytest.raises(QcConflictError):
        add(store, "B1", "10.20", 5)
    assert store.get_series("ICP-01", "lead").revision == 0
