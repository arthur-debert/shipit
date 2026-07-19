- changelog: new `shipit changelog check-fragment` — a required PR-time gate
  that fails a PR merging to `main` when it adds no `CHANGELOG/unreleased-*.md`
  fragment, with a `skip-changelog` label escape hatch for docs/CI/chore-only
  PRs (#1073). It catches the empty-release miss per-PR, where a fragment can
  still be added, instead of at the next cut once every PR is merged. Self-
  gating and offline: the PR context (base ref, labels) is read from the CI
  runner env, the fragment check from the PR's own git diff — no `gh` auth. Wired
  as a required, PR-only `changelog` lane (`local = false`), so it runs in the
  CI checks matrix but never on a laptop commit/push.
