"""质控批次的 SQLite 持久化与并发控制。

设计要点：

- 每个“仪器 × 检测项目”是独立的质控序列，最多保存 100 个批次，
  批次按唯一整数序号排列，序号在该序列内唯一。
- 全库使用一个全局修订号 ``revision``，任何成功写入都让它加 1；
  写入必须携带调用方读到的修订号，不一致则拒绝（乐观并发控制）。
- 开启 WAL 与 ``busy_timeout``；写事务先持有进程内互斥锁，再执行
  ``BEGIN IMMEDIATE`` 抢占 SQLite 写锁。两个 uvicorn 工作进程并发时，
  写事务在数据库层面被严格串行化，配合修订号校验，并发写入与查询
  等价于某种串行执行。
- 一次写事务同时完成“记录写入、结论重算、修订号推进”，任一步失败
  全部回滚；数据落在磁盘上，进程重启后保留。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from .qc_rules import (
    QcBatchInput,
    QcConclusion,
    QcStatus,
    compute_conclusions,
    parse_decimal,
    parse_positive_decimal,
)

MAX_BATCHES_PER_SERIES = 100


def _default_db_path() -> Path:
    """默认数据库位置。

    容器内（Dockerfile 保证 /data 存在且可写）使用 /data/qc_control.db；
    本地开发环境若 /data 不可写，则回退到工作目录下的本地数据目录，
    保证服务不开卷也能直接启动、测试。
    """

    configured = os.environ.get("QC_DB_PATH")
    if configured:
        return Path(configured)
    candidate = Path("/data/qc_control.db")
    try:
        candidate.parent.mkdir(parents=True, exist_ok=True)
        probe = candidate.parent / ".write-probe"
        probe.touch()
        probe.unlink()
    except OSError:
        return Path.cwd() / ".local-data" / "qc_control.db"
    return candidate


DEFAULT_DB_PATH = _default_db_path()


class QcConflictError(Exception):
    """修订号不匹配，或序号/唯一 ID 冲突等 409 类错误。"""


class QcNotFoundError(Exception):
    """目标仪器/检测项目/批次不存在（404）。"""


@dataclass(frozen=True)
class StoredBatch:
    seq: int
    batch_uid: str
    water_result_ids: tuple[str, ...]
    measured: str
    target: str
    stddev: str


@dataclass(frozen=True)
class StatusChange:
    """一个水样结果 ID 的结论状态前后变化。"""

    water_result_id: str
    seq: int
    batch_uid: str
    before_status: QcStatus | None
    after_status: QcStatus


@dataclass(frozen=True)
class WriteOutcome:
    """一次写入的完整结果。"""

    instrument: str
    test_item: str
    revision: int
    affected_seq: int
    conclusion: QcConclusion
    changed: tuple[StatusChange, ...]
    batches_after: tuple[StoredBatch, ...] = field(default_factory=tuple)
    conclusions_after: tuple[QcConclusion, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class SeriesSnapshot:
    """一次查询返回的完整序列：记录、结论、修订号。"""

    instrument: str
    test_item: str
    revision: int
    batches: tuple[StoredBatch, ...]
    conclusions: tuple[QcConclusion, ...]


class QcStore:
    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # 进程内串行化，配合 BEGIN IMMEDIATE 实现跨进程串行。
        self._write_lock = threading.RLock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_path,
            timeout=30.0,
            isolation_level=None,  # 手工管理事务
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_schema(self) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS qc_meta (
                        id INTEGER PRIMARY KEY CHECK (id = 1),
                        revision INTEGER NOT NULL CHECK (revision >= 0)
                    )
                    """
                )
                conn.execute(
                    "INSERT OR IGNORE INTO qc_meta (id, revision) VALUES (1, 0)"
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS qc_batches (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        instrument TEXT NOT NULL,
                        test_item TEXT NOT NULL,
                        seq INTEGER NOT NULL,
                        batch_uid TEXT NOT NULL,
                        water_result_ids TEXT NOT NULL,
                        measured TEXT NOT NULL,
                        target TEXT NOT NULL,
                        stddev TEXT NOT NULL,
                        UNIQUE (instrument, test_item, seq),
                        UNIQUE (instrument, test_item, batch_uid)
                    )
                    """
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    # ---------- 查询 ----------
    def _load_revision(self, conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT revision FROM qc_meta WHERE id = 1").fetchone()
        return int(row["revision"])

    def _load_batches(
        self, conn: sqlite3.Connection, instrument: str, test_item: str
    ) -> list[dict]:
        return [
            dict(row)
            for row in conn.execute(
                """
                SELECT seq, batch_uid, water_result_ids, measured, target, stddev
                FROM qc_batches
                WHERE instrument = ? AND test_item = ?
                ORDER BY seq ASC
                """,
                (instrument, test_item),
            )
        ]

    @staticmethod
    def _row_to_batch(row: dict) -> StoredBatch:
        return StoredBatch(
            seq=int(row["seq"]),
            batch_uid=row["batch_uid"],
            water_result_ids=tuple(json.loads(row["water_result_ids"])),
            measured=row["measured"],
            target=row["target"],
            stddev=row["stddev"],
        )

    @staticmethod
    def _rows_to_inputs(rows: Iterable[dict]) -> list[QcBatchInput]:
        inputs: list[QcBatchInput] = []
        for row in rows:
            inputs.append(
                QcBatchInput(
                    seq=int(row["seq"]),
                    batch_uid=row["batch_uid"],
                    water_result_ids=tuple(json.loads(row["water_result_ids"])),
                    measured=parse_decimal(row["measured"], field="measured"),
                    target=parse_decimal(row["target"], field="target"),
                    stddev=parse_positive_decimal(row["stddev"], field="stddev"),
                )
            )
        return inputs

    def get_series(self, instrument: str, test_item: str) -> SeriesSnapshot:
        """一次查询同时返回记录、结论和当前全库修订号。"""

        with self._connect() as conn:
            revision = self._load_revision(conn)
            rows = self._load_batches(conn, instrument, test_item)
            batches = tuple(self._row_to_batch(row) for row in rows)
            conclusions = compute_conclusions(self._rows_to_inputs(rows))
        return SeriesSnapshot(
            instrument=instrument,
            test_item=test_item,
            revision=revision,
            batches=batches,
            conclusions=conclusions,
        )

    # ---------- 写入公共流程 ----------

    def _commit_write(
        self,
        *,
        instrument: str,
        test_item: str,
        expected_revision: int,
        mutate: Callable[[sqlite3.Connection, list[dict]], int | None],
        affected_seq: int | None = None,
    ) -> WriteOutcome:
        """所有写操作的统一事务模板。

        ``mutate(conn, rows)`` 在已校验修订号的事务内执行具体的
        INSERT/UPDATE，并直接修改/追加内存中的 ``rows``；返回实际受影响的
        序号（新增自动分配序号时使用）。
        """

        with self._write_lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                current_revision = self._load_revision(conn)
                if current_revision != expected_revision:
                    raise QcConflictError(
                        f"修订号不匹配：当前为 {current_revision}，"
                        f"请求携带 {expected_revision}；请重新查询后再写入"
                    )

                rows = self._load_batches(conn, instrument, test_item)
                before = {
                    c.seq: c for c in compute_conclusions(self._rows_to_inputs(rows))
                }

                assigned_seq = mutate(conn, rows)
                if assigned_seq is not None:
                    affected_seq = assigned_seq

                after_inputs = self._rows_to_inputs(rows)
                after = {c.seq: c for c in compute_conclusions(after_inputs)}

                changed: list[StatusChange] = []
                status_by_seq: dict[int, QcStatus] = {
                    seq: conclusion.status for seq, conclusion in after.items()
                }
                for row in rows:
                    seq = int(row["seq"])
                    new_status = status_by_seq[seq]
                    old_status = before[seq].status if seq in before else None
                    if old_status != new_status:
                        for water_result_id in json.loads(row["water_result_ids"]):
                            changed.append(
                                StatusChange(
                                    water_result_id=water_result_id,
                                    seq=seq,
                                    batch_uid=row["batch_uid"],
                                    before_status=old_status,
                                    after_status=new_status,
                                )
                            )

                conn.execute(
                    "UPDATE qc_meta SET revision = ? WHERE id = 1",
                    (current_revision + 1,),
                )

                # 在同一事务内构造最终快照，避免提交后再读被其他写入插入。
                batches_after = tuple(self._row_to_batch(row) for row in rows)
                conclusions_after = compute_conclusions(
                    self._rows_to_inputs(rows)
                )
                new_revision = current_revision + 1
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

        conclusion = next(
            c for c in conclusions_after if c.seq == affected_seq
        )
        return WriteOutcome(
            instrument=instrument,
            test_item=test_item,
            revision=new_revision,
            affected_seq=affected_seq,
            conclusion=conclusion,
            changed=tuple(changed),
            batches_after=batches_after,
            conclusions_after=conclusions_after,
        )

    # ---------- 新增批次 ----------

    def add_batch(
        self,
        *,
        instrument: str,
        test_item: str,
        expected_revision: int,
        batch_uid: str,
        water_result_ids: tuple[str, ...],
        measured: str,
        target: str,
        stddev: str,
        seq: int | None = None,
    ) -> WriteOutcome:
        """新增一批；``seq`` 为空时自动分配当前最大序号 + 1。"""

        # 入口处先完成十进制合法性校验；落库保留原始十进制字符串。
        parse_decimal(measured, field="measured")
        parse_decimal(target, field="target")
        parse_positive_decimal(stddev, field="stddev")

        def mutate(conn: sqlite3.Connection, rows: list[dict]) -> int:
            existing_seqs = [int(row["seq"]) for row in rows]
            existing_uids = {row["batch_uid"] for row in rows}
            if batch_uid in existing_uids:
                raise QcConflictError(
                    f"批次唯一 ID {batch_uid!r} 在该仪器/检测项目下已存在"
                )
            if len(rows) >= MAX_BATCHES_PER_SERIES:
                raise QcConflictError(
                    f"同一仪器/检测项目最多保存 {MAX_BATCHES_PER_SERIES} 个批次"
                )
            if seq is None:
                assigned = (max(existing_seqs) + 1) if existing_seqs else 1
            else:
                if seq in existing_seqs:
                    raise QcConflictError(f"序号 {seq} 已被占用")
                assigned = seq
            ids_json = json.dumps(list(water_result_ids), ensure_ascii=False)
            conn.execute(
                """
                INSERT INTO qc_batches
                    (instrument, test_item, seq, batch_uid,
                     water_result_ids, measured, target, stddev)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    instrument,
                    test_item,
                    assigned,
                    batch_uid,
                    ids_json,
                    measured,
                    target,
                    stddev,
                ),
            )
            rows.append(
                {
                    "seq": assigned,
                    "batch_uid": batch_uid,
                    "water_result_ids": ids_json,
                    "measured": measured,
                    "target": target,
                    "stddev": stddev,
                }
            )
            return assigned

        return self._commit_write(
            instrument=instrument,
            test_item=test_item,
            expected_revision=expected_revision,
            mutate=mutate,
            affected_seq=seq,
        )

    # ---------- 更正实测值 ----------

    def correct_measured(
        self,
        *,
        instrument: str,
        test_item: str,
        expected_revision: int,
        seq: int,
        measured: str,
    ) -> WriteOutcome:
        """更正指定序号批次的质控实测值；其余字段不变，结论整体重算。"""

        parse_decimal(measured, field="measured")

        def mutate(conn: sqlite3.Connection, rows: list[dict]) -> None:
            target_row = next((r for r in rows if int(r["seq"]) == seq), None)
            if target_row is None:
                raise QcNotFoundError(
                    f"仪器 {instrument!r}、检测项目 {test_item!r} 下不存在序号 {seq}"
                )
            conn.execute(
                """
                UPDATE qc_batches SET measured = ?
                WHERE instrument = ? AND test_item = ? AND seq = ?
                """,
                (measured, instrument, test_item, seq),
            )
            target_row["measured"] = measured

        return self._commit_write(
            instrument=instrument,
            test_item=test_item,
            expected_revision=expected_revision,
            affected_seq=seq,
            mutate=mutate,
        )

    # ---------- 补录历史序号 ----------

    def backfill_batch(
        self,
        *,
        instrument: str,
        test_item: str,
        expected_revision: int,
        seq: int,
        batch_uid: str,
        water_result_ids: tuple[str, ...],
        measured: str,
        target: str,
        stddev: str,
    ) -> WriteOutcome:
        """向历史序号位置补录一批；该序号必须空缺且不超过 100 批上限。"""

        parse_decimal(measured, field="measured")
        parse_decimal(target, field="target")
        parse_positive_decimal(stddev, field="stddev")

        def mutate(conn: sqlite3.Connection, rows: list[dict]) -> None:
            existing_seqs = [int(row["seq"]) for row in rows]
            existing_uids = {row["batch_uid"] for row in rows}
            if seq in existing_seqs:
                raise QcConflictError(f"序号 {seq} 已被占用，无法补录")
            if batch_uid in existing_uids:
                raise QcConflictError(
                    f"批次唯一 ID {batch_uid!r} 在该仪器/检测项目下已存在"
                )
            if len(rows) >= MAX_BATCHES_PER_SERIES:
                raise QcConflictError(
                    f"同一仪器/检测项目最多保存 {MAX_BATCHES_PER_SERIES} 个批次"
                )
            ids_json = json.dumps(list(water_result_ids), ensure_ascii=False)
            conn.execute(
                """
                INSERT INTO qc_batches
                    (instrument, test_item, seq, batch_uid,
                     water_result_ids, measured, target, stddev)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    instrument,
                    test_item,
                    seq,
                    batch_uid,
                    ids_json,
                    measured,
                    target,
                    stddev,
                ),
            )
            rows.append(
                {
                    "seq": seq,
                    "batch_uid": batch_uid,
                    "water_result_ids": ids_json,
                    "measured": measured,
                    "target": target,
                    "stddev": stddev,
                }
            )

        return self._commit_write(
            instrument=instrument,
            test_item=test_item,
            expected_revision=expected_revision,
            affected_seq=seq,
            mutate=mutate,
        )
