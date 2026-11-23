# NL2Infra app image. Targets: builder, tools, runtime (the app), test (runtime + tests/).
# Build:  make image      (= podman build --format docker --target runtime -t nl2infra:dev .)
# Tool versions are the Phase 0 baseline that all 60 recorded benchmark runs used.
# No secret is ever copied in: they arrive at run time via --env-file.

# Base: Python 3.12.13 on Debian 13 (trixie), pinned by tag and digest. Written out in each FROM
# (not via ARG) so scanners such as Checkov can see the pin.

# ---------------------------------------------------------------- builder: Python deps
FROM docker.io/library/python:3.12.13-slim-trixie@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36 AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 UV_NO_CACHE=1
RUN pip install uv==0.11.8
COPY requirements.lock /tmp/requirements.lock
# `sync` installs exactly the lock (plus watchdog, which the lock marks Linux-only).
RUN uv venv /opt/venv --python /usr/local/bin/python3.12 \
 && uv pip sync --python /opt/venv/bin/python /tmp/requirements.lock

# ---------------------------------------------------------------- tools: kubectl + Conftest
# Each download is checked against the checksum its project publishes; a mismatch fails the build.
FROM docker.io/library/python:3.12.13-slim-trixie@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36 AS tools
ARG TARGETARCH
ARG KUBECTL_VERSION=v1.37.0
ARG CONFTEST_VERSION=0.69.0
# kubectl: dl.k8s.io/release/v1.37.0/bin/linux/<arch>/kubectl.sha256
ARG KUBECTL_SHA256_arm64=922df28df248cc00a9e025f947704f1d1482de64ece54cfe57e61f19eaf1eef3
ARG KUBECTL_SHA256_amd64=6129359f4e1f3848a5572ccb0b26cf28b8ca08cef38c95a765b2f64a2c961a2f
# Conftest v0.69.0 (embeds OPA 1.19.0): checksums.txt of the GitHub release
ARG CONFTEST_SHA256_arm64=3b9c35223fe35f9988e153cdffb0144f911201306c746758b73be82831c543d9
ARG CONFTEST_SHA256_amd64=96fc2fbf11f0afde51256647127e6f00a64ce839a4d9a0a1aef2426c0e6f4b3f
RUN set -eu; \
    arch="${TARGETARCH:-$(dpkg --print-architecture)}"; \
    case "$arch" in \
      arm64) k_sha="$KUBECTL_SHA256_arm64"; c_sha="$CONFTEST_SHA256_arm64"; c_arch=arm64 ;; \
      amd64) k_sha="$KUBECTL_SHA256_amd64"; c_sha="$CONFTEST_SHA256_amd64"; c_arch=x86_64 ;; \
      *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \
    esac; \
    fetch() { python3 -c 'import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])' "$1" "$2"; }; \
    fetch "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${arch}/kubectl" /tmp/kubectl; \
    echo "${k_sha}  /tmp/kubectl" | sha256sum -c -; \
    fetch "https://github.com/open-policy-agent/conftest/releases/download/v${CONFTEST_VERSION}/conftest_${CONFTEST_VERSION}_Linux_${c_arch}.tar.gz" /tmp/conftest.tgz; \
    echo "${c_sha}  /tmp/conftest.tgz" | sha256sum -c -; \
    tar -xzf /tmp/conftest.tgz -C /tmp conftest; \
    install -m 0755 /tmp/kubectl /tmp/conftest /usr/local/bin/

# ---------------------------------------------------------------- runtime: the app
FROM docker.io/library/python:3.12.13-slim-trixie@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36 AS runtime

# Fixed high UID/GID, as our own policies require of generated workloads.
RUN groupadd --gid 10001 app \
 && useradd --uid 10001 --gid 10001 --home-dir /home/app --create-home --shell /usr/sbin/nologin app

COPY --from=builder /opt/venv /opt/venv
COPY --from=tools /usr/local/bin/kubectl /usr/local/bin/conftest /usr/local/bin/

WORKDIR /app
# Only what the app reads at run time (code, policies, prompts, datasets, workload rules).
COPY contracts.py ./
COPY src/ ./src/
COPY policies/ ./policies/
COPY benchmarks/ ./benchmarks/
COPY eval/ ./eval/
COPY docs/ ./docs/
COPY requirements/ ./requirements/
COPY examples/ ./examples/

# Writable state lives in one volume at /app/state (owned by 10001). The local PR fallback
# writes ./nl2infra-manifests relative to the working directory, so that path points into it.
RUN mkdir -p /app/state/runs /app/state/nl2infra-manifests /app/runs \
 && ln -s /app/state/nl2infra-manifests /app/nl2infra-manifests \
 && chown -R 10001:10001 /app/state /home/app

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MODEL_NAME=openai/gpt-oss-120b \
    MOCK_MODE=true \
    NL2INFRA_RUNS_DIR=/app/state/runs \
    NL2INFRA_AUTH_DB=/app/state/auth.sqlite3 \
    NL2INFRA_EVENTS_FILE=/app/state/events.jsonl \
    GIT_PYTHON_REFRESH=quiet

# Commit-specific metadata last, so a new commit doesn't invalidate the layers above.
# NL2INFRA_GIT_COMMIT is what run logs record as "commit" when git isn't available.
ARG GIT_COMMIT=unknown
ARG VERSION=dev
ARG SOURCE=https://github.com/angelina10504/NL2Infra
LABEL org.opencontainers.image.title="nl2infra" \
      org.opencontainers.image.source="${SOURCE}" \
      org.opencontainers.image.revision="${GIT_COMMIT}" \
      org.opencontainers.image.version="${VERSION}"
ENV NL2INFRA_GIT_COMMIT="${GIT_COMMIT}"

USER 10001
EXPOSE 8000
VOLUME ["/app/state"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/metrics', timeout=4).status == 200 else 1)"]
CMD ["uvicorn", "src.nl2infra.server:app", "--host", "0.0.0.0", "--port", "8000"]

# ---------------------------------------------------------------- test: runtime + the test suite
FROM runtime AS test
# Everything the suite reads (tests, fixtures, samples, CI files, observability configs, ...);
# .containerignore still keeps secrets, run logs and local state out.
COPY --chown=10001:10001 . /app/
CMD ["python", "-m", "pytest", "tests/", "-q", "-p", "no:cacheprovider"]
