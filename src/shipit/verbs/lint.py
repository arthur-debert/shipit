"""lint — the standardized multi-language gate (ROADMAP.lex §3).

shipit INVERTS release's lefthook-as-orchestrator model. Because pixi has no
cross-manifest task inheritance (architecture.lex §5), the per-language
discovery, routing and aggregation cannot live in a pixi task templated into
each consumer — that is drift on pixi.toml. So it lives HERE, in the binary:
lefthook is thin (it calls ``pixi run lint``), pixi is thin (it runs
``shipit lint``), and this verb does the real work. CI and the pre-commit hook
run the IDENTICAL gate because it is ONE binary with ONE config — "both agree"
is structural, not two transcriptions of the rules drifting apart.

It is a HARD gate (architecture.lex §7): a missing tool exits non-zero, it never
skips. A clean run is ``0``; any failure is ``1``.

The gate is CHECK-ONLY by default (release's scar: ``prettier --write`` under
--all-files silently rewrites untouched files, so the gate must never mutate).
``--fix`` is the opt-in formatter pass — and only tools with a safe in-place fix
participate; the rest still run as checks.

The pure logic — the toolchain registry (:data:`LANGS`) and :func:`route` — is
kept out of the subprocess boundary (:func:`_discover`, :func:`_shebang`,
:func:`_run_tool`) so it is unit-testable, the same split checks.py uses against
its gh calls.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .. import gh

# --------------------------------------------------------------------------
# The toolchain registry — the slim, valuable part of release-core's gate
# --------------------------------------------------------------------------
#
# These are release-core toolset.py's battle-tested command lines (NOT its
# lefthook orchestration, toolset provisioning, or verdict parsing — the three
# things the pixi + binary model replaces). Version pinning lives in pixi.toml /
# pixi.lock (the linters) and scripts/provision-lexd.sh (lexd); the registry
# only encodes WHICH tool runs and HOW it is invoked.


@dataclass(frozen=True)
class Tool:
    """One linter invocation: the binary plus its check args (files appended).

    ``fix`` is the formatter form applied under ``--fix``; ``None`` means the
    tool has no safe in-place fix, so it is skipped in fix mode but still runs
    as a check.
    """

    binary: str
    check: tuple[str, ...]
    fix: tuple[str, ...] | None = None

    def argv(self, *, fix: bool) -> tuple[str, ...] | None:
        """The argv prefix for this run, or ``None`` to skip (fix mode, no fixer)."""
        if fix:
            return self.fix  # may be None -> skip
        return self.check


@dataclass(frozen=True)
class Lang:
    """A language leg: how files map to it, and the tools that gate it."""

    name: str
    extensions: tuple[str, ...]
    tools: tuple[Tool, ...]
    shebangs: tuple[str, ...] = ()  # interpreter basenames for extensionless files


PYTHON = Lang(
    name="python",
    extensions=(".py",),
    tools=(
        Tool("ruff", ("check",), fix=("check", "--fix")),
        Tool("ruff", ("format", "--check"), fix=("format",)),
    ),
)
SHELL = Lang(
    name="shell",
    extensions=(".sh", ".bash"),
    shebangs=("sh", "bash"),
    tools=(
        Tool("shellcheck", ("--severity=info",)),
        Tool("shfmt", ("-d",), fix=("-w",)),
    ),
)
YAML = Lang(
    name="yaml",
    extensions=(".yml", ".yaml"),
    tools=(Tool("yamllint", ("--strict",)),),
)
JSON = Lang(
    name="json",
    extensions=(".json",),
    tools=(Tool("prettier", ("--check", "--log-level", "warn"), fix=("--write",)),),
)
MARKDOWN = Lang(
    name="markdown",
    extensions=(".md",),
    tools=(Tool("markdownlint", (), fix=("--fix",)),),
)
LEX = Lang(
    name="lex",
    extensions=(".lex",),
    # `lexd format` writes to stdout only (no in-place form), so lex has no safe
    # --fix; it gates as check-only via `lexd check` (CI-friendly exit codes).
    tools=(Tool("lexd", ("check",)),),
)

LANGS: tuple[Lang, ...] = (PYTHON, SHELL, YAML, JSON, MARKDOWN, LEX)


# --------------------------------------------------------------------------
# Pure routing
# --------------------------------------------------------------------------


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def lang_for(path: str, shebang: str | None = None) -> Lang | None:
    """The language a file routes to — by extension, else by shebang interpreter.

    Extensionless scripts route by their shebang's interpreter basename (release
    routes shell this way; mirror it). A file matching nothing is unmanaged.
    """
    name = _basename(path)
    if "." in name:
        ext = "." + name.rsplit(".", 1)[-1]
        for lang in LANGS:
            if ext in lang.extensions:
                return lang
        return None
    interp = _interp(shebang)
    if interp:
        for lang in LANGS:
            if interp in lang.shebangs:
                return lang
    return None


def _interp(shebang: str | None) -> str | None:
    """The interpreter basename from a shebang line (``/usr/bin/env bash`` → ``bash``)."""
    if not shebang:
        return None
    tokens = shebang.split()
    if not tokens:
        return None
    first = tokens[0].rsplit("/", 1)[-1]
    # `/usr/bin/env bash` — the real interpreter is the arg after env.
    if first == "env" and len(tokens) > 1:
        return tokens[1].rsplit("/", 1)[-1]
    return first


def route(
    paths: list[str], shebangs: dict[str, str | None] | None = None
) -> list[tuple[Lang, list[str]]]:
    """Bucket paths by language, in registry order. Pure (no I/O)."""
    shebangs = shebangs or {}
    buckets: dict[str, list[str]] = {}
    for path in paths:
        lang = lang_for(path, shebangs.get(path))
        if lang is not None:
            buckets.setdefault(lang.name, []).append(path)
    return [(lang, buckets[lang.name]) for lang in LANGS if lang.name in buckets]


# --------------------------------------------------------------------------
# Reporting (pure)
# --------------------------------------------------------------------------


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
    """``0`` when every run passed, ``1`` otherwise — the whole gate contract."""
    return 0 if all(run.ok for run in runs) else 1


# --------------------------------------------------------------------------
# The subprocess + git boundary (patched in tests)
# --------------------------------------------------------------------------


def _discover(root: Path) -> list[str]:
    return gh.git_ls_files(cwd=str(root))


def _shebang(path: Path) -> str | None:
    """The shebang body of ``path`` (without ``#!``), or ``None``."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            first = fh.readline()
    except OSError:
        return None
    return first[2:].strip() if first.startswith("#!") else None


def _run_tool(binary: str, args: list[str], cwd: Path) -> tuple[int, str]:
    """Run ``binary args`` in ``cwd``; return (exit code, combined output).

    A binary missing from PATH is the HARD-gate signal: ``127`` + a clear note,
    never a silent skip.
    """
    try:
        proc = subprocess.run(
            [binary, *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return 127, f"{binary}: not found on PATH (the gate is hard — provision it)"
    return proc.returncode, proc.stdout + proc.stderr


# --------------------------------------------------------------------------
# Orchestrator
# --------------------------------------------------------------------------


def _indent(text: str, prefix: str = "      ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


def run(
    path: str | None = None,
    *,
    fix: bool = False,
    discover: Callable[[Path], list[str]] | None = None,
    run_tool: Callable[[str, list[str], Path], tuple[int, str]] | None = None,
) -> int:
    """Run the gate over the tree at ``path`` (default ``.``). Returns 0/1."""
    root = Path(path or ".").resolve()
    if not root.is_dir():
        print(f"lint: {root} is not a directory", file=sys.stderr)
        return 1

    discover = discover or _discover
    run_tool = run_tool or _run_tool

    files = discover(root)
    shebangs = {p: _shebang(root / p) for p in files if "." not in _basename(p)}
    routed = route(files, shebangs)

    mode = "fix" if fix else "check"
    print(f"lint: {root} ({mode})")
    if not routed:
        print("  no recognized files — nothing to check.")
        return 0

    runs: list[ToolRun] = []
    for lang, paths in routed:
        for tool in lang.tools:
            prefix = tool.argv(fix=fix)
            if prefix is None:
                continue  # fix mode, tool has no in-place fixer — skip
            label = f"{tool.binary} {' '.join(tool.check)}".strip()
            rc, out = run_tool(tool.binary, [*prefix, *paths], root)
            runs.append(ToolRun(lang.name, tool.binary, label, rc, out))
            mark = "ok  " if rc == 0 else "FAIL"
            count = f"{len(paths)} file{'s' if len(paths) != 1 else ''}"
            print(f"  {mark} {lang.name:9} {label} ({count})")
            if rc != 0 and out.strip():
                print(_indent(out.strip()))

    rc = verdict(runs)
    if rc == 0:
        print(f"LINT: OK ({len(runs)} checks)")
    else:
        failed = sorted({f"{r.lang}:{r.binary}" for r in runs if not r.ok})
        print(f"LINT: FAILED ({', '.join(failed)})")
    return rc
