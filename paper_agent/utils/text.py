"""文本工具：相对时间、摘要、字数统计等。"""

from __future__ import annotations

import re
import time
from datetime import date

_WHITESPACE_RE = re.compile(r"\s+")


def _day_gap(timestamp: float, today: date | None = None) -> int | None:
    """时间戳所在日期距今天几天（按真实日期算，跨年跨月都正确）。

    ``today`` 仅供测试注入；不传就用系统当天。
    """
    try:
        target = date.fromtimestamp(timestamp)
    except (OverflowError, OSError, ValueError):
        return None
    return ((today or date.today()) - target).days


def format_relative_time(timestamp: float) -> str:
    """把时间戳格式化为相对时间（刚刚 / N 分钟前 / 昨天 / 日期）。"""
    delta = time.time() - timestamp
    if delta < 60:
        return "刚刚"
    if delta < 3600:
        return f"{int(delta // 60)} 分钟前"
    if delta < 86400:
        return f"{int(delta // 3600)} 小时前"

    local = time.localtime(timestamp)
    days = _day_gap(timestamp)
    if days == 1:
        return "昨天"
    if local.tm_year == time.localtime().tm_year:
        return time.strftime("%m-%d", local)
    return time.strftime("%Y-%m-%d", local)


def format_datetime(timestamp: float, with_year: bool = True) -> str:
    """把时间戳格式化为「年-月-日 时:分」（时间戳为 0 时返回空串）。"""
    if not timestamp:
        return ""
    fmt = "%Y-%m-%d %H:%M" if with_year else "%m-%d %H:%M"
    return time.strftime(fmt, time.localtime(timestamp))


def time_group(timestamp: float) -> str:
    """会话列表分组：今天 / 昨天 / 最近 7 天 / 更早。"""
    days = _day_gap(timestamp)
    if days == 0:
        return "今天"
    if days == 1:
        return "昨天"
    if time.time() - timestamp < 7 * 86400:
        return "最近 7 天"
    return "更早"


def summarize(text: str, limit: int = 80) -> str:
    """折叠空白并截断。"""
    flat = _WHITESPACE_RE.sub(" ", text or "").strip()
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1] + "…"


def count_words(text: str) -> int:
    """统计中英文混排字数：中文按字计，西文按词计。"""
    if not text:
        return 0
    chinese = len(re.findall(r"[\u4e00-\u9fff\u3040-\u30ff]", text))
    western = len(re.findall(r"[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)*", text))
    return chinese + western


def derive_title(text: str, limit: int = 18) -> str:
    """从首条用户消息推导会话标题。"""
    flat = _WHITESPACE_RE.sub(" ", text or "").strip()
    if not flat:
        return "新的论文对话"
    if len(flat) <= limit:
        return flat
    return flat[:limit] + "…"
