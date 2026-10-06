"""What the KOLs are buying right now: every token a KOL touched in the last hours, with live market cap, traders
and the KOLs' entries, built from the same pump.fun trade stream. Counts start when the first KOL trade is seen."""
from __future__ import annotations

import time
from collections import deque

LAMPORTS = 1_000_000_000
SUPPLY_TOKENS = 1_000_000_000          # pump.fun: 1e9 tokens, 6 decimals
KEEP_S = 3 * 3600
MIN_SHOW_SOL = 0.01                 # a KOL buy below this (dust / test buys) does not open a card


def mc_sol(vsol: int, vtok: int) -> float:
    return vsol / vtok * 1e6            # (vsol / 1e9 SOL) / (vtok / 1e6 tokens) * 1e9 tokens


class Watch:
    def __init__(self, names: dict[str, str], clock=time.time):
        self.names, self.clock = names, clock
        self.mints: dict[str, dict] = {}
        self.feed: deque = deque(maxlen=150)

    def on_event(self, ev: dict) -> None:
        mint = ev["mint"]
        if ev["kind"] == "complete":
            if mint in self.mints:
                self.mints[mint]["completed"] = True
            return
        kol = ev["user"] in self.names
        w = self.mints.get(mint)
        if w is None:
            if not (kol and ev["is_buy"] and ev["sol"] >= MIN_SHOW_SOL * LAMPORTS):
                return
            w = self.mints[mint] = {"mint": mint, "first_ts": ev["ts"], "kol_buys": [], "kol_sells": [],
                                    "traders": set(), "recent": deque(), "buys": 0, "sells": 0, "vol_sol": 0.0,
                                    "net": {}, "mc_sol": 0.0, "ath_mc_sol": 0.0, "completed": False,
                                    "last_kol_ts": ev["ts"]}
        sol = ev["sol"] / LAMPORTS
        mc = mc_sol(ev["vsol"], ev["vtok"])
        w["mc_sol"], w["ath_mc_sol"], w["last_ts"] = mc, max(w["ath_mc_sol"], mc), ev["ts"]
        w["traders"].add(ev["user"])
        w["recent"].append((ev["ts"], ev["user"]))
        w["buys" if ev["is_buy"] else "sells"] += 1
        w["vol_sol"] += sol
        w["net"][ev["user"]] = w["net"].get(ev["user"], 0.0) + (-sol if ev["is_buy"] else sol)
        if kol:
            act = {"ts": ev["ts"], "kol": ev["user"], "name": self.names[ev["user"]], "side": "buy" if ev["is_buy"]
                   else "sell", "sol": round(sol, 3), "mc_sol": mc, "price_sol": mc / SUPPLY_TOKENS, "mint": mint}
            w["kol_buys" if ev["is_buy"] else "kol_sells"].append(act)
            w["last_kol_ts"] = ev["ts"]
            self.feed.appendleft(act)

    def prune(self, keep: set[str]) -> None:
        cut = self.clock() - KEEP_S
        for m in [m for m, w in self.mints.items() if w["last_kol_ts"] < cut and m not in keep]:
            del self.mints[m]

    def rows(self, limit: int = 60) -> list[dict]:
        now = self.clock()
        out = []
        for w in sorted(self.mints.values(), key=lambda x: -x["last_kol_ts"])[:limit]:
            while w["recent"] and w["recent"][0][0] < now - 300:
                w["recent"].popleft()
            kols = {}
            for a in w["kol_buys"]:
                k = kols.setdefault(a["kol"], {"name": a["name"], "sol": 0.0, "first_mc_sol": a["mc_sol"],
                                               "first_ts": a["ts"], "sold_sol": 0.0})
                k["sol"] += a["sol"]
            for a in w["kol_sells"]:
                if a["kol"] in kols:
                    kols[a["kol"]]["sold_sol"] += a["sol"]
            out.append({"mint": w["mint"], "first_ts": w["first_ts"], "last_kol_ts": w["last_kol_ts"],
                        "mc_sol": w["mc_sol"], "ath_mc_sol": w["ath_mc_sol"], "completed": w["completed"],
                        "traders": len(w["traders"]), "traders_5m": len({u for _, u in w["recent"]}),
                        "buys": w["buys"], "sells": w["sells"], "vol_sol": round(w["vol_sol"], 2),
                        "kols": [dict(v, kol=k, sol=round(v["sol"], 3), sold_sol=round(v["sold_sol"], 3))
                                 for k, v in kols.items()],
                        "net": w["net"]})
        return out
