"""SQL 访问与批次结果快照读写。"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from .db import dumps
from .evaluation import Switch
from .params import params_from_row
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
