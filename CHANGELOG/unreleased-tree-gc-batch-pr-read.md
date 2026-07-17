- `tree list` and `tree gc` now **read every Tree's PR state in one `gh` call
  per repo** instead of one per Tree (#1011). The old path issued a `gh pr view`
  for each Tree — ~512 sequential GraphQL calls on a large fleet — which both
  made `list` ~60s (70% of it that call) and, worse, **exhausted the hourly
  GraphQL budget mid-sweep**: once drained, every remaining PR read returned
  UNKNOWN, the ladder conservatively kept them, and a sweep that should have
  reclaimed 371 Trees reclaimed 0. A single `gh pr list --json` per repo replaces
  the whole fan-out with ~a dozen calls, so neither `list` time nor the sweep's
  budget cost scales with fleet size.
