"""Adoption is SCOPED to the three boundaries — not a blanket print()->logging sweep.

OBS01-WS03 converts/adds logging at exactly the `gh`, `prstate`, and `review`
boundaries (the surfaces OBS02-04 need observable). The user-facing `print()`
sites in `verbs/*` are CLI output and are deliberately left alone. This test
pins that scope so a future blanket sweep is caught.
"""

from __future__ import annotations

import importlib
import logging


def test_three_boundaries_have_a_shipit_logger():
    # The gh boundary's per-call record moved to the one Exec runner
    # (PROC01-WS02 / ADR-0028): `shipit.gh` no longer logs itself — every
    # subprocess it runs is recorded by `shipit.execrun` on `shipit.exec`.
    for modname, expected in [
        ("shipit.execrun", "shipit.exec"),
        ("shipit.prstate.state", "shipit.prstate"),
        ("shipit.review.service", "shipit.review"),
        ("shipit.review.post", "shipit.review"),
    ]:
        mod = importlib.import_module(modname)
        assert isinstance(mod.logger, logging.Logger)
        assert mod.logger.name == expected


def test_out_of_scope_cli_verbs_were_not_swept():
    """The user-facing CLI verbs still write with bare `print()` and define no
    package logger — proof the adoption did not bleed past the three boundaries."""
    import inspect

    from shipit.verbs import gh_setup, install, lint

    for mod in (gh_setup, install, lint):
        assert not hasattr(mod, "logger"), (
            f"{mod.__name__} unexpectedly gained a logger"
        )
        src = inspect.getsource(mod)
        assert "print(" in src, (
            f"{mod.__name__} should still use print() for CLI output"
        )
        assert "import logging" not in src, f"{mod.__name__} should not import logging"
