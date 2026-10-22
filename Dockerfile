# aobs-server: the collector (`serve`), the dashboard and the CLI, in one image.
# Used by compose.yaml for the local demo (docs/docker.md). Build: docker build -t aobs-server:local .
#
# Two stages: `build` installs the locked dependencies and aobs-server (not editable) into a virtual
# environment with uv; the final stage copies only that environment and runs as a non-root user.
# Nothing secret is baked in: the database URL and the dashboard token come from the environment.

ARG PYTHON_IMAGE=python:3.13-slim

FROM ${PYTHON_IMAGE} AS build
ARG UV_VERSION=0.11.32
RUN pip install --no-cache-dir --disable-pip-version-check "uv==${UV_VERSION}"
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/aobs
WORKDIR /src
# The workspace metadata and the lock file first, so the dependency layer is reused when only
# the server's code changes. --locked fails the build if uv.lock is out of date.
COPY pyproject.toml uv.lock ./
COPY server/pyproject.toml server/README.md server/LICENSE server/
COPY sdk/python/pyproject.toml sdk/python/README.md sdk/python/LICENSE sdk/python/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --package aobs-server --no-install-workspace
COPY server/src server/src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --package aobs-server --no-editable \
    && /opt/aobs/bin/aobs-server --version

FROM ${PYTHON_IMAGE}
LABEL org.opencontainers.image.title="aobs-server" \
      org.opencontainers.image.description="aobs collector, dashboard and CLI (alpha; local demo image)" \
      org.opencontainers.image.source="https://github.com/AshmitJain932/ai-agent-observability" \
      org.opencontainers.image.licenses="Apache-2.0"
RUN groupadd --system --gid 10001 aobs \
    && useradd --system --uid 10001 --gid aobs --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin aobs
COPY --from=build /opt/aobs /opt/aobs
ENV PATH=/opt/aobs/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /tmp
USER aobs:aobs
EXPOSE 8000 8800
ENTRYPOINT ["aobs-server"]
CMD ["--help"]
