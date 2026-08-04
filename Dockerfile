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
    --prune playwright \
    --output-file /static-requirements.txt \
    && ! grep -Eq '^(greenlet|playwright|pyee)==' /static-requirements.txt

WORKDIR /build/native
RUN maturin build \
    --release \
    --locked \
    --interpreter python3 \
    --out /wheels


FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de AS runtime-core

ARG GIT_SHA=unknown
LABEL org.opencontainers.image.source="https://github.com/clusy-io/clusy-crawler" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.revision="${GIT_SHA}"
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    GIT_SHA=${GIT_SHA}

WORKDIR /app

COPY --from=native-builder /wheels /wheels
COPY --from=native-builder /static-requirements.txt /static-requirements.txt
RUN pip install --no-cache-dir --require-hashes -r /static-requirements.txt \
    && pip install --no-cache-dir --no-deps /wheels/*.whl \
    && rm -rf /wheels /static-requirements.txt

RUN useradd --create-home --uid 10001 crawler \
    && chown -R crawler:crawler /app /home/crawler

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
    native/vendor/rs-trafilatura-broad/LICENSE-APACHE \
    native/vendor/rs-trafilatura-broad/LICENSE-MIT \
    /licenses/native-vendor/rs-trafilatura-broad/
COPY --chown=crawler:crawler \
    native/vendor/html-cleaning/LICENSE-APACHE \
    native/vendor/html-cleaning/LICENSE-MIT \
    /licenses/native-vendor/html-cleaning/
COPY --chown=crawler:crawler \
    native/vendor/quick_html2md/LICENSE-APACHE \
    native/vendor/quick_html2md/LICENSE-MIT \
    /licenses/native-vendor/quick_html2md/

EXPOSE 11235

HEALTHCHECK --interval=30s --timeout=5s --retries=5 --start-period=30s \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:11235/health/ready', timeout=3).status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "11235", "--log-level", "info"]


# Static-only production target. It is deliberately declared before every
# browser and quality stage so sequential/legacy builders stop without
# resolving or installing optional Playwright, MinerU, Torch, or CUDA layers.
FROM runtime-core AS static-runtime

ENV PLAYWRIGHT_ENABLED=false \
    PLAYWRIGHT_JAVA_SCRIPT_ENABLED=false
COPY --chown=crawler:crawler app/ app/
USER crawler


# Export the full browser-capable dependency graph only after static-runtime.
# Source installs retain Playwright as a normal dependency; the static image is
# the narrowly pruned deployment profile.
FROM native-builder AS browser-deps-builder

WORKDIR /build
RUN uv export \
    --frozen \
    --no-dev \
    --extra llm \
    --no-emit-project \
    --no-emit-package clusy-native \
    --output-file /browser-requirements.txt \
    && grep -q '^playwright==' /browser-requirements.txt


FROM runtime-core AS browser-runtime-deps

ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    CHROME_DEVEL_SANDBOX=/usr/local/sbin/chrome-devel-sandbox

COPY --from=browser-deps-builder /browser-requirements.txt /browser-requirements.txt
RUN pip install --no-cache-dir --require-hashes -r /browser-requirements.txt \
    && rm -f /browser-requirements.txt

# Some container hosts block Chromium's unprivileged-user-namespace sandbox.
# Preserve Playwright's version-matched SUID helper as the secure fallback
# instead of disabling Chromium's sandbox for untrusted pages.
RUN python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* \
    && chmod -R a=rX /ms-playwright \
    && browser_sandbox="$(find /ms-playwright -type f -name chrome_sandbox -print -quit)" \
    && test -n "${browser_sandbox}" \
    && install -o root -g root -m 4755 \
        "${browser_sandbox}" /usr/local/sbin/chrome-devel-sandbox


FROM browser-runtime-deps AS browser-runtime

COPY --chown=crawler:crawler app/ app/
USER crawler


# Keep the VCS package itself out of the hash-locked requirements file. It is
# built into a wheel from the exact revision below; uv still exports all of its
# lockfile-resolved transitive dependencies with hashes.
FROM browser-deps-builder AS quality-builder

WORKDIR /build
RUN uv export \
    --frozen \
    --no-dev \
    --extra llm \
    --extra quality \
    --prune accelerate \
    --prune pytest \
    --prune pytest-asyncio \
    --no-emit-project \
    --no-emit-package clusy-native \
    --no-emit-package mineru-html \
    --output-file /quality-requirements.txt \
    && ! grep -q "git+" /quality-requirements.txt \
    && ! grep -Eq \
        '^(accelerate|torch[^=]*|cuda-[^=]+|nvidia-[^=]+|triton|pytest[^=]*)==' \
        /quality-requirements.txt

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


# Opt-in image target containing the pinned MinerU-HTML OpenAI-compatible
# production path. Install third-party dependencies from the hash-locked export
# first, then install only the separately built, revision-pinned VCS wheel.
FROM browser-runtime-deps AS quality-runtime-deps

COPY --from=quality-builder /quality-requirements.txt /quality-requirements.txt
COPY --from=quality-builder /quality-wheels /quality-wheels
RUN pip install --no-cache-dir --require-hashes -r /quality-requirements.txt \
    && pip install --no-cache-dir --no-deps /quality-wheels/mineru_html-*.whl \
    && python -c \
        "import importlib.metadata as m, re, mineru_html; from mineru_html.process.simplify_html import simplify_html; from webpage_converter.convert import convert_html_to_structured_data; names = {re.sub(r'[-_.]+', '-', distribution.metadata['Name']).lower() for distribution in m.distributions() if distribution.metadata['Name']}; forbidden = sorted(name for name in names if name in {'accelerate', 'triton'} or name.startswith(('torch', 'cuda-', 'nvidia-', 'pytest'))); assert not forbidden, forbidden; assert m.version('mineru-html') == '1.1.2'; assert m.version('mineru-webkit') == '0.1.6'; simplified, mapped = simplify_html('<html><body><main><p>Clusy serializer smoke</p></main></body></html>', cutoff_length=500); assert '_item_id' in simplified and '_item_id' in mapped; output = convert_html_to_structured_data(main_html=mapped, url=None, output_format='mm_md'); assert isinstance(output, str) and 'Clusy serializer smoke' in output" \
    && rm -rf /quality-wheels /quality-requirements.txt
ENV MINERU_HTML_SOURCE_REVISION=73cf266690befd209cae7e6fdff9716d5b31a976


FROM quality-runtime-deps AS quality-runtime

COPY --chown=crawler:crawler app/ app/
USER crawler
RUN python -c \
    "from app.services.quality_extractor import quality_dependency_available; assert quality_dependency_available()"


# Preserve the historical browser-capable default image. Release and CI paths
# select browser-runtime explicitly so sequential builders stop before quality.
FROM browser-runtime AS runtime
