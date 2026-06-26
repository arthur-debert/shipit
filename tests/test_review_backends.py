"""Tests for the review backends + the `timeout` knob threading (FLU01 #46).

The per-reviewer `timeout` mirrors the existing `model` knob end to end:
`get_backend(..., timeout=…)` forwards it to the backend constructor, `agy`
applies it as `--print-timeout=<N>s` (replacing the old hardcoded value), and
`codex` accepts it for interface parity. An unset timeout defaults to 600s.
"""

from __future__ import annotations

from shipit.review.backends import AgyBackend, CodexBackend, get_backend


def test_agy_default_timeout_is_600s():
    # Behaviour unchanged for anyone who doesn't set a timeout.
    argv = AgyBackend()._argv("/tmp/prompt.md")
    assert "--print-timeout=600s" in argv


def test_agy_applies_configured_timeout():
    argv = AgyBackend(timeout="900s")._argv("/tmp/prompt.md")
    assert "--print-timeout=900s" in argv
    assert "--print-timeout=600s" not in argv


def test_get_backend_forwards_timeout_to_agy():
    backend = get_backend("agy", model="pro", timeout="1200s")
    assert isinstance(backend, AgyBackend)
    assert backend.timeout == "1200s"
    assert "--print-timeout=1200s" in backend._argv("/tmp/p.md")


def test_get_backend_forwards_timeout_to_codex_for_parity():
    # codex accepts the timeout (shared run path passes it) even though its CLI
    # has no per-run timeout flag — it must not blow up on the kwarg.
    backend = get_backend("codex", model="pro", timeout="900s")
    assert isinstance(backend, CodexBackend)
    assert backend.timeout == "900s"
