"""The central repository-creation planner (ADR-0057).

One planner composes the universal consumer-owned seed and every selected
profile's :class:`~.profiles.Contribution` into ONE structured
:class:`CreationPlan` — the ordered set of consumer-owned files creation will
write. Shared manifests (``pixi.toml``, ``.shipit.toml``, ``.gitignore``) are
rendered HERE, exactly once, from the merged contributions; profiles never
splice a shared manifest independently. Conflicting claims — two contributors
owning the same path, the same pixi dependency at differing specs, or the same
Artifact name — are detected and raised rather than resolved by a hidden
overlay order.

The plan is a pure value: it can be inspected without any filesystem effect
(the aligned test seam, ``docs/spec/repo-new.md`` §Design Decisions), and the
orchestrator (:mod:`.create`) is the only thing that turns it into files. Text
is templated (:mod:`.templates`); structured data is serialized through the one
format-aware renderer (:mod:`.tomlio`), never templated (ADR-0058). The
``.github`` CI caller is a fully static consumer-owned file (ADR-0060) — no
interpolation, so shipping it verbatim honors the no-templated-YAML rule.
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

# --------------------------------------------------------------------------
# Universal consumer-owned seed — text and static files
# --------------------------------------------------------------------------

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

#: The universal consumer-owned ``.gitignore`` seed (``docs/spec/repo-new.md``
#: §Proposed Shape). Cross-stack caches and OS files; the managed install module
#: keeps its own release-output block, and each profile adds its ecosystem
#: patterns (Rust adds ``/target/``). Lockfiles are never ignored.
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

#: The thin, generic, consumer-owned CI caller (ADR-0060). It delegates to
#: shipit's published reusable checks workflow by floating major ref and owns
#: the per-Repo triggers/permissions/concurrency; ``shipit install`` never
#: reconciles it as a managed unit. Fully static — no interpolation — so it
#: carries no Rust-specific commands and honors the no-templated-YAML rule.
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

#: The generic, consumer-owned build pixi task the universal seed declares,
#: parallel to the managed lint/test entry points (``docs/spec/repo-new.md``
#: §Design Decisions). Repository creation does not broaden the managed task
#: catalog; ``lint``/``test`` still arrive through their managed blocks.
_BUILD_TASK = "./bin/shipit build"

#: The lint lane's CI-provisioned twin of the managed ``lint`` task, declared in
#: the generated ``[feature.lint.tasks]`` — the exact fleet pattern shipit's own
#: repo uses (``lint-full``, "the provisioned twin of the managed `lint` task").
#: WHY a twin and not the managed bare ``lint``: the ``wf-checks`` lane planner
#: (``shipit ci plan``) keys a lane's provisioned pixi env off the FEATURE that
#: declares its ``run`` task. The managed ``lint`` task anchors in the default
#: ``[tasks]`` table (its tooling — shfmt/prettier/markdownlint/actionlint/…
#: lives in ``[feature.lint]``, run locally via the hook's ``pixi run -e lint
#: lint``), so a lane pointed at it provisions the DEFAULT env and the runner
#: never installs the lint tooling — the lane dies 127 on a stock runner (found
#: in GEN01-WS07 QA, #930). Declaring the twin in ``[feature.lint.tasks]``
#: resolves the lane onto the ``lint`` env, where every lint binary (and the
#: managed rust lint toolchain) is provisioned. It is consumer-owned scaffold
#: data, NOT a managed-catalog entry; ``lint``/``test`` still arrive through
#: their managed blocks and the hook keeps using the bare managed ``lint`` task.
_LINT_LANE_TASK = "./bin/shipit lint"

#: The generated Repo's CI policy: the standard, universal, required lint and
#: test lanes, rendered into the consumer-owned ``.shipit.toml [lanes]`` (spec
#: §CI: "the new Repo follows the existing pattern with required lint and test
#: lanes only"). Each is a ``(name, run)`` pair whose ``run`` names the generic
#: shipit verb (ADR-0039) the managed pixi task of that name wraps, so a lane's
#: CI job, a lefthook hook, and a laptop run are ONE implementation. Both lanes
#: are ``required`` (merge-blocking) and ``local`` (so the required∩local
#: commit/push checks derive as lint+test too — one definition for lefthook and
#: CI, ``shipit.tools.lanes.commit_push_checks``). There is deliberately NO
#: ``build`` lane: build stays reachable through the ``build`` task and later
#: artifact/release flows, never as a default PR lane (spec §CI; ADR-0060 —
#: the caller is consumer-owned policy over the existing Lane/Tool model, not a
#: new one). These are stack-neutral universal-seed policy, not a profile
#: contribution: lint and test are generic Tool verbs, not Rust behavior.
#:
#: The lint lane runs ``lint-full`` (NOT the managed bare ``lint`` task) so the
#: planner provisions the ``lint`` env its tooling needs — see
#: :data:`_LINT_LANE_TASK`. The test lane's tooling (cargo-nextest, rust) is in
#: the default env, so it rides the managed ``test`` task directly.
_REQUIRED_LANES: tuple[tuple[str, str], ...] = (
    ("lint", "lint-full"),
    ("test", "test"),
)


@dataclass(frozen=True)
class CreationPlan:
    """The composed, effect-free set of consumer-owned files to write.

    ``name`` is the validated project name; ``files`` is the full ordered set
    of :class:`~.profiles.OwnedFile` (universal seed + every profile). Purely a
    value — inspectable without touching the filesystem.
    """

    name: ProjectName
    files: tuple[OwnedFile, ...]


def _merge_pixi_dependencies(
    contributions: tuple[Contribution, ...],
) -> dict[str, str]:
    """Union the profiles' default-env pixi dependencies, rejecting clashes.

    Two profiles claiming the same dependency at DIFFERING specs is a
    conflicting claim the planner refuses (ADR-0057); an identical repeat is
    harmless and collapses.
    """
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
    """Render the consumer-owned ``pixi.toml`` once (structured, via tomlio).

    The ``[workspace]`` table matches ``shipit install``'s own seed
    (name/channels/platforms) so a re-install is a no-op; ``[dependencies]``
    carries the merged profile deps; ``[tasks]`` carries only the universal
    ``build`` task (managed blocks splice in ``lint``/``test``/… beneath).
    ``[feature.lint.tasks]`` carries the ``lint-full`` twin (:data:`_LINT_LANE_TASK`)
    the lint CI lane runs so it provisions the ``lint`` env; the managed lint
    dependency/environment blocks splice in beneath (``_insert_under_anchor``
    creates ``[feature.lint.dependencies]``/``[environments]`` at EOF, leaving
    this ``[feature.lint.tasks]`` table intact).
    """
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
            "feature": {"lint": {"tasks": {"lint-full": _LINT_LANE_TASK}}},
        }
    )
    return header + body


def _shipit_manifest(contributions: tuple[Contribution, ...]) -> str:
    """Render the consumer-owned ``.shipit.toml`` CI-policy and Artifact tables.

    ``[lanes]`` carries the universal CI policy — the standard required lint and
    test lanes, no build lane (:data:`_REQUIRED_LANES`) — so the generated Repo
    plugs into the existing Lane/Tool model and its thin ``wf-checks`` caller
    fans out real merge-blocking checks. ``[artifacts]`` carries each profile's
    Artifact as one ``[artifacts.<name>]`` table with a single Rust build target
    (ADR-0057). ``shipit install`` later seeds the remaining policy tables
    (``[toolchains]`` from the detected Cargo manifest, etc.) and stamps
    ``[managed]`` alongside, preserving these consumer-owned tables.
    """
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
    """The universal ``.gitignore`` seed plus each profile's ecosystem lines."""
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
    """Compose the universal seed and profile contributions into one plan.

    ``author`` and ``year`` fill the MIT ``LICENSE`` copyright line (the
    resolved Git author, the local creation year — ``docs/spec/repo-new.md``).
    Raises :class:`CreationError` on any conflicting claim (a duplicated owned
    path, a pixi-dependency clash, or a duplicated Artifact name).
    """
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
