"""Gate B: the ONE approved Helius probe (at most 50 HTTP requests, retries included). Read-only, paper only.

Default = dry run: prints the plan and the ledger state, touches no network. A real run needs, in order:
  python -m insiders.probe --init                                   (once: creates the ledger + its anchor)
  python -m insiders.probe --run --phase N --approval "<owner's approval note>"   (HELIUS_API_KEY set locally)
It is a separate, short process: it imports only pure decoding code (watch.analyse, track.parse_mint_events,
wallets.pair_pnl), never the stream, the poller, the tracker loop or the wallet worker, opens no WebSocket and starts
no thread. Nothing here signs or sends a transaction.

HARD STOP (fail closed everywhere):
  - ledger.jsonl is append-only and hash-chained (each line holds the hash of the one before). A slot is appended and
    fsynced BEFORE its request leaves; the transport never retries by itself (a retry is a new slot).
  - an anchor file OUTSIDE the output folder (~/.insiders_probe/anchor.json) holds the campaign id, the ledger's path,
    its slot count and head hash. A missing, truncated, edited, replaced or moved ledger no longer matches it: refused.
    There is no reset command: --init refuses while a ledger or an anchor exists.
  - CAP 50 in all; each phase has its own hard cap (retries included); what a phase leaves unused is never moved to
    another one. The caps are written into the genesis line: changing them in the code refuses the old ledger.
  - one probe at a time: an exclusive lock file next to the anchor (a leftover lock after a crash = refused until a
    person looks at it and removes it).
  - a ledger or anchor that cannot be read or parsed: refused (never treated as "0 used")."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

CAP = 50
# hard cap per phase, retries included; the sum stays under CAP and nothing moves between phases
PHASE_CAPS = {1: 8, 2: 3, 3: 8, 4: 2, 5: 13, 6: 14}
WALL_S = 15 * 60                         # one phase never runs longer than this
GAP_S = 1.0                              # >= 1 s between requests (the Free plan allows 10 / s)
TIMEOUT_S = 20
RETRY_PER_REQUEST = 2                    # extra attempts on 429 / 5xx / timeout, each one a counted request
OUT = Path("probe_out")
ANCHOR = Path.home() / ".insiders_probe" / "anchor.json"     # fixed on purpose: no option points it elsewhere
GENESIS_PREV = "0" * 64

MINT = "6nQCcuXypcAjncXK7gzvJcdwvqitgJRdG3k1no4Jpump"          # CHUCHU, verified mint
WALLET = "1XtfouHd6c57k3giXkh7FeCR4Gwq27oYprEPyayjCKE"          # first DEPTH wallet (sorted) of result.json
BUSY = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"            # pump.fun program: a new signature every second
NOT_A_MINT = WALLET
FAKE_SIG = "1" * 64                                              # valid base58 (64 zero bytes): never broadcast
LATE_WAITS = (0, 1, 2, 4)                                        # getTransaction tries after the signature is seen

# phase -> (what it measures, the planned first attempts per method)
PLAN = {
    1: ("phân trang until / before (chỉ getSignaturesForAddress)", {"getSignaturesForAddress": 6}),
    2: ("kiểm tra mint (chỉ getAccountInfo)", {"getAccountInfo": 2}),
    3: ("getTransaction: khớp dữ liệu + chữ ký không có bằng chứng tồn tại -> null", {"getTransaction": 6}),
    4: ("1 request lỗi JSON-RPC (có bị tính credit không?)", {"getTransaction": 1}),
    5: ("giao dịch vừa xác nhận: được phục vụ ngay hay sau null?", {"getSignaturesForAddress": 2, "getTransaction": 8}),
    6: ("mẫu lịch sử 1 ví DEPTH + giải mã PnL", {"getSignaturesForAddress": 1, "getTransaction": 10}),
}
PLANNED = sum(sum(m.values()) for _, m in PLAN.values())        # 36 first attempts
if sum(PHASE_CAPS.values()) > CAP or set(PHASE_CAPS) != set(PLAN) or any(
        sum(PLAN[p][1].values()) > c for p, c in PHASE_CAPS.items()):
    raise RuntimeError("probe budget is inconsistent: refuse to load")   # checked at import, before anything runs


class HardStop(Exception):
    """The request budget (all or the phase's) is spent: nothing more leaves this machine."""


class LedgerError(Exception):
    """The ledger / anchor / lock is not in a state that proves how many requests were sent: refuse to run."""


def redact(s: str) -> str:
    key = os.environ.get("HELIUS_API_KEY") or ""
    return s.replace(key, "***") if key else s


def _h(rec: dict) -> str:
    return hashlib.sha256(json.dumps(rec, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _fsync_write(path: Path, text: str, mode: str) -> None:
    with open(path, mode, encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())


def _caps_doc(cap: int, phase_caps: dict) -> dict:
    return {"cap": cap, "phase_caps": {str(k): v for k, v in sorted(phase_caps.items())}}


class ProcessLock:
    """Exclusive lock file (O_CREAT | O_EXCL): a second probe, here or in another process, is refused."""

    def __init__(self, path: Path):
        self.path, self.fd, self.held = Path(path), None, False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise LedgerError(f"đang có probe khác chạy, hoặc lần trước bị dừng đột ngột: kiểm tra rồi tự xoá {self.path}") from None
        os.write(self.fd, json.dumps({"pid": os.getpid(), "ts": time.time()}).encode())
        os.fsync(self.fd)
        self.held = True

    def release(self) -> None:
        if self.held:
            os.close(self.fd)
            self.path.unlink(missing_ok=True)
            self.held = False


class Ledger:
    """Append-only, hash-chained record of every HTTP attempt, tied to an anchor kept outside the output folder."""

    def __init__(self, path: Path, anchor: Path = None, cap: int = CAP, phase_caps: dict = None, lock: ProcessLock = None):
        self.path, self.anchor = Path(path), Path(anchor or ANCHOR)
        self.cap, self.phase_caps = cap, dict(phase_caps or PHASE_CAPS)
        self.lock = lock
        self.rows: list[dict] = []
        self.before_append = lambda rec: None                   # test hooks: a crash at each point
        self.after_append = lambda rec: None

    # ---- create (once)
    def init(self) -> dict:
        if self.anchor.exists() or self.path.exists():
            raise LedgerError("đã có ledger hoặc anchor: không khởi tạo lại (không có lệnh reset)")
        self._need_lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        g = {"i": 0, "kind": "genesis", "campaign": uuid.uuid4().hex, **_caps_doc(self.cap, self.phase_caps),
             "ledger": str(self.path.resolve()), "ts": time.time(), "prev": GENESIS_PREV}
        g["hash"] = _h(g)
        _fsync_write(self.path, json.dumps(g, ensure_ascii=False) + "\n", "x")   # "x": never over an existing file
        self._write_anchor(g["campaign"], 0, g["hash"])
        self.rows = [g]
        return g

    # ---- verify (every open and every take)
    def _read(self) -> list[dict]:
        if not self.anchor.exists():
            raise LedgerError("thiếu anchor: không chứng minh được số request đã gửi" +
                              (" (ledger vẫn còn: anchor bị xoá?)" if self.path.exists() else " (chưa --init?)"))
        try:
            anc = json.loads(self.anchor.read_text(encoding="utf-8"))
            camp, n_anc, head = anc["campaign"], int(anc["n"]), anc["head"]
        except (OSError, ValueError, KeyError, TypeError):
            raise LedgerError("anchor hỏng hoặc không đọc được") from None
        if anc.get("ledger") != str(self.path.resolve()):
            raise LedgerError("anchor trỏ tới ledger khác (đổi thư mục output?)")
        try:
            lines = self.path.read_text(encoding="utf-8").split("\n")
        except FileNotFoundError:
            raise LedgerError("ledger bị mất trong khi anchor còn: nghi bị reset") from None
        except OSError:
            raise LedgerError("không đọc được ledger") from None
        if lines and lines[-1] == "":
            lines.pop()
        rows, prev = [], GENESIS_PREV
        for i, line in enumerate(lines):
            try:
                r = json.loads(line)
            except ValueError:
                raise LedgerError(f"ledger dòng {i} hỏng (ghi dở?)") from None
            body = {k: v for k, v in r.items() if k != "hash"}
            if r.get("i") != i or r.get("prev") != prev or _h(body) != r.get("hash"):
                raise LedgerError(f"chuỗi băm của ledger sai ở dòng {i}: đã bị sửa")
            prev = r["hash"]
            rows.append(r)
        if not rows or rows[0].get("kind") != "genesis" or rows[0].get("campaign") != camp:
            raise LedgerError("ledger không thuộc chiến dịch của anchor (bị thay?)")
        if {k: rows[0].get(k) for k in ("cap", "phase_caps")} != _caps_doc(self.cap, self.phase_caps):
            raise LedgerError("trần trong code khác trần lúc --init: không chạy")
        n = len(rows) - 1
        if n < n_anc or rows[n_anc]["hash"] != head:
            raise LedgerError(f"ledger ngắn hơn / khác anchor ({n} < {n_anc}): đã bị cắt hoặc thay")
        if n > n_anc + 1:
            raise LedgerError("ledger vượt anchor quá 1 dòng: không nhất quán")
        if n == n_anc + 1 and self.lock and self.lock.held:     # crash between the append and the anchor update:
            self._write_anchor(camp, n, rows[-1]["hash"])        # the slot counts (anchor only moves forward)
        return rows

    def open(self) -> "Ledger":
        self.rows = self._read()
        return self

    def used(self, phase: int | None = None) -> int:
        return sum(1 for r in self.rows[1:] if phase is None or r.get("phase") == phase)

    def left(self, phase: int | None = None) -> int:
        tot = self.cap - self.used()
        return max(0, tot if phase is None else min(tot, self.phase_caps.get(phase, 0) - self.used(phase)))

    def take(self, phase: int, method: str, step: str) -> int:
        """One slot, written and fsynced before the request may leave. Fails closed on any doubt."""
        self._need_lock()
        self.rows = self._read()
        if self.used() >= self.cap:
            raise HardStop(f"đã dùng {self.used()}/{self.cap} request: dừng tuyệt đối")
        if phase not in self.phase_caps or self.used(phase) >= self.phase_caps[phase]:
            raise HardStop(f"phase {phase} đã dùng hết hạn mức {self.phase_caps.get(phase, 0)} request")
        n = len(self.rows)
        rec = {"i": n, "kind": "slot", "phase": phase, "method": method, "step": step, "ts": time.time(),
               "prev": self.rows[-1]["hash"]}
        rec["hash"] = _h(rec)
        self.before_append(rec)
        _fsync_write(self.path, json.dumps(rec, ensure_ascii=False) + "\n", "a")
        self.after_append(rec)
        self._write_anchor(self.rows[0]["campaign"], n, rec["hash"])
        self.rows.append(rec)
        return n

    def _need_lock(self) -> None:
        if not (self.lock and self.lock.held):
            raise LedgerError("chưa giữ khoá tiến trình")

    def _write_anchor(self, campaign: str, n: int, head: str) -> None:
        self.anchor.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.anchor.with_suffix(".tmp")
        _fsync_write(tmp, json.dumps({"campaign": campaign, "ledger": str(self.path.resolve()), "n": n, "head": head}), "w")
        os.replace(tmp, self.anchor)


def classify(status, error, js) -> str:
    """What one HTTP attempt told us. Only FOUND / NULL are answers about the transaction; the rest say nothing."""
    if status is None:
        return "TIMEOUT" if error and ("timeout" in error.lower() or "timed out" in error.lower()) else "NETWORK_ERROR"
    if status != 200:
        return f"HTTP_{status}"
    if not isinstance(js, dict):
        return "INVALID_BODY"
    if js.get("error") is not None:
        return "RPC_ERROR"
    return "NULL" if js.get("result") is None else "FOUND"


def late_verdict(attempts: list[dict]) -> dict:
    """Verdict on a signature KNOWN to exist (listed by getSignaturesForAddress). attempts: [{"class", "t"}], t =
    seconds since the listing came back (local clock). Never a network latency: only what this client observed."""
    info = [a for a in attempts if a["class"] in ("FOUND", "NULL")]
    found = next((a for a in info if a["class"] == "FOUND"), None)
    if found is not None:
        nulls = [a for a in info if a["class"] == "NULL" and a["t"] <= found["t"]]
        if not nulls:
            return {"verdict": "SERVED_FIRST_ANSWER", "note": "câu trả lời có thông tin đầu tiên đã có giao dịch"}
        return {"verdict": "SERVED_AFTER_NULL", "null_until_s": nulls[-1]["t"], "found_at_s": found["t"],
                "note": "trả null rồi mới có: thời điểm sẵn sàng nằm trong (null_until_s, found_at_s] theo đồng hồ máy"}
    if info:
        return {"verdict": "NOT_SERVED_IN_WINDOW", "last_null_s": info[-1]["t"],
                "note": "chỉ null trong cửa sổ thử; chữ ký có thật (đã được liệt kê) nên KHÔNG kết luận 'không tồn tại'"}
    return {"verdict": "UNDETERMINED", "note": "chỉ có timeout / lỗi: không có thông tin về giao dịch"}


class Transport:
    """One HTTP POST per call, no retry inside. Every attempt: a ledger slot first, then one log line."""

    def __init__(self, url: str, ledger: Ledger, log_path: Path, opener=None, sleep=time.sleep, clock=time.monotonic):
        self.url, self.ledger, self.log_path = url, ledger, Path(log_path)
        self.opener = opener or (lambda req: urllib.request.urlopen(req, timeout=TIMEOUT_S))
        self.sleep, self.clock = sleep, clock
        self.t0, self.last = clock(), 0.0
        self.stopped = False

    def post(self, method: str, params: list, phase: int, step: str) -> dict:
        if self.stopped:
            raise HardStop("đã dừng")
        if self.clock() - self.t0 > WALL_S:
            self.stopped = True
            raise HardStop("quá thời gian tối đa của phase")
        wait = GAP_S - (self.clock() - self.last)
        if wait > 0:
            self.sleep(wait)
        try:
            n = self.ledger.take(phase, method, step)           # recorded before anything is sent
        except (HardStop, LedgerError):
            self.stopped = True
            raise
        body = json.dumps({"jsonrpc": "2.0", "id": n, "method": method, "params": params}).encode()
        rec = {"n": n, "phase": phase, "step": step, "method": method,
               "params_sha": hashlib.sha256(json.dumps(params).encode()).hexdigest()[:12], "sent_at": time.time()}
        t = self.clock()
        self.last = t
        status, raw, headers, err = None, b"", {}, None
        try:
            resp = self.opener(urllib.request.Request(self.url, data=body, headers={"Content-Type": "application/json"}))
            raw = resp.read()
            status = getattr(resp, "status", 200)
            headers = dict(getattr(resp, "headers", {}) or {})
        except urllib.error.HTTPError as e:
            status, headers = e.code, dict(e.headers or {})
            try:
                raw = e.read() or b""
            except Exception:
                raw = b""
        except Exception as e:                                  # timeout / connection: the slot stays counted
            err = redact(f"{type(e).__name__}: {str(e)[:120]}")
        js = None
        if raw:
            try:
                js = json.loads(raw)
            except ValueError:
                js = None
        cls = classify(status, err, js)
        rec.update(status=status, bytes=len(raw), ms=round((self.clock() - t) * 1000, 1), error=err, outcome=cls,
                   headers={k: v for k, v in headers.items() if k.lower().startswith(("x-ratelimit", "retry-after", "x-credit", "x-request"))})
        if cls == "RPC_ERROR":
            rec["rpc_error"] = redact(str(js["error"])[:200])
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return {"status": status, "error": err, "headers": headers, "json": js, "class": cls}


class Runner:
    """The test steps of one phase. call() = one request + retries, every attempt a slot of THIS phase."""

    def __init__(self, transport: Transport, phase: int, out_dir: Path):
        self.t, self.phase, self.out = transport, phase, Path(out_dir)
        self.checks: list[dict] = []
        self.raw: dict[str, object] = {}

    def call(self, method: str, params: list, step: str, retries: int = RETRY_PER_REQUEST):
        r = None
        for k in range(retries + 1):
            r = self.t.post(method, params, self.phase, step if k == 0 else f"{step} (retry {k})")
            retryable = r["class"] in ("TIMEOUT", "NETWORK_ERROR", "HTTP_429", "HTTP_500", "HTTP_502", "HTTP_503",
                                       "HTTP_504") or (r["class"] == "RPC_ERROR" and isinstance(r["json"]["error"], dict)
                                                        and r["json"]["error"].get("code") in (-32005, -32429, 429))
            if not retryable:
                if r["class"] == "INVALID_BODY" or not isinstance(r["json"], dict):
                    return {"_failed": True, "status": r["status"], "error": "no JSON body"}   # never taken for null
                return r["json"].get("result") if r["class"] != "RPC_ERROR" else r["json"]
            if k < retries:
                try:
                    wait = float((r["headers"] or {}).get("Retry-After") or 2 * 2 ** k)
                except (TypeError, ValueError):
                    wait = 2 * 2 ** k
                self.t.sleep(min(wait, 10))
        return {"_failed": True, "status": r["status"], "error": r["error"], "class": r["class"]}

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.checks.append({"phase": self.phase, "check": name, "result": "PASS" if ok else "FAIL", "detail": detail})
        return ok

    def skip(self, name: str, why: str) -> None:
        self.checks.append({"phase": self.phase, "check": name, "result": "NOT_RUN", "detail": why})

    # --------------------------------------------------------------- phases
    def phase1(self):
        sig = lambda xs: [x["signature"] for x in xs]               # noqa: E731
        a1 = self.call("getSignaturesForAddress", [MINT, {"limit": 1000}], "A1 trang đầu")
        if not self.check("A1 trả danh sách", isinstance(a1, list) and len(a1) > 0, f"{len(a1) if isinstance(a1, list) else a1}"):
            return
        self.raw["A1"] = a1
        bt = [x.get("blockTime") or 0 for x in a1]
        self.check("A1 mới -> cũ", all(bt[i] >= bt[i + 1] for i in range(len(bt) - 1)))
        a2 = self.call("getSignaturesForAddress", [MINT, {"limit": 1000, "before": a1[-1]["signature"]}], "A2 trang sau (before)")
        if isinstance(a2, list):
            self.check("A2 không trùng A1", not set(sig(a1)) & set(sig(a2)))
            self.check("A2 nối tiếp A1 theo thời gian", not a2 or (a2[0].get("blockTime") or 0) <= bt[-1])
            if len(a1) < 1000:
                self.check("A1 < 1000 thì A2 rỗng (đã hết lịch sử)", a2 == [])
        if len(a1) >= 10:
            k = min(500, len(a1) - 1)
            a3 = self.call("getSignaturesForAddress", [MINT, {"limit": 1000, "until": a1[k]["signature"]}], "A3 until")
            if isinstance(a3, list):
                s3, s1 = sig(a3), sig(a1)
                # new transactions may have arrived on top since A1: A3 = new ones + A1[:k], exactly
                tail = s3[len(s3) - k:] if len(s3) >= k else s3
                self.check("A3 until dừng đúng trước checkpoint (không gồm nó)", s1[k] not in s3 and tail == s1[:k],
                           f"{len(s3)} chữ ký, {len(s3) - k} mới phát sinh")
            i, j = min(100, len(a1) - 2), min(300, len(a1) - 1)
            a4 = self.call("getSignaturesForAddress", [MINT, {"limit": 1000, "before": a1[i]["signature"],
                                                              "until": a1[j]["signature"]}], "A4 before + until")
            if isinstance(a4, list):
                self.check("A4 before+until = đúng đoạn giữa", sig(a4) == sig(a1)[i + 1:j], f"{len(a4)} chữ ký")
            p1 = self.call("getSignaturesForAddress", [MINT, {"limit": 10, "before": a1[0]["signature"]}], "A5 limit 10")
            if isinstance(p1, list) and p1:
                p2 = self.call("getSignaturesForAddress", [MINT, {"limit": 10, "before": p1[-1]["signature"]}], "A5 trang 2")
                if isinstance(p2, list):
                    self.check("A5 phân trang nhỏ = cắt lát của A1", sig(p1) + sig(p2) == sig(a1)[1:1 + len(p1) + len(p2)])
        else:
            self.skip("A3-A5", "mint có quá ít giao dịch")

    def phase2(self):
        r = self.call("getAccountInfo", [MINT, {"encoding": "jsonParsed"}], "E1 mint thật")
        v = (r or {}).get("value") if isinstance(r, dict) else None
        p = ((v or {}).get("data") or {}).get("parsed") or {} if isinstance((v or {}).get("data"), dict) else {}
        self.check("E1 owner là token program, type=mint", bool(v) and p.get("type") == "mint" and v.get("owner") in (
            "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"), str(p.get("info", {}).get("decimals")))
        r = self.call("getAccountInfo", [NOT_A_MINT, {"encoding": "jsonParsed"}], "E2 ví (không phải mint)")
        v = (r or {}).get("value") if isinstance(r, dict) else None
        pp = (v or {}).get("data") if isinstance((v or {}).get("data"), dict) else {}
        self.check("E2 bị nhận là không phải mint", (pp.get("parsed") or {}).get("type") != "mint")

    def phase3(self, a1: list | None):
        from insiders.track import parse_mint_events
        if not a1:
            return self.skip("B1-B5", "cần A1 của phase 1 (probe_out/raw_phase1.json)")
        pick = [a1[i] for i in sorted({0, len(a1) // 4, len(a1) // 2, 3 * len(a1) // 4, len(a1) - 1})][:5]
        for x in pick:
            tx = self.call("getTransaction", [x["signature"], {"encoding": "jsonParsed", "commitment": "confirmed",
                                                                "maxSupportedTransactionVersion": 0}], "B getTransaction")
            ok = isinstance(tx, dict) and tx.get("blockTime") == x.get("blockTime") and tx.get("slot") == x.get("slot")
            self.check(f"B {x['signature'][:8]} blockTime/slot khớp getSignaturesForAddress", ok)
            if isinstance(tx, dict):
                self.raw[x["signature"]] = tx
                try:
                    evs = parse_mint_events(tx, MINT)
                    self.check(f"B {x['signature'][:8]} giải mã không lỗi", True, ",".join(sorted({e['kind'] for e in evs})) or "không có event")
                except Exception as e:
                    self.check(f"B {x['signature'][:8]} giải mã không lỗi", False, type(e).__name__)
        r = self.call("getTransaction", [FAKE_SIG, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}], "D2 chữ ký không có bằng chứng tồn tại")
        # null here means "no transaction served"; getTransaction alone cannot tell "never existed" from "not indexed"
        self.check("D2 trả null (không phân biệt được 'không tồn tại' với 'chưa index' chỉ bằng getTransaction)", r is None, str(r)[:80])

    def phase4(self):
        r = self.call("getTransaction", ["không-phải-chữ-ký", {"encoding": "jsonParsed"}], "D1 chữ ký sai định dạng", retries=1)
        self.check("D1 lỗi JSON-RPC (không phải null, không phải giao dịch)", isinstance(r, dict) and "error" in r, str(r)[:120])

    def phase5(self):
        """A signature just listed (commitment confirmed) EXISTS; each getTransaction try (no hidden retry) is
        classified. Reported as observations of this client on its own clock, not as a measured network latency."""
        for k in range(2):
            s = self.call("getSignaturesForAddress", [BUSY, {"limit": 5, "commitment": "confirmed"}], f"C{k + 1} chữ ký mới nhất", retries=0)
            t_seen = time.monotonic()
            cand = [x for x in s if x.get("err") is None] if isinstance(s, list) else []
            if not cand:
                self.skip(f"C{k + 1}", "không lấy được chữ ký mới thành công")
                continue
            x, attempts = cand[0], []
            for i, wait in enumerate(LATE_WAITS):
                if wait:
                    self.t.sleep(wait)
                r = self.t.post("getTransaction", [x["signature"], {"encoding": "jsonParsed", "commitment": "confirmed",
                                                                     "maxSupportedTransactionVersion": 0}], self.phase, f"C{k + 1} thử #{i + 1}")
                attempts.append({"class": r["class"], "t": round(time.monotonic() - t_seen, 2)})
                if r["class"] == "FOUND":
                    tx = r["json"]["result"]
                    attempts[-1]["slot_match"] = tx.get("slot") == x.get("slot")
                    break
            v = late_verdict(attempts)
            self.raw[f"C{k + 1}"] = {"signature": x["signature"], "slot": x.get("slot"),
                                     "confirmationStatus": x.get("confirmationStatus"), "attempts": attempts, **v,
                                     "blocktime_lag_unverified_s": round(time.time() - x["blockTime"], 1) if x.get("blockTime") else None}
            self.checks.append({"phase": self.phase, "check": f"C{k + 1} quan sát giao dịch vừa xác nhận",
                                "result": "OBSERVED" if v["verdict"] != "UNDETERMINED" else "FAIL",
                                "detail": f"{v['verdict']}: {v['note']}"})
            if any(a.get("slot_match") is False for a in attempts):
                self.check(f"C{k + 1} slot khớp", False)

    def phase6(self):
        from insiders.watch import analyse
        from insiders.wallets import pair_pnl
        s = self.call("getSignaturesForAddress", [WALLET, {"limit": 1000}], "F1 lịch sử ví")
        if not (isinstance(s, list) and s):
            return self.skip("F", "ví không có chữ ký")
        ok = [x for x in s if x.get("err") is None]
        pick = [ok[int(i * (len(ok) - 1) / 9)] for i in range(10)] if len(ok) >= 10 else ok
        trades, kinds = {}, {}
        for x in pick:
            tx = self.call("getTransaction", [x["signature"], {"encoding": "jsonParsed", "commitment": "confirmed",
                                                                "maxSupportedTransactionVersion": 0}], "F getTransaction", retries=0)
            if not isinstance(tx, dict) or "error" in tx or tx.get("_failed"):
                continue
            self.raw[x["signature"]] = tx
            for e in analyse(tx, WALLET):
                kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
                if e.get("mint"):
                    trades.setdefault(e["mint"], []).append(e)
        self.check("F giải mã mẫu không lỗi", True, json.dumps(kinds))
        res = {m: pair_pnl(t) for m, t in trades.items()}
        self.raw["F_pnl"] = res
        self.check("F mẫu 10 giao dịch: mọi cặp phải INSUFFICIENT_DATA hoặc có lý do (lịch sử không đầy đủ)",
                   True, f"{len(res)} cặp; đối chiếu tay với explorer theo probe_out/raw_phase6.json")

    def save(self):
        (self.out / f"raw_phase{self.phase}.json").write_text(json.dumps(self.raw), encoding="utf-8")
        (self.out / f"checks_phase{self.phase}.json").write_text(json.dumps(self.checks, ensure_ascii=False, indent=1), encoding="utf-8")


def summary(log_path: Path) -> dict:
    """Per phase and method: attempts (every HTTP request sent, retries included), outcomes, bytes."""
    out: dict[str, dict] = {}
    if not Path(log_path).exists():
        return out
    for line in Path(log_path).read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        m = out.setdefault(f"phase{r['phase']}:{r['method']}", {"attempts": 0, "bytes": 0, "outcomes": {}})
        m["attempts"] += 1
        m["bytes"] += r.get("bytes") or 0
        m["outcomes"][r.get("outcome") or "?"] = m["outcomes"].get(r.get("outcome") or "?", 0) + 1
    return out


def credit_report(log_path: Path, readings: dict) -> dict:
    """Helius credits from dashboard readings typed in by the owner (nothing here reads Helius).
    readings = {"baseline": [n, n, ...] (credits used, read >= 5 min apart before phase 1),
                "phases": {"1": {"before": n, "after": [n, n, ...]}, ...}}   (after: read until two equal readings)
    A per-request figure is given only for a phase with ONE method and a flat baseline; otherwise only the phase's
    total delta. Request counts and bytes come from the probe's own log, never from the credits."""
    s = summary(log_path)
    base = readings.get("baseline") or []
    isolated = len(base) >= 2 and len(set(base)) == 1
    out = {"isolated": isolated, "phases": {},
           "note": "đơn giá chỉ hợp lệ khi nền đứng yên (không dịch vụ nào khác tiêu credit) và phase chỉ có 1 method"}
    for p, rd in sorted((readings.get("phases") or {}).items(), key=lambda kv: int(kv[0])):
        rows = {k.split(":", 1)[1]: v for k, v in s.items() if k.startswith(f"phase{p}:")}
        after = rd.get("after") or []
        settled = len(after) >= 2 and after[-1] == after[-2]
        r = {"attempts": {m: v["attempts"] for m, v in rows.items()}, "bytes": {m: v["bytes"] for m, v in rows.items()},
             "outcomes": {m: v["outcomes"] for m, v in rows.items()}, "settled": settled}
        if not settled:
            r["result"] = "CHƯA ỔN ĐỊNH: đọc thêm dashboard tới khi 2 lần liên tiếp bằng nhau"
        elif not isolated:
            r.update(delta_total=after[-1] - rd["before"], result="KHÔNG CÔ LẬP: chỉ là tổng delta, có thể lẫn tiêu thụ khác")
        else:
            d = after[-1] - rd["before"]
            r["delta_total"] = d
            if len(rows) == 1:
                (m, v), = rows.items()
                ok = v["outcomes"].get("FOUND", 0) + v["outcomes"].get("NULL", 0)
                r.update(result="ĐƠN GIÁ THEO METHOD", method=m, credits_per_attempt=round(d / v["attempts"], 3) if v["attempts"] else None,
                         credits_per_answered=round(d / ok, 3) if ok else None)
            else:
                r["result"] = "CHỈ TỔNG DELTA CỦA PHASE (nhiều method: không tách)"
        out["phases"][p] = r
    return out


def plan_text() -> str:
    lines = [f"Helius probe: trần {CAP} request (gồm retry); hạn mức cứng từng phase, tổng {sum(PHASE_CAPS.values())}; "
             f"kế hoạch {PLANNED} lần đầu."]
    for p, (what, ms) in PLAN.items():
        lines.append(f"  phase {p} (tối đa {PHASE_CAPS[p]}): {what}: " + ", ".join(f"{m} x{n}" for m, n in ms.items()))
    lines.append("Dry run: không gọi mạng. Chạy thật: --init (1 lần), rồi --run --phase N --approval \"...\" với HELIUS_API_KEY.")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--init", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--phase", type=int, choices=sorted(PLAN))
    ap.add_argument("--approval", default="")
    ap.add_argument("--credit-report", metavar="READINGS_JSON")
    ap.add_argument("--out", default=str(OUT))
    a = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")              # Vietnamese text on a Windows console
    except (AttributeError, ValueError):
        pass
    out = Path(a.out)
    print(plan_text())
    if a.credit_report:
        print(json.dumps(credit_report(out / "requests.jsonl", json.loads(Path(a.credit_report).read_text(encoding="utf-8"))),
                         ensure_ascii=False, indent=1))
        return 0
    if not (a.run or a.init):
        try:                                                  # read-only look at the ledger
            lg = Ledger(out / "ledger.jsonl").open()
            print(f"Đã dùng {lg.used()}/{CAP}; theo phase: " + ", ".join(f"{p}: {lg.used(p)}/{c}" for p, c in PHASE_CAPS.items()))
        except LedgerError as e:
            print(f"Ledger: {e}")
        return 0
    lock = ProcessLock(ANCHOR.parent / "probe.lock")
    try:
        lock.acquire()
    except LedgerError as e:
        print(f"Từ chối: {e}")
        return 3
    try:
        ledger = Ledger(out / "ledger.jsonl", lock=lock)
        if a.init:
            try:
                g = ledger.init()
            except LedgerError as e:
                print(f"Từ chối: {e}")
                return 3
            print(f"Đã tạo ledger, chiến dịch {g['campaign']}.")
            return 0
        key = os.environ.get("HELIUS_API_KEY")
        if not (a.phase and a.approval.strip() and key):
            print("Thiếu --phase, --approval hoặc HELIUS_API_KEY: không chạy.")
            return 2
        try:
            ledger.open()
        except LedgerError as e:
            print(f"Từ chối: {e}")
            return 3
        if ledger.left(a.phase) <= 0:
            print(f"Phase {a.phase} hết hạn mức: không chạy.")
            return 4
        with open(out / "approvals.log", "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), "phase": a.phase, "approval": a.approval}, ensure_ascii=False) + "\n")
        t = Transport(f"https://mainnet.helius-rpc.com/?api-key={key}", ledger, out / "requests.jsonl")
        r = Runner(t, a.phase, out)
        try:
            if a.phase == 3:
                p1 = out / "raw_phase1.json"
                r.phase3(json.loads(p1.read_text(encoding="utf-8")).get("A1") if p1.exists() else None)
            else:
                getattr(r, f"phase{a.phase}")()
        except (HardStop, LedgerError) as e:
            r.skip("HARD STOP", str(e))
        finally:
            r.save()
        print(json.dumps(summary(out / "requests.jsonl"), ensure_ascii=False, indent=1))
        print(json.dumps(r.checks, ensure_ascii=False, indent=1))
        print(f"Đã dùng {ledger.used()}/{CAP}; phase {a.phase}: {ledger.used(a.phase)}/{PHASE_CAPS[a.phase]}.")
        return 0
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
