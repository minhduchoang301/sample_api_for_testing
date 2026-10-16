# syntax=docker/dockerfile:1.7
FROM ghcr.io/astral-sh/uv:0.13.1 AS uv

FROM python:3.12-slim AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /srv
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

FROM python:3.12-slim
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /srv --shell /usr/sbin/nologin app \
    && mkdir -p /data && chown app:app /data
WORKDIR /srv
COPY --from=build /srv/.venv /srv/.venv
COPY app/__init__.py app/__init__.py
COPY app/common app/common
COPY app/checkout app/checkout
COPY app/web app/web
ENV PATH="/srv/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATABASE_URL=sqlite:////data/paylab.db \
    PORT=8000
USER app
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=5s --timeout=3s --start-period=5s --retries=10 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status == 200 else 1)"]
CMD ["python", "-m", "app.checkout"]
