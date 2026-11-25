"""Gate B: backup / restore (B1), backfill with pagination and explicit gaps (B2), wallet history + PnL (B3),
INSIDERS as its own cohort (B4). Every test runs on fixtures and fake RPCs: any real network call fails the test."""
import json
import sqlite3
import socket
import urllib.request

import pytest

from insiders import backup as B
from insiders import track as T


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("network call in a Gate B test")
    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    try:
        import websockets
        monkeypatch.setattr(websockets, "connect", refuse)
    except ImportError:
        pass


# ------------------------------------------------------------------------------------------------------------ B1
def _state(tmp_path):
    (tmp_path / "insider_watch.json").write_text(json.dumps({"vip": {"children": {"A": {"parent": "B"}}}}), encoding="utf-8")
    st = T.Store(tmp_path / "insiders_track.db")
    st.x("INSERT INTO mints (mint, name, status) VALUES ('M1', 'ONE', 'live')")
    st.add_event({"sig": "S1", "wallet": "W1", "mint": "M1", "kind": "BUY", "ts": 1.0, "sol": 1.0, "tokens": 5})
    return st


def test_backup_manifest_sha256_and_rows(tmp_path):
    st = _state(tmp_path)
    d = B.backup(tmp_path, reason="test")             # the DB is open (WAL): the copy still has every row
    man = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    assert set(man["files"]) == {"insider_watch.json", "insiders_track.db"}
    for n, e in man["files"].items():
        assert e["sha256"] == B.sha256(d / n) and len(e["sha256"]) == 64
    assert man["files"]["insiders_track.db"]["rows"]["events"] == 1
    assert man["files"]["insiders_track.db"]["rows"]["mints"] == 1
    assert B.verify(d) == []
    st.db.close()


def test_restore_on_a_safe_copy_brings_data_back(tmp_path):
    live = tmp_path / "live"
    live.mkdir()
    st = _state(live)
    d = B.backup(live, reason="before change")
    st.x("DELETE FROM events")                         # the "bad change"
    st.db.close()
    (live / "insider_watch.json").write_text("{}", encoding="utf-8")
    safe = tmp_path / "safe"                           # restore into a separate copy, never over the live folder
    res = B.restore(d, safe)
    assert res["restored"] == ["insider_watch.json", "insiders_track.db"]
    assert json.loads((safe / "insider_watch.json").read_text(encoding="utf-8"))["vip"]["children"]["A"]["parent"] == "B"
    con = sqlite3.connect(safe / "insiders_track.db")
    assert con.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    con.close()
    for n in ("insider_watch.json", "insiders_track.db"):
        assert B.sha256(safe / n) == B.sha256(d / n)
    assert B.table_rows(safe / "insiders_track.db")["events"] == 1


def test_restore_refuses_a_tampered_backup(tmp_path):
    st = _state(tmp_path)
    st.db.close()
    d = B.backup(tmp_path)
    p = d / "insider_watch.json"
    p.write_text(p.read_text(encoding="utf-8").replace("B", "X"), encoding="utf-8")
    assert any("SHA-256" in x for x in B.verify(d))
    safe = tmp_path / "safe"
    with pytest.raises(ValueError):
        B.restore(d, safe)
    assert not (safe / "insider_watch.json").exists()


def test_restore_saves_the_current_state_first(tmp_path):
    st = _state(tmp_path)
    st.db.close()
    d = B.backup(tmp_path, now=lambda: 1000.0)
    (tmp_path / "insider_watch.json").write_text('{"newer": 1}', encoding="utf-8")
    res = B.restore(d, tmp_path, now=lambda: 2000.0)
    prev = tmp_path / "backups" / res["previous_saved_to"]
    assert json.loads((prev / "insider_watch.json").read_text(encoding="utf-8")) == {"newer": 1}
    assert json.loads((tmp_path / "insider_watch.json").read_text(encoding="utf-8"))["vip"]


def test_backup_keeps_n_and_nothing_when_empty(tmp_path):
    assert B.backup(tmp_path) is None
    (tmp_path / "insider_watch.json").write_text("{}", encoding="utf-8")
    for i in range(5):
        B.backup(tmp_path, keep=3, now=lambda i=i: 1000.0 + i)
    assert len(B.backups(tmp_path)) == 3


def test_migration_backup_holds_wal_rows(tmp_path):
    p = tmp_path / "insiders_track.db"
    st = T.Store(p)
    st.x("INSERT INTO mints (mint, name) VALUES ('M1', 'ONE')")
    st.db.execute("PRAGMA user_version=3")               # pretend an older schema: the next open migrates
    st.db.commit()
    st2 = T.Store(p)
    bak = tmp_path / "insiders_track.db.v3.bak"
    assert bak.exists() and B.table_rows(bak)["mints"] == 1
    assert st2.version() == len(T.MIGRATIONS)
    assert st2.q("SELECT name FROM mints")[0]["name"] == "ONE"
    st.db.close()
    st2.db.close()


def test_service_backs_up_before_loading_state(tmp_path, monkeypatch):
    from insiders import service as SV
    (tmp_path / "insider_watch.json").write_text("{}", encoding="utf-8")
    seen = {}

    class StopHere(Exception):
        pass

    def fake_watcher(*a, **k):
        seen["backups"] = B.backups(tmp_path)
        raise StopHere
    import insiders.watch as W
    monkeypatch.setattr(W, "Watcher", fake_watcher)
    svc = SV.InsiderService.__new__(SV.InsiderService)
    svc.result = {"vip": {}}
    with pytest.raises(StopHere):
        svc.start_watch(tmp_path / "insider_watch.json")
    assert seen["backups"] and seen["backups"][0]["reason"] == "start"
