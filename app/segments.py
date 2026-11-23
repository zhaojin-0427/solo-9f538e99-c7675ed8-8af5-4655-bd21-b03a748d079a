"""工况段（设备以同一组封口参数连续运行的极大时间区间）。

规则
----
* 某时刻的有效参数 = 不晚于该时刻的最近一条切换记录；首条切换之前参数未知，
  不形成工况段。
* 相邻切换记录若参数（规范化后）相同，则同属一段——同一组参数连续上报多次
  仍然只算一段；参数换成别的再调回来，前后是两段。
* 段区间为半开 ``[started_at, ended_at)``；``ended_at is None`` 表示该段尚未
  结束（之后再无切换记录）。

本模块只做无 I/O 的纯计算，便于对「拆组 / 并组」做单元测试；SQL 读写见
``repository.py``。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .evaluation import Switch, same_params


@dataclass(frozen=True)
class ConditionSegment:
    """一段参数恒定的工况。"""

    started_at: datetime
    ended_at: datetime | None
    parameters: dict[str, float]


@dataclass(frozen=True)
class SegmentOp:
    """单条工况段变更操作。

    kind:
        * ``insert`` 新增一段
        * ``update`` 修改既有段的终点/参数（行位置由 ``old_started_at`` 标定）
        * ``delete`` 删除既有段（被相邻段并组吸收）
    """

    kind: str
    started_at: datetime
    ended_at: datetime | None
    parameters: dict[str, float]
    old_started_at: datetime | None = None


def compress_segments(switches: list[Switch]) -> list[ConditionSegment]:
    """把切换记录时间线压缩为工况段（全线重建/启动回填用）。

    ``switches`` 无需预先排序，函数内部按时刻排序；同参数的相邻切换合并。
    """
    ordered = sorted(switches, key=lambda switch: switch.at)
    segments: list[ConditionSegment] = []
    for switch in ordered:
        params = dict(switch.params)
        if segments and segments[-1].ended_at is None and same_params(
            segments[-1].parameters, params
        ):
            # 同参数连续上报：不产生新段，当前段继续延伸。
            continue
        if segments:
            segments[-1] = ConditionSegment(
                started_at=segments[-1].started_at,
                ended_at=switch.at,
                parameters=segments[-1].parameters,
            )
        segments.append(
            ConditionSegment(
                started_at=switch.at,
                ended_at=None,
                parameters=dict(params),
            )
        )
    return segments


def plan_segment_changes(
    old_segments: list[ConditionSegment],
    switches: list[Switch],
    changed_intervals: list[dict],
) -> list[SegmentOp]:
    """根据有效参数变更时段，规划工况段的局部增删改。

    只重建与任一变更时段相交的工况段；变更范围之外的段原样保留（不产生
    操作）。重建时自然覆盖两种结果：

    * **拆组**：一段中插入一条不同参数的切换，旧段被切成多段；
    * **并组**：切换被改成同值（或时刻移动）后，相邻同参数段重新连成一段。

    参数
    ----
    old_segments:
        变更前持久化的工况段（按 ``started_at`` 升序）。
    switches:
        变更后的**全量**切换记录（仅在局部窗口内读取）。
    changed_intervals:
        ``evaluation.changed_intervals`` 给出的有效参数差异时段。
    """
    if not changed_intervals:
        return []

    ordered_old = sorted(old_segments, key=lambda segment: segment.started_at)
    ordered_switches = sorted(switches, key=lambda switch: switch.at)

    span_start = min(interval["start"] for interval in changed_intervals)
    span_end_candidates = [
        interval["end"] for interval in changed_intervals if interval["end"] is not None
    ]
    span_end = max(span_end_candidates) if span_end_candidates else None

    affected = [
        segment
        for segment in ordered_old
        if _overlaps(segment.started_at, segment.ended_at, span_start, span_end)
    ]

    # 局部重建窗口 [rebuild_from, rebuild_to)：
    # 起点取最早受影响段的起点（没有受影响段时取变更时段起点）；
    # 终点取首个完全落在变更范围之后的既有段起点，没有则开放到末尾。
    rebuild_from = affected[0].started_at if affected else span_start
    next_outside = next(
        (
            segment
            for segment in ordered_old
            if span_end is not None and segment.started_at >= span_end
        ),
        None,
    )
    rebuild_to = next_outside.started_at if next_outside is not None else None
    prev_segment = next(
        (segment for segment in reversed(ordered_old) if segment.started_at < rebuild_from),
        None,
    )

    runs = _build_runs(ordered_switches, rebuild_from, rebuild_to)

    # 后向并组：窗口左侧既有段与首段同参数 → 吸收，保留旧段起点。
    if runs and prev_segment is not None and same_params(
        prev_segment.parameters, runs[0].parameters
    ):
        head = runs[0]
        runs[0] = ConditionSegment(
            started_at=prev_segment.started_at,
            ended_at=head.ended_at,
            parameters=dict(head.parameters),
        )

    # 前向并组：窗口右侧既有段与尾段同参数 → 尾段延伸，右侧段删除。
    tail_merged = False
    if runs and next_outside is not None and same_params(
        next_outside.parameters, runs[-1].parameters
    ):
        tail = runs[-1]
        runs[-1] = ConditionSegment(
            started_at=tail.started_at,
            ended_at=next_outside.ended_at,
            parameters=dict(tail.parameters),
        )
        tail_merged = True

    # ---- 按键对账：旧段与新段按起点匹配，匹配上 update，否则 delete/insert ----
    touched_old = list(affected)
    if runs and prev_segment is not None and runs[0].started_at == prev_segment.started_at:
        touched_old.append(prev_segment)
    if tail_merged:
        touched_old.append(next_outside)  # type: ignore[arg-type]

    new_by_start = {run.started_at: run for run in runs}
    ops: list[SegmentOp] = []
    for old in touched_old:
        replacement = new_by_start.pop(old.started_at, None)
        if replacement is None:
            ops.append(
                SegmentOp(
                    kind="delete",
                    started_at=old.started_at,
                    ended_at=old.ended_at,
                    parameters=dict(old.parameters),
                    old_started_at=old.started_at,
                )
            )
        else:
            ops.append(
                SegmentOp(
                    kind="update",
                    started_at=replacement.started_at,
                    ended_at=replacement.ended_at,
                    parameters=dict(replacement.parameters),
                    old_started_at=old.started_at,
                )
            )
    for run in new_by_start.values():
        ops.append(
            SegmentOp(
                kind="insert",
                started_at=run.started_at,
                ended_at=run.ended_at,
                parameters=dict(run.parameters),
            )
        )

    ops.sort(key=lambda op: (op.old_started_at or op.started_at, op.kind))
    return ops


def _build_runs(
    ordered_switches: list[Switch],
    rebuild_from: datetime,
    rebuild_to: datetime | None,
) -> list[ConditionSegment]:
    """在半开窗口 ``[rebuild_from, rebuild_to)`` 内重算工况段。

    窗口起点之前的最近一条切换给出窗口开始时的生效参数（可能为 None，即
    参数未知——未知前缀不产生工况段）。窗口内同参数的相邻切换自动合并。
    """
    seed = _latest_switch_before(ordered_switches, rebuild_from)
    seed_params = dict(seed.params) if seed is not None else None

    points: list[tuple[datetime, dict[str, float] | None]] = [
        (rebuild_from, seed_params)
    ]
    for switch in ordered_switches:
        if switch.at >= rebuild_from and (
            rebuild_to is None or switch.at < rebuild_to
        ):
            points.append((switch.at, dict(switch.params)))

    runs: list[dict] = []
    for moment, params in points:
        if runs and same_params(runs[-1]["parameters"], params):
            continue
        if runs:
            runs[-1]["end"] = moment
        runs.append({"start": moment, "end": None, "parameters": params})

    segments = [
        ConditionSegment(
            started_at=run["start"],
            ended_at=run["end"],
            parameters=dict(run["parameters"]),
        )
        for run in runs
        if run["parameters"] is not None
    ]
    if segments:
        segments[-1] = ConditionSegment(
            started_at=segments[-1].started_at,
            ended_at=rebuild_to,
            parameters=dict(segments[-1].parameters),
        )
    return segments


def _overlaps(
    start: datetime,
    end: datetime | None,
    span_start: datetime,
    span_end: datetime | None,
) -> bool:
    """半开区间 ``[start, end)`` 与 ``[span_start, span_end)`` 是否相交。"""
    if end is not None and end <= span_start:
        return False
    if span_end is not None and start >= span_end:
        return False
    return True


def _latest_switch_before(
    ordered_switches: list[Switch], moment: datetime
) -> Switch | None:
    """返回 ``at < moment`` 的最近一条切换（局部重建的种子）。"""
    candidate: Switch | None = None
    for switch in ordered_switches:
        if switch.at < moment:
            candidate = switch
        else:
            break
    return candidate
