from __future__ import annotations

import ast
import functools
import pathlib

import pytest

import shipit

_SRC_ROOT = pathlib.Path(shipit.__file__).parent


@functools.cache
def _parsed(path: pathlib.Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


_ADAPTER_HOMES: dict[str, tuple[str, ...]] = {
    "gh": ("gh.py",),
    "git": ("git.py",),
    "pixi": ("pixienv/read.py", "pixienv/run.py"),
    "gcloud": ("channel/store_provision.py",),
    "cargo": (
        "tools/registry.py",
        "release/bump.py",
        "release/bundle.py",
        "release/publish.py",
    ),
    "go": ("tools/registry.py",),
    "pytest": ("tools/registry.py",),
    "busted": ("tools/registry.py",),
    "tree-sitter": ("tools/registry.py",),
    "npm": (
        "tools/registry.py",
        "tree/create.py",
        "release/bump.py",
        "release/bundle.py",
        "release/publish.py",
        "tools/e2e.py",
    ),
    "wasm-pack": ("release/bundle.py",),
    "uv": ("tools/registry.py", "release/bundle.py"),
    "tar": ("release/bundle.py", "release/sign.py"),
    "zip": ("release/bundle.py", "release/sign.py"),
    "codesign": ("release/sign.py",),
    "security": ("release/sign.py",),
    "xcrun": ("release/sign.py",),
    "hdiutil": ("release/sign.py",),
    "twine": ("release/publish.py",),
    "ruby": ("release/publish.py",),
    "rattler-build": ("release/publish.py",),
    "vsce": ("release/bundle.py", "release/publish.py"),
    "ovsx": ("release/publish.py",),
    "bin/check-e2e": ("tools/e2e.py",),
    "act": ("verbs/wf.py",),
    "docker": ("verbs/wf.py",),
}


@pytest.mark.parametrize(
    ("head", "homes"), sorted(_ADAPTER_HOMES.items()), ids=sorted(_ADAPTER_HOMES)
)
def test_no_tool_argv_outside_its_adapter(head: str, homes: tuple[str, ...]):
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
    for homes in _ADAPTER_HOMES.values():
        for home in homes:
            assert (_SRC_ROOT / home).is_file(), f"missing adapter home: {home}"
