/* New Coins LIVE — every pump.fun coin as it is created, who buys it, which Smart Money, and the evidence.
   Data: /api/nc/* (on-chain logs). Presentation only: one primary signal per coin, risk shown apart. Nothing is signed. */
"use strict";
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const LS = {
  get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch (_) { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (_) { /* private mode */ } },
};
// the strongest opportunity label wins; RISK is never an opportunity label
const PRIORITY = ["HIGH_ALERT", "SMART_MONEY", "MULTI_WALLET", "EARLY_BUY", "NEW"];
const TXT = { HIGH_ALERT: "HIGH ALERT", SMART_MONEY: "SMART", MULTI_WALLET: "MULTI", EARLY_BUY: "EARLY BUY", NEW: "NEW", RISK: "RỦI RO" };
const CHIPS = [["FOCUS", "⭐ Đáng chú ý"], ["", "Tất cả"], ["EARLY_BUY", "Early Buy"], ["SMART_MONEY", "Smart Money"], ["HIGH_ALERT", "High Alert"], ["RISK", "Rủi ro"]];
const FOCUS_AGE = 900;                                     // "Đáng chú ý": Smart Money bought, no risk warning, younger than 15 min
const AGES = [["5m", 300], ["15m", 900], ["1h", 3600], ["2h", 7200]];
const F = Object.assign({ age: 3600, sig: "", minsol: "0", minsm: "0", hasbuy: false, norisk: false, q: "", snd: false }, LS.get("nc.f2", {}));
const S = { code: LS.get("insAccessCode", ""), coins: [], seen: new Set(), alerted: new Set(LS.get("nc.alerted", [])), open: null, skew: 0, first: true };
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const short = (a, n = 4) => a ? `${a.slice(0, n)}…${a.slice(-n)}` : "";
const fmt = (v, d = 2) => v === null || v === undefined ? "–" : Number(v).toLocaleString("en-US", { maximumFractionDigits: d });
const now = () => Date.now() / 1000 + S.skew;
// USD from SOL with the server's SOL price (Jupiter); SOL when the price is not known yet (never guessed)
function usd(sol, small) {
  if (sol === null || sol === undefined) return "–";
  if (!S.solUsd) return `${fmt(sol, 2)} SOL`;
  const v = sol * S.solUsd, a = Math.abs(v);
  const t = a >= 1e6 ? `$${fmt(v / 1e6, 2)}M` : a >= 1e3 ? `$${fmt(v / 1e3, a >= 1e5 ? 0 : 1)}K` : `$${fmt(v, a >= 10 ? 0 : 2)}`;
  return small ? `${t} <span class="why" style="display:inline">(${fmt(sol, 3)} SOL)</span>` : t;
}
const primary = (c) => PRIORITY.find((k) => c.signals.includes(k)) || "NEW";
function age(s) {
  s = Math.max(0, Math.floor(s));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  return `${Math.floor(s / 3600)}h ${String(Math.floor(s % 3600 / 60)).padStart(2, "0")}m`;
}
function toast(html) { const t = $("#toast"); t.innerHTML = html; t.classList.add("on"); clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove("on"), 5000); }
function beep() {
  try { const a = new (window.AudioContext || window.webkitAudioContext)(), o = a.createOscillator(), g = a.createGain();
    o.frequency.value = 880; g.gain.value = 0.06; o.connect(g); g.connect(a.destination); o.start(); o.stop(a.currentTime + 0.15); } catch (_) { /* no audio */ }
}
async function api(path) {
  const r = await fetch(path, { headers: { "X-Access-Code": S.code }, cache: "no-store" });
  if (r.status === 401) { login(); throw new Error("auth"); }
  if (!r.ok) throw new Error("http " + r.status);
  return r.json();
}
function login() {
  if ($("#lg")) return;
  $("#rows").innerHTML = "";
  $("#empty").innerHTML = `<form id="lg" style="display:inline-grid;gap:10px;min-width:300px"><b>Nhập mã truy cập</b>
    <input id="lgc" type="password" style="padding:10px;background:#0e1218;border:1px solid #252e3a;border-radius:8px;color:#e8eef4">
    <button style="padding:10px;background:#33d6ef;border:0;border-radius:8px;font-weight:700;cursor:pointer;color:#06080b">Vào</button></form>`;
  $("#lg").onsubmit = (e) => { e.preventDefault(); S.code = $("#lgc").value.trim(); LS.set("insAccessCode", S.code); $("#empty").innerHTML = ""; tick(); };
}

// ---------------------------------------------------------------- controls (chips + collapsed filter)
function controls() {
  $("#sig").innerHTML = CHIPS.map(([v, t]) => `<button data-v="${v}" class="${v === "FOCUS" ? "focus " : ""}${F.sig === v ? "on" : ""}">${t}</button>`).join("");
  $("#age").innerHTML = AGES.map(([t, v]) => `<button data-v="${v}" class="${F.age === v ? "on" : ""}">${t}</button>`).join("");
  $("#minsol").value = F.minsol; $("#minsm").value = F.minsm; $("#hasbuy").checked = F.hasbuy; $("#norisk").checked = F.norisk; $("#q").value = F.q; $("#snd").checked = F.snd;
  const n = (F.age !== 3600) + (F.minsol !== "0") + (F.minsm !== "0") + F.hasbuy + F.norisk + !!F.q;
  $("#fcount").textContent = n ? `(${n})` : "";
}
function save() { LS.set("nc.f2", F); controls(); render(); }
function bind() {
  $("#sig").onclick = (e) => { const b = e.target.closest("button"); if (b) { F.sig = b.dataset.v; save(); } };
  $("#age").onclick = (e) => { const b = e.target.closest("button"); if (b) { F.age = Number(b.dataset.v); save(); tick(); } };
  $("#minsol").onchange = (e) => { F.minsol = e.target.value; save(); };
  $("#minsm").onchange = (e) => { F.minsm = e.target.value; save(); };
  $("#hasbuy").onchange = (e) => { F.hasbuy = e.target.checked; save(); };
  $("#norisk").onchange = (e) => { F.norisk = e.target.checked; save(); };
  $("#snd").onchange = (e) => { F.snd = e.target.checked; save(); };
  let tm; $("#q").oninput = (e) => { clearTimeout(tm); tm = setTimeout(() => { F.q = e.target.value.trim().toLowerCase(); save(); }, 150); };
  $("#mbtn").onclick = (e) => { e.stopPropagation(); $("#fpop").hidden = true; $("#mlist").hidden = !$("#mlist").hidden; };
  $("#fbtn").onclick = (e) => { e.stopPropagation(); $("#mlist").hidden = true; $("#fpop").hidden = !$("#fpop").hidden; };
  document.addEventListener("click", (e) => { if (!e.target.closest(".menu")) { $("#mlist").hidden = true; $("#fpop").hidden = true; } });
  $("#rows").onclick = (e) => { const r = e.target.closest("[data-m]"); if (r) openCoin(r.dataset.m); };
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeSide(); });
}
function pass(c) {
  if (now() - c.t_create > F.age) return false;
  if (F.sig === "FOCUS") return c.signals.includes("SMART_MONEY") && !c.signals.includes("RISK") && now() - c.t_create < FOCUS_AGE;
  if (F.sig === "RISK" ? !c.signals.includes("RISK") : F.sig && !c.signals.includes(F.sig)) return false;
  if (Number(F.minsol) && (c.buy_sol || 0) < Number(F.minsol)) return false;
  if (Number(F.minsm) && (c.smart || 0) < Number(F.minsm)) return false;
  if (F.hasbuy && !c.buyers) return false;
  if (F.norisk && c.signals.includes("RISK")) return false;
  if (F.q && !(`${c.symbol} ${c.name} ${c.mint}`.toLowerCase().includes(F.q))) return false;
  return true;
}

// ---------------------------------------------------------------- feed
function firstBuy(c) {
  if (!c.first_buy_t) return "chưa có lệnh mua";
  const d = c.first_buy_t - c.t_create;
  return d < 1 ? "mua đầu: cùng giây tạo coin (xem panel)" : `mua đầu sau ${fmt(d, 0)}s`;
}
function reason(c) {
  const e = c.smart_ev;
  if (!e || !e.wallets || !e.wallets.length) return "";
  const w = e.wallets[0], l = e.last || {};
  return `ví #${w.rank} ${short(w.wallet, 4)}${w.multi ? " (đa coin)" : ""}${l.sol ? ` mua ${usd(l.sol)} ở phút ${fmt(l.minute, 1)}` : ""}` +
    `${w.why && w.why[0] ? ` · ${esc(w.why[0])}` : ""}${e.wallets.length > 1 ? ` · +${e.wallets.length - 1} ví Smart khác` : ""}`;
}
function row(c) {
  const p = primary(c);
  const cls = ["rw", p === "HIGH_ALERT" ? "p-ha" : c.smart ? "p-sm" : "", S.first || S.seen.has(c.mint) ? "" : "new", S.open === c.mint ? "sel" : ""].join(" ");
  const mc = c.mc_sol === null || c.mc_sol === undefined ? `<span title="${esc(c.platform)}">–</span>` : usd(c.mc_sol);
  const vol = (c.buy_sol || 0) + (c.sell_sol || 0);
  return `<div class="${cls}" data-m="${c.mint}">
    <div class="cn"><div class="sym">${esc(c.symbol || "?")}</div>
      <div class="nm">${esc(c.name)} · <span class="age" data-t="${c.t_create}">${age(now() - c.t_create)}</span></div>
      <div class="fb">${firstBuy(c)}</div>${F.sig === "FOCUS" && c.smart_ev ? `<div class="why">${reason(c)}</div>` : ""}</div>
    <div><span class="pr ${p}">${TXT[p]}</span></div>
    <div class="num">${mc}</div><div class="num ${c.liq_sol ? "" : "z"}">${usd(c.liq_sol)}</div><div class="num ${vol ? "" : "z"}">${usd(vol)}</div>
    <div class="num ${c.buyers ? "" : "z"}">${c.buyers || 0}</div><div class="num ${c.smart ? "smn" : "z"}">${c.smart || 0}</div>
    <div>${c.signals.includes("RISK") ? `<span class="risk" title="Xem lý do trong panel">⚠ RỦI RO</span>` : ""}</div></div>`;
}
function render() {
  const rows = S.coins.filter(pass);
  $("#rows").innerHTML = (F.sig === "FOCUS" ? `<div class="focusnote" style="padding:12px 20px 0">Coin có ví Smart Money mua, không có cảnh báo rủi ro, dưới 15 phút tuổi. Đây là danh sách để theo dõi, không phải lệnh mua: tỷ lệ đúng đang được đo trong đợt quan sát.</div>` : "") +
    rows.slice(0, 250).map(row).join("");
  $("#empty").innerHTML = rows.length ? "" : (F.sig === "FOCUS" ? "Hiện chưa có coin nào đạt điều kiện: có Smart Money mua, không có cảnh báo rủi ro, dưới 15 phút tuổi. Tín hiệu này hiếm; danh sách tự cập nhật." : S.coins.length ? "Không có coin nào khớp bộ lọc." : (S.status && !S.status.running
    ? `Luồng coin mới đang tắt: ${esc(S.status.note || "")}. Không hiển thị dữ liệu giả.` : "Đang chờ coin mới đầu tiên…"));
}
function ages() { $$(".age[data-t]").forEach((el) => { el.textContent = age(now() - Number(el.dataset.t)); }); }
function header(st) {
  const live = $("#live"), s = st.stream, m = st.metrics;
  if (st.running && s && s.connected) {
    live.className = "live on";
    live.querySelector("b").textContent = `LIVE · trễ ${m.signal_latency_s.p50 ?? "–"}s`;
  } else {
    live.className = "live off";
    live.querySelector("b").textContent = st.running ? "MẤT KẾT NỐI" : "TẮT";
  }
  $("#tiles").innerHTML = m ? `<div class="tile"><div class="k">COIN MỚI</div><div class="v">${fmt(m.coins_24h, 0)}</div><div class="s">24 giờ qua</div></div>
    <div class="tile"><div class="k">CÓ LỆNH MUA</div><div class="v">${fmt(m.with_buys_24h, 0)}</div><div class="s">24 giờ qua</div></div>
    <div class="tile sm"><div class="k">SMART MONEY</div><div class="v">${fmt(m.smart_coins_24h, 0)}</div><div class="s">coin có ví Smart Money mua</div></div>` : "";
}
function alerts(coins) {
  for (const c of coins) {
    for (const k of ["HIGH_ALERT", "SMART_MONEY"]) {
      const key = c.mint + k;
      if (c.signals.includes(k) && !S.alerted.has(key)) {
        S.alerted.add(key);
        if (!S.first) {
          toast(`<span class="pr ${k}" style="min-width:0">${TXT[k]}</span> <b>${esc(c.symbol)}</b> · ${c.smart} Smart Money · ${age(now() - c.t_create)} <a href="#" data-open="${c.mint}">xem</a>`);
          $("#toast a[data-open]").onclick = (e) => { e.preventDefault(); openCoin(c.mint); };
          if (F.snd) beep();
        }
      }
    }
  }
  LS.set("nc.alerted", Array.from(S.alerted).slice(-500));
}
async function tick() {
  try {
    const [st, d] = await Promise.all([api("/api/nc/status"), api(`/api/nc/coins?age=${Math.max(F.age, 900)}`)]);
    S.status = st; S.skew = d.now - Date.now() / 1000; S.solUsd = d.sol_usd || (st.metrics && st.metrics.sol_usd) || null;
    header(st); alerts(d.coins);
    const before = new Set(S.coins.map((c) => c.mint));
    S.coins = d.coins;
    S.seen = S.first ? new Set(d.coins.map((c) => c.mint)) : before;
    render();
    S.first = false;
    if (S.open) refreshSide();
  } catch (e) { if (e.message !== "auth") $("#live").querySelector("b").textContent = "LỖI: " + e.message; }
}

// ---------------------------------------------------------------- right panel
const CLS = { INDEPENDENT: "ind", POSSIBLE_LINK: "pl", LIKELY_SAME_ENTITY: "ls", INSUFFICIENT_DATA: "id" };
function entry(w, c) {
  if (w.first_sig === c.create_sig) return `<span class="bd tx" title="Mua trong chính giao dịch tạo coin (cùng một transaction với creator)">TRONG TX TẠO</span>`;
  if (w.in_create_slot) return `<span class="bd slot" title="Mua trong cùng slot (block) với giao dịch tạo coin">CÙNG SLOT TẠO</span>`;
  return `+${fmt(w.sec, 0)}s`;
}
function evidence(s) {
  const e = s.evidence || {};
  if (s.kind === "NEW") return `creator ${short(e.creator, 5)}`;
  if (s.kind === "EARLY_BUY") return `${short(e.wallet, 5)} mua ${usd(e.sol)}, ${fmt(e.sec_after_create, 1)}s sau khi tạo`;
  if (s.kind === "SMART_MONEY") return (e.wallets || []).map((w) => `#${w.rank} ${short(w.wallet, 4)}${w.multi ? " (đa coin)" : ""}`).join(", ") + (e.last ? ` · mới nhất ${usd(e.last.sol)} phút ${fmt(e.last.minute, 1)}` : "");
  if (s.kind === "MULTI_WALLET") return `${e.buyers_15m} ví mua trong 15′, ${e.independent_groups} nhóm độc lập (${esc(e.note || "")})`;
  if (s.kind === "HIGH_ALERT") return esc(e.why || "");
  return "";
}
function buyerRow(w, c) {
  const tags = [];
  if (w.dev) tags.push(`<span class="bd dev">CREATOR</span>`);
  if (w.smart) tags.push(`<span class="bd sm">SMART #${w.smart.rank}${w.smart.multi ? " ĐA COIN" : ""}</span>`);
  if (w.cls) tags.push(`<span class="bd ${CLS[w.cls] || "id"}">${esc(w.cls)}</span>`);
  if (w.entity) tags.push(`<span class="bd">${esc(w.entity)}</span>`);
  for (const f of w.flags || []) tags.push(`<span class="bd ${f === "CREATOR_GROUP" ? "cg" : f.startsWith("COORD") ? "co" : ""}">${esc(f.replace("COORDINATED_PATTERN", "BOT"))}</span>`);
  return `<tr class="${w.smart ? "smart" : ""}"><td>${entry(w, c)}</td>
    <td><a href="https://solscan.io/account/${w.wallet}" target="_blank" rel="noopener">${short(w.wallet, 5)}</a>${w.smart ? `<div class="why">${esc((w.smart.why || []).join(" · "))}</div>` : ""}</td>
    <td class="r">${usd(w.sol_in)}${w.sol_out ? `<div class="why">bán ${usd(w.sol_out)}</div>` : ""}</td><td class="r">${fmt(w.held_pct, 2)}%</td><td>${tags.join("")}</td></tr>`;
}
async function openCoin(m) {
  S.open = m; history.replaceState(null, "", "#" + m); $("#side").hidden = false;
  $$(".rw.sel").forEach((t) => t.classList.remove("sel")); const r = $(`.rw[data-m="${m}"]`); if (r) r.classList.add("sel");
  await refreshSide();
}
function closeSide() { S.open = null; history.replaceState(null, "", location.pathname); $("#side").hidden = true; $$(".rw.sel").forEach((t) => t.classList.remove("sel")); }
async function refreshSide() {
  const m = S.open;
  let c;
  try { c = await api(`/api/nc/coin?mint=${m}`); } catch (e) { return; }
  if (m !== S.open) return;
  const sm = c.buyers_list.filter((w) => w.smart);
  const sigs = c.signals.filter((s) => s.kind !== "RISK");
  const risk = c.signals.find((s) => s.kind === "RISK");
  const p = primary({ signals: c.signals.map((s) => s.kind) });
  const inTx = c.buyers_list.filter((w) => !w.dev && w.first_sig === c.create_sig).length;
  const inSlot = c.buyers_list.filter((w) => !w.dev && w.in_create_slot && w.first_sig !== c.create_sig).length;
  $("#side").innerHTML = `<button class="ghost x" id="cx">Đóng ✕</button>
    <h2>${esc(c.symbol)}</h2><div class="sub">${esc(c.name)} · ${esc(c.platform)} · tuổi ${age(now() - c.t_create)} · phát hiện sau ${fmt(c.seen_at - c.t_create, 1)}s</div>
    <div style="margin:6px 0 10px"><span class="pr ${p}">${TXT[p]}</span></div>
    <div class="addr">${esc(c.mint)}</div>
    <div class="links"><a href="https://pump.fun/coin/${c.mint}" target="_blank" rel="noopener">pump.fun ↗</a><a href="https://dexscreener.com/solana/${c.mint}" target="_blank" rel="noopener">DexScreener ↗</a>
      <a href="https://solscan.io/token/${c.mint}" target="_blank" rel="noopener">Solscan ↗</a><a href="https://solscan.io/account/${c.creator}" target="_blank" rel="noopener">creator ${short(c.creator)} ↗</a></div>
    <div class="grid4">${[["MC", c.mc_sol === null ? "–" : usd(c.mc_sol)], ["Liq", usd(c.liq_sol)], ["Buyers", c.buyers], ["Smart", sm.length],
      ["Vào (mua)", usd(c.buy_sol)], ["Ra (bán)", usd(c.sell_sol)], ["Creator mua", usd(c.dev_sol)], ["Mua trong tx/slot tạo", `${inTx}/${inSlot}`]]
      .map(([k, v]) => `<div><div class="k">${k}</div><div class="v">${v}</div></div>`).join("")}</div>
    ${risk ? `<div class="riskbox"><b>⚠ CẢNH BÁO RỦI RO</b><br>${(risk.evidence.whys || []).map(esc).join("<br>")}</div>` : ""}
    ${sm.length ? `<h3>Smart Money đã mua (${sm.length})</h3><table class="bt"><tbody>${sm.map((w) => buyerRow(w, c)).join("")}</tbody></table>` : ""}
    <h3>Tín hiệu & bằng chứng</h3>
    <div class="ev">${sigs.map((s) => `<div><span><span class="pr ${s.kind}" style="min-width:0;padding:3px 8px">${TXT[s.kind]}</span></span><span class="m">phút ${fmt(s.minute, 1)}</span><span class="e">${evidence(s)}</span></div>`).join("") || "<div class='e'>Chưa có</div>"}</div>
    <h3>Ai đang mua (${c.buyers_list.length} ví, theo thứ tự)</h3>
    <table class="bt"><thead><tr><th>Vào lúc</th><th>Ví</th><th class="r">Mua</th><th class="r">Giữ</th><th>Bằng chứng</th></tr></thead>
    <tbody>${c.buyers_list.slice(0, 150).map((w) => buyerRow(w, c)).join("")}</tbody></table>
    <div class="note">"TRONG TX TẠO" = mua trong chính giao dịch tạo coin; "CÙNG SLOT TẠO" = cùng block với giao dịch tạo; "+Xs" = mua sau khi coin đã tạo. Dữ liệu trực tiếp từ log on-chain của pump.fun; không có điểm tổng hợp.</div>`;
  $("#cx").onclick = closeSide;
}

async function showReport() {
  $("#mlist").hidden = true; S.open = null; $("#side").hidden = false;
  $("#side").innerHTML = `<button class="ghost x" id="cx">Đóng ✕</button><h2>Báo cáo quan sát</h2><div class="sub">Đang tính…</div>`;
  $("#cx").onclick = closeSide;
  const r = await api("/api/nc/report");
  const pct = (v) => v === undefined ? "–" : `${fmt(v * 100, 1)}%`;
  const rows = Object.entries(r.groups).map(([g, s]) => `<tr><td>${esc(g)}</td><td class="r">${s.coins}</td>` +
    ["1h", "6h", "24h"].map((h) => s[h].n ? `<td class="r">${pct(s[h].hit_2x)}<div class="why">sập ${pct(s[h].crash)} · n ${s[h].n}</div></td>` : `<td class="r">–</td>`).join("") + "</tr>").join("");
  $("#side").innerHTML = `<button class="ghost x" id="cx">Đóng ✕</button><h2>Báo cáo quan sát</h2>
    <div class="sub">${r.coins_with_15m_snapshot} coin có lệnh mua và bản chụp phút 15. Mốc vào lệnh = giá ở phút 15. "Đạt 2x" = giá cao nhất đến mốc ≥ 2 lần giá phút 15; "sập" = giá tại mốc ≤ 30% giá phút 15.</div>
    <table class="bt"><thead><tr><th>Nhóm (xác định ở phút 15)</th><th class="r">Coin</th><th class="r">1 giờ</th><th class="r">6 giờ</th><th class="r">24 giờ</th></tr></thead><tbody>${rows}</tbody></table>
    <h3>Bỏ lỡ</h3><div class="sub">${r.five_x_coins} coin đạt ≥ 5x giá ra mắt trong 24 giờ; ${r.five_x_without_smart_by_15m} coin (${pct(r.missed_share)}) không có Smart Money mua trước phút 15.</div>
    <div class="note">Số liệu tích lũy từ lúc bật đợt quan sát. Cần 3–7 ngày trước khi kết luận; nhóm có ít mẫu (n nhỏ) chưa có ý nghĩa thống kê.</div>`;
  $("#cx").onclick = closeSide;
}
controls(); bind();
$("#rep").onclick = (e) => { e.preventDefault(); showReport(); };
tick().then(() => { const h = location.hash.slice(1); if (/^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(h)) openCoin(h); });
setInterval(tick, 2500);
setInterval(ages, 1000);
