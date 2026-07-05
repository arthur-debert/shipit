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

import logging

import pytest

from shipit import git
from shipit.execrun import (
    CAUSE_EXIT,
    CAUSE_MISSING_BINARY,
    CAUSE_TIMEOUT,
    ExecError,
    ExecResult,
)
from shipit.identity import Sha


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
    # provisioning commit (#232) — and they come back as Sha VALUE OBJECTS
    # (PROC03), so the exclusion compares identities through the type.
    seen = {}

    def fake(args, *, cwd):
        seen["args"] = args
        return _ok(f"{'a' * 40}\n{'b' * 40}\n")

    monkeypatch.setattr(git, "_probe", fake)
    assert git.unpushed_shas(cwd="/x") == (Sha("a" * 40), Sha("b" * 40))
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


def test_push_no_verify_bypasses_the_pre_push_hook(monkeypatch):
    # #477: install's own push must carry `--no-verify` — the freshly-armed
    # pre-push hook runs the WHOLE-TREE lint gate, which a virgin consumer's
    # pre-existing debt fails, killing the very push that delivers the env to
    # clear it (the tripwire armed by the run that trips it). The flag sits
    # before the remote/branch operands, where git parses options.
    seen = {}

    def fake(args, *, cwd, timeout=None):
        seen["args"] = args
        return ""

    monkeypatch.setattr(git, "_git", fake)
    git.push("shipit/install", cwd="/x", force=True, no_verify=True)
    assert seen["args"] == [
        "push",
        "--force",
        "--no-verify",
        "origin",
        "shipit/install",
    ]


def test_push_default_does_not_bypass_hooks(monkeypatch):
    # The bypass is install's deliberate opt-in, never the adapter's default.
    seen = {}

    def fake(args, *, cwd, timeout=None):
        seen["args"] = args
        return ""

    monkeypatch.setattr(git, "_git", fake)
    git.push("main", cwd="/x")
    assert seen["args"] == ["push", "origin", "main"]


def test_commits_between_lists_the_range(monkeypatch):
    # `rev-list <base>..<head>`: exactly what provisioning committed (#232) — the
    # SHAs recorded into .git/shipit-provision.json at Tree birth. Typed at both
    # ends (PROC03): Sha endpoints in, Sha values out — the argv still carries
    # the plain string form.
    seen = {}

    def fake(args, *, cwd):
        seen["args"] = args
        return _ok(f"{'c' * 40}\n")

    monkeypatch.setattr(git, "_probe", fake)
    assert git.commits_between(Sha("a" * 40), Sha("c" * 40), cwd="/x") == [
        Sha("c" * 40)
    ]
    assert seen["args"] == ["rev-list", f"{'a' * 40}..{'c' * 40}"]


def test_commits_between_unreadable_is_none(monkeypatch):
    # A failed or malformed rev-list -> None, so the caller records NOTHING rather
    # than something wrong (an unrecorded provisioning commit only KEEPS the Tree).
    monkeypatch.setattr(git, "_probe", lambda args, *, cwd: _fail("bad ref"))
    assert git.commits_between(Sha("a" * 40), Sha("b" * 40), cwd="/x") is None
    monkeypatch.setattr(git, "_probe", lambda args, *, cwd: _ok("garbage\n"))
    assert git.commits_between(Sha("a" * 40), Sha("b" * 40), cwd="/x") is None


def test_head_commit_returns_a_sha_value_object(monkeypatch):
    # `rev-parse HEAD` is a commit-IDENTITY read (PROC03): the adapter returns
    # the validated Sha value object — lowercase-normalized by the type — never
    # a raw string.
    monkeypatch.setattr(git, "_probe", lambda args, *, cwd: _ok(f"{'AB' * 20}\n"))
    head = git.head_commit(cwd="/x")
    assert head == Sha("ab" * 20)
    assert isinstance(head, Sha)


def test_head_commit_unresolvable_or_malformed_is_none(monkeypatch):
    # Best-effort contract: a failed rev-parse (detached/unborn HEAD) AND output
    # that does not validate as a full sha both degrade to None, never raise.
    monkeypatch.setattr(git, "_probe", lambda args, *, cwd: _fail("unborn HEAD"))
    assert git.head_commit(cwd="/x") is None
    monkeypatch.setattr(git, "_probe", lambda args, *, cwd: _ok("not-a-sha\n"))
    assert git.head_commit(cwd="/x") is None


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


def test_epic_umbrella_exists_launch_failure_raises_not_false(monkeypatch):
    # WS03-FX01 alignment (#297): an absent ref is a normal answer (ok=False ->
    # False, above), but a launch-level failure (missing git, timeout) must NOT
    # read as "not an epic" — it propagates, and the fail-CLOSED WorktreeCreate
    # hook aborts the spawn loudly instead of silently minting a mis-based
    # epic-less holding branch.
    def boom(args, *, cwd):
        raise ExecError(["git", "show-ref"], rc=None, cause=CAUSE_MISSING_BINARY)

    monkeypatch.setattr(git, "_probe", boom)
    with pytest.raises(ExecError):
        git.epic_umbrella_exists("TRE04", cwd="/x")


# --- review-diff reads: typed commit-identity plumbing (PROC03-WS03) --------
#
# The review-diff endpoints are commit identities, so the adapter takes/returns
# `Sha` value objects (`commit_present` / `merge_base` / `diff_range` /
# `diff_name_only`) — the argv still carries the plain string form, and
# `fetch_ref` stays the one deliberately-str refspec seam. Pinned through the
# injected runner seam (`_git` / `_probe`), never a real subprocess.


def test_commit_present_takes_sha_and_probes_cat_file(monkeypatch):
    seen = {}

    def fake(args, *, cwd):
        seen["args"] = args
        return _ok()

    monkeypatch.setattr(git, "_probe", fake)
    assert git.commit_present(Sha("a" * 40), cwd="/x") is True
    # The typed identity stringifies only INTO the argv.
    assert seen["args"] == ["cat-file", "-e", f"{'a' * 40}^{{commit}}"]
    monkeypatch.setattr(git, "_probe", lambda args, *, cwd: _fail())
    assert git.commit_present(Sha("a" * 40), cwd="/x") is False


def test_merge_base_returns_a_sha_value_object(monkeypatch):
    # The merge base IS a commit identity, so it leaves the adapter typed
    # (PROC03) — lowercase-normalized by the Sha constructor.
    seen = {}

    def fake(args, *, cwd):
        seen["args"] = args
        return _ok(f"{'AB' * 20}\n")

    monkeypatch.setattr(git, "_probe", fake)
    base = git.merge_base(Sha("a" * 40), Sha("b" * 40), cwd="/x")
    assert base == Sha("ab" * 20)
    assert isinstance(base, Sha)
    assert seen["args"] == ["merge-base", "a" * 40, "b" * 40]


def test_merge_base_none_on_no_ancestor_or_malformed_output(monkeypatch):
    # Nonzero exit = no common ancestor (the caller fails loud); malformed
    # output degrades to the same None — nothing rather than something wrong.
    monkeypatch.setattr(git, "_probe", lambda args, *, cwd: _fail("no ancestor"))
    assert git.merge_base(Sha("a" * 40), Sha("b" * 40), cwd="/x") is None
    monkeypatch.setattr(git, "_probe", lambda args, *, cwd: _ok("not-a-sha\n"))
    assert git.merge_base(Sha("a" * 40), Sha("b" * 40), cwd="/x") is None


def test_diff_range_takes_sha_endpoints(monkeypatch):
    seen = {}

    def fake(args, *, cwd):
        seen["args"] = args
        return "the diff\n"

    monkeypatch.setattr(git, "_git", fake)
    out = git.diff_range(Sha("a" * 40), Sha("b" * 40), cwd="/x")
    assert out == "the diff\n"
    assert seen["args"] == ["diff", f"{'a' * 40}..{'b' * 40}"]


def test_diff_name_only_takes_sha_endpoints_and_parses_lines(monkeypatch):
    seen = {}

    def fake(args, *, cwd):
        seen["args"] = args
        return "a.py\n\nb/c.py\n"

    monkeypatch.setattr(git, "_git", fake)
    assert git.diff_name_only(Sha("a" * 40), Sha("b" * 40), cwd="/x") == [
        "a.py",
        "b/c.py",
    ]
    assert seen["args"] == ["diff", "--name-only", f"{'a' * 40}..{'b' * 40}"]


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


# --------------------------------------------------------------------------
# #353 — clone_dissociated fails open on a poisoned --reference donor.
# The real failure (git 2.54, reference with a split commit-graph chain) is
# pinned at the seam: the first Exec raises the exact ExecError shape the live
# diagnosis captured, and the adapter must retry ONCE without --reference.
# --------------------------------------------------------------------------


def _poisoned_clone_error(argv: list[str]) -> ExecError:
    # The live #353 signature: rc=128, "unable to parse commit" + git's
    # "Clone succeeded, but checkout failed." epilogue.
    return ExecError(
        argv,
        rc=128,
        stderr=(
            "fatal: unable to parse commit " + "a" * 40 + "\n"
            "warning: Clone succeeded, but checkout failed.\n"
            "You can inspect what was checked out with 'git status'\n"
        ),
        cause=CAUSE_EXIT,
    )


def test_clone_dissociated_retries_full_clone_on_poisoned_reference(
    monkeypatch, caplog
):
    calls: list[list[str]] = []

    def fake(args, *, cwd=None, timeout=None):
        calls.append(args)
        if "--reference" in args:
            raise _poisoned_clone_error(["git", *args])
        return ""

    monkeypatch.setattr(git, "_git", fake)
    with caplog.at_level(logging.WARNING, logger="shipit.git"):
        git.clone_dissociated("https://x/r.git", "/trees/leaf", reference="/ref")

    # Exactly two clones: the referenced attempt (with commit-graph READING off
    # for the clone process — the #372 fix, `-c` BEFORE the subcommand so nothing
    # persists in the new repo), then the bare full clone — no --reference (the
    # poison) and no --dissociate (meaningless without it).
    assert calls == [
        [
            "-c",
            "core.commitGraph=false",
            "clone",
            "--reference",
            "/ref",
            "--dissociate",
            "https://x/r.git",
            "/trees/leaf",
        ],
        ["clone", "https://x/r.git", "/trees/leaf"],
    ]
    # The degradation is narrated at WARNING with the poisoned reference path,
    # so the trail shows WHY this Tree birth was slow.
    warning = next(r for r in caplog.records if r.levelno == logging.WARNING)
    assert "/ref" in warning.getMessage()
    assert "#353" in warning.getMessage()


def test_clone_dissociated_removes_leftover_dest_before_retry(monkeypatch, tmp_path):
    # git leaves the cloned-but-not-checked-out dest behind on the #353 failure;
    # the retry must not trip over those leftovers ("destination path already
    # exists and is not an empty directory").
    dest = tmp_path / "leaf"

    def fake(args, *, cwd=None, timeout=None):
        if "--reference" in args:
            (dest / ".git").mkdir(parents=True)
            raise _poisoned_clone_error(["git", *args])
        assert not dest.exists(), "retry must start from a clean dest"
        return ""

    monkeypatch.setattr(git, "_git", fake)
    git.clone_dissociated("https://x/r.git", str(dest), reference="/ref")


def test_clone_dissociated_propagates_any_other_failure_without_retry(monkeypatch):
    # A genuinely failed clone (bad URL, auth, no space) is NOT the poisoned-
    # reference shape: it must propagate untouched, with no second clone attempt.
    calls: list[list[str]] = []

    def fake(args, *, cwd=None, timeout=None):
        calls.append(args)
        raise ExecError(
            ["git", *args],
            rc=128,
            stderr="fatal: repository not found",
            cause=CAUSE_EXIT,
        )

    monkeypatch.setattr(git, "_git", fake)
    with pytest.raises(ExecError):
        git.clone_dissociated("https://x/nope.git", "/trees/leaf", reference="/ref")
    assert len(calls) == 1


@pytest.mark.parametrize(
    "stderr",
    [
        # Checkout killed for a non-#353 reason (disk space, bad filename):
        # git prints the epilogue without the parse error.
        "warning: Clone succeeded, but checkout failed.",
        # Genuine object corruption without the checkout epilogue.
        "fatal: unable to parse commit " + "b" * 40,
    ],
)
def test_clone_dissociated_requires_both_markers_no_single_marker_retry(
    monkeypatch, stderr
):
    # The #353 signature is BOTH fragments together; either alone has innocent
    # causes and must propagate untouched — no dest removal, no full re-clone.
    calls: list[list[str]] = []

    def fake(args, *, cwd=None, timeout=None):
        calls.append(args)
        raise ExecError(["git", *args], rc=128, stderr=stderr, cause=CAUSE_EXIT)

    monkeypatch.setattr(git, "_git", fake)
    with pytest.raises(ExecError):
        git.clone_dissociated("https://x/r.git", "/trees/leaf", reference="/ref")
    assert len(calls) == 1


def test_clone_dissociated_never_retries_a_timeout(monkeypatch):
    # Even marker-looking partial output does not qualify when the child never
    # exited: retrying a full clone after a 10-minute hang would double the hang.
    calls: list[list[str]] = []

    def fake(args, *, cwd=None, timeout=None):
        calls.append(args)
        raise ExecError(
            ["git", *args],
            rc=None,
            stderr="warning: Clone succeeded, but checkout failed.",
            cause=CAUSE_TIMEOUT,
        )

    monkeypatch.setattr(git, "_git", fake)
    with pytest.raises(ExecError):
        git.clone_dissociated("https://x/r.git", "/trees/leaf", reference="/ref")
    assert len(calls) == 1


def test_configure_safe_reference_donor_writes_the_four_writer_knobs(monkeypatch):
    # The suspenders half of #353: BOTH commit-graph write flags AND the auto-
    # gc/auto-maintenance knobs (the live diagnosis proved the write flags alone
    # are not enough — `git gc --auto` regenerated the chain regardless).
    calls: list[tuple[list[str], str | None]] = []

    def fake(args, *, cwd=None, timeout=None):
        calls.append((args, cwd))
        return ""

    monkeypatch.setattr(git, "_git", fake)
    git.configure_safe_reference_donor(cwd="/trees/leaf")

    assert calls == [
        (["config", "--local", "fetch.writeCommitGraph", "false"], "/trees/leaf"),
        (["config", "--local", "gc.writeCommitGraph", "false"], "/trees/leaf"),
        (["config", "--local", "gc.auto", "0"], "/trees/leaf"),
        (["config", "--local", "maintenance.auto", "false"], "/trees/leaf"),
    ]
