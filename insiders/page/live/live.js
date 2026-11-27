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
const TWO_AGE = 7200;                                      // >= 2 watched wallets in one coin: kept in "Đáng chú ý" for its whole window
const two = (c) => (c.smart || 0) >= 2;
const AGES = [["5m", 300], ["15m", 900], ["1h", 3600], ["2h", 7200]];
const F = Object.assign({ age: 3600, sig: "", minsol: "0", minsm: "0", hasbuy: false, norisk: false, q: "", snd: false }, LS.get("nc.f2", {}));
const DET_TTL = 60;                                         // seconds a coin's project / dev facts are reused in the list
const S = { det: {}, code: LS.get("insAccessCode", ""), coins: [], seen: new Set(), alerted: new Set(LS.get("nc.alerted", [])), open: null, skew: 0, first: true };
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
  if (F.sig === "FOCUS") return (two(c) && now() - c.t_create < TWO_AGE) ||
    (c.signals.includes("SMART_MONEY") && !c.signals.includes("RISK") && now() - c.t_create < FOCUS_AGE);
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
function watchLine(c) {
  const w = c.watch;
  const g = w.groups !== undefined ? ` · ${w.groups} nhóm chủ · ${w.verified_independent} độc lập đã xác minh${w.unverified ? ` · ${w.unverified} chưa xác minh` : ""}` : "";
  return `<b>${w.wallets} ví theo dõi</b>${g} · ví đầu vào sau ${fmt(w.first_t - c.t_create, 0)}s${w.first_mc_sol ? ` lúc MC ${usd(w.first_mc_sol)}` : ""} · ${usd(w.sol)} vào`;
}
function reason(c) {
  const e = c.smart_ev;
  if (!e || !e.wallets || !e.wallets.length) return "";
  const w = e.wallets[0], l = e.last || {};
  return `ví #${w.rank} ${short(w.wallet, 4)}${w.multi ? " (đa coin)" : ""}${l.sol ? ` mua ${usd(l.sol)} ở phút ${fmt(l.minute, 1)}` : ""}` +
    `${w.why && w.why[0] ? ` · ${esc(w.why[0])}` : ""}${e.wallets.length > 1 ? ` · +${e.wallets.length - 1} ví Smart khác` : ""}`;
}
// X / Telegram / web links and the dev in one line (list) — from the coin detail (pump.fun public API + our data)
function xHandle(u) {
  const m = /(?:x|twitter)\.com\/([A-Za-z0-9_]{1,15})(?:[/?#]|$)/i.exec(u || "");
  return m && !["i", "home", "search", "intent"].includes(m[1].toLowerCase()) ? "@" + m[1] : null;
}
function socials(d, compact) {
  const p = d.project || {}, u = d.dev_profile || {}, h = d.dev_history || {};
  const out = [];
  if (p.twitter) {
    const kind = /\/status\//.test(p.twitter) ? " (bài đăng)" : /\/communities\//.test(p.twitter) ? " (cộng đồng)" : "";
    out.push(`<a href="${esc(p.twitter)}" target="_blank" rel="noopener">𝕏 ${esc(/\/communities\//.test(p.twitter) ? "cộng đồng X" : (xHandle(p.twitter) || "link"))}${kind && !/communities/.test(p.twitter) ? kind : ""}</a>`);
  }
  else out.push(`<span class="no">𝕏 không có</span>`);
  if (p.telegram) out.push(`<a href="${esc(p.telegram)}" target="_blank" rel="noopener">Telegram</a>`);
  if (p.website) out.push(`<a href="${esc(p.website)}" target="_blank" rel="noopener">Web</a>`);
  if (p.launcher) out.push(`<span class="warn" title="Công cụ dùng để tạo coin (theo metadata)">tạo bằng ${esc(p.launcher)}</span>`);
  if (!d.project && !compact) out.push(`<span class="no">không đọc được metadata</span>`);
  const dev = u.username ? `${esc(u.username)}${u.x ? ` (𝕏 @${esc(u.x)})` : ""}` : short(d.creator, 4);
  const prev = h.coins_seen ? `${h.coins_seen} coin khác từ ${age(now() - h.since)} trước` : "coin đầu tiên thấy";
  const cls = h.coins_seen >= 5 ? "bad" : h.coins_seen ? "warn" : "ok";
  out.push(`<span>Dev ${dev} · <span class="${cls}">${prev}</span>${h.dev_sold ? ` · <span class="bad">đã bán ở ${h.dev_sold}</span>` : ""}${d.dev_sold ? ` · <span class="bad">ĐÃ BÁN coin này</span>` : ""}</span>`);
  if (!compact && d.dev_flags && d.dev_flags.length) out.push(`<span class="bad">${d.dev_flags.map(esc).join(", ")}</span>`);
  return `<div class="soc">${out.join("")}</div>`;
}
async function detail(m) {
  const h = S.det[m];
  if (h && now() - h.t < DET_TTL) return h.d;
  if (h && h.busy) return h.d;
  S.det[m] = { t: h ? h.t : 0, d: h ? h.d : null, busy: true };
  try { const d = await api(`/api/nc/coin?mint=${m}`); S.det[m] = { t: now(), d }; return d; }
  catch (e) { S.det[m] = { t: now(), d: h ? h.d : null }; return null; }
}
function row(c) {
  const p = primary(c);
  const cls = ["rw", two(c) ? "p-two" : p === "HIGH_ALERT" ? "p-ha" : c.smart ? "p-sm" : "", S.first || S.seen.has(c.mint) ? "" : "new",
    S.open === c.mint ? "sel" : ""].join(" ");
  const mc = c.mc_sol === null || c.mc_sol === undefined ? `<span title="${esc(c.platform)}">–</span>` : usd(c.mc_sol);
  const vol = (c.buy_sol || 0) + (c.sell_sol || 0);
  return `<div class="${cls}" data-m="${c.mint}">
    <div class="cn"><div class="sym">${esc(c.symbol || "?")}${two(c) ? ` <span class="twob">${c.smart} VÍ THEO DÕI</span>` : ""}</div>
      <div class="nm">${esc(c.name)} · <span class="age" data-t="${c.t_create}">${age(now() - c.t_create)}</span></div>
      <div class="fb">${firstBuy(c)}</div>${c.watch ? `<div class="wline">${watchLine(c)}</div>` : ""}${F.sig === "FOCUS" && c.smart_ev ? `<div class="why">${reason(c)}</div>` : ""}
      ${F.sig === "FOCUS" ? (S.det[c.mint] && S.det[c.mint].d ? socials(S.det[c.mint].d, true) : `<div class="soc"><span class="no">đang tải X / dev…</span></div>`) : ""}</div>
    <div class="sgc"><span class="pr ${p}">${TXT[p]}</span></div>
    <div class="num" data-l="MC">${mc}</div><div class="num ${c.liq_sol ? "" : "z"}" data-l="Liq">${usd(c.liq_sol)}</div><div class="num ${vol ? "" : "z"}" data-l="Vol">${usd(vol)}</div>
    <div class="num ${c.buyers ? "" : "z"}" data-l="Buyers">${c.buyers || 0}</div><div class="num ${c.smart ? "smn" : "z"}" data-l="Smart">${c.smart || 0}</div>
    <div class="rk">${c.signals.includes("RISK") ? `<span class="risk" title="Xem lý do trong panel">⚠ RỦI RO</span>` : ""}</div></div>`;
}
function render() {
  const rows = S.coins.filter(pass);
  if (F.sig === "FOCUS") rows.sort((a, b) => two(b) - two(a));      // >= 2 watched wallets first
  $("#rows").innerHTML = (F.sig === "FOCUS" ? `<div class="focusnote" style="padding:12px 20px 0"><span class="twob">XANH</span> = từ 2 ví theo dõi trở lên cùng mua (giữ trong mục này 120 phút, kể cả khi có cảnh báo rủi ro). Còn lại: có 1 ví Smart Money mua, không có cảnh báo rủi ro, dưới 15 phút tuổi. Đây là danh sách để theo dõi, không phải lệnh mua.</div>` : "") +
    rows.slice(0, 250).map(row).join("");
  if (F.sig === "FOCUS") {                                  // a handful of coins: fetch their project / dev facts, then redraw
    const todo = rows.slice(0, 20).filter((c) => !S.det[c.mint] || (now() - S.det[c.mint].t >= DET_TTL && !S.det[c.mint].busy));
    if (todo.length) Promise.all(todo.map((c) => detail(c.mint))).then(() => { if (F.sig === "FOCUS") render(); });
  }
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
function timeline(c) {
  const t = c.timeline || {};
  const tm = (x) => x ? new Date(x * 1000).toLocaleTimeString("vi-VN", { hour12: false }) : "–";
  return `<div class="tl">
    <div><span class="k">TẠO</span><b>${tm(t.t_create)}</b><a href="https://solscan.io/tx/${esc(c.create_sig || "")}" target="_blank" rel="noopener">tx ↗</a></div>
    <div><span class="k">MUA ĐẦU</span><b>${t.first_buy_s === null || t.first_buy_s === undefined ? "–" : "+" + fmt(t.first_buy_s, 1) + "s"}</b></div>
    <div><span class="k">HỆ THỐNG THẤY</span><b>${t.detect_s === null || t.detect_s === undefined ? "–" : "+" + fmt(t.detect_s, 1) + "s"}</b></div>
    <div><span class="k">VÍ THEO DÕI ĐẦU</span><b>${t.watched_first_s === null || t.watched_first_s === undefined ? "–" : "+" + fmt(t.watched_first_s, 0) + "s"}</b>${t.watched_first_mc_sol ? `<span class="k">MC ${usd(t.watched_first_mc_sol)}</span>` : ""}</div>
  </div>
  <div class="sub">${t.partial ? '<span class="old">Coin cũ hơn thời gian lưu giao dịch: chỉ còn lệnh của ví theo dõi, số ví mua không đầy đủ.</span><br>' : ""}${fmt(t.early15, 0)} ví mua trong 15 phút đầu · ${fmt(t.watched, 0)} ví theo dõi${t.groups !== undefined ? ` · <b>${t.groups} nhóm chủ sở hữu</b> (${t.verified_independent} độc lập đã xác minh, ${t.linked} có liên kết, ${t.unverified} chưa đủ dữ liệu)` : ""}</div>`;
}
function buyerRow(w, c) {
  const tags = [];
  if (w.dev) tags.push(`<span class="bd dev">CREATOR</span>`);
  if (w.smart) tags.push(`<span class="bd sm">${w.smart.cohort ? esc(w.smart.cohort) : "#" + esc(w.smart.rank)}${w.smart.multi ? " ĐA COIN" : ""}</span>`);
  if (w.independence) tags.push(`<span class="bd ${IND[w.independence] || ""}">${esc(INDT[w.independence] || w.independence)}</span>`);
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
    ${timeline(c)}
    <div class="grid4">${[["MC", c.mc_sol === null ? "–" : usd(c.mc_sol)], ["Liq", usd(c.liq_sol)], ["Buyers", c.buyers], ["Smart", sm.length],
      ["Vào (mua)", usd(c.buy_sol)], ["Ra (bán)", usd(c.sell_sol)], ["Creator mua", usd(c.dev_sol)], ["Mua trong tx/slot tạo", `${inTx}/${inSlot}`]]
      .map(([k, v]) => `<div><div class="k">${k}</div><div class="v">${v}</div></div>`).join("")}</div>
    <h3>Dự án & Dev</h3>
    <div class="proj">
      ${socials(c, false)}
      ${c.project && c.project.description ? `<div class="desc">${esc(c.project.description)}</div>` : `<div class="no" style="color:var(--dim)">Dev không viết mô tả trong metadata.</div>`}
      ${c.project && c.project.ath_mcap_usd ? `<div><span class="k">ATH (pump.fun)</span> $${fmt(c.project.ath_mcap_usd, 0)}${c.project.complete ? " · đã tốt nghiệp" : ""}</div>` : ""}
      ${c.dev_profile ? `<div><span class="k">Hồ sơ pump.fun của dev</span> ${esc(c.dev_profile.username)} · ${fmt(c.dev_profile.followers, 0)} follower${c.dev_profile.bio ? ` · ${esc(c.dev_profile.bio)}` : ""}</div>` : ""}
      <div><span class="k">Creator</span> <a href="https://solscan.io/account/${c.creator}" target="_blank" rel="noopener">${short(c.creator, 6)} ↗</a> · mua ${usd(c.dev_sol)}${c.dev_sold ? ' · <span style="color:var(--red)">đã bán</span>' : " · chưa bán"}${c.dev_class ? ` · ${esc(c.dev_class)}` : ""}</div>
      ${(c.dev_history && c.dev_history.recent.length) ? `<div><span class="k">Coin khác của dev (hệ thống đã thấy)</span>
        <table class="bt"><tbody>${c.dev_history.recent.slice(0, 8).map((o) => `<tr><td>${esc(o.symbol)}</td><td class="r">${age(now() - o.t_create)} trước</td>
          <td class="r">${o.buyers} buyers</td><td class="r">${o.peak_mc_sol ? "đỉnh " + usd(o.peak_mc_sol) : (o.mc_sol ? usd(o.mc_sol) : "–")}</td>
          <td>${o.dev_sold ? '<span class="bd tx">DEV BÁN</span>' : ""}${o.signals.includes("SMART_MONEY") ? '<span class="bd sm">SMART</span>' : ""}</td></tr>`).join("")}</tbody></table></div>`
        : `<div style="color:var(--dim)">Chưa thấy coin nào khác của dev này kể từ khi luồng bắt đầu chạy.</div>`}
    </div>
    ${risk ? `<div class="riskbox" style="margin-top:12px"><b>⚠ CẢNH BÁO RỦI RO</b><br>${(risk.evidence.whys || []).map(esc).join("<br>")}</div>` : ""}
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
// ---------------------------------------------------------------- VÍ THEO DÕI (second screen)
const IND = { INDEPENDENT: "ind", POSSIBLE_LINK: "pl", LIKELY_SAME_ENTITY: "cg", INSUFFICIENT_DATA: "na" };
const INDT = { INDEPENDENT: "ĐỘC LẬP", POSSIBLE_LINK: "CÓ THỂ LIÊN KẾT", LIKELY_SAME_ENTITY: "CÙNG CHỦ", INSUFFICIENT_DATA: "CHƯA ĐỦ DỮ LIỆU" };
const RQ = { REQUESTED: "Chờ truy vết", RUNNING: "Đang chạy", DONE: "Xong", REJECTED: "Không chạy được" };
const GRP = [["", "Tất cả"], ["DEPTH", "DEPTH"], ["STANK", "STANK"], ["INSIDERS", "INSIDERS"], ["coin", "Ví coin nghiên cứu"], ["auto", "Tự truy vết"], ["manual", "Thêm tay"]];
const WV = Object.assign({ g: "", days: 7, q: "", sort: "sol" }, LS.get("nc.wv", {}));
const tmAgo = (t) => t ? age(now() - t) + " trước" : "–";
function balCell(b) {
  if (!b || b.status === "off") return `<span class="na">tắt</span>`;
  if (b.status === "never") return `<span class="na" title="chưa đọc được số dư">chưa đọc</span>`;
  const v = b.status === "empty" ? `<b>0</b> <span class="na">đã rút hết</span>` : `<b>${fmt(b.sol, 2)}</b>`;
  return `${v}${b.usd ? `<div class="why">$${fmt(b.usd, 0)}</div>` : ""}<div class="why ${b.status === "stale" ? "old" : ""}">${b.status === "stale" ? "cũ · " : ""}${tmAgo(b.t)}</div>`;
}
function srcTags(x) {
  return x.sources.map((t) => `<span class="bd ${["DEPTH", "STANK", "INSIDERS"].includes(t) ? "co" : t === "manual" ? "dev" : "sm"}">${esc(t.replace("coin:", "").replace("auto:", "AUTO "))}</span>`).join("");
}
function setView(v) {
  S.view = v;
  $("#cview").hidden = v !== "coins"; $("#wview").hidden = v !== "wallets";
  $$("#tabs button").forEach((b) => b.classList.toggle("on", b.dataset.view === v));
  if (v === "wallets") { history.replaceState(null, "", "#wallets"); loadWallets(); } else if (location.hash === "#wallets") history.replaceState(null, "", location.pathname);
}
async function loadWallets() {
  let w, rq;
  try { [w, rq] = await Promise.all([api(`/api/nc/wallets?days=${WV.days}`), api("/api/nc/requests")]); } catch (e) { return; }
  S.wal = w; S.req = rq.requests || [];
  drawWallets();
}
function drawWallets() {
  const w = S.wal; if (!w) return;
  const bs = w.by_source || {};
  const inG = (x) => !WV.g || x.sources.some((t) => t === WV.g || t.startsWith(WV.g + ":"));
  const q = WV.q.toLowerCase();
  const list = w.wallets.filter((x) => inG(x) && (!q || `${x.wallet} ${x.sources.join(" ")} ${x.label || ""}`.toLowerCase().includes(q)));
  const key = { sol: (x) => (x.balance && x.balance.sol) || -1, act: (x) => x.bought + x.sold, last: (x) => x.last_trade || 0, links: (x) => x.links }[WV.sort];
  list.sort((a, b) => key(b) - key(a));
  const totSol = list.reduce((t, x) => t + ((x.balance && x.balance.sol) || 0), 0);
  const read = list.filter((x) => x.balance && ["ok", "empty", "stale"].includes(x.balance.status)).length;
  const reqRows = (S.req || []).slice(0, 8).map((r) => `<tr><td><b>${esc(r.symbol)}</b></td><td>${esc(RQ[r.status] || r.status)}<div class="why">${esc(r.note || "")}${r.progress && r.status === "RUNNING" ? ` · ${fmt(r.progress.calls, 0)} lệnh RPC` : ""}</div></td><td class="r">${r.updated ? tmAgo(r.updated) : "–"}</td></tr>`).join("");
  $("#wview").innerHTML = `
    <div class="tiles">${[["DEPTH", bs.DEPTH], ["STANK", bs.STANK], ["INSIDERS", bs.INSIDERS], ["VÍ COIN", (bs.coin || 0) + (bs.auto || 0)], ["THÊM TAY", bs.manual || 0]]
      .map(([k, v]) => `<div class="tile"><div class="k">${k}</div><div class="v">${fmt(v || 0, 0)}</div><div class="s">ví</div></div>`).join("")}
      <div class="tile sm"><div class="k">SOL ĐANG GIỮ</div><div class="v">${fmt(totSol, 0)}</div><div class="s">${read}/${list.length} ví đã đọc số dư${w.balances && w.balances.updated ? " · " + tmAgo(w.balances.updated) : ""}</div></div></div>
    <div class="bar"><div class="chips">${GRP.map(([v, t]) => `<button data-g="${v}" class="${WV.g === v ? "on" : ""}">${t}</button>`).join("")}</div><div class="grow"></div>
      <select id="wdays">${[1, 7, 30].map((d) => `<option value="${d}" ${WV.days == d ? "selected" : ""}>${d} ngày</option>`).join("")}</select>
      <select id="wsort">${[["sol", "SOL nhiều nhất"], ["act", "Giao dịch nhiều"], ["last", "Mới giao dịch"], ["links", "Nhiều liên kết"]].map(([v, t]) => `<option value="${v}" ${WV.sort === v ? "selected" : ""}>${t}</option>`).join("")}</select>
      <input id="wq" type="search" placeholder="Tìm ví, nhóm…" value="${esc(WV.q)}" class="wq2"></div>
    <details class="adds"><summary>＋ Thêm ví / thêm coin để truy vết</summary>
      <form id="wadd" class="rqf"><input id="wa" placeholder="Địa chỉ ví" autocomplete="off"><input id="wl" placeholder="Ghi chú (tuỳ chọn)" maxlength="60" autocomplete="off"><button>Thêm ví</button></form>
      <form id="rqf" class="rqf" style="margin-top:8px"><input id="rqm" placeholder="Mint coin" autocomplete="off"><input id="rqs" placeholder="Tên coin" maxlength="16" autocomplete="off"><button>Thêm coin</button></form>
      <div id="rqmsg" class="why">Coin thêm vào: server tự truy vết (Helius), ví lớn của coin đạt luật sẽ tự vào danh sách.</div>
      ${reqRows ? `<table class="bt"><tbody>${reqRows}</tbody></table>` : ""}
      <h3 style="margin-top:14px">Đồng bộ giữa web và PC</h3>
      <div class="why">Trên trang nguồn (VD web Render) bấm <b>Tải dữ liệu ví về</b>; trên trang đích (PC) bấm <b>Nhập dữ liệu ví</b> và chọn file đó. Dữ liệu chỉ được <b>thêm</b>, không ghi đè.</div>
      <div style="display:flex;gap:8px;margin-top:8px;flex-wrap:wrap"><button class="ghost" id="wexp">Tải dữ liệu ví về</button>
        <label class="ghost" style="cursor:pointer">Nhập dữ liệu ví<input type="file" id="wimp" accept=".json,application/json" hidden></label></div></details>
    <div class="list"><table class="wt"><thead><tr><th>Ví</th><th>Nhóm</th><th class="r">SOL hiện có</th><th class="r">Mua / bán (${WV.days} ngày)</th><th class="r">Coin</th><th class="r">Gần nhất</th><th>Độc lập</th><th class="r">Liên kết</th></tr></thead>
      <tbody>${list.slice(0, 500).map((x) => `<tr data-w="${x.wallet}">
        <td><a class="mono" href="https://solscan.io/account/${x.wallet}" target="_blank" rel="noopener">${short(x.wallet, 5)}</a>${x.program ? ` <span class="bd na">VAULT</span>` : ""}<div class="why">${esc(x.label || x.why || "")}</div></td>
        <td>${srcTags(x)}</td><td class="r">${balCell(x.balance)}</td>
        <td class="r">${x.bought || x.sold ? `${fmt(x.bought, 2)} / ${fmt(x.sold, 2)}` : `<span class="na">–</span>`}</td>
        <td class="r">${x.coins_period || "–"}${x.early_coins.length ? `<div class="why">${x.early_coins.length} coin NC</div>` : ""}</td>
        <td class="r">${x.last_trade ? tmAgo(x.last_trade) : "–"}</td>
        <td><span class="bd ${IND[x.independence] || ""}">${esc(INDT[x.independence] || x.independence || "–")}</span></td>
        <td class="r">${x.links || "–"}</td></tr>`).join("")}</tbody></table>
      ${list.length > 500 ? `<div class="why" style="padding:10px">… ${list.length - 500} ví nữa: dùng ô tìm</div>` : ""}
      <div class="note" style="padding:10px 14px">SOL hiện có = số dư thật trên chain (đọc ${w.balances ? "mỗi 10 phút" : "—"}), khác với SOL đã mua. "0 · đã rút hết" = tài khoản không còn trên chain. "chưa đọc" = chưa lấy được, không phải 0. Mua / bán: lệnh New Coins của ví theo dõi (mua ≥ 1 SOL, mọi lệnh bán). VAULT = tài khoản chương trình, không phải ví người.</div></div>`;
  $$("#wview .chips button").forEach((b) => b.onclick = () => { WV.g = b.dataset.g; LS.set("nc.wv", WV); drawWallets(); });
  $("#wdays").onchange = (e) => { WV.days = Number(e.target.value); LS.set("nc.wv", WV); loadWallets(); };
  $("#wsort").onchange = (e) => { WV.sort = e.target.value; LS.set("nc.wv", WV); drawWallets(); };
  let tm; $("#wq").oninput = (e) => { clearTimeout(tm); tm = setTimeout(() => { WV.q = e.target.value.trim(); drawWallets(); $("#wq").focus(); }, 250); };
  $("#wview tbody").onclick = (e) => { if (e.target.closest("a")) return; const r = e.target.closest("tr[data-w]"); if (r) openWallet(r.dataset.w); };
  const post = async (url, body, msg) => {
    const r = await fetch(url, { method: "POST", headers: { "X-Access-Code": S.code, "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const d = await r.json().catch(() => ({}));
    if (r.ok) { toast(msg(d)); loadWallets(); } else $("#rqmsg").innerHTML = `<span style="color:var(--red)">${esc(d.message || d.error || "lỗi " + r.status)}</span>`;
  };
  $("#wexp").onclick = async () => {
    const d = await api("/api/nc/export");
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([JSON.stringify(d)], { type: "application/json" }));
    a.download = `kol_radar_vi_${new Date().toISOString().slice(0, 10)}.json`; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 3000);
    toast(`Đã tải: ${d.tables.nc_trade.length} giao dịch ví theo dõi, ${d.tables.esm_coin.length} coin tự truy vết, ${d.tables.esm_flag.filter((f) => f.flag === "WATCH").length} ví thêm tay`);
  };
  $("#wimp").onchange = async (e) => {
    const f = e.target.files[0]; if (!f) return;
    const r = await fetch("/api/nc/import", { method: "POST", headers: { "X-Access-Code": S.code, "Content-Type": "application/json" }, body: await f.text() });
    const d = await r.json().catch(() => ({}));
    if (r.ok) { toast("Đã nhập: " + Object.entries(d.added).filter(([, n]) => n).map(([t, n]) => `${n} ${t}`).join(", ") || "không có gì mới"); setTimeout(loadWallets, 1500); }
    else $("#rqmsg").innerHTML = `<span style="color:var(--red)">${esc(d.message || d.error || "lỗi " + r.status)}</span>`;
  };
  $("#wadd").onsubmit = (e) => { e.preventDefault(); post("/api/nc/watch", { wallet: $("#wa").value.trim(), label: $("#wl").value.trim() }, (d) => `Đã thêm ví <b>${short(d.wallet, 5)}</b> vào danh sách theo dõi`); };
  $("#rqf").onsubmit = (e) => { e.preventDefault(); post("/api/nc/request", { mint: $("#rqm").value.trim(), symbol: $("#rqs").value.trim() }, (d) => `Đã thêm <b>${esc(d.symbol)}</b> vào hàng chờ truy vết`); };
}
async function openWallet(w) {
  S.open = null; $("#side").hidden = false;
  $("#side").innerHTML = `<button class="ghost x" id="cx">Đóng ✕</button><h2>${short(w, 6)}</h2><div class="sub">Đang tải số dư và lịch sử…</div>`;
  $("#cx").onclick = closeSide;
  let d;
  try { d = await api(`/api/nc/wallet?w=${w}&tokens=1`); } catch (e) { return; }
  const e = d.entry || {}, tk = d.tokens, b = d.balance || {};
  const indWhy = (d.flags.find((f) => f.flag === "INDEPENDENCE") || {}).evidence;
  const net = d.flags.filter((f) => ["PASS_THROUGH", "BATCH_SPLIT", "COORDINATED_PATTERN"].includes(f.flag));
  $("#side").innerHTML = `<button class="ghost x" id="cx">Đóng ✕</button>
    <h2>${short(w, 6)}</h2><div class="addr">${w}</div>
    <div class="links"><a href="https://solscan.io/account/${w}" target="_blank" rel="noopener">Solscan ↗</a><a href="https://gmgn.ai/sol/address/${w}" target="_blank" rel="noopener">GMGN ↗</a></div>
    <div style="margin:8px 0">${(e.sources || []).length ? srcTags({ sources: e.sources }) : ""} <span class="bd ${IND[d.independence] || ""}">${esc(INDT[d.independence] || d.independence)}</span>${d.program ? ` <span class="bd na">VAULT (tài khoản chương trình)</span>` : ""}</div>
    ${e.cohort_label ? `<div class="sub">${esc(e.cohort_label)}</div>` : ""}
    <div class="grid4">${[["SOL hiện có", b.status === "never" ? "chưa đọc" : b.status === "empty" ? "0 (đã rút)" : fmt(b.sol, 3)], ["Giá trị SOL", b.usd ? "$" + fmt(b.usd, 0) : "–"],
      ["Token (giá trị)", tk && tk.status === "ok" ? "$" + fmt(tk.tokens_usd, 0) : tk ? esc(tk.note || tk.status) : "–"], ["Tổng tài sản", tk && tk.total_usd ? "$" + fmt(tk.total_usd, 0) : "–"]]
      .map(([k, v]) => `<div><div class="k">${k}</div><div class="v">${v}</div></div>`).join("")}</div>
    <div class="why">Số dư đọc ${tmAgo(b.t)}${tk && tk.t ? ` · token đọc ${tmAgo(tk.t)}` : ""}${tk && tk.unpriced ? ` · ${tk.unpriced} token không có giá (không tính)` : ""}</div>
    ${tk && tk.tokens && tk.tokens.length ? `<h3>Token đang giữ</h3><table class="bt"><tbody>${tk.tokens.slice(0, 20).map((t) => `<tr><td><a href="https://dexscreener.com/solana/${t.mint}" target="_blank" rel="noopener">${esc(t.symbol || short(t.mint, 4))}</a></td><td class="r">${fmt(t.amount, 0)}</td><td class="r">$${fmt(t.usd, 0)}</td></tr>`).join("")}</tbody></table>` : ""}
    <h3>Mức độc lập (truy vết V2)</h3>
    <div class="sub">${esc((indWhy && (indWhy.why || indWhy.note)) || (d.independence === "INSUFFICIENT_DATA" ? "Chưa truy vết nguồn tiền của ví này: chưa đủ dữ liệu để nói độc lập hay cùng chủ (không bao giờ được coi là độc lập)." : ""))}${indWhy && indWhy.funders_exchange && indWhy.funders_exchange.length ? ` · nguồn từ sàn: ${indWhy.funders_exchange.map(esc).join(", ")}` : ""}</div>
    ${net.length ? `<div class="sub">${net.map((f) => `<span class="bd co">${esc(f.flag)}</span> ${esc(f.ref || "")}`).join("<br>")}</div>` : ""}
    ${d.links.length ? `<h3>Ví liên quan (${d.links.length})</h3><table class="bt"><tbody>${d.links.slice(0, 30).map((l) => `<tr><td><a href="#" data-w="${l.other}">${short(l.other, 5)}</a>${l.watched ? ` <span class="bd sm">THEO DÕI</span>` : ""}</td>
      <td><span class="bd ${l.strength === "STRONG" ? "cg" : "pl"}">${esc(l.signal)}</span></td><td>${l.sig ? `<a href="https://solscan.io/tx/${esc(l.sig)}" target="_blank" rel="noopener">bằng chứng ↗</a>` : esc(l.coin || "")}</td></tr>`).join("")}</tbody></table>` : ""}
    ${d.funding.length ? `<h3>Dòng tiền đã truy (${d.funding.length})</h3><table class="bt"><tbody>${d.funding.slice(0, 25).map((f) => `<tr><td>${f.dir === "in" ? "← nhận từ" : "→ gửi tới"} <a href="#" data-w="${f.other}">${short(f.other, 4)}</a>${f.label ? ` <span class="bd">${esc(f.label)}</span>` : ""}${f.program ? ` <span class="bd na">vault</span>` : ""}${f.watched ? ` <span class="bd sm">THEO DÕI</span>` : ""}</td>
      <td class="r">${fmt(f.amount, 3)} ${f.asset === "SOL" ? "SOL" : "token"}</td><td><a href="https://solscan.io/tx/${esc(f.sig || "")}" target="_blank" rel="noopener">tx ↗</a></td></tr>`).join("")}</tbody></table>` : ""}
    ${d.research.length ? `<h3>Coin nghiên cứu đã vào sớm</h3><table class="bt"><tbody>${d.research.map((r) => `<tr><td>${esc(r.symbol)}</td><td>phút ${fmt(r.first_buy_min, 1)}</td><td class="r">${r.sol_in === null ? "–" : fmt(r.sol_in, 2) + " SOL"}</td><td class="r">${r.rise_x ? fmt(r.rise_x, 1) + "x" : "–"}</td><td><a href="https://solscan.io/tx/${esc(r.first_buy_sig || "")}" target="_blank" rel="noopener">tx ↗</a></td></tr>`).join("")}</tbody></table>` : ""}
    <h3>Lịch sử mua bán coin mới (${d.trades.length})</h3>
    ${d.trades.length ? `<table class="bt"><tbody>${d.trades.slice(0, 120).map((t) => `<tr><td>${t.symbol ? `<a href="#" data-m="${t.mint}">${esc(t.symbol)}</a>` : short(t.mint, 4)}</td><td>${t.buy ? '<span class="bd sm">MUA</span>' : '<span class="bd tx">BÁN</span>'}</td>
      <td class="r">${fmt(t.sol, 3)} SOL</td><td class="r">${tmAgo(t.t)}${t.t_create ? `<div class="why">+${fmt(t.t - t.t_create, 0)}s sau tạo</div>` : ""}</td><td><a href="https://solscan.io/tx/${esc(t.sig)}" target="_blank" rel="noopener">tx ↗</a></td></tr>`).join("")}</tbody></table>`
      : `<div class="why">Chưa ghi nhận giao dịch coin mới nào của ví này (hệ thống chỉ ghi lệnh mua ≥ 1 SOL của ví theo dõi và mọi lệnh bán, trong 120 phút đầu của coin).</div>`}`;
  $("#cx").onclick = closeSide;
  $$("#side a[data-w]").forEach((a) => a.onclick = (ev) => { ev.preventDefault(); openWallet(a.dataset.w); });
  $$("#side a[data-m]").forEach((a) => a.onclick = (ev) => { ev.preventDefault(); setView("coins"); openCoin(a.dataset.m); });
}
controls(); bind();
$("#rep").onclick = (e) => { e.preventDefault(); showReport(); };
$$("#tabs button").forEach((b) => b.onclick = () => setView(b.dataset.view));
setView(location.hash === "#wallets" ? "wallets" : "coins");
setInterval(() => { if (S.view === "wallets") loadWallets(); }, 60000);
tick().then(() => { const h = location.hash.slice(1); if (/^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(h)) openCoin(h); });
setInterval(tick, 2500);
setInterval(ages, 1000);
