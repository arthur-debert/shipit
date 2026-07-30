import json
import logging
import os
import shutil
import stat
import subprocess
import tomllib
from dataclasses import replace as dc_replace
from pathlib import Path

import pytest
import yaml
from conftest import (
    LOCAL_BIN_PATH_LEG,
    PIXI_ABSENCE_GUARD,
    managed_bashguard_hook_command,
    managed_cc_hook_command,
    managed_pretooluse_hook_command,
)

from shipit import config, execrun, gh, git, pixienv
from shipit.channel import buckets
from shipit.execrun import ExecError
from shipit.identity import Sha
from shipit.install import apply as iapply
from shipit.install import reconcile as irec
from shipit.install import selfcert, splice
from shipit.install import units as iunits
from shipit.install.errors import InstallError, SelfCertError
from shipit.verbs import install as verb

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_STORE = REPO_ROOT / "src" / "shipit" / "data" / "skills"


def _exec_result(rc: int, stdout: str = "", stderr: str = "") -> execrun.ExecResult:
    return execrun.ExecResult(
        argv=("lefthook", "install"),
        rc=rc,
        stdout=stdout,
        stderr=stderr,
        duration_ms=1,
    )


def _plan(root) -> irec.Plan:
    units = iunits.load_units(platforms=verb._declared_platforms(Path(root)))
    retired = irec.load_retired()
    retired_hooks = irec.load_retired_hooks()
    state = irec.gather(Path(root), units, retired, retired_hooks)
    return irec.reconcile(units, retired, state, retired_hooks)


def _apply(root, mode: str = iapply.MODE_TREE, **kw) -> iapply.InstallResult:
    plan = _plan(root)
    assert not plan.nothing_to_do, "test drove apply on a no-op plan"
    return iapply.apply(
        plan,
        mode,
        pr_body=lambda before, hooks, rerendered, pin, debt: verb.format_pr_body(
            plan,
            before,
            hooks,
            rerendered=rerendered,
            stamped_version=pin,
            lint_debt=debt,
        ),
        **kw,
    )


def _apply_plan(plan, root, mode: str = iapply.MODE_TREE, **kw) -> iapply.InstallResult:
    return iapply.apply(
        plan,
        mode,
        pr_body=lambda before, hooks, rerendered, pin, debt: verb.format_pr_body(
            plan,
            before,
            hooks,
            rerendered=rerendered,
            stamped_version=pin,
            lint_debt=debt,
        ),
        **kw,
    )


def _cert_ok(plan, root, **kw) -> selfcert.CertReport:
    return selfcert.CertReport(checks=(selfcert.CertCheck(name="stub", ok=True),))


def test_decide_covers_four_cases():
    assert (
        irec.decide(consumer_hash=None, pristine_hash=None, desired_hash="d")
        == irec.ADD
    )
    assert (
        irec.decide(consumer_hash="d", pristine_hash="p", desired_hash="d") == irec.NOOP
    )
    assert (
        irec.decide(consumer_hash="p", pristine_hash="p", desired_hash="d")
        == irec.UPDATE
    )
    assert (
        irec.decide(consumer_hash="x", pristine_hash="p", desired_hash="d")
        == irec.OVERRIDE
    )
    assert (
        irec.decide(consumer_hash="x", pristine_hash=None, desired_hash="d")
        == irec.OVERRIDE
    )


def test_block_extract_and_splice_roundtrip():
    base = "# Consumer AGENTS\n\nSome consumer-owned text.\n"
    spliced = splice.splice_block(base, "managed body")
    assert iunits.BLOCK_OPEN in spliced and iunits.BLOCK_CLOSE in spliced
    assert "Some consumer-owned text." in spliced
    assert splice.extract_block(spliced) == "managed body"
    again = splice.splice_block(spliced, "new body")
    assert splice.extract_block(again) == "new body"
    assert again.count(iunits.BLOCK_OPEN) == 1
    assert "Some consumer-owned text." in again


def test_extract_block_absent_is_none():
    assert splice.extract_block("no markers here") is None


_STOCK_LINT_ENV = 'lint = ["lint", "shipit-lexd"]'
_LEXD = ("shipit-lexd",)


def _lint_features(text: str) -> list[str]:
    return tomllib.loads(text)["environments"]["lint"]


def test_splice_env_member_creates_the_env_when_absent():
    out = splice.splice_env_member(
        '[workspace]\nname = "a"\n', "lint", _STOCK_LINT_ENV, _LEXD
    )
    assert _lint_features(out) == ["lint", "shipit-lexd"]


def test_splice_env_member_creates_under_an_existing_environments_table():
    out = splice.splice_env_member(
        '[environments]\ndev = ["dev"]\n', "lint", _STOCK_LINT_ENV, _LEXD
    )
    assert tomllib.loads(out)["environments"] == {
        "dev": ["dev"],
        "lint": ["lint", "shipit-lexd"],
    }


def test_splice_env_member_appends_into_a_consumer_owned_env():
    out = splice.splice_env_member(
        '[environments]\nlint = ["lint", "extra"]  # mine\n',
        "lint",
        _STOCK_LINT_ENV,
        _LEXD,
    )
    assert _lint_features(out) == ["lint", "extra", "shipit-lexd"]
    assert "# mine" in out


def test_splice_env_member_is_idempotent_when_already_a_member():
    text = '[environments]\nlint = ["lint", "shipit-lexd"]\n'
    assert splice.splice_env_member(text, "lint", _STOCK_LINT_ENV, _LEXD) == text


def test_splice_env_member_handles_the_table_form_and_multiline_arrays():
    table = '[environments]\nlint = { features = ["lint"] }\n'
    merged = tomllib.loads(
        splice.splice_env_member(table, "lint", _STOCK_LINT_ENV, _LEXD)
    )
    assert merged["environments"]["lint"] == {"features": ["lint", "shipit-lexd"]}
    multiline = '[environments]\nlint = [\n  "lint",\n]\n'
    assert _lint_features(
        splice.splice_env_member(multiline, "lint", _STOCK_LINT_ENV, _LEXD)
    ) == ["lint", "shipit-lexd"]


def test_splice_env_member_preserves_a_malformed_manifest_verbatim():
    broken = "[environments\nlint = ["
    assert splice.splice_env_member(broken, "lint", _STOCK_LINT_ENV, _LEXD) == broken
    assert (
        splice.extract_env_member(broken, "lint", _LEXD) == splice.ENV_MEMBER_MALFORMED
    )


def test_extract_env_member_reads_membership_as_present_or_absent():
    present = '[environments]\nlint = ["lint", "shipit-lexd"]\n'
    assert splice.extract_env_member(present, "lint", _LEXD) == iunits.env_member_token(
        "lint", _LEXD
    )
    assert splice.extract_env_member("[workspace]\nname='a'\n", "lint", _LEXD) is None
    assert (
        splice.extract_env_member('[environments]\nlint = ["lint"]\n', "lint", _LEXD)
        is None
    )


def test_splice_env_member_table_form_preserves_every_sibling_key():
    text = (
        "[environments]\n"
        'lint = { features = ["lint"], solve-group = "shared", '
        "no-default-feature = true }\n"
    )
    spec = tomllib.loads(
        splice.splice_env_member(text, "lint", _STOCK_LINT_ENV, _LEXD)
    )["environments"]["lint"]
    assert spec["features"] == ["lint", "shipit-lexd"]
    assert spec["solve-group"] == "shared"
    assert spec["no-default-feature"] is True


def test_splice_env_member_finds_a_commented_environments_header():
    text = '[environments]  # my envs\nlint = ["lint"]\n'
    out = splice.splice_env_member(text, "lint", _STOCK_LINT_ENV, _LEXD)
    assert _lint_features(out) == ["lint", "shipit-lexd"]
    assert splice.extract_env_member(out, "lint", _LEXD) == iunits.env_member_token(
        "lint", _LEXD
    )


def test_splice_env_member_creates_under_a_commented_header_no_duplicate_table():
    text = '[environments]  # my envs\ndev = ["dev"]\n'
    out = splice.splice_env_member(text, "lint", _STOCK_LINT_ENV, _LEXD)
    assert out.count("[environments]") == 1
    assert tomllib.loads(out)["environments"] == {
        "dev": ["dev"],
        "lint": ["lint", "shipit-lexd"],
    }


def test_splice_env_member_table_form_ignores_a_dotted_features_subkey():
    text = '[environments]\nlint = { metadata.features = ["x"], features = ["lint"] }\n'
    spec = tomllib.loads(
        splice.splice_env_member(text, "lint", _STOCK_LINT_ENV, _LEXD)
    )["environments"]["lint"]
    assert spec["features"] == ["lint", "shipit-lexd"]
    assert spec["metadata"] == {"features": ["x"]}


def test_splice_env_member_table_form_ignores_features_inside_a_string_value():
    text = (
        '[environments]\nlint = { note = "features = [nope]", features = ["lint"] }\n'
    )
    spec = tomllib.loads(
        splice.splice_env_member(text, "lint", _STOCK_LINT_ENV, _LEXD)
    )["environments"]["lint"]
    assert spec["features"] == ["lint", "shipit-lexd"]
    assert spec["note"] == "features = [nope]"


def test_env_member_unsupported_form_surfaces_instead_of_looping():
    not_a_list = '[environments]\nlint = { features = "lint" }\n'
    assert (
        splice.extract_env_member(not_a_list, "lint", _LEXD)
        == splice.ENV_MEMBER_UNSUPPORTED
    )
    assert (
        splice.splice_env_member(not_a_list, "lint", _STOCK_LINT_ENV, _LEXD)
        == not_a_list
    )
    dotted = 'environments.lint = ["lint"]\n'
    assert (
        splice.extract_env_member(dotted, "lint", _LEXD)
        == splice.ENV_MEMBER_UNSUPPORTED
    )
    assert splice.splice_env_member(dotted, "lint", _STOCK_LINT_ENV, _LEXD) == dotted

    not_a_table = 'environments = "oops"\n'
    assert (
        splice.extract_env_member(not_a_table, "lint", _LEXD)
        == splice.ENV_MEMBER_UNSUPPORTED
    )
    assert (
        splice.splice_env_member(not_a_table, "lint", _STOCK_LINT_ENV, _LEXD)
        == not_a_table
    )

    dotted_sibling = 'environments.dev = ["dev"]\n'
    assert (
        splice.extract_env_member(dotted_sibling, "lint", _LEXD)
        == splice.ENV_MEMBER_UNSUPPORTED
    )
    out = splice.splice_env_member(dotted_sibling, "lint", _STOCK_LINT_ENV, _LEXD)
    assert out == dotted_sibling


def test_splice_env_member_never_emits_invalid_toml_for_exotic_forms():
    cases = [
        '[environments]\nlint = ["lint"]\n',
        '[environments]  # c\nlint = ["lint", "extra"]\n',
        '[environments]\nlint = { features = ["lint"], solve-group = "s" }\n',
        '[environments]\nlint = { metadata.features = ["x"], features = ["lint"] }\n',
        '[environments]\nlint = [\n  "lint",\n]\n',
        '[environments]\nlint = { features = "lint" }\n',
        'environments.lint = ["lint"]\n',
        'environments = "oops"\n',
        "",
        '[workspace]\nname = "a"\n',
    ]
    for text in cases:
        out = splice.splice_env_member(text, "lint", _STOCK_LINT_ENV, _LEXD)
        parsed = tomllib.loads(out)
        if out != text:
            lint_env = parsed["environments"]["lint"]
            features = lint_env["features"] if isinstance(lint_env, dict) else lint_env
            assert "shipit-lexd" in features
            assert splice.extract_env_member(
                out, "lint", _LEXD
            ) == iunits.env_member_token("lint", _LEXD)


def test_load_units_includes_lefthook_and_pixi_task_block():
    units = {u.key: u for u in iunits.load_units()}
    assert iunits.LEFTHOOK_FILE in units
    assert units[iunits.LEFTHOOK_FILE].kind == "file"

    pixi = units[iunits.PIXI_KEY]
    assert pixi.kind == "block"
    assert pixi.dest == "pixi.toml"
    assert pixi.anchor == "[tasks]"
    assert pixi.desired_inner() == (
        'changelog = "./bin/shipit changelog"\nlogs = "./bin/shipit logs"'
    )


def test_load_units_anchors_the_lint_task_in_the_lint_feature():
    units = {u.key: u for u in iunits.load_units()}
    lint_task = units[iunits.PIXI_LINT_TASK_KEY]
    assert lint_task.kind == "block"
    assert lint_task.dest == "pixi.toml"
    assert lint_task.anchor == "[feature.lint.tasks]"
    assert lint_task.desired_inner() == 'lint = "./bin/shipit lint"'
    tasks_blocks = [
        u
        for u in units.values()
        if u.dest == iunits.PIXI_FILE
        and u.anchor in ("[tasks]", "[feature.lint.tasks]")
    ]
    defining = [u for u in tasks_blocks if "lint" in tomllib.loads(u.desired_inner())]
    assert [u.key for u in defining] == [iunits.PIXI_LINT_TASK_KEY]


def test_reconcile_migrates_the_lint_task_out_of_the_hostile_default_env(tmp_path):
    from shipit.tools import lanes as lanes_mod

    legacy_inner = (
        'changelog = "./bin/shipit changelog"\n'
        'lint = "./bin/shipit lint"\n'
        'logs = "./bin/shipit logs"'
    )
    (tmp_path / "pixi.toml").write_text(
        '[workspace]\nname = "acme"\nchannels = ["conda-forge"]\n'
        'platforms = ["linux-64"]\n\n'
        "[dependencies]\n"
        'prettier = "3.9.*"\n\n'
        "[tasks]\n"
        f"{iunits.PIXI_OPEN}\n{legacy_inner}\n{iunits.PIXI_CLOSE}\n",
        encoding="utf-8",
    )
    (tmp_path / config.CONFIG_NAME).write_text(
        "[managed]\n"
        f'"{iunits.PIXI_KEY}" = '
        f'"{config.content_hash(legacy_inner.encode("utf-8"))}"\n',
        encoding="utf-8",
    )

    units = iunits.load_units(platforms=frozenset({"linux-64"}))
    retired = irec.load_retired()
    plan = irec.reconcile(units, retired, irec.gather(tmp_path, units, retired))
    by_key = {d.unit.key: d for d in plan.decisions}
    assert by_key[iunits.PIXI_KEY].action == irec.UPDATE
    assert by_key[iunits.PIXI_LINT_TASK_KEY].action == irec.ADD

    iapply.apply(plan, iapply.MODE_TREE)

    manifest = tomllib.loads((tmp_path / "pixi.toml").read_text(encoding="utf-8"))
    assert "lint" not in manifest["tasks"]
    assert manifest["feature"]["lint"]["tasks"]["lint"] == "./bin/shipit lint"
    assert lanes_mod.task_env_sets(manifest)["lint"] == ("lint",)
    assert manifest["feature"]["lint"]["dependencies"]["prettier"] == "3.8.*"
    assert manifest["dependencies"]["prettier"] == "3.9.*"


def test_load_units_includes_the_thin_test_task_block():
    units = {u.key: u for u in iunits.load_units()}
    test_task = units[iunits.PIXI_TEST_TASK_KEY]
    assert test_task.kind == "block"
    assert test_task.dest == "pixi.toml"
    assert test_task.anchor == "[tasks]"
    assert test_task.desired_inner() == 'test = "./bin/shipit test"'


def test_pixi_block_inserts_under_existing_tasks_table():
    consumer = '[project]\nname = "acme"\n\n[tasks]\ntest = "pytest"\n'
    out = splice.splice_block(
        consumer,
        'lint = "shipit lint"',
        iunits.PIXI_OPEN,
        iunits.PIXI_CLOSE,
        anchor="[tasks]",
    )
    tasks_idx = out.index("[tasks]")
    project_after = out.find("[project]", tasks_idx)
    lint_idx = out.index('lint = "shipit lint"')
    assert tasks_idx < lint_idx
    assert project_after == -1
    assert 'test = "pytest"' in out
    assert (
        splice.extract_block(out, iunits.PIXI_OPEN, iunits.PIXI_CLOSE)
        == 'lint = "shipit lint"'
    )


def test_pixi_block_creates_tasks_table_when_absent():
    consumer = '[project]\nname = "acme"\n'
    out = splice.splice_block(
        consumer,
        'lint = "shipit lint"',
        iunits.PIXI_OPEN,
        iunits.PIXI_CLOSE,
        anchor="[tasks]",
    )
    assert "[tasks]" in out
    assert out.index("[tasks]") < out.index('lint = "shipit lint"')


def test_pixi_block_reinstall_replaces_in_place():
    consumer = '[tasks]\ntest = "pytest"\n'
    once = splice.splice_block(
        consumer,
        'lint = "shipit lint"',
        iunits.PIXI_OPEN,
        iunits.PIXI_CLOSE,
        "[tasks]",
    )
    twice = splice.splice_block(
        once, 'lint = "shipit lint"', iunits.PIXI_OPEN, iunits.PIXI_CLOSE, "[tasks]"
    )
    assert twice.count(iunits.PIXI_OPEN) == 1
    assert twice == once


LINT_TOOLS = (
    "ruff",
    "shellcheck",
    "go-shfmt",
    "yamllint",
    "prettier",
    "markdownlint-cli",
    "actionlint",
    "lefthook",
)

LEXD_SERVED_SUBDIRS = buckets.SERVED_SUBDIRS
_LEXD_PIN = {"lexd": iunits.LEXD_PIN}


def _lexd_targets(platforms) -> dict:
    want = set(platforms)
    return {
        sub: {"dependencies": _LEXD_PIN}
        for sub in buckets.SERVED_SUBDIRS
        if sub in want
    }


LEXD_SCOPED_TARGET = _lexd_targets(buckets.SERVED_SUBDIRS)


def test_load_units_includes_the_lint_env_blocks():
    units = {u.key: u for u in iunits.load_units()}

    deps = units[iunits.PIXI_LINT_DEPS_KEY]
    assert deps.kind == "block"
    assert deps.dest == "pixi.toml"
    assert deps.anchor == "[feature.lint.dependencies]"
    assert set(tomllib.loads(deps.desired_inner())) == set(LINT_TOOLS)

    envs = units[iunits.PIXI_ENVS_KEY]
    assert envs.kind == "block"
    assert envs.dest == "pixi.toml"
    assert envs.anchor == "[environments]"
    assert tomllib.loads(envs.desired_inner()) == {"lint": ["lint", "shipit-lexd"]}

    fences = {
        units[k].open_marker
        for k in (
            iunits.PIXI_KEY,
            iunits.PIXI_TEST_TASK_KEY,
            iunits.PIXI_LINT_DEPS_KEY,
            iunits.PIXI_ENVS_KEY,
            iunits.PIXI_LAUNCHER_DEPS_KEY,
            iunits.PIXI_LEXD_KEY,
        )
    }
    assert len(fences) == 6


def test_load_units_includes_the_managed_lexd_feature_block():
    units = {u.key: u for u in iunits.load_units()}
    lexd = units[iunits.PIXI_LEXD_KEY]
    assert lexd.kind == "block"
    assert lexd.dest == "pixi.toml"
    assert lexd.anchor is None
    block = tomllib.loads(lexd.desired_inner())
    feature = block["feature"]["shipit-lexd"]
    assert feature["channels"] == [
        "https://storage.googleapis.com/shipit-artifacts-public/lex-fmt/lex"
    ]
    assert "dependencies" not in feature
    assert feature["target"] == _lexd_targets(iunits.PIXI_SEED_PLATFORMS)
    assert "win-64" not in feature["target"]


def test_lexd_block_targets_are_repo_platforms_intersect_served():
    def _targets(platforms):
        block = tomllib.loads(iunits.lexd_block(frozenset(platforms)))
        return block["feature"]["shipit-lexd"]["target"]

    no_win = _targets({"linux-64", "osx-arm64", "linux-aarch64"})
    assert "win-64" not in no_win
    assert set(no_win) == {"linux-64", "osx-arm64", "linux-aarch64"}

    with_win = _targets({"linux-64", "win-64"})
    assert set(with_win) == {"linux-64", "win-64"}
    assert with_win["win-64"] == {"dependencies": {"lexd": iunits.LEXD_PIN}}

    unserved = _targets({"osx-64", "osx-arm64"})
    assert set(unserved) == {"osx-arm64"}

    assert set(_targets(buckets.SERVED_SUBDIRS)) == set(buckets.SERVED_SUBDIRS)

    assert (
        "dependencies"
        not in tomllib.loads(iunits.lexd_block(frozenset({"linux-64"})))["feature"][
            "shipit-lexd"
        ]
    )


def test_load_units_includes_the_launcher_deps_block():
    units = {u.key: u for u in iunits.load_units()}
    launcher = units[iunits.PIXI_LAUNCHER_DEPS_KEY]
    assert launcher.kind == "block"
    assert launcher.dest == "pixi.toml"
    assert launcher.anchor == "[dependencies]"
    assert set(tomllib.loads(launcher.desired_inner())) == {"uv"}


def test_launcher_deps_uv_pin_agrees_with_layer0_uv_pin():
    script = iunits.data_bytes("bootstrap", "setup-dev-env.sh").decode("utf-8")
    uv_pin = next(
        line.split('"')[1] for line in script.splitlines() if line.startswith("UV_PIN=")
    )
    block = tomllib.loads(
        iunits.data_bytes("pixi-launcher-deps-block.toml").decode("utf-8")
    )
    major, minor, *_ = uv_pin.split(".")
    assert block["uv"] == f"{major}.{minor}.*", (
        f"managed uv spec {block['uv']!r} is not the minor line of "
        f"Layer 0's UV_PIN {uv_pin!r}"
    )


def test_packaged_lint_env_agrees_with_shipits_own_manifest():
    own = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pixi.toml").read_text(encoding="utf-8")
    )
    deps = tomllib.loads(iunits.data_bytes("pixi-lint-deps-block.toml").decode("utf-8"))

    assert set(deps) == set(LINT_TOOLS)
    for tool, pin in deps.items():
        assert own["dependencies"].get(tool) == pin, (
            f"{tool}: packaged pin {pin!r} != shipit's own {own['dependencies'].get(tool)!r}"
        )
    assert own["feature"]["lint"]["dependencies"] == deps

    envs = tomllib.loads(iunits.data_bytes("pixi-lint-env-block.toml").decode("utf-8"))
    assert envs == {"lint": ["lint", "shipit-lexd"]}
    assert own["environments"]["lint"] == envs["lint"]

    own_platforms = frozenset(own["workspace"]["platforms"])
    generated = tomllib.loads(iunits.lexd_block(own_platforms))
    assert own["feature"]["shipit-lexd"] == generated["feature"]["shipit-lexd"]
    assert "win-64" not in own["feature"]["shipit-lexd"]["target"]
    assert all(
        t["dependencies"]["lexd"] == iunits.LEXD_PIN
        for t in own["feature"]["shipit-lexd"]["target"].values()
    )


def test_shipits_own_pixi_manifest_reconciles_to_noop():
    root = Path(__file__).resolve().parents[1]
    units = {
        u.key: u
        for u in iunits.load_units(
            toolchains=frozenset({iunits.TOOLCHAIN_PYTHON}),
            platforms=verb._declared_platforms(root),
        )
    }
    for key in (
        iunits.PIXI_KEY,
        iunits.PIXI_LINT_TASK_KEY,
        iunits.PIXI_LINT_DEPS_KEY,
        iunits.PIXI_ENVS_KEY,
        iunits.PIXI_LAUNCHER_DEPS_KEY,
        iunits.PIXI_PYTHON_RELEASE_DEPS_KEY,
        iunits.PIXI_LEXD_KEY,
    ):
        unit = units[key]
        assert irec.consumer_hash(root, unit) == unit.desired_hash(), key


def _managed_lefthook() -> dict:
    return yaml.safe_load(iunits.data_bytes("lefthook.yml"))


def test_managed_lefthook_is_consumer_generic():
    cfg = _managed_lefthook()
    assert set(cfg) == {"pre-commit", "pre-push", "post-commit"}
    for hook in cfg.values():
        for cmd in hook["commands"].values():
            run = cmd["run"]
            assert run.startswith(PIXI_ABSENCE_GUARD)
            assert "exit 0" in run
            pixi_part = run[len(PIXI_ABSENCE_GUARD) :]
            assert pixi_part.startswith("pixi run -e lint ")
            invoked = pixi_part.removeprefix("pixi run -e lint ").split()[0]
            assert invoked in ("lint", "./bin/shipit")
            assert invoked != "shipit"
            assert "tools/" not in run and ".lex" not in run

    lint = cfg["pre-commit"]["commands"]["lint"]
    assert lint == {"priority": 2, "run": PIXI_ABSENCE_GUARD + "pixi run -e lint lint"}
    assert (
        cfg["pre-push"]["commands"]["lint"]["run"]
        == PIXI_ABSENCE_GUARD + "pixi run -e lint lint"
    )
    assert "classify-gate" not in cfg["pre-push"]["commands"]

    tasks = tomllib.loads(
        iunits.data_bytes("pixi-lint-task-block.toml").decode("utf-8")
    )
    assert "lint" in tasks
    envs = tomllib.loads(iunits.data_bytes("pixi-lint-env-block.toml").decode("utf-8"))
    assert "lint" in envs


def test_shipits_own_lefthook_reconciles_to_noop():
    root = Path(__file__).resolve().parents[1]
    unit = {u.key: u for u in iunits.load_units()}[iunits.LEFTHOOK_FILE]
    assert irec.consumer_hash(root, unit) == unit.desired_hash()


def test_shipits_own_local_config_carries_the_lex_mirror_leg():
    root = Path(__file__).resolve().parents[1]
    local = yaml.safe_load((root / "lefthook-local.yml").read_text(encoding="utf-8"))
    leg = local["pre-commit"]["commands"]["lex-mirror"]
    assert "tools/lex-convert-doc.sh" in leg["run"]
    assert (root / "tools" / "lex-convert-doc.sh").is_file()
    managed = _managed_lefthook()
    assert leg["priority"] < managed["pre-commit"]["commands"]["lint"]["priority"]


def test_lefthook_unit_reconciles_add_noop_override(tmp_path, rec):

    def decision():
        return next(
            d for d in _plan(tmp_path).decisions if d.unit.key == iunits.LEFTHOOK_FILE
        )

    assert decision().action == irec.ADD
    _apply(tmp_path)
    assert (tmp_path / "lefthook.yml").read_bytes() == iunits.data_bytes("lefthook.yml")
    assert decision().action == irec.NOOP
    (tmp_path / "lefthook.yml").write_text("pre-commit: {}\n")
    assert decision().action == irec.OVERRIDE


OLD_PIPED_MANAGED = "pre-commit:\n  piped: true\n  commands:\n    lint:\n      run: x\n"
PARALLEL_LOCAL = "pre-commit:\n  parallel: true\n  commands:\n    leg:\n      run: y\n"


def test_managed_lefthook_sets_no_hook_level_execution_options():
    cfg = _managed_lefthook()
    for hook, body in cfg.items():
        for option in irec.EXCLUSIVE_HOOK_OPTIONS:
            assert option not in body, f"{hook} sets hook-level {option!r} (#544)"
    assert cfg["pre-commit"]["commands"]["lint"]["priority"] == 2


def test_detect_lefthook_conflicts_flags_piped_vs_parallel():
    conflicts = irec.detect_lefthook_conflicts(
        OLD_PIPED_MANAGED, PARALLEL_LOCAL, "lefthook-local.yml"
    )
    assert [(c.hook, c.managed_options, c.local_options) for c in conflicts] == [
        ("pre-commit", ("piped",), ("parallel",))
    ]
    message = irec.format_lefthook_conflict(conflicts[0])
    assert "'piped: true'" in message and "'parallel: true'" in message
    assert "lefthook-local.yml" in message and iunits.LEFTHOOK_FILE in message
    assert "shipit install" in message


def test_detect_lefthook_conflicts_local_value_wins_in_the_merge():
    defused = (
        "pre-commit:\n  piped: false\n  parallel: true\n"
        "  commands:\n    leg:\n      run: y\n"
    )
    assert (
        irec.detect_lefthook_conflicts(OLD_PIPED_MANAGED, defused, "lefthook-local.yml")
        == ()
    )
    plain = "pre-commit:\n  commands:\n    leg:\n      run: y\n"
    assert (
        irec.detect_lefthook_conflicts(OLD_PIPED_MANAGED, plain, "lefthook-local.yml")
        == ()
    )


def test_detect_lefthook_conflicts_is_scoped_to_what_install_can_cause():
    local_self_conflict = (
        "pre-commit:\n  piped: true\n  parallel: true\n"
        "  commands:\n    leg:\n      run: y\n"
    )
    for managed in (
        "pre-commit:\n  commands:\n    lint:\n      run: x\n",
        OLD_PIPED_MANAGED,
    ):
        assert (
            irec.detect_lefthook_conflicts(
                managed, local_self_conflict, "lefthook-local.yml"
            )
            == ()
        )


def test_detect_lefthook_conflicts_tolerates_unreadable_local_config():
    for bad in ("{unclosed", "- a\n- b\n", "just a scalar\n", ""):
        assert (
            irec.detect_lefthook_conflicts(OLD_PIPED_MANAGED, bad, "lefthook-local.yml")
            == ()
        )


def test_format_lefthook_conflict_when_managed_side_sets_both(tmp_path):
    managed_both = (
        "pre-commit:\n  piped: true\n  parallel: true\n"
        "  commands:\n    lint:\n      run: x\n"
    )
    local_defines_hook = "pre-commit:\n  commands:\n    leg:\n      run: y\n"
    conflicts = irec.detect_lefthook_conflicts(
        managed_both, local_defines_hook, "lefthook-local.yml"
    )
    assert len(conflicts) == 1 and conflicts[0].local_options == ()
    message = irec.format_lefthook_conflict(conflicts[0])
    assert "'piped: true'" in message and "'parallel: true'" in message
    assert "managed-config defect" in message and "shipit install" in message
    assert "Remove the option from" not in message


def test_read_lefthook_local_fails_open_on_oserror(tmp_path, monkeypatch):
    (tmp_path / "lefthook-local.yml").write_text(PARALLEL_LOCAL)
    real_read_text = Path.read_text

    def boom(self, *args, **kwargs):
        if self.name in irec.LEFTHOOK_LOCAL_FILES:
            raise PermissionError("permission denied")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", boom)
    assert irec._read_lefthook_local(tmp_path) == (None, None)
    state = irec.gather(tmp_path, iunits.load_units(), [])
    assert state.lefthook_local is None and state.lefthook_local_path is None
    assert _plan(tmp_path).lefthook_conflicts == ()


def test_gather_reads_the_consumer_lefthook_local_config(tmp_path):
    units = iunits.load_units()
    state = irec.gather(tmp_path, units, [])
    assert state.lefthook_local is None and state.lefthook_local_path is None
    (tmp_path / "lefthook-local.yml").write_text(PARALLEL_LOCAL)
    state = irec.gather(tmp_path, units, [])
    assert state.lefthook_local_path == "lefthook-local.yml"
    assert state.lefthook_local == PARALLEL_LOCAL


def test_parallel_local_config_reconciles_clean_against_current_managed_caller(
    tmp_path,
):
    (tmp_path / "lefthook-local.yml").write_text(PARALLEL_LOCAL)
    plan = _plan(tmp_path)
    assert plan.lefthook_conflicts == ()
    assert verb.format_plan_warnings(plan) == ""


def test_lefthook_conflict_warns_in_tree_mode_and_fails_committing_modes_closed(
    tmp_path, rec
):
    conflict = irec.detect_lefthook_conflicts(
        OLD_PIPED_MANAGED, PARALLEL_LOCAL, "lefthook-local.yml"
    )[0]
    plan = dc_replace(_plan(tmp_path), lefthook_conflicts=(conflict,))

    assert irec.format_lefthook_conflict(conflict) in verb.format_plan_warnings(plan)

    for mode in (iapply.MODE_LOCAL, iapply.MODE_PUSH, iapply.MODE_PR):
        with pytest.raises(InstallError, match="lefthook config conflict"):
            iapply.apply(plan, mode, pr_body=lambda *a: "")
    assert rec.calls == [] and rec.hook_activations == []
    assert not (tmp_path / ".shipit.toml").exists()
    assert not (tmp_path / "lefthook.yml").exists()

    result = iapply.apply(plan, iapply.MODE_TREE)
    assert result.mode == iapply.MODE_TREE
    assert (tmp_path / "lefthook.yml").is_file()


def test_conflict_bearing_noop_plan_still_fails_committing_modes(tmp_path, monkeypatch):
    conflict = irec.detect_lefthook_conflicts(
        OLD_PIPED_MANAGED, PARALLEL_LOCAL, "lefthook-local.yml"
    )[0]
    noop_conflict = dc_replace(
        _plan(tmp_path),
        decisions=(),
        retired=(),
        seeds=(),
        current_pin=None,
        target_pin=None,
        lefthook_conflicts=(conflict,),
        claude_skills_link=irec.ClaudeSkillsLink(irec.LINK_NOOP),
    )
    assert noop_conflict.nothing_to_do
    monkeypatch.setattr(verb, "reconcile", lambda *a, **k: noop_conflict)

    assert verb.run(str(tmp_path), local=True) == 1
    assert verb.run(str(tmp_path), push=True) == 1
    assert verb.run(str(tmp_path), pr=True) == 1
    assert not (tmp_path / "lefthook.yml").exists()
    assert not (tmp_path / ".shipit.toml").exists()

    assert verb.run(str(tmp_path), local=True, dry_run=True) == 0
    assert verb.run(str(tmp_path)) == 0


def test_load_units_includes_the_lint_tool_configs():
    units = {u.key: u for u in iunits.load_units()}
    for dest, data_file in iunits.LINT_CONFIG_UNITS:
        unit = units[dest]
        assert unit.kind == "file"
        assert unit.dest == dest
        assert unit.content == iunits.data_bytes(data_file)


def test_managed_markdownlint_config_relaxes_the_changelog_genre_rules():
    cfg = yaml.safe_load(iunits.data_bytes("markdownlint.yaml"))
    assert cfg == {
        "default": True,
        "MD013": False,
        "MD041": False,
        "MD024": {"siblings_only": True},
        "MD033": False,
    }


def test_managed_yamllint_config_extends_default_with_three_relaxations():
    cfg = yaml.safe_load(iunits.data_bytes("yamllint.yaml"))
    assert cfg == {
        "extends": "default",
        "rules": {
            "document-start": "disable",
            "truthy": {"check-keys": False},
            "line-length": {"max": 120},
        },
    }


def test_managed_markdownlintignore_covers_managed_paths_and_testdata():
    from shipit import lint

    entries = [
        line
        for line in iunits.data_bytes("markdownlintignore").decode().splitlines()
        if line and not line.startswith("#")
    ]
    assert entries == ["AGENTS.md", *lint.PROTECTED_TESTDATA_GLOBS]


def test_shipits_own_lint_configs_reconcile_to_noop():
    root = Path(__file__).resolve().parents[1]
    units = {u.key: u for u in iunits.load_units()}
    for dest, _ in iunits.LINT_CONFIG_UNITS:
        unit = units[dest]
        assert irec.consumer_hash(root, unit) == unit.desired_hash(), dest


def test_lint_config_units_reconcile_add_noop_override(tmp_path, rec):
    keys = {dest for dest, _ in iunits.LINT_CONFIG_UNITS}

    def actions():
        return {
            d.unit.key: d.action
            for d in _plan(tmp_path).decisions
            if d.unit.key in keys
        }

    assert set(actions().values()) == {irec.ADD}
    _apply(tmp_path)
    for dest, data_file in iunits.LINT_CONFIG_UNITS:
        assert (tmp_path / dest).read_bytes() == iunits.data_bytes(data_file)
    assert set(actions().values()) == {irec.NOOP}
    (tmp_path / iunits.YAMLLINT_FILE).write_text("extends: relaxed\n")
    assert actions()[iunits.YAMLLINT_FILE] == irec.OVERRIDE
    assert actions()[iunits.MARKDOWNLINT_FILE] == irec.NOOP


def test_load_units_includes_the_gitignore_release_block():
    unit = {u.key: u for u in iunits.load_units()}[iunits.GITIGNORE_KEY]
    assert unit.kind == "block"
    assert unit.dest == iunits.GITIGNORE_FILE == ".gitignore"
    assert unit.open_marker == iunits.GITIGNORE_OPEN
    assert unit.close_marker == iunits.GITIGNORE_CLOSE
    assert unit.anchor is None
    assert unit.content == iunits.data_bytes("gitignore-block")


def test_managed_gitignore_block_covers_the_release_stage_outputs():
    entries = [
        line
        for line in iunits.data_bytes("gitignore-block").decode().splitlines()
        if line and not line.startswith("#")
    ]
    assert entries == ["/RELEASE_NOTES.md", "/dist/", "/dist-signed/"]


def test_gitignore_block_add_noop_override_and_preserves_consumer_entries(
    tmp_path, rec
):

    def action():
        return next(
            d.action
            for d in _plan(tmp_path).decisions
            if d.unit.key == iunits.GITIGNORE_KEY
        )

    (tmp_path / ".gitignore").write_text("# consumer\nnode_modules/\n")
    assert action() == irec.ADD
    _apply(tmp_path)
    body = (tmp_path / ".gitignore").read_text()
    assert "node_modules/" in body
    assert iunits.GITIGNORE_OPEN in body and "/dist-signed/" in body
    assert action() == irec.NOOP
    edited = body.replace("/dist-signed/", "/dist-signed/\ncoverage/")
    (tmp_path / ".gitignore").write_text(edited)
    assert action() == irec.OVERRIDE


def test_gitignore_block_creates_the_file_when_the_consumer_has_none(tmp_path, rec):
    assert not (tmp_path / ".gitignore").exists()
    _apply(tmp_path)
    body = (tmp_path / ".gitignore").read_text()
    assert iunits.GITIGNORE_OPEN in body
    assert splice.extract_block(
        body, iunits.GITIGNORE_OPEN, iunits.GITIGNORE_CLOSE
    ) == iunits.data_bytes("gitignore-block").decode().strip("\n")


def test_shipits_own_gitignore_reconciles_to_noop():
    root = Path(__file__).resolve().parents[1]
    unit = {u.key: u for u in iunits.load_units()}[iunits.GITIGNORE_KEY]
    assert irec.consumer_hash(root, unit) == unit.desired_hash()


def test_load_units_has_skills_agents_and_bootstrap():
    units = iunits.load_units()
    keys = {u.key for u in units}
    assert "AGENTS.md#shipit-block" in keys
    assert "bin/shipit" in keys
    assert any(k.startswith(".agents/skills/") for k in keys)
    assert not any(k.startswith(".shipit-skills/") for k in keys)
    assert not any(k.startswith(".claude/skills/") for k in keys)
    agents = next(u for u in units if u.key == "AGENTS.md#shipit-block")
    assert agents.kind == "block"
    boot = next(u for u in units if u.key == "bin/shipit")
    assert boot.executable is True


def test_every_store_skill_is_emitted_once_into_agents_skills():
    file_units = [u for u in iunits.load_units() if u.kind == "file"]
    keys = [u.key for u in file_units]
    assert len(keys) == len(set(keys)), "colliding managed keys among file units"
    units = {u.key: u for u in file_units}
    store = list(iunits.walk_files(iunits.skills_root()))
    assert store, "the fundamental skill store is empty"
    for rel, content in store:
        agents_key = f"{iunits.AGENTS_SKILLS_DIR}/{rel}"
        assert agents_key in units, f"{agents_key} not emitted"
        assert units[agents_key].content == content
        assert units[agents_key].dest == agents_key
        assert f"{iunits.CLAUDE_SKILLS_DIR}/{rel}" not in units
    assert not any(k.startswith(".shipit-skills/") for k in units)
    assert not any(k.startswith(f"{iunits.CLAUDE_SKILLS_DIR}/") for k in units)


def test_claude_skills_link_planned_and_created_for_a_fresh_consumer(tmp_path, rec):
    plan = _plan(tmp_path)
    assert plan.claude_skills_link.action == irec.LINK_CREATE
    assert plan.claude_skills_link.is_work
    _apply(tmp_path)
    link = tmp_path / iunits.CLAUDE_SKILLS_DIR
    assert link.is_symlink()
    assert str(link.readlink()) == iunits.CLAUDE_SKILLS_LINK_TARGET
    assert (tmp_path / iunits.AGENTS_SKILLS_DIR / "to-spec" / "SKILL.md").is_file()
    assert (link / "to-spec" / "SKILL.md").is_file()


def test_install_blocks_and_preserves_an_existing_real_claude_skills_dir(tmp_path, rec):
    real = tmp_path / ".claude" / "skills"
    shutil.copytree(SKILL_STORE, real)
    assert real.is_dir() and not real.is_symlink()

    plan = _plan(tmp_path)
    assert plan.claude_skills_link.action == irec.LINK_BLOCKED
    assert not plan.claude_skills_link.is_work
    warning = verb.format_plan_warnings(plan)
    assert "claude skills link" in warning
    assert "will not remove it" in warning

    _apply(tmp_path)
    assert real.is_dir() and not real.is_symlink()
    assert (real / "coordinating" / "SKILL.md").is_file()


def test_install_blocks_a_wrong_target_claude_skills_symlink(tmp_path, rec):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "skills").symlink_to("../somewhere-else")

    plan = _plan(tmp_path)
    assert plan.claude_skills_link.action == irec.LINK_BLOCKED
    _apply(tmp_path)
    assert (tmp_path / ".claude" / "skills").is_symlink()
    assert str((tmp_path / ".claude" / "skills").readlink()) == "../somewhere-else"


def test_install_blocks_a_symlinked_claude_parent(tmp_path, rec):
    external = tmp_path.parent / f"{tmp_path.name}-external-claude"
    external.mkdir()
    (tmp_path / ".claude").symlink_to(external, target_is_directory=True)

    plan = _plan(tmp_path)
    assert plan.claude_skills_link.action == irec.LINK_BLOCKED
    assert not plan.claude_skills_link.is_work
    assert "parent" in plan.claude_skills_link.reason
    with pytest.raises(InstallError):
        _apply(tmp_path, iapply.MODE_TREE)
    assert not (external / "skills").exists()


def test_shipits_own_skills_reconcile_to_noop():
    skill_units = [
        u
        for u in iunits.load_units()
        if u.key.startswith(f"{iunits.AGENTS_SKILLS_DIR}/")
    ]
    assert skill_units, "no skill units to check"
    for unit in skill_units:
        component = irec.symlinked_dest_component(REPO_ROOT, unit.dest)
        assert component is None, f"{component} is a symlink (must be real content)"
        assert irec.consumer_hash(REPO_ROOT, unit) == unit.desired_hash(), unit.key
    link = irec.plan_claude_skills_link(REPO_ROOT)
    assert link.action == irec.LINK_NOOP, link
    claude_link = REPO_ROOT / iunits.CLAUDE_SKILLS_DIR
    assert claude_link.is_symlink()
    assert str(claude_link.readlink()) == iunits.CLAUDE_SKILLS_LINK_TARGET


def test_install_refuses_to_write_through_a_symlinked_dest_component(tmp_path, rec):
    external = tmp_path.parent / f"{tmp_path.name}-external-skills"
    external.mkdir()
    sentinel = external / "SKILL.md"
    sentinel.write_text("EXTERNAL — must never be touched\n")

    link_parent = tmp_path / ".agents" / "skills"
    link_parent.mkdir(parents=True)
    (link_parent / "coordinating").symlink_to(external, target_is_directory=True)

    victim_key = ".agents/skills/coordinating/SKILL.md"
    plan = _plan(tmp_path)
    flagged = {sd.unit_key: sd for sd in plan.symlinked_dests}
    assert victim_key in flagged
    assert flagged[victim_key].component == ".agents/skills/coordinating"
    assert all(d.unit.key != victim_key for d in plan.writes)

    with pytest.raises(InstallError, match="symlinked destination"):
        _apply(tmp_path, iapply.MODE_TREE)
    assert sentinel.read_text() == "EXTERNAL — must never be touched\n"


def test_install_refuses_to_write_through_a_symlinked_block_dest(tmp_path, rec):
    external = tmp_path.parent / f"{tmp_path.name}-external-agents.md"
    external.write_text("EXTERNAL AGENTS — must never be touched\n")
    (tmp_path / "AGENTS.md").symlink_to(external)

    plan = _plan(tmp_path)
    flagged = {sd.unit_key: sd for sd in plan.symlinked_dests}
    assert iunits.AGENTS_KEY in flagged
    assert flagged[iunits.AGENTS_KEY].component == "AGENTS.md"
    assert all(d.unit.key != iunits.AGENTS_KEY for d in plan.writes)

    with pytest.raises(InstallError, match="symlinked destination"):
        _apply(tmp_path, iapply.MODE_TREE)
    assert external.read_text() == "EXTERNAL AGENTS — must never be touched\n"


FLEET_PIXI = """\
[workspace]
name = "mkdocs-lex"
channels = ["conda-forge"]
platforms = ["osx-arm64"]

[tasks]
provision-lexd = "./bin/shipit provision lexd"
# The wf-checks lane provisions nothing outside pixi, so the lane task
# provisions it inline: `shipit provision lexd` is idempotent and lands lexd
# in this (default) env's prefix.
test-full = "./bin/shipit provision lexd && ./bin/shipit test"

[feature.lint.tasks]
lint = "./bin/shipit lint"
lint-full = { cmd = "./bin/shipit provision lexd && ./bin/shipit lint" }
"""

LANE_ONLY_PIXI = """\
[workspace]
name = "phos-app"
channels = ["conda-forge"]
platforms = ["osx-arm64"]

[feature.lint.tasks]
lint-full = { cmd = "./bin/shipit provision lexd && ./bin/shipit lint" }
"""


def test_stale_provision_finds_every_fleet_shape_with_its_task_and_table():
    found = {sp.task: sp for sp in irec.stale_provision_tasks(FLEET_PIXI)}
    assert set(found) == {"provision-lexd", "test-full", "lint-full"}
    assert found["provision-lexd"].table == "tasks"
    assert found["test-full"].table == "tasks"
    assert found["lint-full"].table == "feature.lint.tasks"
    assert found["lint-full"].command == (
        "./bin/shipit provision lexd && ./bin/shipit lint"
    )

    (lane_only,) = irec.stale_provision_tasks(LANE_ONLY_PIXI)
    assert lane_only.task == "lint-full"
    assert lane_only.table == "feature.lint.tasks"


def test_stale_provision_ignores_prose_and_repaired_lanes():
    repaired = """\
[workspace]
name = "repaired"
channels = ["conda-forge"]
platforms = ["osx-arm64"]

# lexd rides the managed [feature.shipit-lexd] block now; `shipit provision
# lexd` is retired (ADR-0066) and this lane no longer calls it.
[feature.lint.tasks]
lint-full = { cmd = "./bin/shipit lint" }
"""
    assert irec.stale_provision_tasks(repaired) == ()
    assert irec.stale_provision_tasks("[workspace\nname =") == ()


def test_stale_provision_reads_every_shape_the_fleet_actually_writes():
    manifest = """\
[workspace]
name = "shapes"
channels = ["conda-forge"]
platforms = ["osx-arm64"]

[tasks]
spaced = "./bin/shipit  provision   lexd"
plain = "shipit provision lexd"
argv = ["./bin/shipit", "provision", "lexd"]
chained = "mkdir -p build && ./bin/shipit provision lexd && ./bin/shipit test"
three-segment = "git submodule update --init comms && ./bin/shipit provision lexd && ./bin/shipit lint"
unspaced = "./bin/shipit provision lexd;./bin/shipit test"
assigned = "LEXD_HOME=/tmp/lexd ./bin/shipit provision lexd"
subshell = "(cd sub && ./bin/shipit provision lexd)"
multiline = '''
mkdir -p build
./bin/shipit provision lexd
'''
"""
    assert {sp.task for sp in irec.stale_provision_tasks(manifest)} == {
        "spaced",
        "plain",
        "argv",
        "chained",
        "three-segment",
        "unspaced",
        "assigned",
        "subshell",
        "multiline",
    }


def test_stale_provision_ignores_a_task_that_only_names_the_retired_command():
    manifest = """\
[workspace]
name = "prose"
channels = ["conda-forge"]
platforms = ["osx-arm64"]

[tasks]
explain = "echo 'shipit provision lexd is retired (ADR-0066)'"
explain-argv = ["echo", "shipit provision lexd is retired"]
explain-bare = "echo shipit provision lexd is retired"
explain-table = { cmd = "echo 'drop the shipit provision lexd prefix'" }
narrate = '''
mkdir -p build
# ./bin/shipit provision lexd is retired: lexd rides the channel now
./bin/shipit lint
'''
"""
    assert irec.stale_provision_tasks(manifest) == ()


def test_stale_provision_declines_commands_it_cannot_read_exactly():
    manifest = """\
[workspace]
name = "unjudgeable"
channels = ["conda-forge"]
platforms = ["osx-arm64"]

[tasks]
quoted-and = "echo '&&' shipit provision lexd"
quoted-semi = 'echo "note;shipit" provision lexd'
redirect = "echo > shipit provision lexd"
redirect-fd = "echo 2>shipit provision lexd"
substitution = "echo $(shipit provision lexd)"
backtick = "echo `shipit provision lexd`"
escaped = "echo shipit\\ provision lexd"
argv-operator = ["echo", ";", "shipit", "provision", "lexd"]
argv-quoted = ["echo", "note;shipit", "provision", "lexd"]
"""
    assert irec.stale_provision_tasks(manifest) == ()


def test_stale_provision_walks_a_feature_named_tasks():
    manifest = """\
[workspace]
name = "shadow"
channels = ["conda-forge"]
platforms = ["osx-arm64"]

[feature.tasks.tasks]
lint-full = "./bin/shipit provision lexd && ./bin/shipit lint"
"""
    (found,) = irec.stale_provision_tasks(manifest)
    assert found.task == "lint-full"
    assert found.table == "feature.tasks.tasks"


def test_install_refuses_a_consumer_still_calling_the_retired_provision(tmp_path, rec):
    (tmp_path / "pixi.toml").write_text(FLEET_PIXI)

    plan = _plan(tmp_path)
    flagged = {sp.task: sp for sp in plan.stale_provision}
    assert set(flagged) == {"provision-lexd", "test-full", "lint-full"}

    with pytest.raises(InstallError, match="retired command in pixi.toml") as excinfo:
        _apply(tmp_path, iapply.MODE_TREE)
    message = str(excinfo.value)
    assert "'lint-full'" in message
    assert "[feature.lint.tasks]" in message
    assert "[feature.shipit-lexd]" in message
    assert not (tmp_path / "AGENTS.md").exists()

    warnings = verb.format_plan_warnings(plan)
    assert "install: retired command:" in warnings
    assert irec.format_stale_provision(flagged["lint-full"]) in warnings


def test_stale_provision_refusal_survives_a_no_op_plan():
    plan = irec.Plan(
        root="/repo",
        decisions=(),
        retired=(),
        seeds=(),
        stale_provision=(
            irec.StaleProvisionTask(
                task="lint-full",
                table="feature.lint.tasks",
                command="./bin/shipit provision lexd && ./bin/shipit lint",
            ),
        ),
    )
    assert plan.nothing_to_do
    with pytest.raises(InstallError, match="retired command in pixi.toml"):
        iapply.reject_stale_provision(plan)


IN_SPAN_PIXI = f"""\
[workspace]
name = "clapfig"
channels = ["conda-forge"]
platforms = ["osx-arm64"]

[tasks]
{iunits.PIXI_OPEN}
changelog = "./bin/shipit changelog"
logs = "./bin/shipit logs"
provision-lexd = "./bin/shipit provision lexd"
{iunits.PIXI_CLOSE}
"""


def test_stale_provision_defers_to_the_reconcile_that_deletes_the_call(tmp_path):
    (tmp_path / "pixi.toml").write_text(IN_SPAN_PIXI)

    (on_disk,) = irec.stale_provision_tasks(IN_SPAN_PIXI)
    assert on_disk.task == "provision-lexd"
    plan = _plan(tmp_path)
    assert plan.stale_provision == ()
    iapply.reject_stale_provision(plan)
    assert "install: retired command:" not in verb.format_plan_warnings(plan)

    _apply(tmp_path, iapply.MODE_TREE)
    written = (tmp_path / "pixi.toml").read_text(encoding="utf-8")
    assert "provision-lexd" not in written
    assert irec.stale_provision_tasks(written) == ()


def test_project_pixi_text_rewrites_only_the_spans_it_is_given():
    tasks_unit = next(u for u in iunits.load_units() if u.key == iunits.PIXI_KEY)
    consumer_tail = '\n[feature.lint.tasks]\nlint = "./bin/shipit lint"\n'

    untouched = irec.project_pixi_text(IN_SPAN_PIXI + consumer_tail, ())
    assert untouched == IN_SPAN_PIXI + consumer_tail

    projected = irec.project_pixi_text(IN_SPAN_PIXI + consumer_tail, (tasks_unit,))
    assert projected.endswith(consumer_tail)
    assert (
        splice.extract_block(projected, iunits.PIXI_OPEN, iunits.PIXI_CLOSE)
        == tasks_unit.desired_inner()
    )


def test_stale_provision_still_refuses_the_call_the_reconcile_cannot_reach(tmp_path):
    (tmp_path / "pixi.toml").write_text(
        IN_SPAN_PIXI
        + """
[feature.lint.tasks]
lint-full = { cmd = "./bin/shipit provision lexd && ./bin/shipit lint" }
"""
    )
    plan = _plan(tmp_path)
    assert [sp.task for sp in plan.stale_provision] == ["lint-full"]
    assert plan.stale_provision[0].table == "feature.lint.tasks"
    with pytest.raises(InstallError, match="retired command in pixi.toml"):
        _apply(tmp_path, iapply.MODE_TREE)


def test_stale_provision_refuses_when_the_owning_block_is_declined(tmp_path):
    (tmp_path / "pixi.toml").write_text(IN_SPAN_PIXI)
    (tmp_path / ".shipit.toml").write_text(
        f'[managed.decline]\nkeep = ["{iunits.PIXI_KEY}"]\n'
    )
    plan = _plan(tmp_path)
    assert iunits.PIXI_KEY in plan.declined
    assert [sp.task for sp in plan.stale_provision] == ["provision-lexd"]
    with pytest.raises(InstallError, match="retired command in pixi.toml"):
        iapply.reject_stale_provision(plan)


def test_shipits_own_pixi_toml_calls_no_retired_provision():
    own = (REPO_ROOT / "pixi.toml").read_text(encoding="utf-8")
    assert "provision lexd" in own, "expected the ADR-0066 prose to still be there"
    assert irec.stale_provision_tasks(own) == ()


LAUNCHER_PIN = "c" * 40


def _write_launcher_repo(tmp_path: Path, *, manifest: str | None) -> Path:
    repo = tmp_path / "consumer"
    (repo / "bin").mkdir(parents=True)
    unit = next(u for u in iunits.load_units() if u.key == "bin/shipit")
    launcher = repo / "bin" / "shipit"
    launcher.write_bytes(unit.content)
    launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    if manifest is not None:
        (repo / ".shipit.toml").write_text(manifest)
    return launcher


def _shim(dir_path: Path, name: str, marker: str) -> Path:
    p = dir_path / name
    p.write_text(f'#!/usr/bin/env bash\necho "{marker} $*"\n')
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return p


def _launcher_env(*prepend: Path) -> dict[str, str]:
    path = os.pathsep.join(
        [str(d) for d in prepend] + [os.environ.get("PATH", os.defpath)]
    )
    return {"PATH": path}


def test_launcher_execs_the_pin_via_uv_never_path(tmp_path: Path):
    launcher = _write_launcher_repo(
        tmp_path, manifest=f'[shipit]\nversion = "{LAUNCHER_PIN}"\n\n[managed]\n'
    )
    shims = tmp_path / "shims"
    shims.mkdir()
    _shim(shims, "uv", "FAKE-UV-RAN")
    _shim(shims, "shipit", "PATH-SHIPIT-RAN")

    proc = subprocess.run(
        [str(launcher), "pr", "status"],
        env=_launcher_env(shims),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 0
    assert (
        "FAKE-UV-RAN tool run --from "
        f"git+https://github.com/arthur-debert/shipit@{LAUNCHER_PIN} "
        "shipit pr status" in proc.stdout
    )
    assert "PATH-SHIPIT-RAN" not in proc.stdout


def test_launcher_pinless_repo_fails_loud_toward_the_bootstrap(tmp_path: Path):
    launcher = _write_launcher_repo(
        tmp_path, manifest='[secrets]\nGH_PAT = { env = "X" }\n'
    )
    shims = tmp_path / "shims"
    shims.mkdir()
    _shim(shims, "shipit", "PATH-SHIPIT-RAN")

    proc = subprocess.run(
        [str(launcher), "--version"],
        env=_launcher_env(shims),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 127
    assert "no [shipit].version pin" in proc.stderr
    assert "shipit install --pr" in proc.stderr
    assert "PATH-SHIPIT-RAN" not in proc.stdout


@pytest.mark.parametrize("bad_pin", ["0.0.1", "seed", "c" * 39, "z" * 40])
def test_launcher_non_sha_pin_fails_loud_toward_the_bootstrap(
    tmp_path: Path, bad_pin: str
):
    launcher = _write_launcher_repo(
        tmp_path, manifest=f'[shipit]\nversion = "{bad_pin}"\n\n[managed]\n'
    )
    shims = tmp_path / "shims"
    shims.mkdir()
    _shim(shims, "uv", "FAKE-UV-RAN")
    _shim(shims, "shipit", "PATH-SHIPIT-RAN")

    proc = subprocess.run(
        [str(launcher), "--version"],
        env=_launcher_env(shims),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 127
    assert "not a full git sha" in proc.stderr
    assert "shipit install --pr" in proc.stderr
    assert "FAKE-UV-RAN" not in proc.stdout
    assert "PATH-SHIPIT-RAN" not in proc.stdout


def test_launcher_missing_manifest_fails_loud_too(tmp_path: Path):
    launcher = _write_launcher_repo(tmp_path, manifest=None)
    proc = subprocess.run(
        [str(launcher), "--version"],
        env=_launcher_env(),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 127
    assert "no [shipit].version pin" in proc.stderr


def test_launcher_honors_and_announces_shipit_exec_override(tmp_path: Path):
    launcher = _write_launcher_repo(
        tmp_path, manifest=f'[shipit]\nversion = "{LAUNCHER_PIN}"\n'
    )
    shims = tmp_path / "shims"
    shims.mkdir()
    _shim(shims, "uv", "FAKE-UV-RAN")
    dev_build = _shim(shims, "dev-shipit", "DEV-BUILD-RAN")

    env = _launcher_env(shims)
    env["SHIPIT_EXEC"] = str(dev_build)
    proc = subprocess.run(
        [str(launcher), "lint", "--fix"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 0
    assert "DEV-BUILD-RAN lint --fix" in proc.stdout
    assert "FAKE-UV-RAN" not in proc.stdout
    assert "SHIPIT_EXEC override" in proc.stderr
    assert str(dev_build) in proc.stderr


def test_launcher_refuses_a_self_pointing_shipit_exec(tmp_path: Path):
    launcher = _write_launcher_repo(
        tmp_path, manifest=f'[shipit]\nversion = "{LAUNCHER_PIN}"\n'
    )
    env = _launcher_env()
    env["SHIPIT_EXEC"] = str(launcher)
    proc = subprocess.run(
        [str(launcher), "--version"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 127
    assert "refusing the exec loop" in proc.stderr


def test_launcher_missing_uv_fails_loud_with_instructions(tmp_path: Path):
    launcher = _write_launcher_repo(
        tmp_path, manifest=f'[shipit]\nversion = "{LAUNCHER_PIN}"\n'
    )
    keep = [
        d
        for d in os.environ.get("PATH", os.defpath).split(os.pathsep)
        if d and not (Path(d) / "uv").exists()
    ]
    proc = subprocess.run(
        [str(launcher), "--version"],
        env={"PATH": os.pathsep.join(keep)},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 127
    assert "uv is not on PATH" in proc.stderr


def test_fresh_install_on_a_stock_consumer_stamps_a_full_sha_pin(tmp_path, monkeypatch):
    monkeypatch.setattr(iapply, "_activate_hooks", lambda root: _exec_result(0))
    result = _apply(tmp_path)
    assert result.mode == iapply.MODE_TREE

    pin = config.shipit_pin(tmp_path / ".shipit.toml")
    assert pin is not None
    Sha(pin)
    assert pin != "0.0.1"

    assert _plan(tmp_path).nothing_to_do
    assert config.shipit_pin(tmp_path / ".shipit.toml") == pin


def test_install_fails_closed_when_the_build_identity_is_unresolvable(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(iapply.buildid, "build_sha", lambda: None)
    monkeypatch.setattr(iapply, "_activate_hooks", lambda root: _exec_result(0))
    with pytest.raises(InstallError, match="own commit identity"):
        _apply(tmp_path)


def test_a_code_only_shipit_change_rolls_the_pin_forward(tmp_path, monkeypatch):
    monkeypatch.setattr(iapply, "_activate_hooks", lambda root: _exec_result(0))
    _apply(tmp_path)
    old_pin = config.shipit_pin(tmp_path / ".shipit.toml")
    assert old_pin is not None

    assert _plan(tmp_path).nothing_to_do

    new_sha = "abcdef0123456789abcdef0123456789abcdef01"
    monkeypatch.setattr(iapply.buildid, "build_sha", lambda: Sha(new_sha))
    plan = _plan(tmp_path)
    assert not plan.nothing_to_do
    assert plan.pin_stale
    assert not plan.writes
    assert plan.changed_paths == (config.CONFIG_NAME,)
    assert f"-> {new_sha[:12]}" in verb.format_plan(plan)

    _apply(tmp_path)
    assert config.shipit_pin(tmp_path / ".shipit.toml") == new_sha
    assert _plan(tmp_path).nothing_to_do


def test_load_units_includes_the_three_agent_defs():
    units = {u.key: u for u in iunits.load_units()}
    for role in ("implementer", "shepherd", "explorer"):
        key = f"{iunits.AGENTS_DEF_DIR}/{role}.md"
        assert key in units, f"{key} not registered"
        unit = units[key]
        assert unit.kind == "file"
        assert unit.dest == key
        assert f"name: {role}".encode() in unit.content


def test_load_units_includes_the_agy_native_reviewer_def():
    units = {u.key: u for u in iunits.load_units()}
    key = f"{iunits.AGY_AGENTS_DEF_DIR}/reviewer/agent.md"
    assert key in units, f"{key} not registered"
    unit = units[key]
    assert unit.kind == "file"
    assert unit.dest == key
    assert b"name: reviewer" in unit.content


def test_load_units_includes_the_settings_hook_block():
    units = {u.key: u for u in iunits.load_units()}
    assert iunits.SETTINGS_KEY in units
    unit = units[iunits.SETTINGS_KEY]
    assert unit.kind == "block"
    assert unit.fmt == iunits.FMT_JSON_HOOK
    assert unit.dest == iunits.SETTINGS_FILE
    entry = json.loads(unit.desired_inner())
    assert entry["matcher"] == "Edit|Write|MultiEdit|NotebookEdit"
    assert iunits.SETTINGS_HOOK_MARKER in entry["hooks"][0]["command"]


def test_load_units_includes_the_eval_terminal_hooks():
    units = {u.key: u for u in iunits.load_units()}
    for key, event, marker in (
        (iunits.SETTINGS_STOP_KEY, iunits.EVENT_STOP, iunits.SETTINGS_STOP_MARKER),
        (
            iunits.SETTINGS_SUBAGENTSTOP_KEY,
            iunits.EVENT_SUBAGENTSTOP,
            iunits.SETTINGS_SUBAGENTSTOP_MARKER,
        ),
    ):
        unit = units[key]
        assert unit.fmt == iunits.FMT_JSON_HOOK
        assert unit.dest == iunits.SETTINGS_FILE
        assert unit.event == event
        assert unit.marker == marker
        entry = json.loads(unit.desired_inner())
        assert "matcher" not in entry
        assert marker in entry["hooks"][0]["command"]


def _matcher_alternatives(matcher: str) -> list[str]:
    return matcher.split("|")


def test_each_pretooluse_matcher_routes_the_tools_its_wrapper_is_right_for():
    """A tool routed to the wrong entry gets the wrong failure mode, so matcher and decider must agree."""
    from shipit.harness import policy

    units = {u.key: u for u in iunits.load_units()}
    edit_matcher = json.loads(units[iunits.SETTINGS_KEY].desired_inner())["matcher"]
    bash_matcher = json.loads(units[iunits.SETTINGS_BASHGUARD_KEY].desired_inner())[
        "matcher"
    ]
    edit_tools = _matcher_alternatives(edit_matcher)
    bash_tools = _matcher_alternatives(bash_matcher)
    assert set(edit_tools).isdisjoint(bash_tools)

    for tool in edit_tools:
        assert policy.is_edit_tool(tool), tool
    for tool in bash_tools:
        assert not policy.is_edit_tool(tool), tool

    # Every tool behind the fail-OPEN entry must be one the decider can actually
    # rule on, or the entry buys latency and no coverage.
    assert (
        policy.decide_worktree(
            policy.ToolCall("Bash", command="git worktree add ../t b")
        ).permission
        is policy.Permission.DENY
    )
    assert (
        policy.decide_spawn_isolation(
            policy.tool_call(
                {"tool_name": "Agent", "tool_input": {"subagent_type": "implementer"}}
            )
        ).permission
        is policy.Permission.DENY
    )


def test_the_codex_pretooluse_entry_stays_matcherless_and_total():
    """codex's managed entry carries no matcher, so `_EDIT_TOOLS` alone gates the codex edit guard."""
    from shipit.harness import policy

    unit = {u.key: u for u in iunits.load_units()}[iunits.CODEX_PRETOOLUSE_KEY]
    assert "matcher" not in json.loads(unit.desired_inner())
    for tool in ("apply_patch", "functions.apply_patch"):
        assert policy.is_edit_tool(tool), tool
    assert (
        policy.decide_worktree(
            policy.tool_call(
                {
                    "tool_name": "exec_command",
                    "tool_input": {"cmd": "git worktree add ../t b"},
                }
            )
        ).permission
        is policy.Permission.DENY
    )


def test_hook_units_coexist_on_one_settings_file():
    units = {u.key: u for u in iunits.load_units()}
    text = ""
    for key in (
        iunits.SETTINGS_KEY,
        iunits.SETTINGS_BASHGUARD_KEY,
        iunits.SETTINGS_STOP_KEY,
        iunits.SETTINGS_SUBAGENTSTOP_KEY,
        iunits.SETTINGS_SESSIONSTART_KEY,
        iunits.SETTINGS_WORKTREECREATE_KEY,
    ):
        u = units[key]
        text = splice.splice_settings_hook(text, u.desired_inner(), u.event, u.marker)
    hooks = json.loads(text)["hooks"]
    assert iunits.SETTINGS_HOOK_MARKER in hooks["PreToolUse"][0]["hooks"][0]["command"]
    assert (
        iunits.SETTINGS_BASHGUARD_MARKER
        in hooks["PreToolUse"][1]["hooks"][0]["command"]
    )
    assert iunits.SETTINGS_STOP_MARKER in hooks["Stop"][0]["hooks"][0]["command"]
    assert (
        iunits.SETTINGS_SUBAGENTSTOP_MARKER
        in hooks["SubagentStop"][0]["hooks"][0]["command"]
    )
    assert (
        iunits.SETTINGS_SESSIONSTART_MARKER
        in hooks["SessionStart"][0]["hooks"][0]["command"]
    )
    assert (
        iunits.SETTINGS_WORKTREECREATE_MARKER
        in hooks["WorktreeCreate"][0]["hooks"][0]["command"]
    )
    for key in (
        iunits.SETTINGS_KEY,
        iunits.SETTINGS_BASHGUARD_KEY,
        iunits.SETTINGS_STOP_KEY,
        iunits.SETTINGS_SUBAGENTSTOP_KEY,
        iunits.SETTINGS_SESSIONSTART_KEY,
        iunits.SETTINGS_WORKTREECREATE_KEY,
    ):
        u = units[key]
        got = splice.extract_settings_hook(text, u.event, u.marker)
        assert got == iunits.canonical_hook_entry(json.loads(u.desired_inner()))


def test_load_units_includes_the_agent_start_launcher():
    units = {u.key: u for u in iunits.load_units()}
    assert iunits.AGENT_LAUNCHER_FILE in units
    unit = units[iunits.AGENT_LAUNCHER_FILE]
    assert unit.kind == "file"
    assert unit.dest == "agent-start"
    assert unit.executable is True
    text = unit.content.decode("utf-8")
    assert 'exec claude --worktree "sess-' in text
    assert 'exec "$repo/bin/shipit" session codex "$@"' in text
    assert "exec codex" not in text
    assert "claude)" in text and "codex)" in text
    assert "unset SHIPIT_LOG_CTX_ROLE" in text
    assert 'repo="$(resolve_script_dir "${BASH_SOURCE[0]}")"' in text
    assert 'cd -- "$repo"' in text


def test_launcher_matches_shipits_own_copy():
    units = {u.key: u for u in iunits.load_units()}
    unit = units[iunits.AGENT_LAUNCHER_FILE]
    own = Path(__file__).resolve().parents[1] / unit.dest
    assert own.read_bytes() == unit.content
    assert os.access(own, os.X_OK)


def test_managed_settings_hooks_agree_with_shipits_own_settings():
    own = json.loads(
        (Path(__file__).parent.parent / ".claude" / "settings.json").read_text()
    )
    units = {u.key: u for u in iunits.load_units()}
    for key in (
        iunits.SETTINGS_KEY,
        iunits.SETTINGS_BASHGUARD_KEY,
        iunits.SETTINGS_STOP_KEY,
        iunits.SETTINGS_SUBAGENTSTOP_KEY,
        iunits.SETTINGS_SESSIONSTART_KEY,
        iunits.SETTINGS_WORKTREECREATE_KEY,
    ):
        u = units[key]
        entries = own["hooks"].get(u.event, [])
        matches = [e for e in entries if splice.is_shipit_hook(e, u.marker)]
        assert matches, f"shipit's own settings.json wires no {u.event} entry ({key})"
        assert iunits.canonical_hook_entry(matches[0]) == u.desired_inner()


def test_load_units_includes_the_worktreecreate_adapter_hook():
    units = {u.key: u for u in iunits.load_units()}
    assert iunits.SETTINGS_WORKTREECREATE_KEY in units
    unit = units[iunits.SETTINGS_WORKTREECREATE_KEY]
    assert unit.kind == "block"
    assert unit.fmt == iunits.FMT_JSON_HOOK
    assert unit.dest == iunits.SETTINGS_FILE
    assert unit.event == iunits.EVENT_WORKTREECREATE
    assert unit.marker == iunits.SETTINGS_WORKTREECREATE_MARKER
    entry = json.loads(unit.desired_inner())
    assert "matcher" not in entry
    assert entry["hooks"][0]["command"] == managed_cc_hook_command("worktreecreate")
    assert "pixi run" not in entry["hooks"][0]["command"]
    assert iunits.SETTINGS_WORKTREECREATE_MARKER in entry["hooks"][0]["command"]


def test_load_units_includes_the_sessionstart_activation_hook():
    units = {u.key: u for u in iunits.load_units()}
    assert iunits.SETTINGS_SESSIONSTART_KEY in units
    unit = units[iunits.SETTINGS_SESSIONSTART_KEY]
    assert unit.kind == "block"
    assert unit.fmt == iunits.FMT_JSON_HOOK
    assert unit.dest == iunits.SETTINGS_FILE
    assert unit.event == iunits.EVENT_SESSIONSTART
    assert unit.marker == iunits.SETTINGS_SESSIONSTART_MARKER
    entry = json.loads(unit.desired_inner())
    assert "matcher" not in entry
    assert iunits.SETTINGS_SESSIONSTART_MARKER in entry["hooks"][0]["command"]


CODEX_HOOK_KEYS = (iunits.CODEX_PRETOOLUSE_KEY, iunits.CODEX_SESSIONSTART_KEY)


def test_load_units_includes_the_codex_project_layer():
    units = {u.key: u for u in iunits.load_units()}
    assert iunits.CODEX_CONFIG_FILE in units
    cfg = units[iunits.CODEX_CONFIG_FILE]
    assert cfg.kind == "file"
    assert cfg.dest == ".codex/config.toml"
    parsed = tomllib.loads(cfg.content.decode("utf-8"))
    assert parsed == {"project_doc_max_bytes": 65536}
    for key, event, marker in (
        (
            iunits.CODEX_PRETOOLUSE_KEY,
            iunits.EVENT_PRETOOLUSE,
            iunits.SETTINGS_HOOK_MARKER,
        ),
        (
            iunits.CODEX_SESSIONSTART_KEY,
            iunits.EVENT_SESSIONSTART,
            iunits.SETTINGS_SESSIONSTART_MARKER,
        ),
    ):
        unit = units[key]
        assert unit.kind == "block"
        assert unit.fmt == iunits.FMT_JSON_HOOK
        assert unit.dest == iunits.CODEX_HOOKS_FILE
        assert unit.event == event
        assert unit.marker == marker
        assert marker in json.loads(unit.desired_inner())["hooks"][0]["command"]


def test_codex_hook_commands_adapt_env_and_keep_the_fail_postures():
    units = {u.key: u for u in iunits.load_units()}

    guard = json.loads(units[iunits.CODEX_PRETOOLUSE_KEY].desired_inner())
    guard_cmd = guard["hooks"][0]["command"]
    assert "CLAUDE_PROJECT_DIR" not in guard_cmd
    assert "git rev-parse --show-toplevel" in guard_cmd
    assert 'pixi run --manifest-path "$repo/pixi.toml" -- ' in guard_cmd
    assert '"$repo/bin/shipit" hook pretooluse' in guard_cmd
    assert "exit 2" in guard_cmd
    assert "exit 0" not in guard_cmd
    assert "matcher" not in guard

    session = json.loads(units[iunits.CODEX_SESSIONSTART_KEY].desired_inner())
    session_cmd = session["hooks"][0]["command"]
    assert "CLAUDE_PROJECT_DIR" not in session_cmd
    assert "setup-dev-env.sh" in session_cmd
    assert "exit 0" in session_cmd
    assert "git rev-parse --show-toplevel" in session_cmd
    assert "command -v python3" in session_cmd
    assert "command -v python" in session_cmd
    assert "json.dumps" in session_cmd
    assert 'if [ -n "$py" ]' in session_cmd
    assert '"$repo/bin/shipit" hook sessionstart' in session_cmd


def test_codex_hook_units_coexist_on_one_hooks_file():
    units = {u.key: u for u in iunits.load_units()}
    text = ""
    for key in CODEX_HOOK_KEYS:
        u = units[key]
        text = splice.splice_settings_hook(text, u.desired_inner(), u.event, u.marker)
    hooks = json.loads(text)["hooks"]
    assert iunits.SETTINGS_HOOK_MARKER in hooks["PreToolUse"][0]["hooks"][0]["command"]
    assert (
        iunits.SETTINGS_SESSIONSTART_MARKER
        in hooks["SessionStart"][0]["hooks"][0]["command"]
    )
    for key in CODEX_HOOK_KEYS:
        u = units[key]
        got = splice.extract_settings_hook(text, u.event, u.marker)
        assert got == iunits.canonical_hook_entry(json.loads(u.desired_inner()))


def test_codex_config_unit_reconciles_add_noop_override(tmp_path, rec):

    def decision():
        return next(
            d
            for d in _plan(tmp_path).decisions
            if d.unit.key == iunits.CODEX_CONFIG_FILE
        )

    assert decision().action == irec.ADD
    _apply(tmp_path)
    assert (tmp_path / ".codex" / "config.toml").read_bytes() == iunits.data_bytes(
        "codex-config.toml"
    )
    assert decision().action == irec.NOOP
    (tmp_path / ".codex" / "config.toml").write_text("project_doc_max_bytes = 1\n")
    assert decision().action == irec.OVERRIDE


def test_codex_hook_units_reconcile_add_noop_override(tmp_path, rec):

    def decisions():
        plan = _plan(tmp_path)
        return {d.unit.key: d for d in plan.decisions if d.unit.key in CODEX_HOOK_KEYS}

    assert {d.action for d in decisions().values()} == {irec.ADD}
    _apply(tmp_path)
    assert {d.action for d in decisions().values()} == {irec.NOOP}

    hooks_path = tmp_path / ".codex" / "hooks.json"
    data = json.loads(hooks_path.read_text())
    data["hooks"]["SessionStart"].append(
        {"hooks": [{"type": "command", "command": "echo consumer-own-hook"}]}
    )
    hooks_path.write_text(json.dumps(data, indent=2) + "\n")
    assert {d.action for d in decisions().values()} == {irec.NOOP}

    data["hooks"]["PreToolUse"][0]["hooks"][0]["command"] = (
        "./bin/shipit hook pretooluse # defused"
    )
    hooks_path.write_text(json.dumps(data, indent=2) + "\n")
    got = decisions()
    assert got[iunits.CODEX_PRETOOLUSE_KEY].action == irec.OVERRIDE
    assert got[iunits.CODEX_SESSIONSTART_KEY].action == irec.NOOP

    _apply(tmp_path)
    repaired = json.loads(hooks_path.read_text())
    assert any(
        "echo consumer-own-hook" in h["command"]
        for e in repaired["hooks"]["SessionStart"]
        for h in e["hooks"]
    )
    assert {d.action for d in decisions().values()} == {irec.NOOP}


def test_codex_unit_update_advances_silently_on_a_pristine_consumer(
    tmp_path, rec, monkeypatch
):
    _apply(tmp_path)

    real = iunits.data_bytes
    new_config = b"project_doc_max_bytes = 131072\n"
    new_entry = json.dumps(
        {"hooks": [{"type": "command", "command": "./bin/shipit hook sessionstart"}]}
    ).encode()

    def fake(*parts):
        if parts == ("codex-config.toml",):
            return new_config
        if parts == ("codex-hooks-sessionstart.json",):
            return new_entry
        return real(*parts)

    monkeypatch.setattr(iunits, "data_bytes", fake)
    plan = _plan(tmp_path)
    actions = {
        d.unit.key: d.action
        for d in plan.decisions
        if d.unit.key in (iunits.CODEX_CONFIG_FILE, iunits.CODEX_SESSIONSTART_KEY)
    }
    assert actions == {
        iunits.CODEX_CONFIG_FILE: irec.UPDATE,
        iunits.CODEX_SESSIONSTART_KEY: irec.UPDATE,
    }
    _apply(tmp_path)
    assert (tmp_path / ".codex" / "config.toml").read_bytes() == new_config
    assert _plan(tmp_path).nothing_to_do


def test_managed_codex_layer_agrees_with_shipits_own_copies():
    assert (REPO_ROOT / ".codex" / "config.toml").read_bytes() == iunits.data_bytes(
        "codex-config.toml"
    )
    own = (REPO_ROOT / ".codex" / "hooks.json").read_text()
    units = {u.key: u for u in iunits.load_units()}
    for key in CODEX_HOOK_KEYS:
        u = units[key]
        got = splice.extract_settings_hook(own, u.event, u.marker)
        assert got == iunits.canonical_hook_entry(json.loads(u.desired_inner())), (
            f"shipit's own .codex/hooks.json disagrees with the managed {key}"
        )


def test_managed_settings_hooks_drop_pixi_run_and_fail_open(tmp_path, rec):
    hook_units = [
        u
        for u in iunits.load_units()
        if u.fmt == iunits.FMT_JSON_HOOK and u.dest == iunits.SETTINGS_FILE
    ]
    assert {u.event for u in hook_units} == {
        iunits.EVENT_PRETOOLUSE,
        iunits.EVENT_STOP,
        iunits.EVENT_SUBAGENTSTOP,
        iunits.EVENT_SESSIONSTART,
        iunits.EVENT_WORKTREECREATE,
    }
    additive_units = [u for u in hook_units if u.event != iunits.EVENT_PRETOOLUSE]
    assert {u.event for u in additive_units} == {
        iunits.EVENT_STOP,
        iunits.EVENT_SUBAGENTSTOP,
        iunits.EVENT_SESSIONSTART,
        iunits.EVENT_WORKTREECREATE,
    }
    for u in additive_units:
        command = json.loads(u.desired_inner())["hooks"][0]["command"]
        phase = u.marker.removeprefix("shipit hook ")
        assert command == managed_cc_hook_command(phase)
        assert "pixi run" not in command
        assert "test -x ./bin/shipit || {" in command
        assert "exit 0" in command
        assert u.marker in command


def test_managed_pretooluse_hook_restores_pixi_run_and_fails_closed(tmp_path, rec):
    units = {u.key: u for u in iunits.load_units()}
    unit = units[iunits.SETTINGS_KEY]
    assert unit.event == iunits.EVENT_PRETOOLUSE
    command = json.loads(unit.desired_inner())["hooks"][0]["command"]
    assert command == managed_pretooluse_hook_command()
    assert "pixi run" in command
    assert '--manifest-path "$CLAUDE_PROJECT_DIR"/pixi.toml -- ' in command
    assert "exit 0" not in command
    assert "exit 2" in command
    assert iunits.SETTINGS_HOOK_MARKER in command


def test_managed_bashguard_hook_keeps_pixi_run_and_fails_open(tmp_path, rec):
    units = {u.key: u for u in iunits.load_units()}
    unit = units[iunits.SETTINGS_BASHGUARD_KEY]
    assert unit.event == iunits.EVENT_PRETOOLUSE
    assert unit.marker == iunits.SETTINGS_BASHGUARD_MARKER
    entry = json.loads(unit.desired_inner())
    assert entry["matcher"] == "Bash|Agent"
    command = entry["hooks"][0]["command"]
    assert command == managed_bashguard_hook_command()
    assert "pixi run" in command
    assert '--manifest-path "$CLAUDE_PROJECT_DIR"/pixi.toml -- ' in command
    assert "exit 2" not in command
    assert "exit 0" in command
    assert iunits.SETTINGS_BASHGUARD_MARKER in command


def test_the_edit_entry_is_byte_identical_to_the_fail_closed_original():
    """ADR-0038's command is verbatim: the split adds an entry, it never edits this one."""
    unit = {u.key: u for u in iunits.load_units()}[iunits.SETTINGS_KEY]
    entry = json.loads(unit.desired_inner())
    assert entry["matcher"] == "Edit|Write|MultiEdit|NotebookEdit"
    assert entry["hooks"][0]["command"] == managed_pretooluse_hook_command()


def test_the_two_pretooluse_markers_cannot_strip_each_other():
    """`is_shipit_hook` matches a marker as a command SUBSTRING, so neither may contain the other."""
    edit, bash = iunits.SETTINGS_HOOK_MARKER, iunits.SETTINGS_BASHGUARD_MARKER
    assert edit not in bash
    assert bash not in edit
    units = {u.key: u for u in iunits.load_units()}
    edit_cmd = json.loads(units[iunits.SETTINGS_KEY].desired_inner())["hooks"][0][
        "command"
    ]
    bash_cmd = json.loads(units[iunits.SETTINGS_BASHGUARD_KEY].desired_inner())[
        "hooks"
    ][0]["command"]
    assert bash not in edit_cmd
    assert edit not in bash_cmd


def test_both_pretooluse_entries_coexist_across_a_respliced_settings_file():
    units = {u.key: u for u in iunits.load_units()}
    keys = (iunits.SETTINGS_KEY, iunits.SETTINGS_BASHGUARD_KEY)
    text = ""
    for _round in range(2):
        for key in keys:
            u = units[key]
            text = splice.splice_settings_hook(
                text, u.desired_inner(), u.event, u.marker
            )
    entries = json.loads(text)["hooks"][iunits.EVENT_PRETOOLUSE]
    assert len(entries) == 2
    for key in keys:
        u = units[key]
        assert splice.extract_settings_hook(text, u.event, u.marker) == (
            u.desired_inner()
        )


def test_both_pretooluse_entries_reconcile_to_noop_on_reinstall(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    _apply(tmp_path)
    keys = (iunits.SETTINGS_KEY, iunits.SETTINGS_BASHGUARD_KEY)

    plan = _plan(tmp_path)
    actions = {d.unit.key: d.action for d in plan.decisions if d.unit.key in keys}
    assert actions == dict.fromkeys(keys, irec.NOOP)

    entries = json.loads((tmp_path / iunits.SETTINGS_FILE).read_text())["hooks"][
        iunits.EVENT_PRETOOLUSE
    ]
    assert len(entries) == 2
    managed = config.load_managed(config.load(tmp_path / ".shipit.toml"))
    assert iunits.SETTINGS_BASHGUARD_KEY in managed


def test_load_units_includes_the_setup_dev_env_bootstrap():
    units = {u.key: u for u in iunits.load_units()}
    assert iunits.SETUP_DEV_ENV_FILE in units
    unit = units[iunits.SETUP_DEV_ENV_FILE]
    assert unit.kind == "file"
    assert unit.dest == "bin/setup-dev-env.sh"
    assert unit.executable is True
    text = unit.content.decode("utf-8")
    assert 'PIXI_PIN="' in text and 'UV_PIN="' in text
    assert "github.com/prefix-dev/pixi/releases/download" in text
    assert "github.com/astral-sh/uv/releases/download" in text
    assert "pixi.sh/install" not in text
    assert "astral.sh/uv/install" not in text
    assert "pixi install --locked" in text
    assert 'REPO_ROOT="$(dirname -- "$(resolve_script_dir "$SELF")")"' in text


def test_setup_dev_env_matches_shipits_own_copy():
    unit = next(u for u in iunits.load_units() if u.key == iunits.SETUP_DEV_ENV_FILE)
    own = Path(__file__).resolve().parents[1] / "bin" / "setup-dev-env.sh"
    assert own.read_bytes() == unit.content
    assert os.access(own, os.X_OK)


def _bootstrap_function(name: str, unit: str = "setup-dev-env.sh") -> str:
    lines = iunits.data_bytes("bootstrap", unit).decode("utf-8").splitlines()
    start = lines.index(f"{name}() {{")
    end = start + lines[start:].index("}")
    return "\n".join(lines[start : end + 1])


def _resolve_script_dir_driver(unit: str) -> str:
    parts = ["#!/usr/bin/env bash", "set -euo pipefail"]
    if unit == "setup-dev-env.sh":
        parts.append(_bootstrap_function("warn", unit))
    parts.append(_bootstrap_function("resolve_script_dir", unit))
    parts.append('resolve_script_dir "$1"')
    return "\n".join(parts)


def _repo_root_driver() -> str:
    script = iunits.data_bytes("bootstrap", "setup-dev-env.sh").decode("utf-8")
    repo_root_line = next(
        line for line in script.splitlines() if line.startswith('REPO_ROOT="')
    )
    return "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            _bootstrap_function("warn"),
            _bootstrap_function("resolve_script_dir"),
            'SELF="${BASH_SOURCE[0]:-$0}"',
            repo_root_line,
            'printf "%s\\n" "$REPO_ROOT"',
        ]
    )


@pytest.mark.parametrize(
    "layout", ["plain", "symlinked-bin", "symlinked-script", "symlinked-bin-and-script"]
)
def test_setup_dev_env_repo_root_resolves_through_symlinks(tmp_path, layout):
    repo = tmp_path / "repo"
    (repo / "bin").mkdir(parents=True)
    (repo / "pixi.toml").write_text("[environments]\nlint = ['lint']\n")
    driver = _repo_root_driver()

    if layout == "plain":
        entry = repo / "bin" / "setup-dev-env.sh"
        entry.write_text(driver)
    elif layout == "symlinked-bin":
        shared = tmp_path / "shared" / "bin"
        shared.mkdir(parents=True)
        (shared / "setup-dev-env.sh").write_text(driver)
        (repo / "bin").rmdir()
        (repo / "bin").symlink_to(shared)
        entry = repo / "bin" / "setup-dev-env.sh"
    elif layout == "symlinked-script":
        (repo / "bin" / "setup-dev-env.sh").write_text(driver)
        linkdir = tmp_path / "home" / "bin"
        linkdir.mkdir(parents=True)
        entry = linkdir / "setup-dev-env.sh"
        entry.symlink_to(repo / "bin" / "setup-dev-env.sh")
    else:
        (repo / "bin" / "setup-dev-env.sh").write_text(driver)
        tools = tmp_path / "tools"
        tools.mkdir()
        (tools / "setup-dev-env.sh").symlink_to("../repo/bin/setup-dev-env.sh")
        alias = tmp_path / "alias"
        alias.mkdir()
        (alias / "bin").symlink_to(tools)
        decoy = alias / "repo"
        (decoy / "bin").mkdir(parents=True)
        (decoy / "pixi.toml").write_text("[environments]\nlint = ['lint']\n")
        entry = alias / "bin" / "setup-dev-env.sh"

    bash = shutil.which("bash")
    assert bash is not None
    proc = subprocess.run(
        [bash, str(entry)], capture_output=True, text=True, timeout=10
    )
    assert proc.returncode == 0, proc.stderr
    resolved = Path(proc.stdout.strip())
    assert resolved.samefile(repo), f"resolved {resolved}, wanted {repo}"
    assert (resolved / "pixi.toml").is_file()


def test_setup_dev_env_repo_root_stays_fail_open_when_readlink_errors(tmp_path):
    stub_bin = tmp_path / "stubbin"
    stub_bin.mkdir()
    dirname = shutil.which("dirname")
    assert dirname is not None
    os.symlink(dirname, stub_bin / "dirname")
    stub = stub_bin / "readlink"
    stub.write_text("#!/bin/sh\necho 'readlink: boom' >&2\nexit 1\n")
    stub.chmod(0o755)

    repo = tmp_path / "repo"
    (repo / "bin").mkdir(parents=True)
    (repo / "bin" / "setup-dev-env.sh").write_text(_repo_root_driver())
    linkdir = tmp_path / "home" / "bin"
    linkdir.mkdir(parents=True)
    entry = linkdir / "setup-dev-env.sh"
    entry.symlink_to(repo / "bin" / "setup-dev-env.sh")

    bash = shutil.which("bash")
    assert bash is not None
    proc = subprocess.run(
        [bash, str(entry)],
        env={"PATH": str(stub_bin)},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 0, proc.stderr
    assert "could not read the symlink" in proc.stderr
    resolved = proc.stdout.strip()
    assert resolved != "."
    assert Path(resolved).samefile(linkdir.parent), f"fell back to {resolved}"


@pytest.mark.parametrize("unit", ["setup-dev-env.sh", "agent-start"])
def test_resolve_script_dir_degrades_when_the_final_cd_fails(tmp_path, unit):
    driver = tmp_path / "driver.sh"
    driver.write_text(_resolve_script_dir_driver(unit))
    driver.chmod(0o755)

    link = tmp_path / "link"
    link.symlink_to(tmp_path / "gone" / "target")

    bash = shutil.which("bash")
    assert bash is not None
    proc = subprocess.run(
        [bash, str(driver), str(link)], capture_output=True, text=True, timeout=10
    )
    assert proc.returncode == 0, proc.stderr
    assert "could not resolve the directory holding" in proc.stderr
    resolved = proc.stdout.strip()
    assert resolved != "."
    assert resolved == str(tmp_path / "gone")


@pytest.mark.parametrize("tool", ["sha256sum", "shasum"])
def test_sha256_of_stays_fail_open_when_the_hash_tool_errors(tmp_path, tool):
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    awk = shutil.which("awk")
    assert awk is not None
    os.symlink(awk, stub_bin / "awk")
    stub = stub_bin / tool
    stub.write_text("#!/bin/sh\necho 'hash tool: boom' >&2\nexit 1\n")
    stub.chmod(0o755)
    target = tmp_path / "asset.tar.gz"
    target.write_bytes(b"payload")
    driver = "\n".join(
        [
            "set -euo pipefail",
            _bootstrap_function("sha256_of"),
            f'got="$(sha256_of "{target}")"',
            'printf "got=[%s]\\n" "$got"',
            "echo survived",
        ]
    )
    bash = shutil.which("bash")
    assert bash is not None
    proc = subprocess.run(
        [bash, "-c", driver],
        env={"PATH": str(stub_bin)},
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "got=[]" in proc.stdout
    assert "survived" in proc.stdout


def test_setup_dev_env_pixi_pin_agrees_with_ci():
    script = iunits.data_bytes("bootstrap", "setup-dev-env.sh").decode("utf-8")
    pin = next(
        line.split('"')[1]
        for line in script.splitlines()
        if line.startswith("PIXI_PIN=")
    )
    wf = (
        Path(__file__).resolve().parents[1] / ".github" / "workflows" / "wf-checks.yml"
    ).read_text(encoding="utf-8")
    wf_pins = [
        line.split(":", 1)[1].strip().removeprefix("v")
        for line in wf.splitlines()
        if line.strip().startswith("pixi-version:")
    ]
    assert wf_pins, "wf-checks.yml carries no setup-pixi pixi-version pin"
    assert all(wf_pin == pin for wf_pin in wf_pins)


def test_managed_sessionstart_hook_runs_setup_dev_env_first():
    units = {u.key: u for u in iunits.load_units()}
    command = json.loads(units[iunits.SETTINGS_SESSIONSTART_KEY].desired_inner())[
        "hooks"
    ][0]["command"]
    assert "./bin/setup-dev-env.sh" in command
    assert command.index("./bin/setup-dev-env.sh") < command.index(
        "test -x ./bin/shipit"
    )
    assert "if [ -x ./bin/setup-dev-env.sh ]; then" in command
    assert iunits.SETTINGS_SESSIONSTART_MARKER in command


def test_managed_sessionstart_hook_exports_local_bin_before_the_launcher():
    units = {u.key: u for u in iunits.load_units()}
    command = json.loads(units[iunits.SETTINGS_SESSIONSTART_KEY].desired_inner())[
        "hooks"
    ][0]["command"]
    path_leg = (
        'case ":$PATH:" in *":$HOME/.local/bin:"*) ;; '
        '*) export PATH="$HOME/.local/bin:$PATH" ;; esac; '
    )
    assert path_leg in command
    assert command.index("./bin/setup-dev-env.sh") < command.index(path_leg)
    assert command.index(path_leg) < command.index("test -x ./bin/shipit")
    script = iunits.data_bytes("bootstrap", "setup-dev-env.sh").decode("utf-8")
    expected_leg = path_leg.removesuffix("; ").replace('"', '\\"').replace("$", "\\$")
    assert expected_leg in script
    for key in (
        iunits.SETTINGS_STOP_KEY,
        iunits.SETTINGS_SUBAGENTSTOP_KEY,
        iunits.SETTINGS_WORKTREECREATE_KEY,
    ):
        other = json.loads(units[key].desired_inner())["hooks"][0]["command"]
        assert path_leg not in other


def test_hook_commands_share_one_guarded_local_bin_path_leg():
    units = {u.key: u for u in iunits.load_units()}
    for key in (
        iunits.SETTINGS_KEY,
        iunits.SETTINGS_SESSIONSTART_KEY,
        iunits.CODEX_PRETOOLUSE_KEY,
        iunits.CODEX_SESSIONSTART_KEY,
    ):
        command = json.loads(units[key].desired_inner())["hooks"][0]["command"]
        assert LOCAL_BIN_PATH_LEG in command, (
            f"{key} lacks the shared guarded ~/.local/bin PATH leg"
        )
    guard_cmd = json.loads(units[iunits.CODEX_PRETOOLUSE_KEY].desired_inner())["hooks"][
        0
    ]["command"]
    assert guard_cmd.index(LOCAL_BIN_PATH_LEG) < guard_cmd.index("pixi run")
    session_cmd = json.loads(units[iunits.CODEX_SESSIONSTART_KEY].desired_inner())[
        "hooks"
    ][0]["command"]
    assert (
        session_cmd.index("setup-dev-env.sh")
        < session_cmd.index(LOCAL_BIN_PATH_LEG)
        < session_cmd.index('test -x "$repo/bin/shipit"')
    )
    for key in (
        iunits.SETTINGS_STOP_KEY,
        iunits.SETTINGS_SUBAGENTSTOP_KEY,
        iunits.SETTINGS_WORKTREECREATE_KEY,
    ):
        other = json.loads(units[key].desired_inner())["hooks"][0]["command"]
        assert LOCAL_BIN_PATH_LEG not in other


def test_load_units_toolchain_blocks_are_conditional():
    toolchain_keys = {key for key, *_ in iunits.TOOLCHAIN_UNITS}
    base = {u.key for u in iunits.load_units()}
    assert not (base & toolchain_keys)

    for signal in (
        iunits.TOOLCHAIN_RUST,
        iunits.TOOLCHAIN_GO,
        iunits.TOOLCHAIN_NODE,
        iunits.TOOLCHAIN_PYTHON,
        iunits.TOOLCHAIN_TREE_SITTER,
        iunits.TOOLCHAIN_LUA,
    ):
        expected = {key for key, sig, *_ in iunits.TOOLCHAIN_UNITS if sig == signal}
        keys = {u.key for u in iunits.load_units(toolchains=frozenset({signal}))}
        assert keys - base == expected, signal
    assert (
        len(
            {
                key
                for key, sig, *_ in iunits.TOOLCHAIN_UNITS
                if sig == iunits.TOOLCHAIN_RUST
            }
        )
        == 3
    )

    all_keys = {
        u.key
        for u in iunits.load_units(
            toolchains=frozenset(
                {
                    iunits.TOOLCHAIN_RUST,
                    iunits.TOOLCHAIN_GO,
                    iunits.TOOLCHAIN_NODE,
                    iunits.TOOLCHAIN_PYTHON,
                    iunits.TOOLCHAIN_TREE_SITTER,
                    iunits.TOOLCHAIN_LUA,
                }
            )
        )
    }
    assert all_keys - base == toolchain_keys


def test_toolchain_block_units_have_the_right_shape():
    units = {
        u.key: u
        for u in iunits.load_units(
            toolchains=frozenset(
                {
                    iunits.TOOLCHAIN_RUST,
                    iunits.TOOLCHAIN_GO,
                    iunits.TOOLCHAIN_NODE,
                    iunits.TOOLCHAIN_PYTHON,
                    iunits.TOOLCHAIN_TREE_SITTER,
                    iunits.TOOLCHAIN_LUA,
                }
            )
        )
    }

    rust = units[iunits.PIXI_RUST_DEPS_KEY]
    assert rust.dest == "pixi.toml"
    assert rust.anchor == iunits.PIXI_LINT_DEPS_ANCHOR
    assert tomllib.loads(rust.desired_inner()) == {"rust": "1.96.*"}

    go = units[iunits.PIXI_GO_DEPS_KEY]
    assert go.anchor == iunits.PIXI_LINT_DEPS_ANCHOR
    assert tomllib.loads(go.desired_inner()) == {"go": "1.26.*", "golangci-lint": "2.*"}

    lua = units[iunits.PIXI_LUA_DEPS_KEY]
    assert lua.anchor == iunits.PIXI_LINT_DEPS_ANCHOR
    assert tomllib.loads(lua.desired_inner()) == {"stylua": "2.*", "selene": "0.31.*"}

    node = units[iunits.PIXI_NODE_DEPS_KEY]
    assert node.anchor == "[dependencies]"
    assert tomllib.loads(node.desired_inner()) == {"nodejs": "26.*", "pnpm": "11.*"}

    rust_release = units[iunits.PIXI_RUST_RELEASE_DEPS_KEY]
    assert rust_release.dest == "pixi.toml"
    assert rust_release.anchor == "[dependencies]"
    assert tomllib.loads(rust_release.desired_inner()) == {
        "cargo-edit": "0.13.11.*",
        "wasm-pack": "0.15.*",
    }

    rust_toolchain = units[iunits.PIXI_RUST_RELEASE_TOOLCHAIN_KEY]
    assert rust_toolchain.dest == "pixi.toml"
    assert rust_toolchain.anchor == "[dependencies]"
    assert tomllib.loads(rust_toolchain.desired_inner()) == {
        "rust": "1.96.*",
        "rust-std-wasm32-unknown-unknown": "1.96.*",
    }

    python_release = units[iunits.PIXI_PYTHON_RELEASE_DEPS_KEY]
    assert python_release.dest == "pixi.toml"
    assert python_release.anchor == "[dependencies]"
    assert tomllib.loads(python_release.desired_inner()) == {"twine": "6.2.*"}

    tree_sitter = units[iunits.PIXI_TREE_SITTER_DEPS_KEY]
    assert tree_sitter.dest == "pixi.toml"
    assert tree_sitter.anchor == "[dependencies]"
    assert tomllib.loads(tree_sitter.desired_inner()) == {"tree-sitter-cli": "0.25.*"}

    fences = {
        units[k].open_marker
        for k in (
            iunits.PIXI_LINT_DEPS_KEY,
            iunits.PIXI_RUST_DEPS_KEY,
            iunits.PIXI_RUST_RELEASE_DEPS_KEY,
            iunits.PIXI_RUST_RELEASE_TOOLCHAIN_KEY,
            iunits.PIXI_GO_DEPS_KEY,
            iunits.PIXI_NODE_DEPS_KEY,
            iunits.PIXI_PYTHON_RELEASE_DEPS_KEY,
            iunits.PIXI_TREE_SITTER_DEPS_KEY,
        )
    }
    assert len(fences) == 8


def test_conda_packager_block_is_gated_on_the_conda_endpoint(tmp_path):
    base = {u.key for u in iunits.load_units()}
    assert iunits.PIXI_CONDA_PACKAGER_KEY not in base

    every_toolchain = {
        u.key
        for u in iunits.load_units(
            toolchains=frozenset(
                {
                    iunits.TOOLCHAIN_RUST,
                    iunits.TOOLCHAIN_GO,
                    iunits.TOOLCHAIN_NODE,
                    iunits.TOOLCHAIN_PYTHON,
                    iunits.TOOLCHAIN_TREE_SITTER,
                    iunits.TOOLCHAIN_LUA,
                }
            )
        )
    }
    assert iunits.PIXI_CONDA_PACKAGER_KEY not in every_toolchain

    with_conda = {
        u.key for u in iunits.load_units(endpoints=frozenset({iunits.ENDPOINT_CONDA}))
    }
    assert with_conda - base == {iunits.PIXI_CONDA_PACKAGER_KEY}


def test_conda_packager_block_has_the_right_shape():
    units = {
        u.key: u
        for u in iunits.load_units(endpoints=frozenset({iunits.ENDPOINT_CONDA}))
    }
    packager = units[iunits.PIXI_CONDA_PACKAGER_KEY]
    assert packager.dest == "pixi.toml"
    assert packager.anchor == "[dependencies]"
    assert tomllib.loads(packager.desired_inner()) == {"rattler-build": "0.69.*"}
    release = {
        u.key: u
        for u in iunits.load_units(
            toolchains=frozenset({iunits.TOOLCHAIN_RUST, iunits.TOOLCHAIN_NODE}),
            endpoints=frozenset({iunits.ENDPOINT_CONDA}),
        )
    }
    fences = {
        release[k].open_marker
        for k in (
            iunits.PIXI_CONDA_PACKAGER_KEY,
            iunits.PIXI_RUST_RELEASE_DEPS_KEY,
            iunits.PIXI_RUST_RELEASE_TOOLCHAIN_KEY,
            iunits.PIXI_NODE_DEPS_KEY,
            iunits.PIXI_LAUNCHER_DEPS_KEY,
        )
    }
    assert len(fences) == 5


def test_rust_conda_repo_provisions_rattler_build_exactly_once():
    rust_release = tomllib.loads(
        iunits.data_bytes("pixi-rust-release-deps-block.toml").decode("utf-8")
    )
    assert "rattler-build" not in rust_release
    packager = tomllib.loads(
        iunits.data_bytes("pixi-conda-packager-block.toml").decode("utf-8")
    )
    assert packager == {"rattler-build": "0.69.*"}


def test_conda_packager_coexists_with_rust_release_block_under_dependencies():
    units = {
        u.key: u
        for u in iunits.load_units(
            toolchains=frozenset({iunits.TOOLCHAIN_RUST}),
            endpoints=frozenset({iunits.ENDPOINT_CONDA}),
        )
    }
    release = units[iunits.PIXI_RUST_RELEASE_DEPS_KEY]
    packager = units[iunits.PIXI_CONDA_PACKAGER_KEY]

    text = '[workspace]\nname = "acme"\n'
    for unit in (release, packager):
        text = splice.splice_block(
            text,
            unit.desired_inner(),
            unit.open_marker,
            unit.close_marker,
            unit.anchor,
        )

    assert (
        splice.extract_block(text, release.open_marker, release.close_marker)
        == release.desired_inner()
    )
    assert (
        splice.extract_block(text, packager.open_marker, packager.close_marker)
        == packager.desired_inner()
    )
    headers = [ln for ln in text.splitlines() if ln.strip() == "[dependencies]"]
    assert len(headers) == 1
    merged = tomllib.loads(text)["dependencies"]
    assert merged["rattler-build"] == "0.69.*"
    assert merged["cargo-edit"] == "0.13.11.*"


def test_rust_release_toolchain_pin_agrees_with_the_rust_lint_block():
    toolchain = tomllib.loads(
        iunits.data_bytes("pixi-rust-release-toolchain-block.toml").decode("utf-8")
    )
    lint = tomllib.loads(
        iunits.data_bytes("pixi-rust-lint-deps-block.toml").decode("utf-8")
    )
    assert toolchain == {
        "rust": lint["rust"],
        "rust-std-wasm32-unknown-unknown": lint["rust"],
    }


def test_packaged_rust_pin_agrees_with_shipits_own_test_toolchain():
    own = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pixi.toml").read_text(encoding="utf-8")
    )
    block = tomllib.loads(
        iunits.data_bytes("pixi-rust-lint-deps-block.toml").decode("utf-8")
    )
    assert block["rust"] == own["feature"]["test"]["dependencies"]["rust"]


def test_rust_block_coexists_with_lint_deps_block_under_one_anchor():
    units = {
        u.key: u
        for u in iunits.load_units(toolchains=frozenset({iunits.TOOLCHAIN_RUST}))
    }
    deps = units[iunits.PIXI_LINT_DEPS_KEY]
    rust = units[iunits.PIXI_RUST_DEPS_KEY]

    text = '[workspace]\nname = "acme"\n'
    text = splice.splice_block(
        text, deps.desired_inner(), deps.open_marker, deps.close_marker, deps.anchor
    )
    text = splice.splice_block(
        text, rust.desired_inner(), rust.open_marker, rust.close_marker, rust.anchor
    )

    assert (
        splice.extract_block(text, deps.open_marker, deps.close_marker)
        == deps.desired_inner()
    )
    assert (
        splice.extract_block(text, rust.open_marker, rust.close_marker)
        == rust.desired_inner()
    )
    assert text.count("[feature.lint.dependencies]") == 1
    parsed = tomllib.loads(text)
    merged = parsed["feature"]["lint"]["dependencies"]
    assert merged["rust"] == "1.96.*"
    assert merged["ruff"] == tomllib.loads(deps.desired_inner())["ruff"]


def test_rust_release_block_coexists_with_node_block_under_dependencies():
    units = {
        u.key: u
        for u in iunits.load_units(
            toolchains=frozenset({iunits.TOOLCHAIN_RUST, iunits.TOOLCHAIN_NODE})
        )
    }
    release = units[iunits.PIXI_RUST_RELEASE_DEPS_KEY]
    node = units[iunits.PIXI_NODE_DEPS_KEY]

    text = '[workspace]\nname = "acme"\n'
    text = splice.splice_block(
        text, node.desired_inner(), node.open_marker, node.close_marker, node.anchor
    )
    text = splice.splice_block(
        text,
        release.desired_inner(),
        release.open_marker,
        release.close_marker,
        release.anchor,
    )

    assert (
        splice.extract_block(text, node.open_marker, node.close_marker)
        == node.desired_inner()
    )
    assert (
        splice.extract_block(text, release.open_marker, release.close_marker)
        == release.desired_inner()
    )
    headers = [ln for ln in text.splitlines() if ln.strip() == "[dependencies]"]
    assert len(headers) == 1
    merged = tomllib.loads(text)["dependencies"]
    assert merged["cargo-edit"] == "0.13.11.*"
    assert merged["nodejs"] == "26.*"


def _git_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    return root


def test_consumer_root_redirects_subdir_to_git_root(tmp_path):
    root = _git_repo(tmp_path)
    subdir = root / "crates" / "core"
    subdir.mkdir(parents=True)
    resolved, redirected = verb._consumer_root(str(subdir))
    assert resolved == root.resolve()
    assert redirected == subdir.resolve()


def test_consumer_root_is_noop_at_the_git_root(tmp_path):
    root = _git_repo(tmp_path)
    resolved, redirected = verb._consumer_root(str(root))
    assert resolved == root.resolve()
    assert redirected is None


def test_consumer_root_bootstraps_in_place_off_git(tmp_path):
    resolved, redirected = verb._consumer_root(str(tmp_path))
    assert resolved == tmp_path.resolve()
    assert redirected is None


def test_install_from_subdir_operates_on_git_root_not_a_nested_consumer(
    tmp_path, capsys
):
    root = _git_repo(tmp_path)
    root_pin = "0123456789abcdef0123456789abcdef01234567"
    (root / ".shipit.toml").write_text(f'[shipit]\nversion = "{root_pin}"\n')
    subdir = root / "crates" / "core"
    subdir.mkdir(parents=True)

    assert verb.run(str(subdir), dry_run=True) == 0

    captured = capsys.readouterr()
    assert f"install: {root.resolve()} (dry-run)" in captured.out
    assert "subdirectory of the git working tree" in captured.err
    assert str(root.resolve()) in captured.err
    assert not (subdir / ".shipit.toml").exists()
    assert not (subdir / "pixi.toml").exists()


def test_install_default_invocation_from_subdir_operates_on_git_root(
    tmp_path, capsys, monkeypatch
):
    root = _git_repo(tmp_path)
    root_pin = "0123456789abcdef0123456789abcdef01234567"
    (root / ".shipit.toml").write_text(f'[shipit]\nversion = "{root_pin}"\n')
    subdir = root / "crates" / "core"
    subdir.mkdir(parents=True)
    monkeypatch.chdir(subdir)

    assert verb.run(dry_run=True) == 0

    captured = capsys.readouterr()
    assert f"install: {root.resolve()} (dry-run)" in captured.out
    assert "subdirectory of the git working tree" in captured.err
    assert str(root.resolve()) in captured.err
    assert not (subdir / ".shipit.toml").exists()
    assert not (subdir / "pixi.toml").exists()


def test_detect_toolchains_reads_tracked_manifests(tmp_path):
    root = _git_repo(tmp_path)
    (root / "crates" / "core" / "deep").mkdir(parents=True)
    (root / "crates" / "core" / "deep" / "Cargo.toml").write_text("[package]\n")
    (root / "web").mkdir()
    (root / "web" / "package.json").write_text("{}\n")
    (root / "pyproject.toml").write_text("[project]\n")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    assert irec.detect_toolchains(root) == frozenset(
        {iunits.TOOLCHAIN_RUST, iunits.TOOLCHAIN_NODE, iunits.TOOLCHAIN_PYTHON}
    )


def test_detect_toolchains_ignores_untracked_manifests(tmp_path):
    root = _git_repo(tmp_path)
    (root / "go.mod").write_text("module acme\n")
    assert irec.detect_toolchains(root) == frozenset()


def test_detect_toolchains_falls_back_to_root_manifests_off_git(tmp_path):
    (tmp_path / "package.json").write_text("{}\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "Cargo.toml").write_text("[package]\n")
    assert irec.detect_toolchains(tmp_path) == frozenset({iunits.TOOLCHAIN_NODE})


def test_detect_toolchains_clean_root_is_empty(tmp_path):
    assert irec.detect_toolchains(tmp_path) == frozenset()


def test_declared_signals_unions_node_for_a_declared_wasm_pack(tmp_path):
    (tmp_path / ".shipit.toml").write_text(
        '[artifacts.wasm]\nbuild = ["rust"]\nbundle = { composition = "wasm-pack" }\n'
    )
    assert verb._declared_signals(tmp_path) == {iunits.TOOLCHAIN_NODE}


def test_declared_signals_empty_without_a_wasm_pack_composition(tmp_path):
    (tmp_path / ".shipit.toml").write_text(
        '[artifacts.cli]\nbuild = ["rust"]\nbundle = { composition = "archive" }\n'
    )
    assert verb._declared_signals(tmp_path) == set()


def test_declared_signals_empty_without_config(tmp_path):
    assert verb._declared_signals(tmp_path) == set()


def test_declared_signals_refuses_an_unparseable_config(tmp_path):
    (tmp_path / ".shipit.toml").write_text("this is not = valid = toml\n")
    with pytest.raises(config.ConfigError, match=r"malformed"):
        verb._declared_signals(tmp_path)


def test_declared_signals_refuses_an_unparseable_artifact_map(tmp_path):
    (tmp_path / ".shipit.toml").write_text(
        "[artifacts.grammar]\n"
        'build = ["tree-sitter"]\n'
        'bundle = { composition = "tarball" }\n'
        'endpoints = ["gh-release", "conda"]\n'
    )
    with pytest.raises(config.ConfigError, match=r"missing `leg` and `payload`"):
        verb._declared_signals(tmp_path)


def test_wasm_pack_composition_delivers_the_node_deps_block(tmp_path):
    root = _git_repo(tmp_path)
    (root / "crates" / "wasm").mkdir(parents=True)
    (root / "crates" / "wasm" / "Cargo.toml").write_text("[package]\n")
    (root / ".shipit.toml").write_text(
        '[artifacts.wasm]\nbuild = ["rust"]\nbundle = { composition = "wasm-pack" }\n'
    )
    subprocess.run(["git", "add", "."], cwd=root, check=True)

    signals = irec.detect_toolchains(root) | verb._declared_signals(root)
    assert signals == {iunits.TOOLCHAIN_RUST, iunits.TOOLCHAIN_NODE}
    keys = {u.key for u in iunits.load_units(toolchains=signals)}
    assert iunits.PIXI_NODE_DEPS_KEY in keys


def test_plain_rust_repo_gets_no_node_deps_block(tmp_path):
    root = _git_repo(tmp_path)
    (root / "Cargo.toml").write_text("[package]\n")
    (root / ".shipit.toml").write_text(
        '[artifacts.cli]\nbuild = ["rust"]\nbundle = { composition = "archive" }\n'
    )
    subprocess.run(["git", "add", "."], cwd=root, check=True)

    signals = irec.detect_toolchains(root) | verb._declared_signals(root)
    assert signals == {iunits.TOOLCHAIN_RUST}
    keys = {u.key for u in iunits.load_units(toolchains=signals)}
    assert iunits.PIXI_NODE_DEPS_KEY not in keys
    assert iunits.PIXI_TREE_SITTER_DEPS_KEY not in keys


def test_declared_signals_unions_tree_sitter_for_a_declared_toolchain_leg(tmp_path):
    (tmp_path / ".shipit.toml").write_text('[toolchains]\n"." = "tree-sitter"\n')
    assert verb._declared_signals(tmp_path) == {iunits.TOOLCHAIN_TREE_SITTER}


def test_tree_sitter_toolchain_delivers_the_cli_block(tmp_path):
    root = _git_repo(tmp_path)
    (root / "grammar.js").write_text("module.exports = grammar({});\n")
    (root / ".shipit.toml").write_text(
        "[toolchains]\n"
        '"." = "tree-sitter"\n'
        "[artifacts.tree-sitter-demo]\n"
        'build = ["tree-sitter"]\n'
        'bundle = { composition = "tarball", leg = "tree-sitter", payload = [{ path = "src", required = true }] }\n'
    )
    subprocess.run(["git", "add", "."], cwd=root, check=True)

    signals = irec.detect_toolchains(root) | verb._declared_signals(root)
    assert signals == {iunits.TOOLCHAIN_TREE_SITTER}
    keys = {u.key for u in iunits.load_units(toolchains=signals)}
    assert iunits.PIXI_TREE_SITTER_DEPS_KEY in keys


def test_declared_signals_unions_lua_for_a_declared_toolchain_leg(tmp_path):
    (tmp_path / ".shipit.toml").write_text('[toolchains]\n"." = "lua"\n')
    assert verb._declared_signals(tmp_path) == {iunits.TOOLCHAIN_LUA}


def test_lua_toolchain_delivers_the_lint_block(tmp_path):
    root = _git_repo(tmp_path)
    (root / "lua" / "plugin").mkdir(parents=True)
    (root / "lua" / "plugin" / "init.lua").write_text(
        'local M = {}\nM.version = "0.1.0"\nreturn M\n'
    )
    (root / ".shipit.toml").write_text('[toolchains]\n"." = "lua"\n')
    subprocess.run(["git", "add", "."], cwd=root, check=True)

    signals = irec.detect_toolchains(root) | verb._declared_signals(root)
    assert signals == {iunits.TOOLCHAIN_LUA}
    keys = {u.key for u in iunits.load_units(toolchains=signals)}
    assert iunits.PIXI_LUA_DEPS_KEY in keys


def test_declared_endpoints_unions_conda_across_artifacts(tmp_path):
    (tmp_path / ".shipit.toml").write_text(
        "[artifacts.cli]\n"
        'build = ["rust"]\n'
        'endpoints = ["gh-release"]\n'
        "[artifacts.grammar]\n"
        'build = ["tree-sitter"]\n'
        'bundle = { composition = "tarball", leg = "tree-sitter", payload = [{ path = "src", required = true }] }\n'
        'endpoints = ["gh-release", "conda"]\n'
    )
    assert verb._declared_endpoints(tmp_path) == frozenset({"gh-release", "conda"})


def test_declared_endpoints_empty_without_a_conda_endpoint(tmp_path):
    (tmp_path / ".shipit.toml").write_text(
        '[artifacts.cli]\nbuild = ["rust"]\nendpoints = ["gh-release"]\n'
    )
    assert "conda" not in verb._declared_endpoints(tmp_path)


def test_declared_endpoints_empty_without_config(tmp_path):
    assert verb._declared_endpoints(tmp_path) == frozenset()


def test_declared_endpoints_refuses_an_unparseable_config(tmp_path):
    (tmp_path / ".shipit.toml").write_text("this is not = valid = toml\n")
    with pytest.raises(config.ConfigError, match=r"malformed"):
        verb._declared_endpoints(tmp_path)


def test_non_rust_conda_producer_gets_the_packager_block(tmp_path):
    root = _git_repo(tmp_path)
    (root / "grammar.js").write_text("module.exports = grammar({});\n")
    (root / ".shipit.toml").write_text(
        "[toolchains]\n"
        '"." = "tree-sitter"\n'
        "[artifacts.tree-sitter]\n"
        'build = ["tree-sitter"]\n'
        'bundle = { composition = "tarball", leg = "tree-sitter", payload = [{ path = "src", required = true }] }\n'
        'endpoints = ["gh-release", "conda"]\n'
    )
    subprocess.run(["git", "add", "."], cwd=root, check=True)

    toolchains = irec.detect_toolchains(root) | verb._declared_signals(root)
    endpoints = verb._declared_endpoints(root)
    assert iunits.TOOLCHAIN_RUST not in toolchains
    assert "conda" in endpoints
    keys = {
        u.key for u in iunits.load_units(toolchains=toolchains, endpoints=endpoints)
    }
    assert iunits.PIXI_CONDA_PACKAGER_KEY in keys
    assert iunits.PIXI_RUST_RELEASE_DEPS_KEY not in keys


def test_conda_packager_reconcile_is_not_current_without_it(tmp_path):
    root = _git_repo(tmp_path)
    (root / "grammar.js").write_text("module.exports = grammar({});\n")
    (root / ".shipit.toml").write_text(
        "[toolchains]\n"
        '"." = "tree-sitter"\n'
        "[artifacts.tree-sitter]\n"
        'build = ["tree-sitter"]\n'
        'bundle = { composition = "tarball", leg = "tree-sitter", payload = [{ path = "src", required = true }] }\n'
        'endpoints = ["gh-release", "conda"]\n'
    )
    (root / "pixi.toml").write_text(
        '[workspace]\nname = "acme"\nchannels = ["conda-forge"]\n'
        'platforms = ["linux-64"]\n'
    )
    subprocess.run(["git", "add", "."], cwd=root, check=True)

    toolchains = irec.detect_toolchains(root) | verb._declared_signals(root)
    endpoints = verb._declared_endpoints(root)
    units = iunits.load_units(toolchains=toolchains, endpoints=endpoints)
    retired = irec.load_retired()
    state = irec.gather(root, units, retired)
    plan = irec.reconcile(units, retired, state)
    added = {d.unit.key for d in plan.decisions if d.action == irec.ADD}
    assert iunits.PIXI_CONDA_PACKAGER_KEY in added


def test_install_refuses_an_unparseable_artifact_map(tmp_path, rec, capsys):
    root = _git_repo(tmp_path)
    (root / "grammar.js").write_text("module.exports = grammar({});\n")
    (root / ".shipit.toml").write_text(
        "[toolchains]\n"
        '"." = "tree-sitter"\n'
        "[artifacts.tree-sitter]\n"
        'build = ["tree-sitter"]\n'
        'bundle = { composition = "tarball" }\n'
        'endpoints = ["gh-release", "conda"]\n'
    )
    subprocess.run(["git", "add", "."], cwd=root, check=True)

    rc = verb.run(str(root), local=True)
    assert rc == 1

    captured = capsys.readouterr()
    out, err = captured.out, captured.err
    assert "missing `leg` and `payload`" in err
    assert iunits.PIXI_TREE_SITTER_DEPS_KEY not in out
    assert iunits.PIXI_CONDA_PACKAGER_KEY not in out
    assert not (root / "pixi.toml").exists()
    assert rec.calls == []
    assert rec.hook_activations == []


def test_non_conda_repo_gets_no_packager_block(tmp_path):
    root = _git_repo(tmp_path)
    (root / "Cargo.toml").write_text("[package]\n")
    (root / ".shipit.toml").write_text(
        '[artifacts.cli]\nbuild = ["rust"]\nendpoints = ["gh-release"]\n'
    )
    subprocess.run(["git", "add", "."], cwd=root, check=True)

    toolchains = irec.detect_toolchains(root) | verb._declared_signals(root)
    endpoints = verb._declared_endpoints(root)
    assert toolchains == {iunits.TOOLCHAIN_RUST}
    assert "conda" not in endpoints
    keys = {
        u.key for u in iunits.load_units(toolchains=toolchains, endpoints=endpoints)
    }
    assert iunits.PIXI_CONDA_PACKAGER_KEY not in keys
    assert iunits.PIXI_RUST_RELEASE_DEPS_KEY in keys


def test_rust_conda_migration_moves_rattler_build_in_one_reconcile(tmp_path, rec):
    old_rust_inner = (
        'cargo-edit = "0.13.11.*"\nwasm-pack = "0.15.*"\nrattler-build = "0.69.*"'
    )
    old_pristine = config.content_hash(old_rust_inner.encode("utf-8"))
    (tmp_path / "pixi.toml").write_text(
        "[workspace]\n"
        'name = "acme"\n'
        'channels = ["conda-forge"]\n'
        'platforms = ["linux-64"]\n'
        "\n"
        "[dependencies]\n"
        f"{iunits.PIXI_RUST_RELEASE_DEPS_OPEN}\n"
        f"{old_rust_inner}\n"
        f"{iunits.PIXI_RUST_RELEASE_DEPS_CLOSE}\n"
    )
    (tmp_path / ".shipit.toml").write_text(
        "[artifacts.cli]\n"
        'build = ["rust"]\n'
        'endpoints = ["gh-release", "conda"]\n'
        "\n"
        "[managed]\n"
        f'"{iunits.PIXI_RUST_RELEASE_DEPS_KEY}" = "{old_pristine}"\n'
    )
    (tmp_path / "AGENTS.md").write_text("# Acme\n")

    units = iunits.load_units(
        toolchains=frozenset({iunits.TOOLCHAIN_RUST}),
        endpoints=frozenset({"conda"}),
        platforms=frozenset({"linux-64"}),
    )
    retired = irec.load_retired()
    state = irec.gather(tmp_path, units, retired)
    plan = irec.reconcile(units, retired, state)

    assert plan.pixi_key_conflicts == ()
    conda = next(
        d for d in plan.decisions if d.unit.key == iunits.PIXI_CONDA_PACKAGER_KEY
    )
    assert conda.action == irec.ADD
    rust = next(
        d for d in plan.decisions if d.unit.key == iunits.PIXI_RUST_RELEASE_DEPS_KEY
    )
    assert rust.action == irec.UPDATE

    iapply.apply(plan, iapply.MODE_TREE)

    text = (tmp_path / "pixi.toml").read_text(encoding="utf-8")
    manifest = tomllib.loads(text)
    assert manifest["dependencies"]["rattler-build"] == "0.69.*"
    conda_inner = splice.extract_block(
        text, iunits.PIXI_CONDA_PACKAGER_OPEN, iunits.PIXI_CONDA_PACKAGER_CLOSE
    )
    rust_inner = splice.extract_block(
        text, iunits.PIXI_RUST_RELEASE_DEPS_OPEN, iunits.PIXI_RUST_RELEASE_DEPS_CLOSE
    )
    assert tomllib.loads(conda_inner)["rattler-build"] == "0.69.*"
    rust_keys = tomllib.loads(rust_inner)
    assert "rattler-build" not in rust_keys
    assert rust_keys["cargo-edit"] == "0.13.11.*"
    managed = config.load_managed(config.load(tmp_path / ".shipit.toml"))
    assert iunits.PIXI_CONDA_PACKAGER_KEY in managed


def test_a_declined_donor_block_keeps_its_key_a_conflict(tmp_path, rec):
    old_rust_inner = (
        'cargo-edit = "0.13.11.*"\nwasm-pack = "0.15.*"\nrattler-build = "0.69.*"'
    )
    old_pristine = config.content_hash(old_rust_inner.encode("utf-8"))
    (tmp_path / "pixi.toml").write_text(
        "[workspace]\n"
        'name = "acme"\n'
        'channels = ["conda-forge"]\n'
        'platforms = ["linux-64"]\n'
        "\n"
        "[dependencies]\n"
        f"{iunits.PIXI_RUST_RELEASE_DEPS_OPEN}\n"
        f"{old_rust_inner}\n"
        f"{iunits.PIXI_RUST_RELEASE_DEPS_CLOSE}\n"
    )
    (tmp_path / ".shipit.toml").write_text(
        "[artifacts.cli]\n"
        'build = ["rust"]\n'
        'endpoints = ["gh-release", "conda"]\n'
        "\n"
        "[managed.decline]\n"
        f'keep = ["{iunits.PIXI_RUST_RELEASE_DEPS_KEY}"]\n'
        "\n"
        "[managed]\n"
        f'"{iunits.PIXI_RUST_RELEASE_DEPS_KEY}" = "{old_pristine}"\n'
    )
    (tmp_path / "AGENTS.md").write_text("# Acme\n")

    units = iunits.load_units(
        toolchains=frozenset({iunits.TOOLCHAIN_RUST}),
        endpoints=frozenset({"conda"}),
        platforms=frozenset({"linux-64"}),
    )
    retired = irec.load_retired()
    state = irec.gather(tmp_path, units, retired)
    plan = irec.reconcile(units, retired, state)

    assert iunits.PIXI_RUST_RELEASE_DEPS_KEY in plan.declined
    assert plan.pixi_key_conflicts == (
        irec.PixiKeyConflict(
            unit_key=iunits.PIXI_CONDA_PACKAGER_KEY,
            anchor="[dependencies]",
            keys=("rattler-build",),
        ),
    )
    written = {d.unit.key for d in plan.decisions}
    assert iunits.PIXI_CONDA_PACKAGER_KEY not in written
    assert iunits.PIXI_RUST_RELEASE_DEPS_KEY not in written

    with pytest.raises(InstallError, match="pixi key conflict") as excinfo:
        iapply.apply(plan, iapply.MODE_TREE)
    assert iunits.PIXI_CONDA_PACKAGER_KEY in str(excinfo.value)

    text = (tmp_path / "pixi.toml").read_text(encoding="utf-8")
    assert tomllib.loads(text)["dependencies"]["rattler-build"] == "0.69.*"
    assert (
        splice.extract_block(
            text, iunits.PIXI_CONDA_PACKAGER_OPEN, iunits.PIXI_CONDA_PACKAGER_CLOSE
        )
        is None
    )


def test_declared_platforms_reads_the_workspace_table(tmp_path):
    (tmp_path / "pixi.toml").write_text(
        '[workspace]\nname = "acme"\nplatforms = ["osx-arm64", "win-64"]\n'
    )
    assert verb._declared_platforms(tmp_path) == frozenset({"osx-arm64", "win-64"})


def test_declared_platforms_reads_the_legacy_project_alias(tmp_path):
    (tmp_path / "pixi.toml").write_text(
        '[project]\nname = "acme"\nplatforms = ["linux-64"]\n'
    )
    assert verb._declared_platforms(tmp_path) == frozenset({"linux-64"})


def test_declared_platforms_absent_pixi_uses_the_seed_defaults(tmp_path):
    assert verb._declared_platforms(tmp_path) == frozenset(iunits.PIXI_SEED_PLATFORMS)
    assert "win-64" not in iunits.PIXI_SEED_PLATFORMS


def test_declared_platforms_present_manifest_without_platforms_is_empty(tmp_path):
    (tmp_path / "pixi.toml").write_text('[workspace]\nname = "acme"\n')
    assert verb._declared_platforms(tmp_path) == frozenset()


def test_declared_platforms_trusts_an_explicit_empty_list(tmp_path):
    (tmp_path / "pixi.toml").write_text('[workspace]\nname = "acme"\nplatforms = []\n')
    assert verb._declared_platforms(tmp_path) == frozenset()


def test_declared_platforms_ignores_a_scalar_workspace_table(tmp_path):
    (tmp_path / "pixi.toml").write_text('workspace = "foo"\n')
    assert verb._declared_platforms(tmp_path) == frozenset()


def test_declared_platforms_degrades_on_unparseable_pixi(tmp_path):
    (tmp_path / "pixi.toml").write_text("this is not = valid = toml\n")
    assert verb._declared_platforms(tmp_path) == frozenset(iunits.PIXI_SEED_PLATFORMS)


def test_existing_manifest_without_platforms_emits_no_lexd_target(tmp_path):
    (tmp_path / "pixi.toml").write_text('[workspace]\nname = "acme"\n')
    platforms = verb._declared_platforms(tmp_path)
    assert platforms == frozenset()
    units = {u.key: u for u in iunits.load_units(platforms=platforms)}
    feature = tomllib.loads(units[iunits.PIXI_LEXD_KEY].desired_inner())["feature"][
        "shipit-lexd"
    ]
    assert "target" not in feature
    assert "dependencies" not in feature


def _plan_with_toolchains(root, toolchains: frozenset) -> irec.Plan:
    units = iunits.load_units(toolchains=toolchains)
    retired = irec.load_retired()
    state = irec.gather(Path(root), units, retired)
    return irec.reconcile(units, retired, state)


_CONSUMER_PIXI_WITH_NODE = """\
[workspace]
channels = ["conda-forge"]
name = "acme"
platforms = ["linux-64"]

[dependencies]
nodejs = "22.*"
"""


def test_node_block_cannot_be_delivered_when_the_consumer_already_pins_its_keys(
    tmp_path,
):
    (tmp_path / "pixi.toml").write_text(_CONSUMER_PIXI_WITH_NODE)
    plan = _plan_with_toolchains(tmp_path, frozenset({iunits.TOOLCHAIN_NODE}))

    assert plan.pixi_key_conflicts == (
        irec.PixiKeyConflict(
            unit_key=iunits.PIXI_NODE_DEPS_KEY,
            anchor=iunits.PIXI_NODE_DEPS_ANCHOR,
            keys=("nodejs",),
        ),
    )
    keys = {d.unit.key for d in plan.decisions}
    assert iunits.PIXI_NODE_DEPS_KEY not in keys
    assert iunits.PIXI_LINT_DEPS_KEY in keys
    warnings = verb.format_plan_warnings(plan)
    assert "pixi key conflict" in warnings
    assert "nodejs" in warnings
    assert "delete this repo's own entry" in warnings
    assert "[managed.decline]" in warnings
    assert f'keep = ["{iunits.PIXI_NODE_DEPS_KEY}"]' in warnings


def test_a_key_conflicted_block_refuses_instead_of_silently_under_delivering(
    tmp_path, rec
):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    (tmp_path / "pixi.toml").write_text(_CONSUMER_PIXI_WITH_NODE)
    plan = _plan_with_toolchains(tmp_path, frozenset({iunits.TOOLCHAIN_NODE}))

    with pytest.raises(InstallError, match="pixi key conflict") as excinfo:
        iapply.apply(plan, iapply.MODE_TREE)
    assert "nodejs" in str(excinfo.value)
    assert iunits.PIXI_NODE_DEPS_KEY in str(excinfo.value)

    text = (tmp_path / "pixi.toml").read_text(encoding="utf-8")
    assert tomllib.loads(text)["dependencies"]["nodejs"] == "22.*"
    assert iunits.PIXI_NODE_DEPS_OPEN not in text
    assert not (tmp_path / ".shipit.toml").exists()


def test_declining_the_conflicted_block_is_the_supported_override(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    (tmp_path / "pixi.toml").write_text(_CONSUMER_PIXI_WITH_NODE)
    (tmp_path / ".shipit.toml").write_text(
        f'[managed.decline]\nkeep = ["{iunits.PIXI_NODE_DEPS_KEY}"]\n'
    )
    plan = _plan_with_toolchains(tmp_path, frozenset({iunits.TOOLCHAIN_NODE}))

    assert plan.pixi_key_conflicts == ()
    assert iunits.PIXI_NODE_DEPS_KEY in plan.declined
    iapply.apply(plan, iapply.MODE_TREE)

    text = (tmp_path / "pixi.toml").read_text(encoding="utf-8")
    assert tomllib.loads(text)["dependencies"]["nodejs"] == "22.*"
    assert iunits.PIXI_NODE_DEPS_OPEN not in text
    managed = config.load_managed(config.load(tmp_path / ".shipit.toml"))
    assert iunits.PIXI_NODE_DEPS_KEY not in managed
    assert iunits.PIXI_LINT_DEPS_KEY in managed


def test_key_conflict_refusal_survives_a_no_op_plan(tmp_path, monkeypatch):
    conflict = irec.PixiKeyConflict(
        unit_key=iunits.PIXI_NODE_DEPS_KEY,
        anchor=iunits.PIXI_NODE_DEPS_ANCHOR,
        keys=("nodejs",),
    )
    noop_conflict = dc_replace(
        _plan(tmp_path),
        decisions=(),
        retired=(),
        seeds=(),
        current_pin=None,
        target_pin=None,
        pixi_key_conflicts=(conflict,),
        claude_skills_link=irec.ClaudeSkillsLink(irec.LINK_NOOP),
    )
    assert noop_conflict.nothing_to_do
    with pytest.raises(InstallError, match="pixi key conflict"):
        iapply.reject_pixi_key_conflicts(noop_conflict)

    monkeypatch.setattr(verb, "reconcile", lambda *a, **k: noop_conflict)
    assert verb.run(str(tmp_path), local=True) == 1
    assert verb.run(str(tmp_path), push=True) == 1
    assert verb.run(str(tmp_path), pr=True) == 1
    assert verb.run(str(tmp_path)) == 1
    assert not (tmp_path / ".shipit.toml").exists()
    assert verb.run(str(tmp_path), dry_run=True) == 0


def test_the_advertised_decline_remedy_is_never_advertised_as_invalid_toml(
    tmp_path, monkeypatch, capsys
):
    conflict = irec.PixiKeyConflict(
        unit_key=iunits.PIXI_NODE_DEPS_KEY,
        anchor=iunits.PIXI_NODE_DEPS_ANCHOR,
        keys=("nodejs",),
    )
    monkeypatch.setattr(
        verb,
        "reconcile",
        lambda *a, **k: dc_replace(
            _plan(tmp_path),
            decisions=(),
            retired=(),
            seeds=(),
            current_pin=None,
            target_pin=None,
            pixi_key_conflicts=(conflict,),
            claude_skills_link=irec.ClaudeSkillsLink(irec.LINK_NOOP),
        ),
    )
    assert verb.run(str(tmp_path)) == 1
    rendered = capsys.readouterr().err
    assert "error: pixi key conflict" in rendered
    assert "[managed.decline] keep" not in rendered

    remedy = f'[managed.decline]\nkeep = ["{iunits.PIXI_NODE_DEPS_KEY}"]\n'
    assert config.load_declines(tomllib.loads(remedy), remedy) == (
        iunits.PIXI_NODE_DEPS_KEY,
    )


def test_a_key_conflict_message_never_assumes_the_collision_is_a_version_pin(
    tmp_path,
):
    (tmp_path / "pixi.toml").write_text(
        _CONSUMER_PIXI_WITH_NODE.replace("nodejs", "cmake")
        + '\n[tasks]\nlogs = "tail -f my.log"\n'
    )
    plan = _plan(tmp_path)

    conflict = next(c for c in plan.pixi_key_conflicts if c.unit_key == iunits.PIXI_KEY)
    assert conflict.anchor == "[tasks]"
    assert conflict.keys == ("logs",)

    message = irec.format_pixi_key_conflict(conflict)
    assert "logs" in message and "[tasks]" in message
    for pin_framing in (
        "also pins",
        "fleet pin",
        "managed pin",
        "hand-pins",
        "version",
    ):
        assert pin_framing not in message
    assert "keep its own declaration" in message
    with pytest.raises(InstallError) as excinfo:
        iapply.reject_pixi_key_conflicts(plan)
    assert "version" not in str(excinfo.value)
    assert "off the managed pin" not in str(excinfo.value)
    assert "under-deliver its managed set" in str(excinfo.value)


def test_node_block_delivers_when_the_consumer_has_no_clashing_key(tmp_path):
    (tmp_path / "pixi.toml").write_text(
        _CONSUMER_PIXI_WITH_NODE.replace("nodejs", "cmake")
    )
    plan = _plan_with_toolchains(tmp_path, frozenset({iunits.TOOLCHAIN_NODE}))
    assert plan.pixi_key_conflicts == ()
    node = next(d for d in plan.decisions if d.unit.key == iunits.PIXI_NODE_DEPS_KEY)
    assert node.action == irec.ADD


def test_a_spliced_block_is_not_a_key_conflict(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    toolchains = frozenset({iunits.TOOLCHAIN_NODE})
    plan = _plan_with_toolchains(tmp_path, toolchains)
    iapply.apply(plan, iapply.MODE_TREE)

    again = _plan_with_toolchains(tmp_path, toolchains)
    assert again.pixi_key_conflicts == ()
    node = next(d for d in again.decisions if d.unit.key == iunits.PIXI_NODE_DEPS_KEY)
    assert node.action == irec.NOOP


def test_key_conflict_guard_fails_open_on_an_unparseable_pixi_toml(tmp_path):
    (tmp_path / "pixi.toml").write_text("[[[ not toml\n")
    units = iunits.load_units(toolchains=frozenset({iunits.TOOLCHAIN_NODE}))
    state = irec.gather(Path(tmp_path), units, irec.load_retired())
    plan = irec.reconcile(units, irec.load_retired(), state)
    assert plan.pixi_key_conflicts == ()


def test_key_conflict_guard_covers_the_nested_lint_feature_anchor(tmp_path):
    (tmp_path / "pixi.toml").write_text(
        '[workspace]\nchannels = ["conda-forge"]\nname = "acme"\n'
        'platforms = ["linux-64"]\n\n[feature.lint.dependencies]\nrust = "1.90.*"\n'
    )
    plan = _plan_with_toolchains(tmp_path, frozenset({iunits.TOOLCHAIN_RUST}))
    assert plan.pixi_key_conflicts == (
        irec.PixiKeyConflict(
            unit_key=iunits.PIXI_RUST_DEPS_KEY,
            anchor=iunits.PIXI_LINT_DEPS_ANCHOR,
            keys=("rust",),
        ),
    )


def test_consumer_rust_pin_conflicts_the_toolchain_block_alone(tmp_path):
    (tmp_path / "pixi.toml").write_text(
        '[workspace]\nchannels = ["conda-forge"]\nname = "acme"\n'
        'platforms = ["linux-64"]\n\n[dependencies]\nrust = "1.90.*"\n'
    )
    plan = _plan_with_toolchains(tmp_path, frozenset({iunits.TOOLCHAIN_RUST}))

    assert plan.pixi_key_conflicts == (
        irec.PixiKeyConflict(
            unit_key=iunits.PIXI_RUST_RELEASE_TOOLCHAIN_KEY,
            anchor=iunits.PIXI_NODE_DEPS_ANCHOR,
            keys=("rust",),
        ),
    )
    keys = {d.unit.key for d in plan.decisions}
    assert iunits.PIXI_RUST_RELEASE_TOOLCHAIN_KEY not in keys
    assert iunits.PIXI_RUST_RELEASE_DEPS_KEY in keys
    assert iunits.PIXI_RUST_DEPS_KEY in keys
    deps = next(
        d.unit
        for d in plan.decisions
        if d.unit.key == iunits.PIXI_RUST_RELEASE_DEPS_KEY
    )
    assert "rust-std-wasm32-unknown-unknown" not in tomllib.loads(deps.desired_inner())


_CONSUMER_PIXI_WITH_FEATURE_TEST_TASK = """\
[workspace]
channels = ["conda-forge"]
name = "acme"
platforms = ["linux-64"]

[feature.test.tasks]
test = "cargo nextest run"

[environments]
test = ["test"]
"""


def test_test_task_block_is_skipped_when_a_feature_defines_the_task(tmp_path):
    (tmp_path / "pixi.toml").write_text(_CONSUMER_PIXI_WITH_FEATURE_TEST_TASK)
    plan = _plan(tmp_path)

    assert plan.pixi_task_conflicts == (
        irec.PixiTaskConflict(
            unit_key=iunits.PIXI_TEST_TASK_KEY,
            task="test",
            features=("test",),
        ),
    )
    keys = {d.unit.key for d in plan.decisions}
    assert iunits.PIXI_TEST_TASK_KEY not in keys
    assert iunits.PIXI_KEY in keys
    warnings = verb.format_plan_warnings(plan)
    assert "pixi block skipped" in warnings
    assert "[feature.test.tasks]" in warnings
    assert "ambiguous" in warnings


def test_task_conflict_message_quotes_a_dotted_feature_name():
    dotted = irec.format_pixi_task_conflict(
        irec.PixiTaskConflict(
            unit_key=iunits.PIXI_TEST_TASK_KEY,
            task="test",
            features=("ruamel.yaml", "plain"),
        )
    )
    assert '[feature."ruamel.yaml".tasks]' in dotted
    assert "[feature.plain.tasks]" in dotted
    assert "[feature.ruamel.yaml.tasks]" not in dotted


def test_task_conflict_message_escapes_quotes_and_backslashes():
    weird = irec.format_pixi_task_conflict(
        irec.PixiTaskConflict(
            unit_key=iunits.PIXI_TEST_TASK_KEY,
            task="test",
            features=('a"b', "c\\d"),
        )
    )
    assert '[feature."a\\"b".tasks]' in weird
    assert '[feature."c\\\\d".tasks]' in weird
    for header, expected in (
        ('[feature."a\\"b".tasks]', 'a"b'),
        ('[feature."c\\\\d".tasks]', "c\\d"),
    ):
        parsed = tomllib.loads(f"{header}\nx = 1\n")
        assert parsed["feature"][expected]["tasks"] == {"x": 1}


def test_test_task_block_delivers_when_no_feature_defines_it(tmp_path):
    (tmp_path / "pixi.toml").write_text(
        _CONSUMER_PIXI_WITH_FEATURE_TEST_TASK.replace("test =", "e2e =", 1)
    )
    plan = _plan(tmp_path)
    assert plan.pixi_task_conflicts == ()
    decision = next(
        d for d in plan.decisions if d.unit.key == iunits.PIXI_TEST_TASK_KEY
    )
    assert decision.action == irec.ADD


def test_test_task_block_delivers_when_the_feature_is_not_env_enabled(tmp_path):
    (tmp_path / "pixi.toml").write_text(
        "[workspace]\n"
        'channels = ["conda-forge"]\n'
        'name = "acme"\n'
        'platforms = ["linux-64"]\n\n'
        "[feature.test.tasks]\n"
        'test = "cargo nextest run"\n\n'
        "[environments]\n"
        'dev = ["lint"]\n'
    )
    plan = _plan(tmp_path)
    assert plan.pixi_task_conflicts == ()
    decision = next(
        d for d in plan.decisions if d.unit.key == iunits.PIXI_TEST_TASK_KEY
    )
    assert decision.action == irec.ADD


def test_a_spliced_test_task_block_is_not_a_task_conflict(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    plan = _plan(tmp_path)
    iapply.apply(plan, iapply.MODE_TREE)

    again = _plan(tmp_path)
    assert again.pixi_task_conflicts == ()
    decision = next(
        d for d in again.decisions if d.unit.key == iunits.PIXI_TEST_TASK_KEY
    )
    assert decision.action == irec.NOOP


def test_a_consumer_test_task_in_the_tasks_table_is_the_key_conflict_guards_case(
    tmp_path,
):
    (tmp_path / "pixi.toml").write_text(
        '[workspace]\nchannels = ["conda-forge"]\nname = "acme"\n'
        'platforms = ["linux-64"]\n\n[tasks]\ntest = "pytest"\n'
    )
    plan = _plan(tmp_path)
    assert plan.pixi_task_conflicts == ()
    assert any(
        c.unit_key == iunits.PIXI_TEST_TASK_KEY and c.keys == ("test",)
        for c in plan.pixi_key_conflicts
    )
    assert iunits.PIXI_TEST_TASK_KEY not in {d.unit.key for d in plan.decisions}


def test_gather_reads_pixi_toml_once_and_every_guard_shares_that_snapshot(
    tmp_path, monkeypatch
):
    conflicted = (
        "[workspace]\n"
        'channels = ["conda-forge"]\n'
        'name = "acme"\n'
        'platforms = ["linux-64"]\n\n'
        "[feature.shipit-lexd]\n"
        'platforms = ["linux-64"]\n\n'
        "[feature.test.tasks]\n"
        'test = "cargo nextest run"\n\n'
        "[environments]\n"
        'test = ["test"]\n'
    )
    clean = iunits.pixi_manifest_seed("acme")
    (tmp_path / "pixi.toml").write_text(clean)

    reads: list[Path] = []

    def _changing_source(root: Path) -> str:
        reads.append(root)
        return conflicted if len(reads) == 1 else clean

    monkeypatch.setattr(irec, "_read_pixi_text", _changing_source)
    units = iunits.load_units(platforms=frozenset({"linux-64"}))
    state = irec.gather(tmp_path, units, irec.load_retired())

    assert len(reads) == 1
    assert state.pixi_text == conflicted
    assert [c.unit_key for c in state.pixi_task_conflicts] == [
        iunits.PIXI_TEST_TASK_KEY
    ]
    assert [c.unit_key for c in state.pixi_table_conflicts] == [iunits.PIXI_LEXD_KEY]


def test_shipits_own_repo_keeps_its_feature_test_task_authoritative():
    root = Path(__file__).resolve().parents[1]
    units = iunits.load_units()
    consumer_hashes = {u.key: irec.consumer_hash(root, u) for u in units}
    conflicts = irec._pixi_task_conflicts(
        irec._read_pixi_text(root), units, consumer_hashes
    )
    assert any(
        c.unit_key == iunits.PIXI_TEST_TASK_KEY and c.task == "test" for c in conflicts
    )


def test_shipits_own_install_is_never_refused_by_the_key_conflict_guard():
    root = Path(__file__).resolve().parents[1]
    units = iunits.load_units(
        toolchains=frozenset({iunits.TOOLCHAIN_PYTHON}),
        platforms=verb._declared_platforms(root),
    )
    retired = irec.load_retired()
    plan = irec.reconcile(units, retired, irec.gather(root, units, retired))

    assert plan.pixi_key_conflicts == ()
    iapply.reject_pixi_key_conflicts(plan)
    assert [c.unit_key for c in plan.pixi_task_conflicts] == [iunits.PIXI_TEST_TASK_KEY]


def test_fresh_install_lays_down_the_session_bootstrap_set_idempotently(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    result = _apply(tmp_path)
    assert result.mode == iapply.MODE_TREE

    agent_launcher = tmp_path / "agent-start"
    assert agent_launcher.is_file()
    assert os.access(agent_launcher, os.X_OK)
    assert "--worktree" in agent_launcher.read_text()
    assert "session codex" in agent_launcher.read_text()

    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    entries = settings["hooks"]["SessionStart"]
    assert any(
        splice.is_shipit_hook(e, iunits.SETTINGS_SESSIONSTART_MARKER) for e in entries
    )

    managed = config.load_managed(config.load(tmp_path / ".shipit.toml"))
    assert iunits.AGENT_LAUNCHER_FILE in managed
    assert iunits.SETTINGS_SESSIONSTART_KEY in managed

    rec.calls.clear()
    agent_launcher_before = agent_launcher.read_bytes()
    settings_before = (tmp_path / ".claude" / "settings.json").read_bytes()
    again = _plan(tmp_path)
    assert again.nothing_to_do
    assert rec.calls == []
    assert agent_launcher.read_bytes() == agent_launcher_before
    assert (tmp_path / ".claude" / "settings.json").read_bytes() == settings_before


def _lay_down_launcher(tmp_path: Path) -> Path:
    unit = {u.key: u for u in iunits.load_units()}[iunits.AGENT_LAUNCHER_FILE]
    path = tmp_path / unit.dest
    path.write_bytes(unit.content)
    path.chmod(0o755)
    return path


def _fake_cli(tmp_path: Path, name: str) -> dict[str, str]:
    fakedir = tmp_path / "fakepath"
    fakedir.mkdir(exist_ok=True)
    fake = fakedir / name
    fake.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$@"\n')
    fake.chmod(0o755)
    return {"PATH": str(fakedir) + os.pathsep + os.environ.get("PATH", "")}


def test_agent_start_claude_execs_claude_with_a_minted_session_id(tmp_path: Path):
    agent_start = _lay_down_launcher(tmp_path)
    env = _fake_cli(tmp_path, "claude")

    def launch(*args: str) -> list[str]:
        proc = subprocess.run(
            [str(agent_start), "claude", *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert proc.returncode == 0, proc.stderr
        return proc.stdout.splitlines()

    argv = launch("extra", "--args")
    assert argv[0] == "--worktree"
    assert argv[1].startswith("sess-")
    assert argv[2:] == ["extra", "--args"]

    assert launch()[1] != argv[1]


def test_agent_start_resolves_the_repo_through_a_symlinked_launcher_path(
    tmp_path: Path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    agent_start = _lay_down_launcher(repo)
    linkdir = tmp_path / "home" / "bin"
    linkdir.mkdir(parents=True)
    link = linkdir / "agent-start"
    link.symlink_to(agent_start)

    env = _fake_cli(tmp_path, "claude")
    fake = tmp_path / "fakepath" / "claude"
    fake.write_text("#!/usr/bin/env bash\npwd\n")
    fake.chmod(0o755)

    proc = subprocess.run(
        [str(link), "claude"],
        env=env,
        cwd=str(linkdir),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 0, proc.stderr
    launched_from = Path(proc.stdout.strip())
    assert launched_from.samefile(repo), f"rooted in {launched_from}, wanted {repo}"


def test_agent_start_resolves_the_repo_through_a_symlinked_bin_and_relative_link(
    tmp_path: Path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    agent_start = _lay_down_launcher(repo)
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "agent-start").symlink_to(Path("..") / repo.name / agent_start.name)
    alias = tmp_path / "alias"
    alias.mkdir()
    (alias / "bin").symlink_to(tools)
    decoy = alias / "repo"
    decoy.mkdir(parents=True)

    env = _fake_cli(tmp_path, "claude")
    fake = tmp_path / "fakepath" / "claude"
    fake.write_text("#!/usr/bin/env bash\npwd\n")
    fake.chmod(0o755)

    proc = subprocess.run(
        [str(alias / "bin" / "agent-start"), "claude"],
        env=env,
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 0, proc.stderr
    launched_from = Path(proc.stdout.strip())
    assert not launched_from.samefile(decoy), "rooted in the decoy checkout"
    assert launched_from.samefile(repo), f"rooted in {launched_from}, wanted {repo}"


def test_agent_start_codex_execs_the_pinned_launcher(tmp_path: Path):
    agent_start = _lay_down_launcher(tmp_path)
    env = _fake_cli(tmp_path, "codex")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake_shipit = bindir / "shipit"
    fake_shipit.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$@"\n')
    fake_shipit.chmod(0o755)

    proc = subprocess.run(
        [str(agent_start), "codex", "--model", "foo"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines() == ["session", "codex", "--model", "foo"]


def test_agent_start_scrubs_the_inherited_worker_agent_identity_exports(
    tmp_path: Path,
):
    agent_start = _lay_down_launcher(tmp_path)
    env = _fake_cli(tmp_path, "claude")
    fake = tmp_path / "fakepath" / "claude"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        'echo "role=${SHIPIT_LOG_CTX_ROLE-ABSENT}"\n'
        'echo "agent=${SHIPIT_LOG_CTX_AGENT-ABSENT}"\n'
        'echo "run=${SHIPIT_LOG_CTX_RUN-ABSENT}"\n'
        'echo "pr=${SHIPIT_LOG_CTX_PR-ABSENT}"\n'
    )

    proc = subprocess.run(
        [str(agent_start), "claude"],
        env={
            **env,
            "SHIPIT_LOG_CTX_ROLE": "implementer",
            "SHIPIT_LOG_CTX_AGENT": "deadbeef",
            "SHIPIT_LOG_CTX_RUN": "77",
            "SHIPIT_LOG_CTX_PR": "632",
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines() == [
        "role=ABSENT",
        "agent=ABSENT",
        "run=ABSENT",
        "pr=632",
    ]


def test_agent_start_launches_from_the_repo_root(tmp_path: Path):
    agent_start = _lay_down_launcher(tmp_path)
    env = _fake_cli(tmp_path, "claude")
    (tmp_path / "fakepath" / "claude").write_text("#!/usr/bin/env bash\npwd -P\n")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    proc = subprocess.run(
        [str(agent_start), "claude"],
        env=env,
        cwd=elsewhere,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == os.path.realpath(tmp_path)


def test_agent_start_rejects_an_unknown_or_missing_agent(tmp_path: Path):
    agent_start = _lay_down_launcher(tmp_path)

    proc = subprocess.run(
        [str(agent_start), "goose"], capture_output=True, text=True, timeout=10
    )
    assert proc.returncode == 64
    assert "unknown agent 'goose'" in proc.stderr

    proc = subprocess.run(
        [str(agent_start)], capture_output=True, text=True, timeout=10
    )
    assert proc.returncode == 64
    assert "usage:" in proc.stderr


def test_agent_start_fails_loud_when_the_cli_is_not_on_path(tmp_path: Path):
    agent_start = _lay_down_launcher(tmp_path)

    bindir = tmp_path / "onlybash"
    bindir.mkdir()
    for tool in ("bash", "dirname"):
        binary = shutil.which(tool)
        assert binary is not None
        (bindir / tool).symlink_to(binary)
    proc = subprocess.run(
        [str(agent_start), "claude"],
        env={"PATH": str(bindir)},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 127
    assert "claude CLI is not on PATH" in proc.stderr
    assert "install Claude Code first" in proc.stderr


def test_settings_hook_splice_preserves_other_settings():
    consumer = json.dumps(
        {
            "permissions": {"allow": ["Bash(ls:*)"]},
            "hooks": {
                "SessionStart": [{"hooks": [{"type": "command", "command": "echo hi"}]}]
            },
        }
    )
    inner = json.dumps(
        {
            "matcher": "Edit|Write",
            "hooks": [
                {"type": "command", "command": "pixi run shipit hook pretooluse"}
            ],
        }
    )
    out = splice.splice_settings_hook(consumer, inner)
    data = json.loads(out)
    assert data["permissions"] == {"allow": ["Bash(ls:*)"]}
    assert data["hooks"]["SessionStart"][0]["hooks"][0]["command"] == "echo hi"
    assert splice.extract_settings_hook(out) == iunits.canonical_hook_entry(
        json.loads(inner)
    )


def _unit(key):
    return next(u for u in iunits.load_units() if u.key == key)


def test_settings_hook_splice_is_idempotent_and_replaces_in_place():
    inner = _unit(iunits.SETTINGS_KEY).desired_inner()
    once = splice.splice_settings_hook("", inner)
    twice = splice.splice_settings_hook(once, inner)
    assert twice == once
    pre = json.loads(twice)["hooks"]["PreToolUse"]
    assert sum(splice.is_shipit_hook(e) for e in pre) == 1


def test_settings_hook_extract_is_none_when_absent():
    assert splice.extract_settings_hook("") is None
    assert splice.extract_settings_hook("{}") is None
    other = json.dumps(
        {"hooks": {"PreToolUse": [{"hooks": [{"command": "echo other"}]}]}}
    )
    assert splice.extract_settings_hook(other) is None


def test_settings_hook_extract_flags_malformed_as_non_none():
    assert splice.extract_settings_hook("not json") is not None
    assert splice.extract_settings_hook("{bad json,,}") is not None
    assert splice.extract_settings_hook("[1, 2, 3]") is not None
    assert splice.extract_settings_hook('"a string"') is not None


def test_is_shipit_hook_is_defensive_against_malformed_entries():
    assert splice.is_shipit_hook({"hooks": None}) is False
    assert splice.is_shipit_hook({"hooks": "not-a-list"}) is False
    assert splice.is_shipit_hook({"hooks": [None, "x", 7]}) is False
    assert splice.is_shipit_hook({}) is False
    assert splice.is_shipit_hook("not-a-dict") is False
    assert splice.is_shipit_hook(None) is False
    assert splice.is_shipit_hook({"hooks": [{"command": None}]}) is False
    assert splice.is_shipit_hook({"hooks": [{"command": 7}]}) is False
    assert splice.is_shipit_hook({"hooks": [{}]}) is False


def test_settings_hook_splice_preserves_a_malformed_file_verbatim():
    inner = _unit(iunits.SETTINGS_KEY).desired_inner()
    malformed = '{ "permissions": [ this is not json ]\n'
    assert splice.splice_settings_hook(malformed, inner) == malformed
    not_an_object = "[1, 2, 3]\n"
    assert splice.splice_settings_hook(not_an_object, inner) == not_an_object


def test_settings_hook_reconciles_through_the_four_cases():
    unit = _unit(iunits.SETTINGS_KEY)
    desired = unit.desired_hash()
    extract = splice.extract_settings_hook
    h = lambda inner: config.content_hash(inner.encode("utf-8"))  # noqa: E731

    assert (
        irec.decide(consumer_hash=None, pristine_hash=None, desired_hash=desired)
        == irec.ADD
    )
    on_disk = splice.splice_settings_hook("", unit.desired_inner())
    cur = h(extract(on_disk))
    assert cur == desired
    assert (
        irec.decide(consumer_hash=cur, pristine_hash=desired, desired_hash=desired)
        == irec.NOOP
    )
    edited = on_disk.replace("Edit|Write|MultiEdit|NotebookEdit", "Edit")
    cedit = h(extract(edited))
    assert cedit != desired
    assert (
        irec.decide(consumer_hash=cedit, pristine_hash=desired, desired_hash=desired)
        == irec.OVERRIDE
    )


class _GhRecorder:
    def __init__(self):
        self.calls = []
        self.branch = "main"
        self.tracked = set()
        self.head_carries = None
        self.original_carries = set()
        self.tree_paths_refs = []
        self.pr_body = None
        self.hook_activations = []
        self.commit_paths = ()
        self.commit_all_message = None
        self.rm_cached_paths = ()
        self.commit_no_verify = None
        self.push_no_verify = None
        self.default_branch_after_fetch = None
        self.staged_query = ()
        self.pr_index_file = None
        self.add_index_files = []
        self.rm_cached_index_file = None
        self.staged_index_file = None
        self.commit_all_index_file = None

    def activate_hooks(self, root):
        self.hook_activations.append(root)
        return _exec_result(0)

    def default_branch(self, *, cwd, remote="origin"):
        self.default_branch_after_fetch = any(c[0] == "fetch" for c in self.calls)
        return getattr(self, "_default_branch", "main")

    def fetch(self, *, cwd, remote="origin"):
        self.calls.append(("fetch", remote))

    def switch_create(self, branch, *, cwd):
        self.calls.append(("switch", branch))
        self.branch = branch

    def reset_soft(self, ref, *, cwd):
        self.calls.append(("reset", ref))

    def read_tree(self, ref, *, cwd, index_file):
        self.calls.append(("read_tree", ref))
        self.pr_index_file = index_file

    def switch(self, branch, *, cwd):
        self.calls.append(("switch_back", branch))
        self.branch = branch

    def ls_files_matching(self, pathspecs, *, cwd):
        self.ls_files_pathspecs = tuple(pathspecs)
        return sorted(self.tracked.intersection(pathspecs))

    def tree_paths(self, ref, pathspecs, *, cwd):
        self.tree_paths_refs.append(ref)
        if ref != "HEAD":
            return sorted(self.original_carries.intersection(pathspecs))
        if self.head_carries is None:
            return sorted(pathspecs)
        return sorted(self.head_carries.intersection(pathspecs))

    def add(self, paths, *, cwd, index_file=None):
        self.calls.append(("add", tuple(paths)))
        self.add_index_files.append(index_file)

    def rm_cached(self, paths, *, cwd, index_file=None):
        self.calls.append(("rm_cached", tuple(paths)))
        self.rm_cached_paths = tuple(paths)
        self.rm_cached_index_file = index_file

    def staged_paths(self, paths, *, cwd, index_file=None):
        self.staged_query = tuple(paths)
        self.staged_index_file = index_file
        if getattr(self, "_no_staged", False):
            return []
        return sorted(paths)

    def reset_index(self, *, cwd):
        self.calls.append(("reset_index", None))

    def commit(self, message, paths, *, cwd, no_verify=False):
        self.calls.append(("commit", message))
        self.commit_paths = tuple(paths)
        self.commit_no_verify = no_verify

    def commit_all(self, message, *, cwd, no_verify=False, index_file=None):
        self.calls.append(("commit", message))
        self.commit_all_message = message
        self.commit_no_verify = no_verify
        self.commit_all_index_file = index_file

    def push(self, branch, *, cwd, remote="origin", force=False, no_verify=False):
        self.calls.append(("push", branch))
        self.push_no_verify = no_verify

    def current_branch(self, *, cwd):
        return self.branch

    def pr_url_for_head(self, branch, *, cwd=None):
        return None

    def pr_create(self, *, head, title, body, draft, cwd, base=None, **kw):
        self.calls.append(("pr_create", draft))
        self.pr_body = body
        self.pr_base = base
        return "https://github.com/acme/repo/pull/1"

    def names(self):
        return [c[0] for c in self.calls]


@pytest.fixture
def rec(monkeypatch):
    r = _GhRecorder()
    for name in (
        "switch_create",
        "switch",
        "read_tree",
        "add",
        "ls_files_matching",
        "tree_paths",
        "rm_cached",
        "staged_paths",
        "reset_index",
        "commit",
        "commit_all",
        "push",
        "current_branch",
        "default_branch",
        "fetch",
        "reset_soft",
    ):
        monkeypatch.setattr(git, name, getattr(r, name))
    for name in ("pr_url_for_head", "pr_create"):
        monkeypatch.setattr(gh, name, getattr(r, name))
    monkeypatch.setattr(iapply, "_shipit_version", lambda: "testhash")
    monkeypatch.setattr(iapply, "_activate_hooks", r.activate_hooks)
    monkeypatch.setattr(selfcert, "certify", _cert_ok)
    monkeypatch.setattr(selfcert, "consumer_debt", lambda root, **kw: None)
    return r


def test_dry_run_has_no_side_effects(tmp_path, rec):
    rc = verb.run(str(tmp_path), dry_run=True)
    assert rc == 0
    assert not (tmp_path / ".shipit.toml").exists()
    assert not (tmp_path / ".shipit-skills").exists()
    assert rec.calls == []
    assert rec.hook_activations == []


def test_fresh_install_writes_set_and_opens_draft_pr(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n\nConsumer text.\n")
    result = _apply(tmp_path, iapply.MODE_PR)

    assert result.mode == iapply.MODE_PR
    assert result.branch == iapply.INSTALL_BRANCH
    assert result.pr_url == "https://github.com/acme/repo/pull/1"
    assert result.pr_updated is False

    assert (tmp_path / ".agents" / "skills" / "to-spec" / "SKILL.md").is_file()
    claude_link = tmp_path / ".claude" / "skills"
    assert claude_link.is_symlink()
    assert (claude_link / "to-spec" / "SKILL.md").is_file()
    assert not (tmp_path / ".shipit-skills").exists()
    assert (tmp_path / "bin" / "shipit").is_file()
    agents = (tmp_path / "AGENTS.md").read_text()
    assert "Consumer text." in agents
    assert iunits.BLOCK_OPEN in agents

    cfg = config.load(tmp_path / ".shipit.toml")
    assert config.shipit_version(cfg) == "testhash"
    managed = config.load_managed(cfg)
    assert "bin/shipit" in managed and "AGENTS.md#shipit-block" in managed

    assert ("pr_create", True) in rec.calls
    assert "### Added" in rec.pr_body
    assert rec.names() == [
        "fetch",
        "switch",
        "reset",
        "read_tree",
        "add",
        "rm_cached",
        "commit",
        "push",
        "pr_create",
        "add",
        "switch_back",
    ]
    assert ("reset", "origin/main") in rec.calls
    assert ("switch_back", "main") in rec.calls


def test_pr_mode_bases_staging_branch_on_current_origin_default(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    result = _apply(tmp_path, iapply.MODE_PR)

    assert "fetch" in rec.names()
    assert rec.names().index("fetch") < rec.names().index("reset")
    assert rec.names().index("switch") < rec.names().index("reset")
    assert rec.names().index("reset") < rec.names().index("commit")
    assert ("reset", "origin/main") in rec.calls
    assert result.pr_url == "https://github.com/acme/repo/pull/1"


def test_pr_mode_fetches_before_resolving_the_default_branch(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    _apply(tmp_path, iapply.MODE_PR)

    assert rec.default_branch_after_fetch is True


def test_pr_mode_honors_a_non_main_default_branch(tmp_path, rec):
    rec._default_branch = "trunk"
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    _apply(tmp_path, iapply.MODE_PR)

    assert ("reset", "origin/trunk") in rec.calls
    assert rec.pr_base == "trunk"


def test_pr_mode_commits_the_full_managed_universe_including_noop_units(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    noop = next(u for u in iunits.load_units() if u.dest == iunits.MARKDOWNLINT_FILE)
    (tmp_path / noop.dest).write_bytes(noop.content)

    plan = _plan(tmp_path)
    assert noop.dest not in plan.changed_paths
    assert any(
        d.unit.dest == noop.dest and d.action == irec.NOOP for d in plan.decisions
    )

    _apply(tmp_path, iapply.MODE_PR)
    add_paths = next(paths for name, paths in rec.calls if name == "add")
    assert noop.dest in add_paths


def test_pr_mode_commits_the_whole_index_not_a_worktree_pathspec(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    victim = tmp_path / RETIRED_WORKFLOW_PATH
    victim.parent.mkdir(parents=True)
    victim.write_bytes(PRISTINE_WORKFLOW.read_bytes())

    _apply(tmp_path, iapply.MODE_PR)

    assert RETIRED_WORKFLOW_PATH in rec.rm_cached_paths
    assert rec.commit_all_message == iapply.COMMIT_MESSAGE
    assert rec.commit_paths == ()
    assert rec.commit_no_verify is True


def test_pr_mode_stages_and_commits_on_an_isolated_scratch_index(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    victim = tmp_path / RETIRED_WORKFLOW_PATH
    victim.parent.mkdir(parents=True)
    victim.write_bytes(PRISTINE_WORKFLOW.read_bytes())

    _apply(tmp_path, iapply.MODE_PR)

    assert ("read_tree", "origin/main") in rec.calls
    scratch = rec.pr_index_file
    assert scratch is not None
    assert rec.add_index_files[0] == scratch
    assert rec.rm_cached_index_file == scratch
    assert rec.staged_index_file == scratch
    assert rec.commit_all_index_file == scratch
    names = [c[0] for c in rec.calls]
    assert names.index("read_tree") < names.index("add")
    assert names.index("read_tree") < names.index("commit")


def test_pr_mode_commit_universe_carries_retired_deletes_noops_and_the_changelog(
    tmp_path, rec
):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    victim = tmp_path / RETIRED_WORKFLOW_PATH
    victim.parent.mkdir(parents=True)
    victim.write_bytes(PRISTINE_WORKFLOW.read_bytes())
    assert not (tmp_path / iapply.CHANGELOG_FILE).exists()

    plan = _plan(tmp_path)
    delete_paths = {d.retired.path for d in plan.retire_deletes}
    noop_paths = {d.retired.path for d in plan.retired if d.action == irec.NOOP}
    assert RETIRED_WORKFLOW_PATH in delete_paths
    assert noop_paths, "the manifest must declare more retired files to exercise NOOP"
    assert noop_paths.isdisjoint(plan.changed_paths)
    assert iapply.CHANGELOG_FILE not in plan.changed_paths

    _apply(tmp_path, iapply.MODE_PR)
    universe = set(rec.staged_query)
    assert RETIRED_WORKFLOW_PATH in universe
    assert noop_paths <= universe
    assert iapply.CHANGELOG_FILE in universe


def test_pr_mode_commit_universe_excludes_a_kept_retired_file(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    victim = tmp_path / RETIRED_WORKFLOW_PATH
    victim.parent.mkdir(parents=True)
    victim.write_text(PRISTINE_WORKFLOW.read_text() + "# local tweak\n")

    plan = _plan(tmp_path)
    assert [d.retired.path for d in plan.retire_keeps] == [RETIRED_WORKFLOW_PATH]
    assert not plan.retire_deletes

    _apply(tmp_path, iapply.MODE_PR)
    assert RETIRED_WORKFLOW_PATH not in set(rec.staged_query)
    assert victim.is_file()
    assert "# local tweak" in victim.read_text()


def test_pr_mode_preserves_a_consumer_file_reappearing_at_a_noop_retired_path(
    tmp_path, rec
):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")

    plan = _plan(tmp_path)
    noop_paths = [d.retired.path for d in plan.retired if d.action == irec.NOOP]
    assert noop_paths, "the manifest must declare a retired path that reconciles NOOP"
    victim_rel = noop_paths[0]
    assert victim_rel not in {d.retired.path for d in plan.retire_deletes}

    victim = tmp_path / victim_rel
    victim.parent.mkdir(parents=True, exist_ok=True)
    victim.write_text("# consumer's own file, never shipit's\n")

    _apply_plan(plan, tmp_path, iapply.MODE_PR)

    assert victim.is_file()
    assert "consumer's own file" in victim.read_text()
    add_payloads = [paths for name, paths in rec.calls if name == "add"]
    assert all(victim_rel not in p for p in add_payloads)
    assert victim_rel in rec.rm_cached_paths
    assert victim_rel in set(rec.staged_query)


def test_pr_mode_stages_retired_deletions_via_rm_cached_not_git_add(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    victim = tmp_path / RETIRED_WORKFLOW_PATH
    victim.parent.mkdir(parents=True)
    victim.write_bytes(PRISTINE_WORKFLOW.read_bytes())

    plan = _plan(tmp_path)
    assert [d.retired.path for d in plan.retire_deletes] == [RETIRED_WORKFLOW_PATH]

    _apply(tmp_path, iapply.MODE_PR)
    assert RETIRED_WORKFLOW_PATH in rec.rm_cached_paths
    add_payloads = [paths for name, paths in rec.calls if name == "add"]
    assert all(RETIRED_WORKFLOW_PATH not in p for p in add_payloads)
    assert RETIRED_WORKFLOW_PATH in set(rec.staged_query)
    assert not victim.exists()


def test_pr_mode_leaves_a_retired_path_that_became_a_directory(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    victim = tmp_path / RETIRED_WORKFLOW_PATH
    victim.parent.mkdir(parents=True)
    victim.write_bytes(PRISTINE_WORKFLOW.read_bytes())

    plan = _plan(tmp_path)
    assert [d.retired.path for d in plan.retire_deletes] == [RETIRED_WORKFLOW_PATH]

    victim.unlink()
    victim.mkdir()
    (victim / "consumer.txt").write_text("consumer content\n")

    _apply_plan(plan, tmp_path, iapply.MODE_PR)

    assert victim.is_dir()
    assert (victim / "consumer.txt").read_text() == "consumer content\n"
    assert RETIRED_WORKFLOW_PATH in rec.rm_cached_paths


def test_pr_mode_survives_a_retired_delete_vanishing_between_is_file_and_unlink(
    tmp_path, rec, monkeypatch
):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    victim = tmp_path / RETIRED_WORKFLOW_PATH
    victim.parent.mkdir(parents=True)
    victim.write_bytes(PRISTINE_WORKFLOW.read_bytes())

    plan = _plan(tmp_path)
    assert [d.retired.path for d in plan.retire_deletes] == [RETIRED_WORKFLOW_PATH]

    real_unlink = Path.unlink

    def _racy_unlink(self, missing_ok=False):
        if self == victim and victim.exists():
            real_unlink(victim)
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", _racy_unlink)

    _apply_plan(plan, tmp_path, iapply.MODE_PR)

    assert RETIRED_WORKFLOW_PATH in rec.rm_cached_paths


def test_pr_mode_universe_excludes_a_noop_retired_hook_file(tmp_path, rec, monkeypatch):
    hook = irec.RetiredHook(
        file=".shipit-legacy-hooks.json", event="pre-commit", marker="legacy-cmd"
    )
    monkeypatch.setattr(irec, "load_retired_hooks", lambda: [hook])
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    (tmp_path / ".shipit-legacy-hooks.json").write_text(
        '{"hooks": {"pre-commit": [{"command": "my-own-tool"}]}}\n'
    )

    plan = _plan(tmp_path)
    assert [d.action for d in plan.retired_hooks] == [irec.NOOP]
    assert not plan.retire_hook_deletes

    _apply(tmp_path, iapply.MODE_PR)
    assert ".shipit-legacy-hooks.json" not in set(rec.staged_query)
    assert "my-own-tool" in (tmp_path / ".shipit-legacy-hooks.json").read_text()


def test_pr_mode_reports_no_changes_when_the_managed_set_already_matches_base(
    tmp_path, rec
):
    rec._no_staged = True
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    result = _apply(tmp_path, iapply.MODE_PR)

    assert "commit" not in rec.names()
    assert "push" not in rec.names()
    assert "pr_create" not in rec.names()
    assert result.pr_url is None
    assert result.branch is None
    assert ("switch_back", "main") in rec.calls


def test_fresh_install_provisions_agent_defs_and_settings_hook(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    _apply(tmp_path)

    for role in ("implementer", "shepherd", "explorer"):
        dest = tmp_path / ".claude" / "agents" / f"{role}.md"
        assert dest.is_file()
        assert f"name: {role}" in dest.read_text()

    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    pre = settings["hooks"]["PreToolUse"]
    assert any(splice.is_shipit_hook(e) for e in pre)

    managed = config.load_managed(config.load(tmp_path / ".shipit.toml"))
    assert ".claude/agents/implementer.md" in managed
    assert iunits.SETTINGS_KEY in managed


def test_install_merges_settings_hook_without_clobbering_consumer_settings(
    tmp_path, rec
):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        json.dumps(
            {
                "permissions": {"allow": ["Bash(ls:*)"]},
                "hooks": {
                    "SessionStart": [
                        {"hooks": [{"type": "command", "command": "echo hi"}]}
                    ]
                },
            },
            indent=2,
        )
    )
    _apply(tmp_path)

    merged = json.loads(settings_path.read_text())
    assert merged["permissions"] == {"allow": ["Bash(ls:*)"]}
    assert merged["hooks"]["SessionStart"][0]["hooks"][0]["command"] == "echo hi"
    assert any(splice.is_shipit_hook(e) for e in merged["hooks"]["PreToolUse"])


def test_consumer_edit_to_settings_hook_surfaces_as_override(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    _apply(tmp_path)
    rec.calls.clear()

    settings_path = tmp_path / ".claude" / "settings.json"
    data = json.loads(settings_path.read_text())
    for entry in data["hooks"]["PreToolUse"]:
        if splice.is_shipit_hook(entry):
            entry["matcher"] = "Edit"
    settings_path.write_text(json.dumps(data, indent=2))

    plan = _plan(tmp_path)
    assert [d.unit.key for d in plan.overrides] == [iunits.SETTINGS_KEY]
    result = _apply(tmp_path, iapply.MODE_PR)
    assert result.pr_url is not None
    assert ("pr_create", True) in rec.calls
    assert "### Overrides" in rec.pr_body
    assert iunits.SETTINGS_FILE in rec.pr_body


def test_consumer_edit_to_agent_def_surfaces_as_override(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    _apply(tmp_path)
    rec.calls.clear()

    (tmp_path / ".claude" / "agents" / "implementer.md").write_text("HAND EDIT\n")
    _apply(tmp_path, iapply.MODE_PR)
    assert ("pr_create", True) in rec.calls
    assert "### Overrides" in rec.pr_body
    assert ".claude/agents/implementer.md" in rec.pr_body
    assert "HAND EDIT" in rec.pr_body


def test_install_against_malformed_settings_json_does_not_crash(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    garbage = '{ "permissions": [ this is not valid json,,, ]\n'
    settings_path.write_text(garbage)

    result = _apply(tmp_path, iapply.MODE_PR)

    assert result.pr_url is not None
    assert settings_path.read_text() == garbage
    assert ("pr_create", True) in rec.calls
    assert "### Overrides" in rec.pr_body
    assert iunits.SETTINGS_FILE in rec.pr_body


def test_reinstall_with_no_changes_is_a_clean_noop(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    _apply(tmp_path)
    rec.calls.clear()
    assert _plan(tmp_path).nothing_to_do
    rc = verb.run(str(tmp_path))
    assert rc == 0
    assert rec.calls == []


def test_consumer_edit_surfaces_as_override(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    _apply(tmp_path)
    rec.calls.clear()

    skill = tmp_path / ".agents" / "skills" / "to-spec" / "SKILL.md"
    skill.write_text("CONSUMER EDIT\n")

    _apply(tmp_path, iapply.MODE_PR)
    assert ("pr_create", True) in rec.calls
    assert "### Overrides" in rec.pr_body
    assert ".agents/skills/to-spec/SKILL.md" in rec.pr_body
    assert "CONSUMER EDIT" in rec.pr_body
    assert "```diff" in rec.pr_body


def _decline(root, *keys):
    cfg = root / config.CONFIG_NAME
    existing = cfg.read_text() if cfg.is_file() else ""
    keep = ", ".join(f'"{k}"' for k in keys)
    cfg.write_text(f"{existing}\n[managed.decline]\nkeep = [{keep}]\n")


def test_declined_unit_makes_a_would_be_override_a_clean_noop(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    _apply(tmp_path)
    rec.calls.clear()
    (tmp_path / "bin" / "shipit").write_text("#!/bin/sh\n# MY OWN LAUNCHER\n")
    _decline(tmp_path, "bin/shipit")

    plan = _plan(tmp_path)
    assert plan.declined == (iunits.SHIPIT_LAUNCHER_FILE,)
    assert plan.overrides == ()
    assert all(d.unit.key != iunits.SHIPIT_LAUNCHER_FILE for d in plan.decisions)
    assert plan.nothing_to_do
    rc = verb.run(str(tmp_path))
    assert rc == 0
    assert rec.calls == []
    assert "MY OWN LAUNCHER" in (tmp_path / "bin" / "shipit").read_text()


def test_declined_unit_is_never_written_and_drops_from_the_manifest(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    _apply(tmp_path)
    rec.calls.clear()
    (tmp_path / "bin" / "shipit").write_text("#!/bin/sh\n# MY OWN LAUNCHER\n")
    _decline(tmp_path, "bin/shipit")
    (tmp_path / ".agents" / "skills" / "to-spec" / "SKILL.md").unlink()

    result = _apply(tmp_path, iapply.MODE_PR)
    assert result.pr_url is not None
    assert "MY OWN LAUNCHER" in (tmp_path / "bin" / "shipit").read_text()
    cfg_path = tmp_path / config.CONFIG_NAME
    cfg = config.load(cfg_path)
    managed = config.load_managed(cfg)
    assert iunits.SHIPIT_LAUNCHER_FILE not in managed
    assert ".agents/skills/to-spec/SKILL.md" in managed
    assert config.load_declines(cfg, cfg_path.read_text()) == (
        iunits.SHIPIT_LAUNCHER_FILE,
    )
    assert "### Declined units" in rec.pr_body
    assert "`bin/shipit`" in rec.pr_body


def test_fresh_install_skips_a_pre_declined_unit(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    _decline(tmp_path, "bin/shipit")
    _apply(tmp_path, iapply.MODE_PR)
    assert not (tmp_path / "bin" / "shipit").exists()
    assert "### Declined units" in rec.pr_body


def test_unmatched_decline_key_warns_never_silently_ignores(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    _apply(tmp_path)
    _decline(tmp_path, "no/such-unit")
    plan = _plan(tmp_path)
    assert plan.decline_unmatched == ("no/such-unit",)
    assert plan.declined == ()
    warnings = verb.format_plan_warnings(plan)
    assert "no/such-unit" in warnings
    assert "names no managed unit" in warnings


def test_duplicate_decline_key_is_de_duped_on_both_surfaces(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    _apply(tmp_path)
    _decline(tmp_path, "bin/shipit", "bin/shipit", "no/such-unit", "no/such-unit")
    plan = _plan(tmp_path)
    assert plan.declined == ("bin/shipit",)
    assert plan.decline_unmatched == ("no/such-unit",)


def test_format_plan_renders_the_standing_decline_line():
    plan = irec.Plan(
        root="/consumer",
        decisions=(),
        retired=(),
        seeds=(),
        declined=(iunits.SHIPIT_LAUNCHER_FILE,),
    )
    text = verb.format_plan(plan)
    assert "decline" in text
    assert iunits.SHIPIT_LAUNCHER_FILE in text
    assert "nothing to do — no automated changes to apply." in text


def test_shipits_own_manifest_declines_the_launcher():
    cfg_path = REPO_ROOT / config.CONFIG_NAME
    cfg = config.load(cfg_path)
    assert iunits.SHIPIT_LAUNCHER_FILE in config.load_declines(
        cfg, cfg_path.read_text()
    )
    packaged = iunits.data_bytes("bootstrap", "shipit")
    committed = (REPO_ROOT / "bin" / "shipit").read_bytes()
    assert committed != packaged


def test_fresh_install_delivers_the_lint_environment(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    (tmp_path / "pixi.toml").write_text(
        '[workspace]\nname = "acme"\nchannels = ["conda-forge"]\n'
        'platforms = ["osx-arm64"]\n\n[tasks]\ncheck = "pytest"\n'
    )
    _apply(tmp_path)

    manifest = tomllib.loads((tmp_path / "pixi.toml").read_text())
    assert manifest["workspace"]["name"] == "acme"
    assert manifest["tasks"]["check"] == "pytest"
    assert manifest["feature"]["lint"]["tasks"]["lint"] == "./bin/shipit lint"
    assert "lint" not in manifest["tasks"]
    deps = manifest["feature"]["lint"]["dependencies"]
    assert set(deps) == set(LINT_TOOLS)
    assert manifest["environments"]["lint"] == ["lint", "shipit-lexd"]
    assert "dependencies" not in manifest["feature"]["shipit-lexd"]
    assert manifest["feature"]["shipit-lexd"]["target"] == _lexd_targets({"osx-arm64"})

    managed = config.load_managed(config.load(tmp_path / ".shipit.toml"))
    assert iunits.PIXI_LINT_DEPS_KEY in managed
    assert iunits.PIXI_ENVS_KEY in managed
    assert iunits.PIXI_LEXD_KEY in managed
    assert _plan(tmp_path).nothing_to_do


def test_install_on_a_consumer_declaring_an_unserved_platform_delivers_scoped_lexd(
    tmp_path, rec
):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    (tmp_path / "pixi.toml").write_text(
        '[workspace]\nname = "acme"\nchannels = ["conda-forge"]\n'
        'platforms = ["linux-64", "linux-aarch64", "osx-64", "osx-arm64"]\n\n'
        '[tasks]\ncheck = "pytest"\n'
    )
    _apply(tmp_path)

    manifest = tomllib.loads((tmp_path / "pixi.toml").read_text())
    feature = manifest["feature"]["shipit-lexd"]
    assert "dependencies" not in feature
    assert feature["target"] == _lexd_targets(
        {"linux-64", "linux-aarch64", "osx-64", "osx-arm64"}
    )
    assert "osx-64" not in feature["target"]
    assert "win-64" not in feature["target"]
    assert _plan(tmp_path).nothing_to_do


_LEGACY_BLANKET_LEXD_BLOCK = (
    f"{iunits.PIXI_LEXD_OPEN}\n"
    "[feature.shipit-lexd]\n"
    'channels = ["https://storage.googleapis.com/shipit-artifacts-public/lex-fmt/lex"]\n'
    "[feature.shipit-lexd.dependencies]\n"
    'lexd = "==0.19.10"\n'
    f"{iunits.PIXI_LEXD_CLOSE}\n"
)


def test_upgrade_replaces_blanket_lexd_block_with_scoped_targets(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    (tmp_path / "pixi.toml").write_text(
        '[workspace]\nname = "acme"\nchannels = ["conda-forge"]\n'
        'platforms = ["linux-64", "linux-aarch64", "osx-64", "osx-arm64"]\n\n'
        '[tasks]\ncheck = "pytest"\n\n'
        f"{_LEGACY_BLANKET_LEXD_BLOCK}"
    )
    seeded = tomllib.loads((tmp_path / "pixi.toml").read_text())
    assert seeded["feature"]["shipit-lexd"]["dependencies"] == {"lexd": "==0.19.10"}
    assert "target" not in seeded["feature"]["shipit-lexd"]

    _apply(tmp_path)

    feature = tomllib.loads((tmp_path / "pixi.toml").read_text())["feature"][
        "shipit-lexd"
    ]
    assert "dependencies" not in feature
    assert feature["target"] == _lexd_targets(
        {"linux-64", "linux-aarch64", "osx-64", "osx-arm64"}
    )
    assert "osx-64" not in feature["target"]
    assert "win-64" not in feature["target"]
    assert (tmp_path / "pixi.toml").read_text().count(iunits.PIXI_LEXD_OPEN) == 1
    assert _plan(tmp_path).nothing_to_do


@pytest.mark.skipif(shutil.which("pixi") is None, reason="pixi not on PATH")
def test_scoped_lexd_manifest_is_accepted_by_real_pixi(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    (tmp_path / "pixi.toml").write_text(
        '[workspace]\nname = "acme"\nchannels = ["conda-forge"]\n'
        'platforms = ["linux-64", "linux-aarch64", "osx-64", "osx-arm64"]\n\n'
        '[tasks]\ncheck = "pytest"\n'
    )
    _apply(tmp_path)

    proc = subprocess.run(
        ["pixi", "info", "--manifest-path", str(tmp_path / "pixi.toml")],
        capture_output=True,
        text=True,
        timeout=60,
    )
    combined = f"{proc.stdout}\n{proc.stderr}"
    assert proc.returncode == 0, combined
    assert "Cannot solve" not in combined
    assert "No candidates" not in combined
    assert "does not match any of the platforms" not in combined
    assert "target selector" not in combined


def test_install_on_a_win64_declaring_consumer_keeps_the_win64_target(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    (tmp_path / "pixi.toml").write_text(
        '[workspace]\nname = "acme"\nchannels = ["conda-forge"]\n'
        'platforms = ["linux-64", "osx-arm64", "linux-aarch64", "win-64"]\n\n'
        '[tasks]\ncheck = "pytest"\n'
    )
    _apply(tmp_path)

    feature = tomllib.loads((tmp_path / "pixi.toml").read_text())["feature"][
        "shipit-lexd"
    ]
    assert feature["target"] == LEXD_SCOPED_TARGET
    assert "win-64" in feature["target"]
    assert feature["target"]["win-64"] == {"dependencies": {"lexd": iunits.LEXD_PIN}}
    assert _plan(tmp_path).nothing_to_do


def test_lint_env_block_merges_into_an_existing_environments_table(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    (tmp_path / "pixi.toml").write_text('[environments]\ndev = ["dev"]\n')
    _apply(tmp_path)

    manifest = tomllib.loads((tmp_path / "pixi.toml").read_text())
    assert manifest["environments"] == {
        "dev": ["dev"],
        "lint": ["lint", "shipit-lexd"],
    }


def test_lint_env_merges_lexd_into_a_consumer_owned_lint_env(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    (tmp_path / "pixi.toml").write_text(
        '[workspace]\nname = "acme"\nchannels = ["conda-forge"]\n'
        'platforms = ["osx-arm64"]\n\n[environments]\nlint = ["lint"]\n'
    )
    _apply(tmp_path)

    manifest = tomllib.loads((tmp_path / "pixi.toml").read_text())
    assert manifest["environments"]["lint"] == ["lint", "shipit-lexd"]
    assert manifest["feature"]["shipit-lexd"]["target"] == _lexd_targets({"osx-arm64"})
    assert manifest["feature"]["shipit-lexd"]["channels"] == [
        "https://storage.googleapis.com/shipit-artifacts-public/lex-fmt/lex"
    ]
    assert _plan(tmp_path).nothing_to_do


def test_lint_env_table_form_install_preserves_solve_group(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    (tmp_path / "pixi.toml").write_text(
        '[workspace]\nname = "acme"\nchannels = ["conda-forge"]\n'
        'platforms = ["osx-arm64"]\n\n[environments]\n'
        'lint = { features = ["lint"], solve-group = "shared" }\n'
    )
    _apply(tmp_path)

    lint_env = tomllib.loads((tmp_path / "pixi.toml").read_text())["environments"][
        "lint"
    ]
    assert lint_env == {"features": ["lint", "shipit-lexd"], "solve-group": "shared"}
    assert _plan(tmp_path).nothing_to_do


def test_lint_env_unsupported_form_is_surfaced_not_looped(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    (tmp_path / "pixi.toml").write_text(
        '[workspace]\nname = "acme"\nchannels = ["conda-forge"]\n'
        'platforms = ["osx-arm64"]\n\n[environments]\nlint = { features = "lint" }\n'
    )
    plan = _plan(tmp_path)
    env_decisions = [d for d in plan.decisions if d.unit.key == iunits.PIXI_ENVS_KEY]
    assert [d.action for d in env_decisions] == [irec.OVERRIDE]

    _apply(tmp_path)
    assert '{ features = "lint" }' in (tmp_path / "pixi.toml").read_text()


def test_lint_env_membership_preserves_a_consumer_extra_feature(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    (tmp_path / "pixi.toml").write_text(
        '[workspace]\nname = "acme"\nchannels = ["conda-forge"]\n'
        'platforms = ["osx-arm64"]\n\n[feature.house.dependencies]\nblack = "*"\n\n'
        '[environments]\nlint = ["lint", "house"]\n'
    )
    _apply(tmp_path)

    manifest = tomllib.loads((tmp_path / "pixi.toml").read_text())
    assert manifest["environments"]["lint"] == ["lint", "house", "shipit-lexd"]
    assert _plan(tmp_path).nothing_to_do


def test_consumer_edit_to_lint_deps_block_surfaces_as_override(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    _apply(tmp_path)
    rec.calls.clear()

    pixi_path = tmp_path / "pixi.toml"
    pixi_path.write_text(
        pixi_path.read_text().replace('ruff = "0.15.*"', 'ruff = "0.99.*"')
    )

    plan = _plan(tmp_path)
    assert [d.unit.key for d in plan.overrides] == [iunits.PIXI_LINT_DEPS_KEY]
    _apply(tmp_path, iapply.MODE_PR)
    assert ("pr_create", True) in rec.calls
    assert "### Overrides" in rec.pr_body
    assert 'ruff = "0.99.*"' in rec.pr_body


def test_pixi_manifest_seed_is_valid_toml_with_a_sanitized_name():
    seed = tomllib.loads(iunits.pixi_manifest_seed("shipit-canary"))
    assert seed["workspace"]["name"] == "shipit-canary"
    assert seed["workspace"]["channels"] == list(iunits.PIXI_SEED_CHANNELS)
    assert seed["workspace"]["platforms"] == list(iunits.PIXI_SEED_PLATFORMS)

    weird = tomllib.loads(iunits.pixi_manifest_seed('my repo "v2"!'))
    assert weird["workspace"]["name"] == "my-repo-v2"
    assert tomllib.loads(iunits.pixi_manifest_seed("«»"))["workspace"]["name"]


def test_fresh_consumer_without_pixi_manifest_gets_a_valid_seed(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")

    plan = _plan(tmp_path)
    assert plan.seed_pixi_manifest is True
    assert "pixi.toml ([workspace] table" in verb.format_plan(plan, dry_run=True)

    _apply(tmp_path, iapply.MODE_PR)

    manifest = tomllib.loads((tmp_path / "pixi.toml").read_text())
    assert manifest["workspace"]["name"] == iunits.workspace_name(tmp_path.name)
    assert manifest["workspace"]["channels"] == list(iunits.PIXI_SEED_CHANNELS)
    assert manifest["feature"]["lint"]["tasks"]["lint"] == "./bin/shipit lint"
    assert "lint" not in manifest["tasks"]
    assert manifest["tasks"]["test"] == "./bin/shipit test"
    assert set(manifest["feature"]["lint"]["dependencies"]) == set(LINT_TOOLS)
    assert manifest["environments"]["lint"] == ["lint", "shipit-lexd"]
    assert "dependencies" not in manifest["feature"]["shipit-lexd"]
    assert manifest["feature"]["shipit-lexd"]["target"] == _lexd_targets(
        iunits.PIXI_SEED_PLATFORMS
    )
    assert "win-64" not in manifest["feature"]["shipit-lexd"]["target"]
    assert "uv" in manifest["dependencies"]

    managed = config.load_managed(config.load(tmp_path / ".shipit.toml"))
    pixi_keys = {k for k in managed if k.startswith("pixi.toml")}
    assert pixi_keys == {
        iunits.PIXI_KEY,
        iunits.PIXI_TEST_TASK_KEY,
        iunits.PIXI_LINT_TASK_KEY,
        iunits.PIXI_LINT_DEPS_KEY,
        iunits.PIXI_ENVS_KEY,
        iunits.PIXI_LAUNCHER_DEPS_KEY,
        iunits.PIXI_LEXD_KEY,
    }

    assert "### Pixi manifest seeded" in rec.pr_body

    replan = _plan(tmp_path)
    assert replan.nothing_to_do and replan.seed_pixi_manifest is False


def test_seeded_workspace_table_is_consumer_owned(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    _apply(tmp_path)

    pixi_path = tmp_path / "pixi.toml"
    pixi_path.write_text(
        pixi_path.read_text().replace("platforms = [", 'license = "MIT"\nplatforms = [')
    )
    assert _plan(tmp_path).nothing_to_do


def test_existing_pixi_manifest_is_never_seeded(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    (tmp_path / "pixi.toml").write_text('[workspace]\nname = "acme"\n')

    plan = _plan(tmp_path)
    assert plan.seed_pixi_manifest is False

    _apply(tmp_path)
    manifest = tomllib.loads((tmp_path / "pixi.toml").read_text())
    assert manifest["workspace"] == {"name": "acme"}
    assert manifest["feature"]["lint"]["tasks"]["lint"] == "./bin/shipit lint"


def test_seed_never_clobbers_a_manifest_created_after_gather(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    plan = _plan(tmp_path)
    assert plan.seed_pixi_manifest is True

    (tmp_path / "pixi.toml").write_text('[workspace]\nname = "late"\n')
    iapply.apply(plan)

    manifest = tomllib.loads((tmp_path / "pixi.toml").read_text())
    assert manifest["workspace"] == {"name": "late"}
    assert manifest["feature"]["lint"]["tasks"]["lint"] == "./bin/shipit lint"


def test_open_install_pr_is_updated_not_recreated(tmp_path, rec, monkeypatch):
    monkeypatch.setattr(
        gh, "pr_url_for_head", lambda branch, cwd=None: "https://x/pull/7"
    )
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    result = _apply(tmp_path, iapply.MODE_PR)
    assert result.pr_updated is True
    assert result.pr_url == "https://x/pull/7"
    assert "push" in rec.names()
    assert "pr_create" not in rec.names()


def test_default_install_refreshes_working_tree_without_git_or_pr(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n\nConsumer text.\n")
    result = _apply(tmp_path)
    assert result.mode == iapply.MODE_TREE
    assert result.branch is None and result.pr_url is None

    assert (tmp_path / "bin" / "shipit").is_file()
    agents = (tmp_path / "AGENTS.md").read_text()
    assert "Consumer text." in agents
    assert iunits.BLOCK_OPEN in agents
    managed = config.load_managed(config.load(tmp_path / ".shipit.toml"))
    assert "bin/shipit" in managed
    assert rec.calls == []


def test_default_install_mid_drift_never_branches_or_opens_pr(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    _apply(tmp_path)
    rec.calls.clear()

    skill = tmp_path / ".agents" / "skills" / "to-spec" / "SKILL.md"
    skill.write_text("CONSUMER EDIT\n")
    result = _apply(tmp_path)
    assert "CONSUMER EDIT" not in skill.read_text()
    assert rec.calls == []
    warning = verb.format_result_warnings(result)
    assert "consumer-edited" in warning
    assert ".agents/skills/to-spec/SKILL.md" in warning


def test_push_flag_pushes_to_branch_without_pr(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    result = _apply(tmp_path, iapply.MODE_PUSH)
    assert result.branch == "main"
    assert ("push", "main") in rec.calls
    assert "pr_create" not in rec.names()


def test_pr_mode_on_virgin_repo_with_lint_debt_reaches_the_pr_leg(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    (tmp_path / "DEBT.md").write_text("#bad-heading\nline with trailing spaces  \n")
    debt_calls = []

    def debt_reader(root):
        debt_calls.append(root)
        return 5

    result = _apply(tmp_path, iapply.MODE_PR, debt=debt_reader)

    assert rec.hook_activations == [tmp_path]
    assert rec.names() == [
        "fetch",
        "switch",
        "reset",
        "read_tree",
        "add",
        "rm_cached",
        "commit",
        "push",
        "pr_create",
        "add",
        "switch_back",
    ]
    assert rec.commit_no_verify is True
    assert rec.push_no_verify is True
    assert result.pr_url == "https://github.com/acme/repo/pull/1"
    assert debt_calls == [tmp_path]
    assert result.lint_debt == 5
    assert "5 failing check(s)" in rec.pr_body


def test_break_glass_push_bypasses_the_repo_hooks(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    _apply(tmp_path, iapply.MODE_PUSH)
    assert rec.commit_no_verify is True
    assert rec.push_no_verify is True


def test_local_flag_commits_on_current_branch_without_push_or_pr(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    result = _apply(tmp_path, iapply.MODE_LOCAL)
    assert result.branch == "main"
    assert (tmp_path / "bin" / "shipit").is_file()
    assert rec.names() == ["add", "commit"]
    assert "switch" not in rec.names()
    assert "push" not in rec.names()
    assert "pr_create" not in rec.names()


def test_local_mode_fails_in_detached_head(tmp_path, monkeypatch, rec):
    monkeypatch.setattr(git, "current_branch", lambda *, cwd: None)
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    with pytest.raises(InstallError, match="--local needs a checked-out branch"):
        _apply(tmp_path, iapply.MODE_LOCAL)
    assert "commit" not in rec.names()


def test_local_flag_detached_head_is_a_clean_exit_through_the_shell(
    tmp_path, monkeypatch, rec, capsys
):
    monkeypatch.setattr(git, "current_branch", lambda *, cwd: None)
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    rc = verb.run(str(tmp_path), local=True)
    assert rc == 1
    assert "commit" not in rec.names()
    err = capsys.readouterr().err
    assert err.startswith("error: ") or "error: " in err
    assert "--local needs a checked-out branch" in err


def test_stale_manifest_keys_are_dropped(tmp_path, rec):
    config.write_manifest(
        tmp_path / ".shipit.toml",
        version="old",
        managed={"skills/retired/SKILL.md": "sha256:dead", "bin/shipit": "sha256:old"},
    )
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    _apply(tmp_path)
    managed = config.load_managed(config.load(tmp_path / ".shipit.toml"))
    assert "skills/retired/SKILL.md" not in managed
    assert set(managed) == {u.key for u in iunits.load_units()}


def test_gh_failure_is_a_clean_nonzero_exit(tmp_path, monkeypatch, rec, capsys):
    def boom(*a, **k):
        raise ExecError(["gh"], rc=1, stderr="no remote configured")

    monkeypatch.setattr(git, "switch_create", boom)
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    rc = verb.run(str(tmp_path), pr=True)
    assert rc == 1
    assert "error: " in capsys.readouterr().err


def test_gather_refuses_a_non_directory_target(tmp_path):
    with pytest.raises(InstallError, match="is not a directory"):
        irec.gather(tmp_path / "nope", iunits.load_units(), irec.load_retired())


def test_unreadable_manifest_degrades_to_empty_pristine(tmp_path, rec):
    (tmp_path / ".shipit.toml").write_text("not [ valid toml")
    plan = _plan(tmp_path)
    assert plan.manifest_error is not None
    assert "manifest" in verb.format_plan_warnings(plan)
    assert plan.writes


def _secrets_by_name(root):
    cfg = config.load(root / ".shipit.toml")
    return {s.name: s for s in config.load_secrets(cfg)}


def test_fresh_install_seeds_app_secret_mappings(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    plan = _plan(tmp_path)
    assert "[secrets].CODEX_REVIEW_APP_PRIVATE_KEY" in plan.seeds
    _apply(tmp_path, iapply.MODE_PR)

    secrets = _secrets_by_name(tmp_path)
    for name in (
        "CODEX_REVIEW_APP_PRIVATE_KEY",
        "CODEX_REVIEW_APP_ID",
        "AGY_REVIEW_APP_PRIVATE_KEY",
        "AGY_REVIEW_APP_ID",
    ):
        assert name in secrets
        assert secrets[name].kind == "doppler"
        assert secrets[name].key == name
    assert "### Policy seeded" in rec.pr_body
    assert "[secrets].CODEX_REVIEW_APP_PRIVATE_KEY" in rec.pr_body


def test_fresh_install_seeds_required_reviewer_set(tmp_path, rec):
    from shipit.prstate import reviewers_config as rcfg

    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    _apply(tmp_path)

    assert rcfg.load_roster(str(tmp_path)).required_names == ("copilot",)


def test_install_preserves_existing_secrets_and_reviewers(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    (tmp_path / ".shipit.toml").write_text(
        "[secrets]\n"
        'MY_TOKEN = { env = "MY_TOKEN" }\n'
        'CODEX_REVIEW_APP_ID = { doppler = "CUSTOM_KEY" }\n'
        "\n[reviewers]\n"
        "copilot = { rerun = true }\n"
    )
    _apply(tmp_path)

    secrets = _secrets_by_name(tmp_path)
    assert secrets["MY_TOKEN"].kind == "env"
    assert secrets["CODEX_REVIEW_APP_ID"].key == "CUSTOM_KEY"
    assert "CODEX_REVIEW_APP_PRIVATE_KEY" in secrets
    assert "AGY_REVIEW_APP_PRIVATE_KEY" in secrets
    assert "AGY_REVIEW_APP_ID" in secrets
    cfg = config.load(tmp_path / ".shipit.toml")
    assert cfg["reviewers"] == {"copilot": {"rerun": True}}


def test_reinstall_does_not_reseed_policy(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    _apply(tmp_path)
    before = (tmp_path / ".shipit.toml").read_text()

    rec.calls.clear()
    plan = _plan(tmp_path)
    assert plan.seeds == ()
    assert plan.nothing_to_do
    assert rec.calls == []
    assert (tmp_path / ".shipit.toml").read_text() == before


def test_install_reseeds_policy_when_missing_even_if_managed_current(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    _apply(tmp_path)
    cfg_path = tmp_path / ".shipit.toml"
    managed = config.load_managed(config.load(cfg_path))
    cfg_path.write_text(config.dump_manifest("testhash", managed))

    rec.calls.clear()
    plan = _plan(tmp_path)
    assert not plan.writes and plan.seeds
    assert not plan.nothing_to_do
    result = _apply(tmp_path, iapply.MODE_PR)
    assert ("pr_create", True) in rec.calls
    assert "### Policy seeded" in rec.pr_body
    assert result.hooks_activated is None
    assert "### Checks activated locally" not in rec.pr_body
    secrets = _secrets_by_name(tmp_path)
    assert "CODEX_REVIEW_APP_PRIVATE_KEY" in secrets
    assert "reviewers" in config.load(cfg_path)


def test_dry_run_does_not_seed_policy(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    rc = verb.run(str(tmp_path), dry_run=True)
    assert rc == 0
    assert not (tmp_path / ".shipit.toml").exists()


def test_activates_hooks_is_true_iff_lefthook_is_managed():
    units = iunits.load_units()
    decisions = irec.plan(units, {}, {})
    assert irec.activates_hooks(decisions) is True

    others = [d for d in decisions if d.unit.key != iunits.LEFTHOOK_FILE]
    assert irec.activates_hooks(others) is False


def test_fresh_install_activates_the_check_hooks(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    result = _apply(tmp_path, iapply.MODE_PR)
    assert result.hooks_activated is True
    assert len(rec.hook_activations) == 1
    assert rec.hook_activations[0] == tmp_path.resolve()
    assert "### Checks activated" in rec.pr_body
    assert "lefthook install" in rec.pr_body
    assert "run `./bin/shipit install` on your own checkout" in rec.pr_body


def test_break_glass_push_also_activates_hooks(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    result = _apply(tmp_path, iapply.MODE_PUSH)
    assert result.hooks_activated is True
    assert len(rec.hook_activations) == 1


def test_reinstall_with_writes_reactivates_idempotently(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    _apply(tmp_path)
    assert len(rec.hook_activations) == 1
    (tmp_path / "lefthook.yml").write_text("CONSUMER EDIT\n")
    rec.calls.clear()
    _apply(tmp_path)
    assert len(rec.hook_activations) == 2


def test_install_degrades_but_succeeds_when_activation_fails(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    result = _apply(
        tmp_path,
        iapply.MODE_PR,
        activate_hooks=lambda root: _exec_result(1, stderr="lefthook: broken config"),
    )
    assert ("pr_create", True) in rec.calls
    assert result.hooks_activated is False
    assert "lefthook: broken config" in result.hooks_detail
    assert "could not activate git hooks" in verb.format_result_warnings(result)
    assert "### Checks activated locally" not in rec.pr_body
    assert "local activation skipped" in rec.pr_body
    assert "lefthook install" in rec.pr_body
    assert "run `./bin/shipit install`" in rec.pr_body


def test_install_degrades_but_succeeds_when_lefthook_missing(tmp_path, rec):
    def boom(root):
        raise execrun.ExecError(
            ["lefthook", "install"], rc=None, cause=execrun.CAUSE_MISSING_BINARY
        )

    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    result = _apply(tmp_path, iapply.MODE_PR, activate_hooks=boom)
    assert ("pr_create", True) in rec.calls
    assert result.hooks_activated is False
    assert "### Checks activated locally" not in rec.pr_body
    assert "local activation skipped" in rec.pr_body
    warning = verb.format_result_warnings(result)
    assert "could not activate git hooks" in warning
    assert "pixi not found on PATH" in warning
    assert "`./bin/shipit install` to activate the checks" in warning
    assert "lefthook install" not in warning


def test_install_activation_timeout_does_not_claim_missing_binary(tmp_path, rec):
    def boom(root):
        raise execrun.ExecError(
            ["lefthook", "install"], rc=None, cause=execrun.CAUSE_TIMEOUT
        )

    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    result = _apply(tmp_path, iapply.MODE_PR, activate_hooks=boom)
    assert ("pr_create", True) in rec.calls
    warning = verb.format_result_warnings(result)
    assert "could not activate git hooks" in warning
    assert "not found on PATH" not in warning
    assert "activation could not run" in warning
    assert "`./bin/shipit install` to activate the checks" in warning


def test_activate_hooks_boundary_runs_lefthook_through_consumer_lint_env(
    tmp_path, monkeypatch
):
    captured = {}

    def fake_run(argv, *, cwd=None, check=True, **kw):
        captured["argv"] = argv
        captured["cwd"] = cwd
        captured["check"] = check
        captured["timeout"] = kw.get("timeout")
        return execrun.ExecResult(
            argv=tuple(argv),
            rc=0,
            stdout="sync hooks: ✔️ pre-commit, ✔️ pre-push\n",
            stderr="",
            duration_ms=1,
        )

    monkeypatch.setattr(iapply.execrun, "run", fake_run)
    result = iapply._activate_hooks(tmp_path)
    assert result.ok
    assert captured["argv"] == pixienv.run_argv(
        ["lefthook", "install"], tmp_path, environment=iunits.LINT_ENV
    )
    assert captured["cwd"] == str(tmp_path)
    assert captured["check"] is False
    assert captured["timeout"] == pixienv.INSTALL_TIMEOUT
    assert "pre-commit" in iapply._activation_output(result)


def test_activate_hooks_boundary_missing_binary_is_exec_error(tmp_path, monkeypatch):
    def boom(argv, **kw):
        raise execrun.ExecError(argv, rc=None, cause=execrun.CAUSE_MISSING_BINARY)

    monkeypatch.setattr(iapply.execrun, "run", boom)
    with pytest.raises(execrun.ExecError) as exc_info:
        iapply._activate_hooks(tmp_path)
    assert exc_info.value.cause == execrun.CAUSE_MISSING_BINARY


def test_activation_output_joins_streams_with_newline(tmp_path):
    out = iapply._activation_output(
        _exec_result(1, stdout="done", stderr="fatal: broken")
    )
    assert out == "done\nfatal: broken"


_LEFTHOOK_SHIM = (
    "#!/bin/sh\n"
    'if [ "$LEFTHOOK" = "0" ]; then\n  exit 0\nfi\n'
    'call_lefthook run "pre-commit" "$@"\n'
)


def _hooks_repo(root: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    hooks = root / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    return hooks


def test_preclean_removes_a_stale_lefthook_shim_backup(tmp_path):
    hooks = _hooks_repo(tmp_path)
    (hooks / "pre-commit.old").write_text(_LEFTHOOK_SHIM)
    (hooks / "pre-push.old").write_text(_LEFTHOOK_SHIM)
    iapply._preclean_stale_hook_backups(tmp_path)
    assert not (hooks / "pre-commit.old").exists()
    assert not (hooks / "pre-push.old").exists()


def test_preclean_preserves_a_non_lefthook_backup(tmp_path):
    hooks = _hooks_repo(tmp_path)
    consumer = "#!/bin/sh\necho my own pre-commit\nexit 0\n"
    (hooks / "pre-commit.old").write_text(consumer)
    (hooks / "pre-push.old").write_text("#!/bin/sh\n# mentions LEFTHOOK in a comment\n")
    iapply._preclean_stale_hook_backups(tmp_path)
    assert (hooks / "pre-commit.old").read_text() == consumer
    assert (hooks / "pre-push.old").is_file()


def test_preclean_leaves_the_live_hooks_untouched(tmp_path):
    hooks = _hooks_repo(tmp_path)
    (hooks / "pre-commit").write_text(_LEFTHOOK_SHIM)
    iapply._preclean_stale_hook_backups(tmp_path)
    assert (hooks / "pre-commit").read_text() == _LEFTHOOK_SHIM


def test_preclean_no_hooks_dir_is_a_noop(tmp_path):
    iapply._preclean_stale_hook_backups(tmp_path)


def test_preclean_removes_a_dangling_hook_symlink(tmp_path):
    hooks = _hooks_repo(tmp_path)
    (hooks / "pre-commit").symlink_to("../../scripts/does-not-exist")
    assert (hooks / "pre-commit").is_symlink()
    iapply._preclean_dangling_hook_symlinks(tmp_path)
    assert not (hooks / "pre-commit").is_symlink()
    assert not (hooks / "pre-commit").exists()


def test_preclean_preserves_a_live_hook_symlink(tmp_path):
    hooks = _hooks_repo(tmp_path)
    target = tmp_path / "real-hook.sh"
    target.write_text("#!/bin/sh\necho live\n")
    (hooks / "pre-commit").symlink_to(target)
    iapply._preclean_dangling_hook_symlinks(tmp_path)
    assert (hooks / "pre-commit").is_symlink()
    assert (hooks / "pre-commit").resolve() == target.resolve()


def test_preclean_preserves_a_symlink_whose_stat_fails_non_enoent(tmp_path):
    hooks = _hooks_repo(tmp_path)
    loop = hooks / "pre-commit"
    loop.symlink_to("pre-commit")
    assert loop.is_symlink()
    iapply._preclean_dangling_hook_symlinks(tmp_path)
    assert loop.is_symlink()


def test_preclean_leaves_a_real_hook_file_untouched(tmp_path):
    hooks = _hooks_repo(tmp_path)
    (hooks / "pre-commit").write_text(_LEFTHOOK_SHIM)
    iapply._preclean_dangling_hook_symlinks(tmp_path)
    assert (hooks / "pre-commit").read_text() == _LEFTHOOK_SHIM


def test_preclean_dangling_symlink_no_hooks_dir_is_a_noop(tmp_path):
    iapply._preclean_dangling_hook_symlinks(tmp_path)


def test_pr_install_precleans_a_dangling_hook_symlink_before_activation(tmp_path, rec):
    hooks = _hooks_repo(tmp_path)
    (hooks / "pre-commit").symlink_to("../../scripts/does-not-exist")
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    _apply(tmp_path, iapply.MODE_PR)
    assert not (hooks / "pre-commit").is_symlink()


def test_preclean_reaches_the_shared_hooks_dir_from_a_linked_worktree(tmp_path):
    main = tmp_path / "main"
    main.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=main, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.co"], cwd=main, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=main, check=True)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-qm", "init"], cwd=main, check=True
    )
    wt = tmp_path / "wt"
    subprocess.run(["git", "worktree", "add", "-q", str(wt)], cwd=main, check=True)
    assert (wt / ".git").is_file()

    shared = main / ".git" / "hooks"
    (shared / "pre-commit").symlink_to("../../scripts/does-not-exist")
    assert not (wt / ".git" / "hooks").exists()

    iapply._preclean_dangling_hook_symlinks(wt)
    assert not (shared / "pre-commit").is_symlink()
    assert not (shared / "pre-commit").exists()


def test_pr_install_precleans_a_stale_lefthook_backup_before_activation(tmp_path, rec):
    hooks = _hooks_repo(tmp_path)
    (hooks / "pre-commit.old").write_text(_LEFTHOOK_SHIM)
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    _apply(tmp_path, iapply.MODE_PR)
    assert not (hooks / "pre-commit.old").exists()


def test_pr_flow_restores_the_caller_branch(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    _apply(tmp_path, iapply.MODE_PR)
    assert rec.calls[-1] == ("switch_back", "main")


def test_pr_flow_restores_the_caller_branch_even_when_a_git_step_fails(
    tmp_path, rec, monkeypatch
):
    def boom(*a, **k):
        raise ExecError(["git", "push"], rc=1, stderr="denied")

    monkeypatch.setattr(git, "push", boom)
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    with pytest.raises(ExecError):
        _apply(tmp_path, iapply.MODE_PR)
    assert ("switch_back", "main") in rec.calls


def test_pr_flow_stages_the_managed_writes_into_the_real_index_before_restoring(
    tmp_path, rec
):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    _apply(tmp_path, iapply.MODE_PR)

    names = rec.names()
    assert names.count("add") == 2
    assert names.index("switch_back") == len(names) - 1
    assert rec.add_index_files[-1] is None
    restore_add = [paths for name, paths in rec.calls if name == "add"][-1]
    assert "AGENTS.md" in restore_add
    assert ".shipit.toml" in restore_add


def test_pr_flow_restore_never_stages_a_managed_path_the_caller_already_tracks(
    tmp_path, rec
):
    rec.tracked = {"AGENTS.md", ".shipit.toml"}
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    _apply(tmp_path, iapply.MODE_PR)

    adds = [paths for name, paths in rec.calls if name == "add"]
    restore_adds = [
        p for p, idx in zip(adds, rec.add_index_files, strict=True) if idx is None
    ]
    assert restore_adds, "the restore must still stage the untracked managed adds"
    staged = restore_adds[0]
    assert "AGENTS.md" not in staged
    assert ".shipit.toml" not in staged
    assert "AGENTS.md" in adds[0]
    assert ".shipit.toml" in adds[0]


def test_pr_flow_restore_never_stages_over_a_caller_staged_deletion(tmp_path, rec):
    rec.original_carries = {"AGENTS.md"}
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    _apply(tmp_path, iapply.MODE_PR)

    adds = [paths for name, paths in rec.calls if name == "add"]
    restore_adds = [
        p for p, idx in zip(adds, rec.add_index_files, strict=True) if idx is None
    ]
    assert restore_adds, "the restore must still stage the genuine untracked adds"
    assert "AGENTS.md" not in restore_adds[0]
    assert "AGENTS.md" in adds[0]
    assert "HEAD" in rec.tree_paths_refs
    assert [r for r in rec.tree_paths_refs if r != "HEAD"], (
        "the restore must read the caller's own branch to spot a staged deletion"
    )


def test_pr_flow_leaves_the_callers_index_alone_when_it_never_left_their_branch(
    tmp_path, rec, monkeypatch
):
    def boom(*a, **k):
        raise ExecError(["git", "switch", "-C"], rc=1, stderr="denied")

    monkeypatch.setattr(git, "switch_create", boom)
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    with pytest.raises(ExecError):
        _apply(tmp_path, iapply.MODE_PR)
    assert not any(name == "add" for name, _ in rec.calls)


def test_restore_caller_branch_over_real_git_survives_a_newly_added_managed_path(
    tmp_path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.co"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "AGENTS.md").write_text("old managed\n")
    (repo / "notes.txt").write_text("consumer\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (repo / "notes.txt").write_text("consumer — locally edited\n")

    git.switch_create(iapply.INSTALL_BRANCH, cwd=str(repo))
    git.reset_soft(base_sha, cwd=str(repo))
    (repo / ".shipit-skills").mkdir()
    (repo / ".shipit-skills" / "foo.md").write_text("new shipit skill\n")
    (repo / "AGENTS.md").write_text("new managed\n")
    staged_writes = {".shipit-skills/foo.md", "AGENTS.md"}
    index_file = str(tmp_path / "scratch-index")
    git.read_tree(base_sha, cwd=str(repo), index_file=index_file)
    git.add(sorted(staged_writes), cwd=str(repo), index_file=index_file)
    git.commit_all("reconcile", cwd=str(repo), no_verify=True, index_file=index_file)
    assert (repo / ".shipit-skills" / "foo.md").is_file()
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    assert ".shipit-skills/foo.md" in untracked

    iapply._restore_caller_branch(str(repo), "main", staged_writes)

    assert git.current_branch(cwd=str(repo)) == "main"
    assert not (repo / ".shipit-skills" / "foo.md").exists()
    assert (repo / "AGENTS.md").read_text() == "new managed\n"
    assert (repo / "notes.txt").read_text() == "consumer — locally edited\n"
    assert not subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    scratch_tree = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", iapply.INSTALL_BRANCH],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    assert ".shipit-skills/foo.md" in scratch_tree


def test_restore_over_real_git_preserves_caller_staged_content_on_a_managed_path(
    tmp_path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.co"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "AGENTS.md").write_text("old managed\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (repo / "AGENTS.md").write_text("caller staged edit\n")
    subprocess.run(["git", "add", "AGENTS.md"], cwd=repo, check=True)
    staged_blob = subprocess.run(
        ["git", "show", ":AGENTS.md"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    git.switch_create(iapply.INSTALL_BRANCH, cwd=str(repo))
    git.reset_soft(base_sha, cwd=str(repo))
    (repo / "AGENTS.md").write_text("new managed\n")
    (repo / ".shipit-skills").mkdir()
    (repo / ".shipit-skills" / "foo.md").write_text("new shipit skill\n")
    staged_writes = {"AGENTS.md", ".shipit-skills/foo.md"}
    index_file = str(tmp_path / "scratch-index")
    git.read_tree(base_sha, cwd=str(repo), index_file=index_file)
    git.add(sorted(staged_writes), cwd=str(repo), index_file=index_file)
    git.commit_all("reconcile", cwd=str(repo), no_verify=True, index_file=index_file)

    iapply._restore_caller_branch(str(repo), "main", staged_writes)

    assert (
        subprocess.run(
            ["git", "show", ":AGENTS.md"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        == staged_blob
        == "caller staged edit\n"
    )
    scratch_tree = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", iapply.INSTALL_BRANCH],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    assert ".shipit-skills/foo.md" in scratch_tree
    assert "AGENTS.md" in scratch_tree


def test_restore_over_real_git_preserves_caller_staged_deletion_of_a_managed_path(
    tmp_path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.co"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "AGENTS.md").write_text("old managed\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(["git", "rm", "-q", "--cached", "AGENTS.md"], cwd=repo, check=True)
    (repo / "AGENTS.md").unlink()

    git.switch_create(iapply.INSTALL_BRANCH, cwd=str(repo))
    git.reset_soft(base_sha, cwd=str(repo))
    (repo / "AGENTS.md").write_text("new managed\n")
    (repo / ".shipit-skills").mkdir()
    (repo / ".shipit-skills" / "foo.md").write_text("new shipit skill\n")
    staged_writes = {"AGENTS.md", ".shipit-skills/foo.md"}
    index_file = str(tmp_path / "scratch-index")
    git.read_tree(base_sha, cwd=str(repo), index_file=index_file)
    git.add(sorted(staged_writes), cwd=str(repo), index_file=index_file)
    git.commit_all("reconcile", cwd=str(repo), no_verify=True, index_file=index_file)

    iapply._restore_caller_branch(str(repo), "main", staged_writes)

    assert (
        "AGENTS.md"
        not in subprocess.run(
            ["git", "ls-files"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.split()
    )
    assert not subprocess.run(
        [
            "git",
            "diff",
            "--cached",
            "--name-only",
            "--diff-filter=AM",
            "--",
            "AGENTS.md",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    scratch_tree = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", iapply.INSTALL_BRANCH],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    assert ".shipit-skills/foo.md" in scratch_tree
    assert "AGENTS.md" in scratch_tree


def test_restore_over_real_git_stages_nothing_when_head_predates_the_reconcile(
    tmp_path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.co"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "AGENTS.md").write_text("old managed\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    git.switch_create(iapply.INSTALL_BRANCH, cwd=str(repo))
    git.reset_soft(base_sha, cwd=str(repo))
    (repo / ".shipit-skills").mkdir()
    (repo / ".shipit-skills" / "foo.md").write_text("new shipit skill\n")
    staged_writes = {"AGENTS.md", ".shipit-skills/foo.md"}

    iapply._restore_caller_branch(str(repo), "main", staged_writes)

    assert git.current_branch(cwd=str(repo)) == "main"
    assert not subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    assert ".shipit-skills/foo.md" in untracked


def test_pr_flow_from_detached_head_does_not_restore(tmp_path, rec, monkeypatch):
    monkeypatch.setattr(git, "current_branch", lambda *, cwd: None)
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    _apply(tmp_path, iapply.MODE_PR)
    assert not any(name == "switch_back" for name, _ in rec.calls)


def test_restore_caller_branch_skips_none_and_unstages_on_the_scratch_branch(
    tmp_path, rec
):
    iapply._restore_caller_branch(str(tmp_path), None, {"AGENTS.md"})
    assert rec.calls == []
    iapply._restore_caller_branch(str(tmp_path), iapply.INSTALL_BRANCH, {"AGENTS.md"})
    assert not any(name == "switch_back" for name, _ in rec.calls)
    assert ("reset_index", None) in rec.calls


def test_restore_committing_writes_is_a_transaction(tmp_path):
    (tmp_path / "kept.txt").write_bytes(b"MUTATED\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "new.txt").write_text("added by the failed apply\n")
    snapshot = {
        "kept.txt": iapply._SnapshotCell(b"before\n", 0o644),
        "sub/new.txt": None,
    }
    iapply._restore_committing_writes(tmp_path, snapshot)
    assert (tmp_path / "kept.txt").read_bytes() == b"before\n"
    assert not (tmp_path / "sub" / "new.txt").exists()


def test_restore_committing_writes_is_best_effort_across_files(tmp_path):
    (tmp_path / "blocked").mkdir()
    (tmp_path / "ok.txt").write_bytes(b"MUTATED\n")
    snapshot = {
        "blocked": iapply._SnapshotCell(b"never lands\n", 0o644),
        "ok.txt": iapply._SnapshotCell(b"restored\n", 0o644),
    }
    with pytest.raises(OSError, match="blocked"):
        iapply._restore_committing_writes(tmp_path, snapshot)
    assert (tmp_path / "ok.txt").read_bytes() == b"restored\n"


def test_restore_committing_writes_restores_the_original_mode(tmp_path):
    victim = tmp_path / "bin" / "shipit"
    victim.parent.mkdir(parents=True)
    victim.write_bytes(b"consumer launcher\n")
    victim.chmod(0o644)
    cell = iapply._snapshot_cell(victim)
    assert cell == iapply._SnapshotCell(b"consumer launcher\n", 0o644)
    victim.write_bytes(b"MANAGED launcher\n")
    victim.chmod(0o755)
    iapply._restore_committing_writes(tmp_path, {"bin/shipit": cell})
    assert victim.read_bytes() == b"consumer launcher\n"
    assert stat.S_IMODE(victim.stat().st_mode) == 0o644


def test_pr_selfcert_failure_rolls_the_staged_writes_back(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n\nConsumer text.\n")
    original_agents = (tmp_path / "AGENTS.md").read_text()

    def failing_cert(plan, root, **kw):
        return selfcert.CertReport(
            checks=(selfcert.CertCheck(name="planted", ok=False, detail="boom"),)
        )

    with pytest.raises(SelfCertError):
        _apply(tmp_path, iapply.MODE_PR, certify=failing_cert)

    assert rec.names() == []
    assert not (tmp_path / ".shipit.toml").exists()
    assert not (tmp_path / ".agents" / "skills" / "to-spec" / "SKILL.md").exists()
    assert not (tmp_path / "bin" / "shipit").exists()
    assert (tmp_path / "AGENTS.md").read_text() == original_agents

    again = _plan(tmp_path)
    assert not again.nothing_to_do
    assert again.writes


def test_pr_selfcert_failure_restores_an_executable_managed_files_mode(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    launcher = tmp_path / "bin" / "shipit"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("# the consumer's own launcher\n")
    launcher.chmod(0o644)

    def failing_cert(plan, root, **kw):
        return selfcert.CertReport(
            checks=(selfcert.CertCheck(name="planted", ok=False),)
        )

    with pytest.raises(SelfCertError):
        _apply(tmp_path, iapply.MODE_PR, certify=failing_cert)

    assert launcher.read_text() == "# the consumer's own launcher\n"
    assert stat.S_IMODE(launcher.stat().st_mode) == 0o644


def test_local_selfcert_failure_also_rolls_back(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")

    def failing_cert(plan, root, **kw):
        return selfcert.CertReport(
            checks=(selfcert.CertCheck(name="planted", ok=False),)
        )

    with pytest.raises(SelfCertError):
        _apply(tmp_path, iapply.MODE_LOCAL, certify=failing_cert)
    assert rec.names() == []
    assert not (tmp_path / ".shipit.toml").exists()
    assert not (tmp_path / "bin" / "shipit").exists()
    assert not _plan(tmp_path).nothing_to_do


PRISTINE_WORKFLOW = Path(__file__).parent / "data" / "copilot-review-pristine.yml"
RETIRED_WORKFLOW_PATH = ".github/workflows/copilot-review.yml"
PRISTINE_TO_PRD_SKILL = """---
name: to-prd
description: Turn the current conversation context into a PRD — the authoritative feature spec — and write it to docs/prd/. Use when user wants to create a PRD from the current context.
metadata:
    forked-from: https://github.com/mattpocock/skills (skills/engineering/to-prd)
---
This skill takes the current conversation context and codebase understanding and produces a PRD. Do NOT re-run the requirements interview — that happened earlier, in `/grill-me-with-docs`; synthesize the PRD from what you already know. This is not a fully AFK skill, though: step 2 still expects a short confirmation of the module boundaries and test scope with the user. That scoped confirmation is not a requirements interview.

The issue tracker and triage label vocabulary should have been provided to you — run `/setup-matt-pocock-skills` if not.

## Process

1. Explore the repo to understand the current state of the codebase, if you haven't already. Use the project's domain glossary vocabulary (`CONTEXT.md`) throughout the PRD, and respect any ADRs in the area you're touching.

2. Sketch out the major modules you will need to build or modify to complete the implementation. Actively look for opportunities to extract deep modules that can be tested in isolation.

A deep module (as opposed to a shallow module) is one which encapsulates a lot of functionality in a simple, testable interface which rarely changes.

Check with the user that these modules match their expectations. Check with the user which modules they want tests written for.

3. Write the PRD using the template below. **The PRD is the authoritative feature definition / spec** — the *what & why*. It is a file, not an issue body:

   - Write it to `docs/prd/<slug>.md`. This file is the single source of truth for the spec.
   - That is the whole output of this skill. Do NOT open an epic tracker issue here. The **epic GitHub issue is an execution tracker** — it summarizes the PRD and points to it plus the relevant ADRs — and it is created later, in `/to-tickets` (the issue-planning leg), not by this skill.
   - The epic code (`THEME+NN`, e.g. `GPU02`) is assigned by the human, but it is used later in `/to-tickets` when the epic issue is minted — not here.

4. Once the PRD file is written, record the milestone in the dev-cycle log (best-effort — ADR-0032; if the command errors, continue — a skipped emission is a missing event, never a broken step):

   ```sh
   shipit log event planning.prd.written --about "PRD: docs/prd/<slug>.md"
   ```

<prd-template>

## Problem Statement

The problem that the user is facing, from the user's perspective.

## Solution

The solution to the problem, from the user's perspective.

## User Stories

A LONG, numbered list of user stories. Each user story should be in the format of:

1. As an <actor>, I want a <feature>, so that <benefit>

<user-story-example>
1. As a mobile bank customer, I want to see balance on my accounts, so that I can make better informed decisions about my spending
</user-story-example>

This list of user stories should be extremely extensive and cover all aspects of the feature.

## Implementation Decisions

A list of implementation decisions that were made. This can include:

- The modules that will be built/modified
- The interfaces of those modules that will be modified
- Technical clarifications from the developer
- Architectural decisions
- Schema changes
- API contracts
- Specific interactions

Do NOT include specific file paths or code snippets. They may end up being outdated very quickly.

Exception: if a prototype produced a snippet that encodes a decision more precisely than prose can (state machine, reducer, schema, type shape), inline it within the relevant decision and note briefly that it came from a prototype. Trim to the decision-rich parts — not a working demo, just the important bits.

## Testing Decisions

A list of testing decisions that were made. Include:

- A description of what makes a good test (only test external behavior, not implementation details)
- Which modules will be tested
- Prior art for the tests (i.e. similar types of tests in the codebase)

## Out of Scope

A description of the things that are out of scope for this PRD.

## Further Notes

Any further notes about the feature.

</prd-template>
"""
PRISTINE_GRILL_ADR_FORMAT_SKILL = (
    Path(__file__).parent / "data" / "grill-adr-format-skill-pristine"
)
RETIRED_SKILL_HASHES = {
    "skills/shipit-planning/SKILL.md": (
        "sha256:a16ac4744238b3a5b59da8a887bb6268742fd01a8a285797e0198aba49e44336",
    ),
    "skills/shipit-grill-with-docs/SKILL.md": (
        "sha256:47c25fe56510de6a63da1de9121ef9b6704808f3631d43c7f9ee745f2c32ff62",
    ),
    "skills/shipit-grill-with-docs/ADR-FORMAT.md": (
        "sha256:f1f36cd3f8d3b6474ddd5855da4e233bfc4ae1a1c5024909ccf11871819a41b2",
    ),
    "skills/shipit-grill-with-docs/CONTEXT-FORMAT.md": (
        "sha256:886ce0e96fd0f76f4c72c337c049cf4655227c599862ce920a62297e0929beae",
    ),
    "skills/shipit-to-prd/SKILL.md": (
        "sha256:0f13f20cad06161baff87628ea6b1cf5bac0cc7919beb6176535f9cdf9ae42d8",
        "sha256:4bdf82e153221545c8340744a5def096316c0cf88f0db9548a373bce6f91d0c1",
    ),
    "skills/to-prd/SKILL.md": (
        "sha256:3b1fc2aa002d78a63f9bd858144be177a1b2f69a2ca97e2ca165bc86f6ca5a2e",
    ),
    "skills/shipit-to-issues/SKILL.md": (
        "sha256:4df3706b12c89fb7d844521800addea1c9ab9f448cd7f926b993a5d92f46869b",
        "sha256:e623a477ad4d81c042b5bbc20fead9cd208b1c73cb35d2954b1fdcd7303d9474",
    ),
}


def test_decide_retired_covers_the_matrix():
    assert (
        irec.decide_retired(actual_hash=None, pristine_hashes=("a", "b")) == irec.NOOP
    )
    assert irec.decide_retired(actual_hash="a", pristine_hashes=("a",)) == irec.DELETE
    assert (
        irec.decide_retired(actual_hash="b", pristine_hashes=("a", "b", "c"))
        == irec.DELETE
    )
    assert irec.decide_retired(actual_hash="x", pristine_hashes=("a", "b")) == irec.KEEP
    assert irec.decide_retired(actual_hash="x", pristine_hashes=()) == irec.KEEP


def test_plan_retired_decides_every_manifest_entry():
    entries = [
        irec.RetiredFile(path="a.yml", pristine_hashes=("h1",)),
        irec.RetiredFile(path="b.yml", pristine_hashes=("h2",)),
        irec.RetiredFile(path="c.yml", pristine_hashes=("h3",)),
    ]
    decisions = irec.plan_retired(
        entries, {"a.yml": "h1", "b.yml": "edited", "c.yml": None}
    )
    assert [d.action for d in decisions] == [
        irec.DELETE,
        irec.KEEP,
        irec.NOOP,
    ]


def test_retire_hook_deletes_carries_delete_only_never_noop():
    hooks = irec.plan_retired_hooks(
        [
            irec.RetiredHook(file=".claude/settings.json", event="X", marker="gone"),
            irec.RetiredHook(file=".claude/settings.json", event="X", marker="present"),
        ],
        {
            irec.RetiredHook(
                file=".claude/settings.json", event="X", marker="present"
            ).key: 1
        },
    )
    plan = irec.Plan(
        root="/x",
        decisions=(),
        retired=(),
        seeds=(),
        retired_hooks=tuple(hooks),
    )

    assert [d.action for d in plan.retired_hooks] == [irec.NOOP, irec.DELETE]
    assert {d.retired.file for d in plan.retire_hook_deletes} == {
        ".claude/settings.json"
    }
    assert len(plan.retire_hook_deletes) == 1


@pytest.mark.parametrize(
    "bad",
    [
        "/etc/passwd",
        "C:\\Windows\\system32\\config",
        "C:tmp\\x.yml",
        "\\outside.yml",
        "../outside.yml",
        "nested/../../outside.yml",
        "nested\\..\\..\\outside.yml",
        "",
    ],
)
def test_retired_path_rejects_unsafe_manifest_entries(bad):
    with pytest.raises(ValueError, match="unsafe path"):
        irec._retired_path(bad)


def test_retired_path_accepts_a_plain_relative_path():
    assert irec._retired_path(".github/workflows/x.yml") == ".github/workflows/x.yml"


def test_retired_manifest_carries_the_copilot_workflow_history():
    retired = irec.load_retired()
    entry = next(r for r in retired if r.path == RETIRED_WORKFLOW_PATH)
    assert all(h.startswith("sha256:") for h in entry.pristine_hashes)
    fixture_hash = config.content_hash(PRISTINE_WORKFLOW.read_bytes())
    assert fixture_hash in entry.pristine_hashes


def test_retired_manifest_carries_the_renamed_skill_history():
    retired = {r.path: r for r in irec.load_retired()}

    for path, expected_hashes in RETIRED_SKILL_HASHES.items():
        assert retired[path].pristine_hashes == expected_hashes


RELOCATED_SKILL_STORE_PATHS = (
    "coordinating/SKILL.md",
    "grill-me-with-docs/ADR-FORMAT.md",
    "grill-me-with-docs/CONTEXT-FORMAT.md",
    "grill-me-with-docs/SKILL.md",
    "implementing/SKILL.md",
    "lex-primer/SKILL.md",
    "planning/SKILL.md",
    "shepherding-prs/SKILL.md",
    "shipit-session-status/SKILL.md",
    "to-spec/SKILL.md",
    "to-tickets/SKILL.md",
)


PRISTINE_COORDINATING_SKILL = (
    Path(__file__).parent / "data" / "coordinating-skill-pristine"
)


RETIRED_RELOCATED_SKILL_HASHES = {
    "skills/coordinating/SKILL.md": (
        "sha256:065a793b117dcfbb1e97fea0a80b405c69995850af9a350061dbcc1f4ea4976a",
        "sha256:d39855cd948076fd3d25b10ba345efd4e0f6f8eea26fcc546723cc063c4511bc",
    ),
    "skills/grill-me-with-docs/ADR-FORMAT.md": (
        "sha256:f1f36cd3f8d3b6474ddd5855da4e233bfc4ae1a1c5024909ccf11871819a41b2",
    ),
    "skills/grill-me-with-docs/CONTEXT-FORMAT.md": (
        "sha256:d7f1244807b547de07073244e67279eda8bb0fd681428114bce8452c979ab72c",
    ),
    "skills/grill-me-with-docs/SKILL.md": (
        "sha256:a4eefcb8f9af47b5b2f99a85bde5e52c4b2275c6660c964021f6d186f05b72fd",
        "sha256:a7e8a73594379e378ca2700cd8cb5c8a0b91c32aeaad919b487a52461f1b5c4c",
        "sha256:06428ff9352d2765056d1af43f9a21592b0ea6e59ab7b72f200710678fcdddea",
    ),
    "skills/implementing/SKILL.md": (
        "sha256:fd7d90eff365955e096dbdf2c72ac6bf32a33b12609d7749aa8a544b8cb259a0",
        "sha256:02040db0bd1aedce096473bdbc9d65ec741a81c989f97121fedc242e172a4932",
        "sha256:9aad3759cc233430e9d5a896a33ead5c6ae81e71b5a78af35e8b15133ef5cc69",
    ),
    "skills/lex-primer/SKILL.md": (
        "sha256:eb7c4d13f980dbbf0fdc81998ce6d8815783af5f13fabb18decb7e834bfddee0",
        "sha256:ff616c1c447661298fb8e06a8a8a6ce60400d838c7c881b9e8bd526128e32901",
    ),
    "skills/planning/SKILL.md": (
        "sha256:e26fe5ef4bba7d0bffd09f2dbbf61ba9ea8b0a8aa77a50bb794481766f8fa791",
        "sha256:172678dc99e45431970d2cdd31c6c910622590e7e6893b26026a282fcdc49919",
        "sha256:843d0a0c3dddb6d1ba1b5db6d494e650fdbc40035cb98def47b964d264b8e57c",
    ),
    "skills/shepherding-prs/SKILL.md": (
        "sha256:8f627079f3d9846069742dd77cf4987eb550ff9a6c5e575f751ea8edf958ee2b",
        "sha256:b7250030fa91282fa6d7d5a65073363edbc4522e31984dd8983994dcf9a5aa85",
        "sha256:fed591f8346731ac2e4bb8de18c5fce0f6edd199c2f3ee5bf1cb036114cd4063",
    ),
    "skills/shipit-session-status/SKILL.md": (
        "sha256:4720ad9b381d57c32dfd2aca6cb2a4b338dfa69cac90eedd85a71f7f2f8db962",
    ),
    "skills/to-spec/SKILL.md": (
        "sha256:9f7e5216675070146daabdc0f63ddb8cc8ecf37843ab5d3290354bf57d8097c0",
        "sha256:c614f7c42621a51dc5c4f9b27d0d74c52e43d87483fc56d45ce9127a8a6e2484",
        "sha256:eafedcd9b9797aa3d9e97ee466e40000cc74de2d86f86e855f6e1e36219d3cce",
    ),
    "skills/to-tickets/SKILL.md": (
        "sha256:38df45b1560de0e10be3ab8f7ef4113114ee851e3790ac9087b4c9560c7dc6df",
        "sha256:271ffbaaa98315c752ddf9c2d01e504b041140bdc9c5a21fc7ac152891931b16",
        "sha256:d922c867458135ebf665233149268b25ba8ea3147a59e5b7ffcc9c3748fda3d5",
        "sha256:11419ddc56ed6a6b07cc780b1c38aa5cf20bcdfb89479bda2f4e032a4ea80b57",
    ),
}


def test_retired_manifest_carries_the_relocated_skill_store():
    assert set(RETIRED_RELOCATED_SKILL_HASHES) == {
        f"skills/{rel}" for rel in RELOCATED_SKILL_STORE_PATHS
    }
    retired = {r.path: r for r in irec.load_retired()}
    units = {u.key: u for u in iunits.load_units()}
    for rel in RELOCATED_SKILL_STORE_PATHS:
        new_key = f".agents/skills/{rel}"
        old_path = f"skills/{rel}"
        assert new_key in units, f"{new_key} no longer delivered"
        assert old_path in retired, f"{old_path} not retired"
        assert (
            retired[old_path].pristine_hashes
            == RETIRED_RELOCATED_SKILL_HASHES[old_path]
        )


def test_install_deletes_a_pristine_relocated_skill_and_installs_new_store(
    tmp_path, rec
):
    old_path = "skills/coordinating/SKILL.md"
    old_bytes = PRISTINE_COORDINATING_SKILL.read_bytes()
    assert config.content_hash(old_bytes) in {
        h for r in irec.load_retired() if r.path == old_path for h in r.pristine_hashes
    }
    victim = tmp_path / old_path
    victim.parent.mkdir(parents=True)
    victim.write_bytes(old_bytes)

    plan = _plan(tmp_path)
    assert old_path in [d.retired.path for d in plan.retire_deletes]
    _apply(tmp_path)
    assert not victim.exists()
    assert (tmp_path / ".agents/skills/coordinating/SKILL.md").is_file()
    assert (tmp_path / ".claude/skills/coordinating/SKILL.md").is_file()


RETIRED_SHIPIT_SKILLS_STORE_HASHES = {
    ".shipit-skills/coordinating/SKILL.md": (
        "sha256:065a793b117dcfbb1e97fea0a80b405c69995850af9a350061dbcc1f4ea4976a",
    ),
    ".shipit-skills/grill-me-with-docs/ADR-FORMAT.md": (
        "sha256:f1f36cd3f8d3b6474ddd5855da4e233bfc4ae1a1c5024909ccf11871819a41b2",
    ),
    ".shipit-skills/grill-me-with-docs/CONTEXT-FORMAT.md": (
        "sha256:d7f1244807b547de07073244e67279eda8bb0fd681428114bce8452c979ab72c",
    ),
    ".shipit-skills/grill-me-with-docs/SKILL.md": (
        "sha256:06428ff9352d2765056d1af43f9a21592b0ea6e59ab7b72f200710678fcdddea",
    ),
    ".shipit-skills/implementing/SKILL.md": (
        "sha256:fd7d90eff365955e096dbdf2c72ac6bf32a33b12609d7749aa8a544b8cb259a0",
        "sha256:9aad3759cc233430e9d5a896a33ead5c6ae81e71b5a78af35e8b15133ef5cc69",
    ),
    ".shipit-skills/lex-primer/SKILL.md": (
        "sha256:eb7c4d13f980dbbf0fdc81998ce6d8815783af5f13fabb18decb7e834bfddee0",
    ),
    ".shipit-skills/planning/SKILL.md": (
        "sha256:843d0a0c3dddb6d1ba1b5db6d494e650fdbc40035cb98def47b964d264b8e57c",
    ),
    ".shipit-skills/shepherding-prs/SKILL.md": (
        "sha256:8f627079f3d9846069742dd77cf4987eb550ff9a6c5e575f751ea8edf958ee2b",
        "sha256:fed591f8346731ac2e4bb8de18c5fce0f6edd199c2f3ee5bf1cb036114cd4063",
    ),
    ".shipit-skills/shipit-session-status/SKILL.md": (
        "sha256:4720ad9b381d57c32dfd2aca6cb2a4b338dfa69cac90eedd85a71f7f2f8db962",
    ),
    ".shipit-skills/to-spec/SKILL.md": (
        "sha256:eafedcd9b9797aa3d9e97ee466e40000cc74de2d86f86e855f6e1e36219d3cce",
    ),
    ".shipit-skills/to-tickets/SKILL.md": (
        "sha256:d922c867458135ebf665233149268b25ba8ea3147a59e5b7ffcc9c3748fda3d5",
        "sha256:11419ddc56ed6a6b07cc780b1c38aa5cf20bcdfb89479bda2f4e032a4ea80b57",
    ),
}


def test_retired_manifest_carries_the_shipit_skills_store_history():
    assert set(RETIRED_SHIPIT_SKILLS_STORE_HASHES) == {
        f".shipit-skills/{rel}" for rel in RELOCATED_SKILL_STORE_PATHS
    }
    retired = {r.path: r for r in irec.load_retired()}
    for path, expected_hashes in RETIRED_SHIPIT_SKILLS_STORE_HASHES.items():
        assert retired[path].pristine_hashes == expected_hashes


def test_every_shipit_skills_store_path_is_retired_and_still_delivered():
    retired = {r.path: r for r in irec.load_retired()}
    units = {u.key: u for u in iunits.load_units()}
    for rel in RELOCATED_SKILL_STORE_PATHS:
        old_path = f".shipit-skills/{rel}"
        new_key = f"{iunits.AGENTS_SKILLS_DIR}/{rel}"
        assert new_key in units, f"{new_key} no longer delivered"
        assert old_path in retired, f"{old_path} not retired"


def test_no_retired_path_is_live_in_shipits_own_repo():
    live = [r.path for r in irec.load_retired() if (REPO_ROOT / r.path).exists()]
    assert live == []


def test_skills_root_is_ordinary_package_data_under_src():
    assert Path(str(iunits.skills_root())) == SKILL_STORE
    assert not (REPO_ROOT / ".shipit-skills").exists()
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_bytes().decode())
    force = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert ".shipit-skills" not in force


def test_install_deletes_a_pristine_shipit_skills_store_copy(tmp_path, rec):
    old_path = ".shipit-skills/coordinating/SKILL.md"
    old_bytes = PRISTINE_COORDINATING_SKILL.read_bytes()
    assert config.content_hash(old_bytes) in {
        h for r in irec.load_retired() if r.path == old_path for h in r.pristine_hashes
    }
    victim = tmp_path / old_path
    victim.parent.mkdir(parents=True)
    victim.write_bytes(old_bytes)

    plan = _plan(tmp_path)
    assert old_path in [d.retired.path for d in plan.retire_deletes]
    _apply(tmp_path)
    assert not victim.exists()
    assert (tmp_path / ".agents/skills/coordinating/SKILL.md").is_file()


def test_install_keeps_a_hand_edited_shipit_skills_store_copy(tmp_path, rec):
    old_path = ".shipit-skills/coordinating/SKILL.md"
    victim = tmp_path / old_path
    victim.parent.mkdir(parents=True)
    victim.write_text("my own notes on coordinating\n")

    plan = _plan(tmp_path)
    assert old_path not in [d.retired.path for d in plan.retire_deletes]
    assert old_path in [d.retired.path for d in plan.retired if d.action == irec.KEEP]
    _apply(tmp_path)
    assert victim.read_text() == "my own notes on coordinating\n"


PRISTINE_CLAUDE_START = Path(__file__).parent / "data" / "claude-start-pristine"
PRISTINE_CODEX_START = Path(__file__).parent / "data" / "codex-start-pristine"
RETIRED_LAUNCHER_SHIMS = {
    "claude-start": PRISTINE_CLAUDE_START,
    "codex-start": PRISTINE_CODEX_START,
}


def test_retired_manifest_carries_the_launcher_shim_history():
    retired = {r.path: r for r in irec.load_retired()}
    for path, fixture in RETIRED_LAUNCHER_SHIMS.items():
        entry = retired[path]
        assert all(h.startswith("sha256:") for h in entry.pristine_hashes)
        assert config.content_hash(fixture.read_bytes()) in entry.pristine_hashes


@pytest.mark.parametrize("path", sorted(RETIRED_LAUNCHER_SHIMS))
def test_install_deletes_a_pristine_retired_launcher_shim(tmp_path, rec, path):
    victim = tmp_path / path
    victim.write_bytes(RETIRED_LAUNCHER_SHIMS[path].read_bytes())

    plan = _plan(tmp_path)
    assert path in [d.retired.path for d in plan.retire_deletes]
    _apply(tmp_path)
    assert not victim.exists()
    assert (tmp_path / iunits.AGENT_LAUNCHER_FILE).is_file()


@pytest.mark.parametrize("path", sorted(RETIRED_LAUNCHER_SHIMS))
def test_install_keeps_a_modified_retired_launcher_shim(tmp_path, rec, path):
    victim = tmp_path / path
    victim.write_bytes(RETIRED_LAUNCHER_SHIMS[path].read_bytes() + b"# local\n")

    plan = _plan(tmp_path)
    assert [d.retired.path for d in plan.retire_keeps] == [path]
    _apply(tmp_path)
    assert victim.exists()


def test_install_deletes_a_pristine_retired_file(tmp_path, rec):
    victim = tmp_path / RETIRED_WORKFLOW_PATH
    victim.parent.mkdir(parents=True)
    victim.write_bytes(PRISTINE_WORKFLOW.read_bytes())

    plan = _plan(tmp_path)
    assert [d.retired.path for d in plan.retire_deletes] == [RETIRED_WORKFLOW_PATH]
    assert f"delete   {RETIRED_WORKFLOW_PATH} (retired)" in verb.format_plan(plan)
    _apply(tmp_path)
    assert not victim.exists()


def test_install_deletes_a_pristine_retired_skill_file(tmp_path, rec):
    retired_path = "skills/shipit-grill-with-docs/ADR-FORMAT.md"
    old_bytes = PRISTINE_GRILL_ADR_FORMAT_SKILL.read_bytes()
    assert config.content_hash(old_bytes) in RETIRED_SKILL_HASHES[retired_path]
    victim = tmp_path / retired_path
    victim.parent.mkdir(parents=True)
    victim.write_bytes(old_bytes)

    plan = _plan(tmp_path)
    assert retired_path in [d.retired.path for d in plan.retire_deletes]
    _apply(tmp_path)
    assert not victim.exists()
    assert (tmp_path / ".agents/skills/grill-me-with-docs/ADR-FORMAT.md").is_file()


def test_install_deletes_a_pristine_retired_to_prd_skill_and_installs_to_spec(
    tmp_path, rec
):
    retired_path = "skills/to-prd/SKILL.md"
    old_bytes = PRISTINE_TO_PRD_SKILL.encode()
    assert config.content_hash(old_bytes) in RETIRED_SKILL_HASHES[retired_path]
    victim = tmp_path / retired_path
    victim.parent.mkdir(parents=True)
    victim.write_bytes(old_bytes)

    plan = _plan(tmp_path)
    assert retired_path in [d.retired.path for d in plan.retire_deletes]
    _apply(tmp_path)
    assert not victim.exists()
    assert (tmp_path / ".agents/skills/to-spec/SKILL.md").is_file()


def test_install_keeps_a_modified_retired_file_with_warning(tmp_path, rec):
    victim = tmp_path / RETIRED_WORKFLOW_PATH
    victim.parent.mkdir(parents=True)
    victim.write_text(PRISTINE_WORKFLOW.read_text() + "# local tweak\n")

    plan = _plan(tmp_path)
    assert [d.retired.path for d in plan.retire_keeps] == [RETIRED_WORKFLOW_PATH]
    assert f"keep     {RETIRED_WORKFLOW_PATH} (retired; locally modified)" in (
        verb.format_plan(plan)
    )
    assert f"retired file kept: {RETIRED_WORKFLOW_PATH}" in (
        verb.format_plan_warnings(plan)
    )
    _apply(tmp_path)
    assert victim.is_file()
    assert "# local tweak" in victim.read_text()


def test_install_keeps_a_symlink_at_a_retired_path(tmp_path, rec):
    target = tmp_path / "elsewhere.yml"
    target.write_bytes(PRISTINE_WORKFLOW.read_bytes())
    victim = tmp_path / RETIRED_WORKFLOW_PATH
    victim.parent.mkdir(parents=True)
    victim.symlink_to(target)

    plan = _plan(tmp_path)
    assert [d.retired.path for d in plan.retire_keeps] == [RETIRED_WORKFLOW_PATH]
    _apply(tmp_path)
    assert victim.is_symlink()
    assert f"keep     {RETIRED_WORKFLOW_PATH} (retired; locally modified)" in (
        verb.format_plan(plan)
    )


def test_retired_delete_alone_is_still_a_write(tmp_path, rec):
    _apply(tmp_path)
    victim = tmp_path / RETIRED_WORKFLOW_PATH
    victim.parent.mkdir(parents=True)
    victim.write_bytes(PRISTINE_WORKFLOW.read_bytes())

    plan = _plan(tmp_path)
    assert not plan.writes and plan.retire_deletes
    assert not plan.nothing_to_do
    assert "nothing to do" not in verb.format_plan(plan)
    _apply(tmp_path)
    assert not victim.exists()

    again = _plan(tmp_path)
    assert again.nothing_to_do
    assert "nothing to do" in verb.format_plan(again)


def test_kept_retired_file_changes_the_nothing_to_do_wording(tmp_path, rec):
    _apply(tmp_path)
    victim = tmp_path / RETIRED_WORKFLOW_PATH
    victim.parent.mkdir(parents=True)
    victim.write_text(PRISTINE_WORKFLOW.read_text() + "# local tweak\n")

    plan = _plan(tmp_path)
    assert plan.nothing_to_do and plan.retire_keeps
    report = verb.format_plan(plan)
    assert "nothing to do — no automated changes to apply." in report
    assert "managed set is current" not in report
    assert victim.is_file()


def test_dry_run_reports_but_keeps_a_pristine_retired_file(tmp_path, rec):
    victim = tmp_path / RETIRED_WORKFLOW_PATH
    victim.parent.mkdir(parents=True)
    victim.write_bytes(PRISTINE_WORKFLOW.read_bytes())

    plan = _plan(tmp_path)
    report = verb.format_plan(plan, dry_run=True)
    assert f"delete   {RETIRED_WORKFLOW_PATH} (retired)" in report
    assert "1 retired delete(s)" in report
    rc = verb.run(str(tmp_path), dry_run=True)
    assert rc == 0
    assert victim.is_file()
    assert rec.calls == []


def test_pr_install_commits_the_retired_deletion_and_reports_it(tmp_path, rec):
    victim = tmp_path / RETIRED_WORKFLOW_PATH
    victim.parent.mkdir(parents=True)
    victim.write_bytes(PRISTINE_WORKFLOW.read_bytes())

    plan = _plan(tmp_path)
    assert RETIRED_WORKFLOW_PATH in plan.changed_paths
    _apply(tmp_path, iapply.MODE_PR)
    assert not victim.exists()
    added = next(paths for name, paths in rec.calls if name == "add")
    assert RETIRED_WORKFLOW_PATH not in added
    assert RETIRED_WORKFLOW_PATH in rec.rm_cached_paths
    assert "### Retired files removed" in rec.pr_body
    assert RETIRED_WORKFLOW_PATH in rec.pr_body


LEGACY_RELEASE_CORE_ENTRY = {
    "matcher": "startup|resume",
    "hooks": [
        {
            "type": "command",
            "command": '"$CLAUDE_PROJECT_DIR"/bin/install-release-core',
        }
    ],
}
LEGACY_SETUP_DEV_ENV_ENTRY = {
    "matcher": "startup|resume",
    "hooks": [
        {
            "type": "command",
            "command": '"$CLAUDE_PROJECT_DIR"/bin/setup-dev-env.sh',
        }
    ],
}
RETIRED_RELEASE_CORE_KEY = (
    ".claude/settings.json#SessionStart[bin/install-release-core]"
)


def _managed_sessionstart_entry() -> dict:
    return json.loads(iunits.data_bytes("claude-settings-sessionstart.json"))


def test_is_retired_hook_matches_marker_but_protects_managed_entries():
    assert splice.is_retired_hook(LEGACY_RELEASE_CORE_ENTRY, "bin/install-release-core")
    assert splice.is_retired_hook(LEGACY_SETUP_DEV_ENV_ENTRY, "bin/setup-dev-env.sh")
    managed = _managed_sessionstart_entry()
    assert splice.is_shipit_hook(managed, "bin/setup-dev-env.sh")
    assert not splice.is_retired_hook(managed, "bin/setup-dev-env.sh")
    assert not splice.is_retired_hook(None, "bin/install-release-core")
    assert not splice.is_retired_hook({"hooks": None}, "bin/install-release-core")


def test_decide_retired_hook_covers_both_cases():
    assert irec.decide_retired_hook(count=0) == irec.NOOP
    assert irec.decide_retired_hook(count=1) == irec.DELETE
    assert irec.decide_retired_hook(count=3) == irec.DELETE


def _settings_with_legacy_and_managed() -> str:
    return json.dumps(
        {
            "permissions": {"allow": ["Bash(ls:*)"]},
            "hooks": {
                "SessionStart": [
                    LEGACY_RELEASE_CORE_ENTRY,
                    _managed_sessionstart_entry(),
                ],
                "Stop": [{"hooks": [{"type": "command", "command": "echo hi"}]}],
            },
        },
        indent=2,
    )


def test_count_and_remove_retired_hooks_own_only_the_matched_entries():
    text = _settings_with_legacy_and_managed()
    assert (
        splice.count_retired_hooks(text, "SessionStart", "bin/install-release-core")
        == 1
    )
    out = splice.remove_retired_hooks(text, "SessionStart", "bin/install-release-core")
    data = json.loads(out)
    commands = [h["command"] for e in data["hooks"]["SessionStart"] for h in e["hooks"]]
    assert commands == [_managed_sessionstart_entry()["hooks"][0]["command"]]
    assert data["permissions"] == {"allow": ["Bash(ls:*)"]}
    assert data["hooks"]["Stop"] == [
        {"hooks": [{"type": "command", "command": "echo hi"}]}
    ]


def test_remove_retired_hooks_drops_an_emptied_event_array():
    text = json.dumps(
        {"hooks": {"SessionStart": [LEGACY_RELEASE_CORE_ENTRY]}, "other": True}
    )
    out = splice.remove_retired_hooks(text, "SessionStart", "bin/install-release-core")
    data = json.loads(out)
    assert "hooks" not in data
    assert data["other"] is True


@pytest.mark.parametrize(
    "text",
    [
        "{not json",
        '["a", "b"]',
        json.dumps({"hooks": {"SessionStart": "not-a-list"}}),
        json.dumps({"hooks": {"Stop": [LEGACY_RELEASE_CORE_ENTRY]}}),
        "",
    ],
)
def test_remove_retired_hooks_returns_untouchable_files_verbatim(text):
    assert (
        splice.count_retired_hooks(text, "SessionStart", "bin/install-release-core")
        == 0
    )
    assert (
        splice.remove_retired_hooks(text, "SessionStart", "bin/install-release-core")
        == text
    )


def test_retired_hooks_manifest_carries_the_legacy_sessionstart_entries():
    hooks = irec.load_retired_hooks()
    assert [(h.file, h.event, h.marker) for h in hooks] == [
        (".claude/settings.json", "SessionStart", "bin/install-release-core"),
        (".claude/settings.json", "SessionStart", "bin/setup-dev-env.sh"),
    ]


def test_retired_manifest_carries_the_install_release_core_history():
    retired = {r.path: r for r in irec.load_retired()}
    entry = retired["bin/install-release-core"]
    assert len(entry.pristine_hashes) == 8
    assert all(h.startswith("sha256:") for h in entry.pristine_hashes)


def test_install_removes_a_legacy_sessionstart_hook_entry(tmp_path, rec):
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(_settings_with_legacy_and_managed(), encoding="utf-8")

    plan = _plan(tmp_path)
    assert [d.retired.key for d in plan.retire_hook_deletes] == [
        RETIRED_RELEASE_CORE_KEY
    ]
    assert f"delete   {RETIRED_RELEASE_CORE_KEY} (retired hook entry)" in (
        verb.format_plan(plan)
    )
    _apply(tmp_path)
    data = json.loads(settings.read_text(encoding="utf-8"))
    commands = [h["command"] for e in data["hooks"]["SessionStart"] for h in e["hooks"]]
    assert not any("install-release-core" in c for c in commands)
    assert any("shipit hook sessionstart" in c for c in commands)
    assert data["permissions"] == {"allow": ["Bash(ls:*)"]}


def test_install_removes_the_duplicate_setup_dev_env_entry_only(tmp_path, rec):
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps({"hooks": {"SessionStart": [LEGACY_SETUP_DEV_ENV_ENTRY]}}),
        encoding="utf-8",
    )

    plan = _plan(tmp_path)
    assert [d.retired.marker for d in plan.retire_hook_deletes] == [
        "bin/setup-dev-env.sh"
    ]
    _apply(tmp_path)
    data = json.loads(settings.read_text(encoding="utf-8"))
    commands = [h["command"] for e in data["hooks"]["SessionStart"] for h in e["hooks"]]
    assert len(commands) == 1
    assert "shipit hook sessionstart" in commands[0]

    again = _plan(tmp_path)
    assert not again.retire_hook_deletes
    assert again.nothing_to_do


def test_retired_hook_delete_alone_is_still_a_write(tmp_path, rec):
    _apply(tmp_path)
    settings = tmp_path / ".claude" / "settings.json"
    data = json.loads(settings.read_text(encoding="utf-8"))
    data["hooks"]["SessionStart"].insert(0, LEGACY_RELEASE_CORE_ENTRY)
    settings.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    plan = _plan(tmp_path)
    assert not plan.writes and plan.retire_hook_deletes
    assert not plan.nothing_to_do
    _apply(tmp_path)
    assert "install-release-core" not in settings.read_text(encoding="utf-8")

    again = _plan(tmp_path)
    assert again.nothing_to_do


def test_pr_install_commits_the_retired_hook_removal_and_reports_it(tmp_path, rec):
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps({"hooks": {"SessionStart": [LEGACY_RELEASE_CORE_ENTRY]}}),
        encoding="utf-8",
    )

    plan = _plan(tmp_path)
    assert ".claude/settings.json" in plan.changed_paths
    _apply(tmp_path, iapply.MODE_PR)
    added = next(paths for name, paths in rec.calls if name == "add")
    assert ".claude/settings.json" in added
    assert "### Retired hook entries removed" in rec.pr_body
    assert RETIRED_RELEASE_CORE_KEY in rec.pr_body


def test_gather_counts_retired_hooks_fail_open_on_oserror(tmp_path, monkeypatch):
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps({"hooks": {"SessionStart": [LEGACY_RELEASE_CORE_ENTRY]}}),
        encoding="utf-8",
    )
    hook = irec.load_retired_hooks()[0]
    assert irec.retired_hook_count(tmp_path, hook) == 1

    real_read = Path.read_text

    def boom(self, *a, **kw):
        if self.name == "settings.json":
            raise OSError("permission denied")
        return real_read(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", boom)
    assert irec.retired_hook_count(tmp_path, hook) == 0


def test_apply_fails_open_when_the_retired_hook_rewrite_cannot_be_written(
    tmp_path, rec, monkeypatch
):
    _apply(tmp_path)
    settings = tmp_path / ".claude" / "settings.json"
    data = json.loads(settings.read_text(encoding="utf-8"))
    data["hooks"]["SessionStart"].insert(0, LEGACY_RELEASE_CORE_ENTRY)
    settings.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    plan = _plan(tmp_path)
    assert not plan.writes and plan.retire_hook_deletes

    real_write = Path.write_text

    def boom(self, *a, **kw):
        if self.name == "settings.json":
            raise OSError("permission denied")
        return real_write(self, *a, **kw)

    monkeypatch.setattr(Path, "write_text", boom)

    _apply(tmp_path)

    assert "install-release-core" in settings.read_text(encoding="utf-8")


def test_pr_body_lists_a_kept_retired_file(tmp_path, rec):
    victim = tmp_path / RETIRED_WORKFLOW_PATH
    victim.parent.mkdir(parents=True)
    victim.write_text(PRISTINE_WORKFLOW.read_text() + "# local tweak\n")

    plan = _plan(tmp_path)
    assert RETIRED_WORKFLOW_PATH not in plan.changed_paths
    _apply(tmp_path, iapply.MODE_PR)
    assert victim.is_file()
    added = next(paths for name, paths in rec.calls if name == "add")
    assert RETIRED_WORKFLOW_PATH not in added
    assert "### Retired files kept — locally modified" in rec.pr_body
    assert RETIRED_WORKFLOW_PATH in rec.pr_body


def test_format_plan_reports_the_decided_actions(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    plan = _plan(tmp_path)
    report = verb.format_plan(plan)
    assert report.startswith(f"install: {tmp_path.resolve()}")
    assert "add      AGENTS.md" in report
    assert "seed     [reviewers]" in report
    assert "(dry-run)" not in report
    dry = verb.format_plan(plan, dry_run=True)
    assert "(dry-run)" in dry
    assert "— dry-run, nothing written" in dry
    assert f"{len(plan.writes)} to write" in dry


def test_format_plan_omits_noop_units(tmp_path, rec):
    _apply(tmp_path)
    plan = _plan(tmp_path)
    report = verb.format_plan(plan)
    assert "add      " not in report
    assert "nothing to do — managed set is current." in report


def test_format_result_renders_the_mode_outcomes():
    plan = irec.Plan(root="/x", decisions=(), retired=(), seeds=())
    tree = iapply.InstallResult(plan=plan, mode=iapply.MODE_TREE)
    assert "refreshed the managed set in the working tree" in verb.format_result(tree)
    local = iapply.InstallResult(plan=plan, mode=iapply.MODE_LOCAL, branch="main")
    assert "committed to main (local-only --local)" in verb.format_result(local)
    push = iapply.InstallResult(plan=plan, mode=iapply.MODE_PUSH, branch="main")
    assert "pushed to main (break-glass --push)" in verb.format_result(push)
    opened = iapply.InstallResult(
        plan=plan, mode=iapply.MODE_PR, branch="shipit/install", pr_url="https://x/1"
    )
    assert "opened draft PR: https://x/1" in verb.format_result(opened)
    updated = iapply.InstallResult(
        plan=plan,
        mode=iapply.MODE_PR,
        branch="shipit/install",
        pr_url="https://x/1",
        pr_updated=True,
    )
    assert "updated draft PR: https://x/1" in verb.format_result(updated)
    noop_pr = iapply.InstallResult(plan=plan, mode=iapply.MODE_PR)
    assert "already current on the default branch" in verb.format_result(noop_pr)
    assert "nothing to publish" in verb.format_result(noop_pr)
    live = iapply.InstallResult(plan=plan, mode=iapply.MODE_TREE, hooks_activated=True)
    assert verb.format_result(live).splitlines()[0] == (
        "  activated git hooks (lefthook install) — the checks are live"
    )


def test_format_pr_body_sections_render_from_the_plan(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    plan = _plan(tmp_path)
    body = verb.format_pr_body(plan, {}, True)
    assert body.startswith("`shipit install` reconciled the managed set.")
    assert "### Added" in body
    assert "### Policy seeded" in body
    assert "### Checks activated locally" in body
    deferred = verb.format_pr_body(plan, {}, False)
    assert "### Checks configured — local activation skipped" in deferred
    silent = verb.format_pr_body(plan, {}, None)
    assert "Checks activated" not in silent and "activation skipped" not in silent


def test_cmd_dry_run_wires_argv_to_the_report(tmp_path):
    from click.testing import CliRunner

    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    result = CliRunner().invoke(verb.cmd, [str(tmp_path), "--dry-run"])
    assert result.exit_code == 0
    assert "(dry-run)" in result.output
    assert "— dry-run, nothing written" in result.output


def test_cmd_rejects_a_missing_path_at_parse(tmp_path):
    from click.testing import CliRunner

    result = CliRunner().invoke(verb.cmd, [str(tmp_path / "nope")])
    assert result.exit_code == 2
    assert "does not exist" in result.output


def test_cmd_rejects_a_file_path_at_parse(tmp_path):
    from click.testing import CliRunner

    victim = tmp_path / "a-file"
    victim.write_text("x\n")
    result = CliRunner().invoke(verb.cmd, [str(victim)])
    assert result.exit_code == 2


def test_cmd_mode_flags_are_mutually_exclusive():
    from click.testing import CliRunner

    for pair in (["--local", "--push"], ["--pr", "--local"], ["--pr", "--push"]):
        result = CliRunner().invoke(verb.cmd, [*pair, "."])
        assert result.exit_code == 2
        assert "mutually exclusive" in result.output


def test_format_plan_lines_carry_block_identity(tmp_path, rec):
    plan = _plan(tmp_path)
    report = verb.format_plan(plan)
    for key in (
        iunits.PIXI_KEY,
        iunits.PIXI_LINT_DEPS_KEY,
        iunits.PIXI_ENVS_KEY,
        iunits.SETTINGS_KEY,
        iunits.SETTINGS_STOP_KEY,
    ):
        assert f"add      {key}" in report
    assert not any(
        line.strip() == "add      pixi.toml".strip()
        or line.rstrip().endswith(" pixi.toml")
        for line in report.splitlines()
        if line.strip().startswith("add")
    )


def test_pr_body_lists_units_by_key(tmp_path, rec):
    _apply(tmp_path, iapply.MODE_PR)
    assert f"- `{iunits.PIXI_LINT_DEPS_KEY}`" in rec.pr_body
    assert f"- `{iunits.SETTINGS_SESSIONSTART_KEY}`" in rec.pr_body


def test_format_result_renders_the_pin_stamp_line(tmp_path, rec):
    result = _apply(tmp_path, iapply.MODE_LOCAL)
    assert result.stamped_version == "testhash"
    assert "  pinned to testhash" in verb.format_result(result)


def test_pr_body_carries_the_pin_stamp_line(tmp_path, rec):
    _apply(tmp_path, iapply.MODE_PR)
    assert "Pinned to `testhash`" in rec.pr_body


def test_override_summary_uses_the_unit_key(tmp_path, rec):
    _apply(tmp_path)
    pixi = tmp_path / "pixi.toml"
    pixi.write_text(
        pixi.read_text().replace('logs = "./bin/shipit logs"', 'logs = "true"')
    )
    _apply(tmp_path, iapply.MODE_PR)
    assert f"<code>{iunits.PIXI_KEY}</code>" in rec.pr_body


@pytest.fixture
def stock_consumer(tmp_path):
    root = tmp_path / "stock"
    root.mkdir()
    return root


def test_fresh_install_on_a_truly_stock_consumer(stock_consumer, rec):
    result = _apply(stock_consumer, iapply.MODE_PR)

    assert all(d.action == irec.ADD for d in result.plan.decisions)

    agents = (stock_consumer / "AGENTS.md").read_text()
    assert iunits.BLOCK_OPEN in agents
    manifest = tomllib.loads((stock_consumer / "pixi.toml").read_text())
    assert manifest["workspace"]["name"] == "stock"
    assert "lint" in manifest["feature"]["lint"]["tasks"]
    settings = json.loads((stock_consumer / ".claude" / "settings.json").read_text())
    assert set(settings["hooks"]) == {
        "PreToolUse",
        "Stop",
        "SubagentStop",
        "SessionStart",
        "WorktreeCreate",
    }
    assert (stock_consumer / "lefthook.yml").is_file()
    assert (stock_consumer / "bin" / "shipit").is_file()
    assert (stock_consumer / ".markdownlint.yaml").is_file()

    cfg = config.load(stock_consumer / config.CONFIG_NAME)
    assert config.shipit_version(cfg) == "testhash"
    assert "reviewers" in cfg
    assert ("pr_create", True) in rec.calls


def test_stock_consumer_reinstall_reconciles_to_noop(stock_consumer, rec):
    _apply(stock_consumer)
    again = _plan(stock_consumer)
    assert again.nothing_to_do


def _events(caplog):
    from shipit import events as ev

    return [getattr(r, ev.EXTRA_KEY, None) for r in caplog.records]


def test_install_run_emits_started_and_completed(tmp_path, rec, caplog):
    import logging as _logging

    with caplog.at_level(_logging.INFO, logger="shipit.install"):
        rc = verb.run(str(tmp_path), dry_run=True)
    assert rc == 0
    names = _events(caplog)
    assert "install.started" in names
    assert "install.completed" in names
    assert "install.failed" not in names


def test_failed_install_emits_the_failed_event_with_the_step(
    tmp_path, rec, monkeypatch, caplog
):
    import logging as _logging

    def boom(*a, **k):
        raise ExecError(["git", "push"], rc=1, stderr="denied")

    monkeypatch.setattr(git, "push", boom)
    with caplog.at_level(_logging.INFO, logger="shipit.install"):
        rc = verb.run(str(tmp_path), pr=True)
    assert rc == 1
    from shipit import events as ev

    failed = [
        r for r in caplog.records if getattr(r, ev.EXTRA_KEY, None) == "install.failed"
    ]
    assert len(failed) == 1
    assert failed[0].step == "apply"


def test_selfcert_failure_event_names_the_selfcert_step(
    tmp_path, rec, monkeypatch, caplog
):
    import logging as _logging

    from shipit.install import selfcert as sc

    def failing_cert(plan, root, **kw):
        return sc.CertReport(checks=(sc.CertCheck(name="planted", ok=False),))

    monkeypatch.setattr(sc, "certify", failing_cert)
    with caplog.at_level(_logging.INFO, logger="shipit.install"):
        rc = verb.run(str(tmp_path), pr=True)
    assert rc == 1
    from shipit import events as ev

    failed = [
        r for r in caplog.records if getattr(r, ev.EXTRA_KEY, None) == "install.failed"
    ]
    assert len(failed) == 1
    assert failed[0].step == "self-certification"
    assert rec.names() == []


def test_install_seeds_toolchains_from_the_root_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(iapply, "_activate_hooks", lambda root: _exec_result(0))
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "acme"\n')
    plan = _plan(tmp_path)
    assert "[toolchains]" in plan.seeds
    _apply(tmp_path)
    entries = config.load_toolchains(config.load(tmp_path / ".shipit.toml"))
    assert [(e.path, e.toolchain) for e in entries] == [(".", "python")]
    assert _plan(tmp_path).nothing_to_do


def test_install_never_clobbers_a_consumer_toolchains_map(tmp_path, monkeypatch):
    monkeypatch.setattr(iapply, "_activate_hooks", lambda root: _exec_result(0))
    (tmp_path / "Cargo.toml").write_text("[package]\n")
    (tmp_path / ".shipit.toml").write_text('[toolchains]\n"." = "go"\n')
    plan = _plan(tmp_path)
    assert "[toolchains]" not in plan.seeds
    _apply(tmp_path)
    entries = config.load_toolchains(config.load(tmp_path / ".shipit.toml"))
    assert [(e.path, e.toolchain) for e in entries] == [(".", "go")]


def test_install_seeds_no_toolchains_without_a_manifest_signal(tmp_path):
    plan = _plan(tmp_path)
    assert "[toolchains]" not in plan.seeds


def _changelog_consumer(root: Path) -> None:
    (root / "CHANGELOG").mkdir()
    (root / "CHANGELOG" / "unreleased-first.md").write_text("- Added the thing\n")


def test_stale_changelog_projection_is_reconcile_work(tmp_path, monkeypatch):
    from shipit import changelog as chlog
    from shipit.verbs.changelog import render_current

    monkeypatch.setattr(iapply, "_activate_hooks", lambda root: _exec_result(0))
    _changelog_consumer(tmp_path)
    (tmp_path / "CHANGELOG.md").write_text("# an old renderer's output\n")
    plan = _plan(tmp_path)
    assert plan.rerender_changelog
    assert not plan.nothing_to_do
    assert chlog.CHANGELOG_FILE in plan.changed_paths
    assert "render" in verb.format_plan(plan)

    _apply(tmp_path)
    committed = (tmp_path / chlog.CHANGELOG_FILE).read_text()
    assert committed.startswith(chlog.RENDER_PREAMBLE)
    assert chlog.sync_diff(render_current(tmp_path), committed) is None
    replan = _plan(tmp_path)
    assert not replan.rerender_changelog
    assert replan.nothing_to_do


def test_missing_projection_with_fragments_is_also_stale(tmp_path):
    _changelog_consumer(tmp_path)
    assert _plan(tmp_path).rerender_changelog


def test_matching_changelog_projection_is_not_work(tmp_path):
    from shipit.verbs.changelog import render_current

    _changelog_consumer(tmp_path)
    (tmp_path / "CHANGELOG.md").write_text(render_current(tmp_path))
    assert not _plan(tmp_path).rerender_changelog


def test_unreadable_changelog_projection_fails_open_not_stale(tmp_path, caplog):
    _changelog_consumer(tmp_path)
    (tmp_path / "CHANGELOG.md").write_bytes(b"\xff\xfe not valid utf-8\n")
    with caplog.at_level(logging.WARNING):
        assert irec._changelog_stale(tmp_path) is False
        assert not _plan(tmp_path).rerender_changelog
    assert any("unreadable CHANGELOG projection" in r.message for r in caplog.records)


def test_repo_without_the_fragment_convention_never_rerenders(tmp_path):
    plan = _plan(tmp_path)
    assert not plan.rerender_changelog
    assert "CHANGELOG.md" not in plan.changed_paths


def test_unrenderable_changelog_dir_plans_no_render(tmp_path):
    _changelog_consumer(tmp_path)
    (tmp_path / "CHANGELOG" / "not-semver.md").write_text("bad\n")
    (tmp_path / "CHANGELOG.md").write_text("stale\n")
    assert not _plan(tmp_path).rerender_changelog


def test_pr_body_carries_the_changelog_rerender_section(tmp_path, rec):
    _changelog_consumer(tmp_path)
    (tmp_path / "CHANGELOG.md").write_text("# an old renderer's output\n")
    result = _apply(tmp_path, iapply.MODE_PR)
    assert "Changelog re-rendered" in rec.pr_body
    assert "CHANGELOG.md" in result.plan.changed_paths


def test_rerender_skipped_in_the_window_drops_the_phantom_changelog_path(tmp_path, rec):
    from shipit import changelog as chlog

    _changelog_consumer(tmp_path)
    plan = _plan(tmp_path)
    assert plan.rerender_changelog
    assert chlog.CHANGELOG_FILE in plan.changed_paths

    (tmp_path / "CHANGELOG" / "unreleased-first.md").unlink()
    (tmp_path / "CHANGELOG").rmdir()

    iapply.apply(plan, iapply.MODE_LOCAL)

    assert not (tmp_path / chlog.CHANGELOG_FILE).exists()
    assert chlog.CHANGELOG_FILE not in rec.commit_paths
    add_paths = next(paths for name, paths in rec.calls if name == "add")
    assert chlog.CHANGELOG_FILE not in add_paths


def test_rerender_skipped_in_the_window_omits_the_pr_body_section(tmp_path, rec):
    from shipit import changelog as chlog

    _changelog_consumer(tmp_path)
    plan = _plan(tmp_path)
    assert plan.rerender_changelog

    (tmp_path / "CHANGELOG" / "unreleased-first.md").unlink()
    (tmp_path / "CHANGELOG").rmdir()

    iapply.apply(
        plan,
        iapply.MODE_PR,
        pr_body=lambda before, hooks, rerendered, pin, debt: verb.format_pr_body(
            plan,
            before,
            hooks,
            rerendered=rerendered,
            stamped_version=pin,
            lint_debt=debt,
        ),
    )

    assert "Changelog re-rendered" not in rec.pr_body
    add_paths = next(paths for name, paths in rec.calls if name == "add")
    assert chlog.CHANGELOG_FILE not in add_paths


def _session_store_home(monkeypatch, tmp_path):
    from shipit import sessionstore

    home = tmp_path / "fake-home"
    monkeypatch.setattr(sessionstore, "_default_home", lambda: home)
    return home


def test_install_links_the_canonical_checkout_to_the_store(tmp_path, rec, monkeypatch):
    from shipit import identity, sessionstore
    from shipit.identity import Owner, Repo

    home = _session_store_home(monkeypatch, tmp_path)
    monkeypatch.setattr(
        identity,
        "resolve_repo",
        lambda *a, **k: Repo(owner=Owner(login="acme"), name="widget"),
    )

    assert verb.run(str(tmp_path), local=True) == 0

    link = sessionstore.link_path(tmp_path, home=home)
    assert link.is_symlink()
    assert os.readlink(link) == str(home / ".claude" / "stores" / "acme" / "widget")


def test_a_nothing_to_do_install_still_plants_the_store(tmp_path, rec, monkeypatch):
    from shipit import identity, sessionstore
    from shipit.identity import Owner, Repo

    home = _session_store_home(monkeypatch, tmp_path)
    monkeypatch.setattr(
        identity,
        "resolve_repo",
        lambda *a, **k: Repo(owner=Owner(login="acme"), name="widget"),
    )
    assert verb.run(str(tmp_path), local=True) == 0
    link = sessionstore.link_path(tmp_path, home=home)
    link.unlink()

    assert _plan(tmp_path).nothing_to_do, "test needs the no-op plan shape"
    assert verb.run(str(tmp_path), local=True) == 0

    assert link.is_symlink()
    assert os.readlink(link) == str(home / ".claude" / "stores" / "acme" / "widget")


def test_dry_run_plants_no_session_store(tmp_path, rec, monkeypatch):

    home = _session_store_home(monkeypatch, tmp_path)

    assert verb.run(str(tmp_path), dry_run=True) == 0

    assert not (home / ".claude").exists()


def test_install_survives_an_unplantable_session_store(
    tmp_path, rec, monkeypatch, caplog
):
    import logging as _logging

    from shipit import identity, sessionstore
    from shipit.identity import Owner, Repo

    _session_store_home(monkeypatch, tmp_path)
    monkeypatch.setattr(
        identity,
        "resolve_repo",
        lambda *a, **k: Repo(owner=Owner(login="acme"), name="widget"),
    )

    def boom(*a, **k):
        raise OSError("read-only file system")

    monkeypatch.setattr(sessionstore, "plant", boom)

    with caplog.at_level(_logging.DEBUG, logger="shipit.install"):
        rc = verb.run(str(tmp_path), local=True)

    assert rc == 0
    assert not _session_store_warnings(caplog)
    assert any("session store not planted" in r.message for r in caplog.records)


def test_install_survives_an_unresolvable_repo(tmp_path, rec, caplog):
    import logging as _logging

    with caplog.at_level(_logging.DEBUG, logger="shipit.install"):
        rc = verb.run(str(tmp_path), local=True)

    assert rc == 0
    assert not _session_store_warnings(caplog)


def _session_store_warnings(caplog):
    import logging as _logging

    return [
        r
        for r in caplog.records
        if r.levelno >= _logging.WARNING
        and r.name in ("shipit.install", "shipit.sessionstore")
    ]
