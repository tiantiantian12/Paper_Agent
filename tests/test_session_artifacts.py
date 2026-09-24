"""会话工作区：上传与生成都在**同一棵树**里，两种模式共用，会话之间不串。

用户报的两个问题：

1. 生成的视频「**串文件**」—— 落盘平铺（实测 200+ 个文件、58 个 mp4），而「按名字
   取文件」会扫整个目录兜底，各会话的命名规则又一样（``视频-20260923-144501.mp4``），
   于是拿到的可能是**别的会话**刚生成的那一份。
2. 编程模式的产物「不在工作区里」—— 产物在 ``data/artifacts``、附件在 ``data/attachments``、
   编程模式的工程在 ``workspace/<会话 id>``，三处各管各的。于是「我的论文空间」里的
   文件和工程目录里的不是同一批，模型在工程目录里找不到刚生成的片子。

现在只有一棵树（见 ``services/session_workspace.py``）：

    workspace/<会话 id>/{upload,generated}

解析只认「本会话清单 + 本会话工作区 + 本会话旧产物目录」，**绝不进别的会话目录**。
"""

from __future__ import annotations

import base64
import time

import pytest

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"photo" * 8
PNG_DATA_URL = "data:image/png;base64," + base64.b64encode(PNG_BYTES).decode()

VIDEO_NAME = "视频-20260923-144501.mp4"


def _wait(qapp, predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


# ---------------------------------------------------------------- 布局
def test_upload_and_generated_share_one_workspace():
    """上传目录与生成目录必须同属一个会话工作区（用户要求的那棵树）。"""
    from paper_agent.services import session_workspace

    root = session_workspace.dir_of("sess-a")
    assert session_workspace.upload_dir("sess-a").parent == root
    assert session_workspace.generated_dir("sess-a").parent == root


def test_code_mode_and_doc_mode_share_the_same_workspace():
    """两种模式共用同一个工作区：编程模式的工程目录就在会话工作区里。

    以前编程模式用 ``workspace/<会话 id>``、产物却在 ``artifacts``，两边对不上。
    现在工程目录是工作区里的 ``generated`` —— 代码与视频 / 文档同处一层。
    """
    from paper_agent.services import session_workspace
    from paper_agent.services.skills.code_workspace import code_root

    workspace = session_workspace.dir_of("28eb046862b0")
    assert code_root("28eb046862b0") == workspace / session_workspace.GENERATED_DIR_NAME


def test_generated_and_upload_are_visible_from_the_code_workspace():
    """编程模式列工程目录时，upload / generated 都在里面 —— 模型才找得到。"""
    from paper_agent.services import session_workspace
    from paper_agent.services.skills.code_workspace import file_index

    session_workspace.generated_dir("sess-a").mkdir(parents=True, exist_ok=True)
    (session_workspace.generated_dir("sess-a") / "视频.mp4").write_bytes(b"v")
    (session_workspace.upload_dir("sess-a") / "底图.png").write_bytes(PNG_BYTES)

    lines, total = file_index(session_workspace.dir_of("sess-a"))

    assert total == 2
    assert any("generated/视频.mp4" in line for line in lines)
    assert any("upload/底图.png" in line for line in lines)


@pytest.mark.parametrize("session_id", ["../逃逸", "a/b", "..", "", "../../外面"])
def test_session_id_cannot_escape_the_workspace_root(session_id):
    """会话 id 直接当目录名用，不能借它跑到工作区外面去。"""
    from paper_agent.services import session_workspace

    root = session_workspace.root()
    folder = session_workspace.dir_of(session_id)

    assert folder == root or root in folder.parents, f"{session_id} 逃出了工作区"
    assert folder.name not in ("..", "")


# ---------------------------------------------------------------- 落盘位置
def test_artifact_lands_in_its_session_dir():
    """视频产物要落在**本会话**工作区的 generated 里，不是所有人共用的目录。"""
    from paper_agent.services import image_client as image_module
    from paper_agent.services import session_artifacts, session_workspace

    with session_artifacts.use("sess-a"):
        path = image_module._unique_path(VIDEO_NAME)
        path.write_bytes(b"A")

    assert path.parent == session_workspace.generated_dir("sess-a")


def test_without_session_it_falls_back_to_a_shared_dir():
    """拿不到会话（脚本 / 测试 / 示例引擎）时也要能写进去，别把文件丢了。"""
    from paper_agent.services import image_client as image_module
    from paper_agent.services import session_artifacts, session_workspace

    assert session_artifacts.current() == ""
    path = image_module._unique_path("配图-1.png")

    assert path.parent == session_workspace.generated_dir()
    assert path.parent.parent.name == session_workspace.SHARED_DIR_NAME


def test_image_and_video_savers_write_into_the_session():
    """客户端的真实落盘入口（``_save``）也要按会话走，别只改了辅助函数。"""
    from pathlib import Path

    from paper_agent.services import image_client as image_module
    from paper_agent.services import session_artifacts, session_workspace
    from paper_agent.services import video_client as video_module

    payload = base64.b64encode(PNG_BYTES).decode()

    with session_artifacts.use("sess-a"):
        image = image_module.ImageClient(base_url="http://x/v1", api_key="k", model_id="m")
        video = video_module.VideoClient(base_url="http://x/v1", api_key="k", model_id="m")
        saved_image = image._save(payload, "一只猫", 1, "png")
        saved_video = video._save(b"mp4-bytes", "一段视频")

    assert Path(saved_image["path"]).parent == session_workspace.generated_dir("sess-a")
    assert Path(saved_video["path"]).parent == session_workspace.generated_dir("sess-a")


# ---------------------------------------------------------------- 不串会话（核心回归）
def test_same_named_videos_do_not_cross_sessions():
    """两个会话都生成了同名视频：各自必须解析到自己那一份。

    这就是用户报的「串文件」——平铺时代两边是同一个路径，谁后写谁覆盖，
    模型拿到的是别人的片子（内容不一样，用户一眼就看出来了）。
    """
    from paper_agent.services import session_artifacts
    from paper_agent.services.skills import video_skills

    with session_artifacts.use("sess-a"):
        first = session_artifacts.unique_path(VIDEO_NAME)
        first.write_bytes("A 的片子".encode())
    with session_artifacts.use("sess-b"):
        second = session_artifacts.unique_path(VIDEO_NAME)
        second.write_bytes("B 的片子".encode())

    assert first != second
    suffixes = (".mp4",)
    with session_artifacts.use("sess-a"):
        got = video_skills.resolve_asset(VIDEO_NAME, [], suffixes)
    with session_artifacts.use("sess-b"):
        got_b = video_skills.resolve_asset(VIDEO_NAME, [], suffixes)

    assert got == str(first) and got_b == str(second)
    # 少带了后缀也照样认得（模型经常漏）
    with session_artifacts.use("sess-a"):
        assert video_skills.resolve_asset("视频-20260923-144501", [], suffixes) == str(first)


def test_resolution_never_lists_another_session_dir():
    """候选清单里不能出现别的会话的文件 —— 出现就迟早被认错。"""
    from paper_agent.services import session_artifacts, session_workspace
    from paper_agent.services.skills import video_skills

    (session_workspace.generated_dir("sess-b") / "别人的片子.mp4").write_bytes(b"B")
    (session_workspace.generated_dir("sess-a") / "我的片子.mp4").write_bytes(b"A")

    with session_artifacts.use("sess-a"):
        names = {
            item.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
            for item in video_skills.session_paths([])
        }

    assert "我的片子.mp4" in names
    assert "别人的片子.mp4" not in names


def test_resolution_sees_uploaded_and_generated_files():
    """上传的（upload）和生成的（generated）都要能被按名字解析到。"""
    from paper_agent.services import session_artifacts, session_workspace

    (session_workspace.upload_dir("sess-a") / "底图.png").write_bytes(PNG_BYTES)
    (session_workspace.generated_dir("sess-a") / "配图-1.png").write_bytes(PNG_BYTES)

    with session_artifacts.use("sess-a"):
        paths = {item.rsplit("\\", 1)[-1].rsplit("/", 1)[-1] for item in session_artifacts.candidates()}

    assert {"底图.png", "配图-1.png"} <= paths


def test_files_from_the_previous_layout_still_resolve(tmp_path):
    """上一版落在 ``artifacts/<会话 id>`` 的产物照旧能解析（重启前生成的那批）。"""
    from paper_agent.services import session_artifacts

    root = tmp_path / "artifacts"
    legacy = root / "sess-a" / "配图-旧.png"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(PNG_BYTES)

    with session_artifacts.use("sess-a"):
        assert session_artifacts.find_named("配图-旧.png", base=root) == legacy


def test_image_reference_resolution_is_scoped():
    """图生图按名字找参考图时，也不能拿别的会话的配图。"""
    from paper_agent.services import session_artifacts, session_workspace
    from paper_agent.services.skills import image_skills

    mine = session_workspace.generated_dir("sess-a") / "配图-20260923-144501-1.png"
    theirs = session_workspace.generated_dir("sess-b") / "配图-20260923-144501-1.png"
    mine.write_bytes(PNG_BYTES)
    theirs.write_bytes(PNG_BYTES + b"B")

    tool = image_skills.GenerateImageTool(None, session_files=[])
    with session_artifacts.use("sess-a"):
        resolved = tool._resolve_reference("配图-20260923-144501-1.png")

    assert resolved == PNG_DATA_URL, "拿到的应该是本会话那张，而不是别人的"


# ---------------------------------------------------------------- 文档
def test_documents_are_written_into_the_session_dir():
    from paper_agent.services import session_artifacts, session_workspace
    from paper_agent.services.skills import document_skills

    with session_artifacts.use("sess-a"):
        target = document_skills._unique_path("毕业论文.docx")

    assert target.parent == session_workspace.generated_dir("sess-a")
    assert target.name == "毕业论文.docx"


def test_same_name_document_is_reused_only_for_the_same_session():
    """同一份文档再存一次要原地覆盖；不是自己的同名文件才让位加 ``-1``。"""
    from paper_agent.services import session_artifacts
    from paper_agent.services.skills import document_skills

    with session_artifacts.use("sess-a"):
        first = document_skills._unique_path("论文.docx", {"x"})
        first.write_bytes(b"v1")
        # 会话清单里认得出这是自己那份 → 覆盖，不堆副本
        again = document_skills._unique_path("论文.docx", {str(first)})
        assert again == first
        # 认不出（清单里没有）→ 让位，别覆盖别人的东西
        stranger = document_skills._unique_path("论文.docx", set())
        assert stranger.name == "论文-1.docx"


def test_create_docx_tool_writes_into_the_session_dir():
    """端到端：真的调一次 create_docx，产物要落在本会话目录里。"""
    from pathlib import Path

    from paper_agent.services import session_artifacts, session_workspace
    from paper_agent.services.skills import document_skills

    with session_artifacts.use("sess-a"):
        result = document_skills.CreateDocxTool().run(
            filename="会议记录.docx", markdown="# 会议记录\n\n排期"
        )

    assert result.success is True
    assert Path(result.artifact_paths[0]).parent == session_workspace.generated_dir("sess-a")


def test_append_finds_legacy_file_in_the_root(tmp_path):
    """续写要打到「同名那份」上：旧位置的那份在会话清单里，也得找得到。

    找不到就会另起一个空文件 —— 模型以为在追加，用户拿到的是半篇论文。
    """
    from paper_agent.services import session_artifacts
    from paper_agent.services.skills import document_skills

    root = tmp_path / "artifacts"
    root.mkdir(parents=True, exist_ok=True)
    legacy = root / "毕业论文.docx"
    legacy.write_bytes(b"old")

    tool = document_skills.CreateDocxTool([{"name": legacy.name, "path": str(legacy)}])
    with session_artifacts.use("sess-a"):
        target = tool._target("毕业论文.docx", ".docx", append=True)

    assert target == legacy


def test_list_artifacts_hides_other_sessions():
    """列举产物时不能把别的会话的东西列进来（模型会当真去找）。"""
    from paper_agent.services import session_artifacts, session_workspace
    from paper_agent.services.skills import document_skills

    (session_workspace.generated_dir("sess-b") / "别人的论文.docx").write_bytes(b"B")

    with session_artifacts.use("sess-a"):
        text = document_skills.ListArtifactsTool().run().content

    assert "别人的论文.docx" not in text


# ---------------------------------------------------------------- 接线：worker
def test_worker_writes_into_its_session_dir(qapp):
    """worker 起跑时要把当前会话设好 —— 工具都在这个线程里跑，设了才生效。"""
    from paper_agent.services import image_client as image_module
    from paper_agent.services import session_artifacts, session_workspace
    from paper_agent.services.agent_service import _StreamWorker

    written: list = []

    def producer(emit) -> None:
        assert session_artifacts.current() == "sess-a", "worker 里读不到当前会话"
        path = image_module._unique_path("配图-1.png")
        path.write_bytes(b"x")
        written.append(path)

    worker = _StreamWorker(producer=producer)
    worker.session_id = "sess-a"
    worker.run()

    assert written and written[0].parent == session_workspace.generated_dir("sess-a")


def test_service_start_hands_the_session_to_the_worker(qapp, monkeypatch):
    """``start(session_id=...)`` 要一路传到 worker，否则产物根本分不了目录。"""
    from paper_agent.services import session_artifacts
    from paper_agent.services.agent_service import AgentService, _StreamWorker

    seen: list[str] = []

    def fake_build(*args, **kwargs):
        return _StreamWorker(producer=lambda emit: seen.append(session_artifacts.current()))

    service = AgentService(max_parallel=1)
    monkeypatch.setattr(service, "_build_worker", fake_build)
    service.start("m-1", [{"role": "user", "content": "做条视频"}], session_id="sess-9")

    assert _wait(qapp, lambda: bool(seen)), "worker 没跑起来"
    assert seen == ["sess-9"]
    assert _wait(qapp, lambda: service.running_count == 0)


def test_engine_tool_output_lands_in_the_session_dir():
    """端到端：引擎真跑一轮工具调用，产物要落在本会话目录里。

    这条最接近真实链路（worker 线程设上下文 → 引擎 → 工具 → 落盘），
    只测辅助函数是抓不到「上下文根本没设上」这类问题的。
    """
    import json
    from pathlib import Path

    from paper_agent.services import session_artifacts, session_workspace
    from paper_agent.services.agents.engine import build_orchestrator

    class _Client:
        """第一轮要求调 create_docx，第二轮收尾。"""

        def __init__(self) -> None:
            self.rounds = [
                [
                    {
                        "type": "tool_calls",
                        "calls": [
                            {
                                "id": "c-1",
                                "name": "create_docx",
                                "arguments": json.dumps(
                                    {"filename": "会议记录.docx", "markdown": "# 记录\n\n排期"},
                                    ensure_ascii=False,
                                ),
                            }
                        ],
                    }
                ],
                [{"type": "content", "text": "写好了"}],
            ]

        def stream_events(self, messages, tools=None, temperature=0.7):
            yield from (self.rounds.pop(0) if self.rounds else [])

        def chat(self, *args, **kwargs):
            return "[]"          # Planner 用；下面关掉了计划

    engine = build_orchestrator(
        client=_Client(),
        messages=[{"role": "user", "content": "写个会议记录"}],
        mode="doc",
    )
    engine._use_plan = False

    events: list[tuple[str, str]] = []
    with session_artifacts.use("sess-a"):
        engine.run(emit=lambda kind, data: events.append((kind, data)))

    paths = [json.loads(data)["path"] for kind, data in events if kind == "artifact"]
    assert paths, "引擎没报出产物，链路没走通"
    assert Path(paths[0]).parent == session_workspace.generated_dir("sess-a")


# ---------------------------------------------------------------- 清理
def test_drop_session_removes_only_empty_dirs(tmp_path):
    """删会话时只收旧产物目录的空壳：里面还有文件（别的对话在引用）就留着。"""
    from paper_agent.services import session_artifacts

    root = tmp_path / "artifacts"
    empty = root / "sess-a"
    empty.mkdir(parents=True)
    used = root / "sess-b"
    used.mkdir(parents=True)
    (used / "还在用的.mp4").write_bytes(b"x")

    assert session_artifacts.drop_session("sess-a", base=root) is True
    assert session_artifacts.drop_session("sess-b", base=root) is False
    assert not empty.exists() and used.is_dir()
    assert session_artifacts.drop_session("", base=root) is False


def test_prune_empty_collects_leftover_shells(tmp_path):
    from paper_agent.services import session_artifacts

    root = tmp_path / "artifacts"
    (root / "sess-a").mkdir(parents=True)
    (root / "sess-b").mkdir(parents=True)
    (root / "sess-b" / "有东西.mp4").write_bytes(b"x")

    assert session_artifacts.prune_empty(base=root) == 1
    assert not (root / "sess-a").exists() and (root / "sess-b").is_dir()
    assert session_artifacts.prune_empty(base=tmp_path / "没有这个目录") == 0


def test_workspace_prune_empty_collects_empty_upload_and_generated():
    """清理完文件后，空的 upload / generated（以及空会话目录）都要收掉。"""
    from paper_agent.services import session_workspace

    empty = session_workspace.generated_dir("sess-empty")
    empty.mkdir(parents=True, exist_ok=True)
    kept = session_workspace.generated_dir("sess-kept")
    (kept / "还在用.mp4").write_bytes(b"x")

    assert session_workspace.prune_empty() >= 2     # 空 generated + 空会话目录
    assert not empty.exists()
    assert kept.is_dir() and (kept / "还在用.mp4").is_file()
