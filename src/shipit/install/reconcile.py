"""The reconcile decision core — hash compares in, one frozen :class:`Plan` out.

Per unit: absent -> ADD, hash == desired -> NOOP, hash == stored pristine ->
UPDATE, else OVERRIDE. `gather` is the only read boundary; all writes live in
:mod:`shipit.install.apply`.
"""

from __future__ import annotations

import logging
import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath

import yaml

from .. import config, git
from ..changelog import CHANGELOG_FILE, sync_diff
from .errors import InstallError
from .splice import (
    count_retired_hooks,
    extract_block,
    extract_env_member,
    extract_settings_hook,
    splice_block,
)
from .units import (
    AGENTS_SKILLS_DIR,
    CLAUDE_SKILLS_DIR,
    CLAUDE_SKILLS_LINK_TARGET,
    FMT_ENV_MEMBER,
    FMT_JSON_HOOK,
    FMT_MARKERS,
    LEFTHOOK_FILE,
    PIXI_FILE,
    TOOLCHAIN_GO,
    TOOLCHAIN_NODE,
    TOOLCHAIN_PYTHON,
    TOOLCHAIN_RUST,
    Unit,
    data_bytes,
)

logger = logging.getLogger("shipit.install")

ADD = "add"
NOOP = "noop"
UPDATE = "update"
OVERRIDE = "override"

DELETE = "delete"
KEEP = "keep"

RETIRED_MANIFEST = "retired-files.toml"


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
    """ADD / NOOP / UPDATE / OVERRIDE for one unit; a ``None`` consumer hash means absent."""
    if consumer_hash is None:
        return ADD
    if consumer_hash == desired_hash:
        return NOOP
    if pristine_hash is not None and consumer_hash == pristine_hash:
        return UPDATE
    return OVERRIDE


def plan(
    units: Sequence[Unit],
    consumer_hashes: Mapping[str, str | None],
    pristine: Mapping[str, str],
) -> list[Decision]:
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


def activates_hooks(decisions: Sequence[Decision]) -> bool:
    """True when ``lefthook.yml`` is in the reconciled set; apply performs the activation."""
    return any(d.unit.key == LEFTHOOK_FILE for d in decisions)


@dataclass(frozen=True)
class RetiredFile:
    path: str
    pristine_hashes: tuple[str, ...]


@dataclass(frozen=True)
class RetiredDecision:
    retired: RetiredFile
    action: str
    actual_hash: str | None


def _retired_path(raw: str) -> str:
    """One manifest path, validated plain-relative; raises ``ValueError`` on absolute or traversing."""
    posix = PurePosixPath(raw)
    win = PureWindowsPath(raw)
    if (
        not raw
        or posix.is_absolute()
        or ".." in posix.parts
        or win.drive
        or win.root
        or ".." in win.parts
    ):
        raise ValueError(f"retired-files manifest: unsafe path {raw!r}")
    return raw


def load_retired() -> list[RetiredFile]:
    data = tomllib.loads(data_bytes(RETIRED_MANIFEST).decode("utf-8"))
    return [
        RetiredFile(
            path=_retired_path(str(e["path"])),
            pristine_hashes=tuple(e["pristine"]),
        )
        for e in data.get("retired", [])
    ]


def decide_retired(*, actual_hash: str | None, pristine_hashes: tuple[str, ...]) -> str:
    """NOOP / DELETE / KEEP for one retired path; ``actual_hash is None`` means absent."""
    if actual_hash is None:
        return NOOP
    if actual_hash in pristine_hashes:
        return DELETE
    return KEEP


def plan_retired(
    retired: Sequence[RetiredFile], actual_hashes: Mapping[str, str | None]
) -> list[RetiredDecision]:
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
    """``None`` when absent, ``"symlink"`` for a link — which matches no pristine hash, so it is kept."""
    dest = root / retired.path
    if dest.is_symlink():
        return "symlink"
    if not dest.is_file():
        return None
    return config.content_hash(dest.read_bytes())


@dataclass(frozen=True)
class RetiredHook:
    """One retired consumer-local hook entry, identified by a command substring."""

    file: str
    event: str
    marker: str

    @property
    def key(self) -> str:
        return f"{self.file}#{self.event}[{self.marker}]"


@dataclass(frozen=True)
class RetiredHookDecision:
    retired: RetiredHook
    action: str
    count: int


def load_retired_hooks() -> list[RetiredHook]:
    data = tomllib.loads(data_bytes(RETIRED_MANIFEST).decode("utf-8"))
    return [
        RetiredHook(
            file=_retired_path(str(e["file"])),
            event=str(e["event"]),
            marker=str(e["marker"]),
        )
        for e in data.get("retired_hooks", [])
    ]


def decide_retired_hook(*, count: int) -> str:
    """DELETE when any entry matches, else NOOP — deliberately no KEEP case."""
    return DELETE if count else NOOP


def plan_retired_hooks(
    retired_hooks: Sequence[RetiredHook], counts: Mapping[str, int]
) -> list[RetiredHookDecision]:
    return [
        RetiredHookDecision(
            retired=rh,
            action=decide_retired_hook(count=counts.get(rh.key, 0)),
            count=counts.get(rh.key, 0),
        )
        for rh in retired_hooks
    ]


def retired_hook_count(root: Path, hook: RetiredHook) -> int:
    """How many consumer-local entries ``hook`` matches — 0 when absent, unreadable, or malformed."""
    dest = root / hook.file
    if not dest.is_file():
        return 0
    try:
        text = dest.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        logger.warning(
            "ignoring unreadable hooks file in the retired-hooks pass",
            exc_info=True,
            extra={"root": str(root), "file": hook.file},
        )
        return 0
    return count_retired_hooks(text, hook.event, hook.marker)


EXCLUSIVE_HOOK_OPTIONS = ("piped", "parallel")

LEFTHOOK_LOCAL_FILES = ("lefthook-local.yml", "lefthook-local.yaml")


@dataclass(frozen=True)
class LefthookConflict:
    """One hook whose merged managed+local config lefthook would refuse."""

    hook: str
    local_path: str
    managed_options: tuple[str, ...]
    local_options: tuple[str, ...]


def format_lefthook_conflict(conflict: LefthookConflict) -> str:

    def named(options: tuple[str, ...]) -> str:
        return " and ".join(f"'{o}: true'" for o in options)

    head = (
        f"the managed {LEFTHOOK_FILE} sets {named(conflict.managed_options)} on "
        f"the '{conflict.hook}' hook"
    )
    if conflict.local_options:
        head += f" and this repo's {conflict.local_path} sets {named(conflict.local_options)}"
    tail = (
        f" — lefthook refuses a merged hook with both 'piped' and 'parallel' "
        f"true and crashes BEFORE running any check, blocking every git "
        f"operation that fires '{conflict.hook}'. "
    )
    if conflict.local_options:
        fix = (
            f"Remove the option from {conflict.local_path} (the managed "
            f"{LEFTHOOK_FILE} is regenerated by `shipit install` — never edit "
            f"it), then re-run."
        )
    else:
        fix = (
            f"This is a managed-config defect — re-run `shipit install` to "
            f"regenerate {LEFTHOOK_FILE} (never edit it by hand)."
        )
    return head + tail + fix


def detect_lefthook_conflicts(
    managed_text: str, local_text: str, local_path: str
) -> tuple[LefthookConflict, ...]:
    """The piped/parallel conflicts in the MERGED config; a local value overrides the managed one."""
    try:
        managed = yaml.safe_load(managed_text)
        local = yaml.safe_load(local_text)
    except yaml.YAMLError:
        return ()
    if not isinstance(managed, dict) or not isinstance(local, dict):
        return ()
    conflicts: list[LefthookConflict] = []
    for hook, managed_hook in managed.items():
        local_hook = local.get(hook)
        if not isinstance(managed_hook, dict) or not isinstance(local_hook, dict):
            continue
        local_set = tuple(
            o for o in EXCLUSIVE_HOOK_OPTIONS if local_hook.get(o) is True
        )
        if len(local_set) == len(EXCLUSIVE_HOOK_OPTIONS):
            continue
        managed_set = tuple(
            o for o in EXCLUSIVE_HOOK_OPTIONS if managed_hook.get(o) is True
        )
        if not managed_set:
            continue
        merged = {
            o: local_hook.get(o, managed_hook.get(o)) for o in EXCLUSIVE_HOOK_OPTIONS
        }
        if all(merged[o] is True for o in EXCLUSIVE_HOOK_OPTIONS):
            conflicts.append(
                LefthookConflict(
                    hook=str(hook),
                    local_path=local_path,
                    managed_options=managed_set,
                    local_options=local_set,
                )
            )
    return tuple(conflicts)


def _plan_lefthook_conflicts(
    units: Sequence[Unit], state: ConsumerState
) -> tuple[LefthookConflict, ...]:
    if state.lefthook_local is None or state.lefthook_local_path is None:
        return ()
    unit = next((u for u in units if u.key == LEFTHOOK_FILE), None)
    if unit is None:
        return ()
    return detect_lefthook_conflicts(
        unit.content.decode("utf-8"), state.lefthook_local, state.lefthook_local_path
    )


TOOLCHAIN_MANIFESTS = (
    ("Cargo.toml", TOOLCHAIN_RUST),
    ("go.mod", TOOLCHAIN_GO),
    ("package.json", TOOLCHAIN_NODE),
    ("pyproject.toml", TOOLCHAIN_PYTHON),
)


def detect_toolchains(root: Path) -> frozenset[str]:
    """Toolchain signals off the consumer's TRACKED manifests; root-level existence off git."""
    pathspecs = [
        spec for name, _ in TOOLCHAIN_MANIFESTS for spec in (name, f"*/{name}")
    ]
    tracked = git.ls_files_matching(pathspecs, cwd=str(root))
    if tracked is not None:
        names = {PurePosixPath(p).name for p in tracked}
    else:
        names = {name for name, _ in TOOLCHAIN_MANIFESTS if (root / name).is_file()}
    detected = frozenset(tc for name, tc in TOOLCHAIN_MANIFESTS if name in names)
    if detected:
        logger.debug(
            "toolchain signals detected",
            extra={"root": str(root), "toolchains": ", ".join(sorted(detected))},
        )
    return detected


@dataclass(frozen=True)
class PixiKeyConflict:
    """One pixi block unit whose FIRST splice would duplicate consumer-owned keys in ``anchor``."""

    unit_key: str
    anchor: str
    keys: tuple[str, ...]


def format_pixi_key_conflict(conflict: PixiKeyConflict) -> str:
    keys = " and ".join(f"'{k}'" for k in conflict.keys)
    return (
        f"this repo's pixi.toml already declares {keys} in {conflict.anchor}, "
        f"which the managed block '{conflict.unit_key}' also declares — splicing "
        f"it would duplicate the key(s) and make pixi.toml unparseable, so the "
        f"block CANNOT be delivered and this repo would keep its own declaration "
        f"instead of the managed one. Remedy — pick one: (1) delete this repo's "
        f"own entry and re-run "
        f"`shipit install` to adopt the managed one (usually right: these are "
        f"typically hand-rolled entries shipit's managed set has since taken "
        f"over); or (2) to "
        f"keep this repo's own entry, declare the override in .shipit.toml as "
        f"TWO lines — a '[managed.decline]' header on a line of its own, then "
        f'below it the assignment keep = ["{conflict.unit_key}"] (the header '
        f"spelling is required: a dotted decline.keep under [managed] is "
        f"refused, it would not survive install's re-stamp)."
    )


def _rewritten_pixi_blocks(
    units: Sequence[Unit], state: ConsumerState, kept: frozenset[str]
) -> tuple[Unit, ...]:
    """The present ``pixi.toml`` marker blocks this reconcile will rewrite; ``kept`` names the ones it will not."""
    return tuple(
        u
        for u in units
        if u.kind == "block"
        and u.dest == PIXI_FILE
        and u.fmt == FMT_MARKERS
        and u.key not in kept
        and state.consumer_hashes.get(u.key) is not None
    )


def _departing_managed_keys(
    text: str, rewritten: Sequence[Unit]
) -> dict[str, frozenset[str]]:
    """Anchor -> the keys a rewritten managed block declares now but will not after the rewrite."""
    by_anchor: dict[str, set[str]] = {}
    for unit in rewritten:
        if unit.anchor is None:
            continue
        inner = extract_block(text, unit.open_marker, unit.close_marker)
        if inner is None:
            continue
        try:
            current = tomllib.loads(inner)
            desired = tomllib.loads(unit.desired_inner())
        except tomllib.TOMLDecodeError:
            continue
        by_anchor.setdefault(unit.anchor, set()).update(
            k for k in current if k not in desired
        )
    return {anchor: frozenset(keys) for anchor, keys in by_anchor.items()}


def _plan_pixi_key_conflicts(
    units: Sequence[Unit], state: ConsumerState, kept: frozenset[str]
) -> tuple[PixiKeyConflict, ...]:
    if state.pixi_text is None:
        return ()
    try:
        manifest = tomllib.loads(state.pixi_text)
    except tomllib.TOMLDecodeError:
        return ()
    managed_keys = _departing_managed_keys(
        state.pixi_text, _rewritten_pixi_blocks(units, state, kept)
    )
    consumer_hashes = state.consumer_hashes
    conflicts: list[PixiKeyConflict] = []
    for unit in units:
        if unit.kind != "block" or unit.dest != PIXI_FILE or unit.anchor is None:
            continue
        if unit.key in kept:
            continue
        if unit.fmt != FMT_MARKERS:
            continue
        if consumer_hashes.get(unit.key) is not None:
            continue
        try:
            block_keys = tomllib.loads(unit.desired_inner())
        except tomllib.TOMLDecodeError:  # pragma: no cover — packaged data
            continue
        table: object = manifest
        for part in _split_toml_key(unit.anchor.strip().strip("[]")):
            table = table.get(part) if isinstance(table, dict) else None
        if not isinstance(table, dict):
            continue
        owned = managed_keys.get(unit.anchor, frozenset())
        clashes = tuple(sorted(k for k in block_keys if k in table and k not in owned))
        if clashes:
            conflicts.append(
                PixiKeyConflict(unit_key=unit.key, anchor=unit.anchor, keys=clashes)
            )
    return tuple(conflicts)


@dataclass(frozen=True)
class PixiTaskConflict:
    """One pixi ``[tasks]`` block unit whose FIRST splice would make ``task`` ambiguous across environments."""

    unit_key: str
    task: str
    features: tuple[str, ...]


def _feature_tasks_header(feature: str) -> str:
    """The ``[feature.<name>.tasks]`` header, quoting and escaping ``feature`` when it is not a bare TOML key."""
    if re.fullmatch(r"[A-Za-z0-9_-]+", feature):
        return f"[feature.{feature}.tasks]"
    escaped = feature.replace("\\", "\\\\").replace('"', '\\"')
    return f'[feature."{escaped}".tasks]'


def format_pixi_task_conflict(conflict: PixiTaskConflict) -> str:
    tables = " and ".join(_feature_tasks_header(f) for f in conflict.features)
    return (
        f"this repo's pixi.toml already defines a '{conflict.task}' task in "
        f"{tables}, which the managed block '{conflict.unit_key}' also defines "
        f"in [tasks] — splicing it would make `pixi run {conflict.task}` "
        f"ambiguous (pixi refuses a task defined in several environments), so "
        f"the block was NOT delivered and this repo's own task stays "
        f"authoritative. To adopt the managed caller instead, delete this "
        f"repo's own task and re-run `shipit install`."
    )


def _enabled_features(manifest: Mapping[str, object]) -> frozenset[str]:
    environments = manifest.get("environments")
    if not isinstance(environments, dict):
        return frozenset()
    enabled: set[str] = set()
    for spec in environments.values():
        feats = spec.get("features") if isinstance(spec, dict) else spec
        if isinstance(feats, list):
            enabled.update(str(f) for f in feats)
    return frozenset(enabled)


def _pixi_task_conflicts(
    pixi_text: str | None,
    units: Sequence[Unit],
    consumer_hashes: Mapping[str, str | None],
) -> tuple[PixiTaskConflict, ...]:
    if pixi_text is None:
        return ()
    try:
        manifest = tomllib.loads(pixi_text)
    except tomllib.TOMLDecodeError:
        return ()
    features = manifest.get("feature")
    if not isinstance(features, dict):
        return ()
    enabled = _enabled_features(manifest)
    feature_tasks: dict[str, list[str]] = {}
    for feature, body in features.items():
        if str(feature) not in enabled:
            continue
        tasks = body.get("tasks") if isinstance(body, dict) else None
        if isinstance(tasks, dict):
            for task in tasks:
                feature_tasks.setdefault(str(task), []).append(str(feature))
    if not feature_tasks:
        return ()
    conflicts: list[PixiTaskConflict] = []
    for unit in units:
        if unit.kind != "block" or unit.dest != PIXI_FILE or unit.anchor != "[tasks]":
            continue
        if consumer_hashes.get(unit.key) is not None:
            continue
        try:
            block_tasks = tomllib.loads(unit.desired_inner())
        except tomllib.TOMLDecodeError:  # pragma: no cover — packaged data
            continue
        for task in block_tasks:
            if task in feature_tasks:
                conflicts.append(
                    PixiTaskConflict(
                        unit_key=unit.key,
                        task=str(task),
                        features=tuple(sorted(feature_tasks[task])),
                    )
                )
    return tuple(conflicts)


@dataclass(frozen=True)
class PixiTableConflict:
    """One anchor-less pixi block unit whose FIRST splice would redeclare a consumer-owned table."""

    unit_key: str
    tables: tuple[str, ...]


def format_pixi_table_conflict(conflict: PixiTableConflict) -> str:
    tables = " and ".join(f"[{t}]" for t in conflict.tables)
    return (
        f"this repo's pixi.toml already declares the {tables} table(s), which "
        f"the managed block '{conflict.unit_key}' also declares — splicing it "
        f"would redeclare the table(s) and make pixi.toml unparseable, so the "
        f"block was NOT delivered and this repo's own table stays "
        f"authoritative. To adopt the managed block instead, delete this repo's "
        f"own table(s) and re-run `shipit install`."
    )


def _split_toml_key(key: str) -> tuple[str, ...]:
    """A TOML dotted key-path split into segments; a dot inside quotes is a literal."""
    segments: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    for ch in key:
        if quote is not None:
            if ch == quote:
                quote = None
            else:
                buf.append(ch)
        elif ch in "\"'":
            quote = ch
        elif ch == ".":
            segments.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    segments.append("".join(buf).strip())
    return tuple(segments)


def _toml_table_headers(inner: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Every plain ``[table]`` header as ``(verbatim inner text, quote-aware segments)``."""
    headers: list[tuple[str, tuple[str, ...]]] = []
    for line in inner.splitlines():
        stripped = _strip_toml_comment(line).strip()
        if (
            not stripped.startswith("[")
            or stripped.startswith("[[")
            or not stripped.endswith("]")
        ):
            continue
        raw = stripped[1:-1].strip()
        headers.append((raw, _split_toml_key(raw)))
    return tuple(headers)


def _strip_toml_comment(line: str) -> str:
    """``line`` truncated at the first unquoted ``#``."""
    quote: str | None = None
    for i, ch in enumerate(line):
        if quote is not None:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#":
            return line[:i]
    return line


def _table_declared(manifest: Mapping[str, object], path: tuple[str, ...]) -> bool:
    if not path:
        return False
    table: object = manifest
    for part in path:
        if not isinstance(table, dict) or part not in table:
            return False
        table = table[part]
    return isinstance(table, dict)


def _table_redeclared(
    manifest: Mapping[str, object],
    consumer_headers: frozenset[tuple[str, ...]],
    segs: tuple[str, ...],
) -> bool:
    """Whether appending ``[segs]`` would redeclare a table; a purely implicit super-table is re-openable."""
    if not _table_declared(manifest, segs):
        return False
    if segs in consumer_headers:
        return True
    has_deeper = any(
        h[: len(segs)] == segs and len(h) > len(segs) for h in consumer_headers
    )
    return not has_deeper


def _pixi_table_conflicts(
    pixi_text: str | None,
    units: Sequence[Unit],
    consumer_hashes: Mapping[str, str | None],
) -> tuple[PixiTableConflict, ...]:
    if pixi_text is None:
        return ()
    try:
        manifest = tomllib.loads(pixi_text)
    except tomllib.TOMLDecodeError:
        return ()
    consumer_headers = frozenset(segs for _, segs in _toml_table_headers(pixi_text))
    conflicts: list[PixiTableConflict] = []
    for unit in units:
        if unit.kind != "block" or unit.dest != PIXI_FILE or unit.anchor is not None:
            continue
        if consumer_hashes.get(unit.key) is not None:
            continue
        clashes = tuple(
            raw
            for raw, segments in _toml_table_headers(unit.desired_inner())
            if _table_redeclared(manifest, consumer_headers, segments)
        )
        if clashes:
            conflicts.append(PixiTableConflict(unit_key=unit.key, tables=clashes))
    return tuple(conflicts)


def _changelog_stale(root: Path) -> bool:
    from ..verbs.changelog import render_current

    try:
        rendered = render_current(root)
        if rendered is None:
            return False
        committed_path = root / CHANGELOG_FILE
        committed = (
            committed_path.read_text(encoding="utf-8")
            if committed_path.is_file()
            else None
        )
    except (OSError, UnicodeDecodeError):
        logger.warning(
            "ignoring unreadable CHANGELOG projection — treating as not stale",
            exc_info=True,
            extra={"root": str(root)},
        )
        return False
    return sync_diff(rendered, committed) is not None


def consumer_inner(root: Path, unit: Unit) -> str | None:
    """A block unit's current inner text in the consumer, or ``None`` when absent."""
    dest = root / unit.dest
    if not dest.is_file():
        return None
    text = dest.read_text(encoding="utf-8")
    if unit.fmt == FMT_JSON_HOOK:
        return extract_settings_hook(text, unit.event, unit.marker)
    if unit.fmt == FMT_ENV_MEMBER:
        return extract_env_member(text, unit.env_name or "", unit.required_features)
    return extract_block(text, unit.open_marker, unit.close_marker)


@dataclass(frozen=True)
class SymlinkedDest:
    """A managed unit whose dest crosses a consumer symlink; ``component`` is the shallowest linked element."""

    unit_key: str
    dest: str
    component: str


def symlinked_dest_component(root: Path, dest: str) -> str | None:
    """The shallowest symlinked component of ``dest`` under ``root``, or ``None``."""
    current = root
    for part in Path(dest).parts:
        current = current / part
        if current.is_symlink():
            return str(current.relative_to(root))
    return None


def symlinked_dests(root: Path, units: Sequence[Unit]) -> tuple[SymlinkedDest, ...]:
    found: list[SymlinkedDest] = []
    for u in units:
        component = symlinked_dest_component(root, u.dest)
        if component is not None:
            found.append(
                SymlinkedDest(unit_key=u.key, dest=u.dest, component=component)
            )
    return tuple(found)


def format_symlinked_dest(sd: SymlinkedDest) -> str:
    leaf = "" if sd.component == sd.dest else f" (writing {sd.dest} would follow it)"
    return (
        f"{sd.component} is a symlink{leaf} — shipit refuses to write the managed "
        f"unit {sd.unit_key} through it (it would overwrite the link's target, "
        f"outside this repo). Remove the symlink and re-run `shipit install` to "
        f"receive a real copy"
    )


RETIRED_PROVISION_ARGS = ("provision", "lexd")

_UNJUDGEABLE_CHARS = frozenset("\"'`<>\\")

_COMMAND_SEPARATORS = re.compile(r"[;&|()\n]+")

_ASSIGNMENT_PREFIX_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")


@dataclass(frozen=True)
class StaleProvisionTask:
    """One consumer pixi task still calling the retired ``shipit provision lexd``."""

    task: str
    table: str
    command: str


@dataclass(frozen=True)
class _TaskCommand:
    """One command a pixi task runs; ``argv`` is the word list for a list-form task, ``None`` for a string one."""

    text: str
    argv: tuple[str, ...] | None


def _task_commands(spec: object) -> tuple[_TaskCommand, ...]:
    if isinstance(spec, str):
        return (_TaskCommand(text=spec, argv=None),)
    if isinstance(spec, list):
        argv = tuple(str(x) for x in spec)
        return (_TaskCommand(text=" ".join(argv), argv=argv),)
    if isinstance(spec, dict):
        return _task_commands(spec.get("cmd"))
    return ()


def _runs_retired_provision(words: Sequence[str]) -> bool:
    """Whether ``words`` — ONE simple command — runs the retired call."""
    for index, word in enumerate(words):
        if _ASSIGNMENT_PREFIX_RE.match(word):
            continue
        return (
            PurePosixPath(word).name == "shipit"
            and tuple(words[index + 1 : index + 1 + len(RETIRED_PROVISION_ARGS)])
            == RETIRED_PROVISION_ARGS
        )
    return False


def _shell_command_words(segment: str) -> tuple[str, ...]:
    """One QUOTE-FREE shell segment's words, stopping at an unquoted ``#``."""
    words: list[str] = []
    for word in segment.split():
        if word.startswith("#"):
            break
        words.append(word)
    return tuple(words)


def _calls_retired_provision(command: _TaskCommand) -> bool:
    """Whether ``command`` runs the retired call; a command the reader cannot read exactly is declined (False)."""
    if command.argv is not None:
        return _runs_retired_provision(command.argv)
    if any(ch in _UNJUDGEABLE_CHARS for ch in command.text) or "$(" in command.text:
        return False
    return any(
        _runs_retired_provision(_shell_command_words(segment))
        for segment in _COMMAND_SEPARATORS.split(command.text)
    )


def _tasks_tables(
    node: object, path: tuple[str, ...] = ()
) -> list[tuple[tuple[str, ...], dict]]:
    if not isinstance(node, dict):
        return []
    found: list[tuple[tuple[str, ...], dict]] = []
    for key, value in node.items():
        if not isinstance(value, dict):
            continue
        if key == "tasks":
            found.append(((*path, "tasks"), value))
        found.extend(_tasks_tables(value, (*path, str(key))))
    return found


def stale_provision_tasks(text: str) -> tuple[StaleProvisionTask, ...]:
    try:
        manifest = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return ()
    found: list[StaleProvisionTask] = []
    for table, tasks in _tasks_tables(manifest):
        for task, spec in tasks.items():
            for command in _task_commands(spec):
                if _calls_retired_provision(command):
                    found.append(
                        StaleProvisionTask(
                            task=str(task),
                            table=".".join(table),
                            command=command.text,
                        )
                    )
    return tuple(found)


def project_pixi_text(text: str, rewritten: Sequence[Unit]) -> str:
    """``text`` with every rewritten managed block replaced by its desired inner."""
    for unit in rewritten:
        text = splice_block(
            text, unit.desired_inner(), unit.open_marker, unit.close_marker
        )
    return text


def _plan_stale_provision(
    units: Sequence[Unit], state: ConsumerState, kept: frozenset[str]
) -> tuple[StaleProvisionTask, ...]:
    """Retired ``provision lexd`` calls that SURVIVE this reconcile, judged over the projected manifest."""
    if state.pixi_text is None:
        return ()
    return stale_provision_tasks(
        project_pixi_text(state.pixi_text, _rewritten_pixi_blocks(units, state, kept))
    )


def _read_pixi_text(root: Path) -> str | None:
    path = root / PIXI_FILE
    if not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def format_stale_provision(ref: StaleProvisionTask) -> str:
    return (
        f"pixi.toml: the '{ref.task}' task in [{ref.table}] runs `{ref.command}`, "
        f"which calls the RETIRED `shipit provision lexd` — the `provision` verb "
        f"is retired (ADR-0066, no fallback), so this task is already dead. lexd "
        f"rides the managed [feature.shipit-lexd] block (the public Artifact "
        f"channel) wired into the lint environment, so it is already on PATH "
        f"there: drop the `shipit provision lexd &&` prefix (or the whole "
        f"'{ref.task}' task, if that is all it does) and re-run `shipit install`"
    )


LINK_NOOP = "link-noop"
LINK_CREATE = "link-create"
LINK_BLOCKED = "link-blocked"


@dataclass(frozen=True)
class ClaudeSkillsLink:
    """The ``.claude/skills`` -> ``.agents/skills`` symlink decision; ``reason`` is set on BLOCKED only."""

    action: str
    reason: str = ""

    @property
    def is_work(self) -> bool:
        return self.action == LINK_CREATE


def _claude_skills_exists_reason(link: Path) -> str:
    if link.is_symlink():
        what = f"a symlink to {str(link.readlink())!r}, not the managed {CLAUDE_SKILLS_LINK_TARGET!r}"
    elif link.is_dir():
        what = "a real directory"
    else:
        what = "a regular file"
    return (
        f"{CLAUDE_SKILLS_DIR} already exists ({what}) — shipit will not remove it. "
        f"Remove it (relocate any of your own skills into {AGENTS_SKILLS_DIR} first) "
        f"and re-run `shipit install` to adopt the managed symlink"
    )


def plan_claude_skills_link(root: Path) -> ClaudeSkillsLink:
    parent = symlinked_dest_component(root, str(Path(CLAUDE_SKILLS_DIR).parent))
    if parent is not None:
        return ClaudeSkillsLink(
            LINK_BLOCKED,
            reason=(
                f"a parent of {CLAUDE_SKILLS_DIR} is a symlink ({parent}) — shipit "
                f"will not create or read through it. Remove the symlink and re-run "
                f"to adopt the managed link"
            ),
        )
    link = root / CLAUDE_SKILLS_DIR
    if link.is_symlink():
        if str(link.readlink()) == CLAUDE_SKILLS_LINK_TARGET:
            return ClaudeSkillsLink(LINK_NOOP)
        return ClaudeSkillsLink(LINK_BLOCKED, reason=_claude_skills_exists_reason(link))
    if not link.exists():
        return ClaudeSkillsLink(LINK_CREATE)
    return ClaudeSkillsLink(LINK_BLOCKED, reason=_claude_skills_exists_reason(link))


def format_claude_skills_link(link: ClaudeSkillsLink) -> str:
    if link.action == LINK_CREATE:
        return f"link     {CLAUDE_SKILLS_DIR} -> {CLAUDE_SKILLS_LINK_TARGET}"
    return f"{CLAUDE_SKILLS_DIR}: {link.reason}"


def consumer_hash(root: Path, unit: Unit) -> str | None:
    """The hash of a unit's current content in the consumer, or ``None`` if absent."""
    if unit.kind == "block":
        inner = consumer_inner(root, unit)
        return None if inner is None else config.content_hash(inner.encode("utf-8"))
    dest = root / unit.dest
    if not dest.is_file():
        return None
    return config.content_hash(dest.read_bytes())


@dataclass(frozen=True)
class ConsumerState:
    """What :func:`gather` read off the consumer — the reconcile's only input."""

    root: str
    consumer_hashes: Mapping[str, str | None]
    pristine: Mapping[str, str]
    retired_hashes: Mapping[str, str | None]
    seeds: tuple[str, ...]
    retired_hook_counts: Mapping[str, int] = field(default_factory=dict)
    current_pin: str | None = None
    target_pin: str | None = None
    pixi_manifest_missing: bool = False
    manifest_error: str | None = None
    lefthook_local_path: str | None = None
    lefthook_local: str | None = None
    pixi_text: str | None = None
    pixi_task_conflicts: tuple[PixiTaskConflict, ...] = ()
    pixi_table_conflicts: tuple[PixiTableConflict, ...] = ()
    changelog_stale: bool = False
    declines: tuple[str, ...] = ()
    symlinked_dests: tuple[SymlinkedDest, ...] = ()
    claude_skills_link: ClaudeSkillsLink = field(
        default_factory=lambda: ClaudeSkillsLink(LINK_NOOP)
    )


def gather(
    root: Path,
    units: Sequence[Unit],
    retired: Sequence[RetiredFile],
    retired_hooks: Sequence[RetiredHook] = (),
) -> ConsumerState:
    """Read the consumer's current state — the install domain's ONE read boundary, filesystem only."""
    root = root.resolve()
    if not root.is_dir():
        raise InstallError(f"{root} is not a directory")

    cfg_path = root / config.CONFIG_NAME
    pristine: dict[str, str] = {}
    seeds: list[str] = []
    current_pin: str | None = None
    declines: tuple[str, ...] = ()
    manifest_error: str | None = None
    try:
        if cfg_path.is_file():
            raw = cfg_path.read_text(encoding="utf-8")
            cfg = config.load(cfg_path)
            pristine = config.load_managed(cfg)
            current_pin = config.shipit_version(cfg)
            declines = config.load_declines(cfg, raw)
        seeds = config.plan_policy_seed(
            cfg_path, toolchains=config.derive_toolchains(root)
        )
    except config.ConfigError as exc:
        manifest_error = str(exc)
        logger.warning(
            "ignoring unreadable manifest",
            exc_info=True,
            extra={"root": str(root), "manifest": str(cfg_path)},
        )

    lefthook_local_path, lefthook_local = _read_lefthook_local(root)
    consumer_hashes = {u.key: consumer_hash(root, u) for u in units}
    pixi_text = _read_pixi_text(root)
    return ConsumerState(
        root=str(root),
        consumer_hashes=consumer_hashes,
        pristine=pristine,
        retired_hashes={r.path: retired_actual_hash(root, r) for r in retired},
        seeds=tuple(seeds),
        retired_hook_counts={h.key: retired_hook_count(root, h) for h in retired_hooks},
        current_pin=current_pin,
        target_pin=_target_pin(),
        pixi_manifest_missing=not (root / PIXI_FILE).is_file(),
        manifest_error=manifest_error,
        lefthook_local_path=lefthook_local_path,
        lefthook_local=lefthook_local,
        pixi_text=pixi_text,
        pixi_task_conflicts=_pixi_task_conflicts(pixi_text, units, consumer_hashes),
        pixi_table_conflicts=_pixi_table_conflicts(pixi_text, units, consumer_hashes),
        changelog_stale=_changelog_stale(root),
        declines=declines,
        symlinked_dests=symlinked_dests(root, units),
        claude_skills_link=plan_claude_skills_link(root),
    )


def _read_lefthook_local(root: Path) -> tuple[str | None, str | None]:
    """The consumer's lefthook-local config as ``(filename, text)``; ``(None, None)`` when absent or unreadable."""
    for name in LEFTHOOK_LOCAL_FILES:
        dest = root / name
        if not dest.is_file():
            continue
        try:
            return name, dest.read_text(encoding="utf-8", errors="replace")
        except OSError:
            logger.warning(
                "ignoring unreadable lefthook-local config",
                exc_info=True,
                extra={"root": str(root), "local": name},
            )
            return None, None
    return None, None


def _target_pin() -> str | None:
    """The pin an applying install WOULD stamp — ``None`` if no build identity resolves."""
    from .apply import _shipit_version

    try:
        return _shipit_version()
    except InstallError:
        return None


@dataclass(frozen=True)
class Plan:
    """What install WOULD do — the frozen aggregate :func:`reconcile` returns."""

    root: str
    decisions: tuple[Decision, ...]
    retired: tuple[RetiredDecision, ...]
    seeds: tuple[str, ...]
    retired_hooks: tuple[RetiredHookDecision, ...] = ()
    seed_pixi_manifest: bool = False
    manifest_error: str | None = None
    current_pin: str | None = None
    target_pin: str | None = None
    lefthook_conflicts: tuple[LefthookConflict, ...] = ()
    pixi_key_conflicts: tuple[PixiKeyConflict, ...] = ()
    pixi_task_conflicts: tuple[PixiTaskConflict, ...] = ()
    pixi_table_conflicts: tuple[PixiTableConflict, ...] = ()
    rerender_changelog: bool = False
    declined: tuple[str, ...] = ()
    decline_unmatched: tuple[str, ...] = ()
    symlinked_dests: tuple[SymlinkedDest, ...] = ()
    stale_provision: tuple[StaleProvisionTask, ...] = ()
    claude_skills_link: ClaudeSkillsLink = field(
        default_factory=lambda: ClaudeSkillsLink(LINK_NOOP)
    )

    @property
    def writes(self) -> tuple[Decision, ...]:
        """ADD/UPDATE/OVERRIDE all write shipit's content; only NOOP writes nothing."""
        return tuple(d for d in self.decisions if d.action in (ADD, UPDATE, OVERRIDE))

    @property
    def overrides(self) -> tuple[Decision, ...]:
        return tuple(d for d in self.decisions if d.action == OVERRIDE)

    @property
    def retire_deletes(self) -> tuple[RetiredDecision, ...]:
        return tuple(d for d in self.retired if d.action == DELETE)

    @property
    def retire_keeps(self) -> tuple[RetiredDecision, ...]:
        return tuple(d for d in self.retired if d.action == KEEP)

    @property
    def retire_hook_deletes(self) -> tuple[RetiredHookDecision, ...]:
        return tuple(d for d in self.retired_hooks if d.action == DELETE)

    @property
    def pin_stale(self) -> bool:
        """The consumer's pin differs from the running build's sha — a work axis of its own."""
        return self.target_pin is not None and self.current_pin != self.target_pin

    @property
    def nothing_to_do(self) -> bool:
        """No writes, seeds, retired deletes, pin bump, changelog re-render or skills link — a clean no-op."""
        return (
            not self.writes
            and not self.seeds
            and not self.retire_deletes
            and not self.retire_hook_deletes
            and not self.pin_stale
            and not self.rerender_changelog
            and not self.claude_skills_link.is_work
        )

    @property
    def activates_hooks(self) -> bool:
        return activates_hooks(self.decisions)

    @property
    def changed_paths(self) -> tuple[str, ...]:
        """Every path a writing apply touches — the commit set, manifest included."""
        return tuple(
            sorted(
                {d.unit.dest for d in self.writes}
                | {config.CONFIG_NAME}
                | {d.retired.path for d in self.retire_deletes}
                | {d.retired.file for d in self.retire_hook_deletes}
                | ({CHANGELOG_FILE} if self.rerender_changelog else set())
                | ({CLAUDE_SKILLS_DIR} if self.claude_skills_link.is_work else set())
            )
        )


def reconcile(
    units: Sequence[Unit],
    retired: Sequence[RetiredFile],
    state: ConsumerState,
    retired_hooks: Sequence[RetiredHook] = (),
) -> Plan:
    """Decide the whole install — pure over the gathered :class:`ConsumerState`."""
    decline_set = set(state.declines)
    kept = frozenset(decline_set | {sd.unit_key for sd in state.symlinked_dests})
    pixi_key_conflicts = _plan_pixi_key_conflicts(units, state, kept)
    conflicted = (
        {c.unit_key for c in pixi_key_conflicts}
        | {c.unit_key for c in state.pixi_task_conflicts}
        | {c.unit_key for c in state.pixi_table_conflicts}
        | {sd.unit_key for sd in state.symlinked_dests}
    )
    unit_keys = {u.key for u in units}
    declined = tuple(dict.fromkeys(k for k in state.declines if k in unit_keys))
    decline_unmatched = tuple(
        dict.fromkeys(k for k in state.declines if k not in unit_keys)
    )
    decisions = tuple(
        d
        for d in plan(units, state.consumer_hashes, state.pristine)
        if d.unit.key not in conflicted and d.unit.key not in decline_set
    )
    result = Plan(
        root=state.root,
        decisions=decisions,
        retired=tuple(plan_retired(retired, state.retired_hashes)),
        seeds=state.seeds,
        retired_hooks=tuple(
            plan_retired_hooks(retired_hooks, state.retired_hook_counts)
        ),
        seed_pixi_manifest=state.pixi_manifest_missing
        and any(
            d.unit.dest == PIXI_FILE for d in decisions if d.action in (ADD, UPDATE)
        ),
        manifest_error=state.manifest_error,
        current_pin=state.current_pin,
        target_pin=state.target_pin,
        lefthook_conflicts=_plan_lefthook_conflicts(units, state),
        pixi_key_conflicts=pixi_key_conflicts,
        pixi_task_conflicts=state.pixi_task_conflicts,
        pixi_table_conflicts=state.pixi_table_conflicts,
        rerender_changelog=state.changelog_stale,
        declined=declined,
        decline_unmatched=decline_unmatched,
        symlinked_dests=state.symlinked_dests,
        stale_provision=_plan_stale_provision(units, state, kept),
        claude_skills_link=state.claude_skills_link,
    )
    logger.debug(
        "reconcile plan decided",
        extra={
            "root": state.root,
            "adds": sum(1 for d in result.decisions if d.action == ADD),
            "updates": sum(1 for d in result.decisions if d.action == UPDATE),
            "overrides": len(result.overrides),
            "noops": sum(1 for d in result.decisions if d.action == NOOP),
            "seeds": len(result.seeds),
            "pixi_seed": result.seed_pixi_manifest,
            "retire_deletes": len(result.retire_deletes),
            "retire_keeps": len(result.retire_keeps),
            "retire_hook_deletes": len(result.retire_hook_deletes),
            "pin_stale": result.pin_stale,
            "rerender_changelog": result.rerender_changelog,
            "declined": len(result.declined),
        },
    )
    for key in result.declined:
        logger.info(
            "managed unit declined — kept as the consumer's own "
            "([managed.decline].keep)",
            extra={"root": state.root, "unit": key},
        )
    for key in result.decline_unmatched:
        logger.warning(
            "declined key names no managed unit in this catalog",
            extra={"root": state.root, "unit": key},
        )
    for d in result.retire_keeps:
        logger.warning(
            "retired file kept — locally modified",
            extra={"root": state.root, "path": d.retired.path},
        )
    for c in result.lefthook_conflicts:
        logger.warning(
            "lefthook merge conflict: %s",
            format_lefthook_conflict(c),
            extra={"root": state.root, "hook": c.hook, "local": c.local_path},
        )
    for kc in result.pixi_key_conflicts:
        logger.warning(
            "pixi key conflict: %s",
            format_pixi_key_conflict(kc),
            extra={"root": state.root, "unit": kc.unit_key, "anchor": kc.anchor},
        )
    for tc in result.pixi_task_conflicts:
        logger.warning(
            "pixi task conflict: %s",
            format_pixi_task_conflict(tc),
            extra={"root": state.root, "unit": tc.unit_key, "task": tc.task},
        )
    for bc in result.pixi_table_conflicts:
        logger.warning(
            "pixi table conflict: %s",
            format_pixi_table_conflict(bc),
            extra={"root": state.root, "unit": bc.unit_key, "tables": bc.tables},
        )
    for sd in result.symlinked_dests:
        logger.warning(
            "symlinked dest: %s",
            format_symlinked_dest(sd),
            extra={"root": state.root, "unit": sd.unit_key, "component": sd.component},
        )
    for sp in result.stale_provision:
        logger.warning(
            "retired `provision lexd` reference: %s",
            format_stale_provision(sp),
            extra={"root": state.root, "task": sp.task, "table": sp.table},
        )
    if result.claude_skills_link.action == LINK_BLOCKED:
        logger.warning(
            "claude skills link blocked: %s",
            result.claude_skills_link.reason,
            extra={"root": state.root, "path": CLAUDE_SKILLS_DIR},
        )
    if result.nothing_to_do:
        logger.debug(
            "managed set is current — nothing to do", extra={"root": state.root}
        )
    return result
