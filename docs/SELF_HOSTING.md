# Self-hosting

This guide covers the open-source Compose stack and production container
requirements. It does not replace host, network, or secret-management policy.

## Local Compose stack

The default stack builds `browser-runtime`, starts a bundled Redis 7 cache, and
binds the API to `127.0.0.1:11235`.

```bash
cp .env.example .env
docker compose up --build --detach

docker compose ps
curl --fail http://127.0.0.1:11235/health/ready
```

Follow logs or stop the stack:

```bash
docker compose logs --follow crawler
docker compose down
```

Redis has no persistent volume because it stores rebuildable response-cache
entries only. Stopping the stack may discard its contents.

Local mode permits an empty bearer token and is safe only on a trusted
machine. Compose's loopback binding prevents direct remote access; do not
change it to `0.0.0.0` before production authentication and network policy are
in place.

## Select an image target

Compose uses `browser-runtime` unless `CRAWLER_DOCKER_TARGET` overrides it.

Static-only:

```bash
GIT_SHA="$(git rev-parse HEAD)" \
CRAWLER_DOCKER_TARGET=static-runtime \
docker compose up --build --detach
```

Browser-capable:

```bash
GIT_SHA="$(git rev-parse HEAD)" \
CRAWLER_DOCKER_TARGET=browser-runtime \
docker compose up --build --detach
```

Optional quality client:

```bash
GIT_SHA="$(git rev-parse HEAD)" \
CRAWLER_DOCKER_TARGET=quality-runtime \
docker compose up --build --detach
```

`quality-runtime` contains a pinned client package, not model weights. The
quality lane remains disabled until `QUALITY_EXTRACTION_BASE_URL`,
`QUALITY_EXTRACTION_API_KEY`, and `QUALITY_EXTRACTION_MODEL` are all set.
Review model and service licenses separately.

## Production configuration

Set these values through a secret store or a protected environment file:

| Setting | Requirement |
| --- | --- |
| `ENVIRONMENT` | `prod` |
| `GIT_SHA` | Exact 7–64 character hexadecimal source commit |
| `IMAGE_DIGEST` | Immutable `sha256:` OCI digest when available |
| `CRAWL4AI_API_TOKEN` | Non-empty bearer token |
| `SERVING_FINGERPRINT_KEY` | Independent high-entropy value, at least 32 characters |

The fingerprint key must differ from the bearer token and contain no
whitespace. It binds non-secret serving configuration and credential
identities without exposing their values.

Production startup fails closed when authentication or the fingerprint key is
missing. Production also rejects Redis with `GIT_SHA=unknown`.

Keep `CORS_ALLOW_ORIGINS` empty unless named browser origins need access.
Health and OpenAPI routes remain public; `/crawl`, `/md`, `/html`, and `/map`
require the bearer token in production.

## Container boundaries

Every target runs the API as UID/GID `10001`. The checked-in Compose stack also
uses:

- a read-only root filesystem;
- bounded `/tmp` and crawler-home tmpfs mounts;
- 1 GiB shared memory;
- 256 PIDs;
- 4 GiB memory;
- 2 CPUs; and
- the checked-in seccomp profile.

The static image contains no Playwright package, Chromium binary, browser
cache, or sandbox helper. It can use stricter static-container policies such as
`no-new-privileges` and a full capability drop.

The browser and quality images have a different sandbox contract. They include
Playwright's version-matched Chromium SUID helper for hosts that block
unprivileged user namespaces. The checked-in Compose stack therefore uses
seccomp but deliberately does **not** set `no-new-privileges` or drop every
capability: either can neutralize the SUID fallback. Never solve a browser
startup failure by setting `PLAYWRIGHT_DISABLE_SANDBOX=true`.

See [`../SECURITY.md`](../SECURITY.md#chromium-sandbox) before changing browser
container flags.

## Network boundary

The application rejects unsafe destination addresses and revalidates every
redirect. Production should still enforce egress policy that denies:

- private and loopback ranges;
- link-local and cloud-metadata destinations;
- disallowed IPv6 ranges; and
- any network not required by the deployment.

Place an authenticated reverse proxy or gateway in front of the loopback-bound
service when remote clients need access. Apply tenant quotas, request-rate
limits, TLS, and maximum body limits at the edge.

The crawler intentionally does not provide residential proxy rotation, TLS
impersonation, or bot-wall evasion.

## Redis

Redis is optional. Without `REDIS_URL`, extraction remains functional and
responses are computed live.

Cache keys bind source revision, request options, runtime semantics, serving
configuration, and credential identities. HTML projections are never cached.
`max_age=0` forces a live crawl.

Policy-aware recursive crawls bypass the flat response cache because that
envelope cannot replay the full redirect, robots, and scope decision chain.
Redis is an optimization, not a crawl queue or source of truth.

## Scale and process model

The service can run multiple API replicas, but these components remain
process-local:

- outbound per-domain limiter;
- recursive frontier;
- robots cache; and
- in-process singleflight.

One recursive request is handled within one process. The built-in crawler does
not coordinate host politeness, leases, retries, or ordering across replicas
and is not restart-safe. Use an external durable crawl plane for those
requirements.

Native HTML and PDF parsing runs under bounded worker concurrency, but Python
cannot forcibly terminate a native call that never returns. Use multiple
worker processes with health-based recycling or per-job process isolation when
hard-kill containment is required.

## Verify an installation

Check health and identity:

```bash
curl --fail http://127.0.0.1:11235/health
curl --fail http://127.0.0.1:11235/health/ready
curl --fail http://127.0.0.1:11235/health/version
```

Verify authentication:

```bash
curl --silent --output /dev/null --write-out '%{http_code}\n' \
  --header 'content-type: application/json' \
  --data '{"urls":["https://example.com"]}' \
  http://127.0.0.1:11235/crawl
```

Production should return `401` without a bearer token. Then run an authorized
public crawl and confirm `results[0].error` is null.

Release and rollback checks are in
[`OPERATIONS.md`](OPERATIONS.md#release-checklist).

## Upgrade

1. Review the source diff, changelog, dependency lockfiles, and security notes.
2. Build all image targets used by the installation from an exact clean commit.
3. Record source commit and OCI digest.
4. Start the new revision without replacing the known-good instance.
5. Run readiness, identity, authentication, SSRF, and live-crawl checks.
6. Move traffic only after every required path passes.
7. Retain the previous immutable image until the observation window closes.

Documentation-only and default-off research changes do not require a runtime
rollout.
