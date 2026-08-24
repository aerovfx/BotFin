# Kinh Tế Bot

Bot tin tức kinh tế Việt Nam chạy cục bộ trên máy bạn — tự động thu thập tin từ các báo lớn, theo dõi số liệu thị trường, và đẩy tin mới lên Telegram/Discord. Đi kèm dashboard web phong cách trading để điều khiển bằng nút bấm, không cần gõ lệnh.

## Tính năng nổi bật

### Thu thập tin tự động

- **10 nguồn RSS** từ VnExpress, Tuổi Trẻ, Thanh Niên, VietnamNet, Tiền Phong và 4 chuyên mục CafeF (chứng khoán, tài chính quốc tế, tài chính ngân hàng, vĩ mô đầu tư) — thêm/xóa nguồn ngay trên giao diện
- **Chạy nền theo chu kỳ** 5–60 phút, không cần canh chừng
- **Chống trùng tin thông minh**: ghi nhớ link đã gửi trong `seen.json`, tự dọn link quá 7 ngày
- **Làm sạch nội dung**: tóm tắt bài viết được lọc HTML tự động

### Số liệu thị trường trực tiếp

Mỗi chu kỳ cập nhật bảng dữ liệu gồm 9 chỉ số:

| Nhóm | Chỉ số |
|---|---|
| Chứng khoán VN | VN-Index, VN30, HNX-Index, UPCOM, HNX30 |
| Tỷ giá | USD/VND |
| Hàng hóa | Vàng quốc tế (USD/oz), Dầu WTI, Dầu Brent |

Xanh lá = tăng, đỏ = giảm, hiển thị ngay trên thanh ticker đầu trang.

### Dashboard web (http://127.0.0.1:8787)

- **Nút LẤY TIN NGAY** — lấy tin tức thì bằng một cú bấm
- **Đèn trạng thái LIVE/SAFE MODE** nhấp nháy theo thời gian thực
- **Luồng tin tức** có lọc theo nguồn và tìm kiếm từ khóa
- **Nhật ký hệ thống** dạng terminal nền đen
- **Quản lý kênh gửi**: điền token, bấm Test gửi để kiểm tra ngay
- Chạy hoàn toàn cục bộ, dữ liệu nằm trong thư mục máy bạn

### Phân phối đa kênh

- **Telegram** và **Discord** — dùng một hoặc cả hai
- **Safe Mode mặc định**: bot chỉ thu thập và lưu tin; bật công tắc "Tự động đẩy tin" khi sẵn sàng
- Gửi tin kiểm tra trước khi bật thật để chắc chắn cấu hình đúng

## Khởi động nhanh

macOS — nhấp đúp là chạy:

```
start.command
```

Hoặc bằng Terminal:

```bash
cd ~/bot/bot
.venv/bin/python app.py        # dashboard tại http://127.0.0.1:8787
```

Lần đầu tiên chạy `start.command`, bot tự tạo môi trường ảo và cài đặt mọi thứ.

### Cấu hình kênh gửi (`.env`)

```ini
TELEGRAM_BOT_TOKEN=...      # lấy từ @BotFather
TELEGRAM_CHAT_ID=...
DISCORD_BOT_TOKEN=...
DISCORD_CHANNEL_ID=...
FETCH_INTERVAL_MINUTES=30   # chu kỳ tự động lấy tin
AUTO_SEND=0                 # 1 = tự động đẩy tin khi có tin mới
```

Điền trực tiếp trong `.env` hoặc nhập qua dashboard (khuyên dùng — token bị che khi hiển thị).

### Chế độ dòng lệnh (không cần dashboard)

```bash
.venv/bin/python fetch_news.py --dry-run --limit 5   # xem thử, không gửi
.venv/bin/python fetch_news.py --limit 10            # lấy và gửi tin mới
```

## Cấu trúc thư mục

```
bot/
├── start.command        # nhấp đúp để khởi động (macOS)
├── app.py               # server dashboard + vòng lặp tự động
├── fetch_news.py        # lõi thu thập tin RSS + gửi kênh
├── market_data.py       # số liệu chứng khoán, tỷ giá, vàng, dầu
├── templates/index.html # giao diện dashboard
├── sources.json         # danh sách nguồn RSS
├── .env                 # token Telegram/Discord (không chia sẻ!)
├── news.txt             # bản tin lần chạy cuối
├── news.json            # kho tin cho dashboard
├── market.json          # snapshot chỉ số thị trường
├── seen.json            # chống trùng tin
└── bot.log              # nhật ký hệ thống
```

## Ghi chú kỹ thuật

- Bot chỉ lắng nghe trên `127.0.0.1` — không ai từ ngoài mạng truy cập được dashboard
- Nguồn dữ liệu: RSS chính thống của từng báo, bảng giá CafeF, Yahoo Finance
- Token chỉ lưu trong `.env` trên máy; dashboard luôn che token khi hiển thị
- Muốn bot chạy ngầm mãi dù tắt Terminal: dùng `start.command` trong cửa sổ riêng, hoặc cron `*/30 * * * * cd ~/bot/bot && .venv/bin/python fetch_news.py >> bot.log 2>&1`
