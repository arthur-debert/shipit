"""Unit tests for the git Tool adapter's parsed reads (PROC02-WS03).

These pin the parsing/mapping the registry, hooks, and Tree planner rely on —
the ahead/behind left-right order, upstream-absent → ``None`` / ``(0, 0)``, the
exact-ref ``ls-remote`` equality, and the porcelain line parse — by patching
only the Exec seam (``_git`` / ``_probe``), never a real subprocess. Most
registry reads are PROBES (``check=False`` through the Exec runner, ADR-0028):
a nonzero exit is a normal answer for a scan over the fleet, so the fakes
return an :class:`ExecResult` with the rc under test rather than raising.
"""

from __future__ import annotations

from shipit import git
from shipit.execrun import ExecResult


def _ok(stdout: str = "") -> ExecResult:
    return ExecResult(argv=("git",), rc=0, stdout=stdout, stderr="", duration_ms=1)


def _fail(stderr: str = "", rc: int = 1) -> ExecResult:
    return ExecResult(argv=("git",), rc=rc, stdout="", stderr=stderr, duration_ms=1)


def test_ahead_behind_maps_left_right_to_behind_ahead(monkeypatch):
    # `rev-list --left-right --count @{upstream}...HEAD` prints "<behind> <ahead>".
    monkeypatch.setattr(git, "_probe", lambda args, *, cwd: _ok("3\t5\n"))
    assert git.ahead_behind(cwd="/x") == (5, 3)


def test_ahead_behind_no_upstream_is_level(monkeypatch):
    monkeypatch.setattr(
        git, "_probe", lambda args, *, cwd: _fail("no upstream configured")
    )
    assert git.ahead_behind(cwd="/x") == (0, 0)


def test_unpushed_shas_lists_the_local_only_commits(monkeypatch):
    # `rev-list HEAD --not --remotes`: commits on NO remote at all — the
    # upstream-independent "unpushed" the ephemeral gc ladder is defined over. The
    # SHAs (not a bare count) are what lets the ladder exclude exactly the recorded
    # provisioning commit (#232).
    seen = {}

    def fake(args, *, cwd):
        seen["args"] = args
        return _ok(f"{'a' * 40}\n{'b' * 40}\n")

    monkeypatch.setattr(git, "_probe", fake)
    assert git.unpushed_shas(cwd="/x") == ("a" * 40, "b" * 40)
    assert seen["args"] == ["rev-list", "HEAD", "--not", "--remotes"]


def test_unpushed_shas_empty_when_everything_is_on_a_remote(monkeypatch):
    # Empty output = every commit reachable from HEAD is on some remote: the
    # provably-safe reading, distinct from None (unreadable).
    monkeypatch.setattr(git, "_probe", lambda args, *, cwd: _ok(""))
    assert git.unpushed_shas(cwd="/x") == ()


def test_unpushed_shas_unreadable_is_none_not_empty(monkeypatch):
    # None (unknown) — NEVER () (provably pushed): the caller keeps on unknown, so
    # a git failure must not read as "nothing to lose". Malformed output (a line
    # that is not a SHA) is the same unreadable case.
    monkeypatch.setattr(git, "_probe", lambda args, *, cwd: _fail("unborn HEAD"))
    assert git.unpushed_shas(cwd="/x") is None
    monkeypatch.setattr(git, "_probe", lambda args, *, cwd: _ok("not-a-sha\n"))
    assert git.unpushed_shas(cwd="/x") is None


def test_commits_between_lists_the_range(monkeypatch):
    # `rev-list <base>..<head>`: exactly what provisioning committed (#232) — the
    # SHAs recorded into .git/shipit-provision.json at Tree birth.
    seen = {}

    def fake(args, *, cwd):
        seen["args"] = args
        return _ok(f"{'c' * 40}\n")

    monkeypatch.setattr(git, "_probe", fake)
    assert git.commits_between("a" * 40, "c" * 40, cwd="/x") == ["c" * 40]
    assert seen["args"] == ["rev-list", f"{'a' * 40}..{'c' * 40}"]


def test_commits_between_unreadable_is_none(monkeypatch):
    # A failed or malformed rev-list -> None, so the caller records NOTHING rather
    # than something wrong (an unrecorded provisioning commit only KEEPS the Tree).
    monkeypatch.setattr(git, "_probe", lambda args, *, cwd: _fail("bad ref"))
    assert git.commits_between("a" * 40, "b" * 40, cwd="/x") is None
    monkeypatch.setattr(git, "_probe", lambda args, *, cwd: _ok("garbage\n"))
    assert git.commits_between("a" * 40, "b" * 40, cwd="/x") is None


def test_upstream_ref_returns_tracking_ref(monkeypatch):
    monkeypatch.setattr(git, "_probe", lambda args, *, cwd: _ok("origin/main\n"))
    assert git.upstream_ref(cwd="/x") == "origin/main"


def test_upstream_ref_none_when_absent(monkeypatch):
    monkeypatch.setattr(git, "_probe", lambda args, *, cwd: _fail("no upstream"))
    assert git.upstream_ref(cwd="/x") is None


def test_status_porcelain_parses_to_nonempty_lines(monkeypatch):
    # The centralized porcelain read: the adapter returns the PARSED lines (one
    # per dirty entry, blanks dropped), so callers ask truthiness/len of it
    # instead of re-splitting raw text at each site.
    monkeypatch.setattr(
        git, "_git", lambda args, *, cwd: " M src/a.py\n?? notes.txt\n\n"
    )
    assert git.status_porcelain(cwd="/x") == [" M src/a.py", "?? notes.txt"]
    monkeypatch.setattr(git, "_git", lambda args, *, cwd: "")
    assert git.status_porcelain(cwd="/x") == []


def test_epic_umbrella_exists_checks_remote_tracking_ref_first(monkeypatch):
    # The semantic epic test: `<epic>/umbrella` present as the remote-tracking ref
    # (the usual shape in a clone) -> True, via an EXACT `show-ref --verify` (never a
    # pattern), and the remote ref is tried before any local head.
    seen: list = []

    def fake_git(args, *, cwd):
        seen.append(args)
        return _ok()  # `show-ref --verify --quiet` exits 0 when the ref resolves

    monkeypatch.setattr(git, "_probe", fake_git)
    assert git.epic_umbrella_exists("TRE04", cwd="/x") is True
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

    monkeypatch.setattr(git, "_probe", fake_git)
    assert git.epic_umbrella_exists("TRE04", cwd="/x") is True


def test_epic_umbrella_exists_false_when_no_umbrella(monkeypatch):
    # Neither ref resolves (an ordinary `feature/foo` -> no `feature/umbrella`): the
    # probe reads the nonzero exit as "not an epic" rather than raising.
    monkeypatch.setattr(git, "_probe", lambda args, *, cwd: _fail())
    assert git.epic_umbrella_exists("feature", cwd="/x") is False


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

    def fake_run(args, *, cwd=None, timeout=None):
        calls.append(args)
        return _ls_remote_line("a" * 40, "refs/heads/TRE04/umbrella")

    monkeypatch.setattr(git, "_git", fake_run)
    assert git.remote_branch_exists("TRE04/umbrella", cwd="/x") is True
    # The query is for the FULLY-QUALIFIED ref, not the bare branch name.
    assert calls[0][-1] == "refs/heads/TRE04/umbrella"


def test_remote_branch_exists_false_when_absent(monkeypatch):
    # Empty ls-remote output (no matching head) -> absent.
    monkeypatch.setattr(git, "_git", lambda args, *, cwd=None, timeout=None: "")
    assert git.remote_branch_exists("TRE04/umbrella", cwd="/x") is False


def test_remote_branch_exists_false_for_glob_metachar_branch(monkeypatch):
    # A glob-ish name can never name a real git ref, so it must short-circuit to
    # False WITHOUT ever being sent to git as a pattern (which could expand to a
    # different head and false-positive).
    def boom(args, *, cwd=None, timeout=None):
        raise AssertionError("glob-ish branch name must not reach git ls-remote")

    monkeypatch.setattr(git, "_git", boom)
    assert git.remote_branch_exists("TRE04/*", cwd="/x") is False
    assert git.remote_branch_exists("feat[01]", cwd="/x") is False
    assert git.remote_branch_exists("feat?", cwd="/x") is False


def test_remote_branch_exists_false_when_only_a_different_ref_matches(monkeypatch):
    # Non-empty output but the refname is a DIFFERENT head than the one queried:
    # exact-equality parsing (not any-output) must reject it.
    def fake_run(args, *, cwd=None, timeout=None):
        return _ls_remote_line("b" * 40, "refs/heads/TRE04/umbrella-extra")

    monkeypatch.setattr(git, "_git", fake_run)
    assert git.remote_branch_exists("TRE04/umbrella", cwd="/x") is False


def test_remote_branch_exists_true_when_exact_ref_among_several(monkeypatch):
    # Several lines back; True iff one refname column equals the queried ref exactly.
    def fake_run(args, *, cwd=None, timeout=None):
        return _ls_remote_line(
            "c" * 40, "refs/heads/TRE04/umbrella-extra"
        ) + _ls_remote_line("d" * 40, "refs/heads/TRE04/umbrella")

    monkeypatch.setattr(git, "_git", fake_run)
    assert git.remote_branch_exists("TRE04/umbrella", cwd="/x") is True
