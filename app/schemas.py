"""请求/响应模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .params import canonicalize
from .timeutil import to_utc

BatchNo = Annotated[
    str,
    Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._\-]*$",
        description="批号，字母/数字开头，可含 . _ -",
        examples=["LOT-2026-001"],
    ),
]

RecordNo = Annotated[
    str,
    Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._\-]*$",
        description="切换记录号，唯一",
        examples=["SW-0001"],
    ),
]

Temperature = Annotated[float, Field(ge=-50, le=400, description="封口温度 °C")]
Pressure = Annotated[float, Field(ge=0, le=5000, description="封口压力 kPa")]
Dwell = Annotated[float, Field(ge=0, le=3600, description="停留时间 s")]


def _require_utc(value: datetime) -> datetime:
    try:
        return to_utc(value)
    except ValueError as exc:  # naive datetime
        raise ValueError(str(exc)) from exc


class PlanParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seal_temperature_c: Temperature
    seal_pressure_kpa: Pressure
    dwell_time_s: Dwell

    def normalized(self) -> dict[str, float]:
        return canonicalize(self.model_dump())


class BatchCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={
        "example": {
            "batch_no": "LOT-2026-001",
            "plan_parameters": {
                "seal_temperature_c": 180.0,
                "seal_pressure_kpa": 300.0,
                "dwell_time_s": 1.5,
            },
            "sampling_start": "2026-11-23T09:00:00Z",
            "sampling_end": "2026-11-23T09:30:00Z",
        }
    })

    batch_no: BatchNo
    plan_parameters: PlanParameters
    sampling_start: datetime = Field(description="UTC 采样开始时刻")
    sampling_end: datetime = Field(description="UTC 采样结束时刻")

    @field_validator("batch_no")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()

    @field_validator("sampling_start", "sampling_end")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @model_validator(mode="after")
    def _order(self) -> BatchCreate:
        if self.sampling_end <= self.sampling_start:
            raise ValueError("采样结束时刻必须晚于开始时刻")
        return self


class SwitchUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={
        "example": {
            "switched_at": "2026-11-23T08:50:00Z",
            "parameters": {
                "seal_temperature_c": 180.0,
                "seal_pressure_kpa": 300.0,
                "dwell_time_s": 1.5,
            },
        }
    })

    switched_at: datetime = Field(description="设备完成切换并稳定的 UTC 时刻")
    parameters: PlanParameters

    @field_validator("switched_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return _require_utc(value)


class SwitchUpsertResult(BaseModel):
    operation: Literal["created", "updated", "unchanged"]
    switch: SwitchOut
    changed_intervals: list[ChangedInterval]
    segment_changes: list[SegmentChange] = []
    rechecked_batches: list[RecheckedBatch]
    rechecked_count: int


class SegmentChange(BaseModel):
    operation: Literal["insert", "update", "delete"]
    started_at: datetime
    ended_at: datetime | None = Field(description="null 表示工况段尚未结束")
    parameters: dict[str, float]
    old_started_at: datetime | None = Field(
        default=None, description="update/delete 时被替换旧段的起点"
    )


class SwitchOut(BaseModel):
    record_no: str
    switched_at: datetime
    parameters: dict[str, float]
    created_at: str
    updated_at: str


class ChangedInterval(BaseModel):
    start: datetime
    end: datetime | None = Field(description="null 表示一直延伸到无穷")
    before: dict[str, float] | None
    after: dict[str, float] | None


class RecheckedBatch(BaseModel):
    batch_no: str
    revision: int
    previous_eligible: bool | None
    eligible: bool
    status: Literal["eligible", "ineligible"]
    reason_code: str
    first_problem_at: datetime | None
    first_problem_kind: str | None
    transition: Literal["initial", "revoked", "restored", "unchanged"]


class SegmentOut(BaseModel):
    start: datetime
    end: datetime
    parameters: dict[str, float] | None
    matches_plan: bool
    kind: Literal["ok", "mismatch", "unknown"]
    switch_record_no: str | None
    switch_switched_at: datetime | None


class ViolationOut(BaseModel):
    at: datetime
    kind: Literal["ok", "mismatch", "unknown"]
    parameters: dict[str, float] | None
    switch_record_no: str | None
    switch_switched_at: datetime | None


class RevisionSummary(BaseModel):
    sequence: int
    revision: int
    trigger: str
    trigger_record_no: str | None
    status: Literal["eligible", "ineligible"]
    eligible: bool
    reason_code: str
    first_problem_at: datetime | None
    first_problem_kind: str | None
    first_mismatch_at: datetime | None
    first_unknown_at: datetime | None
    previous_eligible: bool | None
    transition: str | None
    evaluated_at: str


class ResultPayload(BaseModel):
    batch_no: str
    eligible: bool
    status: Literal["eligible", "ineligible"]
    reason_code: str
    reason: str
    sampling_start: datetime
    sampling_end: datetime
    window_start: datetime
    window_end: datetime
    stabilization_minutes: int
    plan_parameters: dict[str, float]
    first_problem_at: datetime | None
    first_problem_kind: Literal["mismatch", "unknown"] | None
    first_mismatch_at: datetime | None
    first_unknown_at: datetime | None
    first_problem_switch_record_no: str | None
    first_problem_switch_switched_at: datetime | None
    segments: list[SegmentOut]
    violations: list[ViolationOut]
    evaluated_at: str
    transition: str | None = None
    previous_eligible: bool | None = None
    revision: int | None = None
    trigger: str | None = None
    trigger_record_no: str | None = None


class BatchDetail(BaseModel):
    batch_no: str
    revision: int
    current_result: ResultPayload
    revision_history: list[RevisionSummary]
    created_at: str
    updated_at: str


class BatchSummary(BaseModel):
    batch_no: str
    revision: int
    status: Literal["eligible", "ineligible"] | None
    eligible: bool | None
    reason_code: str | None
    sampling_start: datetime
    sampling_end: datetime
    plan_parameters: dict[str, float]
    first_problem_at: datetime | None
    first_problem_kind: str | None
    first_mismatch_at: datetime | None
    first_unknown_at: datetime | None
    evaluated_at: str | None


class BatchList(BaseModel):
    items: list[BatchSummary]
    total: int
    limit: int
    offset: int


class SwitchList(BaseModel):
    items: list[SwitchOut]
    total: int


class CheckResponse(ResultPayload):
    revision: int
    revision_history: list[RevisionSummary]


# ------------------------------------------------------------ 工况分组

GroupStartParam = Annotated[
    datetime,
    Query(
        description="UTC 起始时刻（含），按采样开始时刻筛选，必须带时区",
        examples=["2026-11-23T08:00:00Z"],
    ),
]
GroupEndParam = Annotated[
    datetime,
    Query(
        description="UTC 结束时刻（不含），按采样开始时刻筛选，必须带时区",
        examples=["2026-11-23T16:00:00Z"],
    ),
]


class ConditionGroupQuery(BaseModel):
    """GET /analysis/condition-groups 的查询参数（带时区与区间校验）。"""

    start: GroupStartParam
    end: GroupEndParam

    @field_validator("start", "end")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @model_validator(mode="after")
    def _order(self) -> ConditionGroupQuery:
        if self.end <= self.start:
            raise ValueError("结束时刻必须晚于起始时刻（左闭右开区间）")
        return self


def condition_group_query(
    start: GroupStartParam,
    end: GroupEndParam,
) -> ConditionGroupQuery:
    """FastAPI 依赖：naive 时间与倒置区间在此处转为 422。"""
    return ConditionGroupQuery(start=start, end=end)


class ConditionSegmentGroup(BaseModel):
    started_at: datetime = Field(description="工况段实际开始时刻")
    ended_at: datetime | None = Field(
        description="工况段实际结束时刻；null 表示尚未结束"
    )
    parameters: dict[str, float]
    batch_nos: list[str] = Field(description="入选该工况段的合格批号")


class ParameterSegmentStat(BaseModel):
    parameters: dict[str, float]
    segment_count: int = Field(description="该组参数在查询区间内的运行段数（试验重复数）")
    batch_count: int = Field(description="该组参数下入选的合格批次数")


class ConditionGroupResponse(BaseModel):
    query: dict[str, datetime]
    segments: list[ConditionSegmentGroup]
    parameter_stats: list[ParameterSegmentStat]
    segment_count: int
    eligible_batch_count: int
    total_batch_count: int = Field(
        description="查询区间内全部批次数（含不合格，用于对照）"
    )


SwitchUpsertResult.model_rebuild()
