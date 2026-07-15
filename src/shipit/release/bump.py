"""The bump-adapter registry — how the tag decision projects into manifests.

ADR-0041: the tag is the version authority; manifests are PROJECTIONS of the
tag decision. This module is the closed per-toolchain registry (the lint
``Lang`` set's mirror, one entry per :mod:`shipit.tools.registry` toolchain)
of those projections, plus the artifact-declared bundle-config hook:

- **rust** — ``cargo set-version --workspace`` (workspace-wide, intra-workspace
  deps included) then ``cargo update --workspace`` (lock refreshed) — the
  legacy ``prepare-release.yml`` bump, forked by copy (ADR-0001/0010).
  ``set-version`` is a cargo-edit subcommand, provisioned through the
  shipit-MANAGED pixi surface for rust consumers (the
  ``pixi.toml#shipit-rust-release-deps`` block, issue #793) — when it is
  absent, prepare fails LOUDLY naming the install reconcile
  (:func:`explain_command_failure`) and NEVER installs at run time (the #582
  cache doctrine: provisioning rides setup-pixi's lockfile-keyed cache).
- **npm** — ``npm version <v> --no-git-tag-version`` (``package.json`` +
  ``package-lock.json``; the git side stays prepare's, so the tag/commit
  never happens twice).
- **python** — a PURE rewrite of ``pyproject.toml`` ``[project].version``,
  deliberately toolchain-free (the legacy ``prepare-release-python`` choice).
  A prerelease version is NORMALIZED to PEP 440 in the manifest only
  (:func:`to_pep440`, issue #807): the semver the tag names (``-release-rc``,
  ``-rc.1``, …) is valid semver but not PEP 440, so writing it verbatim breaks
  every ``pixi``/``uv`` source build at the tagged commit. The tag stays
  authoritative in semver form (the RC guard keys off the TAG suffix,
  unaffected); only the pyproject value is rewritten to its PEP 440 spelling.
- **go** — the first-class ZERO-FILE adapter (PRD story 22, ADR-0041): the
  tag is the source of truth, the version is injected at build via
  ``-ldflags -X`` (:mod:`shipit.tools.build`). Not an exception — an entry
  whose projection set is empty.
- **tree-sitter** — a ZERO-FILE adapter too (TOL02-WS16 #792): the
  generated-parser tarball is content-addressed by the tag and legacy
  ``tree-sitter.yml`` runs with npm-publish OFF, so there is no published
  manifest whose version a downstream resolves — the tag alone names the
  release (the go shape, for the same ADR-0041 reason: no projection to
  bump).
- **lua** — a PURE rewrite of the Neovim-plugin entry file's ``M.version``
  string (:func:`bump_lua_version`), toolchain-free like python's (no
  luarocks/build tool is invoked — a nvim plugin freezes its version in Lua
  source, and #104 froze lex-fmt/nvim's exactly because shipit had no
  projection to bump it; TOL03-WS01 #972). The ``edit_path`` is the
  leg-relative ``init.lua``: the lua ``[toolchains]`` leg maps to the plugin's
  Lua package directory (``lua/<plugin>``), so the entry file resolves against
  the leg cwd the same way python's ``pyproject.toml`` does (see the ADR). The
  written value is the semver the tag names, VERBATIM — a Lua string is
  arbitrary text (no PEP 440 constraint), so no normalization, the npm shape.

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
from collections.abc import Sequence
from dataclasses import dataclass

from ..changelog import SEMVER_RE
from . import ReleaseError

#: The placeholder token in a command template the resolved version replaces
#: (:meth:`BumpAdapter.commands`).
VERSION_TOKEN = "{version}"

#: cargo's unknown-subcommand failure marker (``error: no such command: …``) —
#: the stderr substring that identifies an UNPROVISIONED cargo-edit at the rust
#: adapter's ``cargo set-version`` (issue #793). Matched as a substring so
#: cargo's quoting style around the subcommand name never matters.
CARGO_NO_SUCH_COMMAND = "no such command"

#: The CLOSED map from a semver prerelease PHASE word to its PEP 440 spelling
#: (issue #807). Semver admits any alphanumeric prerelease identifier; PEP 440
#: admits exactly three prerelease phases — ``a`` (alpha), ``b`` (beta), ``rc``
#: — with canonical single-form spellings. This table pins every semver phase
#: shipit's tags can carry (the reserved ``release-rc`` live-fire suffix
#: included; :data:`shipit.release.version.RELEASE_RC_PRE`) to its PEP 440 form;
#: a phase OUTSIDE it has no clean mapping and is a loud refusal
#: (:func:`to_pep440`), never a silent write. Keys are matched lower-cased.
_PEP440_PHASE: dict[str, str] = {
    "a": "a",
    "alpha": "a",
    "b": "b",
    "beta": "b",
    "c": "rc",
    "rc": "rc",
    "pre": "rc",
    "preview": "rc",
    "release-rc": "rc",
}

#: A single semver prerelease identifier split into its leading phase word and
#: an OPTIONAL trailing number (``rc`` → ``rc``/None, ``rc1`` → ``rc``/``1``,
#: ``release-rc`` → ``release-rc``/None). The phase admits internal hyphens so
#: the reserved ``release-rc`` suffix parses as one word; a purely numeric
#: identifier (``1``) never matches — it has no PEP 440 phase, so it falls
#: through to the loud refusal.
_SEMVER_PRE_PHASE_RE = re.compile(r"^(?P<phase>[A-Za-z][A-Za-z-]*?)(?P<num>[0-9]+)?$")


def explain_command_failure(argv: Sequence[str], stderr: str) -> str | None:
    """A remediation-bearing message for a KNOWN adapter-command failure. Pure.

    ``None`` means "not a shape this registry knows" — the caller re-raises the
    original error untranslated (after also consulting the missing-binary map,
    :func:`shipit.release.provisioning.missing_tool_remedy`, which owns the
    "tool absent outright" shapes — #801). This function's one shape is the
    rust adapter's ``cargo set-version`` dying unprovisioned (issue #793, the
    #784-F2 failure class): cargo-edit rides the shipit-managed pixi surface
    for rust consumers (the ``pixi.toml#shipit-rust-release-deps`` block,
    conda-forge-pinned, cached by setup-pixi under the lockfile key — the #582
    doctrine), so prepare NEVER installs it at run time; the remediation is the
    consumer's install reconcile. The probe is the ATTEMPT itself — cargo
    resolves custom subcommands via ``$CARGO_HOME/bin`` first, then PATH
    (issue #785's empirical finding), so a ``shutil.which`` pre-gate would
    wrongly abort exactly the setups the attempt would have resolved.
    """
    if tuple(argv[:2]) == ("cargo", "set-version") and CARGO_NO_SUCH_COMMAND in stderr:
        return (
            "rust bump needs `cargo set-version` (cargo-edit), which is not "
            "provisioned on this runner. cargo-edit rides the shipit-managed "
            "pixi surface for rust repos (the "
            "`pixi.toml#shipit-rust-release-deps` block, pinned from "
            "conda-forge) and is never installed at release run time — this "
            "repo's shipit pin/managed set is stale. Reconcile with a "
            "COMMITTING install (`shipit install --pr` opens the reconcile "
            "draft PR; `shipit install --local` commits on the current "
            "branch) — only these regenerate and stage pixi.lock alongside "
            "the pixi.toml block, so the committed lock stays coherent; plain "
            "`shipit install` only refreshes the working tree and leaves the "
            "lock stale. Merge/commit the reconcile, then re-run the release."
        )
    return None


@dataclass(frozen=True)
class BumpAdapter:
    """One registry entry: how a toolchain's leg projects the version.

    ``command_templates`` are the argv to run (in order, cwd at the leg's map
    path, through the exec seam) with :data:`VERSION_TOKEN` standing for the
    resolved version. ``edit_path`` is the leg-relative manifest a PURE
    rewrite (:func:`edit_for`) bumps instead of a command — the toolchain-free
    python path. ``stage`` is the leg-relative pathspec set of every file the
    bump may touch — the ONLY paths prepare stages and commits (story 24's
    stage-only-intended-files). A zero-command, zero-edit, zero-stage entry
    (go) is a first-class projection: the tag carries the version alone.
    """

    toolchain: str
    command_templates: tuple[tuple[str, ...], ...] = ()
    edit_path: str | None = None
    stage: tuple[str, ...] = ()

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
)
NPM = BumpAdapter(
    "npm",
    command_templates=(("npm", "version", VERSION_TOKEN, "--no-git-tag-version"),),
    stage=("package.json", "package-lock.json"),
)
PYTHON = BumpAdapter("python", edit_path="pyproject.toml", stage=("pyproject.toml",))
GO = BumpAdapter("go")
#: tree-sitter: zero-file, like go — the generated-parser tarball's version is
#: the tag (ADR-0041), npm publish off (legacy tree-sitter.yml), so no manifest
#: projection to bump (TOL02-WS16 #792).
TREE_SITTER = BumpAdapter("tree-sitter")
#: lua: a pure edit like python (TOL03-WS01 #972) — the Neovim-plugin entry
#: file's ``M.version`` string. ``edit_path`` is leg-relative (``init.lua``),
#: resolved against the lua leg's map path (the plugin's ``lua/<plugin>``
#: package dir), and it is the only file the bump touches, so it is also the
#: whole ``stage`` set.
LUA = BumpAdapter("lua", edit_path="init.lua", stage=("init.lua",))

#: The CLOSED registry, keyed by toolchain name — exactly the
#: :mod:`shipit.tools.registry` set, pinned by test. No "tauri" key, ever
#: (story 25): bundle-level files ride :func:`bump_bundle_config`.
ADAPTERS: dict[str, BumpAdapter] = {
    a.toolchain: a for a in (RUST, GO, PYTHON, NPM, TREE_SITTER, LUA)
}


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

#: A Neovim plugin's ``M.version = "…"`` assignment in its entry ``init.lua``
#: (TOL03-WS01 #972). ``M`` is the conventional module table a plugin returns
#: (``local M = {}`` … ``return M``). ANCHORED to a real assignment LINE
#: (``re.MULTILINE`` + ``^[ \t]*``): the ``M.version`` must open the line after
#: only leading indentation, so a leading Lua comment (``-- M.version = "…"``)
#: or a ``M.version`` embedded in a string / longer identifier
#: (``someM.version``, ``local s = "M.version = 1"``) is NOT bumped — only the
#: statement that actually sets the version. The leading indentation is part of
#: ``head`` so the rewrite preserves it. The quote char is captured (``q``) and
#: back-referenced for the close, so a Lua single- or double-quoted string bumps
#: and keeps its own style; the value spans no quote char, so a semver (no
#: embedded quote) is matched whole. First matching line only — the rewrite
#: preserves the file's formatting byte-for-byte.
_LUA_VERSION_RE = re.compile(
    r"(?P<head>^[ \t]*M\.version[ \t]*=[ \t]*(?P<q>[\"']))(?P<value>[^\"']*)(?P<tail>(?P=q))",
    re.MULTILINE,
)


def edit_for(adapter: BumpAdapter, text: str, version: str) -> str:
    """Apply ``adapter``'s pure manifest rewrite to ``text``. Pure.

    The two edit-shaped adapters dispatch by toolchain: python's
    ``pyproject.toml`` rewrite (:func:`bump_pyproject`) and lua's ``M.version``
    rewrite (:func:`bump_lua_version`, TOL03-WS01 #972); a future edit-shaped
    entry adds its function here. Calling this for an adapter with no
    ``edit_path`` is a caller bug.
    """
    assert adapter.edit_path is not None
    if adapter.toolchain == "lua":
        return bump_lua_version(text, version)
    return bump_pyproject(text, version)


def to_pep440(version: str) -> str:
    """``version`` in PEP 440 spelling for a python manifest. Pure.

    The tag names the version in SEMVER (ADR-0041), but ``pyproject.toml``
    ``[project].version`` must be PEP 440 — the two agree on stable versions
    yet diverge on prereleases: ``X.Y.Z-rc.1`` is valid semver and invalid
    PEP 440, so writing it verbatim breaks every ``pixi``/``uv`` source build
    at the tagged commit (issue #807). This maps the semver prerelease to its
    PEP 440 form and leaves stable versions IDENTICAL:

    - ``X.Y.Z`` (no suffix) → unchanged.
    - ``X.Y.Z-release-rc`` (the reserved live-fire suffix, no number) →
      ``X.Y.Zrc0`` — a deterministic throwaway ``rc0`` for the rc-guard cut
      (the tag keeps ``-release-rc``; the RC guard keys off the tag).
    - ``X.Y.Z-<phase>.N`` / ``X.Y.Z-<phase>N`` where ``<phase>`` is a known
      PEP 440 phase (:data:`_PEP440_PHASE`: alpha→a, beta→b, rc→rc, …) →
      ``X.Y.Z<a|b|rc>N`` (``-rc.1`` → ``rc1``, ``-alpha.2`` → ``a2``,
      ``-beta.3`` → ``b3``); a bare ``<phase>`` with no number → ``…<phase>0``.

    Any suffix with no clean PEP 440 mapping — an unknown phase word
    (``-snapshot.1``), a purely numeric prerelease (``-1``), or a multi-segment
    run (``-rc.1.2``) — is a LOUD :class:`ReleaseError`, never a silent write.
    Build metadata (``+…``) is likewise a LOUD refusal, not a silent drop: the
    release version flow forbids it at the boundary
    (:func:`shipit.release.version.parse_spec`) and disqualifies tags that
    carry it (:func:`shipit.release.version.version_tags`), so a ``+``-annotated
    version never reaches a legitimate manifest write — the manifest value is
    exactly what the tag names (ADR-0041), never annotated. Refusing here keeps
    this normalizer from silently emitting a version the tag does not name.
    """
    match = SEMVER_RE.match(version)
    if match is None:
        raise ReleaseError(f"not a semver version to normalize to PEP 440: {version!r}")
    if "+" in version:
        raise ReleaseError(
            f"build metadata is not allowed in a release version (got: {version!r}); "
            "the manifest version is exactly what the tag names (ADR-0041), never "
            "annotated"
        )
    pre = match.group("pre")
    if pre is None:
        return version  # stable — identical in both spellings
    base = f"{match.group('major')}.{match.group('minor')}.{match.group('patch')}"
    parts = pre.split(".")
    refusal = ReleaseError(
        f"prerelease suffix {pre!r} of {version!r} has no PEP 440 mapping — the "
        "python manifest needs a PEP 440 version (issue #807). Use a phase "
        "shipit can map (rc / alpha / beta, optionally .N) or a stable version."
    )
    if len(parts) == 1:
        ident = _SEMVER_PRE_PHASE_RE.match(parts[0])
        if ident is None:
            raise refusal
        phase_word, num = ident.group("phase"), ident.group("num")
    elif len(parts) == 2 and parts[1].isdigit():
        phase_word, num = parts[0], parts[1]
    else:
        raise refusal
    phase = _PEP440_PHASE.get(phase_word.lower())
    if phase is None:
        raise refusal
    return f"{base}{phase}{int(num) if num is not None else 0}"


def bump_pyproject(text: str, version: str) -> str:
    """``pyproject.toml`` with ``[project].version`` set to ``version``. Pure.

    Deliberately toolchain-free (no build backend is invoked — the legacy
    python bump's contract): a targeted line rewrite that preserves the rest
    of the file byte-for-byte. The written value is NORMALIZED to PEP 440
    (:func:`to_pep440`, issue #807) — a prerelease semver the tag names
    (``-release-rc``, ``-rc.1``) is not PEP 440 and would break the source
    build at the tag; the tag itself stays semver. Raises
    :class:`ReleaseError` when the ``[project]`` table carries no ``version``
    line — a dynamic-version or malformed manifest this projection cannot
    express — or when the version's prerelease suffix has no PEP 440 mapping.
    """
    version = to_pep440(version)
    replaced = _PYPROJECT_VERSION_RE.subn(rf"\g<head>{version}\g<tail>", text, count=1)
    if replaced[1] == 0:
        raise ReleaseError(
            "pyproject.toml has no [project] version line to bump — a static "
            '`version = "…"` under [project] is required (dynamic versions '
            "have no manifest projection)"
        )
    return replaced[0]


def bump_lua_version(text: str, version: str) -> str:
    """A Neovim plugin's entry ``init.lua`` with ``M.version`` set to
    ``version``. Pure.

    Toolchain-free, the python bump's spirit (TOL03-WS01 #972): a targeted
    rewrite of the first ``M.version = "…"`` ASSIGNMENT LINE (anchored to line
    start after indentation, so a leading comment ``-- M.version = "…"`` or a
    ``M.version`` inside a string is never mistaken for it) that preserves the
    rest of the file byte-for-byte (no Lua parse/emit — that would reflow the
    source). The value is written VERBATIM: a Lua version string is arbitrary
    text, so unlike the python manifest there is no PEP 440 constraint to
    normalize toward — the semver the tag names is written as-is (the npm
    shape). Raises :class:`ReleaseError` when the file carries no ``M.version``
    assignment — a plugin that never declared a bumpable version, which this
    projection cannot express (fail loud, never a silent no-op that would then
    trip prepare's no-op-bump guard with a confusing message).

    The replacement is a CALLABLE, not a template string: ``version`` is dynamic
    input, and a template (``rf"\\g<head>{version}\\g<tail>"``) would have
    :meth:`re.Pattern.subn` re-parse the version for backreferences (``\\1``,
    ``\\g<…>``) — a ``\\`` in the value could raise or silently corrupt the
    output (round 1, agy). A callable inserts the captured groups + the raw
    version literally, sidestepping replacement-string escaping entirely.
    """
    replaced = _LUA_VERSION_RE.subn(
        lambda m: f"{m.group('head')}{version}{m.group('tail')}", text, count=1
    )
    if replaced[1] == 0:
        raise ReleaseError(
            'lua entry file has no `M.version = "…"` line to bump — a Neovim '
            "plugin declares its version as a string on its module table "
            '(`M.version = "x.y.z"` in the plugin\'s init.lua) for shipit to '
            "project the tag onto (ADR-0041)"
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
