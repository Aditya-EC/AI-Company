# VCoOS operations runbook

## Services
| Unit | Command | Notes |
|---|---|---|
| `vcoos-web` | `uvicorn config.asgi:application --workers 2 --proxy-headers ...` | HTTP + WebSockets behind nginx. More than one worker requires `VCOOS_REDIS_URL` |
| `vcoos-engine` | `manage.py run_engine` | Orchestration worker. Run on one or more hosts; claims are row-locked. SIGTERM lets tasks reach a checkpoint |
| (optional) Celery | `celery -A config worker` + `celery -A config beat` | Alternative transport; same claim logic |
| `vcoos-backup.timer` | `backup_db` then `verify_audit` | Nightly |
| `vcoos-maintenance.timer` | `apply_retention` | Daily |

## First deployment checklist
1. Provision PostgreSQL 14+ and Redis. Create `/etc/vcoos/vcoos.env` from `.env.example` with mode 600, owned by `root:vcoos`.
2. Generate secrets:
   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(64))"
   ```
   ```bash
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```
   Use the first for `VCOOS_SECRET_KEY` and the second for `VCOOS_ENCRYPTION_KEY`, and set `VCOOS_HEALTH_TOKEN`.
3. Install: `pip install -r requirements.txt`, then run `migrate`, `collectstatic --noinput`, `seed_vcoos`, and `create_founder <name>`.
4. Set `VCOOS_SANDBOX_WRAPPER` (see `.env.example`). Confirm `unshare --net --map-root-user true` succeeds for the `vcoos` user.
5. Run `python manage.py vcoos_preflight`. It must report **0 failed**; review every warning.
6. Enable the units and timers in `deploy/`, plus the nginx site with TLS.
7. Check readiness:
   ```bash
   curl -H "Authorization: Bearer $VCOOS_HEALTH_TOKEN" https://host/healthz/ready
   ```

## Monitoring
- **Liveness** `/healthz` (public): database reachable.
- **Readiness** `/healthz/ready` (token or Founder): database, migrations, cache, channel layer, **live engine workers** (heartbeat within 60 s; fails if work is queued with no worker), disk ≥ 10% free, providers, and the emergency stop. Returns 503 when not ready.
- **Metrics** `/metrics` (Prometheus text, same authorization): `vcoos_tasks{status}`, `vcoos_employees{status}`, `vcoos_workers_alive`, `vcoos_model_calls_total{success}`, `vcoos_model_fallback_calls_total`, `vcoos_estimated_cost_usd_total`, `vcoos_provider_unhealthy{provider}`, `vcoos_pending_approvals`, `vcoos_open_incidents`, `vcoos_emergency_stop`.
- Suggested alerts: `vcoos_workers_alive == 0` with queued tasks; any `vcoos_provider_unhealthy == 1` for 15 minutes; `vcoos_emergency_stop == 1`; `vcoos_open_incidents > 0`.
- The dashboard also shows engine worker status.

## Backups and restore (rehearsed)
- `backup_db` writes a `pg_dump -Fc` archive (or a SQLite online snapshot) plus a SHA-256 manifest, then verifies it. On PostgreSQL, verification parses the whole archive table of contents (`pg_restore --list`) and confirms the audit table is present.
- Verify any backup:
  ```bash
  python manage.py backup_db --verify FILE
  ```
- **Restore drill (do this quarterly):**
  1. Restore into a scratch database:
     ```bash
     createdb vcoos_drill
     ```
     ```bash
     VCOOS_DB_NAME=vcoos_drill python manage.py backup_db --restore FILE
     ```
  2. Check the restored data:
     ```bash
     VCOOS_DB_NAME=vcoos_drill python manage.py verify_audit
     ```
  3. Spot-check counts, then remove the drill database:
     ```bash
     dropdb vcoos_drill
     ```

  The automated test `test_postgres_backup_restore_rehearsal` performs this cycle (including audit-chain verification of the restored database, and rejection of a truncated archive).
- **Real restore**: stop `vcoos-web` and `vcoos-engine`, run `backup_db --restore FILE` (this drops and recreates objects), run `verify_audit`, then start the services. Interrupted tasks are recovered automatically from their expired leases.
- Copy backups off-host. A backup on the same disk is not a backup.

## Data retention
`apply_retention` (daily; use `--dry-run` to preview) removes read notifications, report export files, the workspaces of finished non-repository tasks, and heartbeats older than 7 days. The windows are configured in Company settings. **Never removed:** audit events, tasks, executions, tool calls, reviews, decisions, proposals, knowledge versions, and project repositories.

## Scaling
- Engine throughput is bounded by `max_concurrent_tasks` (company), each employee's limit, and model latency. Add `run_engine` processes or hosts freely: claims are serialized by row locks, so duplicate execution is impossible (load-tested with 8 concurrent workers).
- Key pages issue a constant number of queries regardless of company size (enforced by `tests/test_query_counts.py`).

## Upgrades
1. `backup_db`.
2. `git pull`, then `pip install -r requirements.txt`.
3. `migrate`, then `collectstatic --noinput`.
4. Restart the units. Tasks that were running resume from lease recovery.
5. `vcoos_preflight`.
6. `pip-audit -r requirements.txt` on every release.

## Incident response
- **Stop everything now:** Company settings → *Activate emergency stop*. New claims and all tool use stop immediately; running tasks pause at their next checkpoint; meetings end blocked. History is kept.
- **Provider credential leak:**
  1. Disable the provider (AI models → provider → uncheck *enabled*).
  2. Revoke the key at the provider.
  3. Store the new key, or update the `env:` reference.
- **Encryption key rotation:** set the new `VCOOS_ENCRYPTION_KEY`, then run:
  ```bash
  VCOOS_OLD_ENCRYPTION_KEY=<old> python manage.py rotate_encryption_key
  ```
  It is atomic and idempotent, and aborts if any credential decrypts with neither key.
- **Suspected tampering:** run `verify_audit`. The first broken event id shows where. Restore from the last backup whose chain verifies.
- **Repeated tool denials** create a `security` incident automatically. Review the task's execution and tool calls, then suspend the employee if needed.
