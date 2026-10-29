# VCoOS: AI Virtual Company Operating System

A Django application in which a human **Founder** supervises a company of AI employees. All employees run on **one shared orchestration engine**, reach models through **one model gateway**, use tools only through a **default-deny tool registry**, and leave a **hash-chained audit trail**.

Implemented so far: **Stage 1** (the secure foundation and the end-to-end vertical slice from spec §26), **Stage 2 / Phase 6** (research proposals, CEO evaluation with Founder override, planning, project priorities and the Software Factory), and **Stage 3 / Phase 7** (reports and exports, the knowledge base, AI meetings and notification preferences). Everything in the UI is wired to real backend behaviour. Features from later phases are not shown as placeholder buttons; they are listed under [Roadmap](#roadmap).

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
| `apps/knowledge/` | knowledge base (documents, versions, review rules, agent context) and AI meetings |
| `apps/reports/` | report calculations, CSV/PDF export, export jobs |
| `tests/` | 129 tests, including the full §26 scenario and the Phase 6–7 workflows |
| `deploy/` | systemd units, nginx config, backup timer |

### Company workflow (Phase 6)
```
Founder strategy ─► Commission research (task, output_kind=proposal)
                     └─ researcher uses web.fetch ─► structured proposal (validated; invalid output is sent back to the same model)
                          evidence marked "retrieved" only if the system saw the fetch
CEO evaluates ─► approve (≤ ceo_approval_limit_usd, needs a cost estimate) / reject / defer / revision / escalate
Founder ─► decides escalations, can override any decision (recorded as an override; reversing an approval cancels the project)
Approval ─► Project (+ Git repository) + planning task (output_kind=plan)
Planner ─► milestones + tasks + dependencies (validated: departments, keys, cycles, acceptance criteria) ─► draft tasks
CEO/Founder ─► activate plan ─► tasks queued or blocked on dependencies
Developers ─► work on branch task-<id> in an isolated worktree ─► tests.run evidence ─► review ─► merge to main on accept
Release ─► readiness checklist from recorded evidence ─► Founder approval (deployment approval)
```
- **Priority changes** cascade to a project's open tasks, require a reason, never interrupt running work, and record their effects (tasks updated, running tasks left alone, queue position before and after) as a `Decision`.
- **Pausing a project** stops its running tasks at the next safe checkpoint and blocks new claims. Resuming re-queues the tasks the pause stopped.
- **Repositories:** Git runs server-side with hooks disabled and a minimal environment. Agents can't write `.git` metadata (tool check plus sandbox hook), and each worktree's pointer is verified before use. A merge conflict blocks acceptance and leaves `main` untouched.
- **Test evidence:** `tests.run` and "Run tests on main" execute the unittest suite in the sandbox. A run counts as passing only with exit code 0 **and** a summary line carrying a per-run random nonce, so `os._exit(0)` or a printed fake summary doesn't pass. Agent-written tests can still be weak, which is why human review is still required.
- **Release readiness** checks: repository exists, plan activated, all tasks completed, every reviewed deliverable accepted, and the test suite passed on the *current* `main` commit. An approved release is **authorized for deployment, not deployed**. Running deployments needs a deployment integration, which isn't included.
- **Software policy** (Company settings): permitted categories, languages and frameworks, licensing, testing and security rules, repository conventions, web domain allowlist, and the CEO approval limit. Policies are given to planners and developers, and the category policy is enforced at approval.

### Knowledge, meetings, reports and notifications (Phase 7)
- **Knowledge base** (`apps/knowledge`):
  - Versioned documents with access levels: company, department, project, or leadership (Founder and CEO only).
  - Readers and agents only ever get a document's **approved** version. Anything written by an AI employee (`knowledge.propose`) or taken from an external source starts as a pending version.
  - Policies, coding standards and procedures can only be approved by the **Founder**. Nobody but the Founder approves their own version.
  - Agents get relevant approved documents automatically in their task context (scoped to their department and the task's project, never leadership), and can use `knowledge.search` / `knowledge.read`.
- **AI meetings:**
  - Each meeting has a purpose, agenda, 2–8 participants (each on its own model policy), a facilitator, a round limit (up to 4) and a call budget.
  - Participants can end early by all signalling they're ready to conclude. The facilitator's outcome is validated (owners must be participants), with one correction attempt.
  - A meeting with no decision or action ends as **blocked** with the reason recorded.
  - Action items become **draft** tasks that a person submits. Notes are stored as a non-authoritative system record.
  - Meetings run on the same engine workers (`run_engine` or Celery). They respect the emergency stop, and are recovered as blocked if their worker dies.
- **Reports** (`/reports/`):
  - Company, department, employee and financial / AI-usage reports, with date, department, project and employee filters.
  - Export to CSV (with spreadsheet formula injection neutralized) or PDF.
  - Access is checked server-side per report kind and **again at download**. Company and financial reports need company-wide `view_reports`.
  - Employee performance is reported across several dimensions (output, review acceptance, rework, failures, timeliness, effort), never as one count-based score. Estimated costs are kept separate from verified billed costs.
- **Notifications:**
  - Per-user, per-kind in-app and email preferences. Security, provider-credential, approval and escalation notifications can't be switched off in-app.
  - Email is sent only when the Founder enables it (Company settings) and is redacted.
  - New triggers: budget at 80% (once per period), deadline risk (once per task), repeated tool denials (a security incident), CEO decisions (to the Founder), emergency stop.

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
| `VCOOS_WORKSPACE_ROOT`, `VCOOS_BACKUP_ROOT`, `VCOOS_REPORT_ROOT` | task workspaces, backups and report exports |
| `VCOOS_SANDBOX_WRAPPER` | OS-level sandbox prefix for agent code (strongly recommended) |
| `VCOOS_TASK_LEASE_SECONDS` | worker lease length (default 120) |

---

## 4. Tests
```bash
pytest            # 129 tests, about 1 minute
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
Done here: **Phase 1** (foundation), **Phase 2** (engine), **Phase 3** (gateway), **Phase 4** (tool registry, permissions, sandbox, approvals, reviews, plus web.fetch, git.* and tests.run), **Phase 5** (3D office with a 2D fallback), **Phase 6** (proposals, CEO and Founder governance, planning, priorities, Software Factory, releases), and **Phase 7** (reports, knowledge base, meetings, notifications).

Next stages, building on the existing models and services:
- **Phase 6+**: static analysis and dependency/licence scanning tools, project- and task-level tool-grant UI, deployment integrations (executing approved releases), structured AI meetings for architecture and release reviews.
- **Phase 8**: load testing, query optimization, further hardening.
