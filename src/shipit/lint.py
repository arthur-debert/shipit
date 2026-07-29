"""The standardized multi-language lint checks: registry, routing and execution.

See docs/adr/0037-the-gate-owns-the-config.md.
"""

from __future__ import annotations

import functools
import logging
import os
import re
import sys
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from . import config, execrun, git
from .tree import include

logger = logging.getLogger("shipit.lint")


CONFIG_PLACEHOLDER = "{config}"


def _data_path(name: str) -> str:
    """The path to a canonical-config body shipped under ``shipit/data``; raises ``FileNotFoundError`` if it is missing."""
    path = str(resources.files("shipit.data").joinpath(name))
    if not os.path.isfile(path):
        raise FileNotFoundError(
            f"shipit.data is missing canonical config {name!r} (resolved to "
            f"{path!r}); this file ships with the package — reinstall shipit "
            "or file a bug"
        )
    return path


def data_path(name: str) -> str:
    """Return the filesystem path to a packaged ``shipit.data`` file."""
    return _data_path(name)


_RUSTFMT_CONFIG_PATH = _data_path("rustfmt.toml")


@dataclass(frozen=True)
class Tool:
    """One linter invocation: the binary plus its check args (files appended).

    ``fix`` is the formatter form applied under ``--fix``; ``None`` means the tool runs
    its check form in both modes. ``per_manifest`` tools run once per tracked manifest
    directory, cwd'd there, with no files appended.
    """

    binary: str
    check: tuple[str, ...]
    fix: tuple[str, ...] | None = None
    per_manifest: bool = False
    config_inject: tuple[str, ...] = ()
    editorconfig_pin: tuple[str, ...] = ()

    def argv(
        self,
        *,
        fix: bool,
        pin_editorconfig: bool = False,
        config_path: str | None = None,
    ) -> tuple[str, ...]:
        """The argv prefix for this run: the fix form in fix mode if the tool has one, else the check form."""
        base = self.fix if (fix and self.fix is not None) else self.check
        injected: tuple[str, ...] = ()
        if self.config_inject:
            if any(CONFIG_PLACEHOLDER in tok for tok in self.config_inject):
                if config_path is not None:
                    injected = tuple(
                        tok.replace(CONFIG_PLACEHOLDER, config_path)
                        for tok in self.config_inject
                    )
            else:
                injected = self.config_inject
        pin = (
            self.editorconfig_pin
            if (pin_editorconfig and self.editorconfig_pin)
            else ()
        )
        return (*injected, *pin, *base)


@dataclass(frozen=True)
class Lang:
    """A language leg: how files map to it, and the tools that check it.

    An ordinary Lang claims files repo-wide by extension (else shebang). A Lang with
    ``path_prefixes`` instead claims files sitting directly in those directories,
    ADDITIVELY to the extension route, with ``extensions`` scoping that claim.
    """

    name: str
    extensions: tuple[str, ...]
    tools: tuple[Tool, ...]
    shebangs: tuple[str, ...] = ()
    manifests: tuple[str, ...] = ()
    path_prefixes: tuple[str, ...] = ()
    plugin_scoped_extensions: tuple[str, ...] = ()


PYTHON = Lang(
    name="python",
    extensions=(".py",),
    tools=(
        Tool(
            "ruff",
            ("check",),
            fix=("check", "--fix"),
            config_inject=("--config", CONFIG_PLACEHOLDER),
        ),
        Tool(
            "ruff",
            ("format", "--check"),
            fix=("format",),
            config_inject=("--config", CONFIG_PLACEHOLDER),
        ),
    ),
)
RUST = Lang(
    name="rust",
    extensions=(".rs",),
    manifests=("Cargo.toml",),
    tools=(
        Tool(
            "cargo",
            (
                "clippy",
                "--all",
                "--all-targets",
                "--all-features",
                "--",
                "-D",
                "warnings",
            ),
            per_manifest=True,
        ),
        Tool(
            "cargo",
            ("fmt", "--all", "--", "--check", "--config-path", _RUSTFMT_CONFIG_PATH),
            fix=("fmt", "--all", "--", "--config-path", _RUSTFMT_CONFIG_PATH),
            per_manifest=True,
        ),
    ),
)
SHELL = Lang(
    name="shell",
    extensions=(".sh", ".bash"),
    shebangs=("sh", "bash"),
    tools=(
        Tool("shellcheck", ("--norc", "--severity=info")),
        Tool("shfmt", ("-d",), fix=("-w",), editorconfig_pin=("-i", "0")),
    ),
)
YAML = Lang(
    name="yaml",
    extensions=(".yml", ".yaml"),
    tools=(Tool("yamllint", ("--strict",), config_inject=("-c", CONFIG_PLACEHOLDER)),),
)
ACTIONS = Lang(
    name="actions",
    extensions=(".yml", ".yaml"),
    path_prefixes=(".github/workflows/",),
    tools=(Tool("actionlint", (), config_inject=("-config-file", CONFIG_PLACEHOLDER)),),
)
WEB = Lang(
    name="web",
    extensions=(".json", ".ts", ".tsx", ".svelte"),
    plugin_scoped_extensions=(".svelte",),
    tools=(
        Tool(
            "prettier",
            ("--check", "--log-level", "warn"),
            fix=("--write",),
            config_inject=("--config", CONFIG_PLACEHOLDER),
            editorconfig_pin=("--no-editorconfig",),
        ),
    ),
)
MARKDOWN = Lang(
    name="markdown",
    extensions=(".md",),
    tools=(
        Tool(
            "markdownlint",
            (),
            fix=("--fix",),
            config_inject=("--config", CONFIG_PLACEHOLDER),
        ),
    ),
)
LEX = Lang(
    name="lex",
    extensions=(".lex",),
    tools=(Tool("lexd", ("check",)),),
)
LUA = Lang(
    name="lua",
    extensions=(".lua",),
    tools=(
        Tool("stylua", ("--check",), fix=()),
        Tool("selene", ()),
    ),
)

LANGS: tuple[Lang, ...] = (
    PYTHON,
    RUST,
    SHELL,
    YAML,
    ACTIONS,
    WEB,
    MARKDOWN,
    LEX,
    LUA,
)


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def lang_for(path: str, shebang: str | None = None) -> Lang | None:
    """The ordinary Lang a file routes to (extension, else shebang interpreter), or ``None``; path-claiming Langs are skipped."""
    name = _basename(path)
    if "." in name:
        ext = "." + name.rsplit(".", 1)[-1]
        for lang in LANGS:
            if not lang.path_prefixes and ext in lang.extensions:
                return lang
        return None
    interp = _interp(shebang)
    if interp:
        for lang in LANGS:
            if interp in lang.shebangs:
                return lang
    return None


def _in_dir(path: str, prefix: str) -> bool:
    """``path`` is an immediate child of directory ``prefix`` (trailing-slash, repo-relative), never nested deeper."""
    if not path.startswith(prefix):
        return False
    return "/" not in path[len(prefix) :]


def path_claimed_langs(path: str) -> list[Lang]:
    """Every path-claiming Lang whose prefix directly holds ``path``, in registry order; additive to :func:`lang_for`."""
    claimed: list[Lang] = []
    for lang in LANGS:
        if not lang.path_prefixes:
            continue
        if not any(_in_dir(path, prefix) for prefix in lang.path_prefixes):
            continue
        if lang.extensions and _ext(path) not in lang.extensions:
            continue
        claimed.append(lang)
    return claimed


def _interp(shebang: str | None) -> str | None:
    """The interpreter basename from a shebang line (``/usr/bin/env bash`` → ``bash``)."""
    if not shebang:
        return None
    tokens = shebang.split()
    if not tokens:
        return None
    first = tokens[0].rsplit("/", 1)[-1]
    if first == "env" and len(tokens) > 1:
        return tokens[1].rsplit("/", 1)[-1]
    return first


def manifest_roots(paths: list[str], manifests: tuple[str, ...]) -> list[str]:
    """Every directory holding one of ``manifests`` among ``paths``, sorted; nested manifests are never collapsed under an ancestor."""
    dirs = {
        path.rsplit("/", 1)[0] if "/" in path else "."
        for path in paths
        if _basename(path) in manifests
    }
    return sorted(dirs)


def _ext(path: str) -> str:
    """The lowercase extension (with dot) of ``path``, or ``""`` if it has none."""
    name = _basename(path)
    return "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""


def partition_plugin_scoped(
    paths: list[str], plugin_scoped: tuple[str, ...]
) -> tuple[list[str], list[str]]:
    """Split ``paths`` into ``(plugin_free, plugin_scoped)`` by extension, order preserved."""
    if not plugin_scoped:
        return paths, []
    free: list[str] = []
    scoped: list[str] = []
    for path in paths:
        (scoped if _ext(path) in plugin_scoped else free).append(path)
    return free, scoped


def lex_projections(paths: list[str]) -> set[str]:
    """Tracked ``X.md`` files that are generated projections of a tracked ``X.lex`` source."""
    sources = {p for p in paths if p.endswith(".lex")}
    return {p for p in paths if p.endswith(".md") and p[:-3] + ".lex" in sources}


def editorconfig_declares_root(content: str) -> bool:
    """Whether an ``.editorconfig`` body declares ``root = true`` in its preamble; a duplicated key resolves last-wins."""
    result = False
    for raw in content.splitlines():
        line = raw.strip()
        if not line or line[0] in ";#":
            continue
        if line.startswith("["):
            break
        key, sep, value = line.partition("=")
        if sep and key.strip().lower() == "root":
            result = value.strip().lower() == "true"
    return result


def tracks_editorconfig(paths: list[str], read_root: Callable[[], str | None]) -> bool:
    """Whether ``paths`` (the repo's TOP-LEVEL tracked list) holds a root ``.editorconfig`` declaring ``root = true``; ``read_root`` lazily yields its body and is consulted only then."""
    if ".editorconfig" not in paths:
        return False
    content = read_root()
    return content is not None and editorconfig_declares_root(content)


def _ignore_matchers(patterns: list[str]) -> list[include.PatternSet]:
    """Compile each consumer ``[lint].ignore`` glob into a gitignore matcher; a malformed glob is dropped, never raised."""
    matchers: list[include.PatternSet] = []
    for pattern in patterns:
        try:
            matchers.append(include.parse(pattern))
        except re.error:
            continue
    return matchers


def path_ignored(path: str, patterns: list[str]) -> bool:
    """Whether ``path`` matches any consumer ``[lint].ignore`` glob."""
    return any(m.match(path) for m in _ignore_matchers(patterns))


def drop_ignored(paths: list[str], patterns: list[str]) -> list[str]:
    """``paths`` with every consumer-ignored entry removed, order preserved."""
    if not patterns:
        return paths
    matchers = _ignore_matchers(patterns)
    if not matchers:
        return paths
    return [p for p in paths if not any(m.match(p) for m in matchers)]


PROTECTED_TESTDATA_GLOBS: tuple[str, ...] = (
    "fixtures/",
    "__fixtures__/",
    "testdata/",
    "golden/",
    "goldens/",
    "snapshots/",
    "__snapshots__/",
)


@functools.cache
def _protected_matchers() -> tuple[include.PatternSet, ...]:
    """The compiled :data:`PROTECTED_TESTDATA_GLOBS` matchers, built once."""
    return tuple(_ignore_matchers(list(PROTECTED_TESTDATA_GLOBS)))


def drop_protected_testdata(paths: list[str]) -> list[str]:
    """``paths`` with every built-in protected test-data path removed, order preserved."""
    matchers = _protected_matchers()
    return [p for p in paths if not any(m.match(p) for m in matchers)]


def protected_testdata(paths: list[str]) -> list[str]:
    """The protected subset of ``paths`` — the exact complement of :func:`drop_protected_testdata`."""
    matchers = _protected_matchers()
    return [p for p in paths if any(m.match(p) for m in matchers)]


def route(
    paths: list[str], shebangs: dict[str, str | None] | None = None
) -> list[tuple[Lang, list[str]]]:
    """Bucket paths by language, in registry order; a path can reach both its ordinary Lang and every path-claiming Lang covering it. Pure (no I/O)."""
    shebangs = shebangs or {}
    projections = lex_projections(paths)
    buckets: dict[str, list[str]] = {}
    for path in paths:
        if path in projections:
            continue
        lang = lang_for(path, shebangs.get(path))
        if lang is not None:
            buckets.setdefault(lang.name, []).append(path)
        for claimed in path_claimed_langs(path):
            buckets.setdefault(claimed.name, []).append(path)
    return [(lang, buckets[lang.name]) for lang in LANGS if lang.name in buckets]


@dataclass(frozen=True)
class ToolRun:
    """The outcome of one tool invocation over its file batch."""

    lang: str
    binary: str
    label: str
    returncode: int
    output: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def verdict(runs: list[ToolRun]) -> int:
    """``0`` when every run passed, ``1`` otherwise — the whole check contract."""
    return 0 if all(run.ok for run in runs) else 1


def is_prettier_plugin_load_failure(binary: str, rc: int, output: str) -> bool:
    """Whether a prettier run failed on a plugin it could not RESOLVE — matched tightly on the Node resolver's ``imported from`` phrasing, which a formatting failure never carries."""
    if binary != "prettier" or rc == 0:
        return False
    lowered = output.lower()
    has_resolver_phrase = (
        "cannot find package" in lowered or "cannot find module" in lowered
    )
    return has_resolver_phrase and "imported from" in lowered


_CARGO_VERSION_RE = re.compile(r"^cargo\s+(\d+(?:\.\d+)+)", re.MULTILINE)


def parse_cargo_version(output: str) -> str | None:
    """The numeric version in a ``cargo --version`` banner, or ``None`` when no banner is recognizable."""
    match = _CARGO_VERSION_RE.search(output)
    return match.group(1) if match else None


def rust_pin_satisfied(version: str, spec: str) -> bool:
    """Whether cargo ``version`` satisfies the pixi ``rust`` pin ``spec``.

    Models ``*``, ``==X.Y.Z`` and dot-bounded prefix forms. Any other shape is
    unmodelled and returns ``True``, so ambiguity keeps the gate hard.
    """
    spec = spec.strip()
    if not spec or spec == "*":
        return True
    if spec.startswith("=="):
        return version == spec[2:].strip()
    if any(ch in spec for ch in "><~!,|^@/"):
        return True
    base = spec.lstrip("=").strip()
    if base.endswith(".*"):
        base = base[:-2]
    if not re.fullmatch(r"\d+(?:\.\d+)*", base):
        return True
    return version == base or version.startswith(base + ".")


def rust_pin_from_manifest(data: object) -> str | None:
    """The repo's pinned ``rust`` spec from a parsed ``pixi.toml``, read from ``[feature.lint.dependencies]`` then the default ``[dependencies]``; ``None`` when absent or malformed."""
    if not isinstance(data, dict):
        return None
    feature = data.get("feature")
    lint_feature = feature.get("lint") if isinstance(feature, dict) else None
    tables = (
        lint_feature.get("dependencies") if isinstance(lint_feature, dict) else None,
        data.get("dependencies"),
    )
    for table in tables:
        if not isinstance(table, dict):
            continue
        spec = table.get("rust")
        if isinstance(spec, dict):
            spec = spec.get("version")
        if isinstance(spec, str) and spec.strip():
            return spec.strip()
    return None


def detect_rust_skew(pin: str | None, version_output: str | None) -> str | None:
    """The toolchain-skew note when the resolved cargo escapes the repo's pixi rust pin; ``None`` on either input claims no skew."""
    if pin is None or version_output is None:
        return None
    version = parse_cargo_version(version_output)
    if version is None or rust_pin_satisfied(version, pin):
        return None
    return (
        f"cargo: TOOLCHAIN SKEW — this run resolved cargo {version}, but the "
        f"repo pins rust {pin!r} in pixi.toml, so this rust verdict is not the "
        "canonical one and it WARNS instead of blocking (#602). The pinned-env "
        "gate is authoritative: run `pixi install`, then `pixi run lint` "
        "(the same env CI enforces). Fix the env skew rather than bypassing the "
        "hook with --no-verify."
    )


def _discover(root: Path) -> list[str]:
    return git.ls_files(cwd=str(root))


def _read_editorconfig(path: Path) -> str | None:
    """The body of the root ``.editorconfig`` at ``path``, or ``None`` if unreadable; a leading BOM is stripped."""
    try:
        return path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None


def _tracks_root_editorconfig(root: Path) -> bool:
    """Whether the git repo containing ``root`` tracks a root ``.editorconfig`` declaring ``root = true``, read from the repo's top-level tracked list rather than the routed files."""
    repo_root = git.repo_root(cwd=str(root))
    if repo_root is None:
        return False
    return tracks_editorconfig(
        git.ls_files(cwd=repo_root),
        lambda: _read_editorconfig(Path(repo_root) / ".editorconfig"),
    )


def _ignore_globs(root: Path) -> list[str]:
    """The consumer ``[lint].ignore`` globs from ``root``'s ``.shipit.toml``; no config or no table yields ``[]``."""
    cfg_path = root / config.CONFIG_NAME
    if not cfg_path.is_file():
        return []
    return config.load_lint_ignore(config.load(cfg_path))


def _shebang(path: Path) -> str | None:
    """The shebang body of ``path`` (without ``#!``), or ``None``."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            first = fh.readline()
    except OSError:
        return None
    return first[2:].strip() if first.startswith("#!") else None


CHECK_TIMEOUT: float = execrun.DEFAULT_TIMEOUT


def _snapshot(root: Path, rel_paths: list[str]) -> dict[str, bytes]:
    """The pre-image bytes of each ``rel_paths`` file under ``root`` that exists; a missing path is simply not snapshotted."""
    snapshot: dict[str, bytes] = {}
    for rel in rel_paths:
        try:
            snapshot[rel] = (root / rel).read_bytes()
        except OSError:
            continue
    return snapshot


def _restore(root: Path, snapshot: dict[str, bytes]) -> list[str]:
    """Rewrite each snapshot file a fixer changed back to its pre-image bytes; return the restored paths."""
    restored: list[str] = []
    for rel, original in snapshot.items():
        path = root / rel
        try:
            if path.read_bytes() != original:
                path.write_bytes(original)
                restored.append(rel)
        except OSError:
            continue
    return restored


_TOOL_CONFIG_ENV_VARS: frozenset[str] = frozenset(
    {
        "SHELLCHECK_OPTS",
        "YAMLLINT_CONFIG_FILE",
        "RUFF_CONFIG",
        "CARGO_HOME",
        "CLIPPY_CONF_DIR",
    }
)


def _is_ambient_config_var(key: str) -> bool:
    """Whether env var ``key`` is an ambient-config source the scrub drops: ``HOME``, an ``XDG_*``, or a :data:`_TOOL_CONFIG_ENV_VARS` member."""
    return key == "HOME" or key.startswith("XDG_") or key in _TOOL_CONFIG_ENV_VARS


def _scrubbed_env() -> dict[str, str]:
    """``os.environ`` minus the ambient-config vars — a COMPLETE child env, since it is passed with ``replace_env=True``."""
    return {k: v for k, v in os.environ.items() if not _is_ambient_config_var(k)}


_CANONICAL_CONFIG_FILES: dict[str, str] = {
    "ruff": "ruff.toml",
    "prettier": "prettierrc.yaml",
    "markdownlint": "markdownlint.yaml",
    "yamllint": "yamllint.yaml",
    "actionlint": "actionlint.yaml",
}


def _canonical_config(tool: Tool, root: Path) -> str | None:
    """The packaged canonical config path for ``tool``, or ``None`` when it has no file-config; independent of ``root``, so injection fires in any tree."""
    name = _CANONICAL_CONFIG_FILES.get(tool.binary)
    return _data_path(name) if name is not None else None


def _pinned_rust_spec(root: Path) -> str | None:
    """The repo's pixi-pinned ``rust`` spec from ``root``'s ``pixi.toml``, or ``None`` when missing or malformed."""
    try:
        with (root / "pixi.toml").open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    return rust_pin_from_manifest(data)


def _probe_cargo_version(
    run_tool: Callable[[str, list[str], Path], execrun.ExecResult], cwd: Path
) -> str | None:
    """The resolved toolchain's ``cargo --version`` banner in ``cwd``, or ``None`` when the probe fails; runs in the same cwd as the leg it explains."""
    try:
        result = run_tool("cargo", ["--version"], cwd)
    except execrun.ExecError:
        return None
    if result.rc != 0:
        return None
    return result.stdout + result.stderr


def _run_tool(binary: str, args: list[str], cwd: Path) -> execrun.ExecResult:
    """Run ``binary args`` in ``cwd`` through the one Exec runner.

    ``check=False``: a nonzero rc is the tool's verdict; a launch failure raises
    :class:`~shipit.execrun.ExecError`, which the orchestrator renders as 127. The
    single choke point where the ambient-config env scrub is applied.
    """
    return execrun.run(
        [binary, *args],
        cwd=str(cwd),
        env=_scrubbed_env(),
        replace_env=True,
        check=False,
        timeout=CHECK_TIMEOUT,
    )


def _indent(text: str, prefix: str = "      ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


def run(
    path: str | None = None,
    *,
    fix: bool = False,
    discover: Callable[[Path], list[str]] | None = None,
    run_tool: Callable[[str, list[str], Path], execrun.ExecResult] | None = None,
    tracks_root_editorconfig: Callable[[Path], bool] | None = None,
    canonical_config: Callable[[Tool, Path], str | None] | None = None,
    pinned_rust_spec: Callable[[Path], str | None] | None = None,
    runs_out: list[ToolRun] | None = None,
) -> int:
    """Run the checks over the tree at ``path`` (default ``.``). Returns 0/1.

    ``runs_out``, when given, receives every :class:`ToolRun` outcome. A malformed
    ``.shipit.toml`` raises :class:`~shipit.config.ConfigError`. The remaining keyword
    arguments are the I/O seams tests substitute.
    """
    started = time.monotonic()
    root = Path(path or ".").resolve()
    if not root.is_dir():
        print(f"lint: {root} is not a directory", file=sys.stderr)
        logger.error("lint target is not a directory", extra={"root": str(root)})
        return 1

    discover = discover or _discover
    run_tool = run_tool or _run_tool
    tracks_root_ec = tracks_root_editorconfig or _tracks_root_editorconfig
    canonical_config = canonical_config or _canonical_config
    pinned_rust_spec = pinned_rust_spec or _pinned_rust_spec

    files = drop_ignored(discover(root), _ignore_globs(root))
    shebangs = {p: _shebang(root / p) for p in files if "." not in _basename(p)}
    routed = route(files, shebangs)
    pin_editorconfig = not tracks_root_ec(root)

    rust_pin: str | None = None
    if any(lang.name == RUST.name for lang, _ in routed):
        rust_pin = pinned_rust_spec(root)
    rust_skew_by_dir: dict[str, str | None] = {}

    mode = "fix" if fix else "check"
    print(f"lint: {root} ({mode})")
    if not routed:
        print("  no recognized files — nothing to check.")
        logger.info(
            "lint complete — no recognized files",
            extra={
                "root": str(root),
                "mode": mode,
                "checks": 0,
                "failed": 0,
                "rc": 0,
                "duration_ms": int((time.monotonic() - started) * 1000),
            },
        )
        return 0

    runs: list[ToolRun] = runs_out if runs_out is not None else []
    for lang, paths in routed:
        mdirs = (
            (manifest_roots(files, lang.manifests) or ["."])
            if lang.manifests
            else ["."]
        )
        for tool in lang.tools:
            config_path = canonical_config(tool, root)
            prefix = tool.argv(
                fix=fix, pin_editorconfig=pin_editorconfig, config_path=config_path
            )
            label = f"{tool.binary} {' '.join(prefix)}".strip()
            mutating = fix and tool.fix is not None
            guard_snapshot: dict[str, bytes] | None = None
            if tool.per_manifest:
                if mutating:
                    guard_snapshot = _snapshot(root, protected_testdata(paths))
                batches = [
                    (list(prefix), mdir, f"crate {mdir}", 0, False) for mdir in mdirs
                ]
            else:
                batch_paths = drop_protected_testdata(paths) if mutating else paths
                if mutating and not batch_paths:
                    batches = []
                else:
                    free_paths, scoped_paths = partition_plugin_scoped(
                        batch_paths, lang.plugin_scoped_extensions
                    )
                    batches = []
                    if free_paths:
                        count = (
                            f"{len(free_paths)} file"
                            f"{'s' if len(free_paths) != 1 else ''}"
                        )
                        batches.append(
                            ([*prefix, *free_paths], ".", count, len(free_paths), False)
                        )
                    if scoped_paths:
                        count = (
                            f"{len(scoped_paths)} file"
                            f"{'s' if len(scoped_paths) != 1 else ''}"
                        )
                        batches.append(
                            (
                                [*prefix, *scoped_paths],
                                ".",
                                count,
                                len(scoped_paths),
                                True,
                            )
                        )
            try:
                for args, mdir, note, nfiles, fail_open_ok in batches:
                    fail_open_note: str | None = None
                    try:
                        result = run_tool(tool.binary, args, root / mdir)
                    except execrun.ExecError as exc:
                        rc = 127
                        if exc.cause == execrun.CAUSE_MISSING_BINARY:
                            out = (
                                f"{tool.binary}: not found on PATH "
                                "(the check is hard — provision it)"
                            )
                        else:
                            out = f"{tool.binary}: could not run: {exc}"
                        logger.error(
                            "lint tool could not run",
                            exc_info=True,
                            extra={
                                "lang": lang.name,
                                "tool": tool.binary,
                                "rc": rc,
                                "cwd": mdir,
                                "batch": note,
                            },
                        )
                    else:
                        rc, out = result.rc, result.stdout + result.stderr
                        if fail_open_ok and is_prettier_plugin_load_failure(
                            tool.binary, rc, out
                        ):
                            fail_open_note = (
                                "prettier: skipped — a plugin named in .prettierrc is "
                                "not installed (module-resolution failure). This "
                                "`.svelte` leg needs prettier-plugin-svelte, so it is "
                                "environment-not-provisioned, not a lint failure; "
                                "provision node_modules to enable it. Any `.json`/"
                                "`.ts`/`.tsx` are checked in a separate leg that this "
                                "does not affect.\n" + out.strip()
                            )
                            logger.warning(
                                "lint prettier plugin-load failure — fail open (#498)",
                                extra={
                                    "lang": lang.name,
                                    "tool": tool.binary,
                                    "cwd": mdir,
                                    "batch": note,
                                },
                            )
                            rc, out = 0, ""
                        elif rc != 0 and tool.binary == "cargo" and rust_pin:
                            if mdir not in rust_skew_by_dir:
                                rust_skew_by_dir[mdir] = detect_rust_skew(
                                    rust_pin,
                                    _probe_cargo_version(run_tool, root / mdir),
                                )
                            rust_skew = rust_skew_by_dir[mdir]
                            if rust_skew:
                                fail_open_note = rust_skew + (
                                    "\n" + out.strip() if out.strip() else ""
                                )
                                logger.warning(
                                    "lint rust toolchain skew — warn, not block (#602)",
                                    extra={
                                        "lang": lang.name,
                                        "tool": tool.binary,
                                        "cwd": mdir,
                                        "batch": note,
                                    },
                                )
                                rc, out = 0, ""
                        logger.debug(
                            "lint tool finished",
                            extra={
                                "lang": lang.name,
                                "tool": tool.binary,
                                "rc": rc,
                                "files": nfiles,
                                "cwd": mdir,
                                "batch": note,
                                "duration_ms": result.duration_ms,
                            },
                        )
                    runs.append(ToolRun(lang.name, tool.binary, label, rc, out))
                    mark = "ok  " if rc == 0 else "FAIL"
                    print(f"  {mark} {lang.name:9} {label} ({note})")
                    if fail_open_note:
                        print(_indent(fail_open_note))
                    elif rc != 0 and out.strip():
                        print(_indent(out.strip()))
            finally:
                if guard_snapshot:
                    restored = _restore(root, guard_snapshot)
                    if restored:
                        logger.debug(
                            "lint restored protected fixtures after fix",
                            extra={
                                "lang": lang.name,
                                "tool": tool.binary,
                                "restored": len(restored),
                            },
                        )

    rc = verdict(runs)
    failed = sorted({f"{r.lang}:{r.binary}" for r in runs if not r.ok})
    if rc == 0:
        print(f"LINT: OK ({len(runs)} checks)")
    else:
        print(f"LINT: FAILED ({', '.join(failed)})")
    summary = {
        "root": str(root),
        "mode": mode,
        "checks": len(runs),
        "failed": len(failed),
        "rc": rc,
        "duration_ms": int((time.monotonic() - started) * 1000),
    }
    if failed:
        summary["failed_checks"] = ", ".join(failed)
    logger.info("lint complete", extra=summary)
    return rc
