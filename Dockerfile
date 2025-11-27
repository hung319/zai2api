FROM python:3.12-slim-bookworm
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy
ENV PATH="/app/.venv/bin:$PATH"

# Cài đặt build tools cần thiết cho Gevent (gcc, libc-dev)
# Sau đó xóa đi để giảm dung lượng image
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*

# Cài đặt dependencies
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-project --no-dev

COPY . .

# Sync project
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

EXPOSE 8080

# Chạy với Gunicorn + Gevent
CMD ["gunicorn", "-k", "gevent", "-w", "4", "-b", "0.0.0.0:8080", "--access-logfile", "-", "--error-logfile", "-", "app:app"]