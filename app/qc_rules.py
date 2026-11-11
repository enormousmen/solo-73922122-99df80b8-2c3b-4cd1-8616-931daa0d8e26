"""质控放行规则：十进制 z 值计算与批次结论重算。

所有数值都以十进制字符串进出，内部使用 :class:`decimal.Decimal` 精确计算，
绝不使用二进制浮点。

判定规则（按唯一整数序号 ``seq`` 升序逐批判断）：

1. ``z = (实测值 - 靶值) / 标准差``；
2. ``|z| > 3`` → 暂停该批（``paused``）；
3. 否则若本批与**前一批**的 z 同号，且两者都满足 ``|z| > 2`` → 暂停该批；
4. 其他情况放行（``released``）。

被暂停的批次仍然作为后一批的“前一批”参与判断。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Final, Literal, Sequence

# 只接受普通十进制写法：可选正负号、整数/小数；拒绝科学计数法、NaN、Infinity、空白。
DECIMAL_RE: Final = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$")

# 除法（可能除不尽）使用的有效数字精度。
Z_PRECISION: Final = 50

RELEASED: Final = "released"
PAUSED: Final = "paused"
QcStatus = Literal["released", "paused"]

REASON_WITHIN_LIMITS: Final = "within_limits"
REASON_ABS_Z_GT_3: Final = "abs_z_gt_3"
REASON_SAME_SIGN_GT_2: Final = "same_sign_pair_gt_2"


class DecimalValidationError(ValueError):
    """十进制字符串不合法（含非正数标准差等语义错误）。"""


def parse_decimal(value: str, *, field: str = "value") -> Decimal:
    """把十进制字符串解析为 Decimal，失败时抛出明确错误。"""

    if not isinstance(value, str) or not DECIMAL_RE.fullmatch(value):
        raise DecimalValidationError(
            f"{field} 必须是十进制字符串，例如 '10.25' 或 '-0.01'，"
            f"不接受科学计数法、NaN、Infinity；收到：{value!r}"
        )
    with localcontext() as ctx:
        ctx.prec = Z_PRECISION
        return Decimal(value)


def parse_positive_decimal(value: str, *, field: str) -> Decimal:
    """标准差等必须严格为正的十进制字段。"""

    parsed = parse_decimal(value, field=field)
    if parsed <= 0:
        raise DecimalValidationError(f"{field} 必须是正数，收到：{value!r}")
    return parsed


def format_decimal(value: Decimal) -> str:
    """以普通十进制字符串输出（不使用科学计数法），并归一化负零。"""

    if value == 0:
        value = Decimal(0)
    return format(value, "f")


def compute_z(measured: Decimal, target: Decimal, stddev: Decimal) -> Decimal:
    """按 z = (实测值 - 靶值) / 标准差 精确计算 z 值。"""

    with localcontext() as ctx:
        ctx.prec = Z_PRECISION
        return (measured - target) / stddev


@dataclass(frozen=True)
class QcBatchInput:
    """重算结论所需的一批质控记录（数值为已解析的 Decimal）。"""

    seq: int
    batch_uid: str
    water_result_ids: tuple[str, ...]
    measured: Decimal
    target: Decimal
    stddev: Decimal


@dataclass(frozen=True)
class QcConclusion:
    """单批质控结论。"""

    seq: int
    batch_uid: str
    z: Decimal
    status: QcStatus
    reason: str


def _sign(value: Decimal) -> int:
    if value > 0:
        return 1
    if value < 0:
        return -1
    return 0


def compute_conclusions(rows: Sequence[QcBatchInput]) -> tuple[QcConclusion, ...]:
    """对同一仪器、同一检测项目的全部批次按序号重算结论。

    “前一批”指序号升序后紧邻的已存在批次（允许补录造成的序号空洞）。
    暂停批次不影响其作为后一批的前一批参与判断。
    """

    ordered = sorted(rows, key=lambda row: row.seq)
    conclusions: list[QcConclusion] = []
    previous: QcConclusion | None = None

    for row in ordered:
        z = compute_z(row.measured, row.target, row.stddev)
        abs_z = abs(z)

        if abs_z > 3:
            status: QcStatus = PAUSED
            reason = REASON_ABS_Z_GT_3
        elif (
            previous is not None
            and _sign(z) != 0
            and _sign(z) == _sign(previous.z)
            and abs_z > 2
            and abs(previous.z) > 2
        ):
            status = PAUSED
            reason = REASON_SAME_SIGN_GT_2
        else:
            status = RELEASED
            reason = REASON_WITHIN_LIMITS

        conclusion = QcConclusion(
            seq=row.seq,
            batch_uid=row.batch_uid,
            z=z,
            status=status,
            reason=reason,
        )
        conclusions.append(conclusion)
        previous = conclusion

    return tuple(conclusions)
