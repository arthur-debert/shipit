"""Wheel build hook: embed the building checkout's git HEAD sha (ADR-0033).

``shipit install`` stamps ``.shipit.toml [shipit].version`` with the FULL git
sha of its own build (:mod:`shipit.buildid`). A ``uv`` git install records that
identity in PEP 610 ``direct_url.json``; this hook covers the remaining
installed shape — a wheel built from a git checkout but installed by path/file
(no vcs record) — by resolving ``git rev-parse HEAD`` at build time and
force-including it into the wheel as ``shipit/data/build-sha``.

Best-effort by design: built outside a git checkout (an unpacked sdist) there
is no identity to embed, so the hook embeds nothing and the runtime resolver
degrades to its next source. Nothing is ever written into the source tree —
the file materializes in a build-scoped temp dir and rides ``force_include``.
"""

from __future__ import annotations

import atexit
import shutil
import subprocess
import tempfile
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class BuildShaHook(BuildHookInterface):
    """Embed ``shipit/data/build-sha`` into the wheel when HEAD is resolvable."""

    PLUGIN_NAME = "build-sha"

    def initialize(self, version: str, build_data: dict) -> None:
        if self.target_name != "wheel":
            return
        try:
            proc = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=self.root,
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            return  # not building from a git checkout — nothing to embed
        sha = proc.stdout.strip()
        if not sha:
            return
        # The file must outlive this method — hatch reads it while assembling the
        # wheel, after initialize() returns — but must not survive the build, so
        # clean the dir up at process exit rather than leak one per wheel build.
        tmpdir = tempfile.mkdtemp(prefix="shipit-build-sha-")
        atexit.register(shutil.rmtree, tmpdir, ignore_errors=True)
        tmp = Path(tmpdir) / "build-sha"
        tmp.write_text(sha + "\n", encoding="utf-8")
        build_data.setdefault("force_include", {})[str(tmp)] = "shipit/data/build-sha"
