# Per-stage dispatch: the stage blocks are self-sufficient; the consumer caller stays routing-only

Owner requirement (TOL02-WS09 #780): a chained release (check, prepare, build,
sign, publish, …) must let an operator re-run exactly stage N fresh, by API,
without re-running everything. GitHub's native re-run-failed-jobs only replays
an existing run; ADR-0009's full re-dispatch converges but re-walks every
stage. Neither is "dispatch stage N fresh".

The consumer-side attempt (ADP02-WS06, padz#180) proved the constraint: a
routing-only caller could wire `stage: full | prepare` and nothing more,
because `wf-build` required wf-prepare's outputs (`tag`/`matrix`/`stages` —
underivable in routing-only YAML) and `wf-publish` additionally required four
stage results plus same-run artifact downloads. Wiring prepare→build
consumer-side is exactly the consumer-owned logic ADR-0040 forbids.

## Decision

Make the stage blocks self-sufficient standalone, shipit-side and
@v1-inheritable; bless the routing-only `stage` choice caller as the consumer
dispatch surface.

- Every stage block's plan facts become OPTIONAL. Omitted, an internal `plan`
  job re-derives them at the tag via `shipit release preflight --plan-only`
  (new flag: skip ONLY the secret-presence hard-fail — the plan job runs
  secret-free; presence was the source run's preflight's job, and each
  stage's verb still validates its own names). Same planner, run in the
  block: the ADR-0040 line (derivation lives shipit-side, never consumer
  YAML) holds. The composed chain passes every fact explicitly, so its plan
  jobs are skipped no-ops — no re-derivation tax on the full path.
- The aligned stage-input contract: `prepare` dispatches on `version` (it
  CREATES the tag); `build`/`sign`/`publish` dispatch on `tag` alone
  (ADR-0041 — the tag is the version authority, `v<version>` by
  construction), plus `run-id` on the artifact-consuming stages (`sign`,
  `publish`) naming the SOURCE run whose artifacts feed them. `checks` needs
  no work: wf-checks takes no inputs and plans its own lanes.
  *Amended (#899):* `build` also accepts `run-id` (the prepare run) — its
  standalone-only `carry-notes` job re-uploads `release-notes` from that
  run, making the build run a complete sign source; without it the staged
  relay's sign dispatch (which names the build run) dies at its own
  carry-notes download, the shipit#898 class. The one-source-run rule in
  workflows.lex §8 carries the current statement.
- Standalone `wf-publish` derives stage-result CLAIMS from plan liveness
  (live → success, plan-proven non-live → skipped): the honest statement of a
  re-dispatch — the operator asserts the source run completed its live
  stages. The claims are enforced, not trusted: every implied artifact
  (release-notes, bundle-\*, signed-\*) downloads from the source run and a
  missing one fails loudly — and because a wildcard `signed-*` download
  passes on ANY match, a signed CLAIM is additionally checked PER
  sign-projection entry (`signed-${artifact}-${platform}` enumerated from
  the plan, refused if the source run is missing any), so a partially-signed
  source run can never publish a mixed tree under a success claim. Then the
  verb's scar-#3 gate runs unchanged. On any path with real results
  (composed chain, direct composition) they ride verbatim and nothing is
  translated.

### Alternatives rejected

- **Consumer-side output wiring** — ADR-0040 forbids it, and WS06 proved it
  unwireable anyway.
- **`workflow_dispatch` entry points on the blocks** — the blocks are
  workflow_call-only by contract; a dispatch trigger on the publisher repo
  would run release stages on *shipit*, not the consumer.
- **A generated consumer caller with per-stage dispatch built in** — stays
  open as codegen sugar ON TOP of this shape (the caller it would generate IS
  the blessed caller); this decision requires none of it.

## Consequences

- `wf-build`/`wf-sign-mac`/`wf-publish` each grew a `plan` job — additive to
  the story-42 check-name surface (skipped no-op on every fact-supplied run),
  so existing required checks are untouched.
- `wf-sign-mac`/`wf-publish` declare NO `permissions:` key: a called
  workflow's permissions can only DOWNGRADE the caller's token, and a key
  would strip the `actions: read` a standalone dispatch caller grants for the
  cross-run downloads (which ride the REST API). The composed chain and
  existing direct callers are unchanged — a block declaration could never
  elevate, so callers always had to grant what the stage needs.
- ONE source run per dispatch: a standalone sign re-dispatch makes ITS OWN
  run a complete publish source — it lands signed-\* AND re-uploads the base
  families it did not itself produce (every bundle-\* tree plus
  release-notes, carried from its source run by the `carry-bundles` /
  `carry-notes` jobs), so the follow-up publish names THAT run as its single
  source and finds all three artifact families there. Multi-run stitching is
  unsupported; the converging escape hatch stays the full re-dispatch
  (ADR-0009). The carried duplication (the source run's bundles/notes now
  also live in the sign run) is accepted.
- An `--unsigned` source run re-publishes with `unsigned: true`, or the
  re-derived plan claims a signed path and the signed-\* download fails
  loudly.
- The working contract, the blessed caller shape, and the sharp edges live in
  `docs/dev/workflows.lex §8`; drift guards pin the whole surface
  (`tests/test_release_blocks.py`).
