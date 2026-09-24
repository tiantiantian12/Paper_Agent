"""会话持久化：JSON 文件存储于用户数据目录。

三个可靠性要点：
- **原子写入**：先写 ``.tmp`` 再 ``os.replace``，写盘中途崩溃不会毁掉原文件；
- **限频备份**：同一会话最多 5 分钟留一份 ``.bak``，文件内容坏了还能捞回来；
- **坏文件只跳过、不删除**：单个会话文件损坏不能让整个应用起不来。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

from paper_agent.core.constants import SESSIONS_DIR
from paper_agent.core.models import ChatSession, sort_sessions

BACKUP_INTERVAL = 300.0        # 同一会话两次备份的最小间隔（秒）


class SessionStore:
    """会话读写（一个会话一个 JSON 文件）。"""

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = Path(directory or SESSIONS_DIR)
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, session_id: str) -> Path:
        return self.directory / f"{session_id}.json"

    # ------------------------------------------------------------------ 读
    @staticmethod
    def _load_file(file: Path) -> ChatSession:
        data = json.loads(file.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("会话文件结构不是对象")
        return ChatSession.from_dict(data)

    def load_all(self) -> list[ChatSession]:
        sessions: list[ChatSession] = []
        broken: list[str] = []
        for file in sorted(self.directory.glob("*.json")):
            try:
                sessions.append(self._load_file(file))
            except Exception:      # noqa: BLE001 - 坏文件只跳过，绝不连坐
                try:
                    sessions.append(self._load_file(self._backup_path(file)))
                    broken.append(f"{file.name}（已从备份恢复）")
                except Exception:  # noqa: BLE001
                    broken.append(file.name)
        if broken:
            print(
                f"[会话] 跳过 {len(broken)} 个无法解析的文件：{'、'.join(broken)}",
                file=sys.stderr,
            )
        return sort_sessions(sessions)

    def load(self, session_id: str) -> ChatSession | None:
        path = self._path(session_id)
        if not path.exists():
            return None
        try:
            return self._load_file(path)
        except Exception:          # noqa: BLE001
            try:
                return self._load_file(self._backup_path(path))
            except Exception:      # noqa: BLE001
                return None

    # ------------------------------------------------------------------ 写
    def save(self, session: ChatSession) -> None:
        """原子保存：临时文件写完后替换，避免留下半个 JSON。"""
        path = self._path(session.id)
        payload = json.dumps(session.to_dict(), ensure_ascii=False, indent=2)
        tmp = path.with_name(path.name + ".tmp")
        try:
            tmp.write_text(payload, encoding="utf-8")
            self._backup(path)
            os.replace(tmp, path)      # 同目录替换在 Windows/POSIX 上都是原子的
        except OSError:
            tmp.unlink(missing_ok=True)
            raise

    @staticmethod
    def _backup_path(path: Path) -> Path:
        return path.with_name(path.name + ".bak")

    def _backup(self, path: Path) -> None:
        """限频备份：太频繁会把大文件反复复制，反而拖慢保存。"""
        if not path.exists():
            return
        backup = self._backup_path(path)
        try:
            if backup.exists() and time.time() - backup.stat().st_mtime < BACKUP_INTERVAL:
                return
            shutil.copyfile(path, backup)
        except OSError:            # 备份失败不该影响正常保存
            pass

    def delete(self, session_id: str) -> None:
        for suffix in (".json", ".json.bak", ".json.tmp"):
            target = self.directory / f"{session_id}{suffix}"
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
