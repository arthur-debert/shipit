"""``buildid`` — the running shipit build's OWN commit identity (ADR-0033).

``shipit install`` stamps ``.shipit.toml [shipit].version`` with the FULL git
sha of the build performing the install — the **Shipit pin** the managed
``bin/shipit`` launcher later resolves and execs. The pin must be the build's
own identity, never an operator-supplied value, so the pinned build is provably
the build that wrote the managed files (the pre-ADR-0033 code stamped the
static package version ``0.0.1``, which identifies nothing — the bug that ADR
retires). :func:`build_sha` is the one resolver, trying the three places a
build's commit identity can live, most authoritative first:

1. **The install record** — a ``uv``/pip git install writes PEP 610
   ``direct_url.json`` into the dist-info with ``vcs_info.commit_id``: the
   exact commit the installer resolved and built. Checked FIRST because it is
   immune to ambient-git confusion: a build installed into an env that happens
   to live inside some OTHER repo (a consumer's ``.pixi/envs``) must never
   stamp that repo's HEAD.
2. **The build-time embed** — ``hatch_build.py`` resolves ``git rev-parse
   HEAD`` when the wheel is built from a git checkout and embeds it as
   ``shipit/data/build-sha``; covers a wheel installed by path (no
   ``direct_url.json`` vcs record).
3. **The source checkout** — running from a git checkout of shipit itself
   (the dev/editable install, or a ``SHIPIT_EXEC`` build), the package
   directory's repo HEAD is the identity. Last because it is the only ambient
   probe.

``None`` when all three come up empty — the CALLER decides the posture;
``shipit install`` fails CLOSED on it (stamping nothing identifies nothing).
"""

from __future__ import annotations

import json
from importlib import metadata
from pathlib import Path

from . import git
from .identity import Sha

#: The wheel-embedded identity file ``hatch_build.py`` writes at build time,
#: relative to the package directory (rides ``shipit/data`` like every other
#: packaged data file). Absent in a source checkout — the checkout's git HEAD
#: is the identity there, and an embedded stamp would only go stale.
EMBED_RELPATH = "data/build-sha"

#: The PEP 610 install record's name inside the dist-info directory.
DIRECT_URL_NAME = "direct_url.json"


def build_sha() -> Sha | None:
    """This build's full commit sha, or ``None`` when no identity resolves."""
    return _direct_url_sha() or _embedded_sha() or _checkout_sha()


def sha_from_direct_url(text: str) -> Sha | None:
    """Parse ``vcs_info.commit_id`` out of PEP 610 ``direct_url.json`` text.

    Pure over the record's text so the parse is table-testable. ``None`` on
    malformed JSON, a non-vcs install (a plain ``url`` or an editable
    ``dir_info`` record carries no commit), or a commit id that does not
    validate as a full sha — degrading, never raising: the next resolver in
    :func:`build_sha` gets its turn.
    """
    try:
        data = json.loads(text)
        commit = data["vcs_info"]["commit_id"]
        return Sha(commit)
    except (ValueError, TypeError, KeyError):
        return None


def _direct_url_sha() -> Sha | None:
    """The installed distribution's PEP 610 vcs commit, or ``None``."""
    try:
        dist = metadata.distribution("shipit")
    except metadata.PackageNotFoundError:
        return None
    text = dist.read_text(DIRECT_URL_NAME)
    if text is None:
        return None
    return sha_from_direct_url(text)


def _package_dir() -> Path:
    """The installed ``shipit`` package directory (this module's parent)."""
    return Path(__file__).resolve().parent


def _embedded_sha() -> Sha | None:
    """The build-time embedded sha (``shipit/data/build-sha``), or ``None``."""
    path = _package_dir() / EMBED_RELPATH
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        return Sha(raw)
    except ValueError:
        return None


def _checkout_sha() -> Sha | None:
    """The package directory's repo HEAD (the dev-checkout case), or ``None``."""
    return git.head_commit(cwd=str(_package_dir()))
