"""The lane planner — pure: (declared lanes, event, path-diff) → job matrix.

A **Lane** is the declaration of one CI test unit (CONTEXT.md Build & release;
``.shipit.toml [lanes]``, parsed to :class:`shipit.config.Lane` at the boundary
per ADR-0030). :func:`plan` is where the enforcement vocabulary becomes
executable (docs/prd/tol01-ci-tools.md stories 13–16): it maps the typed
declarations, the CI event, and a path-diff to the ORDERED job matrix the
``wf-checks`` workflow block fans into jobs — the lane-side twin of the release
planner (preflight), same pure-core shape, PR-time axis. Every decision the
matrix encodes is made HERE, fixture-tested; the block carries zero logic
beyond routing (ADR-0040).

The three rules, in the order they apply:

- **Trigger ladder** — a lane's ``trigger`` names the MOST FREQUENT event that
  runs it; every rarer event also runs it (``pr`` < ``push`` < ``nightly`` <
  ``dispatch``, :data:`EVENTS` order). So ``trigger = "pr"`` lanes — the
  ordinary checks — run on every PR update, every push, the nightly schedule,
  and a manual dispatch; ``trigger = "nightly"`` reserves an expensive lane
  for the coverage events (nightly + dispatch); ``trigger = "dispatch"`` is
  manual-only. This is what makes nightly/dispatch comprehensive: they sit at
  the rare end of the ladder, so everything scheduled-worthy runs there.
- **Scope thin/full** — ``scope`` names a lane's related subtree (a
  repo-relative path prefix; ``"."`` = the whole tree). On a ``pr`` event with
  a KNOWN path-diff, a scoped lane is dropped when the diff never enters its
  subtree — the *thin* plan for an unrelated PR (glossary **Scope**: "thin
  runs the minimal set"). Full is FORCED on every non-PR event and whenever
  the diff is unknown (``changed_paths=None``): uncertainty runs MORE, never
  less, so coverage survives without taxing every PR (story 16). Unscoped
  lanes always run.
- **Routing/provisioning fields** — each emitted :class:`Job` carries the lane's ``run``
  string (the pixi task invocation the block executes: ``pixi run <run>``,
  landing in the same shipit verb a laptop runs, ADR-0039), its ``runner``
  (:data:`DEFAULT_RUNNER` when undeclared), and its ``required`` flag — the
  merge-blocking verdict travels with the job so the ``wf-checks`` block can
  run an advisory (``required = false``) lane WITHOUT feeding its failure to
  the stable ``check`` verdict (the block reads the flag; the decision was
  made here). It also carries the setup-pixi env-set identity and static cache
  descriptors (CI-cache spike #582): YAML installs ``matrix.envs`` and gates
  static cache steps on ``matrix.caches.*``; it never infers toolchain policy.
  Declaration order is preserved — ``.shipit.toml`` order is the matrix order,
  no re-sorting (the leg planner's contract, :mod:`shipit.tools.legs`).

``run`` is treated OPAQUELY here: it names a shipit tool or Leg invocation
(``"test"``, ``"test rust"``, ``"changelog check"``) whose tool may land in a
later work stream (build — WS02, e2e — WS03), so the planner never validates
it against today's registry; a bad ``run`` fails loudly in the emitted job,
never silently unrouted.

:func:`commit_push_checks` derives the OTHER face of the same declarations:
the commit/push checks are exactly the required∩local lanes (story 13, the
glossary's **Commit/push checks**) — one definition for lefthook and CI, so
the hooks and the matrix can never drift into two transcriptions of policy.

Pure (no I/O, no Exec): fully fixture-testable, the same split the lint
verb's ``route``/``verdict`` pair and the leg planner use. The effectful
shell — config read, git path-diff, JSON emission — is :mod:`shipit.verbs.ci`.
"""

from __future__ import annotations

import posixpath
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .. import config
from . import legs, registry

#: The event vocabulary, in LADDER ORDER — most to least frequent. The same
#: closed set as the lane ``trigger`` field (:data:`shipit.config.LANE_TRIGGERS`);
#: an event runs every lane whose trigger sits at or before it on this ladder.
EVENT_PR = "pr"
EVENT_PUSH = "push"
EVENT_NIGHTLY = "nightly"
EVENT_DISPATCH = "dispatch"
EVENTS: tuple[str, ...] = (EVENT_PR, EVENT_PUSH, EVENT_NIGHTLY, EVENT_DISPATCH)

#: GitHub Actions event names → the planner vocabulary, so the ``wf-checks``
#: block passes ``${{ github.event_name }}`` VERBATIM and the mapping lives
#: here (fixture-tested), never re-derived in YAML (ADR-0040: zero logic in
#: the block).
GITHUB_EVENTS: dict[str, str] = {
    "pull_request": EVENT_PR,
    "push": EVENT_PUSH,
    "schedule": EVENT_NIGHTLY,
    "workflow_dispatch": EVENT_DISPATCH,
}

#: The runner a lane gets when it declares none — the fleet's ordinary linux
#: runner (the legacy workflows' default; a mac/GPU lane declares its own).
DEFAULT_RUNNER = "ubuntu-latest"


class LanePlanError(Exception):
    """The invocation cannot be planned — a USAGE error (exit 2, ADR-0030).

    Raised for an event outside the closed vocabulary (neither a planner
    event nor a GitHub event name). The message is the whole user-facing
    diagnosis, so the verb prints it verbatim.
    """


@dataclass(frozen=True)
class CacheDescriptor:
    """Cache switches the workflow block gates as static steps.

    The planner owns these booleans (CI-cache spike #582; ADR-0040): YAML reads
    ``matrix.caches.*`` and routes, but never infers whether a lane is Rust, uv,
    or sccache-shaped.
    """

    rust: bool = False
    sccache: bool = False
    uv: bool = False

    def as_matrix_entry(self) -> dict[str, bool]:
        return {"rust": self.rust, "sccache": self.sccache, "uv": self.uv}


@dataclass(frozen=True)
class Job:
    """One emitted matrix entry: a lane routed to a CI job.

    ``name`` is the lane name (the job's display name and check name);
    ``run`` the pixi task invocation the block executes (``pixi run <run>``);
    ``runner`` the resolved ``runs-on`` label (never ``None`` — the planner
    fills :data:`DEFAULT_RUNNER`); ``required`` the merge-blocking flag carried
    from the lane so the block can spare an advisory lane's failure from the
    ``check`` verdict (via ``continue-on-error``).

    ``envs`` / ``envset`` are the setup-pixi provisioning identity for this
    lane's task. ``caches`` and ``rust_workspaces`` are planner-emitted cache
    descriptors consumed by static gated workflow steps; sccache and uv are
    deliberately explicit false until their separate delivery stories land.
    """

    name: str
    run: str
    runner: str
    required: bool
    envs: tuple[str, ...] = ("default",)
    caches: CacheDescriptor = CacheDescriptor()
    rust_workspaces: str = ""

    @property
    def envset(self) -> str:
        """Stable env-set identity for cache keys and single-env PATH exports."""
        return "+".join(self.envs)

    def as_matrix_entry(self) -> dict[str, str | bool | dict[str, bool]]:
        """The GitHub ``matrix.include`` entry — the JSON hand-off shape the
        ``wf-checks`` plan job surfaces as its output. ``required`` rides along
        as a JSON boolean so the block's ``continue-on-error`` can read it."""
        return {
            "name": self.name,
            "run": self.run,
            "runner": self.runner,
            "required": self.required,
            "envs": ",".join(self.envs),
            "envset": self.envset,
            "caches": self.caches.as_matrix_entry(),
            "rust_workspaces": self.rust_workspaces,
        }


def normalize_event(raw: str) -> str:
    """The planner event for ``raw`` — either vocabulary, one normalization.

    Accepts the planner names (:data:`EVENTS`) and the GitHub Actions event
    names (:data:`GITHUB_EVENTS`), so the block passes
    ``${{ github.event_name }}`` untranslated. Anything else raises
    :class:`LanePlanError` naming both vocabularies — a typo dies at the
    boundary, never as a silently-empty matrix.
    """
    event = raw.strip()
    if event in EVENTS:
        return event
    if event in GITHUB_EVENTS:
        return GITHUB_EVENTS[event]
    github_only = (name for name in GITHUB_EVENTS if name not in EVENTS)
    known = ", ".join([*EVENTS, *github_only])
    raise LanePlanError(f"unknown event {raw!r}; known events: {known}")


def _triggered(lane: config.Lane, event: str) -> bool:
    """The trigger ladder: the lane runs when ``event`` is at or past its
    trigger in :data:`EVENTS` order (rarer events run everything before them)."""
    return EVENTS.index(event) >= EVENTS.index(lane.trigger)


def _in_scope(path: str, scope: str) -> bool:
    """Whether a changed ``path`` falls inside a lane's ``scope`` subtree.

    Segment-wise prefix match (``crates/wasm`` matches ``crates/wasm/src/x.rs``
    but not ``crates/wasm2/…``); ``"."`` names the whole tree.
    """
    prefix = scope.rstrip("/")
    if prefix in ("", "."):
        return True
    return path == prefix or path.startswith(prefix + "/")


def task_env_sets(pixi: Mapping[str, object]) -> dict[str, tuple[str, ...]]:
    """Map pixi task name → environment set, from a parsed ``pixi.toml``.

    setup-pixi installs environments, not tasks. Pixi's manifest tells us which
    feature owns a task and which environment includes that feature; this parser
    keeps that provisioning fact in the fixture-tested planner instead of
    encoding it in workflow YAML. Unknown tasks default later to ``default`` so
    a consumer with only default tasks still works.
    """
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


def _lane_task(run: str) -> str:
    """The task/tool token a lane asks pixi to run."""
    return run.split()[0]


def _rust_workspaces(rust_legs: Sequence[legs.Leg]) -> str:
    """Swatinem/rust-cache workspace mapping for rust legs using root target/."""
    entries: list[str] = []
    for leg in rust_legs:
        target = "target" if leg.path == "." else posixpath.relpath("target", leg.path)
        entries.append(f"{leg.path} -> {target}")
    return "\n".join(entries)


def _cache_descriptor(
    lane: config.Lane, toolchains: Sequence[config.ToolchainEntry]
) -> tuple[CacheDescriptor, str]:
    """The planner-owned cache descriptor for one lane.

    Rust is true only for Tool-verb lanes whose selected legs include a rust
    toolchain. uv is env-carried and sccache is deferred per the cache spike, so
    both are emitted explicitly false.
    """
    parts = lane.run.split()
    tool = parts[0] if parts else ""
    if tool not in registry.TOOLS:
        return CacheDescriptor(), ""
    selector = parts[1] if len(parts) > 1 else None
    try:
        planned = legs.plan_legs(toolchains, tool=tool, selector=selector)
    except legs.LegPlanError:
        # The lane's eventual job will fail loudly on a bad selector. The cache
        # descriptor should not make `ci plan` stricter than the existing run
        # contract for opaque lane strings.
        return CacheDescriptor(), ""
    rust_legs = [leg for leg in planned if leg.toolchain == "rust"]
    return CacheDescriptor(rust=bool(rust_legs)), _rust_workspaces(rust_legs)


def plan(
    lanes: Sequence[config.Lane],
    *,
    event: str,
    changed_paths: Sequence[str] | None = None,
    task_envs: Mapping[str, Sequence[str]] | None = None,
    toolchains: Sequence[config.ToolchainEntry] = (),
) -> tuple[Job, ...]:
    """The ordered job matrix for ``event`` over the declared ``lanes``.

    ``event`` must be a planner event (:data:`EVENTS` — callers normalize via
    :func:`normalize_event`; an out-of-vocabulary event here is a caller bug,
    ``ValueError``). ``changed_paths`` is the PR's path-diff, ``None`` when
    unknown — and it only ever THINS a ``pr`` event's plan (module docstring:
    full scope is forced on non-PR events and on an unknown diff). An empty
    matrix is a legitimate plan: a thin PR may drop every scoped lane.
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
        caches, rust_workspaces = _cache_descriptor(lane, toolchains)
        jobs.append(
            Job(
                name=lane.name,
                run=lane.run,
                runner=lane.runner or DEFAULT_RUNNER,
                required=lane.required,
                envs=envs,
                caches=caches,
                rust_workspaces=rust_workspaces,
            )
        )
    return tuple(jobs)


def commit_push_checks(lanes: Sequence[config.Lane]) -> tuple[config.Lane, ...]:
    """The commit/push checks: the lanes both ``required`` and ``local``, in
    declaration order (story 13; the glossary set formerly called "the gate").

    This derivation is the ONE definition of what blocks at the *commit* and
    *push* operations — lefthook enforces exactly this set, CI enforces the
    broader all-lanes policy over the same declarations (commit/push checks ⊆
    lanes), so the two can never drift into separate transcriptions. On
    shipit's own declarations this equals ``lint`` + the fast ``test`` set,
    pinned by test (``tests/test_tools_lanes.py``), not by convention.
    """
    return tuple(lane for lane in lanes if lane.required and lane.local)
