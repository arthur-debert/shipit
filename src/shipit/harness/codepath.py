"""``harness/codepath`` — does this path touch code the coordinator must delegate?

Pure, path-string only, and biased to NON-code when unsure.
"""

from __future__ import annotations

from pathlib import PurePath

_CODE_DIRS = frozenset({"src", "tests", "bin"})

_CODE_NAMES = frozenset({"Makefile", "Dockerfile"})

#: Markup and styling (`*.css`, `*.html`) stay OUT: those are non-code.
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

#: Always non-code, even when they contain a file with a code extension.
_ALLOW_DIRS = frozenset({"docs", ".claude"})

_ALLOW_EXTS = frozenset({".md", ".lex", ".toml", ".json", ".yaml", ".yml"})


def is_code_path(path: str) -> bool:
    """Allow-overrides win first, then the code rules; anything unknown is non-code."""
    if not path:
        return False

    p = PurePath(path)
    parts = set(p.parts)
    suffix = p.suffix.lower()

    if parts & _ALLOW_DIRS:
        return False
    if suffix in _ALLOW_EXTS:
        return False

    if parts & _CODE_DIRS:
        return True
    if p.name in _CODE_NAMES:
        return True
    if suffix in _CODE_EXTS:
        return True

    return False
