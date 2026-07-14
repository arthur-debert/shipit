from pathlib import Path

from shipit import logcontext
from shipit.pixienv import Activation
from shipit.session import bootstrap


def test_mint_session_id_is_recognizable_and_sortable():
    assert bootstrap.mint_session_id(now=1783585261, pid=4242) == (
        "codex-20260709-082101-4242"
    )


def test_codex_argv_roots_interactive_codex_in_tree_and_forwards_extra_args():
    tree = Path("/trees/arthur-debert/shipit/ephemeral/codex-1")

    argv = bootstrap.codex_argv(tree, ["--model", "gpt-5"])

    assert argv == [
        "codex",
        "--cd",
        str(tree),
        bootstrap.BYPASS_FLAG,
        "--model",
        "gpt-5",
    ]


def test_codex_resume_argv_re_roots_the_thread_in_the_tree():
    tree = Path("/trees/arthur-debert/shipit/ephemeral/codex-1")

    argv = bootstrap.codex_resume_argv(tree, "019f-thread", ["--model", "gpt-5"])

    assert argv == [
        "codex",
        "resume",
        "--cd",
        str(tree),
        bootstrap.BYPASS_FLAG,
        "019f-thread",
        "--model",
        "gpt-5",
    ]


def test_codex_env_scrubs_billing_and_project_pointers_keeps_access_token():
    tree = "/trees/shipit/ephemeral/codex-1"
    parent = {
        "PATH": "/bin",
        "OPENAI_API_KEY": "api-billed",
        "CODEX_API_KEY": "also-api-billed",
        "CODEX_ACCESS_TOKEN": "subscription-token",
        "PIXI_PROJECT_ROOT": "/source/checkout",
        "CONDA_PREFIX": "/source/.pixi/envs/default",
        logcontext.ENV_PREFIX + "SESSION": "stale",
    }

    env = bootstrap.codex_env(parent, session_id="codex-1", tree=tree)

    assert env["PATH"] == "/bin"
    assert env["CODEX_ACCESS_TOKEN"] == "subscription-token"
    assert "OPENAI_API_KEY" not in env
    assert "CODEX_API_KEY" not in env
    assert "PIXI_PROJECT_ROOT" not in env
    assert "CONDA_PREFIX" not in env
    assert env[logcontext.ENV_PREFIX + "SESSION"] == "codex-1"
    assert env[logcontext.ENV_PREFIX + "TREE"] == tree


def test_codex_env_applies_pixi_activation_after_scrubbing():
    tree = "/trees/shipit/ephemeral/codex-1"
    parent = {
        "PATH": "/source/.pixi/envs/default/bin:/bin",
        "PIXI_PROJECT_ROOT": "/source/checkout",
    }
    act = Activation(
        environment_variables={
            "PATH": f"{tree}/.pixi/envs/default/bin:/bin",
            "CONDA_PREFIX": f"{tree}/.pixi/envs/default",
        },
        activation_scripts=(),
    )

    env = bootstrap.codex_env(parent, session_id="codex-1", tree=tree, activation=act)

    assert env["PATH"] == f"{tree}/.pixi/envs/default/bin:/bin"
    assert env["CONDA_PREFIX"] == f"{tree}/.pixi/envs/default"
    assert "PIXI_PROJECT_ROOT" not in env


def test_codex_env_drops_all_inherited_log_context_exports():
    # A resumed coordinator may target a different repo/task than its invoking
    # shell, so no inherited correlation field may survive into the fresh Tree.
    parent = {
        "PATH": "/bin",
        **{
            logcontext.ENV_PREFIX + key.upper(): "stale"
            for key in logcontext.DOMAIN_KEYS
        },
    }

    env = bootstrap.codex_env(parent, session_id="codex-1", tree="/trees/codex-1")

    for key in logcontext.DOMAIN_KEYS:
        if key not in {"session", "tree"}:
            assert logcontext.ENV_PREFIX + key.upper() not in env
    assert env[logcontext.ENV_PREFIX + "SESSION"] == "codex-1"
    assert env[logcontext.ENV_PREFIX + "TREE"] == "/trees/codex-1"


def test_format_launch_names_session_tree_and_exact_argv():
    assert bootstrap.format_launch(
        "codex-1",
        "/trees/codex-1",
        ["codex", "--cd", "/trees/codex-1", "--model", "gpt-5"],
    ) == (
        "codex session codex-1\n"
        "tree /trees/codex-1\n"
        "exec codex --cd /trees/codex-1 --model gpt-5"
    )
