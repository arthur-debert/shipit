"""`shipit ci` — the PR-time routing surface the ``wf-checks`` block calls.

``shipit ci plan`` is the ONE planner invocation behind the block's job matrix
(docs/legacy-prd/tol01-ci-tools.md story 15; ADR-0040: every decision the matrix
encodes comes out of the fixture-tested planner, the block routes and nothing
else). The pure planning — trigger ladder, thin/full scope, runner defaults,
setup-pixi env-set identity, cache descriptors — lives in
:mod:`shipit.tools.lanes`; this module is the effectful shell:

- the one config read: ``.shipit.toml [lanes]`` parsed to typed
  :class:`~shipit.config.Lane` values at the boundary (ADR-0030). NO lanes
  declared is a pointed :class:`~shipit.config.ConfigError` (a repo calling
  the checks block with nothing to run is a misconfiguration, fail closed) —
  distinct from a legitimately EMPTY plan (a thin PR dropping every scoped
  lane), which prints ``[]`` and exits 0.
- the path-diff, on ``pr`` events with a ``--base-ref``: through the git
  adapter (:func:`shipit.git.changed_paths_since`, the one git argv home,
  ADR-0028) against ``origin/<base-ref>`` — the block passes
  ``${{ github.base_ref }}`` verbatim (empty on non-PR events, where the flag
  is ignored anyway: full scope is forced). FAIL-SAFE: a diff git cannot
  answer plans FULL — uncertainty runs more checks, never fewer.
- the hand-off: the matrix as single-line JSON on STDOUT (the plan step pipes
  it into ``$GITHUB_OUTPUT``); the human-readable summary goes to stderr so
  the output contract stays machine-clean.
- the uniform exit contract (ADR-0030, story 8): 0 = planned (even ``[]``),
  1 = config/environment failure (via the shared :func:`~._errors.cli_errors`
  shell), 2 = usage (an event outside both vocabularies —
  :class:`~shipit.tools.lanes.LanePlanError`).
"""

from __future__ import annotations

import json
import logging
import sys
import tomllib
from collections.abc import Callable, Sequence
from pathlib import Path

import click

from .. import config, git
from ..tools import lanes as lanes_mod
from ._errors import cli_errors

logger = logging.getLogger("shipit.ci")

PixiTaskData = tuple[dict[str, tuple[str, ...]], dict[str, str]]


def missing_lanes_message() -> str:
    """The pointed error for a repo with no ``[lanes]`` declarations — the fix
    is a copy-paste away (the ordinary required+local checks)."""
    return (
        f"no [lanes] declared in {config.CONFIG_NAME} — `shipit ci plan` routes "
        "CI from that declaration (docs/legacy-prd/tol01-ci-tools.md story 14). "
        "Declare e.g.:\n"
        '  [lanes.lint]\n  run = "lint"\n  required = true\n  local = true\n'
        '  [lanes.test]\n  run = "test"\n  required = true\n  local = true'
    )


def _load_pixi_task_data(root: Path) -> PixiTaskData:
    """Pixi task provisioning + commands from ``pixi.toml``; absent = defaults."""
    pixi_path = root / "pixi.toml"
    if not pixi_path.is_file():
        return {}, {}
    try:
        with pixi_path.open("rb") as fh:
            pixi = tomllib.load(fh)
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise config.ConfigError(f"malformed {pixi_path}: {exc}") from None
    return lanes_mod.task_env_sets(pixi), lanes_mod.task_commands(pixi)


@click.group(name="ci")
def ci() -> None:
    """PR-time CI routing over the declared [lanes] (TOL01-WS05)."""


@ci.command(name="plan")
@click.option(
    "--event",
    "event",
    required=True,
    help="The CI event: pr/push/nightly/dispatch, or the GitHub event name "
    "(pull_request/push/schedule/workflow_dispatch) verbatim.",
)
@click.option(
    "--base-ref",
    "base_ref",
    default="",
    help="The PR base branch (GitHub's `github.base_ref`, e.g. `main`); the "
    "path-diff is taken against origin/<base-ref>. Empty or absent = diff "
    "unknown = full scope. Ignored on non-PR events (full is forced).",
)
def plan_cmd(event: str, base_ref: str) -> None:
    """Emit the CI job matrix for this repo's [lanes] as JSON on stdout.

    One planner invocation per workflow run: the wf-checks block's plan job
    captures the JSON as a job output and fans it into `pixi run <run>` matrix
    jobs. Exit: 0 planned (an empty `[]` matrix is a valid thin plan), 1 no/
    malformed [lanes], 2 unknown event.
    """
    raise SystemExit(run(event=event, base_ref=base_ref))


@cli_errors
def run(
    *,
    event: str,
    base_ref: str = "",
    changed_paths_fn: Callable[[str, str], Sequence[str] | None] | None = None,
) -> int:
    """Plan the matrix from the current directory. Returns 0/1/2.

    ``changed_paths_fn`` injects the git path-diff boundary for tests
    (``(base_ref, cwd) -> paths | None``); the default is the git adapter's
    :func:`~shipit.git.changed_paths_since` against ``origin/<base_ref>``.
    """
    root = Path(".").resolve()
    try:
        normalized = lanes_mod.normalize_event(event)
    except lanes_mod.LanePlanError as exc:
        # The usage tier (ADR-0030, rc 2): the invocation is wrong, and the
        # message is the whole diagnosis.
        print(f"error: {exc}", file=sys.stderr)
        # NB: `event` is the structured-log message key (ADR-0029), so the
        # rejected value rides a differently-named field.
        logger.error("ci plan invocation rejected", extra={"ci_event": event})
        return 2

    cfg_path = root / config.CONFIG_NAME
    cfg = config.load(cfg_path) if cfg_path.is_file() else {}
    lanes = config.load_lanes(cfg)
    if not lanes:
        raise config.ConfigError(missing_lanes_message())
    toolchains = config.load_toolchains(cfg)
    task_envs, task_cmds = _load_pixi_task_data(root)

    changed: Sequence[str] | None = None
    if normalized == lanes_mod.EVENT_PR and base_ref.strip():
        fetch = changed_paths_fn or (
            lambda ref, cwd: git.changed_paths_since(f"origin/{ref}", cwd=cwd)
        )
        changed = fetch(base_ref.strip(), str(root))
        if changed is None:
            # Fail-safe FULL: an unanswerable diff must run more, never fewer.
            print(
                f"ci plan: no diff against origin/{base_ref.strip()} — "
                "planning full scope",
                file=sys.stderr,
            )
            logger.warning(
                "ci plan path-diff unavailable; planning full scope",
                extra={"base_ref": base_ref.strip()},
            )

    jobs = lanes_mod.plan(
        lanes,
        event=normalized,
        changed_paths=changed,
        task_envs=task_envs,
        task_cmds=task_cmds,
        toolchains=toolchains,
    )
    print(json.dumps([job.as_matrix_entry() for job in jobs]))
    dropped = len(lanes) - len(jobs)
    names = ", ".join(job.name for job in jobs) if jobs else "none"
    print(
        f"ci plan: {normalized} -> {len(jobs)} of {len(lanes)} lane"
        f"{'s' if len(lanes) != 1 else ''}: {names}",
        file=sys.stderr,
    )
    logger.info(
        "ci plan complete",
        extra={
            "ci_event": normalized,
            "lanes": len(lanes),
            "jobs": len(jobs),
            "dropped": dropped,
            "diff_known": changed is not None,
        },
    )
    return 0
