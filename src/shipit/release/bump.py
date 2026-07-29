"""The closed bump-adapter registry: how the tag decision projects into manifests.

One entry per toolchain, plus the artifact-declared bundle-config hook.
See docs/adr/0041-tag-authoritative-version-supplied-not-computed.md.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass

from ..changelog import SEMVER_RE
from . import ReleaseError

VERSION_TOKEN = "{version}"

CARGO_NO_SUCH_COMMAND = "no such command"

#: The CLOSED semver-phase -> PEP 440 map, keys matched lower-cased.
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

#: The phase admits internal hyphens, so ``release-rc`` parses as one word.
_SEMVER_PRE_PHASE_RE = re.compile(r"^(?P<phase>[A-Za-z][A-Za-z-]*?)(?P<num>[0-9]+)?$")


def explain_command_failure(argv: Sequence[str], stderr: str) -> str | None:
    """A remediation message for a KNOWN adapter-command failure, else ``None``."""
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
    """How a toolchain's leg projects the version; ``stage`` is all prepare commits."""

    toolchain: str
    command_templates: tuple[tuple[str, ...], ...] = ()
    edit_path: str | None = None
    stage: tuple[str, ...] = ()

    def commands(self, version: str) -> tuple[tuple[str, ...], ...]:
        return tuple(
            tuple(version if part == VERSION_TOKEN else part for part in argv)
            for argv in self.command_templates
        )

    @property
    def projects_files(self) -> bool:
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
TREE_SITTER = BumpAdapter("tree-sitter")
#: ``edit_path`` resolves against the lua leg's ``lua/<plugin>`` map path.
LUA = BumpAdapter("lua", edit_path="init.lua", stage=("init.lua",))

#: The CLOSED registry, exactly the :mod:`shipit.tools.registry` set. No
#: "tauri" key, ever: its bundle file rides :func:`bump_bundle_config`.
ADAPTERS: dict[str, BumpAdapter] = {
    a.toolchain: a for a in (RUST, GO, PYTHON, NPM, TREE_SITTER, LUA)
}


def adapter_for(toolchain: str) -> BumpAdapter:
    adapter = ADAPTERS.get(toolchain)
    if adapter is None:
        known = ", ".join(sorted(ADAPTERS))
        raise ReleaseError(
            f"no bump adapter for toolchain {toolchain!r}; known: {known}"
        )
    return adapter


#: Anchored to ``[project]``, so another table's ``version`` is never rewritten.
_PYPROJECT_VERSION_RE = re.compile(
    r"(?P<head>^\[project\][ \t]*\n(?:(?!^\[).*\n)*?^version[ \t]*=[ \t]*(?P<q>[\"']))"
    r"(?P<value>[^\"']*)(?P<tail>(?P=q))",
    re.MULTILINE,
)

_JSON_VERSION_RE = re.compile(
    r"(?P<head>\"version\"\s*:\s*\")(?P<value>[^\"]*)(?P<tail>\")"
)

#: ANCHORED to a real assignment LINE, so a comment or a string is never bumped.
_LUA_VERSION_RE = re.compile(
    r"(?P<head>^[ \t]*M\.version[ \t]*=[ \t]*(?P<q>[\"']))(?P<value>[^\"']*)(?P<tail>(?P=q))",
    re.MULTILINE,
)


def edit_for(adapter: BumpAdapter, text: str, version: str) -> str:
    assert adapter.edit_path is not None
    if adapter.toolchain == "lua":
        return bump_lua_version(text, version)
    return bump_pyproject(text, version)


def to_pep440(version: str) -> str:
    """``version`` in PEP 440 spelling; an unmappable suffix is a LOUD refusal."""
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
    """A plugin's ``init.lua`` with ``M.version`` set, written VERBATIM."""
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
    """A JSON bundle-config file with its ``version`` member set, formatting preserved."""
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        raise ReleaseError(f"bundle-config file is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict) or not isinstance(parsed.get("version"), str):
        raise ReleaseError(
            'bundle-config file has no top-level string "version" member to bump'
        )
    bumped = _JSON_VERSION_RE.sub(rf"\g<head>{version}\g<tail>", text, count=1)
    # A real parse verifies the textual replace landed on the TOP-LEVEL member.
    if json.loads(bumped).get("version") != version:
        raise ReleaseError(
            'bundle-config file\'s first "version" member is not the top-level '
            "one; refusing an ambiguous rewrite"
        )
    return bumped
