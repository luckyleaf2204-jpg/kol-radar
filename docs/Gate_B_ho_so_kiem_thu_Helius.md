# Cổng B: hồ sơ xin phê duyệt kiểm thử Helius thật (≤ 50 request)

Trạng thái: **CHƯA CHẠY.** Tài liệu này chỉ để chủ dự án xem xét. Chưa có request Helius nào được gửi, và chưa tạo ledger hay anchor.
Branch `insiders-site` @ `35b5a29`, chưa commit. Server PAUSED. Toàn bộ test local: **297 PASS, 0 FAIL.**

---

## 1. Đính chính "13 lỗi" và danh sách đánh số tới 14

Con số 13 trong báo cáo trước là **đếm sai**: tóm tắt được viết trước khi danh sách chốt. Danh sách thật có 14 mục:

| # | Nội dung | Loại |
|---|---|---|
| 1 | Checkpoint / con trỏ gap / poll_last dời trước khi xử lý xong (có cả trong poll cũ) | Lỗi (mất dữ liệu khi crash) |
| 2 | Race WS ↔ quét bù trên cùng chữ ký | Lỗi (xử lý trùng) |
| 3 | Lỗi giải mã làm mất giao dịch (vip) | Lỗi |
| 4 | RPC trả dữ liệu lạ làm vỡ vòng quét bù | Lỗi |
| 5 | Quét bù định kỳ 15 phút → 1 giờ | **Không phải lỗi**: chỉnh chi phí |
| 6 | Một giao dịch lỗi chặn cả hàng đợi (track) | Lỗi |
| 7 | Lỗi giải mã sau khi xoá khỏi hàng đợi (track) | Lỗi |
| 8 | Lỗi ở trang 2 bỏ phí trang 1 | Lỗi (tốn request, không mất dữ liệu) |
| 9 | Bộ đếm lỗi không reset, coin tự dừng | Lỗi |
| 10 | Thêm `gaps_open` cho mỗi coin | **Không phải lỗi**: bổ sung hiển thị |
| 11 | Phí bị trừ hai lần | Lỗi PnL |
| 12 | (a) bán khi đang giữ token nhận chuyển vẫn "OK"; (b) CREATE 0 token tính là RECEIVE | 2 lỗi PnL |
| 13 | (a) ví còn gap báo "done"; (b) một giao dịch lỗi chặn cả ví | 2 lỗi |
| 14 | (a) restore có thể xoá chính bản đang khôi phục; (b) verify thêm integrity_check / JSON; (c) rollback migration | (a) lỗi; (b), (c) gia cố |

**Tổng:** 14 lỗi thực, 2 thay đổi không phải lỗi, 2 gia cố.

---

## 2. Ngân sách: hạn mức cứng cho từng phase

Quy tắc:
- Mỗi phase có **trần cứng riêng, đã gồm retry**.
- Tổng trần của tất cả phase là **48 ≤ 50**. 2 request còn lại không phase nào được dùng.
- Phần một phase không dùng hết **không bao giờ** chuyển sang phase khác: trần là hằng số, kiểm theo số slot của chính phase đó.
- Trần được ghi vào dòng đầu (genesis) của ledger. Sửa trần trong code sau `--init` thì ledger cũ bị từ chối.
- Khi nạp module, code tự kiểm:
  - tổng trần ≤ 50;
  - mỗi phase có trần;
  - số lần gửi đầu theo kế hoạch ≤ trần của phase.

  Sai một trong ba thì module **không nạp được**.

| Phase | Method | Lần gửi đầu theo kế hoạch | **Trần cứng (gồm retry)** | Dữ liệu kiểm | Kết quả mong đợi |
|---|---|---|---|---|---|
| 1 | getSignaturesForAddress | 6 | **8** | A1: 1000 chữ ký mint CHUCHU; A2 `before`; A3 `until`; A4 `before`+`until`; A5 2 trang `limit 10` | A1 mới → cũ; A2 không trùng, nối tiếp; A3 = phần mới phát sinh + A1[:500], không gồm checkpoint; A4 = A1[101:300]; A5 = lát cắt của A1 |
| 2 | getAccountInfo | 2 | **3** | mint CHUCHU; 1 ví thường | mint: owner SPL/Token-2022, `type=mint`; ví: không phải mint |
| 3 | getTransaction | 6 | **8** | 5 chữ ký từ A1; 1 chữ ký tự tạo (64 byte 0) | blockTime/slot khớp; giải mã không lỗi; chữ ký tự tạo → `null` |
| 4 | getTransaction | 1 | **2** | chữ ký sai định dạng | Lỗi JSON-RPC (để đo xem lỗi có bị tính credit không) |
| 5 | gSFA ×2, getTransaction ×8 | 10 | **13** | chữ ký mới nhất của chương trình pump.fun, rồi thử getTransaction sau 0 / 1 / 2 / 4 s (không retry ẩn) | Phân loại theo mục 5; đây là **quan sát**, không phải PASS |
| 6 | gSFA ×1, getTransaction ×10 | 11 | **14** | 1 ví DEPTH, 10 giao dịch rải đều | Giải mã không lỗi; mọi cặp `INSUFFICIENT_DATA`; chủ dự án đối chiếu tay ≥ 5 giao dịch |
| | **Cộng** | **36** | **48** | | |

**Mỗi request được ghi nhận trước khi gửi:**
- `Transport.post()` gọi `ledger.take()`, tức ghi một dòng và `fsync`, **trước** khi tạo HTTP request.
- `id` của JSON-RPC chính là số slot.
- Test `test_each_request_is_recorded_before_it_is_sent` đọc ledger trên đĩa từ bên trong lúc gửi: luôn thấy slot đã có.

---

## 3. Hard-stop 50 request

### 3.1 Thiết kế
- **Ledger `probe_out/ledger.jsonl`**:
  - chỉ ghi nối thêm;
  - mỗi dòng chứa hash của dòng trước (chuỗi băm SHA-256);
  - dòng 0 (genesis) chứa mã chiến dịch ngẫu nhiên, đường dẫn ledger và các trần.
- **Anchor `~/.insiders_probe/anchor.json`**:
  - nằm **ngoài** thư mục output;
  - đường dẫn cố định trong code, không có tuỳ chọn dòng lệnh hay biến môi trường nào đổi được;
  - giữ mã chiến dịch, đường dẫn ledger, số slot và hash của dòng cuối.
- **Khoá tiến trình `~/.insiders_probe/probe.lock`**:
  - tạo bằng `O_CREAT | O_EXCL`, giữ suốt lần chạy;
  - lần chạy trước crash để lại khoá thì **từ chối**, tới khi có người kiểm tra và xoá tay.
- **Không có lệnh reset.** `--init` từ chối khi đã có ledger *hoặc* anchor.
- **Không có retry ẩn.** Probe không dùng `watch.Rpc`. Mỗi lần retry là một slot mới của phase đó.

### 3.2 Bằng chứng: tình huống → kết quả (đều có test)

| Tình huống | Kết quả |
|---|---|
| Xoá ledger, hoặc xoá cả thư mục output | Từ chối ("ledger bị mất trong khi anchor còn") và không `--init` lại được |
| Cắt bớt dòng cuối ledger | Từ chối (ledger ngắn hơn anchor) |
| Sửa một dòng (ví dụ đổi phase) | Từ chối (chuỗi băm sai) |
| Thay bằng một ledger hợp lệ của chiến dịch khác | Từ chối |
| Chuyển ledger sang thư mục khác (`--out` khác) | Từ chối (anchor trỏ tới ledger khác) |
| Anchor hỏng / không đọc được | Từ chối |
| Mất anchor, ledger còn | Từ chối |
| Chưa từng `--init` | Từ chối chạy |
| Sửa trần trong code sau khi init | Từ chối |
| `--init` lần hai, hoặc `--init` với thư mục khác | Từ chối |
| Đang có probe khác giữ khoá | Từ chối (mã 3). Test 4 tiến trình thật tranh khoá cùng lúc: đúng 1 thắng |
| Khoá còn sót sau crash | Từ chối tới khi xoá tay |
| Gọi `take()` khi không giữ khoá | Từ chối |
| **Crash ngay trước khi ghi ledger** | Chưa gửi gì, không tính slot (nhất quán) |
| **Crash sau khi ghi ledger, trước khi cập nhật anchor** | Không gửi gì. Lần sau chấp nhận (ledger hơn anchor đúng 1 dòng), slot **được tính**, anchor chỉ tiến lên |
| Crash trong lúc request đang gửi | Slot đã tính (có thể đếm dư, không bao giờ đếm thiếu) |
| Mất điện giữa lúc ghi một dòng (dòng dở) | Từ chối (dòng hỏng): mặc định đóng |
| Timeout / lỗi mạng | Vẫn tính slot |
| **200 lần khởi động liên tiếp, mọi phase, mọi request đều 429** | Tổng đúng 48 request ra mạng (= tổng trần các phase); mỗi phase đúng bằng trần; không lần nào vượt |

**Kiểm tra đột biến** (đưa từng lỗ hổng trở lại vào bản sao tạm, xem test có FAIL): **8/9 bị bắt.**
- Bị bắt:
  - bỏ kiểm ledger ngắn hơn anchor;
  - bỏ trần phase;
  - ghi ledger sau khi gửi;
  - khoá không độc quyền;
  - coi ledger mất là 0;
  - bỏ chuỗi băm;
  - cho init lại;
  - coi null là "không tồn tại".
- Không bị bắt: bỏ kiểm mã chiến dịch. Đây là lớp kiểm thừa: ledger bị thay vẫn bị từ chối ở bước so hash dòng cuối với anchor.

### 3.3 Giới hạn thật của cơ chế
Cơ chế chống reset **ngoài ý muốn** và chống sai sót. Nó **không chống được** một người cố ý:
- xoá cả ledger lẫn anchor ở hai nơi khác nhau, rồi `--init`;
- hoặc chạy trên máy thứ hai.

Lớp kiểm cuối là **số request trên dashboard Helius** (nếu dashboard có hiện), phải ≤ số dòng ledger. Quy định vận hành:
- chỉ chạy trên một máy;
- không xoá thư mục `~/.insiders_probe` khi chưa có phê duyệt mới.

---

## 4. Credit

### 4.1 Dashboard đo được gì, không đo được gì
Tài liệu Helius ([Credits](https://www.helius.dev/docs/billing/credits), [Plans](https://www.helius.dev/docs/billing/plans)) **không mô tả** trang usage. Vì vậy:

| | Trạng thái |
|---|---|
| Tổng credit đã dùng của tài khoản/dự án theo thời gian | **Giả định đo được.** Chủ dự án phải xác nhận trên dashboard trước khi chạy |
| Credit theo từng method | **Chưa biết** dashboard có tách không. Nếu không tách: chỉ có **tổng delta của phase** |
| Credit theo từng API key | **Chưa biết** |
| Số request theo method | **Chưa biết** |
| Độ trễ cập nhật số liệu | **Chưa biết**: đọc lặp tới khi 2 lần liên tiếp bằng nhau |
| Request lỗi / 429 có bị tính không | Tài liệu không nói; phase 4 đo trường hợp lỗi JSON-RPC. 429 **không đo được** (không cố tình gây) |
| Response bytes | Không lấy từ dashboard. Lấy từ log của probe (`bytes`); không dùng để suy ra credit |

### 4.2 Cách tính
`python -m insiders.probe --credit-report readings.json`

Chủ dự án tự nhập các lần đọc dashboard; code không đọc Helius. Kết quả theo phase:
- **`ĐƠN GIÁ THEO METHOD`**: chỉ khi nền đứng yên (≥ 2 lần đọc trước phase 1 bằng nhau) **và** phase chỉ có một method **và** số đã ổn định.
- **`CHỈ TỔNG DELTA CỦA PHASE`**: phase 5, 6 (nhiều method). Không tách theo method.
- **`KHÔNG CÔ LẬP`**: nền thay đổi trong lúc không chạy probe. Chỉ là tổng delta, có thể lẫn tiêu thụ khác; không đưa ra đơn giá.
- **`CHƯA ỔN ĐỊNH`**: chưa có 2 lần đọc liên tiếp bằng nhau.

Ba đại lượng không bao giờ trộn: **số request** và **bytes** lấy từ log; **credit** lấy từ dashboard.

### 4.3 Cô lập trước khi đo
Những thứ tiêu credit bằng cùng key, tìm được trong code:

| Nguồn | Gọi gì | Bị PAUSE chặn? | Cô lập bằng |
|---|---|---|---|
| Stream WS (`vip.run`) | `logsSubscribe` trên `wss://mainnet.helius-rpc.com` (có thể bị tính theo dung lượng) | Có: đóng socket | `/healthz`: `paused=true`, `connected=false` |
| Poll + quét bù (`vip.poll_forever`) | getSignaturesForAddress, getTransaction | Có | như trên |
| Tracker coin (`track.Tracker`) | getAccountInfo, gSFA, getTransaction | Có | **Không chạy trên server hiện tại** (commit `35b5a29` chưa có); `/healthz` không có mục `track` |
| Devstats | getBalance + **Enhanced Transactions API (100 credit/lần)** | Có | như trên |
| Worker lịch sử ví | gSFA, getTransaction | Mặc định TẮT (`INSIDER_WALLET_HISTORY=0`) | **Không có trên server hiện tại** (chưa deploy) |
| **Rescan `POST /api/insiders/run`** | `scan.Chain` (nhiều RPC) | **KHÔNG**: không phụ thuộc PAUSE | Không ai bấm rescan trong cửa sổ đo; `GET /api/insiders` (cần mã truy cập, chủ dự án tự xem) → `running=false` trước và sau |
| Dịch vụ / máy khác dùng cùng key (nếu có) | ? | Ngoài phạm vi code | Chủ dự án kiểm danh sách key / dịch vụ trên dashboard và Render |
| Máy local (script cũ, phiên khác) | ? | | Không chạy gì khác trong cửa sổ đo |

**Quy trình:**
1. Kiểm `/healthz` (`vip.paused=true`, `vip.connected=false`, commit đúng) và `/api/insiders` (`running=false`) như bảng trên. Chủ dự án tự làm; probe không gọi server.
2. Đọc credit dashboard ≥ 3 lần, cách nhau ≥ 5 phút. Phải bằng nhau. Nếu khác, **dừng**: chưa cô lập được.
3. Chạy từng phase. Sau mỗi phase, đọc lặp tới khi ổn định.
4. Sau phase cuối, đọc lại nền thêm 15 phút để chắc không có tiêu thụ trễ.

**Phương án mạnh hơn (tuỳ chủ dự án):** tạo một API key riêng chỉ cho probe, nếu dashboard tách được theo key. Key này chỉ đặt trên máy chạy probe, không đưa lên Render.

---

## 5. Giao dịch trả muộn

### 5.1 Xác định trên mainnet
- Lấy chữ ký bằng `getSignaturesForAddress(<chương trình pump.fun>, limit 5, commitment confirmed)`. Chữ ký được liệt kê kèm `slot` và `err=null` là **bằng chứng giao dịch tồn tại** ở mức confirmed.
- Ngay sau đó gọi `getTransaction(commitment confirmed)` lần lượt sau 0, 1, 2, 4 giây.
  - Mỗi lần là đúng **một** HTTP request, không có retry ẩn.
  - Dừng khi có giao dịch, và kiểm `slot` khớp.

### 5.2 Phân loại từng lần thử (`classify`)

| Lớp | Nghĩa | Có phải câu trả lời về giao dịch? |
|---|---|---|
| `FOUND` | Có giao dịch | Có |
| `NULL` | `result: null` | Có, nhưng chỉ nghĩa là "node chưa phục vụ" |
| `RPC_ERROR` | Lỗi JSON-RPC | Không |
| `HTTP_429` / `HTTP_5xx` | Bị giới hạn / lỗi máy chủ | Không |
| `TIMEOUT` | Không có phản hồi trong 20 s | Không |
| `NETWORK_ERROR` / `INVALID_BODY` | Lỗi kết nối / body không phải JSON | Không |

### 5.3 Kết luận cho mỗi chữ ký (`late_verdict`)
- **`SERVED_FIRST_ANSWER`**: câu trả lời có thông tin đầu tiên đã có giao dịch.
- **`SERVED_AFTER_NULL`**: null rồi mới có. Thời điểm sẵn sàng nằm trong khoảng (lần null cuối, lần có giao dịch], **theo đồng hồ máy chạy probe**.
- **`NOT_SERVED_IN_WINDOW`**: chỉ null trong cửa sổ thử. Chữ ký đã được liệt kê, nên **không** kết luận "không tồn tại".
- **`UNDETERMINED`**: chỉ có timeout / lỗi. Không có thông tin.

### 5.4 Phân biệt bốn trường hợp

| Trường hợp | Nhận biết được không |
|---|---|
| RPC trả null | Có (`NULL`) |
| Chưa được index | **Không phân biệt được** với "node chưa có" từ phía client. Cả hai đều là `NULL` cho một chữ ký có bằng chứng tồn tại |
| Timeout | Có (`TIMEOUT`, không được tính là null) |
| Không tồn tại | Chỉ khẳng định với chữ ký **tự tạo** (64 byte 0, chưa từng được phát). Với chữ ký bất kỳ, `getTransaction` trả null **không** chứng minh "không tồn tại" |

### 5.5 Không tuyên bố quá bằng chứng
- Kết quả phase 5 ghi là **`OBSERVED`**, không phải PASS.
- Không có đồng hồ độc lập phía node, và gSFA với getTransaction có thể đi tới hai node khác nhau. Vì vậy **không** kết luận được "độ trễ của mạng hay của Helius".
- Chỉ báo: số lần thử và khoảng thời gian quan sát được theo đồng hồ máy.
- Độ chênh so với `blockTime` được ghi trong trường `blocktime_lag_unverified_s`, kèm nhãn *chưa kiểm chứng*: blockTime chỉ chính xác tới giây, là ước lượng của validator, và đồng hồ máy có thể lệch.

---

## 6. Trần cho worker lịch sử ví (chưa đặt số, chưa bật)

- Chi phí một ví: `C_w = c_sig × ⌈N_w/1000⌉ + c_tx × N_w × (1 + r)`.
  - `c_sig`, `c_tx`: credit mỗi request. Chỉ có khi mục 4 cho ra `ĐƠN GIÁ THEO METHOD`. Nếu không, dùng **giá công bố 1 credit/lần** nhưng ghi rõ là *chưa kiểm chứng*.
  - `N_w`: số chữ ký hợp lệ (≤ 10.000); `r`: tỷ lệ retry.
- Cần một lượt census đếm chữ ký của 120 ví (≤ 1.200 request), phê duyệt riêng, để biết `ΣC_w`.
- Trần tháng = min(`ΣC_w`, 25% × (credit tháng của gói − tiêu thụ nền của stream và tracker × 30 − 20% dự phòng)).
- Trần ngày = trần tháng / 30, và ≤ 15 lượt/phút × 1.440.
- Code hiện đếm trần theo **số lượt gọi** (20.000 / 400.000 là giá trị giữ chỗ). Khi có đơn giá thật, nên đổi sang đếm theo credit.

---

## 7. Rủi ro PnL và điều kiện `INSUFFICIENT_DATA`

**Đã có trong code:**
- token còn giữ mà chưa có giá;
- bán khi đang giữ token nhận chuyển;
- bán nhiều hơn lượng mua thấy được;
- lịch sử bị cắt;
- còn gap;
- bội số 2x–10x;
- ví `incomplete` thì không có tổng.

**Đề xuất thêm (chưa làm; cần duyệt):**

| Rủi ro | Điều kiện đề xuất |
|---|---|
| Làm tròn 0,001 SOL | `0,0005 × số giao dịch` > 2% `sol_in` của cặp |
| Đổi token ↔ token / mua bằng USDC | Giao dịch có ≥ 2 mint (ngoài SOL/USDC) cùng đổi số dư → cả hai cặp |
| Dev buy (giá vốn ước tính) | Cặp có CREATE kèm SOL: không gộp vào tổng đã xác minh |
| Bundle nhiều ví | Giao dịch có > 1 owner đổi số dư cùng mint |
| Phí của giao dịch thất bại | Ghi riêng, không ảnh hưởng realized |
| Bot vault | Không phát hiện được; chỉ ghi chú rủi ro |
| Thiên lệch sống sót của WR/ROI | Tổng ví `INSUFFICIENT_DATA` nếu cặp OK < 80% hoặc < 5 cặp đã đóng |
| Lịch sử quá cũ | Đánh dấu stale sau X ngày |

---

## 8. Tiêu chí PASS / FAIL

| Hạng mục | PASS | FAIL → hành động |
|---|---|---|
| Hard-stop | Dòng ledger = dòng `requests.jsonl` ≤ 48; mỗi phase ≤ trần; số request trên dashboard (nếu có) ≤ số dòng ledger | Dashboard nhiều hơn → nghi có nơi khác dùng key: dừng, điều tra |
| Cô lập | Nền đứng yên trước khi chạy và 15 phút sau khi xong | Nền thay đổi → credit chỉ là tổng delta, không có đơn giá |
| Phân trang A1–A5 | Mọi kiểm PASS | Sửa logic B2 trước khi bỏ PAUSE |
| getAccountInfo E1–E2 | PASS | Sửa `step_verify` |
| getTransaction B, D2 | 5/5 khớp blockTime/slot, giải mã không lỗi, D2 = null | Thêm fixture, sửa parser |
| Lỗi D1 | Trả lỗi JSON-RPC | Sửa nhánh lỗi |
| Trả muộn C | `OBSERVED` (không phải `UNDETERMINED`) và slot khớp | `UNDETERMINED`: không có kết luận; không FAIL Cổng B (code đã có retry/gap) |
| Mẫu ví F | Giải mã không lỗi; ≥ 5 giao dịch đối chiếu tay khớp loại và SOL ± 0,001 | Lệch: không bật worker ví tới khi sửa |
| Credit | `ĐƠN GIÁ THEO METHOD` cho phase 1, 2, 3 (và phase 4 cho request lỗi) | Không tách được: ghi "chỉ tổng delta", chưa đặt trần theo credit |

---

## 9. Chủ dự án cần quyết định

1. Duyệt (hoặc sửa) 6 phase, trần cứng 8 / 3 / 8 / 2 / 13 / 14 (tổng 48).
2. Xác nhận dashboard có hiện tổng credit đã dùng hay không, có tách theo method / key không, và gói Helius đang dùng.
3. Cam kết cửa sổ đo:
   - không rescan;
   - không bỏ PAUSE;
   - không dịch vụ hoặc máy nào khác dùng key;
   - hoặc tạo key riêng cho probe.
4. Chạy trên đúng một máy. Không xoá `~/.insiders_probe` khi chưa có phê duyệt mới.
