"""The managed-set catalog — the :class:`Unit` model and the packaged desired state.

A Unit is one managed thing: a whole file, or a marker-delimited block inside a
consumer-owned file. :func:`load_units` is the catalog, each unit carrying its
desired bytes.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from .. import config
from ..channel import buckets

AGENTS_FILE = "AGENTS.md"
AGENTS_KEY = "AGENTS.md#shipit-block"
BLOCK_OPEN = "<!-- Managed by shipit; do not edit. Regenerate via shipit install. -->"
BLOCK_CLOSE = "<!-- End shipit-managed block. -->"

LEFTHOOK_FILE = "lefthook.yml"

MARKDOWNLINT_FILE = ".markdownlint.yaml"
MARKDOWNLINTIGNORE_FILE = ".markdownlintignore"
YAMLLINT_FILE = ".yamllint.yaml"
PRETTIERRC_FILE = ".prettierrc"
LINT_CONFIG_UNITS = (
    (MARKDOWNLINT_FILE, "markdownlint.yaml"),
    (MARKDOWNLINTIGNORE_FILE, "markdownlintignore"),
    (YAMLLINT_FILE, "yamllint.yaml"),
    (PRETTIERRC_FILE, "prettierrc.yaml"),
)

GITIGNORE_FILE = ".gitignore"
GITIGNORE_KEY = ".gitignore#shipit-release-outputs"
GITIGNORE_OPEN = "# >>> shipit-managed release-output ignores (do not edit; regenerate via `shipit install`) >>>"
GITIGNORE_CLOSE = "# <<< shipit-managed release-output ignores <<<"

PIXI_FILE = "pixi.toml"
PIXI_KEY = "pixi.toml#shipit-tasks"
PIXI_OPEN = (
    "# >>> shipit-managed tasks (do not edit; regenerate via `shipit install`) >>>"
)
PIXI_CLOSE = "# <<< shipit-managed tasks <<<"
PIXI_ANCHOR = "[tasks]"

PIXI_LINT_TASK_KEY = "pixi.toml#shipit-lint-task"
PIXI_LINT_TASK_OPEN = (
    "# >>> shipit-managed lint task (do not edit; regenerate via `shipit install`) >>>"
)
PIXI_LINT_TASK_CLOSE = "# <<< shipit-managed lint task <<<"
PIXI_LINT_TASKS_ANCHOR = "[feature.lint.tasks]"

PIXI_TEST_TASK_KEY = "pixi.toml#shipit-test-task"
PIXI_TEST_TASK_OPEN = (
    "# >>> shipit-managed test task (do not edit; regenerate via `shipit install`) >>>"
)
PIXI_TEST_TASK_CLOSE = "# <<< shipit-managed test task <<<"

PIXI_LINT_DEPS_KEY = "pixi.toml#shipit-lint-deps"
PIXI_LINT_DEPS_OPEN = (
    "# >>> shipit-managed lint deps (do not edit; regenerate via `shipit install`) >>>"
)
PIXI_LINT_DEPS_CLOSE = "# <<< shipit-managed lint deps <<<"
PIXI_LINT_DEPS_ANCHOR = "[feature.lint.dependencies]"
PIXI_ENVS_KEY = "pixi.toml#shipit-environments"
PIXI_ENVS_OPEN = "# >>> shipit-managed environments (do not edit; regenerate via `shipit install`) >>>"
PIXI_ENVS_CLOSE = "# <<< shipit-managed environments <<<"
PIXI_ENVS_ANCHOR = "[environments]"

PIXI_LEXD_KEY = "pixi.toml#shipit-lexd"
PIXI_LEXD_OPEN = "# >>> shipit-managed lexd feature (do not edit; regenerate via `shipit install`) >>>"
PIXI_LEXD_CLOSE = "# <<< shipit-managed lexd feature <<<"
LEXD_PIN = "==0.19.10"

PIXI_LAUNCHER_DEPS_KEY = "pixi.toml#shipit-launcher-deps"
PIXI_LAUNCHER_DEPS_OPEN = "# >>> shipit-managed launcher deps (do not edit; regenerate via `shipit install`) >>>"
PIXI_LAUNCHER_DEPS_CLOSE = "# <<< shipit-managed launcher deps <<<"
PIXI_LAUNCHER_DEPS_ANCHOR = "[dependencies]"

TOOLCHAIN_RUST = "rust"
TOOLCHAIN_GO = "go"
TOOLCHAIN_NODE = "node"
TOOLCHAIN_PYTHON = "python"
TOOLCHAIN_TREE_SITTER = "tree-sitter"
TOOLCHAIN_LUA = "lua"
PIXI_RUST_DEPS_KEY = "pixi.toml#shipit-rust-lint-toolchain"
PIXI_RUST_DEPS_OPEN = "# >>> shipit-managed rust lint toolchain (do not edit; regenerate via `shipit install`) >>>"
PIXI_RUST_DEPS_CLOSE = "# <<< shipit-managed rust lint toolchain <<<"
PIXI_GO_DEPS_KEY = "pixi.toml#shipit-go-lint-toolchain"
PIXI_GO_DEPS_OPEN = "# >>> shipit-managed go lint toolchain (do not edit; regenerate via `shipit install`) >>>"
PIXI_GO_DEPS_CLOSE = "# <<< shipit-managed go lint toolchain <<<"
PIXI_LUA_DEPS_KEY = "pixi.toml#shipit-lua-lint-toolchain"
PIXI_LUA_DEPS_OPEN = "# >>> shipit-managed lua lint toolchain (do not edit; regenerate via `shipit install`) >>>"
PIXI_LUA_DEPS_CLOSE = "# <<< shipit-managed lua lint toolchain <<<"
PIXI_NODE_DEPS_KEY = "pixi.toml#shipit-node-deps"
PIXI_NODE_DEPS_OPEN = (
    "# >>> shipit-managed node deps (do not edit; regenerate via `shipit install`) >>>"
)
PIXI_NODE_DEPS_CLOSE = "# <<< shipit-managed node deps <<<"
PIXI_NODE_DEPS_ANCHOR = "[dependencies]"
PIXI_RUST_RELEASE_DEPS_KEY = "pixi.toml#shipit-rust-release-deps"
PIXI_RUST_RELEASE_DEPS_OPEN = "# >>> shipit-managed rust release deps (do not edit; regenerate via `shipit install`) >>>"
PIXI_RUST_RELEASE_DEPS_CLOSE = "# <<< shipit-managed rust release deps <<<"
PIXI_RUST_RELEASE_TOOLCHAIN_KEY = "pixi.toml#shipit-rust-release-toolchain"
PIXI_RUST_RELEASE_TOOLCHAIN_OPEN = "# >>> shipit-managed rust release toolchain (do not edit; regenerate via `shipit install`) >>>"
PIXI_RUST_RELEASE_TOOLCHAIN_CLOSE = "# <<< shipit-managed rust release toolchain <<<"
PIXI_PYTHON_RELEASE_DEPS_KEY = "pixi.toml#shipit-python-release-deps"
PIXI_PYTHON_RELEASE_DEPS_OPEN = "# >>> shipit-managed python release deps (do not edit; regenerate via `shipit install`) >>>"
PIXI_PYTHON_RELEASE_DEPS_CLOSE = "# <<< shipit-managed python release deps <<<"
PIXI_TREE_SITTER_DEPS_KEY = "pixi.toml#shipit-tree-sitter-release-deps"
PIXI_TREE_SITTER_DEPS_OPEN = "# >>> shipit-managed tree-sitter release deps (do not edit; regenerate via `shipit install`) >>>"
PIXI_TREE_SITTER_DEPS_CLOSE = "# <<< shipit-managed tree-sitter release deps <<<"
PIXI_TREE_SITTER_DEPS_ANCHOR = "[dependencies]"
# Rows of (unit key, toolchain signal, open, close, anchor, packaged data file).
TOOLCHAIN_UNITS = (
    (
        PIXI_RUST_DEPS_KEY,
        TOOLCHAIN_RUST,
        PIXI_RUST_DEPS_OPEN,
        PIXI_RUST_DEPS_CLOSE,
        PIXI_LINT_DEPS_ANCHOR,
        "pixi-rust-lint-deps-block.toml",
    ),
    (
        PIXI_RUST_RELEASE_DEPS_KEY,
        TOOLCHAIN_RUST,
        PIXI_RUST_RELEASE_DEPS_OPEN,
        PIXI_RUST_RELEASE_DEPS_CLOSE,
        PIXI_NODE_DEPS_ANCHOR,
        "pixi-rust-release-deps-block.toml",
    ),
    (
        PIXI_RUST_RELEASE_TOOLCHAIN_KEY,
        TOOLCHAIN_RUST,
        PIXI_RUST_RELEASE_TOOLCHAIN_OPEN,
        PIXI_RUST_RELEASE_TOOLCHAIN_CLOSE,
        PIXI_NODE_DEPS_ANCHOR,
        "pixi-rust-release-toolchain-block.toml",
    ),
    (
        PIXI_PYTHON_RELEASE_DEPS_KEY,
        TOOLCHAIN_PYTHON,
        PIXI_PYTHON_RELEASE_DEPS_OPEN,
        PIXI_PYTHON_RELEASE_DEPS_CLOSE,
        PIXI_NODE_DEPS_ANCHOR,
        "pixi-python-release-deps-block.toml",
    ),
    (
        PIXI_GO_DEPS_KEY,
        TOOLCHAIN_GO,
        PIXI_GO_DEPS_OPEN,
        PIXI_GO_DEPS_CLOSE,
        PIXI_LINT_DEPS_ANCHOR,
        "pixi-go-lint-deps-block.toml",
    ),
    (
        PIXI_LUA_DEPS_KEY,
        TOOLCHAIN_LUA,
        PIXI_LUA_DEPS_OPEN,
        PIXI_LUA_DEPS_CLOSE,
        PIXI_LINT_DEPS_ANCHOR,
        "pixi-lua-lint-deps-block.toml",
    ),
    (
        PIXI_NODE_DEPS_KEY,
        TOOLCHAIN_NODE,
        PIXI_NODE_DEPS_OPEN,
        PIXI_NODE_DEPS_CLOSE,
        PIXI_NODE_DEPS_ANCHOR,
        "pixi-node-deps-block.toml",
    ),
    (
        PIXI_TREE_SITTER_DEPS_KEY,
        TOOLCHAIN_TREE_SITTER,
        PIXI_TREE_SITTER_DEPS_OPEN,
        PIXI_TREE_SITTER_DEPS_CLOSE,
        PIXI_TREE_SITTER_DEPS_ANCHOR,
        "pixi-tree-sitter-release-deps-block.toml",
    ),
)

ENDPOINT_CONDA = "conda"
PIXI_CONDA_PACKAGER_KEY = "pixi.toml#shipit-conda-packager"
PIXI_CONDA_PACKAGER_OPEN = "# >>> shipit-managed conda packager (do not edit; regenerate via `shipit install`) >>>"
PIXI_CONDA_PACKAGER_CLOSE = "# <<< shipit-managed conda packager <<<"
PIXI_CONDA_PACKAGER_ANCHOR = PIXI_NODE_DEPS_ANCHOR  # [dependencies]
# Rows of (unit key, endpoint signal, open, close, anchor, packaged data file).
ENDPOINT_UNITS = (
    (
        PIXI_CONDA_PACKAGER_KEY,
        ENDPOINT_CONDA,
        PIXI_CONDA_PACKAGER_OPEN,
        PIXI_CONDA_PACKAGER_CLOSE,
        PIXI_CONDA_PACKAGER_ANCHOR,
        "pixi-conda-packager-block.toml",
    ),
)

LINT_ENV = "lint"

# The ONE operator-facing "(re)activate the checks" command: there is no
# standalone hook-activation verb, and operator guidance never names the
# internal lefthook/pixi layer under it.
HOOK_RECOVERY_CMD = "./bin/shipit install"

PIXI_SEED_CHANNELS = ("conda-forge",)
PIXI_SEED_PLATFORMS = ("linux-64", "linux-aarch64", "osx-arm64")


def workspace_name(raw: str) -> str:
    """A pixi-safe workspace name from a repo directory name; never empty."""
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-.")
    return name or "workspace"


def pixi_manifest_seed(name: str) -> str:
    """The minimal VALID pixi manifest seeded when a consumer has none — just ``[workspace]``."""
    channels = ", ".join(f'"{c}"' for c in PIXI_SEED_CHANNELS)
    platforms = ", ".join(f'"{p}"' for p in PIXI_SEED_PLATFORMS)
    return (
        "# pixi workspace — seeded by `shipit install` (the managed blocks below\n"
        "# need a valid manifest). Consumer-owned from here on: edit freely.\n"
        "[workspace]\n"
        f'name = "{workspace_name(name)}"\n'
        f"channels = [{channels}]\n"
        f"platforms = [{platforms}]\n"
    )


def lexd_block(platforms: frozenset[str]) -> str:
    """The managed ``[feature.shipit-lexd]`` block, its targets = ``platforms`` ∩ the channel's served set."""
    header = data_bytes("pixi-lexd-block.toml").decode("utf-8").rstrip("\n")
    lines = [header]
    for plat in buckets.SERVED_SUBDIRS:
        if plat in platforms:
            lines.append(f"[feature.shipit-lexd.target.{plat}.dependencies]")
            lines.append(f'lexd = "{LEXD_PIN}"')
    return "\n".join(lines) + "\n"


AGENTS_DEF_DIR = ".claude/agents"
AGENTS_SKILLS_DIR = ".agents/skills"
# `.claude/skills` is a whole-directory SYMLINK install ensures, never content
# units: the target is resolved from the link's own dir (`.claude/`).
CLAUDE_SKILLS_DIR = ".claude/skills"
CLAUDE_SKILLS_LINK_TARGET = "../.agents/skills"
AGY_AGENTS_DEF_DIR = ".agents/agents"
SETTINGS_FILE = ".claude/settings.json"
SETTINGS_KEY = ".claude/settings.json#shipit-pretooluse-hook"
SETTINGS_HOOK_MARKER = "shipit hook pretooluse"

# The substring EVERY shipit-managed hook command carries; the retired-hooks
# pass uses it to avoid removing shipit's own entries.
MANAGED_HOOK_COMMAND_MARKER = "shipit hook"

SETTINGS_STOP_KEY = ".claude/settings.json#shipit-stop-hook"
SETTINGS_STOP_MARKER = "shipit hook stop"
SETTINGS_SUBAGENTSTOP_KEY = ".claude/settings.json#shipit-subagentstop-hook"
SETTINGS_SUBAGENTSTOP_MARKER = "shipit hook subagent-stop"

SHIPIT_LAUNCHER_FILE = "bin/shipit"

SETUP_DEV_ENV_FILE = "bin/setup-dev-env.sh"

AGENT_LAUNCHER_FILE = "agent-start"

SETTINGS_SESSIONSTART_KEY = ".claude/settings.json#shipit-sessionstart-hook"
SETTINGS_SESSIONSTART_MARKER = "shipit hook sessionstart"

SETTINGS_WORKTREECREATE_KEY = ".claude/settings.json#shipit-worktreecreate-hook"
SETTINGS_WORKTREECREATE_MARKER = "shipit hook worktreecreate"

CODEX_CONFIG_FILE = ".codex/config.toml"
CODEX_HOOKS_FILE = ".codex/hooks.json"
CODEX_SESSIONSTART_KEY = ".codex/hooks.json#shipit-sessionstart-hook"
CODEX_PRETOOLUSE_KEY = ".codex/hooks.json#shipit-pretooluse-hook"

EVENT_PRETOOLUSE = "PreToolUse"
EVENT_STOP = "Stop"
EVENT_SUBAGENTSTOP = "SubagentStop"
EVENT_SESSIONSTART = "SessionStart"
EVENT_WORKTREECREATE = "WorktreeCreate"

FMT_MARKERS = "markers"
FMT_JSON_HOOK = "json-hook"
FMT_ENV_MEMBER = "env-member"


def env_member_token(env: str, required: Sequence[str]) -> str:
    """The canonical membership token an ``FMT_ENV_MEMBER`` unit hashes on — independent of the env's other features."""
    return json.dumps(
        {"environment": env, "requires": sorted(required)}, sort_keys=True
    )


@dataclass(frozen=True)
class Unit:
    """One managed unit; ``content`` is the desired bytes — a whole file, or a block's inner text."""

    key: str
    dest: str
    kind: str
    content: bytes
    executable: bool = False
    open_marker: str = BLOCK_OPEN
    close_marker: str = BLOCK_CLOSE
    anchor: str | None = None
    fmt: str = FMT_MARKERS
    event: str = EVENT_PRETOOLUSE
    marker: str = SETTINGS_HOOK_MARKER
    env_name: str | None = None
    required_features: tuple[str, ...] = ()

    def desired_inner(self) -> str:
        """A block unit's canonical inner text (newline-trimmed)."""
        return self.content.decode("utf-8").strip("\n")

    def desired_hash(self) -> str:
        """The ``sha256:`` pristine hash of this unit's desired content."""
        if self.fmt == FMT_ENV_MEMBER:
            token = env_member_token(self.env_name or "", self.required_features)
            return config.content_hash(token.encode("utf-8"))
        if self.kind == "block":
            return config.content_hash(self.desired_inner().encode("utf-8"))
        return config.content_hash(self.content)


def data_bytes(*parts: str) -> bytes:
    """Read a ``shipit.data`` file via the resources Traversable API."""
    return resources.files("shipit.data").joinpath(*parts).read_bytes()


def skills_root():
    """The bundled skills store Traversable — a read SOURCE only, never a shipped consumer dest."""
    return resources.files("shipit.data").joinpath("skills")


def agents_root():
    """The bundled subagent agent-defs — wheel package data, or the repo root in dev."""
    bundled = resources.files("shipit.data").joinpath("agents")
    if bundled.is_dir():
        return bundled
    return Path(__file__).resolve().parents[3] / ".claude" / "agents"


def agy_agents_root():
    """The bundled AGY custom-agent defs — wheel package data, or the repo root in dev."""
    bundled = resources.files("shipit.data").joinpath("agy-agents")
    if bundled.is_dir():
        return bundled
    return Path(__file__).resolve().parents[3] / ".agents" / "agents"


def canonical_hook_entry(entry: dict) -> str:
    """The stable serialization of a hooks-event entry, so a unit's hash compares structure, not formatting."""
    return json.dumps(entry, indent=2, sort_keys=True)


def walk_files(node, prefix: str = ""):
    """Yield ``(relpath, bytes)`` for every file under ``node``, depth-first sorted."""
    for child in sorted(node.iterdir(), key=lambda p: p.name):
        rel = f"{prefix}{child.name}"
        if child.is_dir():
            yield from walk_files(child, prefix=f"{rel}/")
        elif child.is_file():
            yield rel, child.read_bytes()


def load_units(
    *,
    toolchains: frozenset[str] = frozenset(),
    endpoints: frozenset[str] = frozenset(),
    platforms: frozenset[str] = frozenset(PIXI_SEED_PLATFORMS),
) -> list[Unit]:
    """The managed set, in a stable order (skills, the AGENTS block, then bootstrap).

    ``toolchains`` and ``endpoints`` gate the conditional pixi dep blocks;
    ``platforms`` scopes the lexd block's targets to what the workspace declares.
    """
    units: list[Unit] = []

    for rel, content in walk_files(skills_root()):
        units.append(
            Unit(
                key=f"{AGENTS_SKILLS_DIR}/{rel}",
                dest=f"{AGENTS_SKILLS_DIR}/{rel}",
                kind="file",
                content=content,
            )
        )

    units.append(
        Unit(
            key=AGENTS_KEY,
            dest=AGENTS_FILE,
            kind="block",
            content=data_bytes("agents-block.md"),
        )
    )

    units.append(
        Unit(
            key=GITIGNORE_KEY,
            dest=GITIGNORE_FILE,
            kind="block",
            content=data_bytes("gitignore-block"),
            open_marker=GITIGNORE_OPEN,
            close_marker=GITIGNORE_CLOSE,
        )
    )

    units.append(
        Unit(
            key=SHIPIT_LAUNCHER_FILE,
            dest=SHIPIT_LAUNCHER_FILE,
            kind="file",
            content=data_bytes("bootstrap", "shipit"),
            executable=True,
        )
    )

    units.append(
        Unit(
            key=SETUP_DEV_ENV_FILE,
            dest=SETUP_DEV_ENV_FILE,
            kind="file",
            content=data_bytes("bootstrap", "setup-dev-env.sh"),
            executable=True,
        )
    )

    units.append(
        Unit(
            key=AGENT_LAUNCHER_FILE,
            dest=AGENT_LAUNCHER_FILE,
            kind="file",
            content=data_bytes("bootstrap", "agent-start"),
            executable=True,
        )
    )

    units.append(
        Unit(
            key=LEFTHOOK_FILE,
            dest=LEFTHOOK_FILE,
            kind="file",
            content=data_bytes("lefthook.yml"),
        )
    )
    for dest, data_file in LINT_CONFIG_UNITS:
        units.append(
            Unit(key=dest, dest=dest, kind="file", content=data_bytes(data_file))
        )
    units.append(
        Unit(
            key=PIXI_KEY,
            dest=PIXI_FILE,
            kind="block",
            content=data_bytes("pixi-tasks-block.toml"),
            open_marker=PIXI_OPEN,
            close_marker=PIXI_CLOSE,
            anchor=PIXI_ANCHOR,
        )
    )
    units.append(
        Unit(
            key=PIXI_TEST_TASK_KEY,
            dest=PIXI_FILE,
            kind="block",
            content=data_bytes("pixi-test-task-block.toml"),
            open_marker=PIXI_TEST_TASK_OPEN,
            close_marker=PIXI_TEST_TASK_CLOSE,
            anchor=PIXI_ANCHOR,
        )
    )
    units.append(
        Unit(
            key=PIXI_LINT_TASK_KEY,
            dest=PIXI_FILE,
            kind="block",
            content=data_bytes("pixi-lint-task-block.toml"),
            open_marker=PIXI_LINT_TASK_OPEN,
            close_marker=PIXI_LINT_TASK_CLOSE,
            anchor=PIXI_LINT_TASKS_ANCHOR,
        )
    )

    units.append(
        Unit(
            key=PIXI_LINT_DEPS_KEY,
            dest=PIXI_FILE,
            kind="block",
            content=data_bytes("pixi-lint-deps-block.toml"),
            open_marker=PIXI_LINT_DEPS_OPEN,
            close_marker=PIXI_LINT_DEPS_CLOSE,
            anchor=PIXI_LINT_DEPS_ANCHOR,
        )
    )
    units.append(
        Unit(
            key=PIXI_ENVS_KEY,
            dest=PIXI_FILE,
            kind="block",
            content=data_bytes("pixi-lint-env-block.toml"),
            open_marker=PIXI_ENVS_OPEN,
            close_marker=PIXI_ENVS_CLOSE,
            anchor=PIXI_ENVS_ANCHOR,
            fmt=FMT_ENV_MEMBER,
            env_name="lint",
            required_features=("shipit-lexd",),
        )
    )

    units.append(
        Unit(
            key=PIXI_LEXD_KEY,
            dest=PIXI_FILE,
            kind="block",
            content=lexd_block(platforms).encode("utf-8"),
            open_marker=PIXI_LEXD_OPEN,
            close_marker=PIXI_LEXD_CLOSE,
            anchor=None,
        )
    )

    units.append(
        Unit(
            key=PIXI_LAUNCHER_DEPS_KEY,
            dest=PIXI_FILE,
            kind="block",
            content=data_bytes("pixi-launcher-deps-block.toml"),
            open_marker=PIXI_LAUNCHER_DEPS_OPEN,
            close_marker=PIXI_LAUNCHER_DEPS_CLOSE,
            anchor=PIXI_LAUNCHER_DEPS_ANCHOR,
        )
    )

    for key, signal, open_marker, close_marker, anchor, data_file in TOOLCHAIN_UNITS:
        if signal in toolchains:
            units.append(
                Unit(
                    key=key,
                    dest=PIXI_FILE,
                    kind="block",
                    content=data_bytes(data_file),
                    open_marker=open_marker,
                    close_marker=close_marker,
                    anchor=anchor,
                )
            )

    for key, signal, open_marker, close_marker, anchor, data_file in ENDPOINT_UNITS:
        if signal in endpoints:
            units.append(
                Unit(
                    key=key,
                    dest=PIXI_FILE,
                    kind="block",
                    content=data_bytes(data_file),
                    open_marker=open_marker,
                    close_marker=close_marker,
                    anchor=anchor,
                )
            )

    for rel, content in walk_files(agents_root()):
        units.append(
            Unit(
                key=f"{AGENTS_DEF_DIR}/{rel}",
                dest=f"{AGENTS_DEF_DIR}/{rel}",
                kind="file",
                content=content,
            )
        )

    agy_root = agy_agents_root()
    if agy_root.is_dir():
        for rel, content in walk_files(agy_root):
            units.append(
                Unit(
                    key=f"{AGY_AGENTS_DEF_DIR}/{rel}",
                    dest=f"{AGY_AGENTS_DEF_DIR}/{rel}",
                    kind="file",
                    content=content,
                )
            )

    for key, marker, event, data_file in (
        (
            SETTINGS_KEY,
            SETTINGS_HOOK_MARKER,
            EVENT_PRETOOLUSE,
            "claude-settings-pretooluse.json",
        ),
        (
            SETTINGS_STOP_KEY,
            SETTINGS_STOP_MARKER,
            EVENT_STOP,
            "claude-settings-stop.json",
        ),
        (
            SETTINGS_SUBAGENTSTOP_KEY,
            SETTINGS_SUBAGENTSTOP_MARKER,
            EVENT_SUBAGENTSTOP,
            "claude-settings-subagentstop.json",
        ),
        (
            SETTINGS_SESSIONSTART_KEY,
            SETTINGS_SESSIONSTART_MARKER,
            EVENT_SESSIONSTART,
            "claude-settings-sessionstart.json",
        ),
        (
            SETTINGS_WORKTREECREATE_KEY,
            SETTINGS_WORKTREECREATE_MARKER,
            EVENT_WORKTREECREATE,
            "claude-settings-worktreecreate.json",
        ),
    ):
        hook_entry = json.loads(data_bytes(data_file))
        units.append(
            Unit(
                key=key,
                dest=SETTINGS_FILE,
                kind="block",
                content=canonical_hook_entry(hook_entry).encode("utf-8"),
                fmt=FMT_JSON_HOOK,
                event=event,
                marker=marker,
            )
        )

    units.append(
        Unit(
            key=CODEX_CONFIG_FILE,
            dest=CODEX_CONFIG_FILE,
            kind="file",
            content=data_bytes("codex-config.toml"),
        )
    )
    for key, marker, event, data_file in (
        (
            CODEX_PRETOOLUSE_KEY,
            SETTINGS_HOOK_MARKER,
            EVENT_PRETOOLUSE,
            "codex-hooks-pretooluse.json",
        ),
        (
            CODEX_SESSIONSTART_KEY,
            SETTINGS_SESSIONSTART_MARKER,
            EVENT_SESSIONSTART,
            "codex-hooks-sessionstart.json",
        ),
    ):
        hook_entry = json.loads(data_bytes(data_file))
        units.append(
            Unit(
                key=key,
                dest=CODEX_HOOKS_FILE,
                kind="block",
                content=canonical_hook_entry(hook_entry).encode("utf-8"),
                fmt=FMT_JSON_HOOK,
                event=event,
                marker=marker,
            )
        )
    return units
