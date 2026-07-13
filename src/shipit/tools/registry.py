"""The closed toolchain registry — the Tool verbs' dispatch axis (ADR-0007/0039).

A **Toolchain** names the build/test ecosystem of one path in a repo (rust,
go, python, npm, tree-sitter) and carries the DEFAULT producing command per **tool slot**
(``test`` from WS01, ``build`` from WS02). The registry is CLOSED, the
lint ``Lang`` set's mirror: adding a toolchain is adding an entry here,
nothing downstream changes — and a toolchain is never a project-Kind switch
("a tauri Kind" is a composition of map entries, not a dispatch label).

The default test commands are release-core's battle-tested runners
(docs/legacy-prd/tol01-ci-tools.md, story 3): rust → cargo-nextest ("the standard
test runner used by bin/check-tests across the fleet"), go → ``go test
./...``, python → pytest. The npm default is NOT pinned by the PRD (story 3
lists only rust/go/python); ADR-0039's registry sketch names ``npm test``
— the package's own test script — and that is the default chosen here.

The default build commands are the legacy CI matrix jobs' single-target
builds (issue #555's legacy digest): rust → ``cargo build --release``
(``rust-ci``'s build-binaries job, minus the CI-routed ``--target`` matrix),
go → ``go build -trimpath -ldflags "-s -w" ./...`` (``go-cli``'s static
build over every package — the test slot's ``./...`` form; a bare ``go
build`` compiles only the root package and fails any repo whose packages
live under subdirs. The ``CGO_ENABLED=0`` env and the ADR-0041 ``-X``
version injection are shaped per invocation by :mod:`shipit.tools.build`,
since env and a supplied version are not argv defaults; an artifact-narrowed
build swaps ``./...`` for the artifact's one package there too), python →
``uv build`` (sdist + wheel in uv's
isolated build env), npm → the package's own build script (``npm run
build`` — same deference as the test slot; ``build-frontend`` is not a tool,
it IS this leg). Pixi is NEVER the build backend: these argv invoke the real
builder directly (PRD story 9) — provisioning stays in ``pixi.toml``.

These argv literals are the producing commands' ONE assembly point: per
ADR-0028, tool argv built outside its adapter is a defect, and the mechanized
sweep (``tests/test_tool_argv_sweep.py``) pins this module as the home for
the ``cargo`` / ``go`` / ``pytest`` / ``npm`` / ``uv`` heads. A per-path
override (``.shipit.toml``, :func:`shipit.config.load_toolchains`)
substitutes a consumer-declared argv for one leg — data, not a second
assembly point.
"""

from __future__ import annotations

from dataclasses import dataclass

#: The tool slots a :class:`Toolchain` entry carries — the CLOSED vocabulary of
#: tree-input Tool verbs: ``test`` (WS01) and ``build`` (WS02). A new slot is
#: a new field on :class:`Toolchain` plus a name here.
TOOL_TEST = "test"
TOOL_BUILD = "build"
TOOLS: tuple[str, ...] = (TOOL_TEST, TOOL_BUILD)


class UnknownToolError(ValueError):
    """A tool slot outside the closed :data:`TOOLS` vocabulary was requested."""


@dataclass(frozen=True)
class Toolchain:
    """One registry entry: a toolchain and its default producing command per tool.

    ``test`` / ``build`` are the default producing argvs per tool slot, run
    with cwd at the leg's map path. A per-path ``.shipit.toml`` override
    replaces one for that leg only (:func:`shipit.config.load_toolchains`);
    the registry never changes per repo. The ``build`` argv is the BASE
    command: per-invocation shaping — the artifact's build target args, go's
    env and version injection — belongs to :mod:`shipit.tools.build`, never
    here. ``provisions_signal`` names a toolchain SIGNAL the entry's own CLI
    needs delivered when a consumer DECLARES this toolchain (#890): the
    manifest walk (:func:`shipit.install.reconcile.detect_toolchains`) covers
    the toolchains a tracked manifest signals, but a tree-sitter grammar has
    no manifest — its ``[toolchains]`` declaration is the only signal, so
    ``shipit install`` unions this off the declared map
    (:func:`shipit.verbs.install._declared_signals`), the exact mechanics of
    :attr:`shipit.release.bundle.Composition.provisions_signal` (the
    wasm-pack→node-deps precedent, #788). ``None`` (every entry whose tools
    already ride a manifest signal) adds nothing.
    """

    name: str
    test: tuple[str, ...]
    build: tuple[str, ...]
    provisions_signal: str | None = None

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


#: rust: cargo-nextest is the fleet's standard test runner (the legacy rust-ci
#: workflow installed it explicitly for bin/check-tests; the PRD keeps it).
#: The build is the legacy build-binaries job's release build; the artifact's
#: cargo workspace package (``-p``) is target shaping, not a default.
RUST = Toolchain(
    "rust",
    test=("cargo", "nextest", "run"),
    build=("cargo", "build", "--release"),
)
#: go: its own runner over every package — the legacy go-ci check job's form.
#: The build is go-cli's static form (``-trimpath``, stripped via ``-s -w``)
#: over every package (``./...``, mirroring the test slot: a bare ``go build``
#: compiles only the root package, so a repo whose packages all live under
#: ``cmd/…`` would red with "no Go files in ."); ``CGO_ENABLED=0`` and the
#: supplied-version ``-X`` injection (ADR-0041) are per-invocation shaping in
#: :mod:`shipit.tools.build`, which also swaps ``./...`` for the artifact's
#: one package (declared, or the module root) when a target narrows the build.
GO = Toolchain(
    "go",
    test=("go", "test", "./..."),
    build=("go", "build", "-trimpath", "-ldflags", "-s -w", "./..."),
)
#: python: pytest, bare — options belong to the repo's committed pytest config
#: (pyproject/pytest.ini), not the dispatch registry. The build is ``uv
#: build`` (sdist + wheel), python-pkg's build job minus the CI routing.
PYTHON = Toolchain("python", test=("pytest",), build=("uv", "build"))
#: npm: the package's own scripts for BOTH slots (``npm test`` / ``npm run
#: build``) — the PRD does not pin npm defaults (story 3 lists only
#: rust/go/python); ADR-0039's registry sketch names ``npm test``, and
#: delegating to the package script is the choice recorded here: node repos
#: already declare their runner and bundler (vitest, vite, …) in
#: ``package.json``, so the registry defers to that declaration rather than
#: picking one for the fleet.
NPM = Toolchain("npm", test=("npm", "test"), build=("npm", "run", "build"))
#: tree-sitter: the bespoke generated-parser toolchain (TOL02-WS16 #792;
#: WS10 NO-GO on pixi-build, #798). The build slot is ``tree-sitter
#: generate`` — regenerates ``src/parser.c`` (and the ``src/tree_sitter/``
#: headers, ``node-types.json``) from ``grammar.js``, the whole-leg build a
#: generated-parser artifact bundles into its tarball (no per-artifact
#: package narrowing — like ``uv build``, ``tree-sitter generate`` produces
#: the parser whole, so :mod:`shipit.tools.build` leaves its argv untouched).
#: The test slot is ``tree-sitter test`` — the CORPUS tests (the
#: ``test/corpus/`` s-expression assertions), the check a corpus lane runs
#: (``run = "test tree-sitter"``) to keep the grammar honest against its
#: fixtures. Legacy ``tree-sitter.yml@v3`` ran the same two commands (npm
#: publish OFF, corpus tests ON); this is that composition, shipit-side. The
#: ``tree-sitter`` CLI is pixi-managed (#890 closed the WS17 open hole 7:
#: conda-forge DOES carry ``tree-sitter-cli``): the
#: ``pixi.toml#shipit-tree-sitter-release-deps`` block delivers it into the
#: default env, unioned off this very declaration via ``provisions_signal`` —
#: no manifest signals a grammar, so the ``[toolchains]`` leg is the signal
#: (the wasm-pack→node-deps mechanics, #788).
TREE_SITTER = Toolchain(
    "tree-sitter",
    test=("tree-sitter", "test"),
    build=("tree-sitter", "generate"),
    provisions_signal="tree-sitter",
)

#: The closed registry, in a stable order. Adding a toolchain is adding an
#: entry here (mirror of the lint ``LANGS`` tuple).
TOOLCHAINS: tuple[Toolchain, ...] = (RUST, GO, PYTHON, NPM, TREE_SITTER)


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
