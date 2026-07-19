"""The managed-set catalog — the :class:`Unit` model and the packaged desired state.

A Unit is one managed thing: a whole file or a marker-delimited block inside a
consumer-owned file. ``load_units()`` is the catalog — the skills tree, the
AGENTS.md block, the bootstrap launchers, the lint-check units, the HAR01
agent-defs, the settings.json JSON-hook entries, and the CDX01 Codex project
layer (``.codex/``) — each carrying its desired bytes, so "what does shipit
distribute" is a value, not a directory walk at the call site.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from .. import config

AGENTS_FILE = "AGENTS.md"
AGENTS_KEY = "AGENTS.md#shipit-block"
BLOCK_OPEN = "<!-- Managed by shipit; do not edit. Regenerate via shipit install. -->"
BLOCK_CLOSE = "<!-- End shipit-managed block. -->"

# The lint-check units Step 2 deferred to Step 3 (docs/legacy-prd/lint-checks.md). The consumer gets
# the thin lefthook caller (whole file) and a `lint = "shipit lint"` task BLOCK
# in its own pixi.toml. The pixi blocks use TOML-comment markers (HTML comments
# are invalid TOML) and anchor under a table header so the managed keys land in
# the right table on a first install.
LEFTHOOK_FILE = "lefthook.yml"

# The lint tool configs the managed gate needs (ADP00-WS10, #436). The managed
# lefthook caller runs the whole-tree lint, and markdownlint/yamllint/prettier
# auto-discover their config from the repo root — so the exact configs
# shipit's own gate relies on are managed whole-file units, and a stock
# consumer lints with what shipit dogfoods (drift is caught by the
# reconcile-to-noop tests over shipit's own copies, the WS01 pattern).
# Packaged names drop the leading dot so the data files stay visible to
# directory listings and packaging globs; ``dest`` restores it.
#
# prettier joins the set (LNT01-WS06 #519): prettier is the ONE managed config
# with a second authority — the TS/Svelte repos run their own prettier via npm
# scripts/editors, which read a committed `.prettierrc` — so shipping it as a
# managed unit resolves that two-authority drift (the injected `--config` already
# governs the shipit gate; the committed file governs the repo's own prettier).
# ruff/rustfmt/golangci stay injection-only: no repo runs them a second way, so
# a committed copy would be dead weight. prettier accepts a YAML body in
# `.prettierrc`, so the canonical `prettierrc.yaml` ships byte-identical.
MARKDOWNLINT_FILE = ".markdownlint.yaml"
MARKDOWNLINTIGNORE_FILE = ".markdownlintignore"
YAMLLINT_FILE = ".yamllint.yaml"
PRETTIERRC_FILE = ".prettierrc"
LINT_CONFIG_UNITS = (
    (MARKDOWNLINT_FILE, "markdownlint.yaml"),
    (MARKDOWNLINTIGNORE_FILE, "markdownlintignore"),
    (YAMLLINT_FILE, "yamllint.yaml"),
    (PRETTIERRC_FILE, "prettierrc.yaml"),
)

# The managed `.gitignore` release-output block (#906). shipit's OWN release
# stages write transient artifacts at the repo ROOT — `shipit release notes`
# writes RELEASE_NOTES.md (shipit/verbs/release.py: DEFAULT_NOTES_FILE), the
# sign stage stages `dist-signed/` (wf-sign-mac.yml `--out dist-signed`), and
# compositions write `dist/` (DEFAULT_BUNDLE_DIR). For a consumer whose
# publishable crate IS the repo root (a single-crate package, not a `crates/`
# workspace), `cargo publish` runs its VCS-dirty check against that root and
# ABORTS when these artifacts sit there uncommitted — the simple-gal
# `v0.20.6-rc.1` failure. So the managed set ignores them fleet-wide: a marker
# block in the consumer-owned `.gitignore` (comment markers — `#` is a valid
# gitignore comment; consumer entries outside the block are untouched, spliced
# in / created at the repo root by the standard block splicer). NOT
# `--allow-dirty`, which would bake the artifacts into the published .crate.
GITIGNORE_FILE = ".gitignore"
GITIGNORE_KEY = ".gitignore#shipit-release-outputs"
GITIGNORE_OPEN = "# >>> shipit-managed release-output ignores (do not edit; regenerate via `shipit install`) >>>"
GITIGNORE_CLOSE = "# <<< shipit-managed release-output ignores <<<"

PIXI_FILE = "pixi.toml"
PIXI_KEY = "pixi.toml#shipit-tasks"
PIXI_OPEN = (
    "# >>> shipit-managed tasks (do not edit; regenerate via `shipit install`) >>>"
)
PIXI_CLOSE = "# <<< shipit-managed tasks <<<"
PIXI_ANCHOR = "[tasks]"

# The thin `test` caller (TOL01-WS01, ADR-0039): `test = "./bin/shipit test"`
# in the consumer's default [tasks] — the pinned-launcher form, like the
# managed `lint` task — so laptop, hook, and CI run the identical verb. Its
# OWN block (not a line in the tasks block above) so it can be skipped
# INDEPENDENTLY: pixi refuses a bare `pixi run test` when a task named `test`
# exists in several environments, so a consumer whose own manifest already
# defines a `test` task in a feature (shipit's own repo does — the full-gate
# task in [feature.test.tasks] needs its rust toolchain env) keeps that task
# authoritative and this block is NOT delivered
# (the reconcile's task-ambiguity guard, `PixiTaskConflict` — the #547
# key-conflict guard's pixi-run-level sibling), while the lint/logs tasks block
# still lands. A `test` key in [tasks] itself is
# caught by the existing duplicate-key guard.
PIXI_TEST_TASK_KEY = "pixi.toml#shipit-test-task"
PIXI_TEST_TASK_OPEN = (
    "# >>> shipit-managed test task (do not edit; regenerate via `shipit install`) >>>"
)
PIXI_TEST_TASK_CLOSE = "# <<< shipit-managed test task <<<"

# The ADP00 managed consumer environment (docs/legacy-prd/adoption.md: THE MANAGED SET
# OWNS THE CONSUMER ENVIRONMENT). Two sibling units join the tasks block in the
# consumer's pixi.toml: the lint feature/dependency block (a marker block carrying
# the fleet-pinned toolchain) and the lint environment definition — so the managed
# lefthook caller's `pixi run -e lint lint` works on a stock consumer with
# nothing pre-installed. This AMENDS the lint PRD's "task line only, never a
# dependency block" decision. Canonical versions live in the packaged
# `pixi-lint-deps-block.toml` (a bump is one data edit, rolled out on each
# consumer's next install reconcile); shipit's own pixi.toml carries the same
# blocks verbatim — its Tree provisioning self-installs, so anything else would
# splice duplicates into its hand-kept manifest — and a drift test asserts the
# packaged block agrees with shipit's own lint environment (the dogfood
# guarantee). lexd is NOT part of the lint-deps block: it rides its own reserved
# `[feature.shipit-lexd]` block (below, PIXI_LEXD_KEY), which carries the
# Artifact channel the lint-deps' conda-forge tools do not need (ARF02-WS06),
# composed into the lint env by the environments unit below.
PIXI_LINT_DEPS_KEY = "pixi.toml#shipit-lint-deps"
PIXI_LINT_DEPS_OPEN = (
    "# >>> shipit-managed lint deps (do not edit; regenerate via `shipit install`) >>>"
)
PIXI_LINT_DEPS_CLOSE = "# <<< shipit-managed lint deps <<<"
PIXI_LINT_DEPS_ANCHOR = "[feature.lint.dependencies]"
# The lint ENVIRONMENT unit is an FMT_ENV_MEMBER MERGE, not a marker block: it owns
# only `shipit-lexd`'s MEMBERSHIP in the `lint` env (the managed invariant), while
# WHICH base features the env carries stays the consumer's own config (ADR-0047). A
# marker block would own the whole `lint = [...]` line and collide with a
# consumer's own `[environments] lint`, get skipped by the key-conflict guard, and
# leave lexd unwired (lint breaks, no `provision` fallback — the #1062 finding). So
# the markers below are vestigial (kept only so the six sibling pixi blocks stay
# fence-distinct); the anchor keeps the unit out of the anchor-less table-conflict
# guard. The consumer read/write is :func:`splice.extract_env_member` /
# :func:`splice.splice_env_member`, hashed on :func:`env_member_token`.
PIXI_ENVS_KEY = "pixi.toml#shipit-environments"
PIXI_ENVS_OPEN = "# >>> shipit-managed environments (do not edit; regenerate via `shipit install`) >>>"
PIXI_ENVS_CLOSE = "# <<< shipit-managed environments <<<"
PIXI_ENVS_ANCHOR = "[environments]"

# The managed lexd block (ARF02-WS06, ADR-0066). `provision lexd` is retired:
# lexd — the one lint-gate tool that was not on conda-forge — is now published
# to the public Artifact channel (ADR-0064) and resolves as an ordinary conda
# dependency through pixi.lock. The block is a dedicated shipit-reserved FEATURE
# (`shipit-lexd`) carrying the channel + the fleet-pinned lexd version, MERGED
# into the lint environment's feature list by the environments unit above
# (`lint` composes `shipit-lexd`). A fresh reserved `[feature.shipit-lexd]`
# table appended at EOF (anchor-less, the artifact-dep pattern) never
# re-declares a table a consumer owns, so the block is collision-free and
# consumer-non-editable — a consumer cannot drift its lexd version (ADR-0047,
# fleet uniformity). shipit's own pixi.toml carries the block verbatim (its Tree
# provisioning self-installs); a drift test (tests/test_install.py) asserts the
# packaged block agrees with shipit's own reserved feature (the dogfood
# guarantee). win-64 is unserved under the Windows build pause (#895, ADR-0071),
# so a win-64 lint solve fails closed with no fallback.
PIXI_LEXD_KEY = "pixi.toml#shipit-lexd"
PIXI_LEXD_OPEN = "# >>> shipit-managed lexd feature (do not edit; regenerate via `shipit install`) >>>"
PIXI_LEXD_CLOSE = "# <<< shipit-managed lexd feature <<<"

# The UNCONDITIONAL launcher-deps block (#758, closed by TOL02-WS17 #794):
# `uv` for the pinned ADR-0033 `bin/shipit` launcher, in the DEFAULT env's
# [dependencies] — every workflow block (wf-checks and the wf-release family)
# runs `pixi run --locked ./bin/shipit`, and hosted runners carry no uv, so
# the launcher's one prerequisite must ride the managed pixi surface (the
# #582 doctrine: provisioning lands in setup-pixi's lockfile-keyed cache,
# never a run-time install). Unconditional — unlike the per-toolchain blocks
# below, EVERY consumer's managed tasks resolve through `bin/shipit` — and
# pinned in lockstep with Layer 0's UV_PIN (bin/setup-dev-env.sh), drift-
# tested in tests/test_install.py. A consumer that already pins `uv` in its
# own [dependencies] (the #758 consumer-side workaround) keeps its pin via
# the PixiKeyConflict first-splice guard, exactly like the cargo-edit
# precedent (#793) — their entry stays until their own reconcile.
PIXI_LAUNCHER_DEPS_KEY = "pixi.toml#shipit-launcher-deps"
PIXI_LAUNCHER_DEPS_OPEN = "# >>> shipit-managed launcher deps (do not edit; regenerate via `shipit install`) >>>"
PIXI_LAUNCHER_DEPS_CLOSE = "# <<< shipit-managed launcher deps <<<"
PIXI_LAUNCHER_DEPS_ANCHOR = "[dependencies]"

# The CONDITIONAL per-toolchain dep blocks (#547 Layer 1): a consumer whose
# tracked manifests signal a toolchain (a `Cargo.toml` anywhere → rust, `go.mod`
# → go, `package.json` → node, a `pyproject.toml` → python — for the compiled
# toolchains the same per-manifest discovery that makes the corresponding
# `shipit lint` leg run, see shipit/lint.py) gets that toolchain
# pinned through pixi/conda-forge, so the lint legs stop hard-failing (127)
# wherever the host happens to lack cargo/go/node — the #526 "clippy is
# local-only" CI gap. rust and go anchor under the lint feature (they provision
# LINTER toolchains, siblings of the managed lint-deps block above); node
# anchors under `[dependencies]` — it provisions the repo's OWN node/pnpm
# runtime, not a linter. The rust signal delivers TWO more blocks in
# `[dependencies]` (#793/#801, the #784-F2 class): the release-side bump tool
# (cargo-edit, whose `cargo set-version` is the rust bump adapter's projection
# command, shipit/release/bump.py) and the rust RELEASE toolchain itself
# (`rust` — cargo for prepare/build/publish; hosted images no longer carry
# Rust, and the lint-feature rust block is invisible to the release runs'
# default env — TOL02-WS17 open hole 1, closed by #801). The python signal
# delivers the release-side publish tool (twine, the pypi endpoint's uploader
# — open hole 2, #801). The tree-sitter signal delivers the grammar
# toolchain's own CLI (`tree-sitter-cli` — `tree-sitter generate` at build,
# the corpus `tree-sitter test` lane; #890 closes open hole 7). The lua signal
# delivers the lua LINT toolchain (`stylua` + `selene`, under the lint feature
# like rust/go — TOL03-WS01 #972), and like tree-sitter it fires off the
# DECLARATION (a nvim plugin has no manifest to detect), so both anchor the
# provisions_signal mechanism on their respective (release / lint) axes. Unlike
# the manifest-detected signals these fire off the DECLARATION — a `.shipit.toml`
# [toolchains] tree-sitter or lua leg, no manifest signals a grammar or a nvim
# plugin — via the same union mechanics as the wasm-pack→node-deps delivery
# (:attr:`shipit.tools.registry.Toolchain.provisions_signal`, read by
# :func:`shipit.verbs.install._declared_signals`). All the release-side
# blocks anchor under
# `[dependencies]`, NOT the lint feature, because the wf-release stages
# execute shipit in the DEFAULT pixi env (`pixi run --locked ./bin/shipit`)
# and the tools must be on THAT run's PATH. Each single-purpose block is
# deliberately its OWN unit (never merged into a shared one) so the
# key-conflict guard below can skip exactly the key a consumer already pins
# without losing the sibling deliveries.
# Provisioning rides pixi/conda-forge under setup-pixi's lockfile-keyed cache
# (the #582 doctrine); the release verbs never install at run time —
# they fail loudly naming this reconcile instead. Delivered only when
# :func:`load_units` is passed the
# toolchain signal (`toolchains=`), so the zero-arg catalog is byte-identical
# to the pre-#547 one. A consumer who ALREADY pins one of a block's keys in its
# anchor table keeps their pin: the first splice would duplicate the TOML key
# and break pixi.toml, so the reconcile skips delivering that block with a loud
# warning instead (:class:`shipit.install.reconcile.PixiKeyConflict`). Accepted
# residue: a consumer that later DELETES its last signal manifest keeps the
# spliced block + `[managed]` hash until manually removed (block-retirement
# machinery is out of scope; the block's own comment says how).
TOOLCHAIN_RUST = "rust"
TOOLCHAIN_GO = "go"
TOOLCHAIN_NODE = "node"
TOOLCHAIN_PYTHON = "python"
TOOLCHAIN_TREE_SITTER = "tree-sitter"
TOOLCHAIN_LUA = "lua"
PIXI_RUST_DEPS_KEY = "pixi.toml#shipit-rust-lint-toolchain"
PIXI_RUST_DEPS_OPEN = "# >>> shipit-managed rust lint toolchain (do not edit; regenerate via `shipit install`) >>>"
PIXI_RUST_DEPS_CLOSE = "# <<< shipit-managed rust lint toolchain <<<"
PIXI_GO_DEPS_KEY = "pixi.toml#shipit-go-lint-toolchain"
PIXI_GO_DEPS_OPEN = "# >>> shipit-managed go lint toolchain (do not edit; regenerate via `shipit install`) >>>"
PIXI_GO_DEPS_CLOSE = "# <<< shipit-managed go lint toolchain <<<"
# The lua lint toolchain (TOL03-WS01 #972): stylua + selene, siblings of the
# rust/go lint blocks under the lint feature. Unlike them it fires off the
# DECLARED `[toolchains]` lua leg (no manifest signals a nvim plugin), via the
# registry entry's provisions_signal — the tree-sitter mechanics on the lint axis.
PIXI_LUA_DEPS_KEY = "pixi.toml#shipit-lua-lint-toolchain"
PIXI_LUA_DEPS_OPEN = "# >>> shipit-managed lua lint toolchain (do not edit; regenerate via `shipit install`) >>>"
PIXI_LUA_DEPS_CLOSE = "# <<< shipit-managed lua lint toolchain <<<"
PIXI_NODE_DEPS_KEY = "pixi.toml#shipit-node-deps"
PIXI_NODE_DEPS_OPEN = (
    "# >>> shipit-managed node deps (do not edit; regenerate via `shipit install`) >>>"
)
PIXI_NODE_DEPS_CLOSE = "# <<< shipit-managed node deps <<<"
PIXI_NODE_DEPS_ANCHOR = "[dependencies]"
PIXI_RUST_RELEASE_DEPS_KEY = "pixi.toml#shipit-rust-release-deps"
PIXI_RUST_RELEASE_DEPS_OPEN = "# >>> shipit-managed rust release deps (do not edit; regenerate via `shipit install`) >>>"
PIXI_RUST_RELEASE_DEPS_CLOSE = "# <<< shipit-managed rust release deps <<<"
PIXI_RUST_RELEASE_TOOLCHAIN_KEY = "pixi.toml#shipit-rust-release-toolchain"
PIXI_RUST_RELEASE_TOOLCHAIN_OPEN = "# >>> shipit-managed rust release toolchain (do not edit; regenerate via `shipit install`) >>>"
PIXI_RUST_RELEASE_TOOLCHAIN_CLOSE = "# <<< shipit-managed rust release toolchain <<<"
PIXI_PYTHON_RELEASE_DEPS_KEY = "pixi.toml#shipit-python-release-deps"
PIXI_PYTHON_RELEASE_DEPS_OPEN = "# >>> shipit-managed python release deps (do not edit; regenerate via `shipit install`) >>>"
PIXI_PYTHON_RELEASE_DEPS_CLOSE = "# <<< shipit-managed python release deps <<<"
PIXI_TREE_SITTER_DEPS_KEY = "pixi.toml#shipit-tree-sitter-release-deps"
PIXI_TREE_SITTER_DEPS_OPEN = "# >>> shipit-managed tree-sitter release deps (do not edit; regenerate via `shipit install`) >>>"
PIXI_TREE_SITTER_DEPS_CLOSE = "# <<< shipit-managed tree-sitter release deps <<<"
PIXI_TREE_SITTER_DEPS_ANCHOR = "[dependencies]"
# (unit key, toolchain signal, open, close, anchor, packaged data file) — the
# catalog rows :func:`load_units` appends per requested toolchain, in this order.
TOOLCHAIN_UNITS = (
    (
        PIXI_RUST_DEPS_KEY,
        TOOLCHAIN_RUST,
        PIXI_RUST_DEPS_OPEN,
        PIXI_RUST_DEPS_CLOSE,
        PIXI_LINT_DEPS_ANCHOR,
        "pixi-rust-lint-deps-block.toml",
    ),
    (
        PIXI_RUST_RELEASE_DEPS_KEY,
        TOOLCHAIN_RUST,
        PIXI_RUST_RELEASE_DEPS_OPEN,
        PIXI_RUST_RELEASE_DEPS_CLOSE,
        PIXI_NODE_DEPS_ANCHOR,
        "pixi-rust-release-deps-block.toml",
    ),
    (
        PIXI_RUST_RELEASE_TOOLCHAIN_KEY,
        TOOLCHAIN_RUST,
        PIXI_RUST_RELEASE_TOOLCHAIN_OPEN,
        PIXI_RUST_RELEASE_TOOLCHAIN_CLOSE,
        PIXI_NODE_DEPS_ANCHOR,
        "pixi-rust-release-toolchain-block.toml",
    ),
    (
        PIXI_PYTHON_RELEASE_DEPS_KEY,
        TOOLCHAIN_PYTHON,
        PIXI_PYTHON_RELEASE_DEPS_OPEN,
        PIXI_PYTHON_RELEASE_DEPS_CLOSE,
        PIXI_NODE_DEPS_ANCHOR,
        "pixi-python-release-deps-block.toml",
    ),
    (
        PIXI_GO_DEPS_KEY,
        TOOLCHAIN_GO,
        PIXI_GO_DEPS_OPEN,
        PIXI_GO_DEPS_CLOSE,
        PIXI_LINT_DEPS_ANCHOR,
        "pixi-go-lint-deps-block.toml",
    ),
    (
        PIXI_LUA_DEPS_KEY,
        TOOLCHAIN_LUA,
        PIXI_LUA_DEPS_OPEN,
        PIXI_LUA_DEPS_CLOSE,
        PIXI_LINT_DEPS_ANCHOR,
        "pixi-lua-lint-deps-block.toml",
    ),
    (
        PIXI_NODE_DEPS_KEY,
        TOOLCHAIN_NODE,
        PIXI_NODE_DEPS_OPEN,
        PIXI_NODE_DEPS_CLOSE,
        PIXI_NODE_DEPS_ANCHOR,
        "pixi-node-deps-block.toml",
    ),
    (
        PIXI_TREE_SITTER_DEPS_KEY,
        TOOLCHAIN_TREE_SITTER,
        PIXI_TREE_SITTER_DEPS_OPEN,
        PIXI_TREE_SITTER_DEPS_CLOSE,
        PIXI_TREE_SITTER_DEPS_ANCHOR,
        "pixi-tree-sitter-release-deps-block.toml",
    ),
)

#: The name of the managed lint environment the env block above defines
#: (``pixi-lint-env-block.toml``: ``lint = ["lint"]``) — where the fleet-pinned
#: toolchain (including ``lefthook``) lives on every consumer. Callers that must
#: run a lint-env binary (Tree provisioning's hook activation, #443) pin this
#: environment rather than guessing at PATH.
LINT_ENV = "lint"

#: The ONE operator-facing recovery command for "(re)activate the checks".
#: Hook activation is a side effect of ``shipit install`` — there is NO
#: standalone hook-activation verb — so every operator-facing recovery
#: instruction speaks *shipit* and re-runs install (idempotent), never the
#: internal ``lefthook``/``pixi`` layer under it. The pinned launcher form
#: (``./bin/shipit install``) is what a consumer has on PATH; prose may say
#: ``shipit install``. Referenced everywhere an operator is told how to fix a
#: missing/failed activation, so the guidance can never drift or leak the
#: internal layer (reviewers re-flag a leaked ``lefthook install`` every round).
HOOK_RECOVERY_CMD = "./bin/shipit install"

# The pixi-manifest seed (ADP00-WS09, #432). A stock consumer with NO pixi.toml
# is the headline adoption case, but the three managed blocks above are tables
# and keys only — pixi refuses a manifest with no `[workspace]`/`[project]`/
# `[package]` table — so splicing them into an empty file self-blocks the very
# first install commit (the freshly-synced pre-commit hook shells into pixi,
# which rejects the manifest). When the consumer has no pixi.toml at all, a
# fresh install first seeds this minimal VALID `[workspace]` table and then
# splices the managed blocks under it. The seed is SCAFFOLD, not a managed
# unit: written once, never hashed into `[managed]`, consumer-owned (and
# freely editable) from its first commit — a consumer WITH a manifest never
# sees it, and a re-install never rewrites it.
PIXI_SEED_CHANNELS = ("conda-forge",)
PIXI_SEED_PLATFORMS = ("linux-64", "linux-aarch64", "osx-arm64")


def workspace_name(raw: str) -> str:
    """A pixi-safe workspace name from a repo directory name.

    Conservative slug: keep the characters directory-and-repo names ordinarily
    carry (alphanumerics, ``-``, ``_``, ``.``), collapse anything else to ``-``,
    and never return empty — so an exotic directory name can neither break the
    seeded TOML string nor produce a name pixi rejects.
    """
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-.")
    return name or "workspace"


def pixi_manifest_seed(name: str) -> str:
    """The minimal VALID pixi manifest seeded when a consumer has none.

    Just the required ``[workspace]`` table — name from the consumer root,
    default channels/platforms — so pixi parses the file from the first
    commit. The managed blocks splice in beneath it via their own anchors.
    """
    channels = ", ".join(f'"{c}"' for c in PIXI_SEED_CHANNELS)
    platforms = ", ".join(f'"{p}"' for p in PIXI_SEED_PLATFORMS)
    return (
        "# pixi workspace — seeded by `shipit install` (the managed blocks below\n"
        "# need a valid manifest). Consumer-owned from here on: edit freely.\n"
        "[workspace]\n"
        f'name = "{workspace_name(name)}"\n'
        f"channels = [{channels}]\n"
        f"platforms = [{platforms}]\n"
    )


# The HAR01 agent harness (docs/legacy-prd/har01-coordinator-guard-and-role-prompts.md):
# the three GENERATED subagent agent-defs and the committed `PreToolUse` hook line
# join the managed set so a consumer's agents follow the same dev cycle + guard.
#
# Agent-defs: whole-file units, sourced from `.claude/agents/<role>.md` — the same
# repo-root-in-dev / wheel-package-data split skills use (force-included via
# pyproject). They are generated (`pixi run regen-roles`); install only vendors the
# committed output, it never regenerates.
#
# settings hook: NOT a whole-file unit. `.claude/settings.json` is Claude-Code-owned
# structured JSON a consumer fills with their own permissions/env/hooks; shipit owns
# ONLY its one `PreToolUse` entry. So it is a `kind="block"` unit with a JSON splice
# (`fmt=FMT_JSON_HOOK`) instead of comment-marker text splice: the managed "inner" is
# shipit's canonical PreToolUse entry, identified in the consumer file by its command
# marker. Reconciliation is the standard four-case `decide()` on that entry's hash —
# the consumer's other settings are merged through untouched, never clobbered, and a
# consumer edit to shipit's own entry surfaces as an OVERRIDE like any other unit.
#
# This entry's COMMAND carries a stricter contract than the other four managed
# settings-hook units below: it is the ADR-0012 coordinator-edit guard, so its
# command (`pixi run --manifest-path "$CLAUDE_PROJECT_DIR"/pixi.toml -- ./bin/shipit
# hook pretooluse`, ADR-0038 — the manifest pin mirrors the pixienv adapter so a
# leaked PIXI_PROJECT_MANIFEST can't resolve the wrong project) fails CLOSED — a
# non-zero exit from the resolution chain blocks the tool call (`exit 2`) —
# where the other four legitimately fail open on a missing launcher (#491). See
# `tests.conftest.managed_pretooluse_hook_command` (the single source of this
# exact string) vs `managed_cc_hook_command` (the other four).
AGENTS_DEF_DIR = ".claude/agents"
#: The AGY native custom-agent def dir (issue #989): the generated
#: ``.agents/agents/<name>/agent.md`` defs `agy --agent <name>` reads. Managed as
#: whole-file units the same way as the Claude agent-defs, sourced from
#: :func:`agy_agents_root` (the force-included wheel data, or the dev checkout).
AGY_AGENTS_DEF_DIR = ".agents/agents"
SETTINGS_FILE = ".claude/settings.json"
SETTINGS_KEY = ".claude/settings.json#shipit-pretooluse-hook"
# The substring that identifies shipit's managed PreToolUse entry in a consumer's
# settings.json, independent of the runner prefix (`pixi run`, a bare path, etc.).
SETTINGS_HOOK_MARKER = "shipit hook pretooluse"

#: The substring EVERY shipit-managed hook entry's command carries — the
#: markers here are all ``shipit hook <verb>`` forms. The retired-hooks pass
#: (#619) uses it as its protection predicate: a consumer-local entry matching
#: a retirement marker is removed only when it is NOT shipit's own managed
#: entry. Needed because the managed SessionStart command itself runs
#: ``./bin/setup-dev-env.sh`` inline, which the setup-dev-env retirement
#: marker would otherwise match (see :func:`shipit.install.splice.is_retired_hook`).
MANAGED_HOOK_COMMAND_MARKER = "shipit hook"

# The HAR02 eval wire (docs/legacy-prd/har02-run-eval.md) adds two more committed
# settings.json hook lines — the terminal-hook eval boundary — each its own
# event array + command marker, reconciled by the SAME event-keyed JSON-hook
# splice as PreToolUse. Stop/SubagentStop entries carry no `matcher` (they bind
# to no tool), so the entry shape is just `{"hooks": [...]}`.
SETTINGS_STOP_KEY = ".claude/settings.json#shipit-stop-hook"
SETTINGS_STOP_MARKER = "shipit hook stop"
SETTINGS_SUBAGENTSTOP_KEY = ".claude/settings.json#shipit-subagentstop-hook"
SETTINGS_SUBAGENTSTOP_MARKER = "shipit hook subagent-stop"

#: The pinned ``bin/shipit`` launcher's unit key AND dest (ADR-0033) — the
#: whole-file bootstrap unit that execs the repo's pinned build via uv. Named
#: so its consumers (the catalog below, selfcert's launcher probe, a
#: ``[managed.decline]`` entry, #600) spell one identifier, never a scattered
#: literal.
SHIPIT_LAUNCHER_FILE = "bin/shipit"

# The Layer 0 bootstrap script (#547): provisions the base system (pixi + uv
# at their pins, then the pixi env solves) that everything above — the managed
# lint env, the pinned launcher's uv resolve — rides on. Shipped like
# `bin/shipit` (an executable whole-file bootstrap unit); shipit-self commits
# a byte-identical copy at the same path (the reconcile-to-noop dogfood
# guarantee).
SETUP_DEV_ENV_FILE = "bin/setup-dev-env.sh"

# The CDX01 generic `./agent-start` launcher (#627): ONE managed entry point
# (`./agent-start <agent> [args...]`) whose common start path — usage, host
# dispatch, CLI presence check, arg forwarding — is written once, dispatching
# through a small host strategy table to narrow per-host launch functions
# (docs/dev/agent-host-seams.md): `claude` mints a `sess-<utc>-<pid>` id and
# execs `claude --worktree <id>` (the WorktreeCreate pre-launch seam, SES01
# Layer D / ADR-0027); `codex` execs the pinned `./bin/shipit session codex`
# (no pre-launch seam — explicit Tree provisioning, then codex). Ships like
# `bin/shipit` (an executable whole-file bootstrap unit at the repo root).
AGENT_LAUNCHER_FILE = "agent-start"

# The SES01 session-bootstrap set (docs/legacy-prd/session-bootstrap.md, ADR-0027):
# the SessionStart activation hook line (Layer A — `shipit hook sessionstart`
# writes the repo's toolchain activation into CLAUDE_ENV_FILE). It joins the
# managed set so adopting a repo turns the capability on with no manual wiring.
# The hook line is one more JSON-hook unit over the same settings.json, owning
# its event. (The Layer D repo-root launcher is the generic `./agent-start`
# unit — see AGENT_LAUNCHER_FILE; the agent-specific `claude-start`/`codex-start`
# shims were retired in #815.)
#
# Failure posture (#848, decided — copilot asked whether the missing fail-open
# guard on the trailing `./bin/shipit hook sessionstart` was deliberate): the
# ABSENCE cases are fail-open (`cd` failing and the launcher-presence probe both
# skip with exit 0 — an unprovisioned checkout must never brick a session), but
# a PRESENT launcher's exit code propagates UNCHANGED, deliberately fail-loud.
# SessionStart is non-blocking in Claude Code (a non-zero hook surfaces stderr
# without denying anything), and a launcher that ran and errored is degraded
# activation the operator must see — wrapping it in `|| exit 0` would hide that
# behind a green session start. Same split as the additive-hook shape in
# tests/conftest.py `managed_cc_hook_command`: fail-open is for a runtime that
# is genuinely absent, never for a hook that ran and errored.
SETTINGS_SESSIONSTART_KEY = ".claude/settings.json#shipit-sessionstart-hook"
SETTINGS_SESSIONSTART_MARKER = "shipit hook sessionstart"

# The ADR-0027 WorktreeCreate adapter wiring (#443, Finding B): the managed
# `agent-start` launcher promises that `claude --worktree` provisions the
# session Tree via `shipit hook worktreecreate`, and shipit's own settings wire
# that hook — but the managed settings variant drifted and never did, so a
# stock consumer's `--worktree` fell through to Claude Code's NATIVE worktree
# (`.claude/worktrees/<id>`), contradicting ADR-0014/0027 (Trees are
# dissociated clones, never native worktrees). One more JSON-hook unit over the
# same settings.json, owning its event, reconciled like the other four.
SETTINGS_WORKTREECREATE_KEY = ".claude/settings.json#shipit-worktreecreate-hook"
SETTINGS_WORKTREECREATE_MARKER = "shipit hook worktreecreate"

# The CDX01 Codex project layer (#603): codex 0.139+ loads trusted repo-local
# `.codex/config.toml` and `.codex/hooks.json`, and its hook commands run with
# the SESSION CWD — so the managed hook entries route straight to the shared
# `./bin/shipit hook ...` verbs with only that env adaptation (no
# `$CLAUDE_PROJECT_DIR` cd; the commands resolve the git root from the session
# cwd before calling the root-pinned launcher/manifest), never a Codex fork of
# the lifecycle logic.
#
# config.toml is a WHOLE-FILE unit: repo-local `.codex/config.toml` is new
# surface (no repo carries one; shipit's own repo carried only a hand-written
# hooks.json, now spliced into line), the layer is deliberately thin, and
# personal Codex config belongs in `~/.codex/config.toml` ($CODEX_HOME), which
# codex layers over this file anyway — so shipit owns the repo-local file
# outright and a consumer edit surfaces as an OVERRIDE like any other
# whole-file unit.
#
# hooks.json REUSES the settings.json JSON-hook splice unchanged (the splicer
# is already dest/event/marker-generic): two `FMT_JSON_HOOK` block units over
# the one file, each owning shipit's single entry in its event array — the
# SessionStart activation/advisory hook (fail-open, additive; the entry
# synthesizes the verb's `{"cwd": ...}` payload via python `os.getcwd()` because
# codex supplies no Claude-shaped payload on stdin) and the PreToolUse
# coordinator tool-guard (fail-CLOSED, the ADR-0038 posture: a resolution
# failure blocks the tool call with exit 2 rather than silently allowing an
# unchecked edit; this entry synthesizes NO payload — it resolves the git root
# via `git rev-parse --show-toplevel` and passes codex's native stdin through). The
# guard entry carries NO matcher: Codex tool names are not Claude's, so the
# entry binds to every tool event and the shared verb's own `is_edit_tool`
# gate scopes the verdict (a non-edit payload is allowed through silently).
# The consumer's other hooks — and their other keys — merge through untouched,
# exactly as on `.claude/settings.json`. The event names and command markers
# are shared with the Claude units on purpose: the verbs (and their marker
# substrings) are the SAME lifecycle logic, only the host file differs.
CODEX_CONFIG_FILE = ".codex/config.toml"
CODEX_HOOKS_FILE = ".codex/hooks.json"
CODEX_SESSIONSTART_KEY = ".codex/hooks.json#shipit-sessionstart-hook"
CODEX_PRETOOLUSE_KEY = ".codex/hooks.json#shipit-pretooluse-hook"

# The settings.json hooks-event arrays each JSON-hook unit owns one entry of.
EVENT_PRETOOLUSE = "PreToolUse"
EVENT_STOP = "Stop"
EVENT_SUBAGENTSTOP = "SubagentStop"
EVENT_SESSIONSTART = "SessionStart"
EVENT_WORKTREECREATE = "WorktreeCreate"

FMT_MARKERS = "markers"  # block splice via open/close comment markers (default)
FMT_JSON_HOOK = "json-hook"  # block splice into a settings.json hooks-event array
FMT_ENV_MEMBER = (
    "env-member"  # merge a managed feature into a pixi [environments] entry
)


def env_member_token(env: str, required: Sequence[str]) -> str:
    """The canonical membership token an ``FMT_ENV_MEMBER`` unit hashes on.

    The unit's managed contribution is not a text block but an INVARIANT — that
    ``env`` composes every ``required`` feature — so both the desired hash and the
    consumer read (:func:`shipit.install.splice.extract_env_member`) reduce to
    this one string. It is deliberately independent of the consumer's OTHER
    features in ``env``: those are the consumer's own config (ADR-0047), so a
    consumer who wired the feature into a hand-authored env still reconciles to a
    clean NOOP.
    """
    return json.dumps(
        {"environment": env, "requires": sorted(required)}, sort_keys=True
    )


@dataclass(frozen=True)
class Unit:
    """One managed unit — a whole file or a marker-delimited block.

    ``content`` is the desired bytes: a file's full contents, or (for a block)
    the inner text that lives between the markers.
    """

    key: str  # the [managed] table key
    dest: str  # path relative to the consumer root
    kind: str  # "file" | "block"
    content: bytes
    executable: bool = False
    # Block units only: the delimiters that fence shipit's region in a
    # consumer-owned file, and (for a TOML table) the header the block anchors
    # under on a first insert. Default to the AGENTS.md HTML-comment markers.
    open_marker: str = BLOCK_OPEN
    close_marker: str = BLOCK_CLOSE
    anchor: str | None = None
    # Block units only: how the managed region is extracted from / spliced into the
    # consumer file. ``FMT_MARKERS`` (default) uses the comment-marker pair above;
    # ``FMT_JSON_HOOK`` parses ``settings.json`` and owns just shipit's one entry in
    # the ``event`` hooks-array (identified by ``marker``), so the consumer's other
    # settings — and shipit's other hook entries — merge through untouched;
    # ``FMT_ENV_MEMBER`` owns just the ``required_features`` MEMBERSHIP in the pixi
    # ``[environments]`` entry named ``env_name`` (the consumer's other features in
    # that env are their own config), so ``content`` is the packaged default line
    # used only when the env is absent — the hash is over the membership invariant.
    fmt: str = FMT_MARKERS
    event: str = EVENT_PRETOOLUSE
    marker: str = SETTINGS_HOOK_MARKER
    # FMT_ENV_MEMBER only: the environment whose feature list must compose every
    # feature in ``required_features`` (a managed invariant, ADR-0047/ADR-0066).
    env_name: str | None = None
    required_features: tuple[str, ...] = ()

    def desired_inner(self) -> str:
        """A block unit's canonical inner text (newline-trimmed)."""
        return self.content.decode("utf-8").strip("\n")

    def desired_hash(self) -> str:
        """The ``sha256:`` pristine hash of this unit's desired content."""
        if self.fmt == FMT_ENV_MEMBER:
            token = env_member_token(self.env_name or "", self.required_features)
            return config.content_hash(token.encode("utf-8"))
        if self.kind == "block":
            return config.content_hash(self.desired_inner().encode("utf-8"))
        return config.content_hash(self.content)


def data_bytes(*parts: str) -> bytes:
    """Read a ``shipit.data`` file via the resources Traversable API."""
    return resources.files("shipit.data").joinpath(*parts).read_bytes()


def skills_root():
    """The bundled skills tree — wheel package data, or the repo root in dev.

    Returns a Traversable (installed wheel) or a :class:`Path` (editable/source
    checkout, where the store lives at the repo root as ``.shipit-skills/`` — a
    dedicated, non-colliding dir, force-included into the built wheel as
    ``shipit/data/skills``). Both honor the ``iterdir`` / ``is_dir`` / ``is_file``
    / ``read_bytes`` protocol :func:`walk_files` uses. The dot-prefix keeps
    shipit's OWN skill store from colliding with the public ``skills/`` convention
    a repo uses to publish its own skills (#921).
    """
    bundled = resources.files("shipit.data").joinpath("skills")
    if bundled.is_dir():
        return bundled
    return Path(__file__).resolve().parents[3] / ".shipit-skills"


def agents_root():
    """The bundled subagent agent-defs — wheel package data, or the repo root in dev.

    Mirrors :func:`skills_root`: the generated ``.claude/agents/<role>.md`` files
    live at the repo root (where Claude Code reads them for shipit-self) and are
    force-included into the wheel at ``shipit/data/agents`` (pyproject). Returns a
    Traversable (installed wheel) or a :class:`Path` (editable checkout).
    """
    bundled = resources.files("shipit.data").joinpath("agents")
    if bundled.is_dir():
        return bundled
    return Path(__file__).resolve().parents[3] / ".claude" / "agents"


def agy_agents_root():
    """The bundled AGY native custom-agent defs — wheel package data, or the repo
    root in dev (issue #989).

    The AGY sibling of :func:`agents_root`: the generated
    ``.agents/agents/<name>/agent.md`` defs live at the repo root (where ``agy
    --agent <name>`` reads them for shipit-self) and are force-included into the
    wheel at ``shipit/data/agy-agents`` (pyproject). Returns a Traversable
    (installed wheel) or a :class:`Path` (editable checkout).
    """
    bundled = resources.files("shipit.data").joinpath("agy-agents")
    if bundled.is_dir():
        return bundled
    return Path(__file__).resolve().parents[3] / ".agents" / "agents"


def canonical_hook_entry(entry: dict) -> str:
    """The stable serialization of a settings.json hooks-event entry.

    Both the desired (bundled) entry and the consumer's extracted entry pass through
    this one function, so the unit's hash compares STRUCTURE, not byte-formatting —
    a consumer who reformats settings.json (whitespace, key order) still reconciles
    to NOOP as long as shipit's entry is semantically unchanged.
    """
    return json.dumps(entry, indent=2, sort_keys=True)


def walk_files(node, prefix: str = ""):
    """Yield ``(relpath, bytes)`` for every file under ``node``, depth-first sorted."""
    for child in sorted(node.iterdir(), key=lambda p: p.name):
        rel = f"{prefix}{child.name}"
        if child.is_dir():
            yield from walk_files(child, prefix=f"{rel}/")
        elif child.is_file():
            yield rel, child.read_bytes()


def load_units(*, toolchains: frozenset[str] = frozenset()) -> list[Unit]:
    """The managed set, in a stable order (skills, then the AGENTS block, then bootstrap).

    ``toolchains`` (#547 Layer 1) names the conditional per-toolchain pixi dep
    blocks to include — any of :data:`TOOLCHAIN_RUST` / :data:`TOOLCHAIN_GO` /
    :data:`TOOLCHAIN_NODE` / :data:`TOOLCHAIN_PYTHON`, as detected from the
    consumer's tracked manifests
    (:func:`shipit.install.reconcile.detect_toolchains`), or
    :data:`TOOLCHAIN_TREE_SITTER`, unioned off the consumer's DECLARED
    tree-sitter toolchain leg (#890 — no manifest signals a grammar;
    :func:`shipit.verbs.install._declared_signals`). The zero-arg call
    returns the unconditional catalog — which since #794 includes the
    launcher-deps block (uv for the pinned ``bin/shipit``, #758): every
    consumer's managed tasks resolve through the launcher, so its
    prerequisite is signal-independent.
    """
    units: list[Unit] = []

    for rel, content in walk_files(skills_root()):
        units.append(
            Unit(
                key=f".shipit-skills/{rel}",
                dest=f".shipit-skills/{rel}",
                kind="file",
                content=content,
            )
        )

    units.append(
        Unit(
            key=AGENTS_KEY,
            dest=AGENTS_FILE,
            kind="block",
            content=data_bytes("agents-block.md"),
        )
    )

    # The managed `.gitignore` release-output block (#906): a marker block in the
    # consumer-owned `.gitignore` ignoring shipit's own repo-root release-stage
    # outputs, so a root-level single-crate `cargo publish` stops aborting on the
    # dirty tree they create — see the GITIGNORE_* constants' comment. No anchor:
    # the block appends at EOF (creating `.gitignore` if the consumer has none).
    units.append(
        Unit(
            key=GITIGNORE_KEY,
            dest=GITIGNORE_FILE,
            kind="block",
            content=data_bytes("gitignore-block"),
            open_marker=GITIGNORE_OPEN,
            close_marker=GITIGNORE_CLOSE,
        )
    )

    units.append(
        Unit(
            key=SHIPIT_LAUNCHER_FILE,
            dest=SHIPIT_LAUNCHER_FILE,
            kind="file",
            content=data_bytes("bootstrap", "shipit"),
            executable=True,
        )
    )

    # The Layer 0 base-system bootstrap (#547): reconcile pixi + uv to their
    # pins from sha256-verified GitHub release tarballs, then pre-solve the
    # pixi envs — what makes a fresh clone / cloud session / stock Ubuntu box
    # survive its first `pixi run` (and gives the ADR-0033 `bin/shipit`
    # launcher the uv it rides). Runs from the managed SessionStart hook,
    # ahead of `shipit hook sessionstart`; fail-open, loud, idempotent. On
    # repos still carrying the retired release-sync script this managed unit
    # takes over the SAME path (their old copy surfaces as an OVERRIDE at the
    # reconcile — the human decides at merge).
    units.append(
        Unit(
            key=SETUP_DEV_ENV_FILE,
            dest=SETUP_DEV_ENV_FILE,
            kind="file",
            content=data_bytes("bootstrap", "setup-dev-env.sh"),
            executable=True,
        )
    )

    # The CDX01 generic `./agent-start` launcher (#627): the one repo-root
    # entry point (`./agent-start <agent> [args...]`) dispatching through the
    # host strategy table — see the AGENT_LAUNCHER_FILE constant's comment.
    units.append(
        Unit(
            key=AGENT_LAUNCHER_FILE,
            dest=AGENT_LAUNCHER_FILE,
            kind="file",
            content=data_bytes("bootstrap", "agent-start"),
            executable=True,
        )
    )

    # The lint-check units (docs/legacy-prd/lint-checks.md): the thin lefthook caller and the
    # `lint = "shipit lint"` task block in the consumer's pixi.toml.
    units.append(
        Unit(
            key=LEFTHOOK_FILE,
            dest=LEFTHOOK_FILE,
            kind="file",
            content=data_bytes("lefthook.yml"),
        )
    )
    # The lint tool configs (#436): markdownlint and yamllint auto-discover
    # these at the repo root, so delivering them is what makes the managed
    # caller's whole-tree lint green on a stock consumer right after install.
    for dest, data_file in LINT_CONFIG_UNITS:
        units.append(
            Unit(key=dest, dest=dest, kind="file", content=data_bytes(data_file))
        )
    units.append(
        Unit(
            key=PIXI_KEY,
            dest=PIXI_FILE,
            kind="block",
            content=data_bytes("pixi-tasks-block.toml"),
            open_marker=PIXI_OPEN,
            close_marker=PIXI_CLOSE,
            anchor=PIXI_ANCHOR,
        )
    )
    # The thin `test` caller (TOL01-WS01, ADR-0039) — its own sibling block in
    # the same [tasks] table, so the reconcile's task-ambiguity guard can skip
    # it alone for a consumer whose own manifest already defines a `test` task
    # (see the PIXI_TEST_TASK_KEY comment above).
    units.append(
        Unit(
            key=PIXI_TEST_TASK_KEY,
            dest=PIXI_FILE,
            kind="block",
            content=data_bytes("pixi-test-task-block.toml"),
            open_marker=PIXI_TEST_TASK_OPEN,
            close_marker=PIXI_TEST_TASK_CLOSE,
            anchor=PIXI_ANCHOR,
        )
    )

    # The ADP00 managed consumer environment (docs/legacy-prd/adoption.md): the lint
    # feature/dependency block (fleet-pinned toolchain) and the lint environment
    # definition, siblings of the tasks block in the same consumer pixi.toml.
    units.append(
        Unit(
            key=PIXI_LINT_DEPS_KEY,
            dest=PIXI_FILE,
            kind="block",
            content=data_bytes("pixi-lint-deps-block.toml"),
            open_marker=PIXI_LINT_DEPS_OPEN,
            close_marker=PIXI_LINT_DEPS_CLOSE,
            anchor=PIXI_LINT_DEPS_ANCHOR,
        )
    )
    units.append(
        Unit(
            key=PIXI_ENVS_KEY,
            dest=PIXI_FILE,
            kind="block",
            content=data_bytes("pixi-lint-env-block.toml"),
            open_marker=PIXI_ENVS_OPEN,
            close_marker=PIXI_ENVS_CLOSE,
            anchor=PIXI_ENVS_ANCHOR,
            # A MEMBERSHIP merge, not a marker block: the lint env must compose the
            # managed `shipit-lexd` feature (ADR-0066), but WHICH base features the
            # env carries is consumer config (ADR-0047). Owning the whole `lint`
            # line would collide with a consumer's own `[environments] lint` and be
            # skipped — leaving lexd unwired (lint breaks, no `provision` fallback).
            # So this unit owns only `shipit-lexd`'s membership: the packaged
            # `content` seeds a fresh env, and an existing consumer env has
            # `shipit-lexd` merged in (append, never replace).
            fmt=FMT_ENV_MEMBER,
            env_name="lint",
            required_features=("shipit-lexd",),
        )
    )

    # The managed lexd feature (ARF02-WS06, ADR-0066): the reserved
    # `[feature.shipit-lexd]` block (channel + fleet-pinned lexd), a fresh table
    # appended at EOF (anchor-less, so it never re-declares a consumer table),
    # wired into the lint env by the environments block above. Replaces the
    # retired `provision lexd` — see the PIXI_LEXD_KEY comment for the whole story.
    units.append(
        Unit(
            key=PIXI_LEXD_KEY,
            dest=PIXI_FILE,
            kind="block",
            content=data_bytes("pixi-lexd-block.toml"),
            open_marker=PIXI_LEXD_OPEN,
            close_marker=PIXI_LEXD_CLOSE,
            anchor=None,
        )
    )

    # The unconditional launcher-deps block (#758, TOL02-WS17 #794): uv for
    # the pinned bin/shipit launcher, in the default env — see the
    # PIXI_LAUNCHER_DEPS_KEY comment above for the whole story.
    units.append(
        Unit(
            key=PIXI_LAUNCHER_DEPS_KEY,
            dest=PIXI_FILE,
            kind="block",
            content=data_bytes("pixi-launcher-deps-block.toml"),
            open_marker=PIXI_LAUNCHER_DEPS_OPEN,
            close_marker=PIXI_LAUNCHER_DEPS_CLOSE,
            anchor=PIXI_LAUNCHER_DEPS_ANCHOR,
        )
    )

    # The conditional per-toolchain dep blocks (#547 Layer 1): the ONLY
    # signal-gated rows — appended only when the caller detected the signal.
    # The unconditional catalog above (launcher-deps included) ships on every
    # call regardless; these rows add only the toolchain-specific blocks.
    # rust/go splice under the same `[feature.lint.dependencies]` anchor as the
    # managed lint-deps block — sibling marker blocks in one table (splice_block
    # places each right after the anchor header; coexistence is fine).
    for key, signal, open_marker, close_marker, anchor, data_file in TOOLCHAIN_UNITS:
        if signal in toolchains:
            units.append(
                Unit(
                    key=key,
                    dest=PIXI_FILE,
                    kind="block",
                    content=data_bytes(data_file),
                    open_marker=open_marker,
                    close_marker=close_marker,
                    anchor=anchor,
                )
            )

    # The HAR01 harness (docs/legacy-prd/har01-coordinator-guard-and-role-prompts.md): the
    # generated subagent agent-defs (whole files) and the committed PreToolUse hook
    # line (a JSON splice into the consumer's settings.json).
    for rel, content in walk_files(agents_root()):
        units.append(
            Unit(
                key=f"{AGENTS_DEF_DIR}/{rel}",
                dest=f"{AGENTS_DEF_DIR}/{rel}",
                kind="file",
                content=content,
            )
        )

    # The AGY native custom-agent defs (#989): `.agents/agents/<name>/agent.md`,
    # delivered as whole-file units exactly like the Claude agent-defs above so
    # `agy --agent reviewer` reads shipit's managed reviewer posture from a
    # consumer's checkout. Guarded on the source dir existing (a checkout that
    # predates the generated def, or a wheel built without it, simply carries no
    # AGY unit) so `load_units` never raises on a missing source tree.
    agy_root = agy_agents_root()
    if agy_root.is_dir():
        for rel, content in walk_files(agy_root):
            units.append(
                Unit(
                    key=f"{AGY_AGENTS_DEF_DIR}/{rel}",
                    dest=f"{AGY_AGENTS_DEF_DIR}/{rel}",
                    kind="file",
                    content=content,
                )
            )

    # Store each desired entry already canonicalized, so its hash matches a consumer
    # entry extracted + canonicalized through the same function (formatting-immune).
    # The PreToolUse coordinator-guard (HAR01), the HAR02 eval terminal hooks
    # (Stop = the coordinator run, SubagentStop = each subagent run), the SES01
    # SessionStart activation hook (coordinator env into CLAUDE_ENV_FILE), and the
    # ADR-0027 WorktreeCreate adapter (#443: `claude --worktree` mints a central-root
    # Tree, never a native worktree) are five JSON-hook units over the SAME
    # settings.json, each owning one event array.
    for key, marker, event, data_file in (
        (
            SETTINGS_KEY,
            SETTINGS_HOOK_MARKER,
            EVENT_PRETOOLUSE,
            "claude-settings-pretooluse.json",
        ),
        (
            SETTINGS_STOP_KEY,
            SETTINGS_STOP_MARKER,
            EVENT_STOP,
            "claude-settings-stop.json",
        ),
        (
            SETTINGS_SUBAGENTSTOP_KEY,
            SETTINGS_SUBAGENTSTOP_MARKER,
            EVENT_SUBAGENTSTOP,
            "claude-settings-subagentstop.json",
        ),
        (
            SETTINGS_SESSIONSTART_KEY,
            SETTINGS_SESSIONSTART_MARKER,
            EVENT_SESSIONSTART,
            "claude-settings-sessionstart.json",
        ),
        (
            SETTINGS_WORKTREECREATE_KEY,
            SETTINGS_WORKTREECREATE_MARKER,
            EVENT_WORKTREECREATE,
            "claude-settings-worktreecreate.json",
        ),
    ):
        hook_entry = json.loads(data_bytes(data_file))
        units.append(
            Unit(
                key=key,
                dest=SETTINGS_FILE,
                kind="block",
                content=canonical_hook_entry(hook_entry).encode("utf-8"),
                fmt=FMT_JSON_HOOK,
                event=event,
                marker=marker,
            )
        )

    # The CDX01 Codex project layer (#603): the thin whole-file config and the
    # two `.codex/hooks.json` JSON-hook units — the SessionStart hook and the
    # PreToolUse tool guard — routing to the same shared `shipit hook` verbs as
    # the Claude units above (see the CODEX_* constants' comment for the whole
    # design: whole-file vs splice, the cwd/env adaptation, the fail postures).
    units.append(
        Unit(
            key=CODEX_CONFIG_FILE,
            dest=CODEX_CONFIG_FILE,
            kind="file",
            content=data_bytes("codex-config.toml"),
        )
    )
    for key, marker, event, data_file in (
        (
            CODEX_PRETOOLUSE_KEY,
            SETTINGS_HOOK_MARKER,
            EVENT_PRETOOLUSE,
            "codex-hooks-pretooluse.json",
        ),
        (
            CODEX_SESSIONSTART_KEY,
            SETTINGS_SESSIONSTART_MARKER,
            EVENT_SESSIONSTART,
            "codex-hooks-sessionstart.json",
        ),
    ):
        hook_entry = json.loads(data_bytes(data_file))
        units.append(
            Unit(
                key=key,
                dest=CODEX_HOOKS_FILE,
                kind="block",
                content=canonical_hook_entry(hook_entry).encode("utf-8"),
                fmt=FMT_JSON_HOOK,
                event=event,
                marker=marker,
            )
        )
    return units
