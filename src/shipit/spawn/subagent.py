"""``spawn/subagent`` — the subagent pipeline as spec → typed result (ADR-0030).

The domain home of ``shipit spawn subagent`` (CLI02-WS02): the whole pipeline —
shape validation → identity → umbrella check → Tree → launch → post-condition
audit — is one typed function, :func:`spawn_subagent`, from a frozen
:class:`SubagentSpec` to a frozen :class:`SpawnResult`. It logs (the ADR-0029
durable twin, including the ``agent.spawned`` / ``agent.done`` dev-cycle events
and the spawn-seam identity binding, ADR-0032) but never prints: rendering is
the verb layer's pure ``format_*`` through the shared render seam, and every
refusal is the :class:`SpawnError` domain exception the shared error shell maps
to ``error: …`` + exit 1 — dissolving the old print+log+rc fusion helper.

Every effectful edge rides the injectable :class:`Boundaries` value (git/gh
reads, Tree creation, the subprocess runner), so each stage is testable
typed-in/typed-out without a network, a clone, or a real backend child.

The pipeline itself is unchanged in behavior (a mechanical ADR-0030 promotion),
plus the RPE01-WS01 registry preflight:

- **Registry-driven role preflight** (RPE01-WS01): the shape gate runs
  :func:`shipit.harness.roleprofile.validate_spawn` for the DETACHED launch
  context, so an unknown role string, a detached explorer (ambient — no Tree,
  ever), a detached coordinator (the host session), or a detached shepherd
  (existing-PR attachment is RPE01-WS04) refuses BEFORE any Tree provisioning
  or backend launch, naming the role and the requested context. Reviewer
  dispatch keys off the registry-parsed :class:`~shipit.harness.role.Role`.
- **Fail-closed** (ADR-0017/0019): a Tree-creation error fails the spawn loud —
  NEVER a silent fallback to a native ``git worktree``. The launcher is reached
  only after a Tree exists, so a failed create can never launch a Run against
  the parent checkout; a missing epic umbrella branch refuses rather than
  falling back to ``origin/main``; a write shape spawned onto a PINLESS base
  (no ``.shipit.toml [shipit].version``) refuses through provisioning's pin
  gate, naming the bootstrap install (ADR-0033).
- Tree creation is REUSED wholesale (:func:`shipit.tree.create.create` /
  :func:`shipit.tree.readonly.create_readonly`) — never reimplemented.
- The Run reports back **through the PR** (ADR-0019 §6): the write tail
  resolves the PR the Run opened on the Tree's branch and audits it (OPEN,
  DRAFT, targeting the Tree's base) before claiming success.
- Any write-tail failure after Tree creation carries the **salvage signal**
  (#587): the refusal appends the Tree's uncommitted-change count when the
  dead Tree still holds work worth inspecting, so a Run killed mid-work is a
  resumable handoff for the coordinator, never a silent loss. A launch
  transport failure (child never started) is covered too — its fresh Tree is
  clean, so the probe finds nothing and the bare refusal passes untouched.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from .. import events, execrun, gh, git, identity, logcontext
from ..harness import roleprofile
from ..harness.role import Role
from ..tree.create import Tree, create, new_agent_hash
from ..tree.layout import (
    TreeSpec,
    epic_umbrella_base,
    issue_branch,
    work_stream_branch,
)
from ..tree.readonly import create_readonly, readonly_plan
from . import backends, launch

#: The spawn subsystem's logger — a child of the package ``shipit`` logger, so its
#: records ride the LOG01 pipeline (JSONL file sink, bound domain keys, redaction)
#: with zero wiring here. Lifecycle narration follows the spray conventions
#: (glassbox PRD / ADR-0029): milestones at INFO with durations where meaningful,
#: mechanics at DEBUG, propagating failures at ERROR with the exception attached.
logger = logging.getLogger("shipit.spawn")

#: The backends a subagent spawn can launch today — **adapter-driven** (ADR-0020
#: §Decision 2): derived from the :mod:`shipit.spawn.backends` registry, not a
#: hand-maintained constant, so wiring a backend is one registry entry. ``claude``
#: (ADR-0019), ``codex``, and ``antigravity`` (the ``agy`` CLI) are all registered —
#: write Runs (WS02/WS03) and reviewer Runs (WS04a). A ``click.Choice`` over this
#: gates the CLI, and the pipeline's own gate re-checks it so the programmatic
#: entry is guarded too.
SUPPORTED_BACKENDS = backends.supported_backends()

#: The role that gets a shared **read-only Tree** + a **Reviewer Run** (ADR-0018)
#: instead of the per-Run write Tree every other spawnable role gets. The pipeline
#: now DISPATCHES on the Role Profile registry's parsed :class:`Role` (RPE01-WS01),
#: so this string constant only labels the reviewer tail's records/results; the
#: duplicated-constant cleanup is a later workstream (RPE01-WS08).
REVIEWER_ROLE = "reviewer"


class SpawnError(RuntimeError):
    """A spawn pipeline refusal — a clean runtime failure, never a traceback.

    Raised at every gate the pipeline refuses (bad shape, wrong checkout,
    missing umbrella, failed Tree, failed launch, failed handshake audit). One
    of the :data:`shipit.verbs._errors.KNOWN_ERRORS`, so the shared shell
    renders it as ``error: …`` + exit 1; the durable ERROR record (with the
    causing exception attached where one exists) is logged at the raise site
    via :func:`_refusal` — the log half of the dissolved print+log+rc helper.
    """


@dataclass(frozen=True)
class SubagentSpec:
    """The typed request for one subagent Run — what the CLI options parse into.

    Two axes decide the Tree. **Role** picks write vs read-only: every role but
    ``reviewer`` gets a per-Run write Tree; ``reviewer`` (ADR-0018) gets the
    shared read-only Tree on the existing PR head. **Shape** picks branch/base:
    ``epic``+``ws`` (branch ``E/WSnn`` cut from ``origin/E/umbrella``) or a
    standalone ``issue`` (branch ``issues/<id>/<session>`` cut from
    ``origin/main``). Deliberately NOT validated at construction: the pipeline's
    request milestone must record even a malformed ask (ADR-0029 — a refused
    spawn still leaves a durable record of what was asked), so the shape gate is
    :func:`spawn_subagent`'s first stage, not ``__post_init__``.
    """

    repo: str
    role: str
    epic: str | None = None
    ws: int | None = None
    issue: int | None = None
    session: str = "work"
    backend: str = "claude"

    @property
    def has_epic_shape(self) -> bool:
        """Whether either half of the epic/work-stream shape was given."""
        return self.epic is not None or self.ws is not None


@dataclass(frozen=True)
class SpawnResult:
    """The finished spawn's coordinates — the typed result the verb renders.

    Exactly the (frozen, agent-parsed) SPAWNED payload: the Tree the Run worked
    in, its branch/base, the role and backend, and — for a write Run — the
    Run↔PR linkage the coordinator drives with ``shipit pr status <N>``. A
    reviewer Run reports through the EXISTING PR and opens none, so its PR
    fields stay ``None`` and are absent from :meth:`to_dict`.
    """

    tree: str
    branch: str
    base: str
    role: str
    backend: str
    pr: int | None = None
    pr_state: str | None = None
    pr_is_draft: bool | None = None

    def to_dict(self) -> dict:
        """The SPAWNED JSON payload — byte-stable field set and order."""
        payload: dict = {
            "tree": self.tree,
            "branch": self.branch,
            "base": self.base,
            "role": self.role,
            "backend": self.backend,
        }
        if self.pr is not None:
            payload["pr"] = self.pr
            payload["pr_state"] = self.pr_state
            payload["pr_is_draft"] = self.pr_is_draft
        return payload


@dataclass(frozen=True)
class Boundaries:
    """The pipeline's injectable effectful edges — real adapters by default.

    One value bundling every I/O seam the pipeline touches, so a test drives
    any stage with fakes (typed-in/typed-out, no monkeypatching) while
    production runs on the defaults. ``runner`` is the subprocess seam the
    launch already exposed (:data:`shipit.spawn.launch.Runner`); ``None`` uses
    the real Exec-backed runner.
    """

    repo_root: Callable[[], str | None] = git.repo_root
    resolve_repo: Callable[[str], identity.Repo] = identity.resolve_repo
    remote_url: Callable[..., str] = git.remote_url
    remote_branch_exists: Callable[..., bool] = git.remote_branch_exists
    create_tree: Callable[..., Tree] = create
    create_readonly_tree: Callable[..., Tree] = create_readonly
    pr_for_head: Callable[..., gh.HeadPr | gh.UnknownPr | None] = gh.pr_for_head
    status_porcelain: Callable[..., list[str]] = git.status_porcelain
    runner: launch.Runner | None = None


#: The production boundary set — one shared instance, since it is frozen.
BOUNDARIES = Boundaries()


def _elapsed_ms(start: float) -> int:
    """Milliseconds elapsed since ``start`` (a ``time.monotonic`` stamp)."""
    return int((time.monotonic() - start) * 1000)


def _refusal(
    message: str, *, exc: BaseException | None = None, **fields: object
) -> SpawnError:
    """Mint the pipeline's refusal: the durable ERROR record + the domain exception.

    The log half of the dissolved print+log+rc fusion: every refusal propagates
    as :class:`SpawnError` (the shell prints and derives the exit code), while
    the durable record lands HERE at ERROR — with the causing exception
    attached via ``exc`` when one exists. ``fields`` land as flat event extras
    (:class:`structlog.stdlib.ExtraAdder` adopts stdlib ``extra=``); ``None``
    values are dropped so the absent-not-null record contract holds for extras
    exactly as it does for domain keys. Returned (not raised) so call sites
    read ``raise _refusal(...)`` — the raise stays visible at the seam.
    """
    extras = {name: value for name, value in fields.items() if value is not None}
    # One exc_info form across the spray (LOG02 convergence): `exc_info=True`
    # reads the ACTIVE exception — with its real traceback — where passing the
    # instance would attach only type+value when it was never raised here. Every
    # caller that passes `exc` does so from inside its `except` block, so the
    # active exception is exactly `exc`.
    logger.error("spawn subagent: %s", message, exc_info=exc is not None, extra=extras)
    return SpawnError(message)


def spawn_subagent(spec: SubagentSpec, bounds: Boundaries | None = None) -> SpawnResult:
    """Validate → resolve identity → create the Tree → launch → audit. The pipeline.

    The one typed spec→result function behind ``shipit spawn subagent``. Raises
    :class:`SpawnError` (a clean runtime refusal, never a traceback) when the
    backend is unsupported, the ROLE fails the Role Profile registry's detached
    preflight (RPE01-WS01: an unknown role string, or a role whose profile does
    not support a detached launch — the explorer is ambient-native only, the
    coordinator is the host session, the shepherd attaches to an existing PR in
    a later WS — each refused BEFORE any Tree provisioning or backend launch,
    naming the role and the requested context), the shape is incomplete/invalid
    (``--epic``/``--ws`` only half given, non-positive ``--ws``/``--issue``, a
    write role without an issue, a reviewer without any shape, a ``--session``
    that sanitizes to nothing), ``--repo`` disagrees with the ambient checkout, the command is not
    run inside a GitHub checkout, a git/gh call fails, **Tree creation fails**
    (fail-closed — no native-worktree fallback; this includes a write shape
    spawned onto a PINLESS base, ADR-0033's surviving guard: provisioning's
    pin gate raises and the refusal names the bootstrap install rather than
    launching a Run whose ``bin/shipit`` could never exec), the child exits
    nonzero, or —
    for a write Run — the post-condition audit fails (no PR on the branch, an
    unreadable PR state, or a PR that is not an OPEN, DRAFT PR targeting the
    Tree's intended base). Any write-tail refusal raised after Tree creation —
    a launch transport failure (child never started), a nonzero child, or a
    failed audit — appends the salvage signal (#587) when the dead Tree still
    holds uncommitted work: the one-line ``git status --porcelain`` count, so
    the coordinator knows the Tree is worth inspecting before discarding it. (A
    transport failure leaves the fresh Tree clean, so the probe finds nothing
    and the bare refusal passes through untouched — a note appears only once a
    Run has run and left work behind.)

    ``bounds`` injects the effectful edges (:class:`Boundaries`) so every stage
    is testable without git, gh, a clone, or a real backend child; ``None`` is
    production (the real adapters).
    """
    bounds = bounds if bounds is not None else BOUNDARIES
    # A fresh spawn OWNS the whole spawn-identity key set: `tree`/`agent` are
    # minted below, and `epic`/`ws`/`role` are THIS spawn's arguments (ADR-0032
    # — the spawn's args ARE the worker's identity). Any of them already bound
    # is stale for this spawn's story — a nested spawn inherits the parent's
    # `SHIPIT_LOG_CTX_*` (rebound at logging setup), and a prior spawn in the
    # same process leaves its bindings behind. Because `bind` DROPS `None`
    # halves (absent-not-null), a standalone-issue spawn would otherwise keep a
    # stale `epic`/`ws` bound and `env_export` would thread the previous
    # workstream's identity into the new child. Drop them all at the entry so
    # the request milestone and any pre-Tree refusal carry NO spawn identity,
    # and each key appears exactly once — at the seam that binds it for this
    # spawn (ADR-0029 record contract).
    logcontext.unbind("tree", "agent", "epic", "ws", "role")
    # Lifecycle milestone (ADR-0029): the spawn REQUEST, narrated as received —
    # before any gate — so even a refused spawn leaves a durable record of what
    # was asked. The shape fields ride as flat extras (absent when not given);
    # the domain keys (`repo` from the CLI entry, `tree` once assigned below)
    # bind via logcontext and land on this and every later record.
    logger.info(
        "spawn subagent: %s run requested on backend %s",
        spec.role,
        spec.backend,
        extra={
            name: value
            for name, value in {
                "role": spec.role,
                "backend": spec.backend,
                "epic": spec.epic,
                "ws": spec.ws,
                "issue": spec.issue,
                "session": spec.session if not spec.has_epic_shape else None,
            }.items()
            if value is not None
        },
    )
    adapter, profile = validate(spec)

    # SPAWN-SEAM identity binding (ADR-0032 / LOG04-WS02): the spawn's own
    # arguments ARE the worker's dev-cycle identity, so `epic`/`ws`/`role` bind
    # here — the moment they are known and validated — and every subsequent
    # record of this spawn carries them. The role binds NORMALIZED (the parsed
    # registry Role, not the raw input) so records never carry a cased variant.
    # `agent` (the spawn id) binds in the launch tails once minted. `env_export`
    # at the launch then threads ALL bound keys into the Run's environment
    # (`SHIPIT_LOG_CTX_*`), so every shipit command the worker runs correlates
    # to its Work Stream with zero worker cooperation. A standalone-issue spawn
    # has no epic/ws; `bind` drops the `None` halves (present-when-bound,
    # absent-not-null).
    logcontext.bind(epic=spec.epic, ws=spec.ws, role=profile.role.value)

    root, repo_identity, url = resolve_spawn_identity(spec, bounds)

    # Reviewer Run (ADR-0018): a shared READ-ONLY Tree on the existing PR head, not a
    # per-Run write Tree. Its target branch follows the SHAPE — the epic work-stream head
    # E/WSnn, or a standalone-issue head issues/<id>/<session> — built from the same
    # grammar helpers the write planner uses so a reviewer pins exactly the branch a
    # write Run pushed. Dispatched before the write path so the two never share
    # provisioning — on the registry-parsed Role (RPE01-WS01), so an oddly cased
    # role input can never slip past the reviewer dispatch into the write tail.
    if profile.role is Role.REVIEWER:
        try:
            review_branch = (
                work_stream_branch(spec.epic, spec.ws)
                if spec.has_epic_shape
                else issue_branch(spec.issue, spec.session)
            )
        except ValueError as exc:
            # Fail loud, identically to the write path: work_stream_branch validates the
            # epic code (an empty/invalid epic must NOT silently yield "/WS01") and
            # issue_branch validates the session — both raise ValueError, surfaced as
            # the clean domain refusal, never a traceback.
            raise _refusal(str(exc), exc=exc) from exc
        return _launch_reviewer(
            repo=repo_identity,
            branch=review_branch,
            source_repo=root,
            github_url=url,
            adapter=adapter,
            bounds=bounds,
        )

    # WRITE Run: build the shape's TreeSpec, then hand off to the shared launch tail.
    tree_spec = plan_write_spec(spec, repo_identity, root, bounds)
    return _launch_write(
        tree_spec,
        source_repo=root,
        github_url=url,
        role=profile.role.value,
        issue=spec.issue,
        backend=spec.backend,
        adapter=adapter,
        bounds=bounds,
    )


def validate(
    spec: SubagentSpec,
) -> tuple[backends.BackendAdapter, roleprofile.RoleProfile]:
    """Stage 1 — the shape gate (before any I/O). Returns (adapter, role profile).

    The explicit backend guard fails an unknown backend LOUD at the boundary
    (no silent default to claude); only then is its adapter resolved (ADR-0020)
    — the adapter supplies the per-backend argv / auth-env / read-only posture,
    and everything downstream is backend-agnostic. The ROLE then rides the Role
    Profile registry's spawn preflight (RPE01-WS01,
    :func:`shipit.harness.roleprofile.validate_spawn`): an unknown role string,
    or a role whose profile does not support a DETACHED launch (explorer —
    ambient, a detached spawn would mint a write Tree it must never have;
    coordinator — the host session; shepherd — existing-PR attachment is a
    later WS), is refused HERE, before any Tree provisioning or backend
    launch, with the role and requested context named. ``--epic`` and ``--ws``
    are a PAIR (the epic/work-stream shape); one without the other is an
    incomplete shape and refused loud, and their ABSENCE selects the
    standalone-issue shape (branch ``issues/<id>/<session>``).
    """
    if spec.backend not in SUPPORTED_BACKENDS:
        supported = ", ".join(SUPPORTED_BACKENDS)
        raise _refusal(
            f"unsupported backend {spec.backend!r} (supported: {supported}); wiring a "
            "new backend is one entry in the adapter registry (ADR-0020).",
            backend=spec.backend,
        )
    adapter = backends.resolve(spec.backend)

    # The registry preflight (RPE01-WS01): every `shipit spawn subagent` launch
    # is DETACHED, so the (role, detached) pairing must be a profile-supported
    # combination. Fail-closed and pre-I/O — the strict public boundary, in
    # deliberate contrast to the hook resolver's lenient unknown-worker
    # fallback (which governs identities but never mints spawns).
    try:
        profile = roleprofile.validate_spawn(
            spec.role, roleprofile.LaunchContext.DETACHED
        )
    except roleprofile.RoleValidationError as exc:
        raise _refusal(str(exc), exc=exc, role=spec.role) from exc

    if spec.has_epic_shape and (spec.epic is None or spec.ws is None):
        raise _refusal(
            "the epic shape needs both --epic and --ws "
            f"(got epic={spec.epic!r}, ws={spec.ws!r}); omit both for a standalone "
            "--issue Tree.",
            epic=spec.epic,
            ws=spec.ws,
        )
    if spec.has_epic_shape and spec.ws < 1:
        raise _refusal(
            f"--ws must be a positive integer (got {spec.ws})",
            epic=spec.epic,
            ws=spec.ws,
        )
    if profile.role is not Role.REVIEWER and (spec.issue is None or spec.issue < 1):
        # ``--issue`` rides the task prompt and the draft PR's issue link (#649:
        # ``closes #<issue>`` for the standalone shape, so the merge auto-closes it;
        # ``for #<issue>`` for the epic shape, non-closing — the umbrella PR closes
        # the epic's issues). A missing or zero/negative value (which click's int
        # type still accepts) would forge a nonsensical issue reference. Refuse it
        # before any Tree/child work, mirroring the ``--ws`` guard above. A reviewer
        # Run implements no issue (it reviews an existing PR head), so the
        # requirement does not apply to it. This holds for BOTH write shapes — the
        # standalone Run's issue also names its branch.
        raise _refusal(
            f"--issue must be a positive integer (got {spec.issue})", role=spec.role
        )
    if not spec.has_epic_shape and spec.issue is None:
        # Reachable only for a reviewer (a write role already required --issue above):
        # with neither an epic shape nor an issue there is no branch to resolve a head
        # from. Refuse it loud with a clear, reviewer-specific message HERE — otherwise
        # the reviewer dispatch would take the issue path and call
        # `issue_branch(None, session)`, which raises a generic ValueError ("issue
        # number must be a positive integer"). A clean refusal either way, but this
        # message names the ACTUAL problem (no shape given), not a confusing complaint
        # about the issue number.
        raise _refusal(
            "a reviewer needs a branch to review — give --epic E --ws N or --issue N.",
            role=spec.role,
        )
    return adapter, profile


def resolve_spawn_identity(
    spec: SubagentSpec, bounds: Boundaries
) -> tuple[str, identity.Repo, str]:
    """Stage 2 — the ambient checkout's identity + the ``--repo`` guard.

    Returns ``(root, repo_identity, github_url)``. Identity derives LOCALLY
    from the origin remote (ADR-0024): one canonical, case-normalized
    :class:`~shipit.identity.Repo` value — a malformed remote fails loud
    rather than feeding a bogus identity into the TreeSpec. ``spec.repo`` is
    the wrong-checkout guard, not a repo SELECTOR yet: a ``--repo`` naming a
    different repo is refused rather than silently ignored, compared through
    the canonical identity (lowercased — GitHub slugs are case-insensitive).
    Multi-repo selection is a later WS.
    """
    root = bounds.repo_root()
    if not root:
        raise _refusal("not inside a git checkout")
    try:
        repo_identity = bounds.resolve_repo(root)
        url = bounds.remote_url(cwd=root)
    except (execrun.ExecError, ValueError) as exc:
        raise _refusal(str(exc), exc=exc) from exc

    if spec.repo.strip().lower() not in (repo_identity.name, repo_identity.slug):
        raise _refusal(
            f"--repo {spec.repo!r} but the ambient checkout is "
            f"{repo_identity.slug!r}; the skeleton spawns from the target checkout "
            "(multi-repo selection is a later WS)."
        )
    return root, repo_identity, url


def plan_write_spec(
    spec: SubagentSpec,
    repo_identity: identity.Repo,
    root: str,
    bounds: Boundaries,
) -> TreeSpec:
    """Stage 3 — the umbrella check + the write shape's :class:`TreeSpec`.

    The epic shape (#176) resolves branch ``E/WSnn`` cut from the epic-grouped
    umbrella base (``origin/E/umbrella``) through the same pure planner
    ``shipit tree create`` uses — after the fail-closed remote pre-check: the
    umbrella branch MUST exist on origin, else the spawn refuses LOUD rather
    than silently falling back to ``origin/main`` (which would land the WS PR
    on the wrong base). Checked here (pre-clone) so the diagnostic names the
    missing epic branch precisely, rather than surfacing as an opaque
    ``git checkout`` failure deep in tree creation. The standalone-issue shape
    validates its branch grammar (positive issue, non-empty session) BEFORE any
    side effect — ``origin/main`` always exists, so there is no umbrella-style
    remote pre-check to run.
    """
    if spec.has_epic_shape:
        try:
            umbrella_base = epic_umbrella_base(spec.epic)  # origin/E/umbrella
        except ValueError as exc:
            # An invalid/empty epic code (not a single alphanumeric token) would build a
            # malformed or path-traversing umbrella ref, so the pure helper refuses it.
            raise _refusal(str(exc), exc=exc) from exc
        umbrella_branch = umbrella_base.split("/", 1)[-1]  # E/umbrella
        try:
            umbrella_exists = bounds.remote_branch_exists(umbrella_branch, cwd=root)
        except execrun.ExecError as exc:
            raise _refusal(str(exc), exc=exc) from exc
        if not umbrella_exists:
            raise _refusal(
                f"epic base branch {umbrella_branch!r} does not exist "
                f"on origin; cannot cut work stream {spec.epic}/WS{spec.ws:02d} from "
                "it. Create the epic umbrella branch first — refusing to fall back to "
                "origin/main, which would target the WS PR at the wrong base "
                "(#176, fail-closed).",
                epic=spec.epic,
                ws=spec.ws,
            )
        return TreeSpec(
            repo=repo_identity,
            agent_hash=new_agent_hash(),
            epic=spec.epic,
            ws=spec.ws,
        )
    try:
        issue_branch(spec.issue, spec.session)  # validation only; the spec re-plans it
    except ValueError as exc:
        raise _refusal(str(exc), exc=exc) from exc
    return TreeSpec(
        repo=repo_identity,
        agent_hash=new_agent_hash(),
        issue=spec.issue,
        session=spec.session,
    )


def salvage_note(tree_path: str, bounds: Boundaries) -> str | None:
    """The salvage signal behind a failed write Run (#587) — best-effort, never fatal.

    A Run that dies mid-work (wall-clock hit while verifying is the observed
    case) can leave its whole diagnosis UNCOMMITTED in the Tree; the bare
    refusal ("child exited 0 but opened no PR") reads as a total loss, so the
    coordinator has no cue to inspect the Tree before discarding it. This
    probes the Tree's working-tree status and returns the one-line salvage
    note the refusal appends — the uncommitted-change count — or ``None`` when
    there is nothing to say (a clean tree, or an unreadable one: the probe
    runs UNDER an already-failing spawn, so a probe error must never mask the
    real refusal — it logs at DEBUG and stays silent). A dirty tree also
    leaves its own WARNING record, the durable twin of the appended line.
    """
    try:
        dirty = bounds.status_porcelain(cwd=tree_path)
    except (execrun.ExecError, OSError):
        logger.debug(
            "salvage probe failed on %s (never masks the refusal)",
            tree_path,
            exc_info=True,
        )
        return None
    if not dirty:
        return None
    count = len(dirty)
    logger.warning(
        "spawn subagent: the failed run left %d uncommitted change(s) in the tree",
        count,
        extra={"uncommitted": count},
    )
    return (
        f"the tree at {tree_path} holds {count} uncommitted change(s) "
        "(git status --porcelain) — the Run's work may be salvageable; inspect "
        "the tree before discarding it."
    )


def audit_handshake(
    pr: gh.HeadPr | gh.UnknownPr | None, *, branch: str, base_branch: str
) -> gh.HeadPr:
    """Stage 6 — the post-condition audit: the Run reported back through its PR.

    Pure over the resolved PR snapshot: the contract (ADR-0019 §6) is an OPEN,
    DRAFT PR on ``branch`` targeting ``base_branch``. A branch with provably no
    PR means the Run did not report back; an undetermined state must not
    masquerade as success; a ready-for-review PR, a closed/merged one, or one
    opened against the wrong base is an INVALID lifecycle state the coordinator
    must not be handed. Each is a clean refusal — never a SPAWNED result.
    """
    if pr is None:
        raise _refusal(
            f"child exited 0 but opened no PR on {branch!r}; "
            "the Run did not report back through a draft PR.",
            branch=branch,
        )
    if pr is gh.UNKNOWN:
        raise _refusal(
            f"child exited 0 but the PR state for {branch!r} "
            "could not be read (gh unreadable); not claiming success.",
            branch=branch,
        )
    if pr.state != "OPEN":
        raise _refusal(
            f"child exited 0 but the PR on {branch!r} is "
            f"{pr.state}, not OPEN; the Run did not report back through an open "
            "draft PR.",
            branch=branch,
            pr=pr.number,
            pr_state=pr.state,
        )
    if not pr.is_draft:
        raise _refusal(
            f"child exited 0 but the PR on {branch!r} is not a "
            "draft; the Run must report back through a draft PR (the turn-signal the "
            "coordinator drives).",
            branch=branch,
            pr=pr.number,
        )
    if pr.base_ref != base_branch:
        raise _refusal(
            f"child exited 0 but the PR on {branch!r} targets "
            f"base {pr.base_ref!r}, not the intended {base_branch!r}; the "
            "Run reported back against the wrong base.",
            branch=branch,
            pr=pr.number,
            pr_base=pr.base_ref,
        )
    return pr


def _run_child(
    cmd: list[str],
    *,
    tree: Tree,
    adapter: backends.BackendAdapter,
    bounds: Boundaries,
    role: str,
) -> launch.LaunchResult:
    """Stage 5 — launch the backend child rooted in the Tree, shared by both tails.

    Emits the ``agent.spawned`` dev-cycle event at launch and ``agent.done`` on
    a clean exit (ADR-0032, verb-witnessed: the spawn seam performs the
    milestone, and the bound keys — epic/ws/agent/role/tree/repo — ride in via
    the pipeline). Argv-level detail is deliberately NOT duplicated here: the
    launch is one Exec through the runner, whose DEBUG record already carries
    the redacted argv, cwd, rc, and duration_ms (ADR-0028). A child that never
    starts (transport failure — the runner normalizes every launch-level OS
    failure into ``ExecError``) and a nonzero child are each a clean refusal; a
    nonzero child stays an UNTAGGED failure (the milestone trail records
    lifecycle ends the cycle can build on).
    """
    events.emit(
        logger,
        "agent.spawned",
        "spawn subagent: launching %s child (role=%s) in the tree",
        adapter.name,
        role,
        extra={"backend": adapter.name, "role": role, "cwd": tree.path},
    )
    events.emit(
        logger,
        "agent.phase",
        "spawn subagent: phase agent_running for %s run",
        role,
        extra={"phase": "agent_running", "backend": adapter.name, "role": role},
    )
    launch_start = time.monotonic()
    try:
        result = launch.launch(
            cmd,
            cwd=tree.path,
            env=launch.scrub_tree_env(logcontext.env_export(adapter.child_env())),
            runner=bounds.runner,
        )
    except execrun.ExecError as exc:
        # The child never started: the backend binary is missing/not on PATH, or the
        # Tree path became unavailable. The Exec runner normalizes every launch-level
        # OS failure into ExecError (ADR-0028) — a nonzero CHILD is a LaunchResult,
        # never raised (check=False), so reaching here always means a transport
        # failure. The Tree exists, so this is a launch failure, not the fail-closed
        # create path — still a clean refusal, never an escaping traceback.
        raise _refusal(str(exc), exc=exc, backend=adapter.name) from exc
    child_ms = _elapsed_ms(launch_start)
    if result.returncode != 0:
        detail = result.stderr.strip()
        raise _refusal(
            f"{adapter.name} child exited {result.returncode}"
            + (f"\n{detail}" if detail else ""),
            backend=adapter.name,
            rc=result.returncode,
            duration_ms=child_ms,
        )
    # Child-outcome milestone (ADR-0019 §6) — the `agent.done` dev-cycle event:
    # the process exit IS the Run's lifecycle end, so the rc and the Run's
    # wall-clock are the record.
    events.emit(
        logger,
        "agent.done",
        "spawn subagent: %s child exited 0 in %dms",
        adapter.name,
        child_ms,
        extra={
            "backend": adapter.name,
            "rc": result.returncode,
            "duration_ms": child_ms,
        },
    )
    return result


def _launch_write(
    spec: TreeSpec,
    *,
    source_repo: str,
    github_url: str,
    role: str,
    issue: int | None,
    backend: str,
    adapter: backends.BackendAdapter,
    bounds: Boundaries,
) -> SpawnResult:
    """Stages 4–6, write tail: materialize the Tree, launch the Run, audit its PR.

    The shared write tail for BOTH shapes (epic/work stream and standalone
    issue): the caller builds the shape's :class:`TreeSpec` and does any
    shape-specific pre-checks (the epic umbrella existence), then this seam
    creates the Tree, launches the backend child rooted in it, and resolves +
    audits the Run↔PR linkage the coordinator drives — identically whichever
    shape produced the spec, since ``tree.base``/``tree.branch`` already encode
    it. Fail-closed (ADR-0017/0019): a Tree-creation error refuses LOUD with no
    native-worktree fallback (the launcher is unreachable unless a real Tree
    exists).
    """
    create_start = time.monotonic()
    events.emit(
        logger,
        "agent.phase",
        "spawn subagent: phase tree_provisioning for %s run",
        role,
        extra={"phase": "tree_provisioning", "role": role, "backend": backend},
    )
    try:
        tree = bounds.create_tree(spec, source_repo=source_repo, github_url=github_url)
    except (ValueError, execrun.ExecError, OSError) as exc:
        # Fail-closed (ADR-0017/0019): a Tree-creation error fails the spawn LOUD.
        # There is deliberately no native-worktree fallback — the launcher below is
        # unreachable unless a real Tree exists, so a failed create can never end up
        # launching a Run against the parent checkout.
        raise _refusal(
            f"tree creation failed: {exc}",
            exc=exc,
            duration_ms=_elapsed_ms(create_start),
        ) from exc

    # SPAWN SEAM for the domain-key context (ADR-0029/0032): the Tree's identity
    # binds here — the coordinator's records from this point carry `tree` (its
    # path, the same identity the SPAWNED payload reports) — alongside `agent`,
    # the spawn id (the Tree dir's disambiguating hash doubles as the Run's
    # identity, so `shipit logs --agent <id>` and the Tree leaf name agree).
    # `env_export` at the launch threads every bound key (tree/agent here; repo
    # from the CLI entry; epic/ws/role from the spawn args) into the Run's
    # environment, so each `shipit` command the Run executes inside the Tree
    # rebinds them at its own logging setup and its records correlate back here.
    logcontext.bind(tree=tree.path, agent=spec.agent_hash)
    # Tree-assignment milestone (ADR-0029): the Run has a home. Tree birth is the
    # slowest, most failure-prone leg of a spawn (clone + provision), so the
    # duration is the meaningful one; the `tree` domain key bound above rides this
    # and every later record.
    create_ms = _elapsed_ms(create_start)
    logger.info(
        "spawn subagent: write tree assigned on %s (base %s) in %dms",
        tree.branch,
        tree.base,
        create_ms,
        extra={"branch": tree.branch, "base": tree.base, "duration_ms": create_ms},
    )
    base_branch = tree.base.split("/", 1)[-1] if "/" in tree.base else tree.base
    # The link keyword follows the write shape (#649): a standalone-issue Run
    # (no epic) links `closes #<issue>` so the merge auto-closes it; an epic
    # work-stream Run links `for #<issue>` (non-closing — the umbrella PR closes
    # the epic's issues at integration).
    task = launch.write_task(
        role,
        issue=issue,
        branch=tree.branch,
        base_branch=base_branch,
        closes=spec.epic is None,
    )
    # Launch the backend child rooted in the Tree through its adapter (ADR-0020): the
    # cwd IS the Tree, the adapter's child_env scrubs the backend's auth-shadowing vars
    # (for claude, ANTHROPIC_API_KEY), and build_command conveys the role (for claude,
    # --agent <role>, so the guard allows the Run's own edits). The task tells the Run
    # to implement the issue and open a draft PR from this branch (the result channel —
    # ADR-0019 §6). Route the write Run THROUGH the Tree's pixi env (ADR-0019
    # amendment): a provisioned write Tree carries `.pixi/envs/default`, so `pixi_wrap`
    # re-expresses the argv as `pixi run --manifest-path <tree>/pixi.toml -- <argv>`
    # and the child's tools resolve to its OWN env (docs/dev/pixi.lex §7);
    # `scrub_tree_env` drops leaked PIXI_*/CONDA_* on top of the adapter's auth scrub.
    cmd = launch.pixi_wrap(adapter.build_command(task, role, cwd=tree.path), tree.path)
    try:
        _run_child(cmd, tree=tree, adapter=adapter, bounds=bounds, role=role)

        events.emit(
            logger,
            "agent.phase",
            "spawn subagent: phase pr_audit for %s run",
            role,
            extra={"phase": "pr_audit", "role": role, "backend": backend},
        )
        # The Run reports back through the PR (ADR-0019 §6): resolve the PR it opened
        # on the Tree's branch through the SAME gh boundary the fleet scan uses — no
        # side database, the PR on the branch IS the Run↔PR link — then audit it.
        pr = audit_handshake(
            bounds.pr_for_head(tree.branch, cwd=tree.path),
            branch=tree.branch,
            base_branch=base_branch,
        )
    except SpawnError as exc:
        # The salvage signal (#587): the Tree exists, so a failure here — a launch
        # transport failure (child never started), a nonzero child, or an exited-0 Run
        # that never reported back — can strand real work uncommitted in the dead Tree.
        # `salvage_note` probes the Tree and returns None when it is clean (the
        # transport-failure case: a fresh Tree has nothing to salvage), so the bare
        # refusal re-raises untouched; only a dirty Tree appends the one-line
        # uncommitted-work count to the refusal the coordinator reads, turning a killed
        # Run into a resumable handoff instead of a silent loss. The original refusal
        # already logged its ERROR at the raise site; the salvage half logs its own
        # WARNING inside `salvage_note`, so the re-minted exception is deliberately
        # NOT routed through `_refusal` again (no duplicate ERROR record).
        note = salvage_note(tree.path, bounds)
        if note is None:
            raise
        raise SpawnError(f"{exc}\n{note}") from exc
    result = SpawnResult(
        tree=tree.path,
        branch=tree.branch,
        base=tree.base,
        role=role,
        backend=backend,
        pr=pr.number,
        pr_state=pr.state,
        pr_is_draft=pr.is_draft,
    )
    _log_spawned(result)
    return result


def _launch_reviewer(
    *,
    repo: identity.Repo,
    branch: str,
    source_repo: str,
    github_url: str,
    adapter: backends.BackendAdapter,
    bounds: Boundaries,
) -> SpawnResult:
    """Stages 4–5, reviewer tail: the shared read-only Tree, then launch + observe.

    The ADR-0018 reviewer path: resolve the shared per-``(repo, branch)``
    read-only Tree (a second reviewer on the same head REUSES the clone), then
    launch the backend child rooted in it with the adapter's **read-only
    posture** (``read_only=True`` — for ``claude`` the read-only ``--tools``
    allow-list; for ``codex`` / ``agy``, which have no allow-list, the
    least-privilege posture that still lets the agent self-post via ``gh pr
    review``, with read-only enforced by the chmod'd Tree, ADR-0020 §Decision 3)
    and the reviewer task — which reads the diff and posts a review THROUGH the
    PR. Unlike the write tail there is no Run↔PR linkage to audit: the Run
    reports out-of-band (the review lands in the existing PR), so success is
    the child's clean exit. Fail-closed mirrors the write path — a
    read-only-Tree error refuses loud, never a fallback.
    """
    plan = readonly_plan(repo=repo, branch=branch)
    create_start = time.monotonic()
    events.emit(
        logger,
        "agent.phase",
        "spawn subagent: phase read_only_tree for reviewer run",
        extra={
            "phase": "read_only_tree",
            "role": REVIEWER_ROLE,
            "backend": adapter.name,
        },
    )
    try:
        tree = bounds.create_readonly_tree(
            plan, source_repo=source_repo, github_url=github_url
        )
    except (ValueError, execrun.ExecError, OSError) as exc:
        # Fail-closed (ADR-0017/0018): a read-only-Tree error fails the spawn LOUD;
        # the launcher below is unreachable unless a real Tree exists.
        raise _refusal(
            f"read-only tree creation failed: {exc}",
            exc=exc,
            duration_ms=_elapsed_ms(create_start),
        ) from exc

    # SPAWN SEAM (ADR-0029/0032), mirroring the write path: the shared read-only
    # Tree's identity binds here, plus a freshly-minted `agent` spawn id — the
    # Tree is SHARED per (repo, branch) so it carries no per-Run hash of its
    # own, but the Run still needs its own identity for `shipit logs --agent`.
    logcontext.bind(tree=tree.path, agent=new_agent_hash())
    create_ms = _elapsed_ms(create_start)
    logger.info(
        "spawn subagent: read-only review tree assigned on %s in %dms",
        tree.branch,
        create_ms,
        extra={"branch": tree.branch, "base": tree.base, "duration_ms": create_ms},
    )

    # The reviewer posture (ADR-0020 §Decision 3): `read_only=True` builds the
    # backend's reviewer argv (claude → read-only --tools; codex →
    # workspace-write+network, NOT the write bypass; agy → drop
    # --dangerously-skip-permissions). `cwd=tree.path` is required by the agy adapter
    # (it ignores the process cwd and roots ONLY via `--add-dir <Tree>`) and ignored
    # by the rest. The chmod'd read-only Tree is the load-bearing FS guard whatever
    # the backend's native posture. `pixi_wrap` is a no-op here by design: a
    # reviewer's read-only Tree is clone+checkout with NO provisioned
    # `.pixi/envs/default`, so it stays BARE (routing a chmod'd tree through
    # `pixi run` would force a solve / fail). The gate, not the call site, decides.
    cmd = launch.pixi_wrap(
        adapter.build_command(
            launch.reviewer_task(branch),
            REVIEWER_ROLE,
            read_only=True,
            cwd=tree.path,
        ),
        tree.path,
    )
    _run_child(cmd, tree=tree, adapter=adapter, bounds=bounds, role=REVIEWER_ROLE)

    result = SpawnResult(
        tree=tree.path,
        branch=tree.branch,
        base=tree.base,
        role=REVIEWER_ROLE,
        backend=adapter.name,
    )
    _log_spawned(result)
    return result


def _log_spawned(result: SpawnResult) -> None:
    """The spawn-handshake milestone (ADR-0029): the SPAWNED coordinates, durably.

    The same coordinates the verb's SPAWNED stdout block hands the coordinator,
    on the durable record — for a write Run that includes the Run↔PR linkage
    (``pr`` doubles as the domain key, so ``jq 'select(.pr==N)'`` finds the
    spawn that minted the PR). The terminal rendering itself is the verb
    layer's (pure ``format_spawned`` through the shared render seam); this is
    its log twin, kept in the domain so a programmatic caller leaves the same
    trail.
    """
    logger.info(
        "spawn subagent: SPAWNED %s run on %s",
        result.role,
        result.branch,
        extra=dict(result.to_dict()),
    )
