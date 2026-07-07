# The gate owns the config

> **Status: Proposed.** Epic LNT01 (#495); decided in the lint-hermeticity
> planning grill. Flips to Accepted when this docs PR merges.
> Extends ADR-0036 (the gate owns style) from *which rules* to *the config
> bytes*; amends the #493 beachhead under ADR-0004 (per-repo "honor tracked
> config") toward one shipit-owned config; extends ADR-0028 (one Exec seam) with
> the env scrub. Precedes ADP02 (#422), where the gate becomes a required fleet
> CI check and this uniformity turns load-bearing.

The lint gate SUPPLIES each tool its configuration and blocks every other
source. shipit ships ONE canonical config per tool; the gate injects it into
every invocation (a pinned `--config` / format flag) and scrubs the ambient
environment at the single `_run_tool` exec seam: `$HOME` (exact) and `XDG_*`
(prefix), plus an explicit, ENUMERATED denylist of per-tool config vars
(`SHELLCHECK_OPTS`, `RUFF_CONFIG`, `CARGO_HOME`, `CLIPPY_CONF_DIR`,
`YAMLLINT_CONFIG_FILE`) — deliberately not a `*_CONFIG*` substring match, which
would also drop `PKG_CONFIG_PATH` / `FONTCONFIG_PATH` and break cargo/C
builds. So no ancestor-directory config file, user-global config, or
environment variable is ever consulted. The verdict is therefore a pure function of the tracked files
under one fixed config: identical on any machine, in any repo, in CI or on a
laptop. This is the same discipline pixi already applies to the tool
*binaries* (one pinned version everywhere), extended to the tool *config* —
the missing half.

The acceptance criterion is a property test, not a catalogue of flags. For
every tool in the registry, lint a fixture twice — clean, and with a hostile
config planted in an ancestor dir **and** `$HOME` **and** the tool's env var —
and assert the verdict does not move. The test is parametrized over the tool
registry itself, so a newly-registered tool is subject to the invariant on day
one without anyone remembering to add a case; a leak fails shipit CI, not a
consumer smoke six repos downstream. Adding the injection flags and the env
scrub is implementation in service of this test, never separately "done."

Divergence is normalized away, not accommodated. Where our own repos differ —
prettier rule sets, Rust crate layouts — the REPO is changed to the one
standard (rules unified and the resulting violations fixed; workspace members
moved to `crates/*`), as long as it stays buildable and testable. Only
STRUCTURALLY irreducible differences are accommodated, and each concretely: a
Tauri backend that must live under `src-tauri/` and a TS monorepo's several
packages are handled by the gate targeting the right subtree; generated
`parser.c` that must never be linted is handled by the `[lint].ignore` list.
The list stays the single legitimate per-repo variation — *which files* are
checked, never *which rules*.

What forced the decision. Reproducible linting had been attempted ~20 times and
always regressed to "needs a lot of work." Every prior attempt was reactive and
per-tool — fix this tool's discovery, scrub that variable — with no artifact
that stayed green, so the next tool, a version bump, or a stray ambient file
silently reopened a door and the leak surfaced downstream. #493 shut the door
for shfmt + prettier's `.editorconfig` only, and did it by HONORING each repo's
own tracked config and neutralizing an ambient one — a per-repo model that
still leaves 8 of 10 registered tools consulting ambient config, and still lets
two repos disagree on the same rule (the fleet survey found exactly this:
prettier's `semi`/`trailingComma` genuinely conflict across four TypeScript
repos). Because we own BOTH ends — the tool and the repos — uniformity is
enforceable rather than merely hoped for.

Considered and rejected: **per-repo "honor tracked, neutralize ambient" for
every tool** (the #493 model generalized) — subtle per-tool discovery logic, and
it PRESERVES rule divergence between repos, which is the opposite of the goal.
**Embedding each config in the binary and passing `--config` with no repo file
at all** — structurally strongest (a stray repo config cannot exist), but heavier
than warranted when we own every repo; managed files the install reconciler
keeps byte-identical are enough, and they stay locally inspectable. **Leaving
the invariance guarantee to a downstream fleet CI check** — the leak then fails in
a consumer, not in shipit, which is the six-repos-later failure this ADR exists
to end.

Consequences. shipit gains a canonical config set — Rust rustfmt+clippy
(greenfield; no repo commits one today), a `ruff.toml` carved out of shipit's
own `pyproject.toml` and seeded fleet-wide, Go's golangci config promoted from
its lone holder, and the universals confirmed — plus config injection and one
env scrub at `_run_tool`. The invariance property test joins shipit CI as the
acceptance gate. The fleet normalizes to the standard in a one-time pass per
repo, each its own PR kept green: prettier rules unified and reformatted; Rust
members relaid to `crates/*`; the canonical configs adopted and the surfaced
debt cleared. `.prettierrc` rule disagreements and off-standard layouts stop
existing rather than being tolerated. The legacy `release`-driven CI may need
path updates where a normalized layout moves directories. And because the gate,
the config, and the verdict are one thing everywhere, `shipit lint` on a laptop
is byte-for-byte the CI check — the standardization is inspectable and workable
locally, which is the whole point.
