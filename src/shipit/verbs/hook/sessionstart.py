"""``shipit hook sessionstart`` — the coordinator-activation boundary (ADR-0027).

THIN by design (mirrors ``hook pretooluse``); independent, additive steps per
session start — three writes, the advisory emits (the source-clone nudge, the
ADR-0033 pin-staleness line, the #444 missing-``test``-task line), and the
``session.started`` dev-cycle event (ADR-0032, :func:`_emit_session_started` —
the hook is the one verb that witnesses a session beginning):

1. **Activation** — detect the toolchain governing the session's ``cwd`` → capture
   pixi's activation (``pixi shell-hook --json`` via
   :func:`shipit.pixienv.shell_hook`) → render it (pure core:
   :mod:`shipit.harness.activation`) → APPEND the export lines to the file named
   by ``CLAUDE_ENV_FILE``, which Claude Code sources as a preamble before every
   Bash tool call. Result: the coordinator's environment is active for every Bash
   call with no wrapper — ``shipit``/``python`` resolve without a ``pixi run``
   prefix.
2. **Liveness** (SES02) — record which session owns this Tree: walk the
   hook's own ancestry to the session-host process — ``claude`` or ``codex``,
   whichever backend's SessionStart entry fired this verb (the hook runs as its
   descendant through the backend's managed hook command and any shell wrappers)
   — and write the :mod:`shipit.session.liveness` pidfile — PID, payload
   ``session_id``, and the PID's OS create-time, read NOW, at write time — into
   the Tree's ``.git`` dir.
   This is the signal the ephemeral-Tree gc ladder consults so an idle-but-live
   session's Tree is never reclaimed out from under it.
3. **Log-context export** (REL01 #349, ADR-0029) — when the session's ``cwd`` is
   an ephemeral session Tree, append ``export SHIPIT_LOG_CTX_SESSION=<id>`` (and
   the matching ``…_TREE=<path>``) to the same ``CLAUDE_ENV_FILE``. The ephemeral
   Tree's dir leaf IS the per-launch session id (ADR-0027) — the exact value
   ``tree/create.py`` binds at the Tree-birth seam — so every shipit command run
   *inside* the session (each one a fresh process) rebinds it at
   ``logsetup.configure_logging`` via :func:`shipit.logcontext.bind_from_env`,
   and the per-repo JSONL log becomes sliceable by session for the records that
   matter most: the ones emitted during the session. NOT the Claude-internal
   ``session_id`` UUID from the payload — that is a different identifier (the
   liveness pidfile records that one for the transcript join).
4. **Source-clone warning** (REL01 #348) — when the session's ``cwd`` is a shipit
   *source clone* (has ``.shipit.toml``, is a git repo) rather than a Tree (any
   dir under :func:`shipit.tree.layout.central_root`), print a one-line warning
   on stdout. A SessionStart hook's stdout is added to the session's context, so
   the coordinator sees it and can relay it; a WARNING log record rides along as
   the durable trail. The direct launch stays fully supported (``claude -w
   <name>`` without the launcher is an explicit path, per the ``claude-start``
   header) — this is a nudge, never a block. The discriminator is the PATH, not
   the branch: session Trees are *ephemeral-by-path, work-by-branch* (ADR-0027),
   so their branch moves off ``ephemeral/*`` mid-session and would false-positive,
   while every Tree kind (ephemeral, write, review) lives under the central root
   by construction.

**Fail-open is the contract** — the same posture as ``hook pretooluse``, the
OPPOSITE of ``hook worktreecreate``. All three writes are ADDITIVE, never
load-bearing: the managed hook commands keep running even without activation,
the gc ladder's liveness-independent rungs (the dirty/unpushed floor, the grace
window, the hard cap) carry teardown safety even with no pidfile, and a record
missing its ``session`` key is merely less sliceable, never lost. ANY failure in
any step (no ``CLAUDE_ENV_FILE``, bad payload, no toolchain, a pixi error, an
unwritable file, no session-host ancestor, a cwd that is no ephemeral Tree) must
therefore cost the session NOTHING: skip that write and exit 0 — and the steps
fail open INDEPENDENTLY, so a broken activation never costs the session its
liveness record or its log context, or vice versa. The source-clone warning is
fail-open too, with one deliberate calibration exception
(#348): a detection error skips at DEBUG, not the canon's WARNING — the check
writes nothing durable, so there is no degraded state to flag, and a broken
detection environment would otherwise WARN on every session start for a purely
advisory nudge. The log-context export's *detection* half shares that
calibration (#349: is this cwd an ephemeral Tree? — same path arithmetic, same
"would WARN every start in a broken environment" failure mode), while its
*write* half keeps the canon's WARNING like the other writes. Levels
follow the fail-open canon in :mod:`shipit.verbs.hook`: a swallowed exception is
a degraded-but-continuing outcome and logs at WARNING; a clean no-op (no
``CLAUDE_ENV_FILE``, no toolchain, no session-host ancestor, not a clone, not an
ephemeral Tree) is mechanics and stays at DEBUG.

The env file is opened in APPEND mode: ``CLAUDE_ENV_FILE`` is a shared seam other
SessionStart hooks may also write to, and this boundary owns only its own lines —
never the whole file.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import sys
import tomllib
from pathlib import Path
from typing import TextIO

import click

from ... import config, events, execrun, gh, logcontext
from ...harness import activation
from ...pixienv import shell_hook
from ...session import current as session_current
from ...session import liveness
from ...tree import layout

logger = logging.getLogger("shipit.hook")

#: The env var Claude Code sets to the file it sources before each Bash call.
ENV_FILE_VAR = "CLAUDE_ENV_FILE"

#: The advisory printed (stdout → session context) when the session lands in a
#: source clone instead of a Tree (REL01 #348). One line, actionable: the fix is
#: a relaunch through the launcher (or the equivalent bare ``claude -w``, which
#: fires the same WorktreeCreate isolation path).
SOURCE_CLONE_WARNING = (
    "shipit: you launched a coordinator directly in the source clone — this "
    "session has no isolated Tree. Restart via ./claude-start for Claude Code "
    "or ./codex-start for Codex."
)

#: The tool repo the ADR-0033 staleness advisory measures a consumer's pin
#: against — the same home the managed launcher's ``SHIPIT_GIT_URL`` points at.
SHIPIT_REPO_SLUG = "arthur-debert/shipit"

#: The branch a pin's lag is measured against.
SHIPIT_MAIN = "main"

#: The pixi task name the tooling contract requires (#444). ``pixi run test``
#: on a manifest WITHOUT it falls through to the POSIX ``test`` shell builtin —
#: silent exit 1, zero output on both streams, indistinguishable from a red
#: suite — so the absence is warned at session start rather than discovered as
#: a lying verification command mid-run.
CONTRACT_TEST_TASK = "test"


@click.command(name="sessionstart")
def cmd() -> None:
    """Write the repo's toolchain activation into ``CLAUDE_ENV_FILE`` + the pidfile.

    Reads the ``SessionStart`` payload as JSON on stdin. Always exits 0; each of
    the steps (activation, log-context export, liveness pidfile, session event,
    source-clone warning) fails OPEN independently on any error, and a repo with
    no activatable toolchain / no session-host ancestor / a cwd that is not a source
    clone or not an ephemeral Tree is a clean no-op for that check.
    """
    raise SystemExit(run())


def run(
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    environ: dict[str, str] | None = None,
    runner=execrun.run,
    probe: liveness.Probe | None = None,
    self_pid: int | None = None,
    commits_ahead=None,
) -> int:
    """Parse stdin → the advisories (source-clone cwd, stale pin, missing
    ``test`` task) → write activation → export the log context → write the
    liveness pidfile → emit the ``session.started`` event. Returns 0 always.

    ``stdout``, ``environ``, ``runner``, ``probe``, ``self_pid``, and
    ``commits_ahead`` are the injectable boundaries (defaults: the real
    ``sys.stdout`` / ``os.environ`` / :func:`shipit.execrun.run` /
    :func:`shipit.session.liveness.os_probe` / ``os.getpid()`` /
    :func:`shipit.gh.commits_ahead`) so tests assert every step without a live
    pixi, a real session-host process tree, or the network. Each check is wrapped fail-open on its own, so a
    bad payload, a pixi failure, an unwritable env file, a probe error, or a
    detection error can never crash the session — and a failure in one check
    never suppresses the others. The log-context export runs AFTER activation so
    its lines land after the pixi exports in the shared env file — but it does
    not depend on activation having succeeded (or on a toolchain existing).
    """
    env = environ if environ is not None else os.environ
    out = stdout if stdout is not None else sys.stdout
    try:
        raw = (stdin if stdin is not None else sys.stdin).read()
    except Exception:  # noqa: BLE001 — fail-open: no payload, nothing to do.
        logger.warning("sessionstart: could not read the payload", exc_info=True)
        return 0
    _warn_source_clone(raw, out)
    # Resolved at CALL time (not a bound default) so a patched gh boundary is
    # honored — the same late-binding stance as the pixienv runners.
    _warn_stale_pin(raw, out, commits_ahead or gh.commits_ahead)
    _warn_missing_test_task(raw, out)
    _write_activation(raw, env, runner)
    _write_log_context(raw, env)
    _write_liveness(raw, probe=probe, self_pid=self_pid)
    _emit_session_started(raw)
    return 0


def _warn_source_clone(raw: str, out: TextIO) -> None:
    """The advisory check: warn when the session landed in a source clone.

    A SessionStart hook's stdout is added to the session's context, so the line
    reaches the coordinator (and the transcript); the WARNING log record is the
    durable trail. Fail-open in isolation — but unlike the two writes, a
    detection error here skips at DEBUG, not WARNING (#348's explicit
    calibration): the check writes nothing durable, so there is no degraded
    state to flag, and a broken detection environment (e.g. a bad
    ``SHIPIT_TREES_ROOT``) would otherwise WARN on every session start for a
    purely advisory nudge.
    """
    try:
        cwd = _payload_cwd(raw)
        if not _is_source_clone(cwd):
            return
        out.write(SOURCE_CLONE_WARNING + "\n")
        logger.warning(
            "sessionstart: session launched directly in the source clone %s — "
            "no isolated Tree (restart via managed coordinator launcher)",
            cwd,
        )
    except Exception:  # noqa: BLE001 — fail-open, DEBUG by design: advisory-only,
        # nothing durable degrades when the detection itself breaks (see docstring).
        logger.debug(
            "sessionstart: source-clone detection failed open — no warning emitted",
            exc_info=True,
        )


def _is_source_clone(cwd: Path) -> bool:
    """Whether ``cwd`` is a shipit SOURCE CLONE rather than a Tree (or neither).

    A source clone has ``.shipit.toml`` at its root and is a git repo (``.git``
    dir or worktree file). What separates it from a Tree — which, being a clone
    of the same repo, carries both markers too — is the PATH: every Tree kind
    (ephemeral, write, review) lives under :func:`shipit.tree.layout.central_root`
    by construction ("the path IS the signal", ADR-0018/0027). The branch is NOT
    consulted: session Trees are *ephemeral-by-path, work-by-branch*, so their
    branch moves off ``ephemeral/*`` mid-session and would false-positive, and a
    git call would cost a subprocess where two stats do.

    Both sides are resolved before comparing so a symlinked home or central root
    (macOS ``/tmp`` → ``/private/tmp`` and friends) cannot split one dir into
    "inside" and "outside" spellings. Only the session root itself is checked —
    a launch from a SUBDIR of the clone is not detected; the payload ``cwd`` is
    the session's root, and a fail-open advisory prefers a false negative over a
    directory walk.
    """
    if not (cwd / config.CONFIG_NAME).is_file():
        return False
    if not (cwd / ".git").exists():
        return False
    return not cwd.resolve().is_relative_to(layout.central_root().resolve())


def _warn_stale_pin(raw: str, out: TextIO, commits_ahead) -> None:
    """The ADR-0033 staleness advisory: one line when the repo's pin lags main.

    Best-effort BY SPECIFICATION: staleness is surfaced, never enforced — with
    pin-wins execution, lag is a scheduling fact, not a hazard. Silent when the
    repo carries no valid pin (nothing to measure), silent when the pin is
    current, and silent at DEBUG on ANY error — no network, no gh auth, an
    unknown sha (the #348 advisory calibration: nothing durable degrades, and a
    broken network must not WARN on every session start). ``commits_ahead`` is
    the injected read boundary (:func:`shipit.gh.commits_ahead`), itself a
    probe that answers ``None`` rather than raising.
    """
    try:
        cwd = _payload_cwd(raw)
        pin = config.shipit_pin(cwd / config.CONFIG_NAME)
        if pin is None:
            logger.debug("sessionstart: no shipit pin — no staleness advisory")
            return
        behind = commits_ahead(SHIPIT_REPO_SLUG, pin, SHIPIT_MAIN)
        if behind is None:
            logger.debug("sessionstart: pin staleness unreadable — no advisory emitted")
            return
        if behind < 1:
            return
        out.write(
            f"shipit: pin {pin[:12]} is {behind} commit"
            f"{'s' if behind != 1 else ''} behind shipit main — the next "
            f"install reconcile PR catches this repo up (ADR-0033).\n"
        )
        logger.info(
            "sessionstart: shipit pin is stale",
            extra={"pin": pin, "behind": behind},
        )
    except Exception:  # noqa: BLE001 — fail-open, DEBUG by design: advisory-only,
        # never blocking, and a broken environment must not warn every start.
        logger.debug(
            "sessionstart: staleness advisory failed open — nothing emitted",
            exc_info=True,
        )


def _warn_missing_test_task(raw: str, out: TextIO) -> None:
    """The #444 advisory: warn when the manifest lacks the contract ``test`` task.

    The managed task block deliberately does NOT own ``test`` (repo-specific by
    the adoption PRD), so a consumer that never defined one hits the POSIX
    ``test``-builtin collision the moment anything runs the tooling contract's
    ``pixi run test`` — a silent exit 1 that reads as a red suite. The check
    looks in the root ``[tasks]`` table and every ``[feature.*.tasks]`` table;
    a repo with no ``pixi.toml`` at all is a clean no-op (not yet a pixi
    consumer — install seeds the manifest, not this advisory). Fail-open at
    DEBUG on any error (advisory calibration, like the other detections).
    """
    try:
        cwd = _payload_cwd(raw)
        manifest = cwd / "pixi.toml"
        if not manifest.is_file():
            logger.debug("sessionstart: no pixi.toml — no test-task advisory")
            return
        data = tomllib.loads(manifest.read_text(encoding="utf-8"))
        tasks = set(data.get("tasks", {}) or {})
        features = data.get("feature", {})
        if isinstance(features, dict):
            for feature in features.values():
                if isinstance(feature, dict):
                    tasks |= set(feature.get("tasks", {}) or {})
        if CONTRACT_TEST_TASK in tasks:
            return
        out.write(
            "shipit: pixi.toml defines no `test` task — `pixi run test` "
            "silently runs the POSIX `test` builtin (exit 1, no output; #444). "
            "Define a repo-specific `test` task before trusting the tooling "
            "contract's verification commands.\n"
        )
        logger.info(
            "sessionstart: manifest lacks the contract test task",
            extra={"manifest": str(manifest)},
        )
    except Exception:  # noqa: BLE001 — fail-open, DEBUG by design: an unreadable
        # manifest is pixi's problem to report, not this advisory's.
        logger.debug(
            "sessionstart: test-task advisory failed open — nothing emitted",
            exc_info=True,
        )


def _write_activation(raw: str, env, runner) -> None:
    """The activation half: toolchain → captured env → append to CLAUDE_ENV_FILE.

    Fail-open in isolation: any error logs at WARNING (the swallow is a degraded
    outcome) and writes nothing, without touching the liveness half.
    """
    try:
        env_file = env.get(ENV_FILE_VAR)
        if not env_file:
            logger.debug("sessionstart: no %s in env — nothing to write", ENV_FILE_VAR)
            return
        toolchain = activation.detect_toolchain(_payload_cwd(raw))
        if toolchain is None:
            logger.debug("sessionstart: no activatable toolchain — clean no-op")
            return
        captured = shell_hook(toolchain.manifest, runner=runner)
        script = activation.activation_script(toolchain, captured)
        if not script:
            return
        _append(Path(env_file), script + "\n")
        logger.debug(
            "sessionstart: wrote %s activation for %s into %s",
            toolchain.kind,
            toolchain.manifest,
            env_file,
        )
    except Exception:  # noqa: BLE001 — fail-open: activation is additive, never load-bearing.
        logger.warning(
            "sessionstart hook failed open (no activation written)", exc_info=True
        )


def _write_log_context(raw: str, env) -> None:
    """The log-context export: bind ``session``/``tree`` for every in-session command.

    When the session's cwd is an ephemeral session Tree, append
    ``export SHIPIT_LOG_CTX_SESSION=<id>`` (+ ``…_TREE=<path>``) to
    ``CLAUDE_ENV_FILE`` — sourced before EVERY Bash call, so each shipit command
    the session runs (a fresh process every time) rebinds the keys at
    ``configure_logging`` (:func:`shipit.logcontext.bind_from_env`) and its JSONL
    records carry the session they belong to (ADR-0029; REL01 #349).

    Fail-open in isolation, in two independently-calibrated halves: the
    *detection* (is this cwd an ephemeral Tree?) skips at DEBUG on any error —
    the same #348 calibration as the source-clone check, whose path arithmetic
    it shares — while the *write* logs at WARNING like the other writes (a
    swallowed append is a degraded outcome: the session's records lose their
    correlation key). The env-file gate comes FIRST, mirroring the activation
    half: without ``CLAUDE_ENV_FILE`` there is nowhere to write and nothing to
    detect.
    """
    env_file = env.get(ENV_FILE_VAR)
    if not env_file:
        logger.debug(
            "sessionstart: no %s in env — no log context exported", ENV_FILE_VAR
        )
        return
    try:
        tree = _ephemeral_tree(_payload_cwd(raw))
    except Exception:  # noqa: BLE001 — fail-open, DEBUG by design: same calibration
        # as the source-clone detection (a broken SHIPIT_TREES_ROOT would otherwise
        # WARN on every session start).
        logger.debug(
            "sessionstart: ephemeral-Tree detection failed open — "
            "no log context exported",
            exc_info=True,
        )
        return
    if tree is None:
        logger.debug(
            "sessionstart: cwd is not an ephemeral Tree — no log context exported"
        )
        return
    try:
        _append(Path(env_file), _log_context_exports(tree))
        logger.debug(
            "sessionstart: exported log context session=%s into %s",
            tree.name,
            env_file,
        )
    except Exception:  # noqa: BLE001 — fail-open: the export is additive; records
        # merely lose their session key, they are never lost.
        logger.warning(
            "sessionstart: log-context export failed open (nothing written)",
            exc_info=True,
        )


def _ephemeral_tree(cwd: Path) -> Path | None:
    """The RESOLVED ephemeral session-Tree dir when ``cwd`` is one, else ``None``.

    Delegates to :func:`shipit.session.current.ephemeral_session_tree` — the ONE
    path-is-the-signal detection (ADR-0018/0027), shared with the resolvers that
    read the id back (``shipit logs --session current``, LOG04) — so the
    exporter and every reader agree on what an ephemeral session Tree looks
    like by construction. Kept as a local seam so this hook's fail-open
    calibration (detection errors skip at DEBUG, per #348) stays wrapped around
    one call site.
    """
    return session_current.ephemeral_session_tree(cwd)


def _log_context_exports(tree: Path) -> str:
    """The export lines for an ephemeral Tree: ``session`` (the leaf) + ``tree``.

    The leaf name IS the per-launch session id (ADR-0027) — the same value the
    Tree-birth seam binds (``tree/create.py``), so in-session records join the
    creation records on one key. The var names come from
    :data:`shipit.logcontext.ENV_PREFIX` — the writer and the reader
    (:func:`shipit.logcontext.bind_from_env`) can never disagree on naming —
    and values are ``shlex``-quoted like every other line this hook sources.
    """
    return (
        f"export {logcontext.ENV_PREFIX}SESSION={shlex.quote(tree.name)}\n"
        f"export {logcontext.ENV_PREFIX}TREE={shlex.quote(str(tree))}\n"
    )


def _emit_session_started(raw: str) -> None:
    """The ``session.started`` dev-cycle event (ADR-0032 / LOG04-WS02).

    The SessionStart hook is the one verb that witnesses a session beginning,
    so the milestone emits here — for EVERY session (coordinator or spawned
    worker; a worker's ``epic``/``ws``/``agent``/``role`` ride in from the
    spawn seam's ``SHIPIT_LOG_CTX_*`` exports, rebound at this process's own
    logging setup). When the session's cwd is an ephemeral session Tree, the
    per-launch ``session``/``tree`` identity (ADR-0027: the dir leaf IS the
    session id) is bound SCOPED to this record — the same value
    ``_write_log_context`` exports for the session's later commands. Fail-open
    like every other step: the detection shares the #348/#349 DEBUG
    calibration (nothing durable degrades — one record merely goes untagged /
    less correlated), and the session never pays for a logging problem.
    """
    try:
        cwd = _payload_cwd(raw)
        tree = _ephemeral_tree(cwd)
        session = tree.name if tree is not None else None
        sid = _payload_session_id(raw)
        with logcontext.scoped(
            session=session, tree=str(tree) if tree is not None else None
        ):
            events.emit(
                logger,
                "session.started",
                "session started in %s",
                cwd,
                # The Claude-internal id joins the record to the transcript
                # (the liveness pidfile's companion); absent-not-null.
                extra={"session_id": sid} if sid else None,
            )
    except Exception:  # noqa: BLE001 — fail-open, DEBUG by design: the emit is
        # advisory correlation, nothing durable degrades when it breaks.
        logger.debug(
            "sessionstart: session.started emission failed open", exc_info=True
        )


def _write_liveness(
    raw: str, *, probe: liveness.Probe | None, self_pid: int | None
) -> None:
    """The liveness half: find the session-host ancestor, write the pidfile into the Tree.

    The recorded PID is NOT this hook's own — the hook runs below the session
    host through the backend's managed hook command and any shell wrappers — but
    the nearest ancestor whose command line looks like a session host (Claude
    Code or Codex, :func:`~shipit.session.liveness.find_session_process` — both
    backends' SessionStart entries route here); its create-time is read from the
    OS here, at write time, exactly as ADR-0027 specifies. Skipped
    cleanly (DEBUG log, no pidfile) when the session's cwd is not a git clone
    (nowhere durable to record), no session-host ancestor is found (launched
    outside a session), or the ancestor's create-time is unreadable (a record
    ``is_live`` could never verify would only ever read as dead). Fail-open in
    isolation.
    """
    try:
        tree = _payload_cwd(raw)
        if not (tree / ".git").is_dir():
            logger.debug("sessionstart: %s is not a clone — no pidfile written", tree)
            return
        info = liveness.find_session_process(
            self_pid if self_pid is not None else os.getpid(),
            probe if probe is not None else liveness.os_probe,
        )
        if info is None or info.create_time is None:
            logger.debug(
                "sessionstart: no session-host ancestor with a readable create-time "
                "— no pidfile written"
            )
            return
        record = liveness.LivenessRecord(
            pid=info.pid,
            session_id=_payload_session_id(raw),
            create_time=info.create_time,
        )
        liveness.write_pidfile(tree, record)
        logger.debug(
            "sessionstart: recorded session pid %s in %s",
            info.pid,
            liveness.pidfile_path(tree),
        )
    except Exception:  # noqa: BLE001 — fail-open: liveness is additive; the gc ladder's
        # liveness-independent rungs carry teardown safety without it.
        logger.warning(
            "sessionstart hook failed open (no pidfile written)", exc_info=True
        )


def _append(env_file: Path, text: str) -> None:
    """Append ``text``, rolling the env file back to its prior state on failure.

    The env file is sourced before EVERY subsequent Bash call, so a torn append
    (disk full, transient I/O error) is WORSE than none: a truncated ``export``
    line — an unterminated quote — would corrupt the whole session's preamble.
    "Write nothing" on failure therefore means exactly that: on any write error,
    best-effort restore the file to its pre-hook bytes (truncate back, or remove
    it if this hook created it), then re-raise into the fail-open boundary.
    """
    # One stat() answers existence AND size atomically — an exists()/stat() pair
    # would race a concurrent delete between the two calls (TOCTOU).
    try:
        original_size: int | None = env_file.stat().st_size
    except FileNotFoundError:
        original_size = None
    try:
        with open(env_file, "a", encoding="utf-8") as handle:
            handle.write(text)
    except Exception:
        try:
            if original_size is not None:
                os.truncate(env_file, original_size)
            else:
                env_file.unlink(missing_ok=True)
        except OSError:
            # A torn append that could not be rolled back may leave a corrupt
            # preamble — degraded-but-continuing, so WARNING per the canon.
            logger.warning(
                "sessionstart: could not roll back partial append to %s",
                env_file,
                exc_info=True,
            )
        raise


def _payload_session_id(raw: str) -> str:
    """The payload's ``session_id``, or ``""`` when missing/malformed.

    The id is recorded for a human joining a Tree back to its transcript — the
    liveness decision never consults it (the OS cannot be asked for it), so a
    missing id degrades to an empty string rather than blocking the pidfile.
    """
    try:
        payload = json.loads(raw)
        sid = payload.get("session_id") if isinstance(payload, dict) else None
        if isinstance(sid, str):
            return sid
    except ValueError:
        logger.warning(
            "sessionstart: unparseable payload — no session id recorded",
            exc_info=True,
        )
    return ""


def _payload_cwd(raw: str) -> Path:
    """The session's working dir from the payload, else the hook process's own cwd.

    Claude Code's ``SessionStart`` payload carries ``cwd`` (the session's root —
    the adopted session Tree once ADR-0027's ``--worktree`` launch lands). Hooks
    also RUN in the project dir, so a missing/malformed payload degrades to
    ``Path.cwd()`` rather than aborting — the manifest still resolves.
    """
    try:
        payload = json.loads(raw)
        cwd = payload.get("cwd") if isinstance(payload, dict) else None
        if isinstance(cwd, str) and cwd:
            return Path(cwd)
    except ValueError:
        logger.warning(
            "sessionstart: unparseable payload — falling back to cwd",
            exc_info=True,
        )
    return Path.cwd()
