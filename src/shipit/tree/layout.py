"""``tree/layout`` — pure resolution of a Tree request into a concrete plan.

``plan(spec) -> TreePlan`` is the deep, pure heart of Tree creation: given a
:class:`TreeSpec` it resolves the three coordinates a clone needs — the **dir**
on disk, the **branch** to check out, and the **base** ref to cut it from — with
no I/O, so the truth table is unit-tested directly (Testing Decisions in the PRD).

:func:`plan` resolves EVERY spec shape — ``--issue N [--session S] [--slug S]``,
``--epic E --ws N [--slug S]``, freeform ``--branch NAME`` with an optional
internal base override, and the coordinator's ``ephemeral`` session Tree
(naming.lex §3; ADR-0027). :class:`TreeSpec` stays a single typed entry point:
adding a shape is adding a field plus a branch in :func:`plan`, not reshaping
callers.

The three load-bearing invariants the tests pin (from the PRD):

- the **agent hash lands on the dir, never on the branch** — two sessions sharing
  one branch is fine, two Trees in one dir is not; the hash disambiguates the dir
  while the branch stays a stable, meaningful namespace. Two shapes carry no hash
  at all: a ``review`` Tree is *shared* per ``(repo, branch)`` (ADR-0018), and an
  ``ephemeral`` session Tree's dir leaf IS the per-launch session id (ADR-0027) —
  in both, the leaf itself is the disambiguator, so a hash would be noise;
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

#: The dir-namespace segment that marks a **shared read-only (reviewer) Tree**
#: (ADR-0018): ``<root>/<org>/<repo>/review/<branch>``. Unlike the per-Run write
#: kinds (``epics`` / ``issues`` / ``branches``), a ``review`` Tree's leaf carries
#: NO agent hash — it is shared per ``(repo, branch)`` (git's branch is its source
#: of truth) — and the segment is the marker :func:`shipit.tree.cleanup.classify`
#: keys its reclaim rule off. Defined here so the read-only planner
#: (:mod:`shipit.tree.readonly`) and ``cleanup`` name it from one place.
REVIEW_KIND = "review"

#: The dir-namespace segment for the coordinator's **ephemeral session Tree**
#: (ADR-0027): ``<root>/<org>/<repo>/ephemeral/<id>``. The session Tree is
#: *ephemeral-by-path, work-by-branch*: the dir leaf is the per-launch session id
#: (the ``claude --worktree <id>`` value — the Tree's identity IS the session, so
#: the leaf carries NO agent hash and is never renamed), while the branch starts as
#: the mirroring ``ephemeral/<id>`` and then MOVES to the real work
#: (``EPIC/umbrella``, ``docs/<slug>``, …) as the session discovers what it is
#: doing — the dir stays. Defined here so the planner and the ``cleanup`` gc rule
#: for the ephemeral kind (SES02 Layer C) name the segment from one place.
EPHEMERAL_KIND = "ephemeral"

#: The kind label for every per-Run write Tree (``epics`` / ``issues`` /
#: ``branches`` namespaces): the default a path that is neither a shared review
#: clone nor an ephemeral session Tree falls to in :func:`tree_kind`.
WRITE_KIND = "write"

#: The write-Tree dir namespaces whose leaf sits one level DEEPER than the kind
#: segment (``epics/<epic>/<leaf>``, ``issues/<id>/<leaf>``): the segments
#: :func:`tree_kind` must check at grandparent depth, because the leaf's PARENT
#: there is a free-form epic code / issue id that could legitimately be named
#: ``review`` or ``ephemeral`` (``branches/<leaf>`` needs no entry — its parent
#: is the literal ``branches``, which collides with no kind segment).
_NESTED_WRITE_NAMESPACES = frozenset({"epics", "issues"})


def tree_kind(path: str | os.PathLike[str]) -> str:
    """Which reclaim family ``path`` belongs to: ``review``/``ephemeral``/``write``.

    There is no manifest — the path IS the signal (ADR-0018/0027), and the kind is
    pinned to exactly the LEAF's parent segment, never "anywhere in the path": a
    substring test would misclassify a write Tree whose org, repo, or central root
    happens to contain a ``review``/``ephemeral`` segment, bypassing the safety
    ladder that matches its true kind. Both the ``cleanup`` classifier (which
    dispatches its per-kind ladders on this) and the ``list`` verb (which renders
    the kind as a first-class column) name the mapping from this one place. Any
    path that is neither special kind is a per-Run **write** Tree
    (:data:`WRITE_KIND`) — the ``epics``/``issues``/``branches`` namespaces.

    The nested write namespaces are checked FIRST, at grandparent depth
    (:data:`_NESTED_WRITE_NAMESPACES`): an epic write Tree is
    ``…/epics/<epic>/<leaf>``, so the leaf's parent is the free-form epic code —
    and ``ephemeral``/``review`` are perfectly valid epic codes (agy review). A
    parent-segment test alone would put an epic named ``ephemeral``'s write Trees
    on the session-Tree gc ladder (removable after a mere hour idle) and hand them
    ``SessionStart`` pidfiles; the grandparent check keeps every ``epics``/
    ``issues`` Tree on the write ladder regardless of what its epic code or issue
    id is named.
    """
    p = Path(path)
    if len(p.parts) >= 3 and p.parts[-3] in _NESTED_WRITE_NAMESPACES:
        return WRITE_KIND
    parent = p.parent.name
    if parent == REVIEW_KIND:
        return REVIEW_KIND
    if parent == EPHEMERAL_KIND:
        return EPHEMERAL_KIND
    return WRITE_KIND


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
    directory, mirroring the dir kind segment (:data:`EPHEMERAL_KIND`).

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
    return f"ephemeral/{normalized}"


@dataclass(frozen=True)
class TreeSpec:
    """A request to materialize a Tree — exactly one of the four shapes is set.

    ``repo`` is the :class:`shipit.identity.Repo` value object that namespaces the
    dir under the central root (``<root>/<owner>/<name>/…``). It arrives already
    canonical — lowercased owner/name from :func:`shipit.identity.resolve_repo` or
    :func:`shipit.identity.repo_from_slug` — so case-varying origins or API slugs
    can never split one repo's Trees across divergent paths (ADR-0024).
    ``agent_hash`` disambiguates the dir for two Trees on one branch (it never
    reaches the branch; the ephemeral shape ignores it — its leaf is the session
    id itself).
    ``root`` overrides the central root for tests; ``None`` resolves
    :func:`central_root`. ``slug`` is the optional human label applied per shape.
    ``session`` names the standalone-issue branch's leaf — ``issues/<id>/<session>``,
    default ``work`` — so a +1 session on the same issue (``issues/<id>/onboard``)
    coexists under the ``issues/<id>/`` ref directory (see :func:`_plan_issue`); it is
    unused by the epic, freeform, and ephemeral shapes. ``base`` is an internal
    override for the freeform shape only: normal freeform work still cuts from
    ``origin/main``, while shepherd PR attachment cuts from ``origin/<head>`` so
    the Tree starts at the current PR head.

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
    agent_hash: str
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


def repo_dir(repo: Repo, root: Path | None = None) -> Path:
    """The per-repo namespace every Tree of ``repo`` lives under: ``<root>/<owner>/<name>``.

    The ONE place a :class:`shipit.identity.Repo` becomes Tree path segments, shared
    by the four write shapes and the read-only (reviewer) planner — so the identity's
    canonical (lowercased) owner/name is what lands on disk everywhere, and one repo
    can never scatter across case-divergent directories (ADR-0024). ``root`` overrides
    the central root for tests; ``None`` resolves :func:`central_root`.
    """
    base_root = root if root is not None else central_root()
    return Path(base_root) / repo.owner.login / repo.name


def _repo_dir(spec: TreeSpec) -> Path:
    """``spec``'s per-repo namespace dir — :func:`repo_dir` over its repo + root."""
    return repo_dir(spec.repo, spec.root)


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
    directory = _repo_dir(spec) / "epics" / spec.epic / leaf
    return TreePlan(dir=directory, branch=branch, base=base)


def _plan_freeform(spec: TreeSpec) -> TreePlan:
    """Resolve the ``--branch <freeform>`` shape.

    - **branch**: the freeform name verbatim — the caller owns its meaning, so the
      planner reflects the request rather than mangling it (naming.lex §3 lists the
      freeform name as a branch form in its own right).
    - **base**: normally ``origin/main`` — freeform work, like a standalone issue,
      is cut from the default branch. An internal caller may override this with
      another explicit remote ref; shepherd PR attachment uses ``origin/<head>``
      so an existing-PR write Tree starts from the PR head instead of from main.
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
    if spec.base is not None and not spec.base.strip():
        raise ValueError(
            "tree.layout.plan: freeform base override must not be empty; "
            "omit it to use origin/main"
        )
    leaf = f"{sanitized}-{spec.agent_hash}"
    directory = _repo_dir(spec) / "branches" / leaf
    base = spec.base.strip() if spec.base is not None else "origin/main"
    return TreePlan(dir=directory, branch=branch, base=base)


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
    directory = _repo_dir(spec) / "issues" / str(spec.issue) / leaf
    return TreePlan(dir=directory, branch=branch, base="origin/main")


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
    - **dir**: ``<root>/<org>/<repo>/ephemeral/<id>`` (:data:`EPHEMERAL_KIND`) —
      the leaf is the normalized session id itself, taken from the branch's last
      segment so dir and branch match at birth BY CONSTRUCTION. It carries **no
      agent hash and no slug**: the dir's identity IS the session (one per launch,
      never renamed), the launcher mints a per-launch-unique id
      (``sess-<utc-stamp>-<pid>``), and a hand-picked duplicate ``--worktree``
      value fails loud in ``create()``'s pre-existing-dir refusal rather than
      silently landing two sessions in one dir.

    The id is validated + normalized by :func:`ephemeral_branch` (same allow-list
    as every other leaf); a degenerate id raises :class:`ValueError`.
    """
    assert spec.ephemeral is not None  # guaranteed by plan()
    branch = ephemeral_branch(spec.ephemeral)  # validates + normalizes the id
    leaf = branch.rsplit("/", 1)[-1]
    directory = _repo_dir(spec) / EPHEMERAL_KIND / leaf
    return TreePlan(dir=directory, branch=branch, base="origin/main")
