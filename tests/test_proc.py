"""Unit tests for :mod:`shipit.proc` — the generic subprocess runner.

The load-bearing test here is the stdin contract (ADR-0020): ``proc.run`` must
redirect a child's stdin from ``DEVNULL`` when no ``input`` is supplied, so a
stdin-reading child (notably ``agy --print``) gets a clean EOF instead of
inheriting — and hanging on — an idle parent pipe. The fix is exercised both at the
boundary (the exact ``subprocess.run`` kwargs) and end-to-end (a real child that
reads stdin returns promptly rather than blocking).
"""

from __future__ import annotations

import subprocess
import sys

from shipit import proc


def test_run_redirects_stdin_from_devnull_when_no_input(monkeypatch) -> None:
    """With no ``input``, ``proc.run`` pins the child's stdin to ``DEVNULL``.

    Inheriting the parent's stdin is the root cause of the intermittent agy hang
    (ADR-0020): a child that reads an idle inherited pipe blocks forever. Assert the
    runner passes ``stdin=subprocess.DEVNULL`` (and NOT ``input``) to subprocess.run.
    """
    captured: dict[str, object] = {}

    def fake_run(cmd, **kwargs):  # noqa: ANN001 — test stub mirrors subprocess.run
        captured.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    proc.run(["true"])

    assert captured["stdin"] is subprocess.DEVNULL
    # input must be None (not piped) so the DEVNULL redirect is the one in effect —
    # passing both input and stdin to subprocess.run is a ValueError.
    assert captured["input"] is None


def test_run_leaves_stdin_to_subprocess_when_input_given(monkeypatch) -> None:
    """When ``input`` IS supplied, ``stdin`` is left as ``None`` for subprocess.

    subprocess.run opens its own pipe to feed ``input`` and closes it after writing;
    pinning ``stdin`` too would raise ValueError. The runner must defer to it.
    """
    captured: dict[str, object] = {}

    def fake_run(cmd, **kwargs):  # noqa: ANN001 — test stub mirrors subprocess.run
        captured.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    proc.run(["cat"], input="hello")

    assert captured["stdin"] is None
    assert captured["input"] == "hello"


def test_run_does_not_hang_on_stdin_reading_child() -> None:
    """End-to-end: a child that reads ALL of stdin returns promptly, not hangs.

    With stdin inherited from this (idle) test process the child would block; with
    the DEVNULL redirect it reads EOF immediately and exits. A real subprocess proves
    the regression is closed, not just the kwargs.
    """
    result = proc.run([sys.executable, "-c", "import sys; sys.stdin.read()"])
    assert result.returncode == 0
