"""The missing-tool → reconcile-remedy translation (#801, TOL02-WS17 holes 1–3).

The release verbs shell out through the one Exec seam (ADR-0028), and a tool
absent from the runner surfaces as :class:`~shipit.execrun.ExecError` with
``cause=CAUSE_MISSING_BINARY`` — a raw 127-shaped death that tells the
operator nothing about WHY the tool was supposed to be there. For every tool
the shipit-managed pixi surface provisions, that failure has exactly one
correct remediation: the consumer's install reconcile (the #582 doctrine —
provisioning rides setup-pixi's lockfile-keyed cache, a release run NEVER
installs at run time). This module is the ONE map from an argv head to that
remediation, shared by the prepare-side bump loop and the publish-side
dispatch loop (:mod:`shipit.verbs.release`), so the two stages can never
explain the same gap two ways.

The probe is the ATTEMPT itself — no ``shutil.which`` pre-gate (issue #785's
resolution finding, generalized: the attempt fails with the missing-binary
cause exactly when PATH genuinely lacks the tool). The cargo-edit shape (#793
— ``cargo`` PRESENT, its ``set-version`` subcommand absent) is NOT this map's:
it dies as a normal nonzero exit, translated by
:func:`shipit.release.bump.explain_command_failure`.

Pure: a dict lookup and string assembly, no I/O.
"""

from __future__ import annotations

from collections.abc import Sequence

from .. import execrun

#: argv head → (what the head needed, the managed pixi block that delivers it,
#: the toolchain signal that block rides). One row per tool the managed pixi
#: surface provisions for the release stages — the pixi-managed rows of the
#: TOL02-WS17 inventory (docs/dev/release-tool-provisioning.md), kept in
#: lockstep with the drift guard (tests/test_tool_provisioning_guard.py).
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
}


def missing_tool_remedy(argv: Sequence[str], cause: str) -> str | None:
    """The reconcile remediation for a KNOWN pixi-managed tool dying absent. Pure.

    ``None`` means "not this failure shape" — either the Exec did not die on a
    missing binary (``cause`` is the caller's :attr:`ExecError.cause
    <shipit.execrun.ExecError>`) or the head is not a tool the managed pixi
    surface owns; the caller re-raises the original error untranslated. The
    message mirrors the #793 cargo-edit remedy: name the gap, name the managed
    block, and name the COMMITTING install forms (only ``--pr``/``--local``
    regenerate and stage pixi.lock alongside the block, so the committed lock
    stays coherent) — never a run-time install (the #582 doctrine).
    """
    if cause != execrun.CAUSE_MISSING_BINARY or not argv:
        return None
    entry = _MANAGED_TOOLS.get(argv[0])
    if entry is None:
        return None
    need, block, signal = entry
    return (
        f"`{argv[0]}` is not provisioned on this runner — this stage needs "
        f"{need}. It rides the shipit-managed pixi surface for {signal} "
        f"repos (the `{block}` block, pinned from conda-forge) and is never "
        f"installed at release run time — this repo's shipit pin/managed set "
        f"is stale. Reconcile with a COMMITTING install (`shipit install "
        f"--pr` opens the reconcile draft PR; `shipit install --local` "
        f"commits on the current branch) — only these regenerate and stage "
        f"pixi.lock alongside the pixi.toml block, so the committed lock "
        f"stays coherent; plain `shipit install` only refreshes the working "
        f"tree and leaves the lock stale. Merge/commit the reconcile, then "
        f"re-run the release."
    )
