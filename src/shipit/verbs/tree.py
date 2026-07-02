"""``shipit tree`` — the Tree command group (PRD docs/prd/where-to-do-work.md).

A NESTED click group: ``shipit tree <verb>`` is the surface for isolated Trees.
``create`` exposes the full spec grammar (naming.lex §3) — the ``--issue N``,
``--epic E --ws N``, and freeform ``--branch NAME`` shapes — each resolved by the
pure planner; ``list`` / ``remove`` / ``gc`` are sibling verbs, each its own
``@tree.command`` block in this module, so concurrent work streams touch disjoint
lines.

The verb is thin: resolve the ambient repo identity (org/repo, local checkout,
origin URL) at the gh/git boundary, hand a typed :class:`TreeSpec` to the pure
planner + effectful orchestrator, and print the READY summary. All the real logic
lives in :mod:`shipit.tree`.
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable
from dataclasses import fields
from pathlib import Path

import click

from .. import gh, proc
from ..session import liveness
from ..tree import cleanup, layout, provision, registry
from ..tree.cleanup import Cleanup
from ..tree.create import Tree, create, new_agent_hash
from ..tree.layout import TreeSpec
from ..tree.readonly import remove_tree
from ..tree.registry import TreeRecord


@click.group(
    name="tree",
    help=(
        "Isolated Trees — independent clones a write-session works in.\n\n"
        "`create` provisions a ready Tree (its own checkout, on a fresh branch) "
        "so concurrent agents never collide on one working tree. `--help` is the map."
    ),
)
def tree() -> None:
    """Root of the ``tree`` subcommand group; verbs are attached below."""


@tree.command(name="create")
@click.option(
    "--issue",
    type=int,
    default=None,
    help="Issue shape: provision a Tree for issue N (branch issues/<n>/<session>).",
)
@click.option(
    "--session",
    default="work",
    show_default=True,
    help=(
        "Issue shape: session name in the branch issues/<n>/<session>. The suffix "
        "keeps issues/<n>/ a ref directory so a +1 session on the same issue "
        "(e.g. --session onboard) coexists with the default `work` (naming.lex §3). "
        "Ignored by the --epic/--ws and --branch shapes."
    ),
)
@click.option(
    "--epic",
    default=None,
    help="Epic shape (with --ws): epic code E, e.g. HAR02 (branch E/WSnn).",
)
@click.option(
    "--ws",
    type=int,
    default=None,
    help="Epic shape (with --epic): work stream number N (branch E/WSnn).",
)
@click.option(
    "--branch",
    default=None,
    help="Freeform shape: provision a Tree on branch NAME, cut from origin/main.",
)
@click.option(
    "--slug",
    default="",
    help=(
        "Optional short label, sanitized to lowercase-dashed. Rides the Tree DIR leaf "
        "only (never the branch): --issue and --epic both keep their canonical branch "
        "(issues/<n>/<session>, E/WSnn); ignored for --branch."
    ),
)
def create_cmd(
    issue: int | None,
    session: str,
    epic: str | None,
    ws: int | None,
    branch: str | None,
    slug: str,
) -> None:
    """Provision an isolated Tree and print its READY summary.

    Accepts exactly ONE of three shapes (naming.lex §3); the planner resolves each
    to a concrete dir/branch/base:

    \b
    - ``--issue N [--session S] [--slug S]`` → branch ``issues/<n>/<session>``,
      base ``origin/main``
    - ``--epic E --ws N [--slug S]``         → branch ``E/WSnn``, base ``origin/E/umbrella``
    - ``--branch NAME``                      → branch ``NAME`` verbatim, base ``origin/main``

    Creates a fully-independent clone under the central root on the resolved branch,
    then prints ``READY {path, branch, base}``. The clone's ``origin`` is the repo's
    GitHub URL, so ``git``/``gh`` work inside it unchanged. Giving zero shapes, more
    than one, or a partial epic (only one of ``--epic``/``--ws``) is a clean exit-1
    error.
    """
    raise SystemExit(
        run_create(
            issue=issue, session=session, epic=epic, ws=ws, branch=branch, slug=slug
        )
    )


def run_create(
    *,
    issue: int | None = None,
    session: str = "work",
    epic: str | None = None,
    ws: int | None = None,
    branch: str | None = None,
    slug: str = "",
) -> int:
    """Select a shape -> resolve repo identity -> plan -> clone -> print READY.

    Returns 0 on success; 1 with a clean stderr message when the flag grammar is
    wrong (zero/multiple/partial shapes), the command is not run inside a GitHub
    checkout, the planner rejects the spec, or a git/gh call fails.
    """
    try:
        _select_shape(issue=issue, epic=epic, ws=ws, branch=branch)
    except ValueError as exc:
        print(f"tree create: {exc}", file=sys.stderr)
        return 1

    root = gh.repo_root()
    if not root:
        print("tree create: not inside a git checkout", file=sys.stderr)
        return 1
    try:
        org_repo = gh.current_repo()
        url = gh.git_remote_url(cwd=root)
    except gh.GhError as exc:
        print(f"tree create: {exc}", file=sys.stderr)
        return 1

    org, _, repo = org_repo.partition("/")
    spec = TreeSpec(
        org=org,
        repo=repo,
        agent_hash=new_agent_hash(),
        issue=issue,
        session=session,
        epic=epic,
        ws=ws,
        branch=branch,
        slug=slug,
    )
    try:
        result = create(spec, source_repo=root, github_url=url)
    except (gh.GhError, ValueError, proc.ProcError, OSError) as exc:
        # The whole create pipeline collapses to a clean exit-1 here: the planner
        # rejects a spec (ValueError), a git/gh call fails (GhError), provisioning
        # exits nonzero (ProcError), or a filesystem step — mkdir/copy/an existing
        # dest — fails (OSError). None of these should surface as a traceback.
        print(f"tree create: {exc}", file=sys.stderr)
        return 1
    _emit_ready(result)
    return 0


def _select_shape(
    *,
    issue: int | None,
    epic: str | None,
    ws: int | None,
    branch: str | None,
) -> str:
    """Validate that exactly one of the three shapes is requested; return its name.

    The flag-grammar gate (a CLI concern): exactly one of {``--issue``,
    ``--epic`` + ``--ws``, ``--branch``} must be given, and the epic shape needs BOTH
    halves. Any violation raises a clean :class:`ValueError` (no planner-module
    prefix), surfaced as an exit-1 stderr message by :func:`run_create`. The per-shape
    DOMAIN invariants — epic-code format, positive work-stream number, non-empty
    freeform name — stay the planner's job; it re-validates the built spec and its
    ``ValueError`` is caught the same way.
    """
    has_epic = epic is not None or ws is not None
    shapes = [
        name
        for name, present in (
            ("epic", has_epic),
            ("issue", issue is not None),
            ("branch", branch is not None),
        )
        if present
    ]
    if len(shapes) != 1:
        raise ValueError(
            "exactly one shape must be given — --issue N, --epic E --ws N, "
            f"or --branch NAME (got {', '.join(shapes) or 'none'})"
        )
    if has_epic and (epic is None or ws is None):
        raise ValueError(
            f"the epic shape needs both --epic and --ws (got epic={epic!r}, ws={ws!r})"
        )
    return shapes[0]


def _emit_ready(result: Tree) -> None:
    """Print the READY summary: a ``READY`` line plus the ``{path, branch, base}`` JSON."""
    print("READY")
    print(
        json.dumps(
            {"path": result.path, "branch": result.branch, "base": result.base},
            indent=2,
        )
    )


@tree.command(name="list")
def list_cmd() -> None:
    """List every Tree under the central root with its at-a-glance state.

    Renders the whole fleet — path, branch, base, age, dirty?, PR state — derived
    purely by SCANNING the central root (no manifest); the state is whatever the
    clones on disk say right now.
    """
    raise SystemExit(run_list())


def run_list() -> int:
    """Scan the central root and render the Tree fleet. Returns an exit code.

    Returns 0 in the normal case — an empty or missing root is a valid "no Trees
    yet" state, not an error; returns 1 with a clean stderr message when the central
    root is MISCONFIGURED (a relative ``SHIPIT_TREES_ROOT`` → ``ValueError``), so a
    config error reads as a message, never a traceback. Repo identity is irrelevant
    here — the central root spans every repo, so ``list`` shows the whole fleet (PRD
    user story 14/22).
    """
    try:
        root = layout.central_root()
    except ValueError as exc:
        print(f"tree list: {exc}", file=sys.stderr)
        return 1
    records = registry.scan(root)
    _render_list(records, now=time.time())
    return 0


#: The fleet table's columns, in render order: each is ``(header, field-extractor)``.
#: A new column is one tuple here — the renderer widths every column to its content.
_LIST_COLUMNS: tuple[tuple[str, str], ...] = (
    ("PATH", "path"),
    ("KIND", "kind"),
    ("BRANCH", "branch"),
    ("BASE", "base"),
    ("AGE", "age"),
    ("DIRTY", "dirty"),
    ("PR", "pr"),
)


def _render_list(records: list[TreeRecord], *, now: float) -> None:
    """Print the fleet as a fixed-width table (one Tree per row), or a hint when empty."""
    if not records:
        print("No Trees under the central root.")
        return
    headers = [header for header, _ in _LIST_COLUMNS]
    rows = [_row_cells(record, now=now) for record in records]
    # Width each column to its widest cell, header included. Pass a single generator
    # to max() (header counts as just another row) rather than star-unpacking one
    # positional arg per row — that materializes an arg list and can hit arg limits.
    all_rows = [headers, *rows]
    widths = [max(len(row[col]) for row in all_rows) for col in range(len(headers))]
    print(_format_row(headers, widths))
    for row in rows:
        print(_format_row(row, widths))


def _row_cells(record: TreeRecord, *, now: float) -> list[str]:
    """The rendered string cells for one Tree row, in :data:`_LIST_COLUMNS` order."""
    values = {
        "path": record.path,
        # The Tree's reclaim family — write / review / ephemeral — is first-class
        # fleet state (ADR-0018/0027): each kind takes a different gc ladder, so
        # the listing says which one applies rather than leaving it implied by path.
        "kind": layout.tree_kind(record.path),
        "branch": record.branch or "(detached)",
        "base": _base_cell(record),
        "age": _format_age(now - record.mtime),
        "dirty": "dirty" if record.dirty else "clean",
        "pr": record.pr or "-",
    }
    return [values[field] for _, field in _LIST_COLUMNS]


def _base_cell(record: TreeRecord) -> str:
    """The BASE cell: the upstream ref, annotated with ahead/behind when diverged."""
    base = record.base or "-"
    marks = []
    if record.ahead:
        marks.append(f"+{record.ahead}")
    if record.behind:
        marks.append(f"-{record.behind}")
    return f"{base} ({'/'.join(marks)})" if marks else base


def _format_row(cells: list[str], widths: list[int]) -> str:
    """Left-justify each cell to its column width and join with two spaces."""
    return "  ".join(cell.ljust(widths[col]) for col, cell in enumerate(cells)).rstrip()


def _format_age(seconds: float) -> str:
    """A compact human age (``"3d"``, ``"4h"``, ``"5m"``, ``"12s"``) for a Tree's mtime."""
    secs = int(max(seconds, 0))
    for unit_seconds, suffix in ((86400, "d"), (3600, "h"), (60, "m")):
        if secs >= unit_seconds:
            return f"{secs // unit_seconds}{suffix}"
    return f"{secs}s"


@tree.command(name="remove")
@click.argument("target")
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    default=False,
    help=(
        "Skip the confirmation prompt unconditionally. The non-interactive default: "
        "removing a Tree with uncommitted or unpushed work without a TTY requires this."
    ),
)
def remove_cmd(target: str, yes: bool) -> None:
    """Delete a single Tree identified by TARGET (its path or its directory name).

    A Tree is a disposable, fully-independent clone, so removing it is usually just
    deleting its directory — no worktree to prune, no shared state to corrupt. The one
    exception is a Tree that still holds work living ONLY in that clone — uncommitted
    changes or commits not yet pushed: that delete is gated behind a confirmation
    (``--yes``/``-y`` skips it). TARGET must resolve to exactly one Tree under the
    central root; an unknown or ambiguous TARGET is a clean error (Tree left untouched).
    """
    raise SystemExit(run_remove(target, assume_yes=yes))


def _stdin_is_tty() -> bool:
    """Whether stdin is an interactive terminal, robust to a missing/closed stream.

    The default ``is_tty`` for removal gating. Reaching for ``sys.stdin.isatty``
    directly is unsafe outside a normal terminal: ``sys.stdin`` can be ``None`` (a
    detached/background process → ``AttributeError``) or a closed stream
    (``isatty()`` → ``ValueError``). Either way the answer we want is "not a TTY",
    so a risky remove is refused rather than crashing — the safe non-interactive
    default.
    """
    stream = sys.stdin
    if stream is None or getattr(stream, "closed", False):
        return False
    try:
        return stream.isatty()
    except (ValueError, OSError):
        return False


def run_remove(
    target: str,
    *,
    assume_yes: bool = False,
    confirm: Callable[[str], bool] | None = None,
    is_tty: Callable[[], bool] | None = None,
) -> int:
    """Resolve TARGET to one Tree and delete its clone dir. Returns an exit code.

    A Tree is a disposable clone, so removal is silent by default — EXCEPT when the
    delete could lose work that lives only in that clone (uncommitted changes or
    unpushed commits). That risky case is gated: with a TTY the user is prompted
    (``confirm``); declining leaves the Tree untouched. ``assume_yes`` (the ``--yes``
    flag) skips the gate unconditionally. Without a TTY and without ``assume_yes`` a
    risky remove is REFUSED rather than silently destroying work or blocking on a
    prompt — the safe non-interactive default. A clean, fully-pushed Tree is always
    removed without a prompt.

    Returns 0 after removing the one matching Tree; 1 with a stderr message when the
    central root is misconfigured (a relative ``SHIPIT_TREES_ROOT`` → ``ValueError``,
    surfaced as a message not a traceback), no Tree matches, more than one does (never
    guess which to delete), the user declines, or a risky remove can't be confirmed
    non-interactively. ``confirm``/``is_tty`` are injectable so the gating is unit-
    testable without a real terminal; they default to ``click.confirm`` and
    :func:`_stdin_is_tty` (a guard around ``sys.stdin.isatty`` that reads as
    not-a-TTY when stdin is missing or closed rather than crashing).
    """
    if confirm is None:
        confirm = click.confirm
    if is_tty is None:
        is_tty = _stdin_is_tty
    try:
        root = layout.central_root()
    except ValueError as exc:
        print(f"tree remove: {exc}", file=sys.stderr)
        return 1
    records = registry.scan(root)
    matches = _match_trees(records, target)
    if not matches:
        print(f"tree remove: no Tree matching {target!r}", file=sys.stderr)
        return 1
    if len(matches) > 1:
        paths = ", ".join(record.path for record in matches)
        print(
            f"tree remove: {target!r} is ambiguous — matches {paths}", file=sys.stderr
        )
        return 1
    record = matches[0]
    block = _gate_removal(record, assume_yes=assume_yes, is_tty=is_tty, confirm=confirm)
    if block is not None:
        print(f"tree remove: {block}", file=sys.stderr)
        return 1
    try:
        remove_tree(record.path)
    except OSError as exc:
        print(f"tree remove: could not remove {record.path}: {exc}", file=sys.stderr)
        return 1
    print(f"REMOVED {record.path}")
    return 0


def _removal_risk(record: TreeRecord) -> str | None:
    """Why removing ``record`` could lose work, as a short phrase — or ``None`` if safe.

    A Tree is a disposable clone, so removal is normally silent; it is only worth a
    confirmation when the delete would discard work that exists ONLY in that clone:
    uncommitted/untracked changes (``dirty``) or commits not yet pushed to the upstream
    (``ahead > 0``). Everything reachable from the upstream survives the delete, so a
    clean, fully-pushed Tree returns ``None``. This is the whole risk-detection seam —
    it reuses the ``dirty``/``ahead`` the registry already derived through the ``gh``
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


def _gate_removal(
    record: TreeRecord,
    *,
    assume_yes: bool,
    is_tty: Callable[[], bool],
    confirm: Callable[[str], bool],
) -> str | None:
    """Decide whether removing ``record`` may proceed; pure gating, no side effects.

    Returns ``None`` to proceed with the delete (the Tree is safe, ``--yes`` was given,
    or the user confirmed), or a stderr-ready message when the removal must NOT happen:
    the user declined the prompt, or a risky Tree cannot be confirmed because there is
    no TTY and no ``--yes``. Keeping this separate from the ``rmtree`` keeps both the
    risk-detection and the prompt-gating unit-testable with an injected ``confirm`` and
    ``is_tty`` — no real terminal, no filesystem.
    """
    risk = _removal_risk(record)
    if risk is None or assume_yes:
        return None
    if is_tty():
        if confirm(f"Tree {record.path} has {risk}; remove anyway?"):
            return None
        return f"aborted — {record.path} left untouched"
    return (
        f"{record.path} has {risk}; refusing to remove non-interactively without --yes"
    )


def _match_trees(records: list[TreeRecord], target: str) -> list[TreeRecord]:
    """Trees whose absolute path equals TARGET or whose dir name equals TARGET.

    Matching on the basename lets a coordinator name a Tree by its short id
    (``7-aaaa``) without typing the whole central-root path; matching the full path
    stays exact. The path form takes precedence — an exact path match is unambiguous.
    """
    by_path = [record for record in records if record.path == target]
    if by_path:
        return by_path
    return [record for record in records if Path(record.path).name == target]


@tree.command(name="gc")
@click.option(
    "--dry-run",
    is_flag=True,
    help=(
        "Preview only: print the removable/stale/keep partition for the whole fleet "
        "and delete NOTHING. The preview is the exact decision the real sweep acts on."
    ),
)
@click.option(
    "--threshold",
    default=None,
    metavar="DURATION",
    help=(
        "Age boundary a Tree must exceed to be reclaimable, as a human duration "
        "(e.g. 14d, 36h, 90m). Defaults to 14d when omitted."
    ),
)
def gc_cmd(dry_run: bool, threshold: str | None) -> None:
    """Sweep the central root: remove only provably-safe Trees, list ambiguous ones.

    Scans every Tree, classifies the fleet, then deletes ONLY the Trees whose PR is
    merged, working tree clean, nothing unpushed, and which are aged past the
    threshold. Trees that merely look abandoned are LISTED as stale (never deleted),
    and anything with live or local work is left untouched. Conservative by default.

    ``--dry-run`` prints the same partition the real sweep would act on and deletes
    nothing; ``--threshold DURATION`` (e.g. ``36h``) overrides the 14-day age boundary
    for this run.
    """
    raise SystemExit(run_gc(dry_run=dry_run, threshold=threshold))


def run_gc(*, dry_run: bool = False, threshold: str | None = None) -> int:
    """Scan, classify, then either preview the partition or sweep the removable set.

    The scan→classify step is shared by both modes (:func:`_scan_and_classify`), so a
    ``--dry-run`` preview can NEVER drift from the action: it renders the very
    :class:`Cleanup` the real sweep would consume; only the "print vs delete" tail
    differs. ``threshold`` (a human duration like ``36h``) overrides the default age
    boundary for this run; ``None`` keeps the 14-day default.

    Returns 0 in the normal case — an empty root or a fleet with nothing to reclaim
    is a valid outcome, not an error; returns 1 with a clean stderr message when the
    central root is misconfigured (a relative ``SHIPIT_TREES_ROOT`` → ``ValueError``)
    or ``threshold`` is not a valid duration, so the gc contract stays "no tracebacks,
    just messages + counts". Repo identity is irrelevant — ``gc`` spans the whole
    central root, like ``list``.
    """
    try:
        root = layout.central_root()
        max_age_seconds = (
            cleanup.DEFAULT_MAX_AGE_SECONDS
            if threshold is None
            else cleanup.parse_duration(threshold)
        )
    except ValueError as exc:
        print(f"tree gc: {exc}", file=sys.stderr)
        return 1
    decision, total, unknown = _scan_and_classify(root, max_age_seconds=max_age_seconds)
    if dry_run:
        _emit_gc_preview(decision, total=total, unknown=unknown)
    else:
        _emit_gc(decision, total=total, unknown=unknown)
    return 0


def _scan_and_classify(
    root: str, *, max_age_seconds: float
) -> tuple[Cleanup, int, int]:
    """Scan the central root and classify the fleet — the step shared by both gc modes.

    Factoring scan→PR-state→``classify`` here is what guarantees dry-run/real-sweep
    parity: both the preview and the sweep call this one path, so the partition they
    render and act on is the identical :class:`Cleanup`. Pure ``classify`` does the
    deciding; this wrapper only supplies the effectful inputs (disk scan, ``now``, PR
    states) it needs.

    Returns the :class:`Cleanup` partition alongside ``total`` (Trees scanned) and
    ``unknown`` (how many had an unreadable PR state). Both gc tails — the real sweep
    and the ``--dry-run`` preview — need those counts to warn about an INCOMPLETE
    view of the fleet, so they travel with the partition rather than being recomputed.
    """
    records = registry.scan(root)
    pr_states = {record.path: _pr_state(record) for record in records}
    decision = cleanup.classify(
        records,
        now=time.time(),
        pr_states=pr_states,
        max_age_seconds=max_age_seconds,
        live_sessions=_live_sessions(records),
        provision_shas=_provision_shas(records),
    )
    unknown = sum(1 for state in pr_states.values() if state == "UNKNOWN")
    return decision, len(records), unknown


def _live_sessions(records: list[TreeRecord]) -> dict[str, bool]:
    """Per-ephemeral-Tree session liveness — the ``live_sessions`` input ``classify`` needs.

    For each *ephemeral* Tree (the only kind whose ladder consults liveness), read
    its pidfile and decide :func:`~shipit.session.liveness.is_live` against the
    real OS probe. No pidfile / an unreadable one reads as NOT live — the safe
    direction, because the pure ladder still protects such a Tree through its
    liveness-independent rungs (the dirty/unpushed floor, the grace window). Other
    kinds are simply absent from the map (``classify`` defaults them to not-live,
    and their ladders never look).
    """
    live: dict[str, bool] = {}
    for record in records:
        if layout.tree_kind(record.path) != layout.EPHEMERAL_KIND:
            continue
        session = liveness.read_pidfile(record.path)
        live[record.path] = session is not None and liveness.is_live(
            session, liveness.os_probe
        )
    return live


def _provision_shas(records: list[TreeRecord]) -> dict[str, frozenset[str]]:
    """Per-ephemeral-Tree provisioning-commit SHAs — ``classify``'s exclusion input.

    For each *ephemeral* Tree (the only ladder that consults the exclusion, #232),
    read the ``.git/shipit-provision.json`` record its birth provisioning wrote.
    A missing or unreadable record reads as the EMPTY set — nothing excluded, so
    the pure ladder's unpushed floor keeps the Tree: the safe direction. Other
    kinds are simply absent from the map (``classify`` defaults them to empty,
    and their ladders never exclude).
    """
    return {
        record.path: provision.read_provision_shas(record.path)
        for record in records
        if layout.tree_kind(record.path) == layout.EPHEMERAL_KIND
    }


def _pr_state(record: TreeRecord) -> str | None:
    """The PR's remote state (``"MERGED"`` / ``"OPEN"`` / ``"CLOSED"`` / ``"UNKNOWN"`` …)
    for one Tree.

    Reads through the same ``gh`` boundary the registry uses, from inside the clone, so
    ``gc`` sees the authoritative merge state rather than re-parsing the rendered label.
    A draft open PR is normalized to ``"DRAFT"`` (mirroring ``registry._pr_label``) so the
    fleet has ONE state vocabulary and ``cleanup.classify``'s draft branch is reachable.
    An unreadable state (``gh.pr_for_head`` returns :data:`~shipit.gh.UNKNOWN`, or a PR
    with a malformed state field) maps to ``"UNKNOWN"`` — distinct from ``None`` (no
    branch / no PR) — so ``gc`` can both treat it conservatively and warn about it.
    """
    if not record.branch:
        return None
    pr = gh.pr_for_head(record.branch, cwd=record.path)
    if pr is gh.UNKNOWN:
        return "UNKNOWN"
    if not pr:
        return None
    state = pr.get("state")
    if not isinstance(state, str):
        return "UNKNOWN"
    state = state.upper()
    if state == "OPEN" and pr.get("isDraft"):
        return "DRAFT"
    return state


def _emit_gc(decision: Cleanup, *, total: int, unknown: int) -> None:
    """Delete the removable Trees, then report what was removed, kept stale, or kept.

    Deletion is best-effort per Tree: if one ``rmtree`` fails (a read-only file, a lock,
    a vanished dir), the failure goes to stderr and the sweep CONTINUES to the next Tree
    rather than aborting mid-fleet. The summary's ``removed`` count reflects what actually
    came off disk, not what was merely planned.

    ``total`` is the number of Trees swept and ``unknown`` how many had an unreadable PR
    state. When any were unknown, a ``swept N of M; K skipped (state unknown)`` warning is
    emitted to stderr so an INCOMPLETE sweep is visible — those Trees were classified
    conservatively (never removed), but a transient ``gh`` failure could be hiding a
    reclaimable Tree, and the operator should know the sweep did not see the whole fleet.
    """
    removed = 0
    for record in decision.removable:
        try:
            deleted = remove_tree(record.path)
        except OSError as exc:
            print(f"FAILED  {record.path}: {exc}", file=sys.stderr)
            continue
        if not deleted:
            # The path was already gone (a concurrent sweep, a manual rm). Nothing came
            # off disk, so it must not be counted or reported as REMOVED — the summary's
            # `removed` reflects actual reclaim, per remove_tree's contract.
            continue
        removed += 1
        print(f"REMOVED {record.path}")
    for record in decision.stale:
        print(f"STALE   {record.path} (ambiguous — left for review, not removed)")
    stale = len(decision.stale)
    kept = len(decision.keep)
    print(f"gc: removed {removed}, stale {stale}, kept {kept}")
    if unknown:
        swept = total - unknown
        print(
            f"swept {swept} of {total}; {unknown} skipped (state unknown)",
            file=sys.stderr,
        )


def _emit_gc_preview(decision: Cleanup, *, total: int, unknown: int) -> None:
    """Print the removable/stale/keep partition WITHOUT touching disk (``--dry-run``).

    Renders the exact :class:`Cleanup` the real sweep would act on, so a preview can
    never disagree with the sweep that follows it. The buckets are walked GENERICALLY
    (``dataclasses.fields``) and each is printed by its own field name — so if an
    upstream change adds a bucket, it surfaces here with no edit and no hard-coded
    state vocabulary to fall out of date. Deletes nothing: there is no ``rmtree`` on
    this path at all.

    The same INCOMPLETE-view warning the real sweep emits is surfaced here too: when
    any Tree had an unreadable PR state, a ``would sweep N of M; K skipped (state
    unknown)`` line goes to stderr, so a dry-run preview tells the operator the fleet
    was only partially seen — exactly as the real sweep would, never silently.
    """
    counts: list[str] = []
    for field in fields(decision):
        bucket: list[TreeRecord] = getattr(decision, field.name)
        for record in bucket:
            print(f"{field.name.upper():<9} {record.path}")
        counts.append(f"{field.name} {len(bucket)}")
    print(f"gc --dry-run (no Trees deleted): {', '.join(counts)}")
    if unknown:
        swept = total - unknown
        print(
            f"would sweep {swept} of {total}; {unknown} skipped (state unknown)",
            file=sys.stderr,
        )
