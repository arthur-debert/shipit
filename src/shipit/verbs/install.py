"""install — vendor shipit's managed "slow set" into a consumer and reconcile it.

``shipit install <path>`` copies the small, file-structure-dependent set (the
skills, the AGENTS.md block, the bootstrap launcher) into a consumer repo,
recording a per-unit pristine ``sha256`` in ``.shipit.toml``. On re-install it
hash-compares each unit against its stored pristine and opens a DRAFT PR with the
changes — never an admin push (docs/dev/architecture.lex §2, docs/prd/install-reconciliation.md).

Reconciliation is a HASH COMPARE, not a subsystem. Per unit there are four
outcomes and no more — the moment it grows features it has become the drift
engine this design exists to delete (docs/dev/lessons-learned.lex §4):

  - absent in the consumer            -> ADD      (write it; record its hash)
  - present, hash == desired          -> NOOP     (already current; nothing to do)
  - present, hash == stored pristine  -> UPDATE   (overwrite silently; advance pristine)
  - present, hash != stored pristine  -> OVERRIDE (consumer-edited: still propose
                                                    shipit's content on the PR
                                                    branch, but FLAG it with a diff
                                                    so the human decides at merge)

ADD/UPDATE/OVERRIDE all write onto the install BRANCH, never to the consumer's
main — nothing lands without the human merging the draft PR (pull, never push).
The OVERRIDE/UPDATE split is the human signal: an UPDATE is safe to merge blind;
an OVERRIDE would discard a consumer edit, so the PR surfaces its diff loudly.

The pure decision logic (:func:`decide` / :func:`plan`) is kept out of the
filesystem + gh boundary so it is unit-testable, the same split checks.py uses.
"""

from __future__ import annotations

import difflib
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from .. import __version__, config, gh

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

AGENTS_FILE = "AGENTS.md"
AGENTS_KEY = "AGENTS.md#shipit-block"
BLOCK_OPEN = "<!-- Managed by shipit; do not edit. Regenerate via shipit install. -->"
BLOCK_CLOSE = "<!-- End shipit-managed block. -->"

# The gate units Step 2 deferred to Step 3 (docs/prd/lint-gate.md). The consumer gets
# the thin lefthook caller (whole file) and a `lint = "shipit lint"` task BLOCK
# in its own pixi.toml — NEVER a linter-dependency block: the linters ride in as
# shipit-the-package's own deps, so the consumer's manifest carries only the
# stable task line (architecture.lex §5). The pixi block uses TOML-comment
# markers (HTML comments are invalid TOML) and anchors under `[tasks]` so the
# managed key lands in the right table on a first install.
LEFTHOOK_FILE = "lefthook.yml"
# Activating the gate is one bounded `lefthook install`, which writes the
# `.git/hooks/{pre-commit,pre-push}` shims that fire `pixi run lint`. This is
# EXACTLY what the `install-hooks` pixi task wraps (`lefthook install`) — one
# definition — so the consumer install and shipit-self's bootstrap activate the
# gate through the same invocation rather than a re-implemented hook writer.
# lefthook install is idempotent and rewrites only its own managed region of a
# hook file, so a re-install is a no-op and pre-existing unrelated hooks survive.
LEFTHOOK_BINARY = "lefthook"
HOOK_ACTIVATE_ARGV = ["install"]
PIXI_FILE = "pixi.toml"
PIXI_KEY = "pixi.toml#shipit-tasks"
PIXI_OPEN = (
    "# >>> shipit-managed tasks (do not edit; regenerate via `shipit install`) >>>"
)
PIXI_CLOSE = "# <<< shipit-managed tasks <<<"
PIXI_ANCHOR = "[tasks]"

INSTALL_BRANCH = "shipit/install"
COMMIT_MESSAGE = "chore(shipit): install/update the managed set"

ADD = "add"
NOOP = "noop"
UPDATE = "update"
OVERRIDE = "override"


# --------------------------------------------------------------------------
# Managed units (loaded from package data)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Unit:
    """One managed unit — a whole file or a marker-delimited block.

    ``content`` is the desired bytes: a file's full contents, or (for a block)
    the inner text that lives between the markers.
    """

    key: str  # the [managed] table key
    dest: str  # path relative to the consumer root
    kind: str  # "file" | "block"
    content: bytes
    executable: bool = False
    # Block units only: the delimiters that fence shipit's region in a
    # consumer-owned file, and (for a TOML table) the header the block anchors
    # under on a first insert. Default to the AGENTS.md HTML-comment markers.
    open_marker: str = BLOCK_OPEN
    close_marker: str = BLOCK_CLOSE
    anchor: str | None = None

    def desired_inner(self) -> str:
        """A block unit's canonical inner text (newline-trimmed)."""
        return self.content.decode("utf-8").strip("\n")

    def desired_hash(self) -> str:
        """The ``sha256:`` pristine hash of this unit's desired content."""
        if self.kind == "block":
            return config.content_hash(self.desired_inner().encode("utf-8"))
        return config.content_hash(self.content)


def _data_bytes(*parts: str) -> bytes:
    """Read a ``shipit.data`` file via the resources Traversable API."""
    return resources.files("shipit.data").joinpath(*parts).read_bytes()


def _skills_root():
    """The bundled skills tree — wheel package data, or the repo root in dev.

    Returns a Traversable (installed wheel) or a :class:`Path` (editable/source
    checkout, where skills/ lives at the repo root and is force-included only
    into the built wheel). Both honor the ``iterdir`` / ``is_dir`` / ``is_file`` /
    ``read_bytes`` protocol :func:`_walk_files` uses.
    """
    bundled = resources.files("shipit.data").joinpath("skills")
    if bundled.is_dir():
        return bundled
    return Path(__file__).resolve().parents[3] / "skills"


def _walk_files(node, prefix: str = ""):
    """Yield ``(relpath, bytes)`` for every file under ``node``, depth-first sorted."""
    for child in sorted(node.iterdir(), key=lambda p: p.name):
        rel = f"{prefix}{child.name}"
        if child.is_dir():
            yield from _walk_files(child, prefix=f"{rel}/")
        elif child.is_file():
            yield rel, child.read_bytes()


def load_units() -> list[Unit]:
    """The managed set, in a stable order (skills, then the AGENTS block, then bootstrap)."""
    units: list[Unit] = []

    for rel, content in _walk_files(_skills_root()):
        units.append(
            Unit(
                key=f"skills/{rel}",
                dest=f"skills/{rel}",
                kind="file",
                content=content,
            )
        )

    units.append(
        Unit(
            key=AGENTS_KEY,
            dest=AGENTS_FILE,
            kind="block",
            content=_data_bytes("agents-block.md"),
        )
    )

    units.append(
        Unit(
            key="bin/shipit",
            dest="bin/shipit",
            kind="file",
            content=_data_bytes("bootstrap", "shipit"),
            executable=True,
        )
    )

    # The gate units (docs/prd/lint-gate.md): the thin lefthook caller and the
    # `lint = "shipit lint"` task block in the consumer's pixi.toml.
    units.append(
        Unit(
            key=LEFTHOOK_FILE,
            dest=LEFTHOOK_FILE,
            kind="file",
            content=_data_bytes("lefthook.yml"),
        )
    )
    units.append(
        Unit(
            key=PIXI_KEY,
            dest=PIXI_FILE,
            kind="block",
            content=_data_bytes("pixi-tasks-block.toml"),
            open_marker=PIXI_OPEN,
            close_marker=PIXI_CLOSE,
            anchor=PIXI_ANCHOR,
        )
    )
    return units


# --------------------------------------------------------------------------
# Block splicing
# --------------------------------------------------------------------------


def extract_block(
    text: str, open_marker: str = BLOCK_OPEN, close_marker: str = BLOCK_CLOSE
) -> str | None:
    """The inner text of the marker-delimited block, or ``None`` when absent."""
    i = text.find(open_marker)
    if i == -1:
        return None
    j = text.find(close_marker, i)
    if j == -1:
        return None
    return text[i + len(open_marker) : j].strip("\n")


def splice_block(
    text: str,
    inner: str,
    open_marker: str = BLOCK_OPEN,
    close_marker: str = BLOCK_CLOSE,
    anchor: str | None = None,
) -> str:
    """Insert or replace the managed block in ``text``, owning only the block.

    When the markers are already present the block is replaced in place. On a
    first insert with an ``anchor`` (a TOML table header), the block is placed
    immediately after that header — creating the header at EOF if absent — so the
    managed keys land inside the right table. Without an anchor it appends at EOF
    (the AGENTS.md case).
    """
    block = f"{open_marker}\n{inner}\n{close_marker}"
    i = text.find(open_marker)
    if i != -1:
        j = text.find(close_marker, i)
        if j != -1:
            return text[:i] + block + text[j + len(close_marker) :]
    if anchor is not None:
        return _insert_under_anchor(text, anchor, block)
    if text and not text.endswith("\n"):
        text += "\n"
    return f"{text}\n{block}\n" if text else f"{block}\n"


def _insert_under_anchor(text: str, anchor: str, block: str) -> str:
    """Place ``block`` right after the ``anchor`` line, adding the anchor if absent."""
    lines = text.splitlines()
    for idx, line in enumerate(lines):
        if line.strip() == anchor:
            spliced = lines[: idx + 1] + block.splitlines() + lines[idx + 1 :]
            return "\n".join(spliced) + "\n"
    base = text.rstrip("\n")
    sep = "\n\n" if base else ""
    return f"{base}{sep}{anchor}\n{block}\n"


# --------------------------------------------------------------------------
# Pure reconciliation
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Decision:
    unit: Unit
    action: str
    desired_hash: str
    consumer_hash: str | None
    pristine_hash: str | None


def decide(
    *, consumer_hash: str | None, pristine_hash: str | None, desired_hash: str
) -> str:
    """The reconciliation outcome for one unit — the whole algorithm, four cases."""
    if consumer_hash is None:
        return ADD
    if consumer_hash == desired_hash:
        return NOOP
    if pristine_hash is not None and consumer_hash == pristine_hash:
        return UPDATE
    return OVERRIDE


def plan(
    units: list[Unit],
    consumer_hashes: dict[str, str | None],
    pristine: dict[str, str],
) -> list[Decision]:
    """Decide every unit against the consumer state and the stored pristine map."""
    decisions: list[Decision] = []
    for unit in units:
        consumer_hash = consumer_hashes.get(unit.key)
        pristine_hash = pristine.get(unit.key)
        desired_hash = unit.desired_hash()
        decisions.append(
            Decision(
                unit=unit,
                action=decide(
                    consumer_hash=consumer_hash,
                    pristine_hash=pristine_hash,
                    desired_hash=desired_hash,
                ),
                desired_hash=desired_hash,
                consumer_hash=consumer_hash,
                pristine_hash=pristine_hash,
            )
        )
    return decisions


def activates_hooks(decisions: list[Decision]) -> bool:
    """Whether this install should activate the git hooks.

    The pure half of the decision: ``True`` whenever ``lefthook.yml`` is part of
    the reconciled set, i.e. the gate config is (now) in place — so its hooks
    belong live. The actual ``lefthook install`` is the bounded side effect
    :func:`_activate_hooks` performs; the plan only records that it WILL happen.
    Because activation is idempotent, we run it on every install that manages the
    caller, not only on the first ADD.
    """
    return any(d.unit.key == LEFTHOOK_FILE for d in decisions)


# --------------------------------------------------------------------------
# Consumer-state I/O
# --------------------------------------------------------------------------


def _consumer_inner(root: Path, unit: Unit) -> str | None:
    """A block unit's current inner text in the consumer, or ``None``."""
    dest = root / unit.dest
    if not dest.is_file():
        return None
    return extract_block(
        dest.read_text(encoding="utf-8"), unit.open_marker, unit.close_marker
    )


def consumer_hash(root: Path, unit: Unit) -> str | None:
    """The hash of a unit's current content in the consumer, or ``None`` if absent."""
    if unit.kind == "block":
        inner = _consumer_inner(root, unit)
        return None if inner is None else config.content_hash(inner.encode("utf-8"))
    dest = root / unit.dest
    if not dest.is_file():
        return None
    return config.content_hash(dest.read_bytes())


def _write_unit(root: Path, unit: Unit) -> None:
    """Apply an ADD/UPDATE: write the file, or splice the block into its file."""
    dest = root / unit.dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    if unit.kind == "block":
        existing = dest.read_text(encoding="utf-8") if dest.is_file() else ""
        dest.write_text(
            splice_block(
                existing,
                unit.desired_inner(),
                unit.open_marker,
                unit.close_marker,
                unit.anchor,
            ),
            encoding="utf-8",
        )
        return
    dest.write_bytes(unit.content)
    if unit.executable:
        dest.chmod(0o755)


def _activate_hooks(root: Path) -> tuple[int, str]:
    """Run ``lefthook install`` in ``root`` — the bounded side effect that turns
    the ``lefthook.yml`` config into live ``.git/hooks``. Returns
    ``(exit code, combined output)``.

    This is the same invocation the ``install-hooks`` pixi task wraps, so the
    gate has one activation definition. A missing ``lefthook`` binary is reported
    (``127``) rather than crashing: unlike the lint gate this is opportunistic
    setup, so install warns and still finishes its PR rather than hard-failing.
    """
    try:
        proc = subprocess.run(
            [LEFTHOOK_BINARY, *HOOK_ACTIVATE_ARGV],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return 127, (
            f"{LEFTHOOK_BINARY}: not found on PATH — provision the pixi env, then "
            f"`pixi run install-hooks` to activate the gate"
        )
    return proc.returncode, proc.stdout + proc.stderr


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def _consumer_snapshot(root: Path, unit: Unit) -> str:
    """The consumer's current text for a unit — captured BEFORE any overwrite."""
    if unit.kind == "block":
        inner = _consumer_inner(root, unit)
        return "" if inner is None else inner + "\n"
    dest = root / unit.dest
    return dest.read_text(encoding="utf-8", errors="replace") if dest.is_file() else ""


def _desired_text(unit: Unit) -> str:
    return (
        unit.desired_inner() + "\n"
        if unit.kind == "block"
        else unit.content.decode("utf-8", errors="replace")
    )


def _override_diff(unit: Unit, consumer_text: str) -> str:
    """A unified diff of the consumer's edit vs shipit's intended content."""
    diff = difflib.unified_diff(
        consumer_text.splitlines(keepends=True),
        _desired_text(unit).splitlines(keepends=True),
        fromfile=f"{unit.dest} (consumer)",
        tofile=f"{unit.dest} (shipit)",
    )
    return "".join(diff)


def _pr_body(decisions: list[Decision], override_before: dict[str, str]) -> str:
    """The PR body: what was added/updated, and every override surfaced with its diff.

    ``override_before`` holds each overridden unit's consumer content captured
    BEFORE the branch write, so the diff shows the real divergence (not an empty
    diff against the content shipit just wrote over it).
    """
    adds = [d for d in decisions if d.action == ADD]
    updates = [d for d in decisions if d.action == UPDATE]
    overrides = [d for d in decisions if d.action == OVERRIDE]

    lines = ["`shipit install` reconciled the managed set.", ""]
    if adds:
        lines.append("### Added")
        lines += [f"- `{d.unit.dest}`" for d in adds]
        lines.append("")
    if updates:
        lines.append("### Updated")
        lines += [f"- `{d.unit.dest}`" for d in updates]
        lines.append("")
    if overrides:
        lines.append("### Overrides — consumer-edited, review before merging")
        lines.append(
            "These units were edited in the consumer since the last shipit install. "
            "This PR proposes restoring shipit's content (the diff below); **merging "
            "discards the consumer edit**. Review each diff and decide — closing the "
            "PR keeps the consumer's version."
        )
        lines.append("")
        for d in overrides:
            lines.append(f"<details><summary><code>{d.unit.dest}</code></summary>")
            lines.append("")
            lines.append("```diff")
            lines.append(
                _override_diff(d.unit, override_before.get(d.unit.key, "")).rstrip("\n")
            )
            lines.append("```")
            lines.append("</details>")
            lines.append("")
    if activates_hooks(decisions):
        lines.append("### Gate activated")
        lines.append(
            "Ran `lefthook install`, so `.git/hooks/{pre-commit,pre-push}` now fire "
            "`pixi run lint` — the gate is **live**, not just configured. Activation "
            "is idempotent and leaves pre-existing unrelated hooks intact."
        )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _shipit_version() -> str:
    """The shipit commit that wrote the set (its repo HEAD), else the package version."""
    pkg_dir = Path(__file__).resolve().parents[1]
    try:
        return gh._git(["rev-parse", "HEAD"], cwd=str(pkg_dir)).strip()
    except gh.GhError:
        return __version__


# --------------------------------------------------------------------------
# Orchestrator
# --------------------------------------------------------------------------


def run(
    path: str | None,
    *,
    dry_run: bool = False,
    push: bool = False,
    activate_hooks: Callable[[Path], tuple[int, str]] | None = None,
) -> int:
    """Install/reconcile the managed set into the consumer at ``path``.

    ``activate_hooks`` injects the lefthook boundary so tests exercise the
    activation contract without mutating a real ``.git/hooks`` (mirrors how
    :func:`shipit.verbs.lint.run` injects ``run_tool``).
    """
    activate = activate_hooks or _activate_hooks
    root = Path(path or ".").resolve()
    if not root.is_dir():
        print(f"install: {root} is not a directory", file=sys.stderr)
        return 1

    units = load_units()
    consumer_hashes = {u.key: consumer_hash(root, u) for u in units}

    cfg_path = root / config.CONFIG_NAME
    pristine: dict[str, str] = {}
    if cfg_path.is_file():
        try:
            pristine = config.load_managed(config.load(cfg_path))
        except config.ConfigError as exc:
            print(f"install: ignoring unreadable manifest: {exc}", file=sys.stderr)

    decisions = plan(units, consumer_hashes, pristine)
    # ADD/UPDATE/OVERRIDE all write onto the branch; only NOOP writes nothing.
    writes = [d for d in decisions if d.action in (ADD, UPDATE, OVERRIDE)]
    overrides = [d for d in decisions if d.action == OVERRIDE]

    print(f"install: {root}{' (dry-run)' if dry_run else ''}")
    for d in decisions:
        if d.action != NOOP:
            print(f"  {d.action:8} {d.unit.dest}")
    if not writes:
        print("  nothing to do — managed set is current.")
        return 0

    if dry_run:
        # Dry-run must have NO side effects: no writes, no git, no PR.
        print(
            f"  ({len(writes)} to write, {len(overrides)} override(s)) — dry-run, nothing written"
        )
        return 0

    # Snapshot each override's consumer content BEFORE writing, so the PR diff
    # shows the real divergence rather than an empty diff against what we wrote.
    override_before = {d.unit.key: _consumer_snapshot(root, d.unit) for d in overrides}

    # Apply the writes, then record the advanced pristine map. Build [managed]
    # from the CURRENT decisions only — so a unit retired in a later shipit
    # version drops out of the manifest rather than lingering as a stale key.
    for d in writes:
        _write_unit(root, d.unit)
    new_managed = {d.unit.key: d.desired_hash for d in decisions}
    config.write_manifest(cfg_path, version=_shipit_version(), managed=new_managed)

    # Turn the gate on: with lefthook.yml on disk, activate the local hooks so
    # `pixi run lint` fires at commit time — the gate ships LIVE, not dormant.
    # Opportunistic, so a missing lefthook warns rather than aborting the PR.
    if activates_hooks(decisions):
        rc_hooks, out_hooks = activate(root)
        if rc_hooks == 0:
            print("  activated git hooks (lefthook install) — the gate is live")
        else:
            print(
                f"install: could not activate git hooks: {out_hooks.strip()}",
                file=sys.stderr,
            )

    changed_paths = sorted({d.unit.dest for d in writes} | {config.CONFIG_NAME})
    cwd = str(root)

    try:
        if push:
            # Break-glass: commit on the current branch and push straight to it
            # (relies on the repo's admin bypass). Reserved for bootstrapping a
            # repo that cannot yet run the PR loop.
            branch = gh.git_current_branch(cwd=cwd)
            if branch is None:
                print("install: --push needs a checked-out branch", file=sys.stderr)
                return 1
            gh.git_add(changed_paths, cwd=cwd)
            gh.git_commit(COMMIT_MESSAGE, changed_paths, cwd=cwd)
            gh.git_push(branch, cwd=cwd)
            print(f"  pushed to {branch} (break-glass --push)")
            return 0

        # Default: stage onto an install branch, push it, open a DRAFT PR.
        gh.git_switch_create(INSTALL_BRANCH, cwd=cwd)
        gh.git_add(changed_paths, cwd=cwd)
        gh.git_commit(COMMIT_MESSAGE, changed_paths, cwd=cwd)
        # The install branch is regenerated from HEAD each run; force so a re-run
        # with an open install PR updates it rather than failing non-fast-forward.
        gh.git_push(INSTALL_BRANCH, cwd=cwd, force=True)
        existing = gh.pr_url_for_head(INSTALL_BRANCH, cwd=cwd)
        if existing:
            # The force-push already refreshed the open PR's diff.
            print(f"  updated draft PR: {existing}")
            return 0
        url = gh.pr_create(
            head=INSTALL_BRANCH,
            title="shipit: install/update the managed set",
            body=_pr_body(decisions, override_before),
            draft=True,
            cwd=cwd,
        )
        print(f"  opened draft PR: {url}")
        return 0
    except gh.GhError as exc:
        # Match gh_setup: a boundary failure (no remote, auth, not a repo) is a
        # clean CLI error + non-zero exit, not a raw traceback.
        print(f"install: git/gh step failed: {exc}", file=sys.stderr)
        return 1
