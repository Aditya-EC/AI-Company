# VCoOS security model and review

## Threat model
| Actor | Assumed capability | Main controls |
|---|---|---|
| Anonymous internet user | Reaches public pages | Login-required middleware (allowlist: login, password reset, `/healthz`, static); rate-limited login and password reset; CSRF; strict CSP |
| Authenticated staff | Uses their granted permissions | Server-side default-deny `can()` / `scope_queryset()` on every view, API, WebSocket, report download; department/project scoping |
| CEO (human or AI) | Operational authority | Founder-exclusive permission set; CEO approval limit; escalation; Founder override is always recorded |
| AI employee / model output | Produces arbitrary text, may be prompt-injected | Agents act only through the engine; tools are default-deny and re-checked server-side on every call; tool and web output is wrapped as untrusted data; no tool can change permissions, models, budgets or audit records; repeated denials raise a security incident |
| Agent-generated code | Arbitrary Python | Sandbox: separate process, empty environment, rlimits, wall timeout, audit hook (network, subprocess, ctypes, filesystem outside the workspace, `.git` writes), plus the **OS-level wrapper** (`VCOOS_SANDBOX_WRAPPER`) |
| External web content | Hostile pages or redirects | `web.fetch`: http(s) only; public addresses only, checked before **and at connect time** (DNS rebinding); every redirect hop is re-checked; size, time and redirect limits; optional domain allowlist |
| Compromised database row | Edited audit records | Hash chain with a unique `prev_hash` (no forks); `verify_audit`, preflight, and the nightly timer detect edits, deletions and reordering |

## Review (Phase 8) — findings and fixes
| # | Finding | Severity | Status |
|---|---|---|---|
| 1 | Daphne → Twisted had 8 known CVEs. Two affect the HTTP server path: CVE-2026-115877 (header injection), and HTTP/2 CPU exhaustion when HTTP/2 is served directly. No stable fix exists | High | **Fixed**: replaced Daphne with Uvicorn; `pip-audit` now reports no known vulnerabilities |
| 2 | Claim race: concurrent workers could exceed the per-employee and company concurrency limits | Medium | **Fixed**: claims lock the company and employee rows and re-check inside the transaction; load test with 8 workers on PostgreSQL and SQLite |
| 3 | `web.fetch` DNS rebinding window between the pre-check and the connection | Medium | **Fixed**: guarded connection classes verify the peer IP. Through an outbound proxy, the proxy must filter destinations too (see Limitations) |
| 4 | No Content-Security-Policy | Medium | **Fixed**: per-request nonce for inline scripts, one pinned CDN, `object-src 'none'`, `frame-ancestors 'none'`, `form-action 'self'`; inline event handler removed |
| 5 | Password reset not rate-limited (mail abuse) | Low | **Fixed**: per-IP and per-address limits; same response either way (no account enumeration) |
| 6 | Colour fields placed in style attributes without validation | Low | **Fixed**: hex validators. Output was already escaped |
| 7 | SQLite "database is locked" under concurrent workers | Low (dev only) | **Fixed**: `IMMEDIATE` transactions + WAL |
| 8 | Employee directory issued one query per row (N+1) | Low (performance) | **Fixed**: prefetch; query-count test over 13 pages |

Also checked, with no issue found: no `|safe`, `mark_safe`, `csrf_exempt`, raw SQL, `eval` or `shell=True`; all client `innerHTML` interpolations escape; object-level checks on every `get_object_or_404`; report downloads re-check access; Git runs with hooks disabled and argument-only commands; WebSocket origin validation (tested live against Uvicorn: a foreign origin is rejected).

## Known limitations (accepted, documented)
- **Sandbox without `VCOOS_SANDBOX_WRAPPER`**: the audit hook is not a hard boundary against a determined attacker. Always set the wrapper in production. `unshare --net --map-root-user` is tested to remove all network access. bubblewrap (filesystem + PID isolation) is stronger, but validate it on your host first.
- **Agent-written tests** can be weak or wrong. The test runner cannot be fooled by `os._exit(0)` or a printed fake summary, but human review remains required.
- **Outbound proxy**: when `HTTPS_PROXY` is set, `web.fetch` connects to the proxy, so the connect-time IP check applies to the proxy. Configure the proxy to deny private, link-local and metadata ranges.
- **Open WebSocket connections** of a user who is deactivated stay open until they reconnect. They are read-only and receive only events within the user's scope at connect time.
- **Celery and `run_engine`** share the database-backed claim logic, so either or both can run. Celery uses `acks_late` and the claim is idempotent.
- **Provider billing**: costs are estimates unless billed costs are recorded; reports label them separately.

## Reporting and response
See [OPERATIONS.md](OPERATIONS.md#incident-response) for the emergency stop, credential revocation and key rotation.
