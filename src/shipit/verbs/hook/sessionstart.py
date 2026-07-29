"""``shipit hook sessionstart`` — write activation + log context into CLAUDE_ENV_FILE.

Every step fails open independently; the hook always exits 0.
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
from ...tree import layout

logger = logging.getLogger("shipit.hook")

ENV_FILE_VAR = "CLAUDE_ENV_FILE"

SOURCE_CLONE_WARNING = (
    "shipit: you launched a coordinator directly in the source clone — this "
    "session has no isolated Tree. Restart via ./agent-start claude for "
    "Claude Code or ./agent-start codex for Codex."
)

SHIPIT_REPO_SLUG = "arthur-debert/shipit"

SHIPIT_MAIN = "main"

CONTRACT_TEST_TASK = "test"


@click.command(name="sessionstart")
def cmd() -> None:
    """Write the repo's toolchain activation and log context into ``CLAUDE_ENV_FILE``."""
    raise SystemExit(run())


def run(
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    environ: dict[str, str] | None = None,
    runner=execrun.run,
    commits_ahead=None,
) -> int:
    """Parse stdin → advisories → write activation → export the log context → emit ``session.started``. Always returns 0."""
    env = environ if environ is not None else os.environ
    out = stdout if stdout is not None else sys.stdout
    try:
        raw = (stdin if stdin is not None else sys.stdin).read()
    except Exception:  # noqa: BLE001 — fail-open: no payload, nothing to do.
        logger.warning("sessionstart: could not read the payload", exc_info=True)
        return 0
    _warn_source_clone(raw, out)
    _warn_stale_pin(raw, out, commits_ahead or gh.commits_ahead)
    _warn_missing_test_task(raw, out)
    _write_activation(raw, env, runner)
    _write_log_context(raw, env)
    _emit_session_started(raw)
    return 0


def _warn_source_clone(raw: str, out: TextIO) -> None:
    """Warn on stdout when the session landed in a source clone rather than a Tree."""
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
    except Exception:  # noqa: BLE001 — fail-open, DEBUG by design
        logger.debug(
            "sessionstart: source-clone detection failed open — no warning emitted",
            exc_info=True,
        )


def _is_source_clone(cwd: Path) -> bool:
    """Whether ``cwd`` is a shipit source clone rather than a Tree (or neither)."""
    if not (cwd / config.CONFIG_NAME).is_file():
        return False
    if not (cwd / ".git").exists():
        return False
    return not cwd.resolve().is_relative_to(layout.central_root().resolve())


def _warn_stale_pin(raw: str, out: TextIO, commits_ahead) -> None:
    """One advisory line when the repo's shipit pin lags main; silent on any error."""
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
    except Exception:  # noqa: BLE001 — fail-open, DEBUG by design
        logger.debug(
            "sessionstart: staleness advisory failed open — nothing emitted",
            exc_info=True,
        )


def _warn_missing_test_task(raw: str, out: TextIO) -> None:
    """Warn when the pixi manifest declares no ``test`` task, which makes ``pixi run test`` fall through to the POSIX ``test`` builtin — a silent exit 1."""
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
    except Exception:  # noqa: BLE001 — fail-open, DEBUG by design
        logger.debug(
            "sessionstart: test-task advisory failed open — nothing emitted",
            exc_info=True,
        )


def _write_activation(raw: str, env, runner) -> None:
    """Toolchain → captured env → append to CLAUDE_ENV_FILE."""
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
    """Append the ``session``/``tree`` exports so every in-session command binds them."""
    env_file = env.get(ENV_FILE_VAR)
    if not env_file:
        logger.debug(
            "sessionstart: no %s in env — no log context exported", ENV_FILE_VAR
        )
        return
    cwd = _payload_cwd(raw)
    try:
        tree = _containing_tree(cwd)
    except Exception:  # noqa: BLE001 — fail-open, DEBUG by design
        logger.debug(
            "sessionstart: Tree detection failed open — no log context exported",
            exc_info=True,
        )
        return
    if tree is None:
        logger.debug("sessionstart: cwd is not inside a Tree — no log context exported")
        return
    session = session_current.current_session_id(env=env, cwd=cwd)
    if session is None:
        logger.debug(
            "sessionstart: no resolvable session id for %s — no log context exported",
            tree,
        )
        return
    try:
        _append(Path(env_file), _log_context_exports(tree, session))
        logger.debug(
            "sessionstart: exported log context session=%s into %s",
            session,
            env_file,
        )
    except Exception:  # noqa: BLE001 — fail-open: the export is additive
        logger.warning(
            "sessionstart: log-context export failed open (nothing written)",
            exc_info=True,
        )


def _containing_tree(cwd: Path) -> Path | None:
    """The resolved flat Tree dir containing ``cwd``, or None."""
    return session_current.containing_tree(cwd)


def _log_context_exports(tree: Path, session: str) -> str:
    """The shlex-quoted export lines for a session Tree: ``session`` and ``tree``."""
    return (
        f"export {logcontext.ENV_PREFIX}SESSION={shlex.quote(session)}\n"
        f"export {logcontext.ENV_PREFIX}TREE={shlex.quote(str(tree))}\n"
    )


def _emit_session_started(raw: str) -> None:
    """Emit the ``session.started`` dev-cycle event."""
    try:
        cwd = _payload_cwd(raw)
        tree = _containing_tree(cwd)
        session = session_current.current_session_id(cwd=cwd)
        sid = _payload_session_id(raw)
        codex_thread = os.environ.get("CODEX_THREAD_ID")
        backend = (
            ("codex" if codex_thread else "claude") if session is not None else None
        )
        native_ids = {
            **({"backend": backend} if backend else {}),
            **({"session_id": sid} if sid else {}),
            **({"codex_thread": codex_thread} if codex_thread else {}),
        }
        with logcontext.scoped(
            session=session, tree=str(tree) if tree is not None else None
        ):
            events.emit(
                logger,
                "session.started",
                "session started in %s",
                cwd,
                extra=native_ids or None,
            )
    except Exception:  # noqa: BLE001 — fail-open, DEBUG by design
        logger.debug(
            "sessionstart: session.started emission failed open", exc_info=True
        )


def _append(env_file: Path, text: str) -> None:
    """Append ``text``, rolling the env file back to its prior bytes on failure — a torn append would corrupt the preamble sourced before every Bash call."""
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
            logger.warning(
                "sessionstart: could not roll back partial append to %s",
                env_file,
                exc_info=True,
            )
        raise


def _payload_session_id(raw: str) -> str:
    """The payload's ``session_id``, or ``""`` when missing or malformed."""
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
    """The session's working dir from the payload, else the hook process's own cwd."""
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
