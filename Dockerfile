# Sử dụng base image nhẹ
FROM python:3.13-slim

# Thiết lập biến môi trường
# PYTHONUNBUFFERED=1: Log in ra ngay lập tức (quan trọng cho Docker logs)
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Tạo user non-root
RUN adduser --disabled-password --gecos '' appuser

# Cài đặt dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Tạo quyền cho cache directory
RUN mkdir -p tiktoken && chown -R appuser:appuser /app

USER appuser

EXPOSE 8080

# [THAY ĐỔI QUAN TRỌNG] Chạy bằng Gunicorn
# app:app -> file app.py, biến app
# --workers 2: Số worker process (tùy số core CPU, 2 là an toàn cho VPS nhỏ)
# --threads 4: Số thread mỗi worker (giúp xử lý nhiều request chờ đợi I/O cùng lúc)
# --timeout 300: 5 phút. Quan trọng vì AI generation stream rất lâu, tránh bị ngắt giữa chừng.
# --access-logfile -: In access log ra stdout để docker capture
CMD ["gunicorn", "--workers", "2", "--threads", "4", "--timeout", "300", "--bind", "0.0.0.0:8080", "--access-logfile", "-", "--error-logfile", "-", "app:app"]
