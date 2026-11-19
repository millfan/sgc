FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --extra postgres --no-install-project
COPY config ./config
COPY src ./src
RUN uv sync --frozen --no-dev --extra postgres
ENTRYPOINT ["uv", "run", "--no-sync", "stockassistant"]
CMD ["--help"]
