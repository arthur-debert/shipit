from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from shipit import config, execrun, gh, git, lint
from shipit.install import apply as iapply
from shipit.install import reconcile as irec
from shipit.install import selfcert
from shipit.install import units as iunits
from shipit.install.errors import InstallError, SelfCertError
from shipit.verbs import install as verb

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

    def default_branch(self, *, cwd, remote="origin"):
        return "main"

    def fetch(self, *, cwd, remote="origin"):
        self.calls.append(("fetch", remote))

    def switch_create(self, branch, *, cwd):
        self.calls.append(("switch", branch))

    def reset_soft(self, ref, *, cwd):
        self.calls.append(("reset", ref))

    def read_tree(self, ref, *, cwd, index_file):
        self.calls.append(("read_tree", ref))

    def add(self, paths, *, cwd, index_file=None):
        self.calls.append(("add", tuple(paths)))

    def rm_cached(self, paths, *, cwd, index_file=None):
        self.calls.append(("rm_cached", tuple(paths)))

    def staged_paths(self, paths, *, cwd, index_file=None):
        return sorted(paths)

    def reset_index(self, *, cwd):
        self.calls.append(("reset_index", None))

    def commit(self, message, paths, *, cwd, no_verify=False):
        self.calls.append(("commit", message))
        self.commit_paths = tuple(paths)
        self.commit_no_verify = no_verify

    def commit_all(self, message, *, cwd, no_verify=False, index_file=None):
        self.calls.append(("commit", message))
        self.commit_all_message = message
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
    for name in (
        "switch_create",
        "read_tree",
        "add",
        "rm_cached",
        "staged_paths",
        "reset_index",
        "commit",
        "commit_all",
        "push",
        "current_branch",
        "default_branch",
        "fetch",
        "reset_soft",
    ):
        monkeypatch.setattr(git, name, getattr(r, name))
    for name in ("pr_url_for_head", "pr_create"):
        monkeypatch.setattr(gh, name, getattr(r, name))
    monkeypatch.setattr(iapply, "_shipit_version", lambda: "testhash")
    monkeypatch.setattr(iapply, "_activate_hooks", r.activate_hooks)
    return r


@pytest.fixture
def staged(tmp_path, rec):
    plan = _plan(tmp_path)
    iapply.apply(plan, iapply.MODE_TREE)
    cfg = tmp_path / config.CONFIG_NAME
    text = cfg.read_text()
    cfg.write_text(text.replace('version = "testhash"', f'version = "{GOOD_SHA}"'))
    return tmp_path


def test_delivered_lint_paths_are_whole_file_units_only(tmp_path, rec):
    plan = _plan(tmp_path)
    paths = selfcert.delivered_lint_paths(plan)
    assert "lefthook.yml" in paths
    assert "bin/shipit" in paths
    assert ".markdownlint.yaml" in paths
    assert "pixi.toml" not in paths
    assert "AGENTS.md" not in paths
    assert ".claude/settings.json" not in paths


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
    seen = {}

    def runner(argv, **kw):
        seen["argv"] = argv
        return _exec_ok(argv)

    assert selfcert._check_manifest(staged, runner).ok
    assert "--locked" not in seen["argv"]


def test_manifest_check_solves_under_a_scrubbed_env(staged, monkeypatch):
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


def test_delivered_lint_runs_each_tool_through_the_lint_env(staged):
    argvs = []

    def runner(argv, **kw):
        argvs.append(argv)
        return _exec_ok(argv)

    check = selfcert._check_delivered_lint(staged, _plan_with_writes(staged), runner)
    assert check.ok
    assert argvs, "the scoped run must actually invoke tools"
    for argv in argvs:
        assert argv[:2] == ["pixi", "run"]
        assert "--environment" in argv and "lint" in argv


def _plan_with_writes(root) -> irec.Plan:
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
        if "markdownlint" in argv:
            return _exec_fail(argv, stdout="skills/x.md:1 MD000 broken")
        return _exec_ok(argv)

    check = selfcert._check_delivered_lint(staged, _plan_with_writes(staged), runner)
    assert not check.ok
    assert "MD000" in check.detail or "FAIL" in check.detail


def test_delivered_lint_is_vacuous_with_no_whole_file_writes(tmp_path, rec):
    plan = irec.Plan(root=str(tmp_path), decisions=(), retired=(), seeds=())
    check = selfcert._check_delivered_lint(
        tmp_path, plan, lambda argv, **kw: _exec_fail(argv)
    )
    assert check.ok


def test_delivered_lint_fails_closed_when_a_planned_file_is_missing(staged):
    plan = _plan_with_writes(staged)
    (staged / "bin" / "shipit").unlink()
    check = selfcert._check_delivered_lint(
        staged, plan, lambda argv, **kw: _exec_ok(argv)
    )
    assert not check.ok
    assert "bin/shipit" in check.detail


def _skill_only_plan(root) -> irec.Plan:
    skills = [
        u
        for u in iunits.load_units()
        if u.key.startswith(f"{iunits.AGENTS_SKILLS_DIR}/")
    ]
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

    def runner(argv, *, cwd, **kw):
        real = argv[argv.index("--") + 1 :]
        kw.setdefault("check", False)
        return execrun.run(real, cwd=cwd, **kw)

    return runner


def test_managed_skill_files_are_in_the_delivered_lint_set(staged):
    paths = selfcert.delivered_lint_paths(_skill_only_plan(staged))
    assert ".agents/skills/grill-me-with-docs/SKILL.md" in paths
    assert ".agents/skills/to-spec/SKILL.md" in paths
    ignore = {
        line.strip()
        for line in (staged / ".markdownlintignore").read_text().splitlines()
    }
    assert ".shipit-skills/" not in ignore
    assert ".agents/skills/" not in ignore
    assert ".claude/skills/" not in ignore


@pytest.mark.skipif(shutil.which("markdownlint") is None, reason="no markdownlint")
def test_delivered_skill_files_pass_the_delivered_config_real(staged):
    check = selfcert._check_delivered_lint(
        staged, _skill_only_plan(staged), _unwrapping_real_runner()
    )
    assert check.ok, check.detail


@pytest.mark.skipif(shutil.which("markdownlint") is None, reason="no markdownlint")
def test_delivered_lint_catches_a_planted_skill_defect_real(staged):
    skill = staged / ".agents" / "skills" / "grill-me-with-docs" / "SKILL.md"
    skill.write_text(skill.read_text() + "\n```\nplanted bare fence\n```\n")
    check = selfcert._check_delivered_lint(
        staged, _skill_only_plan(staged), _unwrapping_real_runner()
    )
    assert not check.ok
    assert "MD040" in check.detail
    assert ".agents/skills/grill-me-with-docs/SKILL.md" in check.detail


def test_hooks_check_requires_a_successful_activation(staged):
    plan = _plan_with_writes(staged)
    check = selfcert._check_hooks(staged, plan, hooks_activated=False)
    assert not check.ok
    assert "hook activation did not succeed" in check.detail
    assert "./bin/shipit install" in check.detail
    assert "lefthook install" not in check.detail


def test_hooks_check_requires_the_hook_files_on_disk(staged):
    subprocess.run(["git", "init", "-q"], cwd=staged, check=True)
    plan = _plan_with_writes(staged)
    check = selfcert._check_hooks(staged, plan, hooks_activated=True)
    assert not check.ok

    hooks = staged / ".git" / "hooks"
    (hooks / "pre-commit").write_text("#!/bin/sh\n# lefthook\n")
    (hooks / "pre-push").write_text("#!/bin/sh\n# lefthook\n")
    assert selfcert._check_hooks(staged, plan, hooks_activated=True).ok


def test_hooks_check_fails_when_the_hooks_dir_cannot_be_resolved(staged, monkeypatch):
    monkeypatch.setattr(git, "hooks_dir", lambda *, cwd: None)
    plan = _plan_with_writes(staged)
    check = selfcert._check_hooks(staged, plan, hooks_activated=True)
    assert not check.ok
    assert "could not be resolved" in check.detail


def test_hooks_check_reads_the_shared_hooks_dir_from_a_linked_worktree(tmp_path):
    main = tmp_path / "main"
    main.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=main, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.co"], cwd=main, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=main, check=True)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-qm", "init"], cwd=main, check=True
    )
    wt = tmp_path / "wt"
    subprocess.run(["git", "worktree", "add", "-q", str(wt)], cwd=main, check=True)
    shared = main / ".git" / "hooks"
    (shared / "pre-commit").write_text("#!/bin/sh\n# lefthook\n")
    (shared / "pre-push").write_text("#!/bin/sh\n# lefthook\n")

    plan = _plan_with_writes(wt)
    assert selfcert._check_hooks(wt, plan, hooks_activated=True).ok


def test_hooks_check_is_vacuous_when_the_plan_activates_nothing(tmp_path, rec):
    plan = irec.Plan(root=str(tmp_path), decisions=(), retired=(), seeds=())
    assert selfcert._check_hooks(tmp_path, plan, hooks_activated=None).ok


def test_hooks_check_is_vacuous_when_activation_was_not_attempted(tmp_path, rec):
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


def _launcher_plan(root, declined: tuple[str, ...] = ()) -> irec.Plan:
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
    monkeypatch.setenv("SHIPIT_EXEC", "/bin/echo")
    check = selfcert._check_launcher(
        staged, _launcher_plan(staged), GOOD_SHA, execrun.run
    )
    assert check.ok, check.detail


def test_launcher_check_makes_no_claim_over_a_declined_launcher(staged):
    launcher = staged / "bin" / "shipit"
    launcher.write_text("#!/usr/bin/env bash\nexit 99\n")
    plan = _launcher_plan(staged, declined=(iunits.SHIPIT_LAUNCHER_FILE,))
    check = selfcert._check_launcher(staged, plan, GOOD_SHA, execrun.run)
    assert check.ok, check.detail


def _dispatching_runner(pin: str):

    def runner(argv, **kw):
        if argv[0] == "bash":
            return _exec_ok(argv, stdout=pin + "\n")
        return _exec_ok(argv)

    return runner


def _live_hooks(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    hooks = root / ".git" / "hooks"
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
    assert f"FAIL {selfcert.CHECK_LAUNCHER}" not in text
    assert "fix belongs in shipit" in text


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
    assert selfcert.CHECK_DELIVERED_LINT in str(err.value)
    assert rec.calls == []
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
    assert seen["pin"] == "testhash"
    assert result.pr_url is not None
    assert rec.names() == [
        "fetch",
        "switch",
        "reset",
        "read_tree",
        "add",
        "rm_cached",
        "commit",
        "push",
        "pr_create",
    ]
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
    assert result.lint_debt == 3
    assert ("pr_create", True) in rec.calls
    assert "whole-tree lint currently red: 3 failing check(s)" in rec.pr_body
    assert "debt-clear pending" in rec.pr_body
    assert rec.commit_no_verify is True
    assert rec.push_no_verify is True


def test_pixi_lock_rides_the_reconcile_commit_when_present(tmp_path, rec):
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


def test_pin_check_probe_needs_no_uv_and_prints_the_pin(staged, monkeypatch):
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
