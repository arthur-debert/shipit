"""``session/liveness`` — the pure liveness decision + the pidfile round-trip.

``is_live`` is a pure function over an injectable process probe (ADR-0027), so the
whole truth table is driven directly with faked probes: PID dead, PID reused by a
stranger (argv mismatch), create-time drift beyond tolerance, a within-tolerance
match — and, the misread the ADR calls out explicitly, a live session whose process
NAME is ``node`` (Claude Code is a Node.js app) must still read as live because the
check matches the command line, never the name.
"""

from __future__ import annotations

import json

import pytest
from shipit.session import liveness
from shipit.session.liveness import (
    CREATE_TIME_TOLERANCE_SECONDS,
    LivenessRecord,
    ProcessInfo,
    find_claude_process,
    is_live,
    looks_like_claude,
    pidfile_path,
    read_pidfile,
    remove_pidfile,
    write_pidfile,
)

CREATED = 1_750_000_000.0

RECORD = LivenessRecord(pid=4242, session_id="c6010bf9-sess", create_time=CREATED)

#: The argv shape a REAL session shows: the OS name would be ``node``, and only
#: the command line betrays Claude Code (the ADR's node-named-session case).
NODE_ARGV = "node /usr/local/lib/node_modules/@anthropic-ai/claude-code/cli.js -w x"


def _probe_returning(info: ProcessInfo | None):
    """A probe that answers ``info`` for every PID (``None`` = nothing alive)."""

    def probe(pid: int) -> ProcessInfo | None:
        return info

    return probe


def _info(**over) -> ProcessInfo:
    base = dict(pid=4242, ppid=1, create_time=CREATED, argv=NODE_ARGV)
    base.update(over)
    return ProcessInfo(**base)


# --------------------------------------------------------------------------
# is_live — the pure truth table
# --------------------------------------------------------------------------


def test_pid_dead_is_not_live():
    assert is_live(RECORD, _probe_returning(None)) is False


def test_pid_alive_but_not_claude_is_not_live():
    # PID reuse: some other process now wears the PID; argv fails the
    # looks-like-claude corroboration even when the create-time happens to match.
    stranger = _info(argv="/usr/sbin/cupsd -l")
    assert is_live(RECORD, _probe_returning(stranger)) is False


def test_create_time_mismatch_is_not_live():
    # Same PID, claude-looking argv, but a create-time far outside tolerance —
    # a post-reboot PID reuse by another claude session. The create-time is the
    # per-PID identity, so this is NOT the recorded session.
    reused = _info(create_time=CREATED + 3_600)
    assert is_live(RECORD, _probe_returning(reused)) is False


def test_within_tolerance_match_is_live():
    drifted = _info(create_time=CREATED + CREATE_TIME_TOLERANCE_SECONDS - 1)
    assert is_live(RECORD, _probe_returning(drifted)) is True


def test_node_named_live_session_reads_as_live():
    # The ADR's explicit misread-guard: the process name is `node`, and ONLY the
    # command line carries `claude` — must read live (argv match, never comm).
    assert is_live(RECORD, _probe_returning(_info(argv=NODE_ARGV))) is True


def test_unreadable_create_time_is_not_live():
    # Alive + claude-looking but the probe could not read the start time: the
    # identity is unverifiable, so the safe answer is "not live" (gc deletes
    # dirs, never processes; the ladder's floor/grace still protect work).
    assert is_live(RECORD, _probe_returning(_info(create_time=None))) is False


def test_tolerance_boundary_is_inclusive():
    at = _info(create_time=CREATED + CREATE_TIME_TOLERANCE_SECONDS)
    past = _info(create_time=CREATED + CREATE_TIME_TOLERANCE_SECONDS + 0.5)
    assert is_live(RECORD, _probe_returning(at)) is True
    assert is_live(RECORD, _probe_returning(past)) is False


# --------------------------------------------------------------------------
# find_claude_process — the hook's ancestor walk
# --------------------------------------------------------------------------


def test_finds_claude_ancestor_through_the_hook_chain():
    # The realistic chain the SessionStart hook sees: shipit ← pixi ← claude.
    table = {
        100: _info(pid=100, ppid=90, argv="python -m shipit hook sessionstart"),
        90: _info(pid=90, ppid=80, argv="pixi run shipit hook sessionstart"),
        80: _info(pid=80, ppid=1, argv=NODE_ARGV),
    }
    found = find_claude_process(100, table.get)
    assert found is not None
    assert found.pid == 80
    assert found.create_time == CREATED


def test_walk_without_claude_ancestor_finds_nothing():
    # Launched outside any Claude session: the chain tops out at init with no
    # claude-looking ancestor -> None (record nothing rather than something wrong).
    table = {
        100: _info(pid=100, ppid=90, argv="python -m shipit hook sessionstart"),
        90: _info(pid=90, ppid=1, argv="/bin/zsh -l"),
    }
    assert find_claude_process(100, table.get) is None


def test_walk_stops_on_a_dead_link():
    table = {100: _info(pid=100, ppid=90, argv="python whatever")}
    assert find_claude_process(100, table.get) is None


def test_walk_survives_a_self_parenting_probe():
    # A degenerate probe (pid == ppid) must terminate, not loop forever.
    table = {100: _info(pid=100, ppid=100, argv="python whatever")}
    assert find_claude_process(100, table.get) is None


# --------------------------------------------------------------------------
# looks_like_claude — the argv matcher (token-shaped, never whole-argv substring)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        # The native shim/binary, bare or fully-pathed, wherever installed.
        "claude --dangerously-skip-permissions",
        "/usr/local/bin/claude --worktree sess-1",
        "/Users/x/.claude/local/claude",
        # The Node entrypoint: the process NAME is `node`; only argv betrays it.
        NODE_ARGV,
        "node /Users/x/.nvm/versions/node/v22.1.0/lib/node_modules/@anthropic-ai/claude-code/cli.js",
    ],
)
def test_real_session_argvs_look_like_claude(argv):
    assert looks_like_claude(argv) is True


@pytest.mark.parametrize(
    "argv",
    [
        # The short-lived intermediates the SessionStart hook's ancestor walk
        # crosses (codex review): an incidental `.claude/…` path segment must NOT
        # read as the session, or the walk records a PID that dies immediately
        # and gc misreads the live session as dead.
        "/bin/zsh -c source /Users/x/.claude/shell-snapshots/snapshot-zsh-17.sh"
        " && eval 'pixi run shipit hook sessionstart'",
        "python /Users/x/.claude/hooks/on-session-start.py",
        "sh -c $CLAUDE_PROJECT_DIR/.claude/hooks/run.sh",
        # Prefixes/suffixes of the executable name are strangers, not the shim.
        "/usr/local/bin/claudette --serve",
        "myclaude --help",
        "/usr/sbin/cupsd -l",
    ],
)
def test_incidental_claude_mentions_do_not_look_like_claude(argv):
    assert looks_like_claude(argv) is False


def test_walk_passes_through_a_dot_claude_shell_wrapper():
    # The chain as observed on a real machine: the hook's shell parent sources a
    # `.claude/shell-snapshots/…` script, and its argv must be walked PAST so the
    # recorded PID is the session's, not the shell's (codex regression).
    wrapper = "/bin/zsh -c source /Users/x/.claude/shell-snapshots/snap.sh && eval '…'"
    table = {
        100: _info(pid=100, ppid=90, argv="python -m shipit hook sessionstart"),
        90: _info(pid=90, ppid=80, argv=wrapper),
        80: _info(pid=80, ppid=1, argv="claude --dangerously-skip-permissions"),
    }
    found = find_claude_process(100, table.get)
    assert found is not None
    assert found.pid == 80


# --------------------------------------------------------------------------
# Pidfile round-trip — lives in .git, never the working tree
# --------------------------------------------------------------------------


@pytest.fixture
def tree(tmp_path):
    """A minimal Tree shape: a dir whose ``.git`` is a directory (a real clone)."""
    (tmp_path / ".git").mkdir()
    return tmp_path


def test_pidfile_lives_inside_dot_git(tree):
    # In the working tree it would dirty the Tree forever and the gc floor would
    # never reclaim it — the whole mechanism inverted.
    assert pidfile_path(tree).parent == tree / ".git"


def test_write_read_round_trip(tree):
    write_pidfile(tree, RECORD)
    assert read_pidfile(tree) == RECORD


def test_read_missing_pidfile_is_none(tree):
    assert read_pidfile(tree) is None


def test_read_corrupt_pidfile_is_none(tree):
    pidfile_path(tree).write_text("{not json", encoding="utf-8")
    assert read_pidfile(tree) is None


def test_read_mistyped_fields_is_none(tree):
    pidfile_path(tree).write_text(
        json.dumps({"pid": "4242", "session_id": 7, "create_time": "soon"}),
        encoding="utf-8",
    )
    assert read_pidfile(tree) is None


def test_write_into_a_non_clone_raises(tmp_path):
    # No .git dir -> nowhere safe to record; the fail-open hook swallows this.
    with pytest.raises(OSError):
        write_pidfile(tmp_path / "not-a-clone", RECORD)


def test_remove_pidfile_is_idempotent(tree):
    write_pidfile(tree, RECORD)
    remove_pidfile(tree)
    assert read_pidfile(tree) is None
    remove_pidfile(tree)  # second removal: missing-is-fine


# --------------------------------------------------------------------------
# The jc-converted ps table behind the real probe (pure — no live ps needed)
# --------------------------------------------------------------------------

#: The exact table shape `os_probe`'s pinned-header format produces: one header
#: line (the same names on macOS and Linux — that is what the pinning buys) and
#: one row whose trailing ARGS column keeps its embedded spaces.
PS_TABLE = (
    "  PID  PPID     ELAPSED ARGS\n"
    " 4242    80 05-18:13:55 node /x/claude-code/cli.js -w a\n"
)

NOW = 2_000_000_000.0


def test_parse_ps_output_extracts_identity_and_argv():
    info = liveness._parse_ps_output(PS_TABLE, now=NOW)
    assert info is not None
    assert (info.pid, info.ppid) == (4242, 80)
    # create-time is now minus the (numeric, locale-free) elapsed time.
    assert info.create_time == NOW - (((5 * 24 + 18) * 60 + 13) * 60 + 55)
    assert info.argv == "node /x/claude-code/cli.js -w a"


def test_parse_ps_output_rejects_garbage():
    assert liveness._parse_ps_output("", now=NOW) is None
    assert liveness._parse_ps_output("not a ps table", now=NOW) is None
    # A converted table with no pid/ppid identity is unusable, not a crash.
    assert liveness._parse_ps_output("garbage here\nmore garbage", now=NOW) is None


def test_parse_ps_output_bad_etime_degrades_to_unverifiable():
    # Alive but with an unparseable elapsed time -> create_time None (is_live
    # then reads it as not live), NOT a discarded row.
    table = "PID PPID ELAPSED ARGS\n4242 80 whenever node /x/claude-code/cli.js\n"
    info = liveness._parse_ps_output(table, now=NOW)
    assert info is not None
    assert info.create_time is None


@pytest.mark.parametrize(
    ("etime", "seconds"),
    [
        ("00:07", 7.0),  # mm:ss — a fresh process
        ("01:02:03", 3_723.0),  # hh:mm:ss
        ("05-18:13:55", ((5 * 24 + 18) * 60 + 13) * 60 + 55.0),  # dd-hh:mm:ss
    ],
)
def test_elapsed_seconds_reads_every_posix_etime_shape(etime, seconds):
    assert liveness._elapsed_seconds(etime) == seconds


@pytest.mark.parametrize("etime", [None, "", "55", "1:2:3:4", "aa:bb", "-1-00:00"])
def test_elapsed_seconds_rejects_non_etime_shapes(etime):
    assert liveness._elapsed_seconds(etime) is None


def test_os_probe_parses_a_faked_ps_through_jc(monkeypatch):
    # The probe end-to-end over a faked runner: the pinned-header argv goes out
    # (identical column names everywhere — no locale to pin, nothing to
    # strptime), the jc-converted row comes back as a ProcessInfo.
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd

        class R:
            rc = 0
            stdout = PS_TABLE

        return R()

    monkeypatch.setattr(liveness.execrun, "run", fake_run)
    info = liveness.os_probe(4242)
    assert seen["cmd"] == [
        "ps", "-p", "4242",
        "-o", "pid=PID", "-o", "ppid=PPID", "-o", "etime=ELAPSED", "-o", "args=ARGS",
    ]  # fmt: skip
    assert info is not None
    assert (info.pid, info.ppid) == (4242, 80)
    assert info.create_time is not None
    assert "claude" in info.argv


def test_os_probe_self_is_alive():
    # One live smoke test against the real ps: the current test process exists,
    # and its just-started elapsed time puts create_time within tolerance of now.
    import os
    import time

    info = liveness.os_probe(os.getpid())
    assert info is not None
    assert info.pid == os.getpid()
    assert info.create_time is not None
    assert info.create_time <= time.time()
