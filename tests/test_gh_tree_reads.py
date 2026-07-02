"""Unit tests for the ``gh`` Tree-registry read helpers.

These pin the parsing/mapping the registry relies on — the ahead/behind left-right
order, upstream-absent → ``None`` / ``(0, 0)``, and the PR-snapshot shape — by
patching only the Exec seam (``_run`` / ``_run_probe`` / ``_git_probe``), never
the network. The registry reads are PROBES (``check=False`` through the Exec
runner, ADR-0028): a nonzero exit is a normal answer for a scan over the fleet,
so the fakes return an :class:`ExecResult` with the rc under test rather than
raising.
"""

from __future__ import annotations

import json

from shipit import gh
from shipit.execrun import ExecResult


def _ok(stdout: str = "") -> ExecResult:
    return ExecResult(argv=("git",), rc=0, stdout=stdout, stderr="", duration_ms=1)


def _fail(stderr: str = "", rc: int = 1) -> ExecResult:
    return ExecResult(argv=("git",), rc=rc, stdout="", stderr=stderr, duration_ms=1)


def test_git_ahead_behind_maps_left_right_to_behind_ahead(monkeypatch):
    # `rev-list --left-right --count @{upstream}...HEAD` prints "<behind> <ahead>".
    monkeypatch.setattr(gh, "_git_probe", lambda args, *, cwd: _ok("3\t5\n"))
    assert gh.git_ahead_behind(cwd="/x") == (5, 3)


def test_git_ahead_behind_no_upstream_is_level(monkeypatch):
    monkeypatch.setattr(
        gh, "_git_probe", lambda args, *, cwd: _fail("no upstream configured")
    )
    assert gh.git_ahead_behind(cwd="/x") == (0, 0)


def test_git_unpushed_shas_lists_the_local_only_commits(monkeypatch):
    # `rev-list HEAD --not --remotes`: commits on NO remote at all — the
    # upstream-independent "unpushed" the ephemeral gc ladder is defined over. The
    # SHAs (not a bare count) are what lets the ladder exclude exactly the recorded
    # provisioning commit (#232).
    seen = {}

    def fake(args, *, cwd):
        seen["args"] = args
        return _ok(f"{'a' * 40}\n{'b' * 40}\n")

    monkeypatch.setattr(gh, "_git_probe", fake)
    assert gh.git_unpushed_shas(cwd="/x") == ("a" * 40, "b" * 40)
    assert seen["args"] == ["rev-list", "HEAD", "--not", "--remotes"]


def test_git_unpushed_shas_empty_when_everything_is_on_a_remote(monkeypatch):
    # Empty output = every commit reachable from HEAD is on some remote: the
    # provably-safe reading, distinct from None (unreadable).
    monkeypatch.setattr(gh, "_git_probe", lambda args, *, cwd: _ok(""))
    assert gh.git_unpushed_shas(cwd="/x") == ()


def test_git_unpushed_shas_unreadable_is_none_not_empty(monkeypatch):
    # None (unknown) — NEVER () (provably pushed): the caller keeps on unknown, so
    # a git failure must not read as "nothing to lose". Malformed output (a line
    # that is not a SHA) is the same unreadable case.
    monkeypatch.setattr(gh, "_git_probe", lambda args, *, cwd: _fail("unborn HEAD"))
    assert gh.git_unpushed_shas(cwd="/x") is None
    monkeypatch.setattr(gh, "_git_probe", lambda args, *, cwd: _ok("not-a-sha\n"))
    assert gh.git_unpushed_shas(cwd="/x") is None


def test_git_commits_between_lists_the_range(monkeypatch):
    # `rev-list <base>..<head>`: exactly what provisioning committed (#232) — the
    # SHAs recorded into .git/shipit-provision.json at Tree birth.
    seen = {}

    def fake(args, *, cwd):
        seen["args"] = args
        return _ok(f"{'c' * 40}\n")

    monkeypatch.setattr(gh, "_git_probe", fake)
    assert gh.git_commits_between("a" * 40, "c" * 40, cwd="/x") == ["c" * 40]
    assert seen["args"] == ["rev-list", f"{'a' * 40}..{'c' * 40}"]


def test_git_commits_between_unreadable_is_none(monkeypatch):
    # A failed or malformed rev-list -> None, so the caller records NOTHING rather
    # than something wrong (an unrecorded provisioning commit only KEEPS the Tree).
    monkeypatch.setattr(gh, "_git_probe", lambda args, *, cwd: _fail("bad ref"))
    assert gh.git_commits_between("a" * 40, "b" * 40, cwd="/x") is None
    monkeypatch.setattr(gh, "_git_probe", lambda args, *, cwd: _ok("garbage\n"))
    assert gh.git_commits_between("a" * 40, "b" * 40, cwd="/x") is None


def test_git_upstream_ref_returns_tracking_ref(monkeypatch):
    monkeypatch.setattr(gh, "_git_probe", lambda args, *, cwd: _ok("origin/main\n"))
    assert gh.git_upstream_ref(cwd="/x") == "origin/main"


def test_git_upstream_ref_none_when_absent(monkeypatch):
    monkeypatch.setattr(gh, "_git_probe", lambda args, *, cwd: _fail("no upstream"))
    assert gh.git_upstream_ref(cwd="/x") is None


def test_pr_for_head_parses_snapshot(monkeypatch):
    payload = json.dumps(
        {"number": 12, "state": "OPEN", "isDraft": True, "baseRefName": "main"}
    )
    monkeypatch.setattr(gh, "_run_probe", lambda args, *, cwd=None: _ok(payload))
    assert gh.pr_for_head("issues/12/work", cwd="/x") == {
        "number": 12,
        "state": "OPEN",
        "isDraft": True,
        "baseRefName": "main",
    }


def test_pr_for_head_none_when_no_pr(monkeypatch):
    # gh's documented "no pull requests found" exit is a PROVABLE absence -> None,
    # distinct from UNKNOWN (an undetermined state).
    monkeypatch.setattr(
        gh,
        "_run_probe",
        lambda args, *, cwd=None: _fail('no pull requests found for branch "x"'),
    )
    assert gh.pr_for_head("issues/12/work", cwd="/x") is None


def test_pr_for_head_unknown_on_loose_no_pr_phrasing(monkeypatch):
    # The no-PR check keys on gh's PRECISE marker ("no pull requests found for
    # branch"), not the loose substring "no pull request": an unrelated failure
    # that merely mentions a pull request must stay UNDETERMINED (UNKNOWN), never
    # collapse to a provable absence (None) that would suppress the gc warning.
    monkeypatch.setattr(
        gh,
        "_run_probe",
        lambda args, *, cwd=None: _fail("could not resolve no pull request here"),
    )
    assert gh.pr_for_head("issues/12/work", cwd="/x") is gh.UNKNOWN


def test_pr_for_head_unknown_on_non_no_pr_error(monkeypatch):
    # Any OTHER gh failure (auth/network/rate-limit) leaves the state UNDETERMINED:
    # it returns the first-class UNKNOWN sentinel, NOT None ("no PR").
    monkeypatch.setattr(
        gh,
        "_run_probe",
        lambda args, *, cwd=None: _fail("HTTP 401: Bad credentials"),
    )
    assert gh.pr_for_head("issues/12/work", cwd="/x") is gh.UNKNOWN


def test_pr_for_head_unknown_when_output_not_json(monkeypatch):
    # A scan/read boundary over the whole fleet: malformed/non-JSON gh output
    # (warnings, prompts, garbage on stdout) must NOT crash `tree list`, but it is an
    # unreadable state -> UNKNOWN (distinct from None / "no PR"), not silently collapsed.
    monkeypatch.setattr(
        gh, "_run_probe", lambda args, *, cwd=None: _ok("not json at all")
    )
    assert gh.pr_for_head("issues/12/work", cwd="/x") is gh.UNKNOWN


def test_pr_for_head_unknown_when_output_empty(monkeypatch):
    # A clean run that yields no stdout is anomalous (a real PR always prints JSON):
    # treat it as an unreadable state, not "no PR".
    monkeypatch.setattr(gh, "_run_probe", lambda args, *, cwd=None: _ok("   "))
    assert gh.pr_for_head("issues/12/work", cwd="/x") is gh.UNKNOWN


def test_pr_for_head_unknown_when_not_a_dict(monkeypatch):
    # Valid JSON but not an object (a list / scalar) is malformed for this read.
    monkeypatch.setattr(gh, "_run_probe", lambda args, *, cwd=None: _ok("[1, 2, 3]"))
    assert gh.pr_for_head("issues/12/work", cwd="/x") is gh.UNKNOWN


def test_pr_for_head_unknown_when_dict_missing_fields(monkeypatch):
    # A JSON object that decoded cleanly but lacks the load-bearing fields (an empty
    # object) is NOT a usable snapshot: returning it would render as `#None None` in
    # `tree list`. Treat it as an unreadable state -> UNKNOWN.
    monkeypatch.setattr(gh, "_run_probe", lambda args, *, cwd=None: _ok("{}"))
    assert gh.pr_for_head("issues/12/work", cwd="/x") is gh.UNKNOWN


def test_pr_for_head_unknown_when_fields_wrong_type(monkeypatch):
    # number/state present but null (or otherwise mistyped) is the same malformed
    # case the `#None None` bug came from: number must be an int and state a str,
    # else the snapshot is undetermined -> UNKNOWN.
    payload = json.dumps({"number": None, "state": None, "isDraft": False})
    monkeypatch.setattr(gh, "_run_probe", lambda args, *, cwd=None: _ok(payload))
    assert gh.pr_for_head("issues/12/work", cwd="/x") is gh.UNKNOWN


def test_epic_umbrella_exists_checks_remote_tracking_ref_first(monkeypatch):
    # The semantic epic test: `<epic>/umbrella` present as the remote-tracking ref
    # (the usual shape in a clone) -> True, via an EXACT `show-ref --verify` (never a
    # pattern), and the remote ref is tried before any local head.
    seen: list = []

    def fake_git(args, *, cwd):
        seen.append(args)
        return _ok()  # `show-ref --verify --quiet` exits 0 when the ref resolves

    monkeypatch.setattr(gh, "_git_probe", fake_git)
    assert gh.epic_umbrella_exists("TRE04", cwd="/x") is True
    assert seen[0] == [
        "show-ref",
        "--verify",
        "--quiet",
        "refs/remotes/origin/TRE04/umbrella",
    ]


def test_epic_umbrella_exists_falls_back_to_local_head(monkeypatch):
    # No remote-tracking ref but a local `refs/heads/<epic>/umbrella` -> still True.
    def fake_git(args, *, cwd):
        if args[-1] == "refs/heads/TRE04/umbrella":
            return _ok()
        return _fail()

    monkeypatch.setattr(gh, "_git_probe", fake_git)
    assert gh.epic_umbrella_exists("TRE04", cwd="/x") is True


def test_epic_umbrella_exists_false_when_no_umbrella(monkeypatch):
    # Neither ref resolves (an ordinary `feature/foo` -> no `feature/umbrella`): the
    # probe reads the nonzero exit as "not an epic" rather than raising.
    monkeypatch.setattr(gh, "_git_probe", lambda args, *, cwd: _fail())
    assert gh.epic_umbrella_exists("feature", cwd="/x") is False


# --- remote_branch_exists: exact-ref equality (codex finding, gh.py:451) ---
#
# `git ls-remote` treats its final arg as a ref *pattern*, so the old
# `bool(non-empty output)` test could false-positive. These pin the helper to
# exact `refs/heads/<branch>` equality — the fail-closed precondition before
# Tree creation depends on it.


def _ls_remote_line(sha: str, refname: str) -> str:
    return f"{sha}\t{refname}\n"


def test_remote_branch_exists_true_when_exact_ref_present(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(args, *, cwd=None):
        calls.append(args)
        return _ls_remote_line("a" * 40, "refs/heads/TRE04/umbrella")

    monkeypatch.setattr(gh, "_run", fake_run)
    assert gh.remote_branch_exists("TRE04/umbrella", cwd="/x") is True
    # The query is for the FULLY-QUALIFIED ref, not the bare branch name.
    assert calls[0][-1] == "refs/heads/TRE04/umbrella"


def test_remote_branch_exists_false_when_absent(monkeypatch):
    # Empty ls-remote output (no matching head) -> absent.
    monkeypatch.setattr(gh, "_run", lambda args, *, cwd=None: "")
    assert gh.remote_branch_exists("TRE04/umbrella", cwd="/x") is False


def test_remote_branch_exists_false_for_glob_metachar_branch(monkeypatch):
    # A glob-ish name can never name a real git ref, so it must short-circuit to
    # False WITHOUT ever being sent to git as a pattern (which could expand to a
    # different head and false-positive).
    def boom(args, *, cwd=None):
        raise AssertionError("glob-ish branch name must not reach git ls-remote")

    monkeypatch.setattr(gh, "_run", boom)
    assert gh.remote_branch_exists("TRE04/*", cwd="/x") is False
    assert gh.remote_branch_exists("feat[01]", cwd="/x") is False
    assert gh.remote_branch_exists("feat?", cwd="/x") is False


def test_remote_branch_exists_false_when_only_a_different_ref_matches(monkeypatch):
    # Non-empty output but the refname is a DIFFERENT head than the one queried:
    # exact-equality parsing (not any-output) must reject it.
    def fake_run(args, *, cwd=None):
        return _ls_remote_line("b" * 40, "refs/heads/TRE04/umbrella-extra")

    monkeypatch.setattr(gh, "_run", fake_run)
    assert gh.remote_branch_exists("TRE04/umbrella", cwd="/x") is False


def test_remote_branch_exists_true_when_exact_ref_among_several(monkeypatch):
    # Several lines back; True iff one refname column equals the queried ref exactly.
    def fake_run(args, *, cwd=None):
        return _ls_remote_line(
            "c" * 40, "refs/heads/TRE04/umbrella-extra"
        ) + _ls_remote_line("d" * 40, "refs/heads/TRE04/umbrella")

    monkeypatch.setattr(gh, "_run", fake_run)
    assert gh.remote_branch_exists("TRE04/umbrella", cwd="/x") is True
