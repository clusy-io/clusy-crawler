# Single-stage build. (A prior multi-stage variant tripped a reproducible
# ACR Tasks layer-export bug on the `COPY --from=builder` → `COPY app/`
# sequence; flattening avoids the cross-stage layer export.)
FROM python:3.12-slim

WORKDIR /app

# Playwright system deps + chromium runtime libs
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libnspr4 libatk1.0-0t64 libatk-bridge2.0-0t64 \
    libcups2t64 libdrm2 libdbus-1-3 libxkbcommon0 \
    libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libpango-1.0-0 libcairo2 libasound2t64 \
    libatspi2.0-0t64 libwayland-client0 libcurl4 \
    && rm -rf /var/lib/apt/lists/*

# Python dependencies. Kept in sync with pyproject.toml — note the
# content-encoding decoders (brotli/zstandard), without which Brotli-served
# sites return garbage. PDF extraction uses pypdfium2 (BSD) and HTML→markdown
# uses trafilatura/markdownify — all permissive; PyMuPDF (AGPL) and html2text
# (GPLv3) were removed so the image can ship under a permissive licence.
RUN pip install --no-cache-dir \
    "fastapi>=0.115.0,<1.0.0" \
    "uvicorn>=0.45.0,<1.0.0" \
    "pydantic>=2.10.0,<3.0.0" \
    "pydantic-settings>=2.7.0,<3.0.0" \
    "httpx[http2]>=0.28.0,<1.0.0" \
    "h2>=4.3.0" \
    "brotli>=1.1.0" \
    "zstandard>=0.23.0" \
    "pypdfium2>=4.30.0" \
    "trafilatura>=2.0,<3.0" \
    "markdownify>=1.2.0,<2.0.0" \
    "beautifulsoup4>=4.12.0,<5.0.0" \
    "readability-lxml>=0.8.1,<1.0.0" \
    "playwright>=1.48.0,<2.0.0" \
    "structlog>=24.4.0,<26.0.0" \
    "tenacity>=9.0.0,<10.0.0" \
    "aiolimiter>=1.0.0,<2.0.0" \
    "lxml>=5.3.0,<6.0.0" \
    "redis>=5.2.0,<6.0.0" \
    "orjson>=3.10.0,<4.0.0"

RUN python -m playwright install chromium && python -m playwright install-deps chromium

COPY app/ app/

EXPOSE 11235

HEALTHCHECK --interval=30s --timeout=5s --retries=5 --start-period=30s \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:11235/health', timeout=3).status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "11235", "--log-level", "info"]
