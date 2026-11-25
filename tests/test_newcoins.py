"""New Coins LIVE: pump.fun log decoding, the invoke-stack filter (forks), signals with evidence, restart without
double counting, many coins at once. Synthetic logs only, no network."""
import base64
import json
import struct
import threading

from insiders import newcoins as N
from insiders import pumplog as P
from insiders import track as T

MINT = "8DzqPteAAAkhozWJeWFwYS9ZiZLD2psU6BPiGFuwpump"
DEV = "6da78GcgVThK55YQM5DN6GhdE5qcb59aj7gUDWSUmLjz"
CURVE = "BbENzJsg9REH23QDYSnKhMw54APD4EATmKFTuTfmhpLY"
W = ["9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM", "5uGbn3xfFEgmy3qhLmXPUGeBQee8hwTuAnHCktTfFRXC", "H9kuqGKNYgPFYCA17uUTkaqXGum7MK8y1GwbS7cxsjDg",
     "HnNG8q3Uuz9yGzcTT6WamSeJiauobC5Anbjvxevjq6r9", "AcysJyF3VVkS6pUU2AgMYNaNrkjXAuTW4Q2LPkPp6TCT", "4dbKnVgJhagHYkagvAfPJQoK7amNpzPPdCAGR1m2n8o9",
     "5DsVcXWvsT11B2WSQ1UqdZWtHideRFvBVpDpZqn3GMVK"]
T0 = 1_800_000_000


def key(a):
    alph = P._ALPH
    n = 0
    for ch in a:
        n = n * 58 + alph.index(ch)
    return n.to_bytes(32, "big")


def s(x):
    b = x.encode()
    return struct.pack("<I", len(b)) + b


def create(mint=MINT, dev=DEV, ts=T0):
    b = P.CREATE + s("Megan Fox") + s("FOX") + s("ipfs://x") + key(mint) + key(CURVE) + key(dev) + key(dev) + struct.pack("<q", ts) + \
        struct.pack("<QQ", 1_073_000_000_000_000, 30_000_000_000)
    return "Program data: " + base64.b64encode(b).decode()


def trade(user, sol, tokens, buy=True, ts=T0 + 10, mint=MINT, vsol=31_000_000_000, vtok=1_040_000_000_000_000):
    b = P.TRADE + key(mint) + struct.pack("<QQ", int(sol * 1e9), int(tokens * 1e6)) + (b"\x01" if buy else b"\x00") + key(user) + \
        struct.pack("<qQQQ", ts, vsol, vtok, int(sol * 1e9))
    return "Program data: " + base64.b64encode(b).decode()


def logs(*data, program=P.PUMP):
    return [f"Program {program} invoke [1]", *data, f"Program {program} success"]


def eng(tmp_path, now=T0 + 20):
    st = T.Store(tmp_path / "t.db")
    e = N.Engine(st, now=lambda: now)
    e.book.loaded = 1e18                                       # no watchlist refresh from esm tables in unit tests
    return st, e


def test_decoder_reads_create_and_trade_and_ignores_forks():
    ev = P.events(logs(create(), trade(W[0], 1.5, 1000)))
    assert [e["ev"] for e in ev] == ["create", "trade"]
    assert ev[0]["mint"] == MINT and ev[0]["symbol"] == "FOX" and ev[0]["creator"] == DEV and ev[0]["ts"] == T0
    assert ev[1]["user"] == W[0] and ev[1]["sol"] == 1_500_000_000 and ev[1]["buy"] is True
    assert P.events(logs(create(), program="ForkProgram1111111111111111111111111111111")) == []   # same layout, other program
    nested = ["Program X invoke [1]", f"Program {P.PUMP} invoke [2]", trade(W[0], 1, 1), f"Program {P.PUMP} success", create(), "Program X success"]
    assert [e["ev"] for e in P.events(nested)] == ["trade"]   # the create was emitted by X, not pump.fun


def test_signals_with_evidence(tmp_path):
    st, e = eng(tmp_path)
    e.book.smart = {W[1]: {"rank": 1, "multi": True, "why": ["vào sớm CHUCHU phút 0"], "creator_of": []},
                    W[2]: {"rank": 2, "multi": True, "why": ["x"], "creator_of": []}}
    e.put("c1", 100, logs(create(), trade(DEV, 3.5, 120_000_000)), T0 + 1)            # dev buys 12 % in the create
    e.put("b1", 100, logs(trade(W[0], 0.5, 1000)), T0 + 3)                              # same slot as the create
    e.put("b2", 100, logs(trade(W[3], 0.5, 1000)), T0 + 3)
    e.put("b3", 100, logs(trade(W[4], 0.5, 1000)), T0 + 3)                              # 3 wallets in the creation slot: bundle
    e.put("b4", 101, logs(trade(W[1], 2.0, 5000, ts=T0 + 40)), T0 + 41)
    e.put("b5", 102, logs(trade(W[5], 0.2, 500, ts=T0 + 50)), T0 + 51)
    e.put("b6", 103, logs(trade(W[2], 1.0, 900, ts=T0 + 60)), T0 + 61)
    e.put("s1", 104, logs(trade(DEV, 3.0, 100_000_000, buy=False, ts=T0 + 70)), T0 + 71)
    e.step(drain(e))
    sig = {r["kind"]: r for r in st.q("SELECT * FROM nc_signal WHERE mint=?", (MINT,))}
    assert set(sig) == {"NEW", "EARLY_BUY", "SMART_MONEY", "MULTI_WALLET", "HIGH_ALERT", "RISK"}
    assert json.loads(sig["EARLY_BUY"]["evidence"])["wallet"] == W[0]
    sm = json.loads(sig["SMART_MONEY"]["evidence"])
    assert [w["wallet"] for w in sm["wallets"]] == [W[1], W[2]] and sm["wallets"][0]["rank"] == 1
    whys = json.loads(sig["RISK"]["evidence"])["whys"]
    assert any("creator mua 12%" in x for x in whys) and any("bundle" in x for x in whys) and any("creator bán" in x for x in whys)
    c = st.q("SELECT * FROM nc_coin WHERE mint=?", (MINT,))[0]
    assert c["buyers"] == 6 and c["smart"] == 2 and round(c["dev_sol"], 2) == 3.5 and c["first_buy_t"] == T0 + 10
    d = N.coin_detail(st, MINT, e.book)
    assert d["buyers_list"][0]["dev"] and [b["wallet"] for b in d["buyers_list"] if b["smart"]] == [W[1], W[2]]


def drain(e):
    out = []
    while not e.q.empty():
        out.append(e.q.get_nowait())
    return out


def test_restart_reloads_young_coins_and_never_counts_twice(tmp_path):
    st, e = eng(tmp_path)
    e.put("c1", 100, logs(create()), T0 + 1)
    e.put("b1", 101, logs(trade(W[0], 1.0, 1000)), T0 + 11)
    e.step(drain(e))
    e2 = N.Engine(st, now=lambda: T0 + 30)                      # a restart
    e2.book.loaded = 1e18
    assert MINT in e2.coins and W[0] in e2.coins[MINT].buyers
    e2.put("c1", 100, logs(create()), T0 + 1)                   # replayed after a reconnect
    e2.put("b1", 101, logs(trade(W[0], 1.0, 1000)), T0 + 11)
    e2.step(drain(e2))
    c = st.q("SELECT buyers, buy_sol, trades FROM nc_coin WHERE mint=?", (MINT,))[0]
    assert (c["buyers"], c["buy_sol"], c["trades"]) == (1, 1.0, 1)
    assert st.q("SELECT COUNT(*) AS n FROM nc_trade")[0]["n"] == 1


def test_trades_of_unknown_or_old_coins_are_not_tracked_and_window_closes(tmp_path):
    st, e = eng(tmp_path, now=T0 + 7300)
    e.put("x", 1, logs(trade(W[0], 1, 1, mint=W[6])), T0)
    e.put("c1", 100, logs(create(ts=T0)), T0 + 1)
    e.step(drain(e))
    assert e.stat["trades_other"] == 1
    assert st.q("SELECT closed FROM nc_coin WHERE mint=?", (MINT,))[0]["closed"] == 1 and MINT not in e.coins


def test_many_coins_at_once_in_one_batch(tmp_path):
    st, e = eng(tmp_path)
    mints = []
    for i in range(300):
        m = P.b58(bytes([i % 256, i // 256]) + bytes(30))
        mints.append(m)
        e.put(f"c{i}", 100 + i, logs(create(mint=m)), T0 + 1)
        e.put(f"b{i}", 200 + i, logs(trade(W[i % 7], 0.1, 10, mint=m)), T0 + 2)
    e.step(drain(e))
    assert st.q("SELECT COUNT(*) AS n FROM nc_coin")[0]["n"] == 300
    assert st.q("SELECT COUNT(*) AS n FROM nc_trade")[0]["n"] == 300 and e.stat["dropped"] == 0


def test_stream_feeds_the_engine_and_reconnects(tmp_path):
    st, e = eng(tmp_path)

    class WS:
        def __init__(self, msgs):
            self.msgs = msgs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def send(self, m):
            assert json.loads(m)["params"][0] == {"mentions": [P.PUMP]}

        async def recv(self):
            if not self.msgs:
                raise ConnectionError("closed")
            return self.msgs.pop(0)

    note = lambda sig, lg: json.dumps({"params": {"result": {"context": {"slot": 7}, "value": {"signature": sig, "err": None, "logs": lg}}}})   # noqa: E731
    conns = [WS([note("c1", logs(create()))]), WS([note("b1", logs(trade(W[0], 1, 1)))])]
    stop = threading.Event()
    stream = N.Stream(e, url="ws://test", connect=lambda: conns.pop(0) if conns else (_ for _ in ()).throw(OSError("down")))

    def stopper():
        while e.q.qsize() < 2:
            pass
        stop.set()
    threading.Thread(target=stopper, daemon=True).start()
    stream.run_forever(stop)
    e.step(drain(e))
    assert stream.stat["reconnects"] >= 1 and st.q("SELECT buyers FROM nc_coin WHERE mint=?", (MINT,))[0]["buyers"] == 1


def test_a_curve_without_sol_side_shows_no_sol_price(tmp_path):
    st, e = eng(tmp_path)
    e.put("c1", 100, logs(create()), T0 + 1)
    e.put("b1", 101, logs(trade(W[0], 0, 5000, vsol=0, vtok=0)), T0 + 11)
    e.step(drain(e))
    c = st.q("SELECT platform, price, mc_sol, buyers, buy_sol FROM nc_coin WHERE mint=?", (MINT,))[0]
    assert c["platform"] == "pump.fun (quote khác SOL)" and c["price"] is None and c["mc_sol"] is None
    assert c["buyers"] == 1 and c["buy_sol"] == 0                                     # the buy is real, its SOL is unknown


def test_a_non_standard_curve_gets_no_mc(tmp_path):
    st, e = eng(tmp_path)
    raw = base64.b64decode(create()[14:])
    odd = raw[:-8] + struct.pack("<Q", 85_000_000_000)          # virtual SOL 85 instead of 30
    e.put("c1", 100, logs("Program data: " + base64.b64encode(odd).decode(), trade(W[0], 1.0, 1000)), T0 + 1)
    e.step(drain(e))
    c = st.q("SELECT platform, mc_sol, buyers, buy_sol FROM nc_coin WHERE mint=?", (MINT,))[0]
    assert c["platform"] == "pump.fun (đường cong khác)" and c["mc_sol"] is None and c["buyers"] == 1 and c["buy_sol"] == 1.0
