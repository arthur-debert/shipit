"""``tree/include`` — resolve ``.treeinclude`` (gitignore syntax) to a file list.

The repo-root allow-list of gitignored-but-needed files a fresh Tree must be given.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

TREEINCLUDE_NAME = ".treeinclude"


@dataclass(frozen=True)
class _Rule:
    """One compiled line; ``floating`` means unanchored, so it matches at any depth."""

    regex: re.Pattern[str]
    negated: bool
    floating: bool
    segments: tuple[str, ...]


class PatternSet:
    """The compiled ``.treeinclude`` rules, in file order (last match wins)."""

    def __init__(self, rules: list[_Rule]) -> None:
        self._rules = rules
        self._has_floating_include = any(r.floating and not r.negated for r in rules)

    def is_empty(self) -> bool:
        """True when no rule could ever include a file, so :func:`resolve` is a no-op."""
        return not any(not r.negated for r in self._rules)

    def match(self, relpath: str) -> bool:
        """Whether ``relpath`` (repo-root-relative, POSIX) is included."""
        included = False
        for rule in self._rules:
            if rule.regex.match(relpath):
                included = not rule.negated
        return included

    def can_descend(self, dir_segments: list[str]) -> bool:
        """Whether an included file could sit below — conservative, never prunes a match."""
        if self._has_floating_include:
            return True
        for rule in self._rules:
            if rule.negated:
                continue
            if _segments_prefix_match(rule.segments, dir_segments):
                return True
        return False


def parse(text: str) -> PatternSet:
    """Compile ``.treeinclude`` ``text`` (``.gitignore`` syntax) into a :class:`PatternSet`."""
    rules: list[_Rule] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        negated = False
        if line.startswith("!"):
            negated = True
            line = line[1:]
        elif line.startswith(("\\#", "\\!")):
            line = line[1:]
        dir_only = line.endswith("/")
        core = line.rstrip("/")
        if not core:
            continue
        anchored = "/" in core
        core = core.lstrip("/")
        body = _glob_to_regex(core)
        prefix = "" if anchored else r"(?:.*/)?"
        tail = r"/.*" if dir_only else r"(?:/.*)?"
        regex = re.compile(f"^{prefix}{body}{tail}$")
        rules.append(
            _Rule(
                regex=regex,
                negated=negated,
                floating=not anchored,
                segments=tuple(core.split("/")),
            )
        )
    return PatternSet(rules)


def resolve(root: str | os.PathLike[str]) -> list[str]:
    """The repo-root-relative POSIX file paths ``.treeinclude`` selects, sorted."""
    root_path = Path(root)
    spec_file = root_path / TREEINCLUDE_NAME
    if not spec_file.is_file():
        return []
    patterns = parse(spec_file.read_text(encoding="utf-8"))
    if patterns.is_empty():
        return []

    matched: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root_path):
        rel_dir = os.path.relpath(dirpath, root_path)
        rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
        kept: list[str] = []
        for name in dirnames:
            if name == ".git":
                continue
            child = name if not rel_dir else f"{rel_dir}/{name}"
            if patterns.can_descend(child.split("/")):
                kept.append(name)
        dirnames[:] = sorted(kept)
        for name in filenames:
            rel = name if not rel_dir else f"{rel_dir}/{name}"
            if patterns.match(rel):
                matched.append(rel)
    return sorted(matched)


def apply(
    src_root: str | os.PathLike[str], dest_root: str | os.PathLike[str]
) -> list[Path]:
    """Copy the selected files into ``dest_root``; one already there is left untouched."""
    src = Path(src_root)
    dest = Path(dest_root)
    written: list[Path] = []
    for rel in resolve(src):
        source = src / rel
        if not source.is_file():
            continue
        target = dest / rel
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        written.append(target)
    return written


def _glob_to_regex(pat: str) -> str:
    """Translate a ``.gitignore`` glob into a regex body over a POSIX path."""
    out: list[str] = []
    i, n = 0, len(pat)
    while i < n:
        c = pat[i]
        if c == "*":
            j = i
            while j < n and pat[j] == "*":
                j += 1
            double = (j - i) >= 2
            prev_slash = i == 0 or pat[i - 1] == "/"
            next_slash = j >= n or pat[j] == "/"
            if double and prev_slash and next_slash:
                if j < n:
                    out.append(r"(?:[^/]+/)*")
                    j += 1  # consume the trailing "/"
                else:
                    out.append(r".*")
            else:
                out.append(r"[^/]*")
            i = j
        elif c == "?":
            out.append(r"[^/]")
            i += 1
        elif c == "/":
            out.append("/")
            i += 1
        elif c == "[":
            out.append(_char_class(pat, i))
            i = _char_class_end(pat, i)
        else:
            out.append(re.escape(c))
            i += 1
    return "".join(out)


def _char_class_end(pat: str, start: int) -> int:
    """Index just past the ``]`` closing the class opened at ``start`` (else ``start+1``)."""
    k = start + 1
    if k < len(pat) and pat[k] in "!^":
        k += 1
    if k < len(pat) and pat[k] == "]":
        k += 1
    while k < len(pat) and pat[k] != "]":
        k += 1
    return k + 1 if k < len(pat) else start + 1


def _char_class(pat: str, start: int) -> str:
    """The regex for the ``[...]`` class at ``start`` (a literal ``[`` if unterminated)."""
    end = _char_class_end(pat, start)
    if end == start + 1:
        return re.escape("[")
    inner = pat[start + 1 : end - 1]
    if inner.startswith("!"):
        inner = "^" + inner[1:]
    return "[" + inner + "]"


def _segments_prefix_match(pattern_segs: tuple[str, ...], dir_segs: list[str]) -> bool:
    """Whether an anchored pattern could match some path *under* ``dir_segs``."""
    pi = 0
    for dseg in dir_segs:
        if pi >= len(pattern_segs):
            return True
        seg = pattern_segs[pi]
        if seg == "**":
            return True
        if not _segment_matches(seg, dseg):
            return False
        pi += 1
    return True


def _segment_matches(seg: str, name: str) -> bool:
    return re.compile(f"^{_glob_to_regex(seg)}$").match(name) is not None
