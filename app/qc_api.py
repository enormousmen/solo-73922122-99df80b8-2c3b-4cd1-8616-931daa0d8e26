"""质控放行 API 路由。

限同一仪器与同一检测项目的质控序列：

- ``GET  /api/v1/qc/{instrument}/{test_item}`` 一次查询返回记录、结论和修订号；
- ``POST .../batches``        提交新批次（序号自动分配）；
- ``PUT  .../batches/{seq}/correction`` 更正实测值；
- ``POST .../backfill``       补录历史序号。

所有写请求都必须携带全库修订号 ``revision``；与库中当前值不符时返回 409，
本次写入完全不生效。所有数值字段均为十进制字符串。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, Field, ValidationInfo, field_validator

from .qc_rules import (
    DecimalValidationError,
    QcConclusion,
    format_decimal,
    parse_decimal,
    parse_positive_decimal,
)
from .qc_store import (
    DEFAULT_DB_PATH,
    QcConflictError,
    QcNotFoundError,
    QcStore,
    SeriesSnapshot,
    StatusChange,
    StoredBatch,
    WriteOutcome,
)

router = APIRouter(prefix="/api/v1/qc", tags=["质控放行"])

# 十进制字符串的请求体校验由 qc_rules 的统一正则完成，保证不接受
# 科学计数法 / NaN / Infinity。
_DECIMAL_DESCRIPTION = "十进制字符串，例如 '10.25'；不接受科学计数法、NaN、Infinity"


# ---------- 依赖 ----------


@lru_cache(maxsize=1)
def get_store() -> QcStore:
    return QcStore(DEFAULT_DB_PATH)


# ---------- 请求模型 ----------


class _DecimalFields(BaseModel):
    measured: str = Field(description="质控实测值（十进制字符串）")
    target: str = Field(description="质控靶值（十进制字符串）")
    stddev: str = Field(description="正标准差（十进制字符串，必须 > 0）")

    @field_validator("measured", "target")
    @classmethod
    def _validate_decimal(cls, value: str, info: ValidationInfo) -> str:
        try:
            parse_decimal(value, field=info.field_name)
        except DecimalValidationError as exc:
            raise ValueError(str(exc)) from exc
        return value

    @field_validator("stddev")
    @classmethod
    def _validate_stddev(cls, value: str) -> str:
        try:
            parse_positive_decimal(value, field="stddev")
        except DecimalValidationError as exc:
            raise ValueError(str(exc)) from exc
        return value


class AddBatchRequest(_DecimalFields):
    """新增批次请求；序号由服务端自动分配。"""

    revision: int = Field(ge=0, description="调用方读到的全库修订号")
    batch_uid: str = Field(
        min_length=1,
        max_length=128,
        description="批次唯一 ID，在同一仪器/检测项目下不可重复",
    )
    water_result_ids: list[str] = Field(
        min_length=1,
        description="本批关联的水样结果 ID 列表，列表内不可重复",
    )

    @field_validator("water_result_ids")
    @classmethod
    def _unique_water_ids(cls, value: list[str]) -> list[str]:
        if any(not item or not item.strip() for item in value):
            raise ValueError("水样结果 ID 不能为空字符串")
        if len(set(value)) != len(value):
            raise ValueError("同一批次内的水样结果 ID 不可重复")
        return value


class CorrectRequest(BaseModel):
    """更正某批次的质控实测值。"""

    revision: int = Field(ge=0, description="调用方读到的全库修订号")
    measured: str = Field(description=f"更正后的质控实测值。{_DECIMAL_DESCRIPTION}")

    @field_validator("measured")
    @classmethod
    def _validate_measured(cls, value: str) -> str:
        try:
            parse_decimal(value, field="measured")
        except DecimalValidationError as exc:
            raise ValueError(str(exc)) from exc
        return value


class BackfillRequest(_DecimalFields):
    """向空缺的历史序号补录一批。"""

    revision: int = Field(ge=0, description="调用方读到的全库修订号")
    seq: int = Field(ge=1, description="要补录的历史序号，必须当前空缺")
    batch_uid: str = Field(
        min_length=1,
        max_length=128,
        description="批次唯一 ID，在同一仪器/检测项目下不可重复",
    )
    water_result_ids: list[str] = Field(
        min_length=1,
        description="本批关联的水样结果 ID 列表，列表内不可重复",
    )

    @field_validator("water_result_ids")
    @classmethod
    def _unique_water_ids(cls, value: list[str]) -> list[str]:
        if any(not item or not item.strip() for item in value):
            raise ValueError("水样结果 ID 不能为空字符串")
        if len(set(value)) != len(value):
            raise ValueError("同一批次内的水样结果 ID 不可重复")
        return value


# ---------- 响应模型 ----------


class BatchRecord(BaseModel):
    seq: int = Field(description="批次序号（同一仪器/检测项目内唯一）")
    batch_uid: str = Field(description="批次唯一 ID")
    water_result_ids: list[str] = Field(description="水样结果 ID 列表")
    measured: str
    target: str
    stddev: str


class Conclusion(BaseModel):
    seq: int
    batch_uid: str
    z: str = Field(description="z 值的十进制字符串，z=(实测值−靶值)/标准差")
    status: str = Field(description="released=放行，paused=暂停出报告")
    reason: str = Field(
        description=(
            "within_limits=在控；abs_z_gt_3=|z|>3；"
            "same_sign_pair_gt_2=与前一批同号且两者 |z|>2"
        )
    )


class WaterResultStatusChange(BaseModel):
    water_result_id: str
    seq: int
    batch_uid: str
    before_status: str | None = Field(
        description="变化前状态；新增批次时为 null（此前不存在）"
    )
    after_status: str = Field(description="变化后状态：released 或 paused")


class QcQueryResponse(BaseModel):
    instrument: str
    test_item: str
    revision: int = Field(description="当前全库修订号")
    batches: list[BatchRecord] = Field(description="按序号升序的全部批次记录")
    conclusions: list[Conclusion] = Field(description="与批次一一对应的结论")


class QcWriteResponse(BaseModel):
    instrument: str
    test_item: str
    revision: int = Field(description="写入成功后的新全库修订号")
    conclusion: Conclusion = Field(description="本次写入批次的最新结论")
    changed_water_results: list[WaterResultStatusChange] = Field(
        description=(
            "已有批次中状态发生改变的水样结果 ID 及其前后状态；"
            "新增/补录批次自身的水样结果（before_status=null）也会列出"
        )
    )
    batches: list[BatchRecord] = Field(
        description="写入后该仪器/检测项目的全部批次记录（按序号升序）"
    )
    conclusions: list[Conclusion] = Field(description="写入后重算的全部结论")


# ---------- 序列化 ----------


def _serialize_batch(batch: StoredBatch) -> BatchRecord:
    return BatchRecord(
        seq=batch.seq,
        batch_uid=batch.batch_uid,
        water_result_ids=list(batch.water_result_ids),
        measured=batch.measured,
        target=batch.target,
        stddev=batch.stddev,
    )


def _serialize_conclusion(conclusion: QcConclusion) -> Conclusion:
    return Conclusion(
        seq=conclusion.seq,
        batch_uid=conclusion.batch_uid,
        z=format_decimal(conclusion.z),
        status=conclusion.status,
        reason=conclusion.reason,
    )


def _serialize_change(change: StatusChange) -> WaterResultStatusChange:
    return WaterResultStatusChange(
        water_result_id=change.water_result_id,
        seq=change.seq,
        batch_uid=change.batch_uid,
        before_status=change.before_status,
        after_status=change.after_status,
    )


def _snapshot_response(snapshot: SeriesSnapshot) -> QcQueryResponse:
    return QcQueryResponse(
        instrument=snapshot.instrument,
        test_item=snapshot.test_item,
        revision=snapshot.revision,
        batches=[_serialize_batch(b) for b in snapshot.batches],
        conclusions=[_serialize_conclusion(c) for c in snapshot.conclusions],
    )


def _write_response(outcome: WriteOutcome) -> QcWriteResponse:
    # 直接使用事务内已取回的快照数据，避免再开一次连接，也保证与修订号一致。
    return QcWriteResponse(
        instrument=outcome.instrument,
        test_item=outcome.test_item,
        revision=outcome.revision,
        conclusion=_serialize_conclusion(outcome.conclusion),
        changed_water_results=[_serialize_change(c) for c in outcome.changed],
        batches=[_serialize_batch(b) for b in outcome.batches_after],
        conclusions=[_serialize_conclusion(c) for c in outcome.conclusions_after],
    )


# ---------- 路径参数 ----------

InstrumentPath = Annotated[str, Path(min_length=1, max_length=128, description="仪器标识")]
TestItemPath = Annotated[str, Path(min_length=1, max_length=128, description="检测项目标识")]
SeqPath = Annotated[int, Path(ge=1, description="批次序号")]


# ---------- /docs 可直接试调的请求体示例 ----------

_ADD_EXAMPLE = {
    "requestBody": {
        "content": {
            "application/json": {
                "examples": {
                    "default": {
                        "summary": "新增一批（z=4.1，将暂停）",
                        "value": {
                            "revision": 0,
                            "batch_uid": "B-20261111-001",
                            "water_result_ids": ["W1001", "W1002", "W1003"],
                            "measured": "10.82",
                            "target": "10.00",
                            "stddev": "0.20",
                        },
                    }
                }
            }
        }
    }
}

_CORRECTION_EXAMPLE = {
    "requestBody": {
        "content": {
            "application/json": {
                "examples": {
                    "default": {
                        "summary": "更正实测值：10.82 → 10.10（z 由 4.1 回到 0.5）",
                        "description": (
                            "先 GET 该仪器/检测项目取得当前 revision，再连同更正后的"
                            "实测值一起提交；revision 过期会返回 409。"
                        ),
                        "value": {"revision": 1, "measured": "10.10"},
                    }
                }
            }
        }
    }
}

_BACKFILL_EXAMPLE = {
    "requestBody": {
        "content": {
            "application/json": {
                "examples": {
                    "default": {
                        "summary": "补录历史序号 1（漏录批次，提交后自动重算后续结论）",
                        "description": "目标 seq 必须当前空缺；序号占用或 ID 重复返回 409。",
                        "value": {
                            "revision": 2,
                            "seq": 1,
                            "batch_uid": "B-20261109-001",
                            "water_result_ids": ["W0901", "W0902"],
                            "measured": "9.95",
                            "target": "10.00",
                            "stddev": "0.20",
                        },
                    }
                }
            }
        }
    }
}


# ---------- 路由 ----------


@router.get(
    "/{instrument}/{test_item}",
    response_model=QcQueryResponse,
    summary="查询某仪器某检测项目的全部质控记录、结论与修订号",
)
def query_series(
    instrument: InstrumentPath,
    test_item: TestItemPath,
    store: QcStore = Depends(get_store),
) -> QcQueryResponse:
    """一次查询同时返回记录、重算结论和当前全库修订号。"""

    return _snapshot_response(store.get_series(instrument, test_item))


@router.post(
    "/{instrument}/{test_item}/batches",
    response_model=QcWriteResponse,
    status_code=201,
    summary="提交新批次（序号自动分配，携带修订号）",
    response_description="本批结论、状态变化的水样结果 ID 及新修订号",
    openapi_extra=_ADD_EXAMPLE,
)
def add_batch(
    payload: AddBatchRequest,
    instrument: InstrumentPath,
    test_item: TestItemPath,
    store: QcStore = Depends(get_store),
) -> QcWriteResponse:
    try:
        outcome = store.add_batch(
            instrument=instrument,
            test_item=test_item,
            expected_revision=payload.revision,
            batch_uid=payload.batch_uid,
            water_result_ids=tuple(payload.water_result_ids),
            measured=payload.measured,
            target=payload.target,
            stddev=payload.stddev,
        )
    except QcConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _write_response(outcome)


@router.put(
    "/{instrument}/{test_item}/batches/{seq}/correction",
    response_model=QcWriteResponse,
    summary="更正指定批次的质控实测值（在 /docs 可直接试调，见请求体示例）",
    response_description="更正批次的结论、受影响的水样结果状态变化及新修订号",
    openapi_extra=_CORRECTION_EXAMPLE,
)
def correct_measured(
    payload: CorrectRequest,
    instrument: InstrumentPath,
    test_item: TestItemPath,
    seq: SeqPath,
    store: QcStore = Depends(get_store),
) -> QcWriteResponse:
    """质控员更正实测值后，服务端重算该批次及后续批次的全部结论。

    示例流程：先 `GET /api/v1/qc/{仪器}/{项目}` 取得 `revision`，再把更正后的
    实测值与该修订号一起提交；若期间已有他人写入，会收到 409，重新查询即可。
    """

    try:
        outcome = store.correct_measured(
            instrument=instrument,
            test_item=test_item,
            expected_revision=payload.revision,
            seq=seq,
            measured=payload.measured,
        )
    except QcNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except QcConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _write_response(outcome)


@router.post(
    "/{instrument}/{test_item}/backfill",
    response_model=QcWriteResponse,
    status_code=201,
    summary="补录空缺的历史序号批次（在 /docs 可直接试调，见请求体示例）",
    response_description="补录批次的结论、受影响的水样结果状态变化及新修订号",
    openapi_extra=_BACKFILL_EXAMPLE,
)
def backfill_batch(
    payload: BackfillRequest,
    instrument: InstrumentPath,
    test_item: TestItemPath,
    store: QcStore = Depends(get_store),
) -> QcWriteResponse:
    """向空缺的历史序号补录一批；补录后自动重算该批次与其后批次的结论。

    示例流程：先查询确认目标序号空缺并取得 `revision`，再提交补录请求体。
    序号已占用、唯一 ID 重复或超过 100 批上限时返回 409，且全部不生效。
    """

    try:
        outcome = store.backfill_batch(
            instrument=instrument,
            test_item=test_item,
            expected_revision=payload.revision,
            seq=payload.seq,
            batch_uid=payload.batch_uid,
            water_result_ids=tuple(payload.water_result_ids),
            measured=payload.measured,
            target=payload.target,
            stddev=payload.stddev,
        )
    except QcConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _write_response(outcome)
