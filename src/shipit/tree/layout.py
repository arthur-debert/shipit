"""``tree/layout`` — pure resolution of a Tree request into a concrete plan.

``plan(spec) -> TreePlan`` is the deep, pure heart of Tree creation: given a
:class:`TreeSpec` it resolves the three coordinates a clone needs — the **dir**
on disk, the **branch** to check out, and the **base** ref to cut it from — with
no I/O, so the truth table is unit-tested directly (Testing Decisions in the PRD).

:func:`plan` resolves EVERY spec shape — ``--issue N [--session S] [--slug S]``,
``--epic E --ws N [--slug S]``, and freeform ``--branch NAME`` (naming.lex §3).
:class:`TreeSpec` stays a single typed entry point: adding a shape is adding a field
plus a branch in :func:`plan`, not reshaping callers.

The three load-bearing invariants the tests pin (from the PRD):

- the **agent hash lands on the dir, never on the branch** — two sessions sharing
  one branch is fine, two Trees in one dir is not; the hash disambiguates the dir
  while the branch stays a stable, meaningful namespace;
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
from pathlib import Path

#: Env override for the central root all Trees live under. Unset → the default
#: below. A module constant + env override is the whole config surface for WS01
#: (the PRD's "keep it simple"); a richer config binding can supersede it later.
CENTRAL_ROOT_ENV = "SHIPIT_TREES_ROOT"

#: The default central root: one predictable place OUTSIDE every repo, so cleanup
#: and inspection are uniform across repos and agents — never inside ``.claude/``
#: (PRD Solution; ADR-0014).
DEFAULT_CENTRAL_ROOT = "~/workspace/trees"

#: The dir-namespace segment that marks a **shared read-only (reviewer) Tree**
#: (ADR-0018): ``<root>/<org>/<repo>/review/<branch>``. Unlike the per-Run write
#: kinds (``epics`` / ``issues`` / ``branches``), a ``review`` Tree's leaf carries
#: NO agent hash — it is shared per ``(repo, branch)`` (git's branch is its source
#: of truth) — and the segment is the marker :func:`shipit.tree.cleanup.classify`
#: keys its reclaim rule off. Defined here so the read-only planner
#: (:mod:`shipit.tree.readonly`) and ``cleanup`` name it from one place.
REVIEW_KIND = "review"

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

    A non-absolute ``SHIPIT_TREES_ROOT`` is rejected with :class:`ValueError`
    rather than resolved against the cwd: a relative root would place Trees under
    wherever ``shipit`` happens to be invoked from — potentially inside the source
    checkout — which breaks the central-root/isolation invariant this whole
    feature rests on (PRD Solution; ADR-0014). The default already expands to an
    absolute path.
    """
    raw = os.environ.get(CENTRAL_ROOT_ENV) or DEFAULT_CENTRAL_ROOT
    root = Path(os.path.expandvars(raw)).expanduser()
    if not root.is_absolute():
        raise ValueError(
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


@dataclass(frozen=True)
class TreeSpec:
    """A request to materialize a Tree — exactly one of the three shapes is set.

    ``org`` / ``repo`` namespace the dir under the central root; ``agent_hash``
    disambiguates the dir for two Trees on one branch (it never reaches the
    branch). ``root`` overrides the central root for tests; ``None`` resolves
    :func:`central_root`. ``slug`` is the optional human label applied per shape.
    ``session`` names the standalone-issue branch's leaf — ``issues/<id>/<session>``,
    default ``work`` — so a +1 session on the same issue (``issues/<id>/onboard``)
    coexists under the ``issues/<id>/`` ref directory (see :func:`_plan_issue`); it is
    unused by the epic and freeform shapes.

    The three shapes :func:`plan` dispatches on (and validates as mutually
    exclusive):

    - **epic/work stream** — ``epic`` + ``ws`` both set (``--epic E --ws N``);
    - **issue** — ``issue`` set (``--issue N`` ``[--session S]``); and
    - **freeform** — ``branch`` set (``--branch <name>``).
    """

    org: str
    repo: str
    agent_hash: str
    issue: int | None = None
    epic: str | None = None
    ws: int | None = None
    branch: str | None = None
    slug: str = ""
    session: str = "work"
    root: Path | None = None


@dataclass(frozen=True)
class TreePlan:
    """The resolved coordinates for one Tree: where, on what branch, from what base."""

    dir: Path
    branch: str
    base: str


def plan(spec: TreeSpec) -> TreePlan:
    """Resolve ``spec`` into a concrete :class:`TreePlan` (pure, no I/O).

    Dispatches on which of the three mutually exclusive shapes the spec carries —
    epic/work stream (``epic`` + ``ws``), issue (``issue``), or freeform
    (``branch``). Exactly one must be set: zero shapes or more than one raises
    :class:`ValueError` rather than guessing, since the dir/branch/base each shape
    resolves to are genuinely different and a silent pick would mis-place a Tree.

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
        )
        if present
    ]
    if len(shapes) != 1:
        raise ValueError(
            "tree.layout.plan: exactly one shape must be set "
            "(--epic/--ws, --issue, or --branch); "
            f"got {shapes or 'none'}"
        )

    shape = shapes[0]
    if shape == "epic":
        return _plan_epic_ws(spec)
    if shape == "issue":
        return _plan_issue(spec)
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
    - **dir**: ``<root>/<org>/<repo>/epics/<E>/WSnn[-<slug>]-<agent-hash>`` — the
      branch path under the ``epics`` kind, with the hash on the leaf. An optional
      sanitized slug rides on the DIR only (never the canonical branch), so a Tree
      reads as ``WS02-tiling-deadbeef`` on disk while the branch stays ``E/WS02``.

    Both user-controlled inputs are validated at this invariant boundary so a
    malformed ref or a path-traversing segment never reaches git or the filesystem:
    :func:`work_stream_branch` requires the epic code to be a single alphanumeric token
    (rejecting empty/whitespace codes and separators / ``..``) and ``ws`` to be a positive
    integer (rejecting ``WS00`` / ``WS-1``), raising :class:`ValueError` on either — the
    SAME validator the reviewer spawn path uses, so both fail loud identically.
    """
    assert spec.epic is not None and spec.ws is not None  # guaranteed by plan()
    branch = work_stream_branch(spec.epic, spec.ws)  # validates epic + ws
    ws_code = f"WS{spec.ws:02d}"
    base = epic_umbrella_base(spec.epic)
    slug = sanitize_slug(spec.slug)
    leaf = (
        f"{ws_code}-{slug}-{spec.agent_hash}"
        if slug
        else f"{ws_code}-{spec.agent_hash}"
    )
    root = spec.root if spec.root is not None else central_root()
    directory = Path(root) / spec.org / spec.repo / "epics" / spec.epic / leaf
    return TreePlan(dir=directory, branch=branch, base=base)


def _plan_freeform(spec: TreeSpec) -> TreePlan:
    """Resolve the ``--branch <freeform>`` shape.

    - **branch**: the freeform name verbatim — the caller owns its meaning, so the
      planner reflects the request rather than mangling it (naming.lex §3 lists the
      freeform name as a branch form in its own right).
    - **base**: ``origin/main`` — freeform work, like a standalone issue, is cut
      from the default branch.
    - **dir**: ``<root>/<org>/<repo>/branches/<sanitized-branch>-<agent-hash>`` —
      the freeform name is sanitized into one safe leaf (slashes and other
      separators collapse to ``-``) so an arbitrary branch like ``spike/foo`` maps
      to a flat, predictable dir; the hash keeps duplicate Trees apart.

    A branch that sanitizes to nothing (empty, whitespace-only, or all separators
    like ``///``) is rejected with :class:`ValueError`: it would yield an unusable
    empty git branch and a bare ``-<hash>`` dir leaf.
    """
    branch = spec.branch
    assert branch is not None  # guarded by plan(); narrows the type for callers
    sanitized = sanitize_slug(branch)
    if not sanitized:
        raise ValueError(
            "tree.layout.plan: freeform --branch must contain at least one "
            "alphanumeric character (it becomes both the branch ref and the dir "
            f"leaf); got {branch!r}, which sanitizes to an empty name — a leaf of "
            "just '-<hash>' and an unusable empty branch."
        )
    root = spec.root if spec.root is not None else central_root()
    leaf = f"{sanitized}-{spec.agent_hash}"
    directory = Path(root) / spec.org / spec.repo / "branches" / leaf
    return TreePlan(dir=directory, branch=branch, base="origin/main")


def _plan_issue(spec: TreeSpec) -> TreePlan:
    """Resolve the ``--issue N [--session S] [--slug S]`` (standalone-issue) shape.

    Mirrors the epic shape (:func:`_plan_epic_ws`): the ``<session>`` (default ``work``)
    plays the structural role ``WSnn`` does, so branch and dir share it, an optional slug
    rides the DIR leaf only, and the hash lands on the leaf, never the branch.

    - **branch**: ``issues/<id>/<session>`` — slash-namespaced (:func:`issue_branch`),
      NEVER the bare ``issues/<id>`` (which would occupy ``refs/heads/issues/<id>`` as a
      ref FILE and block a sibling session); the session suffix keeps ``issues/<id>/`` a
      ref directory so ``issues/<id>/onboard`` can coexist with ``issues/<id>/work``. The
      branch carries neither slug nor hash.
    - **base**: ``origin/main`` — a standalone issue is cut from the default branch (a
      work stream's epic-branch base is the epic shape's concern).
    - **dir**: ``<root>/<org>/<repo>/issues/<id>/<session>[-<slug>]-<agent-hash>`` — the
      branch path under the ``issues`` kind, hash on the leaf. An optional sanitized slug
      rides the DIR only (never the canonical branch), so a Tree reads as
      ``work-header-align-deadbeef`` on disk while the branch stays ``issues/<id>/work``.

    Both ``issue`` (positive integer) and ``session`` (non-empty after sanitization) are
    validated at this invariant boundary by :func:`issue_branch`, so a malformed ref
    never reaches git or the filesystem. Raises :class:`ValueError`.
    """
    assert spec.issue is not None  # guaranteed by plan()
    branch = issue_branch(spec.issue, spec.session)  # validates issue + session
    # Take the normalized session from the branch's last segment rather than
    # re-sanitizing spec.session: the dir leaf then matches the branch BY CONSTRUCTION
    # and cannot drift from issue_branch's normalization if the rules ever change.
    session = branch.rsplit("/", 1)[-1]
    slug = sanitize_slug(spec.slug)
    leaf = (
        f"{session}-{slug}-{spec.agent_hash}"
        if slug
        else f"{session}-{spec.agent_hash}"
    )
    root = spec.root if spec.root is not None else central_root()
    directory = Path(root) / spec.org / spec.repo / "issues" / str(spec.issue) / leaf
    return TreePlan(dir=directory, branch=branch, base="origin/main")
