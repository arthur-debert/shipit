"""Consumer-side Artifact channel — ``[artifact-deps]`` -> managed pixi blocks.

The channel location is DERIVED from the producing repo's visibility; the
version pin is consumer-owned and never projected.
See docs/adr/0077-collapse-to-conda-direct.md.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

from ..channel import buckets
from ..config import ArtifactDep
from .units import PIXI_FILE, Unit

_PIXI_ENVS_DIR = (".pixi", "envs")

PUBLIC_CHANNEL_HOST = buckets.CHANNEL_HOST
PUBLIC_ARTIFACT_BUCKET = buckets.PUBLIC_ARTIFACT_BUCKET

PRIVATE_ARTIFACT_BUCKET = buckets.PRIVATE_ARTIFACT_BUCKET

PRIVATE_CHANNEL_SCHEME = "s3://"

S3_OPTIONS_ENDPOINT_URL = PUBLIC_CHANNEL_HOST
S3_OPTIONS_REGION = "auto"
S3_OPTIONS_FORCE_PATH_STYLE = True

DEFAULT_FEATURE = "shipit-artifacts"
DEFAULT_ENV = "default"

ENVIRONMENTS_KEY = f"{PIXI_FILE}#shipit-artifact-deps-environments"
ENVIRONMENTS_ANCHOR = "[environments]"
ENVIRONMENTS_OPEN = (
    "# >>> shipit-managed artifact-dep environments "
    "(do not edit; regenerate via `shipit install`) >>>"
)
ENVIRONMENTS_CLOSE = "# <<< shipit-managed artifact-dep environments <<<"

S3_OPTIONS_KEY = f"{PIXI_FILE}#shipit-artifact-deps-s3-options"
S3_OPTIONS_OPEN = (
    "# >>> shipit-managed artifact-dep s3-options "
    "(do not edit; regenerate via `shipit install`) >>>"
)
S3_OPTIONS_CLOSE = "# <<< shipit-managed artifact-dep s3-options <<<"


def public_channel_url(repo_slug: str) -> str:
    """The authless HTTPS per-repo channel URL for a PUBLIC producing repo."""
    return f"{PUBLIC_CHANNEL_HOST}/{PUBLIC_ARTIFACT_BUCKET}/{repo_slug}"


def private_channel_url(repo_slug: str) -> str:
    """The ``s3://`` S3-interop per-repo channel URL for a PRIVATE producing repo."""
    return f"{PRIVATE_CHANNEL_SCHEME}{PRIVATE_ARTIFACT_BUCKET}/{repo_slug}"


def channel_url(repo_slug: str, *, private: bool) -> str:
    """Derive the channel URL from ``private``, the already-resolved repo visibility."""
    return private_channel_url(repo_slug) if private else public_channel_url(repo_slug)


def _feature_name(feature: str | None) -> str:
    return DEFAULT_FEATURE if feature is None else f"{DEFAULT_FEATURE}-{feature}"


def pin_feature(feature: str | None) -> str:
    """The pixi feature a consumer authors a target's version pin into — the one carrying its channel."""
    return _feature_name(feature)


def missing_pins(
    deps: Sequence[ArtifactDep], manifest: dict
) -> list[tuple[ArtifactDep, str]]:
    """``(dep, table)`` for every dep with no consumer pin in ``manifest``, in declaration order."""
    features = manifest.get("feature", {})
    features = features if isinstance(features, dict) else {}
    absent: list[tuple[ArtifactDep, str]] = []
    for dep in deps:
        fname = pin_feature(dep.feature)
        table = features.get(fname, {})
        table = table if isinstance(table, dict) else {}
        pins = table.get("dependencies", {})
        pins = pins if isinstance(pins, dict) else {}
        if dep.package not in pins:
            absent.append((dep, f"[feature.{_toml_key(fname)}.dependencies]"))
    return absent


def env_name(feature: str | None) -> str:
    """The environment a target's feature is wired into — the default env for the default target."""
    return DEFAULT_ENV if feature is None else f"{DEFAULT_FEATURE}-{feature}"


def _is_windows_target(target: str) -> bool:
    """Whether a target triple is windows — conda installs its tools to ``Scripts/`` there, not ``bin/``."""
    return "windows" in target


def env_prefix(root: Path, feature: str | None) -> Path:
    """The on-disk pixi env prefix a projected artifact-dep materializes into; pure path arithmetic."""
    return root.joinpath(*_PIXI_ENVS_DIR, env_name(feature))


def materialized_bin_path(root: Path, dep: ArtifactDep, *, target: str) -> Path:
    """On-disk path of a TOOL artifact-dep's binary for ``target``; pure path arithmetic, no probe."""
    prefix = env_prefix(root, dep.feature)
    if _is_windows_target(target):
        return prefix / "Scripts" / f"{dep.package}.exe"
    return prefix / "bin" / dep.package


def _toml_str_list(values: Sequence[str]) -> str:
    """A TOML inline array of double-quoted string VALUES — no escaping; keys go through :func:`_toml_key`."""
    return "[" + ", ".join(f'"{v}"' for v in values) + "]"


_BARE_KEY_RE = re.compile(r"[A-Za-z0-9_-]+")


def _toml_key(name: str) -> str:
    """One TOML key segment, quoted only when it carries a dot or other non-bare char."""
    return name if _BARE_KEY_RE.fullmatch(name) else f'"{name}"'


def _feature_block(
    feature: str | None, resolved: Sequence[tuple[ArtifactDep, str]]
) -> str:
    """The inner text of one target's feature block: its de-duped ``channels``, under its own header."""
    name = _toml_key(_feature_name(feature))
    urls: list[str] = []
    for _, url in resolved:
        if url not in urls:
            urls.append(url)
    return "\n".join([f"[feature.{name}]", f"channels = {_toml_str_list(urls)}"])


def _feature_unit(
    feature: str | None, resolved: Sequence[tuple[ArtifactDep, str]]
) -> Unit:
    """One anchor-less ``[feature.<X>]`` channel block — the whole table, header inside the markers."""
    name = _feature_name(feature)
    return Unit(
        key=f"{PIXI_FILE}#{name}",
        dest=PIXI_FILE,
        kind="block",
        content=_feature_block(feature, resolved).encode("utf-8"),
        open_marker=(
            f"# >>> shipit-managed artifact-dep feature `{name}` "
            f"(do not edit; regenerate via `shipit install`) >>>"
        ),
        close_marker=f"# <<< shipit-managed artifact-dep feature `{name}` <<<",
        anchor=None,
    )


def _environments_unit(features: Sequence[str | None]) -> Unit:
    """The one consolidated block wiring every target's feature into its environment."""
    lines = [
        f"{_toml_key(env_name(f))} = {_toml_str_list([_feature_name(f)])}"
        for f in features
    ]
    return Unit(
        key=ENVIRONMENTS_KEY,
        dest=PIXI_FILE,
        kind="block",
        content="\n".join(lines).encode("utf-8"),
        open_marker=ENVIRONMENTS_OPEN,
        close_marker=ENVIRONMENTS_CLOSE,
        anchor=ENVIRONMENTS_ANCHOR,
    )


def _s3_bucket(url: str) -> str | None:
    """The bucket of a private ``s3://`` channel URL, or ``None`` for a public one."""
    if not url.startswith(PRIVATE_CHANNEL_SCHEME):
        return None
    return url[len(PRIVATE_CHANNEL_SCHEME) :].split("/", 1)[0]


def _s3_options_block(buckets: Sequence[str]) -> str:
    """One ``[s3-options.<bucket>]`` table per private bucket, templated directly into TOML."""
    force = "true" if S3_OPTIONS_FORCE_PATH_STYLE else "false"
    tables = [
        "\n".join(
            [
                f"[s3-options.{_toml_key(bucket)}]",
                f'endpoint-url = "{S3_OPTIONS_ENDPOINT_URL}"',
                f'region = "{S3_OPTIONS_REGION}"',
                f"force-path-style = {force}",
            ]
        )
        for bucket in buckets
    ]
    return "\n\n".join(tables)


def _s3_options_unit(buckets: Sequence[str]) -> Unit:
    """The one consolidated ``[s3-options]`` block for every private bucket in play."""
    return Unit(
        key=S3_OPTIONS_KEY,
        dest=PIXI_FILE,
        kind="block",
        content=_s3_options_block(buckets).encode("utf-8"),
        open_marker=S3_OPTIONS_OPEN,
        close_marker=S3_OPTIONS_CLOSE,
        anchor=None,
    )


def project(resolved: Sequence[tuple[ArtifactDep, str]]) -> list[Unit]:
    """Project resolved ``(ArtifactDep, channel_url)`` pairs into managed pixi units — pure and network-free."""
    if not resolved:
        return []
    groups: dict[str | None, list[tuple[ArtifactDep, str]]] = {}
    for dep, url in resolved:
        groups.setdefault(dep.feature, []).append((dep, url))
    units = [_feature_unit(feature, pairs) for feature, pairs in groups.items()]
    units.append(_environments_unit(list(groups)))
    buckets: list[str] = []
    for _, url in resolved:
        bucket = _s3_bucket(url)
        if bucket is not None and bucket not in buckets:
            buckets.append(bucket)
    if buckets:
        units.append(_s3_options_unit(buckets))
    return units
