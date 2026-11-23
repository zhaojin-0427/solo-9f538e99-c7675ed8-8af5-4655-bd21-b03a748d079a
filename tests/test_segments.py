"""工况段压缩与局部增量规划的单元测试（不依赖 HTTP / SQLite）。"""

from __future__ import annotations

from datetime import datetime, timezone

from app.evaluation import Switch, changed_intervals
from app.segments import (
    ConditionSegment,
    compress_segments,
    plan_segment_changes,
)

UTC = timezone.utc
PLAN = {
    "seal_temperature_c": 180.0,
    "seal_pressure_kpa": 300.0,
    "dwell_time_s": 1.5,
}
OTHER = {
    "seal_temperature_c": 170.0,
    "seal_pressure_kpa": 280.0,
    "dwell_time_s": 1.2,
}
THIRD = {
    "seal_temperature_c": 190.0,
    "seal_pressure_kpa": 310.0,
    "dwell_time_s": 2.0,
}


def at(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)


def sw(record_no: str, text: str, params: dict[str, float]) -> Switch:
    return Switch(record_no=record_no, at=at(text), params=dict(params))


def seg(start: str, end: str | None, params: dict[str, float]) -> ConditionSegment:
    return ConditionSegment(
        started_at=at(start),
        ended_at=None if end is None else at(end),
        parameters=dict(params),
    )


def apply(ops, old):
    """在内存中模拟 SQL apply（delete→update→insert），返回新段集合。"""
    by_start = {s.started_at: s for s in old}
    for op in ops:
        if op.kind == "delete":
            by_start.pop(op.old_started_at, None)
    for op in ops:
        if op.kind == "update":
            by_start.pop(op.old_started_at, None)
            by_start[op.started_at] = ConditionSegment(
                op.started_at, op.ended_at, dict(op.parameters)
            )
    for op in ops:
        if op.kind == "insert":
            by_start[op.started_at] = ConditionSegment(
                op.started_at, op.ended_at, dict(op.parameters)
            )
    return sorted(by_start.values(), key=lambda s: s.started_at)


def plan(old_segments, before, after):
    intervals = changed_intervals(before, after)
    return plan_segment_changes(old_segments, after, intervals)


# ------------------------------------------------------------ compress


def test_compress_merges_consecutive_same_params():
    """同参数连续上报多次仍算一段。"""
    switches = [
        sw("SW-1", "2026-11-23T08:00:00Z", PLAN),
        sw("SW-2", "2026-11-23T09:00:00Z", PLAN),
        sw("SW-3", "2026-11-23T10:00:00Z", PLAN),
    ]
    segments = compress_segments(switches)
    assert segments == [seg("2026-11-23T08:00:00Z", None, PLAN)]


def test_compress_splits_different_params_and_rejoin_counts_two():
    """调成别的参数再调回来，前后算两段。"""
    switches = [
        sw("SW-1", "2026-11-23T08:00:00Z", PLAN),
        sw("SW-2", "2026-11-23T09:00:00Z", OTHER),
        sw("SW-3", "2026-11-23T10:00:00Z", PLAN),
    ]
    segments = compress_segments(switches)
    assert segments == [
        seg("2026-11-23T08:00:00Z", "2026-11-23T09:00:00Z", PLAN),
        seg("2026-11-23T09:00:00Z", "2026-11-23T10:00:00Z", OTHER),
        seg("2026-11-23T10:00:00Z", None, PLAN),
    ]


def test_compress_empty():
    assert compress_segments([]) == []


# ------------------------------------------------------------ 拆组


def test_split_one_segment_into_three_by_insert():
    """段中插入不同参数切换：一段拆成三段，两侧段保留原起点。"""
    before = [sw("SW-1", "2026-11-23T08:00:00Z", PLAN)]
    after = before + [
        sw("SW-2", "2026-11-23T09:00:00Z", OTHER),
        sw("SW-3", "2026-11-23T10:00:00Z", PLAN),
    ]
    old = compress_segments(before)
    ops = plan(old, before, after)
    result = apply(ops, old)
    assert result == compress_segments(after)
    # 旧段起点 08:00 被 update（终点改为 09:00），另两段为 insert
    assert any(op.kind == "update" and op.old_started_at == at("2026-11-23T08:00:00Z") for op in ops)
    assert sum(1 for op in ops if op.kind == "insert") == 2
    assert not any(op.kind == "delete" for op in ops)


def test_split_open_ended_segment_keeps_null_end():
    """未结束段拆组后，最后一段终点仍为 null。"""
    before = [sw("SW-1", "2026-11-23T08:00:00Z", PLAN)]
    after = before + [sw("SW-2", "2026-11-23T09:00:00Z", OTHER)]
    old = compress_segments(before)
    result = apply(plan(old, before, after), old)
    assert result == [
        seg("2026-11-23T08:00:00Z", "2026-11-23T09:00:00Z", PLAN),
        seg("2026-11-23T09:00:00Z", None, OTHER),
    ]


# ------------------------------------------------------------ 并组


def test_merge_three_segments_into_one_by_correcting_to_same_params():
    """把中间段改成与两侧同参数：三段并成一段，两侧段被删除吸收。"""
    before = [
        sw("SW-1", "2026-11-23T08:00:00Z", PLAN),
        sw("SW-2", "2026-11-23T09:00:00Z", OTHER),
        sw("SW-3", "2026-11-23T10:00:00Z", PLAN),
    ]
    after = [
        sw("SW-1", "2026-11-23T08:00:00Z", PLAN),
        sw("SW-2", "2026-11-23T09:00:00Z", PLAN),
        sw("SW-3", "2026-11-23T10:00:00Z", PLAN),
    ]
    old = compress_segments(before)
    ops = plan(old, before, after)
    result = apply(ops, old)
    assert result == [seg("2026-11-23T08:00:00Z", None, PLAN)]
    # 09:00、10:00 两个旧段起点都应被删除；08:00 段 update 为开放段
    deleted = {op.old_started_at for op in ops if op.kind == "delete"}
    assert at("2026-11-23T09:00:00Z") in deleted
    assert at("2026-11-23T10:00:00Z") in deleted
    head = next(op for op in ops if op.old_started_at == at("2026-11-23T08:00:00Z"))
    assert head.kind == "update"
    assert head.ended_at is None


def test_merge_with_left_neighbour_only():
    """并组只发生在窗口左侧：保留左段起点，删除受影响首段。"""
    before = [
        sw("SW-1", "2026-11-23T08:00:00Z", PLAN),
        sw("SW-2", "2026-11-23T09:00:00Z", OTHER),
        sw("SW-3", "2026-11-23T10:00:00Z", THIRD),
    ]
    # 把 09:00 的 OTHER 改成 PLAN → 08:00 段吸收到 10:00
    after = [
        sw("SW-1", "2026-11-23T08:00:00Z", PLAN),
        sw("SW-2", "2026-11-23T09:00:00Z", PLAN),
        sw("SW-3", "2026-11-23T10:00:00Z", THIRD),
    ]
    old = compress_segments(before)
    result = apply(plan(old, before, after), old)
    assert result == [
        seg("2026-11-23T08:00:00Z", "2026-11-23T10:00:00Z", PLAN),
        seg("2026-11-23T10:00:00Z", None, THIRD),
    ]


def test_merge_with_right_neighbour_only():
    """并组只发生在窗口右侧：尾段延伸，右侧段删除。"""
    before = [
        sw("SW-1", "2026-11-23T08:00:00Z", THIRD),
        sw("SW-2", "2026-11-23T09:00:00Z", OTHER),
        sw("SW-3", "2026-11-23T10:00:00Z", PLAN),
    ]
    # 把 09:00 的 OTHER 改成 PLAN → 与 10:00 段并组
    after = [
        sw("SW-1", "2026-11-23T08:00:00Z", THIRD),
        sw("SW-2", "2026-11-23T09:00:00Z", PLAN),
        sw("SW-3", "2026-11-23T10:00:00Z", PLAN),
    ]
    old = compress_segments(before)
    result = apply(plan(old, before, after), old)
    assert result == [
        seg("2026-11-23T08:00:00Z", "2026-11-23T09:00:00Z", THIRD),
        seg("2026-11-23T09:00:00Z", None, PLAN),
    ]


# ------------------------------------------------------------ 时刻移动


def test_move_switch_time_shifts_segment_boundary():
    """移动切换时刻：相邻段边界平移，不产生多余段。"""
    before = [
        sw("SW-1", "2026-11-23T08:00:00Z", PLAN),
        sw("SW-2", "2026-11-23T09:00:00Z", OTHER),
    ]
    after = [
        sw("SW-1", "2026-11-23T08:00:00Z", PLAN),
        sw("SW-2", "2026-11-23T09:30:00Z", OTHER),
    ]
    old = compress_segments(before)
    result = apply(plan(old, before, after), old)
    assert result == [
        seg("2026-11-23T08:00:00Z", "2026-11-23T09:30:00Z", PLAN),
        seg("2026-11-23T09:30:00Z", None, OTHER),
    ]


def test_no_change_intervals_means_no_ops():
    switches = [sw("SW-1", "2026-11-23T08:00:00Z", PLAN)]
    old = compress_segments(switches)
    assert plan_segment_changes(old, switches, []) == []


def test_insert_same_value_creates_no_segments_change():
    """插入同参数切换不改变有效参数时间线 → 无变更时段 → 无操作。"""
    before = [sw("SW-1", "2026-11-23T08:00:00Z", PLAN)]
    after = before + [sw("SW-2", "2026-11-23T09:00:00Z", PLAN)]
    old = compress_segments(before)
    ops = plan(old, before, after)
    assert ops == []
    assert apply(ops, old) == old


# ------------------------------------------------------------ 综合不变量


def test_repeated_operations_converge_to_full_compression():
    """随机风格的连续补改后，增量结果始终等于全线压缩结果。"""
    timeline = [
        sw("SW-1", "2026-11-23T08:00:00Z", PLAN),
        sw("SW-2", "2026-11-23T09:00:00Z", OTHER),
        sw("SW-3", "2026-11-23T10:00:00Z", THIRD),
        sw("SW-4", "2026-11-23T11:00:00Z", PLAN),
        sw("SW-5", "2026-11-23T12:00:00Z", OTHER),
    ]
    current: list[Switch] = []
    segments: list[ConditionSegment] = []
    for switch in timeline:
        after = sorted(current + [switch], key=lambda s: s.at)
        ops = plan(segments, current, after)
        segments = apply(ops, segments)
        assert segments == compress_segments(after)
        current = after

    # 再逐条更正为同参数，验证持续并组
    for index, switch in enumerate(timeline):
        corrected = Switch(
            record_no=switch.record_no, at=switch.at, params=dict(PLAN)
        )
        after = [corrected if s.record_no == switch.record_no else s for s in current]
        ops = plan(segments, current, after)
        segments = apply(ops, segments)
        assert segments == compress_segments(after)
        current = after
    assert segments == [seg("2026-11-23T08:00:00Z", None, PLAN)]
