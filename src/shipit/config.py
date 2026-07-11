"""Reading ``.shipit.toml`` — shipit's policy config.

``.shipit.toml`` owns policy (the secret map, reviewers, the path→toolchain map,
the declared ``[lanes]``, the pristine hashes); ``pixi.toml`` owns provisioning.
They describe different layers, so there is no split-brain
(docs/dev/architecture.lex §6). Step 1 needs only the ``[secrets]`` table.

The ``[secrets]`` table maps a GitHub secret NAME (the table key) to exactly one
source:

    [secrets]
    CARGO_REGISTRY_TOKEN = { doppler = "CRATES_IO_KEY" }
    GH_PAT               = { env = "SHIPIT_GH_PAT" }
    MANUAL_TOKEN         = { prompt = true }
    SCCACHE_GCS_KEY      = { doppler = "SCCACHE_GCS_KEY", optional = true }

``optional = true`` marks a source whose absence is a skip, not a fatal error.
"""

from __future__ import annotations

import hashlib
import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType

from .identity import Sha

CONFIG_NAME = ".shipit.toml"

_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")


class ConfigError(RuntimeError):
    """``.shipit.toml`` is missing or malformed."""


# A CLOSED registry of the top-level tables shipit knows. Adding a table = adding
# an entry HERE (mirror of the review-backend ``_REGISTRY``). Validation rejects any
# top-level table NOT in this set so a typo (``[secretz]``) dies fast instead of
# being silently ignored. ``project`` (alias: ``custom``) is the consumer-owned
# escape hatch — known so validation accepts it, but its SUBTREE is never descended or
# policed.
_KNOWN_TABLES = {
    "secrets",
    "reviewers",
    "managed",
    "shipit",
    "project",
    "lint",
    "toolchains",
    "artifacts",
    "lanes",
}
_ESCAPE_HATCH_TABLES = {"project", "custom"}


def _validate_known_tables(cfg: dict) -> None:
    """Reject any top-level table not in the closed :data:`_KNOWN_TABLES` registry.

    The ``project`` / ``custom`` escape-hatch tables are allowed and their subtree
    is NOT descended or validated — consumers own that namespace. Raises
    :class:`ConfigError` naming the offending key and listing the known set.
    """
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
    """One ``[secrets]`` entry: the GitHub secret ``name`` and where it comes from.

    Exactly one of ``doppler`` / ``env`` / ``prompt`` is set, mirroring the
    single-source-per-entry schema.
    """

    name: str
    kind: str  # "doppler" | "env" | "prompt"
    key: str | None  # doppler KEY or env VAR; None for prompt
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
    """Parse a ``[secrets]`` table (already loaded) into ordered sources."""
    secrets = spec.get("secrets", {})
    if not isinstance(secrets, dict):
        raise ConfigError("[secrets] must be a table")
    return [_parse_secret(name, value) for name, value in secrets.items()]


def load(path: str | Path) -> dict:
    """Parse a ``.shipit.toml`` file into a dict, or raise :class:`ConfigError`.

    "Malformed" spans every way the bytes resist parsing: bad TOML syntax AND
    non-UTF-8 content — :mod:`tomllib` decodes the file as UTF-8, and the
    resulting ``UnicodeDecodeError`` is wrapped in :class:`ConfigError` too, so
    callers guard on the one documented class (#585).
    """
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


# --------------------------------------------------------------------------
# The [shipit] / [managed] manifest — written by ``shipit install``
# --------------------------------------------------------------------------
#
# ``[shipit].version`` pins the shipit commit that last wrote the managed set;
# ``[managed]`` is the per-unit pristine-hash map the next re-install compares
# against (docs/dev/architecture.lex §6, docs/legacy-prd/install-reconciliation.md). tomllib is read-only,
# so the writer below hand-serializes these two flat string tables and splices
# them into an existing file, leaving any ``[secrets]`` (and anything else the
# consumer owns) textually untouched.


def content_hash(data: bytes) -> str:
    """The ``sha256:<hex>`` pristine hash of a managed unit's content."""
    return "sha256:" + hashlib.sha256(data).hexdigest()


#: The consumer-owned decline sub-table riding inside ``[managed]``'s namespace
#: (#600): ``[managed.decline]`` is POLICY (which units this repo refuses), not
#: a pristine entry, so :func:`load_managed` skips it and :func:`write_manifest`
#: preserves it (its header is not the ``[managed]`` header the re-stamp strips).
DECLINE_KEY = "decline"


def load_managed(cfg: dict) -> dict[str, str]:
    """The ``[managed]`` pristine map (path → ``sha256:...``); ``{}`` when absent.

    The ``[managed.decline]`` policy sub-table (:data:`DECLINE_KEY`, parsed by
    :func:`load_declines`) is not a pristine entry and is skipped.
    """
    managed = cfg.get("managed", {})
    if not isinstance(managed, dict):
        raise ConfigError("[managed] must be a table")
    return {str(k): str(v) for k, v in managed.items() if k != DECLINE_KEY}


def _has_table_header(text: str, dotted: str) -> bool:
    """True if TOML ``text`` has an explicit ``[dotted]`` table header line.

    Distinguishes a real sub-table header from the same key reached by a dotted
    path — after :mod:`tomllib` parses, ``[a.b]`` and ``a.b = {...}`` under
    ``[a]`` are indistinguishable in the dict, but only the header form is a
    top-level table the textual re-stamp preserves.
    """
    want = [p.strip() for p in dotted.split(".")]
    for line in text.splitlines():
        # Drop any trailing `# comment` (valid after a header) before matching,
        # so `[managed.decline]  # keep bin/shipit` still reads as the header.
        s = line.split("#", 1)[0].strip()
        if s.startswith("[") and not s.startswith("[[") and s.endswith("]"):
            if [p.strip() for p in s[1:-1].split(".")] == want:
                return True
    return False


def load_declines(cfg: dict, raw: str) -> tuple[str, ...]:
    """The consumer's declined managed-unit keys — ``[managed.decline].keep`` —
    in declaration order; ``()`` when absent (#600).

    A DECLINE is a durable, in-repo "this unit stays the consumer's own":
    ``shipit install`` skips delivering each listed unit entirely (no write, no
    OVERRIDE re-proposal in every reconcile PR) and notes the decision in the
    plan and the PR body. Entries name unit KEYS — the same names the
    ``[managed]`` table uses (``bin/shipit``, ``pixi.toml#shipit-tasks``) ::

        [managed.decline]
        keep = ["bin/shipit"]

    The table MUST be spelled with its own ``[managed.decline]`` header (as
    above): it is consumer-owned policy that must survive the ``[shipit]``/
    ``[managed]`` re-stamp, and :func:`write_manifest` preserves exactly that
    header form. This is why ``raw`` (the manifest's un-parsed text) is
    required: ``tomllib`` flattens a header sub-table and a ``decline.keep``
    dotted key inside the ``[managed]`` body to the same dict, but only the
    header survives the re-stamp — the dotted form would be stripped with the
    body it rides in, silently un-declining on the next install. So a decline
    reached WITHOUT the header raises :class:`ConfigError`, refusing a policy
    that would evaporate rather than accepting it for one run. Malformed shapes
    raise too, so a typo dies at parse instead of silently declining nothing.
    """
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
            "and would silently drop it. Write:\n"
            "    [managed.decline]\n"
            '    keep = ["bin/shipit"]'
        )
    return tuple(keep)


def load_lint_ignore(cfg: dict) -> list[str]:
    """The consumer-owned ``[lint].ignore`` glob list — paths this repo excludes
    from the managed lint gate — or ``[]`` when absent.

    This is the sanctioned, reconcile-safe seam (#484) for a consumer to keep ITS
    OWN non-prose paths — byte-exact test fixtures, generated aggregates like a
    built ``CHANGELOG.md``, vendored/upstream-synced files — out of the gate
    WITHOUT editing a shipit-managed file (the managed ``.markdownlintignore`` /
    ``.markdownlint.yaml`` are whole-file managed units; a consumer path added to
    them is drift that ``shipit install`` reverts). It lives in ``.shipit.toml``,
    the consumer-policy home, so ``install`` never clobbers it (``write_manifest``
    strips only ``[shipit]``/``[managed]``; every other table survives verbatim).

    The globs are Lang-agnostic: they filter the discovered file list BEFORE
    routing, so one ``ignore`` entry drops a path from every leg (markdownlint,
    shfmt, ruff, …). Patterns are gitignore-style — the SAME syntax as the
    ``.markdownlintignore`` this seam replaces, matched by shipit's own
    ``.treeinclude`` engine (:func:`shipit.verbs.lint.path_ignored`): ``*`` does
    not cross ``/``, ``**`` matches any run of segments, a trailing-slash pattern
    matches a directory's whole subtree (``CHANGELOG/`` → every built
    ``CHANGELOG/*.md``), an unanchored name floats to any depth (``CHANGELOG.md``
    matches ``docs/CHANGELOG.md`` too) and a leading ``/`` anchors it to the repo
    root.

        [lint]
        ignore = ["crates/lex-babel/tests/fixtures/**", "CHANGELOG/", "/CHANGELOG.md"]
    """
    section = cfg.get("lint", {})
    if not isinstance(section, dict):
        raise ConfigError("[lint] must be a table")
    ignore = section.get("ignore", [])
    if not isinstance(ignore, list) or not all(isinstance(p, str) for p in ignore):
        raise ConfigError("[lint].ignore must be a list of glob strings")
    return list(ignore)


@dataclass(frozen=True)
class ToolchainEntry:
    """One ``[toolchains]`` map entry: a build-bearing ``path`` (repo-relative,
    ``"."`` for the root), its declared ``toolchain`` (a name from the closed
    registry, :mod:`shipit.tools.registry`), and the per-path producing-command
    ``commands`` overrides — tool slot → argv — with which a nonstandard repo
    opts one leg out of a registry default without forking the tool
    (docs/legacy-prd/tol01-ci-tools.md story 4). Empty ``commands`` means every tool
    runs its registry default on this leg.
    """

    path: str
    toolchain: str
    commands: Mapping[str, tuple[str, ...]]

    def __post_init__(self) -> None:
        # `frozen=True` freezes the attribute bindings, not the dict they point
        # at; wrap `commands` read-only so the "typed frozen values" contract
        # (ADR-0030) can't be violated by mutating the map after parsing. The
        # argv values are already tuples, so this makes the whole entry deep-
        # immutable.
        if not isinstance(self.commands, MappingProxyType):
            object.__setattr__(self, "commands", MappingProxyType(dict(self.commands)))


def _parse_argv(where: str, value: object) -> tuple[str, ...]:
    """One declared producing command — ``where`` names the config key for the
    error: a non-empty list of non-empty strings — an argv, executed through
    the one exec seam, NEVER a shell string (ADR-0028: no shell=True
    anywhere). Shared by the per-path tool overrides and the artifact map's
    bundle/harness commands."""
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


def _reject_path_escape(where: str, value: str) -> None:
    """Refuse a config path that leaves the checkout — absolute, or carrying a
    ``..`` segment. Pure.

    Such a path is later joined to the repo root and READ or REWRITTEN (an
    adapter's leg cwd, a bundle-config bump); an absolute path discards the root
    and ``..`` climbs above it, so a repo's own ``.shipit.toml`` could steer a
    release rewrite at a file outside the tree. Rejected at the parse boundary,
    the one place every value flows through.
    """
    pure = PurePosixPath(value)
    if pure.is_absolute() or ".." in pure.parts:
        raise ConfigError(
            f"{where}: must be a repo-relative path inside the checkout — no "
            f"leading '/', no '..' segment; got {value!r}"
        )


def _parse_toolchain_entry(path: str, spec: object) -> ToolchainEntry:
    """One ``[toolchains]`` entry: a bare toolchain-name string, or a table
    carrying ``toolchain`` plus per-tool argv overrides (see the loader)."""
    from .tools import registry  # lazy — config stays import-light at module load

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
    # Store the CANONICAL repo-relative form (`./web` -> `web`, `web/` -> `web`):
    # the leg's pathspecs are matched against `git status --porcelain` output,
    # which is already canonical, so a non-normalized entry would miss its own
    # changed files and trip a false `no-op bump` refusal in `release prepare`.
    return ToolchainEntry(
        path=str(PurePosixPath(path)), toolchain=name, commands=overrides
    )


def load_toolchains(cfg: dict) -> tuple[ToolchainEntry, ...]:
    """Parse the ``[toolchains]`` path→toolchain map (already loaded) into typed
    entries, in DECLARATION order — the Tool verbs' fan-out order (ADR-0039).

    The map is the repo's structural self-description (ADR-0007: the repo IS
    the set of these entries): each build-bearing path declares its toolchain,
    and the tree-input Tool verbs (``shipit test``, ``shipit build``) walk it
    and dispatch each entry to a producing command. An entry is either a bare
    registry name or a table with per-tool overrides::

        [toolchains]
        "."          = "python"
        "crates/cli" = { toolchain = "rust", test = ["cargo", "test"] }

    ``()`` when the table is absent — the verbs turn that into their pointed
    missing-map error (which is a per-verb message, not a parse failure).
    Malformed shapes raise :class:`ConfigError` naming the offending entry.
    """
    section = cfg.get("toolchains", {})
    if not isinstance(section, dict):
        raise ConfigError("[toolchains] must be a table mapping path -> toolchain")
    return tuple(
        _parse_toolchain_entry(str(path), spec) for path, spec in section.items()
    )


# --------------------------------------------------------------------------
# The [artifacts] map — the repo's declared Artifact set (TOL01-WS02)
# --------------------------------------------------------------------------
#
# An **Artifact** is a produced, distributable unit (CONTEXT.md), declared
# separately from the path→toolchain map and many-to-many with it (ADR-0007:
# one rust workspace → several artifacts; several toolchains → one Tauri app).
# Everything artifact-shaped downstream consumes this parse: `shipit build`
# consumes the build targets and `shipit e2e` the e2e harness declaration
# NOW; the bundle stage, the release stages' endpoint walk, and the sign
# stage consume their fields LATER — parsed here so the whole map is
# validated at the boundary
# (ADR-0030: parse to typed frozen values; construction is validation), with
# loud errors naming the offending key.
#
#     [artifacts.lex-cli]
#     build         = [{ toolchain = "rust", package = "lex-cli" }]
#     bundle        = { composition = "archive" }                # optional
#     bundle-config = "src-tauri/tauri.conf.json"                # optional
#     main-binary   = "lex"                                      # optional
#     product-name  = "Lex"                                      # optional
#     endpoints     = ["gh-release", "crates"]                   # closed set
#     e2e           = { harness = ["bats", "tests/e2e.bats"] }   # optional
#     sign          = true                                       # default false
#
# A build entry may be the bare toolchain name ("python") when the leg's
# default build produces the artifact whole. An artifact may declare ZERO
# build targets (nvim's "the tag is the release": no build, no bundle, one
# endpoint — PRD further notes).


#: The CLOSED distribution-endpoint registry names an ``endpoints`` list may
#: use (PRD: one adapter per endpoint; gh-release, crates, pypi, npm, brew).
#: Adding an endpoint is an adapter plus an entry here; consumed by the
#: release stages later — WS02 only validates the declaration.
ENDPOINTS: tuple[str, ...] = ("gh-release", "crates", "pypi", "npm", "brew")


@dataclass(frozen=True)
class BuildTarget:
    """One producing toolchain build target of an :class:`Artifact`.

    ``toolchain`` names the closed-registry toolchain whose build leg produces
    this artifact. ``package`` narrows the leg's base build command to this
    artifact's unit — the cargo workspace package (``-p``), the go package
    path, the npm workspace — ``None`` when the leg's default build produces
    it whole. ``version_var`` (go only) is the fully qualified variable the
    supplied version is injected into at build via ``-ldflags -X`` (ADR-0041);
    ``None`` keeps the binary's embedded default — the legacy empty
    version-package contract.
    """

    toolchain: str
    package: str | None = None
    version_var: str | None = None

    @property
    def package_basename(self) -> str | None:
        """The binary name this target's ``package`` yields — its path basename
        — or ``None`` when it names none: no ``package``, an empty basename, or
        a bare path-navigation token (``./cmd/padz`` → ``padz``;
        ``.``/``./``/``..``/``/`` → ``None``). The single source of truth for
        "does this package name a binary?", shared by the binary-location
        derivation (:func:`shipit.tools.e2e.binary_location`) and the
        assert-bundle expected-name chain
        (:func:`shipit.release.integrity.expected_main_binary`)."""
        if self.package is None:
            return None
        name = PurePosixPath(self.package).name
        return name if name and name not in (".", "..") else None


@dataclass(frozen=True)
class BundleSpec:
    """An artifact's declared bundle step — the optional composition that
    combines toolchain outputs into the unsigned distributable, run by
    ``shipit release bundle`` (TOL02-WS03; "package" is retired — the stage
    word is bundle).

    ``composition`` names an entry of the CLOSED composition registry
    (:mod:`shipit.release.bundle` — archive, deb, wheel, mac-app), the
    ADR-0007 shape: the bundle step is declared per artifact, keyed off the
    map, never a project-Kind switch. ``command`` is the declared bundler
    argv the ``mac-app`` composition runs (``tauri build``,
    ``electron-builder`` — the only consumer-specific part of the mac path,
    workflows.lex §3.1), through the one exec seam like every producing
    command (ADR-0028); ``source`` is the repo-relative directory that
    bundler leaves the coupled ``.app``/``.dmg`` pair under. Both are
    REQUIRED by ``mac-app`` and rejected for every other composition (their
    commands are registry-assembled, never declared).
    """

    composition: str
    command: tuple[str, ...] | None = None
    source: str | None = None


@dataclass(frozen=True)
class E2eSpec:
    """An artifact's declared e2e harness (consumed by ``shipit e2e``,
    TOL01-WS03). ``harness`` is the declared harness argv; ``None`` (a bare
    ``e2e = {}``) means the registry default (bats-run check-e2e, PRD).
    DECLARING the table at all is what opts an artifact into e2e — a repo
    with no ``e2e`` key has no e2e lane (PRD story 11)."""

    harness: tuple[str, ...] | None = None


@dataclass(frozen=True)
class Artifact:
    """One ``[artifacts]`` entry, fully typed (ADR-0030).

    ``build`` targets are consumed by ``shipit build`` and ``e2e`` by
    ``shipit e2e`` (the harness declaration plus the ``<NAME>_BIN``
    injection); ``bundle_config`` by ``shipit release prepare`` (the
    artifact-declared bundle-config hook, ADR-0041/PRD story 25: the
    repo-root-relative JSON file — ``tauri.conf.json`` — whose top-level
    ``version`` is bumped in lockstep with the leg adapters, keeping "tauri"
    out of the bump dispatch registry); ``bundle`` by ``shipit release
    bundle`` (TOL02-WS03: the declared composition into the unsigned
    distributable); ``main_binary`` / ``product_name`` by ``shipit release
    assert-bundle``'s expected-name fallback chain (workflows.lex §3.2:
    mainBinaryName → productName → package name — the scar-#2 integrity
    guard's inputs); ``endpoints`` by the release stages, and ``sign`` by
    the sign stage / preflight secrets validation — those later.
    """

    name: str
    build: tuple[BuildTarget, ...] = ()
    bundle: BundleSpec | None = None
    bundle_config: str | None = None
    main_binary: str | None = None
    product_name: str | None = None
    endpoints: tuple[str, ...] = ()
    e2e: E2eSpec | None = None
    sign: bool = False


def _reject_unknown_keys(where: str, spec: dict, known: tuple[str, ...]) -> None:
    """Loud ADR-0030 boundary check: an unrecognized key in ``spec`` names
    itself and the known set, so a typo (``endpoint``) dies at parse."""
    for key in spec:
        if key not in known:
            raise ConfigError(
                f"{where}: unknown key `{key}`; known keys: {', '.join(known)}"
            )


def _parse_build_target(where: str, spec: object) -> BuildTarget:
    """One ``build`` list entry: a bare toolchain name, or a table with
    ``toolchain`` plus the optional ``package`` / ``version-var`` narrowing."""
    from .tools import registry  # lazy — config stays import-light at module load

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
            # version-var rides go's -ldflags -X value (a single token the go
            # tool re-splits on whitespace), so whitespace would fragment it
            # into stray tokens/flags — refused at parse (ADR-0041), the same
            # class as a whitespace `--version`.
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
        # ADR-0041: only go injects the version at build (-ldflags -X); every
        # other toolchain's version is a manifest projection bumped at prepare.
        raise ConfigError(
            f"{where}: version-var applies only to the go toolchain "
            f"(ADR-0041: other toolchains carry the version in their manifest)"
        )
    return BuildTarget(toolchain=name, package=package, version_var=version_var)


def _parse_endpoints(where: str, value: object) -> tuple[str, ...]:
    """The ``endpoints`` list, validated against the closed :data:`ENDPOINTS`
    registry — a declaration the release stages consume later."""
    if not isinstance(value, list) or not all(isinstance(e, str) for e in value):
        raise ConfigError(f"{where}: must be a list of endpoint names")
    for endpoint in value:
        if endpoint not in ENDPOINTS:
            known = ", ".join(ENDPOINTS)
            raise ConfigError(
                f"{where}: unknown endpoint `{endpoint}`; known endpoints: {known}"
            )
    return tuple(value)


def _parse_bundle(where: str, spec: object) -> BundleSpec:
    from .release import bundle as bundle_registry  # lazy — config stays import-light

    if not isinstance(spec, dict):
        raise ConfigError(
            f"{where}.bundle: must be a table, e.g. "
            f'{{ composition = "archive" }}; got {spec!r}'
        )
    _reject_unknown_keys(f"{where}.bundle", spec, ("composition", "command", "source"))
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
    command = spec.get("command")
    source = spec.get("source")
    if entry.declared_command:
        # mac-app: the bundler that produces the coupled .app/.dmg pair is the
        # one consumer-specific part of the mac path (workflows.lex §3.1), so
        # the declaration must carry it — and say where the pair lands.
        if command is None:
            raise ConfigError(
                f"{where}.bundle: composition `{composition}` runs the "
                f"artifact's own bundler — declare its argv, e.g. "
                f'command = ["npm", "run", "tauri", "build"]'
            )
        if not isinstance(source, str) or not source:
            raise ConfigError(
                f"{where}.bundle: composition `{composition}` needs `source` — "
                f"the repo-relative directory the bundler leaves the "
                f'.app/.dmg pair under, e.g. "src-tauri/target/release/bundle"'
            )
        _reject_path_escape(f"{where}.bundle.source", source)
        return BundleSpec(
            composition=composition,
            command=_parse_argv(f"{where}.bundle.command", command),
            source=str(PurePosixPath(source)),
        )
    # Registry-assembled compositions (archive, deb, wheel): their commands
    # are the registry's one assembly point (ADR-0028) — a declared argv or
    # source dir would be a second one, refused loudly.
    for key in ("command", "source"):
        if key in spec:
            raise ConfigError(
                f"{where}.bundle: `{key}` applies only to compositions that "
                f"run a declared bundler (mac-app); composition "
                f"`{composition}` assembles its own commands"
            )
    return BundleSpec(composition=composition)


def _parse_e2e(where: str, spec: object) -> E2eSpec:
    if not isinstance(spec, dict):
        raise ConfigError(
            f"{where}.e2e: must be a table (empty for the default harness), "
            f'e.g. {{}} or {{ harness = ["bats", "tests/e2e.bats"] }}; got {spec!r}'
        )
    _reject_unknown_keys(f"{where}.e2e", spec, ("harness",))
    if "harness" not in spec:
        return E2eSpec(harness=None)
    return E2eSpec(harness=_parse_argv(f"{where}.e2e.harness", spec["harness"]))


def _parse_artifact(name: str, spec: object) -> Artifact:
    """One ``[artifacts.<name>]`` table into a typed :class:`Artifact`."""
    where = f"[artifacts].{name}"
    if not isinstance(spec, dict):
        raise ConfigError(f"{where} must be a table; got {spec!r}")
    _reject_unknown_keys(
        where,
        spec,
        (
            "build",
            "bundle",
            "bundle-config",
            "endpoints",
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
        # Canonical form (`./x` -> `x`): the release stage stages this path and
        # matches it against `git status`, so a non-normalized value would read
        # as a different, unchanged file and trip a false no-op / missing-file.
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
    return Artifact(
        name=name,
        build=build,
        bundle=_parse_bundle(where, spec["bundle"]) if "bundle" in spec else None,
        bundle_config=bundle_config,
        main_binary=names["main-binary"],
        product_name=names["product-name"],
        endpoints=_parse_endpoints(f"{where}.endpoints", spec.get("endpoints", [])),
        e2e=_parse_e2e(where, spec["e2e"]) if "e2e" in spec else None,
        sign=sign,
    )


def load_artifacts(cfg: dict) -> tuple[Artifact, ...]:
    """Parse the ``[artifacts]`` map (already loaded) into typed
    :class:`Artifact` values, in DECLARATION order.

    ``()`` when the table is absent — a repo with no artifact map still
    builds (``shipit build`` runs each leg's base build command); declaring
    artifacts is what narrows legs to per-artifact targets and, later, feeds
    the bundle/endpoint/sign/e2e machinery. Malformed shapes raise
    :class:`ConfigError` naming the offending key (ADR-0030).
    """
    section = cfg.get("artifacts", {})
    if not isinstance(section, dict):
        raise ConfigError("[artifacts] must be a table of artifact declarations")
    return tuple(_parse_artifact(str(name), spec) for name, spec in section.items())


# --------------------------------------------------------------------------
# The [lanes] table — declared CI test units (TOL01, PRD story 14)
# --------------------------------------------------------------------------
#
# A Lane is the DECLARATION of one CI test unit (CONTEXT.md Build & release):
# `{ run, required, local, trigger, runner, scope }`, keyed by lane name. The
# lane planner (TOL01-WS05) maps (lanes, event, path-diff) → job matrix; the
# `run` string is a shipit tool or leg invocation, so the same command a CI job
# runs is what a laptop or hook invokes — one definition, enforced everywhere.
#
#     [lanes.changelog-sync]
#     run = "changelog check"
#     required = true
#     trigger = "pr"
#
# `required` = blocking at merge; `local` = also enforced at commit/push (the
# required∩local set IS the commit/push checks); `trigger` = which event runs
# it at all; `runner`/`scope` are routing hints the planner consumes.

#: The lane triggers the planner routes (glossary: pr / push / nightly /
#: dispatch). A closed set so a typo (`trigger = "PR"`) dies at parse.
LANE_TRIGGERS = frozenset({"pr", "push", "nightly", "dispatch"})

#: The per-lane keys `load_lanes` accepts; anything else is a typo that must
#: die fast (the same closed-registry philosophy as `_KNOWN_TABLES`).
_LANE_KEYS = frozenset({"run", "required", "local", "trigger", "runner", "scope"})


@dataclass(frozen=True)
class Lane:
    """One declared CI test unit (the glossary's **Lane**), typed (ADR-0030).

    ``run`` is the shipit tool/leg invocation (``"changelog check"``,
    ``"test rust"``); ``required`` blocks at merge; ``local`` also enforces at
    commit/push; ``trigger`` names the event that runs it; ``runner`` and
    ``scope`` are planner routing hints (``None`` = planner default).
    """

    name: str
    run: str
    required: bool = False
    local: bool = False
    trigger: str = "pr"
    runner: str | None = None
    scope: str | None = None


#: The fragment-sync check declared as a Lane (TOL01-WS06, PRD story 18): a PR
#: that edits the changelog without a fragment — or adds a fragment without the
#: re-rendered changelog — fails before merge. PR-triggered, cheap, required at
#: merge but NOT local (a fragment usually lands with the PR's last commit, so
#: blocking every mid-work commit would only teach `--no-verify`). The `run` is
#: the ordinary `changelog check` invocation (a shipit tool/leg string, no
#: `shipit` prefix — see `run` above), so the lane's CI job and a laptop run are
#: the same command. A repo adopting the changelog model declares
#: exactly this entry in its `[lanes]` table.
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
    # `runner`/`scope` are optional (absent = planner default), but a present
    # value that is blank or whitespace-only is a footgun, not a default: a
    # blank runner yields an invalid `runs-on` label at workflow runtime, and a
    # blank scope drops the lane on every PR with a known diff (it can never
    # match `_in_scope`). Strip and reject empty, exactly as `run` is handled,
    # so the typo dies at parse instead of misbehaving silently in CI.
    cleaned: dict[str, str | None] = {}
    for key, value in (("runner", spec.get("runner")), ("scope", spec.get("scope"))):
        if value is None:
            cleaned[key] = None
        elif not isinstance(value, str) or not value.strip():
            raise ConfigError(f"[lanes].{name}: `{key}` must be a non-empty string")
        else:
            cleaned[key] = value.strip()
    return Lane(
        name=name,
        run=run.strip(),
        required=required,
        local=local,
        trigger=trigger,
        runner=cleaned["runner"],
        scope=cleaned["scope"],
    )


def load_lanes(cfg: dict) -> list[Lane]:
    """Parse the ``[lanes]`` table (already loaded) into ordered, typed
    :class:`Lane` declarations; ``[]`` when absent.

    Declaration order is preserved (TOML table order), so the planner's job
    emission is deterministic from the file. Raises :class:`ConfigError` on any
    malformed entry — an unknown key, a missing/empty ``run``, an
    out-of-vocabulary ``trigger``, a blank ``runner``/``scope`` — so a typo'd
    lane dies at parse, never as a silently-unrouted CI job.
    """
    lanes = cfg.get("lanes", {})
    if not isinstance(lanes, dict):
        raise ConfigError("[lanes] must be a table of lane tables")
    return [_parse_lane(name, spec) for name, spec in lanes.items()]


def shipit_version(cfg: dict) -> str | None:
    """The ``[shipit].version`` pin, or ``None`` when absent."""
    section = cfg.get("shipit", {})
    if not isinstance(section, dict):
        raise ConfigError("[shipit] must be a table")
    value = section.get("version")
    return str(value) if value is not None else None


def shipit_pin(path: str | Path) -> str | None:
    """The ``.shipit.toml`` Shipit pin at ``path`` — ``[shipit].version`` — or ``None``.

    The pin is the full shipit commit sha ``shipit install`` stamped from its
    own build identity (ADR-0033); its presence is what marks a repo as
    BOOTSTRAPPED — carrying a managed set and the exact build that wrote it. A
    ``.shipit.toml`` with only consumer policy (``[secrets]`` / ``[reviewers]``
    / ``[project]``) has no pin.

    The value must VALIDATE as a full git object sha (:class:`~shipit.identity.Sha`):
    this helper is the fail-closed gate, so a non-sha ``version`` — the retired
    static ``0.0.1`` package version, a ``seed`` sentinel, an abbreviated sha —
    is treated as PINLESS, not as a bootstrapped repo. Otherwise provisioning
    would proceed on a bogus pin and the managed launcher would later hand
    ``uv`` a non-commit ref instead of failing with the bootstrap diagnostic.

    Pure (reads, never writes). ``None`` when the file is absent, malformed,
    pinless, or carrying a non-sha version: a config we cannot read a valid pin
    from is treated as pinless, so the fail-closed callers (Tree provisioning's
    pin gate) refuse rather than guess.
    """
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
    """A TOML key, bare when it can be and quoted (paths, ``#``) otherwise."""
    if _BARE_KEY.fullmatch(key):
        return key
    return '"' + key.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _strip_tables(text: str, tables: set[str]) -> str:
    """Drop the given top-level tables (header + body) from TOML ``text``.

    Header detection tolerates a trailing ``# comment`` (valid TOML after a
    header), like :func:`_has_table_header` (#617): a commented ``[managed]``
    still strips, and a commented ``[managed.decline]`` still terminates the
    ``[managed]`` skip so the consumer's decline table survives the re-stamp.
    """
    out: list[str] = []
    skipping = False
    for line in text.splitlines():
        # Drop any trailing `# comment` before matching, as _has_table_header
        # does; the kept output still carries the original line verbatim.
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
    """Serialize the ``[shipit]`` and ``[managed]`` tables to TOML text."""
    lines = ["[shipit]", f'version = "{version}"', "", "[managed]"]
    for key, value in managed.items():
        lines.append(f'{_toml_key(key)} = "{value}"')
    return "\n".join(lines) + "\n"


def write_manifest(path: str | Path, *, version: str, managed: dict[str, str]) -> None:
    """Write the ``[shipit]``/``[managed]`` tables, preserving the rest of the file.

    Preservation is textual and header-scoped: only the ``[shipit]`` and
    ``[managed]`` headers (and their bodies) are stripped and re-serialized, so
    every consumer-owned table survives verbatim — including the
    ``[managed.decline]`` policy sub-table (#600), whose own header stops the
    ``[managed]`` body strip.
    """
    p = Path(path)
    existing = p.read_text(encoding="utf-8") if p.is_file() else ""
    kept = _strip_tables(existing, {"shipit", "managed"})
    block = dump_manifest(version, managed)
    text = f"{kept}\n\n{block}" if kept else block
    p.write_text(text, encoding="utf-8")


# --------------------------------------------------------------------------
# Seed-if-absent consumer policy — the pr-flow plumbing ``shipit install`` carries
# --------------------------------------------------------------------------
#
# Unlike ``[managed]`` (the hash-reconciled slow set), ``[secrets]``,
# ``[reviewers]``, ``[lint]``, and ``[toolchains]`` are CONSUMER-OWNED POLICY
# (docs/dev/architecture.lex §6). They are NOT under the pristine-hash
# reconciliation: ``shipit install`` SEEDS them when absent and NEVER clobbers a
# consumer's edits. The App-secret mappings are MERGED into an existing
# ``[secrets]`` table (only the missing names are added, preserving every entry
# a consumer already wrote); the ``[reviewers]``/``[lint]``/``[toolchains]``
# scaffolds are written ONLY when their whole table is missing. A re-install on
# a fully-seeded config is a no-op. This keeps the seam inside the existing
# model — no new drift engine (issue #25 / INS01).

# The local-reviewer GitHub App credential mappings install seeds into a
# consumer's ``[secrets]``. Each GitHub secret NAME is sourced from the Doppler
# github/prd key of the SAME name; the credentials let a CI-side review post as the
# App bot with the same key the local path sources directly (CI parity). The
# generic gh-setup push only provisions a secret when its source RESOLVES, so
# seeding the mapping is safe even before a consumer's GitHub App is installed.
#
# The key NAMES are never spelled here: they DERIVE from the Backend registry
# (:func:`shipit.agent.backend.funnel_backends` → ``doppler_pem_key`` /
# ``doppler_app_id_key``), the ONE source of every registry-derived name
# (ADR-0025 / COR02). Wiring a new funnel backend is its registry entry alone —
# its App-secret mappings appear in the seeds and scaffold with zero config edits.


def seeded_app_secrets() -> tuple[str, ...]:
    """The GitHub secret NAMES ``shipit install`` seeds into ``[secrets]`` — one
    (PEM, App-id) pair per funnel backend, read off the Backend registry in
    registry order. Imported lazily so ``config`` stays free of an ``agent``
    import at module load (mirror of :func:`reviewers_scaffold`)."""
    from .agent import backend

    return tuple(
        key
        for b in backend.funnel_backends()
        for key in (b.doppler_pem_key, b.doppler_app_id_key)
    )


# The explanatory comment heading the seeded ``[secrets]`` table. The TABLE ITSELF
# is rendered from the Backend registry by :func:`secrets_scaffold`.
_SECRETS_SCAFFOLD_HEADER = """\
# [secrets] — repo Actions secrets. Each table key is the GitHub secret NAME; the
# value names exactly one source ({ doppler = "KEY" } / { env = "VAR" } /
# { prompt = true }). Seeded with shipit's local-reviewer (codex/agy) GitHub App
# credentials, each sourced from Doppler github/prd. `shipit gh-setup` only pushes
# a secret when its source resolves, so these are safe before the App is installed.
[secrets]"""


def secrets_scaffold() -> str:
    """The ``[secrets]`` block ``shipit install`` seeds when a consumer has none.

    The comment header plus one column-aligned entry per seeded App-secret name
    (:func:`seeded_app_secrets` — i.e. the Backend registry), each mapped to its
    like-named Doppler key. Rendered, never hand-written, so the scaffold and the
    registry can never disagree; the golden test pins the current registry's
    rendering byte-identical to the retired literal.
    """
    names = seeded_app_secrets()
    width = max((len(n) for n in names), default=0)
    lines = [f'{n:<{width}} = {{ doppler = "{n}" }}' for n in names]
    return "\n".join([_SECRETS_SCAFFOLD_HEADER, *lines]) + "\n"


# The explanatory comment prepended to the seeded ``[reviewers]`` table. The TABLE
# ITSELF is rendered from the SINGLE required-reviewer default
# (``prstate.reviewers_config.DEFAULT_REVIEWERS``) by :func:`reviewers_scaffold`, so the
# install scaffold and the engine's code-default can never disagree (ADR-0025 / COR01-WS02).
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
    """The ``[reviewers]`` block ``shipit install`` seeds when a consumer has none.

    The comment header plus the table body rendered from the SINGLE required-reviewer
    default (:data:`shipit.prstate.reviewers_config.DEFAULT_REVIEWERS`), imported lazily
    so ``config`` stays free of a ``prstate`` import at module load. Because the seeded
    set comes from the same map the engine defaults to, a freshly-installed repo and a
    repo with no config require exactly the same reviewers.
    """
    from .prstate import reviewers_config

    return f"{_REVIEWERS_SCAFFOLD_HEADER}\n{reviewers_config.default_reviewers_scaffold_body()}"


# The default consumer-owned ``[lint].ignore`` globs ``shipit install`` seeds when
# a repo tracks no ``[lint]`` table (#484). Common generated/assembled paths that
# are not hand-written prose, so the managed lint gate must not lint them — a
# freshly-onboarded repo otherwise takes a latent gate failure on its built
# CHANGELOG or a package lockfile. Gitignore-style globs (the same syntax
# :func:`load_lint_ignore` documents), matched by shipit's own ``.treeinclude``
# engine. The consumer OWNS this list and may extend it; it lives in the
# consumer-policy home, so ``install`` never clobbers it (reconcile-safe #484).
_LINT_SEED_IGNORE: tuple[str, ...] = (
    "CHANGELOG.md",
    "CHANGELOG/**",
    "package-lock.json",
    "pnpm-lock.yaml",
)

# The explanatory comment heading the seeded ``[lint]`` table. Mirrors the other
# managed comments: it states these are generated/assembled files the gate must
# not lint AND that the consumer owns the list (install never clobbers [lint]).
_LINT_SCAFFOLD_HEADER = """\
# [lint].ignore — paths the managed lint gate must SKIP. Seeded with common
# generated/assembled files (a built CHANGELOG, package lockfiles) that are not
# hand-written prose, so a freshly-onboarded repo does not take a latent gate
# failure on them. Gitignore-style globs. You OWN this list and may extend it —
# it is reconcile-safe: `shipit install` seeds [lint] only when absent and never
# clobbers a table you have edited."""


def lint_scaffold() -> str:
    """The ``[lint]`` block ``shipit install`` seeds when a consumer has none.

    The comment header plus the ``ignore`` array of the default generated-path
    globs (:data:`_LINT_SEED_IGNORE`). Seeds the SAME table the gate already
    reads (:func:`load_lint_ignore`, #484) — no new schema — so a freshly-onboarded
    repo excludes its generated files without a hand-edit.
    """
    entries = ",\n".join(f'  "{g}"' for g in _LINT_SEED_IGNORE)
    return f"{_LINT_SCAFFOLD_HEADER}\n[lint]\nignore = [\n{entries},\n]"


# Root-level manifest basename → the toolchain it signals. THE one
# manifest-signal table (TOL01-WS08 #578): the Tool verbs' missing-map error
# names these signals as the copy-paste fix (:mod:`shipit.verbs._tool`), and
# `shipit install` derives the SAME signals into the seeded ``[toolchains]``
# map (:func:`derive_toolchains`) — the error's suggestion and the seed can
# never disagree. Order is precedence: the first manifest present at the
# consumer root decides what ``"."`` seeds (and which example the error shows).
SIGNAL_MANIFESTS: tuple[tuple[str, str], ...] = (
    ("Cargo.toml", "rust"),
    ("go.mod", "go"),
    ("pyproject.toml", "python"),
    ("package.json", "npm"),
)


def derive_toolchains(root: Path) -> tuple[tuple[str, str], ...]:
    """The ``[toolchains]`` entries ``shipit install`` seeds for a consumer that
    declares none — derived from the ROOT manifests (TOL01-WS08 #578).

    ``shipit test``/``build`` dispatch on the declared path→toolchain map
    (ADR-0007/0039) and refuse without it, so install seeds a starting map off
    :data:`SIGNAL_MANIFESTS` — the same detection the verbs' missing-map error
    already suggests. Root-level only, first signal wins (``"."`` maps to ONE
    toolchain): the seed is a consumer-owned starting point, extended by hand
    for nested paths or multi-toolchain repos, never a dispatch fallback (the
    verbs keep refusing an undeclared repo, ADR-0007). ``()`` when no root
    manifest signals a toolchain — nothing is seeded then.
    """
    for name, toolchain in SIGNAL_MANIFESTS:
        if (Path(root) / name).is_file():
            return ((".", toolchain),)
    return ()


# The explanatory comment heading the seeded ``[toolchains]`` table. Mirrors the
# other policy-seed headers: it states where the map came from AND that the
# consumer owns it (install seeds [toolchains] only when absent, #578).
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
    """The ``[toolchains]`` block ``shipit install`` seeds when a consumer has
    none — the comment header plus one ``"path" = "toolchain"`` line per derived
    entry (:func:`derive_toolchains`). Rendered, never hand-written, so the seed
    and the derivation can never disagree."""
    lines = [f'"{path}" = "{toolchain}"' for path, toolchain in entries]
    return "\n".join([_TOOLCHAINS_SCAFFOLD_HEADER, *lines])


def _seeded_secret_line(name: str) -> str:
    """One ``[secrets]`` entry mapping ``name`` to its like-named Doppler key."""
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
    """``cfg[name]`` when it is a table (absent → ``None``); raise
    :class:`ConfigError` when it is present but NOT a table.

    Seeding either merges into a table or writes a fresh ``[name]`` one — a scalar
    (``secrets = "off"``) can be neither: re-heading it would redefine the key and
    produce TOML that no longer parses. We refuse to touch such a file rather than
    corrupt it (install catches the :class:`ConfigError` and skips seeding)."""
    val = cfg.get(name)
    if val is None or isinstance(val, dict):
        return val
    raise ConfigError(
        f"malformed {path}: `{name}` must be a table, not {type(val).__name__}"
    )


def _plan_seed(
    text: str, path: str | Path, toolchains: Sequence[tuple[str, str]] = ()
) -> tuple[list[str], str]:
    """The seed-if-absent items missing from ``text`` and the resulting file text.

    Pure: parses and computes, never writes — ``toolchains`` carries the
    manifest-derived entries in (the callers derive them via
    :func:`derive_toolchains`, keeping this function read-free). Raises
    :class:`ConfigError` for any shape install cannot seed safely — malformed
    TOML, a scalar where a seedable table is expected (every one it touches or
    preserves is shape-checked via :func:`_require_table`: ``secrets``,
    ``reviewers``, ``lint``, and ``toolchains``), or an existing ``[secrets]``
    table that has no literal header to merge under (an inline table or dotted
    keys) — so the caller skips seeding rather than write a broken config.
    """
    cfg = _parse_text(text, path)
    secrets = _require_table(cfg, "secrets", path)
    _require_table(cfg, "reviewers", path)  # validate shape; preserved if present
    _require_table(cfg, "lint", path)  # validate shape; preserved if present
    _require_table(cfg, "toolchains", path)  # validate shape; preserved if present

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

    # Seeded ONLY when no [lint] table is tracked — a re-install NOOPs it and a
    # consumer-edited [lint] is never clobbered (append-only, reconcile-safe #484).
    if "lint" not in cfg:
        text = _append_lines(text, lint_scaffold().splitlines())
        seeded.append("[lint].ignore")

    # Seeded ONLY when the repo's manifests signal a toolchain AND no
    # [toolchains] table is tracked (#578) — the same discipline: an existing
    # consumer-edited map (even an empty table) is never overwritten.
    if toolchains and "toolchains" not in cfg:
        text = _append_lines(text, toolchains_scaffold(toolchains).splitlines())
        seeded.append("[toolchains]")
    return seeded, text


def plan_policy_seed(
    path: str | Path, *, toolchains: Sequence[tuple[str, str]] = ()
) -> list[str]:
    """What seed-if-absent policy ``shipit install`` WOULD add to ``path`` — the
    missing App-secret mappings, ``[reviewers]`` when its table is absent,
    ``[lint].ignore`` (the default generated-path globs) when no ``[lint]`` table
    is tracked, and ``[toolchains]`` (the supplied manifest-derived entries,
    :func:`derive_toolchains`) when the map is absent and ``toolchains`` is
    non-empty (#578).

    Pure over ``path``'s text and ``toolchains``: reads, never writes. An empty
    list means the policy is already in place, so a re-install stays a no-op.
    Raises :class:`ConfigError` on any shape we cannot seed safely (see
    :func:`_plan_seed`), letting the caller skip seeding rather than corrupt the
    file.
    """
    return _plan_seed(_config_text(path), path, toolchains)[0]


def apply_policy_seed(
    path: str | Path, *, toolchains: Sequence[tuple[str, str]] = ()
) -> list[str]:
    """Seed-if-absent the consumer policy into ``path``, preserving every existing
    entry, and return what was seeded (same items :func:`plan_policy_seed` lists,
    given the same ``toolchains``).

    Merge-preserving: a present ``[secrets]`` table keeps all its entries and only
    the missing App mappings are inserted under its header; an absent table gets
    the full :func:`secrets_scaffold`. ``[reviewers]``, ``[lint]``, and
    ``[toolchains]`` are each written only when their table is entirely absent —
    a consumer's own ``[reviewers]``, ``[lint]``, or ``[toolchains]`` is never
    touched.
    Writes the file only when something is seeded, so an already-seeded config is
    left byte-identical (a clean no-op). Raises identically to
    :func:`plan_policy_seed`, so an install that planned a seed never reaches an
    unsafe apply.
    """
    seeded, text = _plan_seed(_config_text(path), path, toolchains)
    if seeded:
        Path(path).write_text(text, encoding="utf-8")
    return seeded


def _append_lines(text: str, lines: list[str]) -> str:
    """Append ``lines`` as a fresh block, separated from prior content by a blank line."""
    base = text.rstrip("\n")
    sep = "\n\n" if base else ""
    return f"{base}{sep}" + "\n".join(lines) + "\n"


def _insert_after_header(
    text: str, table: str, lines: list[str], path: str | Path
) -> str:
    """Insert ``lines`` immediately after the ``[table]`` header, tolerating
    surrounding whitespace and a trailing comment (``[ secrets ]  # note``).

    Raises :class:`ConfigError` when the table is defined without a literal header
    — an inline table (``secrets = { … }``) or dotted keys (``secrets.X = …``) —
    since there is no header to merge under and a fresh ``[table]`` block would
    redefine the key into invalid TOML."""
    header = re.compile(rf"^\s*\[\s*{re.escape(table)}\s*\]\s*(#.*)?$")
    rows = text.splitlines()
    for idx, row in enumerate(rows):
        if header.match(row):
            return "\n".join(rows[: idx + 1] + lines + rows[idx + 1 :]) + "\n"
    raise ConfigError(
        f"malformed {path}: cannot seed [{table}] — no `[{table}]` header to merge "
        f"under (inline table or dotted keys?)"
    )
