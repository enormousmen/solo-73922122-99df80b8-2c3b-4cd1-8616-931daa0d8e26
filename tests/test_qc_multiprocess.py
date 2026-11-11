"""跨进程并发：多个独立 Python 进程写同一个 SQLite 文件。

模拟 docker compose 中两个 uvicorn 工作进程的真实场景，验证 BEGIN IMMEDIATE
+ 修订号能把并发写入等价串行化，且没有任何记录丢失或序号错乱。
"""

from __future__ import annotations

import multiprocessing as mp
import os
from pathlib import Path

import pytest

from app.qc_store import QcConflictError, QcStore

INSTRUMENT = "ICP-X"
TEST_ITEM = "cadmium"
WRITER_PROCESSES = 4
WRITES_PER_PROCESS = 10


def _writer(db_path: str, writer_id: int) -> None:
    store = QcStore(db_path)
    done = 0
    while done < WRITES_PER_PROCESS:
        snapshot = store.get_series(INSTRUMENT, TEST_ITEM)
        try:
            outcome = store.add_batch(
                instrument=INSTRUMENT,
                test_item=TEST_ITEM,
                expected_revision=snapshot.revision,
                batch_uid=f"P{writer_id}-{snapshot.revision}-{done}",
                water_result_ids=(f"W{writer_id}-{done}",),
                measured="10.00",
                target="10.00",
                stddev="0.20",
            )
        except QcConflictError:
            continue  # 修订号竞争失败，重新查询后重试
        assert outcome.revision == snapshot.revision + 1
        done += 1


def test_separate_processes_serialize_writes_on_same_sqlite_file(tmp_path: Path):
    ctx = mp.get_context("spawn")
    db_path = tmp_path / "cross-process.db"

    processes = [
        ctx.Process(target=_writer, args=(str(db_path), writer_id))
        for writer_id in range(WRITER_PROCESSES)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=120)
        assert process.exitcode == 0, f"writer process failed: {process.exitcode}"

    # 独立进程重新打开，校验最终状态。
    verifier = QcStore(db_path)
    snapshot = verifier.get_series(INSTRUMENT, TEST_ITEM)
    expected_total = WRITER_PROCESSES * WRITES_PER_PROCESS
    assert len(snapshot.batches) == expected_total
    assert snapshot.revision == expected_total
    assert [batch.seq for batch in snapshot.batches] == list(
        range(1, expected_total + 1)
    )
    uids = [batch.batch_uid for batch in snapshot.batches]
    assert len(set(uids)) == expected_total
    # 所有批次 z=0，全部放行。
    assert all(c.status == "released" for c in snapshot.conclusions)

    # 另一个仪器/项目序列为空，修订号是全库共享的同一值。
    other = verifier.get_series("OTHER", TEST_ITEM)
    assert other.batches == ()
    assert other.revision == expected_total


@pytest.mark.skipif(os.name == "nt", reason="POSIX 文件锁语义")
def test_stale_revision_from_second_store_instance_is_rejected(tmp_path: Path):
    """同一文件的两个存储实例（近似两个进程）之间的修订号互斥。"""

    first = QcStore(tmp_path / "two-store.db")
    second = QcStore(tmp_path / "two-store.db")

    first.add_batch(
        instrument="I",
        test_item="T",
        expected_revision=0,
        batch_uid="B1",
        water_result_ids=("W1",),
        measured="10.00",
        target="10.00",
        stddev="0.20",
    )
    # second 仍拿着旧修订号 0 写入 → 409 且不生效。
    with pytest.raises(QcConflictError):
        second.add_batch(
            instrument="I",
            test_item="T",
            expected_revision=0,
            batch_uid="B2",
            water_result_ids=("W2",),
            measured="10.00",
            target="10.00",
            stddev="0.20",
        )
    assert second.get_series("I", "T").revision == 1
