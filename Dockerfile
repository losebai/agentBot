# 安装已经通过 CI 检查的 wheel，并严格使用 uv.lock 中的运行依赖。
ARG UV_VERSION=0.11.2
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv
FROM python:3.12-slim-bookworm AS dependencies
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY dist-ci/*.whl /tmp/wheels/
RUN uv pip install --python /app/.venv/bin/python --no-deps /tmp/wheels/*.whl

# 最终镜像不包含 Node、构建缓存、测试源码或服务端密钥。
FROM python:3.12-slim-bookworm AS runtime
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
RUN groupadd --gid 10001 agentbot && useradd --uid 10001 --gid 10001 --no-create-home agentbot \
    && mkdir -p /app/data && chown 10001:10001 /app/data
COPY --from=dependencies /app/.venv /app/.venv
USER 10001:10001
EXPOSE 8080
# 就绪检查只检查本进程与队列，不调用 QQ 或付费模型。
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health/ready', timeout=3).close()"
ENTRYPOINT ["taobot"]
