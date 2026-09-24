"""主题系统：颜色 Token、样式表渲染、Markdown 渲染样式。

- 颜色 token 集中定义（浅色 / 暗色两套）
- 全局 QSS 由 ``resources/styles/base.qss`` 模板渲染
- Markdown 渲染样式同样由 token 派生，保证消息内容与界面风格一致
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from string import Template

from PySide6.QtCore import QObject
from PySide6.QtWidgets import QApplication

from paper_agent.core.constants import STYLES_DIR
from paper_agent.core.signals import signals


@dataclass(frozen=True)
class Theme:
    """一套界面的颜色与尺寸 Token。"""

    name: str
    is_dark: bool = False

    # ---------------- 背景 ----------------
    bg_window: str = "#FFFFFF"
    bg_sidebar: str = "#FAFAF9"
    bg_main: str = "#FFFFFF"
    bg_elevated: str = "#FFFFFF"
    bg_subtle: str = "#F4F4F2"
    bg_hover: str = "#EFEFEC"
    bg_hover_strong: str = "#E4E4E0"
    bg_active: str = "#E8E8E4"
    bg_input: str = "#FFFFFF"
    bg_input_focus: str = "#FFFFFF"
    bg_code: str = "#F2F2EE"
    bg_menu: str = "#FFFFFF"
    bg_toast: str = "#FFFFFF"
    user_bubble: str = "#F4F4F2"

    # ---------------- 边框 ----------------
    border: str = "#E5E5E1"
    border_strong: str = "#D3D3CD"
    border_window: str = "#DEDEDA"
    border_focus: str = "#9A9A93"

    # ---------------- 文本 ----------------
    text_primary: str = "#1B1B19"
    text_secondary: str = "#5F5F58"
    text_muted: str = "#8E8E86"
    text_inverse: str = "#FFFFFF"

    # ---------------- 主色 ----------------
    accent: str = "#1B1B19"
    accent_hover: str = "#333330"
    accent_pressed: str = "#0D0D0C"
    accent_soft: str = "#ECECE9"
    accent_text: str = "#FFFFFF"

    # ---------------- 语义色 ----------------
    link: str = "#2F6FEB"
    success: str = "#1A7F4B"
    warning: str = "#B25E09"
    danger: str = "#C0392B"
    selection: str = "#CFE0FF"

    # ---------------- 代码高亮 ----------------
    code_keyword: str = "#A626A4"
    code_string: str = "#1A7F4B"
    code_comment: str = "#8E8E86"
    code_number: str = "#B25E09"
    code_function: str = "#2F6FEB"

    # ---------------- 滚动条 / 窗口按钮 ----------------
    scrollbar: str = "#DCDCD7"
    scrollbar_hover: str = "#C2C2BB"
    win_close_hover: str = "#E81123"
    win_close_fg: str = "#FFFFFF"
    win_close_pressed: str = "#C10E1D"

    # ---------------- 尺寸 ----------------
    radius_window: int = 10
    radius_control: int = 8
    radius_block: int = 12
    font_size_base: int = 13
    font_size_small: int = 12
    font_size_message: int = 14
    font_ui: str = (
        '"Segoe UI", "Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", sans-serif'
    )
    font_mono: str = '"Cascadia Code", "JetBrains Mono", "Consolas", "Microsoft YaHei", monospace'

    def as_dict(self) -> dict[str, object]:
        return {f.name: getattr(self, f.name) for f in fields(self)}


LIGHT_THEME = Theme(name="light", is_dark=False)

DARK_THEME = Theme(
    name="dark",
    is_dark=True,
    bg_window="#1F1F1E",
    bg_sidebar="#171716",
    bg_main="#1D1D1C",
    bg_elevated="#262624",
    bg_subtle="#242422",
    bg_hover="#2C2C29",
    bg_hover_strong="#363632",
    bg_active="#343430",
    bg_input="#262624",
    bg_input_focus="#2A2A27",
    bg_code="#151514",
    bg_menu="#262624",
    bg_toast="#2A2A27",
    user_bubble="#262624",
    border="#2E2E2B",
    border_strong="#3E3E3A",
    border_window="#31312E",
    border_focus="#5A5A54",
    text_primary="#ECECEA",
    text_secondary="#A6A69F",
    text_muted="#7C7C75",
    text_inverse="#171716",
    accent="#ECECEA",
    accent_hover="#FFFFFF",
    accent_pressed="#CFCFCA",
    accent_soft="#2E2E2B",
    accent_text="#171716",
    link="#7AA7FF",
    success="#4BBF7B",
    warning="#D9A03C",
    danger="#E57365",
    selection="#2B4A7A",
    code_keyword="#C792EA",
    code_string="#A5D6A7",
    code_comment="#6E6E67",
    code_number="#E5A03C",
    code_function="#82AAFF",
    scrollbar="#3A3A36",
    scrollbar_hover="#4C4C47",
    win_close_hover="#C42B1C",
    win_close_fg="#FFFFFF",
    win_close_pressed="#A4231A",
)


class ThemeManager(QObject):
    """主题管理器（单例）。"""

    def __init__(self) -> None:
        super().__init__()
        self._theme = LIGHT_THEME
        self._qss_template = (STYLES_DIR / "base.qss").read_text(encoding="utf-8")
        self._stylesheet = ""
        self._markdown_css: dict[str, str] = {}

    # ------------------------------------------------------------ 属性
    @property
    def theme(self) -> Theme:
        return self._theme

    @property
    def is_dark(self) -> bool:
        return self._theme.is_dark

    @property
    def qss(self) -> str:
        return self._stylesheet

    @property
    def markdown_css(self) -> dict[str, str]:
        return self._markdown_css

    def color(self, name: str) -> str:
        """按名称取颜色（不存在时返回主文本色）。"""
        return getattr(self._theme, name, self._theme.text_primary)

    # ------------------------------------------------------------ 行为
    def set_theme(self, name: str) -> None:
        theme = DARK_THEME if name == "dark" else LIGHT_THEME
        if theme.name == self._theme.name and self._stylesheet:
            return
        self._theme = theme
        self._stylesheet = self._render_stylesheet()
        self._markdown_css = self._build_markdown_css()
        signals.theme_changed.emit(theme.name)

    def toggle(self) -> str:
        self.set_theme("light" if self.is_dark else "dark")
        return self._theme.name

    def apply(self, app: QApplication) -> None:
        """把当前样式表安装到应用。"""
        app.setStyleSheet(self._stylesheet)

    # ------------------------------------------------------------ 内部
    def _render_stylesheet(self) -> str:
        tokens = self._theme.as_dict()
        return Template(self._qss_template).safe_substitute(**tokens)

    def _build_markdown_css(self) -> dict[str, str]:
        t = self._theme
        ui_font = t.font_ui
        mono_font = t.font_mono
        # 说明：正文 / 标题 / 加粗不写死颜色，改为继承 QSS 中
        # QTextBrowser#messageText 的 color，这样即便某次没有触发重渲染，
        # 文字颜色也会随主题自动变化，避免深色背景下出现黑色文字。
        return {
            "body": (
                f"font-family:{ui_font}; font-size:{t.font_size_message}px; "
                f"line-height:150%;"
            ),
            "p": "margin:0px 0px 10px 0px;",
            "h1": "font-size:21px; font-weight:600; margin:16px 0px 8px 0px;",
            "h2": "font-size:18px; font-weight:600; margin:14px 0px 8px 0px;",
            "h3": "font-size:16px; font-weight:600; margin:12px 0px 6px 0px;",
            "h4": "font-size:14px; font-weight:600; margin:10px 0px 6px 0px;",
            "strong": "font-weight:600;",
            "em": "font-style:italic;",
            "del": f"color:{t.text_muted};",
            "a": f"color:{t.link}; text-decoration:none;",
            "code_inline": (
                f"font-family:{mono_font}; font-size:{t.font_size_small}px; "
                f"color:{t.text_primary}; background:{t.bg_code}; "
                f"border-radius:4px; padding:1px 4px;"
            ),
            # word-wrap: Qt 对 <pre> 只认 break-word 才肯在长串内部断行，
            # 光有 white-space:pre-wrap 会让长代码行把文档撑宽、右侧被裁掉。
            #
            # 注意：Qt 富文本**不支持块级元素的 border / padding**（写了也会被忽略），
            # 所以代码块的外框与内边距交给外面那层表格（见 code_card / code_cell）。
            "pre": (
                f"font-family:{mono_font}; font-size:{t.font_size_small}px; "
                f"color:{t.text_primary}; background:{t.bg_code}; "
                f"margin:2px 0px 0px 0px; "
                f"white-space:pre-wrap; word-wrap:break-word;"
            ),
            # 代码卡片：表格边框与单元格内边距在 Qt 里是真生效的
            "code_card": (
                f"border:1px solid {t.border}; background:{t.bg_code}; "
                f"margin:8px 0px 10px 0px;"
            ),
            "code_cell": "padding:8px 12px;",
            "code_lang": f"font-family:{ui_font}; font-size:11px; color:{t.text_muted};",
            "copy_link": (
                f"font-family:{ui_font}; font-size:11px; color:{t.text_secondary}; "
                f"text-decoration:none;"
            ),
            "blockquote": (
                f"border-left:3px solid {t.border_strong}; "
                f"padding-left:12px; margin:8px 0px 10px 0px;"
            ),
            # 长串（路径 / URL / 无空格长句）统一允许在串内断行：
            # 否则内容会把文档撑得比视口宽，右侧被静默裁掉
            "li": "margin:2px 0px; word-wrap:break-word;",
            "ul": "margin:6px 0px 10px 0px; padding-left:22px;",
            "ol": "margin:6px 0px 10px 0px; padding-left:24px;",
            "table": (
                f"border:1px solid {t.border}; border-radius:8px; margin:8px 0px 10px 0px; "
                f"background:{t.bg_code};"
            ),
            "th": (
                f"padding:6px 10px; font-weight:600; color:{t.text_primary}; "
                f"border-bottom:1px solid {t.border_strong}; background:{t.bg_subtle}; "
                f"word-wrap:break-word;"
            ),
            # td 显式给前景色：表格背景是写死的，避免文字颜色靠继承时
            # 与背景撞色（例如亮色主题下继承到深色文字）
            "td": (
                f"padding:6px 10px; color:{t.text_primary}; "
                f"border-bottom:1px solid {t.border}; word-wrap:break-word;"
            ),
            "hr": f"margin:12px 0px; background:{t.border}; height:1px; border:none;",
            "img": "border-radius:8px; margin:4px 0px;",
            "keyword": f"color:{t.code_keyword};",
            "string": f"color:{t.code_string};",
            "comment": f"color:{t.code_comment}; font-style:italic;",
            "number": f"color:{t.code_number};",
            "function": f"color:{t.code_function};",
            "muted": f"color:{t.text_muted};",
        }


theme_manager = ThemeManager()
