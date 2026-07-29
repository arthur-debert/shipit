from __future__ import annotations

import ast
import importlib
import inspect
import logging
import pathlib
import re

import shipit

_SRC_ROOT = pathlib.Path(shipit.__file__).parent

_LOG_METHODS = frozenset({"debug", "info", "warning", "error", "critical"})


def test_sprayed_modules_have_a_shipit_logger():
    for modname, expected in [
        ("shipit.execrun", "shipit.exec"),
        ("shipit.prstate.state", "shipit.prstate"),
        ("shipit.review.service", "shipit.review"),
        ("shipit.review.post", "shipit.review"),
        ("shipit.install.reconcile", "shipit.install"),
        ("shipit.install.apply", "shipit.install"),
        ("shipit.ghsetup", "shipit.ghsetup"),
        ("shipit.lint", "shipit.lint"),
        ("shipit.tree.create", "shipit.tree"),
        ("shipit.tree.cleanup", "shipit.tree"),
        ("shipit.tree.registry", "shipit.tree"),
        ("shipit.tree.readonly", "shipit.tree"),
        ("shipit.verbs.tree", "shipit.tree"),
        ("shipit.spawn.launch", "shipit.spawn"),
        ("shipit.spawn.dogfood", "shipit.spawn"),
        ("shipit.spawn.subagent", "shipit.spawn"),
        ("shipit.prstate.fetch", "shipit.prstate"),
        ("shipit.prstate.reviewers", "shipit.prstate"),
        ("shipit.gh", "shipit.gh"),
        ("shipit.review.checkrun", "shipit.review"),
        ("shipit.review.producer", "shipit.review"),
        ("shipit.prstate.request", "shipit.prstate"),
        ("shipit.prstate.flip", "shipit.prstate"),
        ("shipit.prstate.dispatch", "shipit.prstate"),
        ("shipit.checks", "shipit.checks"),
        ("shipit.sessionstore", "shipit.sessionstore"),
    ]:
        mod = importlib.import_module(modname)
        assert isinstance(mod.logger, logging.Logger)
        assert mod.logger.name == expected


def test_verbs_keep_print_for_user_facing_output():
    from shipit import lint
    from shipit.verbs import _render

    for mod in (lint, _render):
        src = inspect.getsource(mod)
        assert "print(" in src, (
            f"{mod.__name__} should still use print() for CLI output"
        )


def _log_calls():
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in _LOG_METHODS
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "logger"
            ):
                yield path.relative_to(_SRC_ROOT.parent), node


def _format_strings():
    for path, node in _log_calls():
        if (
            node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            yield f"{path}:{node.lineno}", node.args[0].value


_CODE_IDENTIFIER_PREFIX = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*:")


def test_event_names_carry_no_code_identifier_prefix():
    offenders = [
        f"{where}: {fmt[:70]!r}"
        for where, fmt in _format_strings()
        if (m := _CODE_IDENTIFIER_PREFIX.match(fmt))
        and ("_" in m.group(0) or "." in m.group(0))
    ]
    assert not offenders, (
        "log messages must not lead with a code-identifier prefix:\n"
        + "\n".join(offenders)
    )


def test_pr_identifier_renders_as_pr_hash_number():
    offenders = [
        f"{where}: {fmt[:70]!r}"
        for where, fmt in _format_strings()
        if "pr=#" in fmt or re.search(r"%[sd]#%", fmt)
    ]
    assert not offenders, "log messages must render a PR as pr#N:\n" + "\n".join(
        offenders
    )


def test_exceptions_attach_via_exc_info_true_and_are_not_reinterpolated():
    offenders = []
    for path, node in _log_calls():
        where = f"{path}:{node.lineno}"
        for kw in node.keywords:
            if kw.arg == "exc_info" and isinstance(kw.value, ast.Name):
                offenders.append(f"{where}: exc_info={kw.value.id} (pass True)")
        for arg in node.args[1:]:
            if isinstance(arg, ast.Name) and (
                arg.id == "exc" or arg.id.endswith("_exc") or arg.id == "excinfo"
            ):
                offenders.append(f"{where}: interpolates {arg.id} into the message")
    assert not offenders, (
        "exceptions ride records via exc_info=True only:\n" + "\n".join(offenders)
    )
