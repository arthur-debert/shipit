"""Branch-identity derivation (ADR-0032 / LOG04) — a pure parse from a branch
name to the ``(epic, ws)`` correlation it carries, or nothing.

Shipit's branch grammar is slash-namespaced (ADR-0016 / naming.lex §3), and two
of its forms carry dev-cycle identity: a work stream is ``EPIC/WSnn`` and the
epic's integration branch is ``EPIC/umbrella``. This module is the ONE reader
of that identity — shared by the PR verbs (which derive ``epic``/``ws`` from a
target PR's head branch at the fetch seam) and the constrained emit verb
(:mod:`shipit.verbs.logevent`, which also serves the hook tier: the managed
post-commit hook emits through it) — so the parse can never drift per call
site,
and it mirrors the ONE writer (:func:`shipit.tree.layout.work_stream_branch` /
:func:`shipit.tree.layout.epic_umbrella_base`) by construction: whatever those
build, this reads back.

Pure and total: no git, no I/O, and NO raising — a branch name is data from
the wire (a PR's ``headRefName``), so an out-of-grammar name (a standalone
issue branch ``issues/375/work``, a freeform ``spike/foo``, ``main``, garbage)
derives to NOTHING rather than an error. Absent identity stays absent
(:mod:`shipit.logcontext`'s present-when-bound contract): the caller binds the
``None`` halves away, never a placeholder.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: The two-segment work-stream form ``EPIC/WSnn`` — the epic code is a single
#: alphanumeric token (the same shape :data:`shipit.tree.layout._EPIC_CODE`
#: enforces at the writer) and the index is the zero-padded ``WSnn`` display
#: form, two digits minimum (``work_stream_branch`` formats ``%02d``, so an
#: index past 99 legitimately widens to ``WS100``). Anchored: three segments,
#: an empty epic, or a non-``WS`` leaf simply do not match.
_WORK_STREAM = re.compile(r"^(?P<epic>[A-Za-z0-9]+)/WS(?P<ws>\d{2,})$")

#: The epic umbrella form ``EPIC/umbrella`` — the epic's integration branch
#: (ADR-0016: the umbrella name dodges the bare-``EPIC`` ref collision). It
#: carries the epic identity but no Work Stream.
_UMBRELLA = re.compile(r"^(?P<epic>[A-Za-z0-9]+)/umbrella$")


@dataclass(frozen=True)
class BranchIdentity:
    """The dev-cycle identity a branch name carries — either half may be absent.

    ``epic`` is the verbatim code string (``RVW01``); ``ws`` is the Work Stream
    index as an **int** (``WS01`` → ``1`` — the display form is never data,
    ADR-0032). An umbrella branch yields epic only; an out-of-grammar branch
    yields neither. Both halves feed :func:`shipit.logcontext.bind` directly:
    ``None`` values drop there, so absent identity never lands on a record.
    """

    epic: str | None = None
    ws: int | None = None


#: The parse result for every branch that carries no dev-cycle identity.
NOTHING = BranchIdentity()


def derive(branch: object) -> BranchIdentity:
    """The ``(epic, ws)`` identity of ``branch``, or :data:`NOTHING`.

    Total on ANY input: the branch name is wire data (a PR node's
    ``headRefName``), so a non-string (a missing key's ``None``, API drift)
    derives to nothing rather than raising — the caller is a logging seam and
    must never crash on malformed identity.

    The full truth table (pinned by ``tests/test_branchid.py``):

    - ``EPIC/WSnn`` → ``(EPIC, nn)`` — the work-stream form, index as int;
    - ``EPIC/umbrella`` → ``(EPIC, None)`` — the epic's integration branch;
    - everything else → :data:`NOTHING` — standalone-issue branches
      (``issues/375/work``), ephemeral session branches, freeform names,
      ``main``, the empty string, garbage.

    A ``WS00`` leaf is out of grammar (the writer rejects a non-positive index)
    and derives to nothing rather than minting a zero Work Stream: this parser
    reflects only identity the grammar could actually have written.
    """
    if not isinstance(branch, str):
        return NOTHING
    if match := _WORK_STREAM.fullmatch(branch):
        ws = int(match.group("ws"))
        if ws < 1:
            return NOTHING
        return BranchIdentity(epic=match.group("epic"), ws=ws)
    if match := _UMBRELLA.fullmatch(branch):
        return BranchIdentity(epic=match.group("epic"))
    return NOTHING
