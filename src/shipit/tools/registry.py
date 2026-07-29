"""``tools/registry`` — the closed toolchain registry, the Tool verbs' dispatch axis.

Each entry carries the default producing argv per tool slot; a slot may be empty.
See docs/adr/0039-tool-verbs.md.
"""

from __future__ import annotations

from dataclasses import dataclass

TOOL_TEST = "test"
TOOL_BUILD = "build"
TOOLS: tuple[str, ...] = (TOOL_TEST, TOOL_BUILD)


class UnknownToolError(ValueError):
    """A tool slot outside the closed :data:`TOOLS` vocabulary was requested."""


@dataclass(frozen=True)
class Toolchain:
    """The BASE argv per slot, run with cwd at the leg's path; empty = produces
    nothing. ``provisions_signal`` is delivered when a consumer DECLARES this entry.
    """

    name: str
    test: tuple[str, ...]
    build: tuple[str, ...]
    provisions_signal: str | None = None

    def command(self, tool: str) -> tuple[str, ...]:
        if tool not in TOOLS:
            known = ", ".join(TOOLS)
            raise UnknownToolError(f"unknown tool slot {tool!r}; known: {known}")
        return getattr(self, tool)


RUST = Toolchain(
    "rust",
    test=("cargo", "nextest", "run"),
    build=("cargo", "build", "--release"),
)
#: ``./...`` in both slots: a bare ``go build`` compiles only the root package.
GO = Toolchain(
    "go",
    test=("go", "test", "./..."),
    build=("go", "build", "-trimpath", "-ldflags", "-s -w", "./..."),
)
PYTHON = Toolchain("python", test=("pytest",), build=("uv", "build"))
NPM = Toolchain("npm", test=("npm", "test"), build=("npm", "run", "build"))
#: A grammar has no manifest, so this declaration is the only provisioning signal.
TREE_SITTER = Toolchain(
    "tree-sitter",
    test=("tree-sitter", "test"),
    build=("tree-sitter", "generate"),
    provisions_signal="tree-sitter",
)
#: Buildless: a Neovim plugin is interpreted source, and has no manifest either.
LUA = Toolchain("lua", test=("busted",), build=(), provisions_signal="lua")

TOOLCHAINS: tuple[Toolchain, ...] = (RUST, GO, PYTHON, NPM, TREE_SITTER, LUA)


def names() -> tuple[str, ...]:
    return tuple(tc.name for tc in TOOLCHAINS)


def toolchain(name: str) -> Toolchain | None:
    for tc in TOOLCHAINS:
        if tc.name == name:
            return tc
    return None
