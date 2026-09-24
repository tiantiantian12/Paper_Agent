#!/usr/bin/env bash
# Paper Agent Server 数据备份：SQLite 一致性快照（不锁库、不打断服务）+ 清理过期备份。
#
#   sudo bash deploy/backup.sh                     # 手工备一次
#   systemctl list-timers paper-agent-backup       # 看定时任务
#
# 用 sqlite3 的在线 backup API（**不是 cp**：WAL 模式下直接拷文件会拷到一个坏库），
# 直接用项目 venv 里的 python，不额外装 sqlite3 命令行。
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$APP_DIR/.venv/bin/python"
DB_DIR="${PAPER_AGENT_SERVER_DATA:-$APP_DIR/data}"
OUT_DIR="${PAPER_AGENT_BACKUP_DIR:-/var/backups/paper-agent}"
KEEP_DAYS="${PAPER_AGENT_BACKUP_DAYS:-14}"

# 数据目录可能是相对路径（config.json 里填相对路径按项目根解析）
if [[ "$DB_DIR" != /* ]]; then
  DB_DIR="$APP_DIR/$DB_DIR"
fi

[[ -x "$PY" ]] || { echo "[backup] 找不到 $PY，先跑 deploy/install.sh" >&2; exit 1; }
mkdir -p "$OUT_DIR"

TARGET="$OUT_DIR/paper_agent-$(date +%F-%H%M).db"

# 备份 + 校验（坏备份比没有备份更危险，所以备完立刻打开数一下表）
"$PY" - "$DB_DIR/paper_agent.db" "$TARGET" <<'PY'
import pathlib
import sqlite3
import sys

source, target = pathlib.Path(sys.argv[1]), sys.argv[2]
if not source.exists():
    raise SystemExit(f"[backup] 找不到数据库：{source}")

# 只读打开源库，backup() 会取一致性快照（WAL 里还没落盘的数据也会带上）
src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
dst = sqlite3.connect(target)
try:
    with dst:
        src.backup(dst)
finally:
    dst.close()
    src.close()

con = sqlite3.connect(target)
try:
    tables = con.execute(
        "select name from sqlite_master where type='table'"
    ).fetchall()
finally:
    con.close()
if not tables:
    raise SystemExit("[backup] 备份文件里一张表都没有，别指望它 —— 请检查数据目录")
print(f"[backup] 已备份 -> {target}（{len(tables)} 张表）")
PY

find "$OUT_DIR" -maxdepth 1 -type f -name 'paper_agent-*.db' -mtime "+$KEEP_DAYS" -delete
echo "[backup] 目录 $OUT_DIR 现有 $(find "$OUT_DIR" -maxdepth 1 -type f -name 'paper_agent-*.db' | wc -l) 份（保留 ${KEEP_DAYS} 天）"
