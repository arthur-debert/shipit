"""The tool-provisioning drift guard (TOL02-WS17, #794).

Two rc-killing unprovisioned tools were found one at a time (#784 cargo-deb,
#793 cargo-edit) because nothing tied "shipit shells out to X" to "something
provisions X on the runner". This module is that tie, in four directions:

1. **Whitelist coverage** — every ADR-0028 argv-sweep head
   (:data:`test_tool_argv_sweep._ADAPTER_HOMES`) has an entry in
   :data:`PROVISIONING`, and vice versa. Since the argv sweep is where a new
   Exec tool's assembly home must be declared, a new tool cannot join the
   whitelist without a provisioning story landing here in the same diff.
2. **Head discovery** — an AST sweep over the release-surface modules fails
   on any argv-shaped literal head that is neither inventoried nor
   explicitly declared a non-argv literal (:data:`_NON_ARGV_LITERALS`). This
   is the tripwire for a tool that never even reached the argv sweep's
   table: the WS13–WS16 composition tools (vsce, electron-builder, tauri,
   tree-sitter) land HERE first (WS12's wasm-pack has landed — its row is
   below). Do not allowlist a real tool.
3. **Pin lockstep** — each pinned entry is cross-checked against its one
   authority (``CARGO_DEB_VERSION``, the managed pixi block data files, the
   wf blocks' ``pixi-version``), so the registry cannot claim a pin the code
   no longer carries.
4. **Doc lockstep** — every inventoried tool appears in
   ``docs/dev/release-tool-provisioning.md`` (the human-readable half of this
   registry), and every named fails-when-absent test exists in the suite.
"""

from __future__ import annotations

import ast
import pathlib
import re
import tomllib
from dataclasses import dataclass

from test_tool_argv_sweep import _ADAPTER_HOMES

from shipit.install import units as iunits
from shipit.release import bundle as release_bundle
from shipit.release import provisioning

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SRC_ROOT = _REPO_ROOT / "src" / "shipit"
_INVENTORY_DOC = _REPO_ROOT / "docs" / "dev" / "release-tool-provisioning.md"

# The provisioning source vocabulary — the same closed set the inventory
# document defines (docs/dev/release-tool-provisioning.md).
RUNNER_IMAGE = "runner-image"  # preinstalled on the hosted runner image
SETUP_PIXI = "setup-pixi"  # the blocks' prefix-dev/setup-pixi step
PIXI_MANAGED = "pixi-managed"  # a shipit-managed pixi.toml block
SELF_PROVISIONED = "self-provisioned"  # installed at the Exec seam, pinned
CONSUMER_OWNED = "consumer-owned"  # only the consumer's manifest — a HOLE
CONSUMER_ENV = "consumer-env"  # the consumer's own env, by design (not a hole)
REPO_LOCAL = "repo-local"  # a committed script in the repo itself
DEV_HOST = "dev-host"  # dev/CI-harness host tool, never a release runner
OS_PROVIDED = "os"  # the operating system's own process tooling

SOURCES = frozenset(
    {
        RUNNER_IMAGE,
        SETUP_PIXI,
        PIXI_MANAGED,
        SELF_PROVISIONED,
        CONSUMER_OWNED,
        CONSUMER_ENV,
        REPO_LOCAL,
        DEV_HOST,
        OS_PROVIDED,
    }
)

#: Sources whose story is a version we control — an entry claiming one of
#: these MUST state the pin it is provisioned at.
PINNED_SOURCES = frozenset({SETUP_PIXI, PIXI_MANAGED, SELF_PROVISIONED})


@dataclass(frozen=True)
class Provisioned:
    """One provisioning row: a concrete tool reachable through an argv head.

    ``tool`` is the provisioned thing (``cargo-deb``, not ``cargo`` — several
    distinct tools dispatch through one argv head). ``hole=True`` marks a
    documented gap: the row exists so the gap is STATED, and ``note`` must
    name the follow-up story (the inventory doc's "Open holes" section).
    """

    tool: str
    source: str
    pin: str | None = None
    test: str | None = None  # the fails-when-absent test, when one exists
    hole: bool = False
    note: str = ""


#: The registry: ADR-0028 argv-sweep head → the provisioning rows behind it.
#: THE drift guard table — a new `_ADAPTER_HOMES` head without an entry here
#: fails `test_every_exec_tool_has_a_provisioning_entry`. Keep the inventory
#: document in lockstep (test_inventory_doc_names_every_tool enforces it).
PROVISIONING: dict[str, tuple[Provisioned, ...]] = {
    "gh": (
        Provisioned("gh", RUNNER_IMAGE, note="ambient GITHUB_TOKEN auth (ADR-0028)"),
    ),
    "git": (
        Provisioned("git", RUNNER_IMAGE, note="actions/checkout requires it first"),
    ),
    "pixi": (
        Provisioned(
            "pixi",
            SETUP_PIXI,
            pin="v0.71.0",
            test="test_setup_dev_env_pixi_pin_agrees_with_ci",
            note="every wf block's setup-pixi step; lockstep with Layer 0 PIXI_PIN",
        ),
    ),
    "ps": (Provisioned("ps", OS_PROVIDED, note="session liveness probe, dev-side"),),
    "gcloud": (
        Provisioned(
            "gcloud",
            DEV_HOST,
            note="Artifact channel store provisioner (ARF01-WS03, "
            "shipit.channel.store_provision) — the operator's own gcloud, an "
            "opt-in infra harness, never a release runner",
        ),
    ),
    "curl": (
        Provisioned(
            "curl",
            PIXI_MANAGED,
            pin="*",
            note="lexd release fetch; pinned-open in shipit's own default env",
        ),
    ),
    "cargo": (
        Provisioned(
            "cargo",
            PIXI_MANAGED,
            pin="1.96.*",
            test="test_missing_cargo_binary_gets_the_reconcile_remedy",
            note="hosted images no longer carry Rust; the rust release "
            "toolchain block (`pixi.toml#shipit-rust-release-toolchain`, "
            "its own single-key block so a consumer-side `rust` pin "
            "conflicts alone — #801 closes hole 1) puts cargo in the "
            "default env",
        ),
        Provisioned(
            "cargo-edit",
            PIXI_MANAGED,
            pin="0.13.11.*",
            test="test_missing_cargo_set_version_gets_the_reconcile_remedy",
            note="prepare's rust bump (`cargo set-version`), #793/#797",
        ),
        Provisioned(
            "cargo-deb",
            SELF_PROVISIONED,
            pin=release_bundle.CARGO_DEB_VERSION,
            test="test_deb_self_provisions_cargo_deb_when_missing",
            note="deb composition; not on conda-forge — the #785 exception",
        ),
    ),
    "go": (
        Provisioned(
            "go",
            RUNNER_IMAGE,
            hole=True,
            note="build-stage go floats with the ubuntu image; no fleet go "
            "release consumer yet — open hole 4",
        ),
    ),
    "pytest": (
        Provisioned("pytest", CONSUMER_ENV, note="test lane, never a release stage"),
    ),
    "tree-sitter": (
        Provisioned(
            "tree-sitter-cli",
            PIXI_MANAGED,
            pin="0.25.*",
            test="test_missing_tree_sitter_gets_the_reconcile_remedy",
            note="tree-sitter CLI drives generate/corpus/tarball (#792); "
            "conda-forge DOES carry it (#890 closes hole 7, found on the "
            "first consumer rc's missing-binary death) — the "
            "tree-sitter-release-deps block rides the DECLARED [toolchains] "
            "leg (Toolchain.provisions_signal: no manifest signals a "
            "grammar), pinned in parity with the consumer devDependency "
            "line (^0.25.0 — the generated parser follows the CLI's minor "
            "line, bump both together)",
        ),
    ),
    "npm": (
        Provisioned(
            "nodejs",
            PIXI_MANAGED,
            pin="26.*",
            test="test_missing_npm_gets_the_reconcile_remedy",
            note="npm rides the nodejs package (node-deps block); absent "
            "npm fails loudly naming the reconcile (#801 closes hole 3)",
        ),
        Provisioned("pnpm", PIXI_MANAGED, pin="11.*", note="node-deps block"),
    ),
    "wasm-pack": (
        Provisioned(
            "wasm-pack",
            PIXI_MANAGED,
            pin="0.15.*",
            note="the wasm/npm bundle composition's builder (TOL02-WS12 #788); "
            "rides the rust-release-deps block (rust signal), pinned from "
            "conda-forge; 0.15.* per #846 (conda-forge never carried 0.13)",
        ),
        Provisioned(
            "rust-std-wasm32-unknown-unknown",
            PIXI_MANAGED,
            pin="1.96.*",
            note="the wasm32 target std for the managed rust sysroot (#853): "
            "conda-forge's wasm-pack does NOT pull it (the WS12 claim that "
            "it did was false — its only deps are __glibc/libgcc), so it "
            "rides the rust-release-toolchain block beside `rust`, in "
            "lockstep with it — and is skipped WITH it for a consumer that "
            "owns its own rust pin",
        ),
    ),
    "uv": (
        Provisioned(
            "uv",
            PIXI_MANAGED,
            pin="0.11.*",
            test="test_launcher_deps_uv_pin_agrees_with_layer0_uv_pin",
            note="the bin/shipit launcher's prerequisite on EVERY stage "
            "(launcher-deps block, closes #758) + python `uv build`",
        ),
    ),
    "tar": (
        Provisioned("tar", RUNNER_IMAGE, note="archive composition + sign reseal"),
    ),
    "zip": (
        Provisioned(
            "zip",
            RUNNER_IMAGE,
            hole=True,
            note="absent on windows runners; windows legs out of contract — "
            "open hole 5",
        ),
    ),
    "codesign": (
        Provisioned("codesign", RUNNER_IMAGE, note="macos-* Apple toolchain"),
    ),
    "security": (
        Provisioned("security", RUNNER_IMAGE, note="macos-* Apple toolchain"),
    ),
    "xcrun": (
        Provisioned(
            "xcrun", RUNNER_IMAGE, note="notarytool/stapler ride the Xcode image"
        ),
    ),
    "hdiutil": (Provisioned("hdiutil", RUNNER_IMAGE, note="macos-* dmg reseal"),),
    "twine": (
        Provisioned(
            "twine",
            PIXI_MANAGED,
            pin="6.2.*",
            test="test_missing_twine_gets_the_reconcile_remedy",
            note="the pypi endpoint's uploader — python-signal managed "
            "block (`pixi.toml#shipit-python-release-deps`, #801 closes "
            "hole 2)",
        ),
    ),
    "ruby": (
        Provisioned("ruby", RUNNER_IMAGE, note="brew formula `ruby -c` check only"),
    ),
    "rattler-build": (
        Provisioned(
            "rattler-build",
            PIXI_MANAGED,
            pin="0.68.*",
            test="test_missing_rattler_build_gets_the_reconcile_remedy",
            note="the conda endpoint's packager (ARF01-WS01 #950, ADR-0064): "
            "`rattler-build build`/`publish` repackage a final release binary "
            "into a `.conda` and push+reindex the Artifact channel; rides the "
            "rust-release-deps block (rust signal — the walking-skeleton "
            "producer lex-fmt/lex is rust), pinned 0.68.* from conda-forge, "
            "spike-validated at 0.68.0",
        ),
    ),
    "vsce": (
        Provisioned(
            "vsce",
            CONSUMER_OWNED,
            hole=True,
            note="the VS Code extension repo's @vscode/vsce devDependency "
            "(npm ci → node_modules/.bin), used by the vsix composition "
            "(`vsce package`) and the vscode-marketplace endpoint "
            "(`vsce publish`); no fleet-managed block — the consumer's node "
            "manifest owns it, proven on the consumer rc when ADP02 resumes "
            "(#789, open hole 6)",
        ),
    ),
    "ovsx": (
        Provisioned(
            "ovsx",
            CONSUMER_OWNED,
            hole=True,
            note="the extension repo's ovsx devDependency, used by the "
            "open-vsx endpoint (`ovsx publish`); wired-but-off until the "
            "consumer's OVSX_PAT verifies (#789, open hole 6)",
        ),
    ),
    "bin/check-e2e": (
        Provisioned("bin/check-e2e", REPO_LOCAL, note="committed harness script"),
    ),
    "act": (
        Provisioned(
            "act",
            PIXI_MANAGED,
            pin="0.2.*",
            note="`shipit wf test` harness, shipit's own test feature — dev-only",
        ),
    ),
    "docker": (
        Provisioned(
            "docker", DEV_HOST, note="act's daemon; the wf-test smoke skips loudly"
        ),
    ),
}

# ---------------------------------------------------------------------------
# Head discovery — the "new tool without a provisioning story" tripwire
# ---------------------------------------------------------------------------

#: The release-surface modules whose argv literals the discovery sweep walks:
#: the release verbs' assembly points plus the producing/e2e registries — the
#: places a new release-pipeline tool's argv would land (ADR-0028 whitelist).
_RELEASE_SURFACE = (
    "release",  # every module of the release package
    "tools/registry.py",
    "tools/e2e.py",
)

#: String literals the discovery sweep finds heading a list/tuple in the
#: release surface that are NOT tool invocations (result vocabularies,
#: platform names, template keys, manifest filenames, version prefixes, tauri
#: bundle-format subdir names). Every entry must still be discovered (no stale
#: rows) and must not collide with a PROVISIONING head. A REAL tool never
#: belongs here.
_NON_ARGV_LITERALS = frozenset(
    {
        "aarch64",
        "appimage",  # tauri linux bundle-format subdir (_TAURI_LINUX_FORMATS)
        "apple-darwin",
        "build",
        "bundle",
        "darwin",
        "deb",  # tauri linux bundle-format subdir (_TAURI_LINUX_FORMATS)
        "description",
        "dispatch",
        "extension.toml",  # zed extension manifest — ZED_PAYLOAD core (ADR-0068)
        "gh-release",
        "homepage/repository",
        "license",
        "linux",
        "major",
        "on_arm",
        "on_intel",
        "on_linux",
        "on_macos",
        "package.json",
        "preflight",
        "pyproject.toml",
        "release",
        "rust",
        "scope",
        "src",
        "success",
        "v",
        "windows",
    }
)

#: What a binary-invocation head looks like (lowercase path-ish token, never
#: a flag) — the same literal shape the ADR-0028 sweep guards.
_HEAD_SHAPE = re.compile(r"^[a-z][a-z0-9._/-]*$")


def _release_surface_files() -> list[pathlib.Path]:
    files: list[pathlib.Path] = []
    for entry in _RELEASE_SURFACE:
        path = _SRC_ROOT / entry
        if path.is_dir():
            # rglob, not glob: a future subpackage under release/ must not slip
            # past the discovery tripwire because the sweep stayed top-level.
            files.extend(sorted(path.rglob("*.py")))
        else:
            files.append(path)
    return files


def _discovered_heads() -> dict[str, list[str]]:
    """Every argv-shaped literal head in the release surface → its sites."""
    heads: dict[str, list[str]] = {}
    for path in _release_surface_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, (ast.List, ast.Tuple))
                and node.elts
                and isinstance(node.elts[0], ast.Constant)
                and isinstance(node.elts[0].value, str)
                and _HEAD_SHAPE.match(node.elts[0].value)
            ):
                site = f"{path.relative_to(_REPO_ROOT)}:{node.lineno}"
                heads.setdefault(node.elts[0].value, []).append(site)
    return heads


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------


def test_every_exec_tool_has_a_provisioning_entry():
    # Direction 1: whitelist coverage, both ways. A new `_ADAPTER_HOMES` head
    # (the mandatory landing point for a new Exec tool's argv home) without a
    # provisioning story fails HERE — the #784/#793 failure class, statable.
    missing = set(_ADAPTER_HOMES) - set(PROVISIONING)
    stale = set(PROVISIONING) - set(_ADAPTER_HOMES)
    assert not missing, (
        "Exec argv tools with no provisioning entry (add a PROVISIONING row "
        f"AND an inventory-doc row, docs/dev/release-tool-provisioning.md): {sorted(missing)}"
    )
    assert not stale, f"provisioning entries for retired Exec tools: {sorted(stale)}"


def test_release_surface_heads_are_all_inventoried():
    # Direction 2: the tripwire for a tool that never reached the argv
    # sweep's table — any argv-shaped literal head in the release surface is
    # either a PROVISIONING head or an explicitly declared non-argv literal.
    discovered = _discovered_heads()
    unknown = {
        head: sites
        for head, sites in discovered.items()
        if head not in PROVISIONING and head not in _NON_ARGV_LITERALS
    }
    assert not unknown, (
        "release-surface argv heads with no provisioning story (a real tool "
        "needs a PROVISIONING entry + inventory-doc row; a non-tool literal "
        f"joins _NON_ARGV_LITERALS): {unknown}"
    )
    # The allowlist stays honest: no dead rows, no shadowing a real entry.
    assert not (_NON_ARGV_LITERALS & set(PROVISIONING))
    stale_allowlist = _NON_ARGV_LITERALS - set(discovered)
    assert not stale_allowlist, (
        f"allowlisted literals no longer in the release surface: {sorted(stale_allowlist)}"
    )


def test_sources_are_valid_and_pinned_sources_carry_pins():
    for head, rows in PROVISIONING.items():
        assert rows, f"{head}: empty provisioning entry"
        for row in rows:
            assert row.source in SOURCES, f"{head}/{row.tool}: {row.source!r}"
            if row.source in PINNED_SOURCES:
                assert row.pin, f"{head}/{row.tool}: {row.source} requires a pin"
            if row.hole:
                assert row.note, f"{head}/{row.tool}: a hole must state its story"
                assert row.source in {CONSUMER_OWNED, RUNNER_IMAGE}, (
                    f"{head}/{row.tool}: a provisioned source contradicts hole=True"
                )
            if row.source == CONSUMER_OWNED:
                # The converse: CONSUMER_OWNED is DEFINED as a hole this
                # inventory tracks (see the source vocabulary), so a
                # consumer-owned tool cannot land without its hole=True story.
                assert row.hole, (
                    f"{head}/{row.tool}: CONSUMER_OWNED is a hole — mark hole=True"
                )


def _row(head: str, tool: str) -> Provisioned:
    try:
        return next(r for r in PROVISIONING[head] if r.tool == tool)
    except StopIteration:
        raise AssertionError(
            f"no provisioning row for tool {tool!r} under head {head!r}"
        ) from None


def _block_toml(data_file: str) -> dict:
    return tomllib.loads(iunits.data_bytes(data_file).decode("utf-8"))


def test_pins_agree_with_their_one_authority():
    # Direction 3: the registry cannot claim a pin the code/data no longer
    # carries. Each cross-check names the pin's single home (ADR-0028: one
    # adapter, one pin; the managed blocks: one data file).
    assert _row("cargo", "cargo-deb").pin == release_bundle.CARGO_DEB_VERSION
    rust_release = _block_toml("pixi-rust-release-deps-block.toml")
    assert _row("cargo", "cargo-edit").pin == rust_release["cargo-edit"]
    assert _row("wasm-pack", "wasm-pack").pin == rust_release["wasm-pack"]
    assert _row("rattler-build", "rattler-build").pin == rust_release["rattler-build"]
    rust_toolchain = _block_toml("pixi-rust-release-toolchain-block.toml")
    assert _row("cargo", "cargo").pin == rust_toolchain["rust"]
    assert (
        _row("wasm-pack", "rust-std-wasm32-unknown-unknown").pin
        == rust_toolchain["rust-std-wasm32-unknown-unknown"]
    )
    # The wasm32 std is the managed rust toolchain's OWN sysroot component
    # (#853): it rides the toolchain block and its pin moves in lockstep with
    # the `rust` line, or wasm builds solve a std that misses the delivered
    # sysroot version.
    assert rust_toolchain["rust-std-wasm32-unknown-unknown"] == rust_toolchain["rust"]
    # ...and the two managed rust surfaces (release default-env toolchain,
    # lint-feature toolchain) move in lockstep — one rust, two envs (#801).
    rust_lint = _block_toml("pixi-rust-lint-deps-block.toml")
    assert rust_toolchain["rust"] == rust_lint["rust"]
    python_release = _block_toml("pixi-python-release-deps-block.toml")
    assert _row("twine", "twine").pin == python_release["twine"]
    tree_sitter = _block_toml("pixi-tree-sitter-release-deps-block.toml")
    assert _row("tree-sitter", "tree-sitter-cli").pin == tree_sitter["tree-sitter-cli"]
    launcher = _block_toml("pixi-launcher-deps-block.toml")
    assert _row("uv", "uv").pin == launcher["uv"]
    node = _block_toml("pixi-node-deps-block.toml")
    assert _row("npm", "nodejs").pin == node["nodejs"]
    assert _row("npm", "pnpm").pin == node["pnpm"]


def test_remedy_map_agrees_with_the_managed_units():
    # The #801 missing-tool translation names managed block keys in operator
    # remediation text; each must be a real catalog unit key riding the named
    # toolchain signal, or the remedy sends the operator to a block that the
    # reconcile will never deliver.
    for head, (_need, block, signal) in provisioning._MANAGED_TOOLS.items():
        assert head in PROVISIONING, head
        rows = {key: sig for key, sig, *_ in iunits.TOOLCHAIN_UNITS}
        assert rows.get(block) == signal, (head, block, signal)


def test_wf_release_family_pixi_pin_agrees_with_registry():
    # Every wf block provisions pixi through setup-pixi; all of them (the
    # wf-release family AND wf-checks) must pin the registry's version — the
    # PIXI_PIN lockstep test covers wf-checks, this covers the whole family.
    expected = _row("pixi", "pixi").pin
    wf_files = sorted((_REPO_ROOT / ".github" / "workflows").glob("wf-*.yml"))
    assert wf_files, "no wf blocks found"
    saw_pins = False
    for wf in wf_files:
        text = wf.read_text(encoding="utf-8")
        pins = [
            line.split(":", 1)[1].strip()
            for line in text.splitlines()
            if line.strip().startswith("pixi-version:")
        ]
        if "setup-pixi" not in text:
            # The composer (wf-release.yml) carries only `uses:` lines; every
            # stage's provisioning lives in the block it composes.
            assert not pins, f"{wf.name}: pixi-version without a setup-pixi step"
            continue
        saw_pins = True
        assert pins, f"{wf.name}: setup-pixi without a pixi-version pin"
        assert all(pin == expected for pin in pins), f"{wf.name}: {pins} != {expected}"
    assert saw_pins, "no wf block runs setup-pixi — the sweep matched nothing"


def test_inventory_doc_names_every_tool():
    # Direction 4: the human-readable inventory and this registry move
    # together — every head and every concrete tool is named in the doc, as a
    # backticked token so a short name (`go`, `ps`, `tar`) cannot pass on a
    # stray substring (e.g. head `go` inside the doc's "go/no-go" prose) and
    # mask a genuinely missing row.
    doc = _INVENTORY_DOC.read_text(encoding="utf-8")
    for head, rows in PROVISIONING.items():
        assert f"`{head}`" in doc, f"inventory doc misses argv head {head!r}"
        for row in rows:
            assert f"`{row.tool}`" in doc, f"inventory doc misses tool {row.tool!r}"


def test_named_fails_when_absent_tests_exist():
    # A registry row naming a test that no longer exists is the inventory
    # lying about its coverage — the (c) column of the #794 sweep.
    # rglob, not glob: a future test subdirectory must not make this check
    # silently pass by hiding the file that defines a named absent-test.
    suite = "\n".join(
        p.read_text(encoding="utf-8")
        for p in sorted((_REPO_ROOT / "tests").rglob("test_*.py"))
    )
    for rows in PROVISIONING.values():
        for row in rows:
            if row.test is not None:
                assert f"def {row.test}(" in suite, f"missing test {row.test}"
