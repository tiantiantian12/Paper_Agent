"""API Key 请求池：同一模型配置多个 Key 轮换使用，降低触发限流（429）的概率。

- 轮询取 Key，让请求尽量分散到不同 Key 上
- 某个 Key 被限流后进入冷却期，期间不再被选中；调用成功即解除冷却
- 所有 Key 都在冷却时，退而选最早恢复的那个（比直接失败更划算）
- 池按「Base URL + 模型」复用，冷却状态能跨多轮对话保留
"""

from __future__ import annotations

import re
import threading
import time
from typing import Callable, Iterable

DEFAULT_COOLDOWN = 60.0        # 命中 429 后该 Key 的冷却秒数
KEY_SEPARATORS = re.compile(r"[\s,;，；、]+")
# 各服务商对限流的措辞差异很大，这里尽量兜住常见写法
RATE_LIMIT_MARKERS = (
    "429", "rate limit", "rate_limit", "ratelimit", "too many requests",
    "tpm", "rpm", "quota", "exceed", "throttl",
    "限流", "频繁", "额度", "并发",
)


def looks_rate_limited(text: str) -> bool:
    """错误信息是否属于「限流 / 超额」——这类错误换 Key 或等一会儿重试才有意义。"""
    lowered = (text or "").lower()
    return any(marker in lowered for marker in RATE_LIMIT_MARKERS)


def parse_api_keys(text: str) -> list[str]:
    """把粘贴的多行 / 逗号分隔文本解析成去重的 Key 列表（保持输入顺序）。"""
    keys: list[str] = []
    for item in KEY_SEPARATORS.split(text or ""):
        key = item.strip()
        if key and key not in keys:
            keys.append(key)
    return keys


def mask_key(key: str) -> str:
    """打码展示：只留头尾，避免密钥明文出现在界面或日志里。"""
    text = (key or "").strip()
    if not text:
        return ""
    if len(text) <= 8:
        return "*" * len(text)
    return f"{text[:4]}{'*' * 6}{text[-4:]}"


class ApiKeyPool:
    """可轮换的 Key 池（线程安全）。

    Args:
        keys: Key 列表（自动去重、忽略空值）
        cooldown: 命中限流后的冷却秒数
        clock: 计时函数，默认单调时钟；测试可注入假时钟
    """

    def __init__(
        self,
        keys: Iterable[str],
        cooldown: float = DEFAULT_COOLDOWN,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        cleaned: list[str] = []
        for key in keys or []:
            text = (key or "").strip()
            if text and text not in cleaned:
                cleaned.append(text)
        self._keys = tuple(cleaned)
        self._cooldown = float(cooldown)
        self._clock = clock
        self._blocked_until: dict[str, float] = {}
        self._cursor = 0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ 信息
    @property
    def keys(self) -> tuple[str, ...]:
        return self._keys

    def __len__(self) -> int:
        return len(self._keys)

    @property
    def size(self) -> int:
        return len(self._keys)

    def available_count(self) -> int:
        """当前不在冷却期的 Key 数量。"""
        now = self._clock()
        with self._lock:
            return sum(1 for key in self._keys if self._blocked_until.get(key, 0.0) <= now)

    def snapshot(self) -> list[dict]:
        """池状态（Key 已打码），用于界面提示与排障。"""
        now = self._clock()
        with self._lock:
            state = dict(self._blocked_until)
        return [
            {
                "key": mask_key(key),
                "limited": state.get(key, 0.0) > now,
                "retry_in": max(0.0, state.get(key, 0.0) - now),
            }
            for key in self._keys
        ]

    # ------------------------------------------------------------------ 轮换
    def acquire(self) -> str:
        """取下一个可用 Key（轮询）。全部冷却时返回最早恢复的那个。"""
        with self._lock:
            if not self._keys:
                return ""
            now = self._clock()
            count = len(self._keys)
            for offset in range(count):
                index = (self._cursor + offset) % count
                key = self._keys[index]
                if self._blocked_until.get(key, 0.0) <= now:
                    self._cursor = (index + 1) % count
                    return key
            return min(self._keys, key=lambda item: self._blocked_until.get(item, 0.0))

    def report_limited(self, key: str) -> None:
        """该 Key 被限流：冷却一段时间。"""
        if not key:
            return
        with self._lock:
            self._blocked_until[key] = self._clock() + self._cooldown

    def report_success(self, key: str) -> None:
        """该 Key 调用成功：立刻解除冷却。"""
        with self._lock:
            self._blocked_until.pop(key, None)


_POOLS: dict[tuple[str, str], ApiKeyPool] = {}
_POOLS_LOCK = threading.Lock()


def shared_pool(base_url: str, model_id: str, keys: Iterable[str]) -> ApiKeyPool:
    """按「地址 + 模型」复用同一个池，让限流冷却跨请求生效。"""
    cleaned: list[str] = []
    for key in keys or []:
        text = (key or "").strip()
        if text and text not in cleaned:
            cleaned.append(text)

    token = ((base_url or "").strip(), (model_id or "").strip())
    with _POOLS_LOCK:
        pool = _POOLS.get(token)
        if pool is None or pool.keys != tuple(cleaned):
            pool = ApiKeyPool(cleaned)
            _POOLS[token] = pool
        return pool


def reset_shared_pools() -> None:
    """清空共享池（测试用）。"""
    with _POOLS_LOCK:
        _POOLS.clear()
