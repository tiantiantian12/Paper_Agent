"""主服务：桌面端注册 / 登录 API（默认端口 8000）。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from app import request_logs, smtp_service
from app.config import (
    API_PORT,
    APP_TITLE,
    APP_VERSION,
    DOWNLOAD_DIR,
    EXE_NAME,
    SMTP_CONFIG,
)
from app.database import init_db
from app.routers import auth, llm

SITE_DIR = Path(__file__).resolve().parent / "web" / "site"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    request_logs.start()
    yield
    request_logs.stop()


def create_app() -> FastAPI:
    app = FastAPI(title=APP_TITLE, version=APP_VERSION, lifespan=lifespan)
    # 桌面端与浏览器直接调用，放开跨域
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # 请求日志：谁（IP / 昵称 / 邮箱）在什么时候打了什么，看板里能查
    app.add_middleware(request_logs.RequestLogMiddleware, service="api")
    app.include_router(auth.router)
    app.include_router(llm.router)

    @app.get("/")
    def root() -> FileResponse:
        """落地页：介绍客户端并提供 exe 下载入口。"""
        return FileResponse(SITE_DIR / "index.html")

    @app.get("/download", response_model=None)
    def download_client() -> FileResponse | HTMLResponse:
        """下载桌面端 exe；文件不存在时返回 404 提示。"""
        path = DOWNLOAD_DIR / EXE_NAME
        if not path.exists():
            return HTMLResponse(
                "<!doctype html><meta charset='utf-8'><title>未上传</title>"
                "<h2>安装包尚未上传</h2><p>管理员还未构建并上传客户端安装包。</p>"
                "<p><a href='/'>返回首页</a></p>",
                status_code=404,
            )
        return FileResponse(
            str(path),
            filename=EXE_NAME,
            media_type="application/octet-stream",
        )

    @app.get("/api/site/info")
    def site_info() -> dict:
        """落地页用的元信息：下载是否就绪、版本号等。"""
        return {
            "name": APP_TITLE,
            "version": APP_VERSION,
            "download_ready": (DOWNLOAD_DIR / EXE_NAME).exists(),
            "exe_name": EXE_NAME,
        }

    @app.get("/health")
    def health() -> dict:
        return {
            "ok": True,
            "smtp_ready": smtp_service is not None and bool(SMTP_CONFIG.get("sender")),
            "port": API_PORT,
        }

    # 落地页静态资源（style.css / app.js）
    app.mount("/site-static", StaticFiles(directory=str(SITE_DIR)), name="site-static")

    return app


app = create_app()
