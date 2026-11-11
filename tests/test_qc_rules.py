from __future__ import annotations

from decimal import Decimal

import pytest

from app.qc_rules import (
    PAUSED,
    RELEASED,
    REASON_ABS_Z_GT_3,
    REASON_SAME_SIGN_GT_2,
    REASON_WITHIN_LIMITS,
    DecimalValidationError,
    QcBatchInput,
    compute_conclusions,
    compute_z,
    format_decimal,
    parse_decimal,
    parse_positive_decimal,
)


def make_input(seq: int, measured: str, target: str = "10.00", stddev: str = "0.20",
               uid: str | None = None, ids: tuple[str, ...] = ("W1",)) -> QcBatchInput:
    return QcBatchInput(
        seq=seq,
        batch_uid=uid or f"B{seq}",
        water_result_ids=ids,
        measured=parse_decimal(measured),
        target=parse_decimal(target),
        stddev=parse_positive_decimal(stddev, field="stddev"),
    )


def test_parse_decimal_rejects_non_decimal_strings():
    for bad in ["1e3", "NaN", "Infinity", "-Infinity", " 1.0", "1.0 ", "", "abc", "1.2.3"]:
        with pytest.raises(DecimalValidationError):
            parse_decimal(bad)
    # 普通十进制写法都接受，包括前导正号与纯小数。
    for good in ["0", "-0.00", "+1.25", ".5", "-.5", "12345678901234567890.123456789"]:
        assert isinstance(parse_decimal(good), Decimal)


def test_positive_decimal_rejects_zero_and_negative():
    with pytest.raises(DecimalValidationError):
        parse_positive_decimal("0", field="stddev")
    with pytest.raises(DecimalValidationError):
        parse_positive_decimal("-0.01", field="stddev")
    parse_positive_decimal("0.0000001", field="stddev")


def test_z_uses_exact_decimal_arithmetic():
    z = compute_z(Decimal("10.82"), Decimal("10.00"), Decimal("0.20"))
    assert z == Decimal("4.1")
    assert format_decimal(z) == "4.1"
    # 除不尽时按高精度保留且不使用科学计数法。
    z_repeating = compute_z(Decimal("1"), Decimal("0"), Decimal("3"))
    text = format_decimal(z_repeating)
    assert "E" not in text and "e" not in text
    assert text.startswith("0.33333333333333333333333333333333333333333333333333")


def test_negative_zero_normalized():
    assert format_decimal(Decimal("-0.000")) == "0"


def test_first_batch_within_limits_is_released():
    conclusions = compute_conclusions([make_input(1, "10.20")])
    assert conclusions[0].status == RELEASED
    assert conclusions[0].reason == REASON_WITHIN_LIMITS
    assert conclusions[0].z == Decimal("1")


def test_abs_z_above_three_pauses():
    conclusions = compute_conclusions([make_input(1, "10.82")])  # z = 4.1
    assert conclusions[0].status == PAUSED
    assert conclusions[0].reason == REASON_ABS_Z_GT_3


def test_boundary_z_equals_three_is_released():
    # z 恰好 = 3：|z|>3 不成立，且没有前一批 → 放行（严格大于）。
    conclusions = compute_conclusions([make_input(1, "10.60")])
    assert conclusions[0].status == RELEASED


def test_same_sign_pair_both_above_two_pauses_second():
    # 两批 z 分别为 2.5、2.6：第一批放行，第二批因同号且都 >2 暂停。
    conclusions = compute_conclusions(
        [make_input(1, "10.50", uid="B1"), make_input(2, "10.52", uid="B2")]
    )
    assert conclusions[0].status == RELEASED
    assert conclusions[1].status == PAUSED
    assert conclusions[1].reason == REASON_SAME_SIGN_GT_2


def test_opposite_signs_do_not_trigger_pair_rule():
    # z = +2.5 与 -2.5：异号，第二批仍放行。
    conclusions = compute_conclusions(
        [make_input(1, "10.50", uid="B1"), make_input(2, "9.50", uid="B2")]
    )
    assert conclusions[0].status == RELEASED
    assert conclusions[1].status == RELEASED


def test_zero_z_has_no_sign_so_pair_rule_not_triggered():
    # 前一批 z=2.5，本批 z=0：0 无符号，不能算同号。
    conclusions = compute_conclusions(
        [make_input(1, "10.50", uid="B1"), make_input(2, "10.00", uid="B2")]
    )
    assert conclusions[1].status == RELEASED


def test_boundary_z_equals_two_does_not_trigger_pair_rule():
    # z 恰好 2 与 2.1：第一批不满足 >2，第二批放行。
    conclusions = compute_conclusions(
        [make_input(1, "10.40", uid="B1"), make_input(2, "10.42", uid="B2")]
    )
    assert conclusions[0].status == RELEASED
    assert conclusions[1].status == RELEASED


def test_paused_batch_still_participates_in_next_judgement():
    # 第 1 批 z=4.1 暂停；第 2 批 z=2.5，与前一批同号且都 >2 → 也暂停。
    conclusions = compute_conclusions(
        [make_input(1, "10.82", uid="B1"), make_input(2, "10.50", uid="B2")]
    )
    assert conclusions[0].status == PAUSED
    assert conclusions[1].status == PAUSED
    assert conclusions[1].reason == REASON_SAME_SIGN_GT_2


def test_pair_rule_uses_immediate_predecessor_only():
    # z: 2.5（放行）, 1.0（放行）, 2.6（前一批 z=1.0，不触发）→ 全放行。
    conclusions = compute_conclusions(
        [
            make_input(1, "10.50", uid="B1"),
            make_input(2, "10.20", uid="B2"),
            make_input(3, "10.52", uid="B3"),
        ]
    )
    assert [c.status for c in conclusions] == [RELEASED, RELEASED, RELEASED]


def test_negative_same_sign_pair_pauses():
    # z = -2.5、-2.6：同负号且都 >2 → 第二批暂停。
    conclusions = compute_conclusions(
        [make_input(1, "9.50", uid="B1"), make_input(2, "9.48", uid="B2")]
    )
    assert conclusions[0].status == RELEASED
    assert conclusions[1].status == PAUSED


def test_conclusions_sorted_by_seq_even_if_input_unsorted():
    conclusions = compute_conclusions(
        [make_input(2, "10.52", uid="B2"), make_input(1, "10.50", uid="B1")]
    )
    assert [c.seq for c in conclusions] == [1, 2]
    assert conclusions[1].status == PAUSED


def test_gap_in_seq_treats_nearest_existing_batch_as_predecessor():
    # 序号 1 与 3（空洞在 2）：补录前判断 3 时，前一批仍是序号 1。
    conclusions = compute_conclusions(
        [make_input(1, "10.50", uid="B1"), make_input(3, "10.52", uid="B3")]
    )
    assert conclusions[1].status == PAUSED
