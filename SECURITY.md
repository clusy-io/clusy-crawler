# Security Policy

## Reporting a vulnerability

Please report security issues **privately**. Do not open a public GitHub issue
for a vulnerability.

- Use GitHub's **[Private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing/privately-reporting-a-security-vulnerability)**
  (the **Security** tab → *Report a vulnerability*), or
- email **security@clusy.io**.

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
- The Playwright renderer runs **with the same-origin policy enabled** and a
  request guard that aborts sub-resource loads to non-public IP literals.

Operators should still:

1. **Set `CRAWL4AI_API_TOKEN`** so the endpoints require a bearer token. With no
   token set, every endpoint except `/health` is unauthenticated — only
   acceptable on a trusted private network.
2. **Do not expose the service directly to the public internet** without an
   auth token and, ideally, network egress restrictions.
3. **Leave `CORS_ALLOW_ORIGINS` empty** unless a specific browser origin needs
   access; `*` is discouraged.
4. Run it with **least-privilege egress** — the SSRF guard blocks the common
   cases, but network-level egress policy is defense in depth against novel
   DNS-rebinding or IPv6 edge cases.

## Out of scope

- **Bot-wall evasion.** The crawler does not ship residential proxies or TLS
  impersonation; sites behind Cloudflare/DataDome may block it. That's expected.
- Denial of service from a caller you have *already* authenticated — apply your
  own rate limiting / quotas at the edge.

## Supported versions

The `main` branch receives security fixes. Pin a release tag for production and
watch the repository for advisories.
