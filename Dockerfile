FROM python:3.14.8-slim-trixie AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.0 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# Dependencies first so code changes don't invalidate the cached dependency layer.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY README.md alembic.ini ./
COPY src ./src
COPY migrations ./migrations
RUN uv sync --locked --no-dev --no-editable

FROM python:3.14.8-slim-trixie
RUN useradd --system --uid 10001 app
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY --from=build /app/alembic.ini ./
COPY --from=build /app/migrations ./migrations
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
USER app
EXPOSE 8000
CMD ["uvicorn", "ledger_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
