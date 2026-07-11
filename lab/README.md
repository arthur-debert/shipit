# The Review Lab's Ground-truth fixture

`lab/fixture.toml` is the **versioned, in-repo Ground-truth fixture**
(ADR-0048; `docs/spec/review-lab.md`): pinned historical portfolio PR ranges
plus the evidence-backed labels that review experiments are scored against.
The scorer that reads it (`shipit eval score`) is fully deterministic — file +
line-range + normalized claim-token overlap, aliases honored, no LLM anywhere
in the instrument.

## Format (schema 1)

Written and rewritten canonically by `shipit.review.groundtruth` — hand edits
are legal but must re-load (`shipit eval score` validates loudly), and hand
comments do not survive a programmatic save.

```toml
schema = 1        # file-format version (parser compatibility)
version = 7       # LABEL-SET version: bumps on every label/alias change

[[prs]]           # one pinned historical PR range
id = "core-440"                 # stable handle labels reference
repo = "phos-editor/core"       # owner/name — the round-record store key
pr = 440
base_sha = "…full sha…"          # the exact range replays review
head_sha = "…full sha…"          # round-1 head for in-PR-fixed defects
title = "…"                      # informational
language = "rust"                # informational (the corpus must span these)
notes = "…"

[[labels]]        # one Ground-truth label
id = "core-G1"
pr = "core-440"
file = "phos-bench/src/bin/gpu_compare.rs"
lines = [100, 160]              # inclusive, at the PINNED head; omit = file-scoped
severity = "major"              # the one 4-tier ladder (critical|major|minor|nit)
verdict = "real"                # or "not-real" — a banked refutation (measurable FP)
confirmed = true                # human-confirmed; false = candidate, never scored
claim = "one sentence: mechanism → consequence"
aliases = ["banked alternate phrasing"]
[labels.provenance]
kind = "fix-commit"             # fix-commit | confirmed-thread | adjudication
ref = "f211ab3"                 # sha / thread URL / adjudication pointer
```

Rules the parser enforces: every label references a pinned `pr`; ids are
unique; SHAs are hex (≥7 chars); `lines` is `[start, end]` with `start ≤ end`.

## Versioning

`version` bumps whenever the label set changes (new label, new alias). Every
scored report names the fixture version it ran against; **numbers scored
against different versions are never comparable**. `schema` only changes when
the file format itself does.

## Confirmation

Only `confirmed = true` labels enter any metric. A label is confirmed by a
HUMAN — either at the labeling session (scout agents mine fix-commit
archaeology, a normalizer compresses to this format, the maintainer confirms)
or through Adjudication. `confirmed = false` entries are scout-mined
**candidates** awaiting that verdict; flip the flag (and bump `version`) only
after actually ruling on them.

## Banking (Adjudication)

`shipit eval score` surfaces two adjudication feeders; each is ruled on ONCE
and banked, and the fixture absorbs the semantics over time:

- **Unmatched emission** — the corpus does not know the claim. If real, it is
  a recall the fixture was blind to; if not-real, banking it makes that false
  positive measurable forever after:

  ```sh
  shipit eval bank label --id core-A1 --pr core-440 \
      --file phos-editor/src/eval.rs --lines 50:60 \
      --severity major --verdict not-real \
      --claim "Backend::Cpu is used without being imported" \
      --provenance "adjudication:issue-638 T7 rebuttal"
  ```

- **Near-miss** — right file, overlapping lines, wording the lexicon does not
  know. If it names the same defect, bank the phrasing as an alias:

  ```sh
  shipit eval bank alias core-G1 --text "staging buffer math misses row padding"
  ```

Both rewrite `lab/fixture.toml` canonically and bump `version`; commit the
diff through the normal PR flow (the fixture is reviewed like code).

## Fixture v1 provenance

Built at RVW03-WS06 (#695): the 3 RVW02-WS05 baseline PRs carry the study's
human-vetted labels (issue #638, confirmed); the remaining ranges and labels
were mined by parallel scout agents from fix-commit archaeology across the
portfolio and normalized by an economical-model agent, and land as
**unconfirmed candidates** pending maintainer confirmation.

## Cells (`lab/cells/`)

A **Cell** (ADR-0049; `docs/spec/review-lab.md`) is one declarative review
experiment: a small committed TOML file under `lab/cells/` that
`shipit lab run <id>` resolves onto the sanctioned offline replay driver
(`shipit pr review replay [--fanout]`), foreground, on subscription-billed
CLI backends. `shipit lab report <id>` then renders its **convergence
curve** from the banked records — cumulative major-or-worse recall, false
positives / adjudicated precision, token cost, and latency per sweep point,
compared against the baseline cell **at equal budget** (recall per Mtok and
per minute). Both verbs are validated loudly at load: unknown keys, a missing
`baseline`/`axis` declaration, or an unfair pair (different fixture version
or PR subset than the baseline) refuse before any token burns.

### Format (schema 1)

Parsed and validated by `shipit.review.cell`; the demo pair below is
`fanout-baseline.toml` (control) + `fanout-informed.toml` (treatment).

```toml
schema = 1
id = "fanout-informed"      # must equal the filename stem
baseline = "fanout-baseline"  # MANDATORY: the cell compared against (usually the
                              # control; a composition cell names a treatment;
                              # a control names itself)
axis = "sweep mode: informed vs blind"  # MANDATORY: the ONE thing changed
                                        # (a control declares axis = "control")
description = "…"

[fixture]
version = 1                 # the label-set version scores cite (validated at run)
prs = ["core-440", …]       # fixture pin subset (omit = every pin)

[pipeline]
shape = "fanout"            # "single" | "fanout"
dimensions = ["correctness", …]   # fan-out pass set (omit = shipped set)
dedup = "mechanical"        # "mechanical" | "calibrated"
# [pipeline.calibrator]     # required iff dedup = "calibrated"
# backend = "claude"

[invocation]
backend = "codex"           # funnel agent: codex | agy
model = "pro"
timeout = "600s"
# NO `reasoning` key: the codex/claude backends carry the knob (#685/#691), but
# the lab runner does not thread a level into the replay driver yet, so a
# recorded-but-unapplied level would mislabel the arm — the key is rejected.

# [invocation.dimensions.security-robustness]  # experiment-only per-dimension
# model = "opus"                               # Invocation overrides (never Roster)

[sweeps]
count = 2                   # K full sweeps over the same ranges
mode = "blind"              # "blind" | "informed" (sweep k primed with <k's findings)
replicates = 2              # repeat runs per (pin, sweep) for variance
```

### Running and idempotency

`shipit lab run fanout-informed --checkout ~/src/core --checkout ~/src/app …`
executes every (pin × replicate × sweep) point. Replay is offline: each pinned
repo needs a local clone with the pinned commits fetched, and a pin with no
matching checkout refuses loudly before anything runs. Runs are **idempotent
by key** — (cell, fixture PR, fixture version, instructions variant,
replicate, sweep), stamped on each record as `round.cell` — so banked points
are reused, never re-paid: extending a K=1 curve to K=2 pays only for sweep 2.
`--force` is the one explicit re-run path (the newest record per key wins in
the report). The variant hash covers the WHOLE prompt material: the
instructions file, and — for a `fanout` cell — the resolved dimension set
(names, titles, focus texts, per-dimension invocation overrides), which lives
in code (`src/shipit/review/dimensions.py`) and is selected by the cell's
`dimensions` list (#713). Editing any of it changes the variant hash, so
stale banked records are never silently reused for a new prompt.
