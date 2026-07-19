- changelog: new `shipit changelog check-fragment` — a required PR-time gate
  that fails a PR merging to `main` when `CHANGELOG/` holds no unreleased
  fragment (#1073). It is a fragment-PRESENCE check: it asks the cut's own "are
  there unreleased fragments?" question at PR time, over the same discovery
  machinery the cut uses, so a missing release note is caught per-PR — before
  merge — instead of at the next cut once every PR is already in. Self-gating and
  offline: the base ref is read from the CI runner env, the fragment presence
  from the current checkout's `CHANGELOG/` — no `gh` auth and no CI event
  trigger. Wired as a required, PR-only `changelog` lane (`local = false`), so it
  runs in the CI checks matrix but never on a laptop commit/push; it reads the
  working tree, so it needs no workflow YAML change.
