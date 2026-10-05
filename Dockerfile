# syntax=docker/dockerfile:1
# ============================================================================
#  InSilicoTrial MAS - container image.
#
#  Multi-stage, four usable targets:
#
#      docker build -t insilico-trial-mas .                        # runtime (default)
#      docker build --target spark -t insilico-trial-mas:spark .   # + JRE + PySpark
#      docker build --target dev   -t insilico-trial-mas:dev   .   # dev tooling + tests
#      docker build --target test  .                               # runs the suite
#
#  The runtime image installs the package into its own virtual environment, so no
#  compiler or build tooling leaks into the final layer, and runs as an
#  unprivileged user (uid 10001). The LLM provider defaults to the deterministic
#  offline one: the image needs no credentials and no network access to produce a
#  complete, reproducible trial readout.
# ============================================================================

ARG PYTHON_VERSION=3.12

# ---------------------------------------------------------------------------
# Stage 1 - build the virtual environment. Wheels are used wherever they exist
# (numpy, pandas, pyarrow, pydantic all publish manylinux and arm64 wheels), so
# no build-essential is needed; anything that did need a compiler fails here
# rather than silently at runtime.
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS builder

# Optional extras, e.g. `--build-arg EXTRAS=llm` for the Bedrock/OpenAI persona
# providers or `EXTRAS=ml` for the gradient-boosting head. Empty by default: the
# image then needs no credentials and no network to run a full trial.
ARG EXTRAS=""

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_ROOT_USER_ACTION=ignore

WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

# Packaging metadata is copied first so the dependency layer stays cached until
# pyproject.toml actually changes.
COPY pyproject.toml README.md LICENSE ./
COPY src/ ./src/

RUN python -m pip install --upgrade pip setuptools wheel \
 && if [ -n "${EXTRAS}" ]; then python -m pip install ".[${EXTRAS}]"; else python -m pip install "."; fi

# ---------------------------------------------------------------------------
# Stage 2 - runtime. Only the virtual environment, the example profiles and the
# entry point are copied over; all writable state lives on the /data volume.
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

ARG APP_UID=10001
ARG APP_GID=10001
ARG VERSION=1.0.0

LABEL org.opencontainers.image.title="InSilicoTrial MAS" \
      org.opencontainers.image.description="Multi-agent in-silico clinical trial simulation (Databricks and AWS; runs fully offline)" \
      org.opencontainers.image.source="https://github.com/SergeyGer/InSilicoTrial_MAS" \
      org.opencontainers.image.url="https://github.com/SergeyGer/InSilicoTrial_MAS" \
      org.opencontainers.image.documentation="https://github.com/SergeyGer/InSilicoTrial_MAS/blob/main/docs/DOCKER.md" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH="/opt/venv/bin:${PATH}" \
    INSILICO_OUTPUT_DIR=/data/artifacts \
    INSILICO_LLM__PROVIDER=offline \
    INSILICO_STORAGE__BACKEND=local \
    INSILICO_LOG_LEVEL=INFO

COPY --from=builder /opt/venv /opt/venv

# Example profiles ship with the image so `--config /app/conf/...` works as is.
COPY conf/ /app/conf/

RUN groupadd --gid "${APP_GID}" insilico \
 && useradd --uid "${APP_UID}" --gid "${APP_GID}" --create-home --shell /usr/sbin/nologin insilico \
 && mkdir -p /data/artifacts \
 # `chmod a+rX` first: files copied from a host with a restrictive umask arrive as
 # mode 600 and would be unreadable to the unprivileged user even after chown.
 && chmod -R a+rX /app \
 && chown -R "${APP_UID}:${APP_GID}" /data /app

WORKDIR /data
VOLUME ["/data"]
EXPOSE 8765

USER insilico

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/api/config', timeout=4).status == 200 else 1)"

ENTRYPOINT ["insilico-trial"]
CMD ["--help"]

# ---------------------------------------------------------------------------
# Stage 3 - developer image: dev extras plus the repository sources, so tests and
# linters run inside the container against the same interpreter as the package.
# ---------------------------------------------------------------------------
FROM runtime AS dev

# ARG values are scoped to the stage that declares them: without these two lines
# `chown -R "${APP_UID}:${APP_GID}"` expands to `chown -R ":"`, which succeeds
# silently and leaves the sources owned by root - pytest then fails to read them.
ARG APP_UID=10001
ARG APP_GID=10001

USER root
# The working directory must be the project itself before any `pip install .`:
# the runtime stage works in /data, and installing from there would fail with
# "egg_base option: 'src' does not exist".
WORKDIR /app
# The test suite reads repository-root files (pyproject.toml, .env.example, conf/),
# so the dev image carries the same layout as a checkout.
COPY pyproject.toml README.md LICENSE .env.example ./
COPY src/ ./src/
COPY tests/ ./tests/
COPY conf/ ./conf/
COPY scripts/ ./scripts/
COPY Makefile ./

RUN python -m pip install ".[dev]" \
 && python -m pip install -e . \
 && chmod -R a+rX /app \
 && chown -R "${APP_UID}:${APP_GID}" /app

ENV PYTHONPATH=/app/src
USER insilico
CMD ["bash"]

# ---------------------------------------------------------------------------
# Stage 4 - test image: the suite runs during the build, so a broken test breaks
# `docker build --target test .`. Spark tests are excluded because this image has
# no JVM - build the spark target for those.
# ---------------------------------------------------------------------------
FROM dev AS test

RUN python -m pytest -q -m "not spark" \
 && python -m ruff check src tests scripts \
 && python -m mypy

# ---------------------------------------------------------------------------
# Stage 5 - Spark image: adds a headless JVM and the Spark/Delta extras, which is
# what a Databricks-equivalent local run needs. Kept separate so the default
# build never pays for the JRE.
# ---------------------------------------------------------------------------
FROM runtime AS spark

USER root
# The extras are installed from the project metadata, so the sources are copied
# into a scratch directory: the runtime stage works in /data, which holds no
# pyproject.toml, and `pip install ".[spark]"` would fail there.
WORKDIR /tmp/pkg
COPY pyproject.toml README.md LICENSE ./
COPY src/ ./src/

RUN apt-get update \
 && apt-get install -y --no-install-recommends openjdk-17-jre-headless \
 && rm -rf /var/lib/apt/lists/* \
 && python -m pip install ".[spark]" \
 && rm -rf /tmp/pkg

# Resolve the JVM through a stable symlink: the Debian package path differs between
# amd64 (java-17-openjdk-amd64) and arm64 (java-17-openjdk-arm64), and the docs
# promise that both architectures build.
RUN ln -sfn "$(dirname "$(dirname "$(readlink -f "$(command -v java)")")")" /usr/lib/jvm/default-java

ENV JAVA_HOME=/usr/lib/jvm/default-java \
    INSILICO_ENGINE__BACKEND=spark

WORKDIR /data
USER insilico
