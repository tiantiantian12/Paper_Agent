"""从已有视频里取帧（末帧 / 首帧），用于「首尾帧控制」让多段视频接得上。

供应商的 ``mode = keyframe`` 接口要的是图片地址（``first_frame`` / ``last_frame``），
而我们手里只有上一段视频的本地 mp4，所以得先把帧抽出来。

**为什么是 ffmpeg 而不是 QtMultimedia**：这里最早用的是 ``QVideoSink``（当时项目里
没有 ffmpeg）。但它在真实出片上不可靠：为了「异步等帧」要起一条独立线程跑自己的 Qt
事件循环，帧什么时候来只能靠超时兜底 —— 实测在供应商的 mp4 上根本取不到帧，
于是「分段续接」会悄悄退化成「只出第一段」，而调用方还以为自己接上了。
ffmpeg 一条命令几十毫秒就出帧：同步返回、可超时、不碰 GUI 线程，失败也失败得干脆。

抽帧失败一律返回空（``b""`` / ``None`` / ``""``），由调用方决定是报错还是退化成
普通文生视频 —— 不要让「取不到帧」把整轮对话搞崩。
"""

from __future__ import annotations

import base64
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Sequence

from paper_agent.services.video_merge import ffmpeg_exe, probe_duration, run_ffmpeg

DEFAULT_TIMEOUT = 20.0
FRAME_QUALITY = 3                # ffmpeg 的 -q:v：2–5 大致对应 JPEG 90 上下
JPEG_QUALITY = 90                # 自己编码时的质量（encode_image）
# 从结尾往前找的几档位置：正正好 duration 常常取不到帧，先退 150ms，再退 1s
TAIL_SEEKS = ("-0.15", "-1.0")
# 「人物 / 场景基准」的采样位置：开头稍后一点、正中间、接近结尾 ——
# 覆盖到人物与场景就行，多了只是白占请求体（供应商最多收 5 张参考图）
SAMPLE_RATIOS = (0.12, 0.5, 0.88)
# 内联图片的上限：供应商收 data URL 但没必要把 3MB 的原图原样发过去
MAX_INLINE_BYTES = 900_000
MAX_INLINE_WIDTH = 1280
MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".gif": "image/gif",
}


class FrameError(RuntimeError):
    """抽帧失败（文件坏了 / 编解码不可用等）。"""


def missing_ffmpeg_hint() -> str:
    """没装 ffmpeg 时给人看的提示（抽帧、拼接都靠它）。"""
    return (
        "这台机器上没找到 ffmpeg，取不了帧 —— 装一个就好："
        "在**运行本程序的那个 Python 环境**里 `pip install imageio-ffmpeg`"
        "（或把 ffmpeg 放进 PATH）。"
    )


def frame_bytes(
    video_path: str | Path, *, last: bool = True, timeout: float = DEFAULT_TIMEOUT
) -> bytes:
    """抽一帧并编码成 JPEG 字节；取不到返回空 bytes。

    Args:
        last: True 取末帧（续接用），False 取首帧。
    """
    path = Path(str(video_path or ""))
    exe = ffmpeg_exe()
    if not exe or not path.is_file():
        return b""
    workdir = Path(tempfile.mkdtemp(prefix="pa-frame-"))
    try:
        out = workdir / "frame.jpg"
        for seek in (TAIL_SEEKS if last else ("",)):
            args = [exe, "-hide_banner", "-nostdin", "-loglevel", "error", "-y"]
            if seek:
                args += ["-sseof", seek]
            args += [
                "-i", str(path),
                "-frames:v", "1",           # 只出一帧
                "-q:v", str(FRAME_QUALITY),
                str(out),
            ]
            try:
                run_ffmpeg(args, timeout=timeout)
            except (OSError, subprocess.SubprocessError):
                continue
            if out.is_file() and out.stat().st_size:
                return out.read_bytes()
        return b""
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def ratio_frame_bytes(
    video_path: str | Path, ratio: float, timeout: float = DEFAULT_TIMEOUT
) -> bytes:
    """按相对位置（0–1）抽一帧的 JPEG 字节；取不到返回空。

    和「取末帧」不同：末帧只有一个用途（首尾帧衔接），而给模型当**参考**时，
    一帧常常正好是背影/糊的 —— 需要按位置多抽几帧才覆盖得到人物的脸与场景。
    """
    path = Path(str(video_path or ""))
    exe = ffmpeg_exe()
    if not exe or not path.is_file():
        return b""
    duration = probe_duration(path)
    if duration <= 0:
        return frame_bytes(path, last=True, timeout=timeout)
    at = max(0.0, min(duration * max(0.0, min(float(ratio), 1.0)), max(duration - 0.05, 0.0)))
    workdir = Path(tempfile.mkdtemp(prefix="pa-frame-"))
    try:
        out = workdir / "frame.jpg"
        try:
            run_ffmpeg(
                [
                    exe, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
                    "-ss", f"{at:.3f}", "-i", str(path),
                    "-frames:v", "1", "-q:v", str(FRAME_QUALITY), str(out),
                ],
                timeout=timeout,
            )
        except (OSError, subprocess.SubprocessError):
            return b""
        return out.read_bytes() if out.is_file() else b""
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def sample_frames(
    video_path: str | Path,
    ratios: Sequence[float] = SAMPLE_RATIOS,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[str]:
    """从一个视频里抽几帧，返回 ``data:`` 地址列表（当「人物 / 场景基准」用）。

    为什么不是一帧：只有一帧时，那一帧如果是背影 / 远景 / 糊的，模型就拿不到人物信息，
    只能自己编一张脸（实测就是这么「换脸」的）。几帧一起给，人的正脸与服装、以及场景
    的样貌都覆盖得到。
    """
    frames: list[str] = []
    for ratio in ratios:
        raw = ratio_frame_bytes(video_path, ratio, timeout=timeout)
        if raw:
            frames.append(_data_url(raw, "image/jpeg"))
    return frames


def grab_frame(video_path: str | Path, timeout: float = DEFAULT_TIMEOUT):
    """取视频的**最后一帧**并转成 ``QImage``；取不到返回 None。"""
    raw = frame_bytes(video_path, last=True, timeout=timeout)
    if not raw:
        return None
    from PySide6.QtGui import QImage

    image = QImage.fromData(raw, "JPEG")
    return image if not image.isNull() else None


def encode_image(image, fmt: str = "JPEG", quality: int = JPEG_QUALITY) -> bytes:
    """把 ``QImage`` 编码成图片字节（抽帧之外的场合用得上，例如自绘首帧）。"""
    from PySide6.QtCore import QBuffer, QIODevice

    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, fmt, quality)
    return bytes(buffer.data())


def last_frame_bytes(video_path: str | Path, timeout: float = DEFAULT_TIMEOUT) -> bytes:
    """取最后一帧的图片字节（JPEG）；失败返回空 bytes。"""
    return frame_bytes(video_path, last=True, timeout=timeout)


def last_frame_data_url(video_path: str | Path, timeout: float = DEFAULT_TIMEOUT) -> str:
    """取最后一帧并编码成 ``data:image/jpeg;base64,...``（视频接口认这种地址）。

    拿不到帧时返回空串 —— 调用方据此决定是报错还是退化成普通文生视频。
    """
    raw = last_frame_bytes(video_path, timeout)
    return _data_url(raw, "image/jpeg") if raw else ""


def image_data_url(path: str | Path) -> str:
    """把本地图片读成 ``data:`` 地址；太大就先压到能发出去的大小。

    供应商对本地路径是直接回 400 的（「素材必须是公网 http(s) URL 或 Base64，
    不支持本地文件路径」），所以给它的图必须是我们自己内联好的。
    读不出来返回空串。
    """
    target = Path(str(path or ""))
    if not target.is_file():
        return ""
    suffix = target.suffix.lower()
    try:
        raw = target.read_bytes()
    except OSError:
        return ""
    if raw and len(raw) <= MAX_INLINE_BYTES and suffix in MIME_BY_SUFFIX:
        return _data_url(raw, MIME_BY_SUFFIX[suffix])
    packed = _compress_image(target)
    if packed:
        return _data_url(packed, "image/jpeg")
    # 压不动（多数是这台机器没装 ffmpeg）：原样内联总比「传了照片却送不出去」强，
    # 只是请求体会大一些（手机原图可能几 MB）
    if raw and suffix in MIME_BY_SUFFIX:
        return _data_url(raw, MIME_BY_SUFFIX[suffix])
    return ""


def _compress_image(path: Path) -> bytes:
    """用 ffmpeg 把图片转成不太大的 JPEG（超过 1280 宽会等比缩小）。"""
    exe = ffmpeg_exe()
    if not exe:
        return b""
    workdir = Path(tempfile.mkdtemp(prefix="pa-image-"))
    try:
        out = workdir / "packed.jpg"
        try:
            run_ffmpeg(
                [
                    exe, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
                    "-i", str(path),
                    # 逗号在滤镜里要转义，否则会被当成「下一个滤镜」
                    "-vf", rf"scale='min({MAX_INLINE_WIDTH}\,iw)':-2",
                    "-frames:v", "1", "-q:v", str(FRAME_QUALITY),
                    str(out),
                ],
                timeout=30.0,
            )
        except (OSError, subprocess.SubprocessError):
            return b""
        return out.read_bytes() if out.is_file() else b""
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _data_url(raw: bytes, mime: str) -> str:
    if not raw:
        return ""
    return f"data:{mime};base64," + base64.b64encode(raw).decode()
