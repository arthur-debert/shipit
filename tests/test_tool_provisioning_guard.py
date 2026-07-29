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

RUNNER_IMAGE = "runner-image"
SETUP_PIXI = "setup-pixi"
PIXI_MANAGED = "pixi-managed"
SELF_PROVISIONED = "self-provisioned"
CONSUMER_OWNED = "consumer-owned"
CONSUMER_ENV = "consumer-env"
REPO_LOCAL = "repo-local"
DEV_HOST = "dev-host"
OS_PROVIDED = "os"

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

PINNED_SOURCES = frozenset({SETUP_PIXI, PIXI_MANAGED, SELF_PROVISIONED})


@dataclass(frozen=True)
class Provisioned:
    tool: str
    source: str
    pin: str | None = None
    test: str | None = None
    hole: bool = False
    note: str = ""


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
    "gcloud": (
        Provisioned(
            "gcloud",
            DEV_HOST,
            note="Artifact channel store provisioner (ARF01-WS03, "
            "shipit.channel.store_provision) — the operator's own gcloud, an "
            "opt-in infra harness, never a release runner",
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
    "busted": (
        Provisioned(
            "busted",
            CONSUMER_ENV,
            note="the lua toolchain's test-slot runner (TOL03-WS01 #972); a "
            "luarocks package NOT on conda-forge, so like pytest it rides the "
            "test lane in the consumer's own env, never a release stage — no "
            "managed pixi block, no provisions_signal",
        ),
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
            pin="0.69.*",
            test="test_missing_rattler_build_gets_the_reconcile_remedy",
            note="the conda endpoint's packager (ARF01-WS01 #950, ADR-0064): "
            "`rattler-build build`/`publish` repackage a final release binary "
            "into a `.conda` and push+reindex the Artifact channel; rides the "
            "conda-packager block (`pixi.toml#shipit-conda-packager`, gated on "
            "a declared `conda` ENDPOINT — #1071 re-gate off the rust signal so "
            "a non-rust conda producer gets it too), pinned 0.69.* from "
            "conda-forge, seed-validated at 0.69 against the live channel "
            "(#1049 — 0.68.* panicked during the S3 upload)",
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


_RELEASE_SURFACE = (
    "release",
    "tools/registry.py",
    "tools/e2e.py",
)

_NON_ARGV_LITERALS = frozenset(
    {
        "aarch64",
        "appimage",
        "apple-darwin",
        "build",
        "bundle",
        "darwin",
        "deb",
        "description",
        "dispatch",
        "gh-release",
        "homepage/repository",
        "init.lua",
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
        "stage",
        "success",
        "v",
        "windows",
    }
)

_HEAD_SHAPE = re.compile(r"^[a-z][a-z0-9._/-]*$")


def _release_surface_files() -> list[pathlib.Path]:
    files: list[pathlib.Path] = []
    for entry in _RELEASE_SURFACE:
        path = _SRC_ROOT / entry
        if path.is_dir():
            files.extend(sorted(path.rglob("*.py")))
        else:
            files.append(path)
    return files


def _discovered_heads() -> dict[str, list[str]]:
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


def test_every_exec_tool_has_a_provisioning_entry():
    missing = set(_ADAPTER_HOMES) - set(PROVISIONING)
    stale = set(PROVISIONING) - set(_ADAPTER_HOMES)
    assert not missing, (
        "Exec argv tools with no provisioning entry (add a PROVISIONING row "
        f"AND an inventory-doc row, docs/dev/release-tool-provisioning.md): {sorted(missing)}"
    )
    assert not stale, f"provisioning entries for retired Exec tools: {sorted(stale)}"


def test_release_surface_heads_are_all_inventoried():
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
    assert _row("cargo", "cargo-deb").pin == release_bundle.CARGO_DEB_VERSION
    rust_release = _block_toml("pixi-rust-release-deps-block.toml")
    assert _row("cargo", "cargo-edit").pin == rust_release["cargo-edit"]
    assert _row("wasm-pack", "wasm-pack").pin == rust_release["wasm-pack"]
    conda_packager = _block_toml("pixi-conda-packager-block.toml")
    assert _row("rattler-build", "rattler-build").pin == conda_packager["rattler-build"]
    rust_toolchain = _block_toml("pixi-rust-release-toolchain-block.toml")
    assert _row("cargo", "cargo").pin == rust_toolchain["rust"]
    assert (
        _row("wasm-pack", "rust-std-wasm32-unknown-unknown").pin
        == rust_toolchain["rust-std-wasm32-unknown-unknown"]
    )
    assert rust_toolchain["rust-std-wasm32-unknown-unknown"] == rust_toolchain["rust"]
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
    rows = {
        key: sig for key, sig, *_ in (*iunits.TOOLCHAIN_UNITS, *iunits.ENDPOINT_UNITS)
    }
    for head, (_need, block, signal) in provisioning._MANAGED_TOOLS.items():
        assert head in PROVISIONING, head
        assert rows.get(block) == signal, (head, block, signal)


def test_wf_release_family_pixi_pin_agrees_with_registry():
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
            assert not pins, f"{wf.name}: pixi-version without a setup-pixi step"
            continue
        saw_pins = True
        assert pins, f"{wf.name}: setup-pixi without a pixi-version pin"
        assert all(pin == expected for pin in pins), f"{wf.name}: {pins} != {expected}"
    assert saw_pins, "no wf block runs setup-pixi — the sweep matched nothing"


def test_inventory_doc_names_every_tool():
    doc = _INVENTORY_DOC.read_text(encoding="utf-8")
    for head, rows in PROVISIONING.items():
        assert f"`{head}`" in doc, f"inventory doc misses argv head {head!r}"
        for row in rows:
            assert f"`{row.tool}`" in doc, f"inventory doc misses tool {row.tool!r}"


def test_named_fails_when_absent_tests_exist():
    suite = "\n".join(
        p.read_text(encoding="utf-8")
        for p in sorted((_REPO_ROOT / "tests").rglob("test_*.py"))
    )
    for rows in PROVISIONING.values():
        for row in rows:
            if row.test is not None:
                assert f"def {row.test}(" in suite, f"missing test {row.test}"
