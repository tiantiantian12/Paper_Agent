"""pytest 共享配置：无头 Qt、包可导入、数据目录隔离。

测试默认不碰用户真实的 ``%APPDATA%/PaperAgent``：需要落盘的用例通过
``artifacts_dir`` / ``attachments_dir`` fixture 把目录指到临时路径。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="session")
def qapp():
    """整个测试会话共用一个 QApplication（Qt 不允许创建多个）。"""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path_factory, monkeypatch):
    """所有用例都用临时配置文件，不写用户真实的 settings.ini。

    界面用例大量构造 ``MainWindow(AppConfig(), ...)``，其中「切会话时同步当前模型」
    「模型删掉后兜底」等路径都会把值写回配置 —— 不隔离的话跑一次测试就把用户本地
    的当前模型改掉了（历史上真发生过：被测用例写成了 gpt-5）。
    """
    from paper_agent.core import config as config_module

    path = tmp_path_factory.mktemp("settings") / "settings.ini"
    monkeypatch.setattr(config_module, "SETTINGS_FILE", path)
    yield path


@pytest.fixture(autouse=True)
def _isolated_workspace(tmp_path_factory, monkeypatch):
    """所有用例的「默认工作区」都指向临时目录。

    界面用例切到编程模式时会给会话建 ``workspace/<会话 id>``；不隔离的话每跑一次
    全量用例就往用户真实的 ``data/workspace`` 里丢几个空目录（实测 +6 个/次），
    日积月累就是上百个孤儿目录。
    """
    from paper_agent.services.skills import code_workspace

    base = tmp_path_factory.mktemp("workspace")
    # 只换「默认目录」：设置里选过的目录（configured_root）保持真实语义，
    # 那个值本身也被 _isolated_settings 隔离在临时配置文件里
    monkeypatch.setattr(code_workspace, "default_root", lambda: base)
    monkeypatch.delenv("PAPERAGENT_CODE_ROOT", raising=False)
    yield base


@pytest.fixture(autouse=True)
def _doc_mode_by_default():
    """默认让测试跑在「文档模式」。

    工作模式是持久化配置（``chat/mode``）：上一条用例把它改成 code，下一条
    构造 MainWindow 的用例就会进入工作区视图，断言随之崩掉。这里统一复位。
    """
    from paper_agent.core.config import MODE_DOC, AppConfig

    config = AppConfig()
    original = config.mode
    config.mode = MODE_DOC
    try:
        yield
    finally:
        config.mode = original


@pytest.fixture
def artifacts_dir(tmp_path, monkeypatch):
    """产物落点：本会话工作区的 ``generated``（拿不到会话上下文时是 ``_shared``）。

    产物不再落在 ``data/artifacts`` —— 落点由 :mod:`session_workspace` 统一决定
    （工作区根已由 ``_isolated_workspace`` 指到临时目录）。``document_skills.ARTIFACTS_DIR``
    现在只用于解析「升级前留下的旧文件」，这里照样隔离它。
    """
    from paper_agent.services import session_workspace
    from paper_agent.services.skills import document_skills

    target = tmp_path / "artifacts"
    target.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(document_skills, "ARTIFACTS_DIR", target)
    return session_workspace.generated_dir(create=True)


@pytest.fixture
def attachments_dir(tmp_path, monkeypatch):
    from paper_agent.services.skills import document_skills

    target = tmp_path / "attachments"
    target.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(document_skills, "ATTACHMENTS_DIR", target)
    return target


@pytest.fixture
def managed_dirs(tmp_path, monkeypatch):
    """把附件目录与产物落点都指向临时目录（utils.files 与 document_skills 同步）。

    返回的第二个值是**产物落点**（工作区 ``generated``），不是旧的 ``artifacts`` 目录：
    用例断言「生成了什么文件」时看的是这里。
    """
    from paper_agent.services import session_workspace
    from paper_agent.services.skills import document_skills
    from paper_agent import utils

    attachments = tmp_path / "attachments"
    artifacts = tmp_path / "artifacts"
    attachments.mkdir(parents=True, exist_ok=True)
    artifacts.mkdir(parents=True, exist_ok=True)
    for module in (utils.files, document_skills):
        monkeypatch.setattr(module, "ATTACHMENTS_DIR", attachments)
        monkeypatch.setattr(module, "ARTIFACTS_DIR", artifacts)
    return attachments, session_workspace.generated_dir(create=True)


@pytest.fixture
def bg_cache(tmp_path, monkeypatch):
    """把背景图缓存目录指向临时目录。"""
    from paper_agent.services import background_assets

    target = tmp_path / "cache"
    target.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(background_assets, "CACHE_DIR", target)
    return target


@pytest.fixture
def fake_run():
    """往 :class:`AgentService` 里塞一个「假任务」，让别的代码以为某会话正在生成。

    比给 ``is_running`` 打桩更接近真实：走的是同一套 ``is_session_running`` /
    ``streaming_message_id``，所以「按会话判断」的逻辑是真的被验到的。
    返回的 ``add(agent, session_id, message_id)`` 会把假任务塞进去并返回它，
    方便断言「停止只停这一个」（``run.worker.aborted`` / ``run.thread.quit_calls``）。
    """

    class _Thread:
        def __init__(self) -> None:
            self.quit_calls = 0
            self.terminated = False

        def isRunning(self) -> bool:      # noqa: N802 - 模仿 Qt
            return True

        def quit(self) -> None:
            self.quit_calls += 1

        def terminate(self) -> None:
            self.terminated = True

        def wait(self, _ms=None) -> bool:
            return True

        def deleteLater(self) -> None:    # noqa: N802 - 模仿 Qt
            pass

    class _Worker:
        def __init__(self) -> None:
            self.aborted = False

        def request_stop(self) -> None:
            self.aborted = True

        def deleteLater(self) -> None:    # noqa: N802 - 模仿 Qt
            pass

    def add(agent, session_id: str, message_id: str = "m-1"):
        from paper_agent.services.agent_service import _Run

        run = _Run(
            message_id=message_id,
            session_id=session_id,
            thread=_Thread(),
            worker=_Worker(),
        )
        agent._runs[message_id] = run      # noqa: SLF001 - 测试要模拟真实任务表
        return run

    return add


@pytest.fixture
def sample_png(qapp, tmp_path):
    """生成一张用于插入 Word 的临时 PNG（Qt 自带能力，不依赖 Pillow）。"""
    from PySide6.QtGui import QColor, QPixmap

    path = tmp_path / "配图.png"
    pixmap = QPixmap(120, 80)
    pixmap.fill(QColor("#3366ff"))
    assert pixmap.save(str(path), "PNG")
    return path
