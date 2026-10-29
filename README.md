# VCoOS: AI Virtual Company Operating System

A Django application in which a human **Founder** supervises a company of AI employees. All employees run on **one shared orchestration engine**, reach models through **one model gateway**, use tools only through a **default-deny tool registry**, and leave a **hash-chained audit trail**.

This release is **Stage 1: the secure foundation plus the end-to-end vertical slice** from spec §26. Everything in the UI is wired to real backend behaviour. Features from later phases are not shown as placeholder buttons; they are listed under [Roadmap](#roadmap).

---

## 1. Architecture

```
 Browser (Django templates, Three.js office, 2D fallback)
   │  HTTPS (session + CSRF)            │  WSS /ws/activity/ (authenticated, dept-filtered, read-only)
   ▼                                    ▼
 Daphne ASGI ── Django views ── apps.core.permissions (server-side, default deny)
                     │
                     ├── apps.work.services   task state machine (conditional UPDATEs, history, audit)
                     │
 Engine worker(s) ───┤   python manage.py run_engine      or Celery (beat → dispatch → claim)
 (apps.orchestration)│      tick: recover expired leases → unblock deps → claim by priority → execute
                     │      execute: build context → [gateway call → tool call]* → deliverable → review
                     │
                     ├── apps.gateway.router  ordered per-employee fallback, capability/privacy/paid/budget/
                     │        │               quota/circuit-breaker checks, bounded backoff, usage records
                     │        └─ adapters: Ollama · OpenAI · Anthropic · Gemini · xAI · NVIDIA · OpenAI-compatible
                     │
                     ├── apps.tools.registry  schemas, scoped grants (deny wins), task-jailed workspace,
                     │        └─ sandbox      isolated Python: rlimits, empty env, audit hook, optional bwrap
                     │
                     └── apps.core.audit      append-only SHA-256 hash chain (unique prev_hash ⇒ no forks)
 Database: SQLite (dev) / PostgreSQL (prod)     Redis: channel layer, cache, Celery broker (optional in dev)
```

**Key rule:** an AI employee is a *database row* (`EmployeeProfile`) with instructions, skills, an ordered model policy and tool grants. The engine is generic. Creating 100 employees creates 100 rows, not 100 engines. Actual concurrency is bounded by `CompanySettings.max_concurrent_tasks`, each employee's `max_concurrent_tasks`, and provider health.

### How a task runs
1. `queue_task` checks approvals (creates an `ApprovalRequest` and waits) and dependencies (→ `blocked`).
2. The worker **claims** it with a conditional `UPDATE … WHERE status='queued'`. Only one worker can win, and the claim sets a **lease**.
3. If no assignee is set, `select_employee` picks one from the department by skill match, then lowest load.
4. Each step starts at a **checkpoint**: cancel, pause, emergency stop, paused employee, runtime limit, lease renewal. Stopping happens only at checkpoints, so an operation is never cut off midway.
5. The gateway returns a JSON action. Tool calls are permission-checked again server-side. Tool output is fed back wrapped as **untrusted data**.
6. The final output is stored as an `Artifact`, and the task moves to `under_review` (or `completed`). Rejecting a review creates a **revision task** that carries the feedback.
7. If a worker dies, its lease expires. **Recovery** re-queues idempotent tasks and holds non-idempotent ones in `waiting_input` for a human.

### Data model (main relationships)
```
User ─┬─ PermissionAssignment (perm, dept?, project?)          CompanySettings (singleton: emergency stop,
      └─ Notification                                            paid switch, limits, CEO appointment)
Department ─┬─ EmployeeProfile ─┬─ EmployeeModelPolicy(priority) ─ AIModel ─ ModelProvider
            │                   ├─ ToolPermission(scope=employee) ─ ToolDefinition
            │                   └─ RoleTemplate
            ├─ Task ─┬─ TaskDependency ─ Task      ├─ TaskExecution ─┬─ ToolCall
            │        ├─ TaskStatusChange           │                 └─ Artifact
            │        ├─ Review / ApprovalRequest   └─ UsageRecord (tokens, est./billed cost, fallback)
            └─ Project ─ Milestone / ProjectProposal
Budget (company|department|employee|project|provider × daily|monthly|total)   AuditEvent (hash chain)   Incident
```

### Repository layout
| Path | Contents |
|---|---|
| `config/` | settings (all secrets from env), ASGI/Channels routing, Celery app, test settings |
| `apps/core/` | accounts, company, departments, employees, roles, permissions, budgets, audit, backups, notifications, main views |
| `apps/gateway/` | providers, models, policies, usage, adapters, router, credential encryption |
| `apps/tools/` | tool registry, grants, sandbox |
| `apps/work/` | projects, tasks, executions, reviews, approvals, task lifecycle service, events |
| `apps/orchestration/` | the engine, the worker command, Celery tasks |
| `apps/office/` | office state API, WebSocket consumer, office and monitor views |
| `templates/`, `static/` | UI (light and dark themes, responsive) and the Three.js office |
| `tests/` | 78 tests, including the full §26 scenario |
| `deploy/` | systemd units, nginx config, backup timer |

---

## 2. Local development

Requires Python 3.12+ (tested on 3.13).

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
export VCOOS_DEBUG=1
python manage.py migrate
python manage.py seed_vcoos --with-departments     # 36 role templates, tool registry, 13 office rooms
python manage.py create_founder founder             # prompts for a password
```

Run two processes:
```bash
daphne -p 8000 config.asgi:application      # web + WebSockets  → http://localhost:8000
python manage.py run_engine                 # the orchestration engine worker
```

Without Redis, live WebSocket events only reach browsers attached to the same process. The office and monitor also poll every 8 s, so they stay correct either way. Set `VCOOS_REDIS_URL` for instant cross-process updates.

### Connecting a local model (Ollama)
```bash
ollama serve &           # on the same host, default http://localhost:11434
ollama pull qwen2.5:7b   # or any model that follows JSON instructions well
```
In the UI:
1. **AI models → Add provider** with kind *Ollama*, URL `http://localhost:11434`, and *local* ticked.
2. **Run health check**, then **Discover local models**.
3. **Add model**. Tick *JSON output* only if the model really supports it, then **Approve** it.
4. **Employees → New employee** (pick a role template), then **Model priority → Add**, and grant tools on the **Tools** tab.
5. **Tasks → New task → Create & submit**. The engine picks it up, and you review it on the task page.

Small models sometimes return malformed JSON. The gateway treats that as invalid output: it falls back to the employee's next model, and if every model fails, the task's retry policy applies. Each model is tried at most once per call.

### Cloud providers
Add the provider with an API base URL and either a **credential reference** (`env:OPENAI_API_KEY`, preferred) or a key pasted into the encrypted field. Keys are Fernet-encrypted with `VCOOS_ENCRYPTION_KEY`, never rendered back, and redacted from logs and audit details. A consumer chat subscription does **not** grant API access. Mark providers that bill as **paid**: paid models stay unusable until the Founder enables paid usage, the employee's policy entry allows it, and a company budget with room for the call exists. If any of these is missing, the call is denied.

### Optional: Celery
```bash
export VCOOS_REDIS_URL=redis://localhost:6379/0
celery -A config worker -l info
celery -A config beat -l info
```
Celery is only a transport. Claiming, leases and recovery are the same database-backed logic, so `run_engine` and Celery workers can safely coexist.

---

## 3. Environment variables
See `.env.example` for the full list. The essentials:

| Variable | Purpose |
|---|---|
| `VCOOS_DEBUG` | `1` for development only |
| `VCOOS_SECRET_KEY` | required when DEBUG is off |
| `VCOOS_ENCRYPTION_KEY` | Fernet key for stored provider credentials (required in production) |
| `VCOOS_ALLOWED_HOSTS`, `VCOOS_CSRF_TRUSTED_ORIGINS` | host and origin allowlists |
| `VCOOS_DB_ENGINE=postgres` + `VCOOS_DB_*` | PostgreSQL |
| `VCOOS_REDIS_URL` | channel layer, cache (shared login rate limiting), Celery broker |
| `VCOOS_WORKSPACE_ROOT`, `VCOOS_BACKUP_ROOT` | task workspaces and backups |
| `VCOOS_SANDBOX_WRAPPER` | OS-level sandbox prefix for agent code (strongly recommended) |
| `VCOOS_TASK_LEASE_SECONDS` | worker lease length (default 120) |

---

## 4. Tests
```bash
pytest            # 78 tests, about 30 s
```
Coverage of spec §24: role and scoped permissions; unauthorized page, API and report access; model selection and fallback; rate limits, outages and backoff; invalid credentials; quota exhaustion; the circuit breaker; paid-model gating; budget enforcement; task retries, step limits and cancellation; dependency scheduling and cycle detection; recovery after a worker crash (idempotent and non-idempotent); approvals (CEO and Founder-only); tool grants, explicit deny, schema validation and path traversal; audit events and tamper detection; WebSocket auth and department filtering; sandbox isolation (network, filesystem, subprocess, ctypes, environment, timeout); prioritization and on-hold; report and budget calculations; sensitive Founder-only actions; exclusive claiming and concurrency limits; the emergency stop (before and during execution); backup, verification, tamper detection and restore; prompt-injection handling; and the full §26 scenario end to end through the HTTP views.

The adapters are tested over real HTTP against `tests/fake_llm.py`, a scripted test server. It is only a test fixture and is never used at runtime.

---

## 5. Production deployment (Linux)
1. Create a `vcoos` user. Clone to `/opt/vcoos`, create `.venv`, and run `pip install -r requirements.txt`.
2. Provision PostgreSQL and Redis. Write `/etc/vcoos/vcoos.env` from `.env.example` with mode 600.
3. Run `python manage.py migrate && python manage.py collectstatic --noinput && python manage.py seed_vcoos`, then `create_founder`.
4. Install the units in `deploy/` (`vcoos-web`, `vcoos-engine`, `vcoos-backup.timer`) and the nginx site with TLS.
5. Install bubblewrap and set `VCOOS_SANDBOX_WRAPPER` (see `.env.example`). Running agent code on a dedicated, unprivileged host or VM is recommended.
6. Run `python manage.py check --deploy` and `pip-audit -r requirements.txt` on each release.

**Backups:** `python manage.py backup_db` writes a consistent snapshot with a SHA-256 manifest and verifies it (`pg_dump -Fc` on PostgreSQL, the online backup API on SQLite). Check a file with `backup_db --verify FILE`. Restore SQLite with `backup_db --restore FILE` after stopping the services; restore PostgreSQL with `pg_restore`. Run `manage.py verify_audit` regularly. The timer does both nightly.

---

## 6. Security model (summary)
- **Authentication everywhere.** A middleware allowlist exposes only login, password reset, `/healthz` and static files. APIs answer 401 and WebSockets reject unauthenticated connections.
- **Authorization is server-side.** `apps.core.permissions.can()` is used by views, APIs, the WebSocket consumer and the engine. Founder-exclusive permissions can never be granted to the CEO or Staff. Staff start with nothing.
- **Agents cannot escalate.** There is no tool that edits permissions, models, budgets or audit records. Every tool call is re-checked server-side. Tool and web output is wrapped as untrusted data.
- **Audit integrity.** Events are append-only at the ORM level, hash-chained, and can't fork (unique `prev_hash`). `verify_chain` detects direct database edits.
- **Secrets.** Credentials are environment references or encrypted, never sent to the client, and redacted from logs. The sandbox runs with an empty environment.
- **Fail safe.** No budget means no paid calls. No eligible model means the task is paused and reported. The emergency stop halts claims and tools without deleting history.
- **Sandbox limits.** Without `VCOOS_SANDBOX_WRAPPER`, isolation relies on rlimits, a separate process with an empty environment, and a PEP 578 audit hook. That covers accidents and casual misuse, but it is **not a hard boundary** against a determined attacker. Use bubblewrap, a container or a VM in production.

---

## Roadmap
Done here: **Phase 1** (foundation), **Phase 2** (engine), **Phase 3** (gateway), the **Phase 4** tool registry, permissions, sandbox, approvals and reviews, and **Phase 5** (3D office with a 2D fallback).

Next stages, building on the existing models and services:
- **Phase 4+**: more tools (approved web fetch with SSRF protection, Git repositories per project, test runner, static analysis), plus project- and task-level grant UI.
- **Phase 6**: research proposals, CEO evaluation and Founder override, proposal-to-plan decomposition, the Software Factory lifecycle with Git branches per task.
- **Phase 7**: reports with CSV/PDF export, the knowledge base, AI meetings, notification preferences.
- **Phase 8**: load testing, query optimization, further hardening.
