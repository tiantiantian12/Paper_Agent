"""压掉 QtMultimedia 自带 FFmpeg 的日志噪音。

Qt 6 起默认用 FFmpeg 后端解码，它会把自己那些**常规信息**直接打到 stderr：

    Input #0, mov,mp4,m4a,3gp,3g2,mj2, from '...视频-20260922-033106.mp4':
      Duration: 00:00:12.26, bitrate: 2958 kb/s
      Stream #0:0: Video: h264 (High), yuv420p, 1280x720, 24 fps
    [h264_mf @ ...] MFT name: 'H264 Encoder MFT'

在终端或 IDE 里跑应用时，点一下「播放」就刷出一大段，**看着像报错**（实际是正常的，
播放器真正的失败走 ``QMediaPlayer.errorOccurred``，界面上会提示并改用系统播放器）。

这里用 ctypes 把 PySide6 自带的 avutil 日志级别调到「只报错以上」。调不到就算了
（库名变了 / 平台不对 / 后端不是 ffmpeg），绝不影响播放本身。
"""

from __future__ import annotations

import ctypes
import pathlib
import threading

# ffmpeg 的日志级别：AV_LOG_INFO=32 会把上面那段刷出来，调到 ERROR 就安静了
AV_LOG_ERROR = 16

_LOCK = threading.RLock()
_tried = False
_level_set = False


def _avutil_library() -> str:
    """PySide6 自带的 ffmpeg avutil 库路径；找不到返回空串。"""
    import PySide6

    base = pathlib.Path(PySide6.__file__).resolve().parent
    for pattern in ("avutil-*.dll", "libavutil*.so*", "libavutil*.dylib"):
        found = sorted(base.glob(pattern))
        if found:
            return str(found[-1])       # 版本号最大的那个
    return ""


def silence_ffmpeg_logs(level: int = AV_LOG_ERROR) -> bool:
    """静音 ffmpeg 的 info 日志（幂等）；返回是否设置成功。

    必须在**第一次加载媒体之前**调用（``QMediaPlayer.setSource`` 之前），
    所以调用点放在创建播放器那一步。
    """
    global _tried, _level_set
    with _LOCK:
        if _tried:
            return _level_set
        _tried = True
        try:
            path = _avutil_library()
            if not path:
                return False
            ctypes.CDLL(path).av_log_set_level(level)
        except Exception:      # noqa: BLE001 - 压噪音而已，失败当没这回事
            return False
        _level_set = True
        return True
