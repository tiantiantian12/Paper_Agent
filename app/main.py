"""主服务：桌面端注册 / 登录 API（默认端口 8000）。"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import request_logs, smtp_service
from app.config import API_PORT, APP_TITLE, APP_VERSION, SMTP_CONFIG
from app.database import init_db
from app.routers import auth, llm


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
    def root() -> dict:
        return {
            "service": APP_TITLE,
            "version": APP_VERSION,
            "docs": "/docs",
            "auth_api": "/api/auth",
            "llm_proxy": "/api/llm/v1/chat/completions",
        }

    @app.get("/health")
    def health() -> dict:
        return {
            "ok": True,
            "smtp_ready": smtp_service is not None and bool(SMTP_CONFIG.get("sender")),
            "port": API_PORT,
        }

    return app


app = create_app()
