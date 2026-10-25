"""Universe index tests — 喂进假名录，不联网：python tests/test_universe.py"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.data import universe as U  # noqa: E402


def check(label: str, condition: bool, detail: str = "") -> None:
    print(("  PASS  " if condition else "  FAIL  ") + label + (f"  {detail}" if detail else ""))
    if not condition:
        globals()["FAILURES"] += 1


FAILURES = 0

ROWS = [
    ("600519", "贵州茅台"),
    ("000858", "五粮液"),
    ("300750", "宁德时代"),
    ("688981", "中芯国际"),
    ("830799", "艾融软件"),
    ("900901", "B股示例"),
    ("600001", "邯郸钢铁"),  # 退市票不在交易所名录里，这里只当普通沪市票
    ("000001", "平安银行"),
    ("bad", "脏数据"),
    ("600519", "贵州茅台"),  # 重复
    ("12345", "位数不足"),
]


def test_normalize_sorts_and_tags_boards() -> None:
    entries = U.normalize(ROWS)
    codes = [e.code for e in entries]
    check("脏行被丢弃", all(len(c) == 6 and c.isdigit() for c in codes), ",".join(codes))
    check("重复代码只留一条", codes.count("600519") == 1, str(codes))
    check("按代码升序", codes == sorted(codes), ",".join(codes))
    by = {e.code: e for e in entries}
    check("60 归沪市主板", by["600519"].board == "沪市主板", by["600519"].board)
    check("688 归科创板", by["688981"].board == "科创板", by["688981"].board)
    check("300 归创业板", by["300750"].board == "创业板", by["300750"].board)
    check("北交所照列但标注未接通", by["830799"].board == "北交所" and "北交所暂未接通" in by["830799"].blocked, by["830799"].blocked)
    check("B 股照列但标注未接通", "未接通" in by["900901"].blocked, by["900901"].blocked)
    check("沪深主板不标未接通", by["000858"].blocked == "" and by["600519"].blocked == "")


def test_pinyin_indexes_both_abbreviations_and_full_spellings() -> None:
    py, full = U.pinyin("贵州茅台")
    check("简拼 gzmt", py == "gzmt", py)
    check("全拼 guizhoumaotai", full == "guizhoumaotai", full)
    py2, full2 = U.pinyin("万华化学")
    check("多音字以外的简拼照收", py2 == "whhx" and full2.startswith("wan"), f"{py2} / {full2}")
    py3, _ = U.pinyin("TCL科技")
    check("名称里的字母数字进索引", "tcl" in py3, py3)


def test_padded_names_get_collapsed() -> None:
    """名录按定宽补空格（实测 36 只），不合并就搜不到：「万科」匹配不上「万 科Ａ」。"""
    entries = U.normalize([("000002", "万  科Ａ"), ("000001", "平安银行")])
    by = {e.code: e for e in entries}
    check("名称里的空白被去掉", by["000002"].name == "万科Ａ", by["000002"].name)
    check("简拼不含空白", by["000002"].py == "wk", by["000002"].py)  # 全角Ａ不出拼音，只剩 wk
    check("两字名称照样能整词命中", "万科" in by["000002"].name and "万科" not in by["000001"].name)


def test_document_shape_and_roundtrip() -> None:
    document = U.build(rows=ROWS)
    check("文档记来源与总条数", document["total"] == len(document["entries"]) == 8, str(document["total"]))
    check("文档带生成时间", "generated_at" in document and document["generated_at"].endswith("Z"))
    check("文档声明只答准入不答估值", "还在不在市" in document["note"] and "runs/" in document["note"], document["note"][:40])
    first = document["entries"][0]
    check("行含六个字段", set(first) == {"code", "name", "py", "full", "board", "blocked"}, str(sorted(first)))

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "nested" / "universe.json"
        U.write(document, path)
        check("落盘后可读回", U.read(path)["total"] == 8)
        check("落盘不留临时文件", not path.with_suffix(".json.tmp").exists())
        path.write_text("{ 这不是 JSON", encoding="utf-8")
        check("坏文件按没有索引处理", U.read(path) is None)

    try:
        U.build(rows=[])
        check("空名录不落成空索引", False, "未抛 UniverseUnavailable")
    except U.UniverseUnavailable as exc:
        check("空名录不落成空索引", "不写索引文件" in str(exc), str(exc)[:40])


def test_age_and_freshness_decision() -> None:
    now = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
    stamp = (now - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    check("3 天索引算新", U.age_days({"generated_at": stamp}, now) == 3.0)
    check("缺时间戳按极旧处理", U.age_days({}, now) > 1e6)
    check("默认容忍七天", U.DEFAULT_MAX_AGE_DAYS == 7, str(U.DEFAULT_MAX_AGE_DAYS))

    import tempfile

    fetch_rows = U.fetch_rows  # 真去拉一次全名录要一分钟，测试里换掉这一层就够了
    document = U.build(rows=ROWS)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "universe.json"
            U.write(dict(document, generated_at=now.strftime("%Y-%m-%dT%H:%M:%SZ")), path)
            got, status = U.refresh(7, now=now, path=path)
            check("新索引直接复用、不联网", status == "fresh" and got["total"] == 8, status)

            U.fetch_rows = lambda: (_ for _ in ()).throw(U.UniverseUnavailable("网络不通"))
            U.write(dict(document, generated_at=(now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")), path)
            got, status = U.refresh(7, now=now, path=path)
            check("过期且重拉失败时退回旧索引", status == "stale" and got["total"] == 8, status)
            check("旧索引原样留在盘上", U.read(path)["generated_at"].startswith((now - timedelta(days=30)).strftime("%Y-%m-%d")))

            path.unlink()
            try:
                U.refresh(7, now=now, path=path)
                check("没有旧索引可退时如实报错", False, "未抛 UniverseUnavailable")
            except U.UniverseUnavailable as exc:
                check("没有旧索引可退时如实报错", "网络不通" in str(exc), str(exc)[:40])

            U.fetch_rows = lambda: ROWS
            got, status = U.refresh(7, now=now, path=path)
            check("冷启动拉成后即落盘", status == "rebuilt" and U.read(path)["total"] == 8, status)

            U.write(dict(got, generated_at=(now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")), path)
            got, status = U.refresh(7, now=now, path=path)
            check("过期但能重拉时写新索引", status == "updated" and U.read(path)["total"] == 8, status)

            U.write(dict(document, generated_at=now.strftime("%Y-%m-%dT%H:%M:%SZ")), path)
            got, status = U.refresh(7, force=True, now=now, path=path)
            check("--force 跳过新鲜度判断", status == "updated", status)
    finally:
        U.fetch_rows = fetch_rows


def test_listed_answers_admission_only() -> None:
    """名录被借去回答的只有「还在不在市」：三态里 None 是「不知道」，不是「查无此人」。"""
    import tempfile

    now = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
    document = dict(U.build(rows=ROWS), generated_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"))
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "universe.json"
        U.write(document, path)
        verdict, basis = U.listed("600519", path=path, now=now)
        check("名录里有→在", verdict is True and "贵州茅台" in basis, str((verdict, basis)))
        verdict, basis = U.listed("600002", path=path, now=now)
        check("名录里没有→不在", verdict is False and "没有它" in basis, str((verdict, basis)))

        seven = dict(document, generated_at=(now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ"))
        U.write(seven, path)
        verdict, _ = U.listed("600002", path=path, now=now)
        check("第七天当天名录仍然算数", verdict is False, str(verdict))
        eight = dict(document, generated_at=(now - timedelta(days=8)).strftime("%Y-%m-%dT%H:%M:%SZ"))
        U.write(eight, path)
        verdict, basis = U.listed("600002", path=path, now=now)
        check("旧过七天就答不了", verdict is None and "8 天未刷新" in basis, str((verdict, basis)))

        path.unlink()
        verdict, basis = U.listed("600002", path=path, now=now)
        check("没有索引时也答不了", verdict is None and "没有可用的股票索引" in basis, str((verdict, basis)))


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"[{name}]")
            fn()
    print(f"\n失败 {FAILURES} 项")
    raise SystemExit(1 if FAILURES else 0)
