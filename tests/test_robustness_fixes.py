"""第三轮缺陷审计中确认的问题的回归用例。"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from paper_agent.core.models import ChatSession, Message
from paper_agent.core.session_store import SessionStore
from paper_agent.services.agent_service import (
    DEEP_WRITING_HINT,
    AgentService,
    build_chat_messages,
)
from paper_agent.utils.text import format_relative_time, time_group


# ---------------------------------------------------------------- 会话持久化
def test_save_is_atomic_and_leaves_no_tmp(tmp_path):
    store = SessionStore(tmp_path)
    session = ChatSession(title="原子保存")
    session.add_message(Message(role="user", content="你好"))
    store.save(session)

    files = sorted(p.name for p in tmp_path.iterdir())
    assert files == [f"{session.id}.json"], f"不该留下临时文件：{files}"
    assert SessionStore(tmp_path).load(session.id).messages[0].content == "你好"


def test_corrupt_session_file_does_not_kill_startup(tmp_path):
    """坏文件只跳过：以前 from_dict 在 try 之外，一个坏文件会让应用起不来。"""
    store = SessionStore(tmp_path)
    good = ChatSession(title="好会话")
    good.add_message(Message(role="user", content="在"))
    store.save(good)

    (tmp_path / "broken.json").write_text("{ 这不是 JSON", encoding="utf-8")
    (tmp_path / "wrong_shape.json").write_text(json.dumps(["不是对象"]), encoding="utf-8")
    (tmp_path / "null.json").write_text(json.dumps(None), encoding="utf-8")

    loaded = store.load_all()
    assert [s.id for s in loaded] == [good.id]
    assert (tmp_path / "broken.json").exists(), "坏文件不该被删掉，用户可能还要抢救"


def test_broken_file_recovers_from_backup(tmp_path):
    """主文件坏了但备份还在：应该能用备份恢复，而不是丢整段对话。"""
    store = SessionStore(tmp_path)
    session = ChatSession(title="有备份的会话")
    session.add_message(Message(role="user", content="原始内容"))
    store.save(session)                      # 第一次：无备份可写

    (tmp_path / f"{session.id}.json").write_text("{坏了", encoding="utf-8")
    backup = tmp_path / f"{session.id}.json.bak"
    backup.write_text(
        json.dumps(session.to_dict(), ensure_ascii=False), encoding="utf-8"
    )

    loaded = store.load_all()
    assert [s.id for s in loaded] == [session.id]
    assert loaded[0].messages[0].content == "原始内容"
    assert store.load(session.id).messages[0].content == "原始内容"


def test_delete_removes_backup_and_tmp(tmp_path):
    store = SessionStore(tmp_path)
    session = ChatSession(title="待删除")
    store.save(session)
    (tmp_path / f"{session.id}.json.bak").write_text("{}", encoding="utf-8")
    (tmp_path / f"{session.id}.json.tmp").write_text("{}", encoding="utf-8")

    store.delete(session.id)
    assert list(tmp_path.iterdir()) == []


def test_backup_is_throttled(tmp_path, monkeypatch):
    """备份不能每次保存都写：大会话文件反复复制会拖慢保存。"""
    store = SessionStore(tmp_path)
    session = ChatSession(title="限频备份")
    store.save(session)
    backup = tmp_path / f"{session.id}.json.bak"
    assert not backup.exists(), "第一次保存没有旧内容，不该产生备份"

    store.save(session)                       # 第二次：产生备份
    assert backup.exists()

    before = backup.stat().st_mtime
    time.sleep(0.02)
    session.touch()
    store.save(session)                       # 第三次：仍在限频窗口内，不重写
    assert backup.stat().st_mtime == before


# ---------------------------------------------------------------- 停止生成
def test_worker_stop_sets_shared_cancel_event():
    """Esc 要能杀掉 execute_python 的子进程：取消事件必须真的被置位。"""
    from paper_agent.services.agent_service import _StreamWorker

    worker = _StreamWorker(reply="x")
    called = {"n": 0}
    worker.abort_hook = lambda: called.__setitem__("n", called["n"] + 1)

    assert not worker.cancel_event.is_set()
    worker.request_stop()

    assert worker.aborted is True
    assert worker.cancel_event.is_set(), "取消事件没置位 → 长任务工具杀不掉"
    assert called["n"] == 1, "停止时应断掉在途请求"


def test_stop_does_not_block_or_wait_on_main_thread(fake_run):
    """stop() 不能 wait()：run() 是阻塞式 producer，等待会冻住界面。"""
    agent = AgentService()
    run = fake_run(agent, "session-1", "m-1")
    waited = {"n": 0}
    run.thread.wait = lambda _ms=None: waited.__setitem__("n", waited["n"] + 1)

    agent.stop("m-1")

    assert run.worker.aborted is True, "应请求 worker 停止"
    assert run.thread.quit_calls == 1, "应让线程退出事件循环"
    assert waited["n"] == 0, "stop() 不该阻塞主线程"


def test_deep_writing_hint_only_when_requested():
    messages = build_chat_messages([], "写第三章", None, deep=True)
    assert any(DEEP_WRITING_HINT in str(m.get("content")) for m in messages)

    plain = build_chat_messages([], "写第三章", None, deep=False)
    assert all(DEEP_WRITING_HINT not in str(m.get("content")) for m in plain)


def test_composer_has_no_dead_buttons(qapp):
    """输入区不该再有「接了信号但没人处理」的装饰按钮。"""
    from paper_agent.core.config import AppConfig
    from paper_agent.ui.composer.composer import Composer

    composer = Composer(AppConfig())
    assert not hasattr(composer, "web_button"), "联网检索没有实现，按钮不该留着"
    assert composer.deep_button.isCheckable()
    assert "3000 字" in composer.deep_button.toolTip()
    composer.deleteLater()


# ---------------------------------------------------------------- 读取与出图
def test_read_document_rejects_huge_file(tmp_path):
    from paper_agent.services.skills.document_skills import MAX_READ_BYTES, read_document

    huge = tmp_path / "巨大.txt"
    huge.write_bytes(b"a" * (MAX_READ_BYTES + 1))
    with pytest.raises(ValueError) as info:
        read_document(str(huge))
    assert "过大" in str(info.value)

    small = tmp_path / "正常.txt"
    small.write_text("正常内容", encoding="utf-8")
    assert read_document(str(small)) == "正常内容"


def test_read_document_rejects_directory(tmp_path):
    from paper_agent.services.skills.document_skills import read_document

    with pytest.raises(IsADirectoryError):
        read_document(str(tmp_path))


def test_image_client_retries_on_rate_limit(monkeypatch):
    """出图很吃配额，命中 429 应退避重试而不是直接失败。"""
    import urllib.error

    from paper_agent.services import image_client as module

    monkeypatch.setattr(module.time, "sleep", lambda _s: None)
    client = module.ImageClient(base_url="https://x/v1", api_key="sk", model_id="m")
    calls = {"n": 0}

    def fake_urlopen(request, timeout=None, context=None):   # noqa: ARG001
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.HTTPError(
                "https://x/v1", 429, "Too Many Requests", {},
                __import__("io").BytesIO(b'{"error":{"message":"tpm limit"}}'),
            )

        class _R:
            def read(self):
                return b'{"data": []}'

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        return _R()

    monkeypatch.setattr(module.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(module.ImageGenerationError):
        client.generate("画个图")           # 第二次仍失败（没返回图片数据）
    assert calls["n"] >= 2, "429 后应该重试一次"


def test_image_client_does_not_retry_other_errors(monkeypatch):
    import urllib.error

    from paper_agent.services import image_client as module

    sleeps = []
    monkeypatch.setattr(module.time, "sleep", lambda s: sleeps.append(s))
    client = module.ImageClient(base_url="https://x/v1", api_key="sk", model_id="m")

    def fake_urlopen(request, timeout=None, context=None):   # noqa: ARG001
        raise urllib.error.HTTPError(
            "https://x/v1", 401, "Unauthorized", {},
            __import__("io").BytesIO(b'{"error":{"message":"bad key"}}'),
        )

    monkeypatch.setattr(module.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(module.ImageGenerationError):
        client.generate("画个图")
    assert sleeps == [], "非限流错误不该等待重试"


# ---------------------------------------------------------------- 时间分组的跨年边界
def test_yesterday_across_year_boundary():
    """1 月 1 日看 12 月 31 日要算「昨天」——按 tm_yday 相减的实现会漏掉。"""
    from datetime import date

    import paper_agent.utils.text as text

    new_year = date(2026, 1, 1)
    dec31 = time.mktime((2025, 12, 31, 23, 30, 0, 0, 0, -1))
    assert text._day_gap(dec31, new_year) == 1, "跨年的昨天没被识别出来"
    assert text._day_gap(time.mktime((2026, 1, 1, 9, 0, 0, 0, 0, -1)), new_year) == 0

    # 同一年内的普通场景不受影响
    assert text._day_gap(time.mktime((2026, 6, 2, 8, 0, 0, 0, 0, -1)), date(2026, 6, 3)) == 1
    assert format_relative_time(time.time() - 30) == "刚刚"
    assert time_group(time.time()) == "今天"


# ---------------------------------------------------------------- 工具调用记录
def test_tool_run_is_updated_in_place_not_duplicated():
    """同一个调用的 running → done 只留一条记录。

    以前每次调用都追加两条，其中一条永远停在 ``running``、返回为空。重试 / 「继续」时
    这份记录会被回灌给模型（``_resume_context``），模型看到「→ running：（空）」就以为
    没跑成，转头照着自己上一轮的叙述编过程 —— 用户看到的「明明没跑却说 5 段全跑完了」
    有一部分就是这么来的。
    """
    message = Message(role="assistant")
    message.add_tool_run("generate_video", args='{"prompt": "第一段"}', status="running")
    message.add_tool_run(
        "generate_video", args='{"prompt": "第一段"}',
        result="已生成 1 段视频", status="done",
    )

    assert len(message.tool_runs) == 1, "同一个调用不该留两条"
    assert message.tool_runs[0].status == "done"
    assert message.tool_runs[0].result == "已生成 1 段视频"

    # 入参不同就是另一次调用：要新开一条
    message.add_tool_run("merge_videos", args='{"videos": ["a.mp4"]}', status="running")
    message.add_tool_run(
        "merge_videos", args='{"videos": ["a.mp4"]}',
        result="执行失败：缺少 ffmpeg", status="failed",
    )

    assert [run.status for run in message.tool_runs] == ["done", "failed"]
    assert message.tool_runs[-1].result == "执行失败：缺少 ffmpeg"


def test_resume_context_states_tool_status_in_plain_words(qapp):
    """回灌给模型的进度摘要不能把 running 说成「已完成」，也别塞英文状态。"""
    from paper_agent.ui.main_window import MainWindow

    message = Message(role="assistant")
    message.add_tool_run(
        "generate_video", args='{"prompt": "第一段"}', result="已生成 1 段视频", status="done"
    )
    # 卡住 / 被打断，始终没拿到返回
    message.add_tool_run("merge_videos", args='{"videos": ["a.mp4"]}', status="running")
    message.add_tool_run(
        "generate_video", args='{"prompt": "第二段"}',
        result="执行失败：服务繁忙（HTTP 503）", status="failed",
    )

    text = MainWindow._resume_context(message)

    assert "成功" in text and "失败" in text and "未完成" in text
    assert "running" not in text and "failed" not in text, "别把英文状态塞给模型"
    assert "已生成 1 段视频" in text
    assert "（无返回内容）" in text, "没有返回的那条要说清楚，不能让模型当成做完了"
