# The gate owns style

> **Status: Accepted.** Epic RVW02 (#453); decided in the ADP00 retrospective.
> Complements ADR-0031 (engine as sole requester) and the classification seam
> (#423) by bounding WHAT a review finding may be.

No review finding may be mechanically checkable. Style and convention that a
tool could express — formatting, import order, type-hint completeness,
docstring shape, naming pattern — belong to the lint gate or to nobody: either
a configured rule enforces the standard, or the standard does not exist and
reviewers do not enforce it ad hoc. A reviewer who believes a style rule
SHOULD exist proposes the rule, once, in the review summary — a rule proposal,
never per-line findings.

What forced the decision. ADP00's rounds repeatedly carried style findings
backed by no configured standard: the repo had NO `[tool.ruff]` selection at
all (bare E/F defaults), yet reviews requested return-type hints and docstring
adjustments (e.g. #452's `_lint_env_run_tool` finding) — reviewers freelancing
a rubric the repo never adopted, with the reviewer prompt's "judge against the
repo's conventions" pointing at conventions written nowhere. Under review
economics where every finding costs a full round (an addressing agent, a push,
a re-review tail), the epic paid multi-minute, multi-thousand-token rounds for
four-character diffs that a linter — had the rule existed — would have caught
at commit time for free. The general principle this instantiates is already
the repo's: hooks, CI, and `shipit lint` run the identical command precisely
so no human re-derives what a machine checks; review was the last place
mechanical judgment leaked back in.

The enforced floor is deliberately MINIMAL (maintainer decision, RVW02
planning): a small curated ruff selection of correctness-adjacent families
(the exact set is WS03's to propose — bugbear/pyupgrade/isort-class, sized so
the one-time debt-clear stays small), not strict annotation or docstring
enforcement. Everything above the floor is explicitly OUT of review scope.
The floor can rise later, rule by rule, through the same seam: adopt the rule,
clear the debt, and the standard is real from that day — never through a
review finding.

Considered and rejected: strict ANN/D4xx enforcement now (a large debt-clear
buying style value that is marginal next to correctness; the seam above makes
later adoption cheap, so deferral costs nothing irreversible); rubric-only
with no new rules (leaves the standard unenforced, so it drifts, and leaves
reviewers the temptation this ADR exists to remove); status quo, reviewer
judgment (the measured cost above, recurring on every PR the fleet ever
opens).

Consequences: the reviewer role gains the out-of-scope rule and the
rule-proposal channel (RVW02-WS03); nitpick classification volume should drop
measurably (the all-nitpick breaker fires less because fewer nitpicks are
minted); `[tool.ruff.lint]` selection joins the repo with a rationale per
family; and a style disagreement between a reviewer and an author is by
construction a CONFIG discussion on the gate, held once, instead of a review
thread held on every PR.
