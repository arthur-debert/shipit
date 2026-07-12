"""The bump-adapter registry — how the tag decision projects into manifests.

ADR-0041: the tag is the version authority; manifests are PROJECTIONS of the
tag decision. This module is the closed per-toolchain registry (the lint
``Lang`` set's mirror, one entry per :mod:`shipit.tools.registry` toolchain)
of those projections, plus the artifact-declared bundle-config hook:

- **rust** — ``cargo set-version --workspace`` (workspace-wide, intra-workspace
  deps included) then ``cargo update --workspace`` (lock refreshed) — the
  legacy ``prepare-release.yml`` bump, forked by copy (ADR-0001/0010).
  ``set-version`` is cargo-edit's subcommand, and cargo-edit is
  SELF-PROVISIONED (a pinned ``cargo install cargo-edit``, issue #793 —
  the deb composition's cargo-deb shape, #784 F2): it is not on conda-forge,
  so no pixi env can carry it, and the wf-prepare runner arrives without it.
  The adapter CARRIES the provision (:class:`Provision`); prepare executes
  it through the same Exec seam when the probe binary is off PATH.
- **npm** — ``npm version <v> --no-git-tag-version`` (``package.json`` +
  ``package-lock.json``; the git side stays prepare's, so the tag/commit
  never happens twice).
- **python** — a PURE rewrite of ``pyproject.toml`` ``[project].version``,
  deliberately toolchain-free (the legacy ``prepare-release-python`` choice).
- **go** — the first-class ZERO-FILE adapter (PRD story 22, ADR-0041): the
  tag is the source of truth, the version is injected at build via
  ``-ldflags -X`` (:mod:`shipit.tools.build`). Not an exception — an entry
  whose projection set is empty.

"tauri" is NEVER a registry key (PRD story 25, ADR-0007): a Tauri app is a
composition of the npm and rust legs, and its bundle-level version file
(``tauri.conf.json``) is bumped by the artifact-declared bundle-config hook —
``[artifacts.<name>] bundle-config = "src-tauri/tauri.conf.json"`` in
``.shipit.toml`` (:class:`shipit.config.Artifact`), applied by
:func:`bump_bundle_config` in lockstep with the leg adapters.

Everything here is pure data + pure text rewrites: the adapter COMMANDS run
in the effectful shell (``shipit release prepare``,
:mod:`shipit.verbs.release`) through the one Exec seam (ADR-0028) with cwd at
the leg's map path; the rewrites are string→string functions, fixture-tested.
The command literals below are these tools' one RELEASE-side assembly point,
whitelisted alongside :mod:`shipit.tools.registry` in the mechanized argv
sweep (``tests/test_tool_argv_sweep.py``).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from . import ReleaseError

#: The placeholder token in a command template the resolved version replaces
#: (:meth:`BumpAdapter.commands`).
VERSION_TOKEN = "{version}"

#: The pinned cargo-edit version the rust adapter self-provisions (issue
#: #793). A floating ``cargo install cargo-edit`` resolves the latest crate
#: at prepare time — irreproducible bumps and a supply-chain window — so the
#: version is PINNED, the same shape as the deb composition's
#: :data:`shipit.release.bundle.CARGO_DEB_VERSION`. Bump deliberately, in its
#: own change.
CARGO_EDIT_VERSION = "0.13.11"


@dataclass(frozen=True)
class Provision:
    """A registry entry's self-provision: how its missing tool gets installed.

    ``probe`` is the binary name whose ABSENCE from PATH triggers the install
    (``shutil.which``, checked by prepare); ``install`` is the exact argv (a
    PINNED ``cargo install``) prepare runs through the same Exec seam as the
    adapter's commands, cwd at the leg's map path. A failing install raises
    through the seam's ``check=True``, aborting prepare loudly (ADR-0009's
    barrier), never a quiet skip.

    Deliberately NO post-install re-probe (the #785 rounds-1–2 lesson): cargo
    resolves a custom subcommand by searching ``$CARGO_HOME/bin`` ITSELF,
    independent of the process PATH, so the just-installed tool is found even
    in an isolated env (pixi) where ``$CARGO_HOME/bin`` is off PATH — a
    ``shutil.which`` gate would spuriously abort exactly that case. The same
    resolution makes a false-negative probe harmless: re-installing the
    pinned version is a no-op ("already installed", rc 0).
    """

    probe: str
    install: tuple[str, ...]


@dataclass(frozen=True)
class BumpAdapter:
    """One registry entry: how a toolchain's leg projects the version.

    ``command_templates`` are the argv to run (in order, cwd at the leg's map
    path, through the exec seam) with :data:`VERSION_TOKEN` standing for the
    resolved version. ``edit_path`` is the leg-relative manifest a PURE
    rewrite (:func:`edit_for`) bumps instead of a command — the toolchain-free
    python path. ``stage`` is the leg-relative pathspec set of every file the
    bump may touch — the ONLY paths prepare stages and commits (story 24's
    stage-only-intended-files). ``provision`` is the entry's self-provision
    (:class:`Provision`, issue #793) — prepare runs its install through the
    same seam BEFORE the commands when the probe binary is missing; ``None``
    for the adapters whose tools arrive with their toolchains. A
    zero-command, zero-edit, zero-stage entry (go) is a first-class
    projection: the tag carries the version alone.
    """

    toolchain: str
    command_templates: tuple[tuple[str, ...], ...] = ()
    edit_path: str | None = None
    stage: tuple[str, ...] = ()
    provision: Provision | None = None

    def commands(self, version: str) -> tuple[tuple[str, ...], ...]:
        """The concrete argv sequence for ``version`` — templates with
        :data:`VERSION_TOKEN` substituted. Pure."""
        return tuple(
            tuple(version if part == VERSION_TOKEN else part for part in argv)
            for argv in self.command_templates
        )

    @property
    def projects_files(self) -> bool:
        """Whether this adapter touches any file at all — ``False`` exactly
        for the zero-file (go) shape, whose bump is the tag itself."""
        return bool(self.command_templates or self.edit_path)


RUST = BumpAdapter(
    "rust",
    command_templates=(
        ("cargo", "set-version", "--workspace", VERSION_TOKEN),
        ("cargo", "update", "--workspace"),
    ),
    stage=("Cargo.toml", "**/Cargo.toml", "Cargo.lock"),
    # `cargo set-version` is cargo-edit's subcommand — self-provisioned when
    # the wf-prepare runner arrives without it (issue #793; see Provision).
    provision=Provision(
        probe="cargo-set-version",
        install=(
            "cargo",
            "install",
            "cargo-edit",
            "--version",
            CARGO_EDIT_VERSION,
            "--locked",
        ),
    ),
)
NPM = BumpAdapter(
    "npm",
    command_templates=(("npm", "version", VERSION_TOKEN, "--no-git-tag-version"),),
    stage=("package.json", "package-lock.json"),
)
PYTHON = BumpAdapter("python", edit_path="pyproject.toml", stage=("pyproject.toml",))
GO = BumpAdapter("go")

#: The CLOSED registry, keyed by toolchain name — exactly the
#: :mod:`shipit.tools.registry` set, pinned by test. No "tauri" key, ever
#: (story 25): bundle-level files ride :func:`bump_bundle_config`.
ADAPTERS: dict[str, BumpAdapter] = {a.toolchain: a for a in (RUST, GO, PYTHON, NPM)}


def adapter_for(toolchain: str) -> BumpAdapter:
    """The registry entry for ``toolchain``.

    Raises :class:`ReleaseError` for a name outside the closed set — in
    practice unreachable from the verb (``.shipit.toml`` toolchains are
    validated at config parse), kept loud for programmatic callers.
    """
    adapter = ADAPTERS.get(toolchain)
    if adapter is None:
        known = ", ".join(sorted(ADAPTERS))
        raise ReleaseError(
            f"no bump adapter for toolchain {toolchain!r}; known: {known}"
        )
    return adapter


# --------------------------------------------------------------------------
# Pure rewrites — the file-edit projections (fixture-tested)
# --------------------------------------------------------------------------

#: ``[project]`` table's ``version = "…"`` line in a ``pyproject.toml``: the
#: match is anchored to the table header and may cross only NON-header lines
#: (arrays, strings) on the way to the ``version`` line, so a ``version`` key
#: of some OTHER table (``[tool.something] version``) is never rewritten. The
#: quote character is captured (``q``) and back-referenced for the close, so a
#: TOML literal string (``version = '1.0.0'``) bumps and keeps its own style.
_PYPROJECT_VERSION_RE = re.compile(
    r"(?P<head>^\[project\][ \t]*\n(?:(?!^\[).*\n)*?^version[ \t]*=[ \t]*(?P<q>[\"']))"
    r"(?P<value>[^\"']*)(?P<tail>(?P=q))",
    re.MULTILINE,
)

#: A JSON object's top-level ``"version": "…"`` member — matched textually
#: (first occurrence) so the rewrite PRESERVES the consumer's formatting; a
#: JSON round-trip would re-indent the whole file (the legacy jq lesson).
_JSON_VERSION_RE = re.compile(
    r"(?P<head>\"version\"\s*:\s*\")(?P<value>[^\"]*)(?P<tail>\")"
)


def edit_for(adapter: BumpAdapter, text: str, version: str) -> str:
    """Apply ``adapter``'s pure manifest rewrite to ``text``. Pure.

    Today the only edit-shaped adapter is python's
    (:func:`bump_pyproject`); a future edit-shaped entry adds its function
    here. Calling this for an adapter with no ``edit_path`` is a caller bug.
    """
    assert adapter.edit_path is not None
    return bump_pyproject(text, version)


def bump_pyproject(text: str, version: str) -> str:
    """``pyproject.toml`` with ``[project].version`` set to ``version``. Pure.

    Deliberately toolchain-free (no build backend is invoked — the legacy
    python bump's contract): a targeted line rewrite that preserves the rest
    of the file byte-for-byte. Raises :class:`ReleaseError` when the
    ``[project]`` table carries no ``version`` line — a dynamic-version or
    malformed manifest this projection cannot express.
    """
    replaced = _PYPROJECT_VERSION_RE.subn(rf"\g<head>{version}\g<tail>", text, count=1)
    if replaced[1] == 0:
        raise ReleaseError(
            "pyproject.toml has no [project] version line to bump — a static "
            '`version = "…"` under [project] is required (dynamic versions '
            "have no manifest projection)"
        )
    return replaced[0]


def bump_bundle_config(text: str, version: str) -> str:
    """A JSON bundle-config file (``tauri.conf.json``) with its ``version``
    member set to ``version``. Pure.

    The artifact-declared bundle-config hook's rewrite (story 25): a textual
    replace of the FIRST ``"version"`` member, preserving the consumer's
    formatting (a JSON round-trip would rewrite the whole file). The file must
    parse as JSON and carry a string ``version`` — anything else raises
    :class:`ReleaseError` naming the defect, never a silent no-op.
    """
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        raise ReleaseError(f"bundle-config file is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict) or not isinstance(parsed.get("version"), str):
        raise ReleaseError(
            'bundle-config file has no top-level string "version" member to bump'
        )
    bumped = _JSON_VERSION_RE.sub(rf"\g<head>{version}\g<tail>", text, count=1)
    # The textual replace targets the FIRST "version" member; verify against a
    # real parse that it landed on the TOP-LEVEL one (a nested "version"
    # appearing earlier in the file would otherwise be rewritten silently).
    if json.loads(bumped).get("version") != version:
        raise ReleaseError(
            'bundle-config file\'s first "version" member is not the top-level '
            "one; refusing an ambiguous rewrite"
        )
    return bumped
