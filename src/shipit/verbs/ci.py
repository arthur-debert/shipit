"""`shipit ci` — the PR-time routing surface the ``wf-checks`` block calls."""

from __future__ import annotations

import json
import logging
import sys
import tomllib
from collections.abc import Callable, Sequence
from pathlib import Path

import click

from .. import config, git, logcontext, workenv
from ..tools import lanes as lanes_mod
from ._errors import cli_errors

logger = logging.getLogger("shipit.ci")

PixiTaskData = tuple[dict[str, tuple[str, ...]], dict[str, str]]


def missing_lanes_message() -> str:
    """The pointed error for a repo with no ``[lanes]`` declarations."""
    return (
        f"no [lanes] declared in {config.CONFIG_NAME} — `shipit ci plan` routes "
        "CI from that declaration (docs/legacy-prd/tol01-ci-tools.md story 14). "
        "Declare e.g.:\n"
        '  [lanes.lint]\n  run = "lint"\n  required = true\n  local = true\n'
        '  [lanes.test]\n  run = "test"\n  required = true\n  local = true'
    )


def _load_pixi_task_data(root: Path) -> PixiTaskData:
    """Pixi task provisioning and commands from ``pixi.toml``; absent means defaults."""
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
    """PR-time CI routing over the declared [lanes]."""


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
    """Emit the CI job matrix for this repo's [lanes] as JSON on stdout."""
    raise SystemExit(run(event=event, base_ref=base_ref))


@cli_errors
def run(
    *,
    event: str,
    base_ref: str = "",
    changed_paths_fn: Callable[[str, str], Sequence[str] | None] | None = None,
) -> int:
    """Plan the matrix from the current directory; returns 0, 1 or 2."""
    root = Path(".").resolve()
    try:
        normalized = lanes_mod.normalize_event(event)
    except lanes_mod.LanePlanError as exc:
        print(f"error: {exc}", file=sys.stderr)
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
    bound = logcontext.bound()
    for job in jobs:
        logger.info(
            "ci lane work env resolved — pixi-run routing for lane %s",
            job.name,
            extra=workenv.ci_lane_resolution_record(
                working_dir=str(root),
                repo=bound.get("repo"),
                lane=job.name,
                pixi_environment_name=job.envset,
                ci_event=normalized,
                runner=job.runner,
                required=job.required,
            ),
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
