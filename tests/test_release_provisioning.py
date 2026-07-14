"""The missing-tool → reconcile-remedy map (#801, TOL02-WS17 holes 1–3). Pure.

The verb-level wiring — prepare's bump loop and publish's dispatch loop
translating a missing-binary ExecError into the ReleaseError this map
produces — is covered in ``test_release_verb.py`` / ``test_release_publish.py``
(the fails-when-absent tests the provisioning-guard registry names). Here:
the map's own contract, straight.
"""

import pytest

from shipit import execrun
from shipit.release import provisioning


@pytest.mark.parametrize(
    ("head", "block"),
    [
        ("cargo", "pixi.toml#shipit-rust-release-toolchain"),
        ("npm", "pixi.toml#shipit-node-deps"),
        ("twine", "pixi.toml#shipit-python-release-deps"),
    ],
)
def test_each_managed_tool_maps_to_its_block_reconcile(head, block):
    message = provisioning.missing_tool_remedy(
        (head, "whatever", "args"), execrun.CAUSE_MISSING_BINARY
    )
    assert message is not None
    assert f"`{head}`" in message
    assert f"`{block}`" in message
    # The #793 remedy contract: the COMMITTING install forms and the lock —
    # only --pr/--local regenerate and stage pixi.lock with the block.
    assert "`shipit install --pr`" in message
    assert "`shipit install --local`" in message
    assert "pixi.lock" in message
    # Never the superseded run-time-install shape (#795/#796, the #582 rule).
    assert "cargo install" not in message
    assert "pip install" not in message
    assert "npm install" not in message


def test_only_the_missing_binary_cause_translates():
    # A nonzero exit (the tool ran and failed), a timeout, or any other
    # OS-level launch failure is NOT the provisioning gap.
    for cause in (execrun.CAUSE_EXIT, execrun.CAUSE_TIMEOUT, execrun.CAUSE_OS):
        assert provisioning.missing_tool_remedy(("twine", "upload"), cause) is None


def test_an_unmanaged_head_stays_untranslated():
    assert (
        provisioning.missing_tool_remedy(("frobnicate",), execrun.CAUSE_MISSING_BINARY)
        is None
    )
    # Sub-tools that dispatch through a managed head do not match on their
    # own name: the map is keyed by argv HEAD (`cargo`, not `cargo-edit`).
    assert (
        provisioning.missing_tool_remedy(("cargo-edit",), execrun.CAUSE_MISSING_BINARY)
        is None
    )


def test_empty_argv_stays_untranslated():
    assert provisioning.missing_tool_remedy((), execrun.CAUSE_MISSING_BINARY) is None
