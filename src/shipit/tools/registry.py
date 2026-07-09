"""The closed toolchain registry — the Tool verbs' dispatch axis (ADR-0007/0039).

A **Toolchain** names the build/test ecosystem of one path in a repo (rust,
go, python, npm) and carries the DEFAULT producing command per **tool slot**
(this WS fills ``test``; WS02 fills ``build``). The registry is CLOSED, the
lint ``Lang`` set's mirror: adding a toolchain is adding an entry here,
nothing downstream changes — and a toolchain is never a project-Kind switch
("a tauri Kind" is a composition of map entries, not a dispatch label).

The default test commands are release-core's battle-tested runners
(docs/prd/tol01-ci-tools.md, story 3): rust → cargo-nextest ("the standard
test runner used by bin/check-tests across the fleet"), go → ``go test
./...``, python → pytest. The npm default is NOT pinned by the PRD (story 3
lists only rust/go/python); ADR-0039's registry sketch names ``npm test``
— the package's own test script — and that is the default chosen here.

These argv literals are the producing commands' ONE assembly point: per
ADR-0028, tool argv built outside its adapter is a defect, and the mechanized
sweep (``tests/test_tool_argv_sweep.py``) pins this module as the home for
the ``cargo`` / ``go`` / ``pytest`` / ``npm`` heads. A per-path override
(``.shipit.toml``, :func:`shipit.config.load_toolchains`) substitutes a
consumer-declared argv for one leg — data, not a second assembly point.
"""

from __future__ import annotations

from dataclasses import dataclass

#: The tool slots a :class:`Toolchain` entry carries — the CLOSED vocabulary of
#: tree-input Tool verbs. WS01 ships ``test``; WS02 adds ``build`` (a new slot
#: is a new field on :class:`Toolchain` plus a name here).
TOOL_TEST = "test"
TOOLS: tuple[str, ...] = (TOOL_TEST,)


class UnknownToolError(ValueError):
    """A tool slot outside the closed :data:`TOOLS` vocabulary was requested."""


@dataclass(frozen=True)
class Toolchain:
    """One registry entry: a toolchain and its default producing command per tool.

    ``test`` is the default test-producing argv, run with cwd at the leg's map
    path. A per-path ``.shipit.toml`` override replaces it for that leg only
    (:func:`shipit.config.load_toolchains`); the registry never changes per
    repo.
    """

    name: str
    test: tuple[str, ...]

    def command(self, tool: str) -> tuple[str, ...]:
        """The default producing argv for ``tool`` (a :data:`TOOLS` slot).

        Raises :class:`UnknownToolError` for a slot outside the closed
        vocabulary — a caller bug (the verbs only ever pass their own name),
        never a user-facing outcome.
        """
        if tool not in TOOLS:
            known = ", ".join(TOOLS)
            raise UnknownToolError(f"unknown tool slot {tool!r}; known: {known}")
        return getattr(self, tool)


#: cargo-nextest: the fleet's standard rust test runner (the legacy rust-ci
#: workflow installed it explicitly for bin/check-tests; the PRD keeps it).
RUST = Toolchain("rust", test=("cargo", "nextest", "run"))
#: go's own runner over every package — the legacy go-ci check job's form.
GO = Toolchain("go", test=("go", "test", "./..."))
#: pytest, bare: options belong to the repo's committed pytest config
#: (pyproject/pytest.ini), not the dispatch registry.
PYTHON = Toolchain("python", test=("pytest",))
#: The package's own test script (``npm test``) — the PRD does not pin an npm
#: default (story 3 lists only rust/go/python); ADR-0039's registry sketch
#: names ``npm test``, and delegating to the package script is the choice
#: recorded here: node repos already declare their runner (vitest, jest, …)
#: in ``package.json``, so the registry defers to that declaration rather
#: than picking a runner for the fleet.
NPM = Toolchain("npm", test=("npm", "test"))

#: The closed registry, in a stable order. Adding a toolchain is adding an
#: entry here (mirror of the lint ``LANGS`` tuple).
TOOLCHAINS: tuple[Toolchain, ...] = (RUST, GO, PYTHON, NPM)


def names() -> tuple[str, ...]:
    """The registered toolchain names, in registry order — for validation
    messages (:func:`shipit.config.load_toolchains`) and selector errors."""
    return tuple(tc.name for tc in TOOLCHAINS)


def toolchain(name: str) -> Toolchain | None:
    """The registry entry named ``name``, or ``None`` when unregistered.

    The config loader turns ``None`` into a :class:`~shipit.config.ConfigError`
    naming the known set; the planner treats it as a caller bug (entries reach
    it already validated).
    """
    for tc in TOOLCHAINS:
        if tc.name == name:
            return tc
    return None
