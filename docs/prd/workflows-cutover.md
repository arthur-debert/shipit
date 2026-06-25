# workflows + cutover

> Status: **postponed** — not yet started. Planned successor work; see `docs/prd/FUTURE_WORK.md`.
> Origin: scope sketched in the retired roadmap §6, summarized below.

## Intended scope

Deliver the thin reusable workflow (`setup-pixi` + `pixi run ci`) and port the
composable build → package → sign → release jobs from `workflows.lex`:
parametrized command dispatch for the easy 80%, and composable opt-in jobs for the
signing 20%. This is historically where CI complexity has exploded, so the work is
laddered finest here — landing and verifying one job boundary at a time rather
than a single big cutover.

Nothing is built yet. The verification target, once started, is the real cutover:
shipit cuts one REAL release of one real consumer — the artifact inspected for the
right binary, signed and notarized — BEFORE release-core is retired. Only after
that real cut does the second hard rule (keep release-core running until its
replacement is proven on a real artifact) release its hold.
