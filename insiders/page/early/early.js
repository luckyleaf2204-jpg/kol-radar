/* Early Smart Money (120-minute window) — read-only page. Data: /api/insiders/esm/* (v5 esm_* tables, real data
   imported from the research of the 5 verified coins). Nothing here signs or sends a transaction. */
"use strict";
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const LS = {
  get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch (_) { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (_) { /* private mode */ } },
};
const S = { code: LS.get("insAccessCode", ""), cache: {}, wl: LS.get("esm.wl", {}), el: LS.get("esm.el", {}), cw: {} };
const TABS = [["", "RADAR"], ["stats", "Thống kê"], ["wallets", "Xếp hạng ví"], ["entities", "Xếp hạng chủ thể"], ["coins", "Coin"], ["trace", "Truy vết ví"], ["guide", "Chú giải"]];
const BUCKETS = ["0-15", "15-60", "60-120"];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const short = (a, n = 4) => a ? (a.length > 12 ? `${a.slice(0, n)}…${a.slice(-n)}` : a) : "";
const fmt = (v, d = 2) => v === null || v === undefined || Number.isNaN(v) ? "–" : Number(v).toLocaleString("en-US", { maximumFractionDigits: d });
const xf = (v) => v === null || v === undefined ? "–" : `${fmt(v, 2)}x`;
const utc = (t) => t ? new Date(t * 1000).toISOString().replace("T", " ").slice(0, 16) + " UTC" : "–";
const SOLSCAN = { acc: (a) => `https://solscan.io/account/${a}`, tx: (s) => `https://solscan.io/tx/${s}`, tok: (m) => `https://solscan.io/token/${m}` };

function toast(m) { const t = $("#toast"); t.textContent = m; t.classList.add("on"); clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove("on"), 1800); }
function copy(v) { navigator.clipboard?.writeText(v).then(() => toast("Đã chép"), () => toast(v)); }

async function api(name, qs) {
  const key = name + (qs || "");
  if (S.cache[key]) return S.cache[key];
  const r = await fetch(`/api/insiders/esm/${name}${qs ? "?" + qs : ""}`, { headers: { "X-Access-Code": S.code }, cache: "no-store" });
  if (r.status === 401) { login("Cần mã truy cập"); throw new Error("auth"); }
  if (r.status === 404) return null;
  if (!r.ok) throw new Error("http " + r.status);
  return (S.cache[key] = await r.json());
}

function login(msg) {
  $("#main").innerHTML = `<form class="login" id="lg"><h1>Early Smart Money</h1><div class="muted">${esc(msg)}</div>
    <input id="lgc" type="password" placeholder="Mã truy cập" autocomplete="current-password"><button class="btn primary">Vào</button></form>`;
  $("#lg").onsubmit = (e) => { e.preventDefault(); S.code = $("#lgc").value.trim(); LS.set("insAccessCode", S.code); S.cache = {}; route(); };
}

// ---------------------------------------------------------------- badges
const CLS = { LIKELY_SAME_ENTITY: ["ls", "LIKELY_SAME_ENTITY", "Có bằng chứng mạnh cùng chủ với ví khác (chuyển tiền, chung nguồn vốn, trả phí hộ…)"],
  POSSIBLE_LINK: ["pl", "POSSIBLE_LINK", "Có liên kết trung bình (cùng slot, gốc vốn hop 2, ví cấp vốn bận…), chưa đủ kết luận cùng chủ"],
  INDEPENDENT: ["in", "INDEPENDENT", "Đã kiểm tra nguồn vốn, không thấy liên kết"],
  INSUFFICIENT_DATA: ["id", "INSUFFICIENT_DATA", "Chưa đủ dữ liệu để phân loại; không loại ví"] };
function clsBadge(c) { const x = CLS[c] || CLS.INSUFFICIENT_DATA; return `<span class="bd ${x[0]}" title="${esc(x[2])}">${x[1]}</span>`; }
function flagBadges(flags, bot) {
  const out = [];
  for (const f of flags || []) {
    if (f === "CREATOR_GROUP") out.push(`<span class="bd cg" title="Nhóm phát hành (creator + ví liên kết đã xác minh): không xếp hạng">CREATOR_GROUP</span>`);
    else if (f.startsWith("COORDINATED_PATTERN:")) out.push(`<span class="bd co" title="Hành vi đồng bộ (nhãn mô tả, không trừ điểm, không kết luận cùng chủ)">COORD ${esc(f.split(":")[1])}</span>`);
    else if (f.startsWith("CREATOR:")) out.push(`<span class="bd cr" title="Ví tạo coin này (chỉ là nhãn, vẫn xếp hạng nếu không có liên kết)">CREATOR ${esc(f.split(":")[1])}</span>`);
  }
  if (bot) out.push(`<span class="bd bot" title="≥ 10 giao dịch, giữ trung vị < 60 s (chỉ là cờ)">BOT?</span>`);
  return out.join("");
}
const insBadge = (on) => on ? `<span class="bd id" title="Ít nhất một coin thiếu dữ liệu PnL (ví dụ giao dịch bằng USDC, token nhận chuyển)">PnL: INSUFFICIENT_DATA</span>` : "";
const bk = (b) => `<span class="bk b${BUCKETS.indexOf(b)}">${esc(b)}′</span>`;
const coinChip = (c) => `<span class="bd ch">${esc(c)}</span>`;
const walletLink = (w, n = 6) => `<span class="addr"><a href="#/wallet/${w}" title="${w}">${short(w, n)}</a><span class="copy" data-copy="${w}" title="Chép">⧉</span></span>`;

// ---------------------------------------------------------------- generic table (search, sort, paging)
function table(id, cols, rows, opts = {}) {
  const st = S.cw[id] || (S.cw[id] = { sort: opts.sort || null, dir: opts.dir || -1, page: 0 });
  const per = opts.per || 50;
  let rs = rows.slice();
  if (st.sort) {
    const c = cols.find((c) => c.k === st.sort);
    const val = c && (c.sv || ((r) => r[c.k]));
    rs.sort((a, b) => { const x = val(a), y = val(b); if (x === y) return 0; if (x === null || x === undefined) return 1; if (y === null || y === undefined) return -1; return (x > y ? 1 : -1) * st.dir; });
  }
  const pages = Math.max(1, Math.ceil(rs.length / per)); st.page = Math.min(st.page, pages - 1);
  const view = rs.slice(st.page * per, st.page * per + per);
  const head = cols.map((c) => `<th class="${c.num ? "num " : ""}${c.sort === false ? "" : "sort"}${st.sort === c.k ? " on" : ""}" data-k="${c.k}">${esc(c.t)}${st.sort === c.k ? (st.dir > 0 ? " ▲" : " ▼") : ""}</th>`).join("");
  const body = view.length ? view.map((r) => `<tr class="${opts.href ? "click" : ""}" ${opts.href ? `data-href="${esc(opts.href(r))}"` : ""}>${cols.map((c) =>
    `<td class="${c.num ? "num" : ""} ${c.cls || ""}" data-l="${esc(c.t)}">${c.f ? c.f(r) : esc(r[c.k])}</td>`).join("")}</tr>`).join("")
    : `<tr><td colspan="${cols.length}" class="empty">Không có dòng nào khớp bộ lọc</td></tr>`;
  const pager = pages > 1 ? `<div class="pager"><button class="btn small" data-pg="-1" ${st.page ? "" : "disabled"}>‹ Trước</button>
    <span class="mono">${st.page + 1} / ${pages}</span><button class="btn small" data-pg="1" ${st.page < pages - 1 ? "" : "disabled"}>Sau ›</button></div>` : "";
  return `<div data-tbl="${id}"><div class="tbl"><table class="cards"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>${pager}</div>`;
}
function bindTables(root, rerender) {
  $$("[data-tbl]", root).forEach((box) => {
    const st = S.cw[box.dataset.tbl];
    $$("th.sort", box).forEach((th) => th.onclick = () => { if (st.sort === th.dataset.k) st.dir = -st.dir; else { st.sort = th.dataset.k; st.dir = -1; } rerender(); });
    $$("[data-pg]", box).forEach((b) => b.onclick = () => { st.page += Number(b.dataset.pg); rerender(); });
    $$("tr[data-href]", box).forEach((tr) => tr.onclick = (e) => { if (e.target.closest("a,.copy,button")) return; location.hash = tr.dataset.href; });
  });
  $$("[data-copy]", root).forEach((c) => c.onclick = (e) => { e.stopPropagation(); copy(c.dataset.copy); });
}

// ---------------------------------------------------------------- layout
function nav(active) {
  $("#tabs").innerHTML = TABS.map(([k, t]) => `<a href="#/${k}" class="${k === active ? "on" : ""}">${t}</a>`).join("") +
    `<a href="/" class="muted" title="Dashboard Insiders">← Dashboard</a>`;
}
function setMain(html, after) { const m = $("#main"); m.innerHTML = html; bindTables(m, after); }
function kpi(k, v, s, cls = "") { return `<div class="kpi"><span class="k">${esc(k)}</span><span class="v ${cls}">${v}</span>${s ? `<span class="s">${s}</span>` : ""}</div>`; }

// ---------------------------------------------------------------- overview
async function viewOverview() {
  const [sm, cs, ws] = await Promise.all([api("summary"), api("coins"), api("wallets")]);
  if (!sm.imported) return setMain(`<section class="panel"><div class="warnbox">Database chưa có dữ liệu Early Smart Money. Chạy:
    <code>python -m insiders.esm import research/seven_coins</code> rồi tải lại trang.</div></section>`);
  $("#updated").textContent = `Dữ liệu nạp: ${utc(sm.imported_at)} · ${sm.rules_version}`;
  const top = ws.wallets.filter((w) => w.ranked).slice(0, 10);
  const c = sm.classes || {};
  setMain(`
    <section class="kpis k6">
      ${kpi("Coin đã phân tích", sm.coins, `${sm.pending.length} chờ xác minh mint`)}
      ${kpi("Ví mua trong cửa sổ", fmt(sm.wallets, 0), `${fmt(sm.ranked, 0)} ví được xếp hạng`)}
      ${kpi("Vào sớm ≥ 2 coin", sm.early_2plus, "phút 0–60, đã loại CREATOR_GROUP")}
      ${kpi("Chủ thể (cụm ví)", sm.entities, "mỗi coin tính 1 lần")}
      ${kpi("CREATOR_GROUP", sm.flags.CREATOR_GROUP || 0, "không xếp hạng", "bad")}
      ${kpi("COORDINATED_PATTERN", sm.flags.COORDINATED_PATTERN || 0, "nhãn hành vi", "warn")}
    </section>
    <section class="panel"><header><h2>Coin</h2><span class="grow"></span><a href="#/coins" class="small">Tất cả →</a></header>${coinCards(cs)}</section>
    <div class="grid2">
      <section class="panel"><header><h2>Top 10 Smart Money</h2><span class="grow"></span><a href="#/wallets" class="small">Bảng xếp hạng →</a></header>
        ${table("ov-top", walletCols(true), top, { href: (r) => `#/wallet/${r.wallet}`, per: 10 })}</section>
      <section class="panel"><header><h2>Phân loại ví mua</h2></header>
        <div class="tbl"><table><tbody>
          ${Object.keys(CLS).map((k) => `<tr><td>${clsBadge(k)}</td><td class="num mono">${fmt(c[k] || 0, 0)}</td><td class="small muted">${esc(CLS[k][2])}</td></tr>`).join("")}
        </tbody></table></div>
        <div class="note">Điểm: ${esc(sm.score_rule)}. Chỉ dùng dữ liệu trong 120 phút đầu; kết quả sau 2 giờ hiển thị riêng, không vào điểm.</div>
      </section>
    </div>`, viewOverview);
}
function coinCards(cs) {
  const real = cs.coins.map((c) => `<div class="ccard" data-go="#/coin/${c.mint}">
      <div class="hdr"><span class="sym">${esc(c.symbol)}</span>${c.quote !== "SOL" ? `<span class="bd st">${esc(c.quote)}</span>` : ""}
        <span class="grow"></span><span class="bd ${c.status === "DONE" ? "in" : "id"}">${esc(c.status)}</span></div>
      <div class="row"><span>Tạo</span><b>${utc(c.t_create)}</b></div>
      <div class="row"><span>Ví mua 0–15 / 15–60 / 60–120</span><b>${BUCKETS.map((b) => c.by_bucket[b] || 0).join(" / ")}</b></div>
      <div class="xs">${[["15′", c.x_15m], ["60′", c.x_60m], ["120′", c.x_120m], ["Đỉnh", c.peak_x]].map(([k, v]) => `<div><div class="k">${k}</div><div class="v">${xf(v)}</div></div>`).join("")}</div>
    </div>`).join("");
  const pend = cs.pending.map((p) => `<div class="ccard pending">
      <div class="hdr"><span class="sym">${esc(p.symbol)}</span><span class="grow"></span><span class="bd nv">NEEDS_MINT_VERIFICATION</span></div>
      <div class="small muted">Chưa có mint đã xác minh từ nguồn gốc. ${p.candidates.length} token cùng ticker chỉ là ứng viên; không phân tích, không chấm điểm.</div>
    </div>`).join("");
  return `<div class="coins">${real}${pend}</div>`;
}

// ---------------------------------------------------------------- wallets ranking
function walletCols(compact) {
  const cols = [
    { k: "rank", t: "#", num: true, cls: "rank", f: (r) => r.rank ?? "–", sv: (r) => r.rank ?? 1e9 },
    { k: "wallet", t: "Ví", sort: false, f: (r) => walletLink(r.wallet) + (r.cohort ? ` <span class="bd st" title="Cohort cũ (tham chiếu)">${esc(r.cohort)}</span>` : "") },
    { k: "score", t: "Điểm", num: true, f: (r) => r.score === null ? `<span class="dim">–</span>` : `<span class="score">${r.score}</span>` },
    { k: "early", t: "Vào sớm", f: (r) => `<div class="bds">${r.early_coins.map(coinChip).join("") || `<span class="dim">–</span>`}</div>`, sv: (r) => r.early_coins.length },
    { k: "first_min", t: "Phút vào đầu", num: true, f: (r) => fmt(r.first_min, 2) },
  ];
  if (!compact) cols.push(
    { k: "coins", t: "Coin", num: true, f: (r) => r.coins.length, sv: (r) => r.coins.length },
    { k: "sol_in", t: "SOL vào", num: true, f: (r) => fmt(r.sol_in, 3) + (r.stable_in ? `<span class="sub">+${fmt(r.stable_in)} USDC</span>` : "") },
    { k: "best_rise", t: "Tăng sau vào", num: true, f: (r) => xf(r.best_rise) },
    { k: "roles", t: "Vai trò", sort: false, f: (r) => `<span class="small muted">${(r.roles || []).join(", ") || (r.non_buyer ? "không mua trong cửa sổ" : "–")}</span>` });
  cols.push({ k: "class", t: "Phân loại", f: (r) => clsBadge(r.class) + (r.entity ? ` <a class="bd ch" href="#/entity/${r.entity}">${r.entity}</a>` : "") },
    { k: "flags", t: "Cờ", sort: false, f: (r) => `<div class="bds">${flagBadges(r.flags, r.bot)}${compact ? "" : insBadge(r.insufficient)}</div>` });
  return cols;
}
async function viewWallets() {
  const data = await api("wallets"), coins = await api("coins");
  const f = Object.assign({ q: "", coin: "", bucket: "", cls: "", flag: "", ranked: true, min2: false, bots: true, cohort: "" }, S.wl);
  const rows = data.wallets.filter((r) => {
    if (f.ranked && !r.ranked) return false;
    if (f.min2 && r.early_coins.length < 2) return false;
    if (!f.bots && r.bot) return false;
    if (f.coin && !r.coins.includes(f.coin)) return false;
    if (f.bucket && !r.buckets.includes(f.bucket)) return false;
    if (f.cls && r.class !== f.cls) return false;
    if (f.cohort && r.cohort !== f.cohort) return false;
    if (f.flag === "CREATOR_GROUP" && !r.flags.includes("CREATOR_GROUP")) return false;
    if (f.flag === "COORD" && !r.flags.some((x) => x.startsWith("COORDINATED"))) return false;
    if (f.flag === "CREATOR" && !r.flags.some((x) => x.startsWith("CREATOR:"))) return false;
    if (f.flag === "INS" && !r.insufficient) return false;
    if (f.flag === "NONE" && (r.flags.length || r.bot)) return false;
    if (f.q) { const q = f.q.toLowerCase(); if (!(r.wallet.toLowerCase().includes(q) || (r.entity || "").toLowerCase() === q || r.coins.some((c) => c.toLowerCase() === q))) return false; }
    return true;
  });
  const opt = (v, t, cur) => `<option value="${esc(v)}" ${v === cur ? "selected" : ""}>${esc(t)}</option>`;
  setMain(`<section class="panel">
    <header><h2>Xếp hạng Smart Money Wallet</h2><span class="grow"></span><span class="count">${fmt(rows.length, 0)} / ${fmt(data.wallets.length, 0)} ví</span></header>
    <div class="tools">
      <input type="search" id="q" placeholder="Tìm địa chỉ ví, mã chủ thể (E001) hoặc ticker…" value="${esc(f.q)}">
      <select id="coin">${opt("", "Mọi coin", f.coin)}${coins.coins.map((c) => opt(c.symbol, c.symbol, f.coin)).join("")}</select>
      <select id="bucket">${opt("", "Mọi mốc", f.bucket)}${BUCKETS.map((b) => opt(b, `Vào ${b} phút`, f.bucket)).join("")}</select>
      <select id="cls">${opt("", "Mọi phân loại", f.cls)}${Object.keys(CLS).map((k) => opt(k, k, f.cls)).join("")}</select>
      <select id="flag">${opt("", "Mọi cờ", f.flag)}${opt("COORD", "COORDINATED_PATTERN", f.flag)}${opt("CREATOR_GROUP", "CREATOR_GROUP", f.flag)}${opt("CREATOR", "CREATOR", f.flag)}${opt("INS", "PnL INSUFFICIENT_DATA", f.flag)}${opt("NONE", "Không cờ", f.flag)}</select>
      <select id="cohort">${opt("", "Mọi cohort", f.cohort)}${["DEPTH", "STANK", "INSIDERS"].map((k) => opt(k, `Cohort ${k}`, f.cohort)).join("")}</select>
      <label class="chk"><input type="checkbox" id="ranked" ${f.ranked ? "checked" : ""}> Chỉ ví được xếp hạng</label>
      <label class="chk"><input type="checkbox" id="min2" ${f.min2 ? "checked" : ""}> Vào sớm ≥ 2 coin</label>
      <label class="chk"><input type="checkbox" id="bots" ${f.bots ? "checked" : ""}> Hiện cờ BOT?</label>
    </div>
    ${table("wallets", walletCols(false), rows, { href: (r) => `#/wallet/${r.wallet}`, sort: "rank", dir: 1 })}
    <div class="note">CREATOR_GROUP không có hạng (bỏ "Chỉ ví được xếp hạng" để xem). COORDINATED_PATTERN chỉ là nhãn, không trừ điểm. Ví MACCA (giao dịch bằng USDC) không có điểm SOL.</div>
  </section>`, viewWallets);
  const upd = (k, v) => { S.wl = { ...f, [k]: v }; LS.set("esm.wl", S.wl); if (S.cw.wallets) S.cw.wallets.page = 0; viewWallets().then(() => { if (k === "q") { const i = $("#q"); i.focus(); i.setSelectionRange(i.value.length, i.value.length); } }); };
  let tm; $("#q").oninput = (e) => { clearTimeout(tm); tm = setTimeout(() => upd("q", e.target.value.trim()), 220); };
  ["coin", "bucket", "cls", "flag", "cohort"].forEach((k) => $("#" + k).onchange = (e) => upd(k, e.target.value));
  ["ranked", "min2", "bots"].forEach((k) => $("#" + k).onchange = (e) => upd(k, e.target.checked));
}

// ---------------------------------------------------------------- entities ranking
async function viewEntities() {
  const data = await api("entities");
  const f = Object.assign({ q: "", cg: false, multi: false }, S.el);
  const rows = data.entities.filter((e) => (f.cg || e.ranked || !e.flags.includes("CREATOR_GROUP")) && (!f.multi || e.early_coins.length >= 2) &&
    (!f.q || e.entity.toLowerCase().includes(f.q.toLowerCase()) || e.coins.some((c) => c.toLowerCase() === f.q.toLowerCase())));
  const cols = [
    { k: "rank", t: "#", num: true, cls: "rank", f: (r) => r.rank ?? "–", sv: (r) => r.rank ?? 1e9 },
    { k: "entity", t: "Chủ thể", f: (r) => `<a class="mono" href="#/entity/${r.entity}">${r.entity}</a>` },
    { k: "score", t: "Điểm", num: true, f: (r) => r.score === null ? `<span class="dim">–</span>` : `<span class="score">${r.score}</span>` },
    { k: "members", t: "Số ví", num: true, f: (r) => `${r.members}<span class="sub">${r.buyers} ví mua</span>` },
    { k: "early", t: "Vào sớm", f: (r) => `<div class="bds">${r.early_coins.map(coinChip).join("") || "–"}</div>`, sv: (r) => r.early_coins.length },
    { k: "coins", t: "Coin", f: (r) => `<div class="bds">${r.coins.map(coinChip).join("")}</div>`, sv: (r) => r.coins.length },
    { k: "first_min", t: "Phút vào đầu", num: true, f: (r) => fmt(r.first_min, 2) },
    { k: "sol_in", t: "SOL vào", num: true, f: (r) => fmt(r.sol_in, 3) },
    { k: "flags", t: "Cờ", sort: false, f: (r) => `<div class="bds">${flagBadges(r.flags)}${r.cohorts.map((c) => `<span class="bd st">${esc(c)}</span>`).join("")}</div>` },
  ];
  setMain(`<section class="panel">
    <header><h2>Xếp hạng chủ thể (nhóm ví cùng chủ)</h2><span class="grow"></span><span class="count">${rows.length} / ${data.entities.length}</span></header>
    <div class="tools"><input type="search" id="q" placeholder="Tìm mã chủ thể (E001) hoặc ticker…" value="${esc(f.q)}">
      <label class="chk"><input type="checkbox" id="multi" ${f.multi ? "checked" : ""}> Vào sớm ≥ 2 coin</label>
      <label class="chk"><input type="checkbox" id="cg" ${f.cg ? "checked" : ""}> Hiện CREATOR_GROUP (không xếp hạng)</label></div>
    ${table("entities", cols, rows, { href: (r) => `#/entity/${r.entity}`, sort: "rank", dir: 1 })}
    <div class="note">Chủ thể = các ví nối với nhau bằng tín hiệu mạnh (hoặc ≥ 2 tín hiệu). Mỗi coin chỉ tính 1 lần (ví có điểm cao nhất), nên một cụm không được chấm như nhiều ví độc lập.</div>
  </section>`, viewEntities);
  const upd = (k, v) => { S.el = { ...f, [k]: v }; LS.set("esm.el", S.el); viewEntities().then(() => { if (k === "q") { const i = $("#q"); i.focus(); i.setSelectionRange(i.value.length, i.value.length); } }); };
  let tm; $("#q").oninput = (e) => { clearTimeout(tm); tm = setTimeout(() => upd("q", e.target.value.trim()), 220); };
  ["multi", "cg"].forEach((k) => $("#" + k).onchange = (e) => upd(k, e.target.checked));
}

// ---------------------------------------------------------------- coins
async function viewCoins() {
  const cs = await api("coins");
  setMain(`<section class="panel"><header><h2>Coin</h2><span class="grow"></span><span class="count">${cs.coins.length} đã xác minh · ${cs.pending.length} chờ xác minh</span></header>
    ${coinCards(cs)}
    ${cs.pending.map((p) => `<div class="panel"><header><h2>${esc(p.symbol)} · ứng viên cùng ticker</h2><span class="grow"></span><span class="bd nv">NEEDS_MINT_VERIFICATION</span></header>
      ${table("pend-" + p.symbol, [
        { k: "mint", t: "Mint ứng viên", f: (r) => `<span class="addr">${esc(r.mint)}</span><a class="ext" href="${SOLSCAN.tok(r.mint)}" target="_blank" rel="noopener">solscan ↗</a>` },
        { k: "name", t: "Tên" }, { k: "t_create_utc", t: "Tạo (UTC)", f: (r) => esc(r.t_create_utc || "chưa tới giao dịch tạo") },
        { k: "overlap_early", t: "Ví 15′ đầu trùng 5 coin", num: true, f: (r) => `${r.overlap_early}${r.first15_buyers ? ` / ${r.first15_buyers}` : ""}` }],
        p.candidates, { sort: "overlap_early", per: 10 })}
      <div class="note">${esc(p.note)}. Chỉ xác nhận khi có nguồn gốc đáng tin cậy và giao dịch tạo khớp dự án.</div></div>`).join("")}
  </section>`, viewCoins);
}

async function viewCoin(mint) {
  const c = await api("coin", "mint=" + mint);
  if (!c) return setMain(`<section class="panel"><div class="empty">Không có coin này trong dữ liệu Early Smart Money.</div></section>`);
  const sel = S.cw["coin-sel"] || (S.cw["coin-sel"] = { b: "0-15", q: "", cls: "" });
  const cov = c.coverage || {};
  const usd = c.quote !== "SOL";
  const ws = c.wallets.filter((w) => (!sel.b || w.bucket === sel.b) && (!sel.cls || w.class === sel.cls) && (!sel.q || w.wallet.toLowerCase().includes(sel.q.toLowerCase())));
  const tot = Math.max(1, c.wallets.length);
  const cols = [
    { k: "first_buy_min", t: "Phút mua đầu", num: true, f: (r) => fmt(r.first_buy_min, 2) },
    { k: "wallet", t: "Ví", sort: false, f: (r) => walletLink(r.wallet) + (r.cohort ? ` <span class="bd st">${esc(r.cohort)}</span>` : "") },
    { k: "buys", t: "Mua / bán", num: true, f: (r) => `${r.buys} / ${r.sells}` },
    { k: "sol_in", t: usd ? "USDC vào" : "SOL vào", num: true, f: (r) => usd ? fmt(r.stable_in) : fmt(r.sol_in, 3) + (r.stable_in ? `<span class="sub">+${fmt(r.stable_in)} USDC</span>` : ""), sv: (r) => usd ? r.stable_in : r.sol_in },
    { k: "rise_x", t: "Tăng sau vào", num: true, f: (r) => xf(r.rise_x) },
    { k: "result_pct", t: "KQ cửa sổ", num: true, f: (r) => r.result_pct === null ? `<span class="dim">–</span>` : `<span class="${r.result_pct >= 0 ? "good" : "bad"}">${fmt(r.result_pct * 100, 0)}%</span>` },
    { k: "points", t: "Điểm", num: true, f: (r) => r.points ?? "–" },
    { k: "roles", t: "Vai trò", sort: false, f: (r) => `<span class="small">${r.roles.join(", ") || "–"}</span>` },
    { k: "class", t: "Phân loại", f: (r) => clsBadge(r.class) + (r.entity ? ` <a class="bd ch" href="#/entity/${r.entity}">${r.entity}</a>` : "") },
    { k: "flags", t: "Cờ", sort: false, f: (r) => `<div class="bds">${flagBadges(r.flags, r.bot_flag)}${r.status !== "OK" ? `<span class="bd id" title="${esc((r.insufficient || []).join("; "))}">INSUFFICIENT_DATA</span>` : ""}</div>` },
  ];
  setMain(`
    <div class="crumbs"><a href="#/coins">Coin</a> › <span>${esc(c.symbol)}</span></div>
    <section class="panel">
      <div class="hdr"><h1>${esc(c.symbol)}</h1><span class="bd ${c.status === "DONE" ? "in" : "id"}">${esc(c.status)}</span>${usd ? `<span class="bd st">Định giá ${esc(c.quote)}</span>` : ""}</div>
      <dl class="kv">
        <dt>Mint</dt><dd><span class="addr">${esc(c.mint)}</span><span class="copy" data-copy="${c.mint}">⧉</span><a class="ext" href="${SOLSCAN.tok(c.mint)}" target="_blank" rel="noopener">solscan ↗</a></dd>
        <dt>Xác minh</dt><dd>${esc(c.verified || "–")}</dd>
        <dt>t_create</dt><dd>${utc(c.t_create)} · <a href="${SOLSCAN.tx(c.creation_sig)}" target="_blank" rel="noopener">giao dịch tạo ↗</a></dd>
        <dt>Creator</dt><dd>${c.creator ? walletLink(c.creator, 8) : "–"}</dd>
        <dt>Độ phủ dữ liệu</dt><dd class="mono small">${fmt(cov.window_signatures, 0)} chữ ký · ${fmt(cov.window_successful, 0)} thành công · giải mã ${fmt(cov.decoded, 0)} · không phục vụ ${fmt(cov.not_served, 0)} · lỗi ${fmt(cov.decode_errors, 0)} · chưa đọc ${fmt(cov.unread, 0)}</dd>
      </dl>
      ${c.note ? `<div class="warnbox">${esc(c.note)}</div>` : ""}
      <div class="xs">${[["Phút 15", c.x_15m], ["Phút 60", c.x_60m], ["Phút 120", c.x_120m], [`Đỉnh${c.peak_min !== null ? " (" + fmt(c.peak_min, 0) + "′)" : ""}`, c.peak_x]].map(([k, v]) => `<div><div class="k">${k}</div><div class="v">${xf(v)}</div></div>`).join("")}</div>
      <div class="bar3" title="Tỷ lệ ví theo mốc">${BUCKETS.map((b, i) => `<span style="width:${(c.buckets[b].buyers / tot * 100).toFixed(1)}%;background:${["var(--good)", "var(--accent)", "var(--none)"][i]}"></span>`).join("")}</div>
    </section>
    <section class="wins">${BUCKETS.map((b) => { const x = c.buckets[b]; return `<div class="win ${sel.b === b ? "on" : ""}" data-b="${b}">
      <div class="hdr"><span class="t">${b} phút</span><span class="grow"></span>${bk(b)}</div>
      <div class="n">${x.buyers} <span class="small muted">ví mua</span></div>
      <div class="r"><span>${usd ? fmt(x.stable_in) + " USDC" : fmt(x.sol_in, 2) + " SOL"}</span><span>ACC ${x.roles.ACCUMULATE}</span><span>EARLY_SELL ${x.roles.EARLY_SELL}</span><span>HOLD ${x.roles.HOLD}</span><span>GOOD ${x.roles.GOOD}</span></div>
      <div class="r"><span>Thiếu dữ liệu ${x.insufficient}</span><span>Có cờ ${x.flagged}</span></div></div>`; }).join("")}</section>
    <section class="panel">
      <header><h2>Ví ${sel.b ? "mốc " + esc(sel.b) + " phút" : "toàn cửa sổ"}</h2><span class="grow"></span><span class="count">${ws.length} ví</span></header>
      <div class="tools"><input type="search" id="cq" placeholder="Tìm địa chỉ ví…" value="${esc(sel.q)}">
        <div class="seg">${["", ...BUCKETS].map((b) => `<button data-sb="${b}" class="${sel.b === b ? "on" : ""}">${b ? b + "′" : "Tất cả"}</button>`).join("")}</div>
        <select id="ccls"><option value="">Mọi phân loại</option>${Object.keys(CLS).map((k) => `<option ${sel.cls === k ? "selected" : ""}>${k}</option>`).join("")}</select></div>
      ${table("coin-" + c.mint, cols, ws, { href: (r) => `#/wallet/${r.wallet}`, sort: "first_buy_min", dir: 1 })}
    </section>
    <section class="panel"><header><h2>Sau 2 giờ (chỉ đánh giá chiến lược, KHÔNG vào điểm)</h2></header>
      ${table("oc-" + c.mint, [{ k: "horizon", t: "Mốc" }, { k: "x_vs_open", t: "So với giá mở", num: true, f: (r) => xf(r.x_vs_open) },
        { k: "x_vs_120m", t: "So với phút 120", num: true, f: (r) => xf(r.x_vs_120m) }, { k: "trades", t: "Lệnh có giá", num: true, f: (r) => r.trades ?? "–" },
        { k: "status", t: "Trạng thái", f: (r) => r.status === "OK" ? `<span class="bd in">OK</span>` : `<span class="bd id">${esc(r.status)}</span> <span class="small muted">${esc(r.why || "")}</span>` }], c.outcomes, { per: 5 })}
    </section>`, () => viewCoin(mint));
  const re = () => { if (S.cw["coin-" + c.mint]) S.cw["coin-" + c.mint].page = 0; viewCoin(mint); };
  $$(".win").forEach((w) => w.onclick = () => { sel.b = sel.b === w.dataset.b ? "" : w.dataset.b; re(); });
  $$("[data-sb]").forEach((b) => b.onclick = () => { sel.b = b.dataset.sb; re(); });
  $("#ccls").onchange = (e) => { sel.cls = e.target.value; re(); };
  let tm; $("#cq").oninput = (e) => { clearTimeout(tm); tm = setTimeout(() => { sel.q = e.target.value.trim(); re(); setTimeout(() => { const i = $("#cq"); i.focus(); i.setSelectionRange(i.value.length, i.value.length); }); }, 220); };
  $$(".ccard[data-go]").forEach((x) => x.onclick = () => location.hash = x.dataset.go);
}

// ---------------------------------------------------------------- wallet detail
async function viewWallet(w) {
  const d = await api("wallet", "wallet=" + w);
  if (!d) return setMain(`<section class="panel"><div class="empty">Ví này không có trong dữ liệu Early Smart Money.</div></section>`);
  const p = d.perf;
  const tl = d.coins.map((r) => `<div class="hdr"><a href="#/coin/${r.mint}" class="mono" style="min-width:70px">${esc(r.symbol)}</a>
      <div style="flex:1;min-width:200px"><div class="tl">${[15, 60].map((m) => `<span class="mk" style="left:${m / 120 * 100}%"></span>`).join("")}
        <i style="left:${Math.min(100, r.first_buy_min / 120 * 100)}%" title="Mua đầu phút ${fmt(r.first_buy_min, 2)}"></i></div></div>
      ${bk(r.bucket)}</div>`).join("");
  const coinCols = [
    { k: "symbol", t: "Coin", f: (r) => `<a href="#/coin/${r.mint}">${esc(r.symbol)}</a>${r.quote !== "SOL" ? ` <span class="bd st">${esc(r.quote)}</span>` : ""}` },
    { k: "t_create", t: "Coin tạo", f: (r) => `<span class="small">${utc(r.t_create)}</span>` },
    { k: "first_buy_min", t: "Phút mua đầu", num: true, f: (r) => fmt(r.first_buy_min, 2) + `<span class="sub"><a href="${SOLSCAN.tx(r.first_buy_sig)}" target="_blank" rel="noopener">tx ↗</a></span>` },
    { k: "bucket", t: "Mốc", f: (r) => bk(r.bucket) },
    { k: "buys", t: "Mua / bán", num: true, f: (r) => `${r.buys} / ${r.sells}` },
    { k: "sol_in", t: "SOL vào / ra", num: true, f: (r) => `${fmt(r.sol_in, 3)} / ${fmt(r.sol_out, 3)}` + (r.stable_in || r.stable_out ? `<span class="sub">USDC ${fmt(r.stable_in)} / ${fmt(r.stable_out)}</span>` : "") },
    { k: "rise_x", t: "Tăng sau vào", num: true, f: (r) => xf(r.rise_x) },
    { k: "result_sol", t: "KQ cửa sổ (SOL)", num: true, f: (r) => r.result_sol === null ? `<span class="dim">–</span>` : `<span class="${r.result_sol >= 0 ? "good" : "bad"}">${fmt(r.result_sol, 3)}</span><span class="sub">${fmt(r.result_pct * 100, 0)}%</span>` },
    { k: "points", t: "Điểm", num: true, f: (r) => r.points ?? "–" },
    { k: "roles", t: "Vai trò", sort: false, f: (r) => `<span class="small">${r.roles.join(", ") || "–"}</span>` },
    { k: "status", t: "Dữ liệu", f: (r) => r.status === "OK" ? `<span class="bd in">OK</span>` : `<span class="bd id" title="${esc((r.insufficient || []).join("; "))}">INSUFFICIENT_DATA</span><span class="sub">${esc((r.insufficient || []).join("; "))}</span>` },
  ];
  const linkCols = [
    { k: "other", t: "Ví liên kết", sort: false, f: (r) => walletLink(r.other) + (r.other_buyer === 0 ? ` <span class="bd st" title="Không mua trong cửa sổ">không mua</span>` : "") },
    { k: "signal", t: "Tín hiệu" }, { k: "strength", t: "Mức", f: (r) => `<span class="bd ${r.strength === "STRONG" ? "ls" : "pl"}">${esc(r.strength)}</span>` },
    { k: "coin", t: "Coin", f: (r) => esc(r.coin || "–") },
    { k: "evidence", t: "Bằng chứng", sort: false, f: (r) => `<span class="small muted">${esc(evid(r.evidence))}</span>${r.sig ? ` <a class="ext" href="${SOLSCAN.tx(r.sig)}" target="_blank" rel="noopener">tx ↗</a>` : ""}` },
    { k: "other_class", t: "Phân loại ví kia", f: (r) => r.other_class ? clsBadge(r.other_class) : "–" },
    { k: "of", t: "Cờ ví kia", sort: false, f: (r) => `<div class="bds">${flagBadges(r.other_flags)}</div>` },
  ];
  setMain(`
    <div class="crumbs"><a href="#/wallets">Xếp hạng ví</a> › <span class="mono">${short(w, 6)}</span></div>
    <section class="panel">
      <div class="hdr"><h1>${esc(w)}</h1><span class="copy" data-copy="${w}">⧉</span><a class="ext" href="${SOLSCAN.acc(w)}" target="_blank" rel="noopener">solscan ↗</a>
        <span class="grow"></span><a class="btn small" href="#/trace/new/${w}">Truy vết ví này</a></div>
      <div class="bds">${d.rank ? `<span class="bd cr">Hạng #${d.rank}</span>` : `<span class="bd id">Không xếp hạng</span>`}${clsBadge(d.class)}
        ${d.entity ? `<a class="bd ch" href="#/entity/${d.entity}">Chủ thể ${d.entity} · ${d.entity_members.length} ví</a>` : ""}${flagBadges(d.flags)}
        ${d.cohort ? `<span class="bd st">Cohort ${esc(d.cohort)} (tham chiếu)</span>` : ""}</div>
      ${d.flags.includes("CREATOR_GROUP") ? `<div class="warnbox">Ví thuộc CREATOR_GROUP (nhóm phát hành có liên kết đã xác minh): loại khỏi xếp hạng, dữ liệu và bằng chứng giữ nguyên.</div>` : ""}
    </section>
    <section class="kpis k6">
      ${kpi("Điểm", d.score ?? "–", d.rank ? "hạng #" + d.rank : "không xếp hạng")}
      ${kpi("Coin đã tham gia", p.coins, `${p.early} coin vào sớm (0–60′)`)}
      ${kpi("SOL vào", fmt(p.sol_in, 3), p.stable_in ? `+ ${fmt(p.stable_in)} USDC` : "trong cửa sổ", "sm")}
      ${kpi("SOL ra", fmt(p.sol_out, 3), "trong cửa sổ", "sm")}
      ${kpi("Kết quả cửa sổ", p.result_sol === null ? "INSUFFICIENT" : fmt(p.result_sol, 3) + " SOL", p.result_sol === null && p.result_partial_sol !== null ? `một phần: ${fmt(p.result_partial_sol, 3)} SOL` : "realized + giá phút 120", "sm " + (p.result_sol === null ? "" : p.result_sol >= 0 ? "good" : "bad"))}
      ${kpi("Thiếu dữ liệu", p.insufficient, "coin có PnL INSUFFICIENT_DATA", "sm")}
    </section>
    <section class="panel"><header><h2>Thời điểm vào lệnh (0–120 phút)</h2></header>${tl || `<div class="empty">Không mua trong cửa sổ nào (ví trong cụm, không phải ví mua)</div>`}
      <div class="hdr"><span style="min-width:70px"></span><div class="tlax" style="flex:1;min-width:200px">${[0, 15, 60, 120].map((m) => `<span style="left:${m / 120 * 100}%">${m}′</span>`).join("")}</div><span style="min-width:46px"></span></div></section>
    <section class="panel"><header><h2>Lịch sử mua theo coin</h2></header>${table("wc-" + w, coinCols, d.coins, { sort: "t_create", dir: -1 })}</section>
    <section class="panel"><header><h2>Liên kết với ví khác</h2><span class="grow"></span><span class="count">${d.links.length}</span></header>
      ${d.links.length ? table("wl-" + w, linkCols, d.links, { href: (r) => `#/wallet/${r.other}`, per: 25 }) : `<div class="empty">Không có liên kết nào được ghi nhận</div>`}</section>
    ${d.flag_detail.length ? `<section class="panel"><header><h2>Cờ và bằng chứng</h2></header>${d.flag_detail.map((f) => `<div><span class="bd ${f.flag === "CREATOR_GROUP" ? "cg" : f.flag === "CREATOR" ? "cr" : "co"}">${esc(f.flag)}</span> <span class="mono small">${esc(f.ref || "")}</span>
      <pre class="small muted" style="white-space:pre-wrap;margin:6px 0 0">${esc(JSON.stringify(f.evidence, null, 1))}</pre></div>`).join("")}</section>` : ""}`, () => viewWallet(w));
}
function evid(e) {
  if (!e || typeof e !== "object") return "";
  return Object.entries(e).filter(([k]) => k !== "sig").map(([k, v]) => `${k}: ${typeof v === "object" ? JSON.stringify(v) : v}`).join(" · ");
}

// ---------------------------------------------------------------- entity detail
async function viewEntity(id) {
  const d = await api("entity", "id=" + id);
  if (!d) return setMain(`<section class="panel"><div class="empty">Không có chủ thể ${esc(id)}.</div></section>`);
  const s = d.summary || {};
  const cols = walletCols(false).filter((c) => c.k !== "rank");
  setMain(`<div class="crumbs"><a href="#/entities">Xếp hạng chủ thể</a> › <span class="mono">${esc(id)}</span></div>
    <section class="panel"><div class="hdr"><h1>Chủ thể ${esc(id)}</h1>${s.rank ? `<span class="bd cr">Hạng #${s.rank}</span>` : `<span class="bd id">Không xếp hạng</span>`}${flagBadges(s.flags)}</div>
      ${(s.flags || []).includes("CREATOR_GROUP") ? `<div class="warnbox">CREATOR_GROUP: cụm chứa creator có liên kết đã xác minh. Không xếp hạng; dữ liệu và bằng chứng giữ nguyên.</div>` : ""}</section>
    <section class="kpis k6">${kpi("Điểm", s.score ?? "–", "mỗi coin tính 1 lần")}${kpi("Số ví", s.members, `${s.buyers} ví mua trong cửa sổ`)}
      ${kpi("Vào sớm", (s.early_coins || []).join(", ") || "–", "coin 0–60′", "sm")}${kpi("Coin", (s.coins || []).join(", ") || "–", "", "sm")}
      ${kpi("SOL vào", fmt(s.sol_in, 3), "tổng các ví", "sm")}${kpi("Liên kết nội bộ", d.links.length, "tín hiệu giữa các ví", "sm")}</section>
    <section class="panel"><header><h2>Thành viên</h2></header>${table("em-" + id, cols, d.members, { href: (r) => `#/wallet/${r.wallet}`, sort: "score" })}</section>
    <section class="panel"><header><h2>Liên kết trong cụm</h2></header>${table("el-" + id, [
      { k: "a", t: "Ví A", sort: false, f: (r) => walletLink(r.a) }, { k: "b", t: "Ví B", sort: false, f: (r) => walletLink(r.b) },
      { k: "signal", t: "Tín hiệu" }, { k: "strength", t: "Mức", f: (r) => `<span class="bd ${r.strength === "STRONG" ? "ls" : "pl"}">${esc(r.strength)}</span>` },
      { k: "coin", t: "Coin", f: (r) => esc(r.coin || "–") },
      { k: "ev", t: "Bằng chứng", sort: false, f: (r) => `<span class="small muted">${esc(evid(r.evidence))}</span>${r.sig ? ` <a class="ext" href="${SOLSCAN.tx(r.sig)}" target="_blank" rel="noopener">tx ↗</a>` : ""}` }], d.links, { per: 25 })}</section>`, () => viewEntity(id));
}

// ---------------------------------------------------------------- guide
function viewGuide() {
  setMain(`<section class="panel"><header><h2>Chú giải</h2></header><div class="legend">
    <div><span class="bd cg">CREATOR_GROUP</span><span>Nhóm phát hành: cụm chứa creator có liên kết đã xác minh (ví dụ creator SCINU chuyển 16 SOL cho creator CHUCHU). Không xếp hạng, không xóa dữ liệu.</span></div>
    <div><span class="bd cr">CREATOR</span><span>Ví tạo coin. Chỉ là nhãn: không có liên kết thì vẫn xếp hạng.</span></div>
    <div><span class="bd co">COORD</span><span>COORDINATED_PATTERN: giao dịch đồng bộ (cùng slot nhiều lần, cổng ký chung…). Mô tả hành vi, không trừ điểm, không kết luận cùng chủ.</span></div>
    ${Object.keys(CLS).map((k) => `<div>${clsBadge(k)}<span>${esc(CLS[k][2])}</span></div>`).join("")}
    <div><span class="bd bot">BOT?</span><span>≥ 10 giao dịch và thời gian giữ trung vị dưới 60 giây. Chỉ là cờ.</span></div>
    <div><span class="bd nv">NEEDS_MINT_VERIFICATION</span><span>Chưa có mint xác minh từ nguồn gốc (MOONX, COGE). Không phân tích, không chấm điểm.</span></div>
    <div>${bk("0-15")}${bk("15-60")}${bk("60-120")}<span>Mốc của lần mua đầu tính từ lúc tạo coin.</span></div>
  </div></section>
  <section class="panel"><header><h2>Quy tắc chấm điểm</h2></header><div class="note">
    Cửa sổ: t_create → +120 phút; không dùng dữ liệu sau phút 120 để chấm điểm.<br>
    Điểm mỗi coin: vào 0–15′ = 2, 15–60′ = 1; giá phút 120 / giá vốn ≥ 2x +1, ≥ 5x +2; GOOD (+50% trong cửa sổ) +1. Chủ thể: mỗi coin chỉ tính 1 lần.<br>
    Vai trò: ACCUMULATE (≥ 2 lệnh mua, SOL ròng ≥ 1, không bán) · EARLY_SELL (bán ≥ 50% trước phút 120) · HOLD (giữ ≥ 50% tại phút 120) · GOOD.<br>
    Giao dịch bằng USDC/USDT: tính theo lượng token; PnL bằng SOL là INSUFFICIENT_DATA (không quy đổi).</div></section>`, viewGuide);
}

// ---------------------------------------------------------------- RADAR
const R = { min: LS.get("esm.rmin", 4), multi: LS.get("esm.rmulti", false), hideCr: LS.get("esm.rhidecr", true), grp: "same", sq: "", scoin: "", seen: LS.get("esm.seenLive", 0), timer: null,
  notify: LS.get("esm.notify", false) };
async function radarData() {
  delete S.cache["radar" + "min_score=" + R.min];
  return api("radar", "min_score=" + R.min);
}
const rankChip = (w) => `<a class="bd ${w.multi ? "cr" : "ch"}" href="#/wallet/${w.wallet}" title="${esc(w.wallet)}">#${w.rank} ${short(w.wallet, 3)}${w.flags.some((f) => f.startsWith("COORD")) ? " ⚙" : ""}</a>`;
const priceCell = (r) => r.price_sol_per_1m ? `${fmt(r.price_sol_per_1m, 4)}<span class="sub">SOL / 1M token</span>` : (r.stable_in ? `<span class="dim">USDC</span>` : "–");

async function viewRadar() {
  const d = await radarData();
  if (!d.smart) return setMain(`<section class="panel"><div class="warnbox">Database chưa có dữ liệu. Chạy <code>python -m insiders.esm import</code>.</div></section>`);
  $("#updated").textContent = `Dữ liệu lịch sử nạp: ${utc(d.imported_at)}`;
  const isCr = (x) => (x.creator_of || []).length > 0 || (x.flags || []).some((f) => f.startsWith("CREATOR:"));
  const nCr = d.smart.filter(isCr).length;
  const smart = d.smart.filter((s) => (!R.multi || s.multi) && !(R.hideCr && isCr(s)));
  const live = d.live;
  const liveBox = live.on
    ? `<div class="hdr"><span class="pill good"><span class="dot"></span>LIVE: ĐANG CHẠY</span><span class="small muted">Nguồn: tracker on-chain · sự kiện cuối ${utc(live.last_event)}</span></div>`
    : `<div class="hdr"><span class="pill bad">LIVE: TẮT</span><span class="small muted">${esc(live.note || "")}. Không có tín hiệu trực tiếp nào được hiển thị; mọi tín hiệu bên dưới là <b>LỊCH SỬ</b> từ 5 coin đã nghiên cứu.</span></div>`;
  const alertCols = [
    { k: "t", t: "Thời điểm", f: (r) => `<span class="mono small">${utc(r.t)}</span>` },
    { k: "wallet", t: "Ví Smart Money", sort: false, f: (r) => walletLink(r.wallet) },
    { k: "symbol", t: "Coin mới", f: (r) => `<span class="mono">${esc(r.symbol || short(r.mint, 4))}</span><a class="ext" href="${SOLSCAN.tok(r.mint)}" target="_blank" rel="noopener">↗</a>` },
    { k: "sol", t: "SOL", num: true, f: (r) => fmt(r.sol, 3) },
    { k: "price_sol_per_1m", t: "Giá vào", num: true, f: priceCell },
    { k: "class", t: "Bằng chứng ví", sort: false, f: (r) => `<div class="bds">${clsBadge(r.class)}${flagBadges(r.flags)}</div>` },
    { k: "sig", t: "Tx", sort: false, f: (r) => `<a href="${SOLSCAN.tx(r.sig)}" target="_blank" rel="noopener">↗</a>` },
  ];
  const smartCols = [
    { k: "rank", t: "#", num: true, cls: "rank" },
    { k: "wallet", t: "Ví", sort: false, f: (r) => walletLink(r.wallet) + `<span class="sub">${r.multi ? `<span class="bd cr">ĐA COIN</span>` : `<span class="bd st">1 COIN · điểm ≥ ${d.min_score}</span>`}${r.cohort ? ` <span class="bd st">${esc(r.cohort)}</span>` : ""}</span>` },
    { k: "score", t: "Điểm", num: true, f: (r) => `<span class="score">${r.score}</span>` },
    { k: "early", t: "Vào sớm (phút)", sort: false, f: (r) => `<div class="bds">${r.entries.filter((x) => x.bucket !== "60-120").map((x) => `<a class="bd ${x.at_creation ? "cr" : "ch"}" href="#/coin/${x.mint}" ${x.at_creation ? 'title="Mua trong giao dịch tạo coin: giá vào = giá khởi tạo"' : ""}>${esc(x.symbol)} ${x.at_creation ? "lúc tạo" : fmt(x.first_buy_min, 1) + "′"}</a>`).join("")}</div>`, sv: (r) => r.early_coins.length },
    { k: "best_rise", t: "Tăng sau vào", num: true, f: (r) => r.best_rise === null && (r.creator_of || []).length ? `<span class="dim" title="Chỉ có lệnh mua lúc tạo coin: không tính hiệu suất">– <span class="sub">mua lúc tạo</span></span>` : xf(r.best_rise) },
    { k: "avg_result_pct", t: "KQ cửa sổ TB", num: true, f: (r) => r.avg_result_pct === null ? `<span class="dim" title="${r.perf_entries ? "PnL INSUFFICIENT_DATA" : "Chỉ có lệnh mua lúc tạo coin: không tính hiệu suất"}">–${r.perf_entries ? "" : `<span class="sub">mua lúc tạo</span>`}</span>` : `<span class="${r.avg_result_pct >= 0 ? "good" : "bad"}">${fmt(r.avg_result_pct * 100, 0)}%</span><span class="sub">${r.results_known}/${r.perf_entries} coin${r.perf_entries < r.entries.length ? " · bỏ lệnh mua lúc tạo" : ""}</span>` },
    { k: "class", t: "Bằng chứng", sort: false, f: (r) => `<div class="bds">${clsBadge(r.class)}${r.entity ? `<a class="bd ch" href="#/entity/${r.entity}">${r.entity}</a>` : ""}${flagBadges(r.flags, r.bot)}</div>` },
    { k: "why", t: "Vì sao phát hiện", sort: false, f: (r) => `<span class="small muted">${r.why.map(esc).join(" · ")}</span>` },
  ];
  const sigs = d.signals.filter((g) => !(R.hideCr && isCr(g)) && (!R.scoin || g.symbol === R.scoin) && (!R.sq || g.wallet.toLowerCase().includes(R.sq.toLowerCase())) && (!R.multi || g.multi));
  const sigCols = [
    { k: "t", t: "Thời điểm (UTC)", f: (r) => `<span class="bd st">LỊCH SỬ</span><span class="sub mono">${utc(r.t)}</span>` },
    { k: "wallet", t: "Ví", sort: false, f: (r) => walletLink(r.wallet) + `<span class="sub">#${r.rank}${r.multi ? " · đa coin" : ""}</span>` },
    { k: "symbol", t: "Coin", f: (r) => `<a href="#/coin/${r.mint}">${esc(r.symbol)}</a>` },
    { k: "minute", t: "Phút sau tạo", num: true, f: (r) => `${fmt(r.minute, 2)}<span class="sub">${bk(r.bucket)}</span>` },
    { k: "price_sol_per_1m", t: "Giá vào", num: true, f: (r) => priceCell(r) + (r.at_creation ? `<span class="sub"><span class="bd cr">GIÁ KHỞI TẠO</span> mua trong giao dịch tạo coin</span>` : "") },
    { k: "sol_in", t: "SOL vào", num: true, f: (r) => fmt(r.sol_in, 3) },
    { k: "why", t: "Bằng chứng", sort: false, f: (r) => `<div class="bds">${clsBadge(r.class)}${flagBadges(r.flags)}</div><span class="sub">${r.why.map(esc).join(" · ")}</span>` },
    { k: "sig", t: "Tx", sort: false, f: (r) => `<a href="${SOLSCAN.tx(r.sig)}" target="_blank" rel="noopener">↗</a>` },
  ];
  const g = d.groups;
  const grpBody = {
    same: table("rg-same", [
      { k: "entity", t: "Chủ thể", f: (r) => `<a class="mono" href="#/entity/${r.entity}">${r.entity}</a>` }, { k: "members", t: "Số ví", num: true },
      { k: "early", t: "Vào sớm", f: (r) => `<div class="bds">${r.early_coins.map(coinChip).join("") || "–"}</div>`, sv: (r) => r.early_coins.length },
      { k: "score", t: "Điểm (1 lần/coin)", num: true }, { k: "classes", t: "Phân loại", sort: false, f: (r) => `<div class="bds">${r.classes.map(clsBadge).join("")}</div>` },
      { k: "flags", t: "Cờ", sort: false, f: (r) => `<div class="bds">${flagBadges(r.flags)}</div>` }], g.same_owner, { href: (r) => `#/entity/${r.entity}`, sort: "score", per: 15 }),
    coord: table("rg-coord", [
      { k: "id", t: "Nhóm", f: (r) => `<span class="bd co">${esc(r.id)}</span>` }, { k: "n", t: "Số ví", num: true, f: (r) => r.wallets.length, sv: (r) => r.wallets.length },
      { k: "coins", t: "Coin", sort: false, f: (r) => `<div class="bds">${((r.evidence || {}).coins || []).map(coinChip).join("")}</div>` },
      { k: "ev", t: "Bằng chứng hành vi", sort: false, f: (r) => `<span class="small muted">${esc(coordEv(r.evidence))}</span>` },
      { k: "w", t: "Ví", sort: false, f: (r) => `<div class="bds">${r.wallets.slice(0, 8).map((w) => `<a class="bd ch" href="#/wallet/${w}">${short(w, 3)}</a>`).join("")}${r.wallets.length > 8 ? `<span class="dim small">+${r.wallets.length - 8}</span>` : ""}</div>` }],
      g.coordinated, { sort: "n", per: 20 }),
    creator: table("rg-cr", [
      { k: "wallet", t: "Creator", sort: false, f: (r) => walletLink(r.wallet) }, { k: "coin", t: "Coin", f: (r) => coinChip(r.coin) },
      { k: "in_group", t: "Trạng thái", f: (r) => r.in_group ? `<span class="bd cg">CREATOR_GROUP: có liên kết đã xác minh, không xếp hạng</span>` : `<span class="bd cr">CREATOR: chỉ là nhãn, không có liên kết, vẫn xếp hạng</span>` }],
      g.creators, { per: 10 }) + g.creator_groups.map((e) => `<div class="note">Nhóm <a href="#/entity/${e.entity}">${e.entity}</a>: ${e.members} ví, coin ${e.coins.join(", ")}.</div>`).join(""),
  };
  setMain(`
    <section class="panel">${liveBox}</section>
    <section class="panel"><header><h2>Cảnh báo: ví Smart Money mua coin mới</h2><span class="grow"></span>
        ${live.on ? `<label class="chk"><input type="checkbox" id="ntf" ${R.notify ? "checked" : ""}> Thông báo trình duyệt</label>` : ""}</header>
      ${live.on ? (live.alerts.length ? table("r-live", alertCols, live.alerts, { sort: "t", per: 20 }) : `<div class="empty">Tracker đang chạy. Chưa thấy ví nào trong danh sách theo dõi mua coin mới.</div>`)
        : `<div class="empty">Không có cảnh báo vì nguồn LIVE đang tắt. Hệ thống không giả lập tín hiệu.</div>`}
    </section>
    <section class="panel"><header><h2>Ví Smart Money đáng theo dõi</h2><span class="grow"></span><span class="count">${smart.length} ví · lọc từ ${d.candidates} ví vào sớm được xếp hạng</span></header>
      <div class="tools"><span class="small muted">Ví 1 coin cần điểm ≥</span>
        <div class="seg">${[3, 4, 5].map((v) => `<button data-min="${v}" class="${R.min === v ? "on" : ""}">${v}</button>`).join("")}</div>
        <label class="chk"><input type="checkbox" id="rmulti" ${R.multi ? "checked" : ""}> Chỉ ví vào sớm ≥ 2 coin</label>
        <label class="chk" title="Ẩn ví mang nhãn CREATOR (ví tạo coin). Không đổi điểm, hạng hay phân loại."><input type="checkbox" id="rhidecr" ${R.hideCr ? "checked" : ""}> Ẩn creator${nCr ? ` (${nCr})` : ""}</label></div>
      ${table("r-smart", smartCols, smart, { href: (r) => `#/wallet/${r.wallet}`, sort: "rank", dir: 1, per: 25 })}
      <div class="note">Thứ tự theo bằng chứng đã duyệt: số coin vào sớm (0–60′), rồi điểm, rồi phút vào đầu. ĐA COIN = vào sớm ở ≥ 2 coin. Ví 1 coin chỉ hiện khi điểm đạt ngưỡng đã chọn. CREATOR_GROUP không bao giờ có mặt. Ví creator (nhãn CREATOR) bị ẩn khi bật "Ẩn creator"; khi hiện, lệnh mua trong giao dịch tạo coin ghi rõ "lúc tạo" và không được tính vào cột hiệu suất. Hạng (#) giữ nguyên hạng gốc.</div>
    </section>
    <section class="panel"><header><h2>Coin và ví Smart Money tham gia sớm</h2></header>
      <div class="coins">${d.coins.map((c) => `<div class="ccard" data-go="#/coin/${c.mint}">
        <div class="hdr"><span class="sym">${esc(c.symbol)}</span>${c.quote !== "SOL" ? `<span class="bd st">${esc(c.quote)}</span>` : ""}<span class="grow"></span><span class="small muted">phút 120: ${xf(c.x_120m)}</span></div>
        <div class="small muted">Tạo ${utc(c.t_create)}</div>
        <div><span class="bk b0">0–15′</span> <span class="small">${c.w0_15.length} ví</span><div class="bds" style="margin-top:4px">${c.w0_15.filter((w) => !(R.hideCr && isCr(w))).slice(0, 12).map(rankChip).join("") || `<span class="dim small">không có</span>`}${c.w0_15.length > 12 ? `<span class="dim small">+${c.w0_15.length - 12}</span>` : ""}</div></div>
        <div><span class="bk b1">15–60′</span> <span class="small">${c.w15_60.length} ví</span><div class="bds" style="margin-top:4px">${c.w15_60.slice(0, 12).map(rankChip).join("") || `<span class="dim small">không có</span>`}${c.w15_60.length > 12 ? `<span class="dim small">+${c.w15_60.length - 12}</span>` : ""}</div></div>
      </div>`).join("")}
      ${d.pending.map((p) => `<div class="ccard pending"><div class="hdr"><span class="sym">${esc(p)}</span><span class="grow"></span><span class="bd nv">NEEDS_MINT_VERIFICATION</span></div><div class="small muted">Chưa theo dõi: mint chưa xác minh.</div></div>`).join("")}</div>
      <div class="note">Thẻ màu xanh = ví đa coin; ⚙ = thuộc nhóm giao dịch đồng bộ.</div>
    </section>
    <section class="panel"><header><h2>Tín hiệu</h2><span class="bd st">LỊCH SỬ</span><span class="grow"></span><span class="count">${sigs.length}</span></header>
      <div class="tools"><input type="search" id="rsq" placeholder="Tìm địa chỉ ví…" value="${esc(R.sq)}">
        <select id="rscoin"><option value="">Mọi coin</option>${d.coins.map((c) => `<option ${R.scoin === c.symbol ? "selected" : ""}>${esc(c.symbol)}</option>`).join("")}</select></div>
      ${table("r-sig", sigCols, sigs, { sort: "t", per: 25 })}
      <div class="note">Tín hiệu lịch sử: lần mua đầu (0–60′) của ví trong danh sách theo dõi. Danh sách được chọn từ chính 5 coin này, nên đây là kiểm chứng trong mẫu, không phải dự báo. Tín hiệu trực tiếp chỉ xuất hiện ở mục Cảnh báo khi tracker chạy.</div>
    </section>
    <section class="panel"><header><h2>Nhóm ví</h2><span class="grow"></span>
      <div class="seg">${[["same", `Cùng chủ (${g.same_owner.length})`], ["coord", `Bot phối hợp (${g.coordinated.length})`], ["creator", `Creator (${g.creators.length})`]].map(([k, t]) => `<button data-grp="${k}" class="${R.grp === k ? "on" : ""}">${t}</button>`).join("")}</div></header>
      ${grpBody[R.grp]}
      <div class="note">${{ same: "Cùng chủ: các ví nối bằng bằng chứng mạnh (chuyển tiền, chung nguồn vốn, trả phí hộ…). Mỗi coin tính 1 lần cho cả nhóm.", coord: "Bot phối hợp: giao dịch cùng slot nhiều lần hoặc qua cổng ký chung. Chỉ là nhãn hành vi, không trừ điểm, không kết luận cùng chủ.", creator: "Creator: chỉ nhóm creator có liên kết đã xác minh mới bị loại khỏi xếp hạng." }[R.grp]}</div>
    </section>`, viewRadar);
  $$("[data-min]").forEach((b) => b.onclick = () => { R.min = Number(b.dataset.min); LS.set("esm.rmin", R.min); viewRadar(); });
  $("#rmulti").onchange = (e) => { R.multi = e.target.checked; LS.set("esm.rmulti", R.multi); viewRadar(); };
  $("#rhidecr").onchange = (e) => { R.hideCr = e.target.checked; LS.set("esm.rhidecr", R.hideCr); viewRadar(); };
  $$("[data-grp]").forEach((b) => b.onclick = () => { R.grp = b.dataset.grp; viewRadar(); });
  $("#rscoin").onchange = (e) => { R.scoin = e.target.value; viewRadar(); };
  let tm; $("#rsq").oninput = (e) => { clearTimeout(tm); tm = setTimeout(() => { R.sq = e.target.value.trim(); viewRadar().then(() => { const i = $("#rsq"); i.focus(); i.setSelectionRange(i.value.length, i.value.length); }); }, 220); };
  $$(".ccard[data-go]").forEach((x) => x.onclick = (e) => { if (!e.target.closest("a")) location.hash = x.dataset.go; });
  const ntf = $("#ntf");
  if (ntf) ntf.onchange = async (e) => { R.notify = e.target.checked; if (R.notify && "Notification" in window && Notification.permission === "default") await Notification.requestPermission(); LS.set("esm.notify", R.notify); };
  liveWatch(live);
}
function coordEv(e) {
  if (!e) return "";
  if (e.evidence) return `cổng ký chung ${short((e.evidence.all_buys_cosigned_by || [])[0] || "", 4)} · nhịp ${e.evidence.buy_interval_s ? e.evidence.buy_interval_s.median + " s" : "?"} · ${e.evidence.same_funder_wallets} ví cùng nguồn vốn (busy hub)`;
  return `${e.shared_slots} slot chung · cùng phí ${fmt((e.same_fee_share || 0) * 100, 0)}% · ${e.own_fee_payer ? "mỗi ví tự trả phí" : "có ví trả phí hộ"}`;
}
function liveWatch(live) {
  clearInterval(R.timer);
  if (!live.on) return;
  const newest = Math.max(0, ...live.alerts.map((a) => a.t || 0));
  if (newest > R.seen) {
    const fresh = live.alerts.filter((a) => (a.t || 0) > R.seen);
    if (R.seen) {
      toast(`${fresh.length} cảnh báo mới: ví Smart Money mua coin mới`);
      if (R.notify && "Notification" in window && Notification.permission === "granted")
        new Notification("Smart Money mua coin mới", { body: fresh.slice(0, 3).map((a) => `${short(a.wallet)} → ${a.symbol || short(a.mint)}`).join("\n") });
    }
    R.seen = newest; LS.set("esm.seenLive", newest);
  }
  R.timer = setInterval(() => { if ((location.hash || "#/") === "#/" || location.hash === "") viewRadar(); else clearInterval(R.timer); }, 30000);
}

// ---------------------------------------------------------------- router
async function route() {
  const h = (location.hash || "#/").slice(2).split("/");
  const [page, arg] = [h[0] || "", h[1]];
  nav({ wallet: "wallets", entity: "entities", coin: "coins" }[page] || page);
  try {
    if (page === "wallets") await viewWallets();
    else if (page === "entities") await viewEntities();
    else if (page === "coins") await viewCoins();
    else if (page === "coin" && arg) await viewCoin(arg);
    else if (page === "wallet" && arg) await viewWallet(arg);
    else if (page === "entity" && arg) await viewEntity(arg);
    else if (page === "guide") viewGuide();
    else if (page === "trace") await viewTrace(arg, h[2], h[3]);
    else if (page === "stats") await viewOverview();
    else await viewRadar();
    $$(".ccard[data-go]").forEach((x) => x.onclick = () => location.hash = x.dataset.go);
    if (!$("#updated").textContent.startsWith("Dữ liệu")) api("summary").then((s) => { if (s.imported) $("#updated").textContent = `Dữ liệu nạp: ${utc(s.imported_at)} · ${s.rules_version}`; });
  } catch (e) {
    if (e.message !== "auth") setMain(`<section class="panel"><div class="warnbox">Lỗi tải dữ liệu: ${esc(e.message)}</div></section>`);
  }
}
window.addEventListener("hashchange", () => { window.scrollTo(0, 0); route(); });
route();


// ---------------------------------------------------------------- wallet trace (/api/insiders/trace/*)
async function tapi(name, qs, body) {
  const r = await fetch(`/api/insiders/trace/${name}${qs ? "?" + qs : ""}`, body === undefined ? { headers: { "X-Access-Code": S.code }, cache: "no-store" }
    : { method: "POST", headers: { "X-Access-Code": S.code, "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (r.status === 401) { login("Cần mã truy cập"); throw new Error("auth"); }
  const d = await r.json().catch(() => ({}));
  if (r.status === 503) return { off: true, note: d.note };
  if (!r.ok) throw new Error(d.error || "http " + r.status);
  return d;
}
const T = { timer: null };
const TAG = { CREATOR: "cg", CREATOR_GROUP: "cg", DEV: "cg", DEPTH: "cr", STANK: "cr", INSIDERS: "cr", HUB: "bot", EXCHANGE: "bot", FEE: "bot" };
const tagBadges = (tags) => (tags || []).map((t) => `<span class="bd ${TAG[t] || (/^E\d+$/.test(t) ? "ls" : "ch")}" title="${esc(tagHelp(t))}">${esc(t)}</span>`).join("");
function tagHelp(t) {
  return { CREATOR: "Ví tạo coin (Early Smart Money)", CREATOR_GROUP: "Thuộc nhóm phát hành (cụm chứa creator)", DEV: "Ví tạo coin trên luồng New Coins (dev)",
    DEPTH: "Cohort DEPTH", STANK: "Cohort STANK", INSIDERS: "Cohort INSIDERS", HUB: "Hub: ≥ 100 giao dịch, ≥ 1 giao dịch/phút (không mở rộng)",
    EXCHANGE: "Sàn (nhãn đã biết)", FEE: "Ví phí (nhãn đã biết)" }[t] || (/^E\d+$/.test(t) ? "Chủ thể Early Smart Money " + t : t);
}
const asset = (a) => a === "FEE" ? `<span class="bd co" title="Ví này trả phí giao dịch cho ví kia (cùng ký một giao dịch): quan hệ, không phải chuyển tiền">TRẢ PHÍ HỘ</span>` : a === "SOL" ? "SOL" : `<a class="mono" href="${SOLSCAN.tok(a)}" target="_blank" rel="noopener" title="${a}">${short(a, 4)}</a>`;
const amt = (e) => e.asset === "SOL" ? fmt(e.amount, 4) + " SOL" : e.asset === "FEE" ? `phí ${fmt(e.amount, 6)} SOL` : fmt(e.amount, 2);
const tlink = (w, jid) => `<span class="addr"><a href="#/trace/${jid}/${w}" title="${w}">${short(w, 5)}</a><span class="copy" data-copy="${w}" title="Chép">⧉</span><a class="ext" href="${SOLSCAN.acc(w)}" target="_blank" rel="noopener">↗</a></span>`;
const sigLinks = (sigs) => (sigs || []).filter(Boolean).slice(0, 3).map((g) => `<a class="ext" href="${SOLSCAN.tx(g)}" target="_blank" rel="noopener" title="${g}">${g.slice(0, 6)}↗</a>`).join(" ");

async function viewTrace(arg, a2, a3) {
  clearInterval(T.timer);
  if (arg && arg !== "new") return viewTraceJob(Number(arg), a2);
  const st = await tapi("status");
  if (st.off) return setMain(`<section class="panel"><div class="warnbox">Truy vết ví đang tắt trên máy chủ này (${esc(st.note || "")}). Không có lệnh gọi RPC nào được thực hiện.</div></section>`);
  const js = await tapi("jobs");
  const pre = arg === "new" && a2 ? a2 : "";
  const cols = [
    { k: "id", t: "#", num: true }, { k: "root", t: "Ví gốc", sort: false, f: (r) => walletLink(r.root) },
    { k: "status", t: "Trạng thái", f: (r) => `<span class="bd ${r.status === "done" ? "in" : r.status === "running" ? "cr" : "id"}">${esc(r.status)}</span>${r.note ? `<span class="sub">${esc(r.note)}</span>` : ""}` },
    { k: "hops", t: "Tầng / chiều", f: (r) => `${r.hops} · ${esc({ both: "hai chiều", in: "tiền vào", out: "tiền ra" }[r.direction])}` },
    { k: "nodes", t: "Ví", num: true, f: (r) => `${r.nodes}${r.todo ? `<span class="sub">${r.todo} chờ</span>` : ""}` },
    { k: "calls", t: "Lượt RPC", num: true }, { k: "created", t: "Tạo", f: (r) => `<span class="small">${utc(r.created)}</span>` }];
  setMain(`
    <section class="panel"><header><h2>Truy vết ví</h2><span class="grow"></span><span class="small muted">${fmt(st.calls_today, 0)} / ${fmt(st.day_cap, 0)} lượt RPC hôm nay · ${st.running} đang chạy</span></header>
      <form id="tf" class="tools">
        <input type="search" id="tw" placeholder="Địa chỉ ví bất kỳ (base58)…" value="${esc(pre)}" required pattern="[1-9A-HJ-NP-Za-km-z]{32,44}">
        <select id="th" title="Số tầng">${[1, 2, 3].map((v) => `<option value="${v}" ${v === 2 ? "selected" : ""}>${v} tầng</option>`).join("")}</select>
        <select id="td"><option value="both">Hai chiều</option><option value="in">Chỉ tiền vào (nguồn vốn)</option><option value="out">Chỉ tiền ra</option></select>
        <label class="chk">SOL tối thiểu <input id="tm" type="number" min="0" step="0.01" value="0.1" style="width:70px"></label>
        <label class="chk">Giao dịch/ví <input id="ts" type="number" min="5" max="100" value="40" style="width:60px"></label>
        <button class="btn primary">Bắt đầu truy vết</button></form>
      <div class="note">Đọc chữ ký và giao dịch của ví (chỉ đọc, không ký, không gửi), ghi mọi lần chuyển SOL / token vào và ra; ví đối tác được đọc tiếp đến số tầng đã chọn.
        Sàn và hub (≥ 100 giao dịch, ≥ 1/phút) chỉ hiển thị, không mở rộng. Mỗi dòng tiền kèm chữ ký giao dịch để đối chiếu trên Solscan.
        Ví tìm được là ví <b>có liên quan</b> theo bằng chứng on-chain, không phải kết luận cùng chủ.</div></section>
    <section class="panel"><header><h2>Các lần truy vết</h2><span class="grow"></span><span class="count">${js.jobs.length}</span></header>
      ${js.jobs.length ? table("tr-jobs", cols, js.jobs, { href: (r) => `#/trace/${r.id}`, sort: "id" }) : `<div class="empty">Chưa có lần truy vết nào</div>`}</section>`, () => viewTrace(arg, a2, a3));
  $("#tf").onsubmit = async (e) => {
    e.preventDefault();
    try {
      const r = await tapi("start", "", { wallet: $("#tw").value.trim(), hops: Number($("#th").value), direction: $("#td").value, min_sol: Number($("#tm").value), sigs: Number($("#ts").value) });
      location.hash = `#/trace/${r.job.id}`;
    } catch (err) { toast("Không bắt đầu được: " + err.message); }
  };
}

async function viewTraceJob(id, focus) {
  clearInterval(T.timer);
  const d = await tapi("job", "id=" + id + (focus ? "&focus=" + focus : ""));
  if (d.off) return viewTrace();
  const j = d.job, nodes = d.nodes;
  const byState = nodes.reduce((m, n) => (m[n.state] = (m[n.state] || 0) + 1, m), {});
  const running = j.status === "running";
  const sumAsset = (dir) => d.edges.filter((e) => e.asset === "SOL" && (dir === "in" ? e.dst === j.root : e.src === j.root)).reduce((a, e) => a + e.amount, 0);
  const nodeCols = [
    { k: "hop", t: "Tầng", num: true }, { k: "wallet", t: "Ví", sort: false, f: (r) => tlink(r.wallet, id) },
    { k: "tags", t: "Hệ thống đã biết", sort: false, f: (r) => `<div class="bds">${tagBadges(r.tags)}${r.esm_class ? clsBadge(r.esm_class) : ""}</div>${r.creator_of ? `<span class="sub">tạo coin: ${esc(r.creator_of.join(", "))}</span>` : ""}${r.dev_coins ? `<span class="sub">dev ${r.dev_coins} coin: ${esc((r.dev_symbols || []).join(", "))}</span>` : ""}`, sv: (r) => (r.tags || []).length },
    { k: "via", t: "Tìm thấy qua", sort: false, f: (r) => r.via ? `${short(r.via, 4)} ${sigLinks([r.via_sig])}` : `<span class="bd cr">VÍ GỐC</span>` },
    { k: "state", t: "Trạng thái", f: (r) => `<span class="bd ${r.state === "done" ? "in" : r.state === "todo" ? "cr" : "id"}">${esc(r.state)}</span>${r.reason ? `<span class="sub">${esc(r.reason)}</span>` : ""}` },
    { k: "flows", t: "Dòng tiền / tx đọc", num: true, f: (r) => `${r.flows || 0} / ${r.tx_read || 0}` },
    { k: "newest_ts", t: "Giao dịch đã đọc", f: (r) => r.newest_ts ? `<span class="small">${utc(r.oldest_ts)} →<br>${utc(r.newest_ts)}</span>` : "–" }];
  const flowCols = [
    { k: "ts", t: "Thời điểm (UTC)", f: (r) => `<span class="mono small">${utc(r.ts)}</span>` },
    { k: "src", t: "Từ", sort: false, f: (r) => tlink(r.src, id) }, { k: "dst", t: "Đến", sort: false, f: (r) => tlink(r.dst, id) },
    { k: "asset", t: "Tài sản", f: (r) => asset(r.asset) }, { k: "amount", t: "Lượng", num: true, f: amt },
    { k: "sig", t: "Chữ ký giao dịch", sort: false, f: (r) => `<a class="mono small" href="${SOLSCAN.tx(r.sig)}" target="_blank" rel="noopener" title="${r.sig}">${short(r.sig, 8)} ↗</a>` }];
  const matchCols = [
    { k: "wallet", t: "Ví đã biết", sort: false, f: (r) => tlink(r.wallet, id) + (r.label ? `<span class="sub">${esc(r.label)}</span>` : "") },
    { k: "tags", t: "Khớp với", sort: false, f: (r) => `<div class="bds">${tagBadges(r.tags)}</div>${r.creator_of ? `<span class="sub">tạo coin: ${esc(r.creator_of.join(", "))}</span>` : ""}` },
    { k: "hops", t: "Số bước", num: true, f: (r) => r.path.length, sv: (r) => r.path.length },
    { k: "path", t: "Đường đi của tiền (từ ví gốc)", sort: false, f: (r) => r.path.map((p) => `<div class="small">${short(p.from, 4)} → ${short(p.to, 4)} · ${p.asset === "SOL" ? fmt(p.amount, 3) + " SOL" : p.asset === "FEE" ? "trả phí hộ" : fmt(p.amount, 0) + " " + short(p.asset, 3)}${p.n > 1 ? ` (${p.n} lần)` : ""} ${sigLinks(p.sigs)}</div>`).join("") }];
  const histCols = [
    { k: "ts", t: "Thời điểm (UTC)", f: (r) => `<span class="mono small">${utc(r.ts)}</span>` },
    { k: "kind", t: "Loại", f: (r) => `<span class="bd ${r.kind === "BUY" || r.kind === "CREATE" ? "in" : r.kind === "SELL" ? "cg" : "ch"}">${esc(r.kind)}</span>` },
    { k: "mint", t: "Token", f: (r) => asset(r.mint) }, { k: "tokens", t: "Lượng token", num: true, f: (r) => fmt(Number(r.tokens), 0) },
    { k: "sol", t: "SOL", num: true, f: (r) => fmt(r.sol, 4) },
    { k: "sig", t: "Chữ ký", sort: false, f: (r) => `<a class="mono small" href="${SOLSCAN.tx(r.sig)}" target="_blank" rel="noopener">${short(r.sig, 8)} ↗</a>` }];
  setMain(`
    <div class="crumbs"><a href="#/trace">Truy vết ví</a> › <span class="mono">#${id} ${short(j.root, 6)}</span></div>
    <section class="panel"><div class="hdr"><h1>${esc(j.root)}</h1><span class="copy" data-copy="${j.root}">⧉</span><a class="ext" href="${SOLSCAN.acc(j.root)}" target="_blank" rel="noopener">solscan ↗</a>
        <span class="grow"></span>${running ? `<span class="pill good"><span class="dot"></span>ĐANG ĐỌC</span><button class="btn small" id="tstop">Dừng</button>` : `<span class="bd ${j.status === "done" ? "in" : "id"}">${esc(j.status)}</span>`}
        <a class="btn small" href="#/wallet/${j.root}">Dữ liệu Early Smart Money</a></div>
      <div class="small muted">${j.hops} tầng · ${esc({ both: "hai chiều", in: "tiền vào", out: "tiền ra" }[j.direction])} · SOL ≥ ${fmt(j.min_sol, 3)} · ${j.sigs} giao dịch/ví · tạo ${utc(j.created)}${j.note ? " · " + esc(j.note) : ""}</div></section>
    <section class="kpis k6">
      ${kpi("Ví trong truy vết", nodes.length, Object.entries(byState).map(([k, v]) => `${v} ${k}`).join(" · "))}
      ${kpi("Ví đối tác (lá)", d.leaves.length, "dưới ngưỡng / ngoài số tầng")}
      ${kpi("Dòng tiền", fmt(d.flows_total, 0), `${d.edges.length} cạnh đã gộp`)}
      ${kpi("SOL vào ví gốc", fmt(sumAsset("in"), 3), "trong giao dịch đã đọc", "sm good")}
      ${kpi("SOL ra từ ví gốc", fmt(sumAsset("out"), 3), "trong giao dịch đã đọc", "sm bad")}
      ${kpi("Khớp hệ thống", d.matches.length, `${fmt(d.rpc_calls, 0)} lượt RPC`, d.matches.length ? "sm warn" : "sm")}</section>
    <section class="panel"><header><h2>Đối chiếu với DEV / Creator, cohort STANK / DEPTH / INSIDERS, chủ thể ESM</h2><span class="grow"></span><span class="count">${d.matches.length}</span></header>
      ${d.matches.length ? table("tr-m-" + id, matchCols, d.matches, { per: 20 }) : `<div class="empty">Chưa thấy ví nào trong truy vết trùng với dữ liệu hệ thống đã có${running ? " (đang đọc tiếp)" : ""}.</div>`}</section>
    <section class="panel"><header><h2>Quan hệ ví</h2><span class="grow"></span><span class="small muted">bấm một ví để xem lịch sử giao dịch của nó</span></header>${traceGraph(d, id)}</section>
    <section class="panel"><header><h2>Dòng tiền SOL / token</h2><span class="grow"></span><span class="count">${d.flows.length}${d.flows_total > d.flows.length ? " / " + d.flows_total : ""}</span></header>
      ${d.flows.length ? table("tr-f-" + id, flowCols, d.flows, { sort: "ts", per: 50 }) : `<div class="empty">Chưa có dòng tiền nào${running ? " (đang đọc)" : ""}.</div>`}</section>
    <section class="panel"><header><h2>Lịch sử giao dịch: <span class="mono">${short(d.history.wallet, 6)}</span></h2><span class="grow"></span><span class="count">${d.history.trades.length}</span></header>
      ${d.history.trades.length ? table("tr-h-" + id + d.history.wallet, histCols, d.history.trades, { sort: "ts", per: 30 }) : `<div class="empty">Không có lệnh mua / bán / chuyển token trong các giao dịch đã đọc${d.history.wallet !== j.root ? " (ví này chỉ được đọc khi ở tầng chưa phải tầng cuối)" : ""}.</div>`}</section>
    <section class="panel"><header><h2>Các ví</h2><span class="grow"></span><span class="count">${nodes.length}</span></header>${table("tr-n-" + id, nodeCols, nodes, { sort: "hop", dir: 1, per: 40 })}</section>`,
    () => viewTraceJob(id, focus));
  const stop = $("#tstop");
  if (stop) stop.onclick = async () => { await tapi("stop", "id=" + id, {}); viewTraceJob(id, focus); };
  $$("[data-node]").forEach((g) => g.onclick = () => location.hash = `#/trace/${id}/${g.dataset.node}`);
  if (running) T.timer = setInterval(() => { if (location.hash.startsWith(`#/trace/${id}`)) viewTraceJob(id, focus); else clearInterval(T.timer); }, 4000);
}

function traceGraph(d, id) {
  // columns = hop (leaves one column further); a node's colour = what the system already knows about it
  const hop = {}; d.nodes.forEach((n) => hop[n.wallet] = n.hop);
  const maxHop = Math.max(0, ...d.nodes.map((n) => n.hop));
  const deg = {}; d.edges.forEach((e) => { deg[e.src] = (deg[e.src] || 0) + 1; deg[e.dst] = (deg[e.dst] || 0) + 1; });
  const leaves = d.leaves.slice().sort((a, b) => (deg[b.wallet] || 0) - (deg[a.wallet] || 0) || (b.tags || []).length - (a.tags || []).length).slice(0, 40);
  const all = d.nodes.concat(leaves.map((l) => ({ ...l, hop: maxHop + 1, leaf: true })));
  const cols = {}; all.forEach((n) => (cols[n.hop] = cols[n.hop] || []).push(n));
  const W = Math.max(640, (maxHop + 2) * 240), rowH = 26;
  const H = Math.max(160, Math.max(...Object.values(cols).map((c) => c.length)) * rowH + 40);
  const pos = {};
  Object.entries(cols).forEach(([h, c]) => c.forEach((n, i) => pos[n.wallet] = { x: 70 + Number(h) * ((W - 160) / Math.max(1, maxHop + 1)), y: 30 + i * rowH }));
  const color = (n) => (n.tags || []).some((t) => ["CREATOR", "CREATOR_GROUP", "DEV"].includes(t)) ? "var(--bad)" : (n.tags || []).some((t) => ["DEPTH", "STANK", "INSIDERS"].includes(t)) ? "var(--accent)"
    : n.entity ? "#f59a92" : (n.tags || []).some((t) => ["HUB", "EXCHANGE", "FEE"].includes(t)) ? "var(--dim)" : n.hop === 0 ? "var(--good)" : "var(--muted)";
  const lines = d.edges.filter((e) => pos[e.src] && pos[e.dst]).map((e) => {
    const a = pos[e.src], b = pos[e.dst], sol = e.asset === "SOL", fee = e.asset === "FEE";
    const w = sol ? Math.min(5, 0.8 + Math.log10(1 + e.amount) * 2) : 1.2;
    return `<path d="M${a.x},${a.y} C${(a.x + b.x) / 2},${a.y} ${(a.x + b.x) / 2},${b.y} ${b.x},${b.y}" fill="none" stroke="${sol ? "var(--good)" : fee ? "var(--warn)" : "#b48cff"}" ${fee ? 'stroke-dasharray="4 3"' : ""} stroke-opacity=".55" stroke-width="${w.toFixed(1)}" marker-end="url(#ah)"><title>${short(e.src, 4)} → ${short(e.dst, 4)} · ${sol ? fmt(e.amount, 4) + " SOL" : fee ? "trả phí hộ" : fmt(e.amount, 2) + " " + short(e.asset, 3)} · ${e.n} lần · ${(e.sigs || []).join(" ")}</title></path>`;
  }).join("");
  const dots = all.filter((n) => pos[n.wallet]).map((n) => { const p = pos[n.wallet];
    return `<g data-node="${n.wallet}" style="cursor:pointer"><circle cx="${p.x}" cy="${p.y}" r="${n.hop === 0 ? 8 : 6}" fill="${color(n)}" ${n.leaf ? 'fill-opacity=".45"' : ""}/>
      <text x="${p.x + 11}" y="${p.y + 4}" font-size="11" font-family="var(--mono)" fill="var(--text)">${short(n.wallet, 4)}${(n.tags || []).length ? " · " + esc(n.tags.slice(0, 2).join(",")) : ""}</text><title>${n.wallet}</title></g>`; }).join("");
  const heads = Object.keys(cols).map((h) => `<text x="${pos[cols[h][0].wallet].x - 10}" y="14" font-size="11" fill="var(--dim)">${Number(h) > maxHop ? "đối tác (lá)" : h === "0" ? "ví gốc" : "tầng " + h}</text>`).join("");
  return `<div style="overflow:auto;max-height:640px"><svg width="${W}" height="${H}" role="img" aria-label="Đồ thị quan hệ ví">
    <defs><marker id="ah" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="var(--muted)"/></marker></defs>
    ${heads}${lines}${dots}</svg></div>
    <div class="note"><span style="color:var(--good)">━</span> SOL (độ dày theo lượng) · <span style="color:#b48cff">━</span> token · <span style="color:var(--warn)">┅</span> trả phí hộ · chấm đỏ = DEV / creator / CREATOR_GROUP · xanh = cohort DEPTH / STANK / INSIDERS · hồng = chủ thể ESM · xám = sàn / hub.${d.leaves.length > leaves.length ? ` Hiển thị ${leaves.length}/${d.leaves.length} ví lá nhiều liên kết nhất; đầy đủ ở bảng Dòng tiền.` : ""}</div>`;
}
