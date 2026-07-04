"""provision — pinned external tools delivered by the binary (docs/prd/adoption.md).

The domain home for "put a pinned prebuilt tool into the active pixi env":
tools that gate the required-check path but are not on conda-forge, so they
cannot ride pixi.lock. The pin, platform resolution, and fetch logic live in
the binary — consistent with lint-orchestration-in-binary (ADR-0004) — so a
consumer carries only a thin managed task line, never a repo-local script to
reconcile. One module per tool; :mod:`.lexd` is the first.
"""
