// DOM 验收哨兵：Python 那 29 个套件看不见 CSS、看不见滚动、也看不见页面渲染出来的 HTML，这一份补的就是那一层。
//
// 它把盘上 static/ 里 derivation.html 的内联脚本与 common.js 原样读进来，在 vm 里配一个 DOM 桩跑起来，
// 数据一律走面板的真接口——所以跑之前要有两样东西：
//   1. 面板在跑：python scripts/serve_panel.py
//   2. runs/ 里至少有一个 <代码> 的批次（默认 600519）
//
//   node tests/dom_derivation_check.js [代码] [批次]
//   PANEL_BASE=http://127.0.0.1:8765 换端口
//
// 批次不写死：默认取该代码在盘上最新的那一批，两条基准探针自己去盘上找符合条件的批次。
// 退出码：全过 0，有 FAIL 1，跑不起来（面板没开、这批不存在）2。
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const ROOT = path.resolve(__dirname, "..");
const DIR = path.join(ROOT, "backend", "web", "static") + path.sep;
const BASE = process.env.PANEL_BASE || "http://127.0.0.1:8765";
const CODE = process.argv[2] || "600519";

function newestRun(code) {
  const dir = path.join(ROOT, "runs", code);
  if (!fs.existsSync(dir)) return "";
  return fs.readdirSync(dir).filter((n) => /^\d{8}-\d{6}$/.test(n)).sort().pop() || "";
}

const RUN = process.argv[3] || newestRun(CODE);
if (!RUN) {
  console.log(`跑不起来：runs/${CODE} 下没有批次目录（先跑 python scripts/build_baseline.py --code ${CODE}）`);
  process.exit(2);
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
// 页面脚本自己也会 fetch（沙箱里那一层用的就是 node 的全局 fetch），面板没开时这个失败落在微任务里，
// 不在任何 try 之内——不接住就只有一份堆栈，接住才能给出该跑的那条命令。
process.on("unhandledRejection", (exc) => {
  console.log(`跑不起来：${BASE} 没有回应（面板没开？python scripts/serve_panel.py）—— ${String(exc).slice(0, 120)}`);
  process.exit(2);
});

function el(id) {
  return {
    id, innerHTML: "", textContent: "", value: "", title: "", checked: false, type: "", className: "",
    dataset: {}, style: { setProperty() {} }, children: [], handlers: {},
    classList: {
      _set: new Set(),
      toggle(name, on) { if (on === undefined) on = !this._set.has(name); on ? this._set.add(name) : this._set.delete(name); return on; },
      add(name) { this._set.add(name); }, remove(name) { this._set.delete(name); }, contains(name) { return this._set.has(name); },
    },
    addEventListener(type, fn) { (this.handlers[type] ||= []).push(fn); },
    fire(type, event = {}) { (this.handlers[type] || []).forEach((fn) => fn(event)); },
    setPointerCapture() {}, append(...kids) { this.children.push(...kids); },
    insertAdjacentHTML(_pos, html) { this.innerHTML += html; }, setAttribute() {},
    // select 的桩：页面按 `select.options` / `select.add(new Option(...))` 判断某个基准在不在下拉里，
    // 桩得把 innerHTML 里那些 value 认成 option，否则 syncBenchmark 一跑就炸在「not iterable」。
    add(opt) { (this._added ||= []).push(opt); },
    get options() {
      const out = [...String(this.innerHTML || "").matchAll(/value="([^"]*)"/g)].map((m) => ({ value: m[1] }));
      return out.concat(this._added || []);
    },
    getBoundingClientRect() { return { top: 0, left: 0, width: 1680, height: 778 }; },
    querySelector: () => null, querySelectorAll: () => [],
    clientHeight: 700, clientWidth: 1122, offsetHeight: 49, scrollTop: 0, scrollLeft: 0,
  };
}

const nodes = new Map();
const byId = (id) => (nodes.get(id) || nodes.set(id, el(id)).get(id));
const store = {};
// 浮层的退路挂在 document 上（Esc），桩得把这些监听器收下来，最后真的按一次 Escape 才算验过。
const keyHandlers = [];
const sandbox = {
  console, URL, URLSearchParams, Object, Array, Set, Map, Number, String, Math, JSON, Boolean, isNaN, Date, RegExp, Error,
  setTimeout, clearTimeout, Promise,
  Option: function (text, value) { return { text, value: String(value) }; },
  document: {
    getElementById: byId, createElement: (tag) => el(tag), querySelector: () => el("header"),
    documentElement: { style: { setProperty() {} } }, body: { style: {} },
    addEventListener(type, fn) { if (type === "keydown") keyHandlers.push(fn); },
  },
  location: { href: `${BASE}/derivation?code=${CODE}&run=${RUN}`, search: `?code=${CODE}&run=${RUN}` },
  history: { replaceState: (_s, _t, url) => { store.url = String(url); }, state: null },
  window: { addEventListener() {}, innerWidth: 1680, innerHeight: 1050, scrollY: 0, scrollTo: (_x, y) => { win.scrollY = Number(y) || 0; store.scrollY = win.scrollY; } },
  fetch: (url, options) => fetch(url.startsWith("http") ? url : BASE + url, options),
};
const win = sandbox.window;
sandbox.globalThis = sandbox;

// 桩不带真实 DOM，select 的 value 得自己种：留空的话后端只会回「未知口径」。
byId("code").value = CODE;
byId("method").value = "fcff";
byId("terminal").value = "gordon";
byId("rate").value = "";
byId("zoom").value = "fit";

const inline = [...fs.readFileSync(DIR + "derivation.html", "utf8").matchAll(/<script>([\s\S]*?)<\/script>/g)].map((m) => m[1]).join("\n");
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(DIR + "common.js", "utf8"), sandbox);
// 导出行跟页面脚本同一个作用域：state 是 const（词法绑定），挂在 sandbox 上取不到。
vm.runInContext(`${inline}\nglobalThis.__api = { state, openChain, chainOf, backToParentChain, closeChain, write, showFreshness, benchValue, shortValue };`, sandbox);

let bad = 0;
const check = (label, ok, detail = "") => { console.log((ok ? "  PASS  " : "  FAIL  ") + label + (detail ? `  ${detail}` : "")); if (!ok) bad += 1; };

// 独立算一遍「链尾 + 上游两层」：走 /api/derivation 的 edges（from→to）按跳数 BFS，不借页面那个 chainOf。
function twoHops(payload, key) {
  const parents = new Map();
  for (const e of payload.edges) parents.set(e.to, [...(parents.get(e.to) || []), e.from]);
  const seen = new Set([key]);
  let frontier = [key];
  for (let hop = 0; hop < 2; hop++) {
    const next = [];
    for (const k of frontier) for (const up of parents.get(k) || []) if (!seen.has(up)) { seen.add(up); next.push(up); }
    frontier = next;
  }
  return seen;
}

(async () => {
  let options = null;
  try { options = await (await fetch(`${BASE}/api/options`)).json(); } catch (exc) { options = null; }
  if (!options) {
    console.log(`跑不起来：${BASE} 没有回应（面板没开？python scripts/serve_panel.py）`);
    process.exit(2);
  }
  const payload = await (await fetch(`${BASE}/api/derivation?code=${CODE}&run=${RUN}`)).json();
  if (payload.error) {
    console.log(`跑不起来：${CODE} 的批次 ${RUN} 读不出推导图 —— ${payload.error}`);
    process.exit(2);
  }
  const api = sandbox.__api;
  const page = fs.readFileSync(DIR + "derivation.html", "utf8");
  for (let i = 0; i < 80 && !api.state.data; i++) await sleep(100);
  check(`页面跑起来了（${CODE} / ${RUN}）`, Boolean(api.state.data), `att 行数=${Object.keys(api.state.att).length}`);
  check("默认不弹浮层", api.state.chain === "" && byId("chain-overlay").classList.contains("hide")
    && sandbox.document.body.style.overflow !== "hidden");

  // 逐年行在这批可能一个年份都没有（无研报覆盖的公司：营收一致预期就是空表）。
  // 空表在图上会渲染成「undefined:— → undef…」——Python 那 28 套件的桩看不见这一格，这里注入一个空表钉住。
  const emptyNode = { key: "__empty_year_map", unit: "yi" };
  const fullNode = { key: "__full_year_map", unit: "yi" };
  api.state.nodes[emptyNode.key] = emptyNode;
  api.state.nodes[fullNode.key] = fullNode;
  api.state.att[emptyNode.key] = { current: {} };
  api.state.att[fullNode.key] = { current: { 2026: 1.5, 2030: 2.5 } };
  check("逐年行没数据时是破折号，不是 undefined", api.shortValue(emptyNode) === "—", api.shortValue(emptyNode));
  check("逐年行有数据时照旧报首末年", /^2026:.*→.*2030:/.test(api.shortValue(fullNode)), api.shortValue(fullNode));
  // 同一个空表在面板那一列也得是「—」：那一列走的是 common.js 的 fmt，从前它交出空串，格子就一片白。
  check("共用的 fmt 把空表读成破折号", sandbox.fmt({}) === "—", JSON.stringify(sandbox.fmt({})));
  check("共用的 fmt 照旧摊开有年份的表", sandbox.fmt({ 2026: 1.5, 2030: 2.5 }, "yi") === "2026:1.50 2030:2.50",
    sandbox.fmt({ 2026: 1.5, 2030: 2.5 }, "yi"));
  delete api.state.nodes[emptyNode.key];
  delete api.state.nodes[fullNode.key];
  delete api.state.att[emptyNode.key];
  delete api.state.att[fullNode.key];

  api.openChain("wacc");
  const html = () => byId("chain").innerHTML;
  const overlay = byId("chain-overlay");
  const got = api.chainOf("wacc");
  const expect = twoHops(payload, "wacc");
  check("链＝链尾＋上游两层", got.length === expect.size && got.every((k) => expect.has(k)), `${got.length} 跳 vs ${expect.size}`);
  check("两层里含到得了的那几行", ["cost_of_equity", "equity_weight", "beta", "erp", "market_cap", "tax_rate_ltm"].every((k) => got.includes(k)));
  check("第三层以外不在这一屏", !got.includes("risk_free_spot") && !got.includes("risk_free_norm"), got.join(","));
  check("浮层弹出来了", !overlay.classList.contains("hide") && sandbox.document.body.style.overflow === "hidden");
  check("整图没有被换掉", !byId("board").classList.contains("hide"));
  check("链尾那一行同时在侧栏选中", api.state.selected === "wacc");
  check("浮层在整图之外（不在 main 里）", /<\/main>\s*<!--[\s\S]*?-->\s*<div class="overlay hide" id="chain-overlay">/.test(page));
  check("链图只画链上这几行", (() => {
    const boxes = (html().match(/class="cnode/g) || []).length;
    const keys = [...html().matchAll(/class="cnode[^"]*" data-chain="([^"]+)"/g)].map((m) => m[1]);
    return boxes === got.length && keys.every((k) => got.includes(k));
  })(), `${(html().match(/class="cnode/g) || []).length} 格 vs ${got.length} 行`);
  const edgeCount = (re = /^/) => (html().match(/class="cedge[^"]*"/g) || []).filter((c) => re.test(c));
  check("链图里一条边都不越链", (() => {
    const keys = new Set(got);
    return html().includes('<svg ') && edgeCount().length
      === got.reduce((n, k) => n + (payload.nodes.find((x) => x.key === k).upstream.filter((u) => keys.has(u)).length), 0);
  })(), `${edgeCount().length} 条边`);
  // 虚线只跟着「这一批算不动」走：链上哪些行的 live 是 false，指向它们的边就该正好是那些。
  // 不要求一定画出虚线——一批全算得动时，0 条边对 0 行才是对的。
  check("链上的虚线边＝该批算不动的那几行", (() => {
    const dashed = edgeCount(/^class="cedge build-only"$/).length;
    const want = payload.edges.filter((e) => got.includes(e.to) && got.includes(e.from) && payload.nodes.find((x) => x.key === e.to).live === false).length;
    return dashed === want;
  })(), `虚线 ${edgeCount(/^class="cedge build-only"$/).length} 条`);
  check("栏头写明几跳、链尾在最右", html().includes("链尾（本行）") && html().includes("上游 2 跳") && !html().includes("上游 3 跳"));
  // 定宽会在右边留半张白图，公式也被截断——所以栏宽跟着浮层实际宽度摊，图宽就该贴着板子。
  check("小图铺满浮层那一栏", (() => {
    const w = Number((html().match(/class="chain-graph"><svg[^>]*width="(\d+)"/) || [0, "0"])[1]);
    return w > byId("chain").clientWidth - 90 && w <= byId("chain").clientWidth;
  })(), `图宽 ${(html().match(/class="chain-graph"><svg[^>]*width="(\d+)"/) || [0, "?"])[1]} / 板宽 ${byId("chain").clientWidth}`);
  check("顶上写的是返回整图", html().includes("← 返回整图") && !html().includes("上一层链"));
  check("文案说清这里只有这一条链", html().includes("这里只有这一条链") && html().includes("只读"));
  check("链尾给大字结果", /chain-tail[\s\S]*?<b>[^<]+<\/b>/.test(html()), html().match(/chain-tail[\s\S]{0,90}/)?.[0].replace(/\n/g, ""));
  check("浮层里一个输入框都不建", !/<input|<select|buildEditor/.test(html()));
  check("逐跳编号到得了最后一跳", html().includes(`#1/${got.length}`) && html().includes(`#${got.length}/${got.length}`), String(got.length));
  check("WACC 的代入式拼出来了", /<b>代入<\/b>　WACC = \S/.test(html()));
  check("CoE 那一跳也有代入式", html().includes("CoE = +"), html().match(/CoE = [^<]*/)?.[0]);
  check("股权权重那一跳也有代入式", html().includes("w_e = "), html().match(/w_e = [^<]*/)?.[0]);
  check("不含 NaN，也不漏一个花括号", !html().includes("NaN") && !/[{}]/.test(html()),
    JSON.stringify((html().match(/.{0,40}(NaN|[{}]).{0,40}/) || [""])[0]));
  check("再往上走的出口在非根行上", (html().match(/<button class="mag" data-chain=/g) || []).length
    === got.filter((k) => (payload.nodes.find((n) => n.key === k)?.upstream || []).length).length,
    `${(html().match(/<button class="mag" data-chain=/g) || []).length} 个按钮 vs ${got.filter((k) => (payload.nodes.find((n) => n.key === k)?.upstream || []).length).length} 行有上游`);
  check("地址栏带上链尾", store.url.includes("chain=wacc"), store.url);
  check("侧栏跟着链尾渲染", byId("detail").innerHTML.includes("放大这条链") && byId("detail").innerHTML.includes("WACC"),
    byId("detail").innerHTML.slice(0, 40));
  const css = fs.readFileSync(DIR + "derivation.html", "utf8").split("</style>")[0];
  const dim = Number((css.match(/\.node\.fade\s*\{\s*opacity:\s*\.?(\d+)/) || [0, "0"])[1]);
  check("淡出看得见而不是隐身", dim >= 3, `.node.fade opacity .${dim}`);

  api.openChain("cost_of_equity");   // 点「放大到此」＝链尾往上挪一跳
  check("上一层链按钮出现", html().includes("↑ 上一层链") && api.state.trail.join(",") === "wacc,cost_of_equity", api.state.trail.join(","));
  check("链尾换了人就换了一批节点", api.chainOf("cost_of_equity").join(",") !== got.join(",") && !api.chainOf("cost_of_equity").includes("wacc"));
  api.backToParentChain();
  check("回到上一层的链", api.state.chain === "wacc" && api.state.trail.join(",") === "wacc");

  api.openChain("step_per_share");   // 估值段：两层只到得 band 的前几步，靠「放大到此」逐级往上走
  const share = api.chainOf("step_per_share");
  check("每股价值两层＝紧邻的估值步骤", share.includes("step_equity_value") && share.includes("total_shares")
    && !share.includes("wacc") && !share.includes("beta") && share.length <= 8, share.join(","));
  api.openChain("step_equity_value");
  check("再放大一跳就接到净债务", api.chainOf("step_equity_value").includes("net_debt"), api.chainOf("step_equity_value").join(","));

  api.openChain("revenue_path");   // 链里有逐年行才该出表：两层窗口装得下它
  const pathChain = api.chainOf("revenue_path");
  check("链里有逐年行就摊完整表", pathChain.includes("revenue_growth_path") && (html().match(/<table class="series">/g) || []).length >= 1,
    `${pathChain.join(",")} / ${(html().match(/<table class="series">/g) || []).length} 张表`);

  api.openChain("revenue_growth_path");   // 链尾是逐年序列：没有代入式，但必须给完整表
  check("逐年序列摊成表", html().includes('<table class="series">') && html().includes("逐年"), html().match(/逐年<\/b>[^（]*（[^）]*）/)?.[0]);
  check("这一跳没有半条代入式", !html().includes("<b>代入</b>"));
  check("逐年表里也不含 NaN", !html().includes("NaN"));

  api.openChain("wacc");
  api.write("beta", 1.2);
  for (let i = 0; i < 80 && Math.abs((api.state.att.beta?.current || 0) - 1.2) > 1e-9; i++) await sleep(100);
  check("放大态里改数照样重算", Math.abs(api.state.att.beta.current - 1.2) < 1e-9, String(api.state.att.beta?.current));
  check("推导段跟着重算换数", html().includes("1.20"), (html().match(/贝塔[\s\S]{0,120}/) || [""])[0].replace(/\n/g, ""));
  check("整图也照旧重画", byId("graph").innerHTML.length > 1000 && byId("chain").innerHTML.length > 500, `图 ${byId("graph").innerHTML.length} 字符`);

  api.closeChain();
  check("收起后浮层撤掉、背景解锁", api.state.chain === "" && api.state.trail.length === 0
    && byId("chain-overlay").classList.contains("hide") && sandbox.document.body.style.overflow === "");
  check("收起后整图仍在", !byId("board").classList.contains("hide"));
  check("收起后侧栏还停在那一行", byId("detail").innerHTML.includes("放大这条链") && api.state.selected === "wacc");
  check("收起后地址栏干净", !store.url.includes("chain="), store.url);

  // 三条退路都得验到：Esc、点暗处、顶上那个返回按钮——用户点名要「除了 Esc 还得有返回按钮」。
  api.openChain("wacc");
  keyHandlers.forEach((fn) => fn({ key: "Escape" }));
  check("Esc 关得掉", api.state.chain === "" && byId("chain-overlay").classList.contains("hide"));
  api.openChain("wacc");
  byId("chain-back").fire("click");
  check("返回按钮关得掉", api.state.chain === "" && byId("chain-overlay").classList.contains("hide"));
  api.openChain("wacc");
  byId("chain-overlay").fire("click", { target: { id: "graph-scroll" } });
  check("点浮层里的板子不会误关", api.state.chain === "wacc");
  byId("chain-overlay").fire("click", { target: { id: "chain-overlay" } });
  check("点浮层外的暗处也关得掉", api.state.chain === "" && byId("chain-overlay").classList.contains("hide"));

  // ------------------------------------------------------------ 取数台只有一份，两页都接上
  const common = fs.readFileSync(DIR + "common.js", "utf8");
  const panel = fs.readFileSync(DIR + "panel.html", "utf8");
  const deskFiles = { "common.js": common, "panel.html": panel, "derivation.html": page };
  const mentions = (name) => Object.entries(deskFiles).filter(([, text]) => text.includes(name)).map(([f]) => f);
  check("取数机器只写在一处：fetchValuation", mentions("async function fetchValuation").join() === "common.js",
    mentions("async function fetchValuation").join(","));
  check("取数机器只写在一处：showFreshness", mentions("async function showFreshness").join() === "common.js",
    mentions("async function showFreshness").join(","));
  check("取数机器只写在一处：refreshOptions", mentions("async function refreshOptions").join() === "common.js",
    mentions("async function refreshOptions").join(","));
  check("面板接上取数台", panel.includes("configureFetchDesk(") && panel.includes("bindFetchControls()"));
  for (const id of ["benchmark", "fetch", "refetch", "freshness"]) {
    check(`推导图顶栏有 #${id}`, new RegExp(`id="${id}"`).test(page.slice(0, page.indexOf("<main>"))));
  }
  check("推导图接上取数台", page.includes("configureFetchDesk(") && page.includes("bindFetchControls()")
    && page.includes("syncBenchmark(data)"));
  check("推导图响应带这批的基准", Boolean(api.state.data?.benchmark?.code), JSON.stringify(api.state.data?.benchmark || {}));
  check("下拉显示的是这批实际用的基准", byId("benchmark").value === api.state.data.benchmark.code,
    `${byId("benchmark").value} vs ${api.state.data.benchmark.code}`);

  // 切换＝读盘：找一个已预取多个基准的新批次，把下拉换成集合内的另一家，看那一行怎么说。
  const probe = async (run, benchmark) =>
    (await (await fetch(`${BASE}/api/valuation_status?code=${CODE}&run=${run}&benchmark=${benchmark}`)).json());
  const runs = (options.codes.find((c) => c.code === CODE) || {}).runs || [];
  const listed = options.benchmarks.map((b) => b.code);
  let swap = null;
  for (const r of runs) {
    const base = await probe(r.run_id, "");
    const target = (base.benchmark?.prefetched || []).find((c) => c !== base.benchmark?.snapshot?.code) || "";
    if (!target) continue;
    const withTarget = await probe(r.run_id, target);
    if (withTarget.benchmark?.switch) { swap = { run: r.run_id, target, base, probe: withTarget }; break; }
  }
  check("找到一个预取过多基准的批次", Boolean(swap), runs.map((r) => r.run_id).join(","));
  if (swap) {
    // 「不联网」只在别的来源都不缺时才是答复；数据一跨过收盘，这批本来就整个要重取，
    // 那时集合内换基准该不该联网由别的来源说了算。这里钉的是换基准这一跳本身：switch 为真、
    // 不被记成「基准指数」过期、并且不把需重取的清单变长。
    const asSet = (p) => (p.stale_groups || []).join(",");
    check("集合内换基准会被认成可切换", swap.probe.benchmark.switch === true
      && swap.probe.benchmark.prefetched.includes(swap.target), JSON.stringify(swap.probe.benchmark));
    check("集合内换基准不会被标成「基准指数」过期", !asSet(swap.probe).includes("基准指数"), asSet(swap.probe));
    check("换到集合内的基准不让需重取的清单变长", asSet(swap.base) === asSet(swap.probe)
      && (swap.base.action !== "replay" || swap.probe.action === "replay"),
      `${asSet(swap.base) || "无"} ${swap.base.action} → ${asSet(swap.probe) || "无"} ${swap.probe.action}`);
    check("理由交代会另开批次", swap.probe.action !== "replay" || swap.probe.reason.includes("另开一个批次"), swap.probe.reason);
    api.state.run = swap.run;
    byId("benchmark").value = swap.target;
    await api.showFreshness();
    check("顶栏那一行说「换成它不联网」", byId("freshness").textContent.includes("不联网")
      && byId("freshness").textContent.includes("另开一个批次"), byId("freshness").textContent);
  }
  // 集合外的代码仍然要联网，理由得点名缺的是指数那条腿，而不是让人去核对个股代码。
  // 哪一个批次算「集合外」不写死：盘上的批次一批比一批预取得全，这里自己去找到还缺一家的那一批。
  let outside = null;
  for (const r of runs) {
    const base = await probe(r.run_id, "");
    const have = base.benchmark?.prefetched || [];
    const target = listed.find((c) => !have.includes(c));
    if (!target) continue;
    const withTarget = await probe(r.run_id, target);
    outside = { run: r.run_id, target, have, probe: withTarget };
    break;
  }
  if (outside) {
    check("找到一批预取集合装不下全部下拉项的批次", outside.have.length >= 1 && !outside.have.includes(outside.target),
      `${outside.run} 缺 ${outside.target}`);
    check("集合外的基准仍然要联网", outside.probe.action === "fetch" && outside.probe.benchmark.switch === false
      && (outside.probe.stale_groups || []).includes("基准指数"),
      `${outside.probe.action} ${(outside.probe.stale_groups || []).join(",")}`);
    check("联网那一行的理由点名缺的是指数", outside.probe.reason.includes("收盘序列"), outside.probe.reason);
  } else {
    check("每一批都预取了全部下拉项，集合外那三条无从验", true, `下拉 ${listed.length} 家`);
  }
  api.state.run = RUN;
  byId("benchmark").value = api.state.data.benchmark.code;

  console.log(`\n失败 ${bad} 项`);
  process.exit(bad ? 1 : 0);
})();
