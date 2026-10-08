FROM ghcr.io/astral-sh/uv:0.9.5@sha256:f459f6f73a8c4ef5d69f4e6fbbdb8af751d6fa40ec34b39a1ab469acd6e289b7 AS uv
FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    HF_HOME=/var/cache/huggingface PATH=/app/.venv/bin:$PATH
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --extra embeddings --no-install-project
COPY src/ src/
RUN uv sync --locked --no-dev --extra embeddings && \
    useradd --uid 10001 --create-home --shell /usr/sbin/nologin brain && \
    mkdir -p /var/cache/huggingface /var/lib/company-brain/sync && \
    chown -R 10001:10001 /var/cache/huggingface /var/lib/company-brain
USER 10001:10001
CMD ["python", "-m", "company_brain", "serve"]
