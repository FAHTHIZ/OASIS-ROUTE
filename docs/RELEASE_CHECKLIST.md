# Release checklist

Every release follows this list. Items marked **(CI)** are enforced on every push to `main` by
[`.github/workflows/ci.yml`](../.github/workflows/ci.yml); the release workflow
([`.github/workflows/release.yml`](../.github/workflows/release.yml)) re-runs the tests, then builds the
artifacts with an SBOM and signed provenance. `make release-check` runs everything that can be automated
locally.

## 1. Code quality

- [ ] **(CI)** `ruff format --check`, `ruff check`, `mypy` (strict on the package).
- [ ] **(CI)** Full test suite on SQLite and on PostgreSQL 16.
- [ ] **(CI)** Load, soak, concurrency and fault-injection tests (`tests/test_resilience.py`,
      `tests/test_quota_concurrency.py`): no overspent pool, no leaked reservation, usage equals successes.
- [ ] **(CI)** Docs stay in sync: every setting and every API path documented, internal links resolve
      (`tests/test_docs.py`).

## 2. Security

- [ ] [THREAT_MODEL.md](THREAT_MODEL.md) reviewed for every change since the last release; new assets,
      boundaries and threats added with a control and a test; a line added to its review log.
- [ ] No open critical or high findings (threat model, dependency audit, container scan).
- [ ] **(CI)** `pip-audit` on the locked runtime dependencies: no known vulnerabilities.
- [ ] **(CI)** License policy (`scripts/check_licenses.py`): no strong-copyleft or unknown runtime licenses.
- [ ] **(CI)** `gitleaks` over the full history: no secrets.
- [ ] **(CI)** Trivy image scan: no fixable HIGH or CRITICAL vulnerabilities.
- [ ] **(CI)** CycloneDX SBOM generated.

## 3. Install and upgrade paths

- [ ] **(CI)** Fresh install from the built wheel with plain `pip`, in production mode against PostgreSQL:
      migrate, bootstrap, serve, readiness, console, authenticated call, worker running
      (`scripts/install_check.sh`).
- [ ] **(CI)** Upgrade from the previous release with live data: run the old version, make a request,
      upgrade in place, then the old Oasis Key, the old encrypted connections and the history all still
      work and the worker is running (`scripts/upgrade_check.sh`). Set `OASIS_UPGRADE_FROM` to the
      previous release tag once one exists.
- [ ] **(CI)** Migrations apply and revert cleanly, and models match migrations (`tests/test_migrations.py`).
- [ ] **(CI)** Docker image builds; `docker compose up` reaches `ready` with `"worker":"running"`.
- [ ] Backup and restore tested against a copy of real data ([OPERATIONS.md](OPERATIONS.md#backup-and-restore)).

## 4. Documentation

- [ ] [CHANGELOG.md](../CHANGELOG.md): move `Unreleased` into the new version with the date.
- [ ] Version in `pyproject.toml` matches the tag (the release workflow refuses otherwise).
- [ ] README feature matrix and known limitations are truthful for this version.
- [ ] Breaking API changes, if any, follow the [versioning policy](API.md#versioning).

## 5. Publish

- [ ] Tag `vX.Y.Z` on `main` after CI is green. The release workflow builds the wheel, sdist, SBOM and
      container image, attaches signed build provenance, pushes the image to GHCR and creates the GitHub
      release from the changelog section.
- [ ] Verify provenance: `gh attestation verify dist/<file> --repo FAHTHIZ/OASIS-ROUTE` and
      `gh attestation verify oci://ghcr.io/fahthiz/oasis-route:<tag> --repo FAHTHIZ/OASIS-ROUTE`.

## Sign-off log

| Date | Version | Result |
|---|---|---|
| 2026-11-17 | 0.1.0 + unreleased | Sections 1–3 pass: lint, types, 270 tests (SQLite and PostgreSQL), pip-audit clean, 47 runtime licenses compatible, gitleaks clean over 30 commits, Trivy clean, SBOM built; wheel install verified on Python 3.12 and 3.14; upgrade 0002 → 0004 verified with live data. Threat model review: T21 added (account data export/erasure). No open critical or high findings. |
| 2026-11-25 | 0.2.0 | Sections 1–3 pass: lint, types, 409 tests (SQLite and PostgreSQL in CI), supply-chain job clean (audit, licenses, secrets, SBOM), container build verified; upgrade from 0.1.0 (schema 0008 → 0012) verified with live data. Imported catalogue entries stay advertised/unverified; no paid route is enabled or used without explicit policy or approval. No open critical or high findings. |
