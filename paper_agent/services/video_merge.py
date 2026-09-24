"""把多段短视频拼成一个连贯的长视频（ffmpeg concat）。

供应商单次只给到 12 秒，要更长的只能「分段生成 → 再拼起来」。分段之间靠首尾帧
衔接（见 :mod:`paper_agent.services.video_long`），这里只负责把已经落盘的片段合成。

为什么必须用 ffmpeg：MP4 不是能直接首尾相接的格式 —— ``moov`` 里记着时长、采样表
与**绝对字节偏移**，把两个 mp4 的字节拼起来得到的是一个坏文件（能播几秒，然后卡死
或花屏）。所以按这个顺序找一个能用的 ffmpeg：

1. 环境变量 ``PAPER_AGENT_FFMPEG`` 指定的可执行文件（用户想用自己那份）；
2. 系统 PATH 里的 ``ffmpeg``；
3. ``imageio-ffmpeg`` 随包带的二进制（``pip install imageio-ffmpeg``，见 requirements）。
4. 都没有 → 抛错，上层降级成「只给分段，不合成」，而不是给一个假的长视频。

默认用 ``-c copy``（不重编码）：几秒完事、画质无损。分段来自同一个模型、编码参数
一致时这就是对的；一旦发现流对不齐（时长对不上、ffmpeg 报了非单调 DTS 之类的警告），
自动退回重编码。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Sequence

FFMPEG_ENV = "PAPER_AGENT_FFMPEG"
MERGE_TIMEOUT = 900              # 单次合成的超时（重编码长片子会慢一些）
DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
VIDEO_STREAM_RE = re.compile(r"Stream #\d+:\d+.*?: Video: ([A-Za-z0-9_]+).*?(\d+)x(\d+)")
AUDIO_STREAM_RE = re.compile(r"Stream #\d+:\d+.*?: Audio: ([A-Za-z0-9_]+).*?(\d+) Hz, ([^,]+)")
# 真正说明「合成结果是坏的」的报错。注意**不要**把 non-monotonic DTS 算进来：
# 段与段之间 AAC 有编码器延迟，ffmpeg 每次都会提示一句并自己把时间戳顺过去，
# 那是正常的（拿它当失败会导致每条长视频都白重编码一遍）。
FATAL_HINTS = (
    "corrupt",
    "could not find codec",
    "not enough frames",
    "invalid data found",
    "error while opening",
    "no such file",
)
MISSING_FFMPEG_HINT = (
    "没找到可用的 ffmpeg，无法合成完整长视频（分段视频已生成，可直接查看）。"
    "装一个即可：pip install imageio-ffmpeg，或把 ffmpeg 放进 PATH / 用 "
    f"{FFMPEG_ENV} 指定可执行文件路径。"
)


class VideoMergeError(RuntimeError):
    """合成失败（缺 ffmpeg、流对不齐且重编码也失败等）。"""


_exe_cache: str = ""
_exe_probed = False


def ffmpeg_exe() -> str:
    """可用的 ffmpeg 可执行文件路径；找不到返回空串（结果缓存）。"""
    global _exe_cache, _exe_probed
    if _exe_probed:
        return _exe_cache
    _exe_probed = True
    for candidate in _candidates():
        if not candidate:
            continue
        if _runs(candidate):
            _exe_cache = candidate
            break
    return _exe_cache


def reset_probe() -> None:
    """清掉探测缓存（测试用）。"""
    global _exe_cache, _exe_probed
    _exe_cache, _exe_probed = "", False


def _candidates() -> list[str]:
    found = [str(os.environ.get(FFMPEG_ENV) or "").strip()]
    found.append(shutil.which("ffmpeg") or "")
    try:      # imageio-ffmpeg 自带一份二进制，没装就当没有
        import imageio_ffmpeg

        found.append(str(imageio_ffmpeg.get_ffmpeg_exe() or ""))
    except Exception:      # noqa: BLE001 - 没装 / 取不到都只是「这个来源不可用」
        pass
    return found


def _runs(exe: str) -> bool:
    if not Path(exe).is_file():
        return False
    try:
        result = run_ffmpeg([exe, "-hide_banner", "-version"], timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def run_ffmpeg(
    args: Sequence[str], timeout: float = MERGE_TIMEOUT
) -> subprocess.CompletedProcess:
    """跑一条 ffmpeg 命令（同步、带超时、Windows 下不弹黑框）。"""
    flags = 0
    if sys.platform == "win32":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)      # 别在用户脸上弹黑框
    return subprocess.run(
        list(args),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=flags,
    )


def inspect(path: str | Path) -> dict[str, Any]:
    """读一个视频的时长与编码参数（没有 ffprobe，就从 ``ffmpeg -i`` 的 stderr 里抠）。

    Returns:
        ``{"duration": float, "signature": tuple}``；读不出来就是 0.0 / 空元组。
    """
    exe = ffmpeg_exe()
    target = Path(path)
    if not exe or not target.is_file():
        return {"duration": 0.0, "signature": ()}
    try:
        result = run_ffmpeg(
            [exe, "-hide_banner", "-nostdin", "-i", str(target)], timeout=60
        )
    except (OSError, subprocess.SubprocessError):
        return {"duration": 0.0, "signature": ()}
    text = result.stderr or ""
    match = DURATION_RE.search(text)
    duration = 0.0
    if match:
        hours, minutes, seconds = match.groups()
        duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    return {"duration": duration, "signature": _signature(text)}


def probe_duration(path: str | Path) -> float:
    """视频时长（秒）；读不出来返回 0。"""
    return float(inspect(path)["duration"])


def _signature(text: str) -> tuple:
    """从 ``ffmpeg -i`` 的输出里抠出「编码参数指纹」，用来判断能不能无损拼接。"""
    video = VIDEO_STREAM_RE.search(text)
    audio = AUDIO_STREAM_RE.search(text)
    return (
        video.group(1).lower() if video else "",
        video.group(2) if video else "",
        video.group(3) if video else "",
        audio.group(1).lower() if audio else "",
        audio.group(2) if audio else "",
        (audio.group(3) or "").strip().lower() if audio else "",
    )


def params_match(paths: Sequence[str | Path]) -> bool:
    """各段的分辨率 / 编码 / 采样率是否一致（一致才能 ``-c copy`` 无损拼）。

    解析不出来时返回 True：那就别瞎猜，交给时长校验兜底。
    """
    signatures = [inspect(item)["signature"] for item in paths]
    first = signatures[0] if signatures else ()
    if not first:
        return True
    return all(item == first for item in signatures)


def merge_videos(
    paths: Sequence[str | Path],
    out_path: str | Path,
    *,
    reencode: bool = False,
) -> dict[str, Any]:
    """把 ``paths`` 按顺序拼成 ``out_path``。

    Args:
        reencode: 强制重编码（默认先试无损 copy，不行再自动重编码）。

    Returns:
        ``{"path", "duration", "reencoded", "bytes"}``

    Raises:
        VideoMergeError: 缺 ffmpeg / 文件缺失 / 两条路线都失败。
    """
    exe = ffmpeg_exe()
    if not exe:
        raise VideoMergeError(MISSING_FFMPEG_HINT)

    files = [Path(str(item)) for item in paths if str(item or "").strip()]
    if len(files) < 2:
        raise VideoMergeError("至少要两段视频才能合成一个长视频")
    for item in files:
        if not item.is_file():
            raise VideoMergeError(f"找不到要合成的视频：{item.name}")

    target = Path(out_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    wanted = sum(probe_duration(item) for item in files)
    # 各段参数不一致时 -c copy 会拼出一个「时长对但画面坏」的文件，那就先各自转码
    same_params = params_match(files)

    workdir = Path(tempfile.mkdtemp(prefix="pa-merge-"))
    try:
        listing = workdir / "list.txt"
        listing.write_text(
            "\n".join(f"file '{_escape(item.as_posix())}'" for item in files),
            encoding="utf-8",
        )
        routes: list[tuple[str, Callable[[], tuple[bool, str]]]] = []
        if same_params and not reencode:
            routes.append(("copy", lambda: _try_concat(exe, listing, target, wanted)))
        routes.append(
            ("reencode", lambda: _try_renormalized(exe, workdir, files, target, wanted))
        )

        reason = ""
        for _mode, attempt in routes:
            target.unlink(missing_ok=True)
            ok, detail = attempt()
            if ok:
                return {
                    "path": str(target),
                    "duration": probe_duration(target),
                    "reencoded": _mode == "reencode",
                    "bytes": target.stat().st_size,
                }
            reason = detail or reason
        target.unlink(missing_ok=True)
        raise VideoMergeError(f"视频合成失败：{reason or 'ffmpeg 没有输出文件'}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _try_concat(exe: str, listing: Path, target: Path, wanted: float) -> tuple[bool, str]:
    """路线一：直接无损拼（各段编码参数一致时又快又好）。"""
    stderr = _concat(exe, listing, target, reencode=False)
    if not target.is_file():
        return False, _tail(stderr)
    got = probe_duration(target)
    problem = _problem(stderr)
    if not problem and _duration_ok(got, wanted):
        return True, ""
    return False, problem or f"合出来的时长对不上（{got:.1f}s，期望约 {wanted:.1f}s）"


def _try_renormalized(
    exe: str, workdir: Path, files: Sequence[Path], target: Path, wanted: float
) -> tuple[bool, str]:
    """路线二：先把各段统一成同样的编码参数，再无损拼。

    走到这里说明各段的参数本来不一致（某段没音轨、分辨率不同、编码不同…）。
    不做这一步直接拼，会得到一个「时长对但画面坏」的文件 —— 那比合成失败更糟。
    """
    normalized = _normalize(exe, workdir, files)
    if len(normalized) != len(files):
        return False, "分段转码失败（格式差异太大）"
    listing = workdir / "norm.txt"
    listing.write_text(
        "\n".join(f"file '{_escape(item.as_posix())}'" for item in normalized),
        encoding="utf-8",
    )
    return _try_concat(exe, listing, target, wanted)


def _normalize(exe: str, workdir: Path, files: Sequence[Path]) -> list[Path]:
    """把每段转成统一的 H.264 + AAC（统一分辨率 / 帧率 / 声道），返回新文件。"""
    width, height = _target_size(files)
    outputs: list[Path] = []
    for index, item in enumerate(files):
        out = workdir / f"norm-{index:02d}.mp4"
        seconds = probe_duration(item) or 0.0
        args = [
            exe, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
            "-i", str(item),
            # 没音轨的段垫一条等长静音，否则各段流布局不同、拼不起来
            "-f", "lavfi", "-t", f"{max(seconds, 0.5):.3f}", "-i", "anullsrc=r=44100:cl=stereo",
            "-map", "0:v:0",
            "-map", "0:a:0" if _has_audio(item) else "1:a:0",
            "-vf",
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1",
            "-r", "24",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
            str(out),
        ]
        try:
            run_ffmpeg(args)
        except (OSError, subprocess.SubprocessError):
            return []
        if not out.is_file() or out.stat().st_size == 0:
            return []
        outputs.append(out)
    return outputs


def _has_audio(path: Path) -> bool:
    return bool(inspect(path)["signature"][3])


def _target_size(files: Sequence[Path]) -> tuple[int, int]:
    """统一到哪一档分辨率：取第一段能解析出来的（取不到就 1280x720）。"""
    for item in files:
        signature = inspect(item)["signature"]
        if signature[1] and signature[2]:
            width, height = int(signature[1]), int(signature[2])
            if width > 0 and height > 0:
                return (width - width % 2, height - height % 2)
    return (1280, 720)


def _escape(name: str) -> str:
    """ffmpeg concat 清单里的单引号文件名要写成 ``'\\''``。"""
    return name.replace("'", "'\\''")


def _concat(exe: str, listing: Path, target: Path, *, reencode: bool) -> str:
    args = [
        exe, "-hide_banner", "-nostdin", "-loglevel", "warning", "-y",
        "-f", "concat", "-safe", "0", "-i", str(listing),
    ]
    args += ["-c", "copy"] if not reencode else [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
    ]
    args += ["-movflags", "+faststart", str(target)]
    try:
        return run_ffmpeg(args).stderr or ""
    except subprocess.TimeoutExpired:
        return "ffmpeg 超时"
    except OSError as exc:
        return f"无法执行 ffmpeg：{exc}"


def _problem(stderr: str) -> str:
    lowered = (stderr or "").lower()
    for hint in FATAL_HINTS:
        if hint in lowered:
            return f"ffmpeg 报了错（{hint}）"
    return ""


def _duration_ok(got: float, wanted: float) -> bool:
    if wanted <= 0:
        return got > 0
    return abs(got - wanted) <= max(0.5, wanted * 0.03)


def _tail(text: str) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return lines[-1][:200] if lines else ""
