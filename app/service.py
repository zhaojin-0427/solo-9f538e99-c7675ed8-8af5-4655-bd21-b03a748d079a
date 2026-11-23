"""业务编排：录入批次、补录/更正切换记录、增量重查。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import datetime

from . import repository as repo
from .db import Database, loads, transaction
from .evaluation import (
    Switch,
    changed_intervals,
    check_window,
    evaluate,
    serialize_interval,
    window_intersects,
)
from .params import PARAM_KEYS, params_from_row, params_to_json
from .segments import compress_segments, plan_segment_changes
from .timeutil import STABILIZATION, parse_utc, to_iso, utcnow

REASONS = {
    "ok": "检查窗口内计划参数持续有效，可纳入分析。",
    "parameter_mismatch": "检查窗口内存在与计划参数不符的设备参数，不能纳入分析。",
    "unknown_params": "检查窗口早于首条切换记录，参数未知，不能纳入分析。",
}

REQUIRED_SECONDS = int(STABILIZATION.total_seconds())


class ServiceError(Exception):
    """带 HTTP 语义的业务异常。"""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def _transition(previous: bool | None, current: bool) -> str:
    if previous is None:
        return "initial"
    if previous and not current:
        return "revoked"
    if not previous and current:
        return "restored"
    return "unchanged"


class CheckService:
    def __init__(self, database: Database) -> None:
        self.database = database

    # ---------------------------------------------------------------- 连接

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        with closing(self.database.connect()) as conn:
            yield conn

    # ------------------------------------------------------------ 启动回填

    def ensure_segments(self) -> None:
        """旧 SQLite 卷启动时一次性回填工况段；新卷/已回填则空操作。

        在单个事务内完成：读全量切换 → 压缩成段 → 仅当段表为空时写入。
        后续补改走 :meth:`_maintain_segments` 的局部增量维护。
        """
        now = to_iso(utcnow())
        with self._connection() as conn:
            with transaction(conn):
                if repo.count_segments(conn) > 0:
                    return
                for segment in compress_segments(repo.load_switches(conn)):
                    repo.insert_segment(conn, segment, now=now)

    # ------------------------------------------------------------------ 批次

    def create_batch(
        self,
        *,
        batch_no: str,
        plan: dict[str, float],
        sampling_start: datetime,
        sampling_end: datetime,
    ) -> dict:
        now = to_iso(utcnow())
        with self._connection() as conn:
            try:
                with transaction(conn):
                    if repo.fetch_batch(conn, batch_no) is not None:
                        raise ServiceError(
                            409, "batch_exists", f"批号 {batch_no!r} 已存在。"
                        )
                    repo.insert_batch(
                        conn,
                        batch_no=batch_no,
                        plan=plan,
                        sampling_start=sampling_start,
                        sampling_end=sampling_end,
                        now=now,
                    )
                    batch = repo.fetch_batch(conn, batch_no)
                    assert batch is not None
                    _persist_evaluation(
                        conn,
                        batch,
                        repo.load_switches(conn),
                        trigger="batch_created",
                        trigger_record_no=None,
                        now=now,
                    )
            except sqlite3.IntegrityError as exc:  # 并发下唯一约束兜底
                raise ServiceError(
                    409, "batch_exists", f"批号 {batch_no!r} 已存在。"
                ) from exc
            return self._detail(conn, batch_no)

    def list_batches(self, limit: int, offset: int) -> dict:
        with self._connection() as conn:
            total = repo.count_batches(conn)
            rows = repo.list_batches(conn, limit, offset)
            items = [
                self._summary(batch, repo.latest_revision(conn, batch["id"]))
                for batch in rows
            ]
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    def get_batch(self, batch_no: str) -> dict:
        with self._connection() as conn:
            return self._detail(conn, batch_no)

    def check_batch(self, batch_no: str) -> dict:
        with self._connection() as conn:
            detail = self._detail(conn, batch_no)
        return {
            **detail["current_result"],
            "revision": detail["revision"],
            "revision_history": detail["revision_history"],
        }

    # ------------------------------------------------------------ 工况分组

    def condition_groups(
        self, *, start: datetime, end: datetime
    ) -> dict:
        """按采样开始时刻的半开区间 [start, end) 汇总工况段与合格批次。

        一次只读连接、同一已提交状态：

        * 工况段走 ``started_at`` 索引做区间定位（含没有样本的空段），
          不扫描全线切换历史，也不逐条读取段内同值切换记录；
        * 合格批次走 ``sampling_start`` 索引做范围扫描，在内存中按所属
          工况段归组；不合格批次不入选，只计入 ``total_batch_count``。
        """
        start_iso = to_iso(start)
        end_iso = to_iso(end)
        with self._connection() as conn:
            segment_rows = repo.find_segments_overlapping_range(conn, start, end)
            batch_rows = repo.find_eligible_batches_in_range(conn, start, end)
            total_in_range = repo.count_batches_in_range(conn, start, end)

            segments: list[dict] = []
            index_by_start: dict[str, int] = {}
            for row in segment_rows:
                index_by_start[row["started_at"]] = len(segments)
                segments.append(
                    {
                        "started_at": row["started_at"],
                        "ended_at": row["ended_at"],
                        "parameters": params_to_json(params_from_row(row)),
                        "batch_nos": [],
                    }
                )

            for batch in batch_rows:
                segment_row = repo.find_segment_for_moment(
                    conn, parse_utc(batch["sampling_start"])
                )
                if segment_row is None:
                    # 合格批次必然落在某个有效工况段内；出现即说明数据不一致。
                    raise ServiceError(
                        500,
                        "segment_not_found",
                        f"批次 {batch['batch_no']!r} 合格但找不到所属工况段，"
                        "请检查工况段回填状态。",
                    )
                target = index_by_start.get(segment_row["started_at"])
                if target is None:
                    # 理论不可达：合格批次的工况段必与查询区间相交。
                    raise ServiceError(
                        500,
                        "segment_out_of_range",
                        f"批次 {batch['batch_no']!r} 所属工况段不在查询区间内。",
                    )
                segments[target]["batch_nos"].append(batch["batch_no"])

            parameter_stats: dict[str, dict] = {}
            for segment in segments:
                param_key = dumps_key(segment["parameters"])
                stat = parameter_stats.setdefault(
                    param_key,
                    {
                        "parameters": segment["parameters"],
                        "segment_count": 0,
                        "batch_count": 0,
                    },
                )
                stat["segment_count"] += 1
                stat["batch_count"] += len(segment["batch_nos"])

        return {
            "query": {"start": start_iso, "end": end_iso},
            "segments": segments,
            "parameter_stats": list(parameter_stats.values()),
            "segment_count": len(segments),
            "eligible_batch_count": len(batch_rows),
            "total_batch_count": total_in_range,
        }

    def _summary(self, batch: sqlite3.Row, revision: sqlite3.Row | None) -> dict:
        if revision is None:  # 理论上不会出现：批次创建时即写入首条结论
            return {
                "batch_no": batch["batch_no"],
                "revision": batch["current_revision"],
                "status": None,
                "eligible": None,
                "reason_code": None,
                "sampling_start": batch["sampling_start"],
                "sampling_end": batch["sampling_end"],
                "plan_parameters": params_to_json(params_from_row(batch, prefix="plan_")),
                "first_problem_at": None,
                "first_problem_kind": None,
                "first_mismatch_at": None,
                "first_unknown_at": None,
                "evaluated_at": None,
            }
        return {
            "batch_no": batch["batch_no"],
            "revision": batch["current_revision"],
            "status": revision["status"],
            "eligible": bool(revision["eligible"]),
            "reason_code": revision["reason_code"],
            "sampling_start": batch["sampling_start"],
            "sampling_end": batch["sampling_end"],
            "plan_parameters": params_to_json(params_from_row(batch, prefix="plan_")),
            "first_problem_at": revision["first_problem_at"],
            "first_problem_kind": revision["first_problem_kind"],
            "first_mismatch_at": revision["first_mismatch_at"],
            "first_unknown_at": revision["first_unknown_at"],
            "evaluated_at": revision["created_at"],
        }

    def _detail(self, conn: sqlite3.Connection, batch_no: str) -> dict:
        batch = repo.fetch_batch(conn, batch_no)
        if batch is None:
            raise ServiceError(404, "batch_not_found", f"批号 {batch_no!r} 不存在。")
        revision = repo.latest_revision(conn, batch["id"])
        if revision is None:
            raise ServiceError(
                409, "batch_not_evaluated", f"批号 {batch_no!r} 尚无核对结果。"
            )
        history = [
            _revision_summary(row, loads(row["result_json"]))
            for row in repo.list_revisions(conn, batch["id"])
        ]
        return {
            "batch_no": batch["batch_no"],
            "revision": batch["current_revision"],
            "current_result": loads(revision["result_json"]),
            "revision_history": history,
            "created_at": batch["created_at"],
            "updated_at": batch["updated_at"],
        }

    # ------------------------------------------------------------ 切换记录

    def list_switches(self) -> dict:
        with self._connection() as conn:
            rows = conn.execute(
                repo.SWITCH_SELECT + " ORDER BY switched_at, record_no"
            ).fetchall()
        items = [_switch_payload(row) for row in rows]
        return {"items": items, "total": len(items)}

    def get_switch(self, record_no: str) -> dict:
        with self._connection() as conn:
            row = repo.fetch_switch(conn, record_no)
        if row is None:
            raise ServiceError(
                404, "switch_not_found", f"切换记录 {record_no!r} 不存在。"
            )
        return _switch_payload(row)

    def upsert_switch(
        self,
        record_no: str,
        *,
        switched_at: datetime,
        params: dict[str, float],
    ) -> dict:
        now = to_iso(utcnow())
        with self._connection() as conn:
            try:
                with transaction(conn):
                    before = repo.load_switches(conn)
                    existing = repo.fetch_switch(conn, record_no)

                    conflict = repo.switch_by_time(conn, to_iso(switched_at))
                    if conflict is not None and conflict["record_no"] != record_no:
                        raise ServiceError(
                            409,
                            "switch_time_conflict",
                            f"切换时刻 {to_iso(switched_at)} 已被记录 "
                            f"{conflict['record_no']!r} 占用，切换时刻不能重复。",
                        )

                    if existing is None:
                        repo.insert_switch(
                            conn,
                            record_no=record_no,
                            switched_at=switched_at,
                            params=params,
                            now=now,
                        )
                        operation = "created"
                        trigger = "switch_created"
                    elif _switch_is_unchanged(existing, switched_at, params):
                        operation = "unchanged"
                        trigger = "switch_unchanged"
                    else:
                        repo.update_switch(
                            conn,
                            record_no=record_no,
                            switched_at=switched_at,
                            params=params,
                            now=now,
                        )
                        operation = "updated"
                        trigger = "switch_updated"

                    intervals = changed_intervals(before, repo.load_switches(conn))
                    # 先重查批次（最易失败的业务计算），成功后再维护工况段；
                    # 两者在同一事务内，任一步失败整体回滚，段表沿用原分组。
                    rechecked = _recheck_batches(
                        conn,
                        intervals,
                        trigger=trigger,
                        trigger_record_no=record_no,
                        now=now,
                    )
                    segment_ops = self._maintain_segments(conn, intervals, now=now)
                    row = repo.fetch_switch(conn, record_no)
                    assert row is not None
                    payload = _switch_payload(row)
            except sqlite3.IntegrityError as exc:  # 并发下唯一约束兜底
                raise ServiceError(
                    409,
                    "switch_time_conflict",
                    f"切换时刻 {to_iso(switched_at)} 已被其他记录占用，"
                    "或记录号与切换时刻冲突，切换时刻不能重复。",
                ) from exc

        return {
            "operation": operation,
            "switch": payload,
            "changed_intervals": [serialize_interval(i) for i in intervals],
            "segment_changes": [_segment_op_payload(op) for op in segment_ops],
            "rechecked_batches": rechecked,
            "rechecked_count": len(rechecked),
        }

    def _maintain_segments(self, conn, intervals, *, now: str):
        """只更新与变更时段相交的工况段；无差异时段时不动任何段。"""
        if not intervals:
            return []
        old_segments = repo.load_segments(conn)
        ops = plan_segment_changes(old_segments, repo.load_switches(conn), intervals)
        repo.apply_segment_ops(conn, ops, now=now)
        return ops


def _switch_is_unchanged(
    row: sqlite3.Row, switched_at: datetime, params: dict[str, float]
) -> bool:
    if parse_utc(row["switched_at"]) != switched_at:
        return False
    return (
        row["temperature_c"] == params["seal_temperature_c"]
        and row["pressure_kpa"] == params["seal_pressure_kpa"]
        and row["dwell_time_s"] == params["dwell_time_s"]
    )


def _switch_payload(row: sqlite3.Row) -> dict:
    return {
        "record_no": row["record_no"],
        "switched_at": row["switched_at"],
        "parameters": params_to_json(params_from_row(row)),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def dumps_key(parameters: dict[str, float]) -> str:
    """规范化参数的稳定字符串键，用于按参数聚合工况段数。"""
    return "|".join(str(parameters[key]) for key in PARAM_KEYS)


def _segment_op_payload(op) -> dict:
    return {
        "operation": op.kind,
        "started_at": to_iso(op.started_at),
        "ended_at": None if op.ended_at is None else to_iso(op.ended_at),
        "parameters": params_to_json(op.parameters),
        "old_started_at": (
            None if op.old_started_at is None else to_iso(op.old_started_at)
        ),
    }


def _result_payload(batch: sqlite3.Row, evaluation: dict, *, now: str) -> dict:
    return {
        "batch_no": batch["batch_no"],
        "eligible": evaluation["eligible"],
        "status": evaluation["status"],
        "reason_code": evaluation["reason_code"],
        "reason": REASONS[evaluation["reason_code"]],
        "sampling_start": batch["sampling_start"],
        "sampling_end": batch["sampling_end"],
        "window_start": evaluation["window_start"],
        "window_end": evaluation["window_end"],
        "stabilization_minutes": REQUIRED_SECONDS // 60,
        "plan_parameters": params_to_json(params_from_row(batch, prefix="plan_")),
        "first_problem_at": evaluation["first_problem_at"],
        "first_problem_kind": evaluation["first_problem_kind"],
        "first_mismatch_at": evaluation["first_mismatch_at"],
        "first_unknown_at": evaluation["first_unknown_at"],
        "first_problem_switch_record_no": evaluation["first_problem_switch_record_no"],
        "first_problem_switch_switched_at": evaluation[
            "first_problem_switch_switched_at"
        ],
        "segments": evaluation["segments"],
        "violations": evaluation["violations"],
        "evaluated_at": now,
    }


def _persist_evaluation(
    conn: sqlite3.Connection,
    batch: sqlite3.Row,
    switches: list[Switch],
    *,
    trigger: str,
    trigger_record_no: str | None,
    now: str,
) -> dict:
    evaluation = evaluate(
        switches,
        params_from_row(batch, prefix="plan_"),
        parse_utc(batch["sampling_start"]),
        parse_utc(batch["sampling_end"]),
    )
    payload = _result_payload(batch, evaluation, now=now)

    previous_row = repo.latest_revision(conn, batch["id"])
    previous_eligible = (
        bool(previous_row["eligible"]) if previous_row is not None else None
    )
    payload["transition"] = _transition(previous_eligible, payload["eligible"])
    payload["previous_eligible"] = previous_eligible

    sequence = repo.revision_sequence(conn, batch["id"]) + 1
    payload["revision"] = sequence
    payload["trigger"] = trigger
    payload["trigger_record_no"] = trigger_record_no

    repo.record_revision(
        conn,
        batch_id=batch["id"],
        sequence=sequence,
        trigger=trigger,
        trigger_record_no=trigger_record_no,
        previous_eligible=previous_eligible,
        result=payload,
        now=now,
    )
    repo.set_current_revision(conn, batch["id"], sequence, now)
    return payload


def _revision_summary(row: sqlite3.Row, result: dict) -> dict:
    return {
        "sequence": row["sequence"],
        "revision": row["sequence"],
        "trigger": row["trigger"],
        "trigger_record_no": row["trigger_record_no"],
        "status": row["status"],
        "eligible": bool(row["eligible"]),
        "reason_code": row["reason_code"],
        "first_problem_at": row["first_problem_at"],
        "first_problem_kind": row["first_problem_kind"],
        "first_mismatch_at": row["first_mismatch_at"],
        "first_unknown_at": row["first_unknown_at"],
        "previous_eligible": (
            None if row["previous_eligible"] is None else bool(row["previous_eligible"])
        ),
        "transition": result.get("transition"),
        "evaluated_at": row["created_at"],
    }


def _recheck_batches(
    conn: sqlite3.Connection,
    intervals: list[dict],
    *,
    trigger: str,
    trigger_record_no: str | None,
    now: str,
) -> list[dict]:
    if not intervals:
        return []
    switches = repo.load_switches(conn)
    rechecked: list[dict] = []
    for batch in repo.list_all_batches(conn):
        window_start, window_end = check_window(
            parse_utc(batch["sampling_start"]),
            parse_utc(batch["sampling_end"]),
        )
        if not any(
            window_intersects(window_start, window_end, interval)
            for interval in intervals
        ):
            continue
        result = _persist_evaluation(
            conn,
            batch,
            switches,
            trigger=trigger,
            trigger_record_no=trigger_record_no,
            now=now,
        )
        rechecked.append(
            {
                "batch_no": batch["batch_no"],
                "revision": result["revision"],
                "previous_eligible": result["previous_eligible"],
                "eligible": result["eligible"],
                "status": result["status"],
                "reason_code": result["reason_code"],
                "first_problem_at": result["first_problem_at"],
                "first_problem_kind": result["first_problem_kind"],
                "transition": result["transition"],
            }
        )
    return rechecked
