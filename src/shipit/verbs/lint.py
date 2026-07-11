"""CLI-facing wrapper for ``shipit lint``.

The lint service lives in :mod:`shipit.lint`; this verb module keeps only the
CLI runtime-error shell and delegates the work.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from .. import execrun
from .. import lint as service
from ._errors import cli_errors


@cli_errors
def run(
    path: str | None = None,
    *,
    fix: bool = False,
    discover: Callable[[Path], list[str]] | None = None,
    run_tool: Callable[[str, list[str], Path], execrun.ExecResult] | None = None,
    tracks_root_editorconfig: Callable[[Path], bool] | None = None,
    canonical_config: Callable[[service.Tool, Path], str | None] | None = None,
    pinned_rust_spec: Callable[[Path], str | None] | None = None,
    runs_out: list[service.ToolRun] | None = None,
) -> int:
    return service.run(
        path,
        fix=fix,
        discover=discover,
        run_tool=run_tool,
        tracks_root_editorconfig=tracks_root_editorconfig,
        canonical_config=canonical_config,
        pinned_rust_spec=pinned_rust_spec,
        runs_out=runs_out,
    )
