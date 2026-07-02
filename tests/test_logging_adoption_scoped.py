"""Every narrating subsystem logs through a ``shipit.*`` package logger.

OBS01-WS03 scoped adoption to the three boundaries (`gh`, `prstate`, `review`)
and pinned the CLI verbs as print-only. LOG02 (the logging spray, ADR-0029)
retired that scope: the verbs' user-facing ``print()`` output is unchanged, but
anything that is the only record of an action now ALSO logs — so the pin flips:
every sprayed module must expose a ``shipit.*`` logger AND keep its prints.
"""

from __future__ import annotations

import importlib
import inspect
import logging


def test_sprayed_modules_have_a_shipit_logger():
    # The gh boundary's per-call record moved to the one Exec runner
    # (PROC01-WS02 / ADR-0028): `shipit.gh` no longer logs itself — every
    # subprocess it runs is recorded by `shipit.execrun` on `shipit.exec`.
    for modname, expected in [
        ("shipit.execrun", "shipit.exec"),
        ("shipit.prstate.state", "shipit.prstate"),
        ("shipit.review.service", "shipit.review"),
        ("shipit.review.post", "shipit.review"),
        # LOG02-WS04: the remaining print-only surfaces joined the spray.
        ("shipit.verbs.install", "shipit.install"),
        ("shipit.verbs.gh_setup", "shipit.ghsetup"),
        ("shipit.verbs.lint", "shipit.lint"),
        ("shipit.session.liveness", "shipit.session"),
    ]:
        mod = importlib.import_module(modname)
        assert isinstance(mod.logger, logging.Logger)
        assert mod.logger.name == expected


def test_verbs_keep_print_for_user_facing_output():
    """The spray is ADDITIVE: logging joined the verbs, but the user-facing CLI
    output still writes with ``print()`` — logging never replaced stdout."""
    from shipit.verbs import gh_setup, install, lint

    for mod in (gh_setup, install, lint):
        src = inspect.getsource(mod)
        assert "print(" in src, (
            f"{mod.__name__} should still use print() for CLI output"
        )
