"""OpenAI 兼容接口客户端（仅依赖标准库，支持 SSE 流式输出）。

适用于 OpenAI、DeepSeek、通义千问兼容模式、Moonshot、智谱、本地 vLLM /
Ollama / LM Studio 等所有遵循 ``/chat/completions`` 协议的服务。
"""

from __future__ import annotations

import json
import socket
import ssl
import urllib.error
import urllib.request
from typing import Iterator

from paper_agent.core.constants import SERVER_USER_AGENT
from paper_agent.services.key_pool import looks_rate_limited, shared_pool
from paper_agent.services.reasoning import is_unsupported_error, reasoning_payload

# 深度思考模型可能数分钟不产生任何 token，读超时设得足够长（基本等同不限制），
# 需要提前结束时由用户按 Esc 手动停止。
DEFAULT_TIMEOUT = 86400
CONNECT_TIMEOUT = 15

# 单次回复的输出预算。写长篇论文时，模型会把整篇正文塞进 create_docx 的
# markdown 参数里，预算一旦不够，观察到的现象是「工具调用参数为空 / 不完整」。
DEFAULT_MAX_TOKENS = 32768


class OpenAIError(Exception):
    """接口调用失败。"""


THINKING_FIELDS = (
    "reasoning_content",   # DeepSeek / 阿里云 / 智谱等
    "reasoning",           # 部分兼容服务
    "thinking",            # 部分本地推理框架
    "thought",
)


class _DeltaParser:
    """把不同厂商的推理字段与 ``<think>`` 标签统一解析为推理 / 正文两类。"""

    def __init__(self) -> None:
        self.in_tag = False
        self._pending = ""

    def feed(self, delta: dict) -> list[tuple[str, str]]:
        """返回 ``[(kind, text), ...]``。"""
        result: list[tuple[str, str]] = []

        # 1. 显式的推理字段（优先级最高）
        for field in THINKING_FIELDS:
            value = delta.get(field)
            if isinstance(value, str) and value:
                result.append(("thinking", value))

        # 2. 正文：可能混入 <think>...</think>
        content = delta.get("content")
        if isinstance(content, str) and content:
            result.extend(self._split_content(content))

        return result

    def _split_content(self, text: str) -> list[tuple[str, str]]:
        """按 <think> 标签切分正文；支持跨 chunk 的标签。"""
        chunks: list[tuple[str, str]] = []
        buffer = self._pending + text
        self._pending = ""

        while buffer:
            if self.in_tag:
                end = buffer.find("</think>")
                if end == -1:
                    # 结束标签可能被截断，保留尾部等待下一个 chunk
                    keep = max(0, len(buffer) - 8)
                    chunks.append(("thinking", buffer[:keep]))
                    self._pending = buffer[keep:]
                    return chunks
                if end:
                    chunks.append(("thinking", buffer[:end]))
                self.in_tag = False
                buffer = buffer[end + len("</think>") :]
            else:
                start = buffer.find("<think>")
                if start == -1:
                    keep = max(0, len(buffer) - 7)
                    chunks.append(("content", buffer[:keep]))
                    self._pending = buffer[keep:]
                    return chunks
                if start:
                    chunks.append(("content", buffer[:start]))
                self.in_tag = True
                buffer = buffer[start + len("<think>") :]

        return chunks

    def flush(self) -> list[tuple[str, str]]:
        """流结束时把残留内容按当前状态输出。"""
        if not self._pending:
            return []
        kind = "thinking" if self.in_tag else "content"
        text, self._pending = self._pending, ""
        return [(kind, text)]


def _parse_delta(delta: dict, parser: _DeltaParser) -> list[tuple[str, str]]:
    return parser.feed(delta)


class OpenAICompatClient:
    """最小可用的 OpenAI 兼容客户端。

    Args:
        api_key: 主 Key（保留这个写法以兼容旧调用）
        api_keys: 额外 Key；与主 Key 一起组成请求池轮换使用
    """

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        model_id: str = "",
        timeout: int = DEFAULT_TIMEOUT,
        api_keys: list[str] | None = None,
        reasoning: str = "auto",
        effort: str = "medium",
        model_name: str = "",
    ) -> None:
        self.base_url = (base_url or "").strip().rstrip("/")
        self.model_id = (model_id or "").strip()
        self.timeout = timeout

        keys = [api_key] if (api_key or "").strip() else []
        keys.extend(api_keys or [])
        # 同一个模型复用同一个池，限流冷却才能跨轮次生效
        self.pool = shared_pool(self.base_url, self.model_id, keys)
        self.api_key = self.pool.keys[0] if self.pool.keys else ""
        self._active_key = ""      # 本次请求实际使用的 Key（流式途中报错要用它记账）

        # 推理强度：按服务商对应的字段名发送（auto 认不出就不发）
        self._reasoning = reasoning_payload(
            reasoning, effort, self.model_id, model_name
        )
        self._reasoning_keys = set(self._reasoning)
        self.reasoning_dropped = False   # 服务端不认参数时置位（供界面提示）
        self.last_finish_reason = ""     # 最近一次请求的结束原因（length=被长度截断）
        self._response = None            # 在途响应（供 abort 从别的线程断开）

    # ------------------------------------------------------------------ 地址
    @property
    def endpoint(self) -> str:
        """推导 chat/completions 完整地址。"""
        if not self.base_url:
            raise OpenAIError("Base URL 为空")
        url = self.base_url
        # 只填域名时自动补 /v1（多数兼容服务使用 /v1 前缀）
        path = url.split("://", 1)[-1].split("/", 1)[-1] if "://" in url else ""
        if not path:
            url = f"{url}/v1"
        return f"{url}/chat/completions"

    def unreachable(self, exc: Exception) -> str:
        """连不上服务时的提示语 —— **把地址亮出来**。

        最常见的坑是服务端地址填错（登录页里留着一个早就不用的测试端口，
        或者服务端没起来），只说「无法连接服务」根本看不出问题出在哪。
        """
        reason = getattr(exc, "reason", None) or exc
        text = f"无法连接 {self.endpoint}：{reason}"
        if "/api/llm/" in self.endpoint:
            text += "（内置模型走服务端代理，请确认登录页「服务地址」指向正在运行的 Paper_Agent_Server）"
        return text

    # ------------------------------------------------------------------ 请求
    def _request(self, payload: dict, key: str = "") -> urllib.request.Request:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {key or self.api_key}",
            "Accept": "text/event-stream",
            "User-Agent": SERVER_USER_AGENT,
        }
        return urllib.request.Request(
            self.endpoint, data=data, headers=headers, method="POST"
        )

    @staticmethod
    def _context() -> ssl.SSLContext:
        context = ssl.create_default_context()
        return context

    def _payload(
        self,
        messages: list[dict],
        stream: bool,
        temperature: float,
        max_tokens: int,
    ) -> dict:
        """请求体：基础字段 + 本模型支持的推理参数。"""
        payload = {
            "model": self.model_id,
            "messages": messages,
            "stream": stream,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        payload.update(self._reasoning)
        return payload

    def _open_response(self, payload: dict, timeout: float):
        """发起请求；遇到 429（限流）就换池里的下一个 Key 重试。

        每个 Key 最多尝试一次；全都限流时抛出说明性错误。
        若服务端拒绝推理参数（400 未知字段），撤掉参数重试一次并记住。
        """
        attempts = max(1, self.pool.size)
        limited = 0
        last_error = ""
        tried = 0
        dropped_reasoning = False
        dropped_tokens = False   # 服务端不接受 max_tokens 时撤掉该字段重试一次
        reuse_key = ""          # 撤推理参数重试时复用同一把 Key
        while tried < attempts:
            tried += 1
            key = reuse_key or self.pool.acquire()
            reuse_key = ""
            self._active_key = key
            request = self._request(payload, key)
            try:
                response = urllib.request.urlopen(
                    request, timeout=timeout, context=self._context()
                )
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    self.pool.report_limited(key)
                    limited += 1
                    last_error = self._extract_error(exc)
                    continue
                text = self._extract_error(exc)
                if not dropped_reasoning and self._reasoning and is_unsupported_error(text):
                    # 该服务端不认 reasoning_effort / thinking 这类字段：
                    # 撤掉参数、复用同一把 Key 重试一次，并记住「以后都不发」，
                    # 否则每个请求都要白撞一次 400。
                    dropped_reasoning = True
                    self.reasoning_dropped = True
                    self._reasoning = {}
                    for field in self._reasoning_keys:
                        payload.pop(field, None)
                    tried -= 1
                    reuse_key = key
                    continue
                # 有的服务端对 max_tokens 有更小的上限：撤掉这个字段，
                # 改用服务端默认值重试一次（否则长回答永远被拒）
                if not dropped_tokens and "max_tokens" in (payload or {}) and "max_tokens" in text:
                    dropped_tokens = True
                    payload.pop("max_tokens", None)
                    tried -= 1
                    reuse_key = key
                    continue
                raise OpenAIError(text) from exc
            except urllib.error.URLError as exc:
                raise OpenAIError(self.unreachable(exc)) from exc
            except Exception as exc:      # pragma: no cover - 兜底
                raise OpenAIError(f"请求失败：{exc}") from exc
            self.pool.report_success(key)
            self._response = response
            return response

        if limited:
            raise OpenAIError(
                f"{limited} 个 API Key 都被限流（429），可稍后重试或在模型设置里补充 Key："
                f"{last_error}"
            )
        raise OpenAIError(last_error or "请求失败")

    def abort(self) -> None:
        """断开在途请求：用户按 Esc 时调用，让卡在 socket 读的线程立刻退出。

        跨线程关响应是中断阻塞式读取的常规做法；读取侧会以异常结束，
        `_StreamWorker` 再按「已停止」上报。
        """
        response, self._response = self._response, None
        if response is None:
            return
        try:
            response.close()
        except Exception:      # noqa: BLE001 - 已经断开 / 不支持 close 都无所谓
            pass

    def _stream_error(self, message: str) -> OpenAIError:
        """构造流式错误；若是限流，顺手把当前 Key 记入冷却，下次请求换 Key。

        连接已经建立后才收到的 429（服务端在 SSE 里回错误 / 中途限流）
        不会走上面的重试分支，必须在这里补记，否则重试仍会撞同一把 Key。
        """
        if looks_rate_limited(message):
            self.pool.report_limited(self._active_key)
        return OpenAIError(message)

    def stream_chat(
        self,
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> Iterator[tuple[str, str]]:
        """流式对话。

        Yields:
            ``(kind, text)``，kind 为 ``"thinking"``（推理过程）或
            ``"content"``（正式回答）。
        """
        payload = self._payload(messages, stream=True, temperature=temperature, max_tokens=max_tokens)
        response = self._open_response(payload, self.timeout)

        parser = _DeltaParser()
        try:
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                if line.startswith(":"):  # SSE 注释/心跳
                    continue
                if line.startswith("data:"):
                    line = line[5:].strip()
                if not line or line == "[DONE]":
                    if line == "[DONE]":
                        break
                    continue
                try:
                    chunk = json.loads(line)
                except ValueError:
                    continue
                if isinstance(chunk, dict) and chunk.get("error"):
                    error = chunk["error"]
                    text = str(error.get("message", error)) if isinstance(error, dict) else str(error)
                    raise self._stream_error(text)
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                for kind, text in _parse_delta(delta, parser):
                    yield kind, text
            for kind, text in parser.flush():
                yield kind, text
        finally:
            response.close()

    def stream_events(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float = 0.7,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> Iterator[dict]:
        """流式对话，支持 function calling。

        Yields:
            事件字典：
            - ``{"type": "thinking", "text": str}`` 推理过程
            - ``{"type": "content", "text": str}``  正式回答
            - ``{"type": "tool_args", "index", "name", "text"}`` 工具调用参数的原始分片
              （供界面「边写边看」：长正文是塞在参数里的，不实时吐出来的话，
              参数写完之前界面一片空白）
            - ``{"type": "tool_calls", "calls": [{"id", "name", "arguments"}]}`` 工具调用
        """
        payload = self._payload(messages, stream=True, temperature=temperature, max_tokens=max_tokens)
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        response = self._open_response(payload, self.timeout)

        parser = _DeltaParser()
        calls: dict[int, dict] = {}
        self.last_finish_reason = ""
        try:
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line or line.startswith(":"):
                    continue
                if line.startswith("data:"):
                    line = line[5:].strip()
                if not line or line == "[DONE]":
                    if line == "[DONE]":
                        break
                    continue
                try:
                    chunk = json.loads(line)
                except ValueError:
                    continue
                if isinstance(chunk, dict) and chunk.get("error"):
                    error = chunk["error"]
                    text = str(error.get("message", error)) if isinstance(error, dict) else str(error)
                    raise self._stream_error(text)
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                reason = choices[0].get("finish_reason")
                if reason:
                    self.last_finish_reason = reason
                delta = choices[0].get("delta") or {}

                # 工具调用按 index 增量累积（arguments 是分片字符串）
                for piece in delta.get("tool_calls") or []:
                    index = piece.get("index", 0)
                    entry = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
                    if piece.get("id"):
                        entry["id"] = piece["id"]
                    function = piece.get("function") or {}
                    if function.get("name"):
                        entry["name"] = function["name"]
                    if function.get("arguments"):
                        fragment = function["arguments"]
                        entry["arguments"] += fragment
                        # 原始分片也实时吐出去：长正文就在这串参数里，
                        # 界面要能像打字机一样显示出来
                        yield {
                            "type": "tool_args",
                            "index": index,
                            "name": entry["name"],
                            "text": fragment,
                        }

                if not delta.get("tool_calls"):
                    for kind, text in _parse_delta(delta, parser):
                        yield {"type": kind, "text": text}

            for kind, text in parser.flush():
                yield {"type": kind, "text": text}
            if calls:
                yield {"type": "tool_calls", "calls": [calls[i] for i in sorted(calls)]}
        except (TimeoutError, socket.timeout) as exc:
            raise OpenAIError("流式读取超时，服务在长时间内没有返回数据") from exc
        except urllib.error.URLError as exc:
            raise OpenAIError(f"流式中断：{self.unreachable(exc)}") from exc
        except OSError as exc:                # 连接被对端重置等
            raise OpenAIError(f"流式中断：{self.unreachable(exc)}") from exc
        finally:
            response.close()

    def chat(
        self,
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: int = 64,
    ) -> str:
        """非流式对话（用于连接测试）。"""
        payload = self._payload(
            messages, stream=False, temperature=temperature, max_tokens=max_tokens
        )
        response = self._open_response(payload, CONNECT_TIMEOUT)

        try:
            body = response.read().decode("utf-8", errors="replace")
        finally:
            response.close()

        try:
            data = json.loads(body)
        except ValueError as exc:
            raise OpenAIError(f"响应解析失败：{body[:200]}") from exc
        if isinstance(data, dict) and data.get("error"):
            raise OpenAIError(str(data["error"].get("message", data["error"])))
        choices = data.get("choices") or []
        if not choices:
            raise OpenAIError("服务未返回内容")
        message = choices[0].get("message") or {}
        return (message.get("content") or "").strip()

    def test_connection(self) -> tuple[bool, str]:
        """测试连通性，返回 (是否成功, 说明)。"""
        try:
            text = self.chat([{"role": "user", "content": "ping"}], max_tokens=16)
        except OpenAIError as exc:
            return False, str(exc)
        except Exception as exc:  # pragma: no cover - 兜底
            return False, f"测试失败：{exc}"
        preview = text[:40].replace("\n", " ") if text else "（无返回内容）"
        return True, f"连接成功 · {self.model_id} → {preview}"

    # ------------------------------------------------------------------ 内部
    @staticmethod
    def _extract_error(exc: urllib.error.HTTPError) -> str:
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:  # pragma: no cover
            return f"HTTP {exc.code}"
        try:
            data = json.loads(body)
            if isinstance(data, dict) and data.get("error"):
                error = data["error"]
                if isinstance(error, dict):
                    return f"HTTP {exc.code} · {error.get('message', error)}"
                return f"HTTP {exc.code} · {error}"
        except ValueError:
            pass
        return f"HTTP {exc.code} · {body[:200]}"
