"""文生视频客户端（提交任务 + 轮询结果 + 落盘）。

出视频不是「一次请求出结果」，而是两步::

    POST {base_url}/videos                       → {"video_id": "..."}（排队/生成中）
    GET  {base_url}/videos/{id}?model_name=...   → {"status": "completed", ...视频地址...}

出片要几十秒到几分钟，所以这里自带轮询（默认每 2 秒一次，文档建议 1–2 秒）。
拿到视频地址后下载落盘到 ``ARTIFACTS_DIR``，产物会像配图一样直接出现在会话里，
也会收进「我的论文空间」。

供应商返回的字段名各家不完全一致（``video_url`` / ``url`` / ``data.url`` …），
所以解析用「已知键优先 + 递归兜底」，新字段出来不用改代码也能认出来。
"""

from __future__ import annotations

import json
import math
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

from paper_agent.core.constants import SERVER_USER_AGENT
from paper_agent.services import session_artifacts

VIDEO_TIMEOUT = 120              # 创建 / 查询的单次请求超时
# 供应商对「查任务结果」也有限流：2 秒一次会被 429（查询过于频繁），这里放宽到 5 秒
VIDEO_POLL_INTERVAL = 5.0
VIDEO_POLL_MAX_INTERVAL = 20.0   # 连续被限流时的退避上限
PROGRESS_TICK_SECONDS = 1.0      # 进度估算的外推节奏（两次轮询之间每秒推一格）
VIDEO_POLL_TIMEOUT = 900         # 最多等 15 分钟
# 提交被「队列满 / 限流」拒了以后等多久再投（最多重试两次）
SUBMIT_RETRY_DELAYS = (20.0, 45.0)
VIDEO_SIZE = "720P"              # Flash 只支持 720P
ASPECT_OPTIONS = ("16:9", "9:16", "4:3", "1:1", "3:4", "21:9")
SECOND_OPTIONS = ("4", "5", "6", "8", "10", "12")
# 单次请求的上限就是 SECOND_OPTIONS 里最大的那个；再长只能「分段续接 + 合成」，
# 见 paper_agent/services/video_long.py
MAX_SEGMENT_SECONDS = 12
# 一次长视频最多拍这么久：5 段、每段一两分钟，再长用户等不起、额度也顶不住
MAX_TOTAL_SECONDS = 60
DEFAULT_SECONDS = "5"
DEFAULT_ASPECT = "16:9"

DONE_STATES = ("completed", "succeed", "succeeded", "success", "done", "finished")
FAIL_STATES = ("failed", "fail", "error", "cancelled", "canceled", "expired")


class VideoGenerationError(Exception):
    """文生视频调用失败。"""


def _iter_strings(node: Any):
    """深度遍历出所有字符串（找视频地址用）。"""
    if isinstance(node, dict):
        for value in node.values():
            yield from _iter_strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _iter_strings(value)
    elif isinstance(node, str):
        yield node


def pick_video_id(data: Any) -> str:
    """从创建响应里取任务 ID（video_id / id / task_id 都认）。"""
    if not isinstance(data, dict):
        return ""
    for key in ("video_id", "videoId", "id", "task_id", "taskId"):
        value = data.get(key)
        if value:
            return str(value)
    for inner in _walk(data):
        for key in ("video_id", "videoId", "id", "task_id", "taskId"):
            value = inner.get(key)
            if value:
                return str(value)
    return ""


def pick_status(data: Any) -> str:
    """归一化任务状态：``completed`` / ``failed`` / ``pending`` / ``""``。"""
    raw = ""
    if isinstance(data, dict):
        for key in ("status", "state", "task_status", "taskStatus"):
            value = data.get(key)
            if isinstance(value, str) and value:
                raw = value
                break
    if not raw:
        for inner in _walk(data):
            for key in ("status", "state"):
                value = inner.get(key)
                if isinstance(value, str) and value:
                    raw = value
                    break
            if raw:
                break
    lowered = raw.strip().lower()
    if not lowered:
        return ""
    if lowered in DONE_STATES:
        return "completed"
    if lowered in FAIL_STATES:
        return "failed"
    return "pending"


def _as_percent(value: Any) -> int | None:
    """把供应商给的各种进度写法归一成 0–100 的整数；认不出来返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip().rstrip("%"))
        except ValueError:
            return None
    else:
        return None
    if number < 0:
        return None
    # 有的服务商给 0–1 的比例（1.0 = 完成），有的直接给百分数
    if isinstance(value, float) and 0 < number <= 1:
        number *= 100
    return max(0, min(int(round(number)), 100))


PROGRESS_KEYS = (
    "progress", "percent", "percentage", "progress_percent", "progressPercent",
    "progress_rate", "internal_progress",
)


def pick_progress(data: Any) -> int:
    """取任务进度（0–100）；查不到返回 ``-1``。

    字段名各家不一致（比如 ``progress`` / ``percent``），取值一律走「已知键优先 +
    递归兜底」；认不出来也不会报错 —— 界面退回「不确定进度」的样式即可。
    """
    if not isinstance(data, dict):
        return -1
    for key in PROGRESS_KEYS:
        number = _as_percent(data.get(key))
        if number is not None:
            return number
    for inner in _walk(data):
        for key in PROGRESS_KEYS:
            number = _as_percent(inner.get(key))
            if number is not None:
                return number
    return -1


class _ProgressPacer:
    """把供应商那点**粗粒度**进度抹平成「一直在动」的进度。

    实测供应商只在几个档位上跳（常见是 10% 卡一大段时间，然后直接 100%），
    中间几十秒一个数都不给 —— 用户看到的就是「卡在 10% 然后突然完成」。

    这里在两次真实上报之间按时间做**递减外推**：离上次真实值越久估得越靠前，
    但永远不越过 ``CEILING``（真出片才走 100），越接近上限走得越慢，不会
    早早顶到 95% 然后干等。真实值只进不退（供应商偶发回退时不跟着倒退）。
    """

    CEILING = 95          # 出片前的显示上限（100 只留给真完成）
    TAU = 45.0            # 外推时间常数（秒）：越大走得越慢

    def __init__(self, on_progress: Callable[[int], None] | None = None) -> None:
        self._on_progress = on_progress
        self._real = -1           # 供应商给的真实进度（-1 = 还没拿到）
        self._real_at = 0.0
        self._last = -1           # 已经报出去的值（避免重复回调）

    @property
    def known(self) -> bool:
        """是否已经拿到过真实进度。"""
        return self._real >= 0

    def real(self, percent: int) -> None:
        """收到一次真实进度。

        **只有真的往前走了才算新基准**：供应商会在每次轮询里重复回同一个值
        （比如一直回 10%），要是每次都重置外推计时，进度条就永远卡在 10% 附近
        （实测只能爬到 11%）。
        """
        if percent < 0:
            return
        if percent > self._real:
            self._real = percent
            self._real_at = time.monotonic()
        self._emit(self._real)

    def tick(self) -> None:
        """轮询空档里调用：按时间往前推一点。"""
        if not self.known:
            return
        self._emit(self._estimate())

    def _estimate(self) -> int:
        if self._real >= 100:
            return 100
        elapsed = max(0.0, time.monotonic() - self._real_at)
        ceiling = float(self.CEILING)
        value = ceiling - (ceiling - self._real) * math.exp(-elapsed / self.TAU)
        return int(min(value, ceiling))

    def _emit(self, value: int) -> None:
        """只进不退：进度条倒退比不动更让人困惑。"""
        if value < 0 or value <= self._last:
            return
        self._last = value
        if self._on_progress:
            self._on_progress(value)


def pick_video_url(data: Any) -> str:
    """从轮询响应里找视频地址；找不到返回空串。"""
    if not isinstance(data, dict):
        return ""
    preferred = (
        "video_url", "url", "download_url", "result_url", "file_url",
        "video", "output_url", "result",
    )
    for key in preferred:
        value = data.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return value
    # 兜底：任何以 .mp4 结尾的 http(s) 链接
    for text in _iter_strings(data):
        if text.startswith(("http://", "https://")) and ".mp4" in text.split("?")[0]:
            return text
    return ""


def _walk(node: Any):
    """遍历出所有 dict（含嵌套的 data / data[0] 之类）。"""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def snap_seconds(value: Any) -> str:
    """把请求时长收进供应商允许的档位（4/5/6/8/10/12）：**就近上调**。

    **绝不静默回落默认值 5 秒**。真实事故（2026-09-24）：用户要 11 秒，模型也老实传了
    ``seconds="11"``，这里发现「11 不合法」就换成默认的 5 —— 成片 5.18 秒，而结果文案
    还写着「约 11 秒是一整段」。用户连问三次「为什么一直给我 5 秒的」。
    以后遇到档位外的值一律往上取最近一档（7→8、9→10、11→12），片长只多不少；
    超过上限就按上限（更长的片子由 :mod:`paper_agent.services.video_long` 分段）。
    """
    try:
        wanted = float(str(value).strip())
    except (TypeError, ValueError):
        return DEFAULT_SECONDS
    if wanted <= 0:
        return DEFAULT_SECONDS
    for option in SECOND_OPTIONS:      # 升序：4, 5, 6, 8, 10, 12
        if float(option) >= wanted:
            return option
    return SECOND_OPTIONS[-1]


def _unique_path(name: str) -> Path:
    """在**本会话**的产物目录里取一个不重名的路径（同名自动加 ``-1``）。

    落点由 :mod:`paper_agent.services.session_workspace` 决定（本会话工作区的
    ``generated``）—— 视频名字里带时间戳，不同会话很容易撞出同一个名字，
    混在一个平铺目录里就会被互相认错。
    """
    return session_artifacts.unique_path(name)


def new_artifact_path(name: str) -> Path:
    """产物目录里一个不重名的路径（同名自动加 ``-1`` / ``-2``）。

    长视频合成的产物不走 ``generate``，但要落在同一个目录、同一套命名规则里。
    """
    return _unique_path(name)


class VideoClient:
    """文生视频客户端。

    Args:
        base_url: 形如 ``https://apihub.agnes-ai.com/v1``（桌面端是服务端代理地址）
        api_key: Bearer Token
        model_id: 视频模型名，如 ``agnes-video-2.5-flash``
    """

    def __init__(
        self,
        base_url: str = "",
        api_key: str = "",
        model_id: str = "",
        poll_interval: float = VIDEO_POLL_INTERVAL,
        poll_timeout: float = VIDEO_POLL_TIMEOUT,
    ) -> None:
        self.base_url = (base_url or "").strip().rstrip("/")
        self.api_key = (api_key or "").strip()
        self.model_id = (model_id or "").strip()
        self.poll_interval = poll_interval
        self.poll_timeout = poll_timeout

    @property
    def available(self) -> bool:
        return bool(self.api_key and self.model_id)

    # ------------------------------------------------------------------ 生成
    def generate(
        self,
        prompt: str,
        seconds: str = DEFAULT_SECONDS,
        aspect_ratio: str = DEFAULT_ASPECT,
        size: str = VIDEO_SIZE,
        seed: int | None = None,
        images: list[str] | None = None,
        first_frame: str = "",
        last_frame: str = "",
        on_status: Callable[[str], None] | None = None,
        on_progress: Callable[[int], None] | None = None,
        is_stopped: Callable[[], bool] | None = None,
    ) -> list[dict[str, Any]]:
        """提交任务 → 轮询 → 落盘。

        Args:
            images: 参考图片地址（图生视频，``mode: reference``）。本地文件可以先
                转成 ``data:`` 链接传进来。
            first_frame: 首帧图片地址（``mode: keyframe``）。接上一段视频的末帧就能
                让两段接得上、不跳 —— 这是「拍续集」的连贯性来源。
            last_frame: 尾帧图片地址（可选），限定这一段收在哪个画面。
            on_status: 状态回调，用于界面显示「排队中 / 生成中」。
            on_progress: 进度回调（0–100），供应商没给进度就不会被调用；出片要
                一两分钟，界面靠它显示进度条，让用户知道还在动。
            is_stopped: 返回 True 时立刻放弃等待（用户按了 Esc）。

        Returns:
            ``[{"name", "path", "kind", "prompt"}, ...]``
        """
        text = (prompt or "").strip()
        if not text:
            raise VideoGenerationError("视频提示词为空")
        if not self.available:
            raise VideoGenerationError("未配置文生视频模型")

        seconds = snap_seconds(seconds)
        if aspect_ratio not in ASPECT_OPTIONS:
            aspect_ratio = DEFAULT_ASPECT

        payload: dict[str, Any] = {
            "model": self.model_id,
            "prompt": text,
            "seconds": seconds,
            "size": size or VIDEO_SIZE,
            "aspect_ratio": aspect_ratio,
        }
        if seed is not None:
            payload["seed"] = int(seed)

        images = [item for item in (images or []) if item]
        key_first = (first_frame or "").strip()
        key_last = (last_frame or "").strip()
        if key_first or key_last:
            # 首尾帧控制：多段视频要接得上就靠它（下一段的首帧 = 上一段的末帧）。
            #
            # 注意：**首尾帧与参考图不能同时用**。2026-09-23 实测两种写法都被供应商拒：
            #   400 invalid_request「首尾帧素材与参考素材不能同时使用」（param: mode）
            # 所以有首帧时 images 只能丢掉 —— 想用参考图锁人物就只能走 reference 模式，
            # 代价是段与段之间不再逐帧接上（见 README「参考图与首帧互斥」）。
            payload["mode"] = "keyframe"
            if key_first:
                payload["first_frame"] = key_first
            if key_last:
                payload["last_frame"] = key_last
        elif images:
            payload["mode"] = "reference"
            payload["images"] = images[:5]
        else:
            payload["mode"] = "text"

        created = self._submit(payload, on_status=on_status, is_stopped=is_stopped)
        video_id = pick_video_id(created)
        if not video_id:
            raise VideoGenerationError("视频服务没有返回任务 ID")

        info = self._wait(video_id, on_status, on_progress, is_stopped)
        url = pick_video_url(info)
        if not url:
            raise VideoGenerationError(
                "视频已生成，但没有拿到下载地址（状态：" + (pick_status(info) or "?") + "）"
            )
        return [self._save(self._download(url), text)]

    # ------------------------------------------------------------------ 请求
    def _submit(
        self,
        payload: dict[str, Any],
        *,
        on_status: Callable[[str], None] | None = None,
        is_stopped: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """提交任务；被「队列满 / 限流」拒了就**等一会儿再投**。

        为什么要自己重试：供应商的队列是**整个服务**的（实测两把 Key 同一时刻都收到
        ``video_queue_full``，换 Key 没用），只能等它腾出位置。而长视频是一段一段串行
        投的 —— 前几段都出来了、最后一段撞上队列满，整条片子就残了，代价太大。

        只有 429 / 503 这种「忙」才重试（参数错误立刻抛，别白等）；等待期间 Esc 能打断。
        """
        for attempt in range(len(SUBMIT_RETRY_DELAYS) + 1):
            try:
                return self._create(payload)
            except VideoGenerationError as exc:
                if not _looks_busy(str(exc)):
                    raise
                if attempt >= len(SUBMIT_RETRY_DELAYS):
                    raise
                if on_status:
                    on_status("retrying")
                if not _nap(SUBMIT_RETRY_DELAYS[attempt], is_stopped):
                    raise VideoGenerationError("__stopped__")
        raise VideoGenerationError("视频提交失败")      # 走不到这里，兜个底

    def _create(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.base_url}/videos",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
                "User-Agent": SERVER_USER_AGENT,
            },
            method="POST",
        )
        return self._json(request)

    def _status(self, video_id: str) -> dict[str, Any]:
        query = urllib.parse.urlencode({"video_id": video_id, "model_name": self.model_id})
        request = urllib.request.Request(
            f"{self.base_url}/videos/{urllib.parse.quote(video_id)}?{query}",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
                "User-Agent": SERVER_USER_AGENT,
            },
        )
        return self._json(request)

    def _json(self, request: urllib.request.Request) -> dict[str, Any]:
        try:
            with urllib.request.urlopen(
                request, timeout=VIDEO_TIMEOUT, context=ssl.create_default_context()
            ) as response:
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raise VideoGenerationError(_http_detail(exc)) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise VideoGenerationError(
                f"无法连接文生视频服务 {request.full_url}：{exc}"
            ) from exc
        try:
            data = json.loads(body)
        except ValueError as exc:
            raise VideoGenerationError("文生视频服务返回了无法解析的内容") from exc
        return data if isinstance(data, dict) else {"data": data}

    def _wait(
        self,
        video_id: str,
        on_status: Callable[[str], None] | None,
        on_progress: Callable[[int], None] | None,
        is_stopped: Callable[[], bool] | None,
    ) -> dict[str, Any]:
        """轮询到终态（或超时 / 被用户中断）。

        查任务这一步供应商也会限流（429「查询过于频繁」），那不是生成失败 ——
        退避一会儿继续查，只有真的超时才放弃。

        进度交给 :class:`_ProgressPacer`：真实值之间按时间外推，界面上的进度条
        才是一直在走的（供应商只在 10% / 100% 这种档位上跳）。
        """
        deadline = time.monotonic() + self.poll_timeout
        last = ""
        pacer = _ProgressPacer(on_progress)
        interval = self.poll_interval
        while True:
            if is_stopped and is_stopped():
                raise VideoGenerationError("__stopped__")
            try:
                info = self._status(video_id)
            except VideoGenerationError as exc:
                if not _looks_busy(str(exc)) or time.monotonic() > deadline:
                    raise
                interval = min(interval * 2, VIDEO_POLL_MAX_INTERVAL)
                self._sleep(interval, pacer)      # 退避期间进度条也别停
                continue
            interval = self.poll_interval
            state = pick_status(info)
            if state != last:
                last = state
                if on_status:
                    on_status(state)
            pacer.real(pick_progress(info))
            if state == "completed":
                return info
            if state == "failed":
                raise VideoGenerationError(_failure_text(info))
            if time.monotonic() > deadline:
                raise VideoGenerationError("视频生成超时（超过 15 分钟仍未出片）")
            self._sleep(interval, pacer)

    @staticmethod
    def _sleep(seconds: float, pacer: "_ProgressPacer") -> None:
        """睡一段，但每秒推一下估算进度 —— 这一分钟里界面不会像卡死。"""
        deadline = time.monotonic() + max(0.0, seconds)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(PROGRESS_TICK_SECONDS, remaining))
            pacer.tick()

    def _download(self, url: str) -> bytes:
        request = urllib.request.Request(url, headers={"Accept": "video/*,*/*"})
        try:
            with urllib.request.urlopen(
                request, timeout=VIDEO_TIMEOUT, context=ssl.create_default_context()
            ) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            raise VideoGenerationError(f"视频下载失败：HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise VideoGenerationError(f"视频下载失败：{exc}") from exc

    def _save(self, binary: bytes, prompt: str) -> dict[str, Any]:
        if not binary:
            raise VideoGenerationError("视频下载为空")
        path = _unique_path(f"视频-{time.strftime('%Y%m%d-%H%M%S')}.mp4")
        try:
            path.write_bytes(binary)
        except OSError as exc:
            raise VideoGenerationError(f"视频写入失败：{exc}") from exc
        return {"name": path.name, "path": str(path), "kind": "video", "prompt": prompt}


def _http_detail(exc: urllib.error.HTTPError) -> str:
    try:
        body = exc.read().decode("utf-8", errors="replace")
    except Exception:      # pragma: no cover - 读不到正文
        return f"HTTP {exc.code}"
    try:
        data = json.loads(body)
    except ValueError:
        return f"HTTP {exc.code} · {body[:200]}"
    if isinstance(data, dict):
        for key in ("message", "detail", "error", "msg"):
            value = data.get(key)
            if isinstance(value, str) and value:
                if exc.code in (429, 503):
                    return f"服务繁忙（HTTP {exc.code}）：{value} —— 稍后再试"
                return f"HTTP {exc.code} · {value}"
            if isinstance(value, dict) and value.get("message"):
                return f"HTTP {exc.code} · {value['message']}"
    if exc.code in (429, 503):
        return f"服务繁忙（HTTP {exc.code}）：视频队列已满，请稍后再试"
    return f"HTTP {exc.code} · {body[:200]}"


def _nap(seconds: float, is_stopped: Callable[[], bool] | None) -> bool:
    """睡一会儿，中途能被 Esc 叫停；返回 False 表示被叫停了。"""
    deadline = time.monotonic() + max(0.0, float(seconds))
    while True:
        if is_stopped is not None and is_stopped():
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        time.sleep(min(0.5, remaining))


def _looks_busy(text: str) -> bool:
    """是否只是「服务忙 / 查太频繁」（429 / 503）——这类可以退避重试。"""
    lowered = (text or "").lower()
    return any(
        hint in lowered
        for hint in ("429", "503", "过于频繁", "繁忙", "queue", "rate limit", "rate_limit")
    )


def _failure_text(info: dict[str, Any]) -> str:
    for key in ("error", "message", "detail", "fail_reason", "reason"):
        value = info.get(key)
        if isinstance(value, str) and value:
            return f"视频生成失败：{value}"
        if isinstance(value, dict) and value.get("message"):
            return f"视频生成失败：{value['message']}"
    return "视频生成失败"
