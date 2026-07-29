"""The install domain's refusal types."""

from __future__ import annotations


class InstallError(RuntimeError):
    """A runtime refusal from the install domain — one ``error: …`` line + exit 1 at the CLI."""


class SelfCertError(InstallError):
    """Install's self-certification failed: no commit, no PR; the message names every missed check."""

    step: str = "self-certification"
