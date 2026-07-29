"""``tools/lanes`` — pure planner: (declared lanes, event, path-diff) → job matrix.

Applies the trigger ladder and the scope thin/full rule; ``run`` is opaque here.
"""

from __future__ import annotations

import posixpath
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .. import config
from . import legs, registry

#: The event vocabulary, in LADDER ORDER — most to least frequent. An event runs
#: every lane whose trigger sits at or before it on this ladder.
EVENT_PR = "pr"
EVENT_PUSH = "push"
EVENT_NIGHTLY = "nightly"
EVENT_DISPATCH = "dispatch"
EVENTS: tuple[str, ...] = (EVENT_PR, EVENT_PUSH, EVENT_NIGHTLY, EVENT_DISPATCH)

#: GitHub Actions event names → the planner vocabulary, so a workflow block can
#: pass ``${{ github.event_name }}`` verbatim.
GITHUB_EVENTS: dict[str, str] = {
    "pull_request": EVENT_PR,
    "push": EVENT_PUSH,
    "schedule": EVENT_NIGHTLY,
    "workflow_dispatch": EVENT_DISPATCH,
}

DEFAULT_RUNNER = "ubuntu-latest"


class LanePlanError(Exception):
    """An event outside the closed vocabulary; the message is the whole diagnosis."""


@dataclass(frozen=True)
class CacheDescriptor:
    rust: bool = False
    sccache: bool = False
    uv: bool = False

    def as_matrix_entry(self) -> dict[str, bool]:
        return {"rust": self.rust, "sccache": self.sccache, "uv": self.uv}


@dataclass(frozen=True)
class Job:
    """One emitted matrix entry: a lane routed to a CI job.

    ``runner`` is resolved (never ``None``). ``secrets`` names the block secret
    slots this lane opted into — slot NAMES only, never a token value.
    """

    name: str
    run: str
    runner: str
    required: bool
    envs: tuple[str, ...] = ("default",)
    caches: CacheDescriptor = CacheDescriptor()
    rust_workspaces: str = ""
    secrets: tuple[str, ...] = ()

    @property
    def envset(self) -> str:
        return "+".join(self.envs)

    def as_matrix_entry(
        self,
    ) -> dict[str, str | bool | list[str] | dict[str, bool]]:
        """``secrets`` rides as a JSON array so a ``contains`` gate is exact."""
        return {
            "name": self.name,
            "run": self.run,
            "runner": self.runner,
            "required": self.required,
            "envs": ",".join(self.envs),
            "envset": self.envset,
            "caches": self.caches.as_matrix_entry(),
            "rust_workspaces": self.rust_workspaces,
            "secrets": list(self.secrets),
        }


def normalize_event(raw: str) -> str:
    event = raw.strip()
    if event in EVENTS:
        return event
    if event in GITHUB_EVENTS:
        return GITHUB_EVENTS[event]
    github_only = (name for name in GITHUB_EVENTS if name not in EVENTS)
    known = ", ".join([*EVENTS, *github_only])
    raise LanePlanError(f"unknown event {raw!r}; known events: {known}")


def _triggered(lane: config.Lane, event: str) -> bool:
    return EVENTS.index(event) >= EVENTS.index(lane.trigger)


def _in_scope(path: str, scope: str) -> bool:
    prefix = scope.rstrip("/")
    if prefix in ("", "."):
        return True
    return path == prefix or path.startswith(prefix + "/")


def task_env_sets(pixi: Mapping[str, object]) -> dict[str, tuple[str, ...]]:
    task_features: dict[str, set[str]] = {}

    def add_task_names(tasks: object, feature: str) -> None:
        if not isinstance(tasks, Mapping):
            return
        for name in tasks:
            task_features.setdefault(str(name), set()).add(feature)

    add_task_names(pixi.get("tasks"), "default")
    features = pixi.get("feature")
    if isinstance(features, Mapping):
        for feature_name, feature_spec in features.items():
            if isinstance(feature_spec, Mapping):
                add_task_names(feature_spec.get("tasks"), str(feature_name))

    envs_by_feature: dict[str, set[str]] = {"default": {"default"}}
    environments = pixi.get("environments")
    if isinstance(environments, Mapping):
        for env_name, env_spec in environments.items():
            features_in_env: object
            if isinstance(env_spec, Mapping):
                features_in_env = env_spec.get("features", [])
            else:
                features_in_env = env_spec
            if not isinstance(features_in_env, list):
                continue
            for feature in features_in_env:
                if isinstance(feature, str):
                    envs_by_feature.setdefault(feature, set()).add(str(env_name))

    resolved: dict[str, tuple[str, ...]] = {}
    for task, owners in task_features.items():
        envs: set[str] = set()
        for owner in owners:
            envs.update(envs_by_feature.get(owner, {owner}))
        resolved[task] = tuple(sorted(envs))
    return resolved


def task_commands(pixi: Mapping[str, object]) -> dict[str, str]:
    commands: dict[str, str] = {}

    def add_task_commands(tasks: object) -> None:
        if not isinstance(tasks, Mapping):
            return
        for name, spec in tasks.items():
            command = spec
            if isinstance(spec, Mapping):
                command = spec.get("cmd")
            if isinstance(command, str):
                commands[str(name)] = command

    add_task_commands(pixi.get("tasks"))
    features = pixi.get("feature")
    if isinstance(features, Mapping):
        for feature_spec in features.values():
            if isinstance(feature_spec, Mapping):
                add_task_commands(feature_spec.get("tasks"))
    return commands


def _lane_task(run: str) -> str:
    parts = run.split()
    return parts[0] if parts else ""


def _shipit_invocation(
    run: str, task_cmds: Mapping[str, str] | None = None
) -> tuple[str, ...]:
    """Best-effort ``shipit`` argv behind a lane's run string; ``()`` when unrecognized."""
    try:
        parts = shlex.split(run)
    except ValueError:
        return ()
    if not parts:
        return ()
    if parts[0] in registry.TOOLS:
        return tuple(parts)
    command = (task_cmds or {}).get(parts[0])
    if command is None:
        return ()
    try:
        command_parts = shlex.split(command)
    except ValueError:
        return ()
    shipit_at: int | None = None
    for idx, token in enumerate(command_parts):
        if posixpath.basename(token) == "shipit":
            shipit_at = idx
    if shipit_at is None:
        return ()
    return (*command_parts[shipit_at + 1 :], *parts[1:])


def _leg_selector(invocation: Sequence[str]) -> str | None:
    for token in invocation[1:]:
        if token == "--":
            return None
        if token.startswith("-"):
            continue
        return token
    return None


def _rust_workspaces(rust_legs: Sequence[legs.Leg]) -> str:
    """Swatinem/rust-cache workspace mapping for rust legs using root target/."""
    entries: list[str] = []
    for leg in rust_legs:
        target = "target" if leg.path == "." else posixpath.relpath("target", leg.path)
        entries.append(f"{leg.path} -> {target}")
    return "\n".join(entries)


def _cache_descriptor(
    lane: config.Lane,
    toolchains: Sequence[config.ToolchainEntry],
    task_cmds: Mapping[str, str] | None = None,
) -> tuple[CacheDescriptor, str]:
    parts = _shipit_invocation(lane.run, task_cmds)
    tool = parts[0] if parts else ""
    if tool not in registry.TOOLS:
        return CacheDescriptor(), ""
    selector = _leg_selector(parts)
    try:
        planned = legs.plan_legs(toolchains, tool=tool, selector=selector)
    except legs.LegPlanError:
        # A bad selector fails loudly in the lane's own job; the advisory cache
        # descriptor must not make planning stricter than that.
        return CacheDescriptor(), ""
    rust_legs = [leg for leg in planned if leg.toolchain == "rust"]
    return CacheDescriptor(rust=bool(rust_legs)), _rust_workspaces(rust_legs)


def plan(
    lanes: Sequence[config.Lane],
    *,
    event: str,
    changed_paths: Sequence[str] | None = None,
    task_envs: Mapping[str, Sequence[str]] | None = None,
    task_cmds: Mapping[str, str] | None = None,
    toolchains: Sequence[config.ToolchainEntry] = (),
) -> tuple[Job, ...]:
    """The ordered job matrix for ``event`` over the declared ``lanes``.

    ``event`` must already be normalized. ``changed_paths`` (``None`` = unknown)
    only ever thins a ``pr`` plan; an empty matrix is a legitimate result.
    """
    if event not in EVENTS:
        raise ValueError(f"unnormalized event {event!r} reached the planner")
    jobs: list[Job] = []
    for lane in lanes:
        if not _triggered(lane, event):
            continue
        if (
            event == EVENT_PR
            and lane.scope is not None
            and changed_paths is not None
            and not any(_in_scope(p, lane.scope) for p in changed_paths)
        ):
            continue  # thin: the diff never enters this lane's subtree
        task = _lane_task(lane.run)
        envs = tuple((task_envs or {}).get(task, ("default",)))
        caches, rust_workspaces = _cache_descriptor(lane, toolchains, task_cmds)
        jobs.append(
            Job(
                name=lane.name,
                run=lane.run,
                runner=lane.runner or DEFAULT_RUNNER,
                required=lane.required,
                envs=envs,
                caches=caches,
                rust_workspaces=rust_workspaces,
                secrets=lane.secrets,
            )
        )
    return tuple(jobs)


def commit_push_checks(lanes: Sequence[config.Lane]) -> tuple[config.Lane, ...]:
    """The lanes both ``required`` and ``local``, in declaration order."""
    return tuple(lane for lane in lanes if lane.required and lane.local)
