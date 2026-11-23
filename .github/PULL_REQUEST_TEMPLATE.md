## What changed?

<!-- Describe the user-facing or developer-facing change. Link a related issue if one exists. -->

## Why?

<!-- What problem does this solve? -->

## Verification

- [ ] `make check` passes (ruff, mypy strict, full test suite).
- [ ] `make test-pg` passes, if SQL or migrations changed.
- [ ] Added or updated focused regression tests.
- [ ] Docs and `CHANGELOG.md` (`[Unreleased]`) updated where behaviour, settings or endpoints changed.
- [ ] If provider limits changed: `scripts/free_tier_capacity.py` and `docs/FREE_TIER_CAPACITY.md` updated with the source URL and check date, and `python scripts/readme_assets.py` re-run.

## Risk and compatibility

- [ ] No change to credential handling, spend policy, data-policy gating or network exposure.
- [ ] Any such change is described below and covered by tests.

**Notes:**
