"""The mechanized argv-guard sweep: tool argv is built ONLY in its adapter.

ADR-0028's structural rule — "tool argv built outside its Tool adapter is a
statable defect" — enforced as an AST sweep over ``src/shipit``: any list/tuple
literal whose first element is a guarded tool's binary name, in any module
outside that tool's whitelisted assembly point(s), fails the build. The gh-only
sweep that pinned the PROC02 merge (formerly in ``test_gh_adapter.py``) is
generalized here into ONE table-driven test: adding the next tool is a
:data:`_ADAPTER_HOMES` entry, not a new test.

The sweep is deliberately literal-shaped (the same net as the original gh
sweep): it cannot see an argv assembled from a variable head, but every
adapter builds its argv as a literal starting with the binary name, so any
out-of-adapter literal — the copy-paste path a regression would actually take —
is caught mechanically.
"""

from __future__ import annotations

import ast
import functools
import pathlib

import pytest

import shipit

_SRC_ROOT = pathlib.Path(shipit.__file__).parent


@functools.cache
def _parsed(path: pathlib.Path) -> ast.Module:
    """Parse ``path`` once across all parametrized runs — the sweep walks the
    whole package once per guarded tool head, and the source never changes
    mid-suite, so each file is read+parsed at most once."""
    return ast.parse(path.read_text(encoding="utf-8"))


#: The guard table: tool binary name → the module(s) allowed to assemble its
#: argv. One entry per guarded tool; extending the guard to the next tool is a
#: new row here, nothing else.
#:
#: - ``gh``   — the one gh adapter (:mod:`shipit.gh`).
#: - ``git``  — the one git adapter (:mod:`shipit.git`, its ``_argv``).
#: - ``pixi`` — the pixi adapter's two sides: execution
#:   (:mod:`shipit.pixienv.run`, ``run_argv``/``install``) and the read verbs'
#:   literals (:mod:`shipit.pixienv.read`).
#: - ``ps``   — the liveness probe's home (:mod:`shipit.session.liveness`,
#:   ``os_probe``): the OS process table has exactly one reader.
#: - ``curl`` — the lexd release fetch (:mod:`shipit.provision.lexd`): the one
#:   external download shipit performs (ADP00-WS03).
#: - ``cargo`` / ``go`` / ``pytest`` / ``npm`` / ``uv`` — the Tool verbs'
#:   default producing commands (TOL01-WS01/WS02): assembled ONLY in the
#:   closed toolchain registry (:mod:`shipit.tools.registry`); a per-path
#:   ``.shipit.toml`` override is consumer DATA, never a second assembly
#:   point. ``npm`` has a second sanctioned home: the Tree provisioner's
#:   frozen node install (``npm ci``, :mod:`shipit.tree.create` #543) —
#:   provisioning-side, a different concern from the producing dispatch.
#: - ``bin/check-e2e`` — the e2e harness registry's bats default
#:   (TOL01-WS03): the script head is assembled ONLY in the closed harness
#:   registry (:mod:`shipit.tools.e2e`); a declared ``e2e.harness`` argv is
#:   consumer DATA, never a second assembly point.
_ADAPTER_HOMES: dict[str, tuple[str, ...]] = {
    "gh": ("gh.py",),
    "git": ("git.py",),
    "pixi": ("pixienv/read.py", "pixienv/run.py"),
    "ps": ("session/liveness.py",),
    "curl": ("provision/lexd.py",),
    "cargo": ("tools/registry.py",),
    "go": ("tools/registry.py",),
    "pytest": ("tools/registry.py",),
    "npm": ("tools/registry.py", "tree/create.py"),
    "uv": ("tools/registry.py",),
    "bin/check-e2e": ("tools/e2e.py",),
    # The act harness (TOL01-WS04): `shipit wf test` is the one place that
    # drives act, and its docker probes/builds live beside it.
    "act": ("verbs/wf.py",),
    "docker": ("verbs/wf.py",),
}


@pytest.mark.parametrize(
    ("head", "homes"), sorted(_ADAPTER_HOMES.items()), ids=sorted(_ADAPTER_HOMES)
)
def test_no_tool_argv_outside_its_adapter(head: str, homes: tuple[str, ...]):
    """ADR-0028: any list/tuple argv literal starting with ``head`` outside the
    tool's whitelisted assembly point(s) is a review defect — the grep-clean
    criterion, pinned mechanically per tool."""
    allowed = {_SRC_ROOT / home for home in homes}
    offenders = []
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        if path in allowed:
            continue
        for node in ast.walk(_parsed(path)):
            if (
                isinstance(node, (ast.List, ast.Tuple))
                and node.elts
                and isinstance(node.elts[0], ast.Constant)
                and node.elts[0].value == head
            ):
                offenders.append(f"{path.relative_to(_SRC_ROOT.parent)}:{node.lineno}")
    assert not offenders, f"{head} argv built outside its adapter:\n" + "\n".join(
        offenders
    )


def test_every_adapter_home_exists():
    """A renamed/moved adapter must move its table row with it — a whitelist
    entry pointing at nothing would silently guard nothing."""
    for homes in _ADAPTER_HOMES.values():
        for home in homes:
            assert (_SRC_ROOT / home).is_file(), f"missing adapter home: {home}"
