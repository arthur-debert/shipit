"""``tree/removal`` — target resolution and the never-lose-work gate (ADR-0030).

The ``tree remove`` verb's promoted domain half. A Tree is a disposable,
fully-independent clone, so removing it is usually just deleting its directory
— EXCEPT when the delete would discard work living ONLY in that clone. This
module owns that decision as values:

- :func:`resolve_target` picks the ONE Tree a target names (full path or dir
  leaf), refusing unknown and ambiguous targets with the typed
  :class:`RemovalError` refusal.
- :func:`removal_risk` + :func:`gate` are the pure gating: given the record
  and the invocation facts (``assume_yes``, ``interactive``) they return a
  typed :class:`Gate` outcome — proceed / confirm-first / refuse — with the
  prompt or refusal text attached. No callables, no TTY, no filesystem: the
  whole truth table is unit-testable as values in, values out. The PROMPT
  itself (a terminal concern) stays at the verb, which acts on the outcome.
- :func:`remove` is the effectful apply: delete the one matched Tree through
  the reclaim funnel, mapping a filesystem failure to the same typed refusal
  (with the durable ERROR twin attached, ADR-0029).

:class:`RemovalError` is a domain refusal in the ADR-0030 known set: the
:func:`~shipit.verbs._errors.cli_errors` shell renders it as ``error: …`` +
exit 1.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .readonly import remove_tree
from .registry import TreeRecord

logger = logging.getLogger("shipit.tree")


class RemovalError(Exception):
    """A Tree removal that must not happen — the typed domain refusal.

    Raised for every no-go outcome: no Tree matches the target, more than one
    does (never guess which to delete), a risky remove cannot be confirmed, or
    the delete itself failed. Part of the error shell's KNOWN set (ADR-0030),
    so verbs carry no removal-specific ``try/except``.
    """


class GateAction(Enum):
    """What the removal gate decided: the three typed outcomes of :func:`gate`."""

    #: Safe (or ``--yes``): delete without asking.
    PROCEED = "proceed"
    #: Risky but interactive: ask first — :attr:`Gate.prompt` is the question.
    CONFIRM = "confirm"
    #: Risky, no TTY, no ``--yes``: refuse — :attr:`Gate.reason` says why.
    REFUSE = "refuse"


@dataclass(frozen=True)
class Gate:
    """The typed gating outcome for one removal — decision plus its text.

    ``prompt`` is set exactly on :attr:`GateAction.CONFIRM` (the question the
    verb puts to the user); ``reason`` exactly on :attr:`GateAction.REFUSE`
    (the stderr-ready refusal). A :attr:`GateAction.PROCEED` carries neither.
    """

    action: GateAction
    prompt: str | None = None
    reason: str | None = None


def resolve_target(records: list[TreeRecord], target: str) -> TreeRecord:
    """The ONE Tree ``target`` names, by absolute path or by dir leaf. Pure.

    Matching on the basename lets a coordinator name a Tree by its short id
    (``7-aaaa``) without typing the whole central-root path; matching the full
    path stays exact and takes precedence (an exact path match is
    unambiguous). No match, or more than one, raises :class:`RemovalError` —
    an unknown target is not guessable and an ambiguous one must never pick a
    victim.
    """
    matches = _match_trees(records, target)
    if not matches:
        raise RemovalError(f"no Tree matching {target!r}")
    if len(matches) > 1:
        paths = ", ".join(record.path for record in matches)
        raise RemovalError(f"{target!r} is ambiguous — matches {paths}")
    return matches[0]


def _match_trees(records: list[TreeRecord], target: str) -> list[TreeRecord]:
    """Trees whose absolute path equals ``target`` or whose dir name equals it."""
    by_path = [record for record in records if record.path == target]
    if by_path:
        return by_path
    return [record for record in records if Path(record.path).name == target]


def removal_risk(record: TreeRecord) -> str | None:
    """Why removing ``record`` could lose work, as a short phrase — or ``None``
    if safe. Pure.

    A Tree is a disposable clone, so removal is normally silent; it is only
    worth a confirmation when the delete would discard work that exists ONLY
    in that clone: uncommitted/untracked changes (``dirty``) or commits not
    yet pushed to the upstream (``ahead > 0``). Everything reachable from the
    upstream survives the delete, so a clean, fully-pushed Tree returns
    ``None``. This is the whole risk-detection seam — it reuses the
    ``dirty``/``ahead`` the registry already derived through the ``gh``
    boundary, so there is no second shell-out to git.
    """
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
    """Decide whether removing ``record`` may proceed — the typed outcome. Pure.

    A safe Tree (no :func:`removal_risk`) or ``assume_yes`` (the ``--yes``
    flag) → :attr:`~GateAction.PROCEED`. A risky Tree with a terminal
    (``interactive``) → :attr:`~GateAction.CONFIRM`, carrying the prompt the
    verb asks. A risky Tree without a terminal and without ``--yes`` →
    :attr:`~GateAction.REFUSE`, carrying the refusal message — the safe
    non-interactive default: never silently destroy work, never block on a
    prompt nobody will answer.
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
    """Delete the one matched Tree through the reclaim funnel. Effectful.

    A failed delete (a read-only file, a lock) becomes the typed
    :class:`RemovalError` refusal after landing its durable ERROR twin with
    the exception attached (ADR-0029); the successful removal itself is
    recorded by :func:`~shipit.tree.readonly.remove_tree` (the funnel every
    reclaim passes through).
    """
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
