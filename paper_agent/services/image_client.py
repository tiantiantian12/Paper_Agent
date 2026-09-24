"""文生图 / 图生图客户端（OpenAI 兼容的 ``/v1/images/generations``）。

只依赖标准库，错误处理风格与 :mod:`paper_agent.services.openai_client` 保持一致。
接口返回 ``b64_json`` 时直接解码落盘到 ``ARTIFACTS_DIR``，
这样生成的图片会自动出现在「我的论文空间」里，与其它产物统一管理。

**各家请求体不一样**，写错字段直接 400（实测 Agnes 不认 ``output_format``、
``n`` 只能为 1），所以按 base_url 认一下口味再拼包：

======== ===========================================================
standard ``model`` / ``prompt`` / ``n`` / ``size`` / ``output_format`` /
         ``response_format`` / ``watermark``（SenseNova 等）
agnes    只要 ``model`` / ``prompt`` / ``size``，返回格式与**参考图**都在
         ``extra_body`` 里：``{"response_format": "url",
         "image": ["<url 或 data: 链接>"]}``，且每次只出 1 张
======== ===========================================================
"""

from __future__ import annotations

import base64
import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

from paper_agent.core.constants import (
    IMAGE_API_BASE_URL,
    IMAGE_DEFAULT_SIZE,
    IMAGE_MODEL_ID,
    IMAGE_SIZE_OPTIONS,
    SERVER_USER_AGENT,
)
from paper_agent.services import session_artifacts

REQUEST_TIMEOUT = 300
MAX_IMAGES = 4
ALLOWED_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
RETRY_DELAYS = (8.0, 20.0)      # 命中 429 后的退避重试间隔（秒）

PROVIDER_AGNES = "agnes"
PROVIDER_STANDARD = "standard"

# 本地图片 → data URI 用的 MIME 表（参考图要内联发走）
IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}

# 生成器签名：提示词 -> [{"name", "path", "kind", "prompt"}, ...]
ImageGenerator = Callable[[str], list[dict[str, Any]]]


def image_provider(base_url: str = "", model_id: str = "") -> str:
    """认一下是哪家的图片接口（决定请求体怎么写）。

    **地址和模型名都要看**：内置模型走的是服务端代理，``base_url`` 是我们自己的
    ``/api/llm/v1`` —— 只看地址会把 Agnes 的模型当成标准口味，发过去的
    ``output_format`` 会被供应商直接 400 打回。
    """
    raw = f"{base_url or ''} {model_id or ''}".lower()
    return PROVIDER_AGNES if "agnes" in raw else PROVIDER_STANDARD


def _is_busy(text: str) -> bool:
    """是否只是「服务忙 / 队列满，稍后再试」（可以退避重试）。

    提示词要具体：「队列」这种词会误伤 —— 供应商拒绝字段时说的是
    「output_format 不是文生图队列支持的字段」，那是**参数错误**，重试多少次都一样。
    """
    lowered = (text or "").lower()
    return any(
        hint in lowered
        for hint in ("429", "503", "队列已满", "繁忙", "稍后重试", "queue is full", "rate limit")
    )


def image_data_url(path: str | Path) -> str:
    """本地图片 → ``data:`` 链接（图生图的参考图必须内联带上）；读不到返回空串。"""
    target = Path(str(path or ""))
    try:
        raw = target.read_bytes()
    except OSError:
        return ""
    mime = IMAGE_MIME.get(target.suffix.lower(), "image/png")
    return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")


class ImageGenerationError(Exception):
    """文生图调用失败。"""


def image_endpoint(base_url: str = "") -> str:
    """推导 ``/images/generations`` 完整地址。"""
    base = (base_url or IMAGE_API_BASE_URL).strip().rstrip("/")
    if base.endswith("/images/generations"):
        return base
    if not base.endswith("/v1"):
        base = f"{base}/v1"
    return f"{base}/images/generations"


def normalize_size(size: str) -> str:
    """把尺寸收敛到接口支持的取值。"""
    value = (size or "").strip().lower()
    return value if value in IMAGE_SIZE_OPTIONS else IMAGE_DEFAULT_SIZE


def _unique_path(name: str) -> Path:
    """在**本会话**的产物目录里取一个不重名的路径（同名自动加 ``-1``）。

    落点由 :mod:`paper_agent.services.session_workspace` 决定：本会话工作区的
    ``generated`` 目录，别的会话的同类文件（命名规则一样）不会混进来。
    """
    return session_artifacts.unique_path(name)


class ImageClient:
    """文生图 / 图生图客户端。

    Args:
        base_url: 形如 ``https://token.sensenova.cn/v1`` 或 ``https://api.agnes-ai.cn/v1``
        api_key: Bearer Token
        model_id: 模型名，如 ``sensenova-u1.5-lite`` / ``agnes-image-2.5-flash``
        timeout: 单次请求超时（秒），出图较慢，默认给足 300 秒
    """

    def __init__(
        self,
        base_url: str = "",
        api_key: str = "",
        model_id: str = "",
        timeout: int = REQUEST_TIMEOUT,
    ) -> None:
        self.base_url = (base_url or IMAGE_API_BASE_URL).strip()
        self.api_key = (api_key or "").strip()
        self.model_id = (model_id or IMAGE_MODEL_ID).strip() or IMAGE_MODEL_ID
        self.timeout = timeout
        # 请求体按供应商口味拼（字段名对不上会被直接打回 400）
        self.provider = image_provider(self.base_url, self.model_id)

    @property
    def available(self) -> bool:
        """是否具备调用条件（至少要配置 API Key）。"""
        return bool(self.api_key)

    # ------------------------------------------------------------------ 生成
    def generate(
        self,
        prompt: str,
        size: str = IMAGE_DEFAULT_SIZE,
        number: int = 1,
        output_format: str = "png",
        watermark: bool = False,
        reference_images: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """按提示词生成图片并落盘；给了参考图就是**图生图**。

        Args:
            watermark: 是否要平台水印。**默认关掉** —— SenseNova 公测期间去水印
                免费，而带水印的图直接交付给用户并不合适（此前默认开着，出的图
                右下角一直挂着平台 logo）。PPT 背景图同样关（见 background_assets）。
            reference_images: 参考图地址（URL 或 ``data:`` 链接）。传了就是图生图：
                按提示词改造这些图（「换成赛博朋克夜景，保持构图」）。
                Agnes 每次只出 1 张，``number > 1`` 时会拆成多次请求。

        Returns:
            ``[{"name", "path", "kind", "prompt"}, ...]``
        """
        text = (prompt or "").strip()
        if not text:
            raise ImageGenerationError("图片提示词为空")
        if not self.available:
            raise ImageGenerationError("未配置文生图 API Key")

        suffix = (output_format or "png").lstrip(".").lower()
        refs = [item for item in (reference_images or []) if item]
        count = max(1, min(int(number or 1), MAX_IMAGES))
        size_value = normalize_size(size)

        results: list[dict[str, Any]] = []
        for payload in self._payloads(text, size_value, count, suffix, watermark, refs):
            data = self._post(payload)
            results.extend(self._collect(data, text, suffix, len(results) + 1))
        if not results:
            raise ImageGenerationError("文生图接口未返回图片数据")
        return results

    # ------------------------------------------------------------------ 请求体
    def _payloads(
        self,
        prompt: str,
        size: str,
        count: int,
        suffix: str,
        watermark: bool,
        references: list[str],
    ) -> list[dict[str, Any]]:
        """按供应商口味生成请求体（Agnes 一次只出一张，要多张就多给几个请求体）。"""
        if self.provider == PROVIDER_AGNES:
            extra: dict[str, Any] = {"response_format": "url"}
            if references:
                # 图生图：官方就是 extra_body.image 里放参考图地址
                extra["image"] = references[:MAX_IMAGES]
            return [
                {"model": self.model_id, "prompt": prompt, "size": size, "extra_body": extra}
                for _ in range(count)
            ]

        payload: dict[str, Any] = {
            "model": self.model_id,
            "prompt": prompt,
            "n": count,
            "size": size,
            "output_format": suffix,
            "response_format": "b64_json",
            "watermark": bool(watermark),
        }
        if references:
            # 标准形态：参考图放顶层 image（部分服务商用这个字段名）
            payload["image"] = references[:MAX_IMAGES]
        return [payload]

    def _collect(
        self, data: dict[str, Any], prompt: str, suffix: str, start_index: int
    ) -> list[dict[str, Any]]:
        """把响应里的图片取出来落盘：优先 base64，其次下载 url。"""
        results: list[dict[str, Any]] = []
        index = start_index
        for item in data.get("data") or []:
            if not isinstance(item, dict):
                continue
            raw = item.get("b64_json") or item.get("image_base64") or ""
            if raw:
                results.append(self._save(raw, prompt, index, suffix))
                index += 1
                continue
            # 有的服务商不认 response_format、只回图片地址（官方示例就是 url 形态）：
            # 把它下回来落盘，行为保持一致
            url = str(item.get("url") or item.get("image_url") or "").strip()
            if url:
                results.append(self._save(self._download(url), prompt, index, suffix))
                index += 1
        return results

    # ------------------------------------------------------------------ 请求
    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        """提交请求；命中限流 / 队列满时等一会儿重试。

        出图比对话更吃配额，而限流是按时间窗恢复的，立刻失败会白白浪费
        一整轮生成（PPT 背景尤其明显），所以这里做两次退避重试。
        除了 429，供应商还会回 ``503 生图队列已满，请稍后重试`` —— 同样是「等会儿再来」，
        照做就行（实测命中过一次，等 8 秒重试就过了）。
        """
        last_error = ""
        for attempt in range(len(RETRY_DELAYS) + 1):
            if attempt:
                time.sleep(RETRY_DELAYS[attempt - 1])
            try:
                return self._post_once(payload)
            except ImageGenerationError as exc:
                last_error = str(exc)
                if not _is_busy(last_error) or attempt >= len(RETRY_DELAYS):
                    raise
        raise ImageGenerationError(last_error or "文生图请求失败")

    def _post_once(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            image_endpoint(self.base_url),
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
                "User-Agent": SERVER_USER_AGENT,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout, context=ssl.create_default_context()
            ) as response:
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raise ImageGenerationError(self._extract_error(exc)) from exc
        except urllib.error.URLError as exc:
            raise ImageGenerationError(
                f"无法连接文生图服务 {image_endpoint(self.base_url)}：{exc.reason}"
            ) from exc
        except (TimeoutError, OSError) as exc:
            raise ImageGenerationError(
                f"文生图请求失败（{image_endpoint(self.base_url)}）：{exc}"
            ) from exc

        try:
            data = json.loads(body)
        except ValueError as exc:
            raise ImageGenerationError("文生图服务返回了无法解析的内容") from exc
        if isinstance(data, dict) and data.get("error"):
            raise ImageGenerationError(self._error_text(data["error"]))
        return data if isinstance(data, dict) else {}

    # ------------------------------------------------------------------ 落盘
    def _save(self, raw: str | bytes, prompt: str, index: int, output_format: str) -> dict[str, Any]:
        """落盘：``raw`` 是 base64 文本就解码，是字节就直接写（url 形态下载回来的）。"""
        if isinstance(raw, bytes):
            binary = raw
        else:
            try:
                binary = base64.b64decode(raw)
            except Exception as exc:      # noqa: BLE001 - 第三方返回内容异常
                raise ImageGenerationError(f"图片数据解码失败：{exc}") from exc

        suffix = f".{output_format}"
        if suffix not in ALLOWED_SUFFIXES:
            suffix = ".png"
        path = _unique_path(f"配图-{time.strftime('%Y%m%d-%H%M%S')}-{index}{suffix}")
        try:
            path.write_bytes(binary)
        except OSError as exc:
            raise ImageGenerationError(f"图片写入失败：{exc}") from exc
        return {"name": path.name, "path": str(path), "kind": "image", "prompt": prompt}

    def _download(self, url: str) -> bytes:
        """把接口返回的图片地址下回来（部分服务商只给 url，不给 base64）。"""
        request = urllib.request.Request(url, headers={"Accept": "image/*"})
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout, context=ssl.create_default_context()
            ) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            raise ImageGenerationError(f"图片下载失败：HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ImageGenerationError(f"图片下载失败：{exc}") from exc

    # ------------------------------------------------------------------ 错误
    @staticmethod
    def _error_text(error: Any) -> str:
        if isinstance(error, dict):
            return str(error.get("message") or json.dumps(error, ensure_ascii=False))
        return str(error)

    @classmethod
    def _extract_error(cls, exc: urllib.error.HTTPError) -> str:
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:      # pragma: no cover - 读取响应体失败
            return f"HTTP {exc.code}"
        try:
            data = json.loads(body)
            if isinstance(data, dict) and data.get("error"):
                return f"HTTP {exc.code} · {cls._error_text(data['error'])}"
        except ValueError:
            pass
        return f"HTTP {exc.code} · {body[:200]}"
