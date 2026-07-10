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
