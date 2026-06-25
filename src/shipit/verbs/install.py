"""install — vendor shipit's managed "slow set" into a consumer and reconcile it.

``shipit install <path>`` copies the small, file-structure-dependent set (the
skills, the AGENTS.md block, the bootstrap launcher) into a consumer repo,
recording a per-unit pristine ``sha256`` in ``.shipit.toml``. On re-install it
hash-compares each unit against its stored pristine and opens a DRAFT PR with the
changes — never an admin push (docs/dev/architecture.lex §2, ROADMAP.lex §2).

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
import sys
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
    return units


# --------------------------------------------------------------------------
# Block splicing
# --------------------------------------------------------------------------


def extract_block(text: str) -> str | None:
    """The inner text of the shipit-managed block, or ``None`` when absent."""
    i = text.find(BLOCK_OPEN)
    if i == -1:
        return None
    j = text.find(BLOCK_CLOSE, i)
    if j == -1:
        return None
    return text[i + len(BLOCK_OPEN) : j].strip("\n")


def splice_block(text: str, inner: str) -> str:
    """Insert or replace the managed block in ``text``, owning only the block."""
    block = f"{BLOCK_OPEN}\n{inner}\n{BLOCK_CLOSE}"
    i = text.find(BLOCK_OPEN)
    if i != -1:
        j = text.find(BLOCK_CLOSE, i)
        if j != -1:
            return text[:i] + block + text[j + len(BLOCK_CLOSE) :]
    if text and not text.endswith("\n"):
        text += "\n"
    return f"{text}\n{block}\n" if text else f"{block}\n"


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


# --------------------------------------------------------------------------
# Consumer-state I/O
# --------------------------------------------------------------------------


def _consumer_inner(root: Path, unit: Unit) -> str | None:
    """A block unit's current inner text in the consumer, or ``None``."""
    dest = root / unit.dest
    if not dest.is_file():
        return None
    return extract_block(dest.read_text(encoding="utf-8"))


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
        dest.write_text(splice_block(existing, unit.desired_inner()), encoding="utf-8")
        return
    dest.write_bytes(unit.content)
    if unit.executable:
        dest.chmod(0o755)


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def _override_diff(root: Path, decision: Decision) -> str:
    """A short unified diff of shipit's intended content vs the consumer's edit."""
    unit = decision.unit
    if unit.kind == "block":
        consumer_text = (_consumer_inner(root, unit) or "") + "\n"
        desired_text = unit.desired_inner() + "\n"
    else:
        consumer_text = (root / unit.dest).read_text(encoding="utf-8", errors="replace")
        desired_text = unit.content.decode("utf-8", errors="replace")
    diff = difflib.unified_diff(
        consumer_text.splitlines(keepends=True),
        desired_text.splitlines(keepends=True),
        fromfile=f"{unit.dest} (consumer)",
        tofile=f"{unit.dest} (shipit)",
    )
    return "".join(diff)


def _pr_body(root: Path, decisions: list[Decision]) -> str:
    """The PR body: what was added/updated, and every override surfaced with its diff."""
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
            lines.append(_override_diff(root, d).rstrip("\n"))
            lines.append("```")
            lines.append("</details>")
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


def run(path: str | None, *, dry_run: bool = False, push: bool = False) -> int:
    """Install/reconcile the managed set into the consumer at ``path``."""
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
        print(f"  ({len(writes)} to write, {len(overrides)} override(s)) — dry-run, nothing written")
        return 0

    # Apply the writes, then record the advanced pristine map. Every unit the
    # branch now carries shipit's content for is pinned to its desired hash.
    for d in writes:
        _write_unit(root, d.unit)
    new_managed = dict(pristine)
    for d in decisions:
        new_managed[d.unit.key] = d.desired_hash
    config.write_manifest(cfg_path, version=_shipit_version(), managed=new_managed)

    changed_paths = sorted({d.unit.dest for d in writes} | {config.CONFIG_NAME})
    cwd = str(root)

    if push:
        # Break-glass: commit on the current branch and push straight to it
        # (relies on the repo's admin bypass). Reserved for bootstrapping a repo
        # that cannot yet run the PR loop.
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
    gh.git_push(INSTALL_BRANCH, cwd=cwd)
    url = gh.pr_create(
        head=INSTALL_BRANCH,
        title="shipit: install/update the managed set",
        body=_pr_body(root, decisions),
        draft=True,
        cwd=cwd,
    )
    print(f"  opened draft PR: {url}")
    return 0
