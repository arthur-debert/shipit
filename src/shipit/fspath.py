"""The filesystem containment primitives — the shared REFUSE-LINKS predicates
behind every surface that walks a DECLARED path (config- or producer-stated)
into a real tree.

Two surfaces take a path out of a repo's own ``.shipit.toml`` and join it to a
directory shipit then reads or copies from: :mod:`shipit.staging`'s
stage-from-prefix copy (``[stage.<pkg>]`` sources under the env prefix) and
:func:`shipit.release.bundle._compose_declared_payload`'s tar operands
(``bundle.payload`` entries under the toolchain leg). Both need the SAME answer
to the same question — "can this declared path steer the read out of its base
directory?" — so the answer lives once, here, rather than being re-invented per
surface with a different clever check.

The answer is structural, not a denylist: a lexical guard
(:func:`shipit.config.path_escapes`) cannot see a symlink, because the escape is
not in the SPELLING — ``leak/passwd`` is a perfectly well-formed relative path
whose ``leak`` component happens to be a committed ``leak -> /etc``. Resolving
each candidate and re-checking containment is whack-a-mole against symlink, then
junction, then bind-mount. So instead, NO LINK IS EVER FOLLOWED: every component
of a declared path is required to be a REAL directory or file
(:func:`first_link_component`). With no redirect anywhere on the chain, a path
built from real components physically cannot leave its base — containment is
automatic, needing no resolved-path re-check and no cycle guard.

A "link" is a POSIX symlink OR a Windows directory junction / mount-point
reparse point (:func:`is_link`); ``is_symlink`` alone misses the latter.

Policy lives with each caller: these helpers REPORT the offending component and
never raise, so staging raises its :class:`~shipit.staging.StagingError` and the
release lane its :class:`~shipit.release.ReleaseError`, each naming the config
key its own reader has to fix (ADR-0030).
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path


def is_link(path: Path) -> bool:
    """True if ``path``'s final component is a REDIRECT — a POSIX symlink or a
    Windows directory junction / mount-point reparse point.

    Uses lstat/reparse-tag inspection (``is_symlink`` OR ``is_junction`` — the
    latter is what catches an NTFS junction that ``is_symlink`` reports ``False``
    for). Deliberately NOT a ``realpath``-divergence compare: that would
    misclassify a real directory whose ON-DISK CASE differs from the referenced
    name (``Resources`` reached via ``resources`` on a case-insensitive FS, where
    ``os.path.normcase`` is a no-op on darwin) as a redirect. Asking the
    component's own nature is case-agnostic. A non-existent path is not a link.
    """
    return path.is_symlink() or path.is_junction()


def first_link_component(base: Path, parts: Sequence[str]) -> Path | None:
    """The first path under ``base`` — walking ``parts`` ONE COMPONENT AT A TIME
    — that is a link (:func:`is_link`), or ``None`` when the whole chain is real.

    Walking component-by-component is the point: a redirect ANYWHERE along the
    chain is caught, not only at the leaf, so ``leak/passwd`` through a committed
    ``leak -> /etc`` is reported at ``leak``. Callers refuse on a non-``None``
    result; because they then never follow a link, the tree they read is a tree
    of real entries physically inside ``base``.

    ``base`` itself is NOT inspected — it is the caller's own anchor (a resolved
    leg dir, an env prefix), not a declared value, and on darwin it routinely
    sits under a symlinked ancestor such as ``/tmp -> /private/tmp``. Callers
    that also take their anchor from config check it separately.
    """
    current = base
    for part in parts:
        current = current / part
        if is_link(current):
            return current
    return None
