"""Page routing for the multi-page site: which file answers which URL, and the shared assets exist.

    python tests/test_routes.py

No network and no runs/ touched — these are static files behind Flask routes, plus /api/universe driven
off a fabricated index.
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.data import universe  # noqa: E402
from backend.web.app import STATIC_DIR, create_app  # noqa: E402

CLIENT = create_app().test_client()


def check(label: str, condition: bool, detail: str = "") -> None:
    print(("  PASS  " if condition else "  FAIL  ") + label + (f"  {detail}" if detail else ""))
    if not condition:
        globals()["FAILURES"] += 1


FAILURES = 0


def text_of(url: str) -> tuple[int, str]:
    response = CLIENT.get(url)
    return response.status_code, response.get_data(as_text=True)


def test_pages_have_their_own_urls() -> None:
    for url, needle, label in [
        ("/", "估值模型", "首页给功能选择"),
        ("/search", "代码 / 名称 / 简拼", "搜索页可取"),
        ("/panel", "collectOverrides", "面板页可取"),
    ]:
        status, body = text_of(url)
        check(label, status == 200 and needle in body, f"{url} → {status}，{len(body)} 字节")


def test_shared_assets_are_served() -> None:
    status, css = text_of("/assets/app.css")
    check("样式表可取", status == 200 and ".strip" in css, str(status))
    # 表头遮挡的根因是写死的 top:49px；改成 CSS 变量后，真实高度由 common.js 量出来喂进去。
    check("表头吃 CSS 变量", "top:var(--header-h)" in css.replace(" ", ""))
    check("样式表里不再有写死的表头偏移", "top:49px" not in css.replace(" ", ""))
    status, script = text_of("/assets/common.js")
    check("公共脚本可取", status == 200 and "stickHeader" in script, str(status))
    check("公共脚本量功能条高度", "--header-h" in script and "offsetHeight" in script)
    for url, name in [("/", "home"), ("/panel", "panel"), ("/search", "search"), ("/derivation", "derivation")]:
        status, body = text_of(url)
        check(f"{name} 页引用公共资源", "/assets/app.css" in body and "/assets/common.js" in body, str(status))


def test_asset_route_stays_inside_static_dir() -> None:
    for hostile in ("/assets/../app.py", "/assets/..%2Fapp.py", "/assets/%2e%2e/app.py"):
        status, body = text_of(hostile)
        check(f"资源路由不越界: {hostile}", status == 404, f"{status} {body[:40]}")


def test_universe_api_carries_its_own_staleness() -> None:
    """索引过期不隐瞒：天数随载荷一起出去，页面才有资格说「X 天未更新」。"""
    index = universe.UNIVERSE_PATH
    with tempfile.TemporaryDirectory() as tmp:
        universe.UNIVERSE_PATH = Path(tmp) / "universe.json"
        try:
            status, body = text_of("/api/universe")
            check("没有索引时报 404 并给出重建命令", status == 404 and "update_universe.py" in body, f"{status} {body[:60]}")

            document = universe.build(rows=[("600519", "贵州茅台"), ("830799", "艾融软件")])
            universe.write(document)
            data = json.loads(text_of("/api/universe")[1])
            check("载荷带条数与条目", data["total"] == 2 and data["entries"][0]["code"] == "600519", str(data.get("total")))
            check("新索引不标过期", data["stale"] is False and data["age_days"] < 1, str(data["age_days"]))
            check("北交所那条照列但带着未接通原因", data["entries"][1]["blocked"].startswith("北交所暂未接通"), data["entries"][1]["blocked"])
            check("索引声明自己只答准入不答估值", "还在不在市" in data["note"] and "runs/" in data["note"], data["note"][:30])

            universe.write(dict(document, generated_at=(datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")))
            data = json.loads(text_of("/api/universe")[1])
            check("过期索引标出来并报天数", data["stale"] is True and 29 <= data["age_days"] <= 31, str(data["age_days"]))
        finally:
            universe.UNIVERSE_PATH = index


def test_search_page_wires_the_index() -> None:
    page = (STATIC_DIR / "search.html").read_text(encoding="utf-8")
    check("搜索页自己拉索引", "/api/universe" in page and "/api/options" in page)
    check("搜索页有优先级匹配", "function rank" in page and "e.py.startsWith" in page)
    check("搜索页截断后报剩余条数", "还有" in page and "LIMIT" in page)
    check("输入法合成期间不搜", "compositionstart" in page and "isComposing" in page)
    check("退市票给出去处而不是查无此人", "可能已退市" in page and "build_baseline.py" in page)
    # 那句出路本身会过期：名录现在拦在联网之前，所以文案不许再说「联网试试」，命令行也不许多一个 --code。
    check("去处说的是名录否决而不是没取到行情", "查无此票" in page and "没取到行情" not in page)
    check("给的是名录真有的那条出路", "update_universe.py --force" in page and "--code " not in page)
    check("结果行带估值入口", "取数估值" in page and "go=fetch" in page)


def test_panel_lets_the_backend_decide_whether_to_go_online() -> None:
    """「取数估值」只有一个按钮：新旧由 /api/valuation_status 判断，页面只负责把答复说出来。

    M4 起这套机器整个搬进了 common.js（面板与推导图共用一台），所以断言按「浏览器真正跑的那份程序」
    = common.js + 页面内联脚本来读；页面自己只剩控件、markup 与「跑完怎么把新批次读回来」。
    """
    status, body = text_of("/api/valuation_status?code=300001")
    check("新鲜度路由可取", status == 200 and json.loads(body)["action"] == "fetch", f"{status} {body[:60]}")
    page = (STATIC_DIR / "panel.html").read_text(encoding="utf-8")
    desk = (STATIC_DIR / "common.js").read_text(encoding="utf-8") + page
    check("面板先问新鲜度", "async function showFreshness" in desk and "/api/valuation_status" in desk)
    check("复现答复有对应的文案", "out.replayed" in desk and "已复现" in desk)
    check("过期分组显示在状态条", "stale_groups" in desk and 'id="freshness"' in page)
    check("留一条强制重取的出路", 'id="refetch"' in page and "fetchValuation(true)" in desk)
    # 作业期间的第二次点击只会撞上后台那句「已有一个作业在跑」，所以两个按钮一起置灰；
    # 漏掉「强制重取」就是留一个按得动、但什么都不做的按钮——那正是 #63 原来的形状。
    check("取数台把两个按钮一起置灰", '$("fetch").disabled = $("refetch").disabled = true' in desk)
    check("不再有只放开取数按钮的出口", '$("fetch").disabled = false;' not in desk)
    check("四个出口都把两个按钮一起放开", desk.count('$("fetch").disabled = $("refetch").disabled = false;') == 4,
          str(desk.count('$("fetch").disabled = $("refetch").disabled = false;')))
    # addEventListener 会把 Event 对象当第一个实参传进去，直接写 fetchValuation 就等于每次点击都 force。
    check("取数按钮不把点击事件当 force", '$("fetch").addEventListener("click", () => fetchValuation())' in desk)
    check("换代码或批次时重新问过", page.count("showFreshness()") >= 3)
    # 名录否决是第三种答复（既不是重取也不是复现），它只能长在新鲜度那一行里；页面再各写一份就分叉了。
    check("名录否决写在新鲜度那一行", "out.blocked" in desk and "查无此票" in desk)
    # 空批次那句是老文案：它答应「点了就联网」，而名录现在会在联网之前把这次点击拒掉。
    check("空批次提示不预告联网", "联网生成" not in page)
    for name in ("panel.html", "derivation.html"):
        check(f"{name} 的按钮说明交代了名录准入", "名录里没有的代码直接拒" in (STATIC_DIR / name).read_text(encoding="utf-8"))
    # 抽成公共件之后，页面再自带一份就是分叉的开始：两份 fetchValuation 会各存一种「换基准联不联网」的说法。
    for name in ("fetchValuation", "showFreshness", "refreshOptions"):
        check(f"页面不重复声明 {name}", f"function {name}" not in page)


def test_both_pages_run_the_same_fetch_desk() -> None:
    """换基准＝读盘不联网这套说法只许存在一处：两页共用 common.js 的取数台，各自只交回 reload。"""
    common = (STATIC_DIR / "common.js").read_text(encoding="utf-8")
    pages = {name: (STATIC_DIR / name).read_text(encoding="utf-8") for name in ("panel.html", "derivation.html")}
    check("取数台认得这批预取过谁", "out.benchmark.switch" in common and "另开一个批次复现" in common,
          common[common.find("async function showFreshness"):][:600])
    check("预取过的基准不必联网这句写在公共件里", "换成下拉里的另一个基准不联网" in common)
    for name, text in pages.items():
        check(f"{name} 有基准下拉与取数按钮", 'id="benchmark"' in text and 'id="fetch"' in text)
        check(f"{name} 把取数台接上自己的 reload", "configureFetchDesk(" in text and "bindFetchControls()" in text)


def test_derivation_page_reuses_the_panel_s_write_path() -> None:
    """图上改数＝面板改数：这条等式靠「推导图没有自己的写入接口」来保证，而不是靠两处都记得改。"""
    status, page = text_of("/derivation")
    check("推导图页可取", status == 200 and "推导关系图" in page, f"{status} {len(page)} 字节")
    check("图的边由后端给，页面不认识 DERIVES", "/api/derivation" in page and "DERIVES" not in page)
    writes = set(re.findall(r'fetch\("(/api/[a-z_]+)",\s*\{\s*method: "POST"', page))
    check("写入只有面板那两条", writes == {"/api/recompute", "/api/save"}, str(writes))
    # ±%↔绝对值的换算下沉到 common.js 之后，页面只能调用、不能自带一份，否则 manual.json 会分叉。
    check("换算用公共件不自造", "toAbsolute(" in page and "modeOf(" in page and "function toAbsolute" not in page)
    check("画全部行而非子图", "state.order" in page and "无上游" in page)
    check("级联行在图上不可填", "node.derived" in page and "不可直接编辑" in page)
    check("不进定价的行标成参考行", '"ref"' in page and "不进定价" in page)
    panel = (STATIC_DIR / "panel.html").read_text(encoding="utf-8")
    check("面板有一条路过去", "/derivation?code=" in panel and 'id="derivation-link"' in panel)


def test_both_pages_offer_the_erp_window_dropdown() -> None:
    """「ERP 窗口」是同一件控件的两个入口：判据与渲染都只许有一份，页面各自只接线。

    窗口选择就是人工修正——所以它不许带来第三条写入通路：页面自己只 POST 改数与保存那两条。
    """
    common = (STATIC_DIR / "common.js").read_text(encoding="utf-8")
    pages = {name: (STATIC_DIR / name).read_text(encoding="utf-8") for name in ("panel.html", "derivation.html")}
    for name, text in pages.items():
        check(f"{name} 顶栏有 ERP 窗口下拉", 'id="erpwindow"' in text)
        check(f"{name} 只调用公共渲染件", 'renderErpWindow($("erpwindow")' in text and "function renderErpWindow" not in text)
        writes = set(re.findall(r'fetch\("(/api/[a-z_]+)",\s*\{\s*method: "POST"', text))
        # /api/fetch 那条已经搬进公共件，页面剩下的写入通路只有改数与保存。
        check(f"{name} 的写入通路没有多出第三条", writes <= {"/api/recompute", "/api/save"}, str(sorted(writes)))
    desk_writes = set(re.findall(r'fetch\("(/api/[a-z_]+)",\s*\{\s*method: "POST"', common))
    check("公共件里只多一条取数", desk_writes == {"/api/fetch"}, str(sorted(desk_writes)))
    check("可选档位由后端的 usable 决定，页面不再判区间", "(windows || []).filter((w) => w.usable" in common)
    check("「自定义」只在数对不上窗口时出现在下拉里", 'value="custom"' in common and "custom" not in common.split("function erpWindowMarkup")[0])
    check("两页都把自定义当不覆盖手改的值", all('=== "custom"' in text for text in pages.values()), str(sorted(pages)))
    check("一个可用窗口都没有时下拉置灰", "select.disabled = !built.usable" in common)


def test_the_chain_view_is_a_read_only_zoom_of_the_graph() -> None:
    """放大＝页内浮层，里面只装「链尾 + 上游两层」：重画这张小链图，再逐行给符号式与代入式，一个输入框都不带。

    「只读」是这里的核心——它一旦能改数，就得再写一份 ±% 换算与一份保存通路，manual.json 就分叉了。
    「干净」靠的是浮层里另画一张只含链上节点的图，而不是把整图擦掉：背后那张图一个节点都没动。
    """
    status, page = text_of("/derivation")
    check("链视图与整图同一页", status == 200 and "推导关系图" in page, f"{status} {len(page)} 字节")
    # 浮层是 <main> 之外的兄弟节点：它在整图外面，所以放大时整图原样留着，只是被压住。
    check("放大态是页内浮层", '<div class="overlay hide" id="chain-overlay">' in page
          and page.index("</main>") < page.index('<div class="overlay hide" id="chain-overlay">')
          and '<section class="chain hide" id="chain">' not in page)
    check("链＝链尾加两层上游", "const CHAIN_DEPTH = 2;" in page and "for (let hop = 0; hop < depth; hop++)" in page)
    check("只走 upstream，不顺着 downstream 摊", "state.nodes[node]?.upstream" in page
          and "state.nodes[node]?.downstream" not in page)
    check("行序跟着整图排", "state.order.filter((node) => hops.has(node))" in page)
    # 小图自己画一张，不是复用整图：边只在链内两点之间连，链外的上游一律不画。
    check("浮层里重画一条只含链上的小图", "function chainGraph(keys)" in page
          and 'class="chain-graph"' in page and "cnode" in page and "if (pos.has(from))" in page)
    check("小图的栏＝距链尾几跳", "链尾（本行）" in page and "上游 ${cols.length - 1 - c} 跳" in page)
    check("入口在侧栏，返回在链顶", "放大这条链" in page and "← 返回整图" in page and "↑ 上一层链" in page)
    check("深链可分享", 'params.get("chain")' in page and 'url.searchParams.set("chain"' in page)
    # 放大态只切浮层显隐（顺带锁背景滚动）；整图的淡出仍是 paintSelection 的 fade，不是 display:none。
    check("放大时整图不隐藏", 'classList.toggle("hide", !on)' in page and 'classList.toggle("fade"' in page
          and '$("board").classList' not in page)
    check("改数后推导段跟着重算", "if (state.chain) renderChain();" in page)
    # 三条退路指向同一个 closeChain：Esc、返回按钮、点浮层外的暗处。少一条都像被关在弹窗里。
    check("Esc 与返回按钮与暗处都退得掉", 'event.key === "Escape"' in page and '$("chain-back").addEventListener' in page
          and 'event.target.id === "chain-overlay"' in page)
    # 上一版把推导段摊在整图下方，靠页面滚动露出来——不滚就像按钮坏了。浮层不需要那套。
    check("放大不再依赖页面滚动", "revealChainSection" not in page and "window.scrollTo" not in page)
    fade = re.search(r"\.node\.fade\s*\{\s*opacity:\s*\.?(\d+)", page)
    check("淡出是变暗而不是隐身", fade and int(fade.group(1)[0]) >= 3, f"opacity .{fade.group(1) if fade else '缺'}")
    # 侧栏要跟着链尾走：深链进来时它先渲染成「未选中节点」，不重画就和图上高亮的那组对不上。
    check("放大与收起都重画侧栏", page.count("renderDetail(true);") >= 3 and "renderDetail(false);   // 侧栏仍停在链尾那一行" in page)
    check("改动框仍然只有一处", page.count("buildEditor(") == 2, str(page.count("buildEditor(")))
    # 代进去的数来自各行的当前值，用的还是 common.js 那个 fmt——页面不自造一份格式化。
    check("代入式交给公共件拼", "substituteText(node.substitute, scalarOf)" in page)
    writes = set(re.findall(r'fetch\("(/api/[a-z_]+)",\s*\{\s*method: "POST"', page))
    check("放大页不开新的写入通路", writes == {"/api/recompute", "/api/save"}, str(writes))


def test_startup_refreshes_the_index() -> None:
    """股票列表必须在 Web 起来之前拉好——第一下按键就得有全量清单可筛。"""
    server = (ROOT / "scripts" / "serve_panel.py").read_text(encoding="utf-8")
    check("启动脚本调用索引刷新", "universe.refresh(" in server and "prepare_universe" in server)
    check("刷新发生在 create_app 之前", server.index("prepare_universe(force") < server.index("create_app()"))
    check("有 --refresh-universe 强制重拉", "--refresh-universe" in server)
    check("拉不到索引不拦启动", "UniverseUnavailable" in server and "return 0" in server)


def test_shared_helpers_exist_once() -> None:
    """±%↔绝对值的换算只允许有一份，否则面板和推导图会各算一套口径。"""
    common = (STATIC_DIR / "common.js").read_text(encoding="utf-8")
    pages = {name: (STATIC_DIR / name).read_text(encoding="utf-8") for name in ("panel.html", "derivation.html")}
    for name in ("function toAbsolute", "function modeOf", "const DELTA_EPS", "function renderErpWindow",
                 "const ERP_EPS", "function substituteText"):
        for page, text in pages.items():
            check(f"{name} 只在 common.js 出现一次（{page}）", name in common and name not in text)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"[{name}]")
            fn()
    print(f"\n失败 {FAILURES} 项")
    raise SystemExit(1 if FAILURES else 0)
