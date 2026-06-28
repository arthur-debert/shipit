"""Code-path classifier — the HAR01 default "is this a code path?" rule.

`is_code_path(path) -> bool` answers the one question the coordinator guard
(ADR-0012) turns on after `decide()`: does this edit touch *code* (which the
coordinator must delegate) or docs/config (which it may author directly)?

This ships an HAR01 **default** and **converges on the path→toolchain map when
ADR-0007 lands** — HAR01 does not block on that unbuilt map. The default:

  CODE (guarded): anything under a `src/`, `tests/`, or `bin/` directory; known
  code filenames (`Makefile`, `Dockerfile`); and mainstream code extensions
  (`*.py`, `*.sh`, `*.js`/`*.mjs`/`*.cjs`, `*.ts`/`*.tsx`, `*.jsx`, `*.rs`,
  `*.go`, `*.rb`, `*.java`, `*.c`/`*.h`/`*.cpp`/`*.cc`/`*.hpp`). The coordinator
  delegates these.

  NON-CODE (allowed): docs (`docs/**`, `*.md`, `*.lex`), config (`*.toml` /
  `*.json` / `*.yaml` / `*.yml`, including `pixi.toml` / `pyproject.toml`), and
  the agent-harness surface itself (`.claude/**`). The coordinator's planning +
  authoring proceed normally.

The known-code set is deliberately wide — a too-narrow default fails OPEN
(silently lets a `bin/` executable or a `*.ts` edit through the guard), which
materially weakens it. Widening the *known-code* set does not flip the default:
an UNKNOWN extension with no code dir/name still falls through to non-code
(allow), converging on the ADR-0007 toolchain map later.

**Bias: when unsure, NON-code (allow).** The guard runs on this repo's own dev
loop, so a misclassification that *blocks* legitimate work is worse than one
that lets an edit through — fail-open matches the dogfooding-safety stance. The
allow-overrides (doc/config extension, `docs/`, `.claude/`) are therefore checked
*before* the code rules, so a `docs/` `.py` or a `.claude/` config never trips
the guard.

Pure: a function of the path string only, no I/O.
"""

from __future__ import annotations

from pathlib import PurePath

#: Directories whose contents are CODE the coordinator may not edit directly.
_CODE_DIRS = frozenset({"src", "tests", "bin"})

#: Bare filenames (no code extension) that are CODE wherever they live.
_CODE_NAMES = frozenset({"Makefile", "Dockerfile"})

#: File extensions that are CODE regardless of location — mainstream languages
#: plus shell. KEEP markup/styling (`*.css`, `*.html`) OUT: those are non-code.
_CODE_EXTS = frozenset(
    {
        ".py",
        ".sh",
        ".js",
        ".mjs",
        ".cjs",
        ".ts",
        ".tsx",
        ".jsx",
        ".rs",
        ".go",
        ".rb",
        ".java",
        ".c",
        ".h",
        ".cpp",
        ".cc",
        ".hpp",
    }
)

#: Directories that are ALWAYS non-code (allowed), even when they contain a file
#: with a code extension — docs and the agent-harness/config surface.
_ALLOW_DIRS = frozenset({"docs", ".claude"})

#: Doc / config extensions — never code, wherever they live (a top-level
#: `pixi.toml`, a `README.md`, a `.lex` fragment, a `tests/` data `.json`).
_ALLOW_EXTS = frozenset({".md", ".lex", ".toml", ".json", ".yaml", ".yml"})


def is_code_path(path: str) -> bool:
    """True iff `path` is a code path the coordinator must delegate (HAR01 default).

    Allow-overrides win first (docs / config / `.claude/`), then the code rules
    (`src/` or `tests/` directory, or a `*.py` / `*.sh` extension); anything else
    falls through to non-code (the conservative, fail-open default). Handles both
    repo-relative (`src/shipit/x.py`) and absolute (`/…/shipit/src/shipit/x.py`)
    forms via path segments.
    """
    if not path:
        return False

    p = PurePath(path)
    parts = set(p.parts)
    suffix = p.suffix.lower()

    # Allow-overrides first: docs/config/.claude are never code, even a `.py`
    # under them (e.g. a generated `.claude/` helper) — fail-open bias.
    if parts & _ALLOW_DIRS:
        return False
    if suffix in _ALLOW_EXTS:
        return False

    # Code rules.
    if parts & _CODE_DIRS:
        return True
    if p.name in _CODE_NAMES:
        return True
    if suffix in _CODE_EXTS:
        return True

    # Unknown → non-code (allow). Converges on the ADR-0007 toolchain map later.
    return False
