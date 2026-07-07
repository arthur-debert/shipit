"""lint — the standardized multi-language checks (docs/prd/lint-checks.md).

shipit INVERTS release's lefthook-as-orchestrator model. Because pixi has no
cross-manifest task inheritance (architecture.lex §5), the per-language
discovery, routing and aggregation cannot live in a pixi task templated into
each consumer — that is drift on pixi.toml. So it lives HERE, in the binary:
lefthook is thin (it calls ``pixi run lint``), pixi is thin (it runs
``shipit lint``), and this verb does the real work. CI and the pre-commit hook
run the IDENTICAL checks because it is ONE binary with ONE config — "both agree"
is structural, not two transcriptions of the rules drifting apart.

The lint checks are HARD-FAIL (architecture.lex §7): a missing tool exits
non-zero, it never skips. A clean run is ``0``; any failure is ``1``.

The ONE deliberate, narrowly-scoped exception is prettier's plugin-load abort
(issue #498, :func:`is_prettier_plugin_load_failure`): when a repo's
``.prettierrc`` names a plugin absent from ``node_modules`` (a ``--depth 1``
clone with no ``npm install``), prettier aborts on load with a Node
module-resolution error rather than reporting a formatting verdict. Those
plugins never affect JSON output, so this is environment-not-provisioned (same
spirit as the pixi ``command -v`` guard, #482), and the JSON leg FAILS OPEN with
a note. The match is tight — it requires the Node resolver phrasing and never
fires on prettier's own "code style issues" warning — so a genuinely dirty JSON
file still hard-fails. This is not a hole in the hard-fail contract; it is a
documented, single-class carve-out.

The checks are CHECK-ONLY by default (release's scar: ``prettier --write`` under
--all-files silently rewrites untouched files, so they must never mutate).
``--fix`` is the opt-in formatter pass — and only tools with a safe in-place fix
participate; the rest still run as checks.

The pure logic — the toolchain registry (:data:`LANGS`) and :func:`route` — is
kept out of the Exec boundary (:func:`_discover`, :func:`_shebang`,
:func:`_run_tool`) so it is unit-testable, the same split checks.py uses against
its gh calls. Tool execution goes through the one Exec runner
(:mod:`shipit.execrun`, ADR-0028): :func:`_run_tool` returns the runner's
:class:`~shipit.execrun.ExecResult` (``check=False`` — a nonzero rc is the
tool's verdict, not a transport failure) and lets a launch failure surface as
:class:`~shipit.execrun.ExecError`, which the orchestrator renders as the
hard-fail ``127``.

HERMETICITY — the gate owns the config (ADR-0037, epic LNT01 #513; WS01 #514 wired
the mechanism, WS03 #516 shipped the config set). The verdict must be a pure
function of the tracked files under ONE fixed config, identical on any machine and
in any repo. Two mechanisms, both here, enforce it:

* **Config injection** — each :class:`Tool` carries a :attr:`Tool.config_inject`
  fragment pinning it to shipit's canonical config; :meth:`Tool.argv` prepends it
  UNCONDITIONALLY (never gated on repo state, unlike the :attr:`Tool.editorconfig_pin`
  beachhead #493 it generalizes). A ``{config}`` placeholder in the fragment
  receives the canonical config PATH at argv-build time, resolved by
  :func:`_canonical_config` (WS03 #516) to the SHIPPED body under ``shipit/data``
  (``ruff.toml``, ``prettierrc.yaml``, ``markdownlint.yaml``, ``yamllint.yaml``).
  The path is the packaged data file — NOT a repo-tracked copy — so injection
  fires in ANY tree, including one that has not yet adopted the config: that is
  what blocks an ANCESTOR-directory config file (which the env scrub below does
  NOT cover — ancestor discovery walks the filesystem, not the environment). Tools
  whose config is inline flags rather than a file (shellcheck's ``--norc`` +
  ``--severity``, cargo's clippy lints and ``cargo fmt … --config-path <shipped
  rustfmt.toml>``) carry it directly in their ``check`` / ``fix`` tuples instead
  (see :data:`RUST`). Where such a tool ALSO discovers config by walking ancestor
  directories, a discovery-suppressing flag is what closes that walk: shellcheck's
  ``--norc`` (blocks the ``.shellcheckrc`` walk the scrub cannot reach) and shfmt's
  ``-i 0`` (blocks the ``.editorconfig`` walk). The WS02 invariance gate (#515)
  proves each such closure and pins the two that remain OPEN — clippy's
  ``clippy.toml`` and lexd's ``.lex.toml`` ancestor walks, for which no such flag
  exists yet (tracked in #526).
* **Env scrub** — :func:`_run_tool`, the single exec choke point, runs every
  linter under a :func:`_scrubbed_env` (``os.environ`` minus ``$HOME``, ``XDG_*``,
  and an explicit denylist of per-tool config vars — ``SHELLCHECK_OPTS``,
  ``RUFF_CONFIG``, ``CARGO_HOME``, ``CLIPPY_CONF_DIR``, ``YAMLLINT_CONFIG_FILE``)
  passed with ``replace_env=True``, so no user-global config file or tool env var
  is ever consulted. That denylist is deliberately enumerated, NOT a ``*_CONFIG*``
  substring: the substring would also drop ``PKG_CONFIG_PATH`` /
  ``FONTCONFIG_PATH`` and break cargo/C builds — those standard build vars are
  PRESERVED. No new plumbing in :mod:`shipit.execrun` — it already forwards
  ``env`` / ``replace_env`` (the Tree provisioner's mechanism).
"""

from __future__ import annotations

import functools
import logging
import os
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from .. import config, execrun, git
from ..tree import include
from ._errors import cli_errors

logger = logging.getLogger("shipit.lint")

# --------------------------------------------------------------------------
# The toolchain registry — the slim, valuable part of release-core's checks
# --------------------------------------------------------------------------
#
# These are release-core toolset.py's battle-tested command lines (NOT its
# lefthook orchestration, toolset provisioning, or verdict parsing — the three
# things the pixi + binary model replaces). Version pinning lives in pixi.toml /
# pixi.lock (the linters) and tools/provision-lexd.sh (lexd); the registry
# only encodes WHICH tool runs and HOW it is invoked.


#: The literal token a :attr:`Tool.config_inject` fragment carries where the
#: canonical config file PATH belongs; :meth:`Tool.argv` substitutes it with the
#: path :func:`_canonical_config` resolves (WS03 #516). A fragment whose tool has
#: no resolved path — the resolver returns ``None`` — is OMITTED rather than
#: emitted with a dangling placeholder, so a tool without a shipped file-config
#: (or a future tool wired before its body exists) simply runs unpinned rather
#: than crashing on a bogus ``--config {config}`` (ADR-0037, #514).
CONFIG_PLACEHOLDER = "{config}"


def _data_path(name: str) -> str:
    """The absolute filesystem path to a canonical-config body shipped under
    ``shipit/data`` (WS03 #516).

    The gate injects the PACKAGED config, never a repo-tracked copy — that is what
    makes injection fire in ANY tree (a repo that has not adopted the config, a
    bare fixture) and so blocks an ancestor-directory config file the env scrub
    cannot reach (ADR-0037). ``shipit.data`` is a namespace package (no
    ``__init__.py``), so ``resources.files("shipit.data")`` is a ``MultiplexedPath``
    that is NOT ``os.PathLike`` — only its ``joinpath`` result is a real ``Path``.
    We stringify that result with ``str`` (which works for a real ``Path`` AND any
    other ``Traversable``) rather than ``os.fspath`` (which would raise ``TypeError``
    on a non-``PathLike`` Traversable). The fail-fast we want is the existence check
    below, NOT a type error on a detour: shipit ships unzipped data trees, so a
    shipped body always resolves to a real on-disk file; if it does not — a
    zip-style resource with no filesystem path, or a body missing from the package,
    both packaging bugs and never a user-facing outcome — ``os.path.isfile`` is
    False and we raise a clear ``FileNotFoundError`` rather than handing a linter an
    unusable ``--config`` value. (``as_file`` is deliberately NOT used: its context
    manager can hand back a temp path cleaned up before the linter subprocess reads
    it.)
    """
    path = str(resources.files("shipit.data").joinpath(name))
    if not os.path.isfile(path):
        raise FileNotFoundError(
            f"shipit.data is missing canonical config {name!r} (resolved to "
            f"{path!r}); this file ships with the package — reinstall shipit "
            "or file a bug"
        )
    return path


#: The packaged canonical ``rustfmt.toml`` path, resolved once at import so the
#: :data:`RUST` ``cargo fmt`` tuples can carry it inline (WS03 #516). rustfmt takes
#: its config as a file the gate passes AFTER cargo's ``--`` separator
#: (``cargo fmt --all -- --config-path <this>``), which a :attr:`Tool.config_inject`
#: prepend cannot express — so it lives in the ``check`` / ``fix`` tuples directly
#: rather than going through :func:`_canonical_config`.
_RUSTFMT_CONFIG_PATH = _data_path("rustfmt.toml")


@dataclass(frozen=True)
class Tool:
    """One linter invocation: the binary plus its check args (files appended).

    ``fix`` is the formatter form applied under ``--fix``; ``None`` means the
    tool has no safe in-place fix, so in fix mode it falls back to its check
    form. The checks NEVER skip a tool — ``shipit lint --fix`` formats what it can
    AND still checks everything, so it can never pass while a non-fixable leg
    (shellcheck, yamllint, lexd) is failing.

    ``per_manifest`` tools speak to a build unit, not a file list (cargo has no
    file-batch form): they run once per tracked manifest directory of their Lang
    (see :func:`manifest_roots`) with NO files appended, cwd'd into that
    directory.

    ``config_inject`` pins this tool to shipit's ONE canonical config (ADR-0037,
    #514) — the generalization of :attr:`editorconfig_pin` from the #493 beachhead
    to EVERY config source. Unlike ``editorconfig_pin`` (gated on whether the repo
    tracks its own ``.editorconfig``), it is applied UNCONDITIONALLY: the canonical
    config is the only config, never the repo's or the ambient one. It is the flag
    fragment that names the config — ``("--config", "{config}")`` for a tool that
    takes a config FILE (ruff, prettier, markdownlint, yamllint), where the
    :data:`CONFIG_PLACEHOLDER` receives the canonical path; or a self-contained
    inline fragment for a tool whose config IS command-line flags (no external
    file). The canonical config BODIES ship under ``shipit/data`` and
    :func:`_canonical_config` (WS03 #516) maps each file-config tool to its packaged
    path (see :meth:`argv`). A tool whose canonical config lives inline in its
    ``check`` / ``fix`` args instead (shellcheck's severity, clippy's lints on the
    command line, rustfmt's ``--config-path`` after ``--``) leaves this empty and
    carries the config in those tuples directly (see :data:`RUST`).

    ``editorconfig_pin`` is the flag prefix that pins an ``.editorconfig``-aware
    tool to IGNORE any ambient/injected/ancestor ``.editorconfig`` — applied ONLY
    when the repo tracks no root ``.editorconfig`` of its own (see
    :func:`tracks_editorconfig` / issue #493). Empty for tools that do not consult
    ``.editorconfig``. It is a hermeticity pin, not a style choice: shfmt and
    prettier both honor an ``.editorconfig`` — including an untracked one written
    into the working tree by co-resident tooling, or an ancestor above the git
    root — which makes the lint verdict depend on the checkout location rather
    than the commit. Pinning restores "same commit → same verdict everywhere".
    This is the ONE injection the canonical config does not yet subsume: its
    replacement (a canonical shfmt/prettier config that fixes tabs-vs-spaces
    fleet-wide) is a WS03 body decision, so until then the beachhead's
    honor-tracked / neutralize-ambient gate stays — that is exactly the
    "existing lint behavior preserved for tracked-config-honoring cases"
    acceptance boundary of #514, and shipit's own ``[*.sh]`` house style depends
    on it.
    """

    binary: str
    check: tuple[str, ...]
    fix: tuple[str, ...] | None = None
    per_manifest: bool = False
    config_inject: tuple[str, ...] = ()
    editorconfig_pin: tuple[str, ...] = ()

    def argv(
        self,
        *,
        fix: bool,
        pin_editorconfig: bool = False,
        config_path: str | None = None,
    ) -> tuple[str, ...]:
        """The argv prefix for this run: the fix form in fix mode if the tool has
        one, else the check form (never ``None`` — the checks never skip).

        The canonical-config injection (:attr:`config_inject`, ADR-0037 / #514) is
        prepended UNCONDITIONALLY — it is not gated on repo state:

        * A fragment carrying the :data:`CONFIG_PLACEHOLDER` names a config FILE.
          When ``config_path`` is supplied (:func:`_canonical_config`'s packaged
          path, WS03 #516) the placeholder is substituted and the fragment
          prepended; when it is ``None`` the fragment is OMITTED, so a tool with no
          shipped file-config runs unpinned rather than with a dangling placeholder.
        * A fragment with no placeholder is an inline config (command-line flags,
          no external file) and is always prepended.

        When ``pin_editorconfig`` is set AND the tool has an
        :attr:`editorconfig_pin`, that pin is prepended too so the tool ignores any
        ambient ``.editorconfig`` (issue #493). Callers pass it when the repo
        tracks no ``.editorconfig`` of its own; a repo that DOES track one travels
        with that config in every checkout, so it is honored (pin off).
        """
        base = self.fix if (fix and self.fix is not None) else self.check
        injected: tuple[str, ...] = ()
        if self.config_inject:
            # A placeholder can live as its OWN token (`("--config", "{config}")`)
            # OR as a substring of one (`("--config={config}",)`), so match
            # per-token, not exact-element (`in` on the tuple) — the exact form
            # would miss the substring shape and inject the literal `{config}`
            # (round 1, agy). This only widens support: the exact-token form WS03
            # uses still matches, and `tok.replace` below rewrites either shape.
            if any(CONFIG_PLACEHOLDER in tok for tok in self.config_inject):
                if config_path is not None:
                    injected = tuple(
                        tok.replace(CONFIG_PLACEHOLDER, config_path)
                        for tok in self.config_inject
                    )
                # else: the resolver yielded no path for this tool (an inline-config
                # or not-yet-shipped tool) — omit the fragment so it runs unpinned
                # rather than with a dangling `--config {config}` (WS03 #516).
            else:
                injected = self.config_inject
        pin = (
            self.editorconfig_pin
            if (pin_editorconfig and self.editorconfig_pin)
            else ()
        )
        return (*injected, *pin, *base)


@dataclass(frozen=True)
class Lang:
    """A language leg: how files map to it, and the tools that check it."""

    name: str
    extensions: tuple[str, ...]
    tools: tuple[Tool, ...]
    shebangs: tuple[str, ...] = ()  # interpreter basenames for extensionless files
    manifests: tuple[str, ...] = ()  # manifest basenames rooting per_manifest runs


PYTHON = Lang(
    name="python",
    extensions=(".py",),
    # ruff takes its config as a FILE via `--config <path>`; both legs pin to the
    # canonical `ruff.toml` (ADR-0037), carved out of shipit's own pyproject.toml
    # in WS03 (#516) and shipped as `shipit/data/ruff.toml`. `_canonical_config`
    # resolves the placeholder to that packaged path.
    tools=(
        Tool(
            "ruff",
            ("check",),
            fix=("check", "--fix"),
            config_inject=("--config", CONFIG_PLACEHOLDER),
        ),
        Tool(
            "ruff",
            ("format", "--check"),
            fix=("format",),
            config_inject=("--config", CONFIG_PLACEHOLDER),
        ),
    ),
)
RUST = Lang(
    name="rust",
    extensions=(".rs",),
    manifests=("Cargo.toml",),
    # cargo speaks to a crate/workspace, not a file list, so both tools are
    # per_manifest: one run per tracked Cargo.toml directory (see
    # manifest_roots — every tracked manifest runs, never collapsed, so a
    # nested crate that ISN'T a workspace member is never silently skipped).
    # clippy and fmt both carry --all so a workspace root covers its declared
    # members even when only the root manifest is tracked (release-core's
    # battle-tested forms, docs/prd/lint-checks.md). clippy findings are hard
    # errors (-D warnings) and clippy has no safe in-place fix here, so under
    # --fix it still runs its check form; `cargo fmt --all` is the one rust
    # --fix leg. A repo with .rs files but no tracked Cargo.toml runs at the
    # root and fails on cargo's own error — hard, never a silent skip. The
    # rust toolchain is assumed provisioned per the repo's toolchain
    # declaration (ADR-0007); a missing cargo is the standard hard-fail 127.
    #
    # Canonical-config injection (ADR-0037, #514/#516) for cargo is INLINE, not a
    # `config_inject` prefix: cargo's config tokens must follow the `--`
    # separator, which a prepend cannot express. So the config lives in the tuples:
    #
    # * clippy — the canonical clippy floor is `-D warnings`, which denies EVERY
    #   lint clippy warns by default (the `clippy::all` group and rustc's own), i.e.
    #   "clippy must be clean." WS03 (#516) BLESSES that seed as the canonical set
    #   rather than layering `pedantic`/`nursery` on top: Rust is greenfield fleet-
    #   wide (no repo commits a clippy config to promote) and shipit has no Rust to
    #   validate a richer set against, so tightening is deferred to WS05 (#518),
    #   which exercises the gate on real crates. Left EXACT so nothing new is
    #   invented here.
    # * fmt — pinned to the shipped canonical `rustfmt.toml` via `--config-path`
    #   (`_RUSTFMT_CONFIG_PATH`), on BOTH the `--check` and the in-place fix leg, so
    #   the format verdict is a pure function of that one body. The env scrub
    #   (`_run_tool`) already blocks ambient `~/.config/rustfmt` etc.
    tools=(
        Tool(
            "cargo",
            (
                "clippy",
                "--all",
                "--all-targets",
                "--all-features",
                "--",
                "-D",
                "warnings",
            ),
            per_manifest=True,
        ),
        Tool(
            "cargo",
            ("fmt", "--all", "--", "--check", "--config-path", _RUSTFMT_CONFIG_PATH),
            fix=("fmt", "--all", "--", "--config-path", _RUSTFMT_CONFIG_PATH),
            per_manifest=True,
        ),
    ),
)
SHELL = Lang(
    name="shell",
    extensions=(".sh", ".bash"),
    shebangs=("sh", "bash"),
    tools=(
        # shellcheck's canonical config is INLINE flags (it has no `--config
        # <file>`). Two flags, both part of the canonical config the gate OWNS
        # (ADR-0037); `config_inject` stays empty because neither is a placeholder
        # file-config fragment — they ride the check tuple like every other inline
        # tool's config:
        #   * `--norc` — HERMETICITY. shellcheck discovers `.shellcheckrc` by walking
        #     the filesystem UPWARD from the script's directory, and also reads
        #     `$HOME/.shellcheckrc`. The `_run_tool` env scrub drops `$HOME` and
        #     `SHELLCHECK_OPTS`, but it CANNOT stop the ancestor-directory walk — an
        #     untracked `.shellcheckrc` in a PARENT of the checkout is still consulted
        #     even with `$HOME` unset, so the verdict would move with WHERE the tree
        #     is checked out. The WS02 invariance gate (#515) proved this with a real
        #     grandparent `.shellcheckrc`. `--norc` suppresses ALL `.shellcheckrc`
        #     discovery, closing the ancestor leak the scrub cannot reach — the
        #     shellcheck analogue of prettier's `--no-editorconfig` / shfmt's `-i 0`,
        #     but UNCONDITIONAL: the gate owns shellcheck's config outright, with no
        #     "honor the repo's own `.shellcheckrc`" carve-out (unlike the
        #     editorconfig pin, which defers to a repo that tracks its own).
        #   * `--severity=info` — the canonical rule floor WS03 (#516) blessed:
        #     shipit's own shell lints clean at it and there is no fleet driver to
        #     raise it, so nothing new is invented here.
        Tool("shellcheck", ("--norc", "--severity=info")),
        # `-i 0` is shfmt's tab default, but PASSING any formatting flag makes
        # shfmt skip `.editorconfig` entirely — so the pin both defaults to tabs
        # and neutralizes an ambient/injected/ancestor `.editorconfig` when the
        # repo tracks none of its own (issue #493). WS03's deliberate call: KEEP
        # this pin gated, do NOT make `-i 0` unconditional. shfmt's canonical config
        # IS these inline flags, but an unconditional `-i 0` would reformat shipit's
        # own 4-space `[*.sh]` house style (and every fleet repo's) to tabs — a
        # shell-style normalization owned by WS06 (#519), not config definition. See
        # the paired prettier `--no-editorconfig` note above; the gate stays hermetic
        # meanwhile — the env scrub kills `~/.editorconfig`, and when the pin is gated
        # off (the repo tracks its own root `.editorconfig`) that config must declare
        # `root = true` (`tracks_editorconfig` / `editorconfig_declares_root`, #528),
        # which is exactly what stops the ancestor walk — a tracked non-`root = true`
        # file keeps the pin ON.
        Tool("shfmt", ("-d",), fix=("-w",), editorconfig_pin=("-i", "0")),
    ),
)
YAML = Lang(
    name="yaml",
    extensions=(".yml", ".yaml"),
    # yamllint takes its config as a FILE via `-c <path>`; pin to the canonical
    # one (ADR-0037) — the already-managed `shipit/data/yamllint.yaml`, confirmed
    # as canonical in WS03 (#516) and now resolved by `_canonical_config`. This is
    # also the gate for GitHub Actions workflows (`.yml`).
    tools=(Tool("yamllint", ("--strict",), config_inject=("-c", CONFIG_PLACEHOLDER)),),
)
JSON = Lang(
    name="json",
    extensions=(".json",),
    # prettier takes its config as a FILE via `--config <path>`; pin to the
    # canonical `shipit/data/prettierrc.yaml` (ADR-0037, WS03 #516), resolved by
    # `_canonical_config`. The canonical body sets the fleet's one rule set
    # (singleQuote / printWidth 100 / tabWidth 2 / semi false / trailingComma none)
    # + the svelte/tailwind plugin capability.
    #
    # `--no-editorconfig` STAYS the #493 editorconfig pin, gated on the repo
    # tracking its own `.editorconfig`. WS03's deliberate call (the pin's
    # unconditional replacement was left to this WS): do NOT make it unconditional
    # yet. Doing so is paired with shfmt's `-i 0` as a single shell/format-style
    # normalization — shipit's own `.editorconfig [*.sh]` is 4-space by design, so
    # flipping the pin on unconditionally forces reformatting shipit's shell + every
    # fleet repo's editorconfig, which is fleet debt-clear work owned by WS06 (#519),
    # not config-definition work. The gate stays hermetic meanwhile: the env scrub
    # kills `~/.editorconfig`, and when the pin is gated off (the repo tracks its own
    # root `.editorconfig`) that config must declare `root = true`
    # (`tracks_editorconfig` / `editorconfig_declares_root`, #528), which is exactly
    # what stops the ancestor walk — a tracked non-`root = true` file keeps the pin ON.
    tools=(
        Tool(
            "prettier",
            ("--check", "--log-level", "warn"),
            fix=("--write",),
            config_inject=("--config", CONFIG_PLACEHOLDER),
            editorconfig_pin=("--no-editorconfig",),
        ),
    ),
)
MARKDOWN = Lang(
    name="markdown",
    extensions=(".md",),
    # markdownlint takes its config as a FILE via `--config <path>`; pin to the
    # already-managed `shipit/data/markdownlint.yaml` (ADR-0037), confirmed as
    # canonical in WS03 (#516) and resolved by `_canonical_config`. (The separate
    # `.markdownlintignore` is auto-discovered from cwd, not a `--config`, so it is
    # unaffected by injection.)
    tools=(
        Tool(
            "markdownlint",
            (),
            fix=("--fix",),
            config_inject=("--config", CONFIG_PLACEHOLDER),
        ),
    ),
)
LEX = Lang(
    name="lex",
    extensions=(".lex",),
    # `lexd format` writes to stdout only (no in-place form), so lex has no safe
    # --fix; it runs as check-only via `lexd check` (CI-friendly exit codes).
    tools=(Tool("lexd", ("check",)),),
)

LANGS: tuple[Lang, ...] = (PYTHON, RUST, SHELL, YAML, JSON, MARKDOWN, LEX)


# --------------------------------------------------------------------------
# Pure routing
# --------------------------------------------------------------------------


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def lang_for(path: str, shebang: str | None = None) -> Lang | None:
    """The language a file routes to — by extension, else by shebang interpreter.

    Extensionless scripts route by their shebang's interpreter basename (release
    routes shell this way; mirror it). A file matching nothing is unmanaged.
    """
    name = _basename(path)
    if "." in name:
        ext = "." + name.rsplit(".", 1)[-1]
        for lang in LANGS:
            if ext in lang.extensions:
                return lang
        return None
    interp = _interp(shebang)
    if interp:
        for lang in LANGS:
            if interp in lang.shebangs:
                return lang
    return None


def _interp(shebang: str | None) -> str | None:
    """The interpreter basename from a shebang line (``/usr/bin/env bash`` → ``bash``)."""
    if not shebang:
        return None
    tokens = shebang.split()
    if not tokens:
        return None
    first = tokens[0].rsplit("/", 1)[-1]
    # `/usr/bin/env bash` — the real interpreter is the arg after env.
    if first == "env" and len(tokens) > 1:
        return tokens[1].rsplit("/", 1)[-1]
    return first


def manifest_roots(paths: list[str], manifests: tuple[str, ...]) -> list[str]:
    """Every directory (repo-relative, ``"."`` for the root) holding one of
    ``manifests`` among the tracked paths, sorted. Pure (no I/O).

    Every tracked manifest gets its own run — nested manifests are NOT
    collapsed under an ancestor. Cargo does not make a nested manifest a
    workspace member automatically (a repo can have an independent nested
    crate, or a workspace that excludes one), so collapsing would silently
    skip it; the ``--all`` on the tools makes a true workspace root cover its
    declared members, and running every manifest guarantees the rest are
    never skipped. Redundant re-checks of shared members are cargo-cache
    cheap; a silent miss in a hard-fail lint is not.
    """
    dirs = {
        path.rsplit("/", 1)[0] if "/" in path else "."
        for path in paths
        if _basename(path) in manifests
    }
    return sorted(dirs)


def lex_projections(paths: list[str]) -> set[str]:
    """Tracked ``X.md`` files that are projections of a tracked ``X.lex`` source.

    The ``.lex`` is the gated source (the lexd leg); the ``.md`` is generated
    output carrying a "do not hand edit" preamble, so markdownlint's prose
    rules over it are noise about the generator, not signal about a document
    anyone edits. Pure (no I/O): "is a projection" is decided from the tracked
    file list alone, so the rule is consumer-generic — no repo-local
    ``.markdownlintignore`` entry per projection (ADP00-WS10, #436).
    """
    sources = {p for p in paths if p.endswith(".lex")}
    return {p for p in paths if p.endswith(".md") and p[:-3] + ".lex" in sources}


def editorconfig_declares_root(content: str) -> bool:
    """Whether an ``.editorconfig`` body declares ``root = true`` in its preamble. Pure.

    editorconfig is INI-like: ``root`` is only meaningful in the PREAMBLE — the
    lines BEFORE the first ``[section]`` header. Only ``root = true`` (compared
    case-insensitively, e.g. ``Root = True``) stops an editorconfig-aware tool
    from walking UP into an ancestor ``.editorconfig``; ``root`` set to anything
    else, or appearing inside a section, does not — so neither counts here.
    Comment lines (``#`` / ``;``) and blanks are skipped (issue #528).

    LAST-WINS: a duplicated ``root`` in the preamble resolves to the LAST
    assignment, matching editorconfig semantics — and the safe (over-pin on
    ambiguity) direction. Returning on the FIRST match would read
    ``root = true\nroot = false`` as rooted while a real tool treats it as non-root
    and walks up — the one direction that could leak.
    """
    result = False
    for raw in content.splitlines():
        line = raw.strip()
        if not line or line[0] in ";#":
            continue
        if line.startswith("["):
            break  # first section header — the preamble is over
        key, sep, value = line.partition("=")
        if sep and key.strip().lower() == "root":
            result = value.strip().lower() == "true"
    return result


def tracks_editorconfig(paths: list[str], read_root: Callable[[], str | None]) -> bool:
    """Whether the repo tracks a ROOT ``.editorconfig`` that declares ``root = true``. Pure.

    The signal that decides the editorconfig hermeticity pin (issue #493). A repo
    that commits a root ``.editorconfig`` declaring ``root = true`` OWNS its
    formatting config: the file travels with every checkout, so its verdict is
    already commit-determined and shfmt / prettier are left to honor it (shipit's
    own tab-vs-space shell house style depends on this). A repo that tracks none
    gets the pin — the editorconfig-aware tools are told to ignore any
    ambient/injected/ancestor ``.editorconfig`` a co-resident tool or a checkout
    location may have introduced, so the verdict cannot flip on where or beside
    what the tree is checked out.

    Presence is NECESSARY but NOT SUFFICIENT (issue #528). A tracked root
    ``.editorconfig`` disables the pin ONLY when it declares ``root = true`` — that
    declaration is precisely what stops an editorconfig-aware tool from walking UP
    into an ancestor ``.editorconfig``. A tracked non-``root = true`` file leaves
    the pin ON: without ``root = true`` the tool would otherwise still walk up into
    an ancestor config, so honoring it would make the verdict depend on the
    checkout location again (the very leak the pin exists to close). So a tracked
    root file gates the pin off ONLY with ``root = true`` — presence alone no
    longer suffices.

    Keyed on the ROOT ``.editorconfig`` ONLY, never a nested one (round 1, codex):
    the pin is a single tree-wide flag (shfmt/prettier run once at the root), so
    honoring a nested tracked config would need splitting their batches by
    editorconfig scope — deliberately NOT done. Keying on any nested config would
    open a hermeticity HOLE instead: a repo tracking only a nested ``.editorconfig``
    would disable the pin repo-wide, yet files OUTSIDE that nested scope would still
    walk up and consume an untracked root/ancestor config, making the verdict depend
    on checkout location again. Root-only keeps the guarantee absolute — identical
    verdict everywhere, no exceptions. ``paths`` MUST be the repo's canonical
    top-level tracked list, repo-root-relative (see :func:`_tracks_root_editorconfig`),
    so ``.editorconfig`` is the root file and ``sub/.editorconfig`` a nested one; the
    exact match also rejects a lookalike (``my.editorconfig.bak``).

    ``read_root`` lazily returns the root ``.editorconfig`` body (or ``None`` if it
    cannot be read). It is consulted ONLY when the exact path is tracked, so the
    presence gate stays a pure list check and the content read is deferred to the
    one case that needs it.
    """
    if ".editorconfig" not in paths:
        return False
    content = read_root()
    return content is not None and editorconfig_declares_root(content)


def _ignore_matchers(patterns: list[str]) -> list[include.PatternSet]:
    """Compile each consumer ``[lint].ignore`` glob into shipit's gitignore matcher.

    Reuses the ``.gitignore`` engine that backs ``.treeinclude``
    (:mod:`shipit.tree.include`) — the SAME syntax as the managed
    ``.markdownlintignore`` this seam lets a consumer stop editing (#484) — so the
    globs are GENUINELY gitignore-style, not the anchored full-path match
    ``PurePosixPath.full_match`` gave: a trailing-slash directory pattern
    (``CHANGELOG/``) drops that whole subtree, an unanchored name (``CHANGELOG.md``)
    floats to any depth, a leading ``/`` anchors to the repo root, and ``*`` never
    crosses ``/``.

    ONE PatternSet per entry — a path is ignored if ANY entry matches — so a
    single malformed glob narrows nothing rather than crashing the gate mid-run or
    disabling its valid siblings (:func:`shipit.tree.include.parse` compiles to a
    regex, which can raise :class:`re.error` on a bad character class / range).
    """
    matchers: list[include.PatternSet] = []
    for pattern in patterns:
        try:
            matchers.append(include.parse(pattern))
        except re.error:
            continue
    return matchers


def path_ignored(path: str, patterns: list[str]) -> bool:
    """Whether ``path`` matches any consumer ``[lint].ignore`` glob. Pure.

    True gitignore semantics via shipit's ``.treeinclude`` engine (see
    :func:`_ignore_matchers`): ``**`` matches any run of segments, ``*`` never
    crosses ``/``, a trailing-slash pattern matches a directory's whole subtree,
    and an unanchored name floats to any depth. A malformed pattern is a no-match,
    never a crash — a bad glob narrows nothing.
    """
    return any(m.match(path) for m in _ignore_matchers(patterns))


def drop_ignored(paths: list[str], patterns: list[str]) -> list[str]:
    """``paths`` with every consumer-ignored entry removed, order preserved. Pure.

    Applied to the WHOLE discovered file list before routing (#484), so a single
    ``[lint].ignore`` glob drops a path from every Lang leg — the seam is
    Lang-agnostic (markdownlint, shfmt, ruff, …), not per-linter plumbing. Compiles
    the globs ONCE, then filters.
    """
    if not patterns:
        return paths
    matchers = _ignore_matchers(patterns)
    if not matchers:
        return paths
    return [p for p in paths if not any(m.match(p) for m in matchers)]


#: Built-in test-data directory conventions whose files ``--fix`` must NEVER
#: rewrite in place (issue #500). A deliberately-malformed or byte-exact fixture
#: silently corrupted by ``markdownlint --fix`` / ``prettier --write`` /
#: ``shfmt -w`` / ``ruff --fix`` / ``cargo fmt`` breaks the very tests it backs.
#: Gitignore-style directory patterns (the same ``.treeinclude`` engine the
#: consumer ``[lint].ignore`` seam uses): each floats to any depth and drops the
#: whole subtree. Unlike ``[lint].ignore`` (opt-in, applies in BOTH modes), this
#: guard is ALWAYS ON and MUTATION-ONLY. It is enforced two ways, both in
#: :func:`run`: a batch fixer (markdownlint, prettier, shfmt, ruff) has these
#: paths dropped from its file batch (:func:`drop_protected_testdata`); the
#: per-manifest Rust formatter (``cargo fmt``) takes no file batch and rewrites a
#: whole crate — reaching a protected ``.rs`` via a ``mod`` decl or a fixture
#: that is itself a crate — so it is snapshotted and any protected ``.rs`` it
#: rewrites is restored byte-for-byte (:func:`protected_testdata`, #502).
#:
#: CHECK mode still runs every tool over these files, so a genuinely-broken
#: fixture is still reported; only the destructive auto-rewrite is refused. The
#: ONE exception is markdown, spared in check mode too — but by a SEPARATE
#: mechanism, not this guard: the managed ``.markdownlintignore`` lists these
#: same dirs so ``markdownlint`` skips them regardless of argv (malformed
#: markdown is a common fixture genre; see docs/prd/lint-checks.md).
PROTECTED_TESTDATA_GLOBS: tuple[str, ...] = (
    "fixtures/",
    "__fixtures__/",
    "testdata/",
    "golden/",
    "goldens/",
    "snapshots/",
    "__snapshots__/",
)


@functools.cache
def _protected_matchers() -> tuple[include.PatternSet, ...]:
    """The compiled :data:`PROTECTED_TESTDATA_GLOBS` matchers, built ONCE.

    The glob list is a module constant, so the matchers never vary — caching
    them at module level makes the ``run`` loop's repeated guard calls (once per
    mutating tool, NOT per file) genuinely compile-once, and lets the guard
    functions below say "compiled ONCE" truthfully.
    """
    return tuple(_ignore_matchers(list(PROTECTED_TESTDATA_GLOBS)))


def drop_protected_testdata(paths: list[str]) -> list[str]:
    """``paths`` with every built-in protected test-data path removed, order preserved.

    Pure. The MUTATION guard behind #500: applied to the batch handed to a
    batch-fixer running its in-place fix form, so a fixture under a
    :data:`PROTECTED_TESTDATA_GLOBS` directory is never auto-rewritten. Shares
    the module-cached matchers (:func:`_protected_matchers`, compiled ONCE) with
    :func:`protected_testdata`, its exact complement; a consumer needing MORE
    exclusions still has the ``[lint].ignore`` seam.
    """
    matchers = _protected_matchers()
    return [p for p in paths if not any(m.match(p) for m in matchers)]


def protected_testdata(paths: list[str]) -> list[str]:
    """The protected subset of ``paths`` — what :func:`drop_protected_testdata`
    removes — order preserved. Pure.

    The snapshot set for the per-manifest ``cargo fmt`` guard (#502): cargo
    formats a whole crate and takes no file batch, so a protected ``.rs``
    reachable via a ``mod`` decl (or a fixture that is itself a crate) can't be
    kept off an argv the way a batch fixer's is. :func:`run` snapshots these
    paths' bytes before the fix-form run and restores any the fixer rewrote.
    """
    matchers = _protected_matchers()
    return [p for p in paths if any(m.match(p) for m in matchers)]


def route(
    paths: list[str], shebangs: dict[str, str | None] | None = None
) -> list[tuple[Lang, list[str]]]:
    """Bucket paths by language, in registry order. Pure (no I/O).

    Generated lex projections never route to markdown: their ``.lex`` source
    routes to the lexd leg instead (see :func:`lex_projections`).
    """
    shebangs = shebangs or {}
    projections = lex_projections(paths)
    buckets: dict[str, list[str]] = {}
    for path in paths:
        if path in projections:
            continue
        lang = lang_for(path, shebangs.get(path))
        if lang is not None:
            buckets.setdefault(lang.name, []).append(path)
    return [(lang, buckets[lang.name]) for lang in LANGS if lang.name in buckets]


# --------------------------------------------------------------------------
# Reporting (pure)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolRun:
    """The outcome of one tool invocation over its file batch."""

    lang: str
    binary: str
    label: str
    returncode: int
    output: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def verdict(runs: list[ToolRun]) -> int:
    """``0`` when every run passed, ``1`` otherwise — the whole check contract."""
    return 0 if all(run.ok for run in runs) else 1


def is_prettier_plugin_load_failure(binary: str, rc: int, output: str) -> bool:
    """Whether a prettier run failed because a configured plugin could not be
    RESOLVED (a Node module-resolution / plugin-load abort), not because a file
    is misformatted. Pure — the detection is out of the Exec boundary so it is
    unit-testable (ADR-0028).

    The narrow, documented fail-open exception to the hard-fail contract
    (module docstring; issue #498). A repo whose ``.prettierrc`` names a plugin
    (``prettier-plugin-svelte``, tailwind, …) that is absent from
    ``node_modules`` — a ``--depth 1`` clone with no ``npm install`` — makes
    prettier abort ON LOAD with a Node resolver error::

        Cannot find package 'prettier-plugin-svelte' imported from …/noop.js

    That nonzero exit is environment-not-provisioned, NOT a lint verdict: those
    plugins format ``.svelte`` / CSS, never JSON, so the JSON leg's output is
    identical with or without them. Surfacing it as a failure produces false
    failures whenever the gate runs before deps are installed (Tree/CI legs,
    fleet measurement).

    The match is DELIBERATELY tight so it can never swallow a real formatting
    failure: prettier's own "code style issues" warning never carries
    ``imported from`` (the Node ESM-resolver phrase), so the pairing of a
    "cannot find package/module" phrase WITH ``imported from`` isolates the
    plugin-load class alone. Only ``prettier`` and only a nonzero rc qualify;
    a clean run (``rc == 0``) is never a plugin-load failure.
    """
    if binary != "prettier" or rc == 0:
        return False
    lowered = output.lower()
    has_resolver_phrase = (
        "cannot find package" in lowered or "cannot find module" in lowered
    )
    return has_resolver_phrase and "imported from" in lowered


# --------------------------------------------------------------------------
# The Exec + git boundary (patched in tests)
# --------------------------------------------------------------------------


def _discover(root: Path) -> list[str]:
    return git.ls_files(cwd=str(root))


def _read_editorconfig(path: Path) -> str | None:
    """The body of the root ``.editorconfig`` at ``path``, or ``None`` if it cannot
    be read. A separate seam so a test can stub the content read (issue #528).

    Read with ``utf-8-sig`` so a leading UTF-8 BOM is stripped: otherwise the BOM
    rides line 1 and ``﻿root = true`` fails to parse (currently a safe
    over-pin, but wrong — such a file genuinely declares ``root = true``)."""
    try:
        return path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None


def _tracks_root_editorconfig(root: Path) -> bool:
    """Whether the git repo containing ``root`` tracks a ROOT ``.editorconfig`` that
    declares ``root = true`` — reading the root file's content, not just its
    presence, to confirm it (issue #528).

    The editorconfig pin decision (issue #493) is a repo-wide git FACT, so it is
    read from the repo's canonical tracked-file list at its TOP LEVEL — resolved
    via :func:`shipit.git.repo_root` — deliberately NOT from the routed ``files``:

    * ``files`` is filtered by ``[lint].ignore`` (:func:`drop_ignored`), but an
      ignored path must not flip hermeticity — the pin is a git-tracking fact, not
      a routing decision (round 1, copilot / agy).
    * ``files`` is scoped to the ``path`` a run targets, so ``shipit lint src/``
      would miss a root-tracked ``.editorconfig`` and wrongly pin a repo that owns
      one; reading the top level sees it regardless of the target (round 1, agy).

    A ``root`` outside any checkout has no tracked config → not tracked → pinned,
    consistent with the honor-tracked / neutralize-ambient rule.
    """
    repo_root = git.repo_root(cwd=str(root))
    if repo_root is None:
        return False
    return tracks_editorconfig(
        git.ls_files(cwd=repo_root),
        lambda: _read_editorconfig(Path(repo_root) / ".editorconfig"),
    )


def _ignore_globs(root: Path) -> list[str]:
    """The consumer ``[lint].ignore`` globs from ``root``'s ``.shipit.toml`` (#484).

    No config, no ``[lint]`` table, or an empty list → ``[]`` (the gate covers
    everything). This is the ONLY I/O read of the seam; the filtering itself
    (:func:`drop_ignored`) is pure and unit-tested off this.
    """
    cfg_path = root / config.CONFIG_NAME
    if not cfg_path.is_file():
        return []
    return config.load_lint_ignore(config.load(cfg_path))


def _shebang(path: Path) -> str | None:
    """The shebang body of ``path`` (without ``#!``), or ``None``."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            first = fh.readline()
    except OSError:
        return None
    return first[2:].strip() if first.startswith("#!") else None


#: Each check Exec's stated timeout, in seconds (ADR-0028: every Exec states
#: its bound deliberately — never the runner's implicit default). A linter over
#: a whole tree is local but legitimately slow on a large repo, so the runner's
#: generous default IS the right bound — stated on the wire rather than
#: inherited, so the no-implicit-timeout sweep stays grep-verifiable.
CHECK_TIMEOUT: float = execrun.DEFAULT_TIMEOUT


def _snapshot(root: Path, rel_paths: list[str]) -> dict[str, bytes]:
    """The pre-image bytes of each ``rel_paths`` file under ``root`` that exists.

    The per-manifest fix guard's pre-image (#500/#502): ``cargo fmt`` rewrites a
    whole crate and takes no file batch, so a protected ``.rs`` reachable via a
    ``mod`` decl can't be kept off its argv the way a batch fixer's is. Instead
    the verb snapshots the protected files, lets the fixer run, then restores any
    it rewrote (see :func:`_restore`). A missing/unreadable path is simply not
    snapshotted — there is then nothing to restore.
    """
    snapshot: dict[str, bytes] = {}
    for rel in rel_paths:
        try:
            snapshot[rel] = (root / rel).read_bytes()
        except OSError:
            continue
    return snapshot


def _restore(root: Path, snapshot: dict[str, bytes]) -> list[str]:
    """Rewrite each snapshot file a fixer changed back to its pre-image bytes;
    return the restored paths. Only CHANGED files are written, so an untouched
    fixture incurs no write. An unreadable/unwritable path is skipped.
    """
    restored: list[str] = []
    for rel, original in snapshot.items():
        path = root / rel
        try:
            if path.read_bytes() != original:
                path.write_bytes(original)
                restored.append(rel)
        except OSError:
            continue
    return restored


#: The tool-specific config env vars the scrub drops, enumerated DELIBERATELY
#: (round 1, agy): a blanket ``"_CONFIG" in key`` substring was too broad — it
#: also stripped ``PKG_CONFIG_PATH`` / ``FONTCONFIG_PATH`` (standard build vars),
#: which can break the cargo/C builds clippy drives. So the config-file/override
#: vars are listed one by one instead, each a config source for a LANGS tool:
#:
#: * ``SHELLCHECK_OPTS``      — shellcheck: injects arbitrary flags
#: * ``YAMLLINT_CONFIG_FILE`` — yamllint: points at a config file
#: * ``RUFF_CONFIG``          — ruff: points at a config file
#: * ``CARGO_HOME``           — cargo/clippy: roots ambient ``config.toml`` discovery
#: * ``CLIPPY_CONF_DIR``      — clippy: roots ``clippy.toml`` discovery
#:
#: ``CARGO_HOME`` / ``CLIPPY_CONF_DIR`` close the Rust leak (round 1, codex):
#: without them ``cargo clippy`` reads a machine-local ``config.toml`` /
#: ``clippy.toml`` outside the repo. WS03 (#516) may EXTEND this set as it wires
#: more canonical configs (a per-tool config env var for a newly-pinned tool).
_TOOL_CONFIG_ENV_VARS: frozenset[str] = frozenset(
    {
        "SHELLCHECK_OPTS",
        "YAMLLINT_CONFIG_FILE",
        "RUFF_CONFIG",
        "CARGO_HOME",
        "CLIPPY_CONF_DIR",
    }
)


#: Environment variables scrubbed before every linter subprocess (ADR-0037,
#: #514): the ambient sources through which a user-global config file or a
#: tool-specific override would otherwise leak into the verdict. ``$HOME`` roots
#: ``~/.editorconfig`` / ``~/.shellcheckrc`` / ``~/.config`` discovery; ``XDG_*``
#: relocates that config dir; and the explicit :data:`_TOOL_CONFIG_ENV_VARS`
#: denylist names each per-tool config file / override var to DROP. Removed so the
#: verdict is a pure function of the tracked files under the canonical config,
#: never the machine it runs on.
def _is_ambient_config_var(key: str) -> bool:
    """Whether env var ``key`` is an ambient-config source the scrub drops.

    Pure. Three narrow shapes — deliberately NOT a blanket ``"_CONFIG" in key``
    substring, which also stripped ``PKG_CONFIG_PATH`` / ``FONTCONFIG_PATH`` and
    broke cargo/C builds (round 1, agy): ``HOME`` (exact) roots ``~/.config``
    discovery; ``XDG_*`` (prefix) relocates it; and membership in the explicit
    :data:`_TOOL_CONFIG_ENV_VARS` drop-set — the per-tool config vars
    (``SHELLCHECK_OPTS``, ``RUFF_CONFIG``, ``CARGO_HOME``, ``CLIPPY_CONF_DIR``,
    ``YAMLLINT_CONFIG_FILE``), each pointing a specific linter at out-of-repo
    config. It is a denylist (these vars are dropped, everything else kept), NOT
    an allowlist.
    """
    return key == "HOME" or key.startswith("XDG_") or key in _TOOL_CONFIG_ENV_VARS


def _scrubbed_env() -> dict[str, str]:
    """A COMPLETE child environment: ``os.environ`` minus the ambient-config vars
    (:func:`_is_ambient_config_var`). Everything else — ``PATH`` above all — is
    preserved, so the linters still launch; only the config-leaking keys are gone.

    Passed with ``replace_env=True`` (:func:`_run_tool`), which makes this the
    child's WHOLE environment — hence starting from a COPY of ``os.environ`` and
    removing keys, never a bare dict that would strip ``PATH`` and break every
    launch (ADR-0037, #514).
    """
    return {k: v for k, v in os.environ.items() if not _is_ambient_config_var(k)}


#: The file-config tools mapped to the ``shipit/data`` body each pins to (WS03
#: #516). Keyed by :attr:`Tool.binary`: every binary here carries a
#: :data:`CONFIG_PLACEHOLDER` ``config_inject`` fragment that :func:`_canonical_config`
#: fills with the packaged path. `ruff.toml` is the carve-out of shipit's own
#: `[tool.ruff.lint]`; `prettierrc.yaml` is the fleet-unified rule set; the two
#: `*lint.yaml` are the already-managed universals confirmed canonical. The
#: inline-config tools (shellcheck, shfmt, cargo, lexd) are ABSENT by design —
#: their config rides their `check`/`fix` tuples, not a `--config` file.
_CANONICAL_CONFIG_FILES: dict[str, str] = {
    "ruff": "ruff.toml",
    "prettier": "prettierrc.yaml",
    "markdownlint": "markdownlint.yaml",
    "yamllint": "yamllint.yaml",
}


def _canonical_config(tool: Tool, root: Path) -> str | None:
    """The absolute path to shipit's canonical config file for ``tool``, or ``None``.

    The WS03 (#516) resolver behind the WS01 (#514) injection mechanism
    (:attr:`Tool.config_inject` + :meth:`Tool.argv`): a file-config tool
    (:data:`_CANONICAL_CONFIG_FILES`) resolves to the SHIPPED body under
    ``shipit/data`` (:func:`_data_path`); any other tool resolves to ``None``, so
    its placeholder fragment is omitted (an inline-config or unconfigured tool runs
    unpinned rather than with a dangling ``--config``).

    The returned path is the PACKAGED data file, independent of ``root`` — so
    injection fires in ANY tree (a not-yet-adopted repo, a bare invariance-test
    fixture) and thereby blocks an ancestor-directory config file, which the env
    scrub cannot reach (ancestor discovery walks the filesystem, not the
    environment; ADR-0037). ``root`` is kept in the signature for parity with the
    other run-injected boundary seams and so a future per-repo override could hook
    here. Injected in :func:`run` (like :func:`_tracks_root_editorconfig`) so a test
    can supply a stub resolver.
    """
    name = _CANONICAL_CONFIG_FILES.get(tool.binary)
    return _data_path(name) if name is not None else None


def _run_tool(binary: str, args: list[str], cwd: Path) -> execrun.ExecResult:
    """Run ``binary args`` in ``cwd`` through the one Exec runner.

    ``check=False``: a nonzero rc is the tool's *verdict* (the normal failing-check
    outcome), not a transport failure. A launch failure — the binary missing from
    PATH, or any OS-level error — raises :class:`~shipit.execrun.ExecError`, which
    the orchestrator renders as the hard-fail ``127`` (never a silent skip).
    Each Exec states :data:`CHECK_TIMEOUT`; a wedged linter dies at that bound
    as a timeout-cause :class:`~shipit.execrun.ExecError` — the same hard-fail.

    Every linter runs under a :func:`_scrubbed_env` passed ``replace_env=True``
    (ADR-0037, #514): this single exec choke point is where the ambient-config
    scrub is applied, so no tool consults ``$HOME``, ``XDG_*``, or the
    explicitly-denylisted per-tool config vars (:func:`_is_ambient_config_var` /
    :data:`_TOOL_CONFIG_ENV_VARS` — ``SHELLCHECK_OPTS``, ``RUFF_CONFIG``,
    ``CARGO_HOME``, ``CLIPPY_CONF_DIR``, ``YAMLLINT_CONFIG_FILE``). It is NOT a
    ``*_CONFIG*`` catch-all — standard build vars like ``PKG_CONFIG_PATH`` are
    preserved. Reuses execrun's existing ``env`` / ``replace_env`` — no new
    plumbing there.
    """
    return execrun.run(
        [binary, *args],
        cwd=str(cwd),
        env=_scrubbed_env(),
        replace_env=True,
        check=False,
        timeout=CHECK_TIMEOUT,
    )


# --------------------------------------------------------------------------
# Orchestrator
# --------------------------------------------------------------------------


def _indent(text: str, prefix: str = "      ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


@cli_errors
def run(
    path: str | None = None,
    *,
    fix: bool = False,
    discover: Callable[[Path], list[str]] | None = None,
    run_tool: Callable[[str, list[str], Path], execrun.ExecResult] | None = None,
    tracks_root_editorconfig: Callable[[Path], bool] | None = None,
    canonical_config: Callable[[Tool, Path], str | None] | None = None,
    runs_out: list[ToolRun] | None = None,
) -> int:
    """Run the checks over the tree at ``path`` (default ``.``). Returns 0/1.

    ``runs_out``, when given, receives every :class:`ToolRun` outcome — the
    typed per-check verdicts behind the 0/1 exit code, for callers that need
    counts rather than a verdict (install self-certification's consumer-debt
    report, ADR-0033) without re-parsing the printed report.

    A malformed ``.shipit.toml`` read for the ``[lint].ignore`` seam raises
    :class:`~shipit.config.ConfigError`, which the shared
    :func:`~shipit.verbs._errors.cli_errors` shell maps to one ``error: …`` line +
    exit 1 — the same clean, legible failure every config-reading verb gives,
    never a raw traceback mid-gate.
    """
    started = time.monotonic()
    root = Path(path or ".").resolve()
    if not root.is_dir():
        print(f"lint: {root} is not a directory", file=sys.stderr)
        logger.error("lint target is not a directory", extra={"root": str(root)})
        return 1

    discover = discover or _discover
    run_tool = run_tool or _run_tool
    tracks_root_ec = tracks_root_editorconfig or _tracks_root_editorconfig
    canonical_config = canonical_config or _canonical_config

    # Drop the consumer's own non-prose paths (`.shipit.toml [lint].ignore`,
    # #484) from the WHOLE file list before routing, so a single glob excludes a
    # path from every leg. `files` also roots the per-manifest runs below, so
    # filtering here keeps an ignored manifest out of those too.
    files = drop_ignored(discover(root), _ignore_globs(root))
    shebangs = {p: _shebang(root / p) for p in files if "." not in _basename(p)}
    routed = route(files, shebangs)
    # Pin the editorconfig-aware tools (shfmt, prettier) to ignore any ambient
    # `.editorconfig` UNLESS the repo tracks its OWN root `.editorconfig` (issue
    # #493) — so the lint verdict is fixed by the commit, not by the checkout path
    # or co-resident tooling that may have written an untracked `.editorconfig` into
    # the tree. The decision reads the repo's TOP-LEVEL tracked list (see
    # `_tracks_root_editorconfig`), NOT the routed `files` (which are
    # `[lint].ignore`-filtered and `path`-scoped), so it can be flipped by neither
    # an ignore glob nor a subdirectory-scoped run.
    pin_editorconfig = not tracks_root_ec(root)

    mode = "fix" if fix else "check"
    print(f"lint: {root} ({mode})")
    if not routed:
        print("  no recognized files — nothing to check.")
        logger.info(
            "lint complete — no recognized files",
            extra={
                "root": str(root),
                "mode": mode,
                "checks": 0,
                "failed": 0,
                "rc": 0,
                "duration_ms": int((time.monotonic() - started) * 1000),
            },
        )
        return 0

    runs: list[ToolRun] = runs_out if runs_out is not None else []
    for lang, paths in routed:
        # per_manifest tools run once per tracked manifest directory. With no
        # manifest tracked they run at the root, where the tool's own error is
        # the (hard) verdict — never a silent skip.
        mdirs = (
            (manifest_roots(files, lang.manifests) or ["."])
            if lang.manifests
            else ["."]
        )
        for tool in lang.tools:
            # The canonical-config path (ADR-0037, #514/#516) is resolved per tool
            # and injected UNCONDITIONALLY by argv. `_canonical_config` returns the
            # shipped `shipit/data` body for each file-config tool (ruff, prettier,
            # markdownlint, yamllint) and None for the inline-config tools, whose
            # placeholder is then omitted.
            config_path = canonical_config(tool, root)
            prefix = tool.argv(
                fix=fix, pin_editorconfig=pin_editorconfig, config_path=config_path
            )
            # Label from the actual argv that ran, so fix mode never claims it
            # ran the check form when it ran the fix form.
            label = f"{tool.binary} {' '.join(prefix)}".strip()
            mutating = fix and tool.fix is not None
            # #500 guard, per-manifest arm: a batch fixer can have protected
            # paths dropped from its argv (the `else` below), but `cargo fmt`
            # takes no file batch — it rewrites a whole crate, reaching a
            # protected `.rs` via a `mod` decl (or a fixture that is itself a
            # crate). So the fixer runs and any protected `.rs` it rewrites is
            # restored byte-for-byte afterward (#502). Snapshot the pre-image
            # here; the restore runs in the `finally` around the batch loop.
            guard_snapshot: dict[str, bytes] | None = None
            if tool.per_manifest:
                if mutating:
                    guard_snapshot = _snapshot(root, protected_testdata(paths))
                # cargo takes NO file batch — 0 files on the argv (it speaks to
                # the crate, not a file list), so the reported count matches what
                # actually ran.
                batches = [(list(prefix), mdir, f"crate {mdir}", 0) for mdir in mdirs]
            else:
                # A batch fixer running its in-place fix form must NEVER rewrite
                # a protected test-data fixture (#500): drop those paths from THIS
                # batch only. The guard is mutation-scoped — a tool running its
                # check form (in either mode) still covers them, so the CI gate
                # reports a genuinely-broken fixture; only the destructive
                # auto-rewrite is refused.
                batch_paths = drop_protected_testdata(paths) if mutating else paths
                if mutating and not batch_paths:
                    # Every file this fixer would touch is protected test-data —
                    # nothing to rewrite, so skip the fix run rather than hand a
                    # fixer an empty batch (some fixers treat "no files" as an
                    # error). Check mode still lints these files.
                    batches = []
                else:
                    count = (
                        f"{len(batch_paths)} file{'s' if len(batch_paths) != 1 else ''}"
                    )
                    batches = [([*prefix, *batch_paths], ".", count, len(batch_paths))]
            try:
                for args, mdir, note, nfiles in batches:
                    # #498: a prettier plugin-load abort (a configured plugin
                    # absent from node_modules) is environment-not-provisioned,
                    # not a formatting verdict — it fails open with a note (set
                    # below). None for every other outcome, so the normal report
                    # path is untouched.
                    fail_open_note: str | None = None
                    try:
                        result = run_tool(tool.binary, args, root / mdir)
                    except execrun.ExecError as exc:
                        # A binary missing from PATH (or any launch failure) is the
                        # HARD-fail signal: 127 + a clear note, never a silent skip.
                        # It propagates (the run's verdict fails), so ERROR + exception.
                        rc = 127
                        if exc.cause == execrun.CAUSE_MISSING_BINARY:
                            out = (
                                f"{tool.binary}: not found on PATH "
                                "(the check is hard — provision it)"
                            )
                        else:
                            out = f"{tool.binary}: could not run: {exc}"
                        logger.error(
                            "lint tool could not run",
                            exc_info=True,
                            extra={
                                "lang": lang.name,
                                "tool": tool.binary,
                                "rc": rc,
                                "cwd": mdir,
                                "batch": note,
                            },
                        )
                    else:
                        rc, out = result.rc, result.stdout + result.stderr
                        if is_prettier_plugin_load_failure(tool.binary, rc, out):
                            # #498: fail OPEN, but only for this one narrowly-matched
                            # module-resolution class (see
                            # is_prettier_plugin_load_failure) — a genuine dirty-JSON
                            # failure has no `imported from` phrase and still FAILS.
                            # Same spirit as the pixi `command -v` guard (#482):
                            # environment-not-provisioned is not a lint verdict. Zero
                            # the rc so the leg passes, and keep the note (plus the
                            # resolver error) so an operator sees WHY it was skipped.
                            fail_open_note = (
                                "prettier: skipped — a plugin named in .prettierrc is "
                                "not installed (module-resolution failure). JSON "
                                "formatting is plugin-independent, so this is "
                                "environment-not-provisioned, not a lint failure; "
                                "provision node_modules to enable this leg.\n"
                                + out.strip()
                            )
                            logger.warning(
                                "lint prettier plugin-load failure — fail open (#498)",
                                extra={
                                    "lang": lang.name,
                                    "tool": tool.binary,
                                    "cwd": mdir,
                                    "batch": note,
                                },
                            )
                            rc, out = 0, ""
                        # Per-tool outcomes are mechanics; the run summary is the milestone.
                        logger.debug(
                            "lint tool finished",
                            extra={
                                "lang": lang.name,
                                "tool": tool.binary,
                                "rc": rc,
                                # The count actually handed to the tool on THIS
                                # batch's argv — post-#500-drop for a batch fixer,
                                # 0 for a per-manifest tool (cargo takes none) —
                                # so the log matches the argv and the printed note.
                                "files": nfiles,
                                "cwd": mdir,
                                "batch": note,
                                "duration_ms": result.duration_ms,
                            },
                        )
                    runs.append(ToolRun(lang.name, tool.binary, label, rc, out))
                    mark = "ok  " if rc == 0 else "FAIL"
                    print(f"  {mark} {lang.name:9} {label} ({note})")
                    if fail_open_note:
                        # A passing (rc==0) leg that was nonetheless skipped — print
                        # the reason so the ok mark is never silently misleading (#498).
                        print(_indent(fail_open_note))
                    elif rc != 0 and out.strip():
                        print(_indent(out.strip()))
            finally:
                # #500/#502: undo any protected `.rs` the per-manifest fixer
                # rewrote. In a `finally` so a fixer that half-rewrites before an
                # error still leaves the fixtures byte-identical. No-op unless a
                # snapshot was taken (mutating per-manifest run) and a file changed.
                if guard_snapshot:
                    restored = _restore(root, guard_snapshot)
                    if restored:
                        logger.debug(
                            "lint restored protected fixtures after fix",
                            extra={
                                "lang": lang.name,
                                "tool": tool.binary,
                                "restored": len(restored),
                            },
                        )

    rc = verdict(runs)
    failed = sorted({f"{r.lang}:{r.binary}" for r in runs if not r.ok})
    if rc == 0:
        print(f"LINT: OK ({len(runs)} checks)")
    else:
        print(f"LINT: FAILED ({', '.join(failed)})")
    # The orchestration summary — one milestone per run, pass or fail: the
    # verdict propagates through the exit code, not an exception.
    summary = {
        "root": str(root),
        "mode": mode,
        "checks": len(runs),
        "failed": len(failed),
        "rc": rc,
        "duration_ms": int((time.monotonic() - started) * 1000),
    }
    if failed:
        # Present only when meaningful — the absent-not-null record contract.
        summary["failed_checks"] = ", ".join(failed)
    logger.info("lint complete", extra=summary)
    return rc
