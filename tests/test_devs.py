"""Dev / creator profiles: outcome classes without survivorship, rug only with evidence, risk with reason +
evidence + timestamp, persistence across restarts, API history merge, KNOWN / NEW DEV recognition."""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kolbot import api  # noqa: E402
from kolbot import devs as D  # noqa: E402
from kolbot.store import Store  # noqa: E402

T0 = 1_790_000_000.0
L = 1_000_000_000


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def create(mint, creator, ts):
    return {"kind": "create", "mint": mint, "name": mint.lower(), "symbol": mint, "creator": creator, "user": creator,
            "ts": ts}


def tr(mint, user, buy, ts, tok=1_000_000, vs=30 * L, vt=10 ** 15, creator=None):
    return {"kind": "trade", "mint": mint, "user": user, "is_buy": buy, "sol": L // 10, "token": tok, "ts": ts,
            "vsol": vs, "vtok": vt, "fee_bps": 125, "creator": creator}


def tracker(tmp_path, t=T0):
    st = Store(tmp_path / "d.db")
    clk = Clock(t)
    return st, D.DevTracker(st.db, clock=clk), clk


def test_classify_outcomes_no_survivorship():
    now = T0 + 3 * D.DAY
    assert D.classify({"created_ts": now - 3600}, now)["outcome"] == "PENDING"
    assert D.classify({"created_ts": now - 3600, "migrated": 1}, now)["outcome"] == "MIGRATED"
    assert D.classify({"created_ts": now - 2 * D.DAY, "last_trade_ts": now - 3600}, now)["outcome"] == "FAILED"
    c = D.classify({"created_ts": now - 2 * D.DAY, "last_trade_ts": now - 1.5 * D.DAY}, now)
    assert c["outcome"] == "DEAD" and c["failed"] and c["known"]
    p = D.classify({"created_ts": now - 60}, now)
    assert not p["known"] and not p["success"] and not p["failed"]          # pending: neither win nor loss


def test_rug_needs_evidence_and_creation_view():
    now = T0 + 3 * D.DAY
    base = {"created_ts": T0, "first_seen_ts": T0 + 5, "dev_buy_tok": 100, "dev_sell_tok": 95, "peak_mc_sol": 100,
            "last_mc_sol": 10, "first_mc_sol": 30, "last_trade_ts": T0 + 600}
    c = D.classify(base, now)
    assert c["rug"] is True and "dev bán 95%" in c["rug_evidence"]
    assert D.classify(dict(base, dev_sell_tok=50), now)["rug"] is False               # dev kept half
    assert D.classify(dict(base, last_mc_sol=40), now)["rug"] is False                # -60 % only: not enough
    late = D.classify(dict(base, first_seen_ts=T0 + 3600), now)                       # not watched from creation
    assert late["rug"] is None and not late["rug_checkable"] and late["outcome_pct"] is None
    young = D.classify(dict(base, created_ts=now - 60, first_seen_ts=now - 55), now)  # price fell but pending
    assert young["rug"] is None


def test_risk_labels_with_reason():
    p = dict(known=2, rug=0, rug_checkable=0, migrated=0, fail_rate=1.0)
    assert D.risk_of(p)[0] == "UNKNOWN"
    assert D.risk_of(dict(p, known=6, migrated=0))[0] == "REPEAT FAILURE"
    assert D.risk_of(dict(p, known=6, rug=2, rug_checkable=4))[0] == "SUSPICIOUS / RUG HISTORY"
    assert D.risk_of(dict(p, known=4, migrated=1, fail_rate=0.75))[0] == "MEDIUM RISK"
    assert D.risk_of(dict(p, known=4, migrated=2, fail_rate=0.5))[0] == "LOW RISK"
    assert D.risk_of(dict(p, known=4, migrated=1, fail_rate=0.5, rug=1, rug_checkable=1))[0] == "HIGH RISK"
    assert "0 token migrate" in D.risk_of(dict(p, known=6))[1]


def test_tracker_flow_persists_and_recognises_known_dev(tmp_path):
    st, tk, clk = tracker(tmp_path)
    dev = "DEV1"
    tk.on_event(create("OLD", dev, T0))                                               # old token: dev dumps it
    tk.on_event(tr("OLD", dev, True, T0 + 1, tok=100, creator=dev))
    tk.on_event(tr("OLD", "x", True, T0 + 10, vs=120 * L, vt=25 * 10 ** 13, creator=dev))   # peak
    tk.on_event(tr("OLD", dev, False, T0 + 20, tok=100, vs=31 * L, vt=10 ** 15, creator=dev))
    tk.on_event(tr("OLD", "x", False, T0 + 30, vs=3 * L, vt=10 ** 15, creator=dev))   # collapse
    tk.on_event(create("WIN", dev, T0 + 100))
    tk.on_event({"kind": "complete", "mint": "WIN", "ts": T0 + 500})
    tk.tick(force=True)
    clk.t = T0 + 2 * D.DAY                                                            # outcomes become known
    tk.refresh_dev(dev)
    st.db.commit()
    p = D.get_dev_profile(st.db, dev)
    assert p["created"] == 2 and p["known"] == 2 and p["migrated"] == 1 and p["failed"] == 1
    assert p["rug"] == 1 and p["rug_checkable"] == 2 and p["rug_rate"] == 0.5
    assert p["risk"] == "UNKNOWN" and p["risk_ts"] and p["risk_evidence"]["rug_tokens"][0]["mint"] == "OLD"
    assert D.get_token_creator(st.db, "WIN") == dev
    assert D.get_dev_risk(st.db, dev)["history"][0]["risk"] == "UNKNOWN"
    # restart: a new tracker on the same database sees the history; a new token is recognised at once
    st2 = Store(tmp_path / "d.db")
    tk2 = D.DevTracker(st2.db, clock=Clock(T0 + 2 * D.DAY))
    tk2.on_event(create("NEW", dev, T0 + 2 * D.DAY))
    tk2.tick(force=True)
    b = D.dev_badge(st2.db, dev)
    assert b["known_dev"] and b["label"] == "KNOWN DEV" and b["previous_tokens"] == 2
    assert api._token_devs(st2.db, ["NEW"])["NEW"]["label"] == "KNOWN DEV"
    assert D.dev_badge(st2.db, "SOMEONE")["label"] == "NEW DEV"
    toks = D.get_dev_tokens(st2.db, dev, now=T0 + 2 * D.DAY)
    assert toks["total"] == 3 and {t["outcome"] for t in toks["rows"]} == {"PENDING", "MIGRATED", "DEAD"}


def test_api_history_merge_keeps_stream_fields(tmp_path):
    st, tk, clk = tracker(tmp_path)
    tk.on_event(create("S", "D2", T0))
    tk.on_event(tr("S", "x", True, T0 + 2, vs=40 * L, creator="D2"))
    tk.ingest_api("D2", [{"mint": "S", "name": "zzz", "created_timestamp": (T0 - 999) * 1000, "complete": False},
                         {"mint": "P1", "creator": "D2", "created_timestamp": (T0 - 10 * D.DAY) * 1000,
                          "complete": True, "ath_market_cap": 90000, "usd_market_cap": 50000},
                         {"mint": "P2", "created_timestamp": (T0 - 9 * D.DAY) * 1000, "complete": False,
                          "last_trade_timestamp": (T0 - 9 * D.DAY) * 1000}])
    tk.tick(force=True)
    s = st.db.execute("SELECT name, created_ts, source FROM tokens WHERE mint='S'").fetchone()
    assert s == ("s", T0, "stream")                                                    # stream data not overwritten
    p = D.get_dev_profile(st.db, "D2")
    assert p["created"] == 3 and p["migrated"] == 1 and p["dead"] == 1 and p["pending"] == 1
    assert p["rug_checkable"] == 0                                                     # API tokens: never a rug
    link = st.db.execute("SELECT relation, source FROM wallet_links WHERE wallet='D2'").fetchone()
    assert link == ("creator", "pump.fun:creator")


def test_dev_table_sort_and_detail(tmp_path):
    st, tk, clk = tracker(tmp_path)
    for i in range(3):
        tk.on_event(create(f"A{i}", "DA", T0 + i))
    tk.on_event(create("B0", "DB", T0))
    tk.on_event({"kind": "complete", "mint": "A0", "ts": T0 + 50})
    tk.tick(force=True)
    t = api.dev_table(st.db, min_tokens=1)
    assert [r["wallet"] for r in t["rows"]] == ["DA", "DB"] and t["rows"][0]["rank"] == 1
    assert api.dev_table(st.db, min_tokens=2)["total"] == 1
    for k in api.DEV_SORTS:
        assert api.dev_table(st.db, min_tokens=1, sort=k)["total"] == 2
    d = api.dev_detail(st.db, "DA")
    assert d["wallet_url"] == "https://solscan.io/account/DA" and d["tokens"]["total"] == 3
    assert d["tokens"]["rows"][0]["token_url"].startswith("https://pump.fun/coin/")
    assert api.dev_detail(st.db, "NOBODY") is None
    tok = api.token_detail(st.db, "A0", {})
    assert tok["outcome"] == "MIGRATED" and tok["dev"]["creator"] == "DA" and tok["dev"]["known_dev"]
    assert api.token_table(st.db, scope="all")["total"] == 4
    assert api.token_table(st.db, scope="kol")["total"] == 0


def test_engine_ignores_create_events():
    from kolbot.config import Config
    from kolbot.engine import Engine
    e = Engine(Config(), {"K"}, None, log=lambda *_: None)
    e.on_event(create("M", "K", T0))
    assert not e.pending and not e.curves
