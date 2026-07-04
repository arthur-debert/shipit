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
import os
import shutil
import stat
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

from shipit import config, execrun, gh, git
from shipit.execrun import ExecError
from shipit.install import apply as iapply
from shipit.install import reconcile as irec
from shipit.install import splice, units as iunits
from shipit.install.errors import InstallError
from shipit.verbs import install as verb


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
    state = irec.gather(Path(root), units, retired)
    return irec.reconcile(units, retired, state)


def _apply(root, mode: str = iapply.MODE_TREE, **kw) -> iapply.InstallResult:
    """reconcile + apply with the verb's PR-body renderer injected (the wiring
    `run()` performs), so PR-mode tests see the real rendered body."""
    plan = _plan(root)
    assert not plan.nothing_to_do, "test drove apply on a no-op plan"
    return iapply.apply(
        plan,
        mode,
        pr_body=lambda before, hooks: verb.format_pr_body(plan, before, hooks),
        **kw,
    )


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
    # docs/prd/adoption.md — amending the lint PRD's task-line-only decision),
    # tested below. `provision-lexd` invokes the binary's provision subcommand
    # (ADP00-WS03), so no provisioning script is ever distributed.
    assert pixi.desired_inner() == (
        'lint = "shipit lint"\n'
        'logs = "shipit logs"\n'
        'provision-lexd = "shipit provision lexd"'
    )


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
# The ADP00 managed consumer environment (docs/prd/adoption.md) — the lint
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

    # Three sibling blocks in ONE consumer file: their marker fences must be
    # pairwise distinct or extract/splice would bleed across regions.
    fences = {
        units[k].open_marker
        for k in (iunits.PIXI_KEY, iunits.PIXI_LINT_DEPS_KEY, iunits.PIXI_ENVS_KEY)
    }
    assert len(fences) == 3


def test_packaged_lint_env_agrees_with_shipits_own_manifest():
    """The dogfood drift check (docs/prd/adoption.md): shipit's own manifest and
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
    units = {u.key: u for u in iunits.load_units()}
    for key in (iunits.PIXI_KEY, iunits.PIXI_LINT_DEPS_KEY, iunits.PIXI_ENVS_KEY):
        unit = units[key]
        assert irec.consumer_hash(root, unit) == unit.desired_hash(), key


# --------------------------------------------------------------------------
# The ADP00 consumer-generic lefthook caller (docs/prd/adoption.md, #419) —
# the managed variant works on a stock consumer right after install; shipit's
# own repo-local legs live in a committed lefthook-local.yml (lefthook's
# native config layering), never in the managed file.
# --------------------------------------------------------------------------


def _managed_lefthook() -> dict:
    return yaml.safe_load(iunits.data_bytes("lefthook.yml"))


def test_managed_lefthook_is_consumer_generic():
    """Every hook leg of the managed caller runs through the pinned lint env
    and invokes only the managed `lint` task or the shipit binary itself — no
    shipit-repo-local scripts or paths (the stock-consumer guarantee, #419)."""
    cfg = _managed_lefthook()
    assert set(cfg) == {"pre-commit", "pre-push", "post-commit"}
    for hook in cfg.values():
        for cmd in hook["commands"].values():
            run = cmd["run"]
            # Everything rides the pinned lint env (never bare `pixi run`)...
            assert run.startswith("pixi run -e lint ")
            # ...and invokes the managed `lint` task or shipit itself — never
            # a shell indirection into a repo-local script.
            assert run.removeprefix("pixi run -e lint ").split()[0] in (
                "lint",
                "shipit",
            )
            assert "tools/" not in run and ".lex" not in run

    # The exact legs: pre-commit lint (piped, priority 2 — the slot a local
    # leg like shipit's own lex-mirror runs ahead of), pre-push lint + the
    # classification tripwire. (The post-commit dev-cycle leg is asserted in
    # test_logevent.py's managed-hook-tier test.)
    assert cfg["pre-commit"]["piped"] is True
    lint = cfg["pre-commit"]["commands"]["lint"]
    assert lint == {"priority": 2, "run": "pixi run -e lint lint"}
    assert cfg["pre-push"]["commands"]["lint"]["run"] == "pixi run -e lint lint"
    assert (
        cfg["pre-push"]["commands"]["classify-gate"]["run"]
        == "pixi run -e lint shipit pr push-gate"
    )

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
    committed lefthook-local.yml — still regenerating mirrors ahead of lint in
    the piped pre-commit chain, so shipit's own hooks stay green (dogfood)."""
    root = Path(__file__).resolve().parents[1]
    local = yaml.safe_load((root / "lefthook-local.yml").read_text(encoding="utf-8"))
    leg = local["pre-commit"]["commands"]["lex-mirror"]
    assert "tools/lex-convert-doc.sh" in leg["run"]
    assert (root / "tools" / "lex-convert-doc.sh").is_file()
    # It slots BEFORE the managed lint command in the managed piped chain.
    managed = _managed_lefthook()
    assert managed["pre-commit"]["piped"] is True
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


def test_managed_markdownlint_config_relaxes_exactly_two_rules():
    """MD013/MD041 off for the managed set's markdown genre; every other rule
    stays at markdownlint's defaults so real structural issues still fail."""
    cfg = yaml.safe_load(iunits.data_bytes("markdownlint.yaml"))
    assert cfg == {"default": True, "MD013": False, "MD041": False}


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


def test_managed_markdownlintignore_covers_managed_paths_only():
    """The ignore file excludes exactly the managed/vendored markdown — never
    a consumer-authored file (a consumer's README.md is theirs; shipit's own
    README is skipped only because it is a lex projection, which `shipit lint`
    routes to the lexd leg with no ignore entry — tested in test_lint.py)."""
    entries = [
        line
        for line in iunits.data_bytes("markdownlintignore").decode().splitlines()
        if line and not line.startswith("#")
    ]
    assert entries == ["skills/", "AGENTS.md"]


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
    """Fresh consumer install ADDs the three config units, a re-install NOOPs,
    and a consumer edit surfaces as OVERRIDE (never silently kept)."""
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


def _write_launcher(dir_path: Path) -> Path:
    """Write the MANAGED bin/shipit launcher (the shipped template) into dir_path/shipit."""
    unit = next(u for u in iunits.load_units() if u.key == "bin/shipit")
    binp = dir_path / "shipit"
    binp.write_bytes(unit.content)
    binp.chmod(binp.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return binp


def _path_without_shipit() -> str:
    """The real PATH minus any dir that already holds a `shipit` — so the launcher's
    shebang tools (bash/env/realpath) are present but the ONLY `shipit` the test sees is
    the one(s) it plants. Prevents the ambient pixi-env shipit from shadowing the guard."""
    keep = [
        d
        for d in os.environ.get("PATH", "").split(os.pathsep)
        if d and not (Path(d) / "shipit").exists()
    ]
    return os.pathsep.join(keep)


def test_bootstrap_launcher_does_not_self_exec_when_its_dir_is_on_path(tmp_path: Path):
    # Fork-bomb guard (codex/agy ERROR): with the launcher's OWN dir the only one on
    # PATH, a bare `command -v shipit` resolves to the launcher itself — exec'ing it
    # would loop forever. The launcher must detect the self-match, refuse to exec
    # itself, and fail loud (exit 127). `os.defpath` supplies bash/env without a real
    # `shipit`; the 10s timeout would trip (test failure) if it ever self-loops.
    binhome = tmp_path / "bin"
    binhome.mkdir()
    launcher = _write_launcher(binhome)

    proc = subprocess.run(
        [str(launcher), "--version"],
        env={"PATH": str(binhome) + os.pathsep + _path_without_shipit()},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 127
    assert "only this in-repo launcher" in proc.stderr


def test_bootstrap_launcher_execs_the_real_shipit_elsewhere_on_path(tmp_path: Path):
    # Normal case preserved: with the launcher's dir first on PATH (a self-match it
    # skips) and a REAL shipit later on PATH, the launcher execs the real one.
    binhome = tmp_path / "bin"
    binhome.mkdir()
    _write_launcher(binhome)

    realdir = tmp_path / "realbin"
    realdir.mkdir()
    real = realdir / "shipit"
    real.write_text('#!/usr/bin/env bash\necho "REAL-SHIPIT-RAN $*"\n')
    real.chmod(real.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    proc = subprocess.run(
        [str(binhome / "shipit"), "arg1"],
        env={
            "PATH": os.pathsep.join(
                [str(binhome), str(realdir), _path_without_shipit()]
            )
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 0
    assert "REAL-SHIPIT-RAN arg1" in proc.stdout


# --------------------------------------------------------------------------
# The HAR01 harness units — generated agent-defs + the settings.json hook line
# (docs/prd/har01-coordinator-guard-and-role-prompts.md, user stories 17 & 21)
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
    # Splicing all four event entries into one file leaves each in its own event
    # array, none clobbering another — the consumer keeps a single valid settings.json.
    units = {u.key: u for u in iunits.load_units()}
    text = ""
    for key in (
        iunits.SETTINGS_KEY,
        iunits.SETTINGS_STOP_KEY,
        iunits.SETTINGS_SUBAGENTSTOP_KEY,
        iunits.SETTINGS_SESSIONSTART_KEY,
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
    # And each event unit reconciles to NOOP against the file carrying all four.
    for key in (
        iunits.SETTINGS_KEY,
        iunits.SETTINGS_STOP_KEY,
        iunits.SETTINGS_SUBAGENTSTOP_KEY,
        iunits.SETTINGS_SESSIONSTART_KEY,
    ):
        u = units[key]
        got = splice.extract_settings_hook(text, u.event, u.marker)
        assert got == iunits.canonical_hook_entry(json.loads(u.desired_inner()))


# --------------------------------------------------------------------------
# The SES01 session-bootstrap units — ./claude-start launcher + SessionStart
# activation hook (docs/prd/session-bootstrap.md Layers A & D, issue #218)
# --------------------------------------------------------------------------


def test_load_units_includes_the_claude_start_launcher():
    units = {u.key: u for u in iunits.load_units()}
    assert iunits.LAUNCHER_FILE in units
    unit = units[iunits.LAUNCHER_FILE]
    assert unit.kind == "file"
    assert unit.dest == "claude-start"  # repo root, memorable entry point
    assert unit.executable is True
    text = unit.content.decode("utf-8")
    # The launcher's whole job: exec `claude --worktree "<minted-id>" "$@"`.
    assert "--worktree" in text
    assert 'exec claude --worktree "sess-' in text


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


def test_fresh_install_lays_down_the_session_bootstrap_set_idempotently(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    result = _apply(tmp_path)
    assert result.mode == iapply.MODE_TREE

    # The launcher landed at the repo root, executable.
    launcher = tmp_path / "claude-start"
    assert launcher.is_file()
    assert os.access(launcher, os.X_OK)
    assert "--worktree" in launcher.read_text()

    # The SessionStart activation hook landed in .claude/settings.json.
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    entries = settings["hooks"]["SessionStart"]
    assert any(
        splice.is_shipit_hook(e, iunits.SETTINGS_SESSIONSTART_MARKER) for e in entries
    )

    # Both recorded a pristine hash in the manifest.
    managed = config.load_managed(config.load(tmp_path / ".shipit.toml"))
    assert iunits.LAUNCHER_FILE in managed
    assert iunits.SETTINGS_SESSIONSTART_KEY in managed

    # Idempotent: a second reconcile decides NOOP for everything — nothing to
    # apply, no git, no PR, artifacts byte-identical.
    rec.calls.clear()
    launcher_before = launcher.read_bytes()
    settings_before = (tmp_path / ".claude" / "settings.json").read_bytes()
    again = _plan(tmp_path)
    assert again.nothing_to_do
    assert rec.calls == []
    assert launcher.read_bytes() == launcher_before
    assert (tmp_path / ".claude" / "settings.json").read_bytes() == settings_before


def test_claude_start_execs_claude_with_a_minted_session_id(tmp_path: Path):
    # Behavior of the shipped launcher: it execs `claude --worktree <minted-id>`
    # forwarding its own args, with a fresh `sess-`-prefixed id per launch.
    unit = next(u for u in iunits.load_units() if u.key == iunits.LAUNCHER_FILE)
    launcher = tmp_path / "claude-start"
    launcher.write_bytes(unit.content)
    launcher.chmod(0o755)

    # A fake `claude` first on PATH (shadowing any real one) that prints its argv.
    fakedir = tmp_path / "bin"
    fakedir.mkdir()
    fake = fakedir / "claude"
    fake.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$@"\n')
    fake.chmod(0o755)
    env = {"PATH": str(fakedir) + os.pathsep + os.environ.get("PATH", "")}

    def launch(*args: str) -> list[str]:
        proc = subprocess.run(
            [str(launcher), *args], env=env, capture_output=True, text=True, timeout=10
        )
        assert proc.returncode == 0, proc.stderr
        return proc.stdout.splitlines()

    argv = launch("extra", "--args")
    assert argv[0] == "--worktree"
    assert argv[1].startswith("sess-")  # the minted, prefixed session id
    assert argv[2:] == ["extra", "--args"]  # the launcher's args pass through

    # A second launch mints a distinct id — no two sessions share a Tree id.
    assert launch()[1] != argv[1]


def test_claude_start_fails_loud_when_claude_is_not_on_path(tmp_path: Path):
    unit = next(u for u in iunits.load_units() if u.key == iunits.LAUNCHER_FILE)
    launcher = tmp_path / "claude-start"
    launcher.write_bytes(unit.content)
    launcher.chmod(0o755)

    # A minimal PATH with exactly one entry: `bash` (the shebang's interpreter)
    # and nothing else — deterministically no `claude`, regardless of where the
    # developer machine keeps its binaries.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    bash = shutil.which("bash")
    assert bash is not None
    (bindir / "bash").symlink_to(bash)
    proc = subprocess.run(
        [str(launcher)],
        env={"PATH": str(bindir)},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 127
    assert "claude CLI is not on PATH" in proc.stderr


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

    def activate_hooks(self, root):
        # Stand in for `lefthook install`: record the call, mutate nothing.
        self.hook_activations.append(root)
        return _exec_result(0)

    def switch_create(self, branch, *, cwd):
        self.calls.append(("switch", branch))

    def add(self, paths, *, cwd):
        self.calls.append(("add", tuple(paths)))

    def commit(self, message, paths, *, cwd):
        self.calls.append(("commit", message))

    def push(self, branch, *, cwd, remote="origin", force=False):
        self.calls.append(("push", branch))

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
    assert (tmp_path / "skills" / "shipit-to-prd" / "SKILL.md").is_file()
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
    skill = tmp_path / "skills" / "shipit-to-prd" / "SKILL.md"
    skill.write_text("CONSUMER EDIT\n")

    _apply(tmp_path, iapply.MODE_PR)
    assert ("pr_create", True) in rec.calls
    assert "### Overrides" in rec.pr_body
    assert "skills/shipit-to-prd/SKILL.md" in rec.pr_body
    # The diff is captured BEFORE the overwrite, so it shows the consumer's edit
    # (a non-empty diff), not an empty diff against what shipit just wrote.
    assert "CONSUMER EDIT" in rec.pr_body
    assert "```diff" in rec.pr_body


def test_fresh_install_delivers_the_lint_environment(tmp_path, rec):
    # ADP00 (docs/prd/adoption.md): a fresh install ADDs the lint env blocks —
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
    assert manifest["tasks"]["lint"] == "shipit lint"
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
    assert manifest["tasks"]["lint"] == "shipit lint"
    assert set(manifest["feature"]["lint"]["dependencies"]) == set(LINT_TOOLS)
    assert manifest["environments"]["lint"] == ["lint"]

    # The seed is scaffold, not a managed unit: only the three block units are
    # recorded, so the [workspace] table is consumer-owned from here on.
    managed = config.load_managed(config.load(tmp_path / ".shipit.toml"))
    pixi_keys = {k for k in managed if k.startswith("pixi.toml")}
    assert pixi_keys == {
        iunits.PIXI_KEY,
        iunits.PIXI_LINT_DEPS_KEY,
        iunits.PIXI_ENVS_KEY,
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
    assert manifest["tasks"]["lint"] == "shipit lint"


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
    assert manifest["tasks"]["lint"] == "shipit lint"


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

    skill = tmp_path / "skills" / "shipit-to-prd" / "SKILL.md"
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
    assert "skills/shipit-to-prd/SKILL.md" in warning


def test_push_flag_pushes_to_branch_without_pr(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    result = _apply(tmp_path, iapply.MODE_PUSH)
    assert result.branch == "main"
    assert ("push", "main") in rec.calls
    assert "pr_create" not in rec.names()


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
    # The PR body announces the checks are live.
    assert "### Checks activated" in rec.pr_body
    assert "lefthook install" in rec.pr_body


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
    assert "lefthook install" in rec.pr_body


def test_install_degrades_but_succeeds_when_lefthook_missing(tmp_path, rec):
    # A missing/unlaunchable lefthook surfaces as the runner's ExecError
    # (ADR-0028); apply must degrade — pointing at the canonical recovery — and
    # still finish its PR rather than aborting.
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
    # Points at the canonical recovery, which works in a consumer repo too.
    warning = verb.format_result_warnings(result)
    assert "could not activate git hooks" in warning
    assert "not found on PATH" in warning
    assert "lefthook install` to activate the checks" in warning


def test_install_activation_timeout_does_not_claim_missing_binary(tmp_path, rec):
    # A NON-missing-binary transport failure (e.g. a timeout) must not be
    # mislabelled "not found on PATH": the detail branches on exc.cause and
    # points at resolving the failure, still ending in the canonical recovery.
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
    assert "could not run" in warning
    assert "lefthook install` to activate the checks" in warning


def test_activate_hooks_boundary_runs_lefthook_install(tmp_path, monkeypatch):
    # The real boundary hands `lefthook install` to the one Exec runner (the
    # install-hooks task invocation), in the consumer root, check=False — never
    # a re-implemented hook writer, never a raised ExecError on a nonzero rc.
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
    assert captured["argv"] == ["lefthook", "install"]
    assert captured["cwd"] == str(tmp_path)
    assert captured["check"] is False
    # The stated local bound rides the wire (ADR-0028): lefthook writes a few
    # .git/hooks files locally — git's local tier, never the runner's implicit
    # 5-minute default.
    assert captured["timeout"] == iapply.HOOK_ACTIVATE_TIMEOUT
    assert iapply.HOOK_ACTIVATE_TIMEOUT < execrun.DEFAULT_TIMEOUT
    assert "pre-commit" in iapply._activation_output(result)


def test_activate_hooks_boundary_missing_binary_is_exec_error(tmp_path, monkeypatch):
    # A binary absent from PATH surfaces as the runner's single transport error,
    # tagged missing-binary — never a raw FileNotFoundError or a silent skip.
    monkeypatch.setattr(iapply, "LEFTHOOK_BINARY", "shipit-no-such-lefthook-xyz")
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
# Retired files (docs/prd/rvw01-sole-requester.md, ADR-0031)
# --------------------------------------------------------------------------

# A pristine copy of the retired Copilot caller workflow, snapshotted before
# the epic deletes it from this repo — the e2e tests below plant it into a
# consumer, and its hash pins the packaged manifest to real historical content.
PRISTINE_WORKFLOW = Path(__file__).parent / "data" / "copilot-review-pristine.yml"
RETIRED_WORKFLOW_PATH = ".github/workflows/copilot-review.yml"


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
