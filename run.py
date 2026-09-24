"""一键启动：同时拉起 API 服务（8000）与用户看板（8010）。

用法::

    python run.py                 # 两个端口都启动
    python run.py --only api      # 只启动注册登录 API
    python run.py --only admin    # 只启动用户看板
"""

from __future__ import annotations

import argparse
import multiprocessing
import sys

import uvicorn

from app.config import ADMIN_HOST, ADMIN_PORT, API_HOST, API_PORT


def serve(target: str, host: str, port: int) -> None:
    uvicorn.run(target, host=host, port=port, log_level="info")


def main() -> int:
    parser = argparse.ArgumentParser(description="Paper Agent Server 启动脚本")
    parser.add_argument("--only", choices=("api", "admin"), help="只启动其中一个服务")
    args = parser.parse_args()

    jobs = []
    if args.only in (None, "api"):
        jobs.append(("app.main:app", API_HOST, API_PORT, "API"))
    if args.only in (None, "admin"):
        jobs.append(("app.admin_app:app", ADMIN_HOST, ADMIN_PORT, "看板"))

    if len(jobs) == 1:
        target, host, port, _ = jobs[0]
        print(f"启动 {target} -> http://{host if host != '0.0.0.0' else '127.0.0.1'}:{port}")
        serve(target, host, port)
        return 0

    print("Paper Agent Server 启动中（Ctrl+C 退出）")
    for target, host, port, name in jobs:
        shown = host if host != "0.0.0.0" else "127.0.0.1"
        print(f"  · {name}: http://{shown}:{port}")

    processes = []
    try:
        for target, host, port, _name in jobs:
            proc = multiprocessing.Process(target=serve, args=(target, host, port), daemon=True)
            proc.start()
            processes.append(proc)
        for proc in processes:
            proc.join()
    except KeyboardInterrupt:
        for proc in processes:
            proc.terminate()
        print("\n已停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
