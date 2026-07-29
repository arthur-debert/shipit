"""``repocreate/plan`` — compose the universal seed and every profile into one effect-free plan.

See docs/adr/0057-creation-profiles-contribute-to-one-plan.md.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..install.units import (
    PIXI_SEED_CHANNELS,
    PIXI_SEED_PLATFORMS,
    workspace_name,
)
from . import tomlio
from .errors import CreationError
from .names import ProjectName
from .profiles import Contribution, OwnedFile, RustProfile
from .templates import render_text

_README = """\
# {{ name }}

A new shipit-managed project.

## Development

This project uses [pixi](https://pixi.sh) for provisioning and shipit for its
managed development baseline. The public entry points are:

- `pixi run lint` — run the lint gate
- `pixi run test` — run the tests
- `pixi run build` — build the project
"""

_LICENSE = """\
MIT License

Copyright (c) {{ year }} {{ author }}

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

#: Cross-stack caches and OS files; each profile adds its own ecosystem patterns.
_GITIGNORE_SEED = """\
.DS_Store
*.swp
*~
.pixi/
.direnv/
.env
.env.*
!.env.example
.claude/worktrees/
.todos.db
node_modules/
.npm/
.pnpm-store/
coverage/
__pycache__/
*.py[cod]
.venv/
*.egg-info/
.pytest_cache/
.mypy_cache/
.ruff_cache/
.coverage
htmlcov/
"""

#: The consumer-owned CI caller; ``shipit install`` never reconciles it as a managed unit.
_CI_CALLER = """\
name: CI

# Thin generic caller of shipit's reusable checks workflow (ADR-0060): this
# Repo owns its triggers, permissions, and concurrency; shipit owns and
# versions the reusable implementation behind the pinned @v1 major ref.

on:
  push:
    branches: ['**']
  pull_request:

concurrency:
  group: ci-${{ github.event_name }}-${{ github.event.pull_request.number || github.ref }}
  cancel-in-progress: true

permissions:
  contents: read

jobs:
  checks:
    uses: arthur-debert/shipit/.github/workflows/wf-checks.yml@v1
  check:
    needs: checks
    if: always() && needs.checks.result != 'cancelled'
    runs-on: ubuntu-latest
    steps:
      - name: Verdict
        env:
          RESULT: ${{ needs.checks.result }}
        run: |
          echo "wf-checks: $RESULT"
          test "$RESULT" = "success"
"""

#: The consumer-owned build task; ``lint``/``test`` arrive through managed blocks instead.
_BUILD_TASK = "./bin/shipit build"

#: The generated Repo's CI policy as ``(name, run)`` pairs — both required and local,
#: and deliberately no ``build`` lane.
_REQUIRED_LANES: tuple[tuple[str, str], ...] = (
    ("lint", "lint"),
    ("test", "test"),
)


@dataclass(frozen=True)
class CreationPlan:
    """The composed, effect-free ordered set of consumer-owned files to write."""

    name: ProjectName
    files: tuple[OwnedFile, ...]


def _merge_pixi_dependencies(
    contributions: tuple[Contribution, ...],
) -> dict[str, str]:
    """Union the profiles' pixi dependencies; the same dep at DIFFERING specs raises."""
    deps: dict[str, str] = {}
    for contribution in contributions:
        for dep, spec in contribution.pixi_dependencies:
            if dep in deps and deps[dep] != spec:
                raise CreationError(
                    f"conflicting pixi dependency {dep!r}: {deps[dep]!r} vs {spec!r}"
                )
            deps[dep] = spec
    return deps


def _pixi_manifest(name: ProjectName, deps: dict[str, str]) -> str:
    """Render the consumer-owned ``pixi.toml``; its ``[workspace]`` matches install's own seed."""
    header = (
        "# pixi workspace — the consumer-owned provisioning manifest for this\n"
        "# Repo. shipit's managed blocks splice in beneath these tables.\n"
    )
    body = tomlio.dumps(
        {
            "workspace": {
                "name": workspace_name(name.value),
                "channels": list(PIXI_SEED_CHANNELS),
                "platforms": list(PIXI_SEED_PLATFORMS),
            },
            "dependencies": dict(deps),
            "tasks": {"build": _BUILD_TASK},
        }
    )
    return header + body


def _shipit_manifest(contributions: tuple[Contribution, ...]) -> str:
    """Render the consumer-owned ``.shipit.toml`` ``[lanes]`` and ``[artifacts]`` tables."""
    lanes: dict[str, object] = {
        name: {"run": run, "required": True, "local": True}
        for name, run in _REQUIRED_LANES
    }
    artifacts: dict[str, object] = {}
    for contribution in contributions:
        for artifact in contribution.artifacts:
            if artifact.name in artifacts:
                raise CreationError(f"duplicate artifact declaration {artifact.name!r}")
            artifacts[artifact.name] = {
                "build": [
                    tomlio.Inline(
                        {"toolchain": artifact.toolchain, "package": artifact.package}
                    )
                ]
            }
    header = (
        "# shipit policy config. Owns the required lint/test CI lanes and the\n"
        "# primary product declaration for build; `shipit install` seeds the\n"
        "# policy/pristine tables alongside.\n"
    )
    return header + tomlio.dumps({"lanes": lanes, "artifacts": artifacts})


def _gitignore(contributions: tuple[Contribution, ...]) -> str:
    lines: list[str] = []
    for contribution in contributions:
        for line in contribution.gitignore_lines:
            if line not in lines:
                lines.append(line)
    extra = ("\n" + "\n".join(lines) + "\n") if lines else ""
    return _GITIGNORE_SEED + extra


def build_plan(
    name: ProjectName,
    profiles: tuple[RustProfile, ...],
    *,
    author: str,
    year: int,
) -> CreationPlan:
    """Compose the universal seed and profile contributions; any conflicting claim raises."""
    contributions = tuple(profile.contribute(name) for profile in profiles)

    universal = (
        OwnedFile("README.md", render_text(_README, {"name": name.value})),
        OwnedFile(
            "LICENSE",
            render_text(_LICENSE, {"year": str(year), "author": author}),
        ),
        OwnedFile(".gitignore", _gitignore(contributions)),
        OwnedFile(".github/workflows/ci.yml", _CI_CALLER),
        OwnedFile(
            "pixi.toml", _pixi_manifest(name, _merge_pixi_dependencies(contributions))
        ),
        OwnedFile(".shipit.toml", _shipit_manifest(contributions)),
    )

    files: list[OwnedFile] = []
    seen: dict[str, str] = {}
    for owned in (*universal, *(f for c in contributions for f in c.owned_files)):
        origin = "universal seed" if owned in universal else "a Creation profile"
        if owned.path in seen:
            raise CreationError(
                f"conflicting owned file {owned.path!r} claimed by "
                f"{seen[owned.path]} and {origin}"
            )
        seen[owned.path] = origin
        files.append(owned)

    return CreationPlan(name=name, files=tuple(files))
