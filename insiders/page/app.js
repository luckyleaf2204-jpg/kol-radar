/* KOL-RADAR INSIDERS — dashboard (read-only + PAPER). Data: /api/insiders/watch every 20 s, /api/insiders/chart,
   /api/insiders/wallet. Nothing here signs or sends a transaction. */
"use strict";
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const LS = {
  get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch (_) { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (_) { /* private mode */ } },
  del(k) { try { localStorage.removeItem(k); } catch (_) { /* ignore */ } },
};
const S = {
  code: LS.get("insAccessCode", ""), w: null, names: {}, tab: "dashboard", timer: null, prevSig: {}, charts: {}, coins: {},
  sig: { win: LS.get("kr.win", "24h"), multi: LS.get("kr.multi", false), sort: LS.get("kr.sort", "time"), open: null },
  al: { level: "all", view: LS.get("kr.alview", "trade") }, wl: { mode: "list", role: "all", sort: "wins", open: null, detail: null },
  pp: { tab: "shadow" }, seenAlert: LS.get("kr.seenAlert", 0), sound: LS.get("kr.sound", true), err: null,
};
const TABS = [["dashboard", "Dashboard"], ["signals", "Tín hiệu sớm"], ["alerts", "Cảnh báo"], ["wallets", "Nhóm ví"],
  ["paper", "PAPER Trading"], ["performance", "Hiệu suất"], ["research", "Nghiên cứu"], ["settings", "Cài đặt"]];
const WIN = { "5m": 300, "15m": 900, "1h": 3600, "24h": 86400, all: 1e12 };
const ROLE = { holder: ["VÍ GỐC", "bad"], successor: ["VÍ KẾ THỪA", "warn"], child: ["VÍ CON", "acc"], other: ["KHÁC", ""] };
const TIER = { main: ["main", "good", "≥ 2 coin thắng"], watch: ["watch", "warn", "1 coin thắng"], none: ["chưa thắng", "", "chưa có coin thắng"] };
const KIND = { BUY: "mua", CREATE: "tạo + mua", RECEIVE: "nhận token", SELL: "bán", SEND: "chuyển token", SOL_IN: "nhận SOL", FUND: "nạp ví mới", VIPSTRONG: "nhiều nhóm cùng vào" };
const EXIT = { tp2x: "chốt ×2", stop: "cắt lỗ −35%", trail: "trailing", time30: "hết 30 phút", time60: "hết 60 phút", wallet_exit: "ví tín hiệu bán", crash: "sập giá", end: "hết 24 giờ" };

// ---------------------------------------------------------------- formatting
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const short = (a) => a ? a.slice(0, 4) + "…" + a.slice(-4) : "";
const nf = (v, d = 2) => v === null || v === undefined || isNaN(v) ? null : Number(v).toLocaleString("vi-VN", { maximumFractionDigits: d });
const solTxt = (v, d = 2) => nf(v, d) === null ? `<span class="unk">—</span>` : `${nf(v, d)} SOL`;
const usdK = (v) => { if (!v && v !== 0) return null; const a = Math.abs(v); return (v < 0 ? "−" : "") + "$" + (a >= 1e9 ? (a / 1e9).toFixed(2) + "B" : a >= 1e6 ? (a / 1e6).toFixed(2) + "M" : a >= 1e3 ? (a / 1e3).toFixed(1) + "K" : a.toFixed(a < 1 ? 4 : 0)); };
const money = (v) => v === null || v === undefined ? `<span class="unk">—</span>` : `<span class="${v > 0 ? "good" : v < 0 ? "bad" : ""}">${v > 0 ? "+" : v < 0 ? "−" : ""}$${Math.abs(Math.round(v)).toLocaleString("vi-VN")}</span>`;
const pct = (v) => v === null || v === undefined ? `<span class="unk">—</span>` : `<span class="${v > 0 ? "good" : v < 0 ? "bad" : ""}">${v > 0 ? "+" : ""}${v.toFixed(1)}%</span>`;
const mult = (v) => v === null || v === undefined ? `<span class="unk">—</span>` : `<span class="${v >= 2 ? "good" : v < 1 ? "bad" : ""}">×${nf(v, 2)}</span>`;
const now = () => (S.w && S.w.now) || Date.now() / 1000;
function dur(s) {
  if (s === null || s === undefined || isNaN(s)) return "—";
  s = Math.max(0, Math.round(s));
  if (s < 60) return s + " giây"; if (s < 3600) return Math.round(s / 60) + " phút";
  if (s < 86400) return (s / 3600).toFixed(s < 36000 ? 1 : 0) + " giờ"; return (s / 86400).toFixed(1) + " ngày";
}
const ago = (ts) => ts ? dur(now() - ts) + " trước" : "—";
const clock = (ts) => ts ? new Date(ts * 1000).toLocaleTimeString("vi-VN", { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "—";
const when = (ts) => ts ? new Date(ts * 1000).toLocaleString("vi-VN", { hour: "2-digit", minute: "2-digit", day: "2-digit", month: "2-digit" }) : "—";
const acct = (a, label) => a ? `<a class="addr" href="https://solscan.io/account/${esc(a)}" target="_blank" rel="noopener" title="${esc(a)}">${esc(label || short(a))}</a><span class="copy" data-copy="${esc(a)}" title="Sao chép">⧉</span>` : "—";
const tokName = (c) => c.symbol ? "$" + c.symbol : (S.names[c.mint] || short(c.mint));
const tokLinks = (m, chart) => `<span class="links"><a href="${esc(safeUrl(chart) || "https://dexscreener.com/solana/" + m)}" target="_blank" rel="noopener">DexScreener</a><a href="https://pump.fun/coin/${esc(m)}" target="_blank" rel="noopener">pump.fun</a><a href="https://solscan.io/token/${esc(m)}" target="_blank" rel="noopener">Solscan</a></span>`;
function safeUrl(u) { try { const x = new URL(u); return x.protocol === "https:" ? x.href : ""; } catch (_) { return ""; } }
const pill = (txt, cls = "", title = "") => `<span class="pill ${cls}"${title ? ` title="${esc(title)}"` : ""}>${esc(txt)}</span>`;
const rolePill = (r) => pill(...(ROLE[r] || ROLE.other));
const tierPill = (t) => { const x = TIER[t] || TIER.none; return pill(x[0], x[1], x[2]); };
function table(cols, rows, opts = {}) {
  // cols: [label, cls?, sortKey?]; rows: [{key, cls, cells: [html...]}]
  if (!rows.length) return `<div class="empty">${esc(opts.empty || "Chưa có dữ liệu.")}</div>`;
  const head = cols.map(([l, c, k]) => `<th class="${c || ""}${k ? " sort" + (opts.sort === k ? " on" : "") : ""}"${k ? ` data-sort="${k}"` : ""}>${esc(l)}</th>`).join("");
  const body = rows.map((r) => `<tr data-key="${esc(r.key || "")}" data-sig="${esc(r.sig || "")}" class="${r.cls || ""}">${r.cells.map((h, i) =>
    `<td class="${cols[i][1] || ""}" data-l="${esc(cols[i][0])}">${h}</td>`).join("")}</tr>`).join("");
  return `<div class="tbl"><table class="cards"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}

// ---------------------------------------------------------------- API + boot
async function api(path) {
  const r = await fetch(path, { headers: { "X-Access-Code": S.code }, cache: "no-store" });
  if (r.status === 401) { showLogin("Sai mã truy cập"); throw new Error("auth"); }
  if (!r.ok) throw new Error("http " + r.status);
  return r.json();
}
function toast(msg) { const el = $("#toast"); el.textContent = msg; el.classList.add("on"); clearTimeout(el._t); el._t = setTimeout(() => el.classList.remove("on"), 2400); }
function showLogin(err) {
  clearInterval(S.timer);
  document.body.innerHTML = `<div class="login"><div class="brand">KOL-RADAR <span>INSIDERS</span></div>
    <div class="muted">Nhập mã truy cập của site (chỉ cần một lần trên thiết bị này).</div>
    <input id="code" type="password" placeholder="Mã truy cập" autocomplete="current-password">
    <button class="btn primary" id="go">Vào</button><div class="bad small">${esc(err || "")}</div></div><div id="toast" class="toast"></div>`;
  const go = async () => { S.code = $("#code").value.trim(); try { await api("/api/insiders/watch"); LS.set("insAccessCode", S.code); location.reload(); } catch (_) { /* shown */ } };
  $("#go").onclick = go; $("#code").onkeydown = (e) => { if (e.key === "Enter") go(); };
}
async function refresh() {
  try {
    const d = await api("/api/insiders/watch");
    S.w = d.watch; S.names = d.tickers || {}; S.err = null;
  } catch (e) { if (e.message === "auth") return; S.err = "Không tải được dữ liệu (" + e.message + "). Sẽ thử lại."; }
  renderTop(); render(); notifyNew();
}
function start() {
  S.tab = (location.hash || "#dashboard").slice(1);
  if (!TABS.some(([k]) => k === S.tab)) S.tab = "dashboard";
  refresh();
  S.timer = setInterval(() => { if (!document.hidden) refresh(); }, 20000);
  window.addEventListener("hashchange", () => { S.tab = (location.hash || "#dashboard").slice(1); renderTop(); render(true); });
}

// ---------------------------------------------------------------- derived data
const vip = () => (S.w && S.w.vip) || {};
const pf = () => (S.w && S.w.portfolio) || {};
const solUsd = () => pf().sol_usd || 0;
const board = () => (S.w && S.w.board) || [];
const shadow = () => (S.w && S.w.shadow) || { summary: [], rows: [] };
const alerts = () => ((S.w && S.w.alerts) || []).filter((a) => ["vip", "vipstrong"].includes(a.level));
const walletInfo = () => { const m = {}; (vip().list || []).forEach((x) => { m[x.wallet] = x; }); return m; };
const famOf = (w) => (walletInfo()[w] || {}).family || w;
function tradePx(tr, coin) {          // [ts, kind, sol, tokens_raw, sig] -> SOL per token, MC (USD) at the trade
  const sup = coin.supply || {}, ui = tr[3] / Math.pow(10, sup.decimals ?? 6), px = tr[2] > 0 && ui > 0 ? tr[2] / ui : null;
  return { ts: tr[0], kind: tr[1], sol: tr[2], ui, px, mc: px && sup.ui && solUsd() ? px * sup.ui * solUsd() : null };
}
function coinState(c) {
  if (c.n_families >= 2) return ["Nhiều nhóm cùng vào", "bad"];
  if (!c.created_ts || !c.supply) return ["Cần xác minh", "warn"];
  return ["Tín hiệu đơn", ""];
}
function coinMc(c) {                  // estimate: live price x supply, else the last trade's price x supply
  if (c.mcap_est) return { v: c.mcap_est, est: true };
  const last = c.wallets.flatMap((w) => w.trades).filter((t) => t[2] > 0).sort((a, b) => b[0] - a[0])[0];
  const p = last ? tradePx(last, c).mc : null;
  return { v: p, est: true };
}

// ---------------------------------------------------------------- header
function renderTop() {
  const v = vip(), src = (S.w && S.w.sources) || {};
  $("#status").innerHTML = v.connected ? `<span class="pill good"><span class="dot"></span>ĐANG NGHE ${v.subscribed || 0} VÍ</span>`
    : `<span class="pill bad"><span class="dot"></span>MẤT KẾT NỐI${v.error ? " · đang nối lại" : ""}</span>`;
  $("#updated").textContent = S.w ? "Cập nhật " + clock(S.w.now) : "Đang tải…";
  const newAlerts = alerts().filter((a) => a.ts > S.seenAlert).length;
  $("#tabs").innerHTML = TABS.map(([k, l]) => `<a href="#${k}" class="${S.tab === k ? "on" : ""}">${esc(l)}${k === "alerts" && newAlerts ? `<span class="badge">${newAlerts}</span>` : ""}</a>`).join("");
  const name = { stream: "Luồng giao dịch", balances: "Số dư ví", prices: "Nguồn giá" };
  const chips = Object.entries(src).map(([k, s]) => {
    if (!s.ok) return pill(`${name[k]}: ${s.error ? "lỗi" : "chưa có dữ liệu"}`, s.error ? "bad" : "", s.error || "");
    if (s.error) return pill(`${name[k]}: lỗi tạm thời`, "warn", s.error);
    return "";
  }).join("");
  $("#srcs").innerHTML = (S.err ? pill(S.err, "bad") : "") + chips;
}

// ---------------------------------------------------------------- render dispatch (keeps scroll, flashes changed rows)
function render(fromNav) {
  const main = $("#main");
  if (!S.w) { main.innerHTML = `<div class="empty">${esc(S.err || "Đang tải…")}</div>`; return; }
  const y = window.scrollY;
  const fn = { dashboard: viewDashboard, signals: viewSignals, alerts: viewAlerts, wallets: viewWallets, paper: viewPaper,
    performance: viewPerformance, research: viewResearch, settings: viewSettings }[S.tab] || viewDashboard;
  main.innerHTML = fn();
  if (!fromNav) window.scrollTo(0, y); else window.scrollTo(0, 0);
  const seen = S.prevSig[S.tab] || {}, next = {};
  $$("tr[data-key]", main).forEach((tr) => {
    const k = tr.dataset.key; if (!k) return; next[k] = tr.dataset.sig;
    if (!fromNav && seen[k] !== undefined && seen[k] !== tr.dataset.sig) tr.classList.add("flash");
    if (!fromNav && seen[k] === undefined && Object.keys(seen).length) tr.classList.add("flash");
  });
  S.prevSig[S.tab] = next;
  if (S.tab === "alerts") { const top = Math.max(0, ...alerts().map((a) => a.ts || 0)); if (top > S.seenAlert) { S.seenAlert = top; LS.set("kr.seenAlert", top); } }
  bind();
}

// ---------------------------------------------------------------- 1. dashboard
function viewDashboard() {
  const v = vip(), sh = shadow(), day = now() - 86400;
  const sig24 = (sh.rows || []).filter((x) => x.ts > day).length;
  const strong24 = alerts().filter((a) => a.level === "vipstrong" && a.ts > day).length;
  const k = (key, val, sub, cls = "") => `<div class="kpi ${cls}"><span class="k">${key}</span><span class="v">${val}</span><span class="s">${sub}</span></div>`;
  const kpis = `<div class="kpis">
    ${k("Ví đang theo dõi", `${v.subscribed || 0}<span class="dim">/${v.wallets || 0}</span>`, `${v.base || 0} ví gốc · ${v.children || 0} ví con`)}
    ${k("Giao dịch đã xử lý", nf(v.handled, 0) ?? "0", `${v.missed || 0} bị sót · ${v.reconnects || 0} lần nối lại`)}
    ${k("Tín hiệu mới (24 giờ)", sig24, `tổng ${(sh.rows || []).length} tín hiệu đã ghi`)}
    ${k("Cảnh báo mạnh (24 giờ)", strong24, "≥ 2 nhóm ví độc lập cùng vào", strong24 ? "alert" : "")}</div>`;
  const all = (sh.summary || []).find((r) => r.tier === "all" && r.kind === "single") || {};
  const strong = (sh.summary || []).find((r) => r.tier === "all" && r.kind === "strong") || {};
  const perfRow = (lbl, r) => r.n ? `<tr><td>${lbl}</td><td class="num">${r.paper_n || 0}</td><td class="num">${r.paper_n ? Math.round(100 * r.paper_win / r.paper_n) + "%" : "—"}</td><td class="num">${money(r.paper_n ? r.paper_usd : null)}</td><td class="num">${money(r.paper_n ? r.paper_usd_wo_best : null)}</td></tr>` : `<tr><td>${lbl}</td><td class="num" colspan="4"><span class="unk">chưa có mẫu</span></td></tr>`;
  const flows = alerts().filter((a) => a.mint).slice(0, 10).map((a) => ({
    key: a.sig + a.kind + a.wallet, sig: a.sol, cls: a.level === "vipstrong" ? "strong" : "",
    cells: [`<span class="muted small">${ago(a.ts)}</span>`, acct(a.wallet), `${esc(KIND[a.kind] || a.kind)} <b>${esc(a.symbol ? "$" + a.symbol : S.names[a.mint] || short(a.mint))}</b>`, `<span class="mono">${nf(a.sol, 2)}</span>`] }));
  return `${kpis}
    <section class="panel"><header><h2>Tín hiệu coin sớm</h2><span class="grow"></span><a href="#signals">Xem tất cả →</a></header>${signalTable(sortedBoard().slice(0, 8), true)}</section>
    <div class="grid2">
      <section class="panel"><header><h2>Dòng tiền gần nhất</h2><span class="note">giao dịch ≥ ${(S.w.config || {}).min_sol || 4} SOL của ví theo dõi</span></header>
        ${table([["Lúc"], ["Ví", "addr"], ["Coin"], ["SOL", "num"]], flows, { empty: "Chưa có giao dịch nào đạt ngưỡng." })}</section>
      <section class="panel"><header><h2>Hiệu suất PAPER</h2><span class="grow"></span><a href="#performance">Chi tiết →</a></header>
        <div class="tbl"><table class="cards"><thead><tr><th>Loại</th><th class="num">Lệnh đã đóng</th><th class="num">Tỷ lệ thắng</th><th class="num">P&amp;L sau phí</th><th class="num">Bỏ lệnh tốt nhất</th></tr></thead>
        <tbody>${perfRow("Tín hiệu 1 ví", all)}${perfRow("Nhiều nhóm ví", strong)}</tbody></table></div>
        <div class="note">Shadow paper $${sh.usd || 500}/tín hiệu, trượt giá giả định ${Math.round((sh.slip || 0.03) * 100)}% mỗi chiều. Mẫu nhỏ: chưa đủ để kết luận có lợi thế.</div></section>
    </div>`;
}

// ---------------------------------------------------------------- 2. early signals
function sortedBoard() {
  const lim = WIN[S.sig.win] || WIN["24h"], t = now();
  let b = board().filter((c) => t - (c.last_ts || 0) <= lim && (!S.sig.multi || c.n_families >= 2));
  const key = { sol: (c) => -(c.buy_sol || 0), groups: (c) => -(c.n_families || 0) * 1e6 - (c.buy_sol || 0), time: (c) => -(c.first_ts || 0) }[S.sig.sort] || ((c) => -(c.first_ts || 0));
  return b.slice().sort((a, b2) => key(a) - key(b2));
}
function signalTable(rows, compact) {
  return table([["Token"], ["Market cap", "num"], ["SOL mua", "num", compact ? null : "sol"], ["Số nhóm ví", "num", compact ? null : "groups"], ["Tuổi coin", "num"], ["Trạng thái"], ["Lần đầu", "num", compact ? null : "time"]],
    rows.map((c) => {
      const st = coinState(c), mc = coinMc(c);
      return { key: c.mint, sig: [c.buy_sol, c.n_families, c.wallets.length].join("|"), cls: "click" + (c.n_families >= 2 ? " strong" : "") + (S.sig.open === c.mint ? " sel" : ""),
        cells: [`<span class="tok" data-open="${esc(c.mint)}"><b>${esc(tokName(c))}</b> <span class="muted small">${esc(c.name || "")}</span></span>`,
          mc.v ? `<span class="est">${usdK(mc.v)}</span>` : `<span class="unk">—</span>`,
          `<b class="mono">${nf(c.buy_sol, 2)}</b>${c.sell_sol ? `<div class="small muted">bán ${nf(c.sell_sol, 2)}</div>` : ""}`,
          `<b class="mono">${c.n_families}</b><span class="dim small"> / ${c.wallets.length} ví</span>`,
          c.created_ts ? dur(now() - c.created_ts) : `<span class="unk">chưa rõ</span>`, pill(st[0], st[1]),
          `<span class="small">${ago(c.first_ts)}</span>`] };
    }), { empty: "Chưa có coin nào trong khung thời gian này. Coin hiện ra khi ví theo dõi mua tổng ≥ " + ((S.w.config || {}).board_min_sol || 4) + " SOL.", sort: S.sig.sort });
}
function viewSignals() {
  const seg = (k, opts, cur) => `<span class="seg">${opts.map(([v, l]) => `<button data-${k}="${v}" class="${cur === v ? "on" : ""}">${esc(l)}</button>`).join("")}</span>`;
  const rows = sortedBoard(), open = rows.find((c) => c.mint === S.sig.open) || board().find((c) => c.mint === S.sig.open);
  return `<section class="panel"><header><h2>Tín hiệu coin sớm</h2><span class="grow"></span>
      <span class="ctrl">${seg("win", [["5m", "5 phút"], ["15m", "15 phút"], ["1h", "1 giờ"], ["24h", "24 giờ"], ["all", "Tất cả"]], S.sig.win)}
      <label class="chk"><input type="checkbox" id="multi" ${S.sig.multi ? "checked" : ""}> Chỉ token có ≥ 2 nhóm ví</label>
      <select id="sigsort"><option value="time"${S.sig.sort === "time" ? " selected" : ""}>Mới xuất hiện trước</option><option value="groups"${S.sig.sort === "groups" ? " selected" : ""}>Nhiều nhóm ví trước</option><option value="sol"${S.sig.sort === "sol" ? " selected" : ""}>Dòng SOL lớn trước</option></select></span></header>
    <div class="note">Mỗi token một dòng. “Số nhóm ví” đếm nhóm độc lập: ví cha và ví con, hoặc ví gốc và ví kế thừa của nó, chỉ tính một nhóm. Nhiều lệnh nhỏ từ cùng một nhóm không tương đương nhiều nhóm cùng mua. <span class="est"></span> = ước tính.</div>
    ${signalTable(rows, false)}
    ${open ? coinDetail(open) : `<div class="note">Bấm một token để xem biểu đồ và từng ví tham gia.</div>`}</section>`;
}
function walletStats(w, c) {           // buys / sells / remaining of one wallet on one coin, from its recorded trades
  const dec = (c.supply || {}).decimals ?? 6, u = (raw) => raw / Math.pow(10, dec), su = solUsd(), sup = (c.supply || {}).ui;
  let bSol = 0, bTok = 0, sSol = 0, sTok = 0, first = null, recv = 0;
  for (const tr of w.trades || []) {
    if ((tr[1] === "BUY" || tr[1] === "CREATE") && tr[2] > 0) { bSol += tr[2]; bTok += tr[3]; first = first || tr; }
    else if (tr[1] === "RECEIVE") recv += tr[3];
    else if (tr[1] === "SELL" && tr[2] > 0) { sSol += tr[2]; sTok += tr[3]; }
  }
  const bPx = bTok ? bSol / u(bTok) : null, sPx = sTok ? sSol / u(sTok) : null;
  const mc = (px) => px && sup && su ? px * sup * su : null;
  const left = w.tok_in || w.tok_out ? u(Math.max(0, (w.tok_in || 0) - (w.tok_out || 0))) : null;
  return { bSol, sSol, bPx, sPx, bMc: mc(bPx), sMc: mc(sPx), firstMc: first ? mc(first[2] / u(first[3])) : null, firstTs: first ? first[0] : null,
    recv: recv ? u(recv) : 0, left, leftPct: w.tok_in ? Math.min(100, 100 * Math.max(0, w.tok_in - (w.tok_out || 0)) / w.tok_in) : null,
    held: w.held_ui, usdPx: (px) => px && su ? px * su : null };
}
function walletTable(c) {
  const info = walletInfo();
  const fmtPx = (px, st) => px ? `<span class="mono small">$${Number(st.usdPx(px) || 0).toPrecision(3)}</span><div class="dim small">${px.toExponential(2)} SOL</div>` : `<span class="unk">—</span>`;
  const rows = c.wallets.map((w) => {
    const st = walletStats(w, c), role = w.role || (info[w.wallet] || {}).role;
    return { key: w.wallet, sig: [w.buy_sol, w.sell_sol, w.tok_out, w.held_ui].join("|"),
      cells: [`${acct(w.wallet)}<div>${role ? rolePill(role) : ""} ${tierPill(w.tier)}</div><div class="dim small">nhóm ${esc(short(w.family))}</div>`,
        `<b class="mono">${nf(st.bSol, 3)}</b>${st.recv ? `<div class="small">+ nhận ${nf(st.recv, 0)} token</div>` : ""}<div class="dim small">${when(st.firstTs)}</div>`,
        fmtPx(st.bPx, st),
        st.bMc ? `<span class="est">${usdK(st.bMc)}</span>${st.firstMc && Math.abs(st.firstMc - st.bMc) / st.bMc > 0.05 ? `<div class="dim small">lần đầu ${usdK(st.firstMc)}</div>` : ""}` : `<span class="unk">—</span>`,
        st.sSol ? `<b class="mono">${nf(st.sSol, 3)}</b>` : `<span class="dim">chưa bán</span>`, fmtPx(st.sPx, st),
        st.sMc ? `<span class="est">${usdK(st.sMc)}</span>` : `<span class="unk">—</span>`,
        st.left !== null ? `<b class="mono">${nf(st.left, 0)}</b><div class="small ${st.leftPct >= 50 ? "good" : "warn"}">${Math.round(st.leftPct)}% lượng đã nhận</div>` : `<span class="unk">—</span>`,
        st.held === null || st.held === undefined ? `<span class="unk">đang cập nhật</span>` : `<span class="mono">${nf(st.held, 0)}</span>`] };
  });
  return table([["Ví", "addr"], ["Mua (SOL)", "num"], ["Giá mua TB", "num"], ["MC lúc mua", "num"], ["Bán (SOL)", "num"], ["Giá bán TB", "num"],
    ["MC lúc bán", "num"], ["Token còn lại (theo lệnh)", "num"], ["Đang giữ (số dư ví)", "num"]], rows);
}
function coinDetail(c) {
  const st = coinState(c), mc = coinMc(c), info = walletInfo();
  const rows = c.wallets.flatMap((w) => w.trades.map((tr) => ({ w, t: tradePx(tr, c) }))).sort((a, b) => a.t.ts - b.t.ts).map(({ w, t: x }) => ({
    key: x.ts + w.wallet + x.kind, sig: "", cells: [`<span class="small">${when(x.ts)}</span>`, acct(w.wallet), `<span class="mono small">${esc(short(w.family))}</span> ${tierPill(w.tier)}`,
      `<span class="${["BUY", "CREATE", "RECEIVE"].includes(x.kind) ? "good" : "warn"}">${esc(KIND[x.kind] || x.kind)}</span>`, `<span class="mono">${x.sol ? nf(x.sol, 3) : "—"}</span>`,
      x.px ? `<span class="mono small">${x.px.toExponential(2)}</span>` : `<span class="unk">—</span>`, x.mc ? `<span class="est">${usdK(x.mc)}</span>` : `<span class="unk">—</span>`,
      c.created_ts ? dur(x.ts - c.created_ts) : `<span class="unk">—</span>`] }));
  const held = walletTable(c);
  return `<div class="detail"><header class="ctrl"><h3>${esc(tokName(c))} <span class="muted small">${esc(c.name || "")}</span></h3><span class="copy" data-copy="${esc(c.mint)}">⧉ CA</span>${pill(st[0], st[1])}<span class="grow"></span><button class="btn" data-close="sig">Đóng</button></header>
    <div class="facts"><div class="fact"><span class="k">Market cap</span><span class="v">${mc.v ? `<span class="est">${usdK(mc.v)}</span>` : "—"}</span></div>
      <div class="fact"><span class="k">Tuổi coin</span><span class="v">${c.created_ts ? dur(now() - c.created_ts) : "chưa rõ"}</span></div>
      <div class="fact"><span class="k">Tổng SOL mua / bán</span><span class="v">${nf(c.buy_sol, 2)} / ${nf(c.sell_sol, 2)}</span></div>
      <div class="fact"><span class="k">Nhóm ví độc lập</span><span class="v">${c.n_families} (${c.wallets.length} ví)</span></div>
      <div class="fact"><span class="k">Liên kết</span><span class="v">${tokLinks(c.mint, c.chart)}</span></div></div>
    <div class="chart" id="chart" data-mint="${esc(c.mint)}"><div class="muted small">Đang tải biểu đồ…</div></div>
    <div class="legend"><span><span style="color:var(--accent)">▲</span> ví mua / nhận</span><span><span style="color:var(--warn)">▼</span> ví bán / chuyển đi</span><span>trục dọc: market cap (USD, thang log)</span></div>
    <h2>Từng ví</h2>${held}
    <h2>Từng lệnh</h2>
    ${table([["Lúc"], ["Ví", "addr"], ["Nhóm / hạng"], ["Lệnh"], ["SOL", "num"], ["Giá (SOL)", "num"], ["MC lúc đó", "num"], ["Sau mở bán", "num"]], rows)}</div>`;
}
async function drawChart() {
  const el = $("#chart"); if (!el) return;
  const mint = el.dataset.mint, c = board().find((x) => x.mint === mint); if (!c) return;
  let hit = S.charts[mint];
  if (!hit || Date.now() - hit.t > 60000) {
    try { hit = { t: Date.now(), d: await api("/api/insiders/chart?mint=" + encodeURIComponent(mint)) }; S.charts[mint] = hit; }
    catch (_) { if (!hit) { el.innerHTML = `<div class="muted small">Không tải được biểu đồ (GeckoTerminal). Sẽ thử lại.</div>`; return; } }
  }
  if ($("#chart") !== el) return;
  const sup = (c.supply || {}).ui, cs = (hit.d.candles || []).filter((k) => k[4] > 0);
  if (!cs.length || !sup) { el.innerHTML = `<div class="muted small">Chưa có dữ liệu nến cho coin này.</div>`; return; }
  const trades = c.wallets.flatMap((w) => w.trades.map((tr) => ({ ...tradePx(tr, c), wallet: w.wallet }))).filter((x) => x.mc);
  const W = 900, H = 300, L = 60, R = 10, T = 10, B = 24;
  const t0 = Math.min(cs[0][0], ...trades.map((x) => x.ts)), t1 = Math.max(cs[cs.length - 1][0] + 60 * (hit.d.agg || 1), ...trades.map((x) => x.ts));
  const vals = cs.flatMap((k) => [k[2] * sup, k[3] * sup]).concat(trades.map((x) => x.mc)).filter((v) => v > 0);
  let lo = Math.log10(Math.min(...vals) * 0.9), hi = Math.log10(Math.max(...vals) * 1.1); if (hi - lo < 0.3) { lo -= 0.15; hi += 0.15; }
  const X = (ts) => L + (W - L - R) * (ts - t0) / Math.max(1, t1 - t0), Y = (v) => T + (H - T - B) * (1 - (Math.log10(v) - lo) / (hi - lo));
  const cw = Math.max(1, Math.min(8, (W - L - R) / cs.length * 0.7));
  let s = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="Biểu đồ market cap">`;
  for (let k = Math.ceil(lo); k <= Math.floor(hi); k++) for (const f of [1, 3]) { const v = f * 10 ** k; if (Math.log10(v) < lo || Math.log10(v) > hi) continue; s += `<line x1="${L}" x2="${W - R}" y1="${Y(v)}" y2="${Y(v)}" class="c-grid"/><text x="4" y="${Y(v) + 4}" class="c-ax">${usdK(v)}</text>`; }
  for (let i = 0; i <= 4; i++) { const ts = t0 + (t1 - t0) * i / 4; s += `<text x="${X(ts) - (i === 4 ? 40 : i ? 20 : 0)}" y="${H - 6}" class="c-ax">${new Date(ts * 1000).toLocaleTimeString("vi-VN", { hour: "2-digit", minute: "2-digit" })}</text>`; }
  for (const k of cs) { const up = k[4] >= k[1], x = X(k[0]), y1 = Y(Math.max(k[1], k[4]) * sup), y2 = Y(Math.min(k[1], k[4]) * sup);
    s += `<line x1="${x}" x2="${x}" y1="${Y(k[2] * sup)}" y2="${Y(k[3] * sup)}" class="${up ? "c-up" : "c-dn"}"/><rect x="${x - cw / 2}" y="${y1}" width="${cw}" height="${Math.max(1, y2 - y1)}" class="${up ? "c-upf" : "c-dnf"}"/>`; }
  for (const x of trades) { const px = X(x.ts), py = Y(x.mc), buy = ["BUY", "CREATE", "RECEIVE"].includes(x.kind);
    const pts = buy ? `${px},${py - 9} ${px - 6},${py + 3} ${px + 6},${py + 3}` : `${px},${py + 9} ${px - 6},${py - 3} ${px + 6},${py - 3}`;
    s += `<polygon points="${pts}" class="${buy ? "c-buy" : "c-sell"}"><title>${esc(short(x.wallet))} · ${esc(KIND[x.kind] || x.kind)} ${x.sol ? x.sol.toFixed(2) + " SOL" : ""} · MC ${usdK(x.mc)} · ${clock(x.ts)}</title></polygon>`; }
  el.innerHTML = s + `</svg><div class="small muted">MC hiện tại ≈ <b>${usdK(cs[cs.length - 1][4] * sup)}</b> · ${cs.length} nến ${hit.d.agg || 1} phút (GeckoTerminal)</div>`;
}

// ---------------------------------------------------------------- 3. live alerts
const TRADE_KINDS = ["BUY", "CREATE", "SELL", "VIPSTRONG"], MOVE_KINDS = ["RECEIVE", "SEND", "SOL_IN", "FUND"];
function alertStatus(a) {
  const k = (x) => [x.wallet, x.mint || x.from || x.to, x.kind].join("|");
  const older = alerts().find((b) => b !== a && k(b) === k(a) && b.ts <= a.ts && a.ts - b.ts <= 120 && b.sig !== a.sig);
  return a.ts > S.seenAlert ? pill("MỚI", "acc") : older ? pill("Có thể trùng", "warn", "Cùng ví, cùng coin, cùng loại trong 2 phút") : pill("Đã ghi nhận");
}
const coinCell = (a) => `<b>${esc(a.symbol ? "$" + a.symbol : S.names[a.mint] || short(a.mint))}</b> <span class="copy" data-copy="${esc(a.mint)}">⧉</span>`
  + `<div class="small muted">${a.age_h !== undefined && a.age_h !== null ? dur(a.age_h * 3600) + " tuổi" : ""}${a.mcap ? " · MC " + usdK(a.mcap) : ""}</div>`;
function viewAlerts() {
  const cfg = S.w.config || {}, all = alerts(), view = S.al.view || "trade";
  const trades = all.filter((a) => TRADE_KINDS.includes(a.kind)), moves = all.filter((a) => MOVE_KINDS.includes(a.kind));
  const fresh = (xs) => xs.filter((a) => a.ts > S.seenAlert).length;
  const tabs = `<span class="seg">${[["trade", "Mua / bán coin", trades], ["move", "Chuyển / nhận", moves]].map(([v, l, xs]) =>
    `<button data-alview="${v}" class="${view === v ? "on" : ""}">${l} <span class="dim">${xs.length}</span>${fresh(xs) ? ` <span class="badge">${fresh(xs)}</span>` : ""}</button>`).join("")}</span>`;
  let body;
  if (view === "trade") {
    const list = trades.filter((a) => S.al.level === "all" || a.level === S.al.level);
    const strongBlocks = list.filter((a) => a.level === "vipstrong").slice(0, 10).map((a, n) => {
      const c = S.coins[a.mint] || board().find((x) => x.mint === a.mint);
      loadCoin(a.mint);
      return `<details class="detail" ${n === 0 ? "open" : ""}><summary class="ctrl">${pill("MẠNH", "bad")} <b>${esc(a.symbol ? "$" + a.symbol : S.names[a.mint] || short(a.mint))}</b>
          <span class="muted small">${a.n_families || a.n_wallets || 2} nhóm ví · ${(c ? c.wallets.length : (a.holders || []).length) || "?"} ví · ${ago(a.ts)}${a.mcap ? " · MC " + usdK(a.mcap) : ""}${a.age_h !== undefined && a.age_h !== null ? " · " + dur(a.age_h * 3600) + " tuổi" : ""}</span></summary>
        <div class="ctrl"><span class="copy" data-copy="${esc(a.mint)}">⧉ CA</span>${tokLinks(a.mint, a.chart)}</div>
        ${c ? walletTable(c) : `<div class="muted small">Đang tải các ví…</div>`}</details>`;
    }).join("");
    const rows = list.slice(0, 150).map((a) => {
      const strong = a.level === "vipstrong", buy = a.kind !== "SELL";
      return { key: a.sig + a.kind + a.wallet, sig: "", cls: strong ? "strong" : "",
        cells: [`<span class="small">${when(a.ts)}</span><div class="dim small">${ago(a.ts)}</div>`,
          strong ? pill("MẠNH · " + (a.n_families || a.n_wallets || 2) + " nhóm", "bad") : pill(buy ? (a.kind === "CREATE" ? "TẠO + MUA" : "MUA") : "BÁN", buy ? "good" : "warn"),
          coinCell(a), `<b class="mono">${nf(a.sol, 2)}</b>`,
          strong && (a.holders || []).length ? (a.holders || []).map((h) => `<div>${acct(h)}</div>`).join("") : acct(a.wallet),
          alertStatus(a)] };
    });
    body = `<div class="ctrl"><span class="seg">${[["all", "Tất cả"], ["vipstrong", "Chỉ cảnh báo mạnh"], ["vip", "Chỉ 1 ví"]].map(([v, l]) => `<button data-allevel="${v}" class="${S.al.level === v ? "on" : ""}">${l}</button>`).join("")}</span></div>
      <div class="note">Ví theo dõi mua hoặc bán coin ≥ ${cfg.min_sol || 4} SOL. MẠNH: ≥ 2 nhóm ví độc lập (ví cha–con, ví gốc–kế thừa tính một nhóm) cùng mua/nhận một coin, mỗi ví ≥ ${cfg.min_sol || 4} SOL, trong ${dur(cfg.strong_window_s || 86400)}. Bấm vào cảnh báo mạnh để mở chi tiết từng ví.</div>
      ${strongBlocks}
      ${table([["Lúc"], ["Lệnh"], ["Coin"], ["SOL", "num"], ["Ví", "addr"], ["Trạng thái"]], rows, { empty: "Chưa có lệnh mua / bán nào đạt ngưỡng." })}`;
  } else {
    const label = { RECEIVE: ["NHẬN TOKEN", "acc"], SEND: ["CHUYỂN TOKEN", "warn"], SOL_IN: ["NHẬN SOL", "acc"], FUND: ["NẠP VÍ MỚI", "warn"] };
    const rows = moves.slice(0, 150).map((a) => {
      const out = a.kind === "SEND" || a.kind === "FUND", other = out ? a.to : a.from;
      const flow = out ? `${acct(a.wallet)} <span class="dim">→</span> ${other ? acct(other) : `<span class="unk">không rõ</span>`}`
        : `${other ? acct(other) : `<span class="unk">không rõ</span>`} <span class="dim">→</span> ${acct(a.wallet)}`;
      return { key: a.sig + a.kind + a.wallet, sig: "",
        cells: [`<span class="small">${when(a.ts)}</span><div class="dim small">${ago(a.ts)}</div>`, pill(...(label[a.kind] || [a.kind, ""])), flow,
          a.mint ? coinCell(a) : `<span class="mono">SOL</span>`, `<b class="mono">${nf(a.sol, 2)}</b>`, alertStatus(a)] };
    });
    body = `<div class="note">Tiền và token chuyển qua lại: ví theo dõi nhận / chuyển token (giá trị quy ra SOL theo báo giá), nhận SOL từ ví ngoài danh sách, hoặc nạp SOL cho ví mới (ví mới nhận ≥ ${cfg.adopt_min_sol || 0.5} SOL tự được theo dõi thành ví con). SOL nhận từ một ví khác trong danh sách, hoặc tiền bán coin, không bị báo lại ở đây.</div>
      ${table([["Lúc"], ["Loại"], ["Từ → đến", "addr"], ["Token"], ["Giá trị (SOL)", "num"], ["Trạng thái"]], rows, { empty: "Chưa có lần chuyển / nhận nào đạt ngưỡng." })}`;
  }
  return `<section class="panel"><header><h2>Cảnh báo trực tiếp</h2><span class="grow"></span>${tabs}<button class="btn" id="notif">Bật thông báo</button></header>${body}</section>`;
}

// ---------------------------------------------------------------- 4. wallets
function viewWallets() {
  const info = walletInfo(), hold = {}; (pf().wallets || []).forEach((x) => { hold[x.wallet] = x; });
  const seen = vip().last_seen || {}, su = solUsd();
  let list = (vip().list || []).filter((x) => S.wl.role === "all" || x.role === S.wl.role);
  const sk = { wins: (x) => -(x.wins || 0) * 1e9 - ((hold[x.wallet] || {}).sol || 0), sol: (x) => -((hold[x.wallet] || {}).sol || 0), last: (x) => -(seen[x.wallet] || 0) }[S.wl.sort];
  list = list.slice().sort((a, b) => sk(a) - sk(b));
  const rows = list.map((x) => { const h = hold[x.wallet];
    return { key: x.wallet, sig: [h && h.sol, seen[x.wallet]].join("|"), cls: "click" + (S.wl.open === x.wallet ? " sel" : ""),
      cells: [`<span data-wallet="${esc(x.wallet)}">${acct(x.wallet)}</span>`, rolePill(x.role), `<b class="mono">${x.wins || 0}</b>`, tierPill(x.tier),
        h ? `<span class="mono">${nf(h.sol, 2)}</span>` : `<span class="unk">đang cập nhật</span>`,
        h ? ((h.tokens || []).slice(0, 3).map((t) => pill(`${t.symbol ? "$" + t.symbol : short(t.mint)} ${nf(t.sol, 1)}`)).join(" ") || `<span class="dim small">không giữ coin &gt; ${pf().min_sol || 1} SOL</span>`) + (h.tokens_usd && su ? `<div class="small muted">≈ ${nf(h.tokens_usd / su, 1)} SOL</div>` : "") : `<span class="unk">—</span>`,
        `<span class="small">${ago(seen[x.wallet])}</span>`] }; });
  const tree = relationTree(list.length ? vip().list : []);
  return `<section class="panel"><header><h2>Nhóm ví</h2><span class="grow"></span>
      <span class="seg">${[["list", "Danh sách"], ["tree", "Sơ đồ quan hệ"]].map(([v, l]) => `<button data-wlmode="${v}" class="${S.wl.mode === v ? "on" : ""}">${l}</button>`).join("")}</span>
      <select id="wlrole">${[["all", "Mọi vai trò"], ["holder", "Ví gốc"], ["successor", "Ví kế thừa"], ["child", "Ví con"]].map(([v, l]) => `<option value="${v}"${S.wl.role === v ? " selected" : ""}>${l}</option>`).join("")}</select>
      <select id="wlsort">${[["wins", "Nhiều coin thắng"], ["sol", "Số dư SOL lớn"], ["last", "Hoạt động gần nhất"]].map(([v, l]) => `<option value="${v}"${S.wl.sort === v ? " selected" : ""}>${l}</option>`).join("")}</select></header>
    <div class="note">Hạng: <b>main</b> ≥ 2 coin thắng, <b>watch</b> 1 coin thắng (thắng = giữ ≥ ${Math.round(((S.w.config || {}).win_hold_s || 600) / 60)} phút trước khi bán nửa, lãi ≥ ${(S.w.config || {}).win_min_sol || 0.5} SOL và ≥ +${Math.round((((S.w.config || {}).win_min_mult || 1.2) - 1) * 100)}%). Quan hệ giữa các ví là <b>mối liên hệ on-chain</b> (chuyển tiền / token), không khẳng định cùng một người.</div>
    ${S.wl.mode === "list" ? table([["Ví", "addr"], ["Vai trò"], ["Coin thắng", "num"], ["Hạng"], ["Số dư SOL", "num"], ["Coin đang giữ"], ["Hoạt động"]], rows, { empty: "Chưa có danh sách ví." }) : tree}
    ${S.wl.open ? walletDetail() : ""}</section>`;
}
function relationTree(list) {
  const kids = {}; list.forEach((x) => { if (x.parent && x.role !== "holder") (kids[x.parent] = kids[x.parent] || []).push(x); });
  const node = (x, depth) => {
    const ch = kids[x.wallet] || [];
    const label = `${acct(x.wallet)} ${rolePill(x.role)} ${tierPill(x.tier)}${x.funded_sol ? ` <span class="dim">nhận ${nf(x.funded_sol, 2)} SOL</span>` : ""} <a href="#wallets" data-wallet="${esc(x.wallet)}">chi tiết</a>`;
    if (!ch.length || depth > 6) return `<div class="leaf">${label}</div>`;
    return `<details ${depth < 1 ? "open" : ""}><summary>${label} <span class="dim">· ${ch.length} ví liên hệ</span></summary>${ch.map((c) => node(c, depth + 1)).join("")}</details>`;
  };
  const roots = list.filter((x) => x.role === "holder");
  const orphans = list.filter((x) => x.role !== "holder" && !list.some((y) => y.wallet === x.parent));
  return `<div class="tree">${roots.map((r) => node(r, 0)).join("")}${orphans.length ? `<div class="note">Ví có ví nguồn nằm ngoài danh sách theo dõi:</div>${orphans.map((o) => node(o, 1)).join("")}` : ""}</div>`;
}
function walletDetail() {
  const d = S.wl.detail;
  if (!d || d.wallet !== S.wl.open) return `<div class="detail"><div class="muted">Đang tải chi tiết ví…</div></div>`;
  if (d.error) return `<div class="detail"><div class="bad">${esc(d.error)}</div></div>`;
  const h = d.holding;
  const live = (d.coins || []).map((c) => ({ key: c.mint, sig: "", cells: [`<b>${esc(c.symbol ? "$" + c.symbol : S.names[c.mint] || short(c.mint))}</b> ${tokLinks(c.mint)}`,
    `<span class="mono">${nf(c.buy_sol, 2)}</span>`, `<span class="mono">${nf(c.sell_sol, 2)}</span>`, c.half_s !== undefined && c.half_s !== null ? dur(c.half_s) : `<span class="unk">chưa bán nửa</span>`, `<span class="small">${when(c.first_ts)}</span>`] }));
  const hist = (d.history || []).map((x) => { const tr = (x.trades || []).slice().sort((a, b) => a[0] - b[0]);
    let inn = 0, out = 0, t0 = null, half = null; for (const [ts, side, , tk] of tr) { if (side === "buy" || side === "in") { inn += tk; t0 = t0 ?? ts; } else if (t0 !== null) { out += tk; if (half === null && inn && out >= inn / 2) half = ts - t0; } }
    return { key: x.mint, sig: "", cells: [`<b>${esc(x.ticker || short(x.mint))}</b>`, x.status === "ok" ? `<span class="mono">${nf(x.spent_sol, 2)} → ${nf(x.received_sol, 2)}</span>` : `<span class="unk">${esc(x.status)}</span>`,
      x.net_sol === null || x.net_sol === undefined ? `<span class="unk">—</span>` : `<b class="${x.net_sol > 0 ? "good" : "bad"}">${x.net_sol > 0 ? "+" : ""}${nf(x.net_sol, 2)} SOL</b>`,
      half !== null ? dur(half) : `<span class="unk">${t0 ? "chưa bán nửa" : "—"}</span>`, `<span class="small">${when(x.first_ts)}</span>`] }; });
  return `<div class="detail"><header class="ctrl"><h3>${acct(d.wallet, d.wallet)}</h3>${rolePill(d.role)}${tierPill(d.tier)}<span class="grow"></span><button class="btn" data-close="wl">Đóng</button></header>
    <div class="facts"><div class="fact"><span class="k">Vì sao theo dõi</span><span class="v small">${esc(d.why || "—")}</span></div>
      <div class="fact"><span class="k">Ví nguồn (liên hệ on-chain)</span><span class="v">${d.parent ? acct(d.parent) : "—"}</span></div>
      <div class="fact"><span class="k">Nhóm</span><span class="v">${esc(short(d.family))}</span></div>
      <div class="fact"><span class="k">Số dư</span><span class="v">${h ? nf(h.sol, 2) + " SOL" : "đang cập nhật"}</span></div>
      <div class="fact"><span class="k">Hoạt động gần nhất</span><span class="v">${ago(d.last_seen)}</span></div></div>
    ${(d.children || []).length || (d.successors || []).length ? `<div class="small">Ví nhận tiền từ ví này: ${(d.successors || []).map((a) => acct(a)).concat((d.children || []).map((c) => acct(c.wallet) + ` <span class="dim">${nf(c.sol, 2)} SOL</span>`)).join(" · ")}</div>` : ""}
    ${h && (h.tokens || []).length ? `<div class="small">Đang giữ: ${(h.tokens || []).map((t) => `${pill((t.symbol ? "$" + t.symbol : short(t.mint)) + " " + nf(t.sol, 2) + " SOL")}`).join(" ")}</div>` : ""}
    <h2>Giao dịch ghi nhận khi theo dõi trực tiếp</h2>${table([["Coin"], ["Mua (SOL)", "num"], ["Bán (SOL)", "num"], ["Giữ tới khi bán nửa", "num"], ["Lần đầu", "num"]], live, { empty: "Chưa có giao dịch nào từ khi theo dõi." })}
    <h2>Lịch sử trên các coin của kênh (nghiên cứu)</h2>${table([["Coin"], ["Chi → thu (SOL)", "num"], ["P&L", "num"], ["Giữ tới khi bán nửa", "num"], ["Mua lần đầu", "num"]], hist, { empty: "Không có trong dữ liệu nghiên cứu (ví con hoặc ví kế thừa)." })}</div>`;
}
async function loadCoin(mint) {          // every listed wallet on a coin (any size), refreshed at most every 30 s
  const t = Date.now();
  S.coinAt = S.coinAt || {};
  if (S.coinAt[mint] && t - S.coinAt[mint] < 30000) return;
  S.coinAt[mint] = t;
  try { S.coins[mint] = await api("/api/insiders/coin?mint=" + encodeURIComponent(mint)); if (S.tab === "alerts") render(); }
  catch (_) { /* keep the board's copy */ }
}
async function loadWallet(addr) {
  S.wl.open = addr; S.wl.detail = null; render();
  try { S.wl.detail = await api("/api/insiders/wallet?addr=" + encodeURIComponent(addr)); }
  catch (e) { S.wl.detail = { wallet: addr, error: e.message === "http 404" ? "Ví này không còn trong danh sách theo dõi." : "Không tải được chi tiết ví." }; }
  render();
}

// ---------------------------------------------------------------- 5. paper
function viewPaper() {
  const sh = shadow(), slip = sh.slip || 0.03, ru = sh.rules || {}, src = ((S.w.sources || {}).prices) || {};
  const rule = `Chốt nửa ở ×${ru.tp || 2} (phần còn lại dời stop về hoà vốn) · cắt lỗ −${Math.round((1 - (ru.stop || 0.65)) * 100)}% · trailing ${Math.round((ru.trail || 0.3) * 100)}% sau khi chốt nửa · thoát sau ${Math.round((ru.soft_s || 1800) / 60)} phút nếu chưa ×${ru.soft_mult || 1.5} · tối đa ${Math.round((ru.hard_s || 3600) / 60)} phút · thoát khi ví tín hiệu bán nửa · thoát khi sập giá`;
  const open = (sh.rows || []).filter((x) => x.paper && x.paper.status === "open").map((x) => {
    const m = x.entry && x.last ? x.last / x.entry : null, est = m ? (sh.usd || 500) * (m * (1 - slip) / (1 + slip)) - (sh.usd || 500) : null;
    return { key: "o" + x.id, sig: x.last, cells: [`<b>${esc(x.symbol ? "$" + x.symbol : short(x.mint))}</b>${tokLinks(x.mint)}`, `<span class="small">${when(x.ts)}</span>`, `<span class="mono small">$${Number(x.entry).toPrecision(3)}</span>`,
      `<span class="mono small">$${x.last ? Number(x.last).toPrecision(3) : "—"}</span>`, `${pct(m ? (m - 1) * 100 : null)}<div class="small"><span class="est">${money(est)}</span></div>`,
      `${pct(x.dip !== null && x.dip !== undefined ? (x.dip - 1) * 100 : null)}`, `<span class="small">${x.paper.half ? "đã chốt nửa, stop hoà vốn" : "chờ ×2 / −35% / 30 phút"}</span>`] }; });
  const closed = (sh.rows || []).filter((x) => x.paper && x.paper.status === "closed").map((x) => ({ key: "c" + x.id, sig: "",
    cells: [`<b>${esc(x.symbol ? "$" + x.symbol : short(x.mint))}</b>`, `${x.kind === "strong" ? pill("nhiều nhóm", "bad") : pill("1 ví")} ${tierPill(x.tier)}<div class="small dim">${(x.wallets || []).slice(0, 2).map((w) => short(w.wallet) + " " + nf(w.sol, 1) + " SOL").join(", ")}</div>`,
      `<span class="small">${when(x.ts)}</span>`, `<span>${esc(EXIT[x.paper.exit] || x.paper.exit || "—")}</span>`, dur(x.paper.hold_s), money(x.paper.pnl_usd),
      `${mult(x.max && x.max["1h"])} / ${mult(x.max && x.max["24h"])}`] }));
  const op = (S.w.paper || {}), osum = op.summary || {};
  const old = (op.rows || []).map((p) => ({ key: p.mint, sig: p.now, cells: [`<b>${esc(S.names[p.mint] || short(p.mint))}</b>${tokLinks(p.mint)}`, `<span class="small">${when(p.opened)}</span>`, `${pill(p.status === "open" ? "đang giữ" : p.status === "closed" ? "đã đóng" : "không có route", p.status === "open" ? "acc" : "")}`,
    money(p.entry_value !== null && p.entry_value !== undefined ? p.entry_value - p.usd : null), ...["15m", "1h", "6h", "24h"].map((k) => money(p.marks && p.marks[k] !== undefined && p.marks[k] !== null ? p.marks[k] - p.usd : null)), money(p.now !== null && p.now !== undefined ? p.now - p.usd : null)] }));
  const tabs = `<span class="seg">${[["shadow", "Shadow · mọi tín hiệu"], ["strong", "Cảnh báo mạnh · báo giá Jupiter"]].map(([v, l]) => `<button data-pptab="${v}" class="${S.pp.tab === v ? "on" : ""}">${l}</button>`).join("")}</span>`;
  const status = src.error ? pill("Nguồn giá: lỗi tạm thời", "warn", src.error) : src.last ? pill("Nguồn giá Jupiter: " + ago(src.last), "good") : pill("Nguồn giá: chưa có dữ liệu");
  const body = S.pp.tab === "shadow" ? `
    <div class="note">${esc(rule)}. Mỗi tín hiệu $${sh.usd || 500}, trượt giá giả định ${Math.round(slip * 100)}% mỗi chiều. Giá Jupiter lấy mỗi 30 giây. P&amp;L lệnh đang mở là <span class="est">ước tính</span> theo giá gần nhất.</div>
    <h2>Vị thế đang mở</h2>${table([["Token"], ["Vào lúc"], ["Giá vào", "num"], ["Giá hiện tại", "num"], ["P&L", "num"], ["Giảm lớn nhất", "num"], ["Quy tắc thoát"]], open, { empty: "Không có vị thế nào đang mở." })}
    <h2>Lịch sử</h2>${table([["Token"], ["Lý do vào"], ["Vào lúc"], ["Lý do thoát"], ["Thời gian giữ", "num"], ["P&L sau phí", "num"], ["Tăng tối đa 1h / 24h", "num"]], closed, { empty: "Chưa có lệnh nào đóng." })}`
    : `<div class="note">Mỗi cảnh báo MẠNH mở một lệnh $${osum.usd || 500} theo báo giá thật của Jupiter cho đúng cỡ lệnh (đã gồm trượt giá), trừ khoảng $1 phí. Định giá ở 15 phút / 1 giờ / 6 giờ / 24 giờ. Cột “Bán ngay” là chi phí vào rồi ra ngay. Số liệu tách riêng với shadow vì quy tắc khác nhau.</div>
    ${table([["Token"], ["Mở lúc"], ["Trạng thái"], ["Bán ngay", "num"], ["15 phút", "num"], ["1 giờ", "num"], ["6 giờ", "num"], ["24 giờ", "num"], ["Hiện tại", "num"]], old, { empty: "Chưa có cảnh báo mạnh nào, nên chưa có lệnh." })}`;
  return `<section class="panel"><header><h2>PAPER Trading</h2>${pill("PAPER MODE", "acc")}${status}<span class="grow"></span>${tabs}</header>${body}</section>`;
}

// ---------------------------------------------------------------- 6. performance
function viewPerformance() {
  const sh = shadow(), sum = sh.summary || [];
  const kindL = { single: "1 ví", strong: "Nhiều nhóm ví" }, tierL = { all: "Tất cả", main: "main", watch: "watch", none: "chưa thắng" };
  const ci = (c) => c ? `<div class="small dim">${Math.round(c[0] * 100)}–${Math.round(c[1] * 100)}%</div>` : "";
  const hit = (k, n, c) => n ? `<b class="mono">${k}/${n}</b> <span class="small">(${Math.round(100 * k / n)}%)</span>${ci(c)}` : `<span class="unk">chưa đủ thời gian</span>`;
  const rows = sum.slice().sort((a, b) => (a.kind > b.kind ? 1 : -1) || (a.tier === "all" ? -1 : 1)).map((r) => {
    const loss = r.paper_n ? r.paper_n - r.paper_win : null;
    return { key: r.kind + r.tier, sig: [r.n, r.paper_n, r.paper_usd].join("|"), cls: r.tier === "all" ? "strong" : "",
      cells: [`${esc(kindL[r.kind] || r.kind)} · ${esc(tierL[r.tier] || r.tier)}`, `<b class="mono">${r.n}</b><div class="small dim">${r.n_24h || 0} đủ 24 giờ</div>`, hit(r.hit2x_1h, r.n_1h, r.ci_1h), hit(r.hit2x_24h, r.n_24h, r.ci_24h),
        mult(r.med_24h), r.paper_n ? `<span class="mono">${r.paper_n}</span>` : `<span class="unk">0</span>`, loss !== null ? `<span class="mono">${Math.round(100 * loss / r.paper_n)}%</span>` : `<span class="unk">—</span>`,
        money(r.paper_n ? r.paper_usd : null), money(r.paper_n ? r.paper_usd_wo_best : null),
        `<span class="small">${Object.entries(r.exits || {}).map(([k, n]) => `${esc(EXIT[k] || k)} ${n}`).join(" · ") || "—"}</span>`] }; });
  const live = (sh.rows || []).filter((x) => x.status === "live" || x.status === "done");
  const dips = live.map((x) => x.dip).filter((v) => v !== null && v !== undefined).sort((a, b) => a - b);
  const medDip = dips.length ? dips[Math.floor(dips.length / 2)] : null;
  const n = (sum.find((r) => r.kind === "single" && r.tier === "all") || {}).n || 0;
  return `<section class="panel"><header><h2>Hiệu suất và chất lượng tín hiệu</h2></header>
    <div class="note">Shadow đo mọi tín hiệu trên mọi coin, không chỉ coin được kênh call. Tăng tối đa là mức tối thiểu (giá lấy 30 giây/lần). Khoảng dưới mỗi tỷ lệ là khoảng tin cậy 95% (Wilson). <b>Luôn đọc tỷ lệ cùng số mẫu.</b></div>
    ${n < 15 ? `<div>${pill(`Chưa đủ mẫu: ${n}/15 tín hiệu. Chưa kết luận được có lợi thế.`, "warn wrap")}</div>` : ""}
    ${table([["Loại · hạng ví"], ["Tín hiệu", "num"], ["≥ ×2 trong 1 giờ", "num"], ["≥ ×2 trong 24 giờ", "num"], ["Trung vị tăng tối đa 24h", "num"], ["Lệnh paper đã đóng", "num"], ["Tỷ lệ lỗ", "num"], ["P&L sau phí", "num"], ["Bỏ lệnh tốt nhất", "num"], ["Lý do thoát"]], rows, { empty: "Chưa có tín hiệu nào từ lúc bật đo." })}
    <div class="facts"><div class="fact"><span class="k">Mức giảm trước đỉnh (trung vị)</span><span class="v">${medDip !== null ? pct((medDip - 1) * 100) : "—"}</span></div>
      <div class="fact"><span class="k">Tín hiệu đã có giá</span><span class="v">${live.length}</span></div>
      <div class="fact"><span class="k">Tín hiệu không có giá</span><span class="v">${(sh.rows || []).filter((x) => x.status === "no_price").length}</span></div></div></section>`;
}

// ---------------------------------------------------------------- 7. research + 8. settings
function viewResearch() {
  return `<section class="panel"><header><h2>Nghiên cứu lịch sử</h2></header>
    <div class="note">Dữ liệu nghiên cứu từ 97 lần call của kênh (quét 10/10) và 20 call gần nhất (12/10): cây gia phả ví, lợi nhuận theo ví và nhóm deployer, dòng tiền 8 bước, ví lặp lại, danh sách token. Phần này không thay đổi khi theo dõi trực tiếp.</div>
    <div><a class="btn primary" href="/legacy/">Mở trang nghiên cứu đầy đủ</a></div>
    <div class="note">Trang nghiên cứu dùng giao diện cũ và cùng mã truy cập.</div></section>`;
}
function viewSettings() {
  const c = S.w.config || {};
  const row = (k, v, env) => ({ key: k, sig: "", cells: [esc(k), `<b class="mono">${v}</b>`, env ? `<code class="mono small">${esc(env)}</code>` : "—"] });
  const rows = [row("Cảnh báo một ví (SOL tối thiểu)", c.min_sol, "INSIDER_MIN_SOL"), row("Ví hiện trên bảng tín hiệu (tổng mua)", c.board_min_sol, "INSIDER_BOARD_MIN_SOL"),
    row("Cửa sổ cảnh báo mạnh", dur(c.strong_window_s), "INSIDER_STRONG_WINDOW_S"), row("Nạp tiền để thành ví con", c.adopt_min_sol + " SOL", "INSIDER_ADOPT_MIN_SOL"),
    row("Thời gian theo dõi ví con", (c.child_days || 7) + " ngày", null), row("Tối đa ví nghe trực tiếp", c.max_wallets, "INSIDER_MAX_WALLETS"),
    row("Coin thắng: giữ tối thiểu", dur(c.win_hold_s), "INSIDER_WIN_HOLD_S"), row("Coin thắng: lãi tối thiểu", c.win_min_sol + " SOL, ×" + c.win_min_mult, "INSIDER_WIN_MIN_SOL / _MULT")];
  return `<section class="panel"><header><h2>Cài đặt</h2>${pill("CHỈ ĐỌC", "")}</header>
    <div class="note">Các ngưỡng chỉ đổi được qua biến môi trường của server (Render). Trang này không thay đổi logic, ngưỡng hay chế độ read-only + PAPER.</div>
    ${table([["Ngưỡng"], ["Giá trị", "num"], ["Biến môi trường"]], rows)}
    <h2>Trên thiết bị này</h2>
    <div class="ctrl"><button class="btn" id="notif">Bật thông báo trình duyệt</button>
      <label class="chk"><input type="checkbox" id="sound" ${S.sound ? "checked" : ""}> Âm thanh khi có cảnh báo mạnh</label>
      <button class="btn" id="logout">Đăng xuất (xoá mã truy cập)</button></div>
    <div class="note">Telegram: ${S.w.telegram ? "đang bật" : "chưa bật (cần TELEGRAM_BOT_TOKEN và TELEGRAM_CHAT_ID trên server)"}.</div></section>`;
}

// ---------------------------------------------------------------- events
function bind() {
  $$("[data-win]").forEach((b) => b.onclick = () => { S.sig.win = b.dataset.win; LS.set("kr.win", S.sig.win); render(); });
  const m = $("#multi"); if (m) m.onchange = () => { S.sig.multi = m.checked; LS.set("kr.multi", m.checked); render(); };
  const ss = $("#sigsort"); if (ss) ss.onchange = () => { S.sig.sort = ss.value; LS.set("kr.sort", ss.value); render(); };
  $$("th[data-sort]").forEach((th) => th.onclick = () => { S.sig.sort = th.dataset.sort; LS.set("kr.sort", S.sig.sort); render(); });
  $$("[data-open]").forEach((el) => el.closest("tr").onclick = (e) => { if (e.target.closest("a, .copy")) return; S.sig.open = S.sig.open === el.dataset.open ? null : el.dataset.open; if (S.tab !== "signals") location.hash = "#signals"; else render(); });
  $$("[data-allevel]").forEach((b) => b.onclick = () => { S.al.level = b.dataset.allevel; render(); });
  $$("[data-alview]").forEach((b) => b.onclick = () => { S.al.view = b.dataset.alview; LS.set("kr.alview", S.al.view); render(); });
  $$("[data-wlmode]").forEach((b) => b.onclick = () => { S.wl.mode = b.dataset.wlmode; render(); });
  const r = $("#wlrole"); if (r) r.onchange = () => { S.wl.role = r.value; render(); };
  const so = $("#wlsort"); if (so) so.onchange = () => { S.wl.sort = so.value; render(); };
  $$("tr [data-wallet]").forEach((el) => { const tr = el.closest("tr"); if (tr) tr.onclick = (e) => { if (e.target.closest("a, .copy")) return; loadWallet(el.dataset.wallet); }; });
  $$("a[data-wallet]").forEach((a) => a.onclick = (e) => { e.preventDefault(); loadWallet(a.dataset.wallet); });
  $$("[data-close]").forEach((b) => b.onclick = () => { if (b.dataset.close === "sig") S.sig.open = null; else { S.wl.open = null; S.wl.detail = null; } render(); });
  $$("[data-pptab]").forEach((b) => b.onclick = () => { S.pp.tab = b.dataset.pptab; render(); });
  const n = $("#notif"); if (n) { if (!("Notification" in window) || Notification.permission === "granted") n.hidden = true; n.onclick = () => Notification.requestPermission().then(() => render()); }
  const snd = $("#sound"); if (snd) snd.onchange = () => { S.sound = snd.checked; LS.set("kr.sound", snd.checked); };
  const lo = $("#logout"); if (lo) lo.onclick = () => { LS.del("insAccessCode"); location.reload(); };
  if ($("#chart")) drawChart();
}
document.addEventListener("click", async (e) => {
  const c = e.target.closest("[data-copy]"); if (!c) return;
  e.preventDefault(); e.stopPropagation();
  try { await navigator.clipboard.writeText(c.dataset.copy); toast("Đã sao chép"); } catch (_) { toast(c.dataset.copy); }
}, true);
let lastStrong = LS.get("kr.lastStrong", 0);
function notifyNew() {          // a sound + system notification for strong alerts newer than the last one seen on this device
  const strong = alerts().filter((a) => a.level === "vipstrong");
  if (!LS.get("kr.lastStrong", null)) {                  // first visit on this device: remember, do not ring for old ones
    lastStrong = Math.max(1, ...strong.map((a) => a.ts || 0)); LS.set("kr.lastStrong", lastStrong); return;
  }
  const fresh = strong.filter((a) => a.ts > lastStrong);
  if (!fresh.length) return;
  lastStrong = Math.max(...fresh.map((a) => a.ts)); LS.set("kr.lastStrong", lastStrong);
  if (S.sound) { try { const ctx = new AudioContext(), o = ctx.createOscillator(); o.frequency.value = 880; o.connect(ctx.destination); o.start(); o.stop(ctx.currentTime + 0.25); } catch (_) { /* no audio */ } }
  if (navigator.vibrate) navigator.vibrate([120, 60, 120]);
  if ("Notification" in window && Notification.permission === "granted") {
    const a = fresh[0]; try { new Notification("Cảnh báo mạnh: " + (a.symbol ? "$" + a.symbol : short(a.mint)), { body: `${a.n_families || a.n_wallets || 2} nhóm ví cùng vào · ${nf(a.sol, 2)} SOL` }); } catch (_) { /* blocked */ }
  }
}
document.addEventListener("DOMContentLoaded", () => { if (S.code) start(); else showLogin(""); });
