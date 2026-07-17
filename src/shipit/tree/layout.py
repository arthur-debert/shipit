"""``tree/layout`` — pure resolution of a Tree request into a concrete plan.

``plan(spec) -> TreePlan`` is the deep, pure heart of Tree creation: given a
:class:`TreeSpec` it resolves the three coordinates a clone needs — the **dir**
on disk, the **branch** to check out, and the **base** ref to cut it from — with
no I/O, so the truth table is unit-tested directly (Testing Decisions in the PRD).

:func:`plan` resolves EVERY spec shape — ``--issue N [--session S] [--slug S]``,
``--epic E --ws N [--slug S]``, freeform ``--branch NAME`` with an optional
base override supplied by callers that have probed a remote head, and the coordinator's
``ephemeral`` session Tree
(naming.lex §3; ADR-0027). :class:`TreeSpec` stays a single typed entry point:
adding a shape is adding a field plus a branch in :func:`plan`, not reshaping
callers.

The load-bearing invariants the tests pin (from the PRD and ADR-0074):

- the **dir is ONE flat, self-describing leaf** — ``<repo>-<agent>-<timestamp>-<id>``
  (:func:`tree_leaf`), the SAME shape for every Tree, with no owner and no kind
  segment. The leaf records who/when (repo name, backend binary, ``%Y%m%d-%H%M%S``
  stamp, full-UUID id) while the branch/base carry what the Tree is *for*; the id
  disambiguates two Trees that share one branch. Tree identity is resolved from the
  origin remote, never parsed back out of the path;
- the **git branch form is slash-namespaced** (naming.lex §3): a work stream is
  ``EPIC/WSnn`` cut from ``origin/EPIC/umbrella``, siblings under ``refs/heads/EPIC/``
  — the umbrella name dodges the bare-``EPIC`` ref/dir collision. The plain-language
  identifier stays hyphenated (``EPIC-WSnn`` in titles/logs); only the branch slashes; and
- slug/session sanitization lives HERE (:func:`sanitize_slug`), so every shape that
  grows a slug or session gets the same normalization for free — an allow-list to
  ``[a-z0-9-]`` that guarantees a valid git ref component (git-check-ref-format), not
  just a separators denylist.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..identity import Repo


class LayoutError(ValueError):
    """The central root is misconfigured — a typed domain refusal (ADR-0030).

    Raised by :func:`central_root` for a relative ``SHIPIT_TREES_ROOT``: a
    config problem, not a bug, so the error shell
    (:func:`~shipit.verbs._errors.cli_errors`) renders it as a clean
    ``error: …`` + exit 1 on every fleet verb. Subclasses :class:`ValueError`
    so the create pipeline's existing exception mapping keeps catching it
    unchanged.
    """


#: Env override for the central root all Trees live under. Unset → the default
#: below. A module constant + env override is the whole config surface for WS01
#: (the PRD's "keep it simple"); a richer config binding can supersede it later.
CENTRAL_ROOT_ENV = "SHIPIT_TREES_ROOT"

#: The default central root: one predictable place OUTSIDE every repo, so cleanup
#: and inspection are uniform across repos and agents — never inside ``.claude/``
#: (PRD Solution; ADR-0014).
DEFAULT_CENTRAL_ROOT = "~/workspace/trees"

#: The birth-branch prefix for the coordinator's **ephemeral session Tree**
#: (ADR-0027): the branch starts as ``ephemeral/<id>`` and then MOVES to the real
#: work (``EPIC/umbrella``, ``docs/<slug>``, …) as the session discovers what it is
#: doing — the flat dir (:func:`tree_leaf`) stays put and records who/when. Only the
#: BRANCH carries this prefix now; the flat Tree dir has no kind segment (ADR-0074).
EPHEMERAL_BRANCH_PREFIX = "ephemeral"

#: The Tree dir leaf's ``<agent>`` slot must be a lowercase alphanumeric backend
#: BINARY name — ``claude`` / ``codex`` / ``agy``, the three backends shipit
#: supports (ADR-0074; naming.lex §4). Antigravity's ``--backend`` token is
#: ``antigravity``, but its binary and funnel agent name are ``agy``, and the binary
#: is what matches ``claude`` and ``codex`` — so the binary name is what lands in the
#: leaf. Minted from the backend identity (:mod:`shipit.agent.backend`) at each
#: creation path, never smuggled in as a session-id prefix.
_AGENT_TOKEN = re.compile(r"[a-z0-9]+")

#: The ``<timestamp>-<id>`` tail of a flat Tree leaf: a ``%Y%m%d-%H%M%S`` stamp
#: (``\d{8}-\d{6}``) followed by a full UUID, anchored to the END of the name. The
#: leaf's HEAD (``<repo>-<agent>``) may itself carry hyphens, so ``tree list``'s
#: created column recovers the stamp by matching this tail, not by splitting on ``-``.
#: An OLD nested Tree's leaf (WS02 reclaims those by attrition) does not match, so it
#: reads ``-`` in the column rather than a wrong date.
_CREATED_TAIL = re.compile(
    r"(?P<created>\d{8}-\d{6})-"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

#: The strptime format a flat leaf's ``<timestamp>`` must parse under — ``%Y%m%d-%H%M%S``
#: UTC (ADR-0074 / naming.lex §4). Shared by :func:`is_created_stamp` and the minting
#: side (:func:`shipit.tree.create.tree_created_stamp`) so validator and generator agree.
_CREATED_FORMAT = "%Y%m%d-%H%M%S"

#: A flat leaf's ``<timestamp>`` SHAPE (``\d{8}-\d{6}``). Shape is necessary but not
#: sufficient — :func:`is_created_stamp` additionally proves it is a real calendar time
#: (``strptime``), so an in-shape but impossible stamp (month 13, hour 25) is rejected.
_CREATED_STAMP_SHAPE = re.compile(r"\d{8}-\d{6}")

#: A flat leaf's ``<id>``: a full 8-4-4-4-12 hex UUID (ADR-0074 / naming.lex §4). Never a
#: pid (reused — one token eventually names two sessions) and never truncated
#: (``claude --resume`` rejects a prefix).
_FULL_UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)

#: The WHOLE flat leaf ``<repo>-<agent>-<timestamp>-<id>``, anchored end to end (used
#: by :func:`parse_flat_leaf`). ``<repo>`` may itself carry hyphens, so it is matched
#: non-greedily up to the ``<agent>`` (a lowercase alphanumeric backend binary) that
#: immediately precedes the ``<timestamp>-<id>`` tail; only the hex ``<id>`` is
#: case-insensitive, so ``<agent>`` stays lowercase by its own character class.
_FLAT_LEAF = re.compile(
    r"(?P<repo>.+?)-"
    r"(?P<agent>[a-z0-9]+)-"
    r"(?P<created>\d{8}-\d{6})-"
    r"(?P<tree_id>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)


def is_full_uuid(value: object) -> bool:
    """Whether ``value`` is a full 8-4-4-4-12 hex UUID — a flat leaf's ``<id>`` (ADR-0074).

    The single predicate every flat-leaf boundary shares for "is this a real Tree id?":
    :func:`tree_leaf` (guarding the dir it builds), the ``WorktreeCreate`` coordinator
    arm (guarding the harness ``session_id`` it adopts as ``<id>`` —
    :func:`shipit.verbs.hook.worktreecreate._coordinator_tree_id`), and
    :func:`parse_flat_leaf` (recognizing a conforming leaf). A pid or a truncated prefix
    is rejected: reuse and ``claude --resume`` both need the FULL UUID. Non-``str`` is
    ``False``, never a raise.
    """
    return isinstance(value, str) and _FULL_UUID.fullmatch(value) is not None


def is_created_stamp(value: object) -> bool:
    """Whether ``value`` is a strict ``%Y%m%d-%H%M%S`` UTC stamp — a flat leaf's ``<timestamp>``.

    Both SHAPE (``\\d{8}-\\d{6}``) and real-calendar-time (``strptime`` under
    :data:`_CREATED_FORMAT`), so an in-shape but impossible stamp (month 13, hour 25) is
    rejected too. The companion of :func:`is_full_uuid` for the ``<timestamp>`` slot,
    shared by the same boundaries. Non-``str`` is ``False``, never a raise.
    """
    if not isinstance(value, str) or not _CREATED_STAMP_SHAPE.fullmatch(value):
        return False
    try:
        datetime.strptime(value, _CREATED_FORMAT)
    except ValueError:
        return False
    return True


@dataclass(frozen=True)
class FlatLeaf:
    """The four coordinates recovered from a flat Tree dir leaf (ADR-0074 / naming.lex §4).

    The parse is for SHAPE recognition, not identity: repo identity is still resolved
    from the origin remote, never trusted from a path (see :class:`TreeSpec`). ``repo``
    and ``agent`` are exposed for completeness; the load-bearing consumer reads
    ``tree_id`` (the session/resume handle) once :func:`parse_flat_leaf` has confirmed
    the leaf conforms.
    """

    repo: str
    agent: str
    created: str
    tree_id: str


def parse_flat_leaf(name: object) -> FlatLeaf | None:
    """Parse a dir ``name`` as a flat Tree leaf, or ``None`` when it does not conform.

    The single recognizer of "is this directory a flat Tree?" (ADR-0074): a name is a
    flat leaf IFF it is exactly ``<repo>-<agent>-<timestamp>-<id>`` with ``<agent>`` a
    lowercase alphanumeric backend binary name, ``<timestamp>`` a real
    ``%Y%m%d-%H%M%S`` stamp (:func:`is_created_stamp`), and ``<id>`` a full UUID. Used by
    :mod:`shipit.session.current` to tell a flat Tree from an OLD nested Tree (which
    coexists by attrition) or an arbitrary non-Tree directory under the central root, so
    neither is mis-read as a Tree. Returns the parsed coordinates for the caller that
    wants the ``tree_id``; ``None`` otherwise. Never raises.
    """
    if not isinstance(name, str):
        return None
    match = _FLAT_LEAF.fullmatch(name)
    if match is None:
        return None
    created = match.group("created")
    if not is_created_stamp(created):
        return None
    return FlatLeaf(
        repo=match.group("repo"),
        agent=match.group("agent"),
        created=created,
        tree_id=match.group("tree_id"),
    )


def created_from_leaf(name: str) -> str | None:
    """The ``%Y%m%d-%H%M%S`` creation stamp encoded in a flat Tree leaf, or ``None``.

    Sources ``tree list``'s **created** column from the dir name (ADR-0074 / naming.lex
    §4): the flat leaf is ``<repo>-<agent>-<timestamp>-<id>``, so the stamp is the
    ``<timestamp>`` group of the ``<timestamp>-<uuid>`` tail (:data:`_CREATED_TAIL`).
    Returns ``None`` for any name that is not a flat leaf — an old nested Tree still
    coexisting under the root (WS02 reclaims those on its own schedule), so the column
    shows ``-`` rather than a fabricated date. This is a DISPLAY fact only; ``gc`` never
    reads it, because creation-age is not activity-age (ADR-0072).
    """
    match = _CREATED_TAIL.search(name)
    return match.group("created") if match else None


def tree_leaf(repo: Repo, agent: str, created: str, tree_id: str) -> str:
    """The FLAT, self-describing Tree dir leaf: ``<repo>-<agent>-<timestamp>-<id>``.

    ADR-0074 / naming.lex §4 — ONE shape for every Tree, no owner segment and no
    kind segment. ``repo`` contributes its NAME only (``shipit``); repo identity is
    resolved from the origin remote when needed (``_repo_slug``), never parsed back
    out of a path. ``agent`` is the backend BINARY name (``claude`` / ``codex`` /
    ``agy``); ``created`` is the ``%Y%m%d-%H%M%S`` UTC stamp, so a lexical sort is
    chronological within a repo; ``tree_id`` is a full UUID — never a pid (reused, so
    one token eventually names two sessions), never truncated (``claude --resume``
    rejects a prefix). Its PROVENANCE varies by creation path (the coordinator
    session Tree gets the harness session UUID so the dir name IS the resume handle;
    every other path mints its own), but the leaf never records which.

    Repo comes FIRST because it is the axis a human narrows on — ``ls | grep shipit``
    is the tooling-free narrowing this grammar exists to give. Every slot is validated
    at this ONE construction boundary: ``agent`` a lowercase alphanumeric backend binary
    token, ``created`` a strict ``%Y%m%d-%H%M%S`` stamp (:func:`is_created_stamp`), and
    ``tree_id`` a full UUID (:func:`is_full_uuid`, never a pid or a truncated prefix) —
    so a malformed leaf never reaches the filesystem and :func:`created_from_leaf` /
    :func:`parse_flat_leaf` can always recover the tail. Raises :class:`ValueError`.
    """
    if not isinstance(agent, str) or not _AGENT_TOKEN.fullmatch(agent):
        raise ValueError(
            "tree.layout.tree_leaf: agent must be a lowercase alphanumeric backend "
            f"binary name (claude/codex/agy, naming.lex §4); got {agent!r}."
        )
    if not is_created_stamp(created):
        raise ValueError(
            "tree.layout.tree_leaf: created must be a strict %Y%m%d-%H%M%S UTC stamp "
            f"(ADR-0074 / naming.lex §4); got {created!r}."
        )
    if not is_full_uuid(tree_id):
        raise ValueError(
            "tree.layout.tree_leaf: tree_id must be a full UUID (never a pid or a "
            f"truncated prefix; ADR-0074 / naming.lex §4); got {tree_id!r}."
        )
    return f"{repo.name}-{agent}-{created}-{tree_id}"


#: A slug/ref component keeps ONLY lowercase ASCII alphanumerics; EVERY run of any
#: other character collapses to a single ``-``. This is an ALLOW-list, not a
#: separators denylist: it catches the old separators (whitespace, ``/`` ``.`` ``:``)
#: AND every other character ``git check-ref-format`` forbids in a ref component —
#: ``~ ^ ? * [ \\``, space, the ``@{`` sequence, control chars, ``..`` runs, and a
#: trailing ``.lock`` (there is no ``.`` left at all). The output is a pure
#: ``[a-z0-9-]`` token (then trimmed of ``-``), so it is simultaneously a
#: filesystem-safe dir leaf AND a VALID git ref path component — a session/slug that
#: rides ``issues/<id>/<session>`` can never yield an invalid ref that only blows up
#: later inside ``tree create`` / ``spawn`` (codex CHANGES_REQUESTED).
_SLUG_UNSAFE = re.compile(r"[^a-z0-9]+")

#: An epic code is a human-assigned identifier (naming.lex §3: uppercase
#: ``THEME+NN``, e.g. ``HAR02``). Unlike a slug it is kept VERBATIM and becomes
#: BOTH a branch ref component (``E/WSnn``, base ``origin/E/umbrella``) and a path
#: segment (``epics/E/...``) — so it must be a single safe alphanumeric token.
#: Requiring a full alphanumeric match rejects the degenerate inputs reviewers
#: flagged at this invariant boundary: empty/whitespace codes (which would yield
#: refs like ``/WS02`` and ``origin//umbrella`` and a collapsed dir segment) and
#: path-unsafe values — separators or ``..`` — that could escape the central root
#: once the orchestrator runs ``dest.parent.mkdir(...)``.
_EPIC_CODE = re.compile(r"[A-Za-z0-9]+")


def epic_umbrella_base(epic: str) -> str:
    """The remote base ref a work stream Tree is cut from: ``origin/<E>/umbrella``.

    The single place that builds the epic-grouped base string, so the planner
    (:func:`_plan_epic_ws`) and any caller that must resolve the base WITHOUT going
    through :func:`plan` (the ``shipit spawn subagent`` verb, which fail-closes on
    the umbrella branch's existence before creating the Tree) agree by construction.

    A work stream ``E/WSnn`` and its siblings under ``refs/heads/E/`` are all cut
    from the epic's umbrella branch (naming.lex §3); the umbrella name dodges the
    bare-``E`` ref/dir collision. ``epic`` is validated as a single alphanumeric
    token (:data:`_EPIC_CODE`) — the same invariant :func:`_plan_epic_ws` pins — so
    an empty/whitespace or separator/``..`` code can never build a malformed
    ``origin//umbrella`` ref or a path-traversing one. The type is checked before
    the regex so a non-``str`` (e.g. ``None``) raises the documented
    :class:`ValueError` rather than an escaping ``TypeError`` — the fail-closed
    contract holds for ANY caller. Raises :class:`ValueError`.
    """
    if not isinstance(epic, str) or not _EPIC_CODE.fullmatch(epic):
        raise ValueError(
            "tree.layout.epic_umbrella_base: epic code must be a single alphanumeric "
            f"token (naming.lex §3 THEME+NN, e.g. 'HAR02'); got {epic!r}."
        )
    return f"origin/{epic}/umbrella"


def central_root() -> Path:
    """The central root every Tree lives under (env override, else the default).

    Expanded (``~`` and any ``$VARS``) and guaranteed absolute. Pure read of the
    environment — no directory is created here; the orchestrator makes the leaf.

    A non-absolute ``SHIPIT_TREES_ROOT`` is rejected with :class:`LayoutError`
    rather than resolved against the cwd: a relative root would place Trees under
    wherever ``shipit`` happens to be invoked from — potentially inside the source
    checkout — which breaks the central-root/isolation invariant this whole
    feature rests on (PRD Solution; ADR-0014). The default already expands to an
    absolute path.
    """
    raw = os.environ.get(CENTRAL_ROOT_ENV) or DEFAULT_CENTRAL_ROOT
    root = Path(os.path.expandvars(raw)).expanduser()
    if not root.is_absolute():
        raise LayoutError(
            f"{CENTRAL_ROOT_ENV} must be an absolute path so Trees live OUTSIDE "
            f"every repo (got {raw!r}); a relative root would place Trees under the "
            "current working directory and make cleanup cwd-dependent."
        )
    return root


def sanitize_slug(slug: str) -> str:
    """Normalize free text to the lowercase ``[a-z0-9-]`` form used in refs and dir leaves.

    Lowercases, then collapses every run of characters OUTSIDE ``[a-z0-9]`` — the old
    separators (whitespace, ``/`` ``.`` ``:``) AND every other git-ref-forbidden
    character (``~ ^ ? * [ \\``, space, ``@{``, control chars, ``..`` runs, a trailing
    ``.lock``) — to a single ``-``, and trims leading/trailing ``-``. The output is a
    pure ``[a-z0-9-]`` token, so it is simultaneously a filesystem-safe dir leaf and a
    VALID git ref path component: any NON-EMPTY result contains at least one
    alphanumeric and carries none of the characters/sequences ``git check-ref-format``
    rejects. An empty or all-unsafe input normalizes to ``""`` — the caller rejects it
    (e.g. :func:`issue_branch` fails loud so a bare ``issues/<id>/`` ref is never built).
    """
    collapsed = _SLUG_UNSAFE.sub("-", slug.strip().lower())
    return collapsed.strip("-")


def issue_branch(issue: int, session: str) -> str:
    """The standalone-issue branch: ``issues/<id>/<session>`` (session default ``work``).

    The single place that builds the slash-namespaced standalone-issue branch, so the
    planner (:func:`_plan_issue`) and any caller that must resolve the branch WITHOUT
    going through :func:`plan` (the ``shipit spawn subagent`` verb's reviewer path, which
    read-only-checks out an existing issue head) agree by construction — the analog of
    :func:`epic_umbrella_base` for the epic shape.

    Why the ``<session>`` suffix (and never a bare ``issues/<id>``): a bare branch would
    occupy ``refs/heads/issues/<id>`` as a git ref FILE, which cannot coexist with the
    ``refs/heads/issues/<id>/`` ref DIRECTORY a sibling session would need — the same
    file-vs-directory ref collision the epic umbrella name dodges (ADR-0016 / naming.lex
    §3). Keeping ``issues/<id>/`` a ref directory lets a +1 session on the same issue
    (``issues/<id>/onboard``) coexist with the default ``issues/<id>/work``.

    Both inputs are validated at this invariant boundary: ``issue`` must be a positive
    integer — the type is checked before the comparison (parity with
    :func:`work_stream_branch`) so a non-``int`` (e.g. ``None``) raises the documented
    :class:`ValueError` rather than an escaping ``TypeError`` from ``None < 1`` — (``click``
    accepts ``0``/negatives, but they yield out-of-grammar branches like ``issues/0/work``),
    and ``session`` must contain at least one alphanumeric
    character. The session is sanitized by :func:`sanitize_slug` — an allow-list to
    ``[a-z0-9-]`` that strips every git-ref-forbidden character (``~ ^ ? * [ \\`` , space,
    ``@{``, dots, control chars, …), so ``foo~bar`` → ``foo-bar`` and the resulting
    ``issues/<id>/<session>`` is ALWAYS a valid git ref, never one that only fails later
    inside ``tree create``/``spawn``. It becomes both a ref component and a dir-leaf
    component, so a session that sanitizes to EMPTY (``@{``, ``~``, all-separator …) is
    rejected — it would yield a bare ``issues/<id>/`` ref and reintroduce the collision.
    Both raise :class:`ValueError`.
    """
    if not isinstance(issue, int) or issue < 1:
        raise ValueError(
            "tree.layout.issue_branch: issue number must be a positive integer "
            f"(the issues/<id>/<session> grammar, naming.lex §3); got issue={issue!r}. "
            "Zero or negative values produce out-of-grammar branches like "
            "'issues/0/work'."
        )
    if not isinstance(session, str):
        # Parity with the issue guard above (and work_stream_branch): a non-str session
        # would hit `sanitize_slug(session).strip()` and raise an AttributeError, breaking
        # this function's documented "raises ValueError on invalid" contract. Reject it
        # cleanly instead.
        raise ValueError(
            "tree.layout.issue_branch: session must be a string "
            f"(the issues/<id>/<session> grammar, naming.lex §3); got {session!r}."
        )
    normalized = sanitize_slug(session)
    if not normalized:
        raise ValueError(
            "tree.layout.issue_branch: session must contain at least one alphanumeric "
            f"character (it becomes the issues/<id>/<session> ref/dir leaf); got "
            f"{session!r}, which sanitizes to an empty name — a bare 'issues/<id>/' ref "
            "that reintroduces the file-vs-directory collision the session suffix dodges."
        )
    return f"issues/{issue}/{normalized}"


def work_stream_branch(epic: str, ws: int) -> str:
    """The work-stream branch ``E/WSnn`` (validated) — the epic-shape analog of
    :func:`issue_branch`.

    The single place that builds the slash-namespaced work-stream branch AND validates
    its two user-controlled inputs, so the planner (:func:`_plan_epic_ws`) and any caller
    that must resolve the branch WITHOUT going through :func:`plan` (the ``shipit spawn
    subagent`` verb's reviewer path, which read-only-checks out an existing WS head) fail
    loud IDENTICALLY on a bad epic/ws — never silently yield a malformed ``/WS01`` from an
    empty epic. The type is checked before the regex so a non-``str`` (e.g. ``None``)
    raises the documented :class:`ValueError` rather than an escaping ``TypeError``.

    ``epic`` must be a single alphanumeric token (:data:`_EPIC_CODE`) — the code becomes
    both a branch ref component and a path segment, so empty/whitespace values and
    separators or ``..`` (a path-traversal risk) are rejected — and ``ws`` must be a
    positive integer (rejecting out-of-grammar ``WS00`` / ``WS-1``). Both raise
    :class:`ValueError`.
    """
    if not isinstance(epic, str) or not _EPIC_CODE.fullmatch(epic):
        raise ValueError(
            "tree.layout.work_stream_branch: epic code must be a single alphanumeric "
            f"token (naming.lex §3 THEME+NN, e.g. 'HAR02'); got {epic!r}. The code "
            "becomes both a branch ref and a path segment, so empty/whitespace values "
            "and separators or '..' (a path-traversal risk) are rejected."
        )
    if ws < 1:
        raise ValueError(
            "tree.layout.work_stream_branch: work stream number must be a positive "
            f"integer (the WSnn grammar, naming.lex §3); got ws={ws!r}. Zero or negative "
            "values produce out-of-grammar branches like 'WS00'/'WS-1'."
        )
    return f"{epic}/WS{ws:02d}"


def ephemeral_branch(session_id: str) -> str:
    """The session Tree's birth branch: ``ephemeral/<id>`` (validated + normalized).

    The single place that builds the ephemeral branch, the analog of
    :func:`issue_branch` / :func:`work_stream_branch` for the coordinator's session
    Tree (ADR-0027), so the planner (:func:`_plan_ephemeral`) and any caller that
    must name the branch WITHOUT going through :func:`plan` agree by construction.

    This is only the branch AT BIRTH: the session Tree is *ephemeral-by-path,
    work-by-branch*, so the branch is expected to move to the real work
    (``EPIC/umbrella``, ``docs/<slug>``, …) mid-session while the dir keeps the id.
    The slash form keeps every session branch grouped under the ``ephemeral/`` ref
    directory (:data:`EPHEMERAL_BRANCH_PREFIX`); the flat Tree dir no longer mirrors
    it (ADR-0074).

    The id is normalized by :func:`sanitize_slug` — it becomes both a ref component
    and the dir leaf, so it gets the same ``[a-z0-9-]`` allow-list every other
    shape's leaf gets. A non-``str`` or an id that sanitizes to EMPTY (all
    separators / ref-forbidden chars) is rejected with :class:`ValueError` — it
    would yield a bare ``ephemeral/`` ref and a kind dir as the leaf; the boundary
    (the WorktreeCreate hook) synthesizes a random id BEFORE calling in, so a
    launch is never blocked on a degenerate ``--worktree`` value.
    """
    if not isinstance(session_id, str):
        # Parity with issue_branch/work_stream_branch: a non-str must honor the
        # documented ValueError contract, not leak an AttributeError from
        # sanitize_slug(None).
        raise ValueError(
            "tree.layout.ephemeral_branch: session id must be a string "
            f"(the ephemeral/<id> grammar, ADR-0027); got {session_id!r}."
        )
    normalized = sanitize_slug(session_id)
    if not normalized:
        raise ValueError(
            "tree.layout.ephemeral_branch: session id must contain at least one "
            f"alphanumeric character (it becomes the ephemeral/<id> ref AND the dir "
            f"leaf); got {session_id!r}, which sanitizes to an empty name — a bare "
            "'ephemeral/' ref and a leaf-less dir."
        )
    return f"{EPHEMERAL_BRANCH_PREFIX}/{normalized}"


@dataclass(frozen=True)
class TreeSpec:
    """A request to materialize a Tree — exactly one of the four shapes is set.

    ``repo`` is the :class:`shipit.identity.Repo` value object whose NAME leads the
    flat dir leaf (``<repo>-<agent>-<timestamp>-<id>``, ADR-0074). It arrives already
    canonical — lowercased owner/name from :func:`shipit.identity.resolve_repo` or
    :func:`shipit.identity.repo_from_slug` — so case-varying origins or API slugs
    can never split one repo's Trees across divergent spellings (ADR-0024).
    ``agent`` / ``created`` / ``tree_id`` are the other three leaf coordinates
    (:func:`tree_leaf`): the backend binary name, the ``%Y%m%d-%H%M%S`` creation
    stamp, and the full UUID. They are IMPURE to mint (clock + randomness / the
    harness session id), so the caller supplies them and :func:`plan` stays a pure
    function of the spec. Unlike the retired ``agent_hash`` they name the DIR for
    EVERY shape identically — the branch/base still differ per shape, but the dir no
    longer encodes kind or nesting.
    ``root`` overrides the central root for tests; ``None`` resolves
    :func:`central_root`. ``slug`` is the optional human label applied per shape.
    ``session`` names the standalone-issue branch's leaf — ``issues/<id>/<session>``,
    default ``work`` — so a +1 session on the same issue (``issues/<id>/onboard``)
    coexists under the ``issues/<id>/`` ref directory (see :func:`_plan_issue`); it is
    unused by the epic, freeform, and ephemeral shapes. ``base`` is a caller-supplied
    override for the freeform shape only: brand-new freeform work still cuts from
    ``origin/main``, while callers that have identified an existing remote head
    (CLI ``--branch NAME`` or shepherd PR attachment) pass ``origin/<head>`` so
    the Tree starts at that head.

    The four shapes :func:`plan` dispatches on (and validates as mutually
    exclusive):

    - **epic/work stream** — ``epic`` + ``ws`` both set (``--epic E --ws N``);
    - **issue** — ``issue`` set (``--issue N`` ``[--session S]``);
    - **freeform** — ``branch`` set (``--branch <name>``); and
    - **ephemeral** — ``ephemeral`` set to the per-launch session id (the
      coordinator's session Tree, minted by the WorktreeCreate hook on
      ``claude --worktree <id>`` — ADR-0027; no CLI flag mints one by hand).
    """

    repo: Repo
    agent: str
    created: str
    tree_id: str
    issue: int | None = None
    epic: str | None = None
    ws: int | None = None
    branch: str | None = None
    base: str | None = None
    ephemeral: str | None = None
    slug: str = ""
    session: str = "work"
    root: Path | None = None


@dataclass(frozen=True)
class TreePlan:
    """The resolved coordinates for one Tree: where, on what branch, from what base."""

    dir: Path
    branch: str
    base: str


def tree_dir(
    repo: Repo, agent: str, created: str, tree_id: str, root: Path | None = None
) -> Path:
    """The absolute FLAT Tree dir: ``<root>/<repo>-<agent>-<timestamp>-<id>`` (ADR-0074).

    The ONE place a Tree's four leaf coordinates become an absolute path, shared by
    the write planner (:func:`plan`) and the read-only (reviewer) planner
    (:mod:`shipit.tree.readonly`) — so every creation path lands in the single flat
    shape with no owner or kind segment. ``root`` overrides the central root for
    tests; ``None`` resolves :func:`central_root`. Delegates the leaf validation to
    :func:`tree_leaf` (raises :class:`ValueError` on a malformed agent/stamp/id).
    """
    base_root = root if root is not None else central_root()
    return Path(base_root) / tree_leaf(repo, agent, created, tree_id)


def _tree_dir(spec: TreeSpec) -> Path:
    """``spec``'s absolute flat Tree dir — :func:`tree_dir` over its leaf coordinates.

    Shape-INDEPENDENT: every :func:`plan` shape resolves the SAME dir from the spec's
    ``repo``/``agent``/``created``/``tree_id`` (ADR-0074). Only the branch and base
    still differ per shape.
    """
    return tree_dir(spec.repo, spec.agent, spec.created, spec.tree_id, spec.root)


def plan(spec: TreeSpec) -> TreePlan:
    """Resolve ``spec`` into a concrete :class:`TreePlan` (pure, no I/O).

    Dispatches on which of the four mutually exclusive shapes the spec carries —
    epic/work stream (``epic`` + ``ws``), issue (``issue``), freeform (``branch``),
    or ephemeral (``ephemeral``). Exactly one must be set: zero shapes or more than
    one raises :class:`ValueError` rather than guessing, since the dir/branch/base
    each shape resolves to are genuinely different and a silent pick would
    mis-place a Tree.

    A partial epic shape — only ``epic`` or only ``ws`` — also raises: a work
    stream branch ``EPIC/WSnn`` is meaningless without both halves.
    """
    has_epic = spec.epic is not None or spec.ws is not None
    if has_epic and (spec.epic is None or spec.ws is None):
        raise ValueError(
            "tree.layout.plan: the epic shape needs both --epic and --ws "
            f"(got epic={spec.epic!r}, ws={spec.ws!r})"
        )

    shapes = [
        name
        for name, present in (
            ("epic", has_epic),
            ("issue", spec.issue is not None),
            ("branch", spec.branch is not None),
            ("ephemeral", spec.ephemeral is not None),
        )
        if present
    ]
    if len(shapes) != 1:
        raise ValueError(
            "tree.layout.plan: exactly one shape must be set "
            "(--epic/--ws, --issue, --branch, or ephemeral); "
            f"got {shapes or 'none'}"
        )

    shape = shapes[0]
    if shape == "epic":
        return _plan_epic_ws(spec)
    if shape == "issue":
        return _plan_issue(spec)
    if shape == "ephemeral":
        return _plan_ephemeral(spec)
    return _plan_freeform(spec)


def _plan_epic_ws(spec: TreeSpec) -> TreePlan:
    """Resolve the ``--epic E --ws N [--slug S]`` (work stream) shape.

    - **branch**: ``E/WSnn`` — the slash-namespaced work-stream form (naming.lex
      §3), ``ws`` zero-padded to two digits (``--ws 2`` → ``WS02``). The branch
      carries neither slug nor hash; ``E`` is the human-assigned epic code, kept
      verbatim (uppercase ``THEME+NN``) but validated as a single alphanumeric
      token (:data:`_EPIC_CODE`).
    - **base**: ``origin/E/umbrella`` — a work stream is cut from its epic's
      umbrella branch, the sibling of every ``E/WSnn`` under ``refs/heads/E/``.
    - **dir**: the FLAT ``<root>/<repo>-<agent>-<timestamp>-<id>`` leaf (ADR-0074) —
      the same shape every shape resolves; the ``E/WSnn`` work-stream identity lives
      in the BRANCH now, not the path. ``spec.slug`` is accepted for call-site
      compatibility but no longer rides the dir (the flat leaf carries who/when, git
      records what).

    Both user-controlled inputs are validated at this invariant boundary so a
    malformed ref or a path-traversing segment never reaches git or the filesystem:
    :func:`work_stream_branch` requires the epic code to be a single alphanumeric token
    (rejecting empty/whitespace codes and separators / ``..``) and ``ws`` to be a positive
    integer (rejecting ``WS00`` / ``WS-1``), raising :class:`ValueError` on either — the
    SAME validator the reviewer spawn path uses, so both fail loud identically.
    """
    assert spec.epic is not None and spec.ws is not None  # guaranteed by plan()
    branch = work_stream_branch(spec.epic, spec.ws)  # validates epic + ws
    base = epic_umbrella_base(spec.epic)
    return TreePlan(dir=_tree_dir(spec), branch=branch, base=base)


def _plan_freeform(spec: TreeSpec) -> TreePlan:
    """Resolve the ``--branch <freeform>`` shape.

    - **branch**: the freeform name verbatim — the caller owns its meaning, so the
      planner reflects the request rather than mangling it (naming.lex §3 lists the
      freeform name as a branch form in its own right).
    - **base**: normally ``origin/main`` — brand-new freeform work, like a standalone
      issue, is cut from the default branch. A caller may override this with another
      explicit remote ref after it has proven that ref is the intended starting point:
      CLI ``--branch NAME`` uses ``origin/NAME`` when that remote head already exists,
      and shepherd PR attachment uses ``origin/<head>`` so an existing-PR write Tree
      starts from the PR head instead of from main.
    - **dir**: the FLAT ``<root>/<repo>-<agent>-<timestamp>-<id>`` leaf (ADR-0074) —
      the same shape every shape resolves; the freeform name lives in the BRANCH, not
      the path, so an arbitrary ``spike/foo`` never needs sanitizing into a dir leaf.

    A branch that sanitizes to nothing (empty, whitespace-only, or all separators
    like ``///``) is still rejected with :class:`ValueError`: it would yield an
    unusable empty git branch.
    """
    branch = spec.branch
    assert branch is not None  # guarded by plan(); narrows the type for callers
    sanitized = sanitize_slug(branch)
    if not sanitized:
        raise ValueError(
            "tree.layout.plan: freeform --branch must contain at least one "
            f"alphanumeric character (it becomes the branch ref); got {branch!r}, "
            "which sanitizes to an empty name — an unusable empty branch."
        )
    if spec.base is not None and not spec.base.strip():
        raise ValueError(
            "tree.layout.plan: freeform base override must not be empty; "
            "omit it to use origin/main"
        )
    base = spec.base.strip() if spec.base is not None else "origin/main"
    return TreePlan(dir=_tree_dir(spec), branch=branch, base=base)


def _plan_issue(spec: TreeSpec) -> TreePlan:
    """Resolve the ``--issue N [--session S] [--slug S]`` (standalone-issue) shape.

    The ``<session>`` (default ``work``) still plays the structural role ``WSnn`` does
    in the BRANCH; the dir is the flat, shape-independent leaf.

    - **branch**: ``issues/<id>/<session>`` — slash-namespaced (:func:`issue_branch`),
      NEVER the bare ``issues/<id>`` (which would occupy ``refs/heads/issues/<id>`` as a
      ref FILE and block a sibling session); the session suffix keeps ``issues/<id>/`` a
      ref directory so ``issues/<id>/onboard`` can coexist with ``issues/<id>/work``. The
      branch carries no slug.
    - **base**: ``origin/main`` — a standalone issue is cut from the default branch (a
      work stream's epic-branch base is the epic shape's concern).
    - **dir**: the FLAT ``<root>/<repo>-<agent>-<timestamp>-<id>`` leaf (ADR-0074) —
      the same shape every shape resolves; the ``issues/<id>/<session>`` identity lives
      in the BRANCH, not the path.

    Both ``issue`` (positive integer) and ``session`` (non-empty after sanitization) are
    validated at this invariant boundary by :func:`issue_branch`, so a malformed ref
    never reaches git or the filesystem. Raises :class:`ValueError`.
    """
    assert spec.issue is not None  # guaranteed by plan()
    branch = issue_branch(spec.issue, spec.session)  # validates issue + session
    return TreePlan(dir=_tree_dir(spec), branch=branch, base="origin/main")


def _plan_ephemeral(spec: TreeSpec) -> TreePlan:
    """Resolve the ``ephemeral`` (coordinator session Tree) shape (ADR-0027).

    - **branch**: ``ephemeral/<id>`` (:func:`ephemeral_branch`) — the branch AT
      BIRTH only. The session Tree is *ephemeral-by-path, work-by-branch*: the
      coordinator switches this branch to the real work (``EPIC/umbrella``,
      ``docs/<slug>``, …) inside the fixed dir as the session learns its task, so
      dir and branch mirror only at birth and are EXPECTED to diverge.
    - **base**: ``origin/main`` — at launch the work is unknown (the session may be
      planning/triage before any epic or issue exists), so there is nothing to bind
      the Tree to but the default branch.
    - **dir**: the FLAT ``<root>/<repo>-<agent>-<timestamp>-<id>`` leaf (ADR-0074).
      The dir and branch NO LONGER share a leaf: the ``ephemeral/<id>`` identity is
      the BRANCH's, while the dir's ``<id>`` is the harness session UUID (the
      coordinator arm supplies it via ``tree_id`` so the dir name IS the resume
      handle — ADR-0074). ``spec.ephemeral`` therefore only names the branch here.

    The branch id is validated + normalized by :func:`ephemeral_branch` (same
    allow-list as every other ref); a degenerate id raises :class:`ValueError`.
    """
    assert spec.ephemeral is not None  # guaranteed by plan()
    branch = ephemeral_branch(spec.ephemeral)  # validates + normalizes the id
    return TreePlan(dir=_tree_dir(spec), branch=branch, base="origin/main")
