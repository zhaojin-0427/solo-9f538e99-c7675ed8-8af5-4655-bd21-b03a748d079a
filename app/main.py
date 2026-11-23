"""FastAPI 应用入口。"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import asynccontextmanager, closing

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse

from . import __version__
from .db import Database
from .schemas import (
    BatchCreate,
    BatchDetail,
    BatchList,
    CheckResponse,
    SwitchList,
    SwitchOut,
    SwitchUpsert,
    SwitchUpsertResult,
)
from .service import CheckService, ServiceError

DEFAULT_DB_PATH = "data/checks.db"

DESCRIPTION = """
包装线封口参数与箱样核对 API。

**核对规则**

* 每个批次的检查窗口是 `[采样开始 − 5 分钟, 采样结束)`；
  计划参数必须在该窗口内**持续生效**。
* 只允许一条包装线：切换日志是全线的，任意切换记录影响所有批次。
* 设备在 `时刻 t` 的有效参数 = `switched_at <= t` 的最近一条切换记录；
  首条切换记录之前视为**未知**。
* 切换日志按记录号补录或更正；**切换时刻全局唯一**。
* 更正日志后只重查「检查窗口与变更时段相交」的批次，资格可撤下或恢复。
* 切换日志与核对结果在同一事务内生效；校验失败时整体回滚，沿用原结果。

时刻一律使用带时区的 UTC 时间（如 `2026-11-23T09:00:00Z`），精确到秒。
"""


def create_app(database_path: str | None = None) -> FastAPI:
    db_path = database_path or os.environ.get("CHECK_DB_PATH", DEFAULT_DB_PATH)
    database = Database(db_path)
    service = CheckService(database)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> Iterator[None]:
        database.initialize()
        yield

    app = FastAPI(
        title="包装线封口参数箱样核对 API",
        description=DESCRIPTION,
        version=__version__,
        lifespan=lifespan,
    )
    app.state.database = database
    app.state.service = service

    @app.exception_handler(ServiceError)
    async def _service_error(_: Request, exc: ServiceError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.message, "code": exc.code},
        )

    def get_service() -> CheckService:
        return app.state.service

    def get_db() -> Database:
        return app.state.database

    @app.get("/health", tags=["运维"], summary="健康检查")
    def health(db: Database = Depends(get_db)) -> dict:
        with closing(db.connect()) as conn:
            conn.execute("SELECT 1")
        return {"status": "ok", "version": __version__, "database": db.path}

    # ------------------------------------------------------------ 切换日志

    @app.get(
        "/switches",
        response_model=SwitchList,
        tags=["切换日志"],
        summary="列出全部切换记录（按切换时刻排序）",
    )
    def list_switches(svc: CheckService = Depends(get_service)) -> dict:
        return svc.list_switches()

    @app.get(
        "/switches/{record_no}",
        response_model=SwitchOut,
        tags=["切换日志"],
        summary="按记录号查看切换记录",
    )
    def get_switch(record_no: str, svc: CheckService = Depends(get_service)) -> dict:
        return svc.get_switch(record_no)

    @app.put(
        "/switches/{record_no}",
        response_model=SwitchUpsertResult,
        tags=["切换日志"],
        summary="补录或更正切换记录",
        description=(
            "记录号不存在时补录，已存在时更正。切换时刻不能与其他记录重复。\n\n"
            "写入后会计算有效参数变化的时段，只重查检查窗口与之相交的批次；"
            "日志与重查结果在同一事务提交。"
        ),
        responses={
            409: {"description": "切换时刻已被其他记录号占用"},
            422: {"description": "参数或时间格式错误"},
        },
    )
    def upsert_switch(
        record_no: str,
        body: SwitchUpsert,
        svc: CheckService = Depends(get_service),
    ) -> dict:
        return svc.upsert_switch(
            record_no,
            switched_at=body.switched_at,
            params=body.parameters.normalized(),
        )

    # ---------------------------------------------------------------- 批次

    @app.post(
        "/batches",
        response_model=BatchDetail,
        status_code=201,
        tags=["批次"],
        summary="录入批号、计划参数与 UTC 采样起止时刻",
        responses={409: {"description": "批号已存在"}},
    )
    def create_batch(
        body: BatchCreate, svc: CheckService = Depends(get_service)
    ) -> dict:
        return svc.create_batch(
            batch_no=body.batch_no,
            plan=body.plan_parameters.normalized(),
            sampling_start=body.sampling_start,
            sampling_end=body.sampling_end,
        )

    @app.get(
        "/batches",
        response_model=BatchList,
        tags=["批次"],
        summary="列出批次及当前核对结论",
    )
    def list_batches(
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
        svc: CheckService = Depends(get_service),
    ) -> dict:
        return svc.list_batches(limit, offset)

    @app.get(
        "/batches/{batch_no}",
        response_model=BatchDetail,
        tags=["批次"],
        summary="按批号查看核对结论与修订历史",
        responses={404: {"description": "批号不存在"}},
    )
    def get_batch(batch_no: str, svc: CheckService = Depends(get_service)) -> dict:
        return svc.get_batch(batch_no)

    @app.get(
        "/batches/{batch_no}/check",
        response_model=CheckResponse,
        tags=["批次"],
        summary="按批号查询能否纳入分析，以及首个不符/未知时刻",
        responses={404: {"description": "批号不存在"}},
    )
    def check_batch(batch_no: str, svc: CheckService = Depends(get_service)) -> dict:
        return svc.check_batch(batch_no)

    return app


app = create_app()
