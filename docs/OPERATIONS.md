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

The optional quality lane has fixed admission limits that configuration cannot
raise: 1,000,000 raw characters, 5,000 DOM elements, depth 64, and 8,000 text
fragments before preprocessing. Serializer work has additional URL, DOM,
table, image, list, code, and formula caps. Ineligible pages keep the
deterministic candidate and do not count as backend outages. Worker concurrency
is hard-capped at two because the pinned serializer is process-global.

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
3. Record the immutable runtime image identity: the Docker config image ID for
   a host-local build or the OCI manifest digest from a registry.
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

For `quality-runtime`, verify the frozen dependency surface and run the real
pinned preprocessor and MinerU-Webkit converter with networking disabled.
Exercise both supported prompt profiles against a bounded local
OpenAI-compatible stub and require an authenticated
`quality-source-selection-serialization.v1` receipt. A deterministic fallback
is safe service behavior, but it is not a passing quality-release check.

## Release identity

A reviewable release binds:

- source commit;
- immutable runtime image identity;
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
3. Confirm user namespaces are available, `SYS_CHROOT` is the only added
   capability, and the container has writable tmpfs and sufficient shared
   memory.
4. If the host requires the SUID fallback, check whether
   `no-new-privileges` or a `nosuid` mount neutralized it and review the
   orchestrator policy before changing the hardened profile.
5. Never set `PLAYWRIGHT_DISABLE_SANDBOX=true` in production.

### Redis degrades

The cache circuit breaker should preserve live crawling. Confirm connection
and operation timeouts, then repair Redis independently. Do not increase cache
timeouts until they dominate request latency.

### Optional quality backend degrades

Confirm that deterministic fallback succeeds, backend identity is correct, the
v1 source-serialization schema is advertised, and the circuit breaker opens.
Reduce or disable optional model traffic without changing the deterministic
route. Receipts are process-local capabilities; do not queue or pickle them
across workers or expect them to survive a restart.

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
