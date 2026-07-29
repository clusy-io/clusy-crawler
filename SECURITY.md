# Security policy

Clusy Crawler fetches caller-supplied URLs and processes untrusted HTML, PDFs,
and browser content. Treat every caller and origin response as hostile.

## Report a vulnerability

Do not open a public issue or include exploit details in a public pull request.
Use either:

- GitHub
  [private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing/privately-reporting-a-security-vulnerability);
  or
- `hi@clusy.io`.

Include the affected source revision, deployment mode, reproduction, impact,
and known mitigation. Do not send credentials, unrelated personal data, or
unnecessary copies of third-party content.

We target acknowledgement within three business days. Confirmed high-severity
issues target a fix or documented mitigation within 14 days; coordinated
disclosure or an upstream dependency may change the timeline.

## Trust boundaries

```text
untrusted caller
    │
    ├─ authentication and admission budgets
    ├─ URL / DNS / redirect / robots / scope policy
    ├─ public-network fetch and optional Chromium render
    ├─ hostile-content parsers under bounded concurrency
    └─ bounded response with non-secret provenance
```

Application checks are defense in depth, not a replacement for authentication,
quotas, process isolation, and network egress policy.

## Built-in defenses

The current runtime:

- accepts only absolute HTTP(S) destinations;
- resolves destinations and rejects loopback, private, link-local, metadata,
  multicast, reserved, unspecified, and IPv4-mapped unsafe addresses;
- follows redirects manually and revalidates every hop;
- bounds URL length, request bodies, retries, deadlines, decompressed bodies,
  parser work, concurrency, recursive pages, depth, hosts, and output size;
- validates browser document and subresource destinations;
- blocks browser service workers and WebSockets;
- creates a fresh browser context for each render;
- rejects `PLAYWRIGHT_DISABLE_SANDBOX=true` in production when Playwright is
  enabled;
- keeps secrets out of health/version output and configuration fingerprints;
- fails closed on ambiguous recursive robots policy; and
- preserves deterministic extraction when Redis or an optional quality
  backend fails.

DNS can still change between validation and connection. Chromium subresource
validation is not a complete aggregate network-byte quota. Enforce network
policy outside the application.

## Operator requirements

1. Set `ENVIRONMENT=prod`.
2. Set a non-empty `CRAWL4AI_API_TOKEN`.
3. Set an independent high-entropy `SERVING_FINGERPRINT_KEY` with at least 32
   characters. Do not reuse the bearer token.
4. Keep `CORS_ALLOW_ORIGINS` empty unless specific trusted browser origins
   need access.
5. Deny private, loopback, link-local, cloud-metadata, and disallowed IPv6
   egress at the network layer.
6. Apply tenant quotas and edge rate limits.
7. Run as UID/GID `10001`, keep a read-only root filesystem, and provide only
   the bounded writable mounts required by the selected image.
8. Pin the source commit and OCI digest; verify both through `/health/version`.
9. Do not log credentials, provider tokens, authorization headers, or
   unrestricted third-party page bodies.
10. Keep a verified rollback image.

Health and OpenAPI routes are public. `/crawl`, `/md`, `/html`, and `/map`
require bearer authentication in production.

## Chromium sandbox

The static image contains no browser. Prefer it when rendering is unnecessary;
it can use `no-new-privileges` and a full capability drop.

The browser and quality images include Playwright's version-matched Chromium
SUID sandbox helper and can use the user-namespace sandbox where the host
permits it. Exactly one supported sandbox path must be verified:

- when unprivileged user namespaces are available, verify Chromium is using
  that sandbox before adding stricter container flags; or
- when the host blocks unprivileged user namespaces, the SUID helper must be
  executable on a non-`nosuid` mount and container policy must allow its
  privilege transition.

`no-new-privileges` prevents the SUID fallback from working. Dropping every
capability can also make that path unavailable. The checked-in browser Compose
service therefore uses the custom seccomp profile but deliberately does not
set `no-new-privileges` or a full capability drop.

Do not copy static-container flags onto a browser deployment without testing
the active sandbox path. Never recover a failed browser launch by setting
`PLAYWRIGHT_DISABLE_SANDBOX=true`.

The seccomp profile permits namespace syscalls required by Chromium. It is not
a network boundary.

## Native and document parsers

HTML extraction and PDF parsing run under bounded admission and worker
concurrency. Cancellation returns control to the request path, but Python
cannot forcibly terminate a native call that never returns. Use multiple
worker processes with health-based recycling or per-job process isolation when
hard-kill containment is required.

## Cache and optional backends

Redis keys bind request semantics, runtime identity, serving configuration, and
credential identities through non-reversible fingerprints. Policy-aware
recursive requests bypass the flat result cache because its envelope cannot
replay the complete redirect, robots, and scope decision chain.

Optional quality output must never remove the deterministic fallback. Do not
cache successful model output without an immutable backend revision. Provider
and model credentials belong in a secret store.

## Known boundaries

- Rate limits, frontier state, robots cache, and singleflight are process-local.
- The built-in frontier is not restart-safe or cross-replica coordinated.
- Aggregate Chromium subresource bytes are not a complete network quota.
- Native parsers do not have a per-call hard-kill process boundary.
- The service does not provide bot-wall evasion, residential proxy rotation,
  or TLS impersonation.
- Quota enforcement for an already authorized tenant belongs at the edge.

## Supported versions

Security fixes are applied to `main`. Pin a reviewed release identity for
production and monitor repository security advisories.
