"""工况分组查询的 HTTP 端到端测试。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

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

GROUPS = "/analysis/condition-groups"


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "checks.db"))
    with TestClient(app) as test_client:
        yield test_client


def batch(client, batch_no, start, end, plan=None):
    return client.post(
        "/batches",
        json={
            "batch_no": batch_no,
            "plan_parameters": plan or PLAN,
            "sampling_start": start,
            "sampling_end": end,
        },
    )


def switch(client, record_no, at, params=None):
    return client.put(
        f"/switches/{record_no}",
        json={"switched_at": at, "parameters": params or PLAN},
    )


def query(client, start="2026-11-23T00:00:00Z", end="2026-11-24T00:00:00Z"):
    return client.get(GROUPS, params={"start": start, "end": end})


def test_empty_query(client):
    body = query(client).json()
    assert body["segments"] == []
    assert body["parameter_stats"] == []
    assert body["segment_count"] == 0
    assert body["eligible_batch_count"] == 0
    assert body["total_batch_count"] == 0


def test_multiple_batches_in_same_segment_count_as_one_repeat(client):
    """同一段连续工况里的多个合格箱样只算一次试验重复。"""
    switch(client, "SW-1", "2026-11-23T07:00:00Z")
    # 同参数连续上报：不产生新工况段
    switch(client, "SW-2", "2026-11-23T07:30:00Z")
    switch(client, "SW-3", "2026-11-23T08:00:00Z")
    batch(client, "LOT-A", "2026-11-23T09:00:00Z", "2026-11-23T09:30:00Z")
    batch(client, "LOT-B", "2026-11-23T10:00:00Z", "2026-11-23T10:30:00Z")
    batch(client, "LOT-C", "2026-11-23T11:00:00Z", "2026-11-23T11:20:00Z")

    body = query(client).json()
    assert body["segment_count"] == 1
    segment = body["segments"][0]
    assert segment["started_at"] == "2026-11-23T07:00:00Z"
    assert segment["ended_at"] is None  # 未结束的工况段
    assert segment["parameters"] == PLAN
    assert segment["batch_nos"] == ["LOT-A", "LOT-B", "LOT-C"]
    assert body["parameter_stats"] == [
        {"parameters": PLAN, "segment_count": 1, "batch_count": 3}
    ]
    assert body["eligible_batch_count"] == 3
    assert body["total_batch_count"] == 3


def test_half_open_range_filters_by_sampling_start(client):
    switch(client, "SW-1", "2026-11-23T06:00:00Z")
    batch(client, "LOT-AT-START", "2026-11-23T08:00:00Z", "2026-11-23T08:30:00Z")
    batch(client, "LOT-MIDDLE", "2026-11-23T09:00:00Z", "2026-11-23T09:30:00Z")
    batch(client, "LOT-AT-END", "2026-11-23T10:00:00Z", "2026-11-23T10:30:00Z")

    # 左闭右开：起点 08:00 含，终点 10:00 不含
    body = query(
        client,
        start="2026-11-23T08:00:00Z",
        end="2026-11-23T10:00:00Z",
    ).json()
    names = [name for segment in body["segments"] for name in segment["batch_nos"]]
    assert names == ["LOT-AT-START", "LOT-MIDDLE"]
    assert body["eligible_batch_count"] == 2
    assert body["total_batch_count"] == 2


def test_ineligible_batches_excluded_but_counted(client):
    """不合格批次不进分组，但 total_batch_count 仍计入。"""
    switch(client, "SW-1", "2026-11-23T07:00:00Z")
    # 合格批次
    batch(client, "LOT-OK", "2026-11-23T09:00:00Z", "2026-11-23T09:30:00Z")
    # 计划参数与设备参数不符 → 不合格
    batch(
        client,
        "LOT-BAD",
        "2026-11-23T10:00:00Z",
        "2026-11-23T10:30:00Z",
        plan=OTHER,
    )
    # 无切换覆盖的更早批次 → unknown，不合格
    batch(client, "LOT-UNKNOWN", "2026-11-23T05:00:00Z", "2026-11-23T05:30:00Z")

    body = query(client).json()
    all_names = [name for segment in body["segments"] for name in segment["batch_nos"]]
    assert all_names == ["LOT-OK"]
    assert body["eligible_batch_count"] == 1
    assert body["total_batch_count"] == 3


def test_split_then_merge_reflects_in_grouping(client):
    """补改日志拆组、再并组，分组查询必须反映最新已提交状态。"""
    # 初始：一整段 PLAN
    switch(client, "SW-1", "2026-11-23T07:00:00Z")
    batch(client, "LOT-A", "2026-11-23T08:30:00Z", "2026-11-23T09:00:00Z")
    batch(client, "LOT-B", "2026-11-23T10:30:00Z", "2026-11-23T11:00:00Z")

    body = query(client).json()
    assert body["segment_count"] == 1
    assert body["segments"][0]["batch_nos"] == ["LOT-A", "LOT-B"]

    # 拆组：09:00 切成 OTHER，10:00 切回 PLAN → PLAN 变成两段（两次重复）
    switch(client, "SW-2", "2026-11-23T09:00:00Z", OTHER)
    switch(client, "SW-3", "2026-11-23T10:00:00Z", PLAN)

    body = query(client).json()
    assert body["segment_count"] == 3
    by_start = {segment["started_at"]: segment for segment in body["segments"]}
    assert by_start["2026-11-23T07:00:00Z"]["ended_at"] == "2026-11-23T09:00:00Z"
    assert by_start["2026-11-23T07:00:00Z"]["batch_nos"] == ["LOT-A"]
    assert by_start["2026-11-23T09:00:00Z"]["parameters"] == OTHER
    assert by_start["2026-11-23T09:00:00Z"]["batch_nos"] == []
    assert by_start["2026-11-23T10:00:00Z"]["ended_at"] is None
    assert by_start["2026-11-23T10:00:00Z"]["batch_nos"] == ["LOT-B"]
    plan_stat = next(s for s in body["parameter_stats"] if s["parameters"] == PLAN)
    assert plan_stat["segment_count"] == 2  # 同参数两段 = 两次试验重复
    other_stat = next(s for s in body["parameter_stats"] if s["parameters"] == OTHER)
    assert other_stat["segment_count"] == 1

    # 并组：把中间的 OTHER 改回 PLAN → 恢复成一段
    response = switch(client, "SW-2", "2026-11-23T09:00:00Z", PLAN)
    assert response.status_code == 200
    body = query(client).json()
    assert body["segment_count"] == 1
    assert body["segments"][0]["started_at"] == "2026-11-23T07:00:00Z"
    assert body["segments"][0]["ended_at"] is None
    assert body["segments"][0]["batch_nos"] == ["LOT-A", "LOT-B"]
    assert body["parameter_stats"][0]["segment_count"] == 1


def test_correction_disjoint_from_all_windows_still_changes_grouping(client):
    """补改时段与所有样本检查窗口不相交，也须反映拆组/并组（段表独立维护）。"""
    switch(client, "SW-1", "2026-11-23T06:00:00Z")
    batch(client, "LOT-LATE", "2026-11-23T13:00:00Z", "2026-11-23T13:30:00Z")

    # 在凌晨插入一段 OTHER，再切回 PLAN：不与任何批次窗口相交，无批次重查
    switch(client, "SW-EARLY-BAD", "2026-11-23T01:00:00Z", OTHER)
    response = switch(client, "SW-EARLY-OK", "2026-11-23T02:00:00Z", PLAN)
    assert response.json()["rechecked_count"] == 0

    # 批次修订号不变；工况段已拆分（02:00 的 PLAN 与 06:00 的 PLAN 同参数
    # 相邻，按规则并为一段，因此共两段）。
    check = client.get("/batches/LOT-LATE/check").json()
    assert check["revision"] == 1
    body = query(client).json()
    assert body["segment_count"] == 2
    by_start = {segment["started_at"]: segment for segment in body["segments"]}
    assert by_start["2026-11-23T01:00:00Z"]["parameters"] == OTHER
    assert by_start["2026-11-23T01:00:00Z"]["batch_nos"] == []
    assert by_start["2026-11-23T02:00:00Z"]["parameters"] == PLAN
    assert by_start["2026-11-23T02:00:00Z"]["ended_at"] is None
    # LOT-LATE 落在合并后的 PLAN 段
    assert by_start["2026-11-23T02:00:00Z"]["batch_nos"] == ["LOT-LATE"]
    plan_stat = next(s for s in body["parameter_stats"] if s["parameters"] == PLAN)
    assert plan_stat["segment_count"] == 1

    # 再把 02:00 的切回改成 THIRD：与两侧参数都不同，真正拆成三段
    third = {"seal_temperature_c": 190.0, "seal_pressure_kpa": 310.0, "dwell_time_s": 2.0}
    switch(client, "SW-EARLY-OK", "2026-11-23T02:00:00Z", third)
    body = query(client).json()
    assert body["segment_count"] == 3
    stats = {tuple(sorted(s["parameters"].items())): s["segment_count"] for s in body["parameter_stats"]}
    assert len(stats) == 3


def test_failed_correction_keeps_original_grouping(client):
    """补改失败整体回滚：工况段沿用原分组。"""
    switch(client, "SW-1", "2026-11-23T07:00:00Z")
    switch(client, "SW-2", "2026-11-23T09:00:00Z", OTHER)
    batch(client, "LOT-A", "2026-11-23T08:00:00Z", "2026-11-23T08:30:00Z")

    before = query(client).json()
    # 撞切换时刻唯一约束 → 409，段表与结论都不动
    response = switch(client, "SW-3", "2026-11-23T09:00:00Z", PLAN)
    assert response.status_code == 409
    assert query(client).json() == before


def test_recheck_failure_rolls_back_segment_grouping(client, monkeypatch):
    """重查中途异常时，工况段维护与切换日志、核对结果一起回滚。"""
    from app import service as service_module
    from app import repository as repo

    switch(client, "SW-1", "2026-11-23T07:00:00Z")
    # 窗口 [09:55, 10:30) 与 09:00 起的变更时段相交，会触发重查
    batch(client, "LOT-A", "2026-11-23T10:00:00Z", "2026-11-23T10:30:00Z")

    database = client.app.state.database
    conn = database.connect()
    try:
        before = [
            (s.started_at.isoformat(), str(s.ended_at))
            for s in repo.load_segments(conn)
        ]
    finally:
        conn.close()

    original = service_module._persist_evaluation

    def boom(*args, **kwargs):
        raise RuntimeError("模拟重查失败")

    monkeypatch.setattr(service_module, "_persist_evaluation", boom)
    with pytest.raises(RuntimeError):
        switch(client, "SW-2", "2026-11-23T09:00:00Z", OTHER)
    monkeypatch.setattr(service_module, "_persist_evaluation", original)

    conn = database.connect()
    try:
        after = [
            (s.started_at.isoformat(), str(s.ended_at))
            for s in repo.load_segments(conn)
        ]
    finally:
        conn.close()
    assert after == before
    assert client.get("/switches").json()["total"] == 1
    assert client.get("/batches/LOT-A/check").json()["revision"] == 1


def test_segment_changes_reported_in_upsert_response(client):
    """补改响应中给出工况段的 insert/update/delete 明细。"""
    switch(client, "SW-1", "2026-11-23T07:00:00Z")
    response = switch(client, "SW-2", "2026-11-23T09:00:00Z", OTHER).json()
    changes = response["segment_changes"]
    kinds = {item["operation"] for item in changes}
    assert "update" in kinds  # 原开放段终点改为 09:00
    assert "insert" in kinds  # 新的 OTHER 开放段
    # 再并组：出现 delete
    response = switch(client, "SW-2", "2026-11-23T09:00:00Z", PLAN).json()
    assert any(item["operation"] == "delete" for item in response["segment_changes"])


def test_small_range_uses_indexes_not_full_scans(client, monkeypatch):
    """小范围查询只走索引：EXPLAIN QUERY PLAN 不得出现全表扫描。"""
    switch(client, "SW-1", "2026-11-23T06:00:00Z")
    for index in range(20):
        batch(
            client,
            f"LOT-{index:02d}",
            f"2026-11-23T{8 + index // 4:02d}:{index % 4 * 15:02d}:00Z",
            f"2026-11-23T{8 + index // 4:02d}:{(index % 4) * 15 + 10:02d}:00Z",
        )

    database = client.app.state.database
    conn = database.connect()
    try:
        batch_plan = conn.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT b.id FROM batches AS b
              JOIN revisions AS r ON r.batch_id = b.id AND r.sequence = b.current_revision
             WHERE b.sampling_start >= ? AND b.sampling_start < ? AND r.eligible = 1
            """,
            ("2026-11-23T09:00:00Z", "2026-11-23T10:00:00Z"),
        ).fetchall()
        segment_plan = conn.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT * FROM condition_segments
             WHERE started_at <= ? ORDER BY started_at DESC LIMIT 1
            """,
            ("2026-11-23T09:30:00Z",),
        ).fetchall()
    finally:
        conn.close()

    batch_text = " ".join(row[3] for row in batch_plan)
    segment_text = " ".join(row[3] for row in segment_plan)
    assert "SCAN" not in batch_text.upper(), batch_text
    assert "SCAN" not in segment_text.upper(), segment_text
    assert "INDEX" in batch_text.upper()
    assert "INDEX" in segment_text.upper()


def test_backfill_for_legacy_database(tmp_path):
    """旧 SQLite 卷（无工况段表数据）启动时自动回填。"""
    from app.db import Database
    from app import repository as repo
    from app.timeutil import parse_utc

    db_path = tmp_path / "legacy.db"
    database = Database(db_path)
    database.initialize()
    # 直接写切换记录，模拟旧卷里已有日志但没有工况段
    conn = database.connect()
    try:
        repo.insert_switch(
            conn,
            record_no="SW-1",
            switched_at=parse_utc("2026-11-23T07:00:00Z"),
            params=PLAN,
            now="2026-11-23T07:00:00Z",
        )
        repo.insert_switch(
            conn,
            record_no="SW-2",
            switched_at=parse_utc("2026-11-23T09:00:00Z"),
            params=OTHER,
            now="2026-11-23T09:00:00Z",
        )
    finally:
        conn.close()

    app = create_app(str(db_path))
    with TestClient(app) as c:
        body = query(c).json()
        # 没有合格批次，但回填出的两段工况仍应返回（空段）
        assert body["eligible_batch_count"] == 0
        assert body["segment_count"] == 2
        assert all(segment["batch_nos"] == [] for segment in body["segments"])
        # 段表已回填两段
        conn = database.connect()
        try:
            segments = repo.load_segments(conn)
        finally:
            conn.close()
        assert len(segments) == 2
        assert segments[0].parameters == PLAN
        assert segments[1].parameters == OTHER


def test_query_validation_errors(client):
    assert client.get(GROUPS).status_code == 422  # 缺参数
    assert client.get(
        GROUPS,
        params={"start": "2026-11-23T08:00:00", "end": "2026-11-23T16:00:00Z"},
    ).status_code == 422  # naive
    assert client.get(
        GROUPS,
        params={"start": "2026-11-23T16:00:00Z", "end": "2026-11-23T08:00:00Z"},
    ).status_code == 422  # 倒置


def test_segment_overlap_respects_half_open_boundaries(client):
    """工况段与查询区间按半开语义相交：边界点不相交。"""
    switch(client, "SW-1", "2026-11-23T07:00:00Z", PLAN)
    switch(client, "SW-2", "2026-11-23T09:00:00Z", OTHER)

    # 段 A [07:00,09:00)、段 B [09:00,null)
    body = query(
        client,
        start="2026-11-23T09:00:00Z",
        end="2026-11-23T12:00:00Z",
    ).json()
    assert [s["started_at"] for s in body["segments"]] == ["2026-11-23T09:00:00Z"]

    body = query(
        client,
        start="2026-11-23T06:00:00Z",
        end="2026-11-23T09:00:00Z",
    ).json()
    assert [s["started_at"] for s in body["segments"]] == ["2026-11-23T07:00:00Z"]

    # 段 A 与 [08:00,09:00) 有正长度交集
    body = query(
        client,
        start="2026-11-23T08:00:00Z",
        end="2026-11-23T09:00:00Z",
    ).json()
    assert [s["started_at"] for s in body["segments"]] == ["2026-11-23T07:00:00Z"]
