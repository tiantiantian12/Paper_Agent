"""生成视频的内联卡片：点播放就地播，播不了就交给系统默认播放器。

播放本身很吃资源（实测一个 720P 视频约吃掉 15% 单核，起播瞬间还有 100ms 级的
卡顿），而视频画面又必须塞进滚动区里。两条经验都踩过：

- **画面走 QGraphicsVideoItem，不用 QVideoWidget**：后者是原生窗口，由系统直接
  往屏幕上画，Qt 管不到 —— 嵌在滚动区里会盖住兄弟控件、在下方留残影（用户看到
  的「点播放后下面的内容像叠在一起」）。图形视图由 Qt 自己画进后备缓冲，参与
  正常的重绘与裁剪。
- **只保留必要的解码**：同一时间只播一个（点第二个自动暂停前一个）、滚出视野就
  暂停（``pause_active_if_out_of_view``）、播放器惰性创建且随卡片销毁。
"""

from __future__ import annotations

import weakref
from pathlib import Path

from PySide6.QtCore import QPoint, QSizeF, QUrl, Qt, Signal
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QGraphicsVideoItem
from PySide6.QtWidgets import (
    QGraphicsScene,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from paper_agent.core.signals import signals
from paper_agent.core.theme import theme_manager
from paper_agent.resources.icons import build_icon
from paper_agent.ui.widgets.elide_label import ElideLabel
from paper_agent.ui.widgets.icon_button import IconToolButton
from paper_agent.utils.ffmpeg_log import silence_ffmpeg_logs
from paper_agent.utils.files import file_size, human_readable_size
from paper_agent.utils.text import format_datetime

PLAYER_HEIGHT = 240
NAME_MAX_WIDTH = 260
META_MAX_WIDTH = 150

# 当前正在播的那张卡片（弱引用：卡片销毁后自动失效，不用管回收）
_ACTIVE_CARD: "weakref.ref[VideoCard] | None" = None


def active_video_card() -> "VideoCard | None":
    """当前在播的卡片（没有就是 None）。"""
    card = _ACTIVE_CARD() if _ACTIVE_CARD is not None else None
    if card is None or not card.is_playing():
        return None
    return card


def pause_active_if_out_of_view(viewport: QWidget) -> None:
    """把已经滚出视野的播放停掉（滚动时调用）。

    只可能在播的那一张有问题，所以这里不做遍历 —— 每次滚动都扫一遍所有卡片
    反而会把自己拖慢。
    """
    card = active_video_card()
    if card is not None:
        card.pause_if_out_of_view(viewport)


def _pause_other_cards(keep: "VideoCard") -> None:
    """记住新的在播卡片，并把上一张停掉。"""
    global _ACTIVE_CARD
    previous = active_video_card()
    if previous is not None and previous is not keep:
        previous.pause()
    _ACTIVE_CARD = weakref.ref(keep)


class VideoCard(QWidget):
    """消息区里的生成视频：默认只显示信息条，点播放就地展开播放。"""

    open_failed = Signal(str)      # 播放失败时的说明（内部用）

    def __init__(
        self,
        name: str,
        path: str,
        parent: QWidget | None = None,
        *,
        created_at: float = 0.0,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("videoCard")
        self._path = path
        self._created_at = created_at
        self._player: QMediaPlayer | None = None
        self._audio: QAudioOutput | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 6)
        layout.setSpacing(6)

        self.stage = QWidget(self)
        self.stage.setObjectName("videoStage")
        self.stage.setFixedHeight(PLAYER_HEIGHT)
        self.stage.setVisible(False)
        stage_layout = QVBoxLayout(self.stage)
        stage_layout.setContentsMargins(0, 0, 0, 0)

        # 用 QGraphicsVideoItem，不用 QVideoWidget：后者是个**原生窗口**，由系统
        # 直接往屏幕上画，Qt 管不到它 —— 嵌在滚动区里会盖住兄弟控件、滚动或重绘时
        # 在下方留下残影（「点播放后下面的内容像叠在一起」就是这个）。图形视图由
        # Qt 自己画进后备缓冲，和普通控件一样参与重绘与裁剪。
        self.video_view = QGraphicsView(self.stage)
        self.video_view.setObjectName("videoStageView")
        self.video_view.setFrameShape(QGraphicsView.Shape.NoFrame)
        self.video_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.video_view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.video_view.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # 每帧重画整个播放区：局部更新在视频这种连续变化的内容上更容易出残影
        self.video_view.setViewportUpdateMode(
            QGraphicsView.ViewportUpdateMode.FullViewportUpdate
        )
        self.video_view.setBackgroundBrush(QColor("#000000"))     # 比例不合时留黑边
        self.video_scene = QGraphicsScene(self.video_view)
        self.video_item = QGraphicsVideoItem()
        # 视频按比例缩放进播放区，不会拉伸变形
        self.video_item.setAspectRatioMode(Qt.AspectRatioMode.KeepAspectRatio)
        self.video_scene.addItem(self.video_item)
        self.video_view.setScene(self.video_scene)
        stage_layout.addWidget(self.video_view)
        layout.addWidget(self.stage)

        footer = QHBoxLayout()
        footer.setContentsMargins(2, 0, 2, 0)
        footer.setSpacing(6)

        self.play_button = QPushButton("播放", self)
        self.play_button.setObjectName("videoPlay")
        self.play_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.play_button.setIcon(build_icon("play", theme_manager.color("accent_text"), 36))
        self.play_button.clicked.connect(self._on_play_clicked)
        footer.addWidget(self.play_button)

        self.name_label = ElideLabel(
            name or "视频", self,
            mode=Qt.TextElideMode.ElideMiddle, max_width=NAME_MAX_WIDTH,
        )
        self.name_label.setObjectName("chipName")
        footer.addWidget(self.name_label)

        self.meta_label = ElideLabel(self._meta_text(), self, max_width=META_MAX_WIDTH)
        self.meta_label.setObjectName("chipSize")
        footer.addWidget(self.meta_label)
        footer.addStretch(1)

        self.folder_button = IconToolButton(
            "folder", "用系统播放器打开", object_name="messageAction", icon_size=15, parent=self
        )
        self.folder_button.clicked.connect(self._open_in_player)
        footer.addWidget(self.folder_button)
        layout.addLayout(footer)

        signals.theme_changed.connect(self._refresh_icon)

    # ------------------------------------------------------------------ 播放
    def is_playing(self) -> bool:
        return (
            self._player is not None
            and self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        )

    def pause(self) -> None:
        """暂停并复位按钮（别的视频开始播 / 滚出视野 / 窗口收起时调用）。"""
        if self._player is not None and self.is_playing():
            self._player.pause()
        if hasattr(self, "play_button"):
            self.play_button.setText("播放")

    def pause_if_out_of_view(self, viewport: QWidget) -> None:
        """滚出可视范围就暂停：没人看的时候不该继续占着 CPU。"""
        if not self.is_playing():
            return
        try:
            top = self.mapTo(viewport, QPoint(0, 0)).y()
            height = self.height()
            visible_height = viewport.height()
        except RuntimeError:      # 控件已经被销毁（切会话 / 删消息）
            return
        if top + height <= 0 or top >= visible_height:
            self.pause()

    def _on_play_clicked(self) -> None:
        self._ensure_player()
        if self._player is None:
            self._open_external()
            return
        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._player.pause()
            self.play_button.setText("继续")
            return
        # 同一时间只播一个：多个播放器一起解码会把界面拖垮
        _pause_other_cards(self)
        self._sync_video_geometry()
        self._player.play()
        self.play_button.setText("暂停")

    def _ensure_player(self) -> None:
        """惰性创建播放器：解码器缺失时直接失败，避免卡在无声无画的状态。"""
        if self._player is not None:
            return
        target = Path(self._path or "")
        if not target.is_file():
            signals.toast_requested.emit("视频文件已不存在")
            return
        # 加载媒体前先把 FFmpeg 后端的 info 日志压掉：那段 Input #0 / MFT name
        # 是常规信息，刷在控制台里容易被当成报错
        silence_ffmpeg_logs()
        try:
            self._player = QMediaPlayer(self)
            self._audio = QAudioOutput(self)
            self._player.setAudioOutput(self._audio)
            self._sync_video_geometry()
            self._player.setVideoOutput(self.video_item)
            self._player.setSource(QUrl.fromLocalFile(str(target)))
        except Exception as exc:      # noqa: BLE001 - 多媒体后端不可用
            self._player = None
            print(f"[video] 播放器不可用：{exc}", flush=True)
            self._open_external()
            return
        self._player.errorOccurred.connect(self._on_player_error)
        self._player.playbackStateChanged.connect(self._on_state_changed)
        self.stage.setVisible(True)
        # 播放区是刚展开的：这时候才有真实尺寸，视频项按它对齐
        self._sync_video_geometry()

    def _on_player_error(self, *_args) -> None:
        """本地解码不了（缺编解码器）：退回系统默认播放器。"""
        message = ""
        if self._player is not None:
            message = self._player.errorString()
        self._player = None
        self.stage.setVisible(False)
        self.play_button.setText("播放")
        self._open_external(message)

    def _on_state_changed(self, state) -> None:
        if state == QMediaPlayer.PlaybackState.PlayingState:
            self.play_button.setText("暂停")
        elif state == QMediaPlayer.PlaybackState.PausedState:
            self.play_button.setText("继续")
        elif state == QMediaPlayer.PlaybackState.StoppedState:
            self.play_button.setText("播放")

    def _open_in_player(self) -> None:
        """显式用系统播放器打开（用户点文件夹图标 / 内置播放器播不了时）。"""
        self._open_external()

    def _open_external(self, reason: str = "") -> None:
        target = Path(self._path or "")
        if not target.is_file():
            signals.toast_requested.emit("视频文件已不存在")
            return
        opened = QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))
        if not opened:
            signals.toast_requested.emit("打不开视频，请到产物目录里手动打开")
        elif reason:
            # 把原因也带上：只说「播不了」的话，下次出问题还是没法定位
            signals.toast_requested.emit(
                f"内置播放器播不了（{reason[:60]}），已改用系统播放器打开"
            )

    # ------------------------------------------------------------------ 尺寸
    def _sync_video_geometry(self) -> None:
        """把视频项铺满播放区（比列不合时上下 / 左右留黑边）。"""
        if not hasattr(self, "video_item"):
            return
        size = self.video_view.viewport().size()
        self.video_item.setSize(QSizeF(size))
        self.video_scene.setSceneRect(0, 0, size.width(), size.height())

    def resizeEvent(self, event) -> None:      # noqa: N802
        super().resizeEvent(event)
        self._sync_video_geometry()

    def showEvent(self, event) -> None:        # noqa: N802
        super().showEvent(event)
        self._sync_video_geometry()

    # ------------------------------------------------------------------ 其它
    def _meta_text(self) -> str:
        target = Path(self._path or "")
        if not target.is_file():
            return "文件已不存在"
        size = human_readable_size(file_size(str(target)))
        stamp = format_datetime(self._created_at) if self._created_at else ""
        return " · ".join(item for item in (size, stamp) if item)

    def _refresh_icon(self, *_args) -> None:
        self.play_button.setIcon(build_icon("play", theme_manager.color("accent_text"), 36))

    def mouseDoubleClickEvent(self, event) -> None:      # noqa: N802
        self._on_play_clicked()
        super().mouseDoubleClickEvent(event)
