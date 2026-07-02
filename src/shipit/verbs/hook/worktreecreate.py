"""`shipit hook worktreecreate` — the WorktreeCreate adapter (ADR-0017 + ADR-0027).

Claude Code fires the `WorktreeCreate` hook for TWO callers. Left to itself the
harness mints a native `.claude/worktrees/agent-<hash>` worktree — exactly the
thing ADR-0014 forbids and the #139 enforcement gap. This boundary intercepts the
hook and instead provisions a **Tree** (a dissociated clone) via `shipit tree
create`, printing the Tree's path so Claude Code adopts it as the cwd. So BOTH
paths land in a real Tree, closing #139 *by construction* (the supported route can
no longer reach a native worktree). The fork, decided by
:func:`~shipit.harness.worktree_adapter.is_coordinator_launch` (`prompt_id`
absent ⇒ coordinator — see the spike evidence pinned below):

- **the coordinator's own launch** (top-level `claude --worktree <id>`, usually
  minted by `claude-start`): the session cwd is immutable after launch, so
  `--worktree` is the ONE pre-launch seam that can root the coordinator in its own
  isolated workspace — the **ephemeral session Tree** (ADR-0027): dir
  `<root>/<org>/<repo>/ephemeral/<id>`, branch `ephemeral/<id>`, base
  `origin/main`. Ephemeral-by-path, work-by-branch: the branch later moves to the
  real work; the dir stays.
- **an in-CC `Agent(isolation:"worktree")` helper spawn** (the original demoted
  path): resolve the holding branch (`harness.worktree_adapter`) from the epic
  (inferred from the coordinator's `cwd` branch, or the `SHIPIT_EPIC` override,
  then validated against the live `<epic>/umbrella` branch) + the spawn's id. The
  branch is **deferred** (`<epic>/agent-<id>`, base `origin/main`): a coarse
  holding branch the spawned agent self-branches off, because the hook cannot know
  the per-spawn work stream/role (ADR-0017).

THIN by design (mirrors `hook pretooluse`): read the `WorktreeCreate` payload on
stdin → fork on the discriminator → resolve the shape → create the Tree
(`tree.create`, provisioning gated on which manifests exist) → write its absolute
path to stdout.

**Fail-CLOSED — the OPPOSITE of `hook pretooluse`.** Claude Code adopts the path a
zero-exit hook prints and aborts the spawn (or the launch) on a non-zero exit. A
silent fallback to a native worktree would re-open #139, so ANY failure here (bad
payload, not in a checkout, a git/gh/provision error) logs at **ERROR** with the
exception attached (the fail-closed canon in :mod:`shipit.verbs.hook` — the abort
is a propagating failure, and the log is the durable record), prints a diagnostic
to **stderr**, writes NOTHING to stdout, and exits non-zero — it fails loud
rather than escaping to a native worktree.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import sys
from typing import TextIO

import click

from ... import git, identity
from ...harness import worktree_adapter
from ...tree.create import create_from_source, new_agent_hash
from ...tree.layout import TreeSpec, sanitize_slug

logger = logging.getLogger("shipit.hook")

#: Bytes of randomness behind a synthesized agent id when the payload carries none
#: → 8 hex chars, enough to keep concurrent marker-less spawns from colliding on a
#: holding branch.
_ID_BYTES = 4


@click.command(name="worktreecreate")
def cmd() -> None:
    """Provision a Tree for a `WorktreeCreate` caller; print its path.

    Serves both a top-level `claude --worktree` launch (→ the coordinator's
    ephemeral session Tree, ADR-0027) and an in-CC `Agent(isolation:"worktree")`
    spawn (→ the branch-deferred holding Tree, ADR-0017). Reads the payload as
    JSON on stdin and writes the new Tree's absolute path to stdout (which Claude
    Code adopts as the cwd). Exits non-zero on any failure — fail-CLOSED, so a
    failure never falls back to a native worktree.
    """
    raise SystemExit(run())


def run(stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
    """Parse stdin → fork on the caller → create Tree → print path. Fail-CLOSED.

    Returns 0 after printing the Tree path on success; returns 1 (printing a
    diagnostic to stderr and NOTHING to stdout) on any error, so Claude Code aborts
    the spawn/launch rather than minting a native worktree.
    """
    out = stdout if stdout is not None else sys.stdout
    payload: object = None
    try:
        raw = (stdin if stdin is not None else sys.stdin).read()
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError(f"WorktreeCreate payload is not an object: {payload!r}")
        if worktree_adapter.is_coordinator_launch(payload):
            # The coordinator's own `--worktree` launch → its ephemeral session
            # Tree (ADR-0027): ephemeral/<id> off origin/main, dir leaf = the id.
            path = _create_tree(ephemeral=_session_id(payload))
        else:
            # An in-CC helper spawn → the branch-deferred holding branch (ADR-0017).
            path = _create_tree(branch=_resolve_branch(payload))
    except Exception as exc:  # noqa: BLE001 — fail-CLOSED: any error aborts the spawn.
        # The fail-CLOSED failure arm (hook canon, shipit.verbs.hook): the abort
        # is a propagating failure → ERROR with the exception attached and the
        # domain keys the payload yields; the stderr print below is the hook
        # protocol's user-facing surface, never the only record.
        logger.error(
            "worktreecreate hook failed (aborting spawn)",
            exc_info=True,
            extra=_domain_keys(payload),
        )
        print(f"shipit hook worktreecreate: {exc}", file=sys.stderr)
        return 1
    out.write(path + "\n")
    return 0


def _domain_keys(payload: object) -> dict[str, str]:
    """The domain keys derivable from a (possibly unparsed) payload, for the abort record.

    Nothing is bound via :mod:`shipit.logcontext` this early in a hook process, so
    the abort record carries whatever the payload itself yields: ``session`` from
    ``session_id`` when the payload parsed to an object that has one. A failure
    before the parse (unreadable stdin, malformed JSON) derives nothing — the
    record still carries the exception, which is the diagnostic that matters.
    """
    if not isinstance(payload, dict):
        return {}
    sid = payload.get("session_id")
    return {"session": sid} if isinstance(sid, str) and sid else {}


def _session_id(payload: dict[str, object]) -> str:
    """The session id a coordinator launch's ephemeral Tree is named after.

    The payload's `name` is the `--worktree` value verbatim (`sess-<utc>-<pid>`
    when minted by `claude-start`, anything when hand-passed). The pure planner
    (:func:`~shipit.tree.layout.ephemeral_branch`) normalizes AND validates it, so
    the raw value is passed through untouched — except when it would not survive
    normalization (missing, empty, or all ref-forbidden characters), where a random
    id is synthesized instead: a launch is never blocked on a degenerate `-w`
    value, mirroring the helper path's missing-`name` fallback.
    """
    raw = str(payload.get("name") or "")
    return raw if sanitize_slug(raw) else secrets.token_hex(_ID_BYTES)


def _resolve_branch(payload: dict[str, object]) -> str:
    """The holding branch for this spawn: `<epic>/agent-<id>` (epic inferred from cwd).

    The id comes from the payload's **`name`** field — Claude Code's own throwaway
    spawn id, e.g. `"name": "agent-a567b7e2…"` — normalized to a safe ref component;
    if it normalizes to nothing a random id is synthesized so the spawn is never
    blocked on a missing name.

    The epic is inferred from **live git state** (#173): the coordinator's spawning
    branch — read by probing the branch of the payload's `cwd` — already encodes the
    epic per ADR-0016 (`EPIC/WSnn`), so its prefix before the first `/` is the epic
    (`TRE04/WS01` → `TRE04`). The `SHIPIT_EPIC` env var stays supported only as an
    optional explicit override (wins over the inferred branch) for the rare
    cross-epic spawn. Both the git probe and the prefix extraction degrade to `None`
    on a detached / no-slash / unreadable branch or a missing `cwd`, falling back
    safely to an epic-less branch.

    The candidate epic (from the branch prefix OR the override) is then **validated
    against the real `<epic>/umbrella` branch** (:func:`_validated_epic`): only a
    prefix that names an actual epic (its umbrella exists) namespaces the branch, so a
    coordinator on an ordinary `feature/foo` — or an override naming a dead epic —
    degrades to the same safe epic-less fallback rather than minting a bogus
    `feature/agent-…` holding branch.

    **Verified WorktreeCreate payload contract (live probes, Claude Code 2.1.196
    and 2.1.198 — see `docs/dev/ses02-worktreecreate-discriminator-spike.md`).**
    For an in-CC spawn, CC fires the hook with `{session_id, transcript_path, cwd,
    prompt_id, hook_event_name, name}`; the spawn-id field is **`name`** (value
    `agent-<agentId>`), NOT `worktree_name` (an earlier guess that is always absent,
    so reading it always fell through to the random-id fallback and silently broke
    the agent-id→branch link). A top-level `claude --worktree` launch fires the SAME
    payload **minus `prompt_id`** and with `name` = the `--worktree` value verbatim
    — `prompt_id`'s absence is the coordinator-vs-helper discriminator
    (:func:`~shipit.harness.worktree_adapter.is_coordinator_launch`), so this
    function only ever sees helper payloads. `cwd` is the coordinator's working
    dir, used to infer the epic. CC then adopts the **bare path printed to stdout**
    as the cwd (subagent or root session) **without validating it** — so a
    dissociated clone path is adopted verbatim, which is exactly how this
    fail-closed adapter relocates both callers into a Tree. (This contract was lost
    between the #139 spike and WS04; it is pinned here so it cannot be lost again.)
    """
    raw_id = str(payload.get("name") or "")
    agent_id = worktree_adapter.normalize_agent_id(raw_id) or secrets.token_hex(
        _ID_BYTES
    )
    override = os.environ.get(worktree_adapter.EPIC_MARKER_ENV)
    candidate = worktree_adapter.resolve_epic(override, _spawn_branch(payload))
    epic = _validated_epic(candidate, payload)
    return worktree_adapter.resolve_branch(epic, agent_id)


def _validated_epic(candidate: str | None, payload: dict[str, object]) -> str | None:
    """Confirm `candidate` names a REAL epic before it namespaces the holding branch.

    The pure resolver only *extracts* a candidate epic — the spawning branch's prefix,
    or the `SHIPIT_EPIC` override — and cannot tell `TRE04` (a real epic) from
    `feature` (a coordinator merely sitting on `feature/foo`). The semantic test for
    "is `<candidate>` a real epic?" is "does `<candidate>/umbrella` exist as a branch?"
    (ADR-0016: every epic has an umbrella). This validates it with a LOCAL ref lookup
    (`git.epic_umbrella_exists`, no network) in the coordinator's checkout. The SAME
    validation applies to an explicit override, so an override naming a non-existent
    epic degrades just like an inferred non-epic prefix — consistent safe-degrade.

    Returns the candidate only when its umbrella exists; otherwise `None` → the safe
    epic-less `agent-<id>` fallback. `None` in, `None` out (nothing to validate).
    """
    if candidate is None:
        return None
    cwd = _ref_check_cwd(payload)
    if cwd is None:
        return None
    return candidate if git.epic_umbrella_exists(candidate, cwd=cwd) else None


def _ref_check_cwd(payload: dict[str, object]) -> str | None:
    """A checkout of the repo to run the umbrella-existence ref lookup in.

    Prefers the payload `cwd` (the coordinator's checkout — the same place the
    spawning branch was read), falling back to the ambient hook checkout
    (`git.repo_root()`) so an override-only spawn that carries no `cwd` can still be
    validated. `None` when neither resolves, degrading the epic to the safe epic-less
    fallback rather than guessing.
    """
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd:
        return cwd
    return git.repo_root()


def _spawn_branch(payload: dict[str, object]) -> str | None:
    """The coordinator's current branch — the live state the epic is inferred from.

    Probes `git rev-parse --abbrev-ref HEAD` in the payload's `cwd` via
    :func:`git.current_branch`, which already yields `None` on a detached/unborn
    HEAD or any git error. Returns `None` when the payload carries no usable `cwd`,
    so a malformed payload degrades to the epic-less fallback rather than crashing
    the hook.
    """
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        return None
    return git.current_branch(cwd=cwd)


def _create_tree(*, branch: str | None = None, ephemeral: str | None = None) -> str:
    """Provision the Tree for one shape from the ambient checkout; return its path.

    The two shapes this hook mints: a freeform-`branch` spec (the helper spawn's
    holding branch) or an `ephemeral` spec (the coordinator's session Tree — the
    planner resolves the `ephemeral/<id>` dir/branch/base, ADR-0027). Resolves repo
    identity at the git boundary — the canonical, case-normalized
    :class:`shipit.identity.Repo`, derived locally from the origin remote
    (ADR-0024) — hands the :class:`TreeSpec` to the orchestrator, and returns the
    dissociated clone's path — provisioned like any Tree (`tree.create`, gated on
    which manifests exist). Raises on any failure — a missing checkout OR an
    unparseable origin remote (`ValueError`) — so :func:`run` fails closed; there
    is no native-worktree fallback.
    """
    root = git.repo_root()
    if not root:
        raise RuntimeError("not inside a git checkout — cannot provision a Tree")
    spec = TreeSpec(
        repo=identity.resolve_repo(root),
        agent_hash=new_agent_hash(),
        branch=branch,
        ephemeral=ephemeral,
    )
    tree = create_from_source(spec, source_repo=root)
    return tree.path
