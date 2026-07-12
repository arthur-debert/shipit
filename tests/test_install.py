"""Tests for the install domain (CLI02-WS01) — typed values in, typed values out.

The layout mirrors the promoted seam (ADR-0030):

- the pure cores test as plain functions: the four-case managed decision and
  three-case retired decision (:mod:`shipit.install.reconcile`), the text
  splicers (:mod:`shipit.install.splice`), the packaged catalog
  (:mod:`shipit.install.units`);
- ``gather → reconcile`` asserts on the frozen ``Plan``; ``apply`` asserts on
  the typed ``InstallResult`` + the real filesystem + the recorded git/gh
  boundary — no capsys parsing of report text;
- the renderers (:mod:`shipit.verbs.install`) test as pure string functions;
- a thin argv→exit-code smoke layer drives the click command.
"""

import json
import logging
import os
import shutil
import stat
import subprocess
import tomllib
from dataclasses import replace as dc_replace
from pathlib import Path

import pytest
import yaml
from conftest import (
    PIXI_ABSENCE_GUARD,
    managed_cc_hook_command,
    managed_pretooluse_hook_command,
)

from shipit import config, execrun, gh, git, pixienv
from shipit.execrun import ExecError
from shipit.identity import Sha
from shipit.install import apply as iapply
from shipit.install import reconcile as irec
from shipit.install import selfcert, splice
from shipit.install import units as iunits
from shipit.install.errors import InstallError
from shipit.verbs import install as verb

REPO_ROOT = Path(__file__).resolve().parents[1]


def _exec_result(rc: int, stdout: str = "", stderr: str = "") -> execrun.ExecResult:
    """A canned ExecResult for the injected lefthook-activation boundary."""
    return execrun.ExecResult(
        argv=("lefthook", "install"),
        rc=rc,
        stdout=stdout,
        stderr=stderr,
        duration_ms=1,
    )


def _plan(root) -> irec.Plan:
    """gather → reconcile: the typed pipeline up to (and excluding) any write."""
    units = iunits.load_units()
    retired = irec.load_retired()
    retired_hooks = irec.load_retired_hooks()
    state = irec.gather(Path(root), units, retired, retired_hooks)
    return irec.reconcile(units, retired, state, retired_hooks)


def _apply(root, mode: str = iapply.MODE_TREE, **kw) -> iapply.InstallResult:
    """reconcile + apply with the verb's PR-body renderer injected (the wiring
    `run()` performs), so PR-mode tests see the real rendered body."""
    plan = _plan(root)
    assert not plan.nothing_to_do, "test drove apply on a no-op plan"
    return iapply.apply(
        plan,
        mode,
        pr_body=lambda before, hooks, rerendered, pin, debt: verb.format_pr_body(
            plan,
            before,
            hooks,
            rerendered=rerendered,
            stamped_version=pin,
            lint_debt=debt,
        ),
        **kw,
    )


def _cert_ok(plan, root, **kw) -> selfcert.CertReport:
    """A passing certification — the injected default for tests that are not
    about self-certification (no pixi solve, no scoped lint, no launcher run)."""
    return selfcert.CertReport(checks=(selfcert.CertCheck(name="stub", ok=True),))


# --------------------------------------------------------------------------
# Pure reconciliation
# --------------------------------------------------------------------------


def test_decide_covers_four_cases():
    # absent -> ADD
    assert (
        irec.decide(consumer_hash=None, pristine_hash=None, desired_hash="d")
        == irec.ADD
    )
    # already current -> NOOP
    assert (
        irec.decide(consumer_hash="d", pristine_hash="p", desired_hash="d") == irec.NOOP
    )
    # untouched since last install -> UPDATE
    assert (
        irec.decide(consumer_hash="p", pristine_hash="p", desired_hash="d")
        == irec.UPDATE
    )
    # consumer-edited -> OVERRIDE
    assert (
        irec.decide(consumer_hash="x", pristine_hash="p", desired_hash="d")
        == irec.OVERRIDE
    )
    # present but never installed by shipit (no pristine) and divergent -> OVERRIDE
    assert (
        irec.decide(consumer_hash="x", pristine_hash=None, desired_hash="d")
        == irec.OVERRIDE
    )


def test_block_extract_and_splice_roundtrip():
    base = "# Consumer AGENTS\n\nSome consumer-owned text.\n"
    spliced = splice.splice_block(base, "managed body")
    assert iunits.BLOCK_OPEN in spliced and iunits.BLOCK_CLOSE in spliced
    # The consumer's own text is preserved.
    assert "Some consumer-owned text." in spliced
    assert splice.extract_block(spliced) == "managed body"
    # Re-splicing replaces only the block, leaving one block.
    again = splice.splice_block(spliced, "new body")
    assert splice.extract_block(again) == "new body"
    assert again.count(iunits.BLOCK_OPEN) == 1
    assert "Some consumer-owned text." in again


def test_extract_block_absent_is_none():
    assert splice.extract_block("no markers here") is None


# --------------------------------------------------------------------------
# The lint-check units (Step 3) — lefthook caller + pixi [tasks] block
# --------------------------------------------------------------------------


def test_load_units_includes_lefthook_and_pixi_task_block():
    units = {u.key: u for u in iunits.load_units()}
    assert iunits.LEFTHOOK_FILE in units
    assert units[iunits.LEFTHOOK_FILE].kind == "file"

    pixi = units[iunits.PIXI_KEY]
    assert pixi.kind == "block"
    assert pixi.dest == "pixi.toml"
    assert pixi.anchor == "[tasks]"
    # The managed pixi TASKS block stays the thin task lines ONLY; the linter
    # deps ride in their own sibling `[feature.lint.dependencies]` block (ADP00,
    # docs/legacy-prd/adoption.md — amending the lint PRD's task-line-only
    # decision), tested below. `provision-lexd` invokes the binary's provision subcommand
    # (ADP00-WS03), so no provisioning script is ever distributed. `changelog`
    # is the release-notes tool's thin caller (TOL01-WS06, ADR-0039: pixi tasks
    # are one-line callers of the verb). Each task
    # invokes the PINNED launcher `./bin/shipit`, never a bare PATH `shipit`
    # (#481, ADR-0033: hooks/tasks ride the repo's pin, PATH is never consulted).
    assert pixi.desired_inner() == (
        'changelog = "./bin/shipit changelog"\n'
        'lint = "./bin/shipit lint"\n'
        'logs = "./bin/shipit logs"\n'
        'provision-lexd = "./bin/shipit provision lexd"'
    )


def test_load_units_includes_the_thin_test_task_block():
    # The thin `test` caller (TOL01-WS01, ADR-0039): its OWN sibling block in
    # the same [tasks] table — pinned-launcher form like the managed `lint` —
    # so the task-ambiguity guard can skip it alone for a consumer whose own
    # manifest already defines a `test` task (shipit's own repo does).
    units = {u.key: u for u in iunits.load_units()}
    test_task = units[iunits.PIXI_TEST_TASK_KEY]
    assert test_task.kind == "block"
    assert test_task.dest == "pixi.toml"
    assert test_task.anchor == "[tasks]"
    assert test_task.desired_inner() == 'test = "./bin/shipit test"'


def test_pixi_block_inserts_under_existing_tasks_table():
    consumer = '[project]\nname = "acme"\n\n[tasks]\ntest = "pytest"\n'
    out = splice.splice_block(
        consumer,
        'lint = "shipit lint"',
        iunits.PIXI_OPEN,
        iunits.PIXI_CLOSE,
        anchor="[tasks]",
    )
    # The managed line lands inside [tasks], not after some later table.
    tasks_idx = out.index("[tasks]")
    project_after = out.find("[project]", tasks_idx)
    lint_idx = out.index('lint = "shipit lint"')
    assert tasks_idx < lint_idx
    assert project_after == -1  # no table opens between [tasks] and the line
    assert 'test = "pytest"' in out
    # Round-trips through extract with the pixi markers.
    assert (
        splice.extract_block(out, iunits.PIXI_OPEN, iunits.PIXI_CLOSE)
        == 'lint = "shipit lint"'
    )


def test_pixi_block_creates_tasks_table_when_absent():
    consumer = '[project]\nname = "acme"\n'
    out = splice.splice_block(
        consumer,
        'lint = "shipit lint"',
        iunits.PIXI_OPEN,
        iunits.PIXI_CLOSE,
        anchor="[tasks]",
    )
    assert "[tasks]" in out
    # The block follows the freshly-added header.
    assert out.index("[tasks]") < out.index('lint = "shipit lint"')


def test_pixi_block_reinstall_replaces_in_place():
    consumer = '[tasks]\ntest = "pytest"\n'
    once = splice.splice_block(
        consumer,
        'lint = "shipit lint"',
        iunits.PIXI_OPEN,
        iunits.PIXI_CLOSE,
        "[tasks]",
    )
    twice = splice.splice_block(
        once, 'lint = "shipit lint"', iunits.PIXI_OPEN, iunits.PIXI_CLOSE, "[tasks]"
    )
    # Idempotent: exactly one managed block after a second install.
    assert twice.count(iunits.PIXI_OPEN) == 1
    assert twice == once


# --------------------------------------------------------------------------
# The ADP00 managed consumer environment (docs/legacy-prd/adoption.md) — the lint
# feature/dependency block + the lint environment definition, siblings of the
# tasks block in the consumer's pixi.toml.
# --------------------------------------------------------------------------

#: The fleet-pinned lint toolchain the managed deps block must deliver.
LINT_TOOLS = (
    "ruff",
    "shellcheck",
    "go-shfmt",
    "yamllint",
    "prettier",
    "markdownlint-cli",
    # The GitHub Actions workflow gate — the actions Lang (TOL01-WS04 #553).
    "actionlint",
    "lefthook",
)


def test_load_units_includes_the_lint_env_blocks():
    units = {u.key: u for u in iunits.load_units()}

    deps = units[iunits.PIXI_LINT_DEPS_KEY]
    assert deps.kind == "block"
    assert deps.dest == "pixi.toml"
    assert deps.anchor == "[feature.lint.dependencies]"
    assert set(tomllib.loads(deps.desired_inner())) == set(LINT_TOOLS)

    envs = units[iunits.PIXI_ENVS_KEY]
    assert envs.kind == "block"
    assert envs.dest == "pixi.toml"
    assert envs.anchor == "[environments]"
    assert tomllib.loads(envs.desired_inner()) == {"lint": ["lint"]}

    # Five sibling blocks in ONE consumer file: their marker fences must be
    # pairwise distinct or extract/splice would bleed across regions.
    fences = {
        units[k].open_marker
        for k in (
            iunits.PIXI_KEY,
            iunits.PIXI_TEST_TASK_KEY,
            iunits.PIXI_LINT_DEPS_KEY,
            iunits.PIXI_ENVS_KEY,
            iunits.PIXI_LAUNCHER_DEPS_KEY,
        )
    }
    assert len(fences) == 5


def test_load_units_includes_the_launcher_deps_block():
    # #758, closed by TOL02-WS17 (#794): uv — the pinned bin/shipit launcher's
    # one prerequisite (ADR-0033) — rides the managed pixi surface. The block
    # is UNCONDITIONAL (zero-arg catalog: every consumer's managed tasks
    # resolve through the launcher) and anchors in the DEFAULT env, the PATH a
    # bare `pixi run --locked <task>` on a hosted runner actually resolves.
    units = {u.key: u for u in iunits.load_units()}
    launcher = units[iunits.PIXI_LAUNCHER_DEPS_KEY]
    assert launcher.kind == "block"
    assert launcher.dest == "pixi.toml"
    assert launcher.anchor == "[dependencies]"
    assert set(tomllib.loads(launcher.desired_inner())) == {"uv"}


def test_launcher_deps_uv_pin_agrees_with_layer0_uv_pin():
    # The two uv provisioning paths — Layer 0's reconcile-to-pin bootstrap
    # (bin/setup-dev-env.sh, dev machines/cloud sessions) and the managed
    # launcher-deps block (hosted CI runners, via setup-pixi) — must move in
    # lockstep, or dev and CI run different uvs (the ci-cache-spike "second uv
    # pin" caveat, docs/dev/ci-cache-spike.md §4).
    script = iunits.data_bytes("bootstrap", "setup-dev-env.sh").decode("utf-8")
    uv_pin = next(
        line.split('"')[1] for line in script.splitlines() if line.startswith("UV_PIN=")
    )
    block = tomllib.loads(
        iunits.data_bytes("pixi-launcher-deps-block.toml").decode("utf-8")
    )
    major, minor, *_ = uv_pin.split(".")
    assert block["uv"] == f"{major}.{minor}.*", (
        f"managed uv spec {block['uv']!r} is not the minor line of "
        f"Layer 0's UV_PIN {uv_pin!r}"
    )


def test_packaged_lint_env_agrees_with_shipits_own_manifest():
    """The dogfood drift check (docs/legacy-prd/adoption.md): shipit's own manifest and
    the packaged consumer block pin IDENTICAL versions, so shipit dogfoods
    exactly what the fleet receives and a version bump is one data-block edit
    (mirrored into shipit's own hand-written toolchain, or this test fails)."""
    own = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pixi.toml").read_text(encoding="utf-8")
    )
    deps = tomllib.loads(iunits.data_bytes("pixi-lint-deps-block.toml").decode("utf-8"))

    assert set(deps) == set(LINT_TOOLS)
    # Every packaged pin agrees with shipit's own default-env toolchain (where
    # shipit's hand-written lint environment gets its binaries, issue #210).
    for tool, pin in deps.items():
        assert own["dependencies"].get(tool) == pin, (
            f"{tool}: packaged pin {pin!r} != shipit's own {own['dependencies'].get(tool)!r}"
        )
    # ...and shipit's own lint feature carries the managed block verbatim.
    assert own["feature"]["lint"]["dependencies"] == deps

    envs = tomllib.loads(iunits.data_bytes("pixi-lint-env-block.toml").decode("utf-8"))
    assert envs == {"lint": ["lint"]}
    assert own["environments"]["lint"] == envs["lint"]


def test_shipits_own_pixi_manifest_reconciles_to_noop():
    # shipit self-installs at Tree provisioning (`shipit install --local`), so
    # its own pixi.toml must carry every managed pixi block byte-identically —
    # otherwise every fresh Tree would splice a drift commit (or a duplicate
    # `lint` key under [environments]) into shipit's own manifest.
    root = Path(__file__).resolve().parents[1]
    # shipit's own tracked pyproject.toml signals the python toolchain (#801),
    # so the real install's catalog includes the python release-deps block —
    # dogfooded verbatim like every other managed pixi block.
    units = {
        u.key: u
        for u in iunits.load_units(toolchains=frozenset({iunits.TOOLCHAIN_PYTHON}))
    }
    for key in (
        iunits.PIXI_KEY,
        iunits.PIXI_LINT_DEPS_KEY,
        iunits.PIXI_ENVS_KEY,
        iunits.PIXI_LAUNCHER_DEPS_KEY,
        iunits.PIXI_PYTHON_RELEASE_DEPS_KEY,
    ):
        unit = units[key]
        assert irec.consumer_hash(root, unit) == unit.desired_hash(), key


# --------------------------------------------------------------------------
# The ADP00 consumer-generic lefthook caller (docs/legacy-prd/adoption.md, #419) —
# the managed variant works on a stock consumer right after install; shipit's
# own repo-local legs live in a committed lefthook-local.yml (lefthook's
# native config layering), never in the managed file.
# --------------------------------------------------------------------------


def _managed_lefthook() -> dict:
    return yaml.safe_load(iunits.data_bytes("lefthook.yml"))


def test_managed_lefthook_is_consumer_generic():
    """Every hook leg of the managed caller fails open when pixi is absent
    (#482), then runs through the pinned lint env and invokes only the managed
    `lint` task or the PINNED launcher `./bin/shipit` — never a bare PATH
    `shipit` (#481, ADR-0033) and no shipit-repo-local scripts or paths (the
    stock-consumer guarantee, #419)."""
    cfg = _managed_lefthook()
    assert set(cfg) == {"pre-commit", "pre-push", "post-commit"}
    for hook in cfg.values():
        for cmd in hook["commands"].values():
            run = cmd["run"]
            # Fail-open on pixi absence guards every leg (#482)...
            assert run.startswith(PIXI_ABSENCE_GUARD)
            assert "exit 0" in run
            pixi_part = run[len(PIXI_ABSENCE_GUARD) :]
            # ...then everything rides the pinned lint env (never bare
            # `pixi run`)...
            assert pixi_part.startswith("pixi run -e lint ")
            # ...and invokes the managed `lint` task or the PINNED launcher
            # `./bin/shipit` — never a bare PATH `shipit` (#481) and never a
            # shell indirection into a repo-local script.
            invoked = pixi_part.removeprefix("pixi run -e lint ").split()[0]
            assert invoked in ("lint", "./bin/shipit")
            assert invoked != "shipit"
            assert "tools/" not in run and ".lex" not in run

    # The exact legs: pre-commit lint (priority 2 — the slot a local leg like
    # shipit's own lex-mirror runs ahead of in lefthook's priority-ordered
    # sequential run) + pre-push lint. The retired classification tripwire
    # (`classify-gate`, ADR-0044: findings arrive pre-classified, so there is
    # nothing to gate) must NOT reappear. (The post-commit dev-cycle leg is
    # asserted in test_logevent.py's managed-hook-tier test.)
    lint = cfg["pre-commit"]["commands"]["lint"]
    assert lint == {"priority": 2, "run": PIXI_ABSENCE_GUARD + "pixi run -e lint lint"}
    assert (
        cfg["pre-push"]["commands"]["lint"]["run"]
        == PIXI_ABSENCE_GUARD + "pixi run -e lint lint"
    )
    assert "classify-gate" not in cfg["pre-push"]["commands"]

    # The invoked task and environment exist in the managed pixi blocks, so a
    # stock consumer satisfies every reference with nothing pre-installed.
    tasks = tomllib.loads(iunits.data_bytes("pixi-tasks-block.toml").decode("utf-8"))
    assert "lint" in tasks
    envs = tomllib.loads(iunits.data_bytes("pixi-lint-env-block.toml").decode("utf-8"))
    assert "lint" in envs


def test_shipits_own_lefthook_reconciles_to_noop():
    """shipit self-installs at Tree provisioning (`shipit install --local`),
    so its own lefthook.yml must stay BYTE-IDENTICAL to the managed unit —
    otherwise every fresh Tree would clobber shipit's extra hook legs (UPDATE
    or OVERRIDE both write). shipit's repo-local legs live in
    lefthook-local.yml instead: lefthook's own layering carries the
    divergence, the reconciler stays feature-poor (ADR-0003)."""
    root = Path(__file__).resolve().parents[1]
    unit = {u.key: u for u in iunits.load_units()}[iunits.LEFTHOOK_FILE]
    assert irec.consumer_hash(root, unit) == unit.desired_hash()


def test_shipits_own_local_config_carries_the_lex_mirror_leg():
    """The .lex→.md mirror leg moved OUT of the managed caller into shipit's
    committed lefthook-local.yml — still regenerating mirrors ahead of lint
    (lefthook's default sequential run orders commands by priority, 0/unset
    last), so shipit's own hooks stay green (dogfood)."""
    root = Path(__file__).resolve().parents[1]
    local = yaml.safe_load((root / "lefthook-local.yml").read_text(encoding="utf-8"))
    leg = local["pre-commit"]["commands"]["lex-mirror"]
    assert "tools/lex-convert-doc.sh" in leg["run"]
    assert (root / "tools" / "lex-convert-doc.sh").is_file()
    # It slots BEFORE the managed lint command in the priority-ordered run.
    managed = _managed_lefthook()
    assert leg["priority"] < managed["pre-commit"]["commands"]["lint"]["priority"]


def test_lefthook_unit_reconciles_add_noop_override(tmp_path, rec):
    """The consumer-generic caller rides the standard four-case reconcile:
    fresh install ADDs it, an unchanged re-install NOOPs, a consumer edit
    surfaces as OVERRIDE (never silently kept)."""

    def decision():
        return next(
            d for d in _plan(tmp_path).decisions if d.unit.key == iunits.LEFTHOOK_FILE
        )

    assert decision().action == irec.ADD
    _apply(tmp_path)
    assert (tmp_path / "lefthook.yml").read_bytes() == iunits.data_bytes("lefthook.yml")
    assert decision().action == irec.NOOP
    (tmp_path / "lefthook.yml").write_text("pre-commit: {}\n")
    assert decision().action == irec.OVERRIDE


# --------------------------------------------------------------------------
# The lefthook merge-conflict tripwire (#544) — lefthook refuses a merged hook
# where both `piped` and `parallel` are true, crashing BEFORE any check runs,
# so a hook-level option in the managed caller colliding with a consumer's
# committed lefthook-local.yml bricks every commit in that repo (the
# phos-editor incident). Two defenses: the managed caller sets NO hook-level
# execution-order option, and the reconcile detects the class anyway (warn in
# the working-tree mode, fail closed in every committing mode).
# --------------------------------------------------------------------------

# The managed caller's OLD pre-commit shape (hook-level `piped: true`) — the
# content that collided with phos-editor/app's `parallel: true` local config.
OLD_PIPED_MANAGED = "pre-commit:\n  piped: true\n  commands:\n    lint:\n      run: x\n"
PARALLEL_LOCAL = "pre-commit:\n  parallel: true\n  commands:\n    leg:\n      run: y\n"


def test_managed_lefthook_sets_no_hook_level_execution_options():
    """The #544 guarantee: no hook in the managed caller sets an exclusive
    execution-order option, so it can NEVER collide with a consumer's
    `piped`/`parallel` in lefthook-local.yml. Ordering still holds without
    one: lefthook's default sequential run orders commands by priority
    (0/unset last), so the lint leg keeps `priority: 2` and a local leg with
    `priority: 1` still runs first — only the old pipe's stop-on-failure is
    traded away (lint runs redundantly after a failing local leg; the hook
    still fails). Reintroducing such an option is a design change: re-read
    the lefthook.yml comment block and #544 first."""
    cfg = _managed_lefthook()
    for hook, body in cfg.items():
        for option in irec.EXCLUSIVE_HOOK_OPTIONS:
            assert option not in body, f"{hook} sets hook-level {option!r} (#544)"
    assert cfg["pre-commit"]["commands"]["lint"]["priority"] == 2


def test_detect_lefthook_conflicts_flags_piped_vs_parallel():
    # The phos-editor shape: old managed `piped: true` + local `parallel: true`.
    conflicts = irec.detect_lefthook_conflicts(
        OLD_PIPED_MANAGED, PARALLEL_LOCAL, "lefthook-local.yml"
    )
    assert [(c.hook, c.managed_options, c.local_options) for c in conflicts] == [
        ("pre-commit", ("piped",), ("parallel",))
    ]
    message = irec.format_lefthook_conflict(conflicts[0])
    # Actionable: names both files, both options, the blast radius, the fix.
    assert "'piped: true'" in message and "'parallel: true'" in message
    assert "lefthook-local.yml" in message and iunits.LEFTHOOK_FILE in message
    assert "shipit install" in message


def test_detect_lefthook_conflicts_local_value_wins_in_the_merge():
    # lefthook layers the local scalar over the managed one, so a local
    # `piped: false` DEFUSES the managed `piped: true` — no conflict.
    defused = (
        "pre-commit:\n  piped: false\n  parallel: true\n"
        "  commands:\n    leg:\n      run: y\n"
    )
    assert (
        irec.detect_lefthook_conflicts(OLD_PIPED_MANAGED, defused, "lefthook-local.yml")
        == ()
    )
    # A local config touching neither exclusive option never conflicts.
    plain = "pre-commit:\n  commands:\n    leg:\n      run: y\n"
    assert (
        irec.detect_lefthook_conflicts(OLD_PIPED_MANAGED, plain, "lefthook-local.yml")
        == ()
    )


def test_detect_lefthook_conflicts_is_scoped_to_what_install_can_cause():
    # A lefthook-local.yml arguing with ITSELF (both options true) is the
    # consumer's own file, outside the managed set's blast radius — lefthook
    # reports it on their next hook run. This holds regardless of what the
    # managed side sets: a both-true local self-conflict is refused whatever
    # the managed config does, so install neither causes it nor can fix it
    # (#546 review). Managed-sets-neither AND managed-sets-one must both stay
    # clean.
    local_self_conflict = (
        "pre-commit:\n  piped: true\n  parallel: true\n"
        "  commands:\n    leg:\n      run: y\n"
    )
    for managed in (
        "pre-commit:\n  commands:\n    lint:\n      run: x\n",  # sets neither
        OLD_PIPED_MANAGED,  # sets one (piped) — the managed contribution is moot
    ):
        assert (
            irec.detect_lefthook_conflicts(
                managed, local_self_conflict, "lefthook-local.yml"
            )
            == ()
        )


def test_detect_lefthook_conflicts_tolerates_unreadable_local_config():
    # An unparseable or non-mapping local config is a different failure class
    # the consumer owns; the tripwire never turns it into an install refusal.
    for bad in ("{unclosed", "- a\n- b\n", "just a scalar\n", ""):
        assert (
            irec.detect_lefthook_conflicts(OLD_PIPED_MANAGED, bad, "lefthook-local.yml")
            == ()
        )


def test_format_lefthook_conflict_when_managed_side_sets_both(tmp_path):
    # The agy #544-review edge case: a FUTURE managed edit sets BOTH exclusive
    # options and the consumer's local config merely DEFINES the hook (setting
    # neither), so `local_options` is empty — the conflict is entirely
    # managed-side. The message must not tell the consumer to remove an option
    # they never set; it points at regenerating the managed config instead.
    managed_both = (
        "pre-commit:\n  piped: true\n  parallel: true\n"
        "  commands:\n    lint:\n      run: x\n"
    )
    local_defines_hook = "pre-commit:\n  commands:\n    leg:\n      run: y\n"
    conflicts = irec.detect_lefthook_conflicts(
        managed_both, local_defines_hook, "lefthook-local.yml"
    )
    assert len(conflicts) == 1 and conflicts[0].local_options == ()
    message = irec.format_lefthook_conflict(conflicts[0])
    assert "'piped: true'" in message and "'parallel: true'" in message
    assert "managed-config defect" in message and "shipit install" in message
    # Never advise removing an option the consumer's file does not set.
    assert "Remove the option from" not in message


def test_read_lefthook_local_fails_open_on_oserror(tmp_path, monkeypatch):
    # A permission denial / mid-read unlink on the consumer-owned config must
    # degrade to None/None (the best-effort tripwire), never crash install —
    # matching the unreadable-manifest path (#544 review). gather() must return
    # a clean state, and the working-tree refresh downstream must not abort.
    (tmp_path / "lefthook-local.yml").write_text(PARALLEL_LOCAL)
    real_read_text = Path.read_text

    def boom(self, *args, **kwargs):
        if self.name in irec.LEFTHOOK_LOCAL_FILES:
            raise PermissionError("permission denied")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", boom)
    assert irec._read_lefthook_local(tmp_path) == (None, None)
    state = irec.gather(tmp_path, iunits.load_units(), [])  # must not raise
    assert state.lefthook_local is None and state.lefthook_local_path is None
    assert _plan(tmp_path).lefthook_conflicts == ()  # whole pipeline stays clean


def test_gather_reads_the_consumer_lefthook_local_config(tmp_path):
    units = iunits.load_units()
    state = irec.gather(tmp_path, units, [])
    assert state.lefthook_local is None and state.lefthook_local_path is None
    (tmp_path / "lefthook-local.yml").write_text(PARALLEL_LOCAL)
    state = irec.gather(tmp_path, units, [])
    assert state.lefthook_local_path == "lefthook-local.yml"
    assert state.lefthook_local == PARALLEL_LOCAL


def test_parallel_local_config_reconciles_clean_against_current_managed_caller(
    tmp_path,
):
    # The regression proof for the incident repo's shape: a consumer whose
    # committed lefthook-local.yml sets `parallel: true` reconciles with NO
    # conflict against the current managed caller (which sets no hook-level
    # execution-order option) — the very install that rolls the managed set
    # forward UN-bricks such a repo instead of refusing.
    (tmp_path / "lefthook-local.yml").write_text(PARALLEL_LOCAL)
    plan = _plan(tmp_path)
    assert plan.lefthook_conflicts == ()
    assert verb.format_plan_warnings(plan) == ""


def test_lefthook_conflict_warns_in_tree_mode_and_fails_committing_modes_closed(
    tmp_path, rec
):
    # Simulate a future managed edit reintroducing the class: inject the
    # detected conflict into an otherwise-real plan (the shipped caller can no
    # longer produce one — see the no-hook-level-options tripwire above).
    conflict = irec.detect_lefthook_conflicts(
        OLD_PIPED_MANAGED, PARALLEL_LOCAL, "lefthook-local.yml"
    )[0]
    plan = dc_replace(_plan(tmp_path), lefthook_conflicts=(conflict,))

    # The plan's stderr surface carries the same actionable message.
    assert irec.format_lefthook_conflict(conflict) in verb.format_plan_warnings(plan)

    # Every committing mode fails CLOSED before any write or git side effect.
    for mode in (iapply.MODE_LOCAL, iapply.MODE_PUSH, iapply.MODE_PR):
        with pytest.raises(InstallError, match="lefthook config conflict"):
            iapply.apply(plan, mode, pr_body=lambda *a: "")
    assert rec.calls == [] and rec.hook_activations == []
    assert not (tmp_path / ".shipit.toml").exists()
    assert not (tmp_path / "lefthook.yml").exists()

    # The working-tree refresh proceeds (nothing is published; the warning
    # above is the caller's review surface alongside `git diff`).
    result = iapply.apply(plan, iapply.MODE_TREE)
    assert result.mode == iapply.MODE_TREE
    assert (tmp_path / "lefthook.yml").is_file()


def test_conflict_bearing_noop_plan_still_fails_committing_modes(tmp_path, monkeypatch):
    # codex #546-review regression: a committing-mode run whose ONLY finding is
    # a lefthook conflict (managed set already current — nothing to write) must
    # not slip past on the no-op shortcut. `nothing_to_do` returns before
    # apply(), so without the verb's pre-shortcut guard the run would print the
    # warning and exit 0, bypassing the fail-closed refusal. Craft exactly that
    # plan (empty work axes + a conflict) and drive the verb end to end.
    conflict = irec.detect_lefthook_conflicts(
        OLD_PIPED_MANAGED, PARALLEL_LOCAL, "lefthook-local.yml"
    )[0]
    noop_conflict = dc_replace(
        _plan(tmp_path),
        decisions=(),
        retired=(),
        seeds=(),
        current_pin=None,
        target_pin=None,
        lefthook_conflicts=(conflict,),
    )
    assert noop_conflict.nothing_to_do  # the exact bypass shape
    monkeypatch.setattr(verb, "reconcile", lambda *a, **k: noop_conflict)

    # Every committing mode refuses (cli_errors maps InstallError -> exit 1);
    # nothing is published despite the plan being otherwise a no-op.
    assert verb.run(str(tmp_path), local=True) == 1
    assert verb.run(str(tmp_path), push=True) == 1
    assert verb.run(str(tmp_path), pr=True) == 1
    assert not (tmp_path / "lefthook.yml").exists()
    assert not (tmp_path / ".shipit.toml").exists()

    # Dry-run and the working-tree refresh stay warn-only no-ops (exit 0).
    assert verb.run(str(tmp_path), local=True, dry_run=True) == 0
    assert verb.run(str(tmp_path)) == 0


# --------------------------------------------------------------------------
# The ADP00-WS10 lint tool configs (#436) — the managed set delivers the
# configs its own gate needs (markdownlint/yamllint auto-discover them from
# the repo root), so a stock consumer's whole-tree lint is green right after
# install with the managed set present.
# --------------------------------------------------------------------------


def test_load_units_includes_the_lint_tool_configs():
    units = {u.key: u for u in iunits.load_units()}
    for dest, data_file in iunits.LINT_CONFIG_UNITS:
        unit = units[dest]
        assert unit.kind == "file"
        assert unit.dest == dest
        assert unit.content == iunits.data_bytes(data_file)


def test_managed_markdownlint_config_relaxes_the_changelog_genre_rules():
    """MD013/MD041 off + MD024->siblings_only + MD033 off for the managed set's
    markdown/changelog genre; every other rule stays at markdownlint's defaults
    so real structural issues still fail."""
    cfg = yaml.safe_load(iunits.data_bytes("markdownlint.yaml"))
    assert cfg == {
        "default": True,
        "MD013": False,
        "MD041": False,
        "MD024": {"siblings_only": True},
        "MD033": False,
    }


def test_managed_yamllint_config_extends_default_with_three_relaxations():
    cfg = yaml.safe_load(iunits.data_bytes("yamllint.yaml"))
    assert cfg == {
        "extends": "default",
        "rules": {
            "document-start": "disable",
            "truthy": {"check-keys": False},
            "line-length": {"max": 120},
        },
    }


def test_managed_markdownlintignore_covers_managed_paths_and_testdata():
    """The ignore file excludes the managed/vendored markdown (skills/, AGENTS.md)
    plus the test-data conventions (#500) — never other consumer-authored prose
    (a consumer's README.md is theirs; shipit's own README is skipped only
    because it is a lex projection, which `shipit lint` routes to the lexd leg
    with no ignore entry — tested in test_lint.py). The test-data globs match
    lint.PROTECTED_TESTDATA_GLOBS: the fixer refuses to auto-rewrite them AND
    check mode skips these deliberately-malformed fixtures too."""
    from shipit import lint

    entries = [
        line
        for line in iunits.data_bytes("markdownlintignore").decode().splitlines()
        if line and not line.startswith("#")
    ]
    assert entries == ["skills/", "AGENTS.md", *lint.PROTECTED_TESTDATA_GLOBS]


def test_shipits_own_lint_configs_reconcile_to_noop():
    """The dogfood drift check, extended from the WS01 version pattern to
    config: shipit self-installs at Tree provisioning, so its own
    auto-discovered lint configs must stay BYTE-IDENTICAL to the managed
    units — a consumer lints with exactly what shipit's own gate runs, and a
    config edit is one data-file change mirrored here (or this test fails)."""
    root = Path(__file__).resolve().parents[1]
    units = {u.key: u for u in iunits.load_units()}
    for dest, _ in iunits.LINT_CONFIG_UNITS:
        unit = units[dest]
        assert irec.consumer_hash(root, unit) == unit.desired_hash(), dest


def test_lint_config_units_reconcile_add_noop_override(tmp_path, rec):
    """Fresh consumer install ADDs the managed lint config units, a re-install
    NOOPs, and a consumer edit surfaces as OVERRIDE (never silently kept)."""
    keys = {dest for dest, _ in iunits.LINT_CONFIG_UNITS}

    def actions():
        return {
            d.unit.key: d.action
            for d in _plan(tmp_path).decisions
            if d.unit.key in keys
        }

    assert set(actions().values()) == {irec.ADD}
    _apply(tmp_path)
    for dest, data_file in iunits.LINT_CONFIG_UNITS:
        assert (tmp_path / dest).read_bytes() == iunits.data_bytes(data_file)
    assert set(actions().values()) == {irec.NOOP}
    (tmp_path / iunits.YAMLLINT_FILE).write_text("extends: relaxed\n")
    assert actions()[iunits.YAMLLINT_FILE] == irec.OVERRIDE
    assert actions()[iunits.MARKDOWNLINT_FILE] == irec.NOOP


def test_load_units_has_skills_agents_and_bootstrap():
    units = iunits.load_units()
    keys = {u.key for u in units}
    assert "AGENTS.md#shipit-block" in keys
    assert "bin/shipit" in keys
    assert any(k.startswith("skills/") for k in keys)
    agents = next(u for u in units if u.key == "AGENTS.md#shipit-block")
    assert agents.kind == "block"
    boot = next(u for u in units if u.key == "bin/shipit")
    assert boot.executable is True


# --------------------------------------------------------------------------
# The pinned bin/shipit launcher (ADR-0033) — pin-resolve via uv, SHIPIT_EXEC
# override, pinless refusal. The exec seam is FAKED: a shim `uv` (and shim
# override targets) planted first on PATH record their argv instead of
# resolving anything, so these run the REAL shipped bash against fakes.
# --------------------------------------------------------------------------

LAUNCHER_PIN = "c" * 40


def _write_launcher_repo(tmp_path: Path, *, manifest: str | None) -> Path:
    """A stand-in consumer repo: the MANAGED bin/shipit + an optional .shipit.toml.

    Returns the launcher path (``<repo>/bin/shipit``). ``manifest=None`` writes
    no ``.shipit.toml`` at all (the virgin-repo case).
    """
    repo = tmp_path / "consumer"
    (repo / "bin").mkdir(parents=True)
    unit = next(u for u in iunits.load_units() if u.key == "bin/shipit")
    launcher = repo / "bin" / "shipit"
    launcher.write_bytes(unit.content)
    launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    if manifest is not None:
        (repo / ".shipit.toml").write_text(manifest)
    return launcher


def _shim(dir_path: Path, name: str, marker: str) -> Path:
    """An executable shim that prints ``marker`` + its argv and exits 0."""
    p = dir_path / name
    p.write_text(f'#!/usr/bin/env bash\necho "{marker} $*"\n')
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return p


def _launcher_env(*prepend: Path) -> dict[str, str]:
    """The launcher subprocess env: the given shim dirs first, the real PATH after
    (bash/awk/dirname live there), and no ambient SHIPIT_EXEC leaking in."""
    path = os.pathsep.join(
        [str(d) for d in prepend] + [os.environ.get("PATH", os.defpath)]
    )
    return {"PATH": path}


def test_launcher_execs_the_pin_via_uv_never_path(tmp_path: Path):
    # The pin-resolve path (ADR-0033): the launcher reads [shipit].version and
    # execs `uv tool run --from git+…@<pin> shipit <args>`. A `shipit` sitting
    # FIRST on PATH must play no part — PATH is never consulted for the build.
    launcher = _write_launcher_repo(
        tmp_path, manifest=f'[shipit]\nversion = "{LAUNCHER_PIN}"\n\n[managed]\n'
    )
    shims = tmp_path / "shims"
    shims.mkdir()
    _shim(shims, "uv", "FAKE-UV-RAN")
    _shim(shims, "shipit", "PATH-SHIPIT-RAN")  # must never run

    proc = subprocess.run(
        [str(launcher), "pr", "status"],
        env=_launcher_env(shims),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 0
    assert (
        "FAKE-UV-RAN tool run --from "
        f"git+https://github.com/arthur-debert/shipit@{LAUNCHER_PIN} "
        "shipit pr status" in proc.stdout
    )
    assert "PATH-SHIPIT-RAN" not in proc.stdout


def test_launcher_pinless_repo_fails_loud_toward_the_bootstrap(tmp_path: Path):
    # No [shipit].version pin → exit 127 with the bootstrap instructions — and
    # NEVER a PATH fallback, even with a `shipit` sitting right there (the old
    # walk-PATH launcher's silent drift reintroduction, retired by ADR-0033).
    launcher = _write_launcher_repo(
        tmp_path, manifest='[secrets]\nGH_PAT = { env = "X" }\n'
    )
    shims = tmp_path / "shims"
    shims.mkdir()
    _shim(shims, "shipit", "PATH-SHIPIT-RAN")

    proc = subprocess.run(
        [str(launcher), "--version"],
        env=_launcher_env(shims),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 127
    assert "no [shipit].version pin" in proc.stderr
    assert "shipit install --pr" in proc.stderr
    assert "PATH-SHIPIT-RAN" not in proc.stdout


@pytest.mark.parametrize("bad_pin", ["0.0.1", "seed", "c" * 39, "z" * 40])
def test_launcher_non_sha_pin_fails_loud_toward_the_bootstrap(
    tmp_path: Path, bad_pin: str
):
    # A present-but-non-sha [shipit].version (the retired static `0.0.1`, a
    # sentinel, an abbreviated/non-hex value) is NOT a resolvable
    # build: the launcher refuses toward the bootstrap (exit 127) rather than
    # hand uv a ref it would fail on with a murkier error — the same fail-closed
    # posture as config.shipit_pin on the Python side (ADR-0033).
    launcher = _write_launcher_repo(
        tmp_path, manifest=f'[shipit]\nversion = "{bad_pin}"\n\n[managed]\n'
    )
    shims = tmp_path / "shims"
    shims.mkdir()
    _shim(shims, "uv", "FAKE-UV-RAN")  # must never run
    _shim(shims, "shipit", "PATH-SHIPIT-RAN")  # must never run

    proc = subprocess.run(
        [str(launcher), "--version"],
        env=_launcher_env(shims),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 127
    assert "not a full git sha" in proc.stderr
    assert "shipit install --pr" in proc.stderr
    assert "FAKE-UV-RAN" not in proc.stdout
    assert "PATH-SHIPIT-RAN" not in proc.stdout


def test_launcher_missing_manifest_fails_loud_too(tmp_path: Path):
    # The virgin-repo shape of pinless: no .shipit.toml at all — same loud 127.
    launcher = _write_launcher_repo(tmp_path, manifest=None)
    proc = subprocess.run(
        [str(launcher), "--version"],
        env=_launcher_env(),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 127
    assert "no [shipit].version pin" in proc.stderr


def test_launcher_honors_and_announces_shipit_exec_override(tmp_path: Path):
    # The one sanctioned override (ADR-0033): SHIPIT_EXEC=/path is exec'd instead
    # of the pin — honored AND announced on stderr, never silent. uv must not run.
    launcher = _write_launcher_repo(
        tmp_path, manifest=f'[shipit]\nversion = "{LAUNCHER_PIN}"\n'
    )
    shims = tmp_path / "shims"
    shims.mkdir()
    _shim(shims, "uv", "FAKE-UV-RAN")  # must never run
    dev_build = _shim(shims, "dev-shipit", "DEV-BUILD-RAN")

    env = _launcher_env(shims)
    env["SHIPIT_EXEC"] = str(dev_build)
    proc = subprocess.run(
        [str(launcher), "lint", "--fix"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 0
    assert "DEV-BUILD-RAN lint --fix" in proc.stdout
    assert "FAKE-UV-RAN" not in proc.stdout
    # The announcement: loud, on stderr, naming the override.
    assert "SHIPIT_EXEC override" in proc.stderr
    assert str(dev_build) in proc.stderr


def test_launcher_refuses_a_self_pointing_shipit_exec(tmp_path: Path):
    # The self-exec guard, preserved in the override: SHIPIT_EXEC pointing back
    # at the launcher itself would exec(2)-loop forever — refused by inode
    # comparison, loud, 127. The 10s timeout would trip if it ever looped.
    launcher = _write_launcher_repo(
        tmp_path, manifest=f'[shipit]\nversion = "{LAUNCHER_PIN}"\n'
    )
    env = _launcher_env()
    env["SHIPIT_EXEC"] = str(launcher)
    proc = subprocess.run(
        [str(launcher), "--version"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 127
    assert "refusing the exec loop" in proc.stderr


def test_launcher_missing_uv_fails_loud_with_instructions(tmp_path: Path):
    # uv is a hard prerequisite wherever a pin resolves (ADR-0033): absent, the
    # launcher exits 127 pointing at the uv install — never a PATH fallback.
    launcher = _write_launcher_repo(
        tmp_path, manifest=f'[shipit]\nversion = "{LAUNCHER_PIN}"\n'
    )
    # A PATH with the shell utilities but guaranteed no `uv`: copy the real PATH
    # entries, skipping any dir that holds one.
    keep = [
        d
        for d in os.environ.get("PATH", os.defpath).split(os.pathsep)
        if d and not (Path(d) / "uv").exists()
    ]
    proc = subprocess.run(
        [str(launcher), "--version"],
        env={"PATH": os.pathsep.join(keep)},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 127
    assert "uv is not on PATH" in proc.stderr


# --------------------------------------------------------------------------
# The Shipit pin stamp (ADR-0033) — install stamps its OWN build's full sha
# --------------------------------------------------------------------------


def test_fresh_install_on_a_stock_consumer_stamps_a_full_sha_pin(tmp_path, monkeypatch):
    # ADR-0033 acceptance: a fresh install on a synthetic STOCK consumer (an
    # empty directory — no pixi.toml, no configs) stamps .shipit.toml
    # [shipit].version with the FULL git sha of the running build (here the
    # dev-checkout resolver), never the static package version that identifies
    # nothing. And the re-install NOOPs — including the pin.
    monkeypatch.setattr(iapply, "_activate_hooks", lambda root: _exec_result(0))
    result = _apply(tmp_path)  # MODE_TREE: working-tree refresh, no git/gh
    assert result.mode == iapply.MODE_TREE

    pin = config.shipit_pin(tmp_path / ".shipit.toml")
    assert pin is not None
    Sha(pin)  # validates: a FULL sha, or this raises
    assert pin != "0.0.1"

    # Re-install NOOPs, pin included: the plan has nothing to do, so apply
    # never runs and the stamped pin stays byte-identical.
    assert _plan(tmp_path).nothing_to_do
    assert config.shipit_pin(tmp_path / ".shipit.toml") == pin


def test_install_fails_closed_when_the_build_identity_is_unresolvable(
    tmp_path, monkeypatch
):
    # No direct_url record, no embed, no checkout → install REFUSES to stamp
    # (InstallError), rather than minting a pin the launcher could never exec.
    monkeypatch.setattr(iapply.buildid, "build_sha", lambda: None)
    monkeypatch.setattr(iapply, "_activate_hooks", lambda root: _exec_result(0))
    with pytest.raises(InstallError, match="own commit identity"):
        _apply(tmp_path)


def test_a_code_only_shipit_change_rolls_the_pin_forward(tmp_path, monkeypatch):
    # ADR-0033: the install reconcile PR is the ONLY pin-bump vehicle, so a
    # code-only shipit build (new sha, every managed file byte-identical) must
    # STILL be work to do — otherwise the no-op check strands the consumer on
    # the old build forever. The pin travels IN the reconcile payload; a stale
    # pin is a work axis of its own, on par with a pending write.
    monkeypatch.setattr(iapply, "_activate_hooks", lambda root: _exec_result(0))
    _apply(tmp_path)  # stamps the running (dev-checkout) build's sha
    old_pin = config.shipit_pin(tmp_path / ".shipit.toml")
    assert old_pin is not None

    # Same build → the re-plan is a clean no-op (pin matches, nothing changed).
    assert _plan(tmp_path).nothing_to_do

    # A NEW build sha, the managed files untouched: pin-only work to do. The
    # plan writes NOTHING but `.shipit.toml`, and the report names the bump.
    new_sha = "abcdef0123456789abcdef0123456789abcdef01"
    monkeypatch.setattr(iapply.buildid, "build_sha", lambda: Sha(new_sha))
    plan = _plan(tmp_path)
    assert not plan.nothing_to_do
    assert plan.pin_stale
    assert not plan.writes
    assert plan.changed_paths == (config.CONFIG_NAME,)
    assert f"-> {new_sha[:12]}" in verb.format_plan(plan)

    # Applying it rolls the pin forward — the code-only fix reaches the repo.
    _apply(tmp_path)
    assert config.shipit_pin(tmp_path / ".shipit.toml") == new_sha
    assert _plan(tmp_path).nothing_to_do  # and re-settles to a no-op


# --------------------------------------------------------------------------
# The HAR01 harness units — generated agent-defs + the settings.json hook line
# (docs/legacy-prd/har01-coordinator-guard-and-role-prompts.md, user stories 17 & 21)
# --------------------------------------------------------------------------


def test_load_units_includes_the_three_agent_defs():
    units = {u.key: u for u in iunits.load_units()}
    for role in ("implementer", "shepherd", "explorer"):
        key = f"{iunits.AGENTS_DEF_DIR}/{role}.md"
        assert key in units, f"{key} not registered"
        unit = units[key]
        assert unit.kind == "file"
        assert unit.dest == key
        # The bundled content is the generated agent-def (frontmatter names the role).
        assert f"name: {role}".encode() in unit.content


def test_load_units_includes_the_settings_hook_block():
    units = {u.key: u for u in iunits.load_units()}
    assert iunits.SETTINGS_KEY in units
    unit = units[iunits.SETTINGS_KEY]
    assert unit.kind == "block"
    assert unit.fmt == iunits.FMT_JSON_HOOK
    assert unit.dest == iunits.SETTINGS_FILE
    # The managed region is shipit's PreToolUse entry (canonical JSON), nothing else.
    entry = json.loads(unit.desired_inner())
    assert entry["matcher"] == "Edit|Write|MultiEdit|NotebookEdit"
    assert iunits.SETTINGS_HOOK_MARKER in entry["hooks"][0]["command"]


def test_load_units_includes_the_eval_terminal_hooks():
    # HAR02 adds the Stop (coordinator) + SubagentStop (subagent) eval hook lines as
    # two more JSON-hook units over the same settings.json, each owning its event.
    units = {u.key: u for u in iunits.load_units()}
    for key, event, marker in (
        (iunits.SETTINGS_STOP_KEY, iunits.EVENT_STOP, iunits.SETTINGS_STOP_MARKER),
        (
            iunits.SETTINGS_SUBAGENTSTOP_KEY,
            iunits.EVENT_SUBAGENTSTOP,
            iunits.SETTINGS_SUBAGENTSTOP_MARKER,
        ),
    ):
        unit = units[key]
        assert unit.fmt == iunits.FMT_JSON_HOOK
        assert unit.dest == iunits.SETTINGS_FILE
        assert unit.event == event
        assert unit.marker == marker
        entry = json.loads(unit.desired_inner())
        # Terminal-hook entries bind to no tool, so they carry no matcher.
        assert "matcher" not in entry
        assert marker in entry["hooks"][0]["command"]


def test_hook_units_coexist_on_one_settings_file():
    # Splicing all five event entries into one file leaves each in its own event
    # array, none clobbering another — the consumer keeps a single valid settings.json.
    units = {u.key: u for u in iunits.load_units()}
    text = ""
    for key in (
        iunits.SETTINGS_KEY,
        iunits.SETTINGS_STOP_KEY,
        iunits.SETTINGS_SUBAGENTSTOP_KEY,
        iunits.SETTINGS_SESSIONSTART_KEY,
        iunits.SETTINGS_WORKTREECREATE_KEY,
    ):
        u = units[key]
        text = splice.splice_settings_hook(text, u.desired_inner(), u.event, u.marker)
    hooks = json.loads(text)["hooks"]
    assert iunits.SETTINGS_HOOK_MARKER in hooks["PreToolUse"][0]["hooks"][0]["command"]
    assert iunits.SETTINGS_STOP_MARKER in hooks["Stop"][0]["hooks"][0]["command"]
    assert (
        iunits.SETTINGS_SUBAGENTSTOP_MARKER
        in hooks["SubagentStop"][0]["hooks"][0]["command"]
    )
    assert (
        iunits.SETTINGS_SESSIONSTART_MARKER
        in hooks["SessionStart"][0]["hooks"][0]["command"]
    )
    assert (
        iunits.SETTINGS_WORKTREECREATE_MARKER
        in hooks["WorktreeCreate"][0]["hooks"][0]["command"]
    )
    # And each event unit reconciles to NOOP against the file carrying all five.
    for key in (
        iunits.SETTINGS_KEY,
        iunits.SETTINGS_STOP_KEY,
        iunits.SETTINGS_SUBAGENTSTOP_KEY,
        iunits.SETTINGS_SESSIONSTART_KEY,
        iunits.SETTINGS_WORKTREECREATE_KEY,
    ):
        u = units[key]
        got = splice.extract_settings_hook(text, u.event, u.marker)
        assert got == iunits.canonical_hook_entry(json.loads(u.desired_inner()))


# --------------------------------------------------------------------------
# The session-bootstrap launcher units — the generic ./agent-start launcher
# (CDX01 #627) and the SessionStart activation hook
# (docs/legacy-prd/session-bootstrap.md Layers A & D, issue #218)
# --------------------------------------------------------------------------


def test_load_units_includes_the_agent_start_launcher():
    # The CDX01 generic launcher (#627): ONE entry point whose host strategy
    # table dispatches to narrow per-host launch functions — claude rides the
    # WorktreeCreate pre-launch seam, codex execs the pinned
    # `./bin/shipit session codex`.
    units = {u.key: u for u in iunits.load_units()}
    assert iunits.AGENT_LAUNCHER_FILE in units
    unit = units[iunits.AGENT_LAUNCHER_FILE]
    assert unit.kind == "file"
    assert unit.dest == "agent-start"  # repo root, memorable entry point
    assert unit.executable is True
    text = unit.content.decode("utf-8")
    # The claude row: exec `claude --worktree "<minted-id>" "$@"`.
    assert 'exec claude --worktree "sess-' in text
    # The codex row: exec the pinned launcher, never codex directly.
    assert 'exec "$repo/bin/shipit" session codex "$@"' in text
    assert "exec codex" not in text
    # The dispatch is a strategy table over both hosts.
    assert "claude)" in text and "codex)" in text
    # The launch-seam role scrub (#631): the common path unsets an inherited
    # worker-role export before dispatch, so a coordinator launched from a
    # spawned Run's shell cannot silently disarm the edit guard.
    assert "unset SHIPIT_LOG_CTX_ROLE" in text


def test_launcher_matches_shipits_own_copy():
    # The bootstrap dogfood guarantee (the bin/shipit pattern): shipit-self
    # commits a byte-identical, executable copy of the `agent-start` launcher
    # unit at the managed path, so its own Tree provisioning reconciles it to
    # NOOP instead of splicing drift.
    units = {u.key: u for u in iunits.load_units()}
    unit = units[iunits.AGENT_LAUNCHER_FILE]
    own = Path(__file__).resolve().parents[1] / unit.dest
    assert own.read_bytes() == unit.content
    assert os.access(own, os.X_OK)


def test_managed_settings_hooks_agree_with_shipits_own_settings():
    # The dogfood drift guard (the WS01 pattern), for the drift class behind #443
    # Finding B: shipit's own .claude/settings.json wired WorktreeCreate while the
    # managed variant never did. Every managed JSON-hook unit must appear — with
    # the SAME canonical entry — in shipit's own settings, so the two wirings can
    # never diverge again silently.
    own = json.loads(
        (Path(__file__).parent.parent / ".claude" / "settings.json").read_text()
    )
    units = {u.key: u for u in iunits.load_units()}
    for key in (
        iunits.SETTINGS_KEY,
        iunits.SETTINGS_STOP_KEY,
        iunits.SETTINGS_SUBAGENTSTOP_KEY,
        iunits.SETTINGS_SESSIONSTART_KEY,
        iunits.SETTINGS_WORKTREECREATE_KEY,
    ):
        u = units[key]
        entries = own["hooks"].get(u.event, [])
        matches = [e for e in entries if splice.is_shipit_hook(e, u.marker)]
        assert matches, f"shipit's own settings.json wires no {u.event} entry ({key})"
        assert iunits.canonical_hook_entry(matches[0]) == u.desired_inner()


def test_load_units_includes_the_worktreecreate_adapter_hook():
    # #443 Finding B: the managed `agent-start` bootstrap promises that
    # `claude --worktree` provisions the session Tree via shipit's WorktreeCreate
    # hook (ADR-0027) — the managed settings must wire it, or a stock consumer's
    # `--worktree` falls through to Claude Code's native worktree.
    units = {u.key: u for u in iunits.load_units()}
    assert iunits.SETTINGS_WORKTREECREATE_KEY in units
    unit = units[iunits.SETTINGS_WORKTREECREATE_KEY]
    assert unit.kind == "block"
    assert unit.fmt == iunits.FMT_JSON_HOOK
    assert unit.dest == iunits.SETTINGS_FILE
    assert unit.event == iunits.EVENT_WORKTREECREATE
    assert unit.marker == iunits.SETTINGS_WORKTREECREATE_MARKER
    entry = json.loads(unit.desired_inner())
    # WorktreeCreate binds to no tool, so the entry carries no matcher — and the
    # command is consumer-generic (same shape as shipit's own settings entry).
    # It invokes the PINNED launcher `./bin/shipit` (#481, ADR-0033) DIRECTLY —
    # #491 dropped the redundant `pixi run` wrap (the launcher is pixi-independent)
    # and added a launcher-presence fail-open guard. These hooks may run from a CWD
    # that is not the repo root (unlike lefthook, which runs at the root), so the
    # command first `cd`s into `$CLAUDE_PROJECT_DIR` to anchor the relative launcher.
    assert "matcher" not in entry
    assert entry["hooks"][0]["command"] == managed_cc_hook_command("worktreecreate")
    # No pixi dependency on the shipit `hook` subcommands (#491).
    assert "pixi run" not in entry["hooks"][0]["command"]
    # The `shipit hook worktreecreate` marker still appears verbatim in the
    # command (the launcher path ends in `bin/shipit`), so reconcile keeps
    # recognising the managed entry across the command change.
    assert iunits.SETTINGS_WORKTREECREATE_MARKER in entry["hooks"][0]["command"]


def test_load_units_includes_the_sessionstart_activation_hook():
    units = {u.key: u for u in iunits.load_units()}
    assert iunits.SETTINGS_SESSIONSTART_KEY in units
    unit = units[iunits.SETTINGS_SESSIONSTART_KEY]
    assert unit.kind == "block"
    assert unit.fmt == iunits.FMT_JSON_HOOK
    assert unit.dest == iunits.SETTINGS_FILE
    assert unit.event == iunits.EVENT_SESSIONSTART
    assert unit.marker == iunits.SETTINGS_SESSIONSTART_MARKER
    entry = json.loads(unit.desired_inner())
    # SessionStart binds to no tool, so the entry carries no matcher.
    assert "matcher" not in entry
    assert iunits.SETTINGS_SESSIONSTART_MARKER in entry["hooks"][0]["command"]


# --------------------------------------------------------------------------
# The CDX01 Codex project layer (#603) — the thin .codex/config.toml whole-file
# unit and the two .codex/hooks.json JSON-hook units (SessionStart + the
# PreToolUse tool guard), riding the SAME shared `shipit hook` verbs and the
# SAME reconcile machinery as the Claude units.
# --------------------------------------------------------------------------

#: The two Codex JSON-hook units, in catalog order (guard first).
CODEX_HOOK_KEYS = (iunits.CODEX_PRETOOLUSE_KEY, iunits.CODEX_SESSIONSTART_KEY)


def test_load_units_includes_the_codex_project_layer():
    units = {u.key: u for u in iunits.load_units()}
    # The thin config: a whole-file unit under .codex/ (repo-local; personal
    # config layers over it from ~/.codex/config.toml, so shipit owns the file).
    assert iunits.CODEX_CONFIG_FILE in units
    cfg = units[iunits.CODEX_CONFIG_FILE]
    assert cfg.kind == "file"
    assert cfg.dest == ".codex/config.toml"
    # The layer stays THIN: valid TOML that raises the project-doc budget so
    # the AGENTS.md policy block is read whole — no duplicated policy prose.
    parsed = tomllib.loads(cfg.content.decode("utf-8"))
    assert parsed == {"project_doc_max_bytes": 65536}
    # The two hook units: same JSON-hook splice, same events/markers as the
    # Claude units — only the host file differs.
    for key, event, marker in (
        (
            iunits.CODEX_PRETOOLUSE_KEY,
            iunits.EVENT_PRETOOLUSE,
            iunits.SETTINGS_HOOK_MARKER,
        ),
        (
            iunits.CODEX_SESSIONSTART_KEY,
            iunits.EVENT_SESSIONSTART,
            iunits.SETTINGS_SESSIONSTART_MARKER,
        ),
    ):
        unit = units[key]
        assert unit.kind == "block"
        assert unit.fmt == iunits.FMT_JSON_HOOK
        assert unit.dest == iunits.CODEX_HOOKS_FILE
        assert unit.event == event
        assert unit.marker == marker
        assert marker in json.loads(unit.desired_inner())["hooks"][0]["command"]


def test_codex_hook_commands_adapt_env_and_keep_the_fail_postures():
    # The ONLY Codex-specific delta is the payload/env adaptation: codex hook
    # commands run with the session cwd, so neither entry consults
    # $CLAUDE_PROJECT_DIR — and each keeps its Claude twin's fail posture.
    units = {u.key: u for u in iunits.load_units()}

    guard = json.loads(units[iunits.CODEX_PRETOOLUSE_KEY].desired_inner())
    guard_cmd = guard["hooks"][0]["command"]
    assert "CLAUDE_PROJECT_DIR" not in guard_cmd
    # Fail CLOSED (ADR-0038): the pixi-pinned launcher chain, cwd-relative
    # manifest pin, and the exit-2 refusal tail — never a bare `exit 0`.
    assert "git rev-parse --show-toplevel" in guard_cmd
    assert 'pixi run --manifest-path "$repo/pixi.toml" -- ' in guard_cmd
    assert '"$repo/bin/shipit" hook pretooluse' in guard_cmd
    assert "exit 2" in guard_cmd
    assert "exit 0" not in guard_cmd
    # No matcher: Codex tool names are not Claude's, so the entry binds to
    # every tool event and the shared verb's is_edit_tool gate scopes the
    # verdict (a non-edit payload is allowed through silently).
    assert "matcher" not in guard

    session = json.loads(units[iunits.CODEX_SESSIONSTART_KEY].desired_inner())
    session_cmd = session["hooks"][0]["command"]
    assert "CLAUDE_PROJECT_DIR" not in session_cmd
    # Fail OPEN (additive): setup-dev-env best-effort first, the launcher
    # probe skips cleanly, and the verb's `{"cwd": ...}` payload is JSON-encoded
    # from the session cwd (codex supplies no Claude-shaped stdin payload).
    assert "setup-dev-env.sh" in session_cmd
    assert "exit 0" in session_cmd
    assert "git rev-parse --show-toplevel" in session_cmd
    assert "command -v python3" in session_cmd
    assert "command -v python" in session_cmd
    assert "json.dumps" in session_cmd
    assert 'if [ -n "$py" ]' in session_cmd
    assert '"$repo/bin/shipit" hook sessionstart' in session_cmd


def test_codex_hook_units_coexist_on_one_hooks_file():
    # Splicing both event entries into one .codex/hooks.json leaves each in its
    # own event array — and each extracts back to its canonical entry (NOOP).
    units = {u.key: u for u in iunits.load_units()}
    text = ""
    for key in CODEX_HOOK_KEYS:
        u = units[key]
        text = splice.splice_settings_hook(text, u.desired_inner(), u.event, u.marker)
    hooks = json.loads(text)["hooks"]
    assert iunits.SETTINGS_HOOK_MARKER in hooks["PreToolUse"][0]["hooks"][0]["command"]
    assert (
        iunits.SETTINGS_SESSIONSTART_MARKER
        in hooks["SessionStart"][0]["hooks"][0]["command"]
    )
    for key in CODEX_HOOK_KEYS:
        u = units[key]
        got = splice.extract_settings_hook(text, u.event, u.marker)
        assert got == iunits.canonical_hook_entry(json.loads(u.desired_inner()))


def test_codex_config_unit_reconciles_add_noop_override(tmp_path, rec):
    """The whole-file config rides the standard four-case reconcile: fresh
    install ADDs it, an unchanged re-install NOOPs, a consumer edit surfaces
    as OVERRIDE (never silently kept)."""

    def decision():
        return next(
            d
            for d in _plan(tmp_path).decisions
            if d.unit.key == iunits.CODEX_CONFIG_FILE
        )

    assert decision().action == irec.ADD
    _apply(tmp_path)
    assert (tmp_path / ".codex" / "config.toml").read_bytes() == iunits.data_bytes(
        "codex-config.toml"
    )
    assert decision().action == irec.NOOP
    (tmp_path / ".codex" / "config.toml").write_text("project_doc_max_bytes = 1\n")
    assert decision().action == irec.OVERRIDE


def test_codex_hook_units_reconcile_add_noop_override(tmp_path, rec):
    """Both hooks.json units ride the same four cases — and the OVERRIDE is
    scoped to shipit's OWN entry: a consumer hook added beside it is not an
    override, an edit to shipit's entry is."""

    def decisions():
        plan = _plan(tmp_path)
        return {d.unit.key: d for d in plan.decisions if d.unit.key in CODEX_HOOK_KEYS}

    assert {d.action for d in decisions().values()} == {irec.ADD}
    _apply(tmp_path)
    assert {d.action for d in decisions().values()} == {irec.NOOP}

    hooks_path = tmp_path / ".codex" / "hooks.json"
    data = json.loads(hooks_path.read_text())
    # A consumer's own SessionStart hook beside shipit's: still NOOP for both.
    data["hooks"]["SessionStart"].append(
        {"hooks": [{"type": "command", "command": "echo consumer-own-hook"}]}
    )
    hooks_path.write_text(json.dumps(data, indent=2) + "\n")
    assert {d.action for d in decisions().values()} == {irec.NOOP}

    # An edit to shipit's guard entry surfaces as OVERRIDE — the sessionstart
    # unit (its entry untouched) stays NOOP.
    data["hooks"]["PreToolUse"][0]["hooks"][0]["command"] = (
        "./bin/shipit hook pretooluse # defused"
    )
    hooks_path.write_text(json.dumps(data, indent=2) + "\n")
    got = decisions()
    assert got[iunits.CODEX_PRETOOLUSE_KEY].action == irec.OVERRIDE
    assert got[iunits.CODEX_SESSIONSTART_KEY].action == irec.NOOP

    # And the override write repairs shipit's entry while the consumer's own
    # hook merges through untouched.
    _apply(tmp_path)
    repaired = json.loads(hooks_path.read_text())
    assert any(
        "echo consumer-own-hook" in h["command"]
        for e in repaired["hooks"]["SessionStart"]
        for h in e["hooks"]
    )
    assert {d.action for d in decisions().values()} == {irec.NOOP}


def test_codex_unit_update_advances_silently_on_a_pristine_consumer(
    tmp_path, rec, monkeypatch
):
    """The UPDATE case: shipit ships NEW desired content while the consumer's
    copy still matches the stored pristine hash — the reconcile overwrites
    silently and advances the pristine, for the config file and the hook
    entries alike."""
    _apply(tmp_path)

    real = iunits.data_bytes
    new_config = b"project_doc_max_bytes = 131072\n"
    new_entry = json.dumps(
        {"hooks": [{"type": "command", "command": "./bin/shipit hook sessionstart"}]}
    ).encode()

    def fake(*parts):
        if parts == ("codex-config.toml",):
            return new_config
        if parts == ("codex-hooks-sessionstart.json",):
            return new_entry
        return real(*parts)

    monkeypatch.setattr(iunits, "data_bytes", fake)
    plan = _plan(tmp_path)
    actions = {
        d.unit.key: d.action
        for d in plan.decisions
        if d.unit.key in (iunits.CODEX_CONFIG_FILE, iunits.CODEX_SESSIONSTART_KEY)
    }
    assert actions == {
        iunits.CODEX_CONFIG_FILE: irec.UPDATE,
        iunits.CODEX_SESSIONSTART_KEY: irec.UPDATE,
    }
    _apply(tmp_path)
    assert (tmp_path / ".codex" / "config.toml").read_bytes() == new_config
    assert _plan(tmp_path).nothing_to_do  # pristine advanced — re-settles clean


def test_managed_codex_layer_agrees_with_shipits_own_copies():
    # The dogfood drift guard (the WS01 pattern): shipit's own committed
    # .codex/ layer must match the packaged units — config byte-identical,
    # each hooks.json entry canonical-identical — so the fleet's Codex wiring
    # and shipit's own can never diverge silently.
    assert (REPO_ROOT / ".codex" / "config.toml").read_bytes() == iunits.data_bytes(
        "codex-config.toml"
    )
    own = (REPO_ROOT / ".codex" / "hooks.json").read_text()
    units = {u.key: u for u in iunits.load_units()}
    for key in CODEX_HOOK_KEYS:
        u = units[key]
        got = splice.extract_settings_hook(own, u.event, u.marker)
        assert got == iunits.canonical_hook_entry(json.loads(u.desired_inner())), (
            f"shipit's own .codex/hooks.json disagrees with the managed {key}"
        )


def test_managed_settings_hooks_drop_pixi_run_and_fail_open(tmp_path, rec):
    # #491: the four ADDITIVE managed .claude/settings.json hooks (sessionstart,
    # stop, subagent-stop, worktreecreate) invoke `./bin/shipit hook <phase>`
    # DIRECTLY — no `pixi run` wrap (the pinned launcher is pixi-independent,
    # ADR-0033) — behind a launcher-presence fail-open guard symmetric with the
    # #482 lefthook guard. The `hook` subcommands need no lint toolchain, so
    # wrapping them in `pixi run` only added startup cost and a hard pixi
    # dependency at these high-frequency, non-security surfaces.
    #
    # PreToolUse is EXCLUDED here: it is the ADR-0012 coordinator-edit guard, and
    # #529 gave it its own fail-CLOSED command (test below) after this shared
    # fail-open shape regressed it into running unguarded, silently, whenever
    # resolution failed.
    hook_units = [
        u
        for u in iunits.load_units()
        if u.fmt == iunits.FMT_JSON_HOOK and u.dest == iunits.SETTINGS_FILE
    ]
    # All five managed settings-hook events are present.
    assert {u.event for u in hook_units} == {
        iunits.EVENT_PRETOOLUSE,
        iunits.EVENT_STOP,
        iunits.EVENT_SUBAGENTSTOP,
        iunits.EVENT_SESSIONSTART,
        iunits.EVENT_WORKTREECREATE,
    }
    additive_units = [u for u in hook_units if u.event != iunits.EVENT_PRETOOLUSE]
    assert {u.event for u in additive_units} == {
        iunits.EVENT_STOP,
        iunits.EVENT_SUBAGENTSTOP,
        iunits.EVENT_SESSIONSTART,
        iunits.EVENT_WORKTREECREATE,
    }
    for u in additive_units:
        command = json.loads(u.desired_inner())["hooks"][0]["command"]
        # The phase is the marker tail (`shipit hook <phase>`), and the whole
        # command is the single-source managed form for that phase.
        phase = u.marker.removeprefix("shipit hook ")
        assert command == managed_cc_hook_command(phase)
        # No pixi dependency, and the fail-open guard is present.
        assert "pixi run" not in command
        assert "test -x ./bin/shipit || {" in command
        assert "exit 0" in command
        # The reconcile marker survives verbatim so the managed entry is still
        # recognised after the command change.
        assert u.marker in command


def test_managed_pretooluse_hook_restores_pixi_run_and_fails_closed(tmp_path, rec):
    # #529 (regression from #505/#491): the coordinator-edit GUARD must never
    # silently allow an edit it did not actually check. Unlike the four additive
    # hooks above, its managed command restores `pixi run` (so it resolves
    # reliably in the canonical pixi/dogfood repo) and carries NO fail-open
    # `exit 0` anywhere — a resolution failure blocks (`exit 2` after an
    # actionable stderr message) rather than allowing.
    units = {u.key: u for u in iunits.load_units()}
    unit = units[iunits.SETTINGS_KEY]
    assert unit.event == iunits.EVENT_PRETOOLUSE
    command = json.loads(unit.desired_inner())["hooks"][0]["command"]
    assert command == managed_pretooluse_hook_command()
    assert "pixi run" in command
    # The manifest is PINNED (adapter-equivalent `pixi run --manifest-path
    # "$CLAUDE_PROJECT_DIR"/pixi.toml -- …`), so a leaked PIXI_PROJECT_MANIFEST can't
    # make the guard resolve a parent project and fail for the wrong reason (#531).
    assert '--manifest-path "$CLAUDE_PROJECT_DIR"/pixi.toml -- ' in command
    assert "exit 0" not in command  # the never-silent-allow invariant, by construction
    assert "exit 2" in command  # the fail-closed block on resolution failure
    assert iunits.SETTINGS_HOOK_MARKER in command


# --------------------------------------------------------------------------
# The #547 self-provisioning set — Layer 0 (bin/setup-dev-env.sh + the
# SessionStart wire-in) and Layer 1 (conditional per-toolchain dep blocks)
# --------------------------------------------------------------------------


def test_load_units_includes_the_setup_dev_env_bootstrap():
    units = {u.key: u for u in iunits.load_units()}
    assert iunits.SETUP_DEV_ENV_FILE in units
    unit = units[iunits.SETUP_DEV_ENV_FILE]
    assert unit.kind == "file"
    assert unit.dest == "bin/setup-dev-env.sh"
    assert unit.executable is True
    text = unit.content.decode("utf-8")
    # Reconcile-to-pin from GitHub release assets — the one fetch path on the
    # cloud sandbox's default egress allowlist — never a `curl | sh` vendor
    # installer (the decision boundary #547 settles).
    assert 'PIXI_PIN="' in text and 'UV_PIN="' in text
    assert "github.com/prefix-dev/pixi/releases/download" in text
    assert "github.com/astral-sh/uv/releases/download" in text
    assert "pixi.sh/install" not in text
    assert "astral.sh/uv/install" not in text
    # Provisioning never mutates pixi.lock: the env solve is `--locked` only.
    assert "pixi install --locked" in text


def test_setup_dev_env_matches_shipits_own_copy():
    # The bootstrap dogfood guarantee (the bin/shipit pattern): shipit-self
    # commits a byte-identical copy at the managed path, so its own Tree
    # provisioning reconciles the unit to NOOP instead of splicing drift.
    unit = next(u for u in iunits.load_units() if u.key == iunits.SETUP_DEV_ENV_FILE)
    own = Path(__file__).resolve().parents[1] / "bin" / "setup-dev-env.sh"
    assert own.read_bytes() == unit.content
    assert os.access(own, os.X_OK)


def _bootstrap_function(name: str) -> str:
    """Extract one top-level ``name() { … }`` block from the bootstrap script."""
    lines = (
        iunits.data_bytes("bootstrap", "setup-dev-env.sh").decode("utf-8").splitlines()
    )
    start = lines.index(f"{name}() {{")
    end = start + lines[start:].index("}")
    return "\n".join(lines[start : end + 1])


@pytest.mark.parametrize("tool", ["sha256sum", "shasum"])
def test_sha256_of_stays_fail_open_when_the_hash_tool_errors(tmp_path, tool):
    # #598: the script declares LOUD-and-fail-open, but under its
    # `set -euo pipefail` an unguarded `<hash tool> | awk` pipeline aborts the
    # whole script when the tool exists but errors on the file. sha256_of must
    # instead yield "" (returning 0) so a hashing failure flows into
    # fetch_verified's existing `[ -z "$got" ]` fail-open path — exactly like
    # the no-hash-tool-at-all branch already does.
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    awk = shutil.which("awk")
    assert awk is not None
    os.symlink(awk, stub_bin / "awk")
    stub = stub_bin / tool
    stub.write_text("#!/bin/sh\necho 'hash tool: boom' >&2\nexit 1\n")
    stub.chmod(0o755)
    target = tmp_path / "asset.tar.gz"
    target.write_bytes(b"payload")
    driver = "\n".join(
        [
            "set -euo pipefail",
            _bootstrap_function("sha256_of"),
            f'got="$(sha256_of "{target}")"',
            'printf "got=[%s]\\n" "$got"',
            "echo survived",
        ]
    )
    # Hermetic PATH (the stub dir only): with tool=shasum, sha256sum is absent
    # and the elif branch is the one under test.
    bash = shutil.which("bash")
    assert bash is not None
    proc = subprocess.run(
        [bash, "-c", driver],
        env={"PATH": str(stub_bin)},
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "got=[]" in proc.stdout
    assert "survived" in proc.stdout


def test_setup_dev_env_pixi_pin_agrees_with_ci():
    # PIXI_PIN and CI's setup-pixi `pixi-version` must move in lockstep — the
    # bootstrap and CI provisioning the same pixi is the point of the pin.
    # Since the TOL01-WS05 cutover, CI's setup-pixi lives in the wf-checks
    # BLOCK (ci.yml is a thin caller carrying no setup of its own), and the
    # block runs setup-pixi in BOTH its jobs — every occurrence must agree.
    script = iunits.data_bytes("bootstrap", "setup-dev-env.sh").decode("utf-8")
    pin = next(
        line.split('"')[1]
        for line in script.splitlines()
        if line.startswith("PIXI_PIN=")
    )
    wf = (
        Path(__file__).resolve().parents[1] / ".github" / "workflows" / "wf-checks.yml"
    ).read_text(encoding="utf-8")
    wf_pins = [
        line.split(":", 1)[1].strip().removeprefix("v")
        for line in wf.splitlines()
        if line.strip().startswith("pixi-version:")
    ]
    assert wf_pins, "wf-checks.yml carries no setup-pixi pixi-version pin"
    assert all(wf_pin == pin for wf_pin in wf_pins)


def test_managed_sessionstart_hook_runs_setup_dev_env_first():
    # #547 Layer 0 wire-in: the managed SessionStart command runs the guarded
    # setup-dev-env leg BEFORE the launcher guard + `shipit hook sessionstart`,
    # and keeps the marker substring so the JSON-hook reconcile identity is
    # unchanged (the equality-with-fixture check rides the loop test above).
    units = {u.key: u for u in iunits.load_units()}
    command = json.loads(units[iunits.SETTINGS_SESSIONSTART_KEY].desired_inner())[
        "hooks"
    ][0]["command"]
    assert "./bin/setup-dev-env.sh" in command
    assert command.index("./bin/setup-dev-env.sh") < command.index(
        "test -x ./bin/shipit"
    )
    # Guarded on existence+executability and fail-open (`|| echo … >&2`), so a
    # consumer without the script (or a failing bootstrap) never loses the hook.
    assert "if [ -x ./bin/setup-dev-env.sh ]; then" in command
    assert iunits.SETTINGS_SESSIONSTART_MARKER in command


def test_managed_sessionstart_hook_exports_local_bin_before_the_launcher():
    # #601: setup-dev-env.sh provisions pinned pixi/uv into ~/.local/bin, but it
    # runs as a SUBPROCESS of the hook — its own `export PATH` never reaches the
    # hook shell, and the guarded line it appends to CLAUDE_ENV_FILE only affects
    # LATER Bash calls. So on the first session start in an environment where
    # ~/.local/bin is not already on PATH, the `./bin/shipit hook sessionstart`
    # on the SAME command line still failed with "uv is not on PATH" (exit 127,
    # the ADR-0033 launcher hard-requires uv). The hook command itself must
    # prepend ~/.local/bin (idempotently, mirroring the CLAUDE_ENV_FILE line's
    # case-guard) AFTER the setup-dev-env leg and BEFORE the launcher guard.
    units = {u.key: u for u in iunits.load_units()}
    command = json.loads(units[iunits.SETTINGS_SESSIONSTART_KEY].desired_inner())[
        "hooks"
    ][0]["command"]
    path_leg = (
        'case ":$PATH:" in *":$HOME/.local/bin:"*) ;; '
        '*) export PATH="$HOME/.local/bin:$PATH" ;; esac; '
    )
    assert path_leg in command
    assert command.index("./bin/setup-dev-env.sh") < command.index(path_leg)
    assert command.index(path_leg) < command.index("test -x ./bin/shipit")
    # The PATH leg guard mirrors what setup-dev-env.sh appends to
    # CLAUDE_ENV_FILE (minus its grep marker comment) — one idempotence idiom.
    script = iunits.data_bytes("bootstrap", "setup-dev-env.sh").decode("utf-8")
    # setup-dev-env.sh emits the guard through a double-quoted `printf`, so its
    # `"` and `$` are backslash-escaped on disk. Escape the expected leg to the
    # file's literal form instead of stripping every backslash from the whole
    # script (which would also mangle `printf '%s\n'` and mask quoting drift).
    expected_leg = path_leg.removesuffix("; ").replace('"', '\\"').replace("$", "\\$")
    assert expected_leg in script
    # The other three additive hooks are untouched: only sessionstart runs the
    # bootstrap, so only sessionstart needs the same-command-line PATH fix.
    for key in (
        iunits.SETTINGS_STOP_KEY,
        iunits.SETTINGS_SUBAGENTSTOP_KEY,
        iunits.SETTINGS_WORKTREECREATE_KEY,
    ):
        other = json.loads(units[key].desired_inner())["hooks"][0]["command"]
        assert path_leg not in other


def test_load_units_toolchain_blocks_are_conditional():
    # The zero-arg catalog is byte-identical to the pre-#547 one: no toolchain
    # key sneaks in without its signal, and each signal adds EXACTLY its block.
    toolchain_keys = {key for key, *_ in iunits.TOOLCHAIN_UNITS}
    base = {u.key for u in iunits.load_units()}
    assert not (base & toolchain_keys)

    # Per-signal: each signal adds exactly ITS blocks — one for go/node/python,
    # THREE for rust (the lint toolchain + the release-side cargo-edit block
    # #793 + the release toolchain block #801).
    for signal in (
        iunits.TOOLCHAIN_RUST,
        iunits.TOOLCHAIN_GO,
        iunits.TOOLCHAIN_NODE,
        iunits.TOOLCHAIN_PYTHON,
    ):
        expected = {key for key, sig, *_ in iunits.TOOLCHAIN_UNITS if sig == signal}
        keys = {u.key for u in iunits.load_units(toolchains=frozenset({signal}))}
        assert keys - base == expected, signal
    assert (
        len(
            {
                key
                for key, sig, *_ in iunits.TOOLCHAIN_UNITS
                if sig == iunits.TOOLCHAIN_RUST
            }
        )
        == 3
    )

    all_keys = {
        u.key
        for u in iunits.load_units(
            toolchains=frozenset(
                {
                    iunits.TOOLCHAIN_RUST,
                    iunits.TOOLCHAIN_GO,
                    iunits.TOOLCHAIN_NODE,
                    iunits.TOOLCHAIN_PYTHON,
                }
            )
        )
    }
    assert all_keys - base == toolchain_keys


def test_toolchain_block_units_have_the_right_shape():
    units = {
        u.key: u
        for u in iunits.load_units(
            toolchains=frozenset(
                {
                    iunits.TOOLCHAIN_RUST,
                    iunits.TOOLCHAIN_GO,
                    iunits.TOOLCHAIN_NODE,
                    iunits.TOOLCHAIN_PYTHON,
                }
            )
        )
    }

    rust = units[iunits.PIXI_RUST_DEPS_KEY]
    assert rust.dest == "pixi.toml"
    # rust/go provision LINT toolchains, so they anchor in the lint feature —
    # sibling blocks of the managed lint-deps block under ONE table header.
    assert rust.anchor == iunits.PIXI_LINT_DEPS_ANCHOR
    assert tomllib.loads(rust.desired_inner()) == {"rust": "1.96.*"}

    go = units[iunits.PIXI_GO_DEPS_KEY]
    assert go.anchor == iunits.PIXI_LINT_DEPS_ANCHOR
    assert tomllib.loads(go.desired_inner()) == {"go": "1.26.*", "golangci-lint": "2.*"}

    # node provisions the repo's OWN runtime, not a linter → the default env.
    node = units[iunits.PIXI_NODE_DEPS_KEY]
    assert node.anchor == "[dependencies]"
    assert tomllib.loads(node.desired_inner()) == {"nodejs": "26.*", "pnpm": "11.*"}

    # rust ALSO gets the release-side bump tool (#793): cargo-edit anchors in
    # the DEFAULT env — wf-prepare runs shipit via bare `pixi run --locked
    # ./bin/shipit`, which resolves [dependencies], not the lint feature — so
    # `cargo set-version` is on exactly the PATH `release prepare` executes
    # with. Pinned to conda-forge 0.13.11 (the issue's decided pin). wasm-pack
    # rides the same rust-signal block (TOL02-WS12 #788): the wasm/npm bundle
    # composition's builder, on conda-forge in the DEFAULT release env.
    rust_release = units[iunits.PIXI_RUST_RELEASE_DEPS_KEY]
    assert rust_release.dest == "pixi.toml"
    assert rust_release.anchor == "[dependencies]"
    assert tomllib.loads(rust_release.desired_inner()) == {
        "cargo-edit": "0.13.11.*",
        "wasm-pack": "0.13.*",
    }

    # The rust RELEASE toolchain (#801, TOL02-WS17 hole 1): cargo itself in
    # the default env — a SINGLE-KEY block, deliberately separate from the
    # cargo-edit block so a consumer-side `rust` pin conflicts alone (the
    # PixiKeyConflict guard skips per block) without losing cargo-edit.
    rust_toolchain = units[iunits.PIXI_RUST_RELEASE_TOOLCHAIN_KEY]
    assert rust_toolchain.dest == "pixi.toml"
    assert rust_toolchain.anchor == "[dependencies]"
    assert tomllib.loads(rust_toolchain.desired_inner()) == {"rust": "1.96.*"}

    # The python release-side publish tool (#801, TOL02-WS17 hole 2): twine
    # for the pypi endpoint, in the default env — the PATH wf-publish's bare
    # `pixi run --locked ./bin/shipit` resolves.
    python_release = units[iunits.PIXI_PYTHON_RELEASE_DEPS_KEY]
    assert python_release.dest == "pixi.toml"
    assert python_release.anchor == "[dependencies]"
    assert tomllib.loads(python_release.desired_inner()) == {"twine": "6.2.*"}

    # Sibling blocks in one consumer file: fences pairwise distinct, or
    # extract/splice would bleed across regions (the lint-env blocks' rule).
    fences = {
        units[k].open_marker
        for k in (
            iunits.PIXI_LINT_DEPS_KEY,
            iunits.PIXI_RUST_DEPS_KEY,
            iunits.PIXI_RUST_RELEASE_DEPS_KEY,
            iunits.PIXI_RUST_RELEASE_TOOLCHAIN_KEY,
            iunits.PIXI_GO_DEPS_KEY,
            iunits.PIXI_NODE_DEPS_KEY,
            iunits.PIXI_PYTHON_RELEASE_DEPS_KEY,
        )
    }
    assert len(fences) == 7


def test_rust_release_toolchain_pin_agrees_with_the_rust_lint_block():
    # One rust, two envs (#801): the default-env release toolchain and the
    # lint-feature toolchain must pin the same line, or a consumer lints with
    # a different rust than it releases with.
    toolchain = tomllib.loads(
        iunits.data_bytes("pixi-rust-release-toolchain-block.toml").decode("utf-8")
    )
    lint = tomllib.loads(
        iunits.data_bytes("pixi-rust-lint-deps-block.toml").decode("utf-8")
    )
    assert toolchain == {"rust": lint["rust"]}


def test_packaged_rust_pin_agrees_with_shipits_own_test_toolchain():
    # The dogfood-adjacent drift check: the rust the fleet's lint legs get is
    # the rust shipit's own invariance gate runs (pixi.toml [feature.test]).
    own = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pixi.toml").read_text(encoding="utf-8")
    )
    block = tomllib.loads(
        iunits.data_bytes("pixi-rust-lint-deps-block.toml").decode("utf-8")
    )
    assert block["rust"] == own["feature"]["test"]["dependencies"]["rust"]


def test_rust_block_coexists_with_lint_deps_block_under_one_anchor():
    # Round-trip: both blocks spliced under the SAME [feature.lint.dependencies]
    # header must extract back byte-identically — sibling regions, no bleed.
    units = {
        u.key: u
        for u in iunits.load_units(toolchains=frozenset({iunits.TOOLCHAIN_RUST}))
    }
    deps = units[iunits.PIXI_LINT_DEPS_KEY]
    rust = units[iunits.PIXI_RUST_DEPS_KEY]

    text = '[workspace]\nname = "acme"\n'
    text = splice.splice_block(
        text, deps.desired_inner(), deps.open_marker, deps.close_marker, deps.anchor
    )
    text = splice.splice_block(
        text, rust.desired_inner(), rust.open_marker, rust.close_marker, rust.anchor
    )

    assert (
        splice.extract_block(text, deps.open_marker, deps.close_marker)
        == deps.desired_inner()
    )
    assert (
        splice.extract_block(text, rust.open_marker, rust.close_marker)
        == rust.desired_inner()
    )
    # One header, both blocks inside its table, and the merged table parses to
    # the union of the two dependency sets.
    assert text.count("[feature.lint.dependencies]") == 1
    parsed = tomllib.loads(text)
    merged = parsed["feature"]["lint"]["dependencies"]
    assert merged["rust"] == "1.96.*"
    assert merged["ruff"] == tomllib.loads(deps.desired_inner())["ruff"]


def test_rust_release_block_coexists_with_node_block_under_dependencies():
    # The [dependencies] sibling pair (#793): a rust+node consumer gets BOTH
    # the cargo-edit release block and the node runtime block under the ONE
    # [dependencies] header — sibling regions, no bleed, one merged table.
    units = {
        u.key: u
        for u in iunits.load_units(
            toolchains=frozenset({iunits.TOOLCHAIN_RUST, iunits.TOOLCHAIN_NODE})
        )
    }
    release = units[iunits.PIXI_RUST_RELEASE_DEPS_KEY]
    node = units[iunits.PIXI_NODE_DEPS_KEY]

    text = '[workspace]\nname = "acme"\n'
    text = splice.splice_block(
        text, node.desired_inner(), node.open_marker, node.close_marker, node.anchor
    )
    text = splice.splice_block(
        text,
        release.desired_inner(),
        release.open_marker,
        release.close_marker,
        release.anchor,
    )

    assert (
        splice.extract_block(text, node.open_marker, node.close_marker)
        == node.desired_inner()
    )
    assert (
        splice.extract_block(text, release.open_marker, release.close_marker)
        == release.desired_inner()
    )
    # ONE header line (both blocks' comments mention the table by name, so
    # count actual header lines, not substring occurrences).
    headers = [ln for ln in text.splitlines() if ln.strip() == "[dependencies]"]
    assert len(headers) == 1
    merged = tomllib.loads(text)["dependencies"]
    assert merged["cargo-edit"] == "0.13.11.*"
    assert merged["nodejs"] == "26.*"


def _git_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    return root


def test_detect_toolchains_reads_tracked_manifests(tmp_path):
    # Depth-agnostic: git's default pathspec `*` crosses `/` (fnmatch without
    # FNM_PATHNAME; only `:(glob)` magic changes that), so `*/Cargo.toml`
    # matches a manifest at ANY depth — the workspace-member layout included.
    root = _git_repo(tmp_path)
    (root / "crates" / "core" / "deep").mkdir(parents=True)
    (root / "crates" / "core" / "deep" / "Cargo.toml").write_text("[package]\n")
    (root / "web").mkdir()
    (root / "web" / "package.json").write_text("{}\n")
    (root / "pyproject.toml").write_text("[project]\n")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    assert irec.detect_toolchains(root) == frozenset(
        {iunits.TOOLCHAIN_RUST, iunits.TOOLCHAIN_NODE, iunits.TOOLCHAIN_PYTHON}
    )


def test_detect_toolchains_ignores_untracked_manifests(tmp_path):
    # Tracked-only, like the lint scope: an untracked (vendored/ignored)
    # manifest can never summon a toolchain block.
    root = _git_repo(tmp_path)
    (root / "go.mod").write_text("module acme\n")
    assert irec.detect_toolchains(root) == frozenset()


def test_detect_toolchains_falls_back_to_root_manifests_off_git(tmp_path):
    # A non-git root degrades to root-LEVEL existence checks (no walk).
    (tmp_path / "package.json").write_text("{}\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "Cargo.toml").write_text("[package]\n")
    assert irec.detect_toolchains(tmp_path) == frozenset({iunits.TOOLCHAIN_NODE})


def test_detect_toolchains_clean_root_is_empty(tmp_path):
    assert irec.detect_toolchains(tmp_path) == frozenset()


def _plan_with_toolchains(root, toolchains: frozenset) -> irec.Plan:
    """gather → reconcile over the toolchain-conditional catalog (#547)."""
    units = iunits.load_units(toolchains=toolchains)
    retired = irec.load_retired()
    state = irec.gather(Path(root), units, retired)
    return irec.reconcile(units, retired, state)


_CONSUMER_PIXI_WITH_NODE = """\
[workspace]
channels = ["conda-forge"]
name = "acme"
platforms = ["linux-64"]

[dependencies]
nodejs = "22.*"
"""


def test_node_block_is_skipped_when_the_consumer_already_pins_its_keys(tmp_path):
    # The duplicate-key guard (#547 round 1): a consumer whose [dependencies]
    # already pins nodejs must NOT get the node block spliced in — a duplicate
    # TOML key would make pixi.toml unparseable, blocking installs and every
    # hooked commit. The consumer's own pin stays authoritative.
    (tmp_path / "pixi.toml").write_text(_CONSUMER_PIXI_WITH_NODE)
    plan = _plan_with_toolchains(tmp_path, frozenset({iunits.TOOLCHAIN_NODE}))

    assert plan.pixi_key_conflicts == (
        irec.PixiKeyConflict(
            unit_key=iunits.PIXI_NODE_DEPS_KEY,
            anchor=iunits.PIXI_NODE_DEPS_ANCHOR,
            keys=("nodejs",),
        ),
    )
    # The conflicted block never reaches the plan; the rest of the set does.
    keys = {d.unit.key for d in plan.decisions}
    assert iunits.PIXI_NODE_DEPS_KEY not in keys
    assert iunits.PIXI_LINT_DEPS_KEY in keys
    # Warn-only, and worded off the one formatter (never a broken write).
    warnings = verb.format_plan_warnings(plan)
    assert "pixi block skipped" in warnings
    assert "nodejs" in warnings


def test_skipping_a_key_conflicted_block_keeps_pixi_toml_parseable(tmp_path, rec):
    # End to end: apply on a conflicted consumer leaves a pixi.toml pixi can
    # still parse, the consumer's pin intact, and no [managed] entry for the
    # skipped block (nothing was delivered, so nothing is tracked).
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    (tmp_path / "pixi.toml").write_text(_CONSUMER_PIXI_WITH_NODE)
    plan = _plan_with_toolchains(tmp_path, frozenset({iunits.TOOLCHAIN_NODE}))
    iapply.apply(plan, iapply.MODE_TREE)

    manifest = tomllib.loads((tmp_path / "pixi.toml").read_text(encoding="utf-8"))
    assert manifest["dependencies"]["nodejs"] == "22.*"
    assert iunits.PIXI_NODE_DEPS_OPEN not in (tmp_path / "pixi.toml").read_text()
    managed = config.load_managed(config.load(tmp_path / ".shipit.toml"))
    assert iunits.PIXI_NODE_DEPS_KEY not in managed
    assert iunits.PIXI_LINT_DEPS_KEY in managed


def test_node_block_delivers_when_the_consumer_has_no_clashing_key(tmp_path):
    # Contrast: a [dependencies] table WITHOUT the block's keys is no conflict.
    (tmp_path / "pixi.toml").write_text(
        _CONSUMER_PIXI_WITH_NODE.replace("nodejs", "cmake")
    )
    plan = _plan_with_toolchains(tmp_path, frozenset({iunits.TOOLCHAIN_NODE}))
    assert plan.pixi_key_conflicts == ()
    node = next(d for d in plan.decisions if d.unit.key == iunits.PIXI_NODE_DEPS_KEY)
    assert node.action == irec.ADD


def test_a_spliced_block_is_not_a_key_conflict(tmp_path, rec):
    # Once the block's markers are in, its own keys live in the anchor table —
    # a re-reconcile must read that as NOOP, never as a conflict with itself.
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    toolchains = frozenset({iunits.TOOLCHAIN_NODE})
    plan = _plan_with_toolchains(tmp_path, toolchains)
    iapply.apply(plan, iapply.MODE_TREE)

    again = _plan_with_toolchains(tmp_path, toolchains)
    assert again.pixi_key_conflicts == ()
    node = next(d for d in again.decisions if d.unit.key == iunits.PIXI_NODE_DEPS_KEY)
    assert node.action == irec.NOOP


def test_key_conflict_guard_fails_open_on_an_unparseable_pixi_toml(tmp_path):
    # Best-effort, like the lefthook-local read: a consumer who already broke
    # their own TOML hears it from pixi, not from a guard that only inspects.
    (tmp_path / "pixi.toml").write_text("[[[ not toml\n")
    units = iunits.load_units(toolchains=frozenset({iunits.TOOLCHAIN_NODE}))
    state = irec.gather(Path(tmp_path), units, irec.load_retired())
    assert state.pixi_key_conflicts == ()


def test_key_conflict_guard_covers_the_nested_lint_feature_anchor(tmp_path):
    # The guard is generic over anchors: a consumer pinning `rust` in the
    # nested [feature.lint.dependencies] table conflicts with the rust block.
    (tmp_path / "pixi.toml").write_text(
        '[workspace]\nchannels = ["conda-forge"]\nname = "acme"\n'
        'platforms = ["linux-64"]\n\n[feature.lint.dependencies]\nrust = "1.90.*"\n'
    )
    plan = _plan_with_toolchains(tmp_path, frozenset({iunits.TOOLCHAIN_RUST}))
    assert plan.pixi_key_conflicts == (
        irec.PixiKeyConflict(
            unit_key=iunits.PIXI_RUST_DEPS_KEY,
            anchor=iunits.PIXI_LINT_DEPS_ANCHOR,
            keys=("rust",),
        ),
    )


def test_consumer_rust_pin_conflicts_the_toolchain_block_alone(tmp_path):
    # THE design constraint the #801 rust promotion was shaped around (the
    # TOL02-WS17 hole-1 blocker): a consumer already pinning `rust` in its own
    # [dependencies] (padz/lex, the pre-#801 workaround) keeps its pin via the
    # first-splice guard — and because the release toolchain is its OWN
    # single-key block, ONLY that block is skipped: the cargo-edit release
    # block still delivers. A shared block would have lost both.
    (tmp_path / "pixi.toml").write_text(
        '[workspace]\nchannels = ["conda-forge"]\nname = "acme"\n'
        'platforms = ["linux-64"]\n\n[dependencies]\nrust = "1.90.*"\n'
    )
    plan = _plan_with_toolchains(tmp_path, frozenset({iunits.TOOLCHAIN_RUST}))

    assert plan.pixi_key_conflicts == (
        irec.PixiKeyConflict(
            unit_key=iunits.PIXI_RUST_RELEASE_TOOLCHAIN_KEY,
            anchor=iunits.PIXI_NODE_DEPS_ANCHOR,
            keys=("rust",),
        ),
    )
    keys = {d.unit.key for d in plan.decisions}
    assert iunits.PIXI_RUST_RELEASE_TOOLCHAIN_KEY not in keys
    assert iunits.PIXI_RUST_RELEASE_DEPS_KEY in keys  # cargo-edit still lands
    assert iunits.PIXI_RUST_DEPS_KEY in keys  # so does the lint toolchain


# --------------------------------------------------------------------------
# The pixi task-ambiguity guard (TOL01-WS01) — the key-conflict guard's
# pixi-run-level sibling: a managed default-env task a consumer feature also
# defines would make `pixi run <task>` refuse the name, so the block is
# skipped and the consumer's own task stays authoritative.
# --------------------------------------------------------------------------

_CONSUMER_PIXI_WITH_FEATURE_TEST_TASK = """\
[workspace]
channels = ["conda-forge"]
name = "acme"
platforms = ["linux-64"]

[feature.test.tasks]
test = "cargo nextest run"

[environments]
test = ["test"]
"""


def test_test_task_block_is_skipped_when_a_feature_defines_the_task(tmp_path):
    (tmp_path / "pixi.toml").write_text(_CONSUMER_PIXI_WITH_FEATURE_TEST_TASK)
    plan = _plan(tmp_path)

    assert plan.pixi_task_conflicts == (
        irec.PixiTaskConflict(
            unit_key=iunits.PIXI_TEST_TASK_KEY,
            task="test",
            features=("test",),
        ),
    )
    # The conflicted block never reaches the plan; its [tasks] siblings do.
    keys = {d.unit.key for d in plan.decisions}
    assert iunits.PIXI_TEST_TASK_KEY not in keys
    assert iunits.PIXI_KEY in keys
    # Warn-only, worded off the one formatter, and ACTIONABLE both ways.
    warnings = verb.format_plan_warnings(plan)
    assert "pixi block skipped" in warnings
    assert "[feature.test.tasks]" in warnings
    assert "ambiguous" in warnings


def test_test_task_block_delivers_when_no_feature_defines_it(tmp_path):
    (tmp_path / "pixi.toml").write_text(
        _CONSUMER_PIXI_WITH_FEATURE_TEST_TASK.replace("test =", "e2e =", 1)
    )
    plan = _plan(tmp_path)
    assert plan.pixi_task_conflicts == ()
    decision = next(
        d for d in plan.decisions if d.unit.key == iunits.PIXI_TEST_TASK_KEY
    )
    assert decision.action == irec.ADD


def test_test_task_block_delivers_when_the_feature_is_not_env_enabled(tmp_path):
    # A `test` task under [feature.test.tasks] that NO [environments] entry
    # enables never reaches an env, so `pixi run test` is unambiguous — the
    # guard must not over-detect and skip the managed block. (Here the only
    # environment enables a different feature.)
    (tmp_path / "pixi.toml").write_text(
        "[workspace]\n"
        'channels = ["conda-forge"]\n'
        'name = "acme"\n'
        'platforms = ["linux-64"]\n\n'
        "[feature.test.tasks]\n"
        'test = "cargo nextest run"\n\n'
        "[environments]\n"
        'dev = ["lint"]\n'
    )
    plan = _plan(tmp_path)
    assert plan.pixi_task_conflicts == ()
    decision = next(
        d for d in plan.decisions if d.unit.key == iunits.PIXI_TEST_TASK_KEY
    )
    assert decision.action == irec.ADD


def test_a_spliced_test_task_block_is_not_a_task_conflict(tmp_path, rec):
    # Once the managed block is in, a later reconcile must read NOOP — the
    # guard is ADD-bound only, like the key-conflict guard.
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    plan = _plan(tmp_path)
    iapply.apply(plan, iapply.MODE_TREE)

    again = _plan(tmp_path)
    assert again.pixi_task_conflicts == ()
    decision = next(
        d for d in again.decisions if d.unit.key == iunits.PIXI_TEST_TASK_KEY
    )
    assert decision.action == irec.NOOP


def test_a_consumer_test_task_in_the_tasks_table_is_the_key_conflict_guards_case(
    tmp_path,
):
    # A same-named key in the [tasks] anchor itself is a DUPLICATE-KEY splice
    # (the #547 guard), not a task-ambiguity one — the block is still skipped.
    (tmp_path / "pixi.toml").write_text(
        '[workspace]\nchannels = ["conda-forge"]\nname = "acme"\n'
        'platforms = ["linux-64"]\n\n[tasks]\ntest = "pytest"\n'
    )
    plan = _plan(tmp_path)
    assert plan.pixi_task_conflicts == ()
    assert any(
        c.unit_key == iunits.PIXI_TEST_TASK_KEY and c.keys == ("test",)
        for c in plan.pixi_key_conflicts
    )
    assert iunits.PIXI_TEST_TASK_KEY not in {d.unit.key for d in plan.decisions}


def test_shipits_own_repo_keeps_its_feature_test_task_authoritative():
    # The dogfood pin: shipit's own full-gate `test` task lives in
    # [feature.test.tasks] (rust toolchain env + inline lexd provisioning), so
    # the reconcile must SKIP the managed caller here — otherwise every fresh
    # Tree's self-install would make bare `pixi run test` ambiguous.
    root = Path(__file__).resolve().parents[1]
    units = iunits.load_units()
    consumer_hashes = {u.key: irec.consumer_hash(root, u) for u in units}
    conflicts = irec._pixi_task_conflicts(root, units, consumer_hashes)
    assert any(
        c.unit_key == iunits.PIXI_TEST_TASK_KEY and c.task == "test" for c in conflicts
    )


def test_fresh_install_lays_down_the_session_bootstrap_set_idempotently(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    result = _apply(tmp_path)
    assert result.mode == iapply.MODE_TREE

    # The generic launcher landed at the repo root, executable, carrying the
    # host strategy table (#627).
    agent_launcher = tmp_path / "agent-start"
    assert agent_launcher.is_file()
    assert os.access(agent_launcher, os.X_OK)
    assert "--worktree" in agent_launcher.read_text()
    assert "session codex" in agent_launcher.read_text()

    # The SessionStart activation hook landed in .claude/settings.json.
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    entries = settings["hooks"]["SessionStart"]
    assert any(
        splice.is_shipit_hook(e, iunits.SETTINGS_SESSIONSTART_MARKER) for e in entries
    )

    # Both recorded a pristine hash in the manifest.
    managed = config.load_managed(config.load(tmp_path / ".shipit.toml"))
    assert iunits.AGENT_LAUNCHER_FILE in managed
    assert iunits.SETTINGS_SESSIONSTART_KEY in managed

    # Idempotent: a second reconcile decides NOOP for everything — nothing to
    # apply, no git, no PR, artifacts byte-identical.
    rec.calls.clear()
    agent_launcher_before = agent_launcher.read_bytes()
    settings_before = (tmp_path / ".claude" / "settings.json").read_bytes()
    again = _plan(tmp_path)
    assert again.nothing_to_do
    assert rec.calls == []
    assert agent_launcher.read_bytes() == agent_launcher_before
    assert (tmp_path / ".claude" / "settings.json").read_bytes() == settings_before


def _lay_down_launcher(tmp_path: Path) -> Path:
    """Write the shipped ``agent-start`` launcher unit into ``tmp_path``,
    executable, and return its path.

    The generic ``agent-start`` launcher carries the host strategy table, so
    behavior tests need it on disk — exactly what a real install lays down.
    """
    unit = {u.key: u for u in iunits.load_units()}[iunits.AGENT_LAUNCHER_FILE]
    path = tmp_path / unit.dest
    path.write_bytes(unit.content)
    path.chmod(0o755)
    return path


def _fake_cli(tmp_path: Path, name: str) -> dict[str, str]:
    """A fake ``name`` binary first on PATH (shadowing any real one) that
    prints its argv, one arg per line."""
    fakedir = tmp_path / "fakepath"
    fakedir.mkdir(exist_ok=True)
    fake = fakedir / name
    fake.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$@"\n')
    fake.chmod(0o755)
    return {"PATH": str(fakedir) + os.pathsep + os.environ.get("PATH", "")}


def test_agent_start_claude_execs_claude_with_a_minted_session_id(tmp_path: Path):
    # The claude row of the strategy table: exec `claude --worktree <minted-id>`
    # forwarding the remaining args, with a fresh `sess-`-prefixed id per launch.
    agent_start = _lay_down_launcher(tmp_path)
    env = _fake_cli(tmp_path, "claude")

    def launch(*args: str) -> list[str]:
        proc = subprocess.run(
            [str(agent_start), "claude", *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert proc.returncode == 0, proc.stderr
        return proc.stdout.splitlines()

    argv = launch("extra", "--args")
    assert argv[0] == "--worktree"
    assert argv[1].startswith("sess-")  # the minted, prefixed session id
    assert argv[2:] == ["extra", "--args"]  # the launcher's args pass through

    # A second launch mints a distinct id — no two sessions share a Tree id.
    assert launch()[1] != argv[1]


def test_agent_start_codex_execs_the_pinned_launcher(tmp_path: Path):
    # The codex row of the strategy table: exec `./bin/shipit session codex`,
    # forwarding the remaining args — never codex directly (codex has no
    # WorktreeCreate-style pre-launch seam; the pinned launcher provisions).
    agent_start = _lay_down_launcher(tmp_path)
    env = _fake_cli(tmp_path, "codex")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake_shipit = bindir / "shipit"
    fake_shipit.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$@"\n')
    fake_shipit.chmod(0o755)

    proc = subprocess.run(
        [str(agent_start), "codex", "--model", "foo"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines() == ["session", "codex", "--model", "foo"]


def test_agent_start_scrubs_the_inherited_worker_agent_identity_exports(
    tmp_path: Path,
):
    # The launch-seam agent-identity scrub (#631), behaviorally: a coordinator
    # launched via agent-start from inside a spawned worker Run's shell must NOT
    # hand the worker's agent-identity exports to the new session — ROLE (the
    # pretooluse edit guard's fallback would resolve the coordinator to that
    # role and silently disarm) nor the paired AGENT/RUN spawn ids (which would
    # mis-tag the new coordinator's log records with the worker's identity). A
    # task-correlation key like PR still rides — it describes the work, not who
    # is doing it. The scrub lives in the common path, so one fake CLI (claude)
    # covers every host row.
    agent_start = _lay_down_launcher(tmp_path)
    env = _fake_cli(tmp_path, "claude")
    fake = tmp_path / "fakepath" / "claude"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        'echo "role=${SHIPIT_LOG_CTX_ROLE-ABSENT}"\n'
        'echo "agent=${SHIPIT_LOG_CTX_AGENT-ABSENT}"\n'
        'echo "run=${SHIPIT_LOG_CTX_RUN-ABSENT}"\n'
        'echo "pr=${SHIPIT_LOG_CTX_PR-ABSENT}"\n'
    )

    proc = subprocess.run(
        [str(agent_start), "claude"],
        env={
            **env,
            "SHIPIT_LOG_CTX_ROLE": "implementer",
            "SHIPIT_LOG_CTX_AGENT": "deadbeef",
            "SHIPIT_LOG_CTX_RUN": "77",
            "SHIPIT_LOG_CTX_PR": "632",
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines() == [
        "role=ABSENT",
        "agent=ABSENT",
        "run=ABSENT",
        "pr=632",
    ]


def test_agent_start_rejects_an_unknown_or_missing_agent(tmp_path: Path):
    agent_start = _lay_down_launcher(tmp_path)

    proc = subprocess.run(
        [str(agent_start), "goose"], capture_output=True, text=True, timeout=10
    )
    assert proc.returncode == 64
    assert "unknown agent 'goose'" in proc.stderr

    proc = subprocess.run(
        [str(agent_start)], capture_output=True, text=True, timeout=10
    )
    assert proc.returncode == 64
    assert "usage:" in proc.stderr


def test_agent_start_fails_loud_when_the_cli_is_not_on_path(tmp_path: Path):
    agent_start = _lay_down_launcher(tmp_path)

    # A minimal PATH carrying `bash` (the shebang's interpreter) and `dirname`
    # (the launcher's own repo-root probe needs it; without it, bash ≥5.2 turns
    # the probe's `cd ""` into a hard "null directory" error and the script
    # dies at 1 before ever reaching the CLI check — older bash silently
    # no-op'd, which is how this test used to pass by accident) — and
    # deterministically NO `claude`, regardless of where the developer machine
    # keeps its binaries.
    bindir = tmp_path / "onlybash"
    bindir.mkdir()
    for tool in ("bash", "dirname"):
        binary = shutil.which(tool)
        assert binary is not None
        (bindir / tool).symlink_to(binary)
    proc = subprocess.run(
        [str(agent_start), "claude"],
        env={"PATH": str(bindir)},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 127
    # The hint names the PRODUCT to install (restored in #631) — "claude" the
    # binary is Claude Code the product; a generic "install it first" made the
    # user go look the mapping up.
    assert "claude CLI is not on PATH" in proc.stderr
    assert "install Claude Code first" in proc.stderr


def test_settings_hook_splice_preserves_other_settings():
    consumer = json.dumps(
        {
            "permissions": {"allow": ["Bash(ls:*)"]},
            "hooks": {
                "SessionStart": [{"hooks": [{"type": "command", "command": "echo hi"}]}]
            },
        }
    )
    inner = json.dumps(
        {
            "matcher": "Edit|Write",
            "hooks": [
                {"type": "command", "command": "pixi run shipit hook pretooluse"}
            ],
        }
    )
    out = splice.splice_settings_hook(consumer, inner)
    data = json.loads(out)
    # The consumer's unrelated settings survive untouched.
    assert data["permissions"] == {"allow": ["Bash(ls:*)"]}
    assert data["hooks"]["SessionStart"][0]["hooks"][0]["command"] == "echo hi"
    # shipit's entry is now present in PreToolUse.
    assert splice.extract_settings_hook(out) == iunits.canonical_hook_entry(
        json.loads(inner)
    )


def _unit(key):
    return next(u for u in iunits.load_units() if u.key == key)


def test_settings_hook_splice_is_idempotent_and_replaces_in_place():
    inner = _unit(iunits.SETTINGS_KEY).desired_inner()
    once = splice.splice_settings_hook("", inner)
    twice = splice.splice_settings_hook(once, inner)
    assert twice == once
    # Exactly one shipit PreToolUse entry, even after a second splice.
    pre = json.loads(twice)["hooks"]["PreToolUse"]
    assert sum(splice.is_shipit_hook(e) for e in pre) == 1


def test_settings_hook_extract_is_none_when_absent():
    # Genuinely "absent" (→ ADD): empty file, an empty object, or an object that
    # carries only the consumer's own hooks (no shipit entry).
    assert splice.extract_settings_hook("") is None
    assert splice.extract_settings_hook("{}") is None
    other = json.dumps(
        {"hooks": {"PreToolUse": [{"hooks": [{"command": "echo other"}]}]}}
    )
    assert splice.extract_settings_hook(other) is None


def test_settings_hook_extract_flags_malformed_as_non_none():
    # A present-but-malformed file is NOT "absent": extract returns a non-None
    # sentinel so the reconciler reads it as present-but-divergent (→ OVERRIDE),
    # never an ADD onto a file it cannot parse.
    assert splice.extract_settings_hook("not json") is not None
    assert splice.extract_settings_hook("{bad json,,}") is not None
    # Valid JSON that is not an object is also a conflict, not an absent file.
    assert splice.extract_settings_hook("[1, 2, 3]") is not None
    assert splice.extract_settings_hook('"a string"') is not None


def test_is_shipit_hook_is_defensive_against_malformed_entries():
    # Malformed PreToolUse entries must answer "not a shipit hook", never raise.
    assert splice.is_shipit_hook({"hooks": None}) is False
    assert splice.is_shipit_hook({"hooks": "not-a-list"}) is False
    assert splice.is_shipit_hook({"hooks": [None, "x", 7]}) is False
    assert splice.is_shipit_hook({}) is False
    assert splice.is_shipit_hook("not-a-dict") is False
    assert splice.is_shipit_hook(None) is False
    # A hook whose `command` is null/non-string must not crash on `marker in None`.
    assert splice.is_shipit_hook({"hooks": [{"command": None}]}) is False
    assert splice.is_shipit_hook({"hooks": [{"command": 7}]}) is False
    assert splice.is_shipit_hook({"hooks": [{}]}) is False


def test_settings_hook_splice_preserves_a_malformed_file_verbatim():
    # The write path agrees with the read path: an unparseable consumer file (or
    # one that is not a JSON object) is preserved byte-for-byte, never clobbered
    # and never a JSONDecodeError crash.
    inner = _unit(iunits.SETTINGS_KEY).desired_inner()
    malformed = '{ "permissions": [ this is not json ]\n'
    assert splice.splice_settings_hook(malformed, inner) == malformed
    not_an_object = "[1, 2, 3]\n"
    assert splice.splice_settings_hook(not_an_object, inner) == not_an_object


def test_settings_hook_reconciles_through_the_four_cases():
    """The settings hook unit gives the standard ADD/NOOP/UPDATE/OVERRIDE decisions."""
    unit = _unit(iunits.SETTINGS_KEY)
    desired = unit.desired_hash()
    extract = splice.extract_settings_hook
    h = lambda inner: config.content_hash(inner.encode("utf-8"))  # noqa: E731

    # absent → ADD
    assert (
        irec.decide(consumer_hash=None, pristine_hash=None, desired_hash=desired)
        == irec.ADD
    )
    # unchanged (consumer carries shipit's exact entry) → NOOP
    on_disk = splice.splice_settings_hook("", unit.desired_inner())
    cur = h(extract(on_disk))
    assert cur == desired
    assert (
        irec.decide(consumer_hash=cur, pristine_hash=desired, desired_hash=desired)
        == irec.NOOP
    )
    # consumer edited shipit's own entry → OVERRIDE (not clobbered, surfaced in PR)
    edited = on_disk.replace("Edit|Write|MultiEdit|NotebookEdit", "Edit")
    cedit = h(extract(edited))
    assert cedit != desired
    assert (
        irec.decide(consumer_hash=cedit, pristine_hash=desired, desired_hash=desired)
        == irec.OVERRIDE
    )


# --------------------------------------------------------------------------
# apply — typed InstallResult in/out, the git/PR boundary recorded
# --------------------------------------------------------------------------


class _GhRecorder:
    """Records the git/PR boundary calls apply makes, doing nothing real."""

    def __init__(self):
        self.calls = []
        self.pr_body = None
        self.hook_activations = []
        self.commit_paths = ()
        self.commit_no_verify = None
        self.push_no_verify = None

    def activate_hooks(self, root):
        # Stand in for `lefthook install`: record the call, mutate nothing.
        self.hook_activations.append(root)
        return _exec_result(0)

    def switch_create(self, branch, *, cwd):
        self.calls.append(("switch", branch))

    def add(self, paths, *, cwd):
        self.calls.append(("add", tuple(paths)))

    def commit(self, message, paths, *, cwd, no_verify=False):
        self.calls.append(("commit", message))
        self.commit_paths = tuple(paths)
        self.commit_no_verify = no_verify

    def push(self, branch, *, cwd, remote="origin", force=False, no_verify=False):
        self.calls.append(("push", branch))
        self.push_no_verify = no_verify

    def current_branch(self, *, cwd):
        return "main"

    def pr_url_for_head(self, branch, *, cwd=None):
        return None  # no existing PR by default

    def pr_create(self, *, head, title, body, draft, cwd, **kw):
        self.calls.append(("pr_create", draft))
        self.pr_body = body
        return "https://github.com/acme/repo/pull/1"

    def names(self):
        return [c[0] for c in self.calls]


@pytest.fixture
def rec(monkeypatch):
    r = _GhRecorder()
    for name in (
        "switch_create",
        "add",
        "commit",
        "push",
        "current_branch",
    ):
        monkeypatch.setattr(git, name, getattr(r, name))
    for name in ("pr_url_for_head", "pr_create"):
        monkeypatch.setattr(gh, name, getattr(r, name))
    monkeypatch.setattr(iapply, "_shipit_version", lambda: "testhash")
    # Inject the lefthook boundary so no test spawns a real `lefthook install`
    # (mirrors how lint tests inject run_tool). Real activation is covered
    # directly against the Exec runner in test_activate_hooks_* below.
    monkeypatch.setattr(iapply, "_activate_hooks", r.activate_hooks)
    # Stub the self-certification boundaries (ADR-0033) the same way: the
    # committing modes certify by default, and these tests are not about the
    # postconditions (no pixi solve / scoped lint / launcher probe spawns).
    # The real checks are covered in tests/test_install_selfcert.py.
    monkeypatch.setattr(selfcert, "certify", _cert_ok)
    monkeypatch.setattr(selfcert, "consumer_debt", lambda root, **kw: None)
    return r


def test_dry_run_has_no_side_effects(tmp_path, rec):
    # The verb's dry-run stops at the Plan: reconcile reads, nothing writes.
    rc = verb.run(str(tmp_path), dry_run=True)
    assert rc == 0
    assert not (tmp_path / ".shipit.toml").exists()
    assert not (tmp_path / "skills").exists()
    assert rec.calls == []  # no git, no PR
    assert rec.hook_activations == []  # no side effect on dry-run


def test_fresh_install_writes_set_and_opens_draft_pr(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n\nConsumer text.\n")
    result = _apply(tmp_path, iapply.MODE_PR)

    # The typed result names the PR outcome.
    assert result.mode == iapply.MODE_PR
    assert result.branch == iapply.INSTALL_BRANCH
    assert result.pr_url == "https://github.com/acme/repo/pull/1"
    assert result.pr_updated is False

    # Managed files landed.
    assert (tmp_path / "skills" / "to-spec" / "SKILL.md").is_file()
    assert (tmp_path / "bin" / "shipit").is_file()
    # The AGENTS block was spliced in without losing the consumer's text.
    agents = (tmp_path / "AGENTS.md").read_text()
    assert "Consumer text." in agents
    assert iunits.BLOCK_OPEN in agents

    # Manifest written with version + a pristine for every unit.
    cfg = config.load(tmp_path / ".shipit.toml")
    assert config.shipit_version(cfg) == "testhash"
    managed = config.load_managed(cfg)
    assert "bin/shipit" in managed and "AGENTS.md#shipit-block" in managed

    # A DRAFT PR was opened; the rendered body lists the additions.
    assert ("pr_create", True) in rec.calls
    assert "### Added" in rec.pr_body
    # Order: branch -> add -> commit -> push -> pr.
    assert rec.names() == ["switch", "add", "commit", "push", "pr_create"]


def test_fresh_install_provisions_agent_defs_and_settings_hook(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    _apply(tmp_path)

    # The three generated agent-defs land under .claude/agents/.
    for role in ("implementer", "shepherd", "explorer"):
        dest = tmp_path / ".claude" / "agents" / f"{role}.md"
        assert dest.is_file()
        assert f"name: {role}" in dest.read_text()

    # The PreToolUse hook line lands in .claude/settings.json.
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    pre = settings["hooks"]["PreToolUse"]
    assert any(splice.is_shipit_hook(e) for e in pre)

    # Both kinds recorded a pristine hash in the manifest.
    managed = config.load_managed(config.load(tmp_path / ".shipit.toml"))
    assert ".claude/agents/implementer.md" in managed
    assert iunits.SETTINGS_KEY in managed


def test_install_merges_settings_hook_without_clobbering_consumer_settings(
    tmp_path, rec
):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    # A consumer who already has settings.json with their own permissions + hook.
    settings_path.write_text(
        json.dumps(
            {
                "permissions": {"allow": ["Bash(ls:*)"]},
                "hooks": {
                    "SessionStart": [
                        {"hooks": [{"type": "command", "command": "echo hi"}]}
                    ]
                },
            },
            indent=2,
        )
    )
    _apply(tmp_path)

    merged = json.loads(settings_path.read_text())
    # The consumer's settings are intact, and shipit's hook was merged alongside.
    assert merged["permissions"] == {"allow": ["Bash(ls:*)"]}
    assert merged["hooks"]["SessionStart"][0]["hooks"][0]["command"] == "echo hi"
    assert any(splice.is_shipit_hook(e) for e in merged["hooks"]["PreToolUse"])


def test_consumer_edit_to_settings_hook_surfaces_as_override(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    _apply(tmp_path)
    rec.calls.clear()

    # The consumer narrows shipit's managed PreToolUse matcher.
    settings_path = tmp_path / ".claude" / "settings.json"
    data = json.loads(settings_path.read_text())
    for entry in data["hooks"]["PreToolUse"]:
        if splice.is_shipit_hook(entry):
            entry["matcher"] = "Edit"
    settings_path.write_text(json.dumps(data, indent=2))

    # The edit is a typed OVERRIDE decision on the plan...
    plan = _plan(tmp_path)
    assert [d.unit.key for d in plan.overrides] == [iunits.SETTINGS_KEY]
    # ...and the PR-mode apply surfaces it in the body, never clobbered blind.
    result = _apply(tmp_path, iapply.MODE_PR)
    assert result.pr_url is not None
    assert ("pr_create", True) in rec.calls
    assert "### Overrides" in rec.pr_body
    assert iunits.SETTINGS_FILE in rec.pr_body


def test_consumer_edit_to_agent_def_surfaces_as_override(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    _apply(tmp_path)
    rec.calls.clear()

    (tmp_path / ".claude" / "agents" / "implementer.md").write_text("HAND EDIT\n")
    _apply(tmp_path, iapply.MODE_PR)
    assert ("pr_create", True) in rec.calls
    assert "### Overrides" in rec.pr_body
    assert ".claude/agents/implementer.md" in rec.pr_body
    assert "HAND EDIT" in rec.pr_body


def test_install_against_malformed_settings_json_does_not_crash(tmp_path, rec):
    # A consumer whose .claude/settings.json is unparseable must NOT crash install
    # and must NOT be clobbered: the file is left byte-for-byte untouched and the
    # conflict is surfaced as an OVERRIDE for a human (reconcile, never clobber).
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    garbage = '{ "permissions": [ this is not valid json,,, ]\n'
    settings_path.write_text(garbage)

    result = _apply(tmp_path, iapply.MODE_PR)

    assert result.pr_url is not None  # completed without raising
    # The malformed file was left exactly as it was — never overwritten.
    assert settings_path.read_text() == garbage
    # The conflict is surfaced for the human, not silently swallowed.
    assert ("pr_create", True) in rec.calls
    assert "### Overrides" in rec.pr_body
    assert iunits.SETTINGS_FILE in rec.pr_body


def test_reinstall_with_no_changes_is_a_clean_noop(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    _apply(tmp_path)
    rec.calls.clear()
    # The second reconcile decides a no-op plan; the verb never applies it.
    assert _plan(tmp_path).nothing_to_do
    rc = verb.run(str(tmp_path))
    assert rc == 0
    # Nothing committed, no PR opened the second time.
    assert rec.calls == []


def test_consumer_edit_surfaces_as_override(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    _apply(tmp_path)
    rec.calls.clear()

    # The consumer edits a managed skill file.
    skill = tmp_path / "skills" / "to-spec" / "SKILL.md"
    skill.write_text("CONSUMER EDIT\n")

    _apply(tmp_path, iapply.MODE_PR)
    assert ("pr_create", True) in rec.calls
    assert "### Overrides" in rec.pr_body
    assert "skills/to-spec/SKILL.md" in rec.pr_body
    # The diff is captured BEFORE the overwrite, so it shows the consumer's edit
    # (a non-empty diff), not an empty diff against what shipit just wrote.
    assert "CONSUMER EDIT" in rec.pr_body
    assert "```diff" in rec.pr_body


# --------------------------------------------------------------------------
# Declined units (#600) — `.shipit.toml [managed.decline].keep`, the durable
# form of hand-declining the same OVERRIDE in every reconcile PR
# --------------------------------------------------------------------------


def _decline(root, *keys):
    """Append a ``[managed.decline]`` table to the consumer's ``.shipit.toml``."""
    cfg = root / config.CONFIG_NAME
    existing = cfg.read_text() if cfg.is_file() else ""
    keep = ", ".join(f'"{k}"' for k in keys)
    cfg.write_text(f"{existing}\n[managed.decline]\nkeep = [{keep}]\n")


def test_declined_unit_makes_a_would_be_override_a_clean_noop(tmp_path, rec):
    # The #597 shape: the consumer keeps its own bin/shipit (the dogfood repo's
    # source-deferring bootstrap). Without the decline, every reconcile would
    # re-propose the same OVERRIDE and need the same hand-decline at merge.
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    _apply(tmp_path)
    rec.calls.clear()
    (tmp_path / "bin" / "shipit").write_text("#!/bin/sh\n# MY OWN LAUNCHER\n")
    _decline(tmp_path, "bin/shipit")

    plan = _plan(tmp_path)
    assert plan.declined == (iunits.SHIPIT_LAUNCHER_FILE,)
    assert plan.overrides == ()  # the edit is never re-proposed
    assert all(d.unit.key != iunits.SHIPIT_LAUNCHER_FILE for d in plan.decisions)
    assert plan.nothing_to_do  # the recurring hand-decline is gone
    rc = verb.run(str(tmp_path))
    assert rc == 0
    assert rec.calls == []  # no branch, no commit, no PR
    # The consumer's own launcher was never touched.
    assert "MY OWN LAUNCHER" in (tmp_path / "bin" / "shipit").read_text()


def test_declined_unit_is_never_written_and_drops_from_the_manifest(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    _apply(tmp_path)
    rec.calls.clear()
    (tmp_path / "bin" / "shipit").write_text("#!/bin/sh\n# MY OWN LAUNCHER\n")
    _decline(tmp_path, "bin/shipit")
    # Another unit changes, so the plan still has work — an applying install runs.
    (tmp_path / "skills" / "to-spec" / "SKILL.md").unlink()

    result = _apply(tmp_path, iapply.MODE_PR)
    assert result.pr_url is not None
    # The declined unit: untouched on disk, dropped from the re-stamped map (so
    # no stale pristine entry lingers to re-propose the override).
    assert "MY OWN LAUNCHER" in (tmp_path / "bin" / "shipit").read_text()
    cfg_path = tmp_path / config.CONFIG_NAME
    cfg = config.load(cfg_path)
    managed = config.load_managed(cfg)
    assert iunits.SHIPIT_LAUNCHER_FILE not in managed
    assert "skills/to-spec/SKILL.md" in managed
    # The decline itself survives the manifest re-stamp (the durable half)...
    assert config.load_declines(cfg, cfg_path.read_text()) == (
        iunits.SHIPIT_LAUNCHER_FILE,
    )
    # ...and the PR body carries the standing decision.
    assert "### Declined units" in rec.pr_body
    assert "`bin/shipit`" in rec.pr_body


def test_fresh_install_skips_a_pre_declined_unit(tmp_path, rec):
    # Declining BEFORE the first install: the unit is never delivered at all
    # (no ADD), not delivered-then-hand-reverted.
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    _decline(tmp_path, "bin/shipit")
    _apply(tmp_path, iapply.MODE_PR)
    assert not (tmp_path / "bin" / "shipit").exists()
    assert "### Declined units" in rec.pr_body


def test_unmatched_decline_key_warns_never_silently_ignores(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    _apply(tmp_path)
    _decline(tmp_path, "no/such-unit")
    plan = _plan(tmp_path)
    assert plan.decline_unmatched == ("no/such-unit",)
    assert plan.declined == ()
    warnings = verb.format_plan_warnings(plan)
    assert "no/such-unit" in warnings
    assert "names no managed unit" in warnings


def test_duplicate_decline_key_is_de_duped_on_both_surfaces(tmp_path, rec):
    # A key listed twice in [managed.decline].keep must surface once, not twice —
    # both `declined` and `decline_unmatched` de-dupe, so the plan/PR/warning
    # output stays stable instead of emitting the same line per repeat.
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    _apply(tmp_path)
    _decline(tmp_path, "bin/shipit", "bin/shipit", "no/such-unit", "no/such-unit")
    plan = _plan(tmp_path)
    assert plan.declined == ("bin/shipit",)
    assert plan.decline_unmatched == ("no/such-unit",)


def test_format_plan_renders_the_standing_decline_line():
    plan = irec.Plan(
        root="/consumer",
        decisions=(),
        retired=(),
        seeds=(),
        declined=(iunits.SHIPIT_LAUNCHER_FILE,),
    )
    text = verb.format_plan(plan)
    assert "decline" in text
    assert iunits.SHIPIT_LAUNCHER_FILE in text
    # With a declined unit listed, "managed set is current" would read as a
    # contradiction — the wording shifts like the kept-retired case.
    assert "nothing to do — no automated changes to apply." in text


def test_shipits_own_manifest_declines_the_launcher():
    # The dogfood resolution of #600: shipit's own bin/shipit is the
    # source-deferring bootstrap (CI and dev flows exec shipit FROM SOURCE via
    # the pixi env), which necessarily differs from the packaged pinned uv
    # launcher — so the repo carries the durable decline instead of hand-
    # reverting the same override in every reconcile PR (#597).
    cfg_path = REPO_ROOT / config.CONFIG_NAME
    cfg = config.load(cfg_path)
    assert iunits.SHIPIT_LAUNCHER_FILE in config.load_declines(
        cfg, cfg_path.read_text()
    )
    packaged = iunits.data_bytes("bootstrap", "shipit")
    committed = (REPO_ROOT / "bin" / "shipit").read_bytes()
    # The standing reason for the decline: if these ever converge, the decline
    # (and this pin) should be revisited.
    assert committed != packaged


def test_fresh_install_delivers_the_lint_environment(tmp_path, rec):
    # ADP00 (docs/legacy-prd/adoption.md): a fresh install ADDs the lint env blocks —
    # the consumer's pixi.toml ends up a complete, valid manifest whose lint
    # environment carries the fleet-pinned toolchain, alongside the consumer's
    # own untouched content.
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    (tmp_path / "pixi.toml").write_text(
        '[workspace]\nname = "acme"\nchannels = ["conda-forge"]\n'
        'platforms = ["osx-arm64"]\n\n[tasks]\ntest = "pytest"\n'
    )
    _apply(tmp_path)

    manifest = tomllib.loads((tmp_path / "pixi.toml").read_text())  # valid TOML
    # The consumer's own content is preserved.
    assert manifest["workspace"]["name"] == "acme"
    assert manifest["tasks"]["test"] == "pytest"
    # The managed task, the pinned toolchain, and the environment definition —
    # everything `pixi run -e lint lint` needs on a stock consumer.
    assert manifest["tasks"]["lint"] == "./bin/shipit lint"
    deps = manifest["feature"]["lint"]["dependencies"]
    assert set(deps) == set(LINT_TOOLS)
    assert manifest["environments"]["lint"] == ["lint"]

    # Both blocks recorded a pristine hash in the manifest...
    managed = config.load_managed(config.load(tmp_path / ".shipit.toml"))
    assert iunits.PIXI_LINT_DEPS_KEY in managed
    assert iunits.PIXI_ENVS_KEY in managed
    # ...and an unchanged re-install is a clean NOOP.
    assert _plan(tmp_path).nothing_to_do


def test_lint_env_block_merges_into_an_existing_environments_table(tmp_path, rec):
    # A consumer with their own [environments] keeps it: the managed `lint`
    # entry lands INSIDE the existing table (never a duplicate header, which
    # would be invalid TOML).
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    (tmp_path / "pixi.toml").write_text('[environments]\ndev = ["dev"]\n')
    _apply(tmp_path)

    manifest = tomllib.loads((tmp_path / "pixi.toml").read_text())
    assert manifest["environments"] == {"dev": ["dev"], "lint": ["lint"]}


def test_consumer_edit_to_lint_deps_block_surfaces_as_override(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    _apply(tmp_path)
    rec.calls.clear()

    # The consumer bumps a pinned tool inside the managed block.
    pixi_path = tmp_path / "pixi.toml"
    pixi_path.write_text(
        pixi_path.read_text().replace('ruff = "0.15.*"', 'ruff = "0.99.*"')
    )

    # The edit is a typed OVERRIDE decision on the plan...
    plan = _plan(tmp_path)
    assert [d.unit.key for d in plan.overrides] == [iunits.PIXI_LINT_DEPS_KEY]
    # ...surfaced in the PR body with the consumer's edit, never clobbered blind.
    _apply(tmp_path, iapply.MODE_PR)
    assert ("pr_create", True) in rec.calls
    assert "### Overrides" in rec.pr_body
    assert 'ruff = "0.99.*"' in rec.pr_body


# --------------------------------------------------------------------------
# The pixi-manifest seed (ADP00-WS09, #432) — a stock consumer with NO
# pixi.toml gets a minimal VALID [workspace] table around the managed blocks
# --------------------------------------------------------------------------


def test_pixi_manifest_seed_is_valid_toml_with_a_sanitized_name():
    # The pure seed renderer: parseable TOML carrying the one table pixi
    # requires, with the name slugged so an exotic directory name can neither
    # break the TOML string nor produce a name pixi rejects.
    seed = tomllib.loads(iunits.pixi_manifest_seed("shipit-canary"))
    assert seed["workspace"]["name"] == "shipit-canary"
    assert seed["workspace"]["channels"] == list(iunits.PIXI_SEED_CHANNELS)
    assert seed["workspace"]["platforms"] == list(iunits.PIXI_SEED_PLATFORMS)

    weird = tomllib.loads(iunits.pixi_manifest_seed('my repo "v2"!'))
    assert weird["workspace"]["name"] == "my-repo-v2"
    # Never empty, even from a name with no salvageable characters.
    assert tomllib.loads(iunits.pixi_manifest_seed("«»"))["workspace"]["name"]


def test_fresh_consumer_without_pixi_manifest_gets_a_valid_seed(tmp_path, rec):
    # The #432 canary failure: no pixi.toml at all is the STOCK adoption case.
    # Install must leave a manifest pixi parses — a [workspace] table plus the
    # three managed blocks — from the very first commit.
    (tmp_path / "AGENTS.md").write_text("# Acme\n")

    plan = _plan(tmp_path)
    assert plan.seed_pixi_manifest is True
    # The dry-run report announces the seed before anything is written.
    assert "pixi.toml ([workspace] table" in verb.format_plan(plan, dry_run=True)

    _apply(tmp_path, iapply.MODE_PR)

    manifest = tomllib.loads((tmp_path / "pixi.toml").read_text())  # valid TOML
    # The seeded required table, named from the consumer root.
    assert manifest["workspace"]["name"] == iunits.workspace_name(tmp_path.name)
    assert manifest["workspace"]["channels"] == list(iunits.PIXI_SEED_CHANNELS)
    # ...and everything `pixi run -e lint lint` needs, spliced in beneath it.
    assert manifest["tasks"]["lint"] == "./bin/shipit lint"
    assert manifest["tasks"]["test"] == "./bin/shipit test"
    assert set(manifest["feature"]["lint"]["dependencies"]) == set(LINT_TOOLS)
    assert manifest["environments"]["lint"] == ["lint"]
    # ...and the launcher's uv (#758): the managed tasks all ride ./bin/shipit.
    assert "uv" in manifest["dependencies"]

    # The seed is scaffold, not a managed unit: only the five block units are
    # recorded, so the [workspace] table is consumer-owned from here on.
    managed = config.load_managed(config.load(tmp_path / ".shipit.toml"))
    pixi_keys = {k for k in managed if k.startswith("pixi.toml")}
    assert pixi_keys == {
        iunits.PIXI_KEY,
        iunits.PIXI_TEST_TASK_KEY,
        iunits.PIXI_LINT_DEPS_KEY,
        iunits.PIXI_ENVS_KEY,
        iunits.PIXI_LAUNCHER_DEPS_KEY,
    }

    # The PR body tells the merger the table was seeded and is theirs to edit.
    assert "### Pixi manifest seeded" in rec.pr_body

    # A re-install is a clean NOOP — the seed decision does not resurface.
    replan = _plan(tmp_path)
    assert replan.nothing_to_do and replan.seed_pixi_manifest is False


def test_seeded_workspace_table_is_consumer_owned(tmp_path, rec):
    # A consumer edit to the seeded [workspace] table is NOT drift: the table
    # was never hashed into [managed], so a re-install stays a clean no-op.
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    _apply(tmp_path)

    pixi_path = tmp_path / "pixi.toml"
    pixi_path.write_text(
        pixi_path.read_text().replace("platforms = [", 'license = "MIT"\nplatforms = [')
    )
    assert _plan(tmp_path).nothing_to_do


def test_existing_pixi_manifest_is_never_seeded(tmp_path, rec):
    # A consumer WITH a manifest keeps today's behavior: blocks reconciled into
    # it, header untouched, no seed decided.
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    (tmp_path / "pixi.toml").write_text('[workspace]\nname = "acme"\n')

    plan = _plan(tmp_path)
    assert plan.seed_pixi_manifest is False

    _apply(tmp_path)
    manifest = tomllib.loads((tmp_path / "pixi.toml").read_text())
    assert manifest["workspace"] == {"name": "acme"}  # untouched
    assert manifest["tasks"]["lint"] == "./bin/shipit lint"


def test_seed_never_clobbers_a_manifest_created_after_gather(tmp_path, rec):
    # The gather→apply window: a pixi.toml that appeared after the plan was
    # decided is a consumer file — the seed write is skipped, the blocks still
    # splice into it.
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    plan = _plan(tmp_path)
    assert plan.seed_pixi_manifest is True

    (tmp_path / "pixi.toml").write_text('[workspace]\nname = "late"\n')
    iapply.apply(plan)

    manifest = tomllib.loads((tmp_path / "pixi.toml").read_text())
    assert manifest["workspace"] == {"name": "late"}
    assert manifest["tasks"]["lint"] == "./bin/shipit lint"


def test_open_install_pr_is_updated_not_recreated(tmp_path, rec, monkeypatch):
    # An install PR already exists for the branch (a prior unmerged install).
    monkeypatch.setattr(
        gh, "pr_url_for_head", lambda branch, cwd=None: "https://x/pull/7"
    )
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    result = _apply(tmp_path, iapply.MODE_PR)
    # The branch was force-pushed, but no second PR was created — the typed
    # result says which of the two happened.
    assert result.pr_updated is True
    assert result.pr_url == "https://x/pull/7"
    assert "push" in rec.names()
    assert "pr_create" not in rec.names()


def test_default_install_refreshes_working_tree_without_git_or_pr(tmp_path, rec):
    # #359: the DEFAULT mode is a working-tree refresh — the managed set and
    # manifest land on disk uncommitted, and the git/gh side-effect set is
    # empty: no branch switch, no commit, no push, no PR. Committing the
    # refresh into the caller's own work is the caller's job.
    (tmp_path / "AGENTS.md").write_text("# Acme\n\nConsumer text.\n")
    result = _apply(tmp_path)
    assert result.mode == iapply.MODE_TREE
    assert result.branch is None and result.pr_url is None

    # The managed set + manifest are on disk...
    assert (tmp_path / "bin" / "shipit").is_file()
    agents = (tmp_path / "AGENTS.md").read_text()
    assert "Consumer text." in agents
    assert iunits.BLOCK_OPEN in agents
    managed = config.load_managed(config.load(tmp_path / ".shipit.toml"))
    assert "bin/shipit" in managed
    # ...and not one git/gh call was made.
    assert rec.calls == []


def test_default_install_mid_drift_never_branches_or_opens_pr(tmp_path, rec):
    # The #359 trap as a regression test: managed-file drift mid-workstream,
    # install run in the default mode → the drift is refreshed in place (a
    # consumer-edited unit included, surfaced by the renderer) and NOTHING
    # touches git or origin — no shipit/install branch, no stray draft PR.
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    _apply(tmp_path)
    rec.calls.clear()

    skill = tmp_path / "skills" / "to-spec" / "SKILL.md"
    skill.write_text("CONSUMER EDIT\n")
    result = _apply(tmp_path)
    # The drifted unit was refreshed to shipit's content, in the working tree.
    assert "CONSUMER EDIT" not in skill.read_text()
    # No switch, no add, no commit, no push, no pr_create — the trap is closed.
    assert rec.calls == []
    # The override is surfaced loudly for the caller, not silently swallowed:
    # the renderer's stderr warning derives from the typed result.
    warning = verb.format_result_warnings(result)
    assert "consumer-edited" in warning
    assert "skills/to-spec/SKILL.md" in warning


def test_push_flag_pushes_to_branch_without_pr(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    result = _apply(tmp_path, iapply.MODE_PUSH)
    assert result.branch == "main"
    assert ("push", "main") in rec.calls
    assert "pr_create" not in rec.names()


def test_pr_mode_on_virgin_repo_with_lint_debt_reaches_the_pr_leg(tmp_path, rec):
    # #477 acceptance: `install --pr` on a virgin repo installs + ACTIVATES the
    # managed pre-push hook during staging, then pushes its own branch. On a
    # consumer with PRE-EXISTING whole-tree lint debt the freshly-armed pre-push
    # gate would kill that very push — the tripwire armed by the run that trips
    # it — so install's OWN git operations (commit AND push) carry the hook
    # bypass (ADR-0003 / ADR-0033); the debt is REPORTED in the PR body, never
    # a blocker.
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    # Deliberate pre-existing lint debt, untouched by install (the lint gate is
    # faked with the rest of the exec seam; the file stands in for a whole-tree
    # violation like the markdownlint/lexd failures observed on lex-fmt/lex).
    (tmp_path / "DEBT.md").write_text("#bad-heading\nline with trailing spaces  \n")
    debt_calls = []

    def debt_reader(root):
        debt_calls.append(root)
        return 5

    result = _apply(tmp_path, iapply.MODE_PR, debt=debt_reader)

    # The hooks were armed by this very run…
    assert rec.hook_activations == [tmp_path]
    # …and the push/PR leg still ran, both git write ops bypassing the hooks.
    assert rec.names() == ["switch", "add", "commit", "push", "pr_create"]
    assert rec.commit_no_verify is True
    assert rec.push_no_verify is True
    assert result.pr_url == "https://github.com/acme/repo/pull/1"
    # The observed debt is a REPORT in the PR body, not a failure.
    assert debt_calls == [tmp_path]
    assert result.lint_debt == 5
    assert "5 failing check(s)" in rec.pr_body


def test_break_glass_push_bypasses_the_repo_hooks(tmp_path, rec):
    # MODE_PUSH is still install's OWN push (#477): same bypass as the PR-mode
    # push — the whole-tree gate is the repo's bar, never install's.
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    _apply(tmp_path, iapply.MODE_PUSH)
    assert rec.commit_no_verify is True
    assert rec.push_no_verify is True


def test_local_flag_commits_on_current_branch_without_push_or_pr(tmp_path, rec):
    # #170: local-only mode commits the managed set on the CURRENT branch and stops
    # — no branch switch, no push, no PR. This is what Tree provisioning runs so
    # `tree create` never touches origin.
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    result = _apply(tmp_path, iapply.MODE_LOCAL)
    assert result.branch == "main"
    # The managed set was written and committed.
    assert (tmp_path / "bin" / "shipit").is_file()
    assert rec.names() == ["add", "commit"]
    # No branch switch, no push, no PR — the origin-side-effect set is empty.
    assert "switch" not in rec.names()
    assert "push" not in rec.names()
    assert "pr_create" not in rec.names()


def test_local_mode_fails_in_detached_head(tmp_path, monkeypatch, rec):
    # --local commits on the CURRENT branch; in detached HEAD there is none, so
    # the apply refuses with the typed domain error and commits nothing.
    monkeypatch.setattr(git, "current_branch", lambda *, cwd: None)
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    with pytest.raises(InstallError, match="--local needs a checked-out branch"):
        _apply(tmp_path, iapply.MODE_LOCAL)
    assert "commit" not in rec.names()


def test_local_flag_detached_head_is_a_clean_exit_through_the_shell(
    tmp_path, monkeypatch, rec, capsys
):
    # Through the verb, the same refusal is the uniform `error: …` + exit 1.
    monkeypatch.setattr(git, "current_branch", lambda *, cwd: None)
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    rc = verb.run(str(tmp_path), local=True)
    assert rc == 1
    assert "commit" not in rec.names()
    err = capsys.readouterr().err
    assert err.startswith("error: ") or "error: " in err
    assert "--local needs a checked-out branch" in err


def test_stale_manifest_keys_are_dropped(tmp_path, rec):
    # A prior manifest claims a unit shipit no longer manages.
    config.write_manifest(
        tmp_path / ".shipit.toml",
        version="old",
        managed={"skills/retired/SKILL.md": "sha256:dead", "bin/shipit": "sha256:old"},
    )
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    _apply(tmp_path)
    managed = config.load_managed(config.load(tmp_path / ".shipit.toml"))
    # The retired key is gone; the manifest reflects only the current set.
    assert "skills/retired/SKILL.md" not in managed
    assert set(managed) == {u.key for u in iunits.load_units()}


def test_gh_failure_is_a_clean_nonzero_exit(tmp_path, monkeypatch, rec, capsys):
    def boom(*a, **k):
        raise ExecError(["gh"], rc=1, stderr="no remote configured")

    monkeypatch.setattr(git, "switch_create", boom)
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    rc = verb.run(str(tmp_path), pr=True)
    assert rc == 1  # clean exit through the error shell, not a raised traceback
    assert "error: " in capsys.readouterr().err


def test_gather_refuses_a_non_directory_target(tmp_path):
    # The domain refusal for a direct caller; at the CLI the same validation
    # lives at parse (click.Path, exit 2 — see the smoke layer below).
    with pytest.raises(InstallError, match="is not a directory"):
        irec.gather(tmp_path / "nope", iunits.load_units(), irec.load_retired())


def test_unreadable_manifest_degrades_to_empty_pristine(tmp_path, rec):
    (tmp_path / ".shipit.toml").write_text("not [ valid toml")
    plan = _plan(tmp_path)
    # The reason rides the Plan for the renderer's warning...
    assert plan.manifest_error is not None
    assert "manifest" in verb.format_plan_warnings(plan)
    # ...and the reconcile proceeds against an empty pristine map.
    assert plan.writes


# --------------------------------------------------------------------------
# Seed-if-absent consumer policy — App [secrets] mappings + [reviewers] set
# --------------------------------------------------------------------------


def _secrets_by_name(root):
    cfg = config.load(root / ".shipit.toml")
    return {s.name: s for s in config.load_secrets(cfg)}


def test_fresh_install_seeds_app_secret_mappings(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    plan = _plan(tmp_path)
    # The seeds ride the Plan as typed entries...
    assert "[secrets].CODEX_REVIEW_APP_PRIVATE_KEY" in plan.seeds
    _apply(tmp_path, iapply.MODE_PR)

    secrets = _secrets_by_name(tmp_path)
    for name in (
        "CODEX_REVIEW_APP_PRIVATE_KEY",
        "CODEX_REVIEW_APP_ID",
        "AGY_REVIEW_APP_PRIVATE_KEY",
        "AGY_REVIEW_APP_ID",
    ):
        assert name in secrets
        # Each maps to its like-named Doppler key (matches shipit's own .shipit.toml).
        assert secrets[name].kind == "doppler"
        assert secrets[name].key == name
    # The PR body announces the seed under its own section.
    assert "### Policy seeded" in rec.pr_body
    assert "[secrets].CODEX_REVIEW_APP_PRIVATE_KEY" in rec.pr_body


def test_fresh_install_seeds_required_reviewer_set(tmp_path, rec):
    from shipit.prstate import reviewers_config as rcfg

    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    _apply(tmp_path)

    # The seeded [reviewers] table is rendered from the SINGLE required-reviewer
    # default (ADR-0025 / COR01-WS02), so a fresh install requires exactly what the
    # engine code-default does — Copilot only. codex/agy are opt-in per repo (their
    # review Apps are not installed everywhere); shipit's own .shipit.toml opts them in.
    assert rcfg.load_roster(str(tmp_path)).required_names == ("copilot",)


def test_install_preserves_existing_secrets_and_reviewers(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    (tmp_path / ".shipit.toml").write_text(
        "[secrets]\n"
        'MY_TOKEN = { env = "MY_TOKEN" }\n'
        # A consumer who deliberately points one App secret at a custom key must
        # NOT be clobbered by the seed.
        'CODEX_REVIEW_APP_ID = { doppler = "CUSTOM_KEY" }\n'
        "\n[reviewers]\n"
        "copilot = { rerun = true }\n"
    )
    _apply(tmp_path)

    secrets = _secrets_by_name(tmp_path)
    # Consumer entries are left exactly as written.
    assert secrets["MY_TOKEN"].kind == "env"
    assert secrets["CODEX_REVIEW_APP_ID"].key == "CUSTOM_KEY"
    # The absent App mappings are merged in alongside them.
    assert "CODEX_REVIEW_APP_PRIVATE_KEY" in secrets
    assert "AGY_REVIEW_APP_PRIVATE_KEY" in secrets
    assert "AGY_REVIEW_APP_ID" in secrets
    # The pre-existing [reviewers] table is untouched — not overwritten by the scaffold.
    cfg = config.load(tmp_path / ".shipit.toml")
    assert cfg["reviewers"] == {"copilot": {"rerun": True}}


def test_reinstall_does_not_reseed_policy(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    _apply(tmp_path)
    before = (tmp_path / ".shipit.toml").read_text()

    rec.calls.clear()
    plan = _plan(tmp_path)
    # Clean no-op: no seeds decided, nothing to apply, policy text untouched.
    assert plan.seeds == ()
    assert plan.nothing_to_do
    assert rec.calls == []
    assert (tmp_path / ".shipit.toml").read_text() == before


def test_install_reseeds_policy_when_missing_even_if_managed_current(tmp_path, rec):
    # Simulate an older install (or a consumer who dropped the policy tables): the
    # managed set is fully current but `[secrets]`/`[reviewers]` are absent.
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    _apply(tmp_path)
    cfg_path = tmp_path / ".shipit.toml"
    managed = config.load_managed(config.load(cfg_path))
    cfg_path.write_text(config.dump_manifest("testhash", managed))  # policy stripped

    rec.calls.clear()
    plan = _plan(tmp_path)
    # A seed-only change still counts as work (managed set NOOP, policy seeded)...
    assert not plan.writes and plan.seeds
    assert not plan.nothing_to_do
    result = _apply(tmp_path, iapply.MODE_PR)
    assert ("pr_create", True) in rec.calls
    assert "### Policy seeded" in rec.pr_body
    # ...but it does NOT claim to (re)activate the checks — no managed unit was
    # written, so the typed result records no activation at all.
    assert result.hooks_activated is None
    assert "### Checks activated locally" not in rec.pr_body
    # ...and the policy is back in place.
    secrets = _secrets_by_name(tmp_path)
    assert "CODEX_REVIEW_APP_PRIVATE_KEY" in secrets
    assert "reviewers" in config.load(cfg_path)


def test_dry_run_does_not_seed_policy(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    rc = verb.run(str(tmp_path), dry_run=True)
    assert rc == 0
    # No file written on a dry-run, so nothing is seeded.
    assert not (tmp_path / ".shipit.toml").exists()


# --------------------------------------------------------------------------
# Checks activation — the lefthook.yml caller is turned LIVE, not just written
# --------------------------------------------------------------------------


def test_activates_hooks_is_true_iff_lefthook_is_managed():
    units = iunits.load_units()
    decisions = irec.plan(units, {}, {})
    assert irec.activates_hooks(decisions) is True

    # A set with no lefthook unit does not activate.
    others = [d for d in decisions if d.unit.key != iunits.LEFTHOOK_FILE]
    assert irec.activates_hooks(others) is False


def test_fresh_install_activates_the_check_hooks(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    result = _apply(tmp_path, iapply.MODE_PR)
    # The lefthook boundary was invoked exactly once, on the consumer root,
    # and the typed result records the live outcome.
    assert result.hooks_activated is True
    assert len(rec.hook_activations) == 1
    assert rec.hook_activations[0] == tmp_path.resolve()
    # The PR body announces the checks are live (a descriptive mention that
    # `lefthook install` ran is fine)...
    assert "### Checks activated" in rec.pr_body
    assert "lefthook install" in rec.pr_body
    # ...but the reviewers/mergers recovery INSTRUCTION speaks shipit, not the
    # internal lefthook/pixi layer.
    assert "run `./bin/shipit install` on your own checkout" in rec.pr_body


def test_break_glass_push_also_activates_hooks(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    result = _apply(tmp_path, iapply.MODE_PUSH)
    assert result.hooks_activated is True
    assert len(rec.hook_activations) == 1


def test_reinstall_with_writes_reactivates_idempotently(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    _apply(tmp_path)
    assert len(rec.hook_activations) == 1
    # A consumer edit forces a writing re-install; activation re-runs (safe
    # because `lefthook install` is idempotent — we never hand-roll a hook).
    (tmp_path / "lefthook.yml").write_text("CONSUMER EDIT\n")
    rec.calls.clear()
    _apply(tmp_path)
    assert len(rec.hook_activations) == 2


def test_install_degrades_but_succeeds_when_activation_fails(tmp_path, rec):
    # The boundary reports a failed activation (nonzero rc); apply must still
    # finish its PR rather than aborting — activation is opportunistic, not a
    # hard-fail check.
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    result = _apply(
        tmp_path,
        iapply.MODE_PR,
        activate_hooks=lambda root: _exec_result(1, stderr="lefthook: broken config"),
    )
    assert ("pr_create", True) in rec.calls
    # The typed result records the degraded outcome + its detail...
    assert result.hooks_activated is False
    assert "lefthook: broken config" in result.hooks_detail
    # ...the renderer's stderr warning derives from it...
    assert "could not activate git hooks" in verb.format_result_warnings(result)
    # ...and the PR body must NOT claim the checks went live; it records that
    # local activation was deferred so a merger knows to act.
    assert "### Checks activated locally" not in rec.pr_body
    assert "local activation skipped" in rec.pr_body
    # Descriptive "`lefthook install` did not run here" is fine; the post-merge
    # recovery INSTRUCTION is the shipit-level command.
    assert "lefthook install" in rec.pr_body
    assert "run `./bin/shipit install`" in rec.pr_body


def test_install_degrades_but_succeeds_when_lefthook_missing(tmp_path, rec):
    # A missing activation runtime (now `pixi`, since activation routes through
    # the consumer lint env — #478) surfaces as the runner's ExecError
    # (ADR-0028); apply must degrade — pointing at the canonical recovery — and
    # still finish its PR rather than aborting (fail-open, #491's sibling theme).
    def boom(root):
        raise execrun.ExecError(
            ["lefthook", "install"], rc=None, cause=execrun.CAUSE_MISSING_BINARY
        )

    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    result = _apply(tmp_path, iapply.MODE_PR, activate_hooks=boom)
    assert ("pr_create", True) in rec.calls
    assert result.hooks_activated is False
    assert "### Checks activated locally" not in rec.pr_body
    assert "local activation skipped" in rec.pr_body
    # Names the actually-failing runtime (pixi, since activation runs argv[0]=pixi)
    # but the RECOVERY the operator runs is the ONE shipit-level command — never a
    # leaked lefthook/pixi command (activation is a side effect of `shipit install`;
    # there is no standalone hook-activation verb).
    warning = verb.format_result_warnings(result)
    assert "could not activate git hooks" in warning
    assert "pixi not found on PATH" in warning
    assert "`./bin/shipit install` to activate the checks" in warning
    assert "lefthook install" not in warning


def test_install_activation_timeout_does_not_claim_missing_binary(tmp_path, rec):
    # A NON-missing-binary transport failure (e.g. a timeout) must not be
    # mislabelled "not found on PATH": the detail branches on exc.cause, stays
    # binary-neutral, and still ends in the ONE shipit-level recovery command.
    def boom(root):
        raise execrun.ExecError(
            ["lefthook", "install"], rc=None, cause=execrun.CAUSE_TIMEOUT
        )

    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    result = _apply(tmp_path, iapply.MODE_PR, activate_hooks=boom)
    assert ("pr_create", True) in rec.calls
    warning = verb.format_result_warnings(result)
    assert "could not activate git hooks" in warning
    assert "not found on PATH" not in warning
    # Binary-neutral label (not "lefthook: could not run" — the failing runtime
    # is pixi); the echoed exc diagnostic may name the failed argv, but the
    # RECOVERY the operator runs is the shipit-level command.
    assert "activation could not run" in warning
    assert "`./bin/shipit install` to activate the checks" in warning


def test_activate_hooks_boundary_runs_lefthook_through_consumer_lint_env(
    tmp_path, monkeypatch
):
    # #478: the real boundary hands `lefthook install` to the one Exec runner
    # THROUGH the consumer's OWN pixi lint env — `pixi run --manifest-path
    # <root>/pixi.toml --environment lint -- lefthook install` — so the lefthook
    # that runs `install` (and whose absolute path lefthook bakes into the
    # generated .git/hooks shim) is the consumer's own env's, never the
    # installer's (possibly an ephemeral shipit Tree's). check=False — a nonzero
    # rc degrades, never a raised ExecError; never a re-implemented hook writer.
    captured = {}

    def fake_run(argv, *, cwd=None, check=True, **kw):
        captured["argv"] = argv
        captured["cwd"] = cwd
        captured["check"] = check
        captured["timeout"] = kw.get("timeout")
        return execrun.ExecResult(
            argv=tuple(argv),
            rc=0,
            stdout="sync hooks: ✔️ pre-commit, ✔️ pre-push\n",
            stderr="",
            duration_ms=1,
        )

    monkeypatch.setattr(iapply.execrun, "run", fake_run)
    result = iapply._activate_hooks(tmp_path)
    assert result.ok
    # Routed through the consumer's own lint env, with `--manifest-path` pinning
    # resolution to the consumer's manifest (never a leaked PIXI_PROJECT_MANIFEST).
    assert captured["argv"] == pixienv.run_argv(
        ["lefthook", "install"], tmp_path, environment=iunits.LINT_ENV
    )
    assert captured["cwd"] == str(tmp_path)
    assert captured["check"] is False
    # The adapter's long-runner bound rides the wire (ADR-0028): the worst case
    # is a first `pixi run -e lint` solving the lint env — provisioning-shaped
    # work, not the runner's implicit 5-minute default.
    assert captured["timeout"] == pixienv.INSTALL_TIMEOUT
    assert "pre-commit" in iapply._activation_output(result)


def test_activate_hooks_boundary_missing_binary_is_exec_error(tmp_path, monkeypatch):
    # A missing runtime (now `pixi`, since activation routes through the consumer
    # lint env — #478) surfaces as the runner's single transport error, tagged
    # missing-binary — never a raw FileNotFoundError, never swallowed. check=False
    # suppresses only a nonzero rc, never a launch failure; `_activate` upstream
    # is what absorbs this into a degraded (fail-open) outcome.
    def boom(argv, **kw):
        raise execrun.ExecError(argv, rc=None, cause=execrun.CAUSE_MISSING_BINARY)

    monkeypatch.setattr(iapply.execrun, "run", boom)
    with pytest.raises(execrun.ExecError) as exc_info:
        iapply._activate_hooks(tmp_path)
    assert exc_info.value.cause == execrun.CAUSE_MISSING_BINARY


def test_activation_output_joins_streams_with_newline(tmp_path):
    # Join with a newline so a stdout without a trailing newline does not run
    # straight into stderr (e.g. `donefatal: ...`) in the warning we print.
    out = iapply._activation_output(
        _exec_result(1, stdout="done", stderr="fatal: broken")
    )
    assert out == "done\nfatal: broken"


# --------------------------------------------------------------------------
# Retired files (docs/legacy-prd/rvw01-sole-requester.md, ADR-0031)
# --------------------------------------------------------------------------

# A pristine copy of the retired Copilot caller workflow, snapshotted before
# the epic deletes it from this repo — the e2e tests below plant it into a
# consumer, and its hash pins the packaged manifest to real historical content.
PRISTINE_WORKFLOW = Path(__file__).parent / "data" / "copilot-review-pristine.yml"
RETIRED_WORKFLOW_PATH = ".github/workflows/copilot-review.yml"
PRISTINE_TO_PRD_SKILL = """---
name: to-prd
description: Turn the current conversation context into a PRD — the authoritative feature spec — and write it to docs/prd/. Use when user wants to create a PRD from the current context.
metadata:
    forked-from: https://github.com/mattpocock/skills (skills/engineering/to-prd)
---
This skill takes the current conversation context and codebase understanding and produces a PRD. Do NOT re-run the requirements interview — that happened earlier, in `/grill-me-with-docs`; synthesize the PRD from what you already know. This is not a fully AFK skill, though: step 2 still expects a short confirmation of the module boundaries and test scope with the user. That scoped confirmation is not a requirements interview.

The issue tracker and triage label vocabulary should have been provided to you — run `/setup-matt-pocock-skills` if not.

## Process

1. Explore the repo to understand the current state of the codebase, if you haven't already. Use the project's domain glossary vocabulary (`CONTEXT.md`) throughout the PRD, and respect any ADRs in the area you're touching.

2. Sketch out the major modules you will need to build or modify to complete the implementation. Actively look for opportunities to extract deep modules that can be tested in isolation.

A deep module (as opposed to a shallow module) is one which encapsulates a lot of functionality in a simple, testable interface which rarely changes.

Check with the user that these modules match their expectations. Check with the user which modules they want tests written for.

3. Write the PRD using the template below. **The PRD is the authoritative feature definition / spec** — the *what & why*. It is a file, not an issue body:

   - Write it to `docs/prd/<slug>.md`. This file is the single source of truth for the spec.
   - That is the whole output of this skill. Do NOT open an epic tracker issue here. The **epic GitHub issue is an execution tracker** — it summarizes the PRD and points to it plus the relevant ADRs — and it is created later, in `/to-tickets` (the issue-planning leg), not by this skill.
   - The epic code (`THEME+NN`, e.g. `GPU02`) is assigned by the human, but it is used later in `/to-tickets` when the epic issue is minted — not here.

4. Once the PRD file is written, record the milestone in the dev-cycle log (best-effort — ADR-0032; if the command errors, continue — a skipped emission is a missing event, never a broken step):

   ```sh
   shipit log event planning.prd.written --about "PRD: docs/prd/<slug>.md"
   ```

<prd-template>

## Problem Statement

The problem that the user is facing, from the user's perspective.

## Solution

The solution to the problem, from the user's perspective.

## User Stories

A LONG, numbered list of user stories. Each user story should be in the format of:

1. As an <actor>, I want a <feature>, so that <benefit>

<user-story-example>
1. As a mobile bank customer, I want to see balance on my accounts, so that I can make better informed decisions about my spending
</user-story-example>

This list of user stories should be extremely extensive and cover all aspects of the feature.

## Implementation Decisions

A list of implementation decisions that were made. This can include:

- The modules that will be built/modified
- The interfaces of those modules that will be modified
- Technical clarifications from the developer
- Architectural decisions
- Schema changes
- API contracts
- Specific interactions

Do NOT include specific file paths or code snippets. They may end up being outdated very quickly.

Exception: if a prototype produced a snippet that encodes a decision more precisely than prose can (state machine, reducer, schema, type shape), inline it within the relevant decision and note briefly that it came from a prototype. Trim to the decision-rich parts — not a working demo, just the important bits.

## Testing Decisions

A list of testing decisions that were made. Include:

- A description of what makes a good test (only test external behavior, not implementation details)
- Which modules will be tested
- Prior art for the tests (i.e. similar types of tests in the codebase)

## Out of Scope

A description of the things that are out of scope for this PRD.

## Further Notes

Any further notes about the feature.

</prd-template>
"""
RETIRED_SKILL_HASHES = {
    "skills/shipit-planning/SKILL.md": (
        "sha256:a16ac4744238b3a5b59da8a887bb6268742fd01a8a285797e0198aba49e44336",
    ),
    "skills/shipit-grill-with-docs/SKILL.md": (
        "sha256:47c25fe56510de6a63da1de9121ef9b6704808f3631d43c7f9ee745f2c32ff62",
    ),
    "skills/shipit-grill-with-docs/ADR-FORMAT.md": (
        "sha256:f1f36cd3f8d3b6474ddd5855da4e233bfc4ae1a1c5024909ccf11871819a41b2",
    ),
    "skills/shipit-grill-with-docs/CONTEXT-FORMAT.md": (
        "sha256:886ce0e96fd0f76f4c72c337c049cf4655227c599862ce920a62297e0929beae",
    ),
    "skills/shipit-to-prd/SKILL.md": (
        "sha256:0f13f20cad06161baff87628ea6b1cf5bac0cc7919beb6176535f9cdf9ae42d8",
        "sha256:4bdf82e153221545c8340744a5def096316c0cf88f0db9548a373bce6f91d0c1",
    ),
    "skills/to-prd/SKILL.md": (
        "sha256:3b1fc2aa002d78a63f9bd858144be177a1b2f69a2ca97e2ca165bc86f6ca5a2e",
    ),
    "skills/shipit-to-issues/SKILL.md": (
        "sha256:4df3706b12c89fb7d844521800addea1c9ab9f448cd7f926b993a5d92f46869b",
        "sha256:e623a477ad4d81c042b5bbc20fead9cd208b1c73cb35d2954b1fdcd7303d9474",
    ),
}


def test_decide_retired_covers_the_matrix():
    # absent -> no-op
    assert (
        irec.decide_retired(actual_hash=None, pristine_hashes=("a", "b")) == irec.NOOP
    )
    # pristine match -> delete
    assert irec.decide_retired(actual_hash="a", pristine_hashes=("a",)) == irec.DELETE
    # any of several known historical versions -> delete
    assert (
        irec.decide_retired(actual_hash="b", pristine_hashes=("a", "b", "c"))
        == irec.DELETE
    )
    # modified content (matches NO known version) -> warn-and-keep
    assert irec.decide_retired(actual_hash="x", pristine_hashes=("a", "b")) == irec.KEEP
    # present but the manifest knows no versions at all -> keep (never guess)
    assert irec.decide_retired(actual_hash="x", pristine_hashes=()) == irec.KEEP


def test_plan_retired_decides_every_manifest_entry():
    entries = [
        irec.RetiredFile(path="a.yml", pristine_hashes=("h1",)),
        irec.RetiredFile(path="b.yml", pristine_hashes=("h2",)),
        irec.RetiredFile(path="c.yml", pristine_hashes=("h3",)),
    ]
    decisions = irec.plan_retired(
        entries, {"a.yml": "h1", "b.yml": "edited", "c.yml": None}
    )
    assert [d.action for d in decisions] == [
        irec.DELETE,
        irec.KEEP,
        irec.NOOP,
    ]


@pytest.mark.parametrize(
    "bad",
    [
        "/etc/passwd",
        "C:\\Windows\\system32\\config",
        "C:tmp\\x.yml",
        "\\outside.yml",
        "../outside.yml",
        "nested/../../outside.yml",
        "nested\\..\\..\\outside.yml",
        "",
    ],
)
def test_retired_path_rejects_unsafe_manifest_entries(bad):
    # Every manifest entry names a file the IO pass will unlink, so a path
    # that could escape the consumer root fails the load closed.
    with pytest.raises(ValueError, match="unsafe path"):
        irec._retired_path(bad)


def test_retired_path_accepts_a_plain_relative_path():
    assert irec._retired_path(".github/workflows/x.yml") == ".github/workflows/x.yml"


def test_retired_manifest_carries_the_copilot_workflow_history():
    # The packaged manifest's first entry is the Copilot caller workflow, with
    # its known pristine hashes from this repo's git history — including the
    # last-shipped version the fixture snapshots.
    retired = irec.load_retired()
    entry = next(r for r in retired if r.path == RETIRED_WORKFLOW_PATH)
    assert all(h.startswith("sha256:") for h in entry.pristine_hashes)
    fixture_hash = config.content_hash(PRISTINE_WORKFLOW.read_bytes())
    assert fixture_hash in entry.pristine_hashes


def test_retired_manifest_carries_the_renamed_skill_history():
    retired = {r.path: r for r in irec.load_retired()}

    for path, expected_hashes in RETIRED_SKILL_HASHES.items():
        assert retired[path].pristine_hashes == expected_hashes


# The agent-specific launcher shims (#815): repo-root whole-file units shipit
# used to distribute, retired now that all launch logic lives in `agent-start`.
# The fixtures snapshot the last-shipped pristine bytes so the upgrade path —
# an install shedding a stale shim while keeping a locally edited one — is
# covered end-to-end.
PRISTINE_CLAUDE_START = Path(__file__).parent / "data" / "claude-start-pristine"
PRISTINE_CODEX_START = Path(__file__).parent / "data" / "codex-start-pristine"
RETIRED_LAUNCHER_SHIMS = {
    "claude-start": PRISTINE_CLAUDE_START,
    "codex-start": PRISTINE_CODEX_START,
}


def test_retired_manifest_carries_the_launcher_shim_history():
    # Both agent-specific shims are retired, each with its last-shipped pristine
    # hash from this repo's git history (the fixtures snapshot those bytes).
    retired = {r.path: r for r in irec.load_retired()}
    for path, fixture in RETIRED_LAUNCHER_SHIMS.items():
        entry = retired[path]
        assert all(h.startswith("sha256:") for h in entry.pristine_hashes)
        assert config.content_hash(fixture.read_bytes()) in entry.pristine_hashes


@pytest.mark.parametrize("path", sorted(RETIRED_LAUNCHER_SHIMS))
def test_install_deletes_a_pristine_retired_launcher_shim(tmp_path, rec, path):
    # A consumer that already installed the shim sheds it on upgrade, while the
    # surviving generic `agent-start` launcher is (re)installed.
    victim = tmp_path / path
    victim.write_bytes(RETIRED_LAUNCHER_SHIMS[path].read_bytes())

    plan = _plan(tmp_path)
    assert path in [d.retired.path for d in plan.retire_deletes]
    _apply(tmp_path)
    assert not victim.exists()
    assert (tmp_path / iunits.AGENT_LAUNCHER_FILE).is_file()


@pytest.mark.parametrize("path", sorted(RETIRED_LAUNCHER_SHIMS))
def test_install_keeps_a_modified_retired_launcher_shim(tmp_path, rec, path):
    # A locally edited shim is never destroyed: kept on disk, warned loudly.
    victim = tmp_path / path
    victim.write_bytes(RETIRED_LAUNCHER_SHIMS[path].read_bytes() + b"# local\n")

    plan = _plan(tmp_path)
    assert [d.retired.path for d in plan.retire_keeps] == [path]
    _apply(tmp_path)
    assert victim.exists()


def test_install_deletes_a_pristine_retired_file(tmp_path, rec):
    # End-to-end: a checkout that still has a pristine copy of the retired
    # workflow sheds it on install, and the Plan/report both carry the outcome.
    victim = tmp_path / RETIRED_WORKFLOW_PATH
    victim.parent.mkdir(parents=True)
    victim.write_bytes(PRISTINE_WORKFLOW.read_bytes())

    plan = _plan(tmp_path)
    assert [d.retired.path for d in plan.retire_deletes] == [RETIRED_WORKFLOW_PATH]
    assert f"delete   {RETIRED_WORKFLOW_PATH} (retired)" in verb.format_plan(plan)
    _apply(tmp_path)
    assert not victim.exists()


def test_install_deletes_a_pristine_retired_skill_file(tmp_path, rec):
    # A consumer upgrading across the skill rename sheds the old path when its
    # content is a known pristine copy, while the new skill path is installed.
    retired_path = "skills/shipit-grill-with-docs/ADR-FORMAT.md"
    source = REPO_ROOT / "skills/grill-me-with-docs/ADR-FORMAT.md"
    assert (
        config.content_hash(source.read_bytes()) in RETIRED_SKILL_HASHES[retired_path]
    )
    victim = tmp_path / retired_path
    victim.parent.mkdir(parents=True)
    victim.write_bytes(source.read_bytes())

    plan = _plan(tmp_path)
    assert retired_path in [d.retired.path for d in plan.retire_deletes]
    _apply(tmp_path)
    assert not victim.exists()
    assert (tmp_path / "skills/grill-me-with-docs/ADR-FORMAT.md").is_file()


def test_install_deletes_a_pristine_retired_to_prd_skill_and_installs_to_spec(
    tmp_path, rec
):
    # The managed `/to-prd` path was renamed to `/to-spec`; an upgraded
    # consumer with a pristine retired copy must not keep both runnable skills
    # after install.
    retired_path = "skills/to-prd/SKILL.md"
    old_bytes = PRISTINE_TO_PRD_SKILL.encode()
    assert config.content_hash(old_bytes) in RETIRED_SKILL_HASHES[retired_path]
    victim = tmp_path / retired_path
    victim.parent.mkdir(parents=True)
    victim.write_bytes(old_bytes)

    plan = _plan(tmp_path)
    assert retired_path in [d.retired.path for d in plan.retire_deletes]
    _apply(tmp_path)
    assert not victim.exists()
    assert (tmp_path / "skills/to-spec/SKILL.md").is_file()


def test_install_keeps_a_modified_retired_file_with_warning(tmp_path, rec):
    # A locally modified copy is NEVER destroyed: kept on disk, warned loudly.
    victim = tmp_path / RETIRED_WORKFLOW_PATH
    victim.parent.mkdir(parents=True)
    victim.write_text(PRISTINE_WORKFLOW.read_text() + "# local tweak\n")

    plan = _plan(tmp_path)
    assert [d.retired.path for d in plan.retire_keeps] == [RETIRED_WORKFLOW_PATH]
    assert f"keep     {RETIRED_WORKFLOW_PATH} (retired; locally modified)" in (
        verb.format_plan(plan)
    )
    assert f"retired file kept: {RETIRED_WORKFLOW_PATH}" in (
        verb.format_plan_warnings(plan)
    )
    _apply(tmp_path)
    assert victim.is_file()
    assert "# local tweak" in victim.read_text()


def test_install_keeps_a_symlink_at_a_retired_path(tmp_path, rec):
    # `is_file()` follows symlinks: a link whose TARGET carries pristine
    # content must not be deleted — the link is not shipit's output. It is
    # kept and warned like any locally modified copy.
    target = tmp_path / "elsewhere.yml"
    target.write_bytes(PRISTINE_WORKFLOW.read_bytes())
    victim = tmp_path / RETIRED_WORKFLOW_PATH
    victim.parent.mkdir(parents=True)
    victim.symlink_to(target)

    plan = _plan(tmp_path)
    assert [d.retired.path for d in plan.retire_keeps] == [RETIRED_WORKFLOW_PATH]
    _apply(tmp_path)
    assert victim.is_symlink()
    assert f"keep     {RETIRED_WORKFLOW_PATH} (retired; locally modified)" in (
        verb.format_plan(plan)
    )


def test_retired_delete_alone_is_still_a_write(tmp_path, rec):
    # A consumer whose managed set is fully current still sheds a pristine
    # retired file on re-install — the cleanup is not gated on managed drift.
    _apply(tmp_path)
    victim = tmp_path / RETIRED_WORKFLOW_PATH
    victim.parent.mkdir(parents=True)
    victim.write_bytes(PRISTINE_WORKFLOW.read_bytes())

    plan = _plan(tmp_path)
    assert not plan.writes and plan.retire_deletes
    assert not plan.nothing_to_do
    assert "nothing to do" not in verb.format_plan(plan)
    _apply(tmp_path)
    assert not victim.exists()

    # And once gone, a further re-install is back to a clean no-op (absent -> no-op).
    again = _plan(tmp_path)
    assert again.nothing_to_do
    assert "nothing to do" in verb.format_plan(again)


def test_kept_retired_file_changes_the_nothing_to_do_wording(tmp_path, rec):
    # Managed set current + a kept (locally modified) retired file: the loud
    # keep warning must not be followed by "managed set is current", which
    # would read as a contradiction.
    _apply(tmp_path)
    victim = tmp_path / RETIRED_WORKFLOW_PATH
    victim.parent.mkdir(parents=True)
    victim.write_text(PRISTINE_WORKFLOW.read_text() + "# local tweak\n")

    plan = _plan(tmp_path)
    assert plan.nothing_to_do and plan.retire_keeps
    report = verb.format_plan(plan)
    assert "nothing to do — no automated changes to apply." in report
    assert "managed set is current" not in report
    assert victim.is_file()


def test_dry_run_reports_but_keeps_a_pristine_retired_file(tmp_path, rec):
    victim = tmp_path / RETIRED_WORKFLOW_PATH
    victim.parent.mkdir(parents=True)
    victim.write_bytes(PRISTINE_WORKFLOW.read_bytes())

    plan = _plan(tmp_path)
    report = verb.format_plan(plan, dry_run=True)
    assert f"delete   {RETIRED_WORKFLOW_PATH} (retired)" in report
    assert "1 retired delete(s)" in report
    # Through the verb, dry-run touches nothing: no delete, no git, no PR.
    rc = verb.run(str(tmp_path), dry_run=True)
    assert rc == 0
    assert victim.is_file()  # nothing deleted
    assert rec.calls == []


def test_pr_install_commits_the_retired_deletion_and_reports_it(tmp_path, rec):
    victim = tmp_path / RETIRED_WORKFLOW_PATH
    victim.parent.mkdir(parents=True)
    victim.write_bytes(PRISTINE_WORKFLOW.read_bytes())

    plan = _plan(tmp_path)
    # The deleted path joins the typed commit set, so every mode carries it.
    assert RETIRED_WORKFLOW_PATH in plan.changed_paths
    _apply(tmp_path, iapply.MODE_PR)
    assert not victim.exists()
    # The deleted path is staged with the rest of the set, so the PR carries it.
    added = next(paths for name, paths in rec.calls if name == "add")
    assert RETIRED_WORKFLOW_PATH in added
    assert "### Retired files removed" in rec.pr_body
    assert RETIRED_WORKFLOW_PATH in rec.pr_body


# --------------------------------------------------------------------------
# Retired hook entries (#619)
# --------------------------------------------------------------------------

# The two legacy consumer-local SessionStart entries the manifest retires, as
# they actually appear in the fleet (the ADR-0003 release-core boot resolver;
# the pre-managed setup-dev-env duplicate the TOL01 sweep missed in
# simple-gal-ui).
LEGACY_RELEASE_CORE_ENTRY = {
    "matcher": "startup|resume",
    "hooks": [
        {
            "type": "command",
            "command": '"$CLAUDE_PROJECT_DIR"/bin/install-release-core',
        }
    ],
}
LEGACY_SETUP_DEV_ENV_ENTRY = {
    "matcher": "startup|resume",
    "hooks": [
        {
            "type": "command",
            "command": '"$CLAUDE_PROJECT_DIR"/bin/setup-dev-env.sh',
        }
    ],
}
RETIRED_RELEASE_CORE_KEY = (
    ".claude/settings.json#SessionStart[bin/install-release-core]"
)


def _managed_sessionstart_entry() -> dict:
    """The packaged managed SessionStart entry — the one the pass must protect."""
    return json.loads(iunits.data_bytes("claude-settings-sessionstart.json"))


def test_is_retired_hook_matches_marker_but_protects_managed_entries():
    assert splice.is_retired_hook(LEGACY_RELEASE_CORE_ENTRY, "bin/install-release-core")
    assert splice.is_retired_hook(LEGACY_SETUP_DEV_ENV_ENTRY, "bin/setup-dev-env.sh")
    # The managed SessionStart command itself runs ./bin/setup-dev-env.sh
    # inline — the protection predicate (its `shipit hook` marker) must keep
    # the retirement marker from matching shipit's own entry.
    managed = _managed_sessionstart_entry()
    assert splice.is_shipit_hook(managed, "bin/setup-dev-env.sh")
    assert not splice.is_retired_hook(managed, "bin/setup-dev-env.sh")
    # Garbage entries answer False, never raise (the is_shipit_hook walk).
    assert not splice.is_retired_hook(None, "bin/install-release-core")
    assert not splice.is_retired_hook({"hooks": None}, "bin/install-release-core")


def test_decide_retired_hook_covers_both_cases():
    assert irec.decide_retired_hook(count=0) == irec.NOOP
    assert irec.decide_retired_hook(count=1) == irec.DELETE
    assert irec.decide_retired_hook(count=3) == irec.DELETE


def _settings_with_legacy_and_managed() -> str:
    return json.dumps(
        {
            "permissions": {"allow": ["Bash(ls:*)"]},
            "hooks": {
                "SessionStart": [
                    LEGACY_RELEASE_CORE_ENTRY,
                    _managed_sessionstart_entry(),
                ],
                "Stop": [{"hooks": [{"type": "command", "command": "echo hi"}]}],
            },
        },
        indent=2,
    )


def test_count_and_remove_retired_hooks_own_only_the_matched_entries():
    text = _settings_with_legacy_and_managed()
    assert (
        splice.count_retired_hooks(text, "SessionStart", "bin/install-release-core")
        == 1
    )
    out = splice.remove_retired_hooks(text, "SessionStart", "bin/install-release-core")
    data = json.loads(out)
    # The legacy entry is gone; the managed entry and every other consumer key
    # (permissions, the Stop hook) merge through untouched.
    commands = [h["command"] for e in data["hooks"]["SessionStart"] for h in e["hooks"]]
    assert commands == [_managed_sessionstart_entry()["hooks"][0]["command"]]
    assert data["permissions"] == {"allow": ["Bash(ls:*)"]}
    assert data["hooks"]["Stop"] == [
        {"hooks": [{"type": "command", "command": "echo hi"}]}
    ]


def test_remove_retired_hooks_drops_an_emptied_event_array():
    text = json.dumps(
        {"hooks": {"SessionStart": [LEGACY_RELEASE_CORE_ENTRY]}, "other": True}
    )
    out = splice.remove_retired_hooks(text, "SessionStart", "bin/install-release-core")
    data = json.loads(out)
    # No empty-array litter: the emptied event key (and the emptied hooks
    # object with it) is dropped; the consumer's other keys survive.
    assert "hooks" not in data
    assert data["other"] is True


@pytest.mark.parametrize(
    "text",
    [
        "{not json",  # malformed
        '["a", "b"]',  # valid JSON, not an object
        json.dumps({"hooks": {"SessionStart": "not-a-list"}}),  # foreign shape
        json.dumps({"hooks": {"Stop": [LEGACY_RELEASE_CORE_ENTRY]}}),  # other event
        "",  # empty
    ],
)
def test_remove_retired_hooks_returns_untouchable_files_verbatim(text):
    # Fail-safe in lockstep with the count: whatever the write would preserve
    # verbatim, the read counts 0 for — the pass never decides work the write
    # cannot safely do, and a file with nothing to remove is never reformatted.
    assert (
        splice.count_retired_hooks(text, "SessionStart", "bin/install-release-core")
        == 0
    )
    assert (
        splice.remove_retired_hooks(text, "SessionStart", "bin/install-release-core")
        == text
    )


def test_retired_hooks_manifest_carries_the_legacy_sessionstart_entries():
    hooks = irec.load_retired_hooks()
    assert [(h.file, h.event, h.marker) for h in hooks] == [
        (".claude/settings.json", "SessionStart", "bin/install-release-core"),
        (".claude/settings.json", "SessionStart", "bin/setup-dev-env.sh"),
    ]


def test_retired_manifest_carries_the_install_release_core_history():
    # The legacy release-core boot resolver script (ADR-0003) is a retired
    # FILE: every distinct historical version across the six carrying repos
    # rides the manifest, so any repo's copy gets the clean delete.
    retired = {r.path: r for r in irec.load_retired()}
    entry = retired["bin/install-release-core"]
    assert len(entry.pristine_hashes) == 8
    assert all(h.startswith("sha256:") for h in entry.pristine_hashes)


def test_install_removes_a_legacy_sessionstart_hook_entry(tmp_path, rec):
    # End-to-end: a consumer still carrying the legacy release-core resolver
    # hook sheds exactly that entry on install, while shipit's own managed
    # SessionStart entry (spliced by the same run) survives.
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(_settings_with_legacy_and_managed(), encoding="utf-8")

    plan = _plan(tmp_path)
    assert [d.retired.key for d in plan.retire_hook_deletes] == [
        RETIRED_RELEASE_CORE_KEY
    ]
    assert f"delete   {RETIRED_RELEASE_CORE_KEY} (retired hook entry)" in (
        verb.format_plan(plan)
    )
    _apply(tmp_path)
    data = json.loads(settings.read_text(encoding="utf-8"))
    commands = [h["command"] for e in data["hooks"]["SessionStart"] for h in e["hooks"]]
    assert not any("install-release-core" in c for c in commands)
    assert any("shipit hook sessionstart" in c for c in commands)
    # The consumer's unrelated settings survive the rewrite.
    assert data["permissions"] == {"allow": ["Bash(ls:*)"]}


def test_install_removes_the_duplicate_setup_dev_env_entry_only(tmp_path, rec):
    # The simple-gal-ui case: the consumer-local duplicate of the managed
    # hook's inline setup-dev-env run goes; the managed entry — whose command
    # ALSO carries bin/setup-dev-env.sh — is protected by its own marker.
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps({"hooks": {"SessionStart": [LEGACY_SETUP_DEV_ENV_ENTRY]}}),
        encoding="utf-8",
    )

    plan = _plan(tmp_path)
    assert [d.retired.marker for d in plan.retire_hook_deletes] == [
        "bin/setup-dev-env.sh"
    ]
    _apply(tmp_path)
    data = json.loads(settings.read_text(encoding="utf-8"))
    commands = [h["command"] for e in data["hooks"]["SessionStart"] for h in e["hooks"]]
    # Exactly the managed entry remains: the inline setup-dev-env run it
    # carries is shipit's own, not the retired duplicate.
    assert len(commands) == 1
    assert "shipit hook sessionstart" in commands[0]

    # And a re-install is back to a clean no-op — the managed entry's
    # setup-dev-env substring never re-triggers the retirement.
    again = _plan(tmp_path)
    assert not again.retire_hook_deletes
    assert again.nothing_to_do


def test_retired_hook_delete_alone_is_still_a_write(tmp_path, rec):
    # A consumer whose managed set is fully current still sheds the legacy
    # entry on re-install — cleanup is not gated on managed drift.
    _apply(tmp_path)
    settings = tmp_path / ".claude" / "settings.json"
    data = json.loads(settings.read_text(encoding="utf-8"))
    data["hooks"]["SessionStart"].insert(0, LEGACY_RELEASE_CORE_ENTRY)
    settings.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    plan = _plan(tmp_path)
    assert not plan.writes and plan.retire_hook_deletes
    assert not plan.nothing_to_do
    _apply(tmp_path)
    assert "install-release-core" not in settings.read_text(encoding="utf-8")

    again = _plan(tmp_path)
    assert again.nothing_to_do


def test_pr_install_commits_the_retired_hook_removal_and_reports_it(tmp_path, rec):
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps({"hooks": {"SessionStart": [LEGACY_RELEASE_CORE_ENTRY]}}),
        encoding="utf-8",
    )

    plan = _plan(tmp_path)
    # The rewritten hooks file joins the typed commit set (it is also a block
    # unit dest, so the write set already carries it — the union is stable).
    assert ".claude/settings.json" in plan.changed_paths
    _apply(tmp_path, iapply.MODE_PR)
    added = next(paths for name, paths in rec.calls if name == "add")
    assert ".claude/settings.json" in added
    assert "### Retired hook entries removed" in rec.pr_body
    assert RETIRED_RELEASE_CORE_KEY in rec.pr_body


def test_gather_counts_retired_hooks_fail_open_on_oserror(tmp_path, monkeypatch):
    # An unreadable hooks file degrades to "nothing to remove" with a warning,
    # never a crash — gather only inspects it.
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps({"hooks": {"SessionStart": [LEGACY_RELEASE_CORE_ENTRY]}}),
        encoding="utf-8",
    )
    hook = irec.load_retired_hooks()[0]
    assert irec.retired_hook_count(tmp_path, hook) == 1

    real_read = Path.read_text

    def boom(self, *a, **kw):
        if self.name == "settings.json":
            raise OSError("permission denied")
        return real_read(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", boom)
    assert irec.retired_hook_count(tmp_path, hook) == 0


def test_apply_fails_open_when_the_retired_hook_rewrite_cannot_be_written(
    tmp_path, rec, monkeypatch
):
    # The apply-side mirror of the gather fail-open (retired_hook_count): a
    # consumer hooks file that turns unwritable in the gather→apply window makes
    # install degrade to a logged warning instead of crashing. Install first so
    # the managed set is current — then the retire-hook pass is the SOLE writer
    # of settings.json (cf. test_retired_hook_delete_alone_is_still_a_write), so
    # a write failure isolates the guard rather than tripping write_unit.
    _apply(tmp_path)
    settings = tmp_path / ".claude" / "settings.json"
    data = json.loads(settings.read_text(encoding="utf-8"))
    data["hooks"]["SessionStart"].insert(0, LEGACY_RELEASE_CORE_ENTRY)
    settings.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    plan = _plan(tmp_path)
    assert not plan.writes and plan.retire_hook_deletes

    real_write = Path.write_text

    def boom(self, *a, **kw):
        if self.name == "settings.json":
            raise OSError("permission denied")
        return real_write(self, *a, **kw)

    monkeypatch.setattr(Path, "write_text", boom)

    # apply() must not raise: the unguarded rewrite would have crashed install.
    _apply(tmp_path)

    # Degrade, not clobber: the legacy entry the rewrite could not remove survives.
    assert "install-release-core" in settings.read_text(encoding="utf-8")


def test_pr_body_lists_a_kept_retired_file(tmp_path, rec):
    victim = tmp_path / RETIRED_WORKFLOW_PATH
    victim.parent.mkdir(parents=True)
    victim.write_text(PRISTINE_WORKFLOW.read_text() + "# local tweak\n")

    plan = _plan(tmp_path)
    assert RETIRED_WORKFLOW_PATH not in plan.changed_paths  # kept files never staged
    _apply(tmp_path, iapply.MODE_PR)
    assert victim.is_file()
    added = next(paths for name, paths in rec.calls if name == "add")
    assert RETIRED_WORKFLOW_PATH not in added
    assert "### Retired files kept — locally modified" in rec.pr_body
    assert RETIRED_WORKFLOW_PATH in rec.pr_body


# --------------------------------------------------------------------------
# Renderers — pure string functions over Plan / InstallResult
# --------------------------------------------------------------------------


def test_format_plan_reports_the_decided_actions(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    plan = _plan(tmp_path)
    report = verb.format_plan(plan)
    assert report.startswith(f"install: {tmp_path.resolve()}")
    assert "add      AGENTS.md" in report
    assert "seed     [reviewers]" in report
    assert "(dry-run)" not in report
    # The dry-run header + summary line render off the SAME plan.
    dry = verb.format_plan(plan, dry_run=True)
    assert "(dry-run)" in dry
    assert "— dry-run, nothing written" in dry
    assert f"{len(plan.writes)} to write" in dry


def test_format_plan_omits_noop_units(tmp_path, rec):
    _apply(tmp_path)
    plan = _plan(tmp_path)
    report = verb.format_plan(plan)
    # All units are NOOP now: none renders, only the nothing-to-do line.
    assert "add      " not in report
    assert "nothing to do — managed set is current." in report


def test_format_result_renders_the_mode_outcomes():
    plan = irec.Plan(root="/x", decisions=(), retired=(), seeds=())
    tree = iapply.InstallResult(plan=plan, mode=iapply.MODE_TREE)
    assert "refreshed the managed set in the working tree" in verb.format_result(tree)
    local = iapply.InstallResult(plan=plan, mode=iapply.MODE_LOCAL, branch="main")
    assert "committed to main (local-only --local)" in verb.format_result(local)
    push = iapply.InstallResult(plan=plan, mode=iapply.MODE_PUSH, branch="main")
    assert "pushed to main (break-glass --push)" in verb.format_result(push)
    opened = iapply.InstallResult(
        plan=plan, mode=iapply.MODE_PR, branch="shipit/install", pr_url="https://x/1"
    )
    assert "opened draft PR: https://x/1" in verb.format_result(opened)
    updated = iapply.InstallResult(
        plan=plan,
        mode=iapply.MODE_PR,
        branch="shipit/install",
        pr_url="https://x/1",
        pr_updated=True,
    )
    assert "updated draft PR: https://x/1" in verb.format_result(updated)
    # The activation line leads the outcome when the checks went live.
    live = iapply.InstallResult(plan=plan, mode=iapply.MODE_TREE, hooks_activated=True)
    assert verb.format_result(live).splitlines()[0] == (
        "  activated git hooks (lefthook install) — the checks are live"
    )


def test_format_pr_body_sections_render_from_the_plan(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    plan = _plan(tmp_path)
    body = verb.format_pr_body(plan, {}, True)
    assert body.startswith("`shipit install` reconciled the managed set.")
    assert "### Added" in body
    assert "### Policy seeded" in body
    assert "### Checks activated locally" in body
    # The degraded-activation body flips to the deferred wording.
    deferred = verb.format_pr_body(plan, {}, False)
    assert "### Checks configured — local activation skipped" in deferred
    # No activation attempted -> neither section renders.
    silent = verb.format_pr_body(plan, {}, None)
    assert "Checks activated" not in silent and "activation skipped" not in silent


# --------------------------------------------------------------------------
# The argv smoke layer — parse-to-values wiring + the two-tier exit contract
# --------------------------------------------------------------------------


def test_cmd_dry_run_wires_argv_to_the_report(tmp_path):
    from click.testing import CliRunner

    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    result = CliRunner().invoke(verb.cmd, [str(tmp_path), "--dry-run"])
    assert result.exit_code == 0
    assert "(dry-run)" in result.output
    assert "— dry-run, nothing written" in result.output


def test_cmd_rejects_a_missing_path_at_parse(tmp_path):
    # The PATH validation moved to parse (ADR-0030): a nonexistent target is a
    # click usage error — exit 2 — never verb-body code.
    from click.testing import CliRunner

    result = CliRunner().invoke(verb.cmd, [str(tmp_path / "nope")])
    assert result.exit_code == 2
    assert "does not exist" in result.output


def test_cmd_rejects_a_file_path_at_parse(tmp_path):
    from click.testing import CliRunner

    victim = tmp_path / "a-file"
    victim.write_text("x\n")
    result = CliRunner().invoke(verb.cmd, [str(victim)])
    assert result.exit_code == 2


def test_cmd_mode_flags_are_mutually_exclusive():
    from click.testing import CliRunner

    for pair in (["--local", "--push"], ["--pr", "--local"], ["--pr", "--push"]):
        result = CliRunner().invoke(verb.cmd, [*pair, "."])
        assert result.exit_code == 2
        assert "mutually exclusive" in result.output


# --------------------------------------------------------------------------
# Block identity + the pin line (#433) — the reconcile report names blocks,
# and the pin stamp gets its own report/PR-body line.
# --------------------------------------------------------------------------


def test_format_plan_lines_carry_block_identity(tmp_path, rec):
    # A fresh install writes three marker blocks into ONE pixi.toml and five
    # hook entries into ONE settings.json: the report must distinguish them by
    # unit KEY (the [managed] names), never repeat a bare filename (#433).
    plan = _plan(tmp_path)
    report = verb.format_plan(plan)
    for key in (
        iunits.PIXI_KEY,
        iunits.PIXI_LINT_DEPS_KEY,
        iunits.PIXI_ENVS_KEY,
        iunits.SETTINGS_KEY,
        iunits.SETTINGS_STOP_KEY,
    ):
        assert f"add      {key}" in report
    # No line is a bare `add pixi.toml` with nothing after it.
    assert not any(
        line.strip() == "add      pixi.toml".strip()
        or line.rstrip().endswith(" pixi.toml")
        for line in report.splitlines()
        if line.strip().startswith("add")
    )


def test_pr_body_lists_units_by_key(tmp_path, rec):
    _apply(tmp_path, iapply.MODE_PR)
    assert f"- `{iunits.PIXI_LINT_DEPS_KEY}`" in rec.pr_body
    assert f"- `{iunits.SETTINGS_SESSIONSTART_KEY}`" in rec.pr_body


def test_format_result_renders_the_pin_stamp_line(tmp_path, rec):
    result = _apply(tmp_path, iapply.MODE_LOCAL)
    assert result.stamped_version == "testhash"
    assert "  pinned to testhash" in verb.format_result(result)


def test_pr_body_carries_the_pin_stamp_line(tmp_path, rec):
    _apply(tmp_path, iapply.MODE_PR)
    assert "Pinned to `testhash`" in rec.pr_body


def test_override_summary_uses_the_unit_key(tmp_path, rec):
    _apply(tmp_path)
    # Edit the managed tasks BLOCK -> next PR proposes an override, and the
    # <summary> names the block, not the shared filename.
    pixi = tmp_path / "pixi.toml"
    pixi.write_text(
        pixi.read_text().replace('lint = "./bin/shipit lint"', 'lint = "true"')
    )
    _apply(tmp_path, iapply.MODE_PR)
    assert f"<code>{iunits.PIXI_KEY}</code>" in rec.pr_body


# --------------------------------------------------------------------------
# The truly stock consumer (#449 item 8) — empty repo, no manifest, no
# configs, no hooks: the WS09/WS10 class dies here.
# --------------------------------------------------------------------------


@pytest.fixture
def stock_consumer(tmp_path):
    """A TRULY stock consumer: an empty directory. No AGENTS.md, no pixi.toml,
    no .shipit.toml, no .claude/, no hooks — the headline adoption case."""
    root = tmp_path / "stock"
    root.mkdir()
    return root


def test_fresh_install_on_a_truly_stock_consumer(stock_consumer, rec):
    result = _apply(stock_consumer, iapply.MODE_PR)

    # Every managed unit decided ADD (nothing pre-existed to reconcile).
    assert all(d.action == irec.ADD for d in result.plan.decisions)

    # The whole set landed: the block hosts were CREATED around the blocks.
    agents = (stock_consumer / "AGENTS.md").read_text()
    assert iunits.BLOCK_OPEN in agents
    manifest = tomllib.loads((stock_consumer / "pixi.toml").read_text())
    assert manifest["workspace"]["name"] == "stock"  # the seeded table
    assert "lint" in manifest["tasks"]
    settings = json.loads((stock_consumer / ".claude" / "settings.json").read_text())
    assert set(settings["hooks"]) == {
        "PreToolUse",
        "Stop",
        "SubagentStop",
        "SessionStart",
        "WorktreeCreate",
    }
    assert (stock_consumer / "lefthook.yml").is_file()
    assert (stock_consumer / "bin" / "shipit").is_file()
    assert (stock_consumer / ".markdownlint.yaml").is_file()

    # Policy seeded, manifest stamped, PR opened.
    cfg = config.load(stock_consumer / config.CONFIG_NAME)
    assert config.shipit_version(cfg) == "testhash"
    assert "reviewers" in cfg
    assert ("pr_create", True) in rec.calls


def test_stock_consumer_reinstall_reconciles_to_noop(stock_consumer, rec):
    _apply(stock_consumer)
    again = _plan(stock_consumer)
    assert again.nothing_to_do


# --------------------------------------------------------------------------
# Failure-path flow events (#434) — install.started/completed/failed
# --------------------------------------------------------------------------


def _events(caplog):
    from shipit import events as ev

    return [getattr(r, ev.EXTRA_KEY, None) for r in caplog.records]


def test_install_run_emits_started_and_completed(tmp_path, rec, caplog):
    import logging as _logging

    with caplog.at_level(_logging.INFO, logger="shipit.install"):
        rc = verb.run(str(tmp_path), dry_run=True)
    assert rc == 0
    names = _events(caplog)
    assert "install.started" in names
    assert "install.completed" in names
    assert "install.failed" not in names


def test_failed_install_emits_the_failed_event_with_the_step(
    tmp_path, rec, monkeypatch, caplog
):
    import logging as _logging

    def boom(*a, **k):
        raise ExecError(["git", "push"], rc=1, stderr="denied")

    monkeypatch.setattr(git, "push", boom)
    with caplog.at_level(_logging.INFO, logger="shipit.install"):
        rc = verb.run(str(tmp_path), pr=True)
    assert rc == 1  # the cli_errors shell still renders error + exit 1
    from shipit import events as ev

    failed = [
        r for r in caplog.records if getattr(r, ev.EXTRA_KEY, None) == "install.failed"
    ]
    assert len(failed) == 1
    assert failed[0].step == "apply"


def test_selfcert_failure_event_names_the_selfcert_step(
    tmp_path, rec, monkeypatch, caplog
):
    import logging as _logging

    from shipit.install import selfcert as sc

    def failing_cert(plan, root, **kw):
        return sc.CertReport(checks=(sc.CertCheck(name="planted", ok=False),))

    monkeypatch.setattr(sc, "certify", failing_cert)
    with caplog.at_level(_logging.INFO, logger="shipit.install"):
        rc = verb.run(str(tmp_path), pr=True)
    assert rc == 1
    from shipit import events as ev

    failed = [
        r for r in caplog.records if getattr(r, ev.EXTRA_KEY, None) == "install.failed"
    ]
    assert len(failed) == 1
    assert failed[0].step == "self-certification"
    assert rec.names() == []  # fail closed: no branch, no commit, no PR


# --------------------------------------------------------------------------
# TOL01-WS08 (#578): the [toolchains] seed + the changelog re-render — the two
# reconcile-channel fixes for the round-0 fleet sweep's red cells (ADR-0033:
# consumer drift is fixed through `shipit install`, never hand-patched).
# --------------------------------------------------------------------------


def test_install_seeds_toolchains_from_the_root_manifest(tmp_path, monkeypatch):
    # Class A: `shipit test`/`build` refuse without the [toolchains] map
    # (ADR-0007/0039) and install never seeded it. A consumer whose root
    # manifest signals a toolchain now gets the derived map seeded — and the
    # seeded config parses straight through the verbs' own loader.
    monkeypatch.setattr(iapply, "_activate_hooks", lambda root: _exec_result(0))
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "acme"\n')
    plan = _plan(tmp_path)
    assert "[toolchains]" in plan.seeds
    _apply(tmp_path)  # MODE_TREE: working-tree refresh
    entries = config.load_toolchains(config.load(tmp_path / ".shipit.toml"))
    assert [(e.path, e.toolchain) for e in entries] == [(".", "python")]
    # And it settles: the map is in place, so the re-plan is a clean no-op.
    assert _plan(tmp_path).nothing_to_do


def test_install_never_clobbers_a_consumer_toolchains_map(tmp_path, monkeypatch):
    # Seed-when-absent (the [lint] precedent): a consumer-edited map wins over
    # the manifest derivation, forever.
    monkeypatch.setattr(iapply, "_activate_hooks", lambda root: _exec_result(0))
    (tmp_path / "Cargo.toml").write_text("[package]\n")
    (tmp_path / ".shipit.toml").write_text('[toolchains]\n"." = "go"\n')
    plan = _plan(tmp_path)
    assert "[toolchains]" not in plan.seeds
    _apply(tmp_path)
    entries = config.load_toolchains(config.load(tmp_path / ".shipit.toml"))
    assert [(e.path, e.toolchain) for e in entries] == [(".", "go")]


def test_install_seeds_no_toolchains_without_a_manifest_signal(tmp_path):
    # No recognized root manifest → no seed; the Tool verbs keep their pointed
    # missing-map refusal (never a silent dispatch fallback, ADR-0007).
    plan = _plan(tmp_path)
    assert "[toolchains]" not in plan.seeds


def _changelog_consumer(root: Path) -> None:
    """A consumer that adopted the fragment convention (one fragment)."""
    (root / "CHANGELOG").mkdir()
    (root / "CHANGELOG" / "unreleased-first.md").write_text("- Added the thing\n")


def test_stale_changelog_projection_is_reconcile_work(tmp_path, monkeypatch):
    # Class B: a renderer change strands every consumer's committed render
    # (`shipit changelog check` fails fleet-wide). The reconcile detects the
    # stale projection, treats it as a work axis of its own, and the apply
    # regenerates CHANGELOG.md with the CURRENT renderer.
    from shipit import changelog as chlog
    from shipit.verbs.changelog import render_current

    monkeypatch.setattr(iapply, "_activate_hooks", lambda root: _exec_result(0))
    _changelog_consumer(tmp_path)
    (tmp_path / "CHANGELOG.md").write_text("# an old renderer's output\n")
    plan = _plan(tmp_path)
    assert plan.rerender_changelog
    assert not plan.nothing_to_do
    assert chlog.CHANGELOG_FILE in plan.changed_paths
    assert "render" in verb.format_plan(plan)

    _apply(tmp_path)  # MODE_TREE
    committed = (tmp_path / chlog.CHANGELOG_FILE).read_text()
    assert committed.startswith(chlog.RENDER_PREAMBLE)
    # The check's own verdict: the projection now matches a re-render.
    assert chlog.sync_diff(render_current(tmp_path), committed) is None
    # And it settles: nothing left to re-render, the re-plan is a no-op.
    replan = _plan(tmp_path)
    assert not replan.rerender_changelog
    assert replan.nothing_to_do


def test_missing_projection_with_fragments_is_also_stale(tmp_path):
    # The convention exists but CHANGELOG.md was never rendered/committed:
    # the reconcile carries the first render too (same axis, same fix).
    _changelog_consumer(tmp_path)
    assert _plan(tmp_path).rerender_changelog


def test_matching_changelog_projection_is_not_work(tmp_path):
    # A projection that already matches the current renderer plans no render.
    from shipit.verbs.changelog import render_current

    _changelog_consumer(tmp_path)
    (tmp_path / "CHANGELOG.md").write_text(render_current(tmp_path))
    assert not _plan(tmp_path).rerender_changelog


def test_unreadable_changelog_projection_fails_open_not_stale(tmp_path, caplog):
    # gather() runs this advisory read unconditionally, so an unreadable
    # committed CHANGELOG.md (here a non-UTF-8 file → UnicodeDecodeError, which
    # crashes inside render_current's own read; an OSError degrades the same
    # way) must fail OPEN to "not stale" with a warning, never crash `shipit
    # install` on a file it only inspects.
    _changelog_consumer(tmp_path)
    (tmp_path / "CHANGELOG.md").write_bytes(b"\xff\xfe not valid utf-8\n")
    with caplog.at_level(logging.WARNING):
        assert irec._changelog_stale(tmp_path) is False
        # And the whole gather → reconcile pipeline stays upright, not stale.
        assert not _plan(tmp_path).rerender_changelog
    assert any("unreadable CHANGELOG projection" in r.message for r in caplog.records)


def test_repo_without_the_fragment_convention_never_rerenders(tmp_path):
    # No CHANGELOG/ directory: nothing to re-render, never a refusal — the
    # `check` verb's hard error must not leak into the reconcile path.
    plan = _plan(tmp_path)
    assert not plan.rerender_changelog
    assert "CHANGELOG.md" not in plan.changed_paths


def test_unrenderable_changelog_dir_plans_no_render(tmp_path):
    # Unparseable version filenames: a render would silently drop the
    # mis-named section, so the reconcile declines (fail-open, #578) — the
    # consumer hears about the bad name from `shipit changelog check`.
    _changelog_consumer(tmp_path)
    (tmp_path / "CHANGELOG" / "not-semver.md").write_text("bad\n")
    (tmp_path / "CHANGELOG.md").write_text("stale\n")
    assert not _plan(tmp_path).rerender_changelog


def test_pr_body_carries_the_changelog_rerender_section(tmp_path, rec):
    # The reconcile PR explains the refreshed render (and the commit set
    # carries the file), so the merger knows why CHANGELOG.md changed.
    _changelog_consumer(tmp_path)
    (tmp_path / "CHANGELOG.md").write_text("# an old renderer's output\n")
    result = _apply(tmp_path, iapply.MODE_PR)
    assert "Changelog re-rendered" in rec.pr_body
    assert "CHANGELOG.md" in result.plan.changed_paths


def test_rerender_skipped_in_the_window_drops_the_phantom_changelog_path(tmp_path, rec):
    # The gather→apply race: the plan decides a re-render (fragments present, no
    # committed CHANGELOG.md), but CHANGELOG/ vanishes before apply runs, so
    # render_current → None and _rerender_changelog skips the write (the
    # retired-unlink idempotence stance). The now-phantom CHANGELOG.md must NOT
    # reach `git add` — otherwise a committing mode crashes with an opaque
    # pathspec error on a file that is absent AND untracked (#578 review).
    from shipit import changelog as chlog

    _changelog_consumer(tmp_path)
    plan = _plan(tmp_path)  # planned while the fragment tree still exists
    assert plan.rerender_changelog
    assert chlog.CHANGELOG_FILE in plan.changed_paths

    # The window: the fragment tree disappears between gather and apply.
    (tmp_path / "CHANGELOG" / "unreleased-first.md").unlink()
    (tmp_path / "CHANGELOG").rmdir()

    iapply.apply(plan, iapply.MODE_LOCAL)  # no pathspec crash

    # The skip landed no file, and the phantom path was dropped from the commit
    # set — the idempotent skip is complete, not half-done.
    assert not (tmp_path / chlog.CHANGELOG_FILE).exists()
    assert chlog.CHANGELOG_FILE not in rec.commit_paths
    add_paths = next(paths for name, paths in rec.calls if name == "add")
    assert chlog.CHANGELOG_FILE not in add_paths


def test_rerender_skipped_in_the_window_omits_the_pr_body_section(tmp_path, rec):
    # The MODE_PR twin of the drop: when the re-render is skipped in the window,
    # the PR body must NOT claim "Changelog re-rendered" — the body reflects
    # what apply ACTUALLY did (the hooks_activated discipline), never the plan's
    # decision, so it can never claim a re-render whose file was never committed
    # (#578 review).
    from shipit import changelog as chlog

    _changelog_consumer(tmp_path)
    plan = _plan(tmp_path)
    assert plan.rerender_changelog

    (tmp_path / "CHANGELOG" / "unreleased-first.md").unlink()
    (tmp_path / "CHANGELOG").rmdir()

    iapply.apply(
        plan,
        iapply.MODE_PR,
        pr_body=lambda before, hooks, rerendered, pin, debt: verb.format_pr_body(
            plan,
            before,
            hooks,
            rerendered=rerendered,
            stamped_version=pin,
            lint_debt=debt,
        ),
    )

    assert "Changelog re-rendered" not in rec.pr_body
    assert chlog.CHANGELOG_FILE not in rec.commit_paths
