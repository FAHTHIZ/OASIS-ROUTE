# Deployment

The supported production shape is one or more stateless API containers in front of PostgreSQL. Background maintenance (discovery, quota refresh, provider health probes, credential revalidation, offer reconciliation, master-key re-wrap and retention) runs automatically inside the API process. Nothing else has to be scheduled.

## Docker Compose (recommended starting point)

```bash
git clone https://github.com/FAHTHIZ/OASIS-ROUTE.git && cd OASIS-ROUTE
cat > .env <<EOF
POSTGRES_PASSWORD=$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')
OASIS_ROUTE_MASTER_KEY=$(python3 -c 'import os,base64;print(base64.b64encode(os.urandom(32)).decode())')
OASIS_ROUTE_API_KEY_PEPPER=$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')
EOF
chmod 600 .env
docker compose up -d --build
docker compose run --rm api bootstrap --email you@example.com
```

`bootstrap` prints the administrator's Oasis Key once. Store it in a password manager.

What `docker compose up` does:

1. `db`: PostgreSQL 16 with a persistent `pgdata` volume.
2. `migrate`: applies migrations once, then exits. The API waits for it.
3. `api`: serves the gateway, management API and console on `127.0.0.1:8080` and supervises the background worker. The container runs as a non-root user with a read-only filesystem, no Linux capabilities and `no-new-privileges`.

Check it:

```bash
curl -s localhost:8080/readyz      # {"status":"ready","schema_revision":"0004","worker":"running"}
```

Then open `https://<your-host>/console/`, sign in with the Oasis Key, and add a provider API key under **Connections**. Validation, model synchronization, quota learning and routing set themselves up; the Overview shows **Ready** when a request can be served.

**Back up the master key.** `OASIS_ROUTE_MASTER_KEY` encrypts every stored provider credential. If it is lost, those credentials cannot be recovered and users must reconnect. Keep it in a secret manager separate from database backups.

## TLS and the reverse proxy

The compose file binds to localhost on purpose. Put a TLS-terminating proxy in front (Caddy, nginx, Traefik, a cloud load balancer):

- Forward `X-Forwarded-For` and `X-Forwarded-Proto` (uvicorn runs with proxy headers enabled).
- Disable response buffering for `/v1/chat/completions` so streams flow (`proxy_buffering off;` in nginx). Oasis Route also sends `X-Accel-Buffering: no`.
- Allow long reads (at least `OASIS_ROUTE_UPSTREAM_READ_TIMEOUT_S`, default 120 s).
- Do not expose `/metrics` publicly; scrape it from the private network.

Minimal Caddyfile:

```text
route.example.com {
  @metrics path /metrics
  respond @metrics 404
  reverse_proxy 127.0.0.1:8080 {
    flush_interval -1
  }
}
```

## Scaling

- **More API capacity:** run more `api` replicas behind the proxy. Every replica supervises a worker, and leases in the database ensure each job runs on only one of them at a time, so replicas never duplicate work.
- **Dedicated worker:** for large installs, set `OASIS_ROUTE_WORKER_MODE=external` on the API and start the worker service with `docker compose --profile scale up -d`. Any number of workers is safe.
- Per-key rate limits are global on PostgreSQL: the counters live in the database (`OASIS_ROUTE_RATE_LIMIT_BACKEND=auto`), so N replicas still enforce `OASIS_ROUTE_RATE_LIMIT_PER_KEY_RPM` once, not N times. Quota reservations are always global and atomic.

## Other platforms

The image is a normal twelve-factor container:

| Setting | Value |
|---|---|
| Image | build from `Dockerfile`; entrypoint `oasis-route` |
| Command | `serve` (API + worker). Run `migrate up` as a release step or init job before rolling out. |
| Port | `8080` |
| Liveness | `GET /healthz` |
| Readiness | `GET /readyz` |
| Required env | `OASIS_ROUTE_DATABASE_URL`, `OASIS_ROUTE_MASTER_KEY`, `OASIS_ROUTE_API_KEY_PEPPER` |
| Filesystem | read-only is fine; nothing is written locally |
| Graceful stop | `SIGTERM`; in-flight requests finish and the worker releases its leases |

On Kubernetes, run `migrate up` as a `Job` (or Helm pre-upgrade hook), deploy `serve` as a `Deployment` with the probes above, and keep secrets in a `Secret` or external secret store. On a single VM without containers, install with `pip install 'oasis-route[otel]'` and run `oasis-route serve --migrate` under systemd with `Restart=always`.

## Production checklist

- [ ] `OASIS_ROUTE_ENVIRONMENT=production` (the container image default), PostgreSQL URL, master key and pepper set from a secret store.
- [ ] Master key backed up separately from database backups.
- [ ] TLS in front, `/metrics` private, `OASIS_ROUTE_ALLOW_PRIVATE_UPSTREAMS` left `false`.
- [ ] `OASIS_ROUTE_ADMIN_BOOTSTRAP_TOKEN` unset after the first administrator exists (the CLI `bootstrap` command does not need it).
- [ ] Database backups scheduled and a restore tested ([OPERATIONS.md](OPERATIONS.md#backup-and-restore)).
- [ ] `/readyz` reports `"worker":"running"`.
- [ ] Gmail left disabled unless the compliance attestation is complete.
