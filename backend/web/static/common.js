// Shared across the panel, search and derivation pages. Everything here is a plain global on purpose:
// the tool runs off file://-free local Flask with no build step, so a <script src> is the module system.
const KIND = { data: "取数", estimate: "预估", convention: "惯例", derived: "级联", engine: "引擎步骤" };
const $ = (id) => document.getElementById(id);
const num = (v) => (typeof v === "number" ? v : null);
const esc = (text) => String(text ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
// 1.6829 / 100 lands on 0.016829000000000002, which would read as a human edit that never happened.
const round2 = (v) => (typeof v === "number" && Number.isFinite(v) ? Number(v.toPrecision(12)) : v);

function fmt(value, unit) {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "boolean") return value ? "是" : "否";
  if (typeof value === "string") return value;
  // Year-map rows carry one value per year; the baseline column shows the whole path, as the CLI does.
  if (typeof value === "object") {
    const years = Object.keys(value).sort();
    if (!years.length) return "—";
    return years.map((year) => `${year}:${fmt(value[year], unit)}`).join(" ");
  }
  if (unit === "pct") return (value >= 0 ? "+" : "") + (value * 100).toFixed(2) + "%";
  if (Math.abs(value) >= 1000) return value.toLocaleString("zh-CN", { maximumFractionDigits: 2, minimumFractionDigits: 2 });
  return value.toFixed(2);
}
function signed(value, unit = "pct") {
  if (value === null || value === undefined) return '<span class="dim">—</span>';
  const cls = value > 0 ? "pos" : value < 0 ? "neg" : "dim";
  return `<span class="${cls}">${fmt(value, unit)}</span>`;
}

// Percent rows are edited in percent units: a human types 2.5, the engine stores 0.025.
function asPct(row) { return row.unit === "pct"; }
function toValue(row, raw) { return round2(asPct(row) ? Number(raw) / 100 : Number(raw)); }
function displayValue(row) { return asPct(row) && num(row.current) !== null ? row.current * 100 : row.current; }

// 金额行的框是「相比机器基线 ±%」，换算成绝对值才发给引擎——manual.json、归因列和 CLI 的 --overrides
// 一律存绝对值，两处口径不能分叉。基线为 0 或负时百分比没有意义（0 乘任何倍还是 0），退回绝对值框。
const DELTA_EPS = 1e-9;
function modeOf(row, base) {
  const value = base === undefined ? row.baseline : base;
  return row.edit === "delta" && typeof value === "number" && Number.isFinite(value) && value > 0 ? "delta" : "value";
}
function toAbsolute(row, base, raw, mode) {
  const value = Number(raw);
  if (!Number.isFinite(value)) return null;
  return mode === "delta" ? round2(base * (1 + value / 100)) : toValue(row, raw);
}
function toBox(row, base, value, mode) {
  if (value === null || value === undefined) return "";
  return mode === "delta" ? round2((value / base - 1) * 100) : round2(asPct(row) ? value * 100 : value);
}
function boxClass(row, mode) { return "num" + (mode === "delta" ? " delta" : asPct(row) ? " pct" : ""); }
// ±% 框里的 0 就是「没改」；数值框要等换算完才判得出，否则浮点残差会把改动行数虚报。
function isChanged(raw, value, base, mode) {
  return mode === "delta" ? Math.abs(Number(raw)) >= DELTA_EPS : value !== base;
}

// 代入式：后端只下发「哪个符号是图上哪一行」（`{行 key}` 占位符），填数这一步留在浏览器里做，
// 用的还是唯一的 fmt()——所以式子里的数字与该行的显示值同源，页面不为画图再算一遍算术，
// 也就不可能与引擎的口径分叉。
// 任一格取不到标量（那一行不在图上、是逐年序列、是文本行）就整条不出，退回符号式：
// 半截式子或一个 NaN 比不说更糟。
function substituteText(sub, read) {
  if (!sub || !sub.template) return "";
  let missing = false;
  const out = String(sub.template).replace(/\{([a-z0-9_]+)\}/g, (_, key) => {
    const text = read(key) || "";
    if (!text) missing = true;
    return text;
  });
  return missing ? "" : out;
}

// 「ERP 窗口」下拉：面板与推导图共用一套判据。选项由后端下发（那四行实测里能用的窗口），选中一档
// 就是把它的实测数写进级联行——走的是既有的「人工修正」通路（recompute + 保存修正 → manual.json 的
// overrides.erp），不新增批次也不联网。manual.json 里存的是数不是窗口名，所以当前选中项由数反推。
// 负超额／越界的窗口后端已标成 usable=false，这里不再重复那套区间判断，也就不可能与护栏分叉。
const ERP_EPS = 1e-9;
function erpWindowChoice(windows, value) {
  if (num(value) === null) return null;
  return (windows || []).find((w) => w.usable && num(w.value) !== null && Math.abs(w.value - value) <= ERP_EPS) || null;
}
function erpWindowMarkup(windows, override) {
  const usable = (windows || []).filter((w) => w.usable && num(w.value) !== null);
  const parts = ['<option value="">级联所取（不修正）</option>'];
  for (const w of usable) parts.push(`<option value="${w.value}">${esc(w.label)} ${fmt(w.value, "pct")}</option>`);
  const custom = num(override) !== null && !erpWindowChoice(usable, override);
  if (custom) parts.push(`<option value="custom">自定义 ${fmt(override, "pct")}</option>`);
  return {html: parts.join(""), hit: erpWindowChoice(usable, override), custom, usable: usable.length};
}
function renderErpWindow(select, windows, override) {
  const built = erpWindowMarkup(windows, override);
  if (select.dataset.sig !== built.html) {
    select.innerHTML = built.html;
    select.dataset.sig = built.html;
  }
  select.value = built.hit ? String(built.hit.value) : built.custom ? "custom" : "";
  // 一个可用窗口都没有时（四档实测全在可用区间之外）下拉是空的，留着只会让人以为没加载出来。
  select.disabled = !built.usable;
  select.title = select.disabled
    ? "四个窗口没有一个落在可用区间里（负超额不是风险定价），所以没有可选的窗口：要按别的数定价就换市场基准重新取数，或直接改级联那一行"
    : "把这一档窗口的实测超额写进级联行——与在表里改「股权风险溢价（级联采用）」那一行是同一件事，"
      + "保存后写进 manual.json 的 overrides.erp，CLI 复现时就是同一份修正。越出可用区间的窗口不在这里出现，"
      + "那一档的实测数仍摆在参考行里看得见";
}

// 功能条是 flex-wrap 的，窄窗口下会换行变高；表头写死 top 会被它盖住，所以量出来喂给 CSS 变量。
// 两条触发都要：ResizeObserver 跟窗口，MutationObserver 跟内容（批次下拉、状态提示都会让功能条换行）。
// MutationObserver 不是多余的一路——取数往往在后台标签页跑，那时渲染暂停，ResizeObserver 不投递回调。
function stickHeader() {
  const header = document.querySelector("header");
  if (!header) return;
  const apply = () => document.documentElement.style.setProperty("--header-h", `${header.offsetHeight}px`);
  apply();
  if (window.ResizeObserver) new ResizeObserver(apply).observe(header);
  else window.addEventListener("resize", apply);
  if (window.MutationObserver) new MutationObserver(apply).observe(header, { childList: true, characterData: true, subtree: true });
}

// ---------------------------------------------------------------- 取数台（面板与推导图共用）
// 「换一个市场基准、按一个按钮判断要不要联网、起作业再轮询进度」是页面级的机器，与表格还是图无关，
// 所以只写一遍。页面各自提供两件事：state（正在看哪只票的哪一批）与 reload（作业跑完怎么把新批次读回来）。
let options = { codes: [], benchmarks: [] };
let fetchDesk = { reload: async () => {} };
const configureFetchDesk = (hooks) => Object.assign(fetchDesk, hooks);
const entryFor = (code) => options.codes.find((c) => c.code === code);
const typedCode = () => $("code").value.replace(/[^0-9]/g, "").slice(0, 6);
// 推导图那一页没有 --beta 的逃生阀输入，所以这两处读数都要容许元素缺席。
const benchValue = () => (($("benchmark") || {}).value || "");
const betaValue = () => (($("fetchbeta") || {}).value || "").trim();

// 有全收益孪生的基准，ERP 的市场腿直接取全收益指数；没有的那批退回「价格指数 + 股息率」。下拉上标出来，
// 用户就不用记住哪几个代码归中证官网发孪生。
const benchmarkText = (b) => `${esc(b.name || b.code)}${b.total_return ? " · 全收益" : ""}`;
// 两页共用一句说明，免得一处改了另一处还教人「换基准要联网」。
const BENCHMARK_HINT = "β 与股权风险溢价共用的那个指数（等同 CLI 的 --benchmark）：两处「市场」必须是同一个东西。"
  + "标「全收益」者，ERP 的市场腿直接取它的中证全收益指数孪生；没有孪生的退回「价格指数 + 乐咕两市股息率」。"
  + "下拉显示的是这一批快照实际用的基准。一次取数已把下拉里每个基准的收盘序列都存进同一份凭证，"
  + "所以换成下拉里的另一个基准不联网——按下只会另开一个批次复现；要换成下拉之外的代码才需要重新取数。";

async function refreshOptions() {
  options = await (await fetch("/api/options")).json();
  $("codes").innerHTML = options.codes.map((c) => `<option value="${c.code}">${esc(c.name || "")}</option>`).join("");
  const select = $("benchmark");
  if (!select) return;
  select.innerHTML = (options.benchmarks || []).map((b) => `<option value="${b.code}">${benchmarkText(b)}</option>`).join("");
  if (options.benchmark_default) select.value = options.benchmark_default.code;
  select.title = BENCHMARK_HINT;
}

// 下拉显示的是「这一批快照实际用的基准」，读凭证而不是本机默认值——显示错了会让人以为老批次是新口径。
// 只在批次换了时改写选中值：用户切了基准还没取数，中途一次普通重算不该把他的选择吞回去。
let benchmarkSync = "";
function syncBenchmark(data) {
  const select = $("benchmark");
  if (!select) return;
  const shown = data.benchmark || {};
  if (!shown.code) return;
  if (![...select.options].some((o) => o.value === shown.code)) select.add(new Option(benchmarkText(shown), shown.code));
  if (benchmarkSync !== data.run_id) {
    select.value = shown.code;
    benchmarkSync = data.run_id;
  }
}

// 后台按分源阈值（报价=当日收盘、行情=新交易周、三表=披露截止日、一致预期与 rf=30 天）决定这次是
// 联网重取还是复现快照，所以按钮旁这一行要先说清「上一批还作不作数」，否则点下去没反应像坏了。
async function showFreshness() {
  const box = $("freshness");
  if (!box) return;
  if (!state.code) { box.textContent = ""; $("refetch").hidden = true; return; }
  const query = new URLSearchParams({ code: state.code, run: state.run, beta: betaValue(), benchmark: benchValue() });
  let out;
  try { out = await (await fetch("/api/valuation_status?" + query)).json(); }
  catch (exc) { box.textContent = "新鲜度判断失败：" + exc; return; }
  if (out.error) { box.textContent = "✗ " + out.error; return; }
  if (out.blocked) {
    // 名录里没有这只票：这一次根本不会联网，新旧无从谈起，所以不谈阈值，只说查无此票和出路在哪。
    // 按钮不置灰——按下去由 /api/fetch 把同一句全文回回来，比一个没有说明的死按钮好读。
    box.textContent = "✗ 查无此票：" + out.blocked;
    box.title = out.reason;
    $("refetch").hidden = true;
    return;
  }
  const age = out.age_hours === null ? "" : ` · ${out.age_hours < 24 ? out.age_hours.toFixed(1) + " 小时前" : Math.round(out.age_hours / 24) + " 天前"}取数`;
  box.textContent = out.benchmark.switch
    ? `◇ 换成 ${out.benchmark.requested.name} 不联网（这批已预取它的序列）· 按下另开一个批次复现`
    : out.action === "replay"
      ? `○ ${out.run_id} 未过期${age} · 报告期 ${out.as_of}，按下只复现快照`
      : `● ${out.run_id ? out.run_id + " " : ""}需重取${out.stale_groups.length ? "：" + out.stale_groups.join("、") : ""}${age}`;
  box.title = out.reason;
  $("refetch").hidden = out.action !== "replay" || !out.run_id;
}

async function fetchValuation(force = false) {
  const raw = $("code").value.trim();
  if (!raw) { $("status").textContent = "请先填写 6 位股票代码"; return; }
  // 两个按钮一起置灰：作业期间「强制重取」按下去只会撞上后台那句「已有一个作业在跑」，让它可点就是让人点一个不会联的按钮。
  $("fetch").disabled = $("refetch").disabled = true;
  const started = Date.now();
  let verb = "取数";
  const say = (text) => { $("status").textContent = `${verb} ${raw}：${text}（${((Date.now() - started) / 1000).toFixed(0)} 秒）`; };
  let job;
  try {
    const response = await fetch("/api/fetch", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code: raw, run: state.run, beta: betaValue(), benchmark: benchValue(), force }) });
    const out = await response.json();
    if (!response.ok) { $("status").textContent = "✗ " + out.error; $("fetch").disabled = $("refetch").disabled = false; return; }
    state.code = out.code || raw;
    $("code").value = state.code;
    if (out.replayed) {
      // 后台判定数据仍是最新的：不排队、不联网，把这一批重新折一遍就是本次估值。
      await fetchDesk.reload();
      await showFreshness();
      $("status").textContent = `✓ 数据未过期，已复现 ${out.run_id} 快照`;
      $("fetch").disabled = $("refetch").disabled = false;
      return;
    }
    job = out.job_id;
    if (out.action === "replay") {
      // 换的是这批已经预取过的基准：作业只读磁盘，一次网也不联，但会写出一个新批次。
      verb = "复现";
      say(`已排队复现 ${out.replayed_from}`);
    } else {
      say(out.stale_groups.length ? "已排队（" + out.stale_groups.join("、") + "）" : "已排队");
    }
  } catch (exc) {
    $("status").textContent = "✗ 请求失败: " + exc;
    $("fetch").disabled = $("refetch").disabled = false;
    return;
  }
  const timer = setInterval(async () => {
    // 一次轮询的网络抖动不该废掉正在跑的取数，所以只忽略失败的那一轮。
    let status;
    try { status = await (await fetch("/api/job?job=" + encodeURIComponent(job))).json(); }
    catch (exc) { return; }
    if (status.error) return finish("✗ " + status.error);
    if (status.state === "done") return finish(`✓ 基线已写出 ${status.run_id}（${verb}用时 ${(status.elapsed_ms / 1000).toFixed(1)} 秒）`);
    if (status.state === "failed") return finish(`✗ 卡在「${status.step}」：${status.error}`);
    say(status.step);

    async function finish(final) {
      clearInterval(timer);
      $("fetch").disabled = $("refetch").disabled = false;
      await refreshOptions();
      fillRuns();
      await fetchDesk.reload();
      await showFreshness();
      $("status").textContent = final;
    }
  }, 700);
}

// 换基准不改算式里的任何一行，它改的是「这一批数据是对哪个市场测的」，所以这里只重新判断新旧，
// 让那一行立刻变成「需重取：基准指数」或「换成它不联网」，而不是等用户按下按钮才发现没联网。
function bindFetchControls() {
  $("fetch").addEventListener("click", () => fetchValuation());
  $("refetch").addEventListener("click", () => fetchValuation(true));
  $("benchmark").addEventListener("change", showFreshness);
  $("fetchbeta")?.addEventListener("input", showFreshness);
}
