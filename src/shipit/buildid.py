"""The running shipit build's OWN commit identity, never an operator-supplied value.
See docs/adr/0033-repo-pins-its-shipit.md
"""

from __future__ import annotations

import json
from importlib import metadata
from pathlib import Path

from . import git
from .identity import Sha

EMBED_RELPATH = "data/build-sha"

DIRECT_URL_NAME = "direct_url.json"


def build_sha() -> Sha | None:
    """This build's full commit sha — the PEP 610 install record, then the build-time embed, then the checkout HEAD; ``None`` when none resolves."""
    return _direct_url_sha() or _embedded_sha() or _checkout_sha()


def sha_from_direct_url(text: str) -> Sha | None:
    """Parse ``vcs_info.commit_id`` out of PEP 610 ``direct_url.json`` text; ``None`` on malformed, non-vcs, or non-full-sha content."""
    try:
        data = json.loads(text)
        commit = data["vcs_info"]["commit_id"]
        return Sha(commit)
    except (ValueError, TypeError, KeyError):
        return None


def _direct_url_sha() -> Sha | None:
    try:
        dist = metadata.distribution("shipit")
    except metadata.PackageNotFoundError:
        return None
    text = dist.read_text(DIRECT_URL_NAME)
    if text is None:
        return None
    return sha_from_direct_url(text)


def _package_dir() -> Path:
    return Path(__file__).resolve().parent


def _embedded_sha() -> Sha | None:
    """The build-time embedded sha at ``shipit/data/build-sha``, or ``None``."""
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
    """The package directory's repo HEAD — the dev-checkout case — or ``None``."""
    return git.head_commit(cwd=str(_package_dir()))
