/* New Coins LIVE — every pump.fun coin as it is created, who buys it, which Smart Money, and the evidence.
   Data: /api/nc/* (stream of on-chain logs). Read only: nothing here signs or sends a transaction. */
"use strict";
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const LS = {
  get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch (_) { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (_) { /* private mode */ } },
};
const SIGS = ["NEW", "EARLY_BUY", "SMART_MONEY", "MULTI_WALLET", "HIGH_ALERT", "RISK"];
const SIGTXT = { NEW: "NEW", EARLY_BUY: "EARLY BUY", SMART_MONEY: "SMART", MULTI_WALLET: "MULTI", HIGH_ALERT: "HIGH ALERT", RISK: "RISK" };
const AGES = [["5m", 300], ["15m", 900], ["1h", 3600], ["2h", 7200]];
const F = Object.assign({ age: 3600, sig: "", minsol: "0", minsm: "0", hasbuy: false, norisk: false, q: "", snd: false }, LS.get("nc.f", {}));
const S = { code: LS.get("insAccessCode", ""), coins: [], seen: new Set(), alerted: new Set(LS.get("nc.alerted", [])), open: null, skew: 0, first: true };
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const short = (a, n = 4) => a ? `${a.slice(0, n)}…${a.slice(-n)}` : "";
const fmt = (v, d = 2) => v === null || v === undefined ? "–" : Number(v).toLocaleString("en-US", { maximumFractionDigits: d, minimumFractionDigits: 0 });
const now = () => Date.now() / 1000 + S.skew;
function age(s) {
  s = Math.max(0, Math.floor(s));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
  return `${Math.floor(s / 3600)}h ${String(Math.floor(s % 3600 / 60)).padStart(2, "0")}m`;
}
function stageOf(c) {
  const m = (now() - c.t_create) / 60;
  if (c.closed || m >= 120) return ["ĐÓNG", "s3"];
  return m < 15 ? ["0–15′", "s0"] : m < 60 ? ["15–60′", "s1"] : ["60–120′", "s2"];
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
  $("#empty").innerHTML = `<form id="lg" style="display:inline-grid;gap:8px;min-width:280px"><b>Nhập mã truy cập</b>
    <input id="lgc" type="password" style="padding:8px;background:#0c1015;border:1px solid #222c38;border-radius:4px;color:#dfe7ee">
    <button style="padding:8px;background:#36d7f0;border:0;border-radius:4px;font-weight:700;cursor:pointer">Vào</button></form>`;
  $("#lg").onsubmit = (e) => { e.preventDefault(); S.code = $("#lgc").value.trim(); LS.set("insAccessCode", S.code); $("#empty").innerHTML = ""; tick(); };
}

// ---------------------------------------------------------------- filters
function controls() {
  $("#age").innerHTML = AGES.map(([t, v]) => `<button data-v="${v}" class="${F.age === v ? "on" : ""}">${t}</button>`).join("");
  $("#sig").innerHTML = [["", "Tất cả"], ["EARLY_BUY", "Early buy"], ["SMART_MONEY", "Smart"], ["MULTI_WALLET", "Multi"], ["HIGH_ALERT", "High alert"]]
    .map(([v, t]) => `<button data-v="${v}" class="${F.sig === v ? "on" : ""}">${t}</button>`).join("");
  $("#minsol").value = F.minsol; $("#minsm").value = F.minsm; $("#hasbuy").checked = F.hasbuy; $("#norisk").checked = F.norisk; $("#q").value = F.q; $("#snd").checked = F.snd;
}
function save() { LS.set("nc.f", F); render(); }
function bind() {
  $("#age").onclick = (e) => { const b = e.target.closest("button"); if (!b) return; F.age = Number(b.dataset.v); controls(); save(); tick(); };
  $("#sig").onclick = (e) => { const b = e.target.closest("button"); if (!b) return; F.sig = b.dataset.v; controls(); save(); };
  $("#minsol").onchange = (e) => { F.minsol = e.target.value; save(); };
  $("#minsm").onchange = (e) => { F.minsm = e.target.value; save(); };
  $("#hasbuy").onchange = (e) => { F.hasbuy = e.target.checked; save(); };
  $("#norisk").onchange = (e) => { F.norisk = e.target.checked; save(); };
  $("#snd").onchange = (e) => { F.snd = e.target.checked; save(); };
  let tm; $("#q").oninput = (e) => { clearTimeout(tm); tm = setTimeout(() => { F.q = e.target.value.trim().toLowerCase(); save(); }, 150); };
  $("#mbtn").onclick = () => { $("#mlist").hidden = !$("#mlist").hidden; };
  document.addEventListener("click", (e) => { if (!e.target.closest(".menu")) $("#mlist").hidden = true; });
  $("#rows").onclick = (e) => { const tr = e.target.closest("tr[data-m]"); if (tr) openCoin(tr.dataset.m); };
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeSide(); });
}
function pass(c) {
  if (now() - c.t_create > F.age) return false;
  if (F.sig && !c.signals.includes(F.sig)) return false;
  if (Number(F.minsol) && (c.buy_sol || 0) < Number(F.minsol)) return false;
  if (Number(F.minsm) && (c.smart || 0) < Number(F.minsm)) return false;
  if (F.hasbuy && !c.buyers) return false;
  if (F.norisk && c.signals.includes("RISK")) return false;
  if (F.q && !(`${c.symbol} ${c.name} ${c.mint}`.toLowerCase().includes(F.q))) return false;
  return true;
}

// ---------------------------------------------------------------- feed
function row(c) {
  const a = now() - c.t_create, [st, sc] = stageOf(c);
  const sigs = SIGS.filter((s) => c.signals.includes(s) && (s !== "NEW" || c.signals.length === 1));
  const cls = [c.signals.includes("HIGH_ALERT") ? "hot" : c.smart ? "sm" : "", S.first || S.seen.has(c.mint) ? "" : "new", S.open === c.mint ? "sel" : ""].join(" ");
  const fb = c.first_buy_t ? `+${fmt(c.first_buy_t - c.t_create, 0)}s` : "–";
  return `<tr data-m="${c.mint}" class="${cls}">
    <td class="r"><span class="age ${a < 300 ? "" : a < 3600 ? "mid" : "old"}" data-t="${c.t_create}">${age(a)}</span></td>
    <td><span class="sym">${esc(c.symbol || "?")}</span><span class="nm">${esc(c.name)}</span><span class="mint">${short(c.mint)}</span></td>
    <td class="r">${fmt(c.mc_sol, 1)}</td><td class="r">${fmt(c.liq_sol, 2)}</td>
    <td class="r ${c.buyers ? "" : "zero"}">${c.buyers || 0}</td>
    <td class="r ${c.smart ? "smn" : "zero"}">${c.smart || 0}</td>
    <td class="r ${c.buy_sol ? "" : "zero"}">${fmt(c.buy_sol, 2)}</td>
    <td class="r ${c.first_buy_t ? "" : "zero"}">${fb}</td>
    <td>${sigs.map((s) => `<span class="sg ${s}">${SIGTXT[s]}</span>`).join("")}</td>
    <td><span class="st ${sc}">${st}</span></td></tr>`;
}
function render() {
  const rows = S.coins.filter(pass);
  $("#rows").innerHTML = rows.slice(0, 300).map(row).join("");
  $("#cnt").textContent = `${rows.length} / ${S.coins.length} coin`;
  $("#empty").innerHTML = rows.length ? "" : (S.coins.length ? "Không có coin nào khớp bộ lọc." : (S.status && !S.status.running
    ? `Luồng coin mới đang tắt: ${esc(S.status.note || "")}.<br>Không hiển thị dữ liệu giả.` : "Đang chờ coin mới đầu tiên…"));
}
function ages() { $$(".age[data-t]").forEach((el) => { el.textContent = age(now() - Number(el.dataset.t)); }); }

function header(st) {
  const live = $("#live");
  const s = st.stream, m = st.metrics;
  if (st.running && s && s.connected) {
    live.className = "live on";
    live.querySelector("b").textContent = `LIVE · ${s.provider} ws · trễ ${m.signal_latency_s.p50 ?? "–"}s`;
  } else {
    live.className = "live off";
    live.querySelector("b").textContent = st.running ? `MẤT KẾT NỐI${s && s.error ? ": " + s.error.slice(0, 40) : ""}` : "TẮT";
  }
  $("#kpi").innerHTML = m ? [["Coin 24h", m.coins_24h], ["Có lệnh mua", m.with_buys_24h], ["Có Smart Money", m.smart_coins_24h], ["Đang theo dõi", m.active_coins],
    ["Phát hiện p50", (m.discovery_latency_s.p50 ?? "–") + "s"], ["Smart list", m.smart_watchlist], ["Hàng đợi", m.queue]]
    .map(([k, v]) => `<span>${k} <b>${typeof v === "string" ? esc(v) : fmt(v, 2)}</b></span>`).join("") : "";
}
function alerts(coins) {
  for (const c of coins) {
    for (const k of ["HIGH_ALERT", "SMART_MONEY"]) {
      const key = c.mint + k;
      if (c.signals.includes(k) && !S.alerted.has(key)) {
        S.alerted.add(key);
        if (!S.first) {
          toast(`<span class="sg ${k}">${SIGTXT[k]}</span> <b>${esc(c.symbol)}</b> · ${c.smart} Smart Money · tuổi ${age(now() - c.t_create)} <a href="#" data-open="${c.mint}">xem</a>`);
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
    S.status = st; S.skew = d.now - Date.now() / 1000;
    header(st);
    alerts(d.coins);
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
function evidence(s) {
  const e = s.evidence || {};
  if (s.kind === "NEW") return `creator ${short(e.creator, 5)}`;
  if (s.kind === "EARLY_BUY") return `${short(e.wallet, 5)} mua ${fmt(e.sol, 3)} SOL, ${fmt(e.sec_after_create, 1)}s sau khi tạo`;
  if (s.kind === "SMART_MONEY") return (e.wallets || []).map((w) => `#${w.rank} ${short(w.wallet, 4)}${w.multi ? " (đa coin)" : ""}`).join(", ") + (e.last ? ` · mới nhất ${fmt(e.last.sol, 3)} SOL phút ${fmt(e.last.minute, 1)}` : "");
  if (s.kind === "MULTI_WALLET") return `${e.buyers_15m} ví mua trong 15′, ${e.independent_groups} nhóm độc lập (${esc(e.note || "")})`;
  if (s.kind === "HIGH_ALERT") return esc(e.why || "");
  if (s.kind === "RISK") return (e.whys || []).map(esc).join(" · ");
  return "";
}
function buyerRow(w, c) {
  const tags = [];
  if (w.dev) tags.push(`<span class="bd dev">CREATOR</span>`);
  if (w.in_create_slot && !w.dev) tags.push(`<span class="bd bun" title="Mua trong cùng slot tạo coin">SLOT TẠO</span>`);
  if (w.smart) tags.push(`<span class="bd sm" title="${esc((w.smart.why || []).join(" · "))}">SMART #${w.smart.rank}${w.smart.multi ? " ĐA COIN" : ""}</span>`);
  if (w.cls) tags.push(`<span class="bd ${CLS[w.cls] || "id"}">${esc(w.cls)}</span>`);
  if (w.entity) tags.push(`<span class="bd">${esc(w.entity)}</span>`);
  for (const f of w.flags || []) tags.push(`<span class="bd ${f === "CREATOR_GROUP" ? "cg" : f.startsWith("COORD") ? "co" : ""}">${esc(f.replace("COORDINATED_PATTERN", "BOT"))}</span>`);
  return `<tr class="${w.smart ? "smart" : ""}"><td class="r">+${fmt(w.sec, 0)}s</td>
    <td><a href="https://solscan.io/account/${w.wallet}" target="_blank" rel="noopener">${short(w.wallet, 5)}</a>${w.smart ? `<div class="why">${esc((w.smart.why || []).join(" · "))}</div>` : ""}</td>
    <td class="r">${fmt(w.sol_in, 3)}${w.sol_out ? `<div class="why">bán ${fmt(w.sol_out, 3)}</div>` : ""}</td><td class="r">${fmt(w.held_pct, 2)}%</td><td>${tags.join("")}</td></tr>`;
}
async function openCoin(m) { S.open = m; history.replaceState(null, "", "#" + m); $("#side").hidden = false; $$("tr.sel").forEach((t) => t.classList.remove("sel")); const tr = $(`tr[data-m="${m}"]`); if (tr) tr.classList.add("sel"); await refreshSide(); }
function closeSide() { S.open = null; history.replaceState(null, "", location.pathname); $("#side").hidden = true; $$("tr.sel").forEach((t) => t.classList.remove("sel")); }
async function refreshSide() {
  const m = S.open;
  let c;
  try { c = await api(`/api/nc/coin?mint=${m}`); } catch (e) { return; }
  if (m !== S.open) return;
  const smartN = c.buyers_list.filter((w) => w.smart).length;
  const [st] = stageOf(c);
  const sm = c.buyers_list.filter((w) => w.smart);
  $("#side").innerHTML = `<button class="x" id="cx">Đóng ✕</button>
    <h2>${esc(c.symbol)} <span style="font:500 13px var(--sans);color:var(--mut)">${esc(c.name)}</span></h2>
    <div class="sub">${esc(c.platform)} · tạo ${new Date(c.t_create * 1000).toISOString().slice(11, 19)} UTC · tuổi ${age(now() - c.t_create)} · ${st} · phát hiện sau ${fmt(c.seen_at - c.t_create, 1)}s</div>
    <div class="addr">${esc(c.mint)}</div>
    <div class="links"><a href="https://pump.fun/coin/${c.mint}" target="_blank" rel="noopener">pump.fun ↗</a><a href="https://dexscreener.com/solana/${c.mint}" target="_blank" rel="noopener">DexScreener ↗</a>
      <a href="https://solscan.io/token/${c.mint}" target="_blank" rel="noopener">Solscan ↗</a><a href="https://solscan.io/account/${c.creator}" target="_blank" rel="noopener">creator ${short(c.creator)} ↗</a></div>
    <div class="grid4">${[["MC (SOL)", fmt(c.mc_sol, 1)], ["Liq (SOL)", fmt(c.liq_sol, 2)], ["Ví mua", c.buyers], ["Smart Money", smartN],
      ["SOL vào", fmt(c.buy_sol, 2)], ["SOL ra", fmt(c.sell_sol, 2)], ["Creator mua", fmt(c.dev_sol, 2) + " SOL"], ["Mua đầu", c.first_buy_t ? "+" + fmt(c.first_buy_t - c.t_create, 0) + "s" : "–"]]
      .map(([k, v]) => `<div><div class="k">${k}</div><div class="v">${v}</div></div>`).join("")}</div>
    <h3>Tín hiệu & bằng chứng</h3>
    <div class="tl">${c.signals.map((s) => `<div><span><span class="sg ${s.kind}">${SIGTXT[s.kind]}</span></span><span class="m">phút ${fmt(s.minute, 1)}</span><span class="e">${evidence(s)}</span></div>`).join("") || "<div class='e'>Chưa có</div>"}</div>
    ${sm.length ? `<h3>Smart Money đã mua (${sm.length})</h3><table class="bt"><tbody>${sm.map((w) => buyerRow(w, c)).join("")}</tbody></table>` : ""}
    <h3>Ai đang mua (${c.buyers_list.length} ví, theo thứ tự mua)</h3>
    <table class="bt"><thead><tr><th class="r">Sau tạo</th><th>Ví</th><th class="r">SOL</th><th class="r">Giữ</th><th>Bằng chứng</th></tr></thead>
    <tbody>${c.buyers_list.slice(0, 150).map((w) => buyerRow(w, c)).join("")}</tbody></table>
    <h3>Giao dịch gần nhất</h3>
    <table class="bt"><tbody>${c.trades_list.slice(0, 40).map((t) => `<tr><td class="r">+${fmt(t.t - c.t_create, 0)}s</td><td class="${t.buy ? "up" : "down"}">${t.buy ? "MUA" : "BÁN"}</td>
      <td class="r">${fmt(t.sol, 3)} SOL</td><td><a href="https://solscan.io/account/${t.wallet}" target="_blank" rel="noopener">${short(t.wallet, 4)}</a>${t.smart ? ' <span class="bd sm">SMART</span>' : ""}${t.dev ? ' <span class="bd dev">CREATOR</span>' : ""}</td>
      <td><a href="https://solscan.io/tx/${t.sig}" target="_blank" rel="noopener">tx ↗</a></td></tr>`).join("")}</tbody></table>
    <div class="note">Dữ liệu trực tiếp từ log on-chain của pump.fun. Phân loại ví (INDEPENDENT / POSSIBLE_LINK / …) và cờ lấy từ cơ sở dữ liệu Smart Money đã có; ví chưa có hồ sơ hiển thị không nhãn. Không có điểm tổng hợp.</div>`;
  $("#cx").onclick = closeSide;
}

controls(); bind(); tick().then(() => { const h = location.hash.slice(1); if (/^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(h)) openCoin(h); });
setInterval(tick, 2500);
setInterval(ages, 1000);
