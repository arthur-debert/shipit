# The repo pins its shipit — the managed-set lifecycle

> **Status: Accepted.** Epic ADP00; decided in the WS08 regroup after the canary
> dry-run surfaced the tool/managed-set lag window (#420, rounds 5–6). Reverses
> the TRE03-era provisioning reconcile-commit; supersedes the "Step 5"
> pixi-dependency plan noted in ADR-0003 and `docs/prd/install-reconciliation.md`;
> reaffirms ADR-0003's pull-not-push.

A consumer repo pins the exact shipit it runs: `.shipit.toml`'s
`[shipit].version` carries a FULL git commit SHA of the shipit repo (the **Shipit
pin** — see CONTEXT.md), and the managed `bin/shipit` launcher provisions and
execs that pinned build via `uv` (cached after first resolve), reading the pin
directly from `.shipit.toml`. Tool version and managed set thereby travel as ONE
versioned unit in the repo's history: a Tree cut from base X runs the shipit
pinned at X, against the managed files written by that same build — coherent by
construction. This kills the lag window the canary demonstrated live: with the
tool machine-global and auto-updating (the previous PATH story), every window
between a shipit release and a repo's next reconcile made committed managed
files stale relative to the running tool (sessions reading stale settings) and
made Tree provisioning's fail-closed reconcile land `chore(shipit)` commits on
feature branches (PR pollution, observed on canary PR #10).

The lifecycle in one paragraph. `shipit install` STAMPS the pin with its own
build identity — never an operator-supplied value, so the pinned build is
provably the build that wrote the managed files and ran the gates (today's code
stamps the static package version `0.0.1`, which identifies nothing; that is a
bug this ADR retires). The install reconcile PR is the ONLY bump vehicle: one
commit atomically carries pin bump + managed-file updates + pristine hashes
(ADR-0003's seam, unchanged). Rollout is driven from shipit's side — the root
coordinator sweeps the **Portfolio** (`[project.portfolio]`, the machine-readable
fleet manifest; the tracking issue's birds-eye table is a derived human view,
not an authority) with a chosen build, opening reconcile PRs that each repo
merges on its own schedule: pull-not-push, with initiation and version choice
centralized. Rollback is the same seam run with an older build — no `--pin`, no
rollback verb. Staleness is surfaced, never enforced: the sweep reads every
portfolio repo's pin centrally, and a consumer session start emits one
best-effort advisory line ("pin X is N commits behind"); with pin-wins
execution, lag is a scheduling fact, not a hazard.

Launcher resolution is pin-wins, loudly: `bin/shipit` resolves the pin or fails
with instructions; PATH is never consulted in a pinned repo (the old
first-shipit-on-PATH walk silently reintroduced drift). One sanctioned override
exists for development — `SHIPIT_EXEC=/path/to/build` — honored and announced
(stderr + flow log), formalizing what shipit's own repo does with its checkout
build. A repo with no pin fails loudly toward the bootstrap: the external
`uv tool` shipit's remaining roles are exactly (i) a virgin repo's first
install and (ii) operator convenience outside repos; its auto-update property
is explicitly a non-feature inside repos.

Tree provisioning consequently mutates NOTHING managed: the TRE03 behavior of
running `shipit install --local` at Tree birth and fail-closing into a
reconcile commit on the just-cut branch is deleted (it solved tool/base
incoherence that the pin now prevents by construction, at the cost of leaking
infra diffs into every feature PR of a lag window). Provisioning is clone +
branch + pixi env + hook activation (ADP00-WS13) + provenance record; a write
Run spawned onto a PINLESS base refuses loudly ("run the bootstrap install
first") — the only surviving guard. The orchestration boundary is explicit: a
coordinator's (possibly newer) build orchestrates from OUTSIDE (tree create,
launch, PR reads); everything inside the Tree — hooks, lint, `pr next`, the
Run's verbs via `bin/shipit` — rides the repo's pin. A newer shipit changes a
consumer only via a reconcile PR.

Install self-certifies, scoped to what it owns: after staging, it asserts (1)
the manifest parses and the lint env solves, (2) the files it delivered pass the
lint configs it delivered, (3) hooks are live, (4) the launcher resolves the
freshly-stamped pin — failing closed (no PR) on any miss, which makes "the
managed set never fails its own gates" executable (the WS09/WS10 canary class).
Its reconcile commit bypasses the repo's hooks (`--no-verify`): the whole-tree
gate is the REPO'S bar — the ADP01 checklist's lint step, after the sanctioned
debt-clear — not install's. This scoping breaks the observed deadlock where
install's commit was blocked by pre-existing consumer lint debt that can only be
cleared using the env install delivers; consumer debt is reported in the
reconcile PR body, never a blocker.

Considered and rejected: package-version or tag pins (no release machinery
exists until ADP02; a static `0.0.1` distinguishes nothing — tags may later
join as input sugar resolving to the canonical SHA); the deferred Step-5 plan of
shipit as a pinned pixi dependency (duplicates the pin into `pixi.toml` +
`pixi.lock` — a second source that can disagree, plus lockfile churn in every
bump PR ×41 repos, and unusable by pre-env verbs like install/gh-setup);
PATH-fallback launchers (drift through the back door); refuse-on-stale-base for
write Runs (the pre-regroup candidate — gates the symptom where the pin removes
the cause; survives only as the pinless-base refusal); status-quo-documented
(institutionalizes infra-in-feature-diffs and parallel-Run refresh collisions).

Consequences worth naming: a shipit fix reaches a repo at its next reconcile
merge, not instantly — the deliberate price of coherence, paid down by
shipit-side sweeps being cheap (minutes across the fleet) and by every other
fleet tool already behaving this way under the epic's governing principle (the
one previously-exempt tool was shipit itself); `uv` plus private-repo git
credentials become hard prerequisites wherever a pin first resolves (already
true on laptops per the runbook; the runner leg lands with ADP02 as planned);
and the adoption runbook's PATH story, `install-reconciliation.md` Step 5, and
ADR-0031's ruleset-parameter mechanism note (the parameter GitHub rejects,
removed in ADP00-WS11) all need amending in the epic's docs pass.
