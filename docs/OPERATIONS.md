# Operations runbook

Routine operation needs no manual steps: the worker revalidates credentials, probes unhealthy providers, rolls quota windows, reconciles offers, re-wraps credentials after a key rotation, and deletes expired data on its own. This runbook covers the exceptions.

## Health at a glance

| Signal | Where | Meaning |
|---|---|---|
| `GET /healthz` | load balancer | Process is alive. |
| `GET /readyz` | load balancer | Database reachable and migrated. `worker` field: `running`, `degraded` (a job keeps failing or is overdue), `stalled` (no worker heartbeat for `OASIS_ROUTE_WORKER_STALL_AFTER_S`), `disabled`. |
| Console, **Background** tab | administrators | Each job's last success, duration, next run, consecutive failures and last error, with Run now / Pause / Resume. |
| `GET /api/v1/admin/jobs` | automation | Same data as JSON. |
| Notifications of kind `system.worker` | administrators | Raised once (deduplicated) when maintenance stalls or keeps failing. |
| `/metrics` | Prometheus | `oasis_worker_jobs_total{job,outcome}`, `oasis_worker_restarts_total`, `oasis_circuit_open`, `oasis_gateway_requests_total`, `oasis_fallbacks_total`, upstream latency histograms. |

Suggested alerts: `/readyz` failing for 2 minutes; `worker` not `running` for 10 minutes; `increase(oasis_worker_restarts_total[1h]) > 3`; a sustained rise in `oasis_fallbacks_total` or `capacity_exhausted` responses.

## The worker

- In the default `embedded` mode each API process supervises a worker loop and restarts it with backoff if it crashes. In `external` mode run `oasis-route worker` as its own service.
- Each job holds a lease row in `worker_leases`. While a job runs, its lease is renewed every third of `lease_s`, so a long but live job is never taken over. Execution is bounded by the job's timeout (four leases by default), so a hung job is cancelled, recorded as `timeout`, and retried later.
- Failures back off exponentially with jitter (30 s, 60 s, 120 s ... up to `OASIS_ROUTE_WORKER_MAX_BACKOFF_S`). A success resets the schedule to the job's normal interval.
- If a worker process dies mid-job, its lease expires and another worker picks the job up on its next tick. Jobs are idempotent.
- A stopped worker writes `stopped_at` on its heartbeat row; one that vanishes simply stops heartbeating. Either way `/readyz` and the watchdog report `stalled` within `OASIS_ROUTE_WORKER_STALL_AFTER_S`.

**Worker stalled:** check API logs for `worker crashed; restarting` or `worker tick failed` (database connectivity is the usual cause). Restarting the API container always restores it. In `external` mode check that the worker service is running.

**One job keeps failing:** the Background tab shows the error class. `ConnectError` or `TimeoutError` on `discovery.sources` usually means a source site is down; it backs off on its own. Use **Pause** to silence a job while you investigate and **Run now** to verify a fix.

**Pause per project:** `PUT /api/v1/projects/{id}/automation {"paused": true}` (or the button on the Overview page) stops credential checks, health probes and quota refresh for that project only. Routing keeps working.

## Backup and restore

All state is in PostgreSQL. Provider credentials in it are encrypted with the master key, which is not in the database, so back up both, separately.

```bash
# Backup (consistent, online)
docker compose exec -T db pg_dump -U oasis -d oasis_route -Fc > oasis_route-$(date +%F).dump

# Restore into an empty database
docker compose stop api
docker compose exec -T db dropdb -U oasis --if-exists oasis_route
docker compose exec -T db createdb -U oasis oasis_route
docker compose exec -T db pg_restore -U oasis -d oasis_route --no-owner < oasis_route-2026-11-17.dump
docker compose run --rm migrate          # brings an older backup up to the current schema
docker compose start api
curl -s localhost:8080/readyz
```

After a restore, quota reservations that were in flight at backup time are released automatically by `ledger.expire_reservations` within `OASIS_ROUTE_RESERVATION_TTL_S`. Test a restore at least once per quarter.

## Upgrades and migrations

1. Read [CHANGELOG.md](../CHANGELOG.md) for the target version.
2. Back up the database.
3. Pull or build the new image.
4. Apply migrations: `docker compose run --rm migrate` (or your platform's release job). Migrations are forward-compatible with the previous release's code for one version, so rolling deploys are safe.
5. Restart the API: `docker compose up -d api`.
6. Confirm `/readyz` shows the new `schema_revision` and `"worker":"running"`.

Rollback: `oasis-route migrate down --revision <previous>` and redeploy the previous image. Downgrades that drop columns lose the data in them, so prefer restoring the pre-upgrade backup if the new version wrote data you need.

## Master key rotation

1. Generate a new key: `python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"`.
2. Set `OASIS_ROUTE_MASTER_KEY` to the new key and add the old one to `OASIS_ROUTE_PREVIOUS_MASTER_KEYS` (JSON list), then restart. Everything keeps working: new credentials use the new key, existing ones still decrypt with the old one.
3. The `vault.rewrap` job re-wraps every credential's data key under the new master key within an hour. To do it immediately, run `oasis-route rotate-kek`; it prints `{"rewrapped": N, "failed": 0, "remaining": 0}` and exits non-zero if anything remains.
4. When `remaining` is 0, remove the old key from `OASIS_ROUTE_PREVIOUS_MASTER_KEYS` and restart.

Re-wrapping only touches the 32-byte data keys; provider secrets are never decrypted in bulk. A `failed` count means some credential uses a key that is not configured at all: find and restore that key, or have the affected users reconnect.

**Oasis Key pepper:** `OASIS_ROUTE_API_KEY_PEPPER` cannot be rotated in place, because keys are stored only as HMACs. Changing it revokes every Oasis Key. Do it only after a suspected compromise, then issue new keys.

## Incident playbooks

**Suspected Oasis Key leak:** revoke it (`DELETE /api/v1/projects/{id}/keys/{key_id}`, or the console's Oasis Keys tab) and check `GET /api/v1/projects/{id}/audit` and `/usage` for its activity. Rotation issues a replacement in one step.

**Suspected provider credential leak:** revoke the key at the provider first, then `rotate-credential` on the connection (or delete it). Oasis Route never logs or returns provider secrets, so check where else the key was used.

**Stop all traffic for a project:** `PATCH /api/v1/projects/{id} {"kill_switch": true}`. Requests get `503 project_disabled` immediately.

**Provider outage:** nothing to do. Circuit breakers take the provider out of rotation, traffic fails over to other eligible connections, the health probe rechecks it, and the breaker closes after a successful half-open request. The Overview shows **Temporarily degraded** only if no other provider can serve.

**Quota exhausted everywhere:** requests get `429 capacity_exhausted` with `Retry-After` set to the earliest known reset. When a window resets, the first request is admitted as a probe and normal routing resumes. Adding another authorized connection adds capacity; Oasis Route never creates accounts or cycles trials to get more.

## Data retention

| Data | Retention | Job |
|---|---|---|
| Notifications | until `delete_after` (mail facts: `OASIS_ROUTE_MAIL_FACTS_RETENTION_DAYS`) | `retention.records` |
| Idempotency records | 24 hours | `retention.records` |
| Discovery evidence | per-source policy | `retention.evidence` |
| Usage records, route decisions, audit events | kept by default; `OASIS_ROUTE_USAGE_RETENTION_DAYS`, `OASIS_ROUTE_ROUTE_DECISION_RETENTION_DAYS`, `OASIS_ROUTE_AUDIT_RETENTION_DAYS` | `retention.records` |

Deleting a connection destroys its stored credential immediately. `DELETE /api/v1/mail/data` removes all mail-derived notices for a user.

**Export and erasure requests.** `GET /api/v1/projects/{id}/export` and `GET /api/v1/users/{id}/export` return everything stored, without secrets. `DELETE /api/v1/projects/{id}` and `DELETE /api/v1/users/{id}` (body `{"confirm": "<id>"}`) destroy the stored provider credentials first, then delete the rows. The audit events recording what happened are kept until the audit retention removes them. Deleting a user also deletes the projects they own; it refuses if an owned project has other members, or if it would remove the last active administrator.
