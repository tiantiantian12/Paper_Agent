"""管理端接口：用户看板数据。"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Header, HTTPException, Response

from app import request_logs, store
from app.config import ADMIN_ACCESS_TOKEN, STORE_PLAIN_PASSWORD
from app.schemas import AdminUserOut, StatsOut

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _guard(token: str | None) -> None:
    """配置口令时校验，未配置则放行（仅本机演示）。"""
    if ADMIN_ACCESS_TOKEN and token != ADMIN_ACCESS_TOKEN:
        raise HTTPException(status_code=401, detail="管理口令不正确")


@router.get("/users", response_model=list[AdminUserOut])
def list_users(
    q: str = "",
    limit: int = 100,
    offset: int = 0,
    x_admin_token: str | None = Header(default=None),
) -> list[dict]:
    _guard(x_admin_token)
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    rows = store.list_users(q, limit, offset)
    if not STORE_PLAIN_PASSWORD:
        for row in rows:
            row["password"] = None
    return rows


@router.get("/stats", response_model=StatsOut)
def stats(x_admin_token: str | None = Header(default=None)) -> dict:
    _guard(x_admin_token)
    return {
        "users": store.count_users(),
        "today_users": store.count_today_users(),
        "codes_today": store.count_codes_today(),
        "active_tokens": store.count_active_tokens(),
    }


@router.get("/config")
def config() -> dict:
    """前端需要的运行参数。"""
    return {
        "require_token": bool(ADMIN_ACCESS_TOKEN),
        "show_password": STORE_PLAIN_PASSWORD,
    }


# ------------------------------------------------------------------ 请求日志
# 谁（IP / 昵称 / 邮箱）在什么时候打了哪个接口、结果如何 —— 被刷 / 被打时查它。
@router.get("/logs")
def logs(
    q: str = "",
    ip: str = "",
    status: str = "",
    service: str = "",
    model: str = "",
    hours: float = 24,
    limit: int = 200,
    offset: int = 0,
    x_admin_token: str | None = Header(default=None),
) -> dict:
    """日志明细（按时间倒序）。``q`` 是关键词，`ip` / `model` 精确到某个来源 / 模型。"""
    _guard(x_admin_token)
    return request_logs.query_logs(
        q=q, ip=ip, status=status, service=service, model=model,
        hours=hours, limit=limit, offset=offset,
    )


@router.get("/logs/stats")
def logs_stats(
    hours: float = 24,
    top: int = 20,
    x_admin_token: str | None = Header(default=None),
) -> dict:
    """概览：总量 / 异常数 / 独立 IP，外加「按 IP」「按接口」「按模型」的排行。"""
    _guard(x_admin_token)
    return {
        "summary": request_logs.summary(hours=hours),
        "ips": request_logs.ip_stats(hours=hours, limit=top),
        "paths": request_logs.path_stats(hours=hours),
        "models": request_logs.model_stats(hours=hours, limit=top),
        "limited": sum(
            item["limited"] for item in request_logs.model_stats(hours=hours, limit=200)
        ),
    }


@router.get("/logs/models")
def logs_models(
    hours: float = 24,
    top: int = 50,
    x_admin_token: str | None = Header(default=None),
) -> list[dict]:
    """按模型看限流：每个模型近期的请求数、429 次数、最后一次被限流的时间。"""
    _guard(x_admin_token)
    return request_logs.model_stats(hours=hours, limit=top)


@router.delete("/logs")
def clear_logs(
    q: str = "",
    ip: str = "",
    status: str = "",
    service: str = "",
    model: str = "",
    hours: float = 0,
    x_admin_token: str | None = Header(default=None),
) -> dict:
    """清空日志；带上筛选条件就只清那一部分（默认全清）。"""
    _guard(x_admin_token)
    removed = request_logs.clear(
        q=q, ip=ip, status=status, service=service, model=model, hours=hours
    )
    return {"ok": True, "removed": removed}


@router.get("/logs/export")
def export_logs(
    q: str = "",
    ip: str = "",
    status: str = "",
    service: str = "",
    model: str = "",
    hours: float = 24,
    limit: int = 20000,
    x_admin_token: str | None = Header(default=None),
) -> Response:
    """导出 CSV 留证（Excel 可直接打开）。"""
    _guard(x_admin_token)
    text = request_logs.export_csv(
        q=q, ip=ip, status=status, service=service, model=model, hours=hours, limit=limit
    )
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    return Response(
        content=text.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="request-logs-{stamp}.csv"'},
    )
