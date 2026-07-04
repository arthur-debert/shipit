"""lint — the standardized multi-language checks (docs/prd/lint-checks.md).

shipit INVERTS release's lefthook-as-orchestrator model. Because pixi has no
cross-manifest task inheritance (architecture.lex §5), the per-language
discovery, routing and aggregation cannot live in a pixi task templated into
each consumer — that is drift on pixi.toml. So it lives HERE, in the binary:
lefthook is thin (it calls ``pixi run lint``), pixi is thin (it runs
``shipit lint``), and this verb does the real work. CI and the pre-commit hook
run the IDENTICAL checks because it is ONE binary with ONE config — "both agree"
is structural, not two transcriptions of the rules drifting apart.

The lint checks are HARD-FAIL (architecture.lex §7): a missing tool exits
non-zero, it never skips. A clean run is ``0``; any failure is ``1``.

The checks are CHECK-ONLY by default (release's scar: ``prettier --write`` under
--all-files silently rewrites untouched files, so they must never mutate).
``--fix`` is the opt-in formatter pass — and only tools with a safe in-place fix
participate; the rest still run as checks.

The pure logic — the toolchain registry (:data:`LANGS`) and :func:`route` — is
kept out of the Exec boundary (:func:`_discover`, :func:`_shebang`,
:func:`_run_tool`) so it is unit-testable, the same split checks.py uses against
its gh calls. Tool execution goes through the one Exec runner
(:mod:`shipit.execrun`, ADR-0028): :func:`_run_tool` returns the runner's
:class:`~shipit.execrun.ExecResult` (``check=False`` — a nonzero rc is the
tool's verdict, not a transport failure) and lets a launch failure surface as
:class:`~shipit.execrun.ExecError`, which the orchestrator renders as the
hard-fail ``127``.
"""

from __future__ import annotations

import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .. import execrun, git

logger = logging.getLogger("shipit.lint")

# --------------------------------------------------------------------------
# The toolchain registry — the slim, valuable part of release-core's checks
# --------------------------------------------------------------------------
#
# These are release-core toolset.py's battle-tested command lines (NOT its
# lefthook orchestration, toolset provisioning, or verdict parsing — the three
# things the pixi + binary model replaces). Version pinning lives in pixi.toml /
# pixi.lock (the linters) and tools/provision-lexd.sh (lexd); the registry
# only encodes WHICH tool runs and HOW it is invoked.


@dataclass(frozen=True)
class Tool:
    """One linter invocation: the binary plus its check args (files appended).

    ``fix`` is the formatter form applied under ``--fix``; ``None`` means the
    tool has no safe in-place fix, so in fix mode it falls back to its check
    form. The checks NEVER skip a tool — ``shipit lint --fix`` formats what it can
    AND still checks everything, so it can never pass while a non-fixable leg
    (shellcheck, yamllint, lexd) is failing.

    ``per_manifest`` tools speak to a build unit, not a file list (cargo has no
    file-batch form): they run once per tracked manifest directory of their Lang
    (see :func:`manifest_roots`) with NO files appended, cwd'd into that
    directory.
    """

    binary: str
    check: tuple[str, ...]
    fix: tuple[str, ...] | None = None
    per_manifest: bool = False

    def argv(self, *, fix: bool) -> tuple[str, ...]:
        """The argv prefix for this run: the fix form in fix mode if the tool has
        one, else the check form (never ``None`` — the checks never skip)."""
        if fix and self.fix is not None:
            return self.fix
        return self.check


@dataclass(frozen=True)
class Lang:
    """A language leg: how files map to it, and the tools that check it."""

    name: str
    extensions: tuple[str, ...]
    tools: tuple[Tool, ...]
    shebangs: tuple[str, ...] = ()  # interpreter basenames for extensionless files
    manifests: tuple[str, ...] = ()  # manifest basenames rooting per_manifest runs


PYTHON = Lang(
    name="python",
    extensions=(".py",),
    tools=(
        Tool("ruff", ("check",), fix=("check", "--fix")),
        Tool("ruff", ("format", "--check"), fix=("format",)),
    ),
)
RUST = Lang(
    name="rust",
    extensions=(".rs",),
    manifests=("Cargo.toml",),
    # cargo speaks to a crate/workspace, not a file list, so both tools are
    # per_manifest: one run per tracked Cargo.toml directory (see
    # manifest_roots — every tracked manifest runs, never collapsed, so a
    # nested crate that ISN'T a workspace member is never silently skipped).
    # clippy and fmt both carry --all so a workspace root covers its declared
    # members even when only the root manifest is tracked (release-core's
    # battle-tested forms, docs/prd/lint-checks.md). clippy findings are hard
    # errors (-D warnings) and clippy has no safe in-place fix here, so under
    # --fix it still runs its check form; `cargo fmt --all` is the one rust
    # --fix leg. A repo with .rs files but no tracked Cargo.toml runs at the
    # root and fails on cargo's own error — hard, never a silent skip. The
    # rust toolchain is assumed provisioned per the repo's toolchain
    # declaration (ADR-0007); a missing cargo is the standard hard-fail 127.
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
            ("fmt", "--all", "--", "--check"),
            fix=("fmt", "--all"),
            per_manifest=True,
        ),
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
    # --fix; it runs as check-only via `lexd check` (CI-friendly exit codes).
    tools=(Tool("lexd", ("check",)),),
)

LANGS: tuple[Lang, ...] = (PYTHON, RUST, SHELL, YAML, JSON, MARKDOWN, LEX)


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


def manifest_roots(paths: list[str], manifests: tuple[str, ...]) -> list[str]:
    """Every directory (repo-relative, ``"."`` for the root) holding one of
    ``manifests`` among the tracked paths, sorted. Pure (no I/O).

    Every tracked manifest gets its own run — nested manifests are NOT
    collapsed under an ancestor. Cargo does not make a nested manifest a
    workspace member automatically (a repo can have an independent nested
    crate, or a workspace that excludes one), so collapsing would silently
    skip it; the ``--all`` on the tools makes a true workspace root cover its
    declared members, and running every manifest guarantees the rest are
    never skipped. Redundant re-checks of shared members are cargo-cache
    cheap; a silent miss in a hard-fail lint is not.
    """
    dirs = {
        path.rsplit("/", 1)[0] if "/" in path else "."
        for path in paths
        if _basename(path) in manifests
    }
    return sorted(dirs)


def lex_projections(paths: list[str]) -> set[str]:
    """Tracked ``X.md`` files that are projections of a tracked ``X.lex`` source.

    The ``.lex`` is the gated source (the lexd leg); the ``.md`` is generated
    output carrying a "do not hand edit" preamble, so markdownlint's prose
    rules over it are noise about the generator, not signal about a document
    anyone edits. Pure (no I/O): "is a projection" is decided from the tracked
    file list alone, so the rule is consumer-generic — no repo-local
    ``.markdownlintignore`` entry per projection (ADP00-WS10, #436).
    """
    sources = {p for p in paths if p.endswith(".lex")}
    return {p for p in paths if p.endswith(".md") and p[:-3] + ".lex" in sources}


def route(
    paths: list[str], shebangs: dict[str, str | None] | None = None
) -> list[tuple[Lang, list[str]]]:
    """Bucket paths by language, in registry order. Pure (no I/O).

    Generated lex projections never route to markdown: their ``.lex`` source
    routes to the lexd leg instead (see :func:`lex_projections`).
    """
    shebangs = shebangs or {}
    projections = lex_projections(paths)
    buckets: dict[str, list[str]] = {}
    for path in paths:
        if path in projections:
            continue
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
    """``0`` when every run passed, ``1`` otherwise — the whole check contract."""
    return 0 if all(run.ok for run in runs) else 1


# --------------------------------------------------------------------------
# The Exec + git boundary (patched in tests)
# --------------------------------------------------------------------------


def _discover(root: Path) -> list[str]:
    return git.ls_files(cwd=str(root))


def _shebang(path: Path) -> str | None:
    """The shebang body of ``path`` (without ``#!``), or ``None``."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            first = fh.readline()
    except OSError:
        return None
    return first[2:].strip() if first.startswith("#!") else None


#: Each check Exec's stated timeout, in seconds (ADR-0028: every Exec states
#: its bound deliberately — never the runner's implicit default). A linter over
#: a whole tree is local but legitimately slow on a large repo, so the runner's
#: generous default IS the right bound — stated on the wire rather than
#: inherited, so the no-implicit-timeout sweep stays grep-verifiable.
CHECK_TIMEOUT: float = execrun.DEFAULT_TIMEOUT


def _run_tool(binary: str, args: list[str], cwd: Path) -> execrun.ExecResult:
    """Run ``binary args`` in ``cwd`` through the one Exec runner.

    ``check=False``: a nonzero rc is the tool's *verdict* (the normal failing-check
    outcome), not a transport failure. A launch failure — the binary missing from
    PATH, or any OS-level error — raises :class:`~shipit.execrun.ExecError`, which
    the orchestrator renders as the hard-fail ``127`` (never a silent skip).
    Each Exec states :data:`CHECK_TIMEOUT`; a wedged linter dies at that bound
    as a timeout-cause :class:`~shipit.execrun.ExecError` — the same hard-fail.
    """
    return execrun.run(
        [binary, *args], cwd=str(cwd), check=False, timeout=CHECK_TIMEOUT
    )


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
    run_tool: Callable[[str, list[str], Path], execrun.ExecResult] | None = None,
) -> int:
    """Run the checks over the tree at ``path`` (default ``.``). Returns 0/1."""
    started = time.monotonic()
    root = Path(path or ".").resolve()
    if not root.is_dir():
        print(f"lint: {root} is not a directory", file=sys.stderr)
        logger.error("lint target is not a directory", extra={"root": str(root)})
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

    runs: list[ToolRun] = []
    for lang, paths in routed:
        # per_manifest tools run once per tracked manifest directory. With no
        # manifest tracked they run at the root, where the tool's own error is
        # the (hard) verdict — never a silent skip.
        mdirs = (
            (manifest_roots(files, lang.manifests) or ["."])
            if lang.manifests
            else ["."]
        )
        for tool in lang.tools:
            prefix = tool.argv(fix=fix)
            # Label from the actual argv that ran, so fix mode never claims it
            # ran the check form when it ran the fix form.
            label = f"{tool.binary} {' '.join(prefix)}".strip()
            if tool.per_manifest:
                batches = [(list(prefix), mdir, f"crate {mdir}") for mdir in mdirs]
            else:
                count = f"{len(paths)} file{'s' if len(paths) != 1 else ''}"
                batches = [([*prefix, *paths], ".", count)]
            for args, mdir, note in batches:
                try:
                    result = run_tool(tool.binary, args, root / mdir)
                except execrun.ExecError as exc:
                    # A binary missing from PATH (or any launch failure) is the
                    # HARD-fail signal: 127 + a clear note, never a silent skip.
                    # It propagates (the run's verdict fails), so ERROR + exception.
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
                    # Per-tool outcomes are mechanics; the run summary is the milestone.
                    logger.debug(
                        "lint tool finished",
                        extra={
                            "lang": lang.name,
                            "tool": tool.binary,
                            "rc": rc,
                            "files": len(paths),
                            "cwd": mdir,
                            "batch": note,
                            "duration_ms": result.duration_ms,
                        },
                    )
                runs.append(ToolRun(lang.name, tool.binary, label, rc, out))
                mark = "ok  " if rc == 0 else "FAIL"
                print(f"  {mark} {lang.name:9} {label} ({note})")
                if rc != 0 and out.strip():
                    print(_indent(out.strip()))

    rc = verdict(runs)
    failed = sorted({f"{r.lang}:{r.binary}" for r in runs if not r.ok})
    if rc == 0:
        print(f"LINT: OK ({len(runs)} checks)")
    else:
        print(f"LINT: FAILED ({', '.join(failed)})")
    # The orchestration summary — one milestone per run, pass or fail: the
    # verdict propagates through the exit code, not an exception.
    summary = {
        "root": str(root),
        "mode": mode,
        "checks": len(runs),
        "failed": len(failed),
        "rc": rc,
        "duration_ms": int((time.monotonic() - started) * 1000),
    }
    if failed:
        # Present only when meaningful — the absent-not-null record contract.
        summary["failed_checks"] = ", ".join(failed)
    logger.info("lint complete", extra=summary)
    return rc
