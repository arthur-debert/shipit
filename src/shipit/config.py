"""Reading ``.shipit.toml`` — shipit's policy config.

``.shipit.toml`` owns policy (the secret map, reviewers, the path→toolchain map,
the pristine hashes); ``pixi.toml`` owns provisioning. They describe different
layers, so there is no split-brain (docs/dev/architecture.lex §6). Step 1 needs
only the ``[secrets]`` table.

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
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
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
    """Parse a ``.shipit.toml`` file into a dict, or raise :class:`ConfigError`."""
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"no {CONFIG_NAME} at {p}")
    try:
        with p.open("rb") as fh:
            cfg = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"malformed {p}: {exc}") from None
    _validate_known_tables(cfg)
    return cfg


# --------------------------------------------------------------------------
# The [shipit] / [managed] manifest — written by ``shipit install``
# --------------------------------------------------------------------------
#
# ``[shipit].version`` pins the shipit commit that last wrote the managed set;
# ``[managed]`` is the per-unit pristine-hash map the next re-install compares
# against (docs/dev/architecture.lex §6, docs/prd/install-reconciliation.md). tomllib is read-only,
# so the writer below hand-serializes these two flat string tables and splices
# them into an existing file, leaving any ``[secrets]`` (and anything else the
# consumer owns) textually untouched.


def content_hash(data: bytes) -> str:
    """The ``sha256:<hex>`` pristine hash of a managed unit's content."""
    return "sha256:" + hashlib.sha256(data).hexdigest()


def load_managed(cfg: dict) -> dict[str, str]:
    """The ``[managed]`` pristine map (path → ``sha256:...``); ``{}`` when absent."""
    managed = cfg.get("managed", {})
    if not isinstance(managed, dict):
        raise ConfigError("[managed] must be a table")
    return {str(k): str(v) for k, v in managed.items()}


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
    (docs/prd/tol01-ci-tools.md story 4). Empty ``commands`` means every tool
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


def _parse_override(path: str, tool: str, value: object) -> tuple[str, ...]:
    """One per-path producing-command override: a non-empty list of non-empty
    strings — an argv, executed through the one exec seam, NEVER a shell
    string (ADR-0028: no shell=True anywhere)."""
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(a, str) and a for a in value)
    ):
        raise ConfigError(
            f"[toolchains].{path}.{tool} must be a non-empty argv list of "
            f'strings, e.g. ["cargo", "test"]; got {value!r}'
        )
    return tuple(value)


def _parse_toolchain_entry(path: str, spec: object) -> ToolchainEntry:
    """One ``[toolchains]`` entry: a bare toolchain-name string, or a table
    carrying ``toolchain`` plus per-tool argv overrides (see the loader)."""
    from .tools import registry  # lazy — config stays import-light at module load

    if not path or path.startswith("/"):
        raise ConfigError(
            f"[toolchains] paths are repo-relative ({'empty' if not path else path!r}"
            f" is not); use '.' for the repo root"
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
            overrides[tool] = _parse_override(path, tool, value)
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
    return ToolchainEntry(path=path, toolchain=name, commands=overrides)


def load_toolchains(cfg: dict) -> tuple[ToolchainEntry, ...]:
    """Parse the ``[toolchains]`` path→toolchain map (already loaded) into typed
    entries, in DECLARATION order — the Tool verbs' fan-out order (ADR-0039).

    The map is the repo's structural self-description (ADR-0007: the repo IS
    the set of these entries): each build-bearing path declares its toolchain,
    and the tree-input Tool verbs (``shipit test``, WS02's ``build``) walk it
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
    """Drop the given top-level tables (header + body) from TOML ``text``."""
    out: list[str] = []
    skipping = False
    for line in text.splitlines():
        stripped = line.strip()
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
    """Write the ``[shipit]``/``[managed]`` tables, preserving the rest of the file."""
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
# Unlike ``[managed]`` (the hash-reconciled slow set), ``[secrets]`` and
# ``[reviewers]`` are CONSUMER-OWNED POLICY (docs/dev/architecture.lex §6). They
# are NOT under the pristine-hash reconciliation: ``shipit install`` SEEDS them
# when absent and NEVER clobbers a consumer's edits. The App-secret mappings are
# MERGED into an existing ``[secrets]`` table (only the missing names are added,
# preserving every entry a consumer already wrote); the ``[reviewers]`` scaffold
# is written ONLY when the whole table is missing. A re-install on a fully-seeded
# config is a no-op. This keeps the seam inside the existing model — no new drift
# engine (issue #25 / INS01).

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
# `codex = {}`). Review-once: `rerun` defaults OFF (token-billed; opt in per reviewer
# with e.g. `copilot = { rerun = true }`)."""


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


def _plan_seed(text: str, path: str | Path) -> tuple[list[str], str]:
    """The seed-if-absent items missing from ``text`` and the resulting file text.

    Pure: parses and computes, never writes. Raises :class:`ConfigError` for any
    shape install cannot seed safely — malformed TOML, a scalar ``secrets``/
    ``reviewers``, or an existing ``[secrets]`` table that has no literal header to
    merge under (an inline table or dotted keys) — so the caller skips seeding
    rather than write a broken config.
    """
    cfg = _parse_text(text, path)
    secrets = _require_table(cfg, "secrets", path)
    _require_table(cfg, "reviewers", path)  # validate shape; preserved if present
    _require_table(cfg, "lint", path)  # validate shape; preserved if present

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
    return seeded, text


def plan_policy_seed(path: str | Path) -> list[str]:
    """What seed-if-absent policy ``shipit install`` WOULD add to ``path`` — the
    missing App-secret mappings, ``[reviewers]`` when its table is absent, and
    ``[lint].ignore`` (the default generated-path globs) when no ``[lint]`` table
    is tracked.

    Pure: reads, never writes. An empty list means the policy is already in place,
    so a re-install stays a no-op. Raises :class:`ConfigError` on any shape we
    cannot seed safely (see :func:`_plan_seed`), letting the caller skip seeding
    rather than corrupt the file.
    """
    return _plan_seed(_config_text(path), path)[0]


def apply_policy_seed(path: str | Path) -> list[str]:
    """Seed-if-absent the consumer policy into ``path``, preserving every existing
    entry, and return what was seeded (same items :func:`plan_policy_seed` lists).

    Merge-preserving: a present ``[secrets]`` table keeps all its entries and only
    the missing App mappings are inserted under its header; an absent table gets
    the full :func:`secrets_scaffold`. ``[reviewers]`` and ``[lint]`` are each
    written only when their table is entirely absent — a consumer's own
    ``[reviewers]`` or ``[lint]`` is never touched.
    Writes the file only when something is seeded, so an already-seeded config is
    left byte-identical (a clean no-op). Raises identically to
    :func:`plan_policy_seed`, so an install that planned a seed never reaches an
    unsafe apply.
    """
    seeded, text = _plan_seed(_config_text(path), path)
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
