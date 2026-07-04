"""install — vendor shipit's managed "slow set" into a consumer and reconcile it.

``shipit install <path>`` copies the small, file-structure-dependent set (the
skills, the AGENTS.md block, the bootstrap launcher, the ``./claude-start``
session launcher, the ``SessionStart`` activation hook) into a consumer repo,
recording a per-unit pristine ``sha256`` in ``.shipit.toml``. On re-install it
hash-compares each unit against its stored pristine and refreshes the drift IN
THE WORKING TREE — committing the result is the caller's job. Only ``--pr``
opts into the standalone reconcile flow that stages the set on the
``shipit/install`` branch and opens a DRAFT PR — never an admin push
(docs/dev/architecture.lex §2, docs/prd/install-reconciliation.md; #359: the
branch/PR side effect is explicit opt-in, so an install run mid-workstream
never races its own stray PR to main).

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

ADD/UPDATE/OVERRIDE all write shipit's content; only NOOP writes nothing. In
the default working-tree mode the writes land uncommitted, so ``git diff`` is
the review surface before the caller commits them into their own work. On the
``--pr`` path they land on the install BRANCH, never on the consumer's main —
nothing lands without the human merging the draft PR (pull, never push). The
OVERRIDE/UPDATE split is the human signal either way: an UPDATE is safe to take
blind; an OVERRIDE would discard a consumer edit, so it is surfaced loudly (a
stderr warning in the working tree, the diff in the PR body).

Install also runs a RETIRED-FILES pass (docs/prd/rvw01-sole-requester.md,
ADR-0031): a packaged manifest (``retired-files.toml``) lists paths shipit used
to distribute that must no longer exist, each with every known pristine
content hash. Three outcomes, same safety philosophy as the pristine-hash
reconcile above — never destroy a local edit:

  - absent                              -> NOOP   (already gone)
  - present, hash in known pristines    -> DELETE (safe: it is shipit's own content)
  - present, hash matches NO known one  -> KEEP   (locally modified: warn, keep)

The pure decision logic (:func:`decide` / :func:`plan`, and
:func:`decide_retired` / :func:`plan_retired`) is kept out of the filesystem +
gh boundary so it is unit-testable, the same split checks.py uses.
"""

from __future__ import annotations

import difflib
import json
import logging
import sys
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from .. import __version__, config, execrun, gh, git

logger = logging.getLogger("shipit.install")

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

AGENTS_FILE = "AGENTS.md"
AGENTS_KEY = "AGENTS.md#shipit-block"
BLOCK_OPEN = "<!-- Managed by shipit; do not edit. Regenerate via shipit install. -->"
BLOCK_CLOSE = "<!-- End shipit-managed block. -->"

# The lint-check units Step 2 deferred to Step 3 (docs/prd/lint-checks.md). The consumer gets
# the thin lefthook caller (whole file) and a `lint = "shipit lint"` task BLOCK
# in its own pixi.toml — NEVER a linter-dependency block: the linters ride in as
# shipit-the-package's own deps, so the consumer's manifest carries only the
# stable task line (architecture.lex §5). The pixi block uses TOML-comment
# markers (HTML comments are invalid TOML) and anchors under `[tasks]` so the
# managed key lands in the right table on a first install.
LEFTHOOK_FILE = "lefthook.yml"
# Activating the checks is one bounded `lefthook install`, which writes the
# `.git/hooks/{pre-commit,pre-push}` shims that fire `pixi run lint`. This is
# EXACTLY what the `install-hooks` pixi task wraps (`lefthook install`) — one
# definition — so the consumer install and shipit-self's bootstrap activate the
# checks through the same invocation rather than a re-implemented hook writer.
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

# The HAR01 agent harness (docs/prd/har01-coordinator-guard-and-role-prompts.md):
# the three GENERATED subagent agent-defs and the committed `PreToolUse` hook line
# join the managed set so a consumer's agents follow the same dev cycle + guard.
#
# Agent-defs: whole-file units, sourced from `.claude/agents/<role>.md` — the same
# repo-root-in-dev / wheel-package-data split skills use (force-included via
# pyproject). They are generated (`pixi run regen-roles`); install only vendors the
# committed output, it never regenerates.
#
# settings hook: NOT a whole-file unit. `.claude/settings.json` is Claude-Code-owned
# structured JSON a consumer fills with their own permissions/env/hooks; shipit owns
# ONLY its one `PreToolUse` entry. So it is a `kind="block"` unit with a JSON splice
# (`fmt=FMT_JSON_HOOK`) instead of comment-marker text splice: the managed "inner" is
# shipit's canonical PreToolUse entry, identified in the consumer file by its command
# marker. Reconciliation is the standard four-case `decide()` on that entry's hash —
# the consumer's other settings are merged through untouched, never clobbered, and a
# consumer edit to shipit's own entry surfaces as an OVERRIDE like any other unit.
AGENTS_DEF_DIR = ".claude/agents"
SETTINGS_FILE = ".claude/settings.json"
SETTINGS_KEY = ".claude/settings.json#shipit-pretooluse-hook"
# The substring that identifies shipit's managed PreToolUse entry in a consumer's
# settings.json, independent of the runner prefix (`pixi run`, a bare path, etc.).
SETTINGS_HOOK_MARKER = "shipit hook pretooluse"

# The HAR02 eval wire (docs/prd/har02-run-eval.md) adds two more committed
# settings.json hook lines — the terminal-hook eval boundary — each its own
# event array + command marker, reconciled by the SAME event-keyed JSON-hook
# splice as PreToolUse. Stop/SubagentStop entries carry no `matcher` (they bind
# to no tool), so the entry shape is just `{"hooks": [...]}`.
SETTINGS_STOP_KEY = ".claude/settings.json#shipit-stop-hook"
SETTINGS_STOP_MARKER = "shipit hook stop"
SETTINGS_SUBAGENTSTOP_KEY = ".claude/settings.json#shipit-subagentstop-hook"
SETTINGS_SUBAGENTSTOP_MARKER = "shipit hook subagent-stop"

# The SES01 session-bootstrap set (docs/prd/session-bootstrap.md, ADR-0027): the
# `./claude-start` launcher (Layer D — mint a session id and exec
# `claude --worktree <id>`; convenience only, `claude -w <name>` works without it)
# and the SessionStart activation hook line (Layer A — `shipit hook sessionstart`
# writes the repo's toolchain activation into CLAUDE_ENV_FILE). Both join the
# managed set so adopting a repo turns the capability on with no manual wiring.
# The launcher ships like `bin/shipit` (a whole-file bootstrap unit); the hook
# line is one more JSON-hook unit over the same settings.json, owning its event.
LAUNCHER_FILE = "claude-start"
SETTINGS_SESSIONSTART_KEY = ".claude/settings.json#shipit-sessionstart-hook"
SETTINGS_SESSIONSTART_MARKER = "shipit hook sessionstart"

# The settings.json hooks-event arrays each JSON-hook unit owns one entry of.
EVENT_PRETOOLUSE = "PreToolUse"
EVENT_STOP = "Stop"
EVENT_SUBAGENTSTOP = "SubagentStop"
EVENT_SESSIONSTART = "SessionStart"

FMT_MARKERS = "markers"  # block splice via open/close comment markers (default)
FMT_JSON_HOOK = "json-hook"  # block splice into a settings.json hooks-event array

INSTALL_BRANCH = "shipit/install"
COMMIT_MESSAGE = "chore(shipit): install/update the managed set"

ADD = "add"
NOOP = "noop"
UPDATE = "update"
OVERRIDE = "override"

# Retired-files outcomes (docs/prd/rvw01-sole-requester.md). NOOP is shared:
# an absent retired file is the same nothing-to-do as a current managed unit.
DELETE = "delete"
KEEP = "keep"

#: The packaged retired-files manifest (data — retiring a file is an entry, not code).
RETIRED_MANIFEST = "retired-files.toml"


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
    # Block units only: how the managed region is extracted from / spliced into the
    # consumer file. ``FMT_MARKERS`` (default) uses the comment-marker pair above;
    # ``FMT_JSON_HOOK`` parses ``settings.json`` and owns just shipit's one entry in
    # the ``event`` hooks-array (identified by ``marker``), so the consumer's other
    # settings — and shipit's other hook entries — merge through untouched.
    fmt: str = FMT_MARKERS
    event: str = EVENT_PRETOOLUSE
    marker: str = SETTINGS_HOOK_MARKER

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


def _agents_root():
    """The bundled subagent agent-defs — wheel package data, or the repo root in dev.

    Mirrors :func:`_skills_root`: the generated ``.claude/agents/<role>.md`` files
    live at the repo root (where Claude Code reads them for shipit-self) and are
    force-included into the wheel at ``shipit/data/agents`` (pyproject). Returns a
    Traversable (installed wheel) or a :class:`Path` (editable checkout).
    """
    bundled = resources.files("shipit.data").joinpath("agents")
    if bundled.is_dir():
        return bundled
    return Path(__file__).resolve().parents[3] / ".claude" / "agents"


def _canonical_hook_entry(entry: dict) -> str:
    """The stable serialization of a settings.json PreToolUse entry.

    Both the desired (bundled) entry and the consumer's extracted entry pass through
    this one function, so the unit's hash compares STRUCTURE, not byte-formatting —
    a consumer who reformats settings.json (whitespace, key order) still reconciles
    to NOOP as long as shipit's entry is semantically unchanged.
    """
    return json.dumps(entry, indent=2, sort_keys=True)


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

    # The SES01 `./claude-start` launcher (session-bootstrap Layer D): a repo-root
    # alias that mints a session id and execs `claude --worktree <id>`, shipped the
    # same way the bin/shipit bootstrap is.
    units.append(
        Unit(
            key=LAUNCHER_FILE,
            dest=LAUNCHER_FILE,
            kind="file",
            content=_data_bytes("bootstrap", "claude-start"),
            executable=True,
        )
    )

    # The lint-check units (docs/prd/lint-checks.md): the thin lefthook caller and the
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

    # The HAR01 harness (docs/prd/har01-coordinator-guard-and-role-prompts.md): the
    # generated subagent agent-defs (whole files) and the committed PreToolUse hook
    # line (a JSON splice into the consumer's settings.json).
    for rel, content in _walk_files(_agents_root()):
        units.append(
            Unit(
                key=f"{AGENTS_DEF_DIR}/{rel}",
                dest=f"{AGENTS_DEF_DIR}/{rel}",
                kind="file",
                content=content,
            )
        )

    # Store each desired entry already canonicalized, so its hash matches a consumer
    # entry extracted + canonicalized through the same function (formatting-immune).
    # The PreToolUse coordinator-guard (HAR01), the HAR02 eval terminal hooks
    # (Stop = the coordinator run, SubagentStop = each subagent run), and the SES01
    # SessionStart activation hook (coordinator env into CLAUDE_ENV_FILE) are four
    # JSON-hook units over the SAME settings.json, each owning one event array.
    for key, marker, event, data_file in (
        (
            SETTINGS_KEY,
            SETTINGS_HOOK_MARKER,
            EVENT_PRETOOLUSE,
            "claude-settings-pretooluse.json",
        ),
        (
            SETTINGS_STOP_KEY,
            SETTINGS_STOP_MARKER,
            EVENT_STOP,
            "claude-settings-stop.json",
        ),
        (
            SETTINGS_SUBAGENTSTOP_KEY,
            SETTINGS_SUBAGENTSTOP_MARKER,
            EVENT_SUBAGENTSTOP,
            "claude-settings-subagentstop.json",
        ),
        (
            SETTINGS_SESSIONSTART_KEY,
            SETTINGS_SESSIONSTART_MARKER,
            EVENT_SESSIONSTART,
            "claude-settings-sessionstart.json",
        ),
    ):
        hook_entry = json.loads(_data_bytes(data_file))
        units.append(
            Unit(
                key=key,
                dest=SETTINGS_FILE,
                kind="block",
                content=_canonical_hook_entry(hook_entry).encode("utf-8"),
                fmt=FMT_JSON_HOOK,
                event=event,
                marker=marker,
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
# JSON-hook splicing — settings.json PreToolUse entry (the FMT_JSON_HOOK variant)
# --------------------------------------------------------------------------


def _is_shipit_hook(entry: object, marker: str = SETTINGS_HOOK_MARKER) -> bool:
    """Whether a hooks-array entry is shipit's managed one (by command ``marker``).

    Defensive against a malformed consumer file: a non-dict entry, a ``hooks`` that
    is ``null`` or any non-list, a non-dict hook, or a hook whose ``command`` is
    ``null``/non-string all answer ``False`` ("not a shipit hook") rather than
    raising — the structure walk never trips on garbage.
    """
    if not isinstance(entry, dict):
        return False
    hooks = entry.get("hooks")
    if not isinstance(hooks, list):
        return False
    return any(
        isinstance(h, dict) and marker in str(h.get("command") or "") for h in hooks
    )


# Sentinel inner value for a settings.json that exists but is malformed/unparseable
# or is not a JSON object. It is NOT a real hook entry — the read path returns it so
# the unit hashes to something present-but-non-matching (→ OVERRIDE, surfaced for a
# human), and the write path recognizes it to preserve the original byte-for-byte.
_SETTINGS_MALFORMED = "\x00shipit-settings-malformed\x00"


def extract_settings_hook(
    text: str,
    event: str = EVENT_PRETOOLUSE,
    marker: str = SETTINGS_HOOK_MARKER,
) -> str | None:
    """shipit's current ``event`` entry in a settings.json text, canonical, or ``None``.

    Three outcomes, kept in lockstep with :func:`splice_settings_hook` so a read that
    classifies the file is honored by the write that follows:

      - empty file, or a JSON object with no shipit entry -> ``None`` ("absent" -> ADD;
        the write splices shipit's entry into the consumer's object, untouched).
      - a JSON object carrying shipit's entry -> the canonical entry (NOOP/UPDATE/
        OVERRIDE by hash, exactly as before).
      - **unparseable, or valid JSON that is not an object** -> a non-``None`` sentinel
        so the reconciler reads it as present-but-divergent (OVERRIDE): a malformed
        ``.claude/settings.json`` is a CONFLICT to surface for a human, never an
        absent file we ADD onto and never a crash. The matching write preserves it.

    Only shipit's own ``event`` entry (matched by ``marker``) is the managed region;
    the consumer's other settings — and shipit's entries in OTHER event arrays — are
    never inspected.
    """
    text = text.strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return _SETTINGS_MALFORMED
    if not isinstance(data, dict):
        return _SETTINGS_MALFORMED
    hooks = data.get("hooks")
    entries = hooks.get(event, []) if isinstance(hooks, dict) else []
    if not isinstance(entries, list):
        entries = []
    for entry in entries:
        if _is_shipit_hook(entry, marker):
            return _canonical_hook_entry(entry)
    return None


def splice_settings_hook(
    text: str,
    inner: str,
    event: str = EVENT_PRETOOLUSE,
    marker: str = SETTINGS_HOOK_MARKER,
) -> str:
    """Merge shipit's ``event`` entry (``inner``, canonical JSON) into a settings.json.

    Owns ONLY shipit's entry in the ``event`` array: any prior shipit entry there
    (matched by ``marker``) is replaced, every other key and hook the consumer set —
    including shipit's entries in other event arrays — is preserved, and the file is
    returned as pretty-printed JSON. An empty/whitespace input starts from ``{}``.

    Fail-safe, matching :func:`extract_settings_hook`: a consumer file that is
    unparseable or is not a JSON object is NEVER clobbered — the original ``text`` is
    returned verbatim (the read path already classified it as an OVERRIDE conflict, so
    the install surfaces it for a human instead of overwriting or crashing).
    """
    stripped = text.strip()
    if stripped:
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            return text  # malformed → preserve, never clobber (conflict surfaced)
        if not isinstance(data, dict):
            return text  # not a JSON object → preserve, never clobber
    else:
        data = {}
    entry = json.loads(inner)
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        hooks = data["hooks"] = {}
    current = hooks.get(event, [])
    if not isinstance(current, list):
        current = []
    hooks[event] = [e for e in current if not _is_shipit_hook(e, marker)] + [entry]
    return json.dumps(data, indent=2) + "\n"


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
    the reconciled set, i.e. the lint-check config is (now) in place — so its hooks
    belong live. The actual ``lefthook install`` is the bounded side effect
    :func:`_activate_hooks` performs; the plan only records that it WILL happen.
    Because activation is idempotent, we run it on every WRITING install that
    manages the caller (ADD or UPDATE), not only the first ADD. A pure no-op
    re-run returns early in :func:`run` before activation, so it never re-touches
    already-current hooks.
    """
    return any(d.unit.key == LEFTHOOK_FILE for d in decisions)


# --------------------------------------------------------------------------
# Retired files (docs/prd/rvw01-sole-requester.md, ADR-0031)
# --------------------------------------------------------------------------
#
# Files shipit used to distribute (or release-sync-era debris) are removed
# portfolio-wide by the same mechanism that installs files — onboarding a repo
# IS the cleanup. The packaged manifest lists each retired path with the set of
# known pristine content hashes; the pure core below maps (actual hash, known
# hashes) to delete / warn-and-keep / no-op; the thin IO pass in :func:`run`
# applies the decisions and reports them alongside the managed-file results.


@dataclass(frozen=True)
class RetiredFile:
    """One retired path with every known pristine version's ``sha256:`` hash."""

    path: str  # path relative to the consumer root
    pristine_hashes: tuple[str, ...]


@dataclass(frozen=True)
class RetiredDecision:
    retired: RetiredFile
    action: str  # DELETE | KEEP | NOOP
    actual_hash: str | None


def load_retired() -> list[RetiredFile]:
    """The packaged retired-files manifest, in manifest order."""
    data = tomllib.loads(_data_bytes(RETIRED_MANIFEST).decode("utf-8"))
    return [
        RetiredFile(path=str(e["path"]), pristine_hashes=tuple(e["pristine"]))
        for e in data.get("retired", [])
    ]


def decide_retired(*, actual_hash: str | None, pristine_hashes: tuple[str, ...]) -> str:
    """The retired-files outcome for one path — the whole algorithm, three cases.

    ``actual_hash is None`` means the file is absent (the same encoding
    :func:`decide` uses for ``consumer_hash``). A pristine match — ANY of the
    known historical versions — is safe to delete; content differing from every
    known version is a local edit we never destroy (KEEP, warned); absent is done.
    """
    if actual_hash is None:
        return NOOP
    if actual_hash in pristine_hashes:
        return DELETE
    return KEEP


def plan_retired(
    retired: list[RetiredFile], actual_hashes: dict[str, str | None]
) -> list[RetiredDecision]:
    """Decide every retired path against the consumer's actual content hashes."""
    decisions: list[RetiredDecision] = []
    for r in retired:
        actual = actual_hashes.get(r.path)
        decisions.append(
            RetiredDecision(
                retired=r,
                action=decide_retired(
                    actual_hash=actual, pristine_hashes=r.pristine_hashes
                ),
                actual_hash=actual,
            )
        )
    return decisions


def retired_actual_hash(root: Path, retired: RetiredFile) -> str | None:
    """The hash of a retired path's current content, or ``None`` if absent."""
    dest = root / retired.path
    if not dest.is_file():
        return None
    return config.content_hash(dest.read_bytes())


# --------------------------------------------------------------------------
# Consumer-state I/O
# --------------------------------------------------------------------------


def _consumer_inner(root: Path, unit: Unit) -> str | None:
    """A block unit's current inner text in the consumer, or ``None``."""
    dest = root / unit.dest
    if not dest.is_file():
        return None
    text = dest.read_text(encoding="utf-8")
    if unit.fmt == FMT_JSON_HOOK:
        return extract_settings_hook(text, unit.event, unit.marker)
    return extract_block(text, unit.open_marker, unit.close_marker)


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
        if unit.fmt == FMT_JSON_HOOK:
            spliced = splice_settings_hook(
                existing, unit.desired_inner(), unit.event, unit.marker
            )
        else:
            spliced = splice_block(
                existing,
                unit.desired_inner(),
                unit.open_marker,
                unit.close_marker,
                unit.anchor,
            )
        dest.write_text(spliced, encoding="utf-8")
        return
    dest.write_bytes(unit.content)
    if unit.executable:
        dest.chmod(0o755)


#: The activation Exec's stated timeout, in seconds (ADR-0028: every Exec
#: states its bound deliberately — never the runner's implicit default).
#: ``lefthook install`` writes a handful of ``.git/hooks`` files locally —
#: git's local tier, not the runner's 5-minute default; a wedged activation
#: dies at this bound as a timeout-cause :class:`~shipit.execrun.ExecError`,
#: which the caller already renders as its move-on warning.
HOOK_ACTIVATE_TIMEOUT: float = 60.0


def _activate_hooks(root: Path) -> execrun.ExecResult:
    """Run ``lefthook install`` in ``root`` — the bounded side effect that turns
    the ``lefthook.yml`` config into live ``.git/hooks``.

    This is the same invocation the ``install-hooks`` pixi task wraps, so the
    checks have one activation definition. It goes through the one Exec runner
    (ADR-0028): ``check=False`` because a nonzero rc is an outcome the caller
    *warns* about (activation is opportunistic setup, never a hard-fail check);
    a launch failure — ``lefthook`` missing from PATH or not executable, or a
    hang killed at the stated :data:`HOOK_ACTIVATE_TIMEOUT` — surfaces as the
    runner's :class:`~shipit.execrun.ExecError`, which the caller likewise
    renders as a warning and moves on.
    """
    return execrun.run(
        [LEFTHOOK_BINARY, *HOOK_ACTIVATE_ARGV],
        cwd=str(root),
        check=False,
        timeout=HOOK_ACTIVATE_TIMEOUT,
    )


def _activation_output(result: execrun.ExecResult) -> str:
    """Both streams of an activation run, joined for the caller's warning.

    Joined with a newline so a stdout without a trailing newline does not run
    straight into stderr (e.g. ``donefatal: ...``) in the warning we print.
    """
    return "\n".join(s for s in (result.stdout, result.stderr) if s)


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def _consumer_snapshot(root: Path, unit: Unit) -> str:
    """The consumer's current text for a unit — captured BEFORE any overwrite."""
    if unit.kind == "block":
        inner = _consumer_inner(root, unit)
        if inner == _SETTINGS_MALFORMED:
            # A malformed settings.json has no clean managed region; surface the
            # whole file so the OVERRIDE diff shows the human the real content.
            dest = root / unit.dest
            return (
                dest.read_text(encoding="utf-8", errors="replace")
                if dest.is_file()
                else ""
            )
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


def _pr_body(
    decisions: list[Decision],
    override_before: dict[str, str],
    hooks_activated: bool | None,
    seeded: list[str] | None = None,
    retired: list[RetiredDecision] | None = None,
) -> str:
    """The PR body: what was added/updated, and every override surfaced with its diff.

    ``override_before`` holds each overridden unit's consumer content captured
    BEFORE the branch write, so the diff shows the real divergence (not an empty
    diff against the content shipit just wrote over it).

    ``hooks_activated`` carries the real activation outcome so the body never
    claims a success that did not happen: ``None`` when the set has no checks to
    activate, ``True`` when ``lefthook install`` succeeded where install ran,
    ``False`` when it was skipped/failed (binary missing) and a merger must
    activate the checks themselves.
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
    retire_deletes = [d for d in (retired or []) if d.action == DELETE]
    retire_keeps = [d for d in (retired or []) if d.action == KEEP]
    if retire_deletes:
        lines.append("### Retired files removed")
        lines.append(
            "shipit no longer distributes these files; each matched a known "
            "pristine version, so this PR deletes them:"
        )
        lines += [f"- `{d.retired.path}`" for d in retire_deletes]
        lines.append("")
    if retire_keeps:
        lines.append("### Retired files kept — locally modified")
        lines.append(
            "shipit no longer distributes these files, but their content "
            "differs from every known pristine version, so they were NOT "
            "deleted. Remove them yourself once the local edits are no "
            "longer needed:"
        )
        lines += [f"- `{d.retired.path}`" for d in retire_keeps]
        lines.append("")
    if seeded:
        lines.append("### Policy seeded")
        lines.append(
            "Consumer-owned pr-flow policy in `.shipit.toml` (seed-if-absent — "
            "existing entries are never clobbered, only absent ones are added):"
        )
        lines += [f"- `{s}`" for s in seeded]
        lines.append("")
    if hooks_activated is True:
        lines.append("### Checks activated locally")
        lines.append(
            "`lefthook install` ran where this install was invoked, so its "
            "`.git/hooks/{pre-commit,pre-push}` fire `pixi run lint` there now. "
            "Reviewers/mergers: run `lefthook install` on your own checkout "
            "(shipit-self: `pixi run -e lint install-hooks`) to make the checks live "
            "for you too. Activation is idempotent and leaves unrelated hooks intact."
        )
        lines.append("")
    elif hooks_activated is False:
        lines.append("### Checks configured — local activation skipped")
        lines.append(
            "`lefthook.yml` is in this PR, but `lefthook install` did not run here "
            "(lefthook missing or it errored). After merging, run `lefthook install` "
            "(shipit-self: `pixi run -e lint install-hooks`) to activate the checks. "
            "The config is correct; only local activation was deferred."
        )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _shipit_version() -> str:
    """The shipit commit that wrote the set (its repo HEAD), else the package version.

    The version string is a rendered artifact, so the typed :class:`Sha`
    :func:`shipit.git.head_commit` returns stringifies here, at the seam.
    """
    pkg_dir = Path(__file__).resolve().parents[1]
    head = git.head_commit(cwd=str(pkg_dir))
    return str(head) if head is not None else __version__


# --------------------------------------------------------------------------
# Orchestrator
# --------------------------------------------------------------------------


def run(
    path: str | None,
    *,
    dry_run: bool = False,
    pr: bool = False,
    push: bool = False,
    local: bool = False,
    activate_hooks: Callable[[Path], execrun.ExecResult] | None = None,
) -> int:
    """Install/reconcile the managed set into the consumer at ``path``.

    Four write modes, in order of precedence:

      - ``local``  — commit the managed set on the CURRENT branch and stop: no
        branch switch, no push, no PR. This is the Tree-provisioning mode
        (``tree create``): the Tree is already on its planned holding branch and
        provisioning only needs the managed files committed there, never an origin
        side effect (no ``shipit/install`` branch, no draft PR). See #170.
      - ``push``   — break-glass: commit on the current branch and push straight
        to it (admin bypass), no PR.
      - ``pr``     — switch to the ``shipit/install`` branch, commit, force-push,
        and open a DRAFT PR (the standalone consumer-onboarding/reconcile flow).
      - default    — refresh the managed set IN THE WORKING TREE and stop: no
        commit, no branch, no push, no PR. Committing the refresh is the
        caller's job — mid-workstream the refreshed files belong in the
        caller's own commit/PR, never in a parallel PR racing it to main (#359).

    ``activate_hooks`` injects the lefthook boundary so tests exercise the
    activation contract without mutating a real ``.git/hooks`` (mirrors how
    :func:`shipit.verbs.lint.run` injects ``run_tool``).
    """
    activate = activate_hooks or _activate_hooks
    started = time.monotonic()
    root = Path(path or ".").resolve()
    if not root.is_dir():
        print(f"install: {root} is not a directory", file=sys.stderr)
        logger.error("install target is not a directory", extra={"root": str(root)})
        return 1

    units = load_units()
    consumer_hashes = {u.key: consumer_hash(root, u) for u in units}

    cfg_path = root / config.CONFIG_NAME
    pristine: dict[str, str] = {}
    # Seed-if-absent consumer policy (the App `[secrets]` mappings + the
    # `[reviewers]` set) is CONSUMER-OWNED, not the hash-managed slow set: it is
    # planned/applied alongside the manifest but never under the pristine-hash
    # reconciliation (architecture.lex §6, issue #25).
    seed_plan: list[str] = []
    try:
        if cfg_path.is_file():
            pristine = config.load_managed(config.load(cfg_path))
        seed_plan = config.plan_policy_seed(cfg_path)
    except config.ConfigError as exc:
        print(f"install: ignoring unreadable manifest: {exc}", file=sys.stderr)
        # Degraded-but-continuing: the reconcile proceeds against an empty
        # pristine map, so consumer edits will surface as OVERRIDEs.
        logger.warning(
            "ignoring unreadable manifest",
            exc_info=True,
            extra={"root": str(root), "manifest": str(cfg_path)},
        )

    decisions = plan(units, consumer_hashes, pristine)
    # ADD/UPDATE/OVERRIDE all write onto the branch; only NOOP writes nothing.
    writes = [d for d in decisions if d.action in (ADD, UPDATE, OVERRIDE)]
    overrides = [d for d in decisions if d.action == OVERRIDE]

    # The retired-files pass: paths shipit used to distribute that must no
    # longer exist. Decided from the packaged manifest against the consumer's
    # actual content, applied (deletes only) alongside the managed writes.
    retired = load_retired()
    retired_decisions = plan_retired(
        retired, {r.path: retired_actual_hash(root, r) for r in retired}
    )
    retire_deletes = [d for d in retired_decisions if d.action == DELETE]
    retire_keeps = [d for d in retired_decisions if d.action == KEEP]

    # The reconcile plan is mechanics: the decided counts, before any write.
    logger.debug(
        "reconcile plan decided",
        extra={
            "root": str(root),
            "adds": sum(1 for d in decisions if d.action == ADD),
            "updates": sum(1 for d in decisions if d.action == UPDATE),
            "overrides": len(overrides),
            "noops": sum(1 for d in decisions if d.action == NOOP),
            "seeds": len(seed_plan),
            "retire_deletes": len(retire_deletes),
            "retire_keeps": len(retire_keeps),
            "dry_run": dry_run,
        },
    )

    print(f"install: {root}{' (dry-run)' if dry_run else ''}")
    for d in decisions:
        if d.action != NOOP:
            print(f"  {d.action:8} {d.unit.dest}")
    for item in seed_plan:
        print(f"  {'seed':8} {item}")
    # Retired-file outcomes, alongside the managed results: a pristine copy is
    # deleted, a locally modified copy is kept LOUDLY (never destroy local
    # edits), an absent path stays silent like any managed NOOP.
    for d in retire_deletes:
        print(f"  {DELETE:8} {d.retired.path} (retired)")
    for d in retire_keeps:
        print(f"  {KEEP:8} {d.retired.path} (retired; locally modified)")
        print(
            f"install: retired file kept: {d.retired.path} differs from every "
            f"known pristine version, so it was NOT deleted — shipit no longer "
            f"distributes this file; remove it yourself once your local edits "
            f"are no longer needed",
            file=sys.stderr,
        )
        logger.warning(
            "retired file kept — locally modified",
            extra={"root": str(root), "path": d.retired.path},
        )
    # A seed-only change (managed set current, policy missing) still counts as a
    # write, so a re-install picks up policy a consumer never had — but stays a
    # no-op once the policy is in place. A pending retired delete likewise keeps
    # the run a write, so cleanup lands even when the managed set is current.
    if not writes and not seed_plan and not retire_deletes:
        print("  nothing to do — managed set is current.")
        logger.debug(
            "managed set is current — nothing to do", extra={"root": str(root)}
        )
        return 0

    if dry_run:
        # Dry-run must have NO side effects: no writes, no deletes, no git, no PR.
        print(
            f"  ({len(writes)} to write, {len(overrides)} override(s), "
            f"{len(seed_plan)} policy seed(s), {len(retire_deletes)} retired "
            f"delete(s)) — dry-run, nothing written"
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
    # Apply the retired deletes: each decided DELETE re-verified nothing — the
    # decision already proved the content is a known pristine version, so the
    # unlink is the whole IO. KEEPs touch nothing (warned above).
    for d in retire_deletes:
        (root / d.retired.path).unlink()
    # Seed the consumer-owned policy BEFORE the manifest write, which preserves
    # `[secrets]`/`[reviewers]` textually while it re-stamps `[shipit]`/`[managed]`.
    if seed_plan:
        config.apply_policy_seed(cfg_path)
    new_managed = {d.unit.key: d.desired_hash for d in decisions}
    config.write_manifest(cfg_path, version=_shipit_version(), managed=new_managed)
    # The reconcile milestone: the managed set (and manifest) is on disk. The
    # writes above are the action whose only record was the per-unit print.
    logger.info(
        "managed set written",
        extra={
            "root": str(root),
            "adds": sum(1 for d in writes if d.action == ADD),
            "updates": sum(1 for d in writes if d.action == UPDATE),
            "overrides": len(overrides),
            "seeds": len(seed_plan),
            "retire_deletes": len(retire_deletes),
            "retire_keeps": len(retire_keeps),
        },
    )

    # Turn the checks on: with lefthook.yml on disk, activate the local hooks so
    # `pixi run lint` fires at commit time — the checks ship LIVE, not dormant.
    # Opportunistic, so a missing lefthook warns rather than aborting the PR.
    hooks_activated: bool | None = None
    # Only (re)activate when this install actually writes a managed unit; a
    # seed-only change touches just `.shipit.toml` and leaves the live hooks alone.
    if writes and activates_hooks(decisions):
        try:
            activation = activate(root)
        except execrun.ExecError as exc:
            # A transport failure from the runner. The common case is a missing
            # or unlaunchable binary; branch on the cause so a timeout or other
            # OS error is not mislabelled as "install lefthook". `lefthook
            # install` is the canonical activation in BOTH repos (a consumer's
            # pixi.toml has no install-hooks task), so that is the recovery we
            # point the missing-binary case at.
            hooks_activated = False
            if exc.cause == execrun.CAUSE_MISSING_BINARY:
                detail = (
                    f"{LEFTHOOK_BINARY}: not found on PATH — ensure lefthook is "
                    f"installed and on PATH, then `lefthook install` to activate "
                    f"the checks"
                )
            else:
                detail = (
                    f"{LEFTHOOK_BINARY}: could not run ({exc}) — resolve the "
                    f"failure above, then `lefthook install` to activate the checks"
                )
        else:
            hooks_activated = activation.ok
            detail = _activation_output(activation)
        if hooks_activated:
            print("  activated git hooks (lefthook install) — the checks are live")
            logger.info(
                "git hooks activated",
                extra={"root": str(root), "duration_ms": activation.duration_ms},
            )
        else:
            print(
                f"install: could not activate git hooks: {detail.strip()}",
                file=sys.stderr,
            )
            # Degraded-but-continuing: the config shipped, only local activation
            # was deferred — the PR body tells the merger to activate.
            logger.warning(
                "could not activate git hooks: %s",
                detail.strip(),
                extra={"root": str(root)},
            )

    # Deleted retired paths join the commit set: `git add` on a removed path
    # stages the deletion, so every commit mode carries the cleanup.
    changed_paths = sorted(
        {d.unit.dest for d in writes}
        | {config.CONFIG_NAME}
        | {d.retired.path for d in retire_deletes}
    )
    cwd = str(root)

    if not (local or push or pr):
        # Default: working-tree refresh ONLY (#359). The managed set and the
        # manifest are on disk, uncommitted — `git diff` is the review surface,
        # and the caller folds the refresh into their own commit/PR. Zero git/gh
        # side effects: no commit, no branch, no push, no PR.
        print(
            "  refreshed the managed set in the working tree — review with "
            "`git diff` and commit it with your own work (use --pr for the "
            "standalone reconcile draft PR)"
        )
        if overrides:
            names = ", ".join(sorted(d.unit.dest for d in overrides))
            print(
                f"install: {len(overrides)} consumer-edited unit(s) overwritten "
                f"with shipit's content in the working tree: {names} — review "
                f"`git diff` before committing (recover yours from git history "
                f"if the edit was committed)",
                file=sys.stderr,
            )
        logger.info(
            "install refreshed working tree",
            extra={
                "root": str(root),
                "mode": "tree",
                "writes": len(writes),
                "overrides": len(overrides),
                "duration_ms": int((time.monotonic() - started) * 1000),
            },
        )
        return 0

    try:
        if local:
            # Local-only (Tree provisioning, #170): commit the managed set on the
            # current branch and stop — NO branch switch, NO push, NO PR. The Tree
            # is already on its planned holding branch, so provisioning lands the
            # managed files there with zero origin side effects.
            branch = git.current_branch(cwd=cwd)
            if branch is None:
                print("install: --local needs a checked-out branch", file=sys.stderr)
                return 1
            git.add(changed_paths, cwd=cwd)
            git.commit(COMMIT_MESSAGE, changed_paths, cwd=cwd)
            print(f"  committed to {branch} (local-only --local)")
            logger.info(
                "install committed locally",
                extra={
                    "root": str(root),
                    "branch": branch,
                    "mode": "local",
                    "duration_ms": int((time.monotonic() - started) * 1000),
                },
            )
            return 0

        if push:
            # Break-glass: commit on the current branch and push straight to it
            # (relies on the repo's admin bypass). Reserved for bootstrapping a
            # repo that cannot yet run the PR loop.
            branch = git.current_branch(cwd=cwd)
            if branch is None:
                print("install: --push needs a checked-out branch", file=sys.stderr)
                return 1
            git.add(changed_paths, cwd=cwd)
            git.commit(COMMIT_MESSAGE, changed_paths, cwd=cwd)
            git.push(branch, cwd=cwd)
            print(f"  pushed to {branch} (break-glass --push)")
            logger.info(
                "install pushed break-glass",
                extra={
                    "root": str(root),
                    "branch": branch,
                    "mode": "push",
                    "duration_ms": int((time.monotonic() - started) * 1000),
                },
            )
            return 0

        # --pr: stage onto the install branch, push it, open a DRAFT PR — the
        # standalone consumer-onboarding/reconcile flow, explicit opt-in only.
        git.switch_create(INSTALL_BRANCH, cwd=cwd)
        git.add(changed_paths, cwd=cwd)
        git.commit(COMMIT_MESSAGE, changed_paths, cwd=cwd)
        # The install branch is regenerated from HEAD each run; force so a re-run
        # with an open install PR updates it rather than failing non-fast-forward.
        git.push(INSTALL_BRANCH, cwd=cwd, force=True)
        existing = gh.pr_url_for_head(INSTALL_BRANCH, cwd=cwd)
        if existing:
            # The force-push already refreshed the open PR's diff.
            print(f"  updated draft PR: {existing}")
            logger.info(
                "install draft PR updated",
                extra={
                    "root": str(root),
                    "branch": INSTALL_BRANCH,
                    "url": existing,
                    "duration_ms": int((time.monotonic() - started) * 1000),
                },
            )
            return 0
        url = gh.pr_create(
            head=INSTALL_BRANCH,
            title="shipit: install/update the managed set",
            body=_pr_body(
                decisions,
                override_before,
                hooks_activated,
                seed_plan,
                retired_decisions,
            ),
            draft=True,
            cwd=cwd,
        )
        print(f"  opened draft PR: {url}")
        logger.info(
            "install draft PR opened",
            extra={
                "root": str(root),
                "branch": INSTALL_BRANCH,
                "url": url,
                "duration_ms": int((time.monotonic() - started) * 1000),
            },
        )
        return 0
    except execrun.ExecError as exc:
        # Match gh_setup: a boundary failure (no remote, auth, not a repo) is a
        # clean CLI error + non-zero exit, not a raw traceback. The failure
        # propagates (non-zero exit), so it is recorded at ERROR with the
        # exception attached.
        print(f"install: git/gh step failed: {exc}", file=sys.stderr)
        logger.error(
            "install git/gh step failed", exc_info=True, extra={"root": str(root)}
        )
        return 1
