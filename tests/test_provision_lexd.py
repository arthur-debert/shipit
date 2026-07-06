"""lexd provisioning (ADP00-WS03): the pure decision core, the boundary with the
Exec seam faked, and the `shipit provision lexd` verb glue.

The retired ``tools/provision-lexd.sh`` contract, restated as behavior: resolve
the platform to a pinned release triple (fail LOUD where no asset exists —
never a host-lexd fallback), no-op when the pinned lexd is already installed,
checksum-verify the fetched tarball before anything lands, and install the
binary 0755 into the invoking env's ``bin/``.
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

from shipit import execrun
from shipit.provision import lexd
from shipit.verbs import provision as provision_verb

# --------------------------------------------------------------------------
# The pure decision core
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("system", "machine", "triple"),
    [
        ("Linux", "x86_64", "x86_64-unknown-linux-gnu"),
        ("Linux", "aarch64", "aarch64-unknown-linux-gnu"),
        ("Linux", "arm64", "aarch64-unknown-linux-gnu"),
        ("Darwin", "arm64", "aarch64-apple-darwin"),
        ("Darwin", "aarch64", "aarch64-apple-darwin"),
    ],
)
def test_resolve_triple_supported_platforms(system, machine, triple):
    assert lexd.resolve_triple(system, machine) == triple


def test_resolve_triple_intel_mac_fails_loud_with_pinned_alternative():
    # No x86_64 macOS asset at the pin: the refusal carries the one supported
    # alternative (build from the pinned source) — never a host-lexd fallback.
    with pytest.raises(lexd.ProvisionError) as excinfo:
        lexd.resolve_triple("Darwin", "x86_64")
    message = str(excinfo.value)
    assert "cargo install" in message
    assert f"--tag v{lexd.PIN}" in message


@pytest.mark.parametrize(
    ("system", "machine"),
    [("Linux", "riscv64"), ("Darwin", "i386"), ("Windows", "x86_64")],
)
def test_resolve_triple_refuses_unsupported_platforms(system, machine):
    with pytest.raises(lexd.ProvisionError):
        lexd.resolve_triple(system, machine)


def test_every_resolvable_triple_has_a_pinned_sha():
    # The checksum gate can never be structurally skipped: each triple
    # resolve_triple can mint has a pinned SHA-256.
    for system, machine in [
        ("Linux", "x86_64"),
        ("Linux", "aarch64"),
        ("Darwin", "arm64"),
    ]:
        triple = lexd.resolve_triple(system, machine)
        assert lexd.expected_sha(triple)


def test_expected_sha_refuses_an_unpinned_triple():
    with pytest.raises(lexd.ProvisionError, match="no pinned SHA-256"):
        lexd.expected_sha("wasm32-unknown-unknown")


def test_release_url_pins_tag_and_triple():
    url = lexd.release_url("aarch64-apple-darwin")
    assert url == (
        f"https://github.com/lex-fmt/lex/releases/download/v{lexd.PIN}/"
        "lexd-aarch64-apple-darwin.tar.gz"
    )


@pytest.mark.parametrize(
    ("output", "pinned"),
    [
        (None, False),  # probe could not run — no binary
        ("", False),
        ("lexd 0.9.9", False),  # older pin → reinstall
        (f"lexd {lexd.PIN}", True),
        (f"lexd {lexd.PIN} (release)", True),
        # A longer version that merely EMBEDS the pin is not the pin — the
        # tightened token match rejects what a bare `PIN in output` accepted.
        (f"lexd {lexd.PIN}5", False),  # 0.19.15 starts with 0.19.1
        (f"lexd 1{lexd.PIN}5", False),  # 10.19.15 embeds 0.19.1
        # A pre-release / build-metadata suffix is not the pinned release: the
        # right-edge guard is whitespace-or-end, not a word boundary (which sits
        # between the trailing digit and a `-`/`+`).
        (f"lexd {lexd.PIN}-dev", False),
        (f"lexd {lexd.PIN}+meta", False),
    ],
)
def test_is_pinned(output, pinned):
    assert lexd.is_pinned(output) is pinned


# --------------------------------------------------------------------------
# The boundary, with the Exec seam faked
# --------------------------------------------------------------------------


def make_tarball(inner_name: str = "lexd-aarch64-apple-darwin/lexd") -> bytes:
    """A minimal release tarball: one executable member, gzip'd."""
    payload = b"#!/bin/sh\necho fake-lexd\n"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(inner_name)
        info.size = len(payload)
        info.mode = 0o755
        tar.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


def make_runner(calls, *, version_output=None, tarball: bytes | None = None):
    """A fake Exec runner: answers the version probe and materializes the curl fetch.

    The probe Exec only runs against a binary that EXISTS (an absent dest
    answers without an Exec), so ``version_output=None`` fakes a present-but-
    unlaunchable binary (the seam's :class:`~shipit.execrun.ExecError`); a
    string fakes a working lexd whose ``--version`` prints it.
    """

    def runner(argv, **kwargs):
        calls.append((list(argv), kwargs))
        if argv[0] == "curl":
            assert tarball is not None, "unexpected fetch"
            Path(argv[argv.index("-o") + 1]).write_bytes(tarball)
            return execrun.ExecResult(tuple(argv), 0, "", "", 1)
        # the version probe
        if version_output is None:
            raise execrun.ExecError(argv, rc=None, cause=execrun.CAUSE_OS)
        return execrun.ExecResult(tuple(argv), 0, version_output, "", 1)

    return runner


def plant_binary(prefix: Path) -> Path:
    """A pre-existing ``bin/lexd`` at ``prefix`` — makes the probe Exec run."""
    dest = prefix / "bin" / "lexd"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"old-lexd")
    return dest


def test_provision_noop_when_pinned_lexd_already_installed(tmp_path):
    dest = plant_binary(tmp_path)
    calls = []
    runner = make_runner(calls, version_output=f"lexd {lexd.PIN}")
    report = lexd.provision(tmp_path, runner=runner)
    assert report.action == lexd.ACTION_NOOP
    assert report.pin == lexd.PIN
    assert report.triple is None
    # Exactly one Exec — the probe. No fetch, no reinstall.
    assert len(calls) == 1
    assert calls[0][0] == [str(dest), "--version"]
    assert dest.read_bytes() == b"old-lexd"  # untouched


def test_probe_runs_before_platform_resolution(tmp_path):
    # An Intel mac that provisioned lexd another way (the refusal's cargo
    # instruction) still no-ops: idempotence is decided BEFORE the platform
    # refusal can fire.
    plant_binary(tmp_path)
    runner = make_runner([], version_output=f"lexd {lexd.PIN}")
    report = lexd.provision(tmp_path, system="Darwin", machine="x86_64", runner=runner)
    assert report.action == lexd.ACTION_NOOP


def test_fresh_env_probes_without_an_exec(tmp_path, monkeypatch):
    # No binary at dest: "not yet installed" is the normal fresh-env answer —
    # decided by existence, never by manufacturing a missing-binary ExecError
    # (which would put an ERROR record in the log for a healthy provision).
    tarball = make_tarball()
    monkeypatch.setitem(
        lexd.SHAS, "aarch64-apple-darwin", hashlib.sha256(tarball).hexdigest()
    )
    calls = []
    runner = make_runner(calls, tarball=tarball)
    lexd.provision(tmp_path, system="Darwin", machine="arm64", runner=runner)
    # The ONLY Exec is the fetch.
    assert [argv[0] for argv, _ in calls] == ["curl"]


def test_provision_reinstalls_over_an_unlaunchable_binary(tmp_path, monkeypatch):
    # A present-but-broken binary (the probe's ExecError) is an answer, not a
    # transport failure: reinstall.
    plant_binary(tmp_path)
    tarball = make_tarball()
    monkeypatch.setitem(
        lexd.SHAS, "aarch64-apple-darwin", hashlib.sha256(tarball).hexdigest()
    )
    runner = make_runner([], tarball=tarball)  # probe raises (no version_output)
    report = lexd.provision(tmp_path, system="Darwin", machine="arm64", runner=runner)
    assert report.action == lexd.ACTION_INSTALLED


def test_provision_fetches_verifies_and_installs(tmp_path, monkeypatch):
    tarball = make_tarball()
    monkeypatch.setitem(
        lexd.SHAS, "aarch64-apple-darwin", hashlib.sha256(tarball).hexdigest()
    )
    calls = []
    runner = make_runner(calls, tarball=tarball)
    report = lexd.provision(tmp_path, system="Darwin", machine="arm64", runner=runner)
    assert report.action == lexd.ACTION_INSTALLED
    assert report.triple == "aarch64-apple-darwin"
    dest = tmp_path / "bin" / "lexd"
    assert report.dest == str(dest)
    # The installed binary is the tarball's member, executable.
    assert dest.read_bytes() == b"#!/bin/sh\necho fake-lexd\n"
    assert dest.stat().st_mode & 0o777 == 0o755
    # One Exec: the curl fetch of the pinned URL (fresh env — no probe Exec).
    assert len(calls) == 1
    fetch_argv = calls[0][0]
    assert fetch_argv[0] == "curl"
    assert lexd.release_url("aarch64-apple-darwin") in fetch_argv
    assert calls[0][1]["timeout"] == lexd.FETCH_TIMEOUT


def test_provision_reinstalls_over_an_unpinned_binary(tmp_path, monkeypatch):
    # An older lexd on the path is not the pin: same fetch-and-install as absent.
    plant_binary(tmp_path)
    tarball = make_tarball("lexd-x86_64-unknown-linux-gnu/lexd")
    monkeypatch.setitem(
        lexd.SHAS, "x86_64-unknown-linux-gnu", hashlib.sha256(tarball).hexdigest()
    )
    runner = make_runner([], version_output="lexd 0.1.0", tarball=tarball)
    report = lexd.provision(tmp_path, system="Linux", machine="x86_64", runner=runner)
    assert report.action == lexd.ACTION_INSTALLED


def test_provision_refuses_a_checksum_mismatch(tmp_path):
    # The pinned SHAS don't match an arbitrary test tarball — the gate fires
    # and NOTHING is installed.
    runner = make_runner([], tarball=make_tarball())
    with pytest.raises(lexd.ProvisionError, match="SHA-256 mismatch"):
        lexd.provision(tmp_path, system="Darwin", machine="arm64", runner=runner)
    assert not (tmp_path / "bin" / "lexd").exists()


def test_provision_refuses_unsupported_platform_before_any_fetch(tmp_path):
    calls = []
    runner = make_runner(calls)  # no tarball: a fetch would assert
    with pytest.raises(lexd.ProvisionError, match="Intel|x86_64"):
        lexd.provision(tmp_path, system="Darwin", machine="x86_64", runner=runner)
    # The refusal fires before any Exec (fresh env — no probe, no fetch).
    assert calls == []


def test_provision_refuses_without_an_active_env(monkeypatch):
    monkeypatch.delenv("CONDA_PREFIX", raising=False)
    with pytest.raises(lexd.ProvisionError, match="CONDA_PREFIX"):
        lexd.provision(runner=make_runner([]))


def test_provision_defaults_prefix_to_conda_prefix(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDA_PREFIX", str(tmp_path))
    plant_binary(tmp_path)
    runner = make_runner([], version_output=f"lexd {lexd.PIN}")
    report = lexd.provision(runner=runner)
    assert report.dest == str(tmp_path / "bin" / "lexd")


def test_extract_binary_matches_by_basename_not_prefix(tmp_path):
    # The member is found wherever the release nests it — never an assumed prefix.
    tarball = tmp_path / "lexd.tar.gz"
    tarball.write_bytes(make_tarball("some/other/prefix/lexd"))
    binary = lexd._extract_binary(tarball, "https://example.test/lexd.tar.gz")
    assert binary == b"#!/bin/sh\necho fake-lexd\n"


def test_extract_binary_refuses_a_tarball_without_lexd(tmp_path):
    payload = make_tarball("lexd-triple/README.md")
    tarball = tmp_path / "lexd.tar.gz"
    tarball.write_bytes(payload)
    with pytest.raises(lexd.ProvisionError, match="no lexd binary"):
        lexd._extract_binary(tarball, "https://example.test/lexd.tar.gz")


def test_extract_binary_refuses_garbage(tmp_path):
    tarball = tmp_path / "lexd.tar.gz"
    tarball.write_bytes(b"not a tarball")
    with pytest.raises(lexd.ProvisionError, match="unreadable"):
        lexd._extract_binary(tarball, "https://example.test/lexd.tar.gz")


# --------------------------------------------------------------------------
# The verb glue (ADR-0030): render at the edge, refusals through the shell
# --------------------------------------------------------------------------


def test_run_lexd_renders_noop_text(monkeypatch, capsys):
    report = lexd.LexdReport(
        pin=lexd.PIN, action=lexd.ACTION_NOOP, dest="/env/bin/lexd"
    )
    monkeypatch.setattr(provision_verb.lexd, "provision", lambda: report)
    rc = provision_verb.run_lexd()
    assert rc == 0
    out = capsys.readouterr().out
    assert "already provisioned" in out
    assert lexd.PIN in out


def test_run_lexd_renders_install_json(monkeypatch, capsys):
    report = lexd.LexdReport(
        pin=lexd.PIN,
        action=lexd.ACTION_INSTALLED,
        dest="/env/bin/lexd",
        triple="aarch64-apple-darwin",
    )
    monkeypatch.setattr(provision_verb.lexd, "provision", lambda: report)
    rc = provision_verb.run_lexd(as_json=True)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "pin": lexd.PIN,
        "action": "installed",
        "dest": "/env/bin/lexd",
        "triple": "aarch64-apple-darwin",
    }


def test_run_lexd_maps_refusal_to_error_line(monkeypatch, capsys):
    def refuse():
        raise lexd.ProvisionError("provision lexd: unsupported OS 'Plan9'")

    monkeypatch.setattr(provision_verb.lexd, "provision", refuse)
    rc = provision_verb.run_lexd()
    assert rc == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("error: provision lexd: unsupported OS")
    assert captured.out == ""
