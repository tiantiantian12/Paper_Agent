"""密钥管理接口（服务端看板调用）：内置模型、供应商密钥池、用户密钥。"""

from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException

from app import llm_store, request_logs, store
from app.config import ADMIN_ACCESS_TOKEN, USER_KEY_TTL_DAYS
from app.schemas import (
    ModelIn,
    ModelPatch,
    ProviderKeyIn,
    StatusPatch,
    UserKeyIn,
    UserKeyPatch,
)

router = APIRouter(prefix="/api/admin", tags=["keys"])


def _guard(token: str | None) -> None:
    """配置口令时校验，未配置则放行（仅本机演示）。"""
    if ADMIN_ACCESS_TOKEN and token != ADMIN_ACCESS_TOKEN:
        raise HTTPException(status_code=401, detail="管理口令不正确")


def _bad(exc: Exception, status: int = 400) -> HTTPException:
    return HTTPException(status_code=status, detail=str(exc))


# ------------------------------------------------------------------ 内置模型
@router.get("/models")
def list_models(x_admin_token: str | None = Header(default=None)) -> list[dict]:
    _guard(x_admin_token)
    return llm_store.list_models()


@router.post("/models")
def add_model(payload: ModelIn, x_admin_token: str | None = Header(default=None)) -> dict:
    _guard(x_admin_token)
    try:
        return llm_store.add_model(
            payload.model_id, payload.name, payload.base_url, payload.desc, payload.kind
        )
    except llm_store.LlmError as exc:
        raise _bad(exc) from exc


@router.patch("/models/{model_id}")
def patch_model(
    model_id: str, payload: ModelPatch, x_admin_token: str | None = Header(default=None)
) -> dict:
    _guard(x_admin_token)
    try:
        llm_store.set_model(
            model_id,
            name=payload.name,
            base_url=payload.base_url,
            enabled=payload.enabled,
            desc=payload.desc,
            kind=payload.kind,
        )
    except llm_store.LlmError as exc:
        raise _bad(exc) from exc
    return {"ok": True}


@router.delete("/models/{model_id}")
def remove_model(model_id: str, x_admin_token: str | None = Header(default=None)) -> dict:
    _guard(x_admin_token)
    llm_store.delete_model(model_id)
    return {"ok": True}


# ------------------------------------------------------------------ 供应商密钥池
@router.get("/provider-keys")
def list_provider_keys(
    model_id: str = "", x_admin_token: str | None = Header(default=None)
) -> list[dict]:
    _guard(x_admin_token)
    return llm_store.list_provider_keys(model_id)


@router.post("/provider-keys")
def add_provider_key(
    payload: ProviderKeyIn, x_admin_token: str | None = Header(default=None)
) -> dict:
    _guard(x_admin_token)
    try:
        return llm_store.add_provider_key(payload.model_id, payload.api_key, payload.label)
    except llm_store.LlmError as exc:
        raise _bad(exc) from exc


@router.get("/provider-keys/{key_id}/secret")
def provider_key_secret(
    key_id: int, x_admin_token: str | None = Header(default=None)
) -> dict:
    """单取一把供应商密钥的明文，供看板「复制」按钮使用。

    列表接口仍然只回打码值；新加模型想复用旧模型的 Key 时，复制一下最省事。
    """
    _guard(x_admin_token)
    try:
        return {"api_key": llm_store.provider_key_secret(key_id)}
    except llm_store.LlmError as exc:
        raise _bad(exc, status=404) from exc


@router.patch("/provider-keys/{key_id}")
def patch_provider_key(
    key_id: int, payload: StatusPatch, x_admin_token: str | None = Header(default=None)
) -> dict:
    _guard(x_admin_token)
    try:
        llm_store.set_provider_key_status(key_id, payload.status)
    except llm_store.LlmError as exc:
        raise _bad(exc) from exc
    return {"ok": True}


@router.delete("/provider-keys/{key_id}")
def remove_provider_key(key_id: int, x_admin_token: str | None = Header(default=None)) -> dict:
    _guard(x_admin_token)
    llm_store.delete_provider_key(key_id)
    return {"ok": True}


# ------------------------------------------------------------------ 用户密钥
@router.get("/user-keys")
def list_user_keys(
    user_id: int | None = None, x_admin_token: str | None = Header(default=None)
) -> list[dict]:
    _guard(x_admin_token)
    return llm_store.list_user_keys(user_id)


@router.post("/user-keys")
def add_user_key(payload: UserKeyIn, x_admin_token: str | None = Header(default=None)) -> dict:
    _guard(x_admin_token)
    if store.get_user_by_id(payload.user_id) is None:
        raise _bad(store.AuthError("用户不存在"), status=404)
    try:
        row = llm_store.create_user_key(
            payload.user_id, payload.label, payload.days, payload.allowed_models
        )
    except llm_store.LlmError as exc:
        raise _bad(exc) from exc
    return llm_store._user_key_out(row)      # noqa: SLF001 - 同包内复用序列化


@router.patch("/user-keys/{key_id}")
def patch_user_key(
    key_id: int, payload: UserKeyPatch, x_admin_token: str | None = Header(default=None)
) -> dict:
    _guard(x_admin_token)
    try:
        llm_store.set_user_key(
            key_id, status=payload.status, label=payload.label, expires_at=payload.expires_at
        )
    except llm_store.LlmError as exc:
        raise _bad(exc) from exc
    return {"ok": True}


@router.delete("/user-keys/{key_id}")
def remove_user_key(key_id: int, x_admin_token: str | None = Header(default=None)) -> dict:
    _guard(x_admin_token)
    llm_store.delete_user_key(key_id)
    return {"ok": True}


# ------------------------------------------------------------------ 模型健康 / 限流
@router.get("/model-health")
def model_health(
    hours: float = 24, x_admin_token: str | None = Header(default=None)
) -> list[dict]:
    """看板「模型 / 限流」面板的数据：**一眼看出是哪个模型在挨限流**。

    要回答「现在卡住的是谁」，得把三块信息凑到一起 —— 少一块就只能靠猜：

    1. **模型自身**：类型（对话 / 生图 / 视频）、启用与否；
    2. **它的供应商密钥池**：有几把、几把能用、有没有在冷却、最后一次报什么错
       （被限流的那把会被放进冷却，下一轮自动换一把）；
    3. **近期这个模型返回了多少次 429**，以及最后一次被限流的时间。

    按「限流次数」降序排，被卡的模型会浮在最上面。
    """
    _guard(x_admin_token)
    limited = {
        item["model"]: item
        for item in request_logs.model_stats(hours=hours, limit=200)
    }
    items: list[dict] = []
    for model in llm_store.list_models():
        model_id = model["model_id"]
        keys = llm_store.list_provider_keys(model_id)
        cooling = [item for item in keys if item.get("cooling")]
        usable = [item for item in keys if item["status"] == "active" and not item["cooling"]]
        # 最近一次报错取池子里最「新鲜」的那条（冷却中的那把通常就是它）
        errors = [item for item in keys if item.get("last_error")]
        last_error = errors[-1]["last_error"] if errors else ""
        stat = limited.get(model_id, {})
        items.append(
            {
                "model_id": model_id,
                "name": model.get("name") or model_id,
                "kind": llm_store.normalize_kind(model.get("kind")),
                "enabled": bool(model.get("enabled")),
                "keys": len(keys),
                "usable_keys": len(usable),
                "cooling_keys": len(cooling),
                "cooling_detail": "、".join(
                    f"{item['api_key']}（{item['label'] or '无标签'}）" for item in cooling
                ),
                "last_error": last_error,
                "hits": int(stat.get("hits") or 0),
                "limited": int(stat.get("limited") or 0),
                "errors": int(stat.get("errors") or 0),
                "last_limited_at": stat.get("last_limited_at") or "",
                "last_at": stat.get("last_at") or "",
            }
        )
    items.sort(key=lambda item: (item["limited"], item["errors"], item["hits"]), reverse=True)
    return items


# ------------------------------------------------------------------ 看板概览
@router.get("/llm-stats")
def llm_stats(x_admin_token: str | None = Header(default=None)) -> dict:
    _guard(x_admin_token)
    return {
        "models": len(llm_store.list_models()),
        "user_keys": llm_store.count_user_keys(),
        "active_user_keys": llm_store.count_active_user_keys(),
        "default_ttl_days": USER_KEY_TTL_DAYS,
    }
