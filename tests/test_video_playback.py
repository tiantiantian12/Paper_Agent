"""视频内联播放：ffmpeg 日志噪音、播不了时的兜底。

背景：Qt 的 FFmpeg 后端会把容器信息（Input #0 / Stream #0:0 / MFT name …）按
info 级别直接打到 stderr，点一次播放刷一大段，用户以为播放报错了 —— 其实能正常播。
这里盯住两件事：压噪音必须在加载媒体前生效，以及真播不了时要退到系统播放器。
"""

from __future__ import annotations

import pytest


@pytest.fixture
def clean_ffmpeg_state(monkeypatch):
    """把「只压一次」的开关复位，让每个用例都能独立验证。"""
    from paper_agent.utils import ffmpeg_log

    monkeypatch.setattr(ffmpeg_log, "_tried", False)
    monkeypatch.setattr(ffmpeg_log, "_level_set", False)
    yield ffmpeg_log


@pytest.fixture(autouse=True)
def clean_active_card(monkeypatch):
    """「当前在播的卡片」是模块级状态：用例之间不能互相影响。"""
    from paper_agent.ui.chat import video_card

    monkeypatch.setattr(video_card, "_ACTIVE_CARD", None)
    yield


# ---------------------------------------------------------------- 压噪音
def test_silence_sets_error_level(clean_ffmpeg_state, monkeypatch):
    module = clean_ffmpeg_state
    levels: list[int] = []

    class _FakeLib:
        def av_log_set_level(self, level: int) -> None:
            levels.append(level)

    monkeypatch.setattr(module, "_avutil_library", lambda: "C:/fake/avutil-59.dll")
    monkeypatch.setattr(module.ctypes, "CDLL", lambda path: _FakeLib())

    assert module.silence_ffmpeg_logs() is True
    assert levels == [module.AV_LOG_ERROR], "调到 ERROR 才会屏蔽掉那段 Input #0"


def test_silence_is_idempotent(clean_ffmpeg_state, monkeypatch):
    module = clean_ffmpeg_state
    calls: list[str] = []

    class _FakeLib:
        def av_log_set_level(self, level: int) -> None:
            calls.append("set")

    monkeypatch.setattr(module, "_avutil_library", lambda: "avutil-59.dll")
    monkeypatch.setattr(module.ctypes, "CDLL", lambda path: _FakeLib())

    assert module.silence_ffmpeg_logs() is True
    assert module.silence_ffmpeg_logs() is True
    assert calls == ["set"], "重复调用不该反复加载库"


def test_silence_without_library_is_harmless(clean_ffmpeg_state, monkeypatch):
    """非 Windows / 库名变了：静音失败也不能影响播放。"""
    module = clean_ffmpeg_state
    monkeypatch.setattr(module, "_avutil_library", lambda: "")

    assert module.silence_ffmpeg_logs() is False
    assert module.silence_ffmpeg_logs() is False


def test_silence_survives_broken_library(clean_ffmpeg_state, monkeypatch):
    module = clean_ffmpeg_state
    monkeypatch.setattr(module, "_avutil_library", lambda: "avutil-59.dll")

    def boom(path):
        raise OSError("不是有效的 Win32 应用程序")

    monkeypatch.setattr(module.ctypes, "CDLL", boom)

    assert module.silence_ffmpeg_logs() is False


def test_real_library_lookup_does_not_raise():
    """真实环境下找库不报错（找得到就压，找不到就算了）。"""
    from paper_agent.utils.ffmpeg_log import _avutil_library

    assert isinstance(_avutil_library(), str)


# ---------------------------------------------------------------- 卡片行为
def _card(tmp_path, *, with_file: bool = True):
    from paper_agent.ui.chat import video_card as module
    from paper_agent.ui.chat.video_card import VideoCard

    path = tmp_path / "视频-20260922-033106.mp4"
    if with_file:
        path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)
    return module, VideoCard("视频.mp4", str(path))


def test_card_silences_ffmpeg_before_creating_player(qapp, tmp_path, monkeypatch):
    """必须赶在加载媒体之前压噪音（setSource 时就打了）。"""
    module, card = _card(tmp_path)
    calls: list[int] = []
    monkeypatch.setattr(module, "silence_ffmpeg_logs", lambda: calls.append(1) or True)
    monkeypatch.setattr(card, "_open_external", lambda reason="": None)

    card._ensure_player()

    assert calls == [1]


def test_missing_file_reports_instead_of_crashing(qapp, tmp_path, monkeypatch):
    from paper_agent.core.signals import signals

    _, card = _card(tmp_path, with_file=False)
    seen: list[str] = []
    signals.toast_requested.connect(seen.append)

    card._on_play_clicked()      # 不该抛异常

    assert any("不存在" in text for text in seen)


def test_player_error_falls_back_to_system_player(qapp, tmp_path, monkeypatch):
    """本地解码不了：收起播放区、按钮复位、改用系统播放器并提示。"""
    module, card = _card(tmp_path)
    monkeypatch.setattr(module, "silence_ffmpeg_logs", lambda: True)
    opened: list[str] = []
    monkeypatch.setattr(card, "_open_external", lambda reason="": opened.append(reason))

    class _FakePlayer:
        def errorString(self) -> str:      # noqa: N802 - 跟 Qt 的命名保持一致
            return "解码器缺失"

    card.stage.setVisible(True)
    card.play_button.setText("暂停")
    card._player = _FakePlayer()
    card._on_player_error()

    assert card._player is None
    assert card.stage.isHidden()
    assert card.play_button.text() == "播放"
    assert opened == ["解码器缺失"], "退到系统播放器时要带上原因"


# ---------------------------------------------------------------- 节流规矩
class _FakeMediaPlayer:
    """假的 QMediaPlayer：只记状态，不碰真解码。"""

    def __init__(self) -> None:
        from PySide6.QtMultimedia import QMediaPlayer

        self.state = QMediaPlayer.PlaybackState.StoppedState

    def playbackState(self):      # noqa: N802 - 跟 Qt 命名一致
        return self.state

    def play(self) -> None:
        from PySide6.QtMultimedia import QMediaPlayer

        self.state = QMediaPlayer.PlaybackState.PlayingState

    def pause(self) -> None:
        from PySide6.QtMultimedia import QMediaPlayer

        self.state = QMediaPlayer.PlaybackState.PausedState


def test_only_one_video_plays_at_a_time(qapp, tmp_path):
    """同时播多个：每个都在解码，界面必卡。新的开始播就把上一个停掉。"""
    _, first = _card(tmp_path)
    _, second = _card(tmp_path)
    first._player = _FakeMediaPlayer()
    second._player = _FakeMediaPlayer()

    first._on_play_clicked()
    assert first.is_playing()

    second._on_play_clicked()

    assert second.is_playing(), "新点的这个要在播"
    assert not first.is_playing(), "上一个必须停下来"
    assert first.play_button.text() == "播放"


def test_pause_resets_button(qapp, tmp_path):
    _, card = _card(tmp_path)
    card._player = _FakeMediaPlayer()
    card._on_play_clicked()
    assert card.play_button.text() == "暂停"

    card.pause()
    assert card.play_button.text() == "播放"
    assert not card.is_playing()


def test_scrolled_out_of_view_pauses(qapp, tmp_path, monkeypatch):
    """滚出视野就停：QVideoWidget 是原生窗口，一边解码一边滚动最拖界面。"""
    from PySide6.QtCore import QPoint

    _, card = _card(tmp_path)
    card._player = _FakeMediaPlayer()
    card._on_play_clicked()

    monkeypatch.setattr(card, "height", lambda: 240)
    monkeypatch.setattr(card, "mapTo", lambda *_: QPoint(0, -400))    # 滚到上面去了
    card.pause_if_out_of_view(viewport=card)

    assert not card.is_playing()


def test_still_visible_keeps_playing(qapp, tmp_path, monkeypatch):
    from PySide6.QtCore import QPoint

    _, card = _card(tmp_path)
    card._player = _FakeMediaPlayer()
    card._on_play_clicked()

    monkeypatch.setattr(card, "height", lambda: 240)

    class _Viewport:
        def height(self) -> int:
            return 800

    monkeypatch.setattr(card, "mapTo", lambda *_: QPoint(0, 120))     # 还在视野里
    card.pause_if_out_of_view(_Viewport())

    assert card.is_playing(), "看得见就不该乱停"


def test_video_renders_through_graphics_item(qapp, tmp_path):
    """回归：画面必须走 QGraphicsVideoItem，不能用 QVideoWidget。

    QVideoWidget 是原生窗口，由系统直接往屏幕上画、Qt 管不到它 —— 嵌在滚动区里
    会盖住兄弟控件、并在下方留下残影（用户看到的「点播放后下面的内容叠在一起」）。
    """
    from PySide6.QtMultimediaWidgets import QGraphicsVideoItem, QVideoWidget
    from PySide6.QtWidgets import QWidget

    _, card = _card(tmp_path)

    assert isinstance(card.video_item, QGraphicsVideoItem)
    natives = [node for node in card.findChildren(QWidget) if isinstance(node, QVideoWidget)]
    assert not natives, "卡片里不能再有原生视频窗口"


def test_graphics_item_fills_stage(qapp, tmp_path):
    """视频项要跟着播放区尺寸走，否则画面只占一个小角。"""
    _, card = _card(tmp_path)
    card.resize(640, 420)
    card.show()
    qapp.processEvents()

    card._sync_video_geometry()

    assert card.video_item.size() == card.video_view.viewport().size()


def test_scroll_hook_pauses_active_video(qapp, monkeypatch):
    """接线回归：滚动事件要触发「停掉看不见的那个」。"""
    from paper_agent.ui.chat import chat_view as module
    from paper_agent.ui.chat.chat_view import ChatView

    calls: list[object] = []
    monkeypatch.setattr(module, "pause_active_if_out_of_view", calls.append)

    view = ChatView()
    view._on_scrolled(0)

    assert calls, "滚动时要检查在播的视频有没有滚出视野"
