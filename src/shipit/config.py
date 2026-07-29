"""Reading ``.shipit.toml`` — shipit's policy config."""

from __future__ import annotations

import hashlib
import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import MappingProxyType

from .identity import Sha, repo_from_slug

CONFIG_NAME = ".shipit.toml"

_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")


class ConfigError(RuntimeError):
    """``.shipit.toml`` is missing or malformed."""


_KNOWN_TABLES = {
    "secrets",
    "reviewers",
    "managed",
    "shipit",
    "project",
    "lint",
    "toolchains",
    "artifacts",
    "artifact-deps",
    "stage",
    "lanes",
}
_ESCAPE_HATCH_TABLES = {"project", "custom"}


def _validate_known_tables(cfg: dict) -> None:
    """Reject any top-level table outside :data:`_KNOWN_TABLES`; the ``project``/``custom`` subtrees are not descended."""
    allowed = _KNOWN_TABLES | _ESCAPE_HATCH_TABLES
    for key in cfg:
        if key not in allowed:
            known = ", ".join(sorted(allowed))
            raise ConfigError(
                f"unknown top-level table `{key}` in {CONFIG_NAME}; "
                f"known tables: {known}"
            )


@dataclass(frozen=True)
class SecretSource:
    """One ``[secrets]`` entry; exactly one of ``doppler``/``env``/``prompt`` is set."""

    name: str
    kind: str
    key: str | None
    optional: bool = False


def _parse_secret(name: str, spec: object) -> SecretSource:
    if not isinstance(spec, dict):
        raise ConfigError(
            f"[secrets].{name} must be an inline table, e.g. "
            f'{{ doppler = "KEY" }}; got {spec!r}'
        )
    optional = bool(spec.get("optional", False))
    sources = [k for k in ("doppler", "env", "prompt") if k in spec]
    if len(sources) != 1:
        raise ConfigError(
            f"[secrets].{name} must name exactly one source "
            f"(doppler / env / prompt); got {sources or 'none'}"
        )
    kind = sources[0]
    if kind == "prompt":
        if spec.get("prompt") is not True:
            raise ConfigError(f"[secrets].{name}: prompt must be `true`")
        return SecretSource(name=name, kind="prompt", key=None, optional=optional)
    key = spec[kind]
    if not isinstance(key, str) or not key:
        raise ConfigError(f"[secrets].{name}: {kind} must be a non-empty string")
    return SecretSource(name=name, kind=kind, key=key, optional=optional)


def load_secrets(spec: dict) -> list[SecretSource]:
    secrets = spec.get("secrets", {})
    if not isinstance(secrets, dict):
        raise ConfigError("[secrets] must be a table")
    return [_parse_secret(name, value) for name, value in secrets.items()]


def load(path: str | Path) -> dict:
    """Parse a ``.shipit.toml`` file into a dict; bad TOML or non-UTF-8 raises :class:`ConfigError`."""
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"no {CONFIG_NAME} at {p}")
    try:
        with p.open("rb") as fh:
            cfg = tomllib.load(fh)
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ConfigError(f"malformed {p}: {exc}") from None
    _validate_known_tables(cfg)
    return cfg


def content_hash(data: bytes) -> str:
    """The ``sha256:<hex>`` pristine hash of a managed unit's content."""
    return "sha256:" + hashlib.sha256(data).hexdigest()


DECLINE_KEY = "decline"


def load_managed(cfg: dict) -> dict[str, str]:
    """The ``[managed]`` pristine map (path → ``sha256:...``); ``{}`` when absent."""
    managed = cfg.get("managed", {})
    if not isinstance(managed, dict):
        raise ConfigError("[managed] must be a table")
    return {str(k): str(v) for k, v in managed.items() if k != DECLINE_KEY}


def _has_table_header(text: str, dotted: str) -> bool:
    """True when TOML ``text`` carries an explicit ``[dotted]`` header line, not the same key reached by a dotted path."""
    want = [p.strip() for p in dotted.split(".")]
    for line in text.splitlines():
        s = line.split("#", 1)[0].strip()
        if s.startswith("[") and not s.startswith("[[") and s.endswith("]"):
            if [p.strip() for p in s[1:-1].split(".")] == want:
                return True
    return False


def load_declines(cfg: dict, raw: str) -> tuple[str, ...]:
    """The declined managed-unit keys from ``[managed.decline].keep``, in declaration order; a decline reached without that literal header raises :class:`ConfigError`."""
    managed = cfg.get("managed", {})
    if not isinstance(managed, dict):
        raise ConfigError("[managed] must be a table")
    decline = managed.get(DECLINE_KEY, {})
    if not isinstance(decline, dict):
        raise ConfigError("[managed.decline] must be a table")
    unknown = sorted(set(decline) - {"keep"})
    if unknown:
        raise ConfigError(
            f"[managed.decline]: unknown key(s) {', '.join(unknown)}; known keys: keep"
        )
    keep = decline.get("keep", [])
    if not isinstance(keep, list) or not all(isinstance(k, str) and k for k in keep):
        raise ConfigError(
            "[managed.decline].keep must be a list of managed-unit keys, "
            'e.g. ["bin/shipit"]'
        )
    if decline and not _has_table_header(raw, "managed.decline"):
        raise ConfigError(
            "[managed.decline] must be spelled with its own header, not a "
            "dotted key under [managed] — install re-stamps the [managed] body "
            "and would silently drop it. Write it as TWO lines: a "
            "'[managed.decline]' header on a line of its own, then below it the "
            'assignment keep = ["bin/shipit"].'
        )
    return tuple(keep)


def load_lint_ignore(cfg: dict) -> list[str]:
    """The consumer-owned ``[lint].ignore`` globs (gitignore-style, Lang-agnostic), or ``[]`` when absent."""
    section = cfg.get("lint", {})
    if not isinstance(section, dict):
        raise ConfigError("[lint] must be a table")
    ignore = section.get("ignore", [])
    if not isinstance(ignore, list) or not all(isinstance(p, str) for p in ignore):
        raise ConfigError("[lint].ignore must be a list of glob strings")
    return list(ignore)


@dataclass(frozen=True)
class ToolchainEntry:
    """One ``[toolchains]`` entry: a path, its toolchain, and per-tool argv overrides."""

    path: str
    toolchain: str
    commands: Mapping[str, tuple[str, ...]]

    def __post_init__(self) -> None:
        if not isinstance(self.commands, MappingProxyType):
            object.__setattr__(self, "commands", MappingProxyType(dict(self.commands)))


def _parse_argv(where: str, value: object) -> tuple[str, ...]:
    """One declared producing command as argv, never a shell string; ``where`` names the config key for errors."""
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(a, str) and a for a in value)
    ):
        raise ConfigError(
            f"{where} must be a non-empty argv list of "
            f'strings, e.g. ["cargo", "test"]; got {value!r}'
        )
    return tuple(value)


def path_escapes(value: str) -> bool:
    """Whether ``value`` would leave the checkout — absolute, drive-anchored, backslash-bearing, or ``..``-carrying — under BOTH POSIX and Windows rules."""
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    return (
        posix.is_absolute()
        or ".." in posix.parts
        or "\\" in value
        or windows.is_absolute()
        or bool(windows.drive)
        or bool(windows.root)
        or ".." in windows.parts
    )


def _reject_path_escape(where: str, value: str) -> None:
    if path_escapes(value):
        raise ConfigError(
            f"{where}: must be a repo-relative POSIX path inside the checkout — "
            f"no leading '/', no '\\' anywhere, no drive letter, no '..' segment; "
            f"got {value!r}"
        )


def _parse_toolchain_entry(path: str, spec: object) -> ToolchainEntry:
    """One ``[toolchains]`` entry: a bare toolchain name, or a table with ``toolchain`` plus per-tool argv overrides."""
    from .tools import registry

    if not path or path.startswith("/") or ".." in PurePosixPath(path).parts:
        raise ConfigError(
            f"[toolchains] paths are repo-relative and inside the checkout "
            f"({'empty' if not path else path!r} is not); use '.' for the repo root"
        )
    if isinstance(spec, str):
        name, overrides = spec, {}
    elif isinstance(spec, dict):
        name = spec.get("toolchain")
        if not isinstance(name, str) or not name:
            raise ConfigError(
                f"[toolchains].{path} must name its toolchain, e.g. "
                f'{{ toolchain = "rust", test = ["cargo", "test"] }}'
            )
        overrides = {}
        for tool, value in spec.items():
            if tool == "toolchain":
                continue
            if tool not in registry.TOOLS:
                known = ", ".join(registry.TOOLS)
                raise ConfigError(
                    f"[toolchains].{path}: unknown tool slot `{tool}`; "
                    f"known tools: {known}"
                )
            overrides[tool] = _parse_argv(f"[toolchains].{path}.{tool}", value)
    else:
        raise ConfigError(
            f"[toolchains].{path} must be a toolchain name or an inline table, "
            f'e.g. "rust" or {{ toolchain = "rust", test = ["cargo", "test"] }}; '
            f"got {spec!r}"
        )
    if registry.toolchain(name) is None:
        known = ", ".join(registry.names())
        raise ConfigError(
            f"[toolchains].{path}: unknown toolchain `{name}`; "
            f"known toolchains: {known}"
        )
    return ToolchainEntry(
        path=str(PurePosixPath(path)), toolchain=name, commands=overrides
    )


def load_toolchains(cfg: dict) -> tuple[ToolchainEntry, ...]:
    """Parse the ``[toolchains]`` map into typed entries in declaration order; ``()`` when absent."""
    section = cfg.get("toolchains", {})
    if not isinstance(section, dict):
        raise ConfigError("[toolchains] must be a table mapping path -> toolchain")
    return tuple(
        _parse_toolchain_entry(str(path), spec) for path, spec in section.items()
    )


ENDPOINTS: tuple[str, ...] = (
    "gh-release",
    "crates",
    "pypi",
    "npm",
    "vscode-marketplace",
    "open-vsx",
    "brew",
    "notify-downstreams",
    "conda",
    "zed",
)

PLATFORMS: tuple[str, ...] = (
    "darwin-arm64",
    "darwin-x86_64",
    "linux-x86_64",
    "linux-x86_64-musl",
    "linux-arm64",
    "windows-x86_64",
)


@dataclass(frozen=True)
class BuildTarget:
    toolchain: str
    package: str | None = None
    version_var: str | None = None

    @property
    def package_basename(self) -> str | None:
        """The binary name ``package`` yields — its basename — or ``None`` when it names none."""
        if self.package is None:
            return None
        name = PurePosixPath(self.package).name
        return name if name and name not in (".", "..") else None


@dataclass(frozen=True)
class PayloadEntry:
    """One ``bundle.payload`` entry; ``required`` false ships it when present, true fails the bundle when absent."""

    path: str
    required: bool = False


@dataclass(frozen=True)
class BundleSpec:
    """An artifact's declared bundle step: the composition and its per-composition options."""

    composition: str
    command: tuple[str, ...] | None = None
    source: str | None = None
    scope: str | None = None
    wasm_target: str | None = None
    stage: tuple[tuple[str, str], ...] = ()
    leg: str | None = None
    payload: tuple[PayloadEntry, ...] = ()


@dataclass(frozen=True)
class E2eSpec:
    """An artifact's declared e2e harness: ``harness_name`` for a registry entry, ``harness`` for a raw argv override, at most one set."""

    harness: tuple[str, ...] | None = None
    harness_name: str | None = None


@dataclass(frozen=True)
class Artifact:
    """One ``[artifacts]`` entry, fully typed."""

    name: str
    build: tuple[BuildTarget, ...] = ()
    platforms: tuple[str, ...] = ()
    bundle: BundleSpec | None = None
    bundle_config: str | None = None
    main_binary: str | None = None
    product_name: str | None = None
    endpoints: tuple[str, ...] = ()
    downstreams: tuple[str, ...] = ()
    e2e: E2eSpec | None = None
    sign: bool = False


def _reject_unknown_keys(where: str, spec: dict, known: tuple[str, ...]) -> None:
    for key in spec:
        if key not in known:
            raise ConfigError(
                f"{where}: unknown key `{key}`; known keys: {', '.join(known)}"
            )


def _parse_build_target(where: str, spec: object) -> BuildTarget:
    """One ``build`` entry: a bare toolchain name, or a table with ``toolchain`` plus optional ``package``/``version-var``."""
    from .tools import registry

    if isinstance(spec, str):
        if not spec:
            raise ConfigError(
                f"{where}: build target must be a non-empty toolchain name"
            )
        name, package, version_var = spec, None, None
    elif isinstance(spec, dict):
        _reject_unknown_keys(where, spec, ("toolchain", "package", "version-var"))
        name = spec.get("toolchain")
        if not isinstance(name, str) or not name:
            raise ConfigError(
                f"{where} must name its toolchain, e.g. "
                f'{{ toolchain = "rust", package = "my-cli" }}'
            )
        package = spec.get("package")
        if package is not None and (not isinstance(package, str) or not package):
            raise ConfigError(f"{where}: package must be a non-empty string")
        version_var = spec.get("version-var")
        if version_var is not None and (
            not isinstance(version_var, str) or not version_var
        ):
            raise ConfigError(f"{where}: version-var must be a non-empty string")
        if isinstance(version_var, str) and any(ch.isspace() for ch in version_var):
            raise ConfigError(
                f"{where}: version-var must not contain whitespace "
                "(it rides go's -ldflags -X value, ADR-0041)"
            )
    else:
        raise ConfigError(
            f"{where} must be a toolchain name or an inline table, e.g. "
            f'"python" or {{ toolchain = "rust", package = "my-cli" }}; '
            f"got {spec!r}"
        )
    if registry.toolchain(name) is None:
        known = ", ".join(registry.names())
        raise ConfigError(
            f"{where}: unknown toolchain `{name}`; known toolchains: {known}"
        )
    if version_var is not None and name != "go":
        raise ConfigError(
            f"{where}: version-var applies only to the go toolchain "
            f"(ADR-0041: other toolchains carry the version in their manifest)"
        )
    return BuildTarget(toolchain=name, package=package, version_var=version_var)


def _parse_endpoints(where: str, value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(e, str) for e in value):
        raise ConfigError(f"{where}: must be a list of endpoint names")
    for endpoint in value:
        if endpoint not in ENDPOINTS:
            known = ", ".join(ENDPOINTS)
            raise ConfigError(
                f"{where}: unknown endpoint `{endpoint}`; known endpoints: {known}"
            )
    return tuple(value)


def _parse_downstreams(where: str, value: object) -> tuple[str, ...]:
    """The ``downstreams`` list, normalized to canonical ``owner/name`` slugs; duplicates on that canonical key are refused."""
    if not isinstance(value, list) or not all(isinstance(r, str) for r in value):
        raise ConfigError(f"{where}: must be a list of `owner/name` repo slugs")
    seen: set[str] = set()
    canonical: list[str] = []
    for slug in value:
        try:
            canon = repo_from_slug(slug).slug
        except ValueError:
            raise ConfigError(
                f"{where}: `{slug}` is not an `owner/name` repo slug "
                f'(e.g. "lex-fmt/vscode")'
            ) from None
        if canon in seen:
            raise ConfigError(f"{where}: duplicate downstream `{canon}`")
        seen.add(canon)
        canonical.append(canon)
    return tuple(canonical)


def _parse_platforms(where: str, value: object) -> tuple[str, ...]:
    """The ``platforms`` list, validated against the closed :data:`PLATFORMS` registry; duplicates are refused."""
    if not isinstance(value, list) or not all(isinstance(p, str) for p in value):
        raise ConfigError(f"{where}: must be a list of platform names")
    seen: set[str] = set()
    for platform in value:
        if platform not in PLATFORMS:
            known = ", ".join(PLATFORMS)
            raise ConfigError(
                f"{where}: unknown platform `{platform}`; known platforms: {known}"
            )
        if platform in seen:
            raise ConfigError(f"{where}: duplicate platform `{platform}`")
        seen.add(platform)
    return tuple(value)


def _parse_vsix_stage(where: str, value: object) -> tuple[tuple[str, str], ...]:
    """The vsix ``stage`` map as ordered (artifact-dep package, destination path) pairs."""
    if not isinstance(value, dict) or not value:
        raise ConfigError(
            f"{where}.stage: must be a non-empty table mapping an [artifact-deps] "
            f'package to a destination path, e.g. {{ "lexd-lsp" = '
            f'"resources/lexd-lsp" }}; got {value!r}'
        )
    pairs: list[tuple[str, str]] = []
    for pkg, dest in value.items():
        if not _CONDA_PKG_KEY_RE.match(str(pkg)):
            raise ConfigError(
                f"{where}.stage: `{pkg}` is not a valid [artifact-deps] package "
                f"name (lowercase letters, digits, '.', '-', '_'; leading "
                f"alphanumeric)"
            )
        if not isinstance(dest, str) or not dest:
            raise ConfigError(
                f"{where}.stage.{pkg}: destination must be a non-empty "
                f"repo-relative path under the extension layout, e.g. "
                f'"resources/lexd-lsp"; got {dest!r}'
            )
        _reject_path_escape(f"{where}.stage.{pkg}", dest)
        pairs.append((str(pkg), str(PurePosixPath(dest))))
    return tuple(pairs)


_PAYLOAD_EXAMPLE = 'payload = [{ path = "src", required = true }, { path = "queries" }]'


def _paths_overlap(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    """Whether two normalized POSIX paths name the same member or one contains the other, compared by path component."""
    shorter, longer = sorted((left, right), key=len)
    return longer[: len(shorter)] == shorter


def _parse_payload(where: str, value: object) -> tuple[PayloadEntry, ...]:
    """The ``bundle.payload`` list as typed entries in declaration order; at least one must be ``required``."""
    if not isinstance(value, list) or not value:
        raise ConfigError(
            f"{where}.payload: must be a non-empty list of entry tables, e.g. "
            f"{_PAYLOAD_EXAMPLE}; got {value!r}"
        )
    entries: list[PayloadEntry] = []
    seen: set[tuple[str, ...]] = set()
    for index, item in enumerate(value):
        at = f"{where}.payload[{index}]"
        if not isinstance(item, dict):
            raise ConfigError(
                f"{at}: must be a table naming its path (and, for the package's "
                f'core, `required = true`), e.g. {{ path = "src", required = '
                f"true }}; got {item!r}"
            )
        _reject_unknown_keys(at, item, ("path", "required"))
        path = item.get("path")
        if not isinstance(path, str) or not path:
            raise ConfigError(
                f"{at}.path must be a non-empty repo-relative path under the "
                f"`{where}.leg` leg — a file, a directory, or a nested path, e.g. "
                f'"src", "grammar.js", "shared/embedded-grammars.json"; '
                f"got {path!r}"
            )
        _reject_path_escape(f"{at}.path", path)
        required = item.get("required", False)
        if not isinstance(required, bool):
            raise ConfigError(
                f"{at}.required must be a boolean (absent = the entry ships "
                f"WHEN PRESENT); got {required!r}"
            )
        normalized = str(PurePosixPath(path))
        parts = PurePosixPath(normalized).parts
        if not parts:
            raise ConfigError(
                f"{at}.path: `{path}` names the leg directory itself, not a "
                f"payload member — declare the explicit files or subdirectories "
                f'to ship, e.g. "src" or "extension.toml"'
            )
        overlap = next((p for p in seen if _paths_overlap(parts, p)), None)
        if overlap is not None:
            joined = str(PurePosixPath(*overlap))
            raise ConfigError(
                f"{at}.path: `{normalized}` overlaps `{joined}`, already declared "
                f"earlier in {where}.payload — a directory entry ships its whole "
                f"tree, so each archive member is declared exactly once; drop "
                f"whichever of the two is redundant"
            )
        seen.add(parts)
        entries.append(PayloadEntry(path=normalized, required=required))
    if not any(entry.required for entry in entries):
        raise ConfigError(
            f"{where}.payload: no entry declares `required = true` — an "
            f"all-when-present payload can compose an EMPTY archive, which is "
            f"never a quiet outcome. Mark the package's core entry (the one "
            f"whose absence means the build never ran), e.g. {_PAYLOAD_EXAMPLE}"
        )
    return tuple(entries)


def _parse_bundle(where: str, spec: object) -> BundleSpec:
    from .release import bundle as bundle_registry

    if not isinstance(spec, dict):
        raise ConfigError(
            f"{where}.bundle: must be a table, e.g. "
            f'{{ composition = "archive" }}; got {spec!r}'
        )
    composition = spec.get("composition")
    if not isinstance(composition, str) or not composition:
        raise ConfigError(
            f"{where}.bundle must name its composition, e.g. "
            f'{{ composition = "archive" }}'
        )
    entry = bundle_registry.composition(composition)
    if entry is None:
        known = ", ".join(bundle_registry.names())
        raise ConfigError(
            f"{where}.bundle: unknown composition `{composition}`; "
            f"known compositions: {known}"
        )
    _reject_unknown_keys(
        f"{where}.bundle",
        spec,
        ("composition", "command", "source", "leg", "payload", *entry.option_keys),
    )
    if not entry.declared_payload:
        declared = ", ".join(bundle_registry.declared_payload_names())
        for key in ("leg", "payload"):
            if key in spec:
                raise ConfigError(
                    f"{where}.bundle: `{key}` applies only to compositions whose "
                    f"archive contents are producer-declared ({declared}); "
                    f"composition `{composition}` assembles its own payload"
                )
    stage = (
        _parse_vsix_stage(f"{where}.bundle", spec["stage"]) if "stage" in spec else ()
    )
    command = spec.get("command")
    source = spec.get("source")
    if entry.declared_command:
        if command is None:
            raise ConfigError(
                f"{where}.bundle: composition `{composition}` runs the "
                f"artifact's own bundler — declare its argv, e.g. "
                f'command = ["npm", "run", "tauri", "build"]'
            )
        if not isinstance(source, str) or not source:
            raise ConfigError(
                f"{where}.bundle: composition `{composition}` needs `source` — "
                f"the repo-relative directory the bundler leaves its bundles "
                f"under (the mac .app/.dmg pair, tauri linux .AppImage/.deb), "
                f'e.g. "src-tauri/target/release/bundle"'
            )
        _reject_path_escape(f"{where}.bundle.source", source)
        normalized = str(PurePosixPath(source))
        if normalized == ".":
            raise ConfigError(
                f"{where}.bundle.source: composition `{composition}` needs a "
                f"dedicated bundle output subdirectory, not the repo root "
                f'(`.`) — e.g. "src-tauri/target/release/bundle"'
            )
        return BundleSpec(
            composition=composition,
            command=_parse_argv(f"{where}.bundle.command", command),
            source=normalized,
        )
    for key in ("command", "source"):
        if key in spec:
            raise ConfigError(
                f"{where}.bundle: `{key}` applies only to compositions that "
                f"run a declared bundler (mac-app, tauri); composition "
                f"`{composition}` assembles its own commands"
            )
    leg: str | None = None
    payload: tuple[PayloadEntry, ...] = ()
    if entry.declared_payload:
        from .tools import registry

        missing = [key for key in ("leg", "payload") if key not in spec]
        if missing:
            raise ConfigError(
                f"{where}.bundle: composition `{composition}` ships a "
                f"PRODUCER-DECLARED payload and is missing "
                f"{' and '.join(f'`{key}`' for key in missing)}. shipit no "
                f"longer carries a built-in file list for `{composition}` "
                f"(#1092, ADR-0077): the repo states its own contents, so "
                f"shipping one more file is a config edit, not a shipit "
                f"release. Declare the [toolchains] leg the paths are relative "
                f'to plus the files, e.g. leg = "tree-sitter" and '
                f"{_PAYLOAD_EXAMPLE}"
            )
        leg = spec["leg"]
        if not isinstance(leg, str) or not leg:
            raise ConfigError(
                f"{where}.bundle.leg must be a non-empty [toolchains] toolchain "
                f'name — the leg the payload paths are relative to, e.g. "tree-'
                f'sitter"; got {leg!r}'
            )
        if registry.toolchain(leg) is None:
            known = ", ".join(registry.names())
            raise ConfigError(
                f"{where}.bundle.leg: unknown toolchain `{leg}`; known "
                f"toolchains: {known}"
            )
        payload = _parse_payload(f"{where}.bundle", spec["payload"])
    scope = spec.get("scope")
    if scope is not None and (not isinstance(scope, str) or not scope):
        raise ConfigError(f"{where}.bundle: scope must be a non-empty string")
    wasm_target = spec.get("wasm-target")
    if wasm_target is not None and (
        not isinstance(wasm_target, str) or not wasm_target
    ):
        raise ConfigError(f"{where}.bundle: wasm-target must be a non-empty string")
    return BundleSpec(
        composition=composition,
        scope=scope,
        wasm_target=wasm_target,
        stage=stage,
        leg=leg,
        payload=payload,
    )


def _parse_e2e(where: str, spec: object) -> E2eSpec:
    if not isinstance(spec, dict):
        raise ConfigError(
            f"{where}.e2e: must be a table (empty for the default harness), "
            f'e.g. {{}}, {{ harness = "electron" }}, or '
            f'{{ harness = ["bats", "tests/e2e.bats"] }}; got {spec!r}'
        )
    _reject_unknown_keys(f"{where}.e2e", spec, ("harness",))
    if "harness" not in spec:
        return E2eSpec()
    harness = spec["harness"]
    if isinstance(harness, str):
        if not harness:
            raise ConfigError(
                f"{where}.e2e.harness: a named harness must be a non-empty "
                f'string (e.g. "electron", "tauri"), or declare a raw argv '
                f'list (e.g. ["bats", "tests/e2e.bats"])'
            )
        return E2eSpec(harness_name=harness)
    return E2eSpec(harness=_parse_argv(f"{where}.e2e.harness", harness))


def _parse_artifact(name: str, spec: object) -> Artifact:
    where = f"[artifacts].{name}"
    if not isinstance(spec, dict):
        raise ConfigError(f"{where} must be a table; got {spec!r}")
    _reject_unknown_keys(
        where,
        spec,
        (
            "build",
            "platforms",
            "bundle",
            "bundle-config",
            "endpoints",
            "downstreams",
            "e2e",
            "main-binary",
            "product-name",
            "sign",
        ),
    )
    bundle_config = spec.get("bundle-config")
    if bundle_config is not None:
        if not isinstance(bundle_config, str) or not bundle_config:
            raise ConfigError(
                f"{where}.bundle-config: must be a non-empty repo-relative path, "
                f'e.g. "src-tauri/tauri.conf.json"; got {bundle_config!r}'
            )
        _reject_path_escape(f"{where}.bundle-config", bundle_config)
        bundle_config = str(PurePosixPath(bundle_config))
    build_spec = spec.get("build", [])
    if not isinstance(build_spec, list):
        raise ConfigError(f"{where}.build: must be a list of build targets")
    build = tuple(
        _parse_build_target(f"{where}.build[{i}]", entry)
        for i, entry in enumerate(build_spec)
    )
    sign = spec.get("sign", False)
    if not isinstance(sign, bool):
        raise ConfigError(f"{where}.sign: must be a boolean; got {sign!r}")
    names = {}
    for key in ("main-binary", "product-name"):
        value = spec.get(key)
        if value is not None and (not isinstance(value, str) or not value):
            raise ConfigError(f"{where}.{key}: must be a non-empty name; got {value!r}")
        names[key] = value
    platforms = _parse_platforms(f"{where}.platforms", spec.get("platforms", []))
    if sign:
        if not build:
            raise ConfigError(
                f"{where}: sign = true requires at least one build target "
                f"(an artifact with no build produces nothing to sign)"
            )
        if not any(platform.startswith("darwin") for platform in platforms):
            raise ConfigError(
                f"{where}: sign = true requires at least one darwin platform "
                f"(signing runs on macOS only); declare a darwin lane in "
                f"`platforms` or drop `sign`"
            )
    bundle = _parse_bundle(where, spec["bundle"]) if "bundle" in spec else None
    if bundle is not None and not build:
        raise ConfigError(
            f"{where}: bundle requires at least one build target "
            f"(a bundle composes build outputs; an artifact with no build "
            f"produces nothing to bundle)"
        )
    if sign:
        from .release import bundle as bundle_registry

        signable = bundle_registry.signable_names()
        if bundle is None or bundle.composition not in signable:
            got = (
                f"composition `{bundle.composition}`"
                if bundle is not None
                else "no bundle"
            )
            raise ConfigError(
                f"{where}: sign = true requires a bundle composition the "
                f"signer can reopen ({', '.join(signable)}); got {got}"
            )
    if bundle is not None and len(platforms) > 1:
        from .release import bundle as bundle_registry

        if bundle.composition in bundle_registry.platform_independent_names():
            raise ConfigError(
                f"{where}: composition `{bundle.composition}` is platform-"
                f"independent — it emits one unqualified archive, so declaring "
                f"more than one platform would build colliding assets; declare "
                f"at most one platform (or none — it defaults to a single lane)"
            )
    endpoints = _parse_endpoints(f"{where}.endpoints", spec.get("endpoints", []))
    downstreams = _parse_downstreams(
        f"{where}.downstreams", spec.get("downstreams", [])
    )
    if "notify-downstreams" in endpoints and not downstreams:
        raise ConfigError(
            f"{where}: the notify-downstreams endpoint needs a `downstreams` "
            f"list — the `owner/name` repos it fires repository_dispatch at "
            f'(e.g. downstreams = ["lex-fmt/vscode", "lex-fmt/nvim"])'
        )
    if downstreams and "notify-downstreams" not in endpoints:
        raise ConfigError(
            f"{where}: `downstreams` is declared but the notify-downstreams "
            f"endpoint is not — add it to `endpoints`, or drop `downstreams` "
            f"(a list nothing fires is dead config)"
        )
    return Artifact(
        name=name,
        build=build,
        platforms=platforms,
        bundle=bundle,
        bundle_config=bundle_config,
        main_binary=names["main-binary"],
        product_name=names["product-name"],
        endpoints=endpoints,
        downstreams=downstreams,
        e2e=_parse_e2e(where, spec["e2e"]) if "e2e" in spec else None,
        sign=sign,
    )


def load_artifacts(cfg: dict) -> tuple[Artifact, ...]:
    """Parse the ``[artifacts]`` map into typed values in declaration order; ``()`` when absent."""
    section = cfg.get("artifacts", {})
    if not isinstance(section, dict):
        raise ConfigError("[artifacts] must be a table of artifact declarations")
    return tuple(_parse_artifact(str(name), spec) for name, spec in section.items())


_ARTIFACT_DEP_KEYS = ("repo", "feature")

_CONDA_PKG_KEY_RE = re.compile(r"[a-z0-9][a-z0-9._-]*\Z")

_FEATURE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


@dataclass(frozen=True)
class ArtifactDep:
    """One ``[artifact-deps.<pkg>]`` entry: package name, producing ``repo`` slug, optional pixi ``feature``; the version is consumer-owned and never declared here."""

    package: str
    repo: str
    feature: str | None = None


def _parse_artifact_dep(package: str, spec: object) -> ArtifactDep:
    where = f"[artifact-deps].{package}"
    if not _CONDA_PKG_KEY_RE.match(package):
        raise ConfigError(
            f"{where}: the section key is the conda package name and must be a "
            f"valid conda package identifier (LOWERCASE letters, digits, '.', "
            f"'-', '_'); got {package!r}"
        )
    if not isinstance(spec, dict):
        raise ConfigError(
            f'{where} must be a table, e.g. {{ repo = "owner/name" }}; got {spec!r}'
        )
    if "version" in spec:
        raise ConfigError(
            f"{where}.version is no longer allowed (conda-direct, ADR-0077): the "
            f"version is consumer-owned. Remove it here and declare the pin in the "
            f"artifact's pixi feature — the SAME feature that carries the derived "
            f"channel — e.g. `[feature.shipit-artifacts.dependencies] {package} = "
            f'"{spec["version"]}"` (or `[feature.shipit-artifacts-<feature>'
            f".dependencies]` for a named `feature`), so `pixi.lock` resolves it "
            f"against the channel and a generic bot bumps it."
        )
    _reject_unknown_keys(where, spec, _ARTIFACT_DEP_KEYS)

    repo = spec.get("repo")
    if not isinstance(repo, str) or not repo:
        raise ConfigError(
            f"{where}.repo must be the producing repo's `owner/name` slug, "
            f'e.g. "lex-fmt/lex"; got {repo!r}'
        )
    try:
        canonical = repo_from_slug(repo).slug
    except ValueError as exc:
        raise ConfigError(f"{where}.repo: {exc}") from exc

    feature = spec.get("feature")
    if feature is not None:
        if not isinstance(feature, str) or not _FEATURE_NAME_RE.match(feature):
            raise ConfigError(
                f"{where}.feature must be a pixi feature name (alphanumerics, "
                f"'.', '-', '_'); got {feature!r}"
            )

    return ArtifactDep(package=package, repo=canonical, feature=feature)


def load_artifact_deps(cfg: dict) -> tuple[ArtifactDep, ...]:
    """Parse the ``[artifact-deps]`` map into typed values in declaration order; ``()`` when absent."""
    section = cfg.get("artifact-deps", {})
    if not isinstance(section, dict):
        raise ConfigError(
            "[artifact-deps] must be a table of `<pkg>` artifact declarations"
        )
    return tuple(_parse_artifact_dep(str(name), spec) for name, spec in section.items())


_STAGING_ROOT = "resources"


@dataclass(frozen=True)
class StageEntry:
    """One resolved ``[stage.<pkg>]`` copy: ``source`` is env-prefix-relative, ``dest`` a strict descendant of ``resources/``."""

    package: str
    source: str
    dest: str


def _parse_stage_table(package: str, spec: object) -> tuple[StageEntry, ...]:
    where = f"[stage.{package}]"
    if not _CONDA_PKG_KEY_RE.match(package):
        raise ConfigError(
            f"{where}: the section key is the conda package name and must be a "
            f"valid conda package identifier (LOWERCASE letters, digits, '.', "
            f"'-', '_'); got {package!r}"
        )
    if not isinstance(spec, dict) or not spec:
        raise ConfigError(
            f"{where} must be a non-empty table mapping a source-in-prefix path to "
            f'a destination under {_STAGING_ROOT}/, e.g. {{ "bin/lexd-lsp" = '
            f'"{_STAGING_ROOT}/lexd-lsp" }}; got {spec!r}'
        )
    entries: list[StageEntry] = []
    for source, dest in spec.items():
        src = str(source)
        if not src:
            raise ConfigError(f"{where}: a source path must be non-empty")
        _reject_path_escape(f"{where} {src!r} (source)", src)
        if not isinstance(dest, str) or not dest:
            raise ConfigError(
                f"{where} {src!r}: destination must be a non-empty path under "
                f'{_STAGING_ROOT}/, e.g. "{_STAGING_ROOT}/lexd-lsp"; got {dest!r}'
            )
        _reject_path_escape(f"{where} {src!r} (dest)", dest)
        dest_parts = PurePosixPath(dest).parts
        if len(dest_parts) < 2 or dest_parts[0] != _STAGING_ROOT:
            raise ConfigError(
                f"{where} {src!r}: destination must be a path UNDER the staging root "
                f"{_STAGING_ROOT}/ (a strict descendant, e.g. "
                f'"{_STAGING_ROOT}/lexd-lsp"); got {dest!r} — staging is bounded to '
                f"the shipped-bundle dir so it can never touch the checkout root, "
                f".git, or the env"
            )
        entries.append(
            StageEntry(
                package=package,
                source=str(PurePosixPath(src)),
                dest=str(PurePosixPath(dest)),
            )
        )
    return tuple(entries)


def load_stage(cfg: dict) -> tuple[StageEntry, ...]:
    """Parse the ``[stage]`` map into typed values in declaration order; ``()`` when absent, overlapping dests refused."""
    section = cfg.get("stage", {})
    if not isinstance(section, dict):
        raise ConfigError(
            "[stage] must be a table of `<pkg>` stage-from-prefix declarations"
        )
    entries: list[StageEntry] = []
    seen_dest: dict[str, str] = {}
    for package, table in section.items():
        for entry in _parse_stage_table(str(package), table):
            dest_path = PurePosixPath(entry.dest)
            for seen, seen_pkg in seen_dest.items():
                seen_path = PurePosixPath(seen)
                if dest_path.is_relative_to(seen_path) or seen_path.is_relative_to(
                    dest_path
                ):
                    kind = "duplicate" if entry.dest == seen else "overlapping"
                    raise ConfigError(
                        f"[stage] {kind} destination {entry.dest!r} (from "
                        f"[stage.{entry.package}]) and {seen!r} (from "
                        f"[stage.{seen_pkg}]) — destinations must be DISJOINT, since "
                        f"the idempotent re-run rmtrees each dest subtree and an "
                        f"ancestor stage would silently drop a sibling's staged "
                        f"files from the bundle; give each a distinct, "
                        f"non-nested destination"
                    )
            seen_dest[entry.dest] = entry.package
            entries.append(entry)
    return tuple(entries)


LANE_TRIGGERS = frozenset({"pr", "push", "nightly", "dispatch"})

_LANE_KEYS = frozenset(
    {"run", "required", "local", "trigger", "runner", "scope", "secrets"}
)

_SECRET_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def _valid_secret_name(name: object) -> bool:
    """Whether ``name`` is a declarable GitHub secret identifier: alnum/underscore, no leading digit, no ``GITHUB_`` prefix."""
    return (
        isinstance(name, str)
        and bool(_SECRET_NAME_RE.match(name))
        and not name.upper().startswith("GITHUB_")
    )


_LANE_SECRET_SLOTS = frozenset({"lane_token"})


@dataclass(frozen=True)
class Lane:
    """One declared CI test unit; ``secrets`` is the allowlist of secret input slots this lane may be handed."""

    name: str
    run: str
    required: bool = False
    local: bool = False
    trigger: str = "pr"
    runner: str | None = None
    scope: str | None = None
    secrets: tuple[str, ...] = ()


CHANGELOG_SYNC_LANE = Lane(
    name="changelog-sync",
    run="changelog check",
    required=True,
    local=False,
    trigger="pr",
)


def _parse_lane(name: str, spec: object) -> Lane:
    if not isinstance(spec, dict):
        raise ConfigError(
            f"[lanes].{name} must be a table, e.g. "
            f'{{ run = "changelog check", required = true }}; got {spec!r}'
        )
    unknown = sorted(set(spec) - _LANE_KEYS)
    if unknown:
        known = ", ".join(sorted(_LANE_KEYS))
        raise ConfigError(
            f"[lanes].{name}: unknown key(s) {', '.join(unknown)}; known keys: {known}"
        )
    run = spec.get("run")
    if not isinstance(run, str) or not run.strip():
        raise ConfigError(
            f"[lanes].{name}: `run` must be a non-empty string naming a shipit "
            "tool or leg invocation"
        )
    required = spec.get("required", False)
    local = spec.get("local", False)
    if not isinstance(required, bool) or not isinstance(local, bool):
        raise ConfigError(f"[lanes].{name}: `required`/`local` must be booleans")
    trigger = spec.get("trigger", "pr")
    if not isinstance(trigger, str) or trigger not in LANE_TRIGGERS:
        allowed = ", ".join(sorted(LANE_TRIGGERS))
        raise ConfigError(
            f"[lanes].{name}: `trigger` must be one of {allowed}; got {trigger!r}"
        )
    cleaned: dict[str, str | None] = {}
    for key, value in (("runner", spec.get("runner")), ("scope", spec.get("scope"))):
        if value is None:
            cleaned[key] = None
        elif not isinstance(value, str) or not value.strip():
            raise ConfigError(f"[lanes].{name}: `{key}` must be a non-empty string")
        else:
            cleaned[key] = value.strip()
    secrets = _parse_lane_secrets(name, spec.get("secrets"))
    return Lane(
        name=name,
        run=run.strip(),
        required=required,
        local=local,
        trigger=trigger,
        runner=cleaned["runner"],
        scope=cleaned["scope"],
        secrets=secrets,
    )


def _parse_lane_secrets(name: str, value: object) -> tuple[str, ...]:
    """A lane's ``secrets`` allowlist as an ordered, deduplicated tuple; ``()`` when absent."""
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConfigError(
            f"[lanes].{name}: `secrets` must be a list of secret names, e.g. "
            '`secrets = ["lane_token"]`'
        )
    for entry in value:
        if not _valid_secret_name(entry):
            raise ConfigError(
                f"[lanes].{name}: `secrets` entry {entry!r} is not a valid GitHub "
                "secret name (alphanumerics/underscore, no leading digit, no "
                "`GITHUB_` prefix)"
            )
        if entry not in _LANE_SECRET_SLOTS:
            slots = ", ".join(sorted(_LANE_SECRET_SLOTS))
            raise ConfigError(
                f"[lanes].{name}: `secrets` entry {entry!r} is not a "
                f"workflow-supported secret slot (known slots: {slots}). The "
                "`wf-checks.yml` block routes only these; a well-formed but "
                "unlisted name would ride the matrix and receive nothing."
            )
    return tuple(dict.fromkeys(value))


def load_lanes(cfg: dict) -> list[Lane]:
    """Parse the ``[lanes]`` table into ordered, typed declarations; ``[]`` when absent."""
    lanes = cfg.get("lanes", {})
    if not isinstance(lanes, dict):
        raise ConfigError("[lanes] must be a table of lane tables")
    return [_parse_lane(name, spec) for name, spec in lanes.items()]


def shipit_version(cfg: dict) -> str | None:
    section = cfg.get("shipit", {})
    if not isinstance(section, dict):
        raise ConfigError("[shipit] must be a table")
    value = section.get("version")
    return str(value) if value is not None else None


def shipit_pin(path: str | Path) -> str | None:
    """The shipit pin in the ``.shipit.toml`` at ``path``; ``None`` when the file is absent, malformed, pinless, or carrying a non-sha version."""
    p = Path(path)
    if not p.is_file():
        return None
    try:
        with p.open("rb") as fh:
            cfg = tomllib.load(fh)
    except tomllib.TOMLDecodeError:
        return None
    try:
        raw = shipit_version(cfg)
    except ConfigError:
        return None
    if raw is None:
        return None
    try:
        return str(Sha(raw))
    except ValueError:
        return None


def _toml_key(key: str) -> str:
    if _BARE_KEY.fullmatch(key):
        return key
    return '"' + key.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _strip_tables(text: str, tables: set[str]) -> str:
    out: list[str] = []
    skipping = False
    for line in text.splitlines():
        stripped = line.split("#", 1)[0].strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            name = stripped.strip("[]").strip()
            skipping = name in tables
            if skipping:
                continue
        if skipping:
            continue
        out.append(line)
    return "\n".join(out).strip()


def dump_manifest(version: str, managed: dict[str, str]) -> str:
    lines = ["[shipit]", f'version = "{version}"', "", "[managed]"]
    for key, value in managed.items():
        lines.append(f'{_toml_key(key)} = "{value}"')
    return "\n".join(lines) + "\n"


def write_manifest(path: str | Path, *, version: str, managed: dict[str, str]) -> None:
    """Write the ``[shipit]``/``[managed]`` tables, preserving every other table verbatim."""
    p = Path(path)
    existing = p.read_text(encoding="utf-8") if p.is_file() else ""
    kept = _strip_tables(existing, {"shipit", "managed"})
    block = dump_manifest(version, managed)
    text = f"{kept}\n\n{block}" if kept else block
    p.write_text(text, encoding="utf-8")


def seeded_app_secrets() -> tuple[str, ...]:
    """The GitHub secret names ``shipit install`` seeds into ``[secrets]``, in Backend-registry order."""
    from .agent import backend

    return tuple(
        key
        for b in backend.funnel_backends()
        for key in (b.doppler_pem_key, b.doppler_app_id_key)
    )


_SECRETS_SCAFFOLD_HEADER = """\
# [secrets] — repo Actions secrets. Each table key is the GitHub secret NAME; the
# value names exactly one source ({ doppler = "KEY" } / { env = "VAR" } /
# { prompt = true }). Seeded with shipit's local-reviewer (codex/agy) GitHub App
# credentials, each sourced from Doppler github/prd. `shipit gh-setup` pushes an
# App credential only when its reviewer is declared in [reviewers]; an undeclared
# pair is flagged as an orphan (not pushed), so seeding is safe before opt-in.
[secrets]"""


def secrets_scaffold() -> str:
    names = seeded_app_secrets()
    width = max((len(n) for n in names), default=0)
    lines = [f'{n:<{width}} = {{ doppler = "{n}" }}' for n in names]
    return "\n".join([_SECRETS_SCAFFOLD_HEADER, *lines]) + "\n"


_REVIEWERS_SCAFFOLD_HEADER = """\
# [reviewers] — the required-reviewer SET for this repo's PRs (the map KEYS are
# required; ALL must be DONE to flip Ready). Seeded with shipit's shipped default
# (Copilot, review-once), rendered from the single source in
# `prstate.reviewers_config.DEFAULT_REVIEWERS`. codex/agy are NOT seeded by default —
# their review GitHub Apps are not installed on an arbitrary repo, so requiring them
# would park PRs at REVIEWS_PENDING; a repo that HAS the Apps opts them in here (e.g.
# `codex = {}`). `rerun` defaults ON (head-strict — re-review every push, ADR-0043);
# a metered reviewer opts OUT to review-once with e.g. `copilot = { rerun = false }`
# (which is exactly why the shipped Copilot default is review-once)."""


def reviewers_scaffold() -> str:
    from .prstate import reviewers_config

    return f"{_REVIEWERS_SCAFFOLD_HEADER}\n{reviewers_config.default_reviewers_scaffold_body()}"


_LINT_SEED_IGNORE: tuple[str, ...] = (
    "CHANGELOG.md",
    "CHANGELOG/**",
    "package-lock.json",
    "pnpm-lock.yaml",
)

_LINT_SCAFFOLD_HEADER = """\
# [lint].ignore — paths the managed lint gate must SKIP. Seeded with common
# generated/assembled files (a built CHANGELOG, package lockfiles) that are not
# hand-written prose, so a freshly-onboarded repo does not take a latent gate
# failure on them. Gitignore-style globs. You OWN this list and may extend it —
# it is reconcile-safe: `shipit install` seeds [lint] only when absent and never
# clobbers a table you have edited."""


def lint_scaffold() -> str:
    entries = ",\n".join(f'  "{g}"' for g in _LINT_SEED_IGNORE)
    return f"{_LINT_SCAFFOLD_HEADER}\n[lint]\nignore = [\n{entries},\n]"


SIGNAL_MANIFESTS: tuple[tuple[str, str], ...] = (
    ("Cargo.toml", "rust"),
    ("go.mod", "go"),
    ("pyproject.toml", "python"),
    ("package.json", "npm"),
)


def derive_toolchains(root: Path) -> tuple[tuple[str, str], ...]:
    """The ``[toolchains]`` entries derived from ``root``'s manifests — root-level only, first signal wins; ``()`` when none signals."""
    for name, toolchain in SIGNAL_MANIFESTS:
        if (Path(root) / name).is_file():
            return ((".", toolchain),)
    return ()


_TOOLCHAINS_SCAFFOLD_HEADER = """\
# [toolchains] — the path->toolchain map `shipit test`/`shipit build` dispatch
# on (ADR-0007/0039): each build-bearing path declares its toolchain. Seeded
# from this repo's root manifests. You OWN this map and may extend it — nested
# paths, or per-tool argv overrides, e.g.
#   "crates/cli" = { toolchain = "rust", test = ["cargo", "test"] }
# It is reconcile-safe: `shipit install` seeds [toolchains] only when absent
# and never clobbers a map you have edited.
[toolchains]"""


def toolchains_scaffold(entries: Sequence[tuple[str, str]]) -> str:
    lines = [f'"{path}" = "{toolchain}"' for path, toolchain in entries]
    return "\n".join([_TOOLCHAINS_SCAFFOLD_HEADER, *lines])


def _seeded_secret_line(name: str) -> str:
    return f'{name} = {{ doppler = "{name}" }}'


def _config_text(path: str | Path) -> str:
    p = Path(path)
    return p.read_text(encoding="utf-8") if p.is_file() else ""


def _parse_text(text: str, path: str | Path) -> dict:
    if not text.strip():
        return {}
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"malformed {path}: {exc}") from None


def _require_table(cfg: dict, name: str, path: str | Path) -> dict | None:
    """``cfg[name]`` when it is a table (absent → ``None``); raises :class:`ConfigError` when present and not a table."""
    val = cfg.get(name)
    if val is None or isinstance(val, dict):
        return val
    raise ConfigError(
        f"malformed {path}: `{name}` must be a table, not {type(val).__name__}"
    )


def _plan_seed(
    text: str, path: str | Path, toolchains: Sequence[tuple[str, str]] = ()
) -> tuple[list[str], str]:
    """The seed-if-absent items missing from ``text`` and the resulting file text; pure."""
    cfg = _parse_text(text, path)
    secrets = _require_table(cfg, "secrets", path)
    _require_table(cfg, "reviewers", path)
    _require_table(cfg, "lint", path)
    _require_table(cfg, "toolchains", path)

    missing = [n for n in seeded_app_secrets() if n not in (secrets or {})]
    seeded: list[str] = []
    if missing:
        if secrets is None:
            text = _append_lines(text, secrets_scaffold().splitlines())
        else:
            text = _insert_after_header(
                text, "secrets", [_seeded_secret_line(n) for n in missing], path
            )
        seeded += [f"[secrets].{n}" for n in missing]

    if "reviewers" not in cfg:
        text = _append_lines(text, reviewers_scaffold().splitlines())
        seeded.append("[reviewers]")

    if "lint" not in cfg:
        text = _append_lines(text, lint_scaffold().splitlines())
        seeded.append("[lint].ignore")

    if toolchains and "toolchains" not in cfg:
        text = _append_lines(text, toolchains_scaffold(toolchains).splitlines())
        seeded.append("[toolchains]")
    return seeded, text


def plan_policy_seed(
    path: str | Path, *, toolchains: Sequence[tuple[str, str]] = ()
) -> list[str]:
    """What seed-if-absent policy ``shipit install`` would add to ``path``; empty when it is already in place."""
    return _plan_seed(_config_text(path), path, toolchains)[0]


def apply_policy_seed(
    path: str | Path, *, toolchains: Sequence[tuple[str, str]] = ()
) -> list[str]:
    """Seed-if-absent the consumer policy into ``path`` and return what was seeded; writes only when something is."""
    seeded, text = _plan_seed(_config_text(path), path, toolchains)
    if seeded:
        Path(path).write_text(text, encoding="utf-8")
    return seeded


def _append_lines(text: str, lines: list[str]) -> str:
    base = text.rstrip("\n")
    sep = "\n\n" if base else ""
    return f"{base}{sep}" + "\n".join(lines) + "\n"


def _insert_after_header(
    text: str, table: str, lines: list[str], path: str | Path
) -> str:
    """Insert ``lines`` after the ``[table]`` header; raises :class:`ConfigError` when the table has no literal header."""
    header = re.compile(rf"^\s*\[\s*{re.escape(table)}\s*\]\s*(#.*)?$")
    rows = text.splitlines()
    for idx, row in enumerate(rows):
        if header.match(row):
            return "\n".join(rows[: idx + 1] + lines + rows[idx + 1 :]) + "\n"
    raise ConfigError(
        f"malformed {path}: cannot seed [{table}] — no `[{table}]` header to merge "
        f"under (inline table or dotted keys?)"
    )
