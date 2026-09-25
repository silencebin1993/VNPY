"""
通用接口：白名单后台任务（POST /api/jobs/run）、任务清单、第三版功能总览。
"""
from typing import Annotated, Any

from fastapi import APIRouter, Body

from ..common import ok


router = APIRouter()


@router.get("/api/tasks")
def tasks_list() -> Any:
    from ... import tasks
    return ok({"tasks": tasks.listing()})


@router.post("/api/jobs/run")
def jobs_run(
    name: Annotated[str, Body(embed=True)],
    params: Annotated[dict | None, Body(embed=True)] = None,
) -> Any:
    """启动一个登记过的后台任务，返回 {job_id}；进度用 GET /api/jobs/{id} 查询"""
    from ... import tasks
    return ok({"job_id": tasks.submit(name, params)})
