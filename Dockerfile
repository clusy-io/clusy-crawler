# Build the pinned Rust/PyO3 extraction backend against the same Python ABI as
# the runtime. Copying the pinned Rust toolchain into the Python image keeps the
# final service image free of compilers and Cargo caches.
FROM rust:1.85-slim@sha256:9f841bbe9e7d8e37ceb96ed907265a3a0df7f44e3737d0b100e7907a679acb36 AS rust-toolchain

FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de AS native-builder

ENV CARGO_HOME=/usr/local/cargo \
    RUSTUP_HOME=/usr/local/rustup \
    PATH=/usr/local/cargo/bin:${PATH}

COPY --from=rust-toolchain /usr/local/cargo /usr/local/cargo
COPY --from=rust-toolchain /usr/local/rustup /usr/local/rustup

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        build-essential \
        ca-certificates \
        git \
        pkg-config \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir "maturin==1.14.1" "uv==0.11.6"

WORKDIR /build
COPY pyproject.toml uv.lock ./
COPY native/ native/
RUN uv export \
    --frozen \
    --no-dev \
    --extra llm \
    --no-emit-project \
    --no-emit-package clusy-native \
    --output-file /requirements.txt

WORKDIR /build/native
RUN maturin build \
    --release \
    --locked \
    --interpreter python3 \
    --out /wheels


# Keep the VCS package itself out of the hash-locked requirements file. It is
# built into a wheel from the exact revision below; uv still exports all of its
# lockfile-resolved transitive dependencies with hashes.
FROM native-builder AS quality-builder

WORKDIR /build
RUN uv export \
    --frozen \
    --no-dev \
    --extra llm \
    --extra quality \
    --no-emit-project \
    --no-emit-package clusy-native \
    --no-emit-package mineru-html \
    --output-file /quality-requirements.txt \
    && ! grep -q "git+" /quality-requirements.txt

RUN git init /build/mineru-html \
    && git -C /build/mineru-html remote add origin \
        https://github.com/opendatalab/MinerU-HTML.git \
    && git -C /build/mineru-html fetch --depth 1 origin \
        73cf266690befd209cae7e6fdff9716d5b31a976 \
    && git -C /build/mineru-html checkout --detach FETCH_HEAD \
    && test "$(git -C /build/mineru-html rev-parse HEAD)" = \
        "73cf266690befd209cae7e6fdff9716d5b31a976"
RUN SOURCE_DATE_EPOCH=1774521487 uv build \
        --wheel \
        --out-dir /quality-wheels \
        /build/mineru-html \
    && echo \
        "ff55e06b0f463a89e5a87015a1afd8d8468759166931fb92661daf340cbd06fe  /quality-wheels/mineru_html-1.1.2-py3-none-any.whl" \
        | sha256sum --check -


FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de AS runtime-base

ARG GIT_SHA=unknown
LABEL org.opencontainers.image.source="https://github.com/clusy-io/clusy-crawler" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.revision="${GIT_SHA}"
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    CHROME_DEVEL_SANDBOX=/usr/local/sbin/chrome-devel-sandbox \
    GIT_SHA=${GIT_SHA}

WORKDIR /app

COPY --from=native-builder /wheels /wheels
COPY --from=native-builder /requirements.txt /requirements.txt
RUN pip install --no-cache-dir --require-hashes -r /requirements.txt \
    && pip install --no-cache-dir --no-deps /wheels/*.whl \
    && rm -rf /wheels

# Preserve Playwright's version-matched SUID helper as a secure fallback on
# container platforms that block Chromium's unprivileged-user-namespace
# sandbox. Do not disable Chromium's sandbox for untrusted pages.
RUN python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 crawler \
    && chown -R crawler:crawler /app /home/crawler \
    && chmod -R a=rX /ms-playwright \
    && browser_sandbox="$(find /ms-playwright -type f -name chrome_sandbox -print -quit)" \
    && test -n "${browser_sandbox}" \
    && install -o root -g root -m 4755 \
        "${browser_sandbox}" /usr/local/sbin/chrome-devel-sandbox

COPY --chown=crawler:crawler \
    LICENSE \
    NOTICE \
    THIRD_PARTY_LICENSES.md \
    /licenses/
COPY --chown=crawler:crawler \
    native/vendor/NOTICE.md \
    /licenses/native-vendor/NOTICE.md
COPY --chown=crawler:crawler \
    native/vendor/rs-trafilatura/LICENSE-APACHE \
    native/vendor/rs-trafilatura/LICENSE-MIT \
    /licenses/native-vendor/rs-trafilatura/
COPY --chown=crawler:crawler \
    native/vendor/html-cleaning/LICENSE-APACHE \
    native/vendor/html-cleaning/LICENSE-MIT \
    /licenses/native-vendor/html-cleaning/
COPY --chown=crawler:crawler \
    native/vendor/quick_html2md/LICENSE-APACHE \
    native/vendor/quick_html2md/LICENSE-MIT \
    /licenses/native-vendor/quick_html2md/
COPY --chown=crawler:crawler app/ app/

USER crawler

EXPOSE 11235

HEALTHCHECK --interval=30s --timeout=5s --retries=5 --start-period=30s \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:11235/health/ready', timeout=3).status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "11235", "--log-level", "info"]


# Opt-in image target containing the pinned MinerU-HTML OpenAI-compatible
# production path. Install third-party dependencies from the hash-locked export
# first, then install only the separately built, revision-pinned VCS wheel.
FROM runtime-base AS quality-runtime

USER root
COPY --from=quality-builder /quality-requirements.txt /quality-requirements.txt
COPY --from=quality-builder /quality-wheels /quality-wheels
RUN pip install --no-cache-dir --require-hashes -r /quality-requirements.txt \
    && pip install --no-cache-dir --no-deps /quality-wheels/mineru_html-*.whl \
    && python -c \
        "import importlib.metadata as m, mineru_html; assert m.version('mineru-html') == '1.1.2'" \
    && rm -rf /quality-wheels /quality-requirements.txt
ENV MINERU_HTML_SOURCE_REVISION=73cf266690befd209cae7e6fdff9716d5b31a976
USER crawler


# Keep the lightweight deterministic image as both the named and implicit
# default target. `docker build .` therefore does not pull/build MinerU-HTML.
FROM runtime-base AS runtime
