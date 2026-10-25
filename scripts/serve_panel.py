"""Serve the assumption-correction panel.

    python scripts/serve_panel.py                # http://127.0.0.1:8765
    python scripts/serve_panel.py --port 9000 --debug
    python scripts/serve_panel.py --refresh-universe   # 先重建股票索引再开面板

From Windows Explorer, double-click start_panel.bat in the repository root — same thing, plus it
keeps the console open so a startup error is readable. On a machine that has nothing installed yet,
deploy_panel.bat creates .venv, installs requirements.txt, then runs this file.
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from flask import Flask
except ImportError:  # pragma: no cover - dependency guard
    raise SystemExit("缺少 Flask：pip install 'flask>=3'（详见 requirements.txt）") from None

from backend.data import universe  # noqa: E402
from backend.web.app import create_app  # noqa: E402


def prepare_universe(force: bool = False) -> int:
    """Index the A-share roster before Flask exists, so 搜索页第一下按键就有全量清单可筛.

    A missing index is not a reason to refuse to start — the panel itself works offline off runs/. 所以这里
    只把情况打印出来，搜索页会在页面上说明同样的话并给「重建索引」。
    """
    try:
        document, status = universe.refresh(force=force)
    except universe.UniverseUnavailable as exc:
        print(f"股票索引不可用：{exc}", file=sys.stderr)
        print("搜索页会空着；联网后跑 python scripts/update_universe.py，或加 --refresh-universe 重启。", file=sys.stderr)
        return 0
    age = universe.age_days(document)
    label = {"fresh": "复用", "updated": "已更新", "rebuilt": "已建立", "stale": "重拉失败、只能用旧索引"}[status]
    print(f"股票索引: {label} {document['total']} 只，生成于 {document['generated_at']}（{age:.1f} 天前）")
    if status == "stale":
        print(f"索引已 {age:.0f} 天没更新（超过 {universe.DEFAULT_MAX_AGE_DAYS} 天）：新退市／新上市的票这阵子搜不到。", file=sys.stderr)
    return document["total"]


def port_in_use(host: str, port: int) -> bool:
    """Werkzeug binds with SO_REUSEADDR, which on Windows lets a second panel start on a taken
    port and serve nothing — so ask the port directly instead of waiting for bind to fail."""
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="本地假设修正面板")
    parser.add_argument("--host", default="127.0.0.1", help="默认只监听本机")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--debug", action="store_true", help="改代码自动重载")
    parser.add_argument("--refresh-universe", action="store_true", help="无视索引新鲜度，重拉一次全名录")
    args = parser.parse_args(argv)

    # Werkzeug 的重载器会带着 WERKZEUG_RUN_MAIN 把本文件再跑一遍。子进程那一次端口正被父进程占着，
    # 股票索引也刚在父进程里查过——两件事都别再干，否则 --debug 一开就自己把自己挡在门外。
    reloaded = os.environ.get("WERKZEUG_RUN_MAIN") == "true"
    if not reloaded and port_in_use(args.host, args.port):
        print(f"端口 {args.port} 已有服务在听：多半是面板已经开着（http://{args.host}:{args.port}）。")
        print(f"要另开一个实例用 --port {args.port + 1}；确认旧实例已退出后再重跑本命令。")
        return 2

    if not reloaded:
        prepare_universe(force=args.refresh_universe)
    app: Flask = create_app()
    print(f"面板: http://{args.host}:{args.port}  （Ctrl+C 退出）")
    app.run(host=args.host, port=args.port, debug=args.debug, use_reloader=args.debug, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
