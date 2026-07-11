# Reusable workflows ship as stage blocks; scar invariants live in the blocks

shipit publishes its CI workflows at two layers from `arthur-debert/shipit@vN`
(ADR-0010): stage-level reusable workflows (`wf-prepare`, `wf-build`,
`wf-sign-mac`, `wf-publish`, …) as the composable building blocks — per-job
re-run, artifact flow into the release as stages complete, reuse across stacks —
plus one composed `wf-release.yml` that chains them via nested `workflow_call`,
so a standard consumer's caller is a single `uses:` line while odd repos compose
stages directly, sanctioned. The decisive rule: the scar invariants live inside
the blocks they protect, never in the chain. `wf-publish` takes upstream stage
results — plus the plan's stage-liveness facts (`matrix`, `stages`), verbatim —
as explicit inputs and enforces partial-release prevention (ADR-0009 /
workflows.lex §3.3: publish only if every live stage succeeded — a plan-proven
non-live build/bundle may be skipped, the empty-matrix "tag is the release"
shape — and sign succeeded-or-was-skipped);
`assert-bundle` (the right-binary integrity guard) runs at `wf-sign-mac`'s entry
and on `wf-publish`'s unsigned path. We rejected leaving the `needs:` wiring to
consumers — that puts copy-pasted, drift-prone logic in 19 repos, the exact thing
"CI adds routing, never logic" forbids — and rejected a monolith, which loses
composability and cheap re-runs. Consequence: the composed workflow carries zero
logic, so it is act-testable except for the known mac holes.
