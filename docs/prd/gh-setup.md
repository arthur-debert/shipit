# gh-setup — repo conformance (ruleset / labels / secrets)

> Status: **implemented** (shipped before the per-feature PRD convention)
> Origin: this capability was built from the retired roadmap §1, reproduced below so the
> design + verification rationale survives the roadmap's retirement.
> Decisions: `docs/adr/0001-reuse-release-core-by-copy.md` (copy-not-depend) ·
> `architecture.lex §6` (the `.shipit.toml` secret map).

## Original design (reproduced)

The `shipit gh-setup <repo>` subcommand makes a GitHub repo conform to the
portfolio standard in three idempotent passes — branch ruleset, issue labels,
repo secrets. It is the roadmap's first useful increment, and because it has NO
pixi dependence (pure GitHub + Doppler plumbing) it is also the right place to
stand up the shipit CLI skeleton itself.

### Prerequisite — stand up the shipit CLI

There is no shipit code yet (only docs + skills). gh-setup is the first
subcommand, so Step 1 also creates the `shipit` console-script entry with
git-style subcommands (`architecture.lex §4`). Do not invent the scaffold —
reuse release-core's proven patterns. release-core is Python 3.11+ / click /
hatchling at `/Users/adebert/h/release/templates/commons/lib/release_core/`: a
hierarchical click tree (`cli_entry.py`), a verb-per-module convention
(`verbs/<name>.py` exposing `main(argv) -> int`, attached by
`cli/_helpers.py:wrap_verb`), and a single GitHub boundary in `gh.py` (`rest()`,
`secret_set()`, `secret_list()`, `repo_view()`).

### Decision to confirm with the maintainer first (the one real fork)

How shipit reuses release-core — DEPEND on the published release-core package and
re-skin its entry points, or COPY the handful of needed pieces (`gh.py` plus the
two verbs named below) into a fresh slim shipit package. `architecture.lex §4`
("KEEP that state machine; do not rewrite it, only re-skin its entry points")
leans toward reuse; the global no-adapters / no-backwards-compat principle leans
toward a clean copy. This sets the package's shape, so settle it before writing
code.

> Resolved: COPY, not depend — captured in `docs/adr/0001-reuse-release-core-by-copy.md`.

### The three passes

Each idempotent (safe to re-run; install AND update share this command):

#### a. Ruleset

Apply the standardized main-branch-protection ruleset. The captured shape is
`gh/main-branch-protection.json`, but it is a CAPTURE from phos-app and carries
fields that must be stripped or recomputed per target repo: `id`, `source`, and
the hardcoded `required_status_checks` contexts (`app-ui-unit-test / check`,
`tauri-wire-contract-test / check`). Port the auto-discovery from release-core's
`release_core/verbs/apply_ruleset.py` — it resolves the required checks from the
target repo's own workflows and PUT/POSTs `gh api repos/{repo}/rulesets`. The
rest is fixed: `target=branch`, `ref=~DEFAULT_BRANCH`, `pull_request` (0
approvals), `required_linear_history`, `non_fast_forward`, `deletion`, admin
bypass.

#### b. Labels

Ensure the standard label set exists. Source is `gh/issue-lables.toml` (data to
clean up while here: the filename is misspelled; the `duplicate-of` entry uses
`descriptions=` not `description=`; no entry has a `color`). release-core has NO
bulk-label verb, so this pass is net-new: read the TOML and
`gh label create --force` (or the `gh api` equivalent) each one so it is
created-or-updated idempotently. The set: `bug`, `feature`, `ready-for-agent`,
`small`, `needs-decision`, `duplicate-of`.

#### c. Secrets

Resolve each secret from the consumer's `.shipit.toml` `[secrets]` map and push
it with `gh secret set`. The map schema is in `architecture.lex §6`: each entry
maps a gh secret NAME (the table key) to a source — `{ doppler = "KEY" }`,
`{ env = "VAR" }`, or `{ prompt = true }`. No `.shipit.toml` exists yet, so Step
1 also defines that file and its `[secrets]` table. Port the sourcing +
`secret_set()` flow from release-core's
`release_core/verbs/install_release_secrets.py`; Doppler resolution is
`doppler secrets get <KEY> --plain --project github --config prd`. A changed
secret is re-set to its new value (the desired behavior per `README.lex`); a
missing OPTIONAL source is skipped, not fatal.

### Verified by

Running `shipit gh-setup` against a test repo yields the main-branch-protection
ruleset carrying THAT repo's own required checks (not phos's), the full six-label
set, and every mapped secret present in repo settings — and a second run is a
clean no-op (proving idempotence).
