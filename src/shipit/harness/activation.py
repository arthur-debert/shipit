"""``harness/activation`` — a checkout's toolchain to the coordinator's activation lines."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from ..pixienv import MANIFEST_NAME, Activation

#: The one activatable toolchain kind today; the kind-keyed dispatch is the seam.
PIXI = "pixi"

#: A name failing this cannot be an ``export`` line, so it is dropped, not written broken.
_SHELL_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class Toolchain:
    """An activatable toolchain found in a checkout: its kind + its manifest."""

    kind: str
    manifest: Path


def detect_toolchain(cwd: Path) -> Toolchain | None:
    """The toolchain governing ``cwd``, found by walking up; ``None`` is a normal answer."""
    base = Path(cwd).resolve()
    for directory in (base, *base.parents):
        manifest = directory / MANIFEST_NAME
        if manifest.is_file():
            return Toolchain(kind=PIXI, manifest=manifest)
    return None


def export_lines(activation: Activation) -> str:
    """Render an :class:`Activation` as ``shlex``-quoted, sourceable ``export`` lines."""
    return "\n".join(
        f"export {key}={shlex.quote(value)}"
        for key, value in activation.environment_variables.items()
        if _SHELL_IDENTIFIER.match(key)
    )


def activation_script(
    toolchain: Toolchain | None, activation: Activation | None
) -> str:
    """The toolchain→activation-lines mapping; anything unhandled is the EMPTY script."""
    if toolchain is None or activation is None:
        return ""
    if toolchain.kind != PIXI:
        return ""
    return export_lines(activation)
