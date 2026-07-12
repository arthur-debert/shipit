"""Install self-certification (ADR-0033, ADP00-WS15 #449) — the staged
postconditions, the fail-closed apply seam, and the debt report.

Layout mirrors the install suite: the check functions test as plain functions
against a REAL staged consumer (written by a working-tree apply) with the Exec
boundary injected; the launcher postcondition runs the REAL delivered bash
launcher under its ``SHIPIT_PIN_CHECK`` probe; the apply seam asserts the
fail-closed contract (no git, no PR) on the recorded boundary.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from shipit import config, execrun, gh, git, lint
from shipit.install import apply as iapply
from shipit.install import reconcile as irec
from shipit.install import selfcert
from shipit.install import units as iunits
from shipit.install.errors import InstallError, SelfCertError
from shipit.verbs import install as verb

#: A syntactically valid full sha for pins the launcher must accept.
GOOD_SHA = "a" * 40


def _exec_ok(argv=("x",), stdout="", stderr="") -> execrun.ExecResult:
    return execrun.ExecResult(
        argv=tuple(argv), rc=0, stdout=stdout, stderr=stderr, duration_ms=1
    )


def _exec_fail(argv=("x",), stdout="", stderr="boom") -> execrun.ExecResult:
    return execrun.ExecResult(
        argv=tuple(argv), rc=1, stdout=stdout, stderr=stderr, duration_ms=1
    )


def _plan(root) -> irec.Plan:
    units = iunits.load_units()
    retired = irec.load_retired()
    state = irec.gather(Path(root), units, retired)
    return irec.reconcile(units, retired, state)


class _GhRecorder:
    """Records the git/PR boundary calls apply makes, doing nothing real
    (the same shape as test_install.py's recorder)."""

    def __init__(self):
        self.calls = []
        self.pr_body = None
        self.commit_paths = ()
        self.commit_no_verify = None
        self.push_no_verify = None

    def activate_hooks(self, root):
        return execrun.ExecResult(
            argv=("lefthook", "install"), rc=0, stdout="", stderr="", duration_ms=1
        )

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
        return None

    def pr_create(self, *, head, title, body, draft, cwd, **kw):
        self.calls.append(("pr_create", draft))
        self.pr_body = body
        return "https://github.com/acme/repo/pull/1"

    def names(self):
        return [c[0] for c in self.calls]


@pytest.fixture
def rec(monkeypatch):
    r = _GhRecorder()
    for name in ("switch_create", "add", "commit", "push", "current_branch"):
        monkeypatch.setattr(git, name, getattr(r, name))
    for name in ("pr_url_for_head", "pr_create"):
        monkeypatch.setattr(gh, name, getattr(r, name))
    monkeypatch.setattr(iapply, "_shipit_version", lambda: "testhash")
    monkeypatch.setattr(iapply, "_activate_hooks", r.activate_hooks)
    return r


@pytest.fixture
def staged(tmp_path, rec):
    """A consumer with the managed set STAGED (default working-tree apply) and
    a valid pin stamped — the state self-certification asserts over."""
    plan = _plan(tmp_path)
    iapply.apply(plan, iapply.MODE_TREE)
    cfg = tmp_path / config.CONFIG_NAME
    text = cfg.read_text()
    cfg.write_text(text.replace('version = "testhash"', f'version = "{GOOD_SHA}"'))
    return tmp_path


# --------------------------------------------------------------------------
# The scoped lint set — whole-file units only
# --------------------------------------------------------------------------


def test_delivered_lint_paths_are_whole_file_units_only(tmp_path, rec):
    plan = _plan(tmp_path)
    paths = selfcert.delivered_lint_paths(plan)
    # The delivered whole files are in scope...
    assert "lefthook.yml" in paths
    assert "bin/shipit" in paths
    assert ".markdownlint.yaml" in paths
    # ...but the block units' host files are not: install delivered a region,
    # not the file — the consumer content around it is debt, never a blocker.
    assert "pixi.toml" not in paths
    assert "AGENTS.md" not in paths
    assert ".claude/settings.json" not in paths


# --------------------------------------------------------------------------
# Postcondition 1 — manifest parses + lint env solves
# --------------------------------------------------------------------------


def test_manifest_check_solves_the_lint_env(staged):
    seen = {}

    def runner(argv, **kw):
        seen["argv"] = argv
        return _exec_ok(argv)

    check = selfcert._check_manifest(staged, runner)
    assert check.ok
    assert seen["argv"] == ["pixi", "install", "--environment", "lint"]


def test_manifest_solve_is_unlocked_so_managed_block_edits_stay_lock_coherent(
    staged,
):
    # The #793 lock-coherence contract: a reconcile that edits a managed
    # pixi.toml block (e.g. delivering the cargo-edit release block) makes the
    # committed pixi.lock stale — every consumer `pixi run --locked` would then
    # hard-fail. Self-cert's solve deliberately carries NO `--locked`, so pixi
    # REGENERATES the workspace lock to match the reconciled manifest (and
    # apply stages it into the same commit — the PIXI_LOCK tests below); a
    # locked solve here would fail on exactly the edit install just made.
    seen = {}

    def runner(argv, **kw):
        seen["argv"] = argv
        return _exec_ok(argv)

    assert selfcert._check_manifest(staged, runner).ok
    assert "--locked" not in seen["argv"]


def test_manifest_check_solves_under_a_scrubbed_env(staged, monkeypatch):
    # A parent dev session's leaked project pointer must not reach the solve:
    # self-cert hands pixi a scrubbed, complete child env (replace_env), so the
    # lint-env solve targets the consumer checkout, never the parent project.
    monkeypatch.setenv("PIXI_PROJECT_MANIFEST", "/parent/pixi.toml")
    seen = {}

    def runner(argv, **kw):
        seen.update(kw)
        return _exec_ok(argv)

    selfcert._check_manifest(staged, runner)
    assert seen["replace_env"] is True
    assert "PIXI_PROJECT_MANIFEST" not in seen["env"]


def test_delivered_lint_scrubs_the_child_env(staged, monkeypatch):
    monkeypatch.setenv("PIXI_PROJECT_MANIFEST", "/parent/pixi.toml")
    seen = {}

    def runner(argv, **kw):
        seen.update(kw)
        return _exec_ok(argv)

    selfcert._check_delivered_lint(staged, _plan_with_writes(staged), runner)
    assert seen["replace_env"] is True
    assert "PIXI_PROJECT_MANIFEST" not in seen["env"]


def test_manifest_check_fails_on_a_broken_stamped_config(staged):
    (staged / config.CONFIG_NAME).write_text("not = valid = toml\n")
    check = selfcert._check_manifest(staged, lambda argv, **kw: _exec_ok(argv))
    assert not check.ok
    assert config.CONFIG_NAME in check.detail


def test_manifest_check_fails_when_the_lint_env_does_not_solve(staged):
    def runner(argv, **kw):
        raise execrun.ExecError(argv, rc=1, stderr="solve failed", cause="exit")

    check = selfcert._check_manifest(staged, runner)
    assert not check.ok
    assert "pixi install" in check.detail


def test_manifest_check_fails_without_a_pixi_manifest(staged):
    (staged / "pixi.toml").unlink()
    check = selfcert._check_manifest(staged, lambda argv, **kw: _exec_ok(argv))
    assert not check.ok


# --------------------------------------------------------------------------
# Postcondition 2 — delivered files pass delivered lint configs (scoped run)
# --------------------------------------------------------------------------


def test_delivered_lint_runs_each_tool_through_the_lint_env(staged):
    argvs = []

    def runner(argv, **kw):
        argvs.append(argv)
        return _exec_ok(argv)

    check = selfcert._check_delivered_lint(staged, _plan_with_writes(staged), runner)
    assert check.ok
    # Every tool invocation rode `pixi run --environment lint` against the
    # consumer's own manifest — never bare PATH.
    assert argvs, "the scoped run must actually invoke tools"
    for argv in argvs:
        assert argv[:2] == ["pixi", "run"]
        assert "--environment" in argv and "lint" in argv


def _plan_with_writes(root) -> irec.Plan:
    """A plan whose write set is the full managed catalog (fresh-install shape),
    decided against an EMPTY pristine map so every unit is a write."""
    units = iunits.load_units()
    retired = irec.load_retired()
    state = irec.ConsumerState(
        root=str(root),
        consumer_hashes={u.key: None for u in units},
        pristine={},
        retired_hashes={},
        seeds=(),
    )
    return irec.reconcile(units, retired, state)


def test_delivered_lint_failure_fails_the_check_with_the_report(staged):
    def runner(argv, **kw):
        # Every markdownlint leg fails; the rest pass.
        if "markdownlint" in argv:
            return _exec_fail(argv, stdout="skills/x.md:1 MD000 broken")
        return _exec_ok(argv)

    check = selfcert._check_delivered_lint(staged, _plan_with_writes(staged), runner)
    assert not check.ok
    assert "MD000" in check.detail or "FAIL" in check.detail


def test_delivered_lint_is_vacuous_with_no_whole_file_writes(tmp_path, rec):
    # A NOOP re-run shape: no writes at all -> nothing to lint, trivially ok.
    plan = irec.Plan(root=str(tmp_path), decisions=(), retired=(), seeds=())
    check = selfcert._check_delivered_lint(
        tmp_path, plan, lambda argv, **kw: _exec_fail(argv)
    )
    assert check.ok


def test_delivered_lint_fails_closed_when_a_planned_file_is_missing(staged):
    # A whole-file unit the plan writes but that is absent on disk is a delivery
    # failure (install did not write a file it intended to), not "nothing to
    # lint": self-cert must fail CLOSED and name the missing path (ADR-0033).
    plan = _plan_with_writes(staged)
    (staged / "bin" / "shipit").unlink()
    check = selfcert._check_delivered_lint(
        staged, plan, lambda argv, **kw: _exec_ok(argv)
    )
    assert not check.ok
    assert "bin/shipit" in check.detail


# --------------------------------------------------------------------------
# Postcondition 2, managed skills (#777) — the delivered skill/*.md content is
# no longer exempt from the delivered markdownlint gate, so self-cert's
# delivered-lint CATCHES a skill-content defect that would otherwise ride the
# managed set into a consumer's markdownlint gate (modes 4+6). These route the
# REAL markdownlint through selfcert's own `_check_delivered_lint` boundary,
# scoped to just the skill files so only the markdown leg runs.
# --------------------------------------------------------------------------


def _skill_only_plan(root) -> irec.Plan:
    """A plan whose write set is JUST the managed skill files, so a scoped
    delivered-lint routes only markdownlint over the shipped skill/*.md."""
    skills = [u for u in iunits.load_units() if u.key.startswith("skills/")]
    decisions = tuple(
        irec.Decision(
            unit=u,
            action=irec.ADD,
            desired_hash="h",
            consumer_hash=None,
            pristine_hash=None,
        )
        for u in skills
    )
    return irec.Plan(root=str(root), decisions=decisions, retired=(), seeds=())


def _unwrapping_real_runner():
    """A self-cert Exec boundary that unwraps `pixi run ... -- <tool> <args>`
    and runs the REAL tool on PATH. The tests provision the lint toolchain, so
    this exercises the actual markdownlint gate over the delivered skill files —
    the delivered `.markdownlint.yaml` (`--config`) and `.markdownlintignore`
    (auto-discovered from cwd) — without solving a pixi env."""

    def runner(argv, *, cwd, **kw):
        real = argv[argv.index("--") + 1 :]
        return execrun.run(real, cwd=cwd, check=False)

    return runner


def test_managed_skill_files_are_in_the_delivered_lint_set(staged):
    # The root-cause guard (no binary): every managed skill/*.md is a whole-file
    # unit, so it is in scope for the delivered-lint check — the blindness was
    # only the shipped `.markdownlintignore` exempting `skills/`, now removed.
    paths = selfcert.delivered_lint_paths(_skill_only_plan(staged))
    assert "skills/grill-me-with-docs/SKILL.md" in paths
    assert "skills/to-spec/SKILL.md" in paths
    # The delivered ignore no longer blanket-exempts the managed skills tree.
    ignore = (staged / ".markdownlintignore").read_text().splitlines()
    assert "skills/" not in {line.strip() for line in ignore}


@pytest.mark.skipif(shutil.which("markdownlint") is None, reason="no markdownlint")
def test_delivered_skill_files_pass_the_delivered_config_real(staged):
    # The two files #777 fixed (and the whole shipped skills tree) pass the
    # delivered markdownlint config under self-cert's own scoped run: the managed
    # set never fails its own checks (the WS09/WS10 canary class, ADR-0033).
    check = selfcert._check_delivered_lint(
        staged, _skill_only_plan(staged), _unwrapping_real_runner()
    )
    assert check.ok, check.detail


@pytest.mark.skipif(shutil.which("markdownlint") is None, reason="no markdownlint")
def test_delivered_lint_catches_a_planted_skill_defect_real(staged):
    # Plant an MD040 bare-fence defect (mode 4's exact class) into a delivered
    # skill file: self-cert must now CATCH it — the defect can no longer ship.
    skill = staged / "skills" / "grill-me-with-docs" / "SKILL.md"
    skill.write_text(skill.read_text() + "\n```\nplanted bare fence\n```\n")
    check = selfcert._check_delivered_lint(
        staged, _skill_only_plan(staged), _unwrapping_real_runner()
    )
    assert not check.ok
    assert "MD040" in check.detail
    assert "skills/grill-me-with-docs/SKILL.md" in check.detail


# --------------------------------------------------------------------------
# Postcondition 3 — hooks live
# --------------------------------------------------------------------------


def test_hooks_check_requires_a_successful_activation(staged):
    plan = _plan_with_writes(staged)
    check = selfcert._check_hooks(staged, plan, hooks_activated=False)
    assert not check.ok
    # Operator-facing recovery speaks shipit, never the internal lefthook layer.
    assert "hook activation did not succeed" in check.detail
    assert "./bin/shipit install" in check.detail
    assert "lefthook install" not in check.detail


def test_hooks_check_requires_the_hook_files_on_disk(staged):
    plan = _plan_with_writes(staged)
    check = selfcert._check_hooks(staged, plan, hooks_activated=True)
    assert not check.ok  # activation claimed success but .git/hooks is empty

    hooks = staged / ".git" / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "pre-commit").write_text("#!/bin/sh\n# lefthook\n")
    (hooks / "pre-push").write_text("#!/bin/sh\n# lefthook\n")
    assert selfcert._check_hooks(staged, plan, hooks_activated=True).ok


def test_hooks_check_is_vacuous_when_the_plan_activates_nothing(tmp_path, rec):
    plan = irec.Plan(root=str(tmp_path), decisions=(), retired=(), seeds=())
    assert selfcert._check_hooks(tmp_path, plan, hooks_activated=None).ok


def test_hooks_check_is_vacuous_when_activation_was_not_attempted(tmp_path, rec):
    # The seed-only / retire-delete-only committing install: the managed set
    # (lefthook.yml included) is already current, so `activates_hooks` is True
    # but there are no writes — apply skips activation and leaves the live hooks
    # alone, so `hooks_activated` is None. The postcondition must mirror that
    # predicate and stay vacuous, not fail the install closed.
    lefthook = next(u for u in iunits.load_units() if u.key == iunits.LEFTHOOK_FILE)
    noop = irec.Decision(
        unit=lefthook,
        action=irec.NOOP,
        desired_hash="h",
        consumer_hash="h",
        pristine_hash="h",
    )
    plan = irec.Plan(root=str(tmp_path), decisions=(noop,), retired=(), seeds=())
    assert plan.activates_hooks and not plan.writes
    assert selfcert._check_hooks(tmp_path, plan, hooks_activated=None).ok


# --------------------------------------------------------------------------
# Postcondition 4 — the launcher resolves the freshly-stamped pin
# (REAL bash, REAL delivered launcher, no uv, no network)
# --------------------------------------------------------------------------


def _launcher_plan(root, declined: tuple[str, ...] = ()) -> irec.Plan:
    """A minimal plan for the launcher postcondition — only its ``declined``
    record matters to the probe (#600)."""
    return irec.Plan(
        root=str(root), decisions=(), retired=(), seeds=(), declined=declined
    )


def test_launcher_check_resolves_the_stamped_pin_for_real(staged):
    check = selfcert._check_launcher(
        staged, _launcher_plan(staged), GOOD_SHA, execrun.run
    )
    assert check.ok, check.detail


def test_launcher_check_fails_on_a_pin_mismatch(staged):
    check = selfcert._check_launcher(
        staged, _launcher_plan(staged), "b" * 40, execrun.run
    )
    assert not check.ok
    assert GOOD_SHA[:8] in check.detail or "resolved" in check.detail


def test_launcher_check_fails_when_the_stamp_is_not_a_sha(staged):
    cfg = staged / config.CONFIG_NAME
    cfg.write_text(cfg.read_text().replace(GOOD_SHA, "testhash"))
    check = selfcert._check_launcher(
        staged, _launcher_plan(staged), "testhash", execrun.run
    )
    assert not check.ok


def test_launcher_check_fails_when_the_launcher_is_missing(staged):
    (staged / "bin" / "shipit").unlink()
    check = selfcert._check_launcher(
        staged, _launcher_plan(staged), GOOD_SHA, execrun.run
    )
    assert not check.ok
    assert "bin/shipit" in check.detail


def test_launcher_probe_ignores_an_ambient_shipit_exec(staged, monkeypatch):
    # A dev session's SHIPIT_EXEC override precedes the pin parse in the
    # launcher; the probe must strip it or it would exec a build mid-install.
    monkeypatch.setenv("SHIPIT_EXEC", "/bin/echo")
    check = selfcert._check_launcher(
        staged, _launcher_plan(staged), GOOD_SHA, execrun.run
    )
    assert check.ok, check.detail


def test_launcher_check_makes_no_claim_over_a_declined_launcher(staged):
    # #600: a consumer that DECLINED bin/shipit keeps its own launcher — the
    # dogfood repo's source-deferring bootstrap has no SHIPIT_PIN_CHECK probe,
    # so running it would fail a postcondition over a file install does not
    # own. The check must skip: install delivered nothing to probe.
    launcher = staged / "bin" / "shipit"
    launcher.write_text("#!/usr/bin/env bash\nexit 99\n")  # would fail the probe
    plan = _launcher_plan(staged, declined=(iunits.SHIPIT_LAUNCHER_FILE,))
    check = selfcert._check_launcher(staged, plan, GOOD_SHA, execrun.run)
    assert check.ok, check.detail


# --------------------------------------------------------------------------
# certify — run ALL checks, aggregate every miss
# --------------------------------------------------------------------------


def _dispatching_runner(pin: str):
    """A fake Exec boundary satisfying every check: pixi solves, tools pass,
    the launcher probe answers the pin."""

    def runner(argv, **kw):
        if argv[0] == "bash":
            return _exec_ok(argv, stdout=pin + "\n")
        return _exec_ok(argv)

    return runner


def _live_hooks(root: Path) -> None:
    hooks = root / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    (hooks / "pre-commit").write_text("#!/bin/sh\n")
    (hooks / "pre-push").write_text("#!/bin/sh\n")


def test_certify_passes_all_four_postconditions_on_a_healthy_stage(staged):
    _live_hooks(staged)
    report = selfcert.certify(
        _plan_with_writes(staged),
        staged,
        hooks_activated=True,
        stamped_pin=GOOD_SHA,
        runner=_dispatching_runner(GOOD_SHA),
    )
    assert report.ok
    assert len(report.checks) == 4


def test_certify_collects_every_miss_never_fail_fast(staged):
    # Sabotage two postconditions at once: a planted bad lint outcome AND a
    # dead activation — the report must name BOTH.
    def runner(argv, **kw):
        if argv[0] == "bash":
            return _exec_ok(argv, stdout=GOOD_SHA + "\n")
        if "yamllint" in argv:
            return _exec_fail(argv, stdout="lefthook.yml:1:1 planted failure")
        return _exec_ok(argv)

    report = selfcert.certify(
        _plan_with_writes(staged),
        staged,
        hooks_activated=False,
        stamped_pin=GOOD_SHA,
        runner=runner,
    )
    assert not report.ok
    names = {c.name for c in report.failures}
    assert selfcert.CHECK_DELIVERED_LINT in names
    assert selfcert.CHECK_HOOKS in names


def test_certify_reports_malformed_config_across_postconditions(staged):
    # Moving lint behind its service boundary must not let ConfigError escape
    # self-certification: every postcondition still reports its own failure.
    (staged / config.CONFIG_NAME).write_text("not = valid = toml\n")
    _live_hooks(staged)

    report = selfcert.certify(
        _plan_with_writes(staged),
        staged,
        hooks_activated=True,
        stamped_pin=GOOD_SHA,
        runner=_dispatching_runner(GOOD_SHA),
    )

    assert not report.ok
    failures = {check.name: check.detail for check in report.failures}
    assert selfcert.CHECK_MANIFEST in failures
    assert selfcert.CHECK_DELIVERED_LINT in failures
    assert "scoped lint could not run" in failures[selfcert.CHECK_DELIVERED_LINT]


def test_format_failure_names_every_missed_postcondition():
    report = selfcert.CertReport(
        checks=(
            selfcert.CertCheck(selfcert.CHECK_MANIFEST, False, "no solve"),
            selfcert.CertCheck(selfcert.CHECK_LAUNCHER, True),
        )
    )
    text = selfcert.format_failure(report)
    assert "self-certification failed" in text
    assert f"FAIL {selfcert.CHECK_MANIFEST}" in text
    assert "no solve" in text
    # Only the failures are listed — the passing launcher check stays out.
    assert f"FAIL {selfcert.CHECK_LAUNCHER}" not in text
    assert "fix belongs in shipit" in text


# --------------------------------------------------------------------------
# The apply seam — fail closed BEFORE any git/gh side effect
# --------------------------------------------------------------------------


def _fail_report() -> selfcert.CertReport:
    return selfcert.CertReport(
        checks=(selfcert.CertCheck(selfcert.CHECK_DELIVERED_LINT, False, "planted"),)
    )


def test_sabotaged_install_fails_closed_with_no_pr(tmp_path, rec):
    plan = _plan(tmp_path)
    with pytest.raises(SelfCertError) as err:
        iapply.apply(
            plan,
            iapply.MODE_PR,
            pr_body=lambda *a: "body",
            certify=lambda *a, **kw: _fail_report(),
        )
    # Fail closed: the refusal names the miss, and NOTHING touched git/gh —
    # no branch, no commit, no push, no PR.
    assert selfcert.CHECK_DELIVERED_LINT in str(err.value)
    assert rec.calls == []
    # The refusal is an InstallError (the CLI error shell's known set) and
    # carries the step the failure event names (#434).
    assert isinstance(err.value, InstallError)
    assert err.value.step == "self-certification"


@pytest.mark.parametrize("mode", [iapply.MODE_LOCAL, iapply.MODE_PUSH])
def test_local_and_push_modes_also_certify(tmp_path, rec, mode):
    with pytest.raises(SelfCertError):
        iapply.apply(
            _plan(tmp_path),
            mode,
            certify=lambda *a, **kw: _fail_report(),
        )
    assert rec.calls == []


def test_default_tree_refresh_does_not_certify(tmp_path, rec):
    called = []
    plan = _plan(tmp_path)
    iapply.apply(
        plan,
        iapply.MODE_TREE,
        certify=lambda *a, **kw: called.append(1) or _fail_report(),
    )
    # The working-tree refresh publishes nothing: no certification, no git.
    assert called == []
    assert rec.calls == []


def test_healthy_install_certifies_then_opens_the_pr(tmp_path, rec):
    seen = {}

    def ok_cert(plan, root, *, hooks_activated, stamped_pin, **kw):
        seen["pin"] = stamped_pin
        return selfcert.CertReport(checks=(selfcert.CertCheck("stub", True),))

    result = iapply.apply(
        _plan(tmp_path),
        iapply.MODE_PR,
        pr_body=lambda before, hooks, rerendered, pin, debt: verb.format_pr_body(
            _plan(tmp_path),
            before,
            hooks,
            rerendered=rerendered,
            stamped_version=pin,
            lint_debt=debt,
        ),
        certify=ok_cert,
        debt=lambda root, **kw: 0,
    )
    # Certification saw the pin the manifest was stamped with, and the run
    # proceeded to the normal PR side effects.
    assert seen["pin"] == "testhash"
    assert result.pr_url is not None
    assert rec.names() == ["switch", "add", "commit", "push", "pr_create"]
    # The reconcile commit bypasses the repo's hooks (ADR-0033): the
    # whole-tree gate is the repo's bar, not install's.
    assert rec.commit_no_verify is True


def test_debt_laden_consumer_still_installs_with_debt_reported(tmp_path, rec):
    result = iapply.apply(
        _plan(tmp_path),
        iapply.MODE_PR,
        pr_body=lambda before, hooks, rerendered, pin, debt: verb.format_pr_body(
            _plan(tmp_path),
            before,
            hooks,
            rerendered=rerendered,
            stamped_version=pin,
            lint_debt=debt,
        ),
        certify=lambda *a, **kw: selfcert.CertReport(
            checks=(selfcert.CertCheck("stub", True),)
        ),
        debt=lambda root, **kw: 3,
    )
    # Debt is REPORTED, never a blocker: the PR opened, the body carries it.
    assert result.lint_debt == 3
    assert ("pr_create", True) in rec.calls
    assert "whole-tree lint currently red: 3 failing check(s)" in rec.pr_body
    assert "debt-clear pending" in rec.pr_body
    # And install's own git ops carried the hook bypass (#477): the debt never
    # gets a chance to block via the pre-push gate this run just armed.
    assert rec.commit_no_verify is True
    assert rec.push_no_verify is True


def test_pixi_lock_rides_the_reconcile_commit_when_present(tmp_path, rec):
    # The committed-lockfile decision (#439): the solve materializes pixi.lock;
    # a committing apply stages it with the managed set.
    (tmp_path / "pixi.lock").write_text("version: 6\n")
    iapply.apply(
        _plan(tmp_path),
        iapply.MODE_LOCAL,
        certify=lambda *a, **kw: selfcert.CertReport(
            checks=(selfcert.CertCheck("stub", True),)
        ),
    )
    assert "pixi.lock" in rec.commit_paths


def test_no_pixi_lock_means_no_extra_staged_path(tmp_path, rec):
    iapply.apply(
        _plan(tmp_path),
        iapply.MODE_LOCAL,
        certify=lambda *a, **kw: selfcert.CertReport(
            checks=(selfcert.CertCheck("stub", True),)
        ),
    )
    assert "pixi.lock" not in rec.commit_paths


# --------------------------------------------------------------------------
# consumer_debt — best-effort whole-tree count, never a raise
# --------------------------------------------------------------------------


def test_consumer_debt_counts_failing_checks(staged, monkeypatch):
    monkeypatch.setattr(lint, "_discover", lambda root: ["a.md", "b.yaml", "c.py"])

    def runner(argv, **kw):
        if "markdownlint" in argv or "yamllint" in argv:
            return _exec_fail(argv)
        return _exec_ok(argv)

    assert selfcert.consumer_debt(staged, runner=runner) == 2


def test_consumer_debt_zero_on_a_green_tree(staged, monkeypatch):
    monkeypatch.setattr(lint, "_discover", lambda root: ["a.md"])
    assert selfcert.consumer_debt(staged, runner=lambda a, **kw: _exec_ok(a)) == 0


def test_consumer_debt_is_none_when_unreadable(staged, monkeypatch):
    def boom(root):
        raise RuntimeError("no git")

    monkeypatch.setattr(lint, "_discover", boom)
    assert selfcert.consumer_debt(staged) is None


# --------------------------------------------------------------------------
# The launcher's SHIPIT_PIN_CHECK probe — the shipped script's contract
# --------------------------------------------------------------------------


def test_pin_check_probe_needs_no_uv_and_prints_the_pin(staged, monkeypatch):
    # An empty PATH-ish env: bash + coreutils only; uv absent by construction.
    import subprocess

    result = subprocess.run(
        ["bash", str(staged / "bin" / "shipit")],
        env={"PATH": "/usr/bin:/bin", "SHIPIT_PIN_CHECK": "1"},
        capture_output=True,
        text=True,
        cwd=staged,
        timeout=30,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == GOOD_SHA


def test_pin_check_probe_reads_a_crlf_manifest(staged):
    # A Windows checkout / core.autocrlf=true rewrites .shipit.toml with CRLF
    # line endings, so the `[shipit]` header arrives as `[shipit]\r`. The awk
    # `$0 == "[shipit]"` match never fires on that, leaving an empty pin and a
    # launcher exit 127. The launcher strips CR on the way in, so the probe
    # resolves the real pin regardless of line endings (#501).
    import subprocess

    cfg = staged / config.CONFIG_NAME
    crlf = cfg.read_text().replace("\n", "\r\n")
    cfg.write_bytes(crlf.encode())

    result = subprocess.run(
        ["bash", str(staged / "bin" / "shipit")],
        env={"PATH": "/usr/bin:/bin", "SHIPIT_PIN_CHECK": "1"},
        capture_output=True,
        text=True,
        cwd=staged,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == GOOD_SHA


def test_pin_check_probe_never_sigpipes_under_pipefail(staged):
    # Regression guard for #533: the pin parse must not `… | awk '…exit'`. Under
    # `set -euo pipefail` awk's early `exit` on the matched pin closes the pipe
    # while the upstream CR-strip is still writing; the upstream takes SIGPIPE,
    # the pipeline returns 141 (128 + SIGPIPE(13)), and `set -e` aborts the
    # launcher — an intermittent, timing-dependent flake. awk reading the file
    # directly leaves no second process to SIGPIPE.
    #
    # A multi-megabyte trailing blob AFTER the [shipit].version match guarantees
    # the upstream is still writing (its output far exceeds the OS pipe buffer)
    # when awk exits on the early match — so a piped implementation SIGPIPEs
    # deterministically, not just occasionally. The loop is belt-and-suspenders.
    import subprocess

    cfg = staged / config.CONFIG_NAME
    padded = (
        cfg.read_text()
        + "\n"
        + "# padding line to widen the SIGPIPE window\n" * 150_000
    )
    cfg.write_text(padded)

    for _ in range(10):
        result = subprocess.run(
            ["bash", str(staged / "bin" / "shipit")],
            env={"PATH": "/usr/bin:/bin", "SHIPIT_PIN_CHECK": "1"},
            capture_output=True,
            text=True,
            cwd=staged,
            timeout=30,
        )
        assert result.returncode == 0, (result.returncode, result.stderr)
        assert result.stdout.strip() == GOOD_SHA
