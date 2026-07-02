"""``harness/activation`` — toolchain-aware coordinator activation (ADR-0027).

The pure core behind ``shipit hook sessionstart``: map the repo's toolchain(s) to
the activation *lines* that make the coordinator's environment active for every
Bash tool call. The hook writes these lines into the file named by
``CLAUDE_ENV_FILE``, which Claude Code sources as a preamble before each Bash
command — so the coordinator's own Tree resolves ``shipit``/``python`` without a
``pixi run`` prefix. This is the coordinator-side twin of ADR-0019's
:func:`shipit.spawn.launch.pixi_wrap` (which routes *spawned* children through
pixi); both are ADDITIVE — the committed ``pixi run shipit hook …`` lines keep
their prefix, so hook correctness never depends on activation having succeeded.

The mapping is **toolchain-aware, extensible per toolchain** (ADR-0027): a pixi
repo activates the ``default`` env via ``pixi shell-hook``'s output; a repo with
no activatable toolchain maps to the EMPTY script — a graceful no-op, never an
error where there is no ``pixi.toml``.

**Why exports rendered from ``shell-hook --json``, not the raw script** (issue
#217 acceptance: "verified suitable as a sourced preamble"): the plain
``pixi shell-hook`` script was probed (pixi 0.63 / live repo) and is NOT pure
exports — it ends with a ``pixi()`` shell *function* wrapper (an interactive
convenience that re-evals activation after ``pixi add/install``), exactly the
kind of thing a sourced preamble must not carry. The ``--json`` variant is the
pixi-blessed pure form: the COMPLETE env-var snapshot activation produces
(:class:`shipit.pixienv.Activation`, ADR-0022 — borrowed from pixi's own
computation, never hand-derived), which :func:`export_lines` renders as plain
``export KEY='value'`` lines. Pure exports by construction.

**Why ``Activation.activation_scripts`` is deliberately not rendered**: pixi
EXECUTES activation scripts (``[activation] scripts`` and conda packages') as
part of computing ``shell-hook --json``, so their env-var effects are already
folded into ``environment_variables`` — probed live (pixi 0.71): a script's
``export SCRIPT_VAR=…`` shows up in the JSON env map, with ``activation_scripts``
merely listing the paths that were run. Rendering the exports therefore matches
the env ``pixi run`` produces; sourcing the scripts a second time from the
preamble would double-apply them (and re-admit the non-preamble-safe constructs
the ``--json`` form exists to avoid).

Everything here is pure (the manifest probe is a filesystem ``is_file`` only,
table-testable against a tmp dir — the same discipline as ``pixi_wrap``'s gate);
the I/O — read stdin, shell out to ``pixi shell-hook --json``, write the env
file — lives in :mod:`shipit.verbs.hook.sessionstart`.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from ..pixienv import Activation
from ..tree.create import PIXI_MANIFEST

#: The one activatable toolchain kind today. The :class:`Toolchain` value object +
#: the kind-keyed dispatch in :func:`activation_script` are the extension seam a
#: future toolchain (npm, cargo, …) plugs into.
PIXI = "pixi"

#: Env vars whose names do not parse as shell identifiers cannot be rendered as an
#: ``export`` line; they are dropped rather than written broken into a sourced file.
_SHELL_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class Toolchain:
    """An activatable toolchain found in a checkout: its kind + its manifest."""

    kind: str
    manifest: Path


def detect_toolchain(cwd: Path) -> Toolchain | None:
    """The activatable toolchain governing ``cwd``, or ``None`` (→ graceful no-op).

    Mirrors pixi's own manifest discovery: walk UP from ``cwd`` (the session's
    working dir — the coordinator's session Tree once SES02 lands, the plain
    checkout today) to the filesystem root, and the first ``pixi.toml`` found is
    the manifest. ``None`` — a repo with no activatable toolchain — is a normal
    answer, never an error (ADR-0027).
    """
    base = Path(cwd).resolve()
    for directory in (base, *base.parents):
        manifest = directory / PIXI_MANIFEST
        if manifest.is_file():
            return Toolchain(kind=PIXI, manifest=manifest)
    return None


def export_lines(activation: Activation) -> str:
    """Render an :class:`Activation` snapshot as sourceable ``export`` lines.

    One ``export KEY=<quoted value>`` line per env var pixi's activation sets,
    in pixi's own order, values ``shlex``-quoted so an embedded space/quote/``$``
    survives the sourcing verbatim. A key that is not a valid shell identifier is
    skipped (it cannot be exported and must not corrupt the preamble). Empty
    ``environment_variables`` → the empty string.
    """
    return "\n".join(
        f"export {key}={shlex.quote(value)}"
        for key, value in activation.environment_variables.items()
        if _SHELL_IDENTIFIER.match(key)
    )


def activation_script(
    toolchain: Toolchain | None, activation: Activation | None
) -> str:
    """The toolchain→activation-lines mapping — THE pure core of the hook.

    - pixi (+ its captured :class:`Activation`) → the export lines that activate
      the env (:func:`export_lines`).
    - no toolchain, an unknown kind, or no captured activation → the EMPTY script:
      the caller writes nothing, the hook is a clean no-op.
    """
    if toolchain is None or activation is None:
        return ""
    if toolchain.kind != PIXI:
        return ""
    return export_lines(activation)
