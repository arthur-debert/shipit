"""`shipit hook worktreecreate` — provision a Tree for a `WorktreeCreate` caller
and print its path, so neither caller lands in a native worktree.
See docs/adr/0017-shipit-owned-subagent-spawning.md.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import sys
from typing import TextIO

import click

from ... import git, identity, workenv
from ...harness import worktree_adapter
from ...tree.create import create_from_source, new_tree_id, new_tree_naming
from ...tree.layout import TreeSpec, is_full_uuid, sanitize_slug

logger = logging.getLogger("shipit.hook")

_ID_BYTES = 4


@click.command(name="worktreecreate")
def cmd() -> None:
    """Provision a Tree for a `WorktreeCreate` caller; print its path."""
    raise SystemExit(run())


def run(stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
    """0 after printing the Tree path; 1 on any error (fail-CLOSED: stdout stays empty)."""
    out = stdout if stdout is not None else sys.stdout
    payload: object = None
    try:
        raw = (stdin if stdin is not None else sys.stdin).read()
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError(f"WorktreeCreate payload is not an object: {payload!r}")
        if worktree_adapter.is_coordinator_launch(payload):
            path = _create_tree(
                ephemeral=_session_id(payload),
                tree_id=_coordinator_tree_id(payload),
            )
        else:
            path = _create_tree(
                branch=_resolve_branch(payload),
                tree_id=new_tree_id(),
            )
    except Exception as exc:  # noqa: BLE001 — fail-CLOSED: any error aborts the spawn.
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
    if not isinstance(payload, dict):
        return {}
    sid = payload.get("session_id")
    return {"session": sid} if isinstance(sid, str) and sid else {}


def _session_id(payload: dict[str, object]) -> str:
    """The session id a coordinator launch's ephemeral Tree is named after."""
    raw = str(payload.get("name") or "")
    return raw if sanitize_slug(raw) else secrets.token_hex(_ID_BYTES)


def _coordinator_tree_id(payload: dict[str, object]) -> str:
    """The payload's ``session_id`` when it is a full UUID, else a freshly minted one."""
    sid = str(payload.get("session_id") or "")
    return sid if is_full_uuid(sid) else new_tree_id()


def _resolve_branch(payload: dict[str, object]) -> str:
    """The holding branch `<epic>/agent-<id>`; the payload's spawn-id field is `name`."""
    raw_id = str(payload.get("name") or "")
    agent_id = worktree_adapter.normalize_agent_id(raw_id) or secrets.token_hex(
        _ID_BYTES
    )
    override = os.environ.get(worktree_adapter.EPIC_MARKER_ENV)
    candidate = worktree_adapter.resolve_epic(override, _spawn_branch(payload))
    epic = _validated_epic(candidate, payload)
    return worktree_adapter.resolve_branch(epic, agent_id)


def _validated_epic(candidate: str | None, payload: dict[str, object]) -> str | None:
    """`candidate` when its `<candidate>/umbrella` branch exists, else `None`."""
    if candidate is None:
        return None
    cwd = _ref_check_cwd(payload)
    if cwd is None:
        return None
    return candidate if git.epic_umbrella_exists(candidate, cwd=cwd) else None


def _ref_check_cwd(payload: dict[str, object]) -> str | None:
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd:
        return cwd
    return git.repo_root()


def _spawn_branch(payload: dict[str, object]) -> str | None:
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        return None
    return git.current_branch(cwd=cwd)


def _create_tree(
    *, tree_id: str, branch: str | None = None, ephemeral: str | None = None
) -> str:
    """Provision the Tree for one shape from the ambient checkout; raises on any failure."""
    root = git.repo_root()
    if not root:
        raise RuntimeError("not inside a git checkout — cannot provision a Tree")
    spec = TreeSpec(
        repo=identity.resolve_repo(root),
        **new_tree_naming("claude", tree_id=tree_id),
        branch=branch,
        ephemeral=ephemeral,
    )
    tree = create_from_source(spec, source_repo=root)
    if ephemeral is not None:
        session_env = workenv.resolve_session_env(
            repo=spec.repo,
            tree_path=tree.path,
            branch=tree.branch,
            base=tree.base,
            activation=None,
        )
        logger.info(
            "worktreecreate: work env resolved — %s routing for coordinator session tree",
            session_env.routing.value,
            extra=workenv.resolution_record(
                session_env,
                boundary="session.worktreecreate",
                role="coordinator",
            ),
        )
    return tree.path
