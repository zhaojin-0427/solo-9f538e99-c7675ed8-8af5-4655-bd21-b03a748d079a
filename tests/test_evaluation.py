"""核对核心逻辑的单元测试（不依赖 HTTP / SQLite）。"""

from __future__ import annotations

from datetime import datetime, timezone

from app.evaluation import (
    Switch,
    changed_intervals,
    evaluate,
    serialize_interval,
    window_intersects,
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


def at(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)


def sw(record_no: str, text: str, params: dict[str, float] | None = None) -> Switch:
    return Switch(record_no=record_no, at=at(text), params=dict(params or PLAN))


def evaluate_batch(switches: list[Switch]) -> dict:
    return evaluate(
        sorted(switches, key=lambda s: s.at),
        dict(PLAN),
        at("2026-11-23T09:00:00Z"),
        at("2026-11-23T09:30:00Z"),
    )


def test_eligible_when_stable_before_window():
    result = evaluate_batch([sw("SW-1", "2026-11-23T08:50:00Z")])
    assert result["eligible"] is True
    assert result["reason_code"] == "ok"
    assert result["window_start"] == "2026-11-23T08:55:00Z"
    assert result["window_end"] == "2026-11-23T09:30:00Z"
    assert result["first_problem_at"] is None
    assert [s["kind"] for s in result["segments"]] == ["ok"]


def test_switch_exactly_at_window_start_is_effective():
    result = evaluate_batch([sw("SW-1", "2026-11-23T08:55:00Z")])
    assert result["eligible"] is True


def test_unknown_without_any_switch():
    result = evaluate_batch([])
    assert result["eligible"] is False
    assert result["reason_code"] == "unknown_params"
    assert result["first_unknown_at"] == "2026-11-23T08:55:00Z"
    assert result["first_problem_at"] == "2026-11-23T08:55:00Z"
    assert result["segments"][0]["parameters"] is None


def test_unknown_before_first_switch():
    result = evaluate_batch(
        [sw("SW-1", "2026-11-23T08:57:00Z"), sw("SW-2", "2026-11-23T08:59:00Z")]
    )
    assert result["eligible"] is False
    assert result["first_unknown_at"] == "2026-11-23T08:55:00Z"
    assert result["first_problem_kind"] == "unknown"
    # 08:57 起参数恢复正确，但窗口前段已经未知
    assert [s["kind"] for s in result["segments"]] == ["unknown", "ok", "ok"]


def test_mismatch_reports_first_bad_moment_and_matching_switch():
    result = evaluate_batch(
        [
            sw("SW-1", "2026-11-23T08:40:00Z", OTHER),
            sw("SW-2", "2026-11-23T09:10:00Z", PLAN),
        ]
    )
    assert result["eligible"] is False
    assert result["reason_code"] == "parameter_mismatch"
    assert result["first_mismatch_at"] == "2026-11-23T08:55:00Z"
    assert result["first_problem_at"] == "2026-11-23T08:55:00Z"
    assert result["first_problem_switch_record_no"] == "SW-1"
    assert result["first_problem_switch_switched_at"] == "2026-11-23T08:40:00Z"
    assert result["first_unknown_at"] is None


def test_mismatch_in_middle_of_window():
    result = evaluate_batch(
        [
            sw("SW-1", "2026-11-23T08:50:00Z", PLAN),
            sw("SW-2", "2026-11-23T09:05:00Z", OTHER),
            sw("SW-3", "2026-11-23T09:20:00Z", PLAN),
        ]
    )
    assert result["eligible"] is False
    assert result["first_mismatch_at"] == "2026-11-23T09:05:00Z"
    assert [s["kind"] for s in result["segments"]] == ["ok", "mismatch", "ok"]


def test_switch_at_sampling_end_is_outside_window():
    result = evaluate_batch(
        [
            sw("SW-1", "2026-11-23T08:50:00Z", PLAN),
            sw("SW-2", "2026-11-23T09:30:00Z", OTHER),
        ]
    )
    assert result["eligible"] is True


def test_switch_before_window_with_different_params_is_ignored_if_superseded():
    result = evaluate_batch(
        [
            sw("SW-1", "2026-11-23T07:00:00Z", OTHER),
            sw("SW-2", "2026-11-23T08:55:00Z", PLAN),
        ]
    )
    assert result["eligible"] is True


def test_first_problem_is_earliest_of_unknown_and_mismatch():
    # 窗口起点未知（无切换记录）；之后 09:00 生效的一直是不符参数。
    result = evaluate(
        [sw("SW-1", "2026-11-23T09:00:00Z", OTHER)],
        dict(PLAN),
        at("2026-11-23T09:00:00Z"),
        at("2026-11-23T09:30:00Z"),
    )
    assert result["first_unknown_at"] == "2026-11-23T08:55:00Z"
    assert result["first_mismatch_at"] == "2026-11-23T09:00:00Z"
    assert result["first_problem_at"] == "2026-11-23T08:55:00Z"


def test_changed_intervals_insert_bounded_by_next_switch():
    before = [sw("SW-1", "2026-11-23T08:00:00Z", PLAN)]
    after = [
        sw("SW-1", "2026-11-23T08:00:00Z", PLAN),
        sw("SW-2", "2026-11-23T09:00:00Z", OTHER),
    ]
    intervals = changed_intervals(before, after)
    assert len(intervals) == 1
    assert intervals[0]["start"] == at("2026-11-23T09:00:00Z")
    assert intervals[0]["end"] is None
    assert serialize_interval(intervals[0])["after"] == OTHER


def test_changed_intervals_insert_then_superseded_is_bounded():
    before = [
        sw("SW-1", "2026-11-23T08:00:00Z", PLAN),
        sw("SW-2", "2026-11-23T10:00:00Z", PLAN),
    ]
    after = before + [sw("SW-9", "2026-11-23T09:00:00Z", OTHER)]
    intervals = changed_intervals(before, after)
    assert len(intervals) == 1
    assert intervals[0]["start"] == at("2026-11-23T09:00:00Z")
    assert intervals[0]["end"] == at("2026-11-23T10:00:00Z")


def test_changed_intervals_correction_moves_time():
    before = [
        sw("SW-1", "2026-11-23T08:00:00Z", PLAN),
        sw("SW-2", "2026-11-23T09:00:00Z", OTHER),
    ]
    after = [
        sw("SW-1", "2026-11-23T08:00:00Z", PLAN),
        sw("SW-2", "2026-11-23T08:55:00Z", OTHER),
    ]
    intervals = changed_intervals(before, after)
    assert len(intervals) == 1
    assert intervals[0]["start"] == at("2026-11-23T08:55:00Z")
    assert intervals[0]["end"] == at("2026-11-23T09:00:00Z")
    assert intervals[0]["before"] == PLAN
    assert intervals[0]["after"] == OTHER


def test_changed_intervals_noop_returns_empty():
    switches = [sw("SW-1", "2026-11-23T08:00:00Z", PLAN)]
    assert changed_intervals(switches, list(switches)) == []
    # 新插入一条与当前生效值相同的记录，不产生差异时段
    assert changed_intervals(switches, switches + [sw("SW-2", "2026-11-23T09:00:00Z", PLAN)]) == []


def test_changed_intervals_params_only():
    before = [sw("SW-1", "2026-11-23T08:00:00Z", PLAN)]
    after = [sw("SW-1", "2026-11-23T08:00:00Z", OTHER)]
    intervals = changed_intervals(before, after)
    assert len(intervals) == 1
    assert intervals[0]["start"] == at("2026-11-23T08:00:00Z")
    assert intervals[0]["end"] is None


def test_window_intersects_semantics():
    ws = at("2026-11-23T08:55:00Z")
    we = at("2026-11-23T09:30:00Z")
    assert window_intersects(ws, we, {"start": at("2026-11-23T08:00:00Z"), "end": ws}) is False
    assert window_intersects(ws, we, {"start": ws, "end": at("2026-11-23T09:00:00Z")}) is True
    assert window_intersects(ws, we, {"start": we, "end": None}) is False
    assert window_intersects(ws, we, {"start": at("2026-11-23T09:29:00Z"), "end": None}) is True
    assert window_intersects(ws, we, {"start": at("2026-11-23T08:00:00Z"), "end": None}) is True


def test_window_is_five_minutes_before_sampling():
    result = evaluate_batch([sw("SW-1", "2026-11-23T08:54:59Z")])
    assert result["eligible"] is True
    result = evaluate_batch([sw("SW-1", "2026-11-23T08:55:01Z")])
    assert result["eligible"] is False
    assert result["first_unknown_at"] == "2026-11-23T08:55:00Z"


def test_rounding_is_three_decimals():
    rounded = {"seal_temperature_c": 180.0004, "seal_pressure_kpa": 300.0, "dwell_time_s": 1.5}
    result = evaluate_batch([sw("SW-1", "2026-11-23T08:50:00Z", rounded)])
    # 直接构造 Switch 时未规范化，此处应按原始值比较；API 层负责规范化。
    assert result["eligible"] is False
