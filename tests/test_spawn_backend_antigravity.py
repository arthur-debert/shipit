"""Unit tests for the ``antigravity`` (``agy``) backend adapter (ADR-0020 §Decision-per-backend).

The per-backend WRITE launch contract recorded by the WS00 spike — the exact
``agy --new-project --add-dir … --print`` argv (with the load-bearing ``--add-dir``
cwd-rooting quirk and ``--dangerously-skip-permissions``), the ``GEMINI_API_KEY`` /
``GOOGLE_API_KEY`` scrub, and the write-role prompt-prepend) plus the **reviewer**
posture: ``read_only=True`` under the reviewer role drops
``--dangerously-skip-permissions`` (read-only rides the chmod'd Tree, ADR-0020
§Decision 3) AND, since #989, selects AGY 1.1.2's native ``--agent reviewer`` def and
passes the task directly. The ``--agent`` capability probe / upgrade preflight
(:func:`supports_agent_flag` / :func:`require_agent_support`) is covered too — asserted
through the adapter and the injectable seam, mirroring ``test_spawn_backend_claude.py``.
No real ``agy`` binary is ever invoked.
"""

from __future__ import annotations

import pytest

from shipit.spawn import backends
from shipit.spawn.backends import antigravity as agy_backend

AGY = agy_backend.AntigravityAdapter()
TREE = "/trees/widget/TRE05-WS03"


def test_build_command_is_the_adr_write_contract():
    cmd = AGY.build_command("do the thing", "implementer", cwd=TREE)

    # The literal ADR-0020 §Decision-per-backend WRITE invocation, in order. The model
    # is the resolved verbatim agy name (alias `pro` -> capable, non-agentic model).
    assert cmd == [
        "agy",
        "--new-project",
        "--add-dir",
        TREE,
        "--model=Gemini 3.1 Pro (High)",
        "--print-timeout=600s",
        "--dangerously-skip-permissions",
        "--print",
        "You are acting as the 'implementer' role for this Run.\n\ndo the thing",
    ]


def test_build_command_roots_in_the_tree_via_add_dir():
    # The cwd-rooting invariant (ADR-0020 §Decision 3): agy IGNORES its process cwd, so
    # the Tree path must appear in the argv as `--add-dir <Tree>` or writes land in agy's
    # scratch dir. `--new-project` immediately precedes it (establishes the workspace).
    cmd = AGY.build_command("t", "implementer", cwd=TREE)
    add_dir = cmd.index("--add-dir")
    assert cmd[add_dir + 1] == TREE
    assert cmd[add_dir - 1] == "--new-project"


def test_build_command_requires_cwd_for_the_tree_root():
    # Fail-closed on the cwd-rooting invariant: without the Tree path agy would silently
    # write to its scratch dir, so a missing cwd is a loud error, never a degraded Run.
    with pytest.raises(ValueError, match="requires cwd"):
        AGY.build_command("t", "implementer")


def test_build_command_carries_bypass_permissions_for_a_write_run():
    # `--dangerously-skip-permissions` is agy's bypassPermissions equivalent (ADR-0020):
    # a non-interactive --print WRITE Run stalls on permission prompts without it.
    cmd = AGY.build_command("t", "implementer", cwd=TREE)
    assert "--dangerously-skip-permissions" in cmd


def test_reviewer_build_command_uses_direct_task_and_no_agent():
    # Reviewer posture (issue #1033): read_only + the reviewer role. We reverted the
    # `--agent reviewer` def since it goes agentic and fails. We now pass the review task
    # using prompt-prepend. It still OMITS --dangerously-skip-permissions (WS04a: agy runs the
    # network shell commands a reviewer needs without it, and read-only rides the chmod'd
    # Tree, ADR-0020 §Decision 3), and the --add-dir cwd-rooting / model / timeout are
    # unchanged.
    cmd = AGY.build_command("review it", "reviewer", cwd=TREE, read_only=True)
    assert "--dangerously-skip-permissions" not in cmd
    assert cmd == [
        "agy",
        "--new-project",
        "--add-dir",
        TREE,
        "--model=Gemini 3.1 Pro (High)",
        "--print-timeout=600s",
        "--print",
        "You are acting as the 'reviewer' role for this Run.\n\nreview it",
    ]


def test_reviewer_build_command_still_requires_cwd():
    # The cwd-rooting invariant holds for the reviewer too: agy reads the diff inside the
    # same --add-dir-rooted Tree, so a missing cwd is still a loud error.
    with pytest.raises(ValueError, match="requires cwd"):
        AGY.build_command("t", "reviewer", read_only=True)


def test_write_and_reviewer_argv_differ_by_posture():
    # The reviewer posture differs from the write posture in exactly one way:
    # it drops --dangerously-skip-permissions. The write run is unchanged.
    write = AGY.build_command("t", "reviewer", cwd=TREE)
    reviewer = AGY.build_command("t", "reviewer", cwd=TREE, read_only=True)
    assert write != reviewer
    assert "--dangerously-skip-permissions" in write
    assert "--agent" not in write
    assert write[-1] == "You are acting as the 'reviewer' role for this Run.\n\nt"
    assert reviewer[-1] == "You are acting as the 'reviewer' role for this Run.\n\nt"


def test_read_only_non_reviewer_role_keeps_prompt_prepend_and_no_agent():
    # `--agent reviewer` is gated on the REVIEWER role, not merely read_only: a read-only
    # run under any other role (e.g. an explorer) keeps the legacy prompt-prepend and
    # emits NO --agent flag, so the native-agent posture stays scoped to the reviewer.
    cmd = AGY.build_command("look around", "explorer", cwd=TREE, read_only=True)
    assert "--agent" not in cmd
    assert (
        cmd[-1] == "You are acting as the 'explorer' role for this Run.\n\nlook around"
    )


def test_build_command_prepends_the_role_natively_for_a_write_run():
    # A WRITE run has no native agent def, so the role rides in the --print text
    # (prompt-prepend, ADR-0020). The role name appears, and the task is preserved.
    cmd = AGY.build_command("implement #7", "shepherd", cwd=TREE)
    print_text = cmd[cmd.index("--print") + 1]
    assert "--agent" not in cmd
    assert (
        print_text
        == "You are acting as the 'shepherd' role for this Run.\n\nimplement #7"
    )
    assert "shepherd" in print_text
    assert print_text.endswith("implement #7")


def test_supports_agent_flag_probes_agy_help(monkeypatch):
    # The capability probe (#989) greps `agy --help` for `--agent`, so the reviewer
    # preflight is capability-direct, not a fragile version parse.
    from shipit import execrun

    calls = {}

    def fake_run(argv, **kwargs):
        calls["argv"] = argv
        return execrun.ExecResult(
            argv=tuple(argv),
            rc=0,
            stdout="  --agent <name>\n",
            stderr="",
            duration_ms=0,
        )

    monkeypatch.setattr(agy_backend.shutil, "which", lambda _b: "/usr/bin/agy")
    monkeypatch.setattr(agy_backend.execrun, "run", fake_run)
    assert agy_backend.supports_agent_flag() is True
    assert calls["argv"] == ["agy", "--help"]


def test_supports_agent_flag_false_when_binary_missing(monkeypatch):
    monkeypatch.setattr(agy_backend.shutil, "which", lambda _b: None)
    assert agy_backend.supports_agent_flag() is False


def test_supports_agent_flag_false_when_help_exits_nonzero(monkeypatch):
    # A NON-ZERO `agy --help` is not a trustworthy capability signal even if
    # `--agent` appears (e.g. in an error banner): only a clean rc-0 help counts,
    # so a failed probe reads as unsupported rather than a false positive that
    # would pass preflight and then fail mid-run when `--agent` is actually used.
    from shipit import execrun

    monkeypatch.setattr(agy_backend.shutil, "which", lambda _b: "/usr/bin/agy")
    monkeypatch.setattr(
        agy_backend.execrun,
        "run",
        lambda *a, **k: execrun.ExecResult(
            argv=("agy", "--help"),
            rc=2,
            stdout="",
            stderr="error: unknown flag; usage mentions --agent\n",
            duration_ms=0,
        ),
    )
    assert agy_backend.supports_agent_flag() is False


def test_build_command_never_emits_a_tools_flag():
    # agy has no native tool allow-list (the read-only signal is the read_only flag, not
    # a tool tuple): no --tools flag appears in either posture — read-only rides the
    # chmod'd Tree (ADR-0018).
    assert "--tools" not in AGY.build_command("t", "reviewer", cwd=TREE, read_only=True)
    assert "--tools" not in AGY.build_command("t", "implementer", cwd=TREE)


def test_build_command_honours_construction_model_and_timeout():
    # A consumer can pin a different model/timeout; both flow into the argv. A bare-name
    # model is passed verbatim (only aliases are resolved).
    adapter = agy_backend.AntigravityAdapter(
        model="Gemini 3.5 Flash (High)", timeout="900s"
    )
    cmd = adapter.build_command("t", "implementer", cwd=TREE)
    assert "--model=Gemini 3.5 Flash (High)" in cmd
    assert "--print-timeout=900s" in cmd


def test_default_model_resolves_pro_to_a_capable_non_agentic_name():
    # The default alias `pro` MUST NOT resolve to Flash (which goes agentic in --print
    # and never answers): it is pinned to the capable Gemini 3.1 Pro (High).
    assert AGY.model == "Gemini 3.1 Pro (High)"
    assert agy_backend.resolve_model("pro") == "Gemini 3.1 Pro (High)"
    # An already-verbatim name passes through untouched.
    assert agy_backend.resolve_model("Gemini 3.1 Pro (High)") == "Gemini 3.1 Pro (High)"


def test_child_env_scrubs_agy_auth_vars():
    parent = {
        "PATH": "/bin",
        "GEMINI_API_KEY": "stale",
        "GOOGLE_API_KEY": "also-stale",
        "HOME": "/home/a",
    }

    env = AGY.child_env(parent)

    # Both auth vars are gone so agy's Antigravity OAuth login wins; everything else stays.
    assert "GEMINI_API_KEY" not in env
    assert "GOOGLE_API_KEY" not in env
    assert env == {"PATH": "/bin", "HOME": "/home/a"}


def test_child_env_without_keys_is_a_plain_copy():
    parent = {"PATH": "/bin"}
    env = AGY.child_env(parent)
    assert env == {"PATH": "/bin"}
    assert env is not parent  # a copy, never the caller's dict


def test_child_env_defaults_to_os_environ_and_scrubs_it(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "from-os-environ")
    monkeypatch.setenv("GOOGLE_API_KEY", "from-os-environ")
    monkeypatch.setenv("SHIPIT_SPAWN_MARKER", "present")

    env = AGY.child_env()

    assert "GEMINI_API_KEY" not in env
    assert "GOOGLE_API_KEY" not in env
    assert env.get("SHIPIT_SPAWN_MARKER") == "present"


def test_registry_resolves_the_antigravity_adapter():
    adapter = backends.resolve("antigravity")
    assert isinstance(adapter, agy_backend.AntigravityAdapter)
    assert adapter.name == "antigravity"
    assert "antigravity" in backends.supported_backends()


def test_output_schema_path_is_accepted_and_ignored():
    # TRE05-WS04b: agy has no native --output-schema flag, so the seam's
    # output_schema_path is accepted (uniform signature) but never appears in the argv —
    # a capture reviewer's schema rides agy's prompt prose instead.
    cmd = AGY.build_command(
        "t", "reviewer", read_only=True, cwd="/tree", output_schema_path="/tmp/s.json"
    )
    assert "--output-schema" not in cmd
    assert "/tmp/s.json" not in cmd


def test_agy_has_no_reasoning_knob_and_reports_none():
    # RVW03-WS04 (#685): agy carries NO reasoning/effort flag (probed 1.1.1), so
    # by design the adapter takes no `reasoning` parameter and inherits the base
    # `reasoning = None` (see AntigravityAdapter's docstring). This pins the
    # OBSERVABLE contract — `reasoning is None` and no effort/reasoning knob in
    # argv, the record stamp for "unset", never an echoed config value the CLI
    # ignored — rather than the constructor signature, so it stays the arbiter
    # of what agy actually runs however the adapter is later refactored.
    from shipit.spawn.backends import antigravity as agy_backend

    adapter = agy_backend.AntigravityAdapter()
    assert adapter.reasoning is None
    cmd = adapter.build_command("task", "reviewer", read_only=True, cwd="/tree")
    assert not any("effort" in arg or "reasoning" in arg for arg in cmd)
