# Early Smart Money: migration v5, worker và giao diện (thiết kế, chưa triển khai production)

Trạng thái: **chỉ chạy local**. Chưa deploy, chưa bật worker trên server, chưa đổi ENV, không dùng thêm Helius nếu chưa được duyệt.

## 1. Quy tắc đã chốt (không đổi)

| Mục | Quy tắc |
|---|---|
| Cửa sổ | `t_create` (block time của giao dịch tạo mint) → `t_create + 7200 s` |
| Mốc | 0–15 / 15–60 / 60–120 phút |
| Vai trò | ACCUMULATE (≥ 2 lệnh mua, SOL ròng ≥ 1, không bán) · EARLY_SELL (bán ≥ 50% lượng mua trước phút 120) · HOLD (giữ ≥ 50% tại phút 120) · GOOD (realized + MTM tại giá phút 120 ≥ +50% SOL vào) |
| Cờ bot | ≥ 10 giao dịch và thời gian giữ trung vị < 60 s |
| Điểm | vào 0–15 = 2, 15–60 = 1; giá phút 120 / giá vốn ≥ 2x +1, ≥ 5x +2; GOOD +1; mỗi chủ thể tính 1 lần mỗi coin |
| Sau 2 giờ | bảng riêng `esm_outcome`, **không bao giờ** vào điểm |
| CREATOR_GROUP | chủ thể chứa creator (và ví cùng cụm): loại khỏi xếp hạng, giữ dữ liệu và bằng chứng |
| COORDINATED_PATTERN | nhãn hành vi (đồng bộ slot / cổng ký chung); **không** đổi phân loại chủ sở hữu |
| Thiếu dữ liệu | INSUFFICIENT_DATA (kể cả giao dịch định giá bằng USDC/USDT: không quy đổi sang SOL) |
| Mint | chỉ mint đã xác minh; ứng viên → `esm_pending_mint` (NEEDS_MINT_VERIFICATION) |

## 2. Migration v5 (đã viết, có test)

- File: `insiders/track.py`, `MIGRATIONS[4]`. **Chỉ thêm bảng** `esm_*`, không ALTER/DROP bảng cũ.
- Trước migration, `Store` tự sao lưu `<db>.v4.bak`. Migration là all-or-nothing; lỗi thì giữ schema v4 và toàn bộ dữ liệu.
- Các bảng:

| Bảng | Khóa | Nội dung |
|---|---|---|
| `esm_coin` | mint | ký hiệu, cách xác minh, t_create, creator, quote (SOL/USDC), giá p0/15/60/120, x, đỉnh, coverage, trạng thái, rules_version |
| `esm_window_wallet` | (mint, wallet) | mốc, phút mua đầu, lệnh mua/bán, SOL vào/ra, stable vào/ra, giá vốn, tăng sau khi vào, vai trò, kết quả, cờ bot, INSUFFICIENT lý do, điểm |
| `esm_link` | (a, b, signal) | tín hiệu liên kết + mức STRONG/MEDIUM + chữ ký bằng chứng |
| `esm_wallet_entity` | wallet | chủ thể, phân loại, `buyer` (0 = ví trong cụm nhưng không mua trong cửa sổ) |
| `esm_flag` | (wallet, flag, ref) | CREATOR_GROUP / COORDINATED_PATTERN + bằng chứng |
| `esm_outcome` | (mint, horizon) | giá 6 h / 24 h (tách riêng) |
| `esm_rank` | (rules_version, wallet) | điểm, coin vào sớm, phân loại, cờ |
| `esm_pending_mint` | symbol | MOONX / COGE: ứng viên + bằng chứng, chưa xác minh |

- Nạp dữ liệu nghiên cứu: `insiders/esm.py: import_research(store, "research/seven_coins")`, idempotent, một giao dịch DB. Chỉ ghi vào `esm_*`; bảng `wallets` (DEPTH/STANK/INSIDERS) chỉ được đọc để hiển thị tham chiếu.
- Test: `tests/test_insiders_esm.py`:
  - v4 → v5 giữ mọi dòng và cohort;
  - CREATOR_GROUP bị loại khỏi xếp hạng nhưng còn dữ liệu;
  - import idempotent;
  - outcome không đổi điểm;
  - mint chờ xác minh không vào `esm_coin`;
  - import lỗi thì không đổi gì.

## 3. Worker cửa sổ 120 phút (thiết kế, chưa viết; mặc định TẮT)

Bật bằng `INSIDER_ESM=1`. Không đặt biến này thì worker không chạy. Hiện chưa đặt ở đâu cả.

1. **Hàng đợi coin.**
   - Nguồn: coin đã xác minh (`mints.verified`) và mint mới do người dùng thêm.
   - Coin chưa xác minh → `esm_pending_mint`, không phân tích.
2. **Tìm `t_create`.**
   - Phân trang `getSignaturesForAddress(mint)` về giao dịch cũ nhất.
   - Có trần số trang; vượt trần → INSUFFICIENT_DATA "chưa tới giao dịch tạo". Không dùng getTransactionsForAddress.
   - Xác nhận giao dịch tạo bằng `initializeMint` hoặc pump `Create`.
3. **Cửa sổ.**
   - Chỉ lấy chữ ký có `blockTime ≤ t_create + 7200`.
   - Coin chưa đủ 120 phút → trạng thái RUNNING, chạy lại khi đủ. Không chấm sớm.
4. **Giải mã.**
   - `parse_mint_events` (đã có `maxSupportedTransactionVersion: 1` và gắn `quote` cho lệnh USDC/USDT).
   - Coverage ghi rõ: decoded / not_served / decode_errors / unread.
   - Pool định giá bằng stablecoin: quote = USDC, điểm SOL = INSUFFICIENT_DATA.
5. **Bảng ví.** Như `analyze_window.py`; ghi vào `esm_window_wallet`.
6. **Funding.**
   - hop 1 (10 giao dịch trước lần mua đầu, ≥ 0,05 SOL, ≤ 72 giờ); hop 2 cho ví cấp vốn không phải sàn và không phải busy hub.
   - Mỗi coin có ngân sách request riêng; hết ngân sách → INSUFFICIENT_DATA.
7. **Liên kết và phân loại.**
   - Như `entity.py` (STRONG/MEDIUM, mỗi chữ ký chỉ làm bằng chứng cho 1 tín hiệu, union-find).
   - Đếm riêng ví mua và ví không mua.
8. **Cờ.** CREATOR_GROUP (cụm chứa creator) và COORDINATED_PATTERN (như `coordination.py`).
9. **Xếp hạng.**
   - Tính lại `esm_rank` theo `rules_version`.
   - Đổi quy tắc → `rules_version` mới, bản cũ giữ nguyên.
10. **Sau 2 giờ.** Job riêng ghi `esm_outcome` ở +6 h / +24 h; không đụng điểm.

Ngân sách và an toàn:
- Dùng `_call` có đo đếm (bảng `rpc_usage`) và trần ngày/tháng như Gate B.
- Có hard-stop theo ledger.
- Checkpoint chỉ tiến sau khi đã xử lý xong.
- Lỗi/thiếu dữ liệu → mở `gaps`.
- Không ký, không gửi giao dịch; key chỉ ở server.

Ước lượng request mỗi coin (từ 5 coin đã chạy):

| Bước | Request |
|---|---|
| Giao dịch trong cửa sổ | 50–2.300 getTransaction |
| Funding | ~10 cho mỗi ví mua + hop 2 |

Ví dụ: CHUCHU hết khoảng 2.220 + ~10.000.

## 4. Giao diện Early Smart Money (thiết kế, chưa viết)

Trang mới `/insiders/early/`, chỉ đọc, dùng chung cổng truy cập hiện có. API dưới `/api/insiders/esm/*` gọi các hàm trong `insiders/esm.py`; không có key nào xuống frontend.

1. **Coin.**
   - Mỗi coin một thẻ: mint, cách xác minh, t_create, coverage, giá phút 15/60/120 và đỉnh, số ví theo mốc (SOL hoặc USDC).
   - Kết quả sau 2 giờ để riêng, có nhãn "không vào điểm".
2. **Xếp hạng.**
   - Bảng ví: coin vào sớm (phút mua đầu), điểm, SOL/lệnh, tăng sau khi vào, vai trò, chủ thể, phân loại, cohort cũ (tham chiếu).
   - Cờ: COORDINATED_PATTERN, bot?, INSUFFICIENT_DATA.
   - Lọc: ≥ 2 coin, theo mốc, ẩn cờ bot.
3. **Chủ thể.** Mỗi chủ thể tính 1 lần mỗi coin; xem được thành viên và tín hiệu (link Solscan theo chữ ký).
4. **CREATOR_GROUP.** Bảng riêng: creator, ví trong cụm, bằng chứng (ví dụ 16 SOL giữa creator SCINU → CHUCHU). Không có điểm.
5. **COORDINATED_PATTERN.** Từng nhóm: slot chung, phí giống nhau, router; ghi rõ "không phải kết luận cùng chủ".
6. **Mint chờ xác minh.** MOONX / COGE với các ứng viên và bằng chứng; chỉ người dùng xác nhận mint mới chuyển sang phân tích.

## 5. Những gì chưa làm (cần duyệt)

- Viết worker và trang (mục 3–4).
- Chạy migration v5 trên DB server và bật worker production.
- Dùng thêm Helius cho:
  - funding của ví mới phát sinh sau khi sửa lỗi USDC;
  - hop 2 của nhóm MACCA;
  - coin mới.
