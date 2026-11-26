# Tệp ví theo dõi duy nhất (`insiders/wallet_db.json`)

Một tệp cho toàn bộ ví mà New Coins LIVE theo dõi. Có bản `insiders/wallet_db.csv` để mở bằng Excel.

- **Nguồn:**
  - ví DEPTH / STANK / INSIDERS của nghiên cứu cũ (`insiders/result.json`, chỉ đọc, bỏ ví sàn / ví nạp);
  - ví lớn của mọi coin nghiên cứu: mua ≥ 5 SOL trong một coin mà ví vào sớm (0–60 phút), hoặc vào sớm ≥ 2 coin và mua
    ≥ 1 SOL. Creator và CREATOR_GROUP không bao giờ được đưa vào.
- **Mỗi ví ghi:**
  - `sources`: cohort và/hoặc `coin:<SYM>`;
  - từng coin: phút vào, SOL, mức tăng sau khi vào, vai trò;
  - `max_buy_sol`, thứ hạng Early Smart Money;
  - `independence`, `entity`, cờ PASS_THROUGH / BATCH_SPLIT / COORDINATED;
  - ngày thêm (`added`), `in_latest_build`.
- **Quy tắc gộp:** ví đã có trong tệp KHÔNG bao giờ bị xóa. Lần gộp sau chỉ cập nhật bằng chứng và thêm ví mới. Ví
  không còn đạt ngưỡng vẫn nằm trong tệp, với `in_latest_build = 0`.
- **Lệnh live:** lệnh mua dưới 1 SOL không tính (`SMART_MIN_LIVE_SOL`). Luật HIGH_ALERT / MULTI_WALLET không đổi.

## Thêm coin mới

Pipeline nghiên cứu nằm trong `research/seven_coins`, chỉ dùng RPC công khai:

1. Thêm `SYM: mint` vào `COINS` trong `collect.py`, xác minh mint on-chain, ghi `verified.json`.
2. `python collect.py SYM 1500`
3. `python fetch_events.py SYM <cap> <workers> public 7200`
4. `python analyze_window.py SYM`
5. `FUND_SOURCE=public python funding_window.py`
6. `python funding_window.py trace`
7. `python entity.py`
8. `python compile_wallets.py <db>`
9. `python review_tierA.py <db>`
10. `python merge_early.py`
11. Chép các file `*.json` cần cho bước import vào `insiders/esm_seed/`.
12. `python -m insiders.esm walletdb` để gộp vào `insiders/wallet_db.json` / `.csv`.
13. Chạy `pytest` (rc = 0), commit rồi push. Lúc khởi động, production sao lưu DB rồi nạp seed mới (seed được nhận ra là
    mới qua mã SHA-256).
