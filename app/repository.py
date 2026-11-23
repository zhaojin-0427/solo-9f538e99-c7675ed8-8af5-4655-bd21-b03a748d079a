"""SQL 访问与批次结果快照读写。"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from .db import dumps
from .evaluation import Switch
from .params import params_from_row
from .segments import ConditionSegment, SegmentOp
from .timeutil import parse_utc, to_iso

BATCH_SELECT = """
SELECT id, batch_no, plan_temperature_c, plan_pressure_kpa, plan_dwell_time_s,
       sampling_start, sampling_end, created_at, updated_at, current_revision
FROM batches
"""

SWITCH_SELECT = """
SELECT record_no, switched_at, temperature_c, pressure_kpa, dwell_time_s,
       created_at, updated_at
FROM switches
"""

SEGMENT_SELECT = """
SELECT id, started_at, ended_at, temperature_c, pressure_kpa, dwell_time_s,
       created_at, updated_at
FROM condition_segments
"""


def switches_from_rows(rows: list[sqlite3.Row]) -> list[Switch]:
    return [
        Switch(
            record_no=row["record_no"],
            at=parse_utc(row["switched_at"]),
            params=params_from_row(row),
        )
        for row in rows
    ]


def load_switches(conn: sqlite3.Connection) -> list[Switch]:
    rows = conn.execute(f"{SWITCH_SELECT} ORDER BY switched_at, record_no").fetchall()
    return switches_from_rows(rows)


def fetch_switch(conn: sqlite3.Connection, record_no: str) -> sqlite3.Row | None:
    return conn.execute(
        f"{SWITCH_SELECT} WHERE record_no = ?", (record_no,)
    ).fetchone()


def switch_by_time(conn: sqlite3.Connection, switched_at: str) -> sqlite3.Row | None:
    return conn.execute(
        f"{SWITCH_SELECT} WHERE switched_at = ?", (switched_at,)
    ).fetchone()


def fetch_batch(conn: sqlite3.Connection, batch_no: str) -> sqlite3.Row | None:
    return conn.execute(f"{BATCH_SELECT} WHERE batch_no = ?", (batch_no,)).fetchone()


def list_batches(
    conn: sqlite3.Connection, limit: int, offset: int
) -> list[sqlite3.Row]:
    return conn.execute(
        f"{BATCH_SELECT} ORDER BY id LIMIT ? OFFSET ?", (limit, offset)
    ).fetchall()


def count_batches(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0])


def list_all_batches(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(f"{BATCH_SELECT} ORDER BY id").fetchall()


def insert_batch(
    conn: sqlite3.Connection,
    *,
    batch_no: str,
    plan: dict[str, float],
    sampling_start: datetime,
    sampling_end: datetime,
    now: str,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO batches (
            batch_no, plan_temperature_c, plan_pressure_kpa, plan_dwell_time_s,
            sampling_start, sampling_end, created_at, updated_at, current_revision
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
        """,
        (
            batch_no,
            plan["seal_temperature_c"],
            plan["seal_pressure_kpa"],
            plan["dwell_time_s"],
            to_iso(sampling_start),
            to_iso(sampling_end),
            now,
            now,
        ),
    )
    return int(cursor.lastrowid)


def insert_switch(
    conn: sqlite3.Connection,
    *,
    record_no: str,
    switched_at: datetime,
    params: dict[str, float],
    now: str,
) -> None:
    conn.execute(
        """
        INSERT INTO switches (
            record_no, switched_at, temperature_c, pressure_kpa, dwell_time_s,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record_no,
            to_iso(switched_at),
            params["seal_temperature_c"],
            params["seal_pressure_kpa"],
            params["dwell_time_s"],
            now,
            now,
        ),
    )


def update_switch(
    conn: sqlite3.Connection,
    *,
    record_no: str,
    switched_at: datetime,
    params: dict[str, float],
    now: str,
) -> None:
    conn.execute(
        """
        UPDATE switches
           SET switched_at = ?, temperature_c = ?, pressure_kpa = ?,
               dwell_time_s = ?, updated_at = ?
         WHERE record_no = ?
        """,
        (
            to_iso(switched_at),
            params["seal_temperature_c"],
            params["seal_pressure_kpa"],
            params["dwell_time_s"],
            now,
            record_no,
        ),
    )


def latest_revision(conn: sqlite3.Connection, batch_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM revisions
         WHERE batch_id = ?
         ORDER BY sequence DESC
         LIMIT 1
        """,
        (batch_id,),
    ).fetchone()


def revision_sequence(conn: sqlite3.Connection, batch_id: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(sequence), 0) FROM revisions WHERE batch_id = ?",
        (batch_id,),
    ).fetchone()
    return int(row[0])


def list_revisions(
    conn: sqlite3.Connection, batch_id: int
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM revisions WHERE batch_id = ? ORDER BY sequence",
        (batch_id,),
    ).fetchall()


def record_revision(
    conn: sqlite3.Connection,
    *,
    batch_id: int,
    sequence: int,
    trigger: str,
    trigger_record_no: str | None,
    previous_eligible: bool | None,
    result: dict,
    now: str,
) -> None:
    conn.execute(
        """
        INSERT INTO revisions (
            batch_id, sequence, trigger, trigger_record_no, status, eligible,
            reason_code, previous_eligible, first_problem_at, first_problem_kind,
            first_mismatch_at, first_unknown_at, result_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            batch_id,
            sequence,
            trigger,
            trigger_record_no,
            result["status"],
            1 if result["eligible"] else 0,
            result["reason_code"],
            None if previous_eligible is None else (1 if previous_eligible else 0),
            result["first_problem_at"],
            result["first_problem_kind"],
            result["first_mismatch_at"],
            result["first_unknown_at"],
            dumps(result),
            now,
        ),
    )


def set_current_revision(
    conn: sqlite3.Connection, batch_id: int, sequence: int, now: str
) -> None:
    conn.execute(
        "UPDATE batches SET current_revision = ?, updated_at = ? WHERE id = ?",
        (sequence, now, batch_id),
    )


# ------------------------------------------------------------ 工况段


def segment_from_row(row: sqlite3.Row) -> ConditionSegment:
    return ConditionSegment(
        started_at=parse_utc(row["started_at"]),
        ended_at=None if row["ended_at"] is None else parse_utc(row["ended_at"]),
        parameters=params_from_row(row),
    )


def load_segments(conn: sqlite3.Connection) -> list[ConditionSegment]:
    rows = conn.execute(f"{SEGMENT_SELECT} ORDER BY started_at").fetchall()
    return [segment_from_row(row) for row in rows]


def count_segments(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM condition_segments").fetchone()[0])


def insert_segment(
    conn: sqlite3.Connection, segment: ConditionSegment, *, now: str
) -> None:
    conn.execute(
        """
        INSERT INTO condition_segments (
            started_at, ended_at, temperature_c, pressure_kpa, dwell_time_s,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            to_iso(segment.started_at),
            None if segment.ended_at is None else to_iso(segment.ended_at),
            segment.parameters["seal_temperature_c"],
            segment.parameters["seal_pressure_kpa"],
            segment.parameters["dwell_time_s"],
            now,
            now,
        ),
    )


def update_segment(
    conn: sqlite3.Connection, op: SegmentOp, *, now: str
) -> None:
    conn.execute(
        """
        UPDATE condition_segments
           SET started_at = ?, ended_at = ?, temperature_c = ?, pressure_kpa = ?,
               dwell_time_s = ?, updated_at = ?
         WHERE started_at = ?
        """,
        (
            to_iso(op.started_at),
            None if op.ended_at is None else to_iso(op.ended_at),
            op.parameters["seal_temperature_c"],
            op.parameters["seal_pressure_kpa"],
            op.parameters["dwell_time_s"],
            now,
            to_iso(op.old_started_at),
        ),
    )


def delete_segment(conn: sqlite3.Connection, old_started_at: datetime) -> None:
    conn.execute(
        "DELETE FROM condition_segments WHERE started_at = ?",
        (to_iso(old_started_at),),
    )


def apply_segment_ops(
    conn: sqlite3.Connection, ops: list[SegmentOp], *, now: str
) -> None:
    """按 delete → update → insert 顺序应用，避开 started_at 唯一约束。"""
    for op in ops:
        if op.kind == "delete":
            assert op.old_started_at is not None
            delete_segment(conn, op.old_started_at)
    for op in ops:
        if op.kind == "update":
            update_segment(conn, op, now=now)
    for op in ops:
        if op.kind == "insert":
            insert_segment(
                conn,
                ConditionSegment(
                    started_at=op.started_at,
                    ended_at=op.ended_at,
                    parameters=op.parameters,
                ),
                now=now,
            )


def find_eligible_batches_in_range(
    conn: sqlite3.Connection, start: datetime, end: datetime
) -> list[sqlite3.Row]:
    """按采样开始时刻在半开区间 [start, end) 内定位**当前合格**的批次。

    走 ``idx_batches_sampling_start`` 索引做范围扫描，不读取区间外批次；
    合格判定取每批最新修订（revisions.eligible），在 SQL 内完成关联。
    """
    return conn.execute(
        """
        SELECT b.id, b.batch_no, b.plan_temperature_c, b.plan_pressure_kpa,
               b.plan_dwell_time_s, b.sampling_start, b.sampling_end,
               b.created_at, b.updated_at, b.current_revision,
               r.eligible AS eligible_flag
          FROM batches AS b
          JOIN revisions AS r
            ON r.batch_id = b.id
           AND r.sequence = b.current_revision
         WHERE b.sampling_start >= ?
           AND b.sampling_start < ?
           AND r.eligible = 1
         ORDER BY b.sampling_start, b.id
        """,
        (to_iso(start), to_iso(end)),
    ).fetchall()


def find_segments_overlapping_range(
    conn: sqlite3.Connection, start: datetime, end: datetime
) -> list[sqlite3.Row]:
    """返回与半开区间 ``[start, end)`` 相交的全部工况段（含无批次的空段）。

    半开相交：``started_at < end AND (ended_at IS NULL OR ended_at > start)``，
    走 ``started_at`` 索引做范围定位，不扫描全线历史。
    """
    return conn.execute(
        f"""
        {SEGMENT_SELECT}
         WHERE started_at < ?
           AND (ended_at IS NULL OR ended_at > ?)
         ORDER BY started_at
        """,
        (to_iso(end), to_iso(start)),
    ).fetchall()


def find_segment_for_moment(
    conn: sqlite3.Connection, moment: datetime
) -> sqlite3.Row | None:
    """定位采样时刻所属工况段：``started_at <= moment`` 的最近一段。

    走 ``condition_segments.started_at`` 唯一索引，单条索引定位；
    不逐条读取段内的同值切换记录。
    """
    return conn.execute(
        f"""
        {SEGMENT_SELECT}
         WHERE started_at <= ?
         ORDER BY started_at DESC
         LIMIT 1
        """,
        (to_iso(moment),),
    ).fetchone()


def count_batches_in_range(
    conn: sqlite3.Connection, start: datetime, end: datetime
) -> int:
    """区间内全部批次数（含不合格），用于响应里的对照计数。"""
    return int(
        conn.execute(
            """
            SELECT COUNT(*) FROM batches
             WHERE sampling_start >= ? AND sampling_start < ?
            """,
            (to_iso(start), to_iso(end)),
        ).fetchone()[0]
    )


def count_eligible_batches_in_range(
    conn: sqlite3.Connection, start: datetime, end: datetime
) -> int:
    return int(
        conn.execute(
            """
            SELECT COUNT(*)
              FROM batches AS b
              JOIN revisions AS r
                ON r.batch_id = b.id AND r.sequence = b.current_revision
             WHERE b.sampling_start >= ?
               AND b.sampling_start < ?
               AND r.eligible = 1
            """,
            (to_iso(start), to_iso(end)),
        ).fetchone()[0]
    )

