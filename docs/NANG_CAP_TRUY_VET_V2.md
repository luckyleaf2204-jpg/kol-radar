# Nâng cấp truy vết mạng lưới (V2) trên nền hệ thống hiện có

Ngày: 2026-11-26. Môi trường: development (DB dev `data/dev_v2/insiders_track.db`, sao chép bằng SQLite backup từ DB
local; production, Render, worker, biến môi trường: KHÔNG đổi, KHÔNG deploy). Nguồn dữ liệu chain: chỉ RPC công khai
(không Helius).

## 1. Audit: cái đã có và cái còn thiếu

| Năng lực | Đã có (file / bảng / hàm) | Trạng thái trước V2 |
|---|---|---|
| DEV / Creator | `insiders/devstats.py` (lịch sử dev, Helius ở server), `nc_coin.creator` (New Coins), `esm_flag` CREATOR / CREATOR_GROUP, `result.json` tokens[].deployer (97 coin đã gọi) | Đã triển khai (production) |
| Cohort DEPTH / STANK / INSIDERS | `insiders/wallets.py` `cohorts()`, bảng `wallets` | Đã triển khai; chỉ đọc |
| Bảng đồ thị ví | `track.py` bảng `edges` + `merge_edges`, `edge_weight`, `clusters`, `evidence_paths` (truy NGƯỢC), `dispersal` (1 nguồn → nhiều ví mua) | Đã triển khai; chỉ nhận cạnh của tracker live (funding VÀO) |
| Funding 2 tầng (live) | `Tracker.step_funding` (`fund_todo`, FUND_HOPS=2, chỉ chuyển VÀO) | Đã triển khai |
| Truy sâu 3–4 tầng 2 chiều | `deep.py` `deep_trace` (lùi + xuôi, dừng ở sàn / hub) | Chỉ cho kênh call (Helius, `result.json`), không nối với nghiên cứu 7 coin |
| Funding nghiên cứu | `research/seven_coins/funding_window.py` (hop 1 / hop 2) | Chỉ 1 mốc mua cho mỗi ví, chỉ chuyển VÀO |
| Cụm chủ sở hữu | `research/seven_coins/entity.py` → `entities.json` → `esm.import_research` → `esm_link` / `esm_wallet_entity` | Đã triển khai (bản 5 coin trong `esm_seed`) |
| Smart Money scoring | `esm_view.radar` (danh sách) + `newcoins.SmartBook` (`group()` cho MULTI_WALLET / smart_groups) | Đã triển khai; độc lập chỉ theo entity / COORDINATED |
| Chuyển token: bên gửi / nhận | `track.sol_and_token_in` (chỉ phía nhận của 1 ví) | THIẾU: không có hàm chung theo owner token account; events nghiên cứu không ghi bên kia |
| Truy xuôi (ví nhận) cho nghiên cứu | — | THIẾU |
| Ví trung chuyển gần 1:1 | — | THIẾU |
| Chuyển tách cặp lặp lại | — (dispersal chỉ bắt 1 → nhiều ví cùng mua 1 coin) | THIẾU |
| Ví nhận chung | — | THIẾU |
| Mức độc lập trong Smart Money | `tierA_review.json` (file nghiên cứu) | THIẾU trong DB / SmartBook |

## 2. Mở rộng (không module mới, không bảng mới, không migration)

| File | Mở rộng |
|---|---|
| `insiders/track.py` | `token_moves(tx)`: bên gửi / nhận token theo OWNER của token account (không theo fee payer / signer); swap với pool bị loại; nhiều-nhiều = `ambiguous`, không đoán. `pass_through(edges)`: SOL vào → ra gần 1:1 trong 15 phút (chênh ≤ max(0,02 SOL, 2 %)), kèm `src_program` / `dst_program`. `batch_splits(edges)`: 1 nguồn trả ≥ 2 ví trong 1 giao dịch, lặp lại; `persons` = mọi người nhận là ví (không phải tài khoản chương trình). `shared_counterparties(edges, wallets, 'in'/'out')`: ví cấp vốn / ví nhận chung (loại sàn, hub, tài khoản chương trình) |
| `research/seven_coins/funding_window.py` | Bước `trace` (RPC công khai, resume qua `fundw_cache.json`): funding trước MỖI lần mua đầu của từng coin; truy XUÔI SOL + token sau lần mua; nhiều tầng (TRACE_HOPS); đọc đủ giao dịch bằng chứng (signer, payer, owner token). Cộng dồn nhiều lệnh chuyển cùng cặp trong 1 giao dịch. Output `network_trace.json` |
| `research/seven_coins/entity.py` | Đọc `network_trace.json`: STRONG `direct_transfer` / `shared_funder` từ funding nhiều mốc (cùng luật cũ); MEDIUM mới `transfer_outside`, `shared_recipient`, `relay_link`, `batch_split`; cờ `PASS_THROUGH`, `BATCH_SPLIT`. Luật gộp cụm giữ nguyên |
| `research/seven_coins/review_tierA.py` | Funding nhiều mốc; ví cấp vốn là sàn tách riêng (`funders_exchange`); chuyển ra; cờ V2; ghi chú lệnh mua không khớp; đối chiếu chéo (cohort, deployer 97 coin, scan entities, deep, family tree, creator / giao dịch New Coins); dùng lại kết quả on-chain cũ |
| `insiders/esm.py` `import_research` | Cạnh V2 vào bảng `edges` có sẵn (kind `research_sol` / `research_token`, thay mới mỗi lần nạp, cạnh live không đụng); `PASS_THROUGH`, `BATCH_SPLIT`, `INDEPENDENCE` vào `esm_flag` |
| `insiders/newcoins.py` `SmartBook` | `independence(w)` (mức độc lập, mặc định INSUFFICIENT_DATA, không bao giờ coi là độc lập khi chưa có bằng chứng); mỗi ví Smart Money mang `independence` + cờ mạng lưới trong evidence của tín hiệu; `group()` gộp các ví được trả cùng nhau lặp lại (BATCH_SPLIT giữa ví người). Luật HIGH_ALERT / MULTI_WALLET không đổi |
| `insiders/esm_view.py` `_reason` | Dòng giải thích: mức độc lập V2, ví trung chuyển, trả tiền cùng nhau |
| `tests/test_insiders_trace_v2.py` | Hồi quy: 9Y2, HHLz, E007, 2phTY, 2rbMg, 6pNndy; nạp idempotent; không mất cụm / tín hiệu cũ |

## 3. Chạy trên development (2026-11-26)

Lệnh (theo thứ tự, RPC công khai, không Helius):
`python funding_window.py trace` → `entity.py` → `compile_wallets.py <db dev>` → `review_tierA.py <db dev>` →
`python -m insiders.esm import research/seven_coins --db data/dev_v2/insiders_track.db`.

- Truy vết: 37 ví gốc (27 hạng A + 10 thành viên cùng cụm), 3 tầng, 237 lượt mở rộng (hàng đợi còn 0), ~2.264 lệnh
  RPC công khai, 166 địa chỉ, 3.101 cạnh (2.964 SOL, 137 token). Dừng ở: 28 tài khoản chương trình, 13 hub bận, 5 sàn.
- Truy xuôi: 69 mốc mua; 53 đọc đủ từ lần mua đến giao dịch cuối của ví trong cửa sổ 120 phút; 16 chưa đủ (ví quá
  bận, > 4.000 chữ ký) – ghi rõ trong `coverage`. Không giao dịch nào bị từ chối phục vụ.
- DB dev sau khi nạp: `edges` research_sol 2.964 / research_token 137; `esm_flag` PASS_THROUGH 11, INDEPENDENCE 27;
  SmartBook: 98 ví Smart Money, mỗi ví có mức độc lập (POSSIBLE_LINK 53, INDEPENDENT 38, LIKELY_SAME_ENTITY 5,
  INSUFFICIENT_DATA 2).
- Kiểm thử: `pytest` 353 passed, rc=0 (gồm 10 test V2, trong đó 2 test hồi quy trên dữ liệu nghiên cứu).

### Hồi quy so với archive_7coins_2026-11-26
- Phân loại hành vi (SNIPER 103, BOT_FLAG 46, CREATOR 7, EARLY_HOLDER 169), 549 ví đủ điều kiện, 27 ví hạng A, overlap
  STANK 26: KHÔNG đổi. Mọi cụm cũ vẫn nằm trọn trong một cụm; không ví LIKELY_SAME_ENTITY nào bị hạ; không mất tín hiệu
  cũ nào (907 → 911 cặp; +2 direct_transfer, +10 transfer_outside).
- 6 ví đổi lớp vì có dữ liệu mới:
  - `6pNndy8…` và `HxmwX3x…`: INDEPENDENT → LIKELY_SAME_ENTITY (cụm E020). HxmwX3x chuyển 1 SOL cho 6pNndy ~40 phút trước
    lần mua MOONX; bản cũ chỉ truy trước lần mua CHUCHU nên bỏ sót.
  - `9Y2Es2…`: INSUFFICIENT_DATA → POSSIBLE_LINK (transfer_outside với một ví mua khác).
  - `9w1ZT5…`: INDEPENDENT → INSUFFICIENT_DATA (funding nhiều mốc chỉ ra sàn Bybit: không xác định được chủ).
  - `Gx55…`: INSUFFICIENT_DATA → INDEPENDENT (nay đã truy, không có SOL ≥ 0,05 vào trong 72 giờ, cùng luật 20 ví kia).
  - `CiVeL…`: đã truy nhưng giữ INSUFFICIENT_DATA ở bước rà soát hạng A: ví mua bằng USDC, funding SOL không nói gì về
    nguồn USDC.
- Rà soát 27 ví: INDEPENDENT 15 → 14, POSSIBLE_LINK 4 → 5, LIKELY_SAME_ENTITY 4 → 5, INSUFFICIENT_DATA 4 → 3; vẫn 25 nhóm
  chủ theo bằng chứng mạnh; on-chain 57 OK / 2 MISMATCH (không đổi).

### Kiểm tra lại các case V2 của ChatGPT
| Case V2 | Kết quả trên dữ liệu |
|---|---|
| 9Y2 “pass-through ARu4n5 → 9Y2 → 9mTZH” | Đúng mẫu gần 1:1 (18 s, 70 s; chênh = phí). NHƯNG ARu4n5 và 9mTZH đều là tài khoản chương trình (ngoài curve), không phải ví người: 9Y2 chuyển SOL của nó giữa hai dịch vụ. Không phải liên kết người–người. Khoản 2,74 SOL tới 9MUh… cũng vào tài khoản chương trình |
| HHLz “phân phối theo cặp DyrKfa / HNbZo” | Đúng là tách cặp lặp lại 3 lần, nhưng cả hai đích là tài khoản chương trình → trả tiền cho dịch vụ, không liên kết ai (không gắn BATCH_SPLIT). Mới: `5DkYfy…` chuyển tiếp 1,12 SOL từ `5F1seM…` sang HHLz trong 15 s (trung chuyển người → người) |
| 2phTY “ví nhận chung của E007” | 2phTY là tài khoản chương trình (phí / vault) → không phải bằng chứng cùng chủ |
| E007 | Giữ nguyên cụm 3 ví hạng A (2351du, CKz4, 7ER9). Mới: `HDQ6t4…` chuyển tiếp 0,22 SOL từ CKz4 sang `3rgtLz…` trong 3 s; cfpU (cùng cụm) chuyển tiếp từ 2351du vào tài khoản chương trình |
| H62ok “funders_hop1 rỗng” | Không phải lỗi: ví cấp vốn là KuCoin Hot Wallet (sàn), theo luật không tính là liên kết chủ. Nay ghi riêng ở `funders_exchange` |
| 6pNndy “thiếu trong hop1” | Lỗi thật của bản cũ (1 mốc mua / ví). Đã sửa bằng funding nhiều mốc |
| 2rbMg signer mismatch | Đã đọc đủ: giao dịch CHUCHU do `4Jf25t…` ký và trả phí, MOONX do `E67QyV…`; token từ pool về ví (không có lệnh chuyển token giữa hai ví). Không phải bằng chứng ví tự mua |
| 408 / 406 lượt chuyển ra | Chênh do đếm: trong cùng 1 giao dịch có thể có 2 lệnh chuyển cùng cặp (ví dụ 9Y2 → 9mTZH 3,6177 + 0,0015 SOL). Nay gộp theo cặp / giao dịch |
| “Decode 3.000 chữ ký của ARu / 4raqd / G5GF” | Không làm: ba ví này là hub bận (≥ 1.000 giao dịch trong < 2 ngày); ARu còn là tài khoản chương trình. Đi qua hub sẽ nối mọi ví với nhau |

Tổng quát: 162 / 192 lượt SOL ≥ 0,05 mà 27 ví chuyển ra sau lần mua đi vào tài khoản chương trình (bot, dịch vụ),
không phải ví người. Liên kết người–người mới đáng truy tiếp: `5DkYfy…` → HHLz, `HDQ6t4…` (sau CKz4), `G6cbnH…` (sau 9Y2),
cụm E020 (`HxmwX3x…` / `6pNndy8…`).

## 4. Chưa làm / giới hạn
- Chưa commit, chưa deploy; production (`esm_seed`, Render, worker, biến môi trường) không đổi. Bản production không
  có `network_trace.json` nên `import_research` chạy như cũ.
- Trên DB dev, cạnh research nằm trong `edges`: tracker live (nếu bật trên DB dev) sẽ dùng chúng cho LINKED_BUY /
  DISPERSAL (paper).
- Luật HIGH_ALERT vẫn đếm số VÍ Smart Money (≥ 2), không đếm số nhóm độc lập: hai ví cùng cụm vẫn có thể bật HIGH_ALERT.
  Theo yêu cầu trước (“chưa sửa thuật toán HIGH ALERT/MULTI”), em chưa đổi; mức độc lập đã có sẵn trong evidence.
- 16 mốc truy xuôi chưa đủ (ví quá bận); tầng ≥ 2 chỉ đọc 1 trang chữ ký mới nhất của ví nhận.
- Nguồn vốn USDC (CiVeL) chưa truy.
- DB local không có bảng `wallets` (cohort); đối chiếu cohort dùng `insiders/result.json` (giống `compile_wallets.py`):
  27 ví + 120 nút mạng lưới không trùng DEPTH / STANK / INSIDERS, không là deployer của 97 coin đã gọi, không là creator
  trong New Coins; 3 ví hạng A từng là ví mua sớm / repeat wallet trong lần quét kênh call.

