"""`shipit channel` — the Artifact channel's consumer-facing verbs (ARF01).

ADR-0030 glue + renderer over the channel domain (:mod:`shipit.channel`). The
one subcommand today is ``channel receive`` — the consumer-side end of the
artifact-pinned Cascade (ARF01-WS07 #956): the managed receive-workflow invokes
it when an upstream this repo pins publishes a release, and it bumps the
matching ``[artifact-deps]`` pins, re-renders the managed pixi block, and opens
a draft bump PR. The decision core (payload parse + surgical bump) lives in
:mod:`shipit.channel.cascade_receive`; this module is click glue and a
renderer, with refusals mapped by the one :func:`~._errors.cli_errors` shell.
"""

from __future__ import annotations

from pathlib import Path

import click

from ..channel import cascade_receive
from ._errors import cli_errors
from ._render import emit


@click.group(name="channel")
def channel() -> None:
    """Consume another repo's released artifacts as version-pinned deps.

    The Artifact channel projects a `.shipit.toml [artifact-deps]` declaration
    into a managed pixi block so pixi resolves/locks/fetches a cross-repo
    artifact like any dependency; a producer release cascades a version bump
    into a draft PR here.
    """


@channel.command(name="receive")
@click.option(
    "--upstream",
    required=True,
    help="The producing repo's `owner/name` slug (the dispatch payload's "
    "`upstream`); every `[artifact-deps]` entry whose `repo` matches it bumps.",
)
@click.option(
    "--version",
    "version",
    required=True,
    help="The released semver every matching pin bumps to (the dispatch "
    "payload's `version`).",
)
@click.option(
    "--path",
    "path",
    type=click.Path(exists=True, file_okay=False),
    default=None,
    help="The consumer repo root (defaults to the current directory).",
)
def receive_cmd(upstream: str, version: str, path: str | None) -> None:
    """Apply an `upstream-release` Cascade: bump the matching pins, open a draft PR.

    Invoked by the managed receive-workflow on a `repository_dispatch`. Bumps
    every `[artifact-deps]` entry whose `repo` matches `--upstream` to
    `--version`, re-renders the managed pixi block, and opens a DRAFT bump PR
    that rides the normal review loop. An unknown upstream (nothing matches) or
    an already-current version is a clean no-op — no branch, no PR, and
    `.shipit.toml` is left untouched.
    """
    raise SystemExit(run_receive(upstream=upstream, version=version, path=path))


@cli_errors
def run_receive(*, upstream: str, version: str, path: str | None = None) -> int:
    """Parse → bump → render. Returns an exit code (refusals map to 1 via the
    shell). A malformed payload or an unrewritable entry raises
    :class:`~shipit.channel.cascade_receive.CascadeError`, mapped to ``error: …``
    + exit 1; ``.shipit.toml`` is never left half-edited."""
    root = Path(path or ".").resolve()
    result = cascade_receive.receive(root, upstream, version)
    emit(result, format_receive)
    return 0


def format_receive(result: cascade_receive.ReceiveResult) -> str:
    """The pure text renderer — the no-op line, or the bump summary + PR URL."""
    if not result.bumped:
        return (
            "channel receive: no `[artifact-deps]` entry matched — nothing to "
            "bump (.shipit.toml untouched)."
        )
    lines = ["channel receive: bumped"]
    lines += [
        f"  {b.package}: {b.old_version} -> {b.new_version}" for b in result.bumped
    ]
    if result.url:
        lines.append(f"  opened draft PR: {result.url}")
    return "\n".join(lines)
