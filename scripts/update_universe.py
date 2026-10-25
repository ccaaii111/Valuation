"""Rebuild the search index:  python scripts/update_universe.py [--force]

Web 面板启动时会自动做一次（七天内的旧索引直接复用），这个脚本是给「刚退市一只票、想立刻搜到」或者
启动时没网、事后补拉的情况用的。约 30~60 秒：akshare 要翻 18 个分页。

It writes data/universe.json only. 这份文件不进模型：估值用的每个数都得有 runs/ 里的原始响应作凭证
（见 backend/data/universe.py 的模块说明）。它只被问一句「这只票还在不在市」——`backend.builder.admission`
据此在联网之前就拒绝退市／查无此票的代码，所以刚上市的新票被名录漏掉时，出路就是带 --force 重跑这里。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.data import universe  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="刷新股票搜索索引")
    parser.add_argument("--force", action="store_true", help="即使现有索引还很新也重拉")
    parser.add_argument("--max-age-days", type=float, default=universe.DEFAULT_MAX_AGE_DAYS)
    args = parser.parse_args(argv)

    try:
        document, status = universe.refresh(max_age_days=args.max_age_days, force=args.force)
    except universe.UniverseUnavailable as exc:
        print(f"索引拉取失败：{exc}", file=sys.stderr)
        print("盘上没有可退的旧索引，搜索页会提示先联网再刷新。", file=sys.stderr)
        return 2

    age = universe.age_days(document)
    print(f"{status}：{document['total']} 只，生成于 {document['generated_at']}（{age:.1f} 天前）→ {universe.UNIVERSE_PATH}")
    if status == "stale":
        print(f"重拉失败，只能用 {age:.0f} 天前的旧索引；先跑一遍确认网络，或看错误详情。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
