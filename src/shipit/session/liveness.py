"""``session/liveness`` — is the Claude Code session that owns a Tree still alive?

The liveness seam the ephemeral-Tree gc ladder reads (ADR-0027 Consequences). An
ephemeral session Tree has no PR, so the merged-PR ladder alone strands it; and it
is often *clean* (a planning session that never committed), so "clean + aged"
alone would delete a Tree out from under a live idle session. The tiebreaker is a
**pidfile** the ``SessionStart`` hook writes into the Tree recording the ``claude``
session's PID, its ``session_id``, and the PID's **OS process create-time** (read
from the OS at write time, not wall-clock "now" — the hook fires slightly after
the process starts, so the two are close but not equal).

The module mirrors the codebase's functional-core idiom (ADR-0021): the DECISION —
:func:`is_live` — is pure over an injectable **process probe**
(``probe(pid) -> ProcessInfo | None``), so the whole truth table (PID dead, PID
reused by a stranger, create-time drift, a ``node``-named live session) is
unit-tested with a faked probe; only :func:`os_probe` touches the OS. A Tree is
live when the PID is alive **and** the process's command line looks like Claude
Code **and** its create-time matches the recorded one within a small tolerance.

Two deliberate asymmetries, both from the ADR:

- **"Looks like Claude Code" matches the command line, never the process name.**
  Claude Code is a Node.js app, so the OS comm is usually ``node`` (or a
  versioned node path); asserting ``name == "claude"`` would misread every live
  session as dead. Argv is corroboration; the create-time — already a strong
  per-PID identity — is the primary signal.
- **PID reuse fails safe.** ``gc`` deletes directories, never processes: a false
  "alive" only lets a dead Tree linger until the hard time cap; a reused PID
  belonging to some other process fails the create-time (and argv) test and reads
  as dead — which is correct, and the dirty/unpushed floor still protects work.

The pidfile lives INSIDE the clone's ``.git`` directory, not the working tree: a
tracked-tree file would make every session Tree permanently *dirty* and thereby
unreclaimable (the gc ladder's absolute floor keeps dirty Trees), defeating the
entire mechanism. ``.git`` dies with the Tree, so the record's lifetime is the
Tree's by construction.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

import jc

from .. import execrun

logger = logging.getLogger("shipit.session")

#: How far apart (seconds) the recorded and the probed create-time may be while
#: still identifying the SAME process. The create-time is derived from ``ps``'s
#: one-second-granular ``etime`` plus a wall-clock read, and the write/read
#: sides each carry that rounding independently, so exact equality would
#: misread every live session; a few seconds absorbs that measurement
#: granularity while still making post-reboot PID reuse (minutes-to-days apart)
#: fail the match.
CREATE_TIME_TOLERANCE_SECONDS = 5.0

#: The pidfile's name inside the clone's ``.git`` directory (see module docstring
#: for why it must NOT live in the working tree: a tracked file would dirty the
#: Tree forever and the gc floor would never reclaim it).
PIDFILE_NAME = "shipit-session.json"

#: The npm package path that marks a Node-run Claude Code entrypoint
#: (``node …/node_modules/@anthropic-ai/claude-code/cli.js``): matched as a
#: substring of a single argv token, since the surrounding install prefix varies
#: per machine/node-version-manager.
_CLAUDE_ENTRYPOINT_MARKER = "@anthropic-ai/claude-code"

#: The unscoped entrypoint suffix (``…/claude-code/cli.js``) some install layouts
#: show instead of the full ``@anthropic-ai`` scope. Matched as a token SUFFIX —
#: the token must BE the cli.js entrypoint, so a stranger process that merely
#: lives under a directory named ``claude-code`` (e.g. a checkout of the repo)
#: does not read as the session.
_CLAUDE_ENTRYPOINT_SUFFIX = "claude-code/cli.js"

#: Executable basenames that ARE Claude Code: the ``claude`` shim/binary on PATH
#: (however deep its install prefix) and the ``claude-code`` alias some installs
#: use. Matched against a token's BASENAME exactly — never as a substring of the
#: whole command line, which would also match incidental ``.claude/…`` path
#: segments (hook scripts, ``.claude/shell-snapshots/…`` shell wrappers) and make
#: :func:`find_claude_process` record a short-lived intermediate instead of the
#: session (codex review).
_CLAUDE_EXECUTABLES = frozenset({"claude", "claude-code"})


@dataclass(frozen=True)
class ProcessInfo:
    """One process as the probe saw it — the whole OS surface ``is_live`` reads.

    ``create_time`` is the process's start time in epoch seconds (``None`` when
    the probe could not read it); ``argv`` is the full command line as one
    string. ``ppid`` exists for :func:`find_claude_process`'s ancestor walk.
    """

    pid: int
    ppid: int
    create_time: float | None
    argv: str


#: The injectable process-probe seam: ``probe(pid)`` returns the live process's
#: :class:`ProcessInfo`, or ``None`` when no such PID is alive (or it cannot be
#: read). Tests fake it; production passes :func:`os_probe`.
Probe = Callable[[int], ProcessInfo | None]


@dataclass(frozen=True)
class LivenessRecord:
    """The pidfile's contents: which process owns this Tree, identified strongly.

    ``pid`` alone is reusable after reboot/wraparound; ``create_time`` (the PID's
    OS process create-time, epoch seconds, read at WRITE time) makes the pair a
    practically unique process identity. ``session_id`` is Claude Code's own id
    for the session — not consulted by :func:`is_live` (the OS cannot be asked
    for it) but recorded so a human inspecting a Tree can join it back to a
    transcript.
    """

    pid: int
    session_id: str
    create_time: float


def looks_like_claude(argv: str) -> bool:
    """Whether ``argv`` reads as a Claude Code session's command line.

    Matches the command line, NEVER the OS process name — which for the
    Node.js-based Claude Code is usually ``node`` (ADR-0027). A token is a match
    when its basename is a Claude executable (:data:`_CLAUDE_EXECUTABLES` — the
    ``claude`` shim, wherever installed) or it carries the npm entrypoint path
    (:data:`_CLAUDE_ENTRYPOINT_MARKER`). Deliberately NOT a whole-argv substring
    test: ``claude`` appears incidentally in NON-session command lines — a hook
    script under ``.claude/hooks/…``, the ``zsh -c 'source
    ~/.claude/shell-snapshots/…'`` wrapper Claude Code runs commands through —
    and :func:`find_claude_process` must walk PAST those short-lived
    intermediates, not record them (a recorded hook/shell PID dies immediately
    and gc would misread the live session as dead).
    """
    for token in argv.lower().split():
        if _CLAUDE_ENTRYPOINT_MARKER in token:
            return True
        if token.endswith(_CLAUDE_ENTRYPOINT_SUFFIX):
            return True
        if token.rstrip("/").rsplit("/", 1)[-1] in _CLAUDE_EXECUTABLES:
            return True
    return False


def is_live(
    record: LivenessRecord,
    probe: Probe,
    *,
    tolerance: float = CREATE_TIME_TOLERANCE_SECONDS,
) -> bool:
    """Whether the session ``record`` describes is still alive — pure over ``probe``.

    Live means ALL of: the PID is alive (``probe`` found it), its command line
    looks like Claude Code (:func:`looks_like_claude` — argv, never the ``node``
    process name), and its create-time matches the recorded one within
    ``tolerance`` seconds (the primary per-PID identity). A dead PID, a PID
    reused by some other process (argv or create-time mismatch), or an unreadable
    create-time all read as NOT live — the safe direction: ``gc`` deletes
    directories, never processes, and the ladder's dirty/unpushed floor plus the
    grace window still protect a misread Tree's work.

    Each probe records its verdict AND the rung that decided it at DEBUG
    (mechanics — the gc decision built on it is the caller's milestone), so a
    surprising sweep is reconstructable from the log without re-probing.
    """
    info = probe(record.pid)
    if info is None:
        live, reason = False, "pid not alive"
    elif not looks_like_claude(info.argv):
        live, reason = False, "argv does not look like claude"
    elif info.create_time is None:
        live, reason = False, "create-time unreadable"
    elif abs(info.create_time - record.create_time) <= tolerance:
        live, reason = True, "pid and create-time match"
    else:
        live, reason = False, "create-time mismatch (pid reused)"
    logger.debug(
        "liveness probe: %s",
        reason,
        extra={
            "pid": record.pid,
            "session": record.session_id,
            "live": live,
            "rung": reason,
        },
    )
    return live


#: Upper bound on :func:`find_claude_process`'s parent walk. Any real chain is a
#: handful of hops (claude → shell → pixi → python …); the cap only guards
#: against a cyclic/ill-behaved probe fake.
_MAX_ANCESTOR_HOPS = 32


def find_claude_process(start_pid: int, probe: Probe) -> ProcessInfo | None:
    """The nearest ancestor of ``start_pid`` (inclusive) that IS Claude Code.

    The ``SessionStart`` hook runs as a great-grandchild of the session (claude →
    shell → ``pixi run`` → ``shipit``), so its own PID is not the one to record;
    this walks the ``ppid`` chain from ``start_pid`` upward and returns the first
    process whose command line looks like Claude Code — the session process the
    pidfile should name. ``None`` when the walk exhausts (reached init, a dead
    link, or the hop cap) without a match, so a caller launched OUTSIDE any
    Claude session records nothing rather than something wrong.
    """
    pid = start_pid
    for _ in range(_MAX_ANCESTOR_HOPS):
        if pid <= 1:
            return None
        info = probe(pid)
        if info is None:
            return None
        if looks_like_claude(info.argv):
            return info
        if info.ppid == pid:  # a probe fake or a pid-1-like self-parent: stop.
            return None
        pid = info.ppid
    return None


# --------------------------------------------------------------------------
# Pidfile I/O — the record's home inside the Tree's .git dir
# --------------------------------------------------------------------------


def pidfile_path(tree: str | Path) -> Path:
    """Where ``tree``'s liveness pidfile lives: ``<tree>/.git/shipit-session.json``.

    Inside ``.git`` deliberately: the working tree would show the pidfile as an
    untracked file, making every session Tree permanently DIRTY — and the gc
    ladder's absolute floor keeps dirty Trees, so the mechanism would strand the
    very Trees it exists to reclaim. ``.git`` is also removed with the Tree, so
    the record can never outlive its subject.
    """
    return Path(tree) / ".git" / PIDFILE_NAME


def write_pidfile(tree: str | Path, record: LivenessRecord) -> None:
    """Write ``record`` as ``tree``'s pidfile (overwriting a prior session's).

    Raises :class:`OSError` when the Tree has no ``.git`` directory to hold it
    (not a clone) or the write fails — the caller (the fail-open ``SessionStart``
    hook) decides that liveness is additive and swallows it.
    """
    path = pidfile_path(tree)
    if not path.parent.is_dir():
        raise FileNotFoundError(
            f"{path.parent} is not a directory — {tree} is not a git clone, "
            "so there is nowhere safe to record session liveness"
        )
    path.write_text(json.dumps(asdict(record), indent=2) + "\n", encoding="utf-8")
    # The mutation milestone: which session now owns this Tree — the pidfile
    # itself is the only other record, and it dies with the Tree.
    logger.info(
        "session pidfile written",
        extra={
            "tree": str(tree),
            "session": record.session_id,
            "pid": record.pid,
        },
    )


def read_pidfile(tree: str | Path) -> LivenessRecord | None:
    """The Tree's recorded :class:`LivenessRecord`, or ``None`` when unreadable.

    ``None`` covers every degenerate case — no pidfile, unreadable file, malformed
    JSON, missing or mis-typed fields — because the READER (the gc ladder) treats
    "no record" as "not live" and its liveness-independent rungs (the dirty floor,
    the grace window, the hard cap) carry the safety; a corrupt pidfile must never
    crash a fleet-wide sweep.
    """
    try:
        raw = pidfile_path(tree).read_text(encoding="utf-8")
        data = json.loads(raw)
        pid = data["pid"]
        session_id = data["session_id"]
        create_time = data["create_time"]
        if (
            isinstance(pid, int)
            and not isinstance(pid, bool)
            and isinstance(session_id, str)
            and isinstance(create_time, (int, float))
            and not isinstance(create_time, bool)
        ):
            return LivenessRecord(
                pid=pid, session_id=session_id, create_time=float(create_time)
            )
        logger.debug("liveness: pidfile for %s has mis-typed fields: %r", tree, data)
    except (OSError, ValueError, TypeError, KeyError):
        logger.debug("liveness: no readable pidfile for %s", tree, exc_info=True)
    return None


def remove_pidfile(tree: str | Path) -> None:
    """Best-effort removal of the Tree's pidfile (the fast-path teardown half).

    Missing-is-fine and errors are swallowed to a DEBUG log: the pidfile lives in
    ``.git`` and dies with the Tree anyway, so removal is a tidy-up, never
    load-bearing — the gc ladder's create-time check already defuses a stale one.
    """
    try:
        pidfile_path(tree).unlink()
    except FileNotFoundError:
        return  # missing-is-fine: nothing was removed, nothing to record.
    except OSError:
        logger.debug("liveness: could not remove pidfile for %s", tree, exc_info=True)
        return
    # The teardown half of the pidfile lifecycle — a real removal happened.
    logger.info("session pidfile removed", extra={"tree": str(tree)})


# --------------------------------------------------------------------------
# The real probe — the one effectful function in the module
# --------------------------------------------------------------------------

#: The ``ps`` columns the probe reads, each with a PINNED header (POSIX
#: ``keyword=HEADER`` renaming) so jc's header-driven table conversion sees the
#: same column names on macOS and Linux (whose default headers differ — e.g.
#: ``args`` prints as ``COMMAND`` under procps). Each pair rides its own ``-o``
#: flag: in a combined comma list, everything after the first ``=`` becomes ONE
#: header string. ``etime`` (elapsed ``[[dd-]hh:]mm:ss``) replaces ``lstart``
#: deliberately — it is purely numeric, so there is no locale-dependent
#: day/month rendering to pin (``LC_ALL=C``) or ``strptime`` (ADR-0028's jc
#: evaluation, issue #258).
_PS_COLUMNS = (("pid", "PID"), ("ppid", "PPID"), ("etime", "ELAPSED"), ("args", "ARGS"))


def os_probe(pid: int) -> ProcessInfo | None:
    """Read one live process from the OS: the production :data:`Probe`.

    Shells out to ``ps -p <pid>`` with the :data:`_PS_COLUMNS` format — portable
    across macOS and Linux, no psutil dependency (the runtime deps stay
    pure-python wheel pulls) — and hands the output to jc's ``ps`` converter
    (ADR-0028: harvest the most structured form; ``ps`` has no native JSON, so
    converted output is the top rung). The create-time is derived as ``now -
    etime`` — elapsed time is purely numeric, so the old locale-pinned
    ``lstart``/``strptime`` fragility is gone by construction. ``None`` when the
    PID is not alive or the output cannot be parsed; a row whose ``etime`` fails
    to parse still returns the process with ``create_time=None`` (alive,
    identity unverifiable — :func:`is_live` then reads it as not live, the safe
    direction).
    """
    if pid <= 0:
        return None
    argv = ["ps", "-p", str(pid)]
    for keyword, header in _PS_COLUMNS:
        argv += ["-o", f"{keyword}={header}"]
    result = execrun.run(argv, check=False)
    if result.rc != 0:
        return None
    return _parse_ps_output(result.stdout, now=time.time())


def _parse_ps_output(output: str, *, now: float) -> ProcessInfo | None:
    """Convert one header+row ``ps`` table into a :class:`ProcessInfo` — pure.

    jc's ``ps`` parser does the table shaping (header-keyed dicts, pid/ppid
    already ints; the trailing ``ARGS`` column keeps its embedded spaces —
    argv separators inside an argument are not recoverable from ``ps``, and the
    argv check is token-shaped anyway). ``None`` on a malformed table; a merely
    unparseable ``etime`` degrades to ``create_time=None`` rather than
    discarding a process that is demonstrably alive.
    """
    try:
        rows = jc.parse("ps", output, quiet=True)
    except Exception:  # noqa: BLE001 — a conversion crash is "unreadable", never a probe crash.
        logger.debug(
            "liveness: jc could not convert ps output %r", output, exc_info=True
        )
        return None
    if not isinstance(rows, list) or not rows:
        return None
    row = rows[0]
    pid, ppid = row.get("pid"), row.get("ppid")
    if not isinstance(pid, int) or not isinstance(ppid, int):
        return None
    elapsed = _elapsed_seconds(row.get("elapsed"))
    create_time = None if elapsed is None else now - elapsed
    return ProcessInfo(
        pid=pid, ppid=ppid, create_time=create_time, argv=str(row.get("args") or "")
    )


def _elapsed_seconds(etime: object) -> float | None:
    """``ps``'s ``etime`` (``[[dd-]hh:]mm:ss``) as seconds — purely numeric.

    ``None`` on anything that does not read as the POSIX elapsed-time shape;
    the caller degrades that to an unverifiable (not-live) identity.
    """
    if not isinstance(etime, str):
        return None
    days_part, dash, clock = etime.strip().partition("-")
    if not dash:
        days_part, clock = "0", etime.strip()
    parts = clock.split(":")
    if len(parts) not in (2, 3):
        return None
    try:
        days = int(days_part)
        fields = [int(part) for part in parts]
    except ValueError:
        return None
    if days < 0 or any(field < 0 for field in fields):
        return None
    hours, minutes, seconds = [0] * (3 - len(fields)) + fields
    return float(((days * 24 + hours) * 60 + minutes) * 60 + seconds)
