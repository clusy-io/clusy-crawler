# Security Policy

## Reporting a vulnerability

Please report security issues **privately**. Do not open a public GitHub issue
for a vulnerability.

- Use GitHub's **[Private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing/privately-reporting-a-security-vulnerability)**
  (the **Security** tab → *Report a vulnerability*), or
- email **hi@clusy.io**.

We aim to acknowledge within 3 business days and to ship a fix or mitigation for
confirmed high-severity issues within 14 days. We're happy to credit reporters
in the release notes unless you prefer to stay anonymous.

## Threat model — read this before self-hosting

This service **fetches arbitrary, caller-supplied URLs**. That makes it a
potential **SSRF (Server-Side Request Forgery)** vector by design: a caller who
can reach your instance can ask it to make HTTP requests on their behalf.

The service ships with defenses on by default:

- **SSRF guard** (`app/services/fetcher.py`) resolves every URL — and every
  redirect hop — and refuses any that resolve to a non-public address: private
  RFC1918 ranges, loopback, link-local (including the `169.254.169.254` cloud
  metadata endpoint), unique-local IPv6, multicast, reserved, and the
  unspecified address. IPv4-mapped IPv6 is unwrapped before the check.
- **Redirects are followed manually** with `follow_redirects=False` so each hop
  is re-validated — a public URL cannot 302-redirect into your private network.
- **Scheme allow-list**: only `http`/`https`.
- **Response size cap** on the decompressed body (defends against
  decompression bombs).
- The Playwright renderer runs **with Chromium's sandbox and same-origin policy
  enabled**, creates and destroys a fresh browser context for every render,
  blocks service workers and WebSockets, and validates document and subresource
  destinations before allowing a request.

Operators should still:

1. **Set `CRAWL4AI_API_TOKEN`** so crawl/extraction endpoints require a bearer
   token. Health diagnostics and OpenAPI discovery remain public; with no token,
   the data endpoints are unauthenticated and safe only on a trusted network.
2. **Set an independent `SERVING_FINGERPRINT_KEY`** with at least 32 diverse
   characters. Production mode requires it, and it must differ from the bearer
   token. It keys the public serving-configuration fingerprint so
   secret-bearing state can be bound without exposing the underlying values.
3. **Do not expose the service directly to the public internet** without an
   auth token and, ideally, network egress restrictions.
4. **Leave `CORS_ALLOW_ORIGINS` empty** unless a specific browser origin needs
   access; `*` is discouraged.
5. Run it with **least-privilege egress** — the SSRF guard blocks the common
   cases, but network-level egress policy is defense in depth against novel
   DNS-rebinding or IPv6 edge cases.
6. Keep the container **non-root** and use the checked-in
   `seccomp_profile.json`. The profile is Docker's default policy plus the
   user-namespace syscalls required for Chromium's sandbox. When Playwright is
   enabled, do not add `no-new-privileges` or drop every Linux capability:
   either setting disables Chromium's version-matched SUID sandbox fallback on
   hosts that restrict unprivileged user namespaces. Do not set
   `PLAYWRIGHT_DISABLE_SANDBOX=true` for ordinary crawling.

Application checks are not a complete network sandbox. Chromium request
destinations are resolved and filtered before they are allowed, but DNS can
change between that check and the browser's connection. The renderer also caps
the final DOM/HTML rather than the aggregate bytes of every script or XHR.
Production deployments should enforce an egress policy that denies private,
loopback, link-local, and cloud-metadata ranges and should bound container
network usage.

Native HTML extraction and PDFium parsing run in bounded worker threads.
Cancellation and request deadlines return control without allowing unbounded
new workers, but Python cannot forcibly terminate a native call that never
returns. Deploy multiple worker processes with health-based recycling if
hard-kill isolation for hostile HTML/PDF inputs is required.

## Out of scope

- **Bot-wall evasion.** The crawler does not ship residential proxies or TLS
  impersonation; sites behind Cloudflare/DataDome may block it. That's expected.
- Denial of service from a caller you have *already* authenticated — apply your
  own rate limiting / quotas at the edge.
- Cross-replica rate limiting, persistent crawl queues, and restart recovery.
  Recursive requests enforce robots policy, but the frontier and limiter remain
  process-local.

## Supported versions

The `main` branch receives security fixes. Pin a release tag for production and
watch the repository for advisories.
