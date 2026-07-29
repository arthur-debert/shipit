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
    assert "`shipit install --pr`" in message
    assert "`shipit install --local`" in message
    assert "pixi.lock" in message
    assert "cargo install" not in message
    assert "pip install" not in message
    assert "npm install" not in message


def test_only_the_missing_binary_cause_translates():
    for cause in (execrun.CAUSE_EXIT, execrun.CAUSE_TIMEOUT, execrun.CAUSE_OS):
        assert provisioning.missing_tool_remedy(("twine", "upload"), cause) is None


def test_an_unmanaged_head_stays_untranslated():
    assert (
        provisioning.missing_tool_remedy(("frobnicate",), execrun.CAUSE_MISSING_BINARY)
        is None
    )
    assert (
        provisioning.missing_tool_remedy(("cargo-edit",), execrun.CAUSE_MISSING_BINARY)
        is None
    )


def test_empty_argv_stays_untranslated():
    assert provisioning.missing_tool_remedy((), execrun.CAUSE_MISSING_BINARY) is None
