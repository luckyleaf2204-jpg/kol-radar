"use strict";
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const getCode = () => { try { return JSON.parse(localStorage.getItem("insAccessCode") || '""'); } catch (e) { return window._code || ""; } };
const setCode = (v) => { try { localStorage.setItem("insAccessCode", JSON.stringify(v)); } catch (e) { window._code = v; } };
const TABS = [["signals", "Live Signals"], ["callers", "Elite Callers"], ["history", "Call History"], ["coins", "Early Coins"],
  ["backtest", "Backtest"], ["watch", "Watchlist"]];
const S = { tab: (location.hash || "#signals").slice(1), caller: null, bt: { h: "24h", group: "week" } };

async function api(path, opt) {
  const r = await fetch(path, Object.assign({ cache: "no-store", headers: { "X-Access-Code": getCode() } }, opt || {}));
  if (r.status === 401) { $("#gate").hidden = false; throw new Error("Cần mã truy cập"); }
  if (!r.ok) throw new Error(path + " → " + r.status);
  return r.json();
}
const pct = (v, d = 0) => v == null ? "–" : `<span class="${v >= 0 ? "pos" : "neg"}">${v >= 0 ? "+" : ""}${(100 * v).toFixed(d)}%</span>`;
const mult = (v) => v == null ? "–" : `${(1 + v).toFixed(2)}x`;
const usd = (v) => v == null ? "–" : v >= 1e6 ? `$${(v / 1e6).toFixed(2)}M` : v >= 1e3 ? `$${(v / 1e3).toFixed(1)}k` : `$${(+v).toPrecision(3)}`;
const ts = (t) => t ? new Date(t * 1000).toLocaleString("vi-VN") : "–";
const short = (m) => m ? `${m.slice(0, 4)}…${m.slice(-4)}` : "–";
const pill = (x) => x ? `<span class="pill ${esc(x)}">${esc(x)}</span>` : "";
const dur = (s) => s == null ? "–" : s < 3600 ? `${Math.round(s / 60)}m` : s < 86400 ? `${(s / 3600).toFixed(1)}h` : `${(s / 86400).toFixed(1)}d`;
const mintLink = (m) => m ? `<a class="ext mono" target="_blank" rel="noopener" href="https://dexscreener.com/solana/${esc(m)}">${short(m)}</a>` : "–";
function table(head, rows, click) {
  return `<div class="card"><table><tr>${head.map((h) => `<th>${h}</th>`).join("")}</tr>${rows.length ? rows.map((r, i) => `<tr${click ? ` class="click" data-i="${i}"` : ""}>${r.map((c) => `<td>${c}</td>`).join("")}</tr>`).join("") : `<tr><td class="mut" colspan="${head.length}">chưa có dữ liệu</td></tr>`}</table></div>`;
}

async function status() {
  const o = await api("/api/callers/overview");
  const x = o.x || {};
  $("#status").innerHTML = [
    `X: <b>${x.enabled ? "BẬT" : "TẮT"}</b>${x.enabled ? ` · đã dùng ~$${x.spent_est_usd} / $${x.budget_usd} tháng ${esc(x.month)}` : x.token_set ? " · token có, ngân sách = 0" : " · chưa có X_BEARER_TOKEN"}`,
    `Caller: ${Object.entries(o.tiers || {}).map(([k, v]) => `${k} <b>${v}</b>`).join(" · ") || "0"}`,
    `Call: <b>${o.calls}</b> · chờ đánh giá ${o.pending_eval}`,
    `Tín hiệu: ${Object.entries(o.signals || {}).map(([k, v]) => `${k} <b>${v}</b>`).join(" · ") || "0"}`,
  ].map((c) => `<span class="chip">${c}</span>`).join("");
}

const pages = {
  async signals() {
    const s = await api("/api/callers/signals?limit=150");
    return `<h2>Live Signals — coin vừa được caller call (khi bot thấy bài trong 10 phút)</h2>` + table(
      ["Lúc", "Coin", "Caller", "Hạng caller", "Điểm caller", "Số caller tốt cùng call", "Giá", "Market cap", "Thanh khoản", "Vol 1h", "Signal Score", "Nhãn", "Cờ", "Bài X", "Mint"],
      s.map((r) => [ts(r.created_at), esc((r.snapshot || {}).symbol || r.ticker || ""), "@" + esc(r.username), pill(r.tier), r.caller_score ?? "–",
        (r.components || {}).consensus ?? "–", usd((r.snapshot || {}).price_usd), usd((r.snapshot || {}).mcap), usd((r.snapshot || {}).liquidity_usd),
        usd((r.snapshot || {}).volume_h1), `<b>${r.score}</b>`, pill(r.label), esc(((r.components || {}).flags || []).join(", ")),
        r.url ? `<a class="ext" target="_blank" rel="noopener" href="${esc(r.url)}">mở</a>` : "–", mintLink(r.mint)]));
  },
  async callers() {
    const d = await api("/api/callers/leaderboard");
    const rows = d.callers;
    window._callers = rows;
    return `<h2>Bảng xếp hạng (chỉ xếp hạng khi có ≥ ${d.min_ranked_calls} call đã kiểm chứng)</h2>` + table(
      ["#", "Caller", "Nguồn", "Hạng", "Điểm", "Tổng call", "Đã kiểm chứng", "≥2x", "≥5x", "≥10x", "Win rate", "Win rate (cận dưới 95%)", "Mức tăng trung vị", "Call muộn", "Rug", "TG tới đỉnh TB"],
      rows.map((r) => [r.rank ?? "–", "@" + esc(r.username), esc(r.platform), pill(r.tier), r.score ?? "–", r.calls ?? 0, r.verified ?? 0, r.wins_2x ?? 0, r.wins_5x ?? 0,
        r.wins_10x ?? 0, pct(r.win_rate), pct(r.win_rate_low95), pct(r.median_gain), pct(r.late_share), pct(r.rug_rate), dur(r.avg_time_to_peak_s)]), true);
  },
  async history() {
    const list = (await api("/api/callers/leaderboard")).callers;
    const u = S.caller || (list[0] || {}).username;
    if (!u) return `<div class="card mut">Chưa có caller.</div>`;
    const d = await api("/api/callers/caller?u=" + encodeURIComponent(u));
    const m = d.caller.metrics || {};
    const opts = list.map((c) => `<option ${c.username === u ? "selected" : ""} value="${esc(c.username)}">@${esc(c.username)} (${c.verified ?? 0})</option>`).join("");
    return `<div class="row"><select id="pick">${opts}</select> ${pill(d.caller.tier)} <span class="mut">điểm ${d.caller.score ?? "–"}</span></div>
      <div class="grid">${[["Call kiểm chứng", m.verified], ["≥2x", m.wins_2x], ["Win rate", pct(m.win_rate)], ["Trung vị tăng", pct(m.median_gain)], ["Call muộn", pct(m.late_share)], ["Rug", pct(m.rug_rate)]]
        .map(([l, v]) => `<div class="kpi"><div class="k">${v ?? "–"}</div><div class="l">${l}</div></div>`).join("")}</div>
      <h2>Hiệu suất theo thời gian (mức tăng bán được tốt nhất trong 7 ngày, mỗi chấm = 1 call)</h2>${chart(d.calls)}
      <h2>Toàn bộ call</h2>` + table(["Lúc call", "Coin", "Kết quả", "Giá vào (bán được)", "Đỉnh bán được", "Đỉnh lý thuyết", "1h", "6h", "24h", "7d", "Tăng trước call 1h", "Rug", "Nguồn TK", "Bài"],
      d.calls.slice().reverse().map((c) => [ts(c.post_time), mintLink(c.mint) + (c.ticker ? ` $${esc(c.ticker)}` : ""), pill(c.cls || c.status) + (c.final ? "" : ` <span class="mut">tạm</span>`),
        c.entry_raw ? c.entry_raw.toPrecision(3) : "–", pct(c.best_tradable_gain), pct(c.best_theoretical_gain),
        ...["1h", "6h", "24h", "7d"].map((h) => pct(((c.horizons || {})[h] || {}).tradable_gain)), pct(c.runup_1h), c.rug ? "có" : "", esc(c.liq_source || ""),
        c.url ? `<a class="ext" target="_blank" rel="noopener" href="${esc(c.url)}">mở</a>` : esc(c.source)]));
  },
  async coins() {
    const c = await api("/api/callers/coins");
    return `<h2>Early Coins — xếp theo Signal Score cao nhất</h2>` + table(["Coin", "Signal Score", "Số caller", "Call đầu tiên", "Giá", "Market cap", "Thanh khoản", "Mint authority", "Freeze authority", "Top10 nắm", "Mint"],
      c.map((r) => [esc(r.symbol || ""), `<b>${r.score}</b>`, r.callers, ts(r.first_call), usd((r.snapshot || {}).price_usd), usd((r.snapshot || {}).mcap), usd((r.snapshot || {}).liquidity_usd),
        (r.safety || {}).mint_authority ? "CÒN" : "đã bỏ", (r.safety || {}).freeze_authority ? "CÒN" : "đã bỏ", pct((r.safety || {}).top10_share), mintLink(r.mint)]));
  },
  async backtest() {
    const b = await api(`/api/callers/backtest?h=${S.bt.h}&group=${S.bt.group}`);
    const sel = (id, v, opts) => `<select id="${id}">${opts.map(([k, l]) => `<option value="${k}" ${k === v ? "selected" : ""}>${l}</option>`).join("")}</select>`;
    return `<div class="row">Mốc ${sel("bth", S.bt.h, [["1h", "1 giờ"], ["6h", "6 giờ"], ["24h", "24 giờ"], ["7d", "7 ngày"]])} Gom theo ${sel("btg", S.bt.group, [["day", "ngày"], ["week", "tuần"], ["month", "tháng"]])}
      <span class="mut">${b.verified_calls}/${b.all_calls} call có giá để kiểm chứng · ${esc(b.note)}</span></div>
      <h2>Hiệu suất theo mốc</h2>` + table(["Mốc", "Số call", "Tỷ lệ ≥2x (bán được)", "Mức tăng bán được trung vị", "Lời/lỗ nếu giữ tới mốc (TB)", "Đỉnh lý thuyết trung vị"],
      Object.entries(b.per_horizon).map(([k, v]) => [k, v.n, pct(v.share_2x), pct(v.median_tradable_gain), pct(v.mean_return_at), pct(v.median_theoretical_gain)])) +
      `<h2>Win rate theo ${esc(b.group)} (mốc ${esc(b.horizon)})</h2>` + table(["Kỳ", "Số call", "≥2x", "Win rate", "Giữ tới mốc (TB)"], b.periods.map((p) => [esc(p.period), p.calls, p.wins_2x, pct(p.win_rate), pct(p.mean_return_at)])) +
      `<h2>Caller tốt nhất (mốc ${esc(b.horizon)})</h2>` + table(["Caller", "Số call", "Win rate", "Giữ tới mốc (TB)", "Call muộn"], b.best_callers.map((c) => ["@" + esc(c.username), c.calls, pct(c.win_rate), pct(c.mean_return_at), pct(c.late_share)])) +
      `<h2>Caller hay call muộn (coin đã tăng ≥100% trong 1 giờ trước bài)</h2>` + table(["Caller", "Số call", "Tỷ lệ call muộn", "Win rate"], b.late_callers.map((c) => ["@" + esc(c.username), c.calls, pct(c.late_share), pct(c.win_rate)])) +
      `<h2>Coin tăng mạnh nhất sau call</h2>` + table(["Coin", "Caller", "Lúc call", "Tăng bán được", "Đỉnh lý thuyết"], b.top_coins.map((c) => [mintLink(c.mint), "@" + esc(c.username), ts(c.post_time), pct(c.tradable_gain), pct(c.theoretical_gain)]));
  },
  async watch() {
    const w = await api("/api/callers/watchlist");
    return `<h2>Thêm vào theo dõi</h2><div class="row"><select id="wk"><option value="coin">Coin (mint)</option><option value="caller">Caller (username X)</option></select>
      <input id="wkey" placeholder="mint hoặc @username" size="46"> <button id="wadd">Thêm</button></div>
      <h2>Nhập call thủ công</h2><div class="card"><div class="mut">Mỗi dòng: <span class="mono">link bài X, mint hoặc $TICKER</span> (giờ đăng lấy chính xác từ ID bài) — hoặc <span class="mono">username, thời gian ISO, mint</span>.</div>
      <textarea id="imp" placeholder="https://x.com/someone/status/1890000000000000000, 9xQe...pump"></textarea><div class="row"><button id="impgo">Nhập</button> <span id="impres" class="mut"></span></div></div>
      <h2>Coin đang theo dõi</h2>` + table(["Coin", "Giá", "Thanh khoản", "Caller đã call", "Thêm lúc", ""], w.coins.map((c) => [mintLink(c.key) + " " + esc(c.symbol || ""), usd((c.snapshot || {}).price_usd), usd((c.snapshot || {}).liquidity_usd),
        c.calls.map((x) => "@" + esc(x.username)).join(", ") || "–", ts(c.added_at), `<button data-rm="coin|${esc(c.key)}">Bỏ</button>`])) +
      `<h2>Caller đang theo dõi</h2>` + table(["Caller", "Hạng", "Điểm", "Call gần đây", ""], w.callers.map((c) => ["@" + esc(c.key), pill(c.tier), c.score ?? "–",
        c.recent.map((x) => short(x.mint)).join(", ") || "–", `<button data-rm="caller|${esc(c.key)}">Bỏ</button>`]));
  },
};

function chart(calls) {
  const pts = calls.filter((c) => c.best_tradable_gain != null);
  if (!pts.length) return `<div class="card mut">Chưa có call nào đủ dữ liệu giá.</div>`;
  const W = 900, H = 220, P = 30, t0 = pts[0].post_time, t1 = pts[pts.length - 1].post_time || t0 + 1;
  const y = (g) => { const v = Math.log2(Math.max(0.05, 1 + g)); return H - P - (v + 4.33) / (4.33 + 4) * (H - 2 * P); };
  const x = (t) => P + (t1 > t0 ? (t - t0) / (t1 - t0) : 0.5) * (W - 2 * P);
  const grid = [[-0.5, "0.5x"], [0, "1x"], [1, "2x"], [4, "5x"], [9, "10x"]].map(([g, l]) => `<line x1="${P}" x2="${W - P}" y1="${y(g)}" y2="${y(g)}" stroke="var(--line)"/><text x="2" y="${y(g) + 3}">${l}</text>`).join("");
  const dots = pts.map((c) => `<circle cx="${x(c.post_time)}" cy="${y(c.best_tradable_gain)}" r="4" fill="${c.best_tradable_gain >= 1 ? "var(--good)" : c.best_tradable_gain >= 0 ? "var(--warn)" : "var(--bad)"}"><title>${short(c.mint)} ${mult(c.best_tradable_gain)} · ${ts(c.post_time)}</title></circle>`).join("");
  return `<div class="card"><svg viewBox="0 0 ${W} ${H}" width="100%" role="img" aria-label="Hiệu suất call theo thời gian">${grid}${dots}</svg></div>`;
}

async function route() {
  if (!TABS.some(([k]) => k === S.tab)) S.tab = "signals";
  $("#tabs").innerHTML = TABS.map(([k, l]) => `<a href="#${k}" class="${S.tab === k ? "on" : ""}">${l}</a>`).join("");
  try {
    await status();
    $("#app").innerHTML = await pages[S.tab]();
    $("#err").textContent = "";
    bind();
  } catch (e) { $("#err").textContent = e.message; }
}

function bind() {
  document.querySelectorAll("tr.click").forEach((tr) => tr.addEventListener("click", () => {
    const r = (window._callers || [])[+tr.dataset.i]; if (r) { S.caller = r.username; location.hash = "#history"; }
  }));
  const pick = $("#pick"); if (pick) pick.onchange = () => { S.caller = pick.value; route(); };
  const bh = $("#bth"), bg = $("#btg");
  if (bh) bh.onchange = () => { S.bt.h = bh.value; route(); };
  if (bg) bg.onchange = () => { S.bt.group = bg.value; route(); };
  const add = $("#wadd");
  if (add) add.onclick = async () => {
    await api(`/api/callers/watchlist?kind=${$("#wk").value}&key=${encodeURIComponent($("#wkey").value.trim())}&op=add`, { method: "POST" }); route();
  };
  document.querySelectorAll("[data-rm]").forEach((b) => b.onclick = async () => {
    const [k, key] = b.dataset.rm.split("|"); await api(`/api/callers/watchlist?kind=${k}&key=${encodeURIComponent(key)}&op=remove`, { method: "POST" }); route();
  });
  const ig = $("#impgo");
  if (ig) ig.onclick = async () => {
    const r = await api("/api/callers/import", { method: "POST", body: JSON.stringify({ text: $("#imp").value }) });
    $("#impres").textContent = `Đã thêm ${r.added} call` + (r.errors.length ? ` · lỗi: ${r.errors.join("; ")}` : "");
  };
}

$("#gate").addEventListener("submit", (e) => { e.preventDefault(); setCode($("#code").value); $("#gate").hidden = true; route(); });
window.addEventListener("hashchange", () => { S.tab = (location.hash || "#signals").slice(1); route(); });
route();
setInterval(() => { if (["signals", "coins"].includes(S.tab)) route(); }, 30000);
