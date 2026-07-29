"""``tree/removal`` — ``tree remove`` target resolution and its never-lose-work gate."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .readonly import remove_tree
from .registry import TreeRecord

logger = logging.getLogger("shipit.tree")


class RemovalError(Exception):
    """A Tree removal that must not happen — the typed domain refusal the error
    shell renders as ``error: …`` + exit 1."""


class GateAction(Enum):
    """What the removal gate decided."""

    PROCEED = "proceed"
    CONFIRM = "confirm"
    REFUSE = "refuse"


@dataclass(frozen=True)
class Gate:
    """A gating outcome: ``prompt`` is set exactly on ``CONFIRM``, ``reason`` exactly
    on ``REFUSE``."""

    action: GateAction
    prompt: str | None = None
    reason: str | None = None


def resolve_target(records: list[TreeRecord], target: str) -> TreeRecord:
    """The ONE Tree ``target`` names, by absolute path or by dir leaf. Pure.

    No match, or more than one, raises :class:`RemovalError`.
    """
    matches = _match_trees(records, target)
    if not matches:
        raise RemovalError(f"no Tree matching {target!r}")
    if len(matches) > 1:
        paths = ", ".join(record.path for record in matches)
        raise RemovalError(f"{target!r} is ambiguous — matches {paths}")
    return matches[0]


def _match_trees(records: list[TreeRecord], target: str) -> list[TreeRecord]:
    """Trees whose absolute path equals ``target`` (exact wins) or whose dir name does."""
    by_path = [record for record in records if record.path == target]
    if by_path:
        return by_path
    return [record for record in records if Path(record.path).name == target]


def removal_risk(record: TreeRecord) -> str | None:
    """Why removing ``record`` could lose work, as a short phrase — ``None`` if safe."""
    reasons: list[str] = []
    if record.dirty:
        reasons.append("uncommitted changes")
    if record.ahead:
        plural = "s" if record.ahead != 1 else ""
        reasons.append(f"{record.ahead} unpushed commit{plural}")
    if not reasons:
        return None
    return " and ".join(reasons)


def gate(record: TreeRecord, *, assume_yes: bool, interactive: bool) -> Gate:
    """Decide whether removing ``record`` may proceed. Pure.

    Safe or ``assume_yes`` → PROCEED; risky and interactive → CONFIRM; risky with
    neither → REFUSE, so a non-interactive run never silently destroys work nor
    blocks on a prompt nobody will answer.
    """
    risk = removal_risk(record)
    if risk is None or assume_yes:
        return Gate(action=GateAction.PROCEED)
    if interactive:
        return Gate(
            action=GateAction.CONFIRM,
            prompt=f"Tree {record.path} has {risk}; remove anyway?",
        )
    return Gate(
        action=GateAction.REFUSE,
        reason=(
            f"{record.path} has {risk}; refusing to remove non-interactively "
            "without --yes"
        ),
    )


def remove(record: TreeRecord) -> None:
    """Delete the matched Tree; a filesystem failure becomes :class:`RemovalError`."""
    try:
        remove_tree(record.path)
    except OSError as exc:
        logger.error(
            "tree remove failed for %s",
            record.path,
            exc_info=True,
            extra={"tree": record.path},
        )
        raise RemovalError(f"could not remove {record.path}: {exc}") from exc
