# Contributing

Thanks for your interest in improving Clusy Crawler. Contributions of all sizes
are welcome — bug reports, docs, tests, and code.

## Development setup

Requires Python 3.12+ and [uv](https://github.com/astral-sh/uv).

```bash
git clone https://github.com/clusy-io/clusy-crawler
cd clusy-crawler
uv sync --extra dev
uv run playwright install chromium   # for JS-rendering tests

# Run the service
uv run uvicorn app.main:app --reload --port 11235
```

## Before you open a PR

Everything CI checks, run locally:

```bash
uv run ruff check .        # lint (must be clean)
uv run mypy app            # types (strict; must be clean)
uv run pytest -q           # tests (must pass)
```

New behavior needs tests. Bug fixes should include a regression test.

## Extraction quality changes

If you touch `app/services/extractor.py`, please check the change against the
neutral benchmark so we don't regress article-body quality:

```bash
git clone https://github.com/scrapinghub/article-extraction-benchmark /tmp/aeb
uv run python bench/neutral_benchmark.py /tmp/aeb
cd /tmp/aeb && python evaluate.py     # our row is `clusy_crawler`
```

See [`bench/NEUTRAL_BENCHMARK.md`](bench/NEUTRAL_BENCHMARK.md). Report the F1
before/after in your PR. Beware of overfitting to the 181-page corpus — validate
that gains hold on the held-out `test` half and don't regress other page types.

## Guidelines

- Keep it dependency-light and permissively licensed. **No GPL/AGPL
  dependencies** — this project is Apache-2.0 and must stay installable by
  commercial users (see [`THIRD_PARTY_LICENSES.md`](THIRD_PARTY_LICENSES.md)).
- Don't add bot-wall evasion (TLS impersonation, residential proxy rotation).
  It's deliberately out of scope — see [`SECURITY.md`](SECURITY.md).
- Match the existing style; `ruff` and `mypy --strict` are the arbiters.
- Security-sensitive changes (the SSRF guard, fetch/redirect handling): please
  flag them in the PR description so they get an extra review.

## Reporting security issues

Do **not** open a public issue. See [`SECURITY.md`](SECURITY.md).

By contributing you agree your contributions are licensed under Apache-2.0.
