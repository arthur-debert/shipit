"""The missing-tool -> reconcile-remedy translation, shared by the release verbs.

The probe is the ATTEMPT itself: no ``which`` pre-gate. Pure.
"""

from __future__ import annotations

from collections.abc import Sequence

from .. import execrun

#: argv head → (what it needed, the managed pixi block delivering it, the
#: PROVISIONING SIGNAL that block rides — a toolchain, or a distribution
#: ENDPOINT for a block gated on where the artifact ships).
_MANAGED_TOOLS: dict[str, tuple[str, str, str]] = {
    "cargo": (
        "the rust toolchain (conda-forge `rust` carries cargo)",
        "pixi.toml#shipit-rust-release-toolchain",
        "rust",
    ),
    "npm": (
        "the node runtime (npm rides the `nodejs` package)",
        "pixi.toml#shipit-node-deps",
        "node",
    ),
    "twine": (
        "twine (the pypi endpoint's uploader)",
        "pixi.toml#shipit-python-release-deps",
        "python",
    ),
    "tree-sitter": (
        "the tree-sitter CLI (conda-forge `tree-sitter-cli` — `tree-sitter "
        "generate` at build, the corpus `tree-sitter test` lane)",
        "pixi.toml#shipit-tree-sitter-release-deps",
        "tree-sitter",
    ),
    "rattler-build": (
        "rattler-build (the conda endpoint's packager — repackages a final "
        "release binary into a `.conda` and pushes+reindexes the Artifact "
        "channel)",
        "pixi.toml#shipit-conda-packager",
        "conda",
    ),
}


def missing_tool_remedy(argv: Sequence[str], cause: str) -> str | None:
    """The reconcile remediation for a KNOWN pixi-managed tool dying absent, else ``None``."""
    if cause != execrun.CAUSE_MISSING_BINARY or not argv:
        return None
    entry = _MANAGED_TOOLS.get(argv[0])
    if entry is None:
        return None
    need, block, signal = entry
    return (
        f"`{argv[0]}` is not provisioned on this runner — this stage needs "
        f"{need}. It rides the shipit-managed pixi surface for {signal} "
        f"producers (the `{block}` block, pinned from conda-forge) and is never "
        f"installed at release run time — this repo's shipit pin/managed set "
        f"is stale. Reconcile with a COMMITTING install (`shipit install "
        f"--pr` opens the reconcile draft PR; `shipit install --local` "
        f"commits on the current branch) — only these regenerate and stage "
        f"pixi.lock alongside the pixi.toml block, so the committed lock "
        f"stays coherent; plain `shipit install` only refreshes the working "
        f"tree and leaves the lock stale. Merge/commit the reconcile, then "
        f"re-run the release."
    )
