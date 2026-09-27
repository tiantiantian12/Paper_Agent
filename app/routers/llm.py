"""大模型代理：桌面端的内置模型统一经服务端转发到供应商。

链路::

    桌面端 ──(Bearer 用户密钥)──▶ /api/llm/v1/chat/completions ──(Bearer 供应商 Key)──▶ 供应商

要点：

- **用户密钥**：每个用户一把（可多把），服务端可设过期时间 / 停用；密钥不对或
  已过期的请求直接挡在服务端，不会打到供应商。
- **供应商密钥池**：同一个模型可以配多个 Key，轮换使用；命中限流或 5xx 时把该
  Key 放进冷却，自动换下一个重试。
- **接口形态**：对外就是 OpenAI 兼容的 ``/v1/chat/completions``（支持 ``stream``），
  桌面端现有的 OpenAI 兼容客户端不用改就能用。
"""

from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from typing import Iterator

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app import llm_store, request_logs
from app.config import (
    PROXY_CONNECT_TIMEOUT,
    PROXY_MAX_KEY_TRIES,
    PROXY_READ_TIMEOUT,
    USER_KEY_TTL_DAYS,
)
from app.schemas import BuiltinModelOut, UserKeyOut

router = APIRouter(prefix="/api/llm", tags=["llm"])


def _no_provider_key_error(model_id: str) -> HTTPException:
    """一把可用密钥都没有时的报错：**限流要说成限流**。

    冷却几乎都是上游 429 触发的（``inference exceeds tpm/rpm limit``）。以前一律回
    503「暂无可用的供应商密钥」，客户端把它当网络故障，1.5 秒后立刻重发 —— 于是一直
    撞限流、一直 503（2026-09-26：deepseek-flash 两把 Key 全在冷却，客户端按秒重发）。
    回 429 才会走客户端的**限流退避**（20s / 45s / 90s），用户看到的也是「服务繁忙」。
    """
    detail = f"模型 {model_id} 暂无可用的供应商密钥"
    try:
        rows = llm_store.list_provider_keys(model_id) or []
    except Exception:      # noqa: BLE001 - 读不到就按普通 503 报
        rows = []
    cooling = [
        str(row.get("last_error") or "")
        for row in rows
        if row.get("cooling") or row.get("cooldown_until")
    ]
    if any("429" in text or "rate" in text.lower() or "限流" in text for text in cooling):
        return HTTPException(
            status_code=429,
            detail=f"上游限流：模型 {model_id} 的供应商密钥都在冷却中，请稍后再试",
        )
    return HTTPException(status_code=503, detail=detail)


def _upstream_url(base_url: str, path: str) -> str:
    """拼上游地址：**路径不重复**。

    看板里很容易把 Base URL 填成**完整接口地址**（``https://x/v1/chat/completions``），
    而这里还会再拼一次 ``/chat/completions`` —— 于是请求打到
    ``/v1/chat/completions/chat/completions``，供应商直接 404
    （2026-09-26：``deepseek-flash`` 这条就是这么配的，非流式请求全 404）。
    所以结尾已经带了这段路径时不再追加。
    """
    root = str(base_url or "").strip().rstrip("/")
    if not root:
        return path
    return root if root.endswith(path) else f"{root}{path}"


def _bearer(authorization: str | None) -> str:
    if not authorization:
        return ""
    lowered = authorization.strip()
    return lowered[7:].strip() if lowered.lower().startswith("bearer ") else lowered


def _bad(exc: Exception, status: int = 400) -> HTTPException:
    return HTTPException(status_code=status, detail=str(exc))


# ------------------------------------------------------------------ 代理入口
@router.post("/v1/chat/completions", response_model=None)
async def chat_completions(
    request: Request,
    authorization: str | None = Header(default=None),
    x_user_key: str | None = Header(default=None),
):
    """OpenAI 兼容的聊天补全代理。

    ``response_model=None``：返回值可能是 JSONResponse 也可能是流式响应，
    不能让 FastAPI 按类型注解去推导响应模型。
    """
    try:
        raw = await request.body()
        payload = json.loads(raw.decode("utf-8")) if raw else {}
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="请求体不是合法 JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")

    model_id = str(payload.get("model") or "").strip()
    if not model_id:
        raise HTTPException(status_code=400, detail="缺少 model 字段")
    messages = payload.get("messages")
    request_logs.note(
        request,
        f"对话 · 模型 {model_id} · {'流式' if payload.get('stream') else '非流式'}"
        f" · {len(messages) if isinstance(messages, list) else 0} 条消息",
        model=model_id,
    )

    # 1) 校验用户密钥
    user_key_value = x_user_key or _bearer(authorization)
    try:
        user_key = llm_store.resolve_user_key(user_key_value)
    except llm_store.LlmError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    if not llm_store.model_allowed(user_key, model_id):
        raise HTTPException(status_code=403, detail="该密钥没有此模型的访问权限")

    # 2) 找到内置模型配置
    model = llm_store.get_model(model_id)
    if model is None:
        raise HTTPException(status_code=404, detail=f"服务端未配置模型 {model_id}")
    if not model["enabled"]:
        raise HTTPException(status_code=403, detail=f"模型 {model_id} 已停用")

    llm_store.touch_user_key(user_key["id"])
    wants_stream = bool(payload.get("stream"))
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    # 3) 轮流试供应商密钥（限流 / 服务异常时换下一个）
    last_error: HTTPException | None = None
    for _attempt in range(max(1, PROXY_MAX_KEY_TRIES)):
        provider_key = llm_store.pick_provider_key(model_id)
        if provider_key is None:
            raise _no_provider_key_error(model_id)
        url = _upstream_url(model["base_url"], "/chat/completions")
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if wants_stream else "application/json",
            "Authorization": f"Bearer {provider_key['api_key']}",
        }

        try:
            if wants_stream:
                llm_store.mark_provider_key_used(provider_key["id"])
                return StreamingResponse(
                    _iter_upstream(url, headers, body),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                )
            status, raw_text = _post_upstream(url, headers, body)
        except urllib.error.HTTPError as exc:
            detail = _read_error(exc)
            cooldown = exc.code == 429 or exc.code >= 500
            llm_store.mark_provider_key_error(
                provider_key["id"], f"HTTP {exc.code} {detail}", cooldown=cooldown
            )
            last_error = HTTPException(status_code=exc.code, detail=detail)
            if not cooldown:
                raise last_error
            continue
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            llm_store.mark_provider_key_error(
                provider_key["id"], f"网络错误：{exc}", cooldown=True
            )
            last_error = HTTPException(status_code=502, detail=f"无法连接大模型服务：{exc}")
            continue

        if status >= 400:
            cooldown = status == 429 or status >= 500
            llm_store.mark_provider_key_error(
                provider_key["id"], f"HTTP {status} {raw_text[:200]}", cooldown=cooldown
            )
            last_error = HTTPException(status_code=status, detail=raw_text[:2000])
            if not cooldown:
                raise last_error
            continue

        llm_store.mark_provider_key_used(provider_key["id"])
        return JSONResponse(content=json.loads(raw_text or "{}"), status_code=status)

    raise last_error or HTTPException(status_code=502, detail="代理请求失败")


@router.post("/v1/images/generations", response_model=None)
async def images_generations(
    request: Request,
    authorization: str | None = Header(default=None),
    x_user_key: str | None = Header(default=None),
):
    """文生图代理：和对话代理共用同一套用户密钥与供应商密钥池。

    出图比对话慢得多（几十秒起），也更容易命中限流，所以同样会在供应商密钥之间
    轮换；桌面端只认识服务端地址，供应商的 Base URL 与 Key 都留在看板里。
    请求体按 OpenAI 兼容的 ``/images/generations`` 原样转发。
    """
    try:
        raw = await request.body()
        payload = json.loads(raw.decode("utf-8")) if raw else {}
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="请求体不是合法 JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")

    model_id = str(payload.get("model") or "").strip()
    if not model_id:
        raise HTTPException(status_code=400, detail="缺少 model 字段")
    request_logs.note(
        request,
        f"文生图 · 模型 {model_id} · {payload.get('size') or '默认尺寸'}"
        f" · {payload.get('n') or 1} 张",
        model=model_id,
    )

    user_key_value = x_user_key or _bearer(authorization)
    try:
        user_key = llm_store.resolve_user_key(user_key_value)
    except llm_store.LlmError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    if not llm_store.model_allowed(user_key, model_id):
        raise HTTPException(status_code=403, detail="该密钥没有此模型的访问权限")

    model = llm_store.get_model(model_id)
    if model is None:
        raise HTTPException(status_code=404, detail=f"服务端未配置模型 {model_id}")
    if not model["enabled"]:
        raise HTTPException(status_code=403, detail=f"模型 {model_id} 已停用")

    llm_store.touch_user_key(user_key["id"])
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    last_error: HTTPException | None = None

    for _attempt in range(max(1, PROXY_MAX_KEY_TRIES)):
        provider_key = llm_store.pick_provider_key(model_id)
        if provider_key is None:
            raise _no_provider_key_error(model_id)
        url = _upstream_url(model["base_url"], "/images/generations")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {provider_key['api_key']}",
        }
        try:
            status, raw_text = _post_upstream(url, headers, body)
        except urllib.error.HTTPError as exc:
            detail = _read_error(exc)
            cooldown = exc.code == 429 or exc.code >= 500
            llm_store.mark_provider_key_error(
                provider_key["id"], f"HTTP {exc.code} {detail}", cooldown=cooldown
            )
            last_error = HTTPException(status_code=exc.code, detail=detail)
            if not cooldown:
                raise last_error
            continue
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            llm_store.mark_provider_key_error(
                provider_key["id"], f"网络错误：{exc}", cooldown=True
            )
            last_error = HTTPException(status_code=502, detail=f"无法连接生图服务：{exc}")
            continue

        if status >= 400:
            cooldown = status == 429 or status >= 500
            llm_store.mark_provider_key_error(
                provider_key["id"], f"HTTP {status} {raw_text[:200]}", cooldown=cooldown
            )
            last_error = HTTPException(status_code=status, detail=raw_text[:2000])
            if not cooldown:
                raise last_error
            continue

        llm_store.mark_provider_key_used(provider_key["id"])
        try:
            return JSONResponse(content=json.loads(raw_text or "{}"), status_code=status)
        except ValueError as exc:
            raise HTTPException(status_code=502, detail="生图服务返回了无法解析的内容") from exc

    raise last_error or HTTPException(status_code=502, detail="生图代理请求失败")


# ------------------------------------------------------------------ 视频代理
# 文生视频不是一次请求出结果：先 POST /videos 建任务，再拿 video_id 反复查
# /agnesapi 直到 completed / failed。供应商给的地址（apihub 的 /agnesapi）和
# base_url 不同根，这里按文档拼：base_url 去掉 /v1 → 拼上 /agnesapi。
VIDEO_STATUS_PATH = "/agnesapi"

# 任务 → 建它时用的那把供应商 Key。
#
# 真实事故（2026-09-24）：视频模型在池子里配了**两把** Key，「建任务」按「最久未用」
# 取到 A，0.4 秒后「查结果」取到 B —— 任务是挂在 A 那个账号下的，B 去查就是
# 404「任务不存在」（服务端日志里就是 POST 200 + 紧接着 GET 404）。
# 所以建完任务要把「用的哪把 Key」记下来，后面轮询照用它。
_VIDEO_TASK_KEYS: dict[str, str] = {}
_VIDEO_TASK_KEYS_MAX = 500      # 只留最近这么多条，免得攒成内存垃圾


def _extract_video_id(raw_text: str) -> str:
    """从创建响应里取任务 id（各家字段名不同，逐个认）。"""
    try:
        data = json.loads(raw_text or "")
    except ValueError:
        return ""
    if not isinstance(data, dict):
        return ""
    for key in ("video_id", "videoId", "id", "task_id", "taskId"):
        value = data.get(key)
        if value:
            return str(value)
    return ""


def _remember_task_key(video_id: str, api_key: str) -> None:
    """记住「这个任务是哪把 Key 建的」。"""
    if not video_id or not api_key:
        return
    if len(_VIDEO_TASK_KEYS) >= _VIDEO_TASK_KEYS_MAX:
        # 简单丢掉最早的一批：任务查完就不需要了，这里只是兜底防膨胀
        for stale in list(_VIDEO_TASK_KEYS)[: _VIDEO_TASK_KEYS_MAX // 2]:
            _VIDEO_TASK_KEYS.pop(stale, None)
    _VIDEO_TASK_KEYS[video_id] = api_key


def _video_model_or_404(model_id: str) -> sqlite3.Row:
    model = llm_store.get_model(model_id)
    if model is None:
        raise HTTPException(status_code=404, detail=f"服务端未配置模型 {model_id}")
    if not model["enabled"]:
        raise HTTPException(status_code=403, detail=f"模型 {model_id} 已停用")
    if llm_store.normalize_kind(model["kind"]) != llm_store.KIND_VIDEO:
        raise HTTPException(status_code=400, detail=f"模型 {model_id} 不是视频模型")
    return model


def _video_origin(base_url: str) -> str:
    """视频结果查询的根地址（去掉 base_url 末尾的 /v1）。"""
    root = (base_url or "").rstrip("/")
    return root[: -len("/v1")] if root.endswith("/v1") else root


@router.post("/v1/videos", response_model=None)
async def videos_create(
    request: Request,
    authorization: str | None = Header(default=None),
    x_user_key: str | None = Header(default=None),
):
    """文生视频：提交生成任务，返回供应商的原始响应（里面有 video_id）。

    出视频很吃配额，免费额度下供应商会回 429 / 503（队列满），这里把状态码与错误
    文案原样透传，让客户端能明确告诉用户「稍后再试」而不是报成网络故障。
    """
    payload, user_key, model = await _video_prepare(
        request, authorization, x_user_key
    )
    request_logs.note(
        request,
        f"文生视频 · 模型 {model['model_id']} · {payload.get('seconds') or 5}s"
        f" {payload.get('size') or '720P'} · {payload.get('mode') or 'text'}",
        model=model["model_id"],
    )
    llm_store.touch_user_key(user_key["id"])
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    status, raw_text, api_key = _video_post(
        _upstream_url(model["base_url"], "/videos"), model, body
    )
    # 记住这把 Key：轮询查结果必须用同一把，否则 404「任务不存在」（见 _pick_key）
    _remember_task_key(_extract_video_id(raw_text), api_key)
    try:
        return JSONResponse(content=json.loads(raw_text or "{}"), status_code=status)
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="视频服务返回了无法解析的内容") from exc


@router.get("/v1/videos/{video_id}", response_model=None)
def videos_status(
    video_id: str,
    request: Request,
    model_name: str,      # 供应商按「video_id + model_name」查结果，必填
    authorization: str | None = Header(default=None),
    x_user_key: str | None = Header(default=None),
):
    """查视频任务结果（客户端会反复轮询，直到 completed / failed）。"""
    request_logs.note(
        request, f"查视频任务 · {model_name} · {video_id}", model=model_name
    )
    user_key_value = x_user_key or _bearer(authorization)
    try:
        user_key = llm_store.resolve_user_key(user_key_value)
    except llm_store.LlmError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    if not llm_store.model_allowed(user_key, model_name):
        raise HTTPException(status_code=403, detail="该密钥没有此模型的访问权限")

    if not model_name:
        raise HTTPException(status_code=400, detail="缺少 model_name（要查的是哪个模型）")
    model = _video_model_or_404(model_name)
    query = urllib.parse.urlencode({"video_id": video_id, "model_name": model_name})
    url = f"{_video_origin(model['base_url'])}{VIDEO_STATUS_PATH}?{query}"
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {_pick_key(model_name, video_id)}",
    }
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, headers=headers), timeout=PROXY_READ_TIMEOUT
        ) as response:
            raw_text = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raise HTTPException(status_code=exc.code, detail=_read_error(exc)) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise HTTPException(status_code=502, detail=f"无法连接视频服务：{exc}") from exc
    try:
        return JSONResponse(content=json.loads(raw_text or "{}"), status_code=200)
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="视频服务返回了无法解析的内容") from exc


async def _video_prepare(request: Request, authorization: str | None, x_user_key: str | None):
    """视频接口的公共校验：解析请求体 → 校验用户密钥 → 取出视频模型。"""
    try:
        raw = await request.body()
        payload = json.loads(raw.decode("utf-8")) if raw else {}
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="请求体不是合法 JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")

    model_id = str(payload.get("model") or "").strip()
    if not model_id:
        raise HTTPException(status_code=400, detail="缺少 model 字段")

    user_key_value = x_user_key or _bearer(authorization)
    try:
        user_key = llm_store.resolve_user_key(user_key_value)
    except llm_store.LlmError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    if not llm_store.model_allowed(user_key, model_id):
        raise HTTPException(status_code=403, detail="该密钥没有此模型的访问权限")
    return payload, user_key, _video_model_or_404(model_id)


def _pick_key(model_id: str, video_id: str = "") -> str:
    """取一把可用的供应商密钥（视频生成慢，不做轮换冷却，用完就归还）。

    **查结果要认准建任务那把 Key**：池子里有多把时按「最久未用」取，建任务用 A、
    查结果就会取到 B，而任务挂在 A 的账号下，B 查就是 404「任务不存在」。
    所以带了 ``video_id`` 时先查 :data:`_VIDEO_TASK_KEYS` 里记的那把。
    """
    remembered = _VIDEO_TASK_KEYS.get(video_id or "", "")
    if remembered and llm_store.provider_key_active(model_id, remembered):
        return remembered
    provider_key = llm_store.pick_provider_key(model_id)
    if provider_key is None:
        raise HTTPException(status_code=503, detail=f"模型 {model_id} 暂无可用的供应商密钥")
    llm_store.mark_provider_key_used(provider_key["id"])
    return provider_key["api_key"]


def _video_post(url: str, model: dict, body: bytes) -> tuple[int, str, str]:
    """把「创建任务」转发给供应商，被限流就换池里下一把钥匙重试。

    返回 ``(状态码, 响应文本, 真正建任务的那个 Key)`` —— 第三个值要给
    :func:`_remember_task_key` 记下来，轮询查结果时用同一把。

    为什么这里要重试：视频是最容易撞免费档速率限制的一环，之前的做法是直接把这个
    429 抛回客户端 —— 池子里明明还有别的钥匙却没试。现在按 429 / 503 换钥匙重试，
    并把失败的那把放进冷却，和对话 / 生图代理保持一致；真全试完了才把错误带回去
    （客户端仍会据此提示用户）。创建任务不会「部分成功」，重试是安全的。
    """
    last_status, last_text = 0, ""
    for _attempt in range(max(1, PROXY_MAX_KEY_TRIES)):
        provider_key = llm_store.pick_provider_key(model["model_id"])
        if provider_key is None:
            if last_status:
                return last_status, last_text, ""
            raise _no_provider_key_error(model["model_id"])
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {provider_key['api_key']}",
        }
        try:
            status, text = _post_upstream(url, headers, body)
        except urllib.error.HTTPError as exc:
            status, text = exc.code, _read_error(exc)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            llm_store.mark_provider_key_error(
                provider_key["id"], f"网络错误：{exc}", cooldown=True
            )
            last_status, last_text = 502, f"无法连接视频服务：{exc}"
            continue

        if status == 429 or status >= 500:
            llm_store.mark_provider_key_error(
                provider_key["id"], f"HTTP {status} {text[:200]}", cooldown=True
            )
            last_status, last_text = status, text
            continue          # 换下一把钥匙再试
        llm_store.mark_provider_key_used(provider_key["id"])
        return status, text, provider_key["api_key"]

    return last_status or 502, last_text, ""


def _post_upstream(url: str, headers: dict, body: bytes) -> tuple[int, str]:
    """非流式转发，返回 ``(状态码, 响应文本)``。"""
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=PROXY_READ_TIMEOUT) as response:
        return response.status, response.read().decode("utf-8", errors="replace")


def _iter_upstream(url: str, headers: dict, body: bytes) -> Iterator[bytes]:
    """流式转发：把供应商的 SSE 分片原样透传。

    同步生成器交给 FastAPI 时会放进线程池执行，不会阻塞事件循环。
    """
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=PROXY_READ_TIMEOUT) as response:
            while True:
                chunk = response.read(2048)
                if not chunk:
                    break
                yield chunk
    except urllib.error.HTTPError as exc:
        yield _read_error(exc).encode("utf-8")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        yield f"代理失败：{exc}".encode("utf-8")


def _read_error(exc: urllib.error.HTTPError) -> str:
    try:
        return exc.read().decode("utf-8", errors="replace")[:2000]
    except Exception:      # noqa: BLE001 - 读不到正文也要给出状态码
        return f"HTTP {exc.code}"


def _ensure_socket_timeout() -> None:
    """连接超时靠 socket 全局默认值兜底（urllib 没有单独的连接超时参数）。"""
    import socket

    current = socket.getdefaulttimeout()
    if current is None:
        socket.setdefaulttimeout(PROXY_CONNECT_TIMEOUT)


_ensure_socket_timeout()


# ------------------------------------------------------------------ 桌面端取密钥
@router.get("/user-key", response_model=UserKeyOut)
def my_user_key(request: Request, authorization: str | None = Header(default=None)) -> dict:
    """桌面端用登录令牌换取（或自动创建）自己的代理密钥。"""
    from app.routers.auth import current_user

    request_logs.note(request, "取代理密钥")
    user = current_user(_bearer(authorization))
    keys = llm_store.list_user_keys(user["id"])
    active = next(
        (item for item in keys if item["status"] == "active" and not item["expired"]), None
    )
    if active is None:
        row = llm_store.create_user_key(user["id"], label="默认密钥")
        active = llm_store._user_key_out(row)      # noqa: SLF001 - 同模块内复用序列化
        active["nickname"] = user["nickname"]
        active["email"] = user["email"]
    return {
        "key": active["key"],
        "label": active["label"],
        "status": active["status"],
        "expires_at": active["expires_at"],
        "expired": active["expired"],
        "allowed_models": active["allowed_models"],
        "default_ttl_days": USER_KEY_TTL_DAYS,
        "proxy_base_url": "/api/llm/v1",
    }


@router.get("/models", response_model=list[BuiltinModelOut])
def my_models(request: Request, authorization: str | None = Header(default=None)) -> list[dict]:
    """桌面端可见的内置模型（未启用、没配密钥的不下发）。

    名称与备注由看板维护，桌面端拉下来直接显示在模型下拉框里 —— 看板改完，
    客户端下次启动 / 登录就跟着变，不用发新版。``kind`` 告诉桌面端这条是对话
    模型还是文生图模型（后者会把输入当提示词，请求走 ``/v1/images/generations``）。

    只回显显示与分流需要的字段：供应商地址与密钥都在服务端，桌面端拿不到也不需要。
    """
    from app.routers.auth import current_user

    request_logs.note(request, "取内置模型列表")
    current_user(_bearer(authorization))
    return [
        {
            "model_id": item["model_id"],
            "name": item["name"],
            "desc": item["desc"],
            "kind": item["kind"],
        }
        for item in llm_store.list_models()
        if item["enabled"] and item["key_count"]
    ]
