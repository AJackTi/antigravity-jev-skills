# Bộ kỹ năng Antigravity + TypeSafe Jev (Hướng dẫn Cài đặt & Vận hành)

Gói mã nguồn kết hợp **Antigravity CLI (`agy`)**, **TypeSafe Jev (`$jev`)** và **Codex/ChatGPT** gồm 3 kỹ năng:
- `antigravity-task-runner`: Điều phối tác vụ phần mềm giới hạn phạm vi ghi (bounded write scope).
- `antigravity-sol-quant-runner`: Kiểm toán chiến lược định lượng, dữ liệu, chi phí ma sát và rủi ro.
- `jev` (thư mục `jev-calibrated-decisions`): Phân loại bằng chứng có kiểu dữ liệu (typed triage) và phán đoán độc lập.

## Kiến trúc & Nguyên tắc vận hành
Quy trình `scripts/run_workflow.py` chạy tuần tự 5 bước:
1. **Gemini implement/fix (`agy` worker)**: Thực thi/sửa code trong phạm vi cho phép.
2. **Mechanical checks**: Chạy lệnh test, kiểm tra phạm vi file (`scope`) và khoảng trắng (`whitespace`).
3. **Jev typed triage (`$jev`)**: Dùng model text `jev-1.13.0` phân loại tính đầy đủ/nhất quán của bằng chứng.
4. **Fresh Gemini review(s)**: Phiên `agy` độc lập mới (khác `conversation_id` với worker) phản biện bản diff và kết quả test.
5. **ChatGPT final review**: Theo nguyên tắc toàn cục external-first ([global-config/AGENTS.md](global-config/AGENTS.md)), ChatGPT chỉ điều phối và nghiệm thu `proof-pack.txt`, **không tự viết code/test hay fallback cục bộ** trừ khi người dùng cho phép ngoại lệ rõ ràng theo từng tác vụ.

**Ràng buộc model & phản biện:**
- Mọi phiên `agy` ghim cứng `--model gemini-4-argon-high --effort high` (không dùng model dự phòng).
- Tác vụ `task` cần **1** phiên Gemini review mới; `quant` cần tối thiểu **2** phiên Gemini review mới độc lập (tối đa **2 vòng rework** khi bị `REJECT`).
- Phiên mới của cùng model chỉ đảm bảo **tính độc lập**, không phải model mạnh hơn; nghĩa vụ cần `stronger-reviewer` không thể giả mạo bằng cùng model `gemini-4-argon-high`.

## Hướng dẫn cài đặt từng bước

### 1. Điều kiện tiên quyết
- **macOS** hoặc **Linux**, **Python 3.10+**.
- **Antigravity CLI (`agy`)** chính thức đã cài đặt và xác thực theo tài liệu: https://antigravity.google/docs/getting-started/
- Kiểm tra CLI và xác nhận có đúng model `gemini-4-argon-high` (không hỗ trợ fallback sang model khác):
  ```bash
  agy --help
  agy models
  ```
- Tài khoản và API key **TypeSafe AI** (https://docs.typesafe.ai).
- **Codex** đọc quy ước toàn cục tại `~/.codex/AGENTS.md`.

### 2. Tải mã nguồn & Cấu trúc thư mục
- **Clone từ GitHub** vào thư mục gốc chứa `install.py`:
  ```bash
  git clone <YOUR_REPO_URL> antigravity-jev-skills
  cd antigravity-jev-skills
  ```
- **Hoặc từ file `.zip`:** Giải nén gói (thư mục gốc `antigravity-jev-skills`) rồi `cd antigravity-jev-skills`.
- **Cấu trúc thư mục** (phải giữ 3 thư mục kỹ năng nằm cạnh nhau trong cùng thư mục mẹ):
  - `antigravity-task-runner/`: `scripts/run_workflow.py` và cổng review dùng chung `scripts/jev_review_gate.py`.
  - `antigravity-sol-quant-runner/`: Kỹ năng kiểm toán định lượng ([SKILL.md](antigravity-sol-quant-runner/SKILL.md)).
  - `jev-calibrated-decisions/`: Tên thư mục tương thích ngược, được đăng ký thành kỹ năng `jev` và CLI `jev`, `jev-pool`.
  - `global-config/AGENTS.md`, `licenses/typesafe-ai-skills-MIT.txt`, [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md), [INSTALL.md](INSTALL.md), `manifest.json`, `install.py`.

### 3. Kiểm tra tính toàn vẹn (Offline)
- Nếu tải gói `.zip` kèm file `.sha256`, kiểm tra mã băm gói nén trước:
  ```bash
  shasum -a 256 -c *.zip.sha256
  # Ví dụ: shasum -a 256 -c antigravity-jev-skills-20261031.zip.sha256
  ```
- Tại thư mục gốc, đối chiếu toàn bộ file với `manifest.json`:
  ```bash
  python3 -B install.py
  ```
  *Lưu ý:* Nếu sửa code/tài liệu nằm trong manifest, người đóng gói phải tạo lại `manifest.json` thật — tuyệt đối không làm giả mã băm để lách kiểm tra.

### 4. Đăng ký Symlink kỹ năng & CLI
- Tạo 3 symlink kỹ năng vào `~/.agents/skills/` và 2 launcher (`jev`, `jev-pool`) vào `~/.local/bin/`:
  ```bash
  python3 -B install.py --install
  ```
- **Nguyên tắc an toàn:**
  - Lệnh chỉ tạo symlink, **không sao chép kỹ năng** và **không tự kích hoạt** `global-config/AGENTS.md`. Hãy giữ nguyên vị trí thư mục vừa clone.
  - Thư mục thật hiện có luôn được bảo toàn; `--replace-links` chỉ thay thế symlink thuộc quyền sở hữu của user, không ghi đè thư mục thật.
  - **Máy đã có sẵn bộ kỹ năng toàn cục đang hoạt động:** Không cài đè hoặc xóa cưỡng bức kỹ năng đang dùng. Hãy thử nghiệm cô lập bằng `--target-root` và chuyển đổi (migrate) chủ động sau khi kiểm tra:
    ```bash
    python3 -B install.py --install --target-root /tmp/agy-jev-trial
    ```

### 5. Cấu hình `PATH` & Kiểm tra CLI Offline
- Thêm `~/.local/bin` vào `PATH` trong shell hiện tại (và thêm một lần vào `~/.zshrc` / `~/.bashrc` để giữ lâu dài):
  ```bash
  export PATH="$HOME/.local/bin:$PATH"
  jev --help
  jev-pool --help
  ```

### 6. Hợp nhất chính sách toàn cục (`~/.codex/AGENTS.md`)
- Rà soát và gộp thủ công nội dung [global-config/AGENTS.md](global-config/AGENTS.md) vào `~/.codex/AGENTS.md` (giữ nguyên các quy tắc sẵn có; trình cài đặt không tự sửa file này). Không dùng lệnh ghi đè mù quáng, không chỉnh sửa khóa hay cài đặt đặc quyền.
- Khởi động phiên chat Codex mới để nhận diện kỹ năng và định tuyến các yêu cầu ngôn ngữ tự nhiên thông thường. `AGENTS.md` là quy ước hướng dẫn, không phải cơ chế cưỡng chế runtime; quy tắc cấp repo hoặc chỉ thị cao hơn luôn được ưu tiên và nếu xung đột sẽ dừng ở `BLOCKED`.

### 7. Thiết lập Key Pool cho `jev-pool`
- Workflow gọi trực tiếp `jev-pool`, nên chỉ dùng `jev auth set` hoặc chỉ đặt biến môi trường `TYPESAFE_API_KEY` là **không đủ**.
- `jev-pool` yêu cầu thư mục thật `~/.config/typesafe/keys` (quyền `0700`, từ chối symlink) chứa các file khóa thường `*.key` thuộc quyền sở hữu của bạn (quyền `0600`). Slot được chọn theo thứ tự tên file từ điển (`task` mặc định slot `1`, `quant` mặc định slot `2`; không tự động failover khi khóa bị từ chối).
- Tạo `key-01.key` an toàn **ngoài repo** bằng Python `getpass` với cờ `O_EXCL` (bảo toàn cấu hình sẵn có, không ghi đè khóa cũ, không lộ khóa qua tham số hay lịch sử shell):
  ```bash
  python3 -c '
  import getpass, os
  from pathlib import Path
  d = Path.home() / ".config/typesafe/keys"
  d.parent.mkdir(parents=True, exist_ok=True)
  if d.is_symlink(): raise SystemExit("Refusing symlink directory")
  d.mkdir(mode=0o700, exist_ok=True)
  os.chmod(d, 0o700)
  k = getpass.getpass("TypeSafe API key (slot 1): ").strip()
  assert k and k.isascii(), "Key must be non-empty ASCII"
  fd = os.open(d / "key-01.key", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
  with os.fdopen(fd, "w", encoding="ascii") as f: f.write(k + "\n")
  '
  ```
- Có thể tạo thêm `key-02.key`, `key-03.key` nếu muốn, nhưng **không bắt buộc có nhiều khóa**: nếu chỉ có 1 khóa, khi chạy `quant` hãy truyền `--key-slot 1`.

### 8. Chạy tác vụ kiểm thử tối thiểu (Smoke Task)
- Tạo workspace trống tách biệt và `packet.txt` mô tả ngắn gọn phạm vi ghi cùng lệnh kiểm tra:
  ```bash
  mkdir -p /tmp/agy-smoke-ws/tests
  cat << 'EOF' > /tmp/agy-smoke-packet.txt
  TASK: Create greet.py with hello(name) returning "Hi, <name>!" and unit test in tests/test_greet.py.
  WRITE SCOPE: greet.py, tests/test_greet.py.
  VERIFY: authorized shell command is exactly: python3 -B -m unittest discover -s tests -v
  EOF
  ```
- Chạy lệnh headless duy nhất (`--output-dir` phải là thư mục **mới** nằm ngoài write scope; lệnh tiêu thụ quota API thật và ChatGPT chỉ đóng vai trò điều phối):
  ```bash
  python3 -B ~/.agents/skills/antigravity-task-runner/scripts/run_workflow.py \
    --packet-file /tmp/agy-smoke-packet.txt \
    --workspace /tmp/agy-smoke-ws \
    --id smoke-1 \
    --scope greet.py tests/test_greet.py \
    --verify-cmd "python3 -B -m unittest discover -s tests -v" \
    --key-slot 1 \
    --output-dir /tmp/agy-smoke-out
  ```
- Kiểm tra `/tmp/agy-smoke-out/workflow-result.json` và `/tmp/agy-smoke-out/proof-pack.txt`: xác nhận exit code `0`, `verdict: "PASS"`, metadata model `gemini-4-argon-high` (`effort: "high"`), và có test thực thi vượt qua (chỉ sửa cấu hình sẽ không đạt `PASS`).

### 9. Chế độ Định lượng (`quant`) & Tài liệu nâng cao
- Chế độ `--kind quant` yêu cầu `--quant-contract` khai báo đầy đủ nguồn gốc dữ liệu (`data_provenance`), tính nhân quả không nhìn trước tương lai (`causality`), chi phí ma sát (`friction`), đối xứng `LONG`/`SHORT` (`parity`), hạt giống tái lập (seeds) và đối soát kế toán, kèm **2** phiên Gemini review mới; kết quả kiểm toán **không phải là phê duyệt giao dịch thật**. Xem chi tiết tại [antigravity-sol-quant-runner/SKILL.md](antigravity-sol-quant-runner/SKILL.md) và [references/quant-audit.md](antigravity-sol-quant-runner/references/quant-audit.md).
- Tính năng ảnh và `Box` tùy chọn (`jev-1.14.0+`, bật qua `--enable-images`) được mô tả tại [jev-calibrated-decisions/references/images.md](jev-calibrated-decisions/references/images.md).

### 10. Kiểm tra Offline, Lỗi `BLOCKED` & Bộ đếm Token
- Chạy kiểm tra offline từ từng thư mục kỹ năng:
  ```bash
  # Trong antigravity-task-runner/ (36 unit tests + 6 replay cases: 3 task, 3 quant)
  python3 -B -m unittest discover -s tests -v
  python3 -B scripts/replay_fixtures.py
  # Trong jev-calibrated-decisions/ (48 unit tests)
  python3 -B -m unittest discover -s tests -v
  ```
  *Lưu ý:* Kiểm tra offline không bảo đảm trạng thái model trực tuyến hay độ chính xác trên dữ liệu thực tế mới.
- **Lý do `BLOCKED` thường gặp:** thiếu `agy` hoặc model `gemini-4-argon-high`; lỗi xác thực/quota; sai quyền thư mục/file khóa (`0700`/`0600`) hoặc thiếu slot khóa; xung đột đường dẫn/`--output-dir` đã tồn tại; bản diff `> 12000` ký tự; hợp đồng `> 8000` ký tự; hoặc quá **2 vòng rework** không thành công.
- **Thống kê sử dụng:** Các số đếm token đầu vào/đầu ra/cache chỉ là thống kê kỹ thuật, không phải hóa đơn chi phí và không đảm bảo mức tiết kiệm chung cho mọi tác vụ.

### 11. Đưa lên GitHub & Lưu ý Bản quyền
- Đưa **nội dung bên trong** thư mục `antigravity-jev-skills` lên làm gốc (root) của repo để GitHub hiển thị `README.md` (qua giao diện web GitHub hoặc Git CLI với `<YOUR_REPO_URL>` và nhánh trung lập như `main`; kiểm tra danh sách file bằng `git status --short` trước khi `git add`):
  ```bash
  git init -b main
  git status --short
  git add .
  git commit -m "Initial source bundle"
  git remote add origin <YOUR_REPO_URL>
  git push -u origin main
  ```
- Không commit file khóa `*.key`, cấu hình nhạy cảm, thư mục log/output (`*.log`, `*.jsonl`, `workflow-result.json`, `proof-pack.txt`) hay thư mục `$HOME` (kiểm tra `git status --short --ignored` và `.gitignore` trước khi commit/push).
- **Bản quyền & Quyền phân phối ([THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)):** Tài liệu kỹ năng chính thức của TypeSafe AI tuân theo giấy phép MIT ([licenses/typesafe-ai-skills-MIT.txt](licenses/typesafe-ai-skills-MIT.txt), chỉ áp dụng cho các phần nội dung thượng nguồn TypeSafe AI). Đối với client `jev-calibrated-decisions/scripts/jev.py` (dựa trên Gist https://gist.github.com/AJackTi/6f70237e29767100936e213a22f4340f) và mã điều phối cục bộ, chủ sở hữu repo xác nhận quyền sở hữu bản client gốc từ Gist và quyền tái phân phối mã của mình (tuyên bố của người dùng ngày 2026-10-31; ghi nhận theo tuyên bố này, không phải thẩm định pháp lý độc lập). Không tự chọn `LICENSE` ở thư mục gốc hay cấp phép thay cho chủ sở hữu (không có `LICENSE` gốc nghĩa là chưa khai báo giấy phép tái sử dụng chung cho các phần mã gốc), không gắn giấy phép MIT cho toàn bộ repo và không hàm ý sự bảo chứng từ thượng nguồn.
