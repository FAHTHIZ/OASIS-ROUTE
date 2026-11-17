# Security policy

## Supported versions

Security fixes are made on the latest minor release and on `main`.

| Version | Supported |
|---|---|
| 0.1.x | Yes |
| < 0.1 | No |

## Reporting a vulnerability

Please do not open a public issue for security problems.

Report privately through [GitHub security advisories](https://github.com/FAHTHIZ/OASIS-ROUTE/security/advisories/new). Include the affected version or commit, a description, reproduction steps and the impact you expect. Do not include real provider credentials or other people's data.

You can expect an acknowledgement within 3 working days and a first assessment within 7. We coordinate a fix and disclosure date with you, credit you in the advisory unless you prefer otherwise, and aim to release fixes for critical issues within 14 days.

## Scope

In scope: this repository's code, container image and default deployment configuration, including the gateway, credential vault, quota ledger, router, discovery fetcher, worker, console and CLI.

Especially valuable: tenant-isolation bypasses, credential or Oasis Key disclosure, SSRF through connections or discovery, quota-ledger races that allow overspending a provider allocation, authentication or scope bypass, and anything that would make Oasis Route violate a provider's terms.

Out of scope: vulnerabilities in upstream providers, denial of service that needs more traffic than the documented rate limits, and findings that require a compromised host or database.

## Safe harbour

Good-faith research that respects this policy, avoids privacy violations and service disruption, and only uses accounts and credentials you own is welcome; we will not pursue legal action for it.

## Design references

The security model is described in [docs/SECURITY_AND_COMPLIANCE.md](docs/SECURITY_AND_COMPLIANCE.md) and [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md). Operational guidance for key rotation and incident response is in [docs/OPERATIONS.md](docs/OPERATIONS.md).
