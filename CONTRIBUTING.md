# Contributing

Thanks for helping improve Oasis Route.

## Ground rules

- Oasis Route discovers public information and uses provider access that the account owner explicitly authorized. Contributions that create provider identities, harvest credentials, bypass access controls, evade quotas or rotate accounts to reset free allocations will not be accepted.
- Provider-specific behaviour belongs in an adapter (see [docs/PROVIDER_ADAPTERS.md](docs/PROVIDER_ADAPTERS.md)), not in routing or quota code.
- Never commit secrets. Tests must be deterministic and must not call the network.
- Report security issues privately as described in [SECURITY.md](SECURITY.md).

## Workflow

1. Set up with `uv sync` and run `uv run oasis-route dev` ([docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)).
2. Make your change with tests. Bug fixes need a regression test.
3. Run `make check` (ruff, mypy strict, full test suite). If you touched SQL or migrations, also run `make test-pg`.
4. Update documentation when behaviour, settings or endpoints change. `tests/test_docs.py` fails if a setting or API path is undocumented.
5. Add an entry under `[Unreleased]` in [CHANGELOG.md](CHANGELOG.md).
6. Open a pull request describing the change and how you tested it.

## Adding a provider

- Confirm the provider's terms allow API access through a self-hosted gateway, and record the evidence (terms URL, date) in the catalogue seed.
- Reuse the OpenAI-compatible adapter with a profile when the provider exposes that API; write a new adapter only when it does not.
- Implement non-billable credential validation, error normalization, usage extraction and rate-limit header parsing, and add contract tests against a local mock.

## Commit messages

Use a short imperative summary line (for example `router: prefer pooled capacity on ties`), followed by detail if needed. By contributing you agree your work is licensed under the [Apache License 2.0](LICENSE).

## Conduct

Participation is governed by the [Code of Conduct](CODE_OF_CONDUCT.md).
