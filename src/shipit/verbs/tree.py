"""``shipit tree`` — the Tree command group (PRD docs/legacy-prd/where-to-do-work.md).

A NESTED click group: ``shipit tree <verb>`` is the surface for isolated Trees.
``create`` exposes the full spec grammar (naming.lex §3) — the ``--issue N``,
``--epic E --ws N``, and freeform ``--branch NAME`` shapes — each resolved by the
pure planner; ``list`` / ``remove`` / ``gc`` are sibling verbs, each its own
``@tree.command`` block in this module, so concurrent work streams touch disjoint
lines.

The verb is thin (ADR-0030): resolve the ambient repo identity — the canonical
:class:`shipit.identity.Repo`, derived locally from the origin remote (ADR-0024) —
hand a typed :class:`TreeSpec` to the pure planner + effectful orchestrator, and
print the READY summary. All the real logic lives in :mod:`shipit.tree`:
the fleet listing as typed rows (:mod:`shipit.tree.fleet`), removal gating as a
typed outcome (:mod:`shipit.tree.removal`), and gc as a plan + a sweep
(:mod:`shipit.tree.gc`). This module holds only click glue, the pure
``format_*`` renderers, and the exit codes the typed results derive — runtime
failures map through the shared :func:`~._errors.cli_errors` shell.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from collections.abc import Callable
from dataclasses import fields

import click

from .. import execrun, git, identity
from ..tree import cleanup, fleet, gc, layout, registry, removal
from ..tree.create import Tree, create, new_agent_hash
from ..tree.layout import TreeSpec
from ..tree.removal import GateAction, RemovalError
from ._errors import cli_errors
from ._params import DURATION, json_option
from ._render import emit

#: The Tree axis logs on the shared ``shipit.tree`` logger (LOG02): the verb's
#: user-facing ``print``/``echo`` output is unchanged, but the actions it is the
#: only record of — a failed create, a dry-run preview — also land in the
#: durable JSONL record (spray convention, ADR-0029). The promoted domain
#: modules carry their own twins.
logger = logging.getLogger("shipit.tree")


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
    help=(
        "Freeform shape: provision a Tree on branch NAME. Existing remote heads "
        "start from origin/NAME; new branches start from origin/main."
    ),
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
    - ``--branch NAME``                      → branch ``NAME`` verbatim, base ``origin/NAME``
      when that remote head exists, else ``origin/main``

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

    root = git.repo_root()
    if not root:
        print("tree create: not inside a git checkout", file=sys.stderr)
        return 1
    try:
        # Identity derives LOCALLY from the origin remote (ADR-0024): the one
        # canonical, case-normalized Repo — never an API slug re-split by hand.
        repo_identity = identity.resolve_repo(root)
        url = git.remote_url(cwd=root)
        base = _freeform_base(branch, cwd=root) if branch is not None else None
    except (execrun.ExecError, ValueError) as exc:
        print(f"tree create: {exc}", file=sys.stderr)
        return 1

    spec = TreeSpec(
        repo=repo_identity,
        agent_hash=new_agent_hash(),
        issue=issue,
        session=session,
        epic=epic,
        ws=ws,
        branch=branch,
        base=base,
        slug=slug,
    )
    try:
        result = create(spec, source_repo=root, github_url=url)
    except (ValueError, execrun.ExecError, OSError) as exc:
        # The whole create pipeline collapses to a clean exit-1 here: the planner
        # rejects a spec (ValueError), a git/gh call or provisioning Exec fails (ExecError),
        # fails (ExecError), or a filesystem step — mkdir/copy/an existing
        # dest — fails (OSError). None of these should surface as a traceback.
        # The stderr line is the user surface; the durable ERROR record (with the
        # exception attached) covers the pre-pipeline failures — a rejected spec,
        # an existing dest — that `create`'s own rollback record never sees.
        logger.error("tree create failed", exc_info=True)
        print(f"tree create: {exc}", file=sys.stderr)
        return 1
    _emit_ready(result)
    return 0


def _freeform_base(branch: str, *, cwd: str) -> str | None:
    """Return the base override for a freeform branch, if it already exists remotely.

    ``shipit tree create --branch NAME`` has two user intents behind the same flag:
    attach a Tree to an existing remote branch, or start a brand-new branch. The
    remote head is the signal. Existing remote heads are cut from ``origin/NAME`` so
    the new Tree starts at the branch's current tip; absent heads return ``None`` so
    the pure planner keeps its normal ``origin/main`` default for new freeform work.
    Names that sanitize to empty also return ``None`` without probing, leaving the
    planner's domain validation to raise the canonical error.
    """
    if not layout.sanitize_slug(branch):
        return None
    return f"origin/{branch}" if git.remote_branch_exists(branch, cwd=cwd) else None


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
@json_option
def list_cmd(as_json: bool) -> None:
    """List every Tree under the central root with its at-a-glance state.

    Renders the whole fleet — path, branch, base, age, dirty?, PR state — derived
    purely by SCANNING the central root (no manifest); the state is whatever the
    clones on disk say right now.
    """
    raise SystemExit(run_list(as_json=as_json))


@cli_errors
def run_list(*, as_json: bool = False) -> int:
    """Scan the central root and render the Tree fleet. Returns an exit code.

    Scan → the pure :func:`shipit.tree.fleet.build` typed rows → the render
    seam (:func:`format_fleet` text, or ``--json`` off the result's own field
    set). Returns 0 in the normal case — an empty or missing root is a valid
    "no Trees yet" state, not an error. A MISCONFIGURED central root (a
    relative ``SHIPIT_TREES_ROOT`` → :class:`~shipit.tree.layout.LayoutError`)
    maps to ``error: …`` + exit 1 through the shared shell, never a traceback.
    Repo identity is irrelevant here — the central root spans every repo, so
    ``list`` shows the whole fleet (PRD user story 14/22).
    """
    records = registry.scan(layout.central_root())
    emit(fleet.build(records, now=time.time()), format_fleet, as_json=as_json)
    return 0


#: The fleet table's columns, in render order: each is ``(header, cell-renderer)``.
#: A new column is one tuple here — the renderer widths every column to its content.
_LIST_COLUMNS: tuple[tuple[str, Callable[[fleet.FleetTree], str]], ...] = (
    ("PATH", lambda row: row.path),
    # The Tree's reclaim family — write / review / ephemeral — is first-class
    # fleet state (ADR-0018/0027): each kind takes a different gc ladder, so
    # the listing says which one applies rather than leaving it implied by path.
    ("KIND", lambda row: row.kind),
    ("BRANCH", lambda row: row.branch or "(detached)"),
    ("BASE", lambda row: _format_base(row)),
    ("AGE", lambda row: _format_age(row.age_seconds)),
    ("DIRTY", lambda row: "dirty" if row.dirty else "clean"),
    ("PR", lambda row: row.pr or "-"),
)


def format_fleet(result: fleet.Fleet) -> str:
    """The pure text renderer: the fleet as a fixed-width table, or a hint when empty.

    A plain string function over the typed rows (ADR-0030 render seam) — no
    printing; :func:`~._render.emit` owns the terminal write.
    """
    if not result.trees:
        return "No Trees under the central root."
    headers = [header for header, _ in _LIST_COLUMNS]
    rows = [[cell(row) for _, cell in _LIST_COLUMNS] for row in result.trees]
    # Width each column to its widest cell, header included. Pass a single generator
    # to max() (header counts as just another row) rather than star-unpacking one
    # positional arg per row — that materializes an arg list and can hit arg limits.
    all_rows = [headers, *rows]
    widths = [max(len(row[col]) for row in all_rows) for col in range(len(headers))]
    return "\n".join(_format_row(row, widths) for row in all_rows)


def _format_base(row: fleet.FleetTree) -> str:
    """The BASE cell: the upstream ref, annotated with ahead/behind when diverged."""
    base = row.base or "-"
    marks = []
    if row.ahead:
        marks.append(f"+{row.ahead}")
    if row.behind:
        marks.append(f"-{row.behind}")
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


@cli_errors
def run_remove(
    target: str,
    *,
    assume_yes: bool = False,
    confirm: Callable[[str], bool] | None = None,
    is_tty: Callable[[], bool] | None = None,
) -> int:
    """Resolve TARGET to one Tree and delete its clone dir. Returns an exit code.

    Glue over the promoted domain (:mod:`shipit.tree.removal`): scan →
    :func:`~shipit.tree.removal.resolve_target` → the pure typed gate → act on
    its outcome. The one terminal concern — putting the CONFIRM prompt to the
    user — stays here; declining raises the same typed refusal every other
    no-go outcome does, so every failure maps to ``error: …`` + exit 1 through
    the shared shell (misconfigured root, unknown/ambiguous target, declined
    prompt, refused non-interactive risk, failed delete). A clean,
    fully-pushed Tree is always removed without a prompt; ``assume_yes`` (the
    ``--yes`` flag) skips the gate unconditionally.

    ``confirm``/``is_tty`` are injectable so the prompt wiring is testable
    without a real terminal; they default to ``click.confirm`` and
    :func:`_stdin_is_tty` (a guard around ``sys.stdin.isatty`` that reads as
    not-a-TTY when stdin is missing or closed rather than crashing).
    """
    if confirm is None:
        confirm = click.confirm
    if is_tty is None:
        is_tty = _stdin_is_tty
    records = registry.scan(layout.central_root())
    record = removal.resolve_target(records, target)
    gate = removal.gate(record, assume_yes=assume_yes, interactive=is_tty())
    if gate.action is GateAction.REFUSE:
        raise RemovalError(gate.reason)
    if gate.action is GateAction.CONFIRM and not confirm(gate.prompt or ""):
        raise RemovalError(f"aborted — {record.path} left untouched")
    removal.remove(record)
    print(f"REMOVED {record.path}")
    return 0


@tree.command(name="gc")
@click.option(
    "--dry-run",
    is_flag=True,
    help=(
        "Preview only: print the removable/keep partition for the whole fleet "
        "and delete NOTHING. The preview is the exact decision the real sweep acts on."
    ),
)
@click.option(
    "--threshold",
    default=None,
    type=DURATION,
    metavar="DURATION",
    help=(
        "How long a Tree must be IDLE — no file written anywhere in it — before it "
        "counts as abandoned, as a human duration (e.g. 48h, 36h, 90m). Defaults to "
        "48h when omitted. A Tree with uncommitted changes or unpushed commits is "
        "kept no matter how idle."
    ),
)
def gc_cmd(dry_run: bool, threshold: float | None) -> None:
    """Sweep the central root: remove only provably-safe Trees.

    Scans every Tree and deletes ONLY those that hold nothing you could lose and that
    nobody has touched in two days::

        KEEP  if  dirty  ||  unpushed  ||  idle < 48h

    Idle is measured, not inferred: the newest file mtime anywhere in the Tree (build
    and env dirs pruned). Across the whole fleet the signal separates cleanly — a Tree
    someone is working in reads under an hour idle, an abandoned one days — so there is
    no ambiguous middle to list for a human, and no PR state, session pidfile or Tree
    kind in the rule at all.

    ``--dry-run`` prints the same partition the real sweep would act on and deletes
    nothing; ``--threshold DURATION`` (e.g. ``36h``) overrides the 48h idle boundary
    for this run.

    Each Tree is reported as it is removed, so an interrupted sweep still leaves a
    record of what it destroyed. Exits 1 if any Tree was kept because a signal could
    not be read rather than because it was judged safe: the fleet was only partly
    judged, so "nothing to reclaim" would be a guess.
    """
    raise SystemExit(run_gc(dry_run=dry_run, idle_threshold_seconds=threshold))


@cli_errors
def run_gc(
    *, dry_run: bool = False, idle_threshold_seconds: float | None = None
) -> int:
    """Build the gc plan, then either preview it or sweep it. Returns an exit code.

    Glue over the promoted domain (:mod:`shipit.tree.gc`): ONE
    :func:`~shipit.tree.gc.plan_fleet` call builds the frozen plan BOTH modes
    consume, so a ``--dry-run`` preview can NEVER drift from the action — it
    renders the very plan the real :func:`~shipit.tree.gc.sweep` applies; only
    the "render vs delete" tail differs. ``idle_threshold_seconds`` overrides
    the 48h idle boundary (the ``--threshold`` flag, already parsed to seconds
    at click per the two-tier exit contract: a malformed duration is a usage
    error, exit 2).

    Returns 0 in the normal case — an empty root or a fleet with nothing to
    reclaim is a valid outcome, not an error; a misconfigured central root (a
    relative ``SHIPIT_TREES_ROOT`` → :class:`~shipit.tree.layout.LayoutError`)
    maps to ``error: …`` + exit 1 through the shared shell. Repo identity is
    irrelevant — ``gc`` spans the whole central root, like ``list``.

    Returns 1 — the contract's runtime-failure tier, both modes alike — when the
    fleet was only PARTIALLY judged (:attr:`~shipit.tree.gc.GcPlan.incomplete`):
    gc's job is to decide the whole root, and a run that could not read part of
    it did not do that job, however many Trees it reclaimed along the way.
    Reporting that as success is what let 526 Trees accumulate (#1011) — a
    drained ``gh`` budget turned 371 removable Trees into ``removable 0``, exit
    0, indistinguishable from a clean fleet. That exact CAUSE is gone with the PR
    read (ADR-0072), but the shape is not: the rule's own unreadable-signal arms
    inherited it, and a fleet-wide walk or ``rev-list`` failure would now keep
    everything and report the same clean bill of health. The exit code carries no
    threshold: one unexamined Tree and five hundred are the same claim ("this
    verdict is not the whole root"), and the counts say which it was.

    Removals are streamed as they happen rather than rendered at the end (the
    ``on_removed`` sink): a sweep is a multi-minute destructive operation, and
    if it is interrupted the lines already on stdout are the only record of
    what it destroyed.
    """
    plan = gc.plan_fleet(
        layout.central_root(),
        idle_threshold_seconds=(
            cleanup.IDLE_THRESHOLD_SECONDS
            if idle_threshold_seconds is None
            else idle_threshold_seconds
        ),
    )
    if dry_run:
        _render_gc_preview(plan)
        return 1 if plan.incomplete else 0
    result = gc.sweep(plan, on_removed=_print_removed)
    _render_gc_result(result)
    return 1 if result.incomplete else 0


def _print_removed(path: str) -> None:
    """Announce one Tree the sweep just took off disk — the streaming sink.

    Passed to :func:`~shipit.tree.gc.sweep` as its ``on_removed``, so the
    domain stays print-free while each ``REMOVED`` line reaches the terminal at
    the moment of the delete. Flushed per line ON PURPOSE: stdout to a pipe or
    a file is block-buffered, and a killed sweep takes its unflushed buffer
    with it — which is the whole failure this sink exists to fix (#1011).
    """
    print(f"REMOVED {path}", flush=True)


def _render_gc_result(result: gc.GcResult) -> None:
    """Render the sweep's tail: the failures and the summary.

    The terminal half of the plan+sweep split: every fact printed here came
    back in the :class:`~shipit.tree.gc.GcResult` — the delete failures the
    sweep continued past (stderr), the ``removed`` count that reflects what
    actually came off disk, and the incomplete-view report. The ``REMOVED``
    lines are NOT printed here: :func:`_print_removed` already streamed them
    from inside the sweep, and reprinting them would double the audit trail.
    There is no STALE list to print any more — the bucket it rendered is gone
    (ADR-0072), and a Tree that is not removable is simply kept.
    """
    for failure in result.failed:
        print(f"FAILED  {failure.path}: {failure.error}", file=sys.stderr)
    counts = f"removed {len(result.removed)}, kept {result.kept}"
    print(f"gc: {_lead(result)}{counts}")
    _render_incomplete_view(result, verb="judged")


def _lead(view: gc.GcPlan | gc.GcResult) -> str:
    """The summary's leading clause — empty for a complete view, loud otherwise.

    An incomplete run's counts describe only the part of the root gc could
    judge, so the gap goes IN FRONT of them: ``gc: INCOMPLETE — 502 of 512
    unexamined …`` can't be skimmed as the healthy ``gc: removed 0, …`` that hid
    this failure for a whole fleet's lifetime (#1011). A complete view reads
    exactly as it always has.
    """
    if not view.incomplete:
        return ""
    return (
        f"INCOMPLETE — {view.unexamined} of {view.total} unexamined "
        "(a signal could not be read); "
    )


def _render_incomplete_view(view: gc.GcPlan | gc.GcResult, *, verb: str) -> None:
    """Explain a partially-judged fleet on stderr, or print nothing if it was whole.

    The summary's leading clause states THAT the view was incomplete; this states
    what it means and what to do — that the unexamined Trees were kept because a
    signal could not be read, not because they were judged safe, and what most
    likely caused it. Naming a cause is the point: a bare "502 unexamined" reads as
    a fleet mystery, and an operator with no lead will either ignore it or go
    hunting (#1011).

    The cause text follows the SIGNALS, and ADR-0072 changed which ones can do this.
    It used to name the PR read — a drained ``gh`` budget, then (once #1014 batched
    the read per repo) a single repo's failed ``gh pr list``. Reclaim no longer reads
    PR state at all, so neither can hide a Tree from the rule; what can is the rule's
    own two unreadable-signal arms (:func:`~shipit.tree.cleanup.is_unexamined`), and
    both are LOCAL — a failed ``git rev-list`` or a failed activity walk. So the
    leads are local too: permissions, a vanished mount, a Tree being written as it was
    read. An operator sent to check `gh api rate_limit` for what is now a filesystem
    problem is an operator sent to the wrong machine entirely.

    ``verb`` is the mode's tense (``judged`` / ``would judge``), the only difference
    between the two gc tails.
    """
    if not view.incomplete:
        return
    print(
        f"gc: {verb} {view.judged} of {view.total}; {view.unexamined} kept UNEXAMINED "
        "— a signal could not be read (the unpushed-commit list, or the activity "
        "walk), so those Trees were kept without a verdict, not judged safe. This "
        "verdict covers only part of the root.",
        file=sys.stderr,
    )
    print(
        "gc: both signals are read from the Tree itself, so the likeliest causes are "
        "local — a permissions change, a vanished mount, or a Tree being written as "
        "it was read. Re-running usually clears a transient failure.",
        file=sys.stderr,
    )
    print(
        "gc: if the gap covers most of the fleet, suspect the root itself rather than "
        "any one Tree — check that the central root is readable and fully mounted.",
        file=sys.stderr,
    )


def _render_gc_preview(plan: gc.GcPlan) -> None:
    """Render the removable/keep partition WITHOUT touching disk (``--dry-run``).

    Renders the exact plan the real sweep would apply, so a preview can never
    disagree with the sweep that follows it. The buckets are walked GENERICALLY
    (``dataclasses.fields``) and each is printed by its own field name — so if an
    upstream change adds a bucket, it surfaces here with no edit and no hard-coded
    state vocabulary to fall out of date. Deletes nothing: there is no ``rmtree`` on
    this path at all.

    The same INCOMPLETE-view report the real sweep surfaces is emitted here too,
    off the same helpers and leading the same summary — a preview is the mode an
    operator uses to ASK whether the fleet is clean, so it is the mode that most
    have to admit when it does not know (#1011).
    """
    counts: list[str] = []
    for field in fields(plan.partition):
        bucket = getattr(plan.partition, field.name)
        for record in bucket:
            print(f"{field.name.upper():<9} {record.path}")
        counts.append(f"{field.name} {len(bucket)}")
    # Mechanics at DEBUG: a dry run deletes nothing, so its partition is not a
    # milestone — the per-Tree ladder decisions are already recorded by classify.
    logger.debug("gc --dry-run: %s", ", ".join(counts))
    if plan.incomplete:
        logger.warning(
            "gc --dry-run: would judge %d of %d; %d kept unexamined (a signal could "
            "not be read — incomplete view of the fleet)",
            plan.judged,
            plan.total,
            plan.unexamined,
        )
    print(f"gc --dry-run (no Trees deleted): {_lead(plan)}{', '.join(counts)}")
    _render_incomplete_view(plan, verb="would judge")
