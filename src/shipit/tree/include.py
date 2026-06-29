"""``tree/include`` — resolve ``.treeinclude`` (gitignore syntax) to a file list.

A Tree is a fresh, dissociated clone (ADR-0014): it carries every TRACKED file,
but none of the **gitignored-but-needed** files a working session relies on — the
``.env``, the Doppler config, downloaded models. ``.treeinclude`` is a repo-root
allow-list, written in **``.gitignore`` syntax**, of exactly those files;
:func:`resolve` turns it into the concrete set of paths to COPY from the source
checkout into the new Tree (copied, never symlinked — a Tree is self-contained and
disposable, PRD "tree/include.py").

The matching is the deep, pure heart here and is unit-tested directly as a truth
table: patterns are evaluated **relative to the repo root** (a leading ``/``
anchors to the repo root, exactly like ``.gitignore``), support **globs**
(``*`` ``?`` ``**`` and character classes) and **negations** (``!``), and the
**last matching rule wins**. The only effect is :func:`apply` (the copy), kept
thin so everything that decides *which* files move is testable without touching a
real Tree.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

#: The repo-root allow-list file (``.gitignore`` syntax) naming the
#: gitignored-but-needed files a fresh Tree must receive.
TREEINCLUDE_NAME = ".treeinclude"


@dataclass(frozen=True)
class _Rule:
    """One compiled ``.treeinclude`` line.

    ``regex`` matches a repo-root-relative POSIX path (and, because a matched
    directory carries its whole subtree, any path *under* a matched directory).
    ``negated`` is the leading-``!`` re-exclude. ``floating`` (no path separator →
    matches at any depth) and ``segments`` (the anchored pattern split on ``/``)
    drive directory pruning so a huge unrelated subtree is never walked.
    """

    regex: re.Pattern[str]
    negated: bool
    floating: bool
    segments: tuple[str, ...]


class PatternSet:
    """The compiled ``.treeinclude`` rules, in file order (last match wins)."""

    def __init__(self, rules: list[_Rule]) -> None:
        self._rules = rules
        #: Any floating (unanchored) include forces a full walk — a match can sit
        #: at any depth, so no directory can be pruned away up front.
        self._has_floating_include = any(r.floating and not r.negated for r in rules)

    def is_empty(self) -> bool:
        """True when no rule could ever include a file (so :func:`resolve` is a no-op)."""
        return not any(not r.negated for r in self._rules)

    def match(self, relpath: str) -> bool:
        """Whether ``relpath`` (repo-root-relative, POSIX) is included.

        Standard ``.gitignore`` evaluation: walk the rules in order; each matching
        rule sets inclusion to ``True`` (an include) or ``False`` (a ``!`` negation),
        and the LAST match wins.
        """
        included = False
        for rule in self._rules:
            if rule.regex.match(relpath):
                included = not rule.negated
        return included

    def can_descend(self, dir_segments: list[str]) -> bool:
        """Whether the directory ``dir_segments`` could contain any included file.

        Conservative: returns ``True`` whenever an included match is *possible*
        below this directory, so pruning never drops a real match. A directory is
        skipped only when EVERY include is anchored and none of them lines up with
        this directory's path — which is what keeps ``node_modules`` / ``target``
        out of the walk when the patterns are specific.
        """
        if self._has_floating_include:
            return True
        for rule in self._rules:
            if rule.negated:
                continue
            if _segments_prefix_match(rule.segments, dir_segments):
                return True
        return False


def parse(text: str) -> PatternSet:
    """Compile ``.treeinclude`` ``text`` (``.gitignore`` syntax) into a :class:`PatternSet`.

    Blank lines and ``#`` comments are skipped; a leading ``\\#`` / ``\\!`` is a
    literal first character; a leading ``!`` is a negation; a trailing ``/`` marks a
    directory (its whole subtree is included); a leading ``/`` or any internal ``/``
    anchors the pattern to the repo root, otherwise it floats (matches at any depth).
    """
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
    """The repo-root-relative POSIX paths under ``root`` that ``.treeinclude`` selects.

    Reads ``<root>/.treeinclude``; absent or empty → ``[]``. Walks the source tree,
    pruning ``.git`` and any directory the patterns cannot reach (see
    :meth:`PatternSet.can_descend`), and returns the matching FILES, sorted.
    """
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
    """Copy every ``.treeinclude``-selected file from ``src_root`` into ``dest_root``.

    Files are COPIED (parents created as needed), never symlinked, so the Tree is
    self-contained and a plain ``rm -rf`` is a safe delete. Returns the destination
    paths written, in resolution order.
    """
    src = Path(src_root)
    dest = Path(dest_root)
    written: list[Path] = []
    for rel in resolve(src):
        source = src / rel
        if not source.is_file():
            continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        written.append(target)
    return written


# --------------------------------------------------------------------------
# glob → regex (the .gitignore-syntax translator)
# --------------------------------------------------------------------------


def _glob_to_regex(pat: str) -> str:
    """Translate a ``.gitignore`` glob into a regex body over a POSIX path.

    ``*`` matches within a path segment (``[^/]*``); ``?`` a single non-``/`` char;
    a ``**`` path segment matches zero or more segments (``a/**/b`` → ``a/b``,
    ``a/x/b``, …); ``[...]`` is a character class (``[!...]`` negates). Everything
    else is matched literally.
    """
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
                if j < n:  # "**/..." — zero or more leading path segments
                    out.append(r"(?:[^/]+/)*")
                    j += 1  # consume the trailing "/"
                else:  # trailing "/**" — everything below
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
    if end == start + 1:  # no closing bracket → literal "["
        return re.escape("[")
    inner = pat[start + 1 : end - 1]
    if inner.startswith("!"):
        inner = "^" + inner[1:]
    return "[" + inner + "]"


# --------------------------------------------------------------------------
# directory pruning (segment-wise prefix match)
# --------------------------------------------------------------------------


def _segments_prefix_match(pattern_segs: tuple[str, ...], dir_segs: list[str]) -> bool:
    """Whether an anchored pattern could match some path *under* ``dir_segs``.

    Walks the directory's segments against the pattern's: a ``**`` segment (matches
    any number of segments) or running out of pattern (the pattern matched a
    shallower directory, whose subtree is included) means yes; a concrete segment
    mismatch means no; consuming all directory segments leaves the pattern free to
    extend deeper, so yes.
    """
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
    """Whether a single-segment glob ``seg`` matches the directory name ``name``."""
    return re.compile(f"^{_glob_to_regex(seg)}$").match(name) is not None
