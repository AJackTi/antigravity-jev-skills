# Jev — nội dung bundle

- CLI/thư viện Python dùng standard library; `scripts/jev` hoạt động qua symlink.
- Noul, Choice, Score cho phán đoán có kiểu; `ask` hỗ trợ assets PNG/JPEG và Box
  khi bật rõ `--enable-images`. Box cần model `jev-1.14.0` trở lên.
- Mặc định văn bản và cổng review giữ `jev-1.13.0`; không tự đổi model của các
  ngưỡng đã đo. Ảnh/Box là tính năng thử nghiệm, cần dữ liệu có nhãn riêng.
- `jev-pool` chọn slot/stable/round-robin có lock; không tự chuyển key khi API lỗi.
- Khóa API thuộc cấu hình riêng của máy, không nằm trong bundle xuất ZIP.
- `scripts/evaluate_results.py` tính metric offline với ID/hash/model; test bao
  phủ evaluator, metadata, key pool, transport ảnh, Box và launcher.
- `references/images.md` có ví dụ và nguồn API chính thức; hai ảnh trong
  fixtures là dữ liệu giả lập có/không có hình chữ nhật đỏ.

Nguồn client ban đầu: https://gist.github.com/AJackTi/6f70237e29767100936e213a22f4340f
(revision tham chiếu 6bb90186f27677fbc5a83b57ddcb8ee3aef3fef1).
API hiện tại: https://docs.typesafe.ai/images và
https://docs.typesafe.ai/primitives/box.
