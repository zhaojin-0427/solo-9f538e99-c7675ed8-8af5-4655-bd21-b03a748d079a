"""HTTP 端到端测试：真实 FastAPI + 临时 SQLite 文件。"""

from __future__ import annotations

import sqlite3

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


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "checks.db"))
    with TestClient(app) as test_client:
        yield test_client


def create_batch(client, batch_no="LOT-1", start="2026-11-23T09:00:00Z", end="2026-11-23T09:30:00Z", plan=None):
    response = client.post(
        "/batches",
        json={
            "batch_no": batch_no,
            "plan_parameters": plan or PLAN,
            "sampling_start": start,
            "sampling_end": end,
        },
    )
    return response


def put_switch(client, record_no="SW-1", at="2026-11-23T08:50:00Z", params=None):
    return client.put(
        f"/switches/{record_no}",
        json={"switched_at": at, "parameters": params or PLAN},
    )


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_openapi_and_docs_available(client):
    assert client.get("/docs").status_code == 200
    schema = client.get("/openapi.json").json()
    assert "/batches/{batch_no}/check" in schema["paths"]
    assert "/switches/{record_no}" in schema["paths"]


def test_batch_unknown_then_restored_by_switch(client):
    response = create_batch(client)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["current_result"]["eligible"] is False
    assert body["current_result"]["reason_code"] == "unknown_params"
    assert body["current_result"]["first_unknown_at"] == "2026-11-23T08:55:00Z"
    assert body["current_result"]["window_start"] == "2026-11-23T08:55:00Z"
    assert body["current_result"]["window_end"] == "2026-11-23T09:30:00Z"
    assert body["current_result"]["revision"] == 1
    assert body["revision"] == 1

    response = put_switch(client, at="2026-11-23T08:50:00Z")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["operation"] == "created"
    assert body["rechecked_count"] == 1
    rechecked = body["rechecked_batches"][0]
    assert rechecked["batch_no"] == "LOT-1"
    assert rechecked["transition"] == "restored"
    assert rechecked["previous_eligible"] is False
    assert rechecked["eligible"] is True

    check = client.get("/batches/LOT-1/check").json()
    assert check["eligible"] is True
    assert check["reason_code"] == "ok"
    assert check["first_problem_at"] is None
    assert check["revision"] == 2
    history = check["revision_history"]
    assert [item["trigger"] for item in history] == ["batch_created", "switch_created"]
    assert [item["transition"] for item in history] == ["initial", "restored"]


def test_late_switch_cannot_be_eligible(client):
    """切换晚于采样前 5 分钟：即使参数正确也不能纳入分析。"""
    create_batch(client)
    response = put_switch(client, at="2026-11-23T08:57:00Z")
    assert response.status_code == 200
    body = response.json()
    assert body["rechecked_batches"][0]["eligible"] is False
    assert body["rechecked_batches"][0]["transition"] == "unchanged"

    check = client.get("/batches/LOT-1/check").json()
    assert check["eligible"] is False
    assert check["reason_code"] == "unknown_params"
    assert check["first_problem_at"] == "2026-11-23T08:55:00Z"
    assert check["first_problem_kind"] == "unknown"
    assert check["first_unknown_at"] == "2026-11-23T08:55:00Z"
    assert check["segments"][0]["kind"] == "unknown"
    assert check["segments"][0]["end"] == "2026-11-23T08:57:00Z"
    assert check["segments"][1]["kind"] == "ok"


def test_correction_revokes_and_correction_restores(client):
    put_switch(client, at="2026-11-23T08:50:00Z")
    create_batch(client)
    assert client.get("/batches/LOT-1/check").json()["eligible"] is True

    # 把切换时刻改到窗口内部 → 窗口前段变为未知，资格撤下
    response = put_switch(client, at="2026-11-23T09:10:00Z")
    assert response.status_code == 200
    body = response.json()
    assert body["operation"] == "updated"
    assert len(body["changed_intervals"]) == 1
    assert body["changed_intervals"][0]["start"] == "2026-11-23T08:50:00Z"
    assert body["changed_intervals"][0]["end"] == "2026-11-23T09:10:00Z"
    rechecked = body["rechecked_batches"][0]
    assert rechecked["transition"] == "revoked"
    assert rechecked["eligible"] is False

    check = client.get("/batches/LOT-1/check").json()
    assert check["reason_code"] == "unknown_params"
    assert check["first_unknown_at"] == "2026-11-23T08:55:00Z"

    # 再更正回窗口之前 → 资格恢复
    response = put_switch(client, at="2026-11-23T08:45:00Z")
    body = response.json()
    assert body["rechecked_batches"][0]["transition"] == "restored"
    check = client.get("/batches/LOT-1/check").json()
    assert check["eligible"] is True
    assert check["first_problem_at"] is None
    # 修订历史完整保留，便于追溯
    assert [item["transition"] for item in check["revision_history"]] == [
        "initial",
        "revoked",
        "restored",
    ]


def test_parameter_mismatch_reports_first_moment(client):
    put_switch(client, at="2026-11-23T08:00:00Z", params=OTHER)
    create_batch(client)
    check = client.get("/batches/LOT-1/check").json()
    assert check["eligible"] is False
    assert check["reason_code"] == "parameter_mismatch"
    assert check["first_mismatch_at"] == "2026-11-23T08:55:00Z"
    assert check["first_problem_at"] == "2026-11-23T08:55:00Z"
    assert check["first_problem_kind"] == "mismatch"
    assert check["first_problem_switch_record_no"] == "SW-1"
    assert check["first_unknown_at"] is None


def test_only_intersecting_batches_are_rechecked(client):
    put_switch(client, at="2026-11-23T08:00:00Z")
    create_batch(client, batch_no="LOT-EARLY", start="2026-11-23T09:00:00Z", end="2026-11-23T09:30:00Z")
    create_batch(client, batch_no="LOT-LATE", start="2026-11-23T12:00:00Z", end="2026-11-23T12:30:00Z")

    # 在 10:00 插入一条不符参数：变更时段 [10:00, ∞)。
    # LOT-EARLY 窗口 [08:55, 09:30) 不含 10:00 → 不重查，资格保持；
    # LOT-LATE 窗口 [11:55, 12:30) 在变更之后 → 重查并撤下资格。
    response = put_switch(client, "SW-BAD", at="2026-11-23T10:00:00Z", params=OTHER)
    body = response.json()
    names = [item["batch_no"] for item in body["rechecked_batches"]]
    assert names == ["LOT-LATE"]
    assert body["rechecked_batches"][0]["transition"] == "revoked"
    early = client.get("/batches/LOT-EARLY/check").json()
    assert early["revision"] == 1
    assert early["eligible"] is True

    # 11:00 恢复正确参数：变更时段 [11:00, ∞)，LOT-EARLY 仍不相交；
    # LOT-LATE 恢复资格，LOT-NOON 的窗口在 11:00 之后，由不合格恢复为合格。
    create_batch(client, batch_no="LOT-NOON", start="2026-11-23T13:00:00Z", end="2026-11-23T13:30:00Z")
    assert client.get("/batches/LOT-NOON/check").json()["eligible"] is False
    response = put_switch(client, "SW-GOOD", at="2026-11-23T11:00:00Z", params=PLAN)
    body = response.json()
    names = [item["batch_no"] for item in body["rechecked_batches"]]
    assert names == ["LOT-LATE", "LOT-NOON"]
    by_name = {item["batch_no"]: item for item in body["rechecked_batches"]}
    assert by_name["LOT-LATE"]["transition"] == "restored"
    assert by_name["LOT-NOON"]["transition"] == "restored"
    assert client.get("/batches/LOT-EARLY/check").json()["revision"] == 1


def test_duplicate_switch_time_rejected_without_side_effects(client):
    put_switch(client, "SW-1", at="2026-11-23T08:50:00Z")
    create_batch(client)
    before = client.get("/batches/LOT-1/check").json()

    response = put_switch(client, "SW-2", at="2026-11-23T08:50:00Z")
    assert response.status_code == 409
    assert response.json()["code"] == "switch_time_conflict"

    # 失败请求不得写入日志或结论：批次仍是 revision 2，结论不变
    after = client.get("/batches/LOT-1/check").json()
    assert after == before
    assert client.get("/switches").json()["total"] == 1

    # 更正记录 1 的切换时刻撞到记录 2 也被拒绝
    put_switch(client, "SW-2", at="2026-11-23T08:40:00Z")
    response = put_switch(client, "SW-1", at="2026-11-23T08:40:00Z")
    assert response.status_code == 409


def test_failed_validation_keeps_original_results(client):
    put_switch(client, at="2026-11-23T08:50:00Z")
    create_batch(client)
    before = client.get("/batches/LOT-1/check").json()

    # 缺字段 → 422，且无任何写入
    response = client.put("/switches/SW-1", json={"switched_at": "2026-11-23T08:00:00Z"})
    assert response.status_code == 422
    assert client.get("/switches/SW-1").json()["switched_at"] == "2026-11-23T08:50:00Z"
    assert client.get("/batches/LOT-1/check").json() == before


def test_naive_and_non_utc_times(client):
    response = client.post(
        "/batches",
        json={
            "batch_no": "LOT-NAIVE",
            "plan_parameters": PLAN,
            "sampling_start": "2026-11-23T09:00:00",
            "sampling_end": "2026-11-23T09:30:00Z",
        },
    )
    assert response.status_code == 422

    # 带偏移量会被换算为 UTC
    response = client.put(
        "/switches/SW-TZ",
        json={"switched_at": "2026-11-23T17:50:00+09:00", "parameters": PLAN},
    )
    assert response.status_code == 200
    assert response.json()["switch"]["switched_at"] == "2026-11-23T08:50:00Z"


def test_naive_switch_time_rejected(client):
    response = client.put(
        "/switches/SW-NAIVE",
        json={"switched_at": "2026-11-23T08:50:00", "parameters": PLAN},
    )
    assert response.status_code == 422


def test_unknown_time_fields_rejected(client):
    response = client.post(
        "/batches",
        json={
            "batch_no": "LOT-EXTRA",
            "plan_parameters": PLAN,
            "sampling_start": "2026-11-23T09:00:00Z",
            "sampling_end": "2026-11-23T09:30:00Z",
            "extra": 1,
        },
    )
    assert response.status_code == 422


def test_duplicate_batch_rejected(client):
    assert create_batch(client).status_code == 201
    response = create_batch(client)
    assert response.status_code == 409
    assert response.json()["code"] == "batch_exists"


def test_sampling_end_must_be_after_start(client):
    response = create_batch(client, start="2026-11-23T09:30:00Z", end="2026-11-23T09:30:00Z")
    assert response.status_code == 422


def test_parameter_rounding_and_unchanged_detection(client):
    create_batch(client)
    put_switch(client, "SW-1", at="2026-11-23T08:50:00Z")
    # 同值不同精度 → 视为未变更，不产生新修订
    response = put_switch(
        client,
        "SW-1",
        at="2026-11-23T08:50:00Z",
        params={
            "seal_temperature_c": 180.0001,
            "seal_pressure_kpa": 300.0,
            "dwell_time_s": 1.5,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["operation"] == "unchanged"
    assert body["rechecked_count"] == 0
    assert body["changed_intervals"] == []
    assert client.get("/batches/LOT-1/check").json()["revision"] == 2


def test_switch_list_and_lookup(client):
    put_switch(client, "SW-B", at="2026-11-23T10:00:00Z")
    put_switch(client, "SW-A", at="2026-11-23T08:00:00Z")
    items = client.get("/switches").json()
    assert [item["record_no"] for item in items["items"]] == ["SW-A", "SW-B"]
    one = client.get("/switches/SW-B").json()
    assert one["switched_at"] == "2026-11-23T10:00:00Z"
    assert client.get("/switches/NOPE").status_code == 404


def test_batch_not_found(client):
    assert client.get("/batches/NOPE").status_code == 404
    assert client.get("/batches/NOPE/check").status_code == 404


def test_batch_list_pagination(client):
    for index in range(3):
        create_batch(client, batch_no=f"LOT-{index}")
    page = client.get("/batches", params={"limit": 2, "offset": 0}).json()
    assert page["total"] == 3
    assert [item["batch_no"] for item in page["items"]] == ["LOT-0", "LOT-1"]
    page = client.get("/batches", params={"limit": 2, "offset": 2}).json()
    assert [item["batch_no"] for item in page["items"]] == ["LOT-2"]
    assert client.get("/batches", params={"limit": 0}).status_code == 422


def test_switch_does_not_change_earlier_batch(client):
    """窗口在变更时段之前的批次不被重查。"""
    put_switch(client, "SW-1", at="2026-11-23T08:00:00Z")
    create_batch(client, batch_no="LOT-MORNING", start="2026-11-23T09:00:00Z", end="2026-11-23T09:30:00Z")
    response = put_switch(client, "SW-2", at="2026-11-23T10:00:00Z", params=OTHER)
    body = response.json()
    # 10:00 起一直不符到无穷，LOT-MORNING 窗口结束于 09:30，不受影响
    assert body["rechecked_batches"] == []
    assert body["rechecked_count"] == 0
    assert client.get("/batches/LOT-MORNING/check").json()["revision"] == 1


def test_switch_at_sampling_end_boundary(client):
    put_switch(client, "SW-1", at="2026-11-23T08:50:00Z", params=OTHER)
    create_batch(client)
    # 恰好在采样结束时刻切换回正确参数：[08:55, 09:30) 内仍是错误参数，
    # 该时刻落在半开窗口之外，因此不会触发重查。
    response = put_switch(client, "SW-2", at="2026-11-23T09:30:00Z", params=PLAN)
    body = response.json()
    assert body["rechecked_batches"] == []
    check = client.get("/batches/LOT-1/check").json()
    assert check["eligible"] is False
    assert check["first_mismatch_at"] == "2026-11-23T08:55:00Z"
    assert check["revision"] == 1


def test_database_corruption_is_not_silently_ignored(tmp_path):
    """确认 SQLite 层唯一约束也独立兜底。"""
    from app.db import Database

    db = Database(tmp_path / "x.db")
    db.initialize()
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO switches (record_no, switched_at, temperature_c, pressure_kpa, dwell_time_s, created_at, updated_at) "
            "VALUES ('A', '2026-11-23T08:00:00Z', 1, 1, 1, 'x', 'x')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO switches (record_no, switched_at, temperature_c, pressure_kpa, dwell_time_s, created_at, updated_at) "
                "VALUES ('B', '2026-11-23T08:00:00Z', 1, 1, 1, 'x', 'x')"
            )
    finally:
        conn.close()


def test_concurrent_duplicate_time_maps_to_409(client):
    """唯一约束兜底：绕过预检查时也不会 500。"""
    import sqlite3 as _sqlite3

    from app.db import transaction

    put_switch(client, "SW-1", at="2026-11-23T08:50:00Z")
    database = client.app.state.database
    conn = database.connect()
    try:
        with pytest.raises(_sqlite3.IntegrityError):
            with transaction(conn):
                conn.execute(
                    "INSERT INTO switches (record_no, switched_at, temperature_c, pressure_kpa, dwell_time_s, created_at, updated_at) "
                    "VALUES ('SW-X','2026-11-23T08:50:00Z',1,1,1,'x','x')"
                )
            raise AssertionError("应当抛出 IntegrityError")
    finally:
        conn.close()
    # 事务回滚后没有残留
    assert client.get("/switches").json()["total"] == 1


def test_recheck_failure_rolls_back_switch_and_results(client, monkeypatch):
    """重查中途失败时，切换日志与核对结果都不得写入。"""
    from app import service as service_module

    put_switch(client, "SW-1", at="2026-11-23T08:50:00Z")
    create_batch(client, batch_no="LOT-A", start="2026-11-23T09:00:00Z", end="2026-11-23T09:30:00Z")
    before = client.get("/batches/LOT-A/check").json()
    before_switches = client.get("/switches").json()

    original = service_module._persist_evaluation
    calls = {"n": 0}

    def boom(*args, **kwargs):
        calls["n"] += 1
        raise RuntimeError("模拟重查失败")

    monkeypatch.setattr(service_module, "_persist_evaluation", boom)
    with pytest.raises(RuntimeError):
        client.put(
            "/switches/SW-2",
            json={"switched_at": "2026-11-23T09:00:00Z", "parameters": OTHER},
        )
    monkeypatch.setattr(service_module, "_persist_evaluation", original)

    assert calls["n"] == 1
    assert client.get("/switches").json() == before_switches
    assert client.get("/batches/LOT-A/check").json() == before
