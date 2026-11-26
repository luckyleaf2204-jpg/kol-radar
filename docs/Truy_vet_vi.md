# Truy vết ví (SOL / token hai chiều, nhiều tầng)

Trạng thái: chạy được ở môi trường development. **Mặc định TẮT** (`INSIDER_TRACE=1` mới bật). Chưa bật trên Render.

## 1. Tận dụng thành phần sẵn có (không tạo database hay module trùng lặp)

| Thành phần sẵn có | Dùng cho truy vết |
|---|---|
| `insiders_track.db` (`track.Store`, migration có backup) | Cùng file DB. Migration v8 chỉ **thêm** 2 bảng `trace_job`, `trace_node` |
| Bảng `edges` (đồ thị chuyển tiền của bước funding) | Mọi dòng tiền tìm được ghi vào đây, `kind='trace'`. Khóa UNIQUE (sig, src, dst, asset) nên không trùng với cạnh funding |
| `WalletHistory.ingest` + `watch.analyse` (decoder Gate B) | Ghi lệnh mua / bán / gửi / nhận của ví được truy vết vào `wallet_trades` (lịch sử giao dịch) |
| `wallet_info.busy` (nhận diện hub của tracker) | Hub và sàn được hiển thị nhưng không mở rộng |
| `rpc_usage`, `Budget` | Đếm lượt gọi (worker `trace`), giới hạn theo phút, theo lần truy vết và theo ngày |
| `esm_*`, `wallets`, `nc_coin`, `KNOWN_LABELS` | Đối chiếu: chủ thể / phân loại ESM, CREATOR / CREATOR_GROUP, DEV (New Coins), cohort DEPTH / STANK / INSIDERS, sàn |
| Trang `/early` (RADAR) | Thêm tab **Truy vết ví**. Trang chi tiết ví có nút "Truy vết ví này" |

Lý do có `trace_job` / `trace_node` mới: `fund_todo` chỉ đi ngược (nguồn vốn của người mua một coin) và gắn với mint. Nó không có ví gốc tùy ý, không đi chiều tiền ra, không có trạng thái theo từng lần truy vết.

## 2. Quan hệ được ghi (`insiders/trace.py: flows`)

- **SOL**: lệnh system `transfer` / `createAccount` / `transferWithSeed`, cả lệnh lồng bên trong. Số lamports chính xác, vào và ra.
- **Token**: chênh lệch số dư token theo chủ sở hữu giữa hai **ví**. Phía bên kia là pool / vault (không nằm trên đường cong) thì đó là swap, không tính là chuyển. Không tính WSOL, USDC, USDT.
- **FEE (trả phí hộ)**: ví trả phí giao dịch ký một giao dịch mà trong đó ví khác cũng ký hoặc có số dư token thay đổi. Đây là quan hệ `paid_by` của Early Smart Money (E007). Chỉ là quan hệ, không phải chuyển giá trị.
- Giao dịch lỗi bị bỏ qua. Không tính sàn và ví phí (nhãn đã biết).

## 3. Cách chạy

```
INSIDER_TRACE=1 python main.py --no-watch          # trang + worker truy vết (RPC công khai)
INSIDER_TRACE_RPC=<url> ...                        # RPC riêng; hoặc INSIDER_TRACE_HELIUS=1 để dùng HELIUS_API_KEY
python -m insiders.trace <db> <ví> [tầng] [both|in|out] [SOL tối thiểu] [giao dịch/ví]   # CLI development
```

- API (cùng mã truy cập):
  - `GET  /api/insiders/trace/status | jobs[?wallet=] | job?id=&focus=`
  - `POST /api/insiders/trace/start {wallet, hops 1–3, direction, min_sol, sigs 5–100}`
  - `POST /api/insiders/trace/stop?id=`
- Giới hạn:
  - `INSIDER_TRACE_CALLS_PER_MIN` (60);
  - `INSIDER_TRACE_JOB_CAP` (1.500 lượt mỗi lần truy vết; đạt trần thì trạng thái `capped`);
  - `INSIDER_TRACE_DAY_CAP` (20.000 lượt/ngày);
  - `INSIDER_TRACE_MAX_NODES` (150 ví mỗi lần).
- Chi phí mỗi ví mở rộng = 1 `getSignaturesForAddress` + N `getTransaction` (N = số giao dịch/ví). Ví ở tầng cuối chỉ được ghi nhận (`leaf`), không đọc thêm.

## 4. Kiểm thử bằng dữ liệu on-chain thật

- `tools/trace_fixtures.py` ghi lại nguyên văn các phản hồi RPC công khai vào `tests/fixtures/trace_rpc.json.gz`. Danh sách bộ dữ liệu nằm ở `tests/fixtures/trace_sets.json`.
- `tests/test_insiders_trace.py` chạy lại offline. Gọi RPC chưa được ghi thì test fail; không giả lập.
- Mỗi dòng tiền được kiểm lại độc lập trên giao dịch gốc: lamports của lệnh system, chênh lệch số dư token, phí và ví trả phí.
- Bộ dữ liệu:
  - **Bằng chứng ESM**: mọi `direct_transfer` và `paid_by` mà nghiên cứu ghi kèm chữ ký đều phải được decoder tìm lại đúng cặp ví;
  - **Case**: E007 (5 ví), `9Y2Es2V6…`, `HHLzvoz1…`, creator CHUCHU (`5uGbn3…`, CREATOR_GROUP E001). Mỗi case 2 tầng, hai chiều;
  - **Tier A**: chưa có danh sách 27 ví chính thức trong repo. Tạm dùng RADAR điểm ≥ 5 (26 ví) và 27 ví CREATOR_GROUP, mỗi ví 1 tầng. Khi có danh sách chính thức: thay trong `tools/trace_fixtures.py: sets()` rồi ghi lại.
- STACK: không có thành phần tên STACK trong repo. Đã đối chiếu với cohort / nhóm **STANK**.

## 5. Chưa làm (cần duyệt)

- Bật trên Render (`INSIDER_TRACE=1`) và chọn nguồn RPC (công khai hay Helius).
- Danh sách Tier A chính thức (27 ví).

## 6. Kết quả kiểm chứng (development, RPC công khai, 26/11/2026)

- Ghi lại 56 lần truy vết: 4 case, mỗi case 2 tầng, và 52 ví Tier A tạm, mỗi ví 1 tầng. Tổng 628 lượt gọi, 690 phản hồi RPC.
- Chạy lại offline: **344 dòng tiền** (142 SOL, 96 token, 106 trả phí hộ) và 204 ví. Mọi dòng tiền đều khớp giao dịch gốc khi kiểm độc lập.
- Toàn bộ test: 360 pass. Gồm 354 test cũ và test mới; đã cập nhật số phiên bản schema 7 → 8.
- Các ví khớp với dữ liệu hệ thống, kèm chữ ký giao dịch:
  - **E007**: `Em8J3g…` trả phí hộ cho lệnh của `A9Dam8…`, chữ ký `4nGzpeNK1V5V…`. Cả hai đều thuộc E007.
  - **HHLz**: nhận 1,833 SOL từ `F7p3dF…`. Ví này là E003 trong ESM và mang nhãn sàn "Relay Solver (bridge)", nên không mở rộng. Chữ ký `3tinBSYXMCqi…`.
  - **9Y2**: chuyển 1,4399 SOL tới `N9WqrX…` (chủ thể E002), chữ ký `24nXyzQmbYNN…`. Đường tới E009 đi qua một hub nên chỉ là tín hiệu yếu.
  - **DEV/Creator CHUCHU** (`5uGbn3…`):
    - creator chuyển 26.594.655 CHUCHU cho `2EpFa1…` (CREATOR_GROUP E001), chữ ký `22jzE8WcK9Nv…`;
    - ví `F5XZHy…` (E001) nhận CHUCHU từ 17 ví khác cùng E001: dòng gom token của nhóm phát hành.
- Kết quả 2 tầng từ ví creator CHUCHU chỉ đọc 10 giao dịch gần nhất của mỗi ví. Liên kết tới nhóm E001 nằm ở giao dịch cũ hơn, nên cần tăng "giao dịch/ví" khi truy vết thật.
