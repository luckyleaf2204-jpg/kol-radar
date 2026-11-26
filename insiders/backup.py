"""Backups of the Insiders state before anything changes it (Gate B1). Local files only: no network, no RPC.

backup(state_dir)  copies insider_watch.json and insiders_track.db (SQLite online backup API: consistent even with
                   the database open in WAL mode) into state_dir/backups/<UTC time>/, with manifest.json holding the
                   SHA-256, size and (for databases) row count per table of every file. The newest KEEP are kept.
verify(dir)        the problems of a backup (missing file, SHA-256 or size mismatch); [] = intact.
restore(dir, to)   refuses a backup that fails verify; otherwise backs up the current state first ("pre-restore"),
                   then puts each file back atomically (temp file + rename) and drops stale -wal / -shm files."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import time
from pathlib import Path

FILES = ("insider_watch.json", "insiders_track.db")
KEEP = int(os.environ.get("INSIDER_BACKUP_KEEP") or 2)    # 2026-11-26: 2 (was 10) - each copy is the whole database
RESERVE_BYTES = int(float(os.environ.get("INSIDER_BACKUP_RESERVE_GB") or 0.5) * 1e9)   # left free for the live database
DIR = "backups"


def sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def table_rows(db_path: str | Path) -> dict[str, int]:
    con = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
    try:
        names = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        return {n: con.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0] for n in sorted(names)}
    finally:
        con.close()


def copy_db(src: str | Path, dst: str | Path) -> None:
    """A consistent copy of a live SQLite file (pages still in the WAL included)."""
    s = sqlite3.connect(str(src), timeout=30)
    d = sqlite3.connect(str(dst))
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()


def _is_db(name: str) -> bool:
    return name.endswith(".db")


def needed_bytes(state_dir: str | Path, present) -> int:
    """Room a backup needs: the size of the state files. The -wal file is NOT added: its pages fold into the copy
    (a WAL left large by a big delete is mostly free pages again)."""
    return sum((Path(state_dir) / n).stat().st_size for n in present)


def backup(state_dir: str | Path, files=FILES, keep: int = KEEP, reason: str = "", now=time.time) -> Path | None:
    """Back up the state files that exist; None when there is nothing to back up."""
    state_dir = Path(state_dir)
    present = [n for n in files if (state_dir / n).is_file()]
    if not present:
        return None
    root = state_dir / DIR
    if keep > 0:                                               # room first: the oldest copies beyond keep - 1 go (one stays);
        prune(root, max(1, keep - 1))                          # keep=0 (pre-restore) never prunes anything
    need = needed_bytes(state_dir, present)
    free = shutil.disk_usage(state_dir).free
    if free < need * 1.1 + RESERVE_BYTES:                      # a copy must never fill the disk the live database needs
        raise RuntimeError(f"không đủ dung lượng để sao lưu: cần ~{need / 1e9:.2f} GB + dự phòng {RESERVE_BYTES / 1e9:.1f} GB, "
                           f"còn trống {free / 1e9:.2f} GB")
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now()))
    out, k = root / stamp, 1
    while out.exists():
        k += 1
        out = root / f"{stamp}-{k}"
    out.mkdir(parents=True)
    man = {"created": now(), "reason": reason, "files": {}}
    try:
        for n in present:
            src, dst = state_dir / n, out / n
            if _is_db(n):
                copy_db(src, dst)
            else:
                shutil.copyfile(src, dst)
            entry = {"sha256": sha256(dst), "bytes": dst.stat().st_size}
            if _is_db(n):
                entry["rows"] = table_rows(dst)
            man["files"][n] = entry
        (out / "manifest.json").write_text(json.dumps(man, indent=1), encoding="utf-8")
    except BaseException:
        shutil.rmtree(out, ignore_errors=True)                 # a half-written copy is not a backup: free its space
        raise
    prune(root, keep)
    return out


def prune(root: str | Path, keep: int = KEEP) -> list[Path]:
    """Drop the oldest backups beyond `keep` (only folders holding a manifest: nothing else is touched)."""
    root = Path(root)
    if not root.is_dir() or keep <= 0:
        return []
    dirs = sorted((d for d in root.iterdir() if d.is_dir() and (d / "manifest.json").is_file()), key=lambda d: d.name)
    gone = dirs[:-keep] if len(dirs) > keep else []
    for d in gone:
        shutil.rmtree(d, ignore_errors=True)
    return gone


def backups(state_dir: str | Path) -> list[dict]:
    root = Path(state_dir) / DIR
    if not root.is_dir():
        return []
    out = []
    for d in sorted((d for d in root.iterdir() if (d / "manifest.json").is_file()), key=lambda d: d.name, reverse=True):
        try:
            man = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
        except ValueError:
            man = {}
        out.append({"dir": d.name, "created": man.get("created"), "reason": man.get("reason"),
                    "files": sorted((man.get("files") or {}).keys())})
    return out


def verify(backup_dir: str | Path) -> list[str]:
    d = Path(backup_dir)
    try:
        man = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ["manifest.json thiếu hoặc hỏng"]
    probs = []
    if not man.get("files"):
        probs.append("manifest không có file nào")
    for n, e in (man.get("files") or {}).items():
        p = d / n
        if not p.is_file():
            probs.append(f"{n}: thiếu file")
            continue
        if p.stat().st_size != e.get("bytes"):
            probs.append(f"{n}: kích thước khác manifest")
        if sha256(p) != e.get("sha256"):
            probs.append(f"{n}: SHA-256 không khớp")
        elif _is_db(n):
            try:
                con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
                try:
                    ok = con.execute("PRAGMA integrity_check").fetchone()[0]
                finally:
                    con.close()
                if ok != "ok":
                    probs.append(f"{n}: integrity_check: {ok}")
            except sqlite3.Error as x:
                probs.append(f"{n}: không mở được SQLite ({type(x).__name__})")
        elif n.endswith(".json"):
            try:
                json.loads(p.read_text(encoding="utf-8"))
            except ValueError:
                probs.append(f"{n}: JSON hỏng")
    return probs


def restore(backup_dir: str | Path, state_dir: str | Path, now=time.time) -> dict:
    """Put a verified backup back. The state being replaced is backed up first, so a restore can be undone."""
    d, state_dir = Path(backup_dir), Path(state_dir)
    probs = verify(d)
    if probs:
        raise ValueError("backup không hợp lệ: " + "; ".join(probs))
    man = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    # keep=0: no pruning here, so the backup being restored (maybe the oldest) can never be deleted by this copy
    before = backup(state_dir, files=tuple(man["files"]), keep=0, reason="pre-restore", now=now)
    state_dir.mkdir(parents=True, exist_ok=True)
    for n in man["files"]:
        tmp = state_dir / f".{n}.restoring"
        shutil.copyfile(d / n, tmp)
        if sha256(tmp) != man["files"][n]["sha256"]:
            tmp.unlink()
            raise ValueError(f"{n}: bản chép tạm sai SHA-256, không khôi phục")
        if _is_db(n):
            for side in ("-wal", "-shm"):
                (state_dir / f"{n}{side}").unlink(missing_ok=True)
        os.replace(tmp, state_dir / n)
    return {"restored": sorted(man["files"]), "from": d.name, "previous_saved_to": before.name if before else None}


if __name__ == "__main__":                                    # python -m insiders.backup list|verify|restore ...
    import sys
    cmd, args = (sys.argv[1:2] or ["list"])[0], sys.argv[2:]
    if cmd == "list" and len(args) == 1:
        print(json.dumps(backups(args[0]), indent=1))
    elif cmd == "verify" and len(args) == 1:
        p = verify(args[0])
        print("OK" if not p else "\n".join(p))
        sys.exit(1 if p else 0)
    elif cmd == "restore" and len(args) == 2:                 # run only with the service stopped
        print(json.dumps(restore(args[0], args[1]), indent=1))
    else:
        print("usage: python -m insiders.backup list <state_dir> | verify <backup_dir> | restore <backup_dir> <state_dir>")
        sys.exit(2)
