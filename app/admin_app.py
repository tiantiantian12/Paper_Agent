"""管理端服务：独立端口上的用户看板（默认端口 8010）。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import request_logs
from app.config import ADMIN_PORT
from app.database import init_db
from app.routers import admin, keys

WEB_DIR = Path(__file__).resolve().parent / "web"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    request_logs.start()
    yield
    request_logs.stop()


def create_admin_app() -> FastAPI:
    app = FastAPI(title="Paper Agent 用户看板", version="0.1.0", lifespan=lifespan)
    # 看板自己也是暴露在公网上的，谁在试口令一样要留痕（service=admin）
    app.add_middleware(request_logs.RequestLogMiddleware, service="admin")
    app.include_router(admin.router)
    app.include_router(keys.router)

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")
    return app


app = create_admin_app()


def port() -> int:
    return ADMIN_PORT
