"""空会话时的欢迎页与快捷任务。"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from paper_agent.core.config import MODE_CODE, MODE_DOC
from paper_agent.core.signals import signals
from paper_agent.core.theme import theme_manager
from paper_agent.resources.icons import build_icon

THESIS_SUGGESTIONS: list[dict[str, str]] = [
    {"title": "选题与开题", "desc": "分析研究方向，给出可落地的选题", "prompt": "我想写计算机方向的毕业论文，请帮我推荐 3 个可行选题并说明研究价值。"},
    {"title": "论文大纲", "desc": "生成完整的章节结构与写作要点", "prompt": "请为我的论文生成一份完整的章节大纲，包含每章的写作要点。"},
    {"title": "文献综述", "desc": "梳理研究现状、争议与研究空白", "prompt": "请帮我撰写文献综述部分，要求按研究主题分类梳理并指出研究空白。"},
    {"title": "正文写作", "desc": "按章节扩写，论证充分、学术规范", "prompt": "请帮我扩写“研究方法”一章，要求方法描述严谨、可复现。"},
    {"title": "降重与润色", "desc": "降低重复率，提升学术表达", "prompt": "请帮我润色这段内容，降低重复率并保持学术风格。"},
    {"title": "参考文献格式", "desc": "GB/T 7714 等格式校对", "prompt": "请按 GB/T 7714 规范帮我校验并整理参考文献列表。"},
]

# 编程模式：换成工程类引导，否则空会话里全是论文选题，看着像切模式没生效
CODE_SUGGESTIONS: list[dict[str, str]] = [
    {
        "title": "搭一个前端页面",
        "desc": "Vue / React 起项目，用模拟数据先跑起来",
        "prompt": "用 Vue 3 + Element Plus 做一个待办事项管理页面，先用模拟数据，能直接跑起来并启动服务。",
    },
    {
        "title": "写后端接口",
        "desc": "FastAPI / Express 建路由与模型，附测试",
        "prompt": "用 FastAPI 写一个待办事项接口（增删改查 + 数据模型），补上 pytest 测试并跑通。",
    },
    {
        "title": "启动并预览",
        "desc": "装依赖、起 dev server、浏览器打开",
        "prompt": "帮我安装依赖并启动这个项目的开发服务器，用 start_service 起服务并告诉我访问地址。",
    },
    {
        "title": "跑测试",
        "desc": "补单元测试 / 跑现有测试套件",
        "prompt": "为这个项目的核心模块补上 pytest 用例，然后跑测试，把失败的修掉。",
    },
    {
        "title": "排查报错",
        "desc": "复现错误、定位原因并给出修复补丁",
        "prompt": "这个项目的启动报错了，帮我读日志定位原因，给出最小改动的修复补丁并验证。",
    },
    {
        "title": "代码审查",
        "desc": "检查性能、边界与安全，给出重构建议",
        "prompt": "审查这段代码的边界情况、性能与异常处理，给出具体的重构建议和改写后的代码。",
    },
]

SUGGESTIONS = THESIS_SUGGESTIONS       # 向后兼容：默认文档模式

MODE_COPY = {
    MODE_DOC: (
        "今天想写点什么？",
        "上传参考文献、粘贴资料或描述你的选题，我会协助你完成毕业论文的选题、提纲、正文与润色。",
    ),
    MODE_CODE: (
        "想让代码做什么？",
        "描述需求或直接说要改哪个文件：搭工程、写代码、装依赖、跑测试、起服务看效果，都能在工程目录内完成。",
    ),
}


class SuggestionCard(QPushButton):
    """快捷任务卡片。"""

    def __init__(self, data: dict[str, str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("suggestionCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._prompt = data["prompt"]
        self.setMinimumHeight(60)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        # 容器内不再叠加 margins，由 QSS 的 padding 统一控制，避免双向累加
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        self.icon_label = QLabel(self)
        self.icon_label.setFixedSize(18, 18)
        layout.addWidget(self.icon_label, 0, Qt.AlignmentFlag.AlignVCenter)

        text_layout = QVBoxLayout()
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(2)
        title = QLabel(data["title"], self)
        title.setObjectName("suggestionTitle")
        desc = QLabel(data["desc"], self)
        desc.setObjectName("suggestionDesc")
        desc.setWordWrap(True)
        text_layout.addWidget(title)
        text_layout.addWidget(desc)
        layout.addLayout(text_layout, 1)

        self._icon_name = data.get("icon", "sparkles")
        self._refresh_icon()
        signals.theme_changed.connect(self._refresh_icon)

    def prompt(self) -> str:
        return self._prompt

    def _refresh_icon(self, *_args) -> None:
        color = theme_manager.color("text_secondary")
        self.icon_label.setPixmap(build_icon(self._icon_name, color, 36).pixmap(18, 18))


class WelcomeView(QWidget):
    """欢迎页：标题 + 快捷任务网格。"""

    suggestion_selected = Signal(str)

    def __init__(
        self, parent: QWidget | None = None, mode: str = MODE_DOC, user_name: str = ""
    ) -> None:
        super().__init__(parent)
        # Preferred/Preferred：让容器按内容计算高度，避免被 stretch 压缩
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        # 内容期望高度：标题 + 副标题 + 3 行卡片 + 间距 = 约 330 像素
        self.setMinimumHeight(360)
        self._mode = mode if mode in (MODE_DOC, MODE_CODE) else MODE_DOC
        self._user_name = user_name.strip()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 12, 0, 18)
        outer.setSpacing(0)

        outer.addStretch(1)

        center = QHBoxLayout()
        center.addStretch(1)

        column = QWidget(self)
        column.setMaximumWidth(760)
        column.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        layout = QVBoxLayout(column)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)

        # 已登录时在标题上方加一句「欢迎回来，xxx」
        self.greeting_label = QLabel("", column)
        self.greeting_label.setObjectName("welcomeGreeting")
        self.greeting_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.greeting_label.setVisible(False)
        layout.addWidget(self.greeting_label)

        title_text, subtitle_text = MODE_COPY[self._mode]
        self.title_label = QLabel(title_text, column)
        self.title_label.setObjectName("welcomeTitle")
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.title_label)

        self.subtitle_label = QLabel(subtitle_text, column)
        self.subtitle_label.setObjectName("welcomeSubtitle")
        self.subtitle_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.subtitle_label.setWordWrap(True)
        layout.addWidget(self.subtitle_label)

        self.grid = QGridLayout()
        self.grid.setContentsMargins(0, 4, 0, 0)
        self.grid.setSpacing(10)
        layout.addLayout(self.grid)
        self._rebuild_cards()

        center.addWidget(column)
        center.addStretch(1)
        outer.addLayout(center)
        outer.addStretch(1)

        self.set_user(self._user_name)

    # ------------------------------------------------------------------ 账号
    def set_user(self, name: str) -> None:
        """设置欢迎语里的昵称；未登录（空字符串）时整句隐藏。"""
        self._user_name = (name or "").strip()
        self.greeting_label.setText(f"欢迎回来，{self._user_name}" if self._user_name else "")
        self.greeting_label.setVisible(bool(self._user_name))

    # ------------------------------------------------------------------ 模式
    def set_mode(self, mode: str) -> None:
        """按工作模式换一套引导：文档模式说论文，编程模式说工程。"""
        mode = mode if mode in (MODE_DOC, MODE_CODE) else MODE_DOC
        if mode == self._mode:
            return
        self._mode = mode
        title_text, subtitle_text = MODE_COPY[mode]
        self.title_label.setText(title_text)
        self.subtitle_label.setText(subtitle_text)
        self._rebuild_cards()

    def _rebuild_cards(self) -> None:
        suggestions = CODE_SUGGESTIONS if self._mode == MODE_CODE else THESIS_SUGGESTIONS
        while self.grid.count():
            item = self.grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        for index, data in enumerate(suggestions):
            card = SuggestionCard(data, self)
            card.clicked.connect(
                lambda _checked=False, c=card: self.suggestion_selected.emit(c.prompt())
            )
            self.grid.addWidget(card, index // 2, index % 2)

    # 让欢迎页在 chat 区域中真正"居中"
    def sizeHint(self) -> QSize:  # noqa: N802
        hint = super().sizeHint()
        return QSize(hint.width(), max(hint.height(), self.minimumHeight()))
