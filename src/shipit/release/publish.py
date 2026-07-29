"""The closed endpoint-adapter registry: staged Artifacts -> Distribution endpoints.

One adapter per name of :data:`shipit.config.ENDPOINTS`; :func:`plan` decides what fires.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import config, execrun
from ..changelog import SEMVER_RE
from ..channel import buckets
from . import ReleaseError, secretreq
from . import brew as brew_mod
from . import bundle as bundle_mod
from . import integrity as integrity_mod
from .bundle import VSCE_TARGETS
from .version import RELEASE_RC_PRE

STAGE_RESULTS: tuple[str, ...] = ("success", "failure", "cancelled", "skipped")

RESULT_SUCCESS = "success"
RESULT_SKIPPED = "skipped"

HOMEBREW_TAP = "arthur-debert/homebrew-tools"

#: The tap clone is fresh and hookless, so the formula commit states an identity.
TAP_COMMITTER = ("shipit release", "shipit-release@users.noreply.github.com")

TESTPYPI_URL = "https://test.pypi.org/legacy/"

#: The GitHub SECRET NAME each adapter looks its token up under. gh-release
#: declares none: ``gh`` rides its ambient auth, never a synced secret.
CRATES_SECRET = secretreq.ENDPOINT_SECRETS["crates"][0]
PYPI_SECRET = secretreq.ENDPOINT_SECRETS["pypi"][0]
TESTPYPI_SECRET = secretreq.TESTPYPI_SECRET
NPM_SECRET = secretreq.ENDPOINT_SECRETS["npm"][0]
VSCE_SECRET = secretreq.ENDPOINT_SECRETS["vscode-marketplace"][0]
OVSX_SECRET = secretreq.ENDPOINT_SECRETS["open-vsx"][0]
TAP_SECRET = secretreq.ENDPOINT_SECRETS["brew"][0]
NOTIFY_SECRET = secretreq.ENDPOINT_SECRETS["notify-downstreams"][0]
CONDA_KEY_ID_SECRET = secretreq.ENDPOINT_SECRETS["conda"][0]
CONDA_SECRET_KEY_SECRET = secretreq.ENDPOINT_SECRETS["conda"][1]

#: The one ``repository_dispatch`` type downstream repos filter the cascade on.
NOTIFY_EVENT_TYPE = "upstream-release"

#: The CLOSED release-triple -> conda-subdir map. A triple with no entry
#: (osx-64, musl) is UNSERVED: the conda endpoint skips its archive.
CONDA_SUBDIRS: dict[str, str] = {
    "aarch64-apple-darwin": "osx-arm64",
    "x86_64-unknown-linux-gnu": "linux-64",
    "aarch64-unknown-linux-gnu": "linux-aarch64",
    "x86_64-pc-windows-msvc": "win-64",
}

#: The Artifact channel buckets; the per-repo channel root is ``<bucket>/<owner/name>``.
PUBLIC_ARTIFACT_BUCKET = buckets.PUBLIC_ARTIFACT_BUCKET
PRIVATE_ARTIFACT_BUCKET = buckets.PRIVATE_ARTIFACT_BUCKET

NOARCH_SUBDIR = buckets.NOARCH_SUBDIR

#: ``region = "auto"`` and the global endpoint host are load-bearing for GCS interop.
CONDA_S3_ENDPOINT = buckets.CHANNEL_HOST
CONDA_S3_REGION = "auto"

#: rattler-build resolves S3 config through the AWS SDK credential chain: the
#: ``S3_*`` names its ``--help`` suggests are IGNORED and it dies "Could not
#: determine region from AWS SDK configuration".
CONDA_S3_ENDPOINT_ENV = "AWS_ENDPOINT_URL"
CONDA_S3_REGION_ENV = "AWS_REGION"
CONDA_S3_KEY_ID_ENV = "AWS_ACCESS_KEY_ID"
CONDA_S3_SECRET_KEY_ENV = "AWS_SECRET_ACCESS_KEY"

#: Scratch subdirs under the staged assets tree, namespaced per artifact
#: (``<scratch>/<artifact>/…``) — never top-level, so gh-release cannot ship them.
CONDA_RECIPE_SCRATCH = "conda-recipe"
CONDA_CHANNEL_SCRATCH = "conda-channel"

#: The child-process env var each tool READS its token under — distinct from the
#: secret NAME above.
CARGO_TOKEN_ENV = "CARGO_REGISTRY_TOKEN"
NPM_AUTH_ENV = "NODE_AUTH_TOKEN"

VSCE_PAT_ENV = "VSCE_PAT"
OVSX_PAT_ENV = "OVSX_PAT"

VSIX_TARGET_STRINGS: frozenset[str] = frozenset(VSCE_TARGETS.values())

#: cargo's already-published stderr signatures (lowercased match).
CRATE_ALREADY_PUBLISHED_MARKERS: tuple[str, ...] = (
    "already uploaded",
    "already exists",
)

#: npm's publish-over-existing stderr signatures (lowercased match).
NPM_ALREADY_PUBLISHED_MARKERS: tuple[str, ...] = (
    "previously published",
    "cannot publish over",
)

#: vsce/ovsx's already-published stderr signatures (lowercased match).
VSIX_ALREADY_PUBLISHED_MARKERS: tuple[str, ...] = (
    "already exists",
    "already published",
    "is already published",
)

#: ``RunCmd`` raises on a nonzero rc; ``Probe`` returns it for the adapter to
#: classify. A non-``None`` ``env`` is MERGED over the process environment.
RunCmd = Callable[[Sequence[str], Path, Mapping[str, str] | None], execrun.ExecResult]
Probe = Callable[[Sequence[str], Path, Mapping[str, str] | None], execrun.ExecResult]


@dataclass(frozen=True)
class PublishRequest:
    """Everything one endpoint dispatch needs: the artifact and its release context."""

    artifact: config.Artifact
    entries: tuple[config.ToolchainEntry, ...]
    root: Path
    assets_dir: Path
    version: str
    tag: str
    prerelease: bool
    notes_path: Path
    env: Mapping[str, str]
    run_cmd: RunCmd
    probe: Probe
    ghio: Any
    gitio: Any
    repo: str | None = None
    testpypi: bool = False


@dataclass(frozen=True)
class Published:
    """One completed endpoint dispatch, as short human-readable action lines."""

    artifact: str
    endpoint: str
    actions: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "artifact": self.artifact,
            "endpoint": self.endpoint,
            "actions": list(self.actions),
        }


def build_is_live(matrix: str) -> bool:
    """Whether the plan's build stage is live; malformed ``matrix`` JSON raises."""
    try:
        entries = json.loads(matrix)
    except json.JSONDecodeError as exc:
        raise ReleaseError(
            f"--matrix is not valid JSON ({exc}) — pass the preflight plan's "
            f"`matrix` field verbatim (wf-prepare's output)"
        ) from exc
    if not isinstance(entries, list):
        raise ReleaseError(
            "--matrix must be the preflight plan's `matrix` JSON array, "
            f"got {type(entries).__name__}"
        )
    return bool(entries)


def bundle_is_live(stages: str) -> bool:
    """Whether the plan's bundle stage is live; malformed ``stages`` JSON raises."""
    try:
        names_ = json.loads(stages)
    except json.JSONDecodeError as exc:
        raise ReleaseError(
            f"--stages is not valid JSON ({exc}) — pass the preflight plan's "
            f"`stages` field verbatim (wf-prepare's output)"
        ) from exc
    if not isinstance(names_, list):
        raise ReleaseError(
            "--stages must be the preflight plan's `stages` JSON array, "
            f"got {type(names_).__name__}"
        )
    return "bundle" in names_


def check_gate(
    build: str,
    bundle: str,
    sign: str,
    *,
    build_live: bool = True,
    bundle_live: bool = True,
) -> None:
    """Refuse unless every live stage succeeded (sign: success-or-skipped either way)."""
    blockers = []
    for stage, result, live in (
        ("build", build, build_live),
        ("bundle", bundle, bundle_live),
    ):
        if live:
            if result != RESULT_SUCCESS:
                blockers.append(f"{stage}={result} (live {stage} requires success)")
        elif result not in (RESULT_SUCCESS, RESULT_SKIPPED):
            blockers.append(
                f"{stage}={result} (success-or-skipped required for a non-live {stage})"
            )
    if sign not in (RESULT_SUCCESS, RESULT_SKIPPED):
        blockers.append(f"sign={sign} (success-or-skipped required)")
    if blockers:
        raise ReleaseError(
            "publish refused — upstream stage results block the release: "
            + ", ".join(blockers)
            + " (a live build/bundle must be success, a plan-proven non-live "
            "one success-or-skipped, sign success-or-skipped; never ship a "
            "half-built set — workflows.lex §3.3)"
        )


def is_live_fire(version: str) -> bool:
    """Whether ``version`` is a ``-release-rc`` live-fire cut. Pure."""
    match = SEMVER_RE.match(version)
    pre = match.group("pre") if match else None
    return pre is not None and (
        pre == RELEASE_RC_PRE or pre.startswith(f"{RELEASE_RC_PRE}.")
    )


#: A skip verdict's reason strings — data, so the plan renders without dispatching.
SKIP_RC_GUARD = "rc-guard: -release-rc publishes to the GH release only"
SKIP_STABLE_ONLY = "stable-channel only: a prerelease never moves the tap formula"
SKIP_NOTIFY_PRERELEASE = (
    "notify-downstreams fires on real releases only: a prerelease notifies no one"
)
SKIP_ZED_PRERELEASE = (
    "the zed extensions registry serves stable versions: a prerelease renders "
    "no registry entry"
)
SKIP_SELECTOR = "--endpoint selector: this run publishes only the selected endpoints"

#: The endpoint ``--endpoint`` can never deselect: gh-release IS the Release.
RELEASE_ENDPOINT = "gh-release"


@dataclass(frozen=True)
class Dispatch:
    """One planned (artifact, endpoint) pair: dispatch it, or skip it with a stated reason."""

    artifact: config.Artifact
    adapter: EndpointAdapter
    skip: str | None = None


def _check_selector(
    selector: Sequence[str], artifacts: Sequence[config.Artifact]
) -> None:
    """Refuse an unknown, undeclared, or gh-release-deselecting ``--endpoint`` selector."""
    declared = {name for artifact in artifacts for name in artifact.endpoints}
    unknown = [name for name in selector if adapter_for(name) is None]
    if unknown:
        raise ReleaseError(
            "publish refused — `--endpoint` names unknown endpoint(s) "
            + ", ".join(f"`{name}`" for name in unknown)
            + f"; known endpoints: {', '.join(names())}"
        )
    undeclared = [name for name in selector if name not in declared]
    if undeclared:
        raise ReleaseError(
            "publish refused — `--endpoint` selects "
            + ", ".join(f"`{name}`" for name in undeclared)
            + ", which no artifact in this repo declares: nothing would "
            "publish under "
            + ("that endpoint" if len(undeclared) == 1 else "those endpoints")
            + ". Declared here: "
            + (", ".join(sorted(declared)) if declared else "(none)")
        )
    if RELEASE_ENDPOINT in declared and RELEASE_ENDPOINT not in selector:
        raise ReleaseError(
            "publish refused — `--endpoint` cannot deselect `gh-release`: it "
            "is the Release itself, not a distribution channel. The selector "
            "narrows which registries publish; the Release that lands always "
            "carries every declared artifact's assets (ADR-0009 — a partial "
            f"release is structurally impossible). Add `--endpoint "
            f"{RELEASE_ENDPOINT}` to the selection."
        )


def plan(
    artifacts: Sequence[config.Artifact],
    *,
    prerelease: bool,
    live_fire: bool,
    selector: Sequence[str] | None = None,
) -> tuple[Dispatch, ...]:
    """The ordered dispatch plan: every ``release`` endpoint before any ``derived`` one."""
    if selector is not None:
        _check_selector(selector, artifacts)
    dispatches: list[Dispatch] = []
    for stage in ("release", "derived"):
        for artifact in artifacts:
            for name in artifact.endpoints:
                adapter = adapter_for(name)
                if adapter is None:
                    known = ", ".join(names())
                    raise ReleaseError(
                        f"[artifacts.{artifact.name}] names unknown endpoint "
                        f"`{name}`; known endpoints: {known}"
                    )
                if adapter.stage != stage:
                    continue
                skip = None
                # Guards first: both reasons hold for a non-selected external
                # endpoint on an rc cut, and the stronger one must be reported.
                if live_fire and adapter.external:
                    skip = SKIP_RC_GUARD
                elif prerelease and adapter.stable_only:
                    skip = adapter.stable_skip_reason
                elif selector is not None and name not in selector:
                    skip = SKIP_SELECTOR
                dispatches.append(Dispatch(artifact, adapter, skip))
    live = [d.adapter.name for d in dispatches if d.skip is None]
    if "brew" in live and "gh-release" not in live:
        raise ReleaseError(
            "publish plan invalid — a brew endpoint renders a formula pointing "
            "at gh-release assets (`releases/download/<tag>/…`), but no unskipped "
            "gh-release endpoint is planned: declare `gh-release` so the release "
            "the formula targets is created and its assets uploaded (both "
            "endpoints are idempotent — a resume converges, nothing is duplicated)"
        )
    if "notify-downstreams" in live and "gh-release" not in live:
        raise ReleaseError(
            "publish plan invalid — notify-downstreams tells the downstream "
            "repos to rebuild against this release, but no unskipped gh-release "
            "endpoint is planned: declare `gh-release` so the release the "
            "downstreams target lands on GitHub before they are notified (both "
            "endpoints are idempotent — a resume converges, nothing is duplicated)"
        )
    # conda is deliberately NOT bound to gh-release: it packages the staged build
    # output directly, so a conda-only plan is valid.
    return tuple(dispatches)


def required_env_keys(adapter: EndpointAdapter, *, testpypi: bool) -> tuple[str, ...]:
    """The token env keys this run of ``adapter`` needs (testpypi swaps pypi's)."""
    if adapter.name == "pypi" and testpypi:
        return (TESTPYPI_SECRET,)
    return adapter.secrets


def missing_secrets(
    dispatches: Sequence[Dispatch],
    env: Mapping[str, str],
    *,
    testpypi: bool,
) -> tuple[tuple[str, str], ...]:
    """The ``(endpoint, env key)`` pairs absent from ``env``, over unskipped dispatches."""
    missing: list[tuple[str, str]] = []
    for dispatch in dispatches:
        if dispatch.skip is not None:
            continue
        for key in required_env_keys(dispatch.adapter, testpypi=testpypi):
            pair = (dispatch.adapter.name, key)
            if not env.get(key) and pair not in missing:
                missing.append(pair)
    return tuple(missing)


def _leg_for(
    artifact: config.Artifact,
    entries: Sequence[config.ToolchainEntry],
    toolchain: str,
    endpoint: str,
) -> config.ToolchainEntry:
    """The first ``[toolchains]`` leg of ``toolchain``, or a refusal naming the endpoint."""
    leg = next((entry for entry in entries if entry.toolchain == toolchain), None)
    if leg is None:
        raise ReleaseError(
            f"[artifacts.{artifact.name}] {endpoint} endpoint needs a "
            f"[toolchains] {toolchain} leg, and none is mapped"
        )
    return leg


def _leg_dir(root: Path, leg: config.ToolchainEntry) -> Path:
    """The leg's absolute directory (``"."`` -> repo root)."""
    return root if leg.path in (".", "") else root / leg.path


def _asset_names(assets_dir: Path) -> tuple[str, ...]:
    """The regular non-hidden files directly under ``assets_dir``, sorted."""
    if not assets_dir.is_dir():
        return ()
    return tuple(
        sorted(
            p.name
            for p in assets_dir.iterdir()
            if p.is_file() and not p.name.startswith(".")
        )
    )


def release_assets(assets_dir: Path) -> tuple[str, ...]:
    """The asset names gh-release ships — everything but the mac reseal payload."""
    return tuple(
        name
        for name in _asset_names(assets_dir)
        if not name.endswith(".unsigned-app.tar.gz")
    )


def _require_token(req: PublishRequest, endpoint: str, key: str) -> str:
    """``req.env[key]``, or the loud missing-token refusal."""
    token = req.env.get(key)
    if not token:
        raise ReleaseError(
            f"[artifacts.{req.artifact.name}] {endpoint}: required token "
            f"{key} is not set — provision it (gh-setup derives the needed "
            f"set from the declared endpoints), never skip silently"
        )
    return token


def _tail(text: str, limit: int = 2000) -> str:
    """The last ``limit`` characters of ``text``, stripped."""
    return text.strip()[-limit:]


def _publish_gh_release(req: PublishRequest) -> Published:
    """Create-or-edit the GH Release from the notes text, then upload the staged assets."""
    if not req.notes_path.is_file():
        raise ReleaseError(
            f"[artifacts.{req.artifact.name}] gh-release: no notes file at "
            f"{req.notes_path} — `shipit release prepare` writes the one "
            f"coalesced notes text (story 26); pass --notes to point at it"
        )
    cwd = str(req.root)
    kind = "prerelease" if req.prerelease else "release"
    actions = []
    if req.ghio.release_exists(req.tag, cwd=cwd):
        req.ghio.release_edit(
            req.tag,
            notes_file=str(req.notes_path),
            prerelease=req.prerelease,
            cwd=cwd,
        )
        actions.append(f"updated {kind} {req.tag} (prerelease flag re-asserted)")
    else:
        req.ghio.release_create(
            req.tag,
            notes_file=str(req.notes_path),
            prerelease=req.prerelease,
            cwd=cwd,
        )
        actions.append(f"created {kind} {req.tag}")
    assets = release_assets(req.assets_dir)
    if assets:
        req.ghio.release_upload(
            req.tag, [str(req.assets_dir / name) for name in assets], cwd=cwd
        )
        actions.append(f"uploaded {len(assets)} asset(s): {', '.join(assets)}")
    return Published(req.artifact.name, "gh-release", tuple(actions))


def crates_publish_order(metadata: dict) -> tuple[str, ...]:
    """Workspace crate names in dependency order, ``publish = false`` members excluded."""
    id_to_name = {
        pkg.get("id"): pkg.get("name")
        for pkg in metadata.get("packages", [])
        if pkg.get("publish") != []
    }
    member_names = {
        id_to_name[member]
        for member in metadata.get("workspace_members", [])
        if member in id_to_name
    }
    deps: dict[str, set[str]] = {}
    for pkg in metadata.get("packages", []):
        name = pkg.get("name")
        if name not in member_names:
            continue
        deps[name] = {
            dep.get("name")
            for dep in pkg.get("dependencies", [])
            if dep.get("kind") != "dev"
            and dep.get("name") in member_names
            and dep.get("name") != name
        }
    order: list[str] = []
    remaining = dict(deps)
    while remaining:
        ready = sorted(
            name for name, needs in remaining.items() if not (needs & remaining.keys())
        )
        if not ready:
            raise ReleaseError(
                "crates: dependency cycle among workspace crates: "
                + ", ".join(sorted(remaining))
            )
        for name in ready:
            order.append(name)
            del remaining[name]
    return tuple(order)


def crate_already_published(stderr: str) -> bool:
    lowered = stderr.lower()
    return any(marker in lowered for marker in CRATE_ALREADY_PUBLISHED_MARKERS)


def _publish_crates(req: PublishRequest) -> Published:
    """Publish the workspace crates in dependency order; already-uploaded is success."""
    leg = _leg_for(req.artifact, req.entries, "rust", "crates")
    leg_dir = _leg_dir(req.root, leg)
    token = _require_token(req, "crates", CRATES_SECRET)
    metadata = req.run_cmd(
        ["cargo", "metadata", "--format-version", "1", "--no-deps"], leg_dir, None
    )
    order = crates_publish_order(json.loads(metadata.stdout))
    if not order:
        raise ReleaseError(
            f"[artifacts.{req.artifact.name}] crates: `cargo metadata` names "
            f"no workspace members under {leg_dir}"
        )
    actions = []
    for crate in order:
        result = req.probe(
            ["cargo", "publish", "-p", crate], leg_dir, {CARGO_TOKEN_ENV: token}
        )
        if result.rc == 0:
            actions.append(f"{crate} {req.version} published")
        elif crate_already_published(result.stderr):
            actions.append(f"{crate} {req.version} already published — resumed")
        else:
            raise ReleaseError(
                f"[artifacts.{req.artifact.name}] crates: `cargo publish -p "
                f"{crate}` failed:\n{_tail(result.stderr)}"
            )
    return Published(req.artifact.name, "crates", tuple(actions))


def _canonical_dist(name: str) -> str:
    """The PEP 503 key: runs of ``-``/``_``/``.`` collapse to one ``_``, and case folds."""
    return re.sub(r"[-_.]+", "_", name).lower()


def pypi_uploads(names: Sequence[str], dist: str) -> tuple[str, ...]:
    """The staged wheels of distribution ``dist`` plus each wheel's matching sdist."""
    want = _canonical_dist(dist)
    sdists = sorted(n for n in names if n.endswith(".tar.gz"))
    files: list[str] = []
    for wheel in sorted(n for n in names if n.endswith(".whl")):
        parts = wheel.split("-")
        if len(parts) < 2 or _canonical_dist(parts[0]) != want:
            continue
        files.append(wheel)
        version = parts[1]
        for sdist in sdists:
            cand_dist, _sep, cand_version = sdist[: -len(".tar.gz")].rpartition("-")
            if (
                cand_version == version
                and _canonical_dist(cand_dist) == want
                and sdist not in files
            ):
                files.append(sdist)
                break
    return tuple(files)


def _pypi_dist_name(req: PublishRequest) -> str:
    """The python leg's ``pyproject.toml`` ``[project].name`` — what the upload is scoped to."""
    leg = _leg_for(req.artifact, req.entries, "python", "pypi")
    pyproject = _leg_dir(req.root, leg) / "pyproject.toml"
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ReleaseError(
            f"[artifacts.{req.artifact.name}] pypi: cannot read {pyproject} "
            f"to scope the upload to this artifact's distribution: {exc}"
        ) from exc
    project = data.get("project") if isinstance(data, dict) else None
    name = project.get("name") if isinstance(project, dict) else None
    if not name:
        raise ReleaseError(
            f"[artifacts.{req.artifact.name}] pypi: {pyproject} has no "
            f"[project].name — publish scopes the upload by distribution name, "
            f"never ships the whole bundle tree to the index"
        )
    return str(name)


def _publish_pypi(req: PublishRequest) -> Published:
    """Twine upload of this artifact's staged wheel+sdist, scoped to its distribution."""
    key = TESTPYPI_SECRET if req.testpypi else PYPI_SECRET
    token = _require_token(req, "pypi", key)
    dist = _pypi_dist_name(req)
    files = pypi_uploads(_asset_names(req.assets_dir), dist)
    if not files:
        raise ReleaseError(
            f"[artifacts.{req.artifact.name}] pypi: no wheel for distribution "
            f"`{dist}` under {req.assets_dir} — the bundle stage's wheel "
            f"composition produces it; run `shipit release bundle` first"
        )
    argv = ["twine", "upload", "--non-interactive", "--skip-existing"]
    if req.testpypi:
        argv += ["--repository-url", TESTPYPI_URL]
    argv += [str(req.assets_dir / name) for name in files]
    req.run_cmd(
        argv, req.root, {"TWINE_USERNAME": "__token__", "TWINE_PASSWORD": token}
    )
    where = "testpypi" if req.testpypi else "pypi"
    return Published(
        req.artifact.name,
        "pypi",
        (f"uploaded to {where}: {', '.join(files)}",),
    )


def npm_already_published(stderr: str) -> bool:
    lowered = stderr.lower()
    return any(marker in lowered for marker in NPM_ALREADY_PUBLISHED_MARKERS)


def npm_tarball_name(pkg_name: str, version: str) -> str:
    """The ``npm pack`` filename: package name flattened (``@`` dropped, ``/`` -> ``-``)."""
    stem = pkg_name.lstrip("@").replace("/", "-")
    return f"{stem}-{version}.tgz"


def _publish_npm(req: PublishRequest) -> Published:
    """Publish the staged npm tarball — the wasm-pack composition's artifact, no rebuild."""
    token = _require_token(req, "npm", NPM_SECRET)
    pkg_name = integrity_mod.expected_main_binary(req.artifact)
    tarball = npm_tarball_name(pkg_name, req.version)
    path = req.assets_dir / tarball
    if not path.is_file():
        raise ReleaseError(
            f"[artifacts.{req.artifact.name}] npm: no tarball `{tarball}` for "
            f"package `{pkg_name}` under {req.assets_dir} — the wasm-pack bundle "
            f"composition produces it; run `shipit release bundle` first"
        )
    result = req.probe(
        ["npm", "publish", str(path), "--ignore-scripts"],
        req.root,
        {NPM_AUTH_ENV: token},
    )
    if result.rc == 0:
        action = f"published {pkg_name} {req.version} ({tarball})"
    elif npm_already_published(result.stderr):
        action = f"{pkg_name} {req.version} already published — resumed"
    else:
        raise ReleaseError(
            f"[artifacts.{req.artifact.name}] npm: `npm publish` failed:\n"
            f"{_tail(result.stderr)}"
        )
    return Published(req.artifact.name, "npm", (action,))


def vsix_uploads(names: Sequence[str], artifact: str) -> tuple[str, ...]:
    """This artifact's staged ``<artifact>-<vsce-target>.vsix`` files, sorted."""
    prefix = f"{artifact}-"
    return tuple(
        sorted(
            n
            for n in names
            if n.endswith(".vsix")
            and n.startswith(prefix)
            and n[len(prefix) : -len(".vsix")] in VSIX_TARGET_STRINGS
        )
    )


def vsix_already_published(stderr: str) -> bool:
    lowered = stderr.lower()
    return any(marker in lowered for marker in VSIX_ALREADY_PUBLISHED_MARKERS)


def _publish_vsix_marketplace(
    req: PublishRequest,
    endpoint: str,
    argv_head: Sequence[str],
    secret: str,
    token_env: str,
) -> Published:
    """Publish this artifact's staged ``.vsix`` files via ``argv_head``, from the npm leg dir."""
    token = _require_token(req, endpoint, secret)
    leg = _leg_for(req.artifact, req.entries, "npm", endpoint)
    pkg_dir = _leg_dir(req.root, leg)
    vsixes = vsix_uploads(_asset_names(req.assets_dir), req.artifact.name)
    if not vsixes:
        raise ReleaseError(
            f"[artifacts.{req.artifact.name}] {endpoint}: no .vsix under "
            f"{req.assets_dir} — the vsix composition produces the per-target "
            f"packages these endpoints publish; run `shipit release bundle` first"
        )
    actions = []
    for vsix in vsixes:
        result = req.probe(
            [*argv_head, str(req.assets_dir / vsix)], pkg_dir, {token_env: token}
        )
        if result.rc == 0:
            actions.append(f"published {vsix}")
        elif vsix_already_published(result.stderr):
            actions.append(f"{vsix} already published — resumed")
        else:
            raise ReleaseError(
                f"[artifacts.{req.artifact.name}] {endpoint}: publishing {vsix} "
                f"failed:\n{_tail(result.stderr)}"
            )
    return Published(req.artifact.name, endpoint, tuple(actions))


def _publish_vscode_marketplace(req: PublishRequest) -> Published:
    """``npm exec -- vsce publish --packagePath`` of this artifact's staged ``.vsix`` files."""
    return _publish_vsix_marketplace(
        req,
        "vscode-marketplace",
        ["npm", "exec", "--", "vsce", "publish", "--packagePath"],
        VSCE_SECRET,
        VSCE_PAT_ENV,
    )


def _publish_open_vsx(req: PublishRequest) -> Published:
    """``npm exec -- ovsx publish`` of this artifact's staged ``.vsix`` files."""
    return _publish_vsix_marketplace(
        req,
        "open-vsx",
        ["npm", "exec", "--", "ovsx", "publish"],
        OVSX_SECRET,
        OVSX_PAT_ENV,
    )


def brew_archives(artifact_name: str, names: Sequence[str]) -> dict[str, str]:
    """``{target triple: archive name}`` for the artifact's staged mac/linux tarballs."""
    prefix = f"{artifact_name}-"
    archives: dict[str, str] = {}
    for name in sorted(names):
        if not (name.startswith(prefix) and name.endswith(".tar.gz")):
            continue
        triple = name[len(prefix) : -len(".tar.gz")]
        if "apple-darwin" in triple or "linux" in triple:
            archives[triple] = name
    return archives


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _publish_brew(req: PublishRequest) -> Published:
    """Render, syntax-check, and push the tap formula; an unchanged formula pushes nothing."""
    if req.repo is None:
        raise ReleaseError(
            f"[artifacts.{req.artifact.name}] brew: no source repo resolved — "
            f"the formula's asset URLs point at "
            f"github.com/<owner/name>/releases/…, so an unresolved repo is a "
            f"hard error"
        )
    token = _require_token(req, "brew", TAP_SECRET)
    archives = brew_archives(req.artifact.name, _asset_names(req.assets_dir))
    if not archives:
        raise ReleaseError(
            f"[artifacts.{req.artifact.name}] brew: no "
            f"{req.artifact.name}-<triple>.tar.gz archives under "
            f"{req.assets_dir} — the archive composition produces the "
            f"release assets the formula points at"
        )
    targets = {
        triple: (
            f"https://github.com/{req.repo}/releases/download/{req.tag}/{name}",
            _sha256(req.assets_dir / name),
        )
        for triple, name in archives.items()
    }
    leg = _leg_for(req.artifact, req.entries, "rust", "brew")
    metadata = req.run_cmd(
        ["cargo", "metadata", "--format-version", "1", "--no-deps"],
        _leg_dir(req.root, leg),
        None,
    )
    desc, homepage, license_ = brew_mod.metadata_for(
        json.loads(metadata.stdout), req.artifact
    )
    binary = integrity_mod.expected_main_binary(req.artifact)
    text = brew_mod.render(
        binary=binary,
        version=req.version,
        desc=desc,
        homepage=homepage,
        license_=license_,
        targets=targets,
        private=bool(req.ghio.repo_is_private(req.repo)),
    )
    # A scratch SUBDIR, never a top-level file: a gh-release re-run would ship it.
    formula_rel = f"Formula/{binary}.rb"
    scratch = req.assets_dir / "brew"
    scratch.mkdir(parents=True, exist_ok=True)
    rendered = scratch / f"{binary}.rb"
    rendered.write_text(text, encoding="utf-8", newline="\n")
    req.run_cmd(["ruby", "-c", str(rendered)], req.root, None)
    actions = [f"rendered {formula_rel} ({', '.join(sorted(targets))})"]
    with tempfile.TemporaryDirectory(prefix="shipit-brew-tap-") as tmp:
        tap_dir = Path(tmp) / "tap"
        req.gitio.clone(
            f"https://x-access-token:{token}@github.com/{HOMEBREW_TAP}.git",
            str(tap_dir),
        )
        dest = tap_dir / formula_rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8", newline="\n")
        tap_cwd = str(tap_dir)
        if not req.gitio.status_porcelain(cwd=tap_cwd):
            actions.append(f"{HOMEBREW_TAP} unchanged — nothing to push")
        else:
            branch = req.gitio.current_branch(cwd=tap_cwd)
            if branch is None:  # pragma: no cover — a fresh clone has a branch
                raise ReleaseError(f"brew: tap clone at {tap_dir} has no branch")
            name, email = TAP_COMMITTER
            req.gitio.configure_identity(name, email, cwd=tap_cwd)
            req.gitio.add([formula_rel], cwd=tap_cwd)
            req.gitio.commit(f"{binary} {req.version}", [formula_rel], cwd=tap_cwd)
            req.gitio.push(branch, cwd=tap_cwd)
            actions.append(f"pushed {formula_rel} to {HOMEBREW_TAP}")
    return Published(req.artifact.name, "brew", tuple(actions))


def _publish_notify_downstreams(req: PublishRequest) -> Published:
    """Fire one ``repository_dispatch`` at each declared downstream repo."""
    if req.repo is None:
        raise ReleaseError(
            f"[artifacts.{req.artifact.name}] notify-downstreams: no source "
            f"repo resolved — the dispatch payload names the upstream "
            f"`owner/name` the downstreams rebuild against, so an unresolved "
            f"repo is a hard error, never a null payload"
        )
    token = _require_token(req, "notify-downstreams", NOTIFY_SECRET)
    if not req.artifact.downstreams:
        raise ReleaseError(
            f"[artifacts.{req.artifact.name}] notify-downstreams: no "
            f"`downstreams` declared — the endpoint fires repository_dispatch "
            f"at the artifact's downstream repos, and there are none"
        )
    payload = {
        "repo": req.repo,
        "tag": req.tag,
        "version": req.version,
        "artifact": req.artifact.name,
    }
    actions = []
    for slug in req.artifact.downstreams:
        req.ghio.repository_dispatch(
            slug, event_type=NOTIFY_EVENT_TYPE, payload=payload, token=token
        )
        actions.append(f"dispatched {NOTIFY_EVENT_TYPE} to {slug}")
    return Published(req.artifact.name, "notify-downstreams", tuple(actions))


#: The conda package-name vocabulary.
_CONDA_PACKAGE_NAME_RE = re.compile(r"[a-z0-9._-]+")

#: The one noarch-eligible composition whose single archive is an npm ``.tgz``
#: rather than the ``<artifact>.tar.gz`` the others stage.
NOARCH_WASM_COMPOSITION = bundle_mod.WASM_PACK.name

#: A data artifact has no binary: its payload installs under
#: ``$PREFIX/share/<package>/``, namespaced so two noarch artifacts never collide.
CONDA_NOARCH_INSTALL_DIR = "share"


def conda_subdir(triple: str) -> str | None:
    """The conda subdir for a release target ``triple``, or ``None`` when unserved."""
    return CONDA_SUBDIRS.get(triple)


def conda_assets(
    artifact: config.Artifact, staged: Sequence[str]
) -> dict[str, tuple[str, str]]:
    """``{subdir: (triple, asset)}`` for declared platforms that are served and staged."""
    from . import preflight  # lazy — avoid a publish<->preflight import cycle

    present = set(staged)
    assets: dict[str, tuple[str, str]] = {}
    for platform in artifact.platforms or (preflight.DEFAULT_PLATFORM,):
        spec = preflight.PLATFORM_MATRIX[platform]
        subdir = conda_subdir(spec.target)
        if subdir is None:
            continue
        name = f"{artifact.name}-{spec.target}{spec.ext_archive}"
        if name in present:
            assets[subdir] = (spec.target, name)
    return assets


def conda_served_subdirs(artifacts: Sequence[config.Artifact]) -> tuple[str, ...]:
    """The served conda subdirs this repo's conda producer actually publishes."""
    from . import preflight  # lazy — avoid a publish<->preflight import cycle

    found: set[str] = set()
    for artifact in artifacts:
        if "conda" not in artifact.endpoints or not artifact.build:
            continue
        for platform in artifact.platforms or (preflight.DEFAULT_PLATFORM,):
            subdir = conda_subdir(preflight.PLATFORM_MATRIX[platform].target)
            if subdir is not None:
                found.add(subdir)
    return tuple(s for s in buckets.SERVED_SUBDIRS if s in found)


def conda_package_name(artifact: config.Artifact) -> str:
    """The conda package name — the artifact's main-binary name, lowercased and validated."""
    name = integrity_mod.expected_main_binary(artifact).lower()
    if not _CONDA_PACKAGE_NAME_RE.fullmatch(name):
        raise ReleaseError(
            f"[artifacts.{artifact.name}] conda: derived package name `{name}` "
            f"is not a valid conda package name (lowercase letters, digits, "
            f"`.`, `_`, `-` only) — set `main-binary` to a conda-safe name or "
            f"drop the conda endpoint. A scoped wasm-pack identity (`@scope/x`) "
            f"or a spaced `product-name` cannot name a conda package."
        )
    return name


def _conda_binary_layout(subdir: str, binary: str) -> tuple[str, str, str]:
    """``(source_filename, install_dir, install_filename)`` for a ``subdir`` package's binary."""
    if subdir == "win-64":
        return f"{binary}.exe", "Scripts", f"{binary}.exe"
    return binary, "bin", binary


def render_conda_recipe(
    *,
    package: str,
    version: str,
    archive_path: str,
    source_binary: str,
    install_dir: str,
    install_binary: str,
) -> str:
    """The ``rattler-build`` recipe.yaml repackaging one prebuilt binary into a ``.conda``."""
    return (
        f"package:\n"
        f"  name: {package}\n"
        f'  version: "{version}"\n'
        f"\n"
        f"source:\n"
        f"  - path: {json.dumps(archive_path)}\n"
        f"\n"
        f"build:\n"
        f"  number: 0\n"
        f"  dynamic_linking:\n"
        f"    # prebuilt+signed binary: no relink (needs a per-OS toolchain the\n"
        f"    # single runner lacks, and rewriting would break the signature)\n"
        f"    binary_relocation: false\n"
        f"  script:\n"
        f'    - mkdir -p "${{PREFIX}}/{install_dir}"\n'
        f'    - cp "{source_binary}" "${{PREFIX}}/{install_dir}/{install_binary}"\n'
    )


def conda_noarch_eligible(artifact: config.Artifact) -> bool:
    """Whether the artifact's bundle composition is ``platform_independent``."""
    bundle = artifact.bundle
    if bundle is None:
        return False
    composition = bundle_mod.composition(bundle.composition)
    return composition is not None and composition.platform_independent


def conda_noarch_asset_name(artifact: config.Artifact, version: str) -> str:
    """The known staged name of a noarch artifact's one platform-independent archive."""
    bundle = artifact.bundle
    if bundle is not None and bundle.composition == NOARCH_WASM_COMPOSITION:
        return npm_tarball_name(integrity_mod.expected_main_binary(artifact), version)
    return f"{artifact.name}.tar.gz"


def conda_noarch_asset(
    artifact: config.Artifact, version: str, names: Sequence[str]
) -> str | None:
    """That archive's name when staged, else ``None``."""
    want = conda_noarch_asset_name(artifact, version)
    return want if want in names else None


def conda_noarch_package_name(artifact: config.Artifact) -> str:
    """The noarch conda package name — the derived identity flattened and validated."""
    raw = integrity_mod.expected_main_binary(artifact)
    name = raw.lstrip("@").replace("/", "-").lower()
    if not _CONDA_PACKAGE_NAME_RE.fullmatch(name):
        raise ReleaseError(
            f"[artifacts.{artifact.name}] conda (noarch): derived package name "
            f"`{name}` is not a valid conda package name (lowercase letters, "
            f"digits, `.`, `_`, `-` only) — set `main-binary` to a conda-safe "
            f"name or drop the conda endpoint. A spaced `product-name` cannot "
            f"name a conda package."
        )
    return name


def render_conda_noarch_recipe(
    *,
    package: str,
    version: str,
    archive_path: str,
    install_dir: str,
) -> str:
    """The ``rattler-build`` recipe.yaml repackaging one archive as ``noarch: generic``."""
    return (
        f"package:\n"
        f"  name: {package}\n"
        f'  version: "{version}"\n'
        f"\n"
        f"source:\n"
        f"  - path: {json.dumps(archive_path)}\n"
        f"    target_directory: payload\n"
        f"\n"
        f"build:\n"
        f"  number: 0\n"
        f"  noarch: generic\n"
        f"  script:\n"
        f'    - mkdir -p "${{PREFIX}}/{install_dir}/{package}"\n'
        f'    - cp -R payload/. "${{PREFIX}}/{install_dir}/{package}"\n'
    )


def _conda_channel_env(key_id: str, secret_key: str) -> dict[str, str]:
    """The rattler-build S3 child env: fixed endpoint/region plus the write HMAC pair."""
    return {
        CONDA_S3_ENDPOINT_ENV: CONDA_S3_ENDPOINT,
        CONDA_S3_REGION_ENV: CONDA_S3_REGION,
        CONDA_S3_KEY_ID_ENV: key_id,
        CONDA_S3_SECRET_KEY_ENV: secret_key,
    }


def _conda_channel_url(req: PublishRequest) -> str:
    """``s3://<bucket>/<repo>``, the bucket derived from the repo's visibility."""
    private = bool(req.ghio.repo_is_private(req.repo))
    bucket = PRIVATE_ARTIFACT_BUCKET if private else PUBLIC_ARTIFACT_BUCKET
    return f"s3://{bucket}/{req.repo}"


def _publish_conda_noarch(
    req: PublishRequest, *, key_id: str, secret_key: str
) -> Published:
    """Build the one ``noarch: generic`` ``.conda`` and publish+reindex it to ``noarch/``."""
    package = conda_noarch_package_name(req.artifact)
    asset_name = conda_noarch_asset(
        req.artifact, req.version, release_assets(req.assets_dir)
    )
    if asset_name is None:
        want = conda_noarch_asset_name(req.artifact, req.version)
        raise ReleaseError(
            f"[artifacts.{req.artifact.name}] conda (noarch): no `{want}` under "
            f"{req.assets_dir} — the `{req.artifact.bundle.composition}` "
            f"composition stages the one platform-independent archive the noarch "
            f"mode repackages; run `shipit release bundle` first"
        )
    # `assets_dir` is stage-wide, so an un-namespaced channel tree would let a
    # second conda artifact's post-build glob pick up this one's `.conda`.
    recipe_dir = (
        req.assets_dir / CONDA_RECIPE_SCRATCH / req.artifact.name / NOARCH_SUBDIR
    )
    channel_dir = req.assets_dir / CONDA_CHANNEL_SCRATCH / req.artifact.name
    recipe_dir.mkdir(parents=True, exist_ok=True)
    recipe = recipe_dir / "recipe.yaml"
    recipe.write_text(
        render_conda_noarch_recipe(
            package=package,
            version=req.version,
            archive_path=(req.assets_dir / asset_name).as_posix(),
            install_dir=CONDA_NOARCH_INSTALL_DIR,
        ),
        encoding="utf-8",
        newline="\n",
    )
    req.run_cmd(
        [
            "rattler-build",
            "build",
            "--recipe",
            str(recipe),
            # NO `--target-platform noarch`: rattler-build refuses it ("that should
            # be defined in the recipe"); `build.noarch: generic` routes it.
            "--output-dir",
            str(channel_dir),
            "--package-format",
            "conda",
            "--no-build-id",
            "--test",
            "native",
        ],
        req.root,
        None,
    )
    built = sorted((channel_dir / NOARCH_SUBDIR).glob("*.conda"))
    if not built:  # pragma: no cover — a successful build always writes a .conda
        raise ReleaseError(
            f"[artifacts.{req.artifact.name}] conda (noarch): rattler-build "
            f"produced no `.conda` under {channel_dir / NOARCH_SUBDIR} — the "
            f"build recorded success but emitted no package"
        )
    channel_url = _conda_channel_url(req)
    req.run_cmd(
        [
            "rattler-build",
            "publish",
            "--to",
            channel_url,
            "--force",
            *[str(path) for path in built],
        ],
        req.root,
        _conda_channel_env(key_id, secret_key),
    )
    return Published(
        req.artifact.name,
        "conda",
        (
            f"built {len(built)} noarch package(s) from {asset_name}",
            f"published {len(built)} package(s) to {channel_url}/noarch (+ reindex)",
        ),
    )


def _publish_conda(req: PublishRequest) -> Published:
    """Repackage the staged build-output archives into ``.conda`` packages and publish them."""
    if req.repo is None:
        raise ReleaseError(
            f"[artifacts.{req.artifact.name}] conda: no source repo resolved — "
            f"the per-repo channel root is `<bucket>/<owner/name>`, so an "
            f"unresolved repo is a hard error, never a mis-rooted channel write"
        )
    key_id = _require_token(req, "conda", CONDA_KEY_ID_SECRET)
    secret_key = _require_token(req, "conda", CONDA_SECRET_KEY_SECRET)
    # A data artifact has no triple, so it takes the single-package noarch path
    # and derives its own flattened package name inside it.
    if conda_noarch_eligible(req.artifact):
        return _publish_conda_noarch(req, key_id=key_id, secret_key=secret_key)
    package = conda_package_name(req.artifact)
    assets = conda_assets(req.artifact, release_assets(req.assets_dir))
    if not assets:
        served = ", ".join(sorted(CONDA_SUBDIRS.values()))
        raise ReleaseError(
            f"[artifacts.{req.artifact.name}] conda: no declared platform maps "
            f"to a served conda subdir ({served}) with a staged build-output "
            f"archive under {req.assets_dir} — the endpoint packages the staged "
            f"`{req.artifact.name}-<triple>.tar.gz`/`.zip` archives derived from "
            f"the artifact's `platforms` declaration; an unserved-only set "
            f"(osx-64 / musl) or an unbuilt matrix publishes nothing"
        )
    binary = integrity_mod.expected_main_binary(req.artifact)
    # `assets_dir` is stage-wide, so an un-namespaced `conda-channel/<subdir>`
    # would let a second conda artifact re-publish this one's `.conda`.
    recipe_root = req.assets_dir / CONDA_RECIPE_SCRATCH / req.artifact.name
    channel_dir = req.assets_dir / CONDA_CHANNEL_SCRATCH / req.artifact.name
    built: list[Path] = []
    actions: list[str] = []
    for subdir, (_triple, asset_name) in sorted(assets.items()):
        binary_name, install_dir, install_binary = _conda_binary_layout(subdir, binary)
        # rattler-build STRIPS the archive's single top-level dir on extraction,
        # so the copy source is the bare binary name, never a `<stem>/` prefix.
        source_binary = binary_name
        recipe_dir = recipe_root / subdir
        recipe_dir.mkdir(parents=True, exist_ok=True)
        recipe = recipe_dir / "recipe.yaml"
        recipe.write_text(
            render_conda_recipe(
                package=package,
                version=req.version,
                archive_path=(req.assets_dir / asset_name).as_posix(),
                source_binary=source_binary,
                install_dir=install_dir,
                install_binary=install_binary,
            ),
            encoding="utf-8",
            newline="\n",
        )
        req.run_cmd(
            [
                "rattler-build",
                "build",
                "--recipe",
                str(recipe),
                "--target-platform",
                subdir,
                "--output-dir",
                str(channel_dir),
                "--package-format",
                "conda",
                "--no-build-id",
                # `native` skips the tests on a cross-subdir repackage — the
                # binary cannot execute on the build host.
                "--test",
                "native",
            ],
            req.root,
            None,
        )
        produced = sorted((channel_dir / subdir).glob("*.conda"))
        built.extend(produced)
        actions.append(f"built {len(produced)} {subdir} package(s) from {asset_name}")
    if not built:  # pragma: no cover — a successful build always writes a .conda
        raise ReleaseError(
            f"[artifacts.{req.artifact.name}] conda: rattler-build produced no "
            f"`.conda` under {channel_dir} — the build recorded success but "
            f"emitted no package"
        )
    channel_url = _conda_channel_url(req)
    req.run_cmd(
        [
            "rattler-build",
            "publish",
            "--to",
            channel_url,
            "--force",
            *[str(path) for path in built],
        ],
        req.root,
        _conda_channel_env(key_id, secret_key),
    )
    actions.append(f"published {len(built)} package(s) to {channel_url} (+ reindex)")
    return Published(req.artifact.name, "conda", tuple(actions))


#: The foreign, review-gated registry a zed extension publishes THROUGH, by a
#: maintainer-merged PR. shipit never pushes here; it only renders coordinates.
ZED_REGISTRY = "zed-industries/extensions"

#: The manifest the endpoint reads the registry-keying extension id from; the
#: zed bundle must declare it as a required payload entry.
ZED_MANIFEST = "extension.toml"

#: A scratch SUBDIR, never a top-level file: a gh-release re-run would ship it.
ZED_SCRATCH = "zed"

#: The Zed extension-id grammar. The id is UNTRUSTED repo content used as both a
#: TOML table key and a scratch filename, so full-matching it is a security
#: boundary: it must not traverse the scratch dir or break the rendered row.
_ZED_EXTENSION_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")


def zed_extension_id(text: str) -> str:
    """The Zed extension ``id`` from an ``extension.toml`` text, validated against the grammar."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ReleaseError(
            f"zed: cannot parse {ZED_MANIFEST} to read the extension id: {exc}"
        ) from exc
    ext_id = data.get("id") if isinstance(data, dict) else None
    if not ext_id or not isinstance(ext_id, str):
        raise ReleaseError(
            f"zed: {ZED_MANIFEST} has no top-level `id` — the "
            f"{ZED_REGISTRY} registry row and its submodule dir "
            f"(extensions/<id>) are keyed by the extension id"
        )
    if not _ZED_EXTENSION_ID_RE.fullmatch(ext_id):
        # ascii(): a rejected id may carry newlines or terminal control
        # sequences, which raw interpolation would inject into the error output.
        raise ReleaseError(
            f"zed: {ZED_MANIFEST} id {ascii(ext_id)} is not a valid Zed "
            f"extension id (one lowercase segment of letters, digits, `-`, `_`; "
            f"no slashes, dots, spaces, or newlines) — the id becomes both the "
            f"`extensions.toml` table key and a scratch filename, so it must not "
            f"escape the scratch dir or break the rendered row"
        )
    return ext_id


def render_zed_registry_entry(*, ext_id: str, version: str, repo: str, tag: str) -> str:
    """The ``zed-industries/extensions`` coordinates a maintainer applies in the publish PR."""
    return (
        f"# {ZED_REGISTRY} registry entry — apply in a PR (ADR-0068):\n"
        f"# advance submodule extensions/{ext_id} to "
        f"github.com/{repo} @ {tag}, then set:\n"
        f"[{ext_id}]\n"
        f'submodule = "extensions/{ext_id}"\n'
        f'version = "{version}"\n'
    )


#: The leg an endpoint-only Zed artifact (no ``bundle``) reads its manifest from
#: — the only case with no declaration to follow.
ZED_ENDPOINT_ONLY_LEG = "rust"


def _zed_manifest_leg(artifact: config.Artifact) -> str:
    """The toolchain leg holding ``extension.toml`` — the artifact's own ``bundle.leg``."""
    spec = artifact.bundle
    if spec is None or spec.composition != "zed":
        return ZED_ENDPOINT_ONLY_LEG
    if not any(entry.path == ZED_MANIFEST and entry.required for entry in spec.payload):
        raise ReleaseError(
            f"[artifacts.{artifact.name}] zed: `bundle.payload` does not declare "
            f"`{ZED_MANIFEST}` as a required entry — the registry row is keyed by "
            f"the extension id read from that manifest, so an archive that may "
            f"omit it would publish coordinates for a package that does not "
            f'carry it. Add {{ path = "{ZED_MANIFEST}", required = true }}'
        )
    if spec.leg is None:
        raise ReleaseError(
            f"[artifacts.{artifact.name}] zed: the declared bundle carries no "
            f"`bundle.leg` — the endpoint reads its manifest from the leg the "
            f"bundle archived"
        )
    return spec.leg


def _publish_zed(req: PublishRequest) -> Published:
    """Render the Zed registry coordinates into a scratch subdir; no cross-repo write."""
    if req.repo is None:
        raise ReleaseError(
            f"[artifacts.{req.artifact.name}] zed: no source repo resolved — the "
            f"registry submodule points at github.com/<owner/name> @ <tag>, so "
            f"an unresolved repo is a hard error, never a null-source row"
        )
    leg = _leg_for(req.artifact, req.entries, _zed_manifest_leg(req.artifact), "zed")
    manifest = _leg_dir(req.root, leg) / ZED_MANIFEST
    try:
        text = manifest.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReleaseError(
            f"[artifacts.{req.artifact.name}] zed: cannot read {manifest} to "
            f"render the registry entry — the zed composition ships this manifest "
            f"as the extension's required core; run `shipit release bundle` first"
        ) from exc
    ext_id = zed_extension_id(text)
    entry = render_zed_registry_entry(
        ext_id=ext_id, version=req.version, repo=req.repo, tag=req.tag
    )
    scratch = req.assets_dir / ZED_SCRATCH
    scratch.mkdir(parents=True, exist_ok=True)
    rendered = scratch / f"{ext_id}.extensions-toml"
    rendered.write_text(entry, encoding="utf-8", newline="\n")
    return Published(
        req.artifact.name,
        "zed",
        (
            f"rendered {ZED_REGISTRY} registry entry for {ext_id} {req.version} "
            f"(submodule extensions/{ext_id} -> github.com/{req.repo}@{req.tag})",
            f"manual step: open a PR against {ZED_REGISTRY} applying this entry — "
            f"the tag is the release, shipit does not push into the registry",
        ),
    )


@dataclass(frozen=True)
class EndpointAdapter:
    """One registry entry: name, ordering stage, declared secret names, publish function."""

    name: str
    stage: str
    publish: Callable[[PublishRequest], Published]
    secrets: tuple[str, ...] = ()
    external: bool = True
    stable_only: bool = False
    stable_skip_reason: str = SKIP_STABLE_ONLY
    needs_repo: bool = False


GH_RELEASE = EndpointAdapter(
    "gh-release", "release", _publish_gh_release, external=False
)
CRATES = EndpointAdapter(
    "crates", "release", _publish_crates, secrets=secretreq.ENDPOINT_SECRETS["crates"]
)
PYPI = EndpointAdapter(
    "pypi", "release", _publish_pypi, secrets=secretreq.ENDPOINT_SECRETS["pypi"]
)
NPM = EndpointAdapter(
    "npm", "release", _publish_npm, secrets=secretreq.ENDPOINT_SECRETS["npm"]
)
VSCODE_MARKETPLACE = EndpointAdapter(
    "vscode-marketplace",
    "release",
    _publish_vscode_marketplace,
    secrets=secretreq.ENDPOINT_SECRETS["vscode-marketplace"],
)
OPEN_VSX = EndpointAdapter(
    "open-vsx",
    "release",
    _publish_open_vsx,
    secrets=secretreq.ENDPOINT_SECRETS["open-vsx"],
)
BREW = EndpointAdapter(
    "brew",
    "derived",
    _publish_brew,
    secrets=secretreq.ENDPOINT_SECRETS["brew"],
    stable_only=True,
    needs_repo=True,
)
NOTIFY_DOWNSTREAMS = EndpointAdapter(
    "notify-downstreams",
    "derived",
    _publish_notify_downstreams,
    secrets=secretreq.ENDPOINT_SECRETS["notify-downstreams"],
    stable_only=True,
    stable_skip_reason=SKIP_NOTIFY_PRERELEASE,
    needs_repo=True,
)
CONDA = EndpointAdapter(
    "conda",
    "derived",
    _publish_conda,
    secrets=secretreq.ENDPOINT_SECRETS["conda"],
    # Not stable_only: prereleases publish for manual pin-testing.
    needs_repo=True,
)
ZED = EndpointAdapter(
    "zed",
    "derived",
    _publish_zed,
    secrets=secretreq.ENDPOINT_SECRETS["zed"],
    stable_only=True,
    stable_skip_reason=SKIP_ZED_PRERELEASE,
    needs_repo=True,
)

#: The CLOSED registry, in a stable order; :data:`shipit.config.ENDPOINTS` names
#: exactly this set. Adding an endpoint is adding an entry, never a switch.
ADAPTERS: tuple[EndpointAdapter, ...] = (
    GH_RELEASE,
    CRATES,
    PYPI,
    NPM,
    VSCODE_MARKETPLACE,
    OPEN_VSX,
    BREW,
    NOTIFY_DOWNSTREAMS,
    CONDA,
    ZED,
)


def names() -> tuple[str, ...]:
    return tuple(a.name for a in ADAPTERS)


def adapter_for(name: str) -> EndpointAdapter | None:
    for adapter in ADAPTERS:
        if adapter.name == name:
            return adapter
    return None
