"""核对核心逻辑（无 I/O，便于单元测试）。

规则
----
* 某时刻的设备有效参数 = 不晚于该时刻的最近一条切换记录的参数；
  在首条切换记录之前为「未知」。
* 批次的检查窗口 = ``[采样开始 - 5 分钟, 采样结束)``（左闭右开）。
  窗口内任一时刻的参数未知或与计划参数不同，该批次即不能纳入分析。
* 更正日志后，比较新旧两条「有效参数时间线」，得到前后参数不同的时段；
  只有检查窗口与这些时段相交的批次才需要重新核对。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .params import PARAM_KEYS
from .timeutil import STABILIZATION, to_iso

UNKNOWN = "unknown"
MISMATCH = "mismatch"
OK = "ok"

ELIGIBLE = "eligible"
INELIGIBLE = "ineligible"


@dataclass(frozen=True)
class Switch:
    """一条设备切换记录。``params`` 已是四舍五入后的规范值。"""

    record_no: str
    at: datetime
    params: dict[str, float]


def effective_switch(switches: list[Switch], moment: datetime) -> Switch | None:
    """返回 ``moment`` 时刻生效（不晚于该时刻的最近一条）的切换记录。

    ``switches`` 必须按 ``at`` 升序；同一时刻全局唯一，无需处理并列。
    """
    found: Switch | None = None
    for switch in switches:
        if switch.at <= moment:
            found = switch
        else:
            break
    return found


def same_params(
    left: dict[str, float] | None, right: dict[str, float] | None
) -> bool:
    if left is None or right is None:
        return left is right
    return all(left[key] == right[key] for key in PARAM_KEYS)


def _segment(
    start: datetime,
    end: datetime,
    switch: Switch | None,
    plan: dict[str, float],
) -> dict:
    params = dict(switch.params) if switch is not None else None
    if params is None:
        kind = UNKNOWN
        matches = False
    else:
        matches = same_params(params, plan)
        kind = OK if matches else MISMATCH
    return {
        "start": start,
        "end": end,
        "parameters": params,
        "matches_plan": matches,
        "kind": kind,
        "switch_record_no": switch.record_no if switch is not None else None,
        "switch_switched_at": switch.at if switch is not None else None,
    }


def _serialize_segment(segment: dict) -> dict:
    return {
        "start": to_iso(segment["start"]),
        "end": to_iso(segment["end"]),
        "parameters": segment["parameters"],
        "matches_plan": segment["matches_plan"],
        "kind": segment["kind"],
        "switch_record_no": segment["switch_record_no"],
        "switch_switched_at": (
            to_iso(segment["switch_switched_at"])
            if segment["switch_switched_at"] is not None
            else None
        ),
    }


def evaluate(
    switches: list[Switch],
    plan: dict[str, float],
    sampling_start: datetime,
    sampling_end: datetime,
) -> dict:
    """核对一个批次，返回可直接持久化/序列化的结果。"""
    window_start = sampling_start - STABILIZATION
    window_end = sampling_end

    # 窗口内部（严格晚于起点、严格早于终点）发生的切换；
    # 恰好落在起点的切换由 effective_switch 覆盖。
    inside = [switch for switch in switches if window_start < switch.at < window_end]
    initial = effective_switch(switches, window_start)

    points: list[tuple[datetime, Switch | None]] = [(window_start, initial)]
    points.extend((switch.at, switch) for switch in inside)

    segments: list[dict] = []
    for index, (start, switch) in enumerate(points):
        end = points[index + 1][0] if index + 1 < len(points) else window_end
        if end <= start:  # 防御性分支
            continue
        segments.append(_segment(start, end, switch, plan))

    violations = [segment for segment in segments if segment["kind"] != OK]
    unknown = next((s for s in violations if s["kind"] == UNKNOWN), None)
    mismatch = next((s for s in violations if s["kind"] == MISMATCH), None)

    candidates = [s for s in (unknown, mismatch) if s is not None]
    first_problem = min(candidates, key=lambda s: s["start"]) if candidates else None

    eligible = not violations
    if eligible:
        reason_code = "ok"
    elif first_problem is not None and first_problem["kind"] == UNKNOWN:
        reason_code = "unknown_params"
    else:
        reason_code = "parameter_mismatch"

    return {
        "eligible": eligible,
        "status": ELIGIBLE if eligible else INELIGIBLE,
        "reason_code": reason_code,
        "window_start": to_iso(window_start),
        "window_end": to_iso(window_end),
        "first_problem_at": to_iso(first_problem["start"]) if first_problem else None,
        "first_problem_kind": first_problem["kind"] if first_problem else None,
        "first_mismatch_at": to_iso(mismatch["start"]) if mismatch else None,
        "first_unknown_at": to_iso(unknown["start"]) if unknown else None,
        "first_problem_switch_record_no": (
            first_problem["switch_record_no"] if first_problem else None
        ),
        "first_problem_switch_switched_at": (
            to_iso(first_problem["switch_switched_at"])
            if first_problem is not None
            and first_problem["switch_switched_at"] is not None
            else None
        ),
        "segments": [_serialize_segment(segment) for segment in segments],
        "violations": [
            {
                "at": to_iso(segment["start"]),
                "kind": segment["kind"],
                "parameters": segment["parameters"],
                "switch_record_no": segment["switch_record_no"],
                "switch_switched_at": (
                    to_iso(segment["switch_switched_at"])
                    if segment["switch_switched_at"] is not None
                    else None
                ),
            }
            for segment in violations
        ],
    }


def changed_intervals(before: list[Switch], after: list[Switch]) -> list[dict]:
    """比较两条有效参数时间线，返回前后参数不同的时段。

    ``before`` / ``after`` 为 ``None`` 表示该时段参数未知。
    时段 ``end`` 为 ``None`` 表示一直延伸到无穷（之后不再有切换记录）。
    """
    before = sorted(before, key=lambda switch: switch.at)
    after = sorted(after, key=lambda switch: switch.at)
    moments = sorted(
        {switch.at for switch in before} | {switch.at for switch in after}
    )
    intervals: list[dict] = []

    for index, moment in enumerate(moments):
        following = moments[index + 1] if index + 1 < len(moments) else None
        old = effective_switch(before, moment)
        new = effective_switch(after, moment)
        old_params = dict(old.params) if old is not None else None
        new_params = dict(new.params) if new is not None else None

        if same_params(old_params, new_params):
            # 该时刻前后参数相同，[moment, following) 内不会有差异，
            # 因此任何在此结束的变更时段都不能再继续延伸。
            continue

        if (
            intervals
            and intervals[-1]["end"] == moment
            and same_params(intervals[-1]["before"], old_params)
            and same_params(intervals[-1]["after"], new_params)
        ):
            intervals[-1]["end"] = following
            continue

        intervals.append(
            {
                "start": moment,
                "end": following,
                "before": old_params,
                "after": new_params,
            }
        )

    return intervals


def window_intersects(
    window_start: datetime,
    window_end: datetime,
    interval: dict,
) -> bool:
    """半开区间 ``[window_start, window_end)`` 与变更时段是否有正长度交集。"""
    start: datetime = interval["start"]
    end: datetime | None = interval["end"]
    if end is not None and end <= window_start:
        return False
    if start >= window_end:
        return False
    return True


def serialize_interval(interval: dict) -> dict:
    return {
        "start": to_iso(interval["start"]),
        "end": to_iso(interval["end"]) if interval["end"] is not None else None,
        "before": interval["before"],
        "after": interval["after"],
    }


def check_window(
    sampling_start: datetime, sampling_end: datetime
) -> tuple[datetime, datetime]:
    return sampling_start - STABILIZATION, sampling_end
