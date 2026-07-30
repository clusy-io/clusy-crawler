# Operations

This guide covers health, observability, release verification, rollback, and
failure handling. Image selection and host setup are documented in
[`SELF_HOSTING.md`](SELF_HOSTING.md).

## Request cache policy

The request-level persistent-result cache controls are independent:

- `max_age=0` bypasses persistent crawl-result cache reads;
- `store_in_cache=false` prevents persistent crawl-result cache writes; and
- using both produces a live no-store request with an explicit response
  receipt.

This boundary is deliberately narrower than zero-data retention. Operational
logs, transient process memory, network transit, target-site behavior,
browser state, and infrastructure telemetry require separate policies.

Do not log, commit, bake into images, or pass credentials on a command line.

## Health and identity

```bash
curl --fail http://127.0.0.1:11235/health
curl --fail http://127.0.0.1:11235/health/ready
curl --fail http://127.0.0.1:11235/health/version
```

| Route | Meaning |
| --- | --- |
| `/health` | Process is alive |
| `/health/ready` | Required configured components are ready |
| `/health/version` | Source, image, dependency, pipeline, router, and non-secret configuration identity |

Readiness includes Redis, Chromium, or the quality client only when the
corresponding capability is configured. The version response never returns
credentials or plaintext internal endpoints.

Readiness alone is not a release gate.

## Release checklist

Before moving traffic:

1. Start from a clean, reviewed commit.
2. Build from that exact commit and inject its `GIT_SHA`.
3. Record the immutable OCI digest.
4. Supply credentials through the platform secret store without printing
   them.
5. Start a candidate revision separately from the known-good instance.
6. Confirm `/health/ready`, exact source identity, and image identity.
7. Confirm an unauthenticated data request returns HTTP 401.
8. Confirm a metadata/private destination is rejected.
9. Run an authorized crawl against a real public URL.
10. Test browser or quality paths when the release depends on them.
11. Move traffic only after every required check passes.
12. Repeat readiness, identity, unauthorized-request, and crawl checks through
    the main endpoint.
13. Retain the previous immutable image until the observation window closes.

Documentation-only changes and default-off research code do not require a
runtime deployment.

## Release identity

A reviewable release binds:

- source commit;
- OCI digest;
- selected Docker target;
- pipeline and adaptive-router revisions;
- dependency/native identities;
- serving-configuration fingerprint; and
- optional backend revision where applicable.

Do not repair a failed image in place. Produce a new immutable identity.

## Rollback

Rollback when:

- readiness becomes unhealthy;
- source or image identity differs from the release;
- unauthenticated data routes are reachable;
- SSRF or redirect policy differs from the verified contract;
- authorized crawl output becomes incompatible;
- a required browser or quality path fails;
- p95 latency, error rate, memory, or cost crosses its release threshold; or
- a downstream API contract check fails.

Move traffic to the last verified image before investigating. Preserve failed
revision logs and identities without preserving credentials or unrestricted
third-party page bodies.

## Observability

Structured logs cover:

- route and request outcome;
- fetch, render, extraction, and cache stages;
- extraction route and routing reasons;
- recursive frontier totals and rejection reasons;
- optional backend health and circuit-breaker state; and
- shutdown cleanup failures.

Successful result metadata includes stage timings and completeness provenance.
`completeness_coverage` distinguishes `unassessed`, `output_only`,
`source_prefix`, and `source_full`; an unassessed specialist result is not a
zero-quality score.

Monitor at least:

- readiness and error rate;
- request and stage p50/p95/p99 latency;
- active admission, crawl, render, extraction, and optional-backend work;
- origin status/error classes;
- response truncation and size;
- browser restarts and render failures;
- Redis failure/circuit state;
- recursive rejection categories; and
- process RSS, CPU, file descriptors, PIDs, and network volume.

## Incident playbooks

### Origin failures increase

1. Separate DNS, connect, TLS, status, redirect, decoding, and body-cap errors.
2. Check whether one origin is consuming retry capacity.
3. Verify proxy and egress policy.
4. Keep SSRF, retry, and body caps unchanged while diagnosing.

### Browser readiness fails

1. Confirm the image is `browser-runtime` or `quality-runtime`.
2. Verify the version-matched sandbox helper and checked-in seccomp profile.
3. Confirm the container has writable tmpfs and sufficient shared memory.
4. Check whether `no-new-privileges`, a capability drop, or a `nosuid` mount
   disabled the required sandbox path.
5. Do not set `PLAYWRIGHT_DISABLE_SANDBOX=true` in production.

### Redis degrades

The cache circuit breaker should preserve live crawling. Confirm connection
and operation timeouts, then repair Redis independently. Do not increase cache
timeouts until they dominate request latency.

### Optional quality backend degrades

Confirm that deterministic fallback succeeds, backend identity is correct, and
the circuit breaker opens. Reduce or disable optional model traffic without
changing the deterministic route.

### Memory pressure

Inspect HTML-output requests, browser concurrency, PDF size, structured
extraction, and response budgets first. Do not remove global caps to recover
one large request.

### Recursive crawl stalls

Inspect robots status, host delays, trap rejection, frontier terminal reasons,
and origin retries. The request-local frontier is deliberately bounded; do not
convert an explicit terminal reason into an unbounded retry.

## Benchmark-related incidents

If a released extraction change regresses quality:

1. identify the exact source, image, profile, and serving fingerprint;
2. reproduce the affected page without benchmark labels in runtime input;
3. rerun the preregistered benchmark and required non-regression suites;
4. rollback if the promotion gate no longer holds; and
5. publish the negative result when it changes the promotion decision.

Local extraction-loop throughput must not be compared with HTTP, browser, or
live-web service latency.
