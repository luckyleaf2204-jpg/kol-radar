# KOL Radar — bot copy KOL trên pump.fun (PAPER)

Bot độc lập, không dùng code của các bot trước. Nghe **mọi lệnh trên bonding curve pump.fun** (RPC công khai của
Solana), copy lệnh mua của **565 ví KOL** (danh sách kolscan chốt ngày 2026-10-06, `kols.json`) **trên giấy**, và bán
khi KOL bán. **Không ví, không private key, không gửi giao dịch nào.**

```
python main.py            # chạy bot + giao diện http://127.0.0.1:8780
python main.py --report   # xem kết quả trong terminal
run.bat                   # như trên, trên Windows
```

## Giao diện
* **KOL đang bắt coin nào:** mỗi coin là một thẻ gồm:
  * KOL nào vào, bao nhiêu SOL, vào ở MC bao nhiêu, đã bán chưa.
  * MC hiện tại và % thay đổi so với lúc KOL đầu tiên vào.
  * Số người trade, số ví trade trong 5 phút, số lệnh mua/bán, volume, ATH.
  * Số bình luận, trạng thái livestream, link X/TG/Web.
  * **Đánh giá dev:** dev đã tạo bao nhiêu coin, bao nhiêu coin tốt nghiệp, có đang bán coin này không.
* **"Người đang xem":** pump.fun không công khai số này. Bot hiển thị thay bằng số ví trade trong 5 phút, số bình
  luận và trạng thái livestream.
* Bên phải giao diện: kết quả bot giấy, luồng lệnh KOL realtime, và các lệnh giấy đã đóng.

## Dashboard (KOLs · Devs · Tokens · Trades · Live)
* **KOLs:**
  * Mở trang là thấy ngay **Top KOL / Top losers since first seen**.
  * Dải KPI gồm: số KOL, tổng lệnh, P&L, ROI, win rate, sample size (status), thời gian dữ liệu, trạng thái stream.
  * Bảng KOL phân trang: Rank, First seen, Trades, Win rate, Cumulative P&L, ROI, 7D, 30D, Avg, Status.
  * Bộ lọc: nhóm, khoảng thời gian, min n, lời/lỗ, status, sắp xếp.
  * 4 chart: P&L tích lũy, phân bố ROI, thắng/thua, số lệnh theo thời gian.
* **Chi tiết KOL:**
  * Performance since first seen: P&L, ROI, trades, win rate, median, best/worst, streak.
  * Chart P&L tích lũy, snapshot theo ngày, lịch sử giao dịch phân trang, breakdown theo token.
  * Link ví sang Solscan.
* **Status:**
  * n < 100 → INCONCLUSIVE.
  * n ≥ 100 và CI95 > 0 → PROVISIONAL. Chưa thể là PASS vì bot paper không có nhóm control.
  * n ≥ 100 và CI95 ≤ 0 → REJECT.
* **Devs:**
  * Mỗi token gắn với creator (lấy từ CreateEvent/TradeEvent). Lịch sử trước đó lấy từ pump.fun.
  * Outcome: PENDING (< 24 giờ), MIGRATED, FAILED, DEAD.
  * Rug chỉ gán khi có bằng chứng: theo dõi từ lúc tạo, dev bán ≥ 90%, MC giảm ≥ 80%, không migrate.
  * Risk: UNKNOWN / LOW / MEDIUM / HIGH / REPEAT FAILURE / SUSPICIOUS, có lý do, bằng chứng và thời điểm tính, kèm lịch sử thay đổi.
  * Nhãn KNOWN DEV / NEW DEV hiển thị trên token, trong lệnh và trên thẻ Live.
* **Lưu trữ bền** (SQLite `data/paper.db`, hoặc đường dẫn trong biến môi trường `KOL_DB`):
  * Sổ lệnh có khóa chống trùng `uid` = kol:mint:trigger.
  * Bảng `kols`: wallet là khóa chính, first_seen không bao giờ reset.
  * `kol_daily`: tính lại từ sổ lệnh.
  * `tokens`, `devs`, `dev_risk_log`, `wallet_links` (chỉ lưu quan hệ ví có nguồn rõ ràng).
* Gói free của Render **xóa ổ đĩa mỗi lần deploy hoặc restart**. Muốn giữ lịch sử thì gắn Persistent Disk và đặt `KOL_DB` trỏ vào đó, hoặc chạy local.
* Trước mỗi lần deploy nên **tải CSV** sổ lệnh ở tab Trades.
* Test giao diện với dữ liệu giả: `python tools/demo_db.py`, rồi `python main.py --no-stream --db data/demo.db --port 8781`.

## Cách khớp lệnh giấy (giống thật nhất có thể)
* **Lúc mua:** lệnh của mình coi như vào sau KOL `delay_s` = 3 giây. Giá khớp tính trên reserves thật của curve,
  lấy theo lệnh đầu tiên trên coin đó sau mốc 3 giây. Nếu coin im lặng thì dùng trạng thái curve mới nhất.
* **Chi phí đã trừ:**
  * phí pump.fun 1.25% mỗi chiều (đọc từ event của coin);
  * trượt giá thêm 1% mỗi chiều;
  * phí ưu tiên 0.005 SOL và phí mạng 0.00011 SOL cho mỗi giao dịch.
* **Lúc bán:** 3 giây sau khi KOL bán. Nếu coin tốt nghiệp thì bán ở giá cuối trên curve. Giữ tối đa 24 giờ.
* **Bỏ qua (không copy):**
  * KOL mua dưới 0.05 SOL;
  * coin đã có lệnh của mình;
  * đã đủ 10 lệnh mở;
  * đã lỗ đủ hạn mức trong ngày (1 SOL);
  * KOL bán trước khi lệnh của mình kịp vào.
* Sửa tham số trong `config.json`, dùng đúng tên trường trong `kolbot/config.py`.

## Đọc kết quả
* **Thước đo chính:** lãi trung bình mỗi lệnh sau toàn bộ chi phí, kèm CI 95% bootstrap theo KOL.
* **Trạng thái mẫu:** dưới 30 lệnh là INSUFFICIENT, 30–99 là PRELIMINARY, từ 100 lệnh trở lên là OK. Chưa đủ 100
  lệnh thì không kết luận.
* Lệnh nào có thời gian giữ trùng với lúc stream mất kết nối quá 5 phút sẽ bị loại khỏi thống kê.

Đây là kiểm tra forward của hướng C. Nó **không thay** cho bài kiểm tra đã đăng ký trước trong
sol_memecoin_hunter (`docs/kol_plan.md`, kết quả sau 27/10).
Chỉ nên nghĩ tới tiền thật khi cả hai đều cho kết quả dương, với CI nằm hẳn trên 0.
