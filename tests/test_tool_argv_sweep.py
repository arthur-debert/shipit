"""The mechanized argv-guard sweep: tool argv is built ONLY in its adapter.

ADR-0028's structural rule — "tool argv built outside its Tool adapter is a
statable defect" — enforced as an AST sweep over ``src/shipit``: any list/tuple
literal whose first element is a guarded tool's binary name, in any module
outside that tool's whitelisted assembly point(s), fails the build. The gh-only
sweep that pinned the PROC02 merge (formerly in ``test_gh_adapter.py``) is
generalized here into ONE table-driven test: adding the next tool is a
:data:`_ADAPTER_HOMES` entry, not a new test — plus a provisioning row in
``tests/test_tool_provisioning_guard.py`` (TOL02-WS17 #794: a new Exec tool
cannot land without a provisioning story; that guard fails until the row and
its inventory-doc line exist).

The sweep is deliberately literal-shaped (the same net as the original gh
sweep): it cannot see an argv assembled from a variable head, but every
adapter builds its argv as a literal starting with the binary name, so any
out-of-adapter literal — the copy-paste path a regression would actually take —
is caught mechanically.
"""

from __future__ import annotations

import ast
import functools
import pathlib

import pytest

import shipit

_SRC_ROOT = pathlib.Path(shipit.__file__).parent


@functools.cache
def _parsed(path: pathlib.Path) -> ast.Module:
    """Parse ``path`` once across all parametrized runs — the sweep walks the
    whole package once per guarded tool head, and the source never changes
    mid-suite, so each file is read+parsed at most once."""
    return ast.parse(path.read_text(encoding="utf-8"))


#: The guard table: tool binary name → the module(s) allowed to assemble its
#: argv. One entry per guarded tool; extending the guard to the next tool is a
#: new row here, nothing else.
#:
#: - ``gh``   — the one gh adapter (:mod:`shipit.gh`).
#: - ``git``  — the one git adapter (:mod:`shipit.git`, its ``_argv``).
#: - ``pixi`` — the pixi adapter's two sides: execution
#:   (:mod:`shipit.pixienv.run`, ``run_argv``/``install``) and the read verbs'
#:   literals (:mod:`shipit.pixienv.read`).
#: - ``ps``   — the liveness probe's home (:mod:`shipit.session.liveness`,
#:   ``os_probe``): the OS process table has exactly one reader.
#: - ``curl`` — the lexd release fetch (:mod:`shipit.provision.lexd`): the one
#:   external download shipit performs (ADP00-WS03).
#: - ``cargo`` / ``go`` / ``pytest`` / ``npm`` / ``uv`` / ``tree-sitter`` /
#:   ``busted`` — the Tool verbs' default producing commands (TOL01-WS01/WS02,
#:   ``tree-sitter`` TOL02-WS16, ``busted`` the lua test slot TOL03-WS01):
#:   assembled ONLY in the
#:   closed toolchain registry (:mod:`shipit.tools.registry`); a per-path
#:   ``.shipit.toml`` override is consumer DATA, never a second assembly
#:   point. ``npm`` has a second sanctioned home: the Tree provisioner's
#:   frozen node install (``npm ci``, :mod:`shipit.tree.create` #543) —
#:   provisioning-side, a different concern from the producing dispatch.
#:   ``cargo`` and ``npm`` gain a third: the closed bump-adapter registry
#:   (:mod:`shipit.release.bump`, TOL02-WS01) — the release-side projection
#:   commands (``cargo set-version``/``cargo update``, ``npm version``),
#:   ADR-0041's one assembly point for the manifest bumps. ``cargo``,
#:   ``uv`` and ``npm`` gain the bundle-composition registry
#:   (:mod:`shipit.release.bundle`, TOL02-WS03) — the bundle-side composition
#:   commands (``cargo deb``, ``uv build --out-dir``, ``npm pack`` of the
#:   wasm-pack npm tree, TOL02-WS12 #788).
#: - ``wasm-pack`` — the wasm/npm bundle composition's builder (TOL02-WS12
#:   #788): assembled ONLY in the composition registry
#:   (:mod:`shipit.release.bundle`) — ``wasm-pack build`` the rust crate into
#:   the ``pkg/`` npm tree that ``npm pack`` then tarballs.
#: - ``tar`` / ``zip`` — the archiver invocations of the bundle compositions
#:   (TOL02-WS03): assembled ONLY in the composition registry
#:   (:mod:`shipit.release.bundle`) — the tarball/zip contract and the mac
#:   reseal payload. Both gain the signer unit (:mod:`shipit.release.sign`):
#:   ``tar`` for the archive leg's RE-EMIT (TOL02-WS08 #779 — the reseal
#:   payload's unpack and the archive leg's reopen now read structured
#:   :mod:`tarfile` metadata, no tar subprocess), ``zip`` for the archive
#:   leg's per-binary notary container.
#: - ``codesign`` / ``security`` / ``xcrun`` / ``hdiutil`` — the mac signer
#:   unit's tools (TOL02-WS04): assembled ONLY in
#:   :mod:`shipit.release.sign` — keychain lifecycle, inner-first codesign,
#:   hdiutil reseal, notarytool submit/poll/staple.
#: - ``bin/check-e2e`` — the e2e harness registry's bats default
#:   (TOL01-WS03): the script head is assembled ONLY in the closed harness
#:   registry (:mod:`shipit.tools.e2e`); a declared ``e2e.harness`` argv is
#:   consumer DATA, never a second assembly point.
#: - ``rattler-build`` — the conda endpoint's packager (ARF01-WS01 #950):
#:   ``rattler-build build``/``publish`` assembled ONLY in the closed
#:   endpoint-adapter registry (:mod:`shipit.release.publish`).
_ADAPTER_HOMES: dict[str, tuple[str, ...]] = {
    "gh": ("gh.py",),
    "git": ("git.py",),
    "pixi": ("pixienv/read.py", "pixienv/run.py"),
    "ps": ("session/liveness.py",),
    "curl": ("provision/lexd.py",),
    # gcloud — the Artifact channel store provisioner (ARF01-WS03): the one
    # place that provisions the two access-tier GCS buckets + reader-SA IAM,
    # assembled ONLY in :mod:`shipit.channel.store_provision`. Operator-side
    # infra provisioning (the operator's own gcloud), never a release runner.
    "gcloud": ("channel/store_provision.py",),
    "cargo": (
        "tools/registry.py",
        "release/bump.py",
        "release/bundle.py",
        "release/publish.py",
    ),
    "go": ("tools/registry.py",),
    "pytest": ("tools/registry.py",),
    # busted (TOL03-WS01 #972): the lua toolchain's test-slot runner, assembled
    # ONLY in the closed registry. Like pytest it is a test-lane tool, not a
    # release-stage tool.
    "busted": ("tools/registry.py",),
    # tree-sitter (TOL02-WS16 #792): the generated-parser toolchain's
    # generate/corpus commands, assembled ONLY in the closed registry. The
    # tarball composition's payload is bytes, not a tree-sitter argv — the
    # tar invocation stays under `tar`'s bundle home.
    "tree-sitter": ("tools/registry.py",),
    "npm": (
        "tools/registry.py",
        "tree/create.py",
        "release/bump.py",
        "release/bundle.py",
        "release/publish.py",
    ),
    # wasm-pack: the wasm/npm bundle composition's builder (TOL02-WS12 #788) —
    # assembled ONLY in the closed composition registry, like every other
    # bundle-side tool.
    "wasm-pack": ("release/bundle.py",),
    "uv": ("tools/registry.py", "release/bundle.py"),
    "tar": ("release/bundle.py", "release/sign.py"),
    "zip": ("release/bundle.py", "release/sign.py"),
    "codesign": ("release/sign.py",),
    "security": ("release/sign.py",),
    "xcrun": ("release/sign.py",),
    "hdiutil": ("release/sign.py",),
    # The publish-side endpoint adapters (TOL02-WS05): twine (pypi upload)
    # and ruby (the brew formula's `ruby -c` syntax check) are assembled
    # ONLY in the closed endpoint-adapter registry. `cargo publish` /
    # `npm publish` extend those tools' home lists above.
    "twine": ("release/publish.py",),
    "ruby": ("release/publish.py",),
    # The conda endpoint (ARF01-WS01 #950): rattler-build `build` (repackage
    # a final release binary into a `.conda`) + `publish` (push+reindex the
    # per-repo Artifact channel) are assembled ONLY in the closed
    # endpoint-adapter registry.
    "rattler-build": ("release/publish.py",),
    # The VS Code marketplace path (TOL02-WS13 #789): vsce/ovsx are the
    # consumer's node_modules/.bin devDependencies, never PATH binaries, so
    # they ride `npm exec -- vsce/ovsx ...` — the argv HEAD is `npm` (covered by
    # npm's homes above: `release/bundle.py` for `vsce package`,
    # `release/publish.py` for `vsce/ovsx publish`), so the sweep never sees a
    # bare vsce/ovsx head. These entries still pin the ONLY files that may
    # assemble those tools' argv AND keep the provisioning bijection
    # (test_tool_provisioning_guard) — vsce/ovsx are provisioned tools whether
    # or not they front their own argv.
    "vsce": ("release/bundle.py", "release/publish.py"),
    "ovsx": ("release/publish.py",),
    "bin/check-e2e": ("tools/e2e.py",),
    # The act harness (TOL01-WS04): `shipit wf test` is the one place that
    # drives act, and its docker probes/builds live beside it.
    "act": ("verbs/wf.py",),
    "docker": ("verbs/wf.py",),
}


@pytest.mark.parametrize(
    ("head", "homes"), sorted(_ADAPTER_HOMES.items()), ids=sorted(_ADAPTER_HOMES)
)
def test_no_tool_argv_outside_its_adapter(head: str, homes: tuple[str, ...]):
    """ADR-0028: any list/tuple argv literal starting with ``head`` outside the
    tool's whitelisted assembly point(s) is a review defect — the grep-clean
    criterion, pinned mechanically per tool."""
    allowed = {_SRC_ROOT / home for home in homes}
    offenders = []
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        if path in allowed:
            continue
        for node in ast.walk(_parsed(path)):
            if (
                isinstance(node, (ast.List, ast.Tuple))
                and node.elts
                and isinstance(node.elts[0], ast.Constant)
                and node.elts[0].value == head
            ):
                offenders.append(f"{path.relative_to(_SRC_ROOT.parent)}:{node.lineno}")
    assert not offenders, f"{head} argv built outside its adapter:\n" + "\n".join(
        offenders
    )


def test_every_adapter_home_exists():
    """A renamed/moved adapter must move its table row with it — a whitelist
    entry pointing at nothing would silently guard nothing."""
    for homes in _ADAPTER_HOMES.values():
        for home in homes:
            assert (_SRC_ROOT / home).is_file(), f"missing adapter home: {home}"
