"""背景素材获取：本地有就用本地的，没有就联机生成并缓存。

给 PPT 这类需要整页底图的产物用。同一主题只生成一次，之后直接复用缓存，
既省调用也省等待。
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Any, Callable

import functools

from paper_agent.core.constants import CACHE_DIR, IMAGE_BACKGROUND_SIZE

# 「自动生成」的同义写法；命中时提示词只用主题，不追加额外描述
AUTO_HINTS = {"auto", "自动", "自动生成", "自动生成背景", "联网", "联网获取", "生成"}
CACHE_PREFIX = "ppt-bg-"
# 生成参数指纹：换了尺寸/水印策略要让旧缓存失效
GENERATION_PROFILE = "v2-landscape-nowatermark"


def background_prompt(topic: str, description: str = "") -> str:
    """拼背景图提示词：强调可叠字（留白、无文字水印）。"""
    subject = (topic or "").strip() or "学术汇报"
    prompt = (
        f"作为演示文稿（PPT）的整页背景底图：主题是「{subject}」。"
        "整体风格学术、专业、沉稳；大面积留白或大块低对比色面，"
        "重要元素靠边或靠角，不要把主体放在页面正中；"
        "画面中不要出现任何文字、字母、数字、水印、logo 或人脸；"
        "输出干净、可叠加标题与要点文字的抽象背景。"
    )
    extra = (description or "").strip()
    if extra and extra.lower() not in AUTO_HINTS:
        prompt += f" 具体要求：{extra}"
    return prompt


def cache_path(prompt: str) -> Path:
    """按提示词取缓存路径（同一主题复用同一张图）。"""
    digest = hashlib.sha1(f"{GENERATION_PROFILE}\n{prompt}".encode("utf-8")).hexdigest()[:12]
    return CACHE_DIR / f"{CACHE_PREFIX}{digest}.png"


def background_generator_for(client: Any) -> Callable[[str], list[dict[str, Any]]] | None:
    """把文生图客户端包成「背景图专用」生成器：横版 + 无水印。

    平台默认会给图片打水印，而背景图铺满每一页，水印会到处露出来，
    因此这里显式关掉；尺寸取横版，铺 16:9 时裁切损失最小。
    """
    if client is None or not getattr(client, "available", False):
        return None
    return functools.partial(
        client.generate, size=IMAGE_BACKGROUND_SIZE, watermark=False
    )


def is_auto(value: str) -> bool:
    """空值或「自动」类写法都表示让系统自己搞定背景。"""
    return not (value or "").strip() or (value or "").strip().lower() in AUTO_HINTS


def acquire_background(
    generator: Callable[[str], list[dict[str, Any]]] | None,
    topic: str,
    description: str = "",
    *,
    cancel: Any = None,
) -> tuple[str, str]:
    """获取背景图，返回 ``(本地路径, 说明)``；失败时路径为空并给出原因。

    Args:
        generator: 形如 ``ImageClient.generate`` 的文生图可调用对象
        topic: 主题（一般取讲题或文件名）
        description: 画面描述；为「自动」类写法时只用主题
        cancel: 可选取消信号（``threading.Event`` 风格），已取消则不再请求
    """
    prompt = background_prompt(topic, description)
    cached = cache_path(prompt)
    if cached.exists():
        return str(cached), "复用了同主题的背景图缓存"

    if generator is None:
        return "", "未配置文生图模型，已改用主题色底"
    if is_cancelled(cancel):
        return "", "已取消背景图生成"

    items = generator(prompt)
    source = next(
        (str(item.get("path", "")) for item in items or [] if item.get("path")), ""
    )
    if not source or not Path(source).exists():
        return "", "文生图接口未返回可用图片，已改用主题色底"

    # 移进缓存而非复制：避免产物目录里留一张没人引用的图
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(source, cached)
    except OSError:
        try:
            shutil.copyfile(source, cached)
        except OSError as exc:
            return source, f"背景图已生成（缓存失败：{exc}）"
    return str(cached), "已按主题生成背景图"


def is_cancelled(cancel: Any) -> bool:
    if cancel is None:
        return False
    checker = getattr(cancel, "is_set", None)
    try:
        return bool(checker()) if callable(checker) else bool(cancel)
    except Exception:      # noqa: BLE001 - 取消信号异常不该影响主流程
        return False
