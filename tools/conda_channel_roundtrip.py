#!/usr/bin/env python3
"""The ADR-0064 `file://` conda round-trip harness — build → local channel →
scratch `pixi` resolve → read the env-prefix staging path.

The ADR-0064 spike ("build → pixi resolve → run → version bump → transparent
re-resolve") was validated ONCE by hand and never automated; this module is the
reusable form of that loop, so a producer/consumer change can prove the round
trip via `pixi run -e test pytest` instead of a cut release (#1053, the ARF02
Gate-0 local harness). It is the harness ARF02 Steps 1/2 (#1078 verify the
noarch UNION channel layout, #1079 the staging tool) drive against.

Two pieces, importable OR runnable:

- :func:`resolve_from_file_channel` — the reusable core. Given an ALREADY-BUILT
  local channel tree (``<channel>/<subdir>/repodata.json`` + ``.conda`` — exactly
  what :func:`shipit.release.publish._publish_conda` writes to its scratch
  ``--output-dir``, or what ``rattler-build build`` emits directly), it writes a
  PLAIN ``[workspace]`` scratch ``pixi.toml`` pointed at ``file://<channel>`` and
  a single dependency, runs ``pixi install``, and returns the env-prefix path the
  package staged into. It deliberately does NOT use the ``[artifact-deps]``
  projection: that projection hard-codes the GCS channel host
  (:func:`shipit.config.artifactdeps.public_channel_url`), so a ``file://``
  channel can only be exercised by a hand-written plain channel+dep manifest.

- :func:`build_tool_channel` — a convenience builder for the manual spike loop
  (the ``__main__`` path): renders the producer's OWN recipe
  (:func:`shipit.release.publish.render_conda_recipe`) over a tiny prebuilt-
  binary archive and runs a real ``rattler-build build`` into a channel tree.
  Tests drive the real producer (``_publish_conda``) instead and only reuse
  :func:`resolve_from_file_channel`.

Run it directly for a throwaway end-to-end check on the host's NATIVE subdir
(the package must match the host to install)::

    pixi run -e test python tools/conda_channel_roundtrip.py

It prints the env-prefix staging path (``…/bin/<binary>``) and the binary's
output, then exits non-zero if the round trip did not stage the binary.
"""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

# The served conda subdir keyed by the host's (system, machine). Mirrors
# publish.CONDA_SUBDIRS (triple → subdir) reduced to the host axis — kept local
# so the harness has no import-time dependency on a live release request.
#
# Only the unix hosts of the served set are mapped. win-64 IS served but stages
# under a ``Scripts/<binary>.exe`` layout the harness's bin/<binary> fixture does
# not model, and Intel-mac (osx-64) is not in the served set at all — so on those
# hosts `host_conda_subdir()` raises and callers (the __main__ path, the tests)
# treat the raise as "skip cleanly, this host can't run the round trip" rather
# than guessing an unsupported subdir.
_HOST_SUBDIR = {
    ("Darwin", "arm64"): "osx-arm64",
    ("Darwin", "aarch64"): "osx-arm64",
    ("Linux", "x86_64"): "linux-64",
    ("Linux", "aarch64"): "linux-aarch64",
    ("Linux", "arm64"): "linux-aarch64",
}


def host_conda_subdir() -> str:
    """The conda subdir of the machine running this process — the ONLY subdir a
    ``pixi install`` here can actually stage (a cross-subdir package resolves but
    will not install on a foreign host). Raises ``RuntimeError`` on an unmapped
    host (Windows, Intel mac, or any host outside the mapped unix set) rather
    than guessing a subdir the round trip cannot honour; callers catch that to
    SKIP cleanly on a host where the harness cannot run."""
    key = (platform.system(), platform.machine())
    subdir = _HOST_SUBDIR.get(key)
    if subdir is None:  # pragma: no cover — CI/dev hosts are the mapped four
        raise RuntimeError(
            f"no served conda subdir for host {key}; the file:// round trip needs "
            f"a package built for the host's native subdir"
        )
    return subdir


def resolve_from_file_channel(
    *,
    channel_dir: Path,
    package: str,
    version: str,
    binary: str,
    scratch: Path,
    pixi: str = "pixi",
) -> Path:
    """Resolve ``package ==version`` from the local ``channel_dir`` via a scratch
    ``pixi`` project and return the env-prefix path the ``binary`` staged into.

    ``channel_dir`` is a built channel tree (``<subdir>/repodata.json`` +
    ``.conda``). A plain ``[workspace]`` ``pixi.toml`` is written under
    ``scratch`` with ``channels = ["file://<channel_dir>"]`` and one dependency
    on ``package``, then ``pixi install`` runs there. Returns
    ``<env-prefix>/bin/<binary>`` (or ``Scripts/<binary>.exe`` on win) — the
    staging path callers assert on. Raises if the binary did not land, so a
    silent empty resolve is never mistaken for a round trip.

    This is the ADR-0064 loop's load-bearing step; ARF02 Steps 1/2 (#1078/#1079)
    call it to inspect a producer-built channel offline.
    """
    proj = scratch / "resolve"
    proj.mkdir(parents=True, exist_ok=True)
    channel_url = channel_dir.resolve().as_uri()
    subdir = host_conda_subdir()
    # A PLAIN channel+dep manifest — never the [artifact-deps] projection, which
    # hard-codes the GCS host and so cannot point at a file:// channel. The dep
    # key and constraint are rendered as QUOTED TOML strings (json.dumps): a
    # dotted conda package name (`foo.bar`) is a single bare key here, not a TOML
    # dotted-key path, and a CLI-supplied version can't inject unescaped TOML.
    manifest = (
        f"[workspace]\n"
        f'name = "conda-roundtrip"\n'
        f"channels = [{json.dumps(channel_url)}]\n"
        f"platforms = [{json.dumps(subdir)}]\n"
        f"\n"
        f"[dependencies]\n"
        f"{json.dumps(package)} = {json.dumps(f'=={version}')}\n"
    )
    (proj / "pixi.toml").write_text(manifest, encoding="utf-8")
    subprocess.run(
        [pixi, "install", "--manifest-path", str(proj / "pixi.toml")],
        cwd=str(proj),
        check=True,
        capture_output=True,
        text=True,
        timeout=600,
    )
    prefix = proj / ".pixi" / "envs" / "default"
    staged = (
        prefix / "Scripts" / f"{binary}.exe"
        if subdir == "win-64"
        else prefix / "bin" / binary
    )
    if not staged.exists():
        raise RuntimeError(
            f"file:// round trip resolved {package}=={version} from {channel_url} "
            f"but staged no binary at {staged} — the channel package placed no "
            f"`{binary}` on PATH"
        )
    return staged


def build_tool_channel(
    *,
    channel_dir: Path,
    package: str,
    version: str,
    binary: str,
    subdir: str,
    binary_body: bytes,
) -> Path:
    """Render the producer's recipe over a tiny prebuilt-binary archive and run a
    real ``rattler-build build`` into ``channel_dir`` for ``subdir`` — the manual
    spike loop's builder. Returns the built ``.conda`` path.

    The archive wraps the binary in a top-level ``<package>-<subdir>/`` dir (the
    release-stage shape rattler-build STRIPS, #1049 — the subdir stands in for the
    triple in that wrapper name; the recipe only ever copies the bare binary).
    Tests exercise the real producer (``_publish_conda``) instead and reuse only
    :func:`resolve_from_file_channel`; this keeps the ``__main__`` self-contained.
    """
    # Imported lazily so `resolve_from_file_channel` carries no shipit import cost.
    from shipit.release.publish import render_conda_recipe

    work = channel_dir.parent
    channel_dir.mkdir(parents=True, exist_ok=True)
    stage = work / f"{package}-{subdir}"
    stage.mkdir(parents=True, exist_ok=True)
    staged_binary = stage / binary
    staged_binary.write_bytes(binary_body)
    # A prebuilt tool is executable; the recipe `cp`s it verbatim, so a non-exec
    # source stages a package whose `bin/<binary>` a consumer cannot run.
    staged_binary.chmod(staged_binary.stat().st_mode | 0o111)
    archive = work / f"{package}-{subdir}.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(stage, arcname=stage.name)
    recipe = work / "recipe.yaml"
    recipe.write_text(
        render_conda_recipe(
            package=package,
            version=version,
            archive_path=archive.as_posix(),
            source_binary=binary,
            install_dir="bin",
            install_binary=binary,
        ),
        encoding="utf-8",
        newline="\n",
    )
    subprocess.run(
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
            "--test",
            "native",
        ],
        cwd=str(work),
        check=True,
        timeout=600,
    )
    built = sorted((channel_dir / subdir).glob("*.conda"))
    if not built:
        raise RuntimeError(
            f"rattler-build wrote no .conda under {channel_dir / subdir}"
        )
    return built[0]


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="ADR-0064 file:// conda round-trip spike loop."
    )
    parser.add_argument("--package", default="roundtrip-demo")
    parser.add_argument("--version", default="1.2.3")
    parser.add_argument("--binary", default="roundtrip-demo")
    args = parser.parse_args(argv)

    # The unsupported-host check comes FIRST: a host the round trip can't run on
    # (Windows / Intel-mac / anything off the mapped unix set) is a clean skip
    # (message + rc 0, never a traceback) REGARDLESS of which tools are installed
    # — the host, not a missing tool, is why it can't run. Ordering this after
    # the tool-presence loop would misreport such a host as a tool-missing rc 2.
    try:
        subdir = host_conda_subdir()  # native — so pixi can install it here
    except RuntimeError as exc:
        print(f"skipping: {exc} — unsupported host", file=sys.stderr)
        return 0

    for tool in ("rattler-build", "pixi"):
        if shutil.which(tool) is None:
            print(
                f"error: {tool} not on PATH (needed for the round trip)",
                file=sys.stderr,
            )
            return 2

    with tempfile.TemporaryDirectory(prefix="conda-roundtrip-") as tmp:
        root = Path(tmp)
        channel = root / "channel"
        build_tool_channel(
            channel_dir=channel,
            package=args.package,
            version=args.version,
            binary=args.binary,
            subdir=subdir,
            binary_body=f"#!/bin/sh\necho hi from {args.binary}\n".encode(),
        )
        staged = resolve_from_file_channel(
            channel_dir=channel,
            package=args.package,
            version=args.version,
            binary=args.binary,
            scratch=root,
        )
        print(f"staged: {staged}")
        # Run the staged tool DIRECTLY (via its own +x bit / shebang), never
        # `/bin/sh <path>`: invoking through a shell would mask a non-executable
        # stage — the exact defect the executable round trip is meant to catch.
        out = subprocess.run([str(staged)], capture_output=True, text=True, timeout=30)
        print(f"binary output: {out.stdout.strip()!r}")
        if out.returncode != 0:
            # A GENUINE run failure — the staged tool resolved but won't execute.
            # Fail loudly (non-zero); distinct from the unmapped-host skip above,
            # which is an intentional rc-0. The round trip did not hold.
            print(
                f"error: staged tool {staged} exited {out.returncode}: "
                f"{out.stderr.strip()!r}",
                file=sys.stderr,
            )
            return 1
    return 0


if __name__ == "__main__":  # pragma: no cover — manual spike-loop entrypoint
    raise SystemExit(_main())
