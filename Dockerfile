# Build the wheel with uv, then install it into a slim runtime. The final image
# carries only the runtime dependencies and the tool, and runs as a non-root user.
#
#   docker build -t ci-pinpoint .
#   docker run --rm -v "$PWD:/work" ci-pinpoint explain build.log --provider ollama
#
# Logs are read from the mounted working directory or from URLs; pass tokens and
# provider settings with -e (e.g. -e GITHUB_TOKEN -e CI_PINPOINT_BASE_URL).

FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir build~=1.2 \
    && python -m build --wheel --outdir /dist

FROM python:3.12-slim AS runtime
LABEL org.opencontainers.image.title="ci-pinpoint" \
      org.opencontainers.image.description="Find the log lines that broke a CI build and explain why." \
      org.opencontainers.image.source="https://github.com/xeeshan16/ci-pinpoint" \
      org.opencontainers.image.licenses="MIT"
# Install the wheel and its runtime dependencies only (no build tools, no dev deps).
RUN --mount=from=build,source=/dist,target=/dist \
    pip install --no-cache-dir /dist/*.whl \
    && useradd --create-home --uid 10001 app
USER app
WORKDIR /work
ENTRYPOINT ["ci-pinpoint"]
CMD ["--help"]
