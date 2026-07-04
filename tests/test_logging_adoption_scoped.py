"""Every narrating subsystem logs through a ``shipit.*`` package logger.

OBS01-WS03 scoped adoption to the three boundaries (`gh`, `prstate`, `review`)
and pinned the CLI verbs as print-only. LOG02 (the logging spray, ADR-0029)
retired that scope: the verbs' user-facing ``print()`` output is unchanged, but
anything that is the only record of an action now ALSO logs — so the pin flips:
every sprayed module must expose a ``shipit.*`` logger AND keep its prints.

LOG02-WS05 (convergence) widens the guard to every module the spray reached —
verbs.pr.* joined last (#285) — and adds the CONVENTION-level sweeps the
convergence settled (no per-message string assertions, per the epic):

- event names are domain phrases, never code identifiers (no
  ``function_name:`` / ``module.attr:`` prefixes);
- a PR renders as ``pr#N`` in messages (never ``pr=#N`` / ``owner/repo#N``);
- exceptions attach via ``exc_info=True`` — never ``exc_info=<instance>``, and
  never ALSO interpolated into the message text.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import logging
import pathlib
import re

import shipit

_SRC_ROOT = pathlib.Path(shipit.__file__).parent

#: Log methods on the module-level ``logger`` name — the spray's one idiom.
_LOG_METHODS = frozenset({"debug", "info", "warning", "error", "critical"})


def test_sprayed_modules_have_a_shipit_logger():
    # The gh boundary's per-call TRANSPORT record moved to the one Exec runner
    # (PROC01-WS02 / ADR-0028): every subprocess is recorded by `shipit.execrun`
    # on `shipit.exec`. The merged gh adapter (PROC02-WS01) logs only what the
    # runner cannot see — the GraphQL semantic failure and the draft-flip
    # milestone — on its own `shipit.gh` logger.
    for modname, expected in [
        ("shipit.execrun", "shipit.exec"),
        ("shipit.prstate.state", "shipit.prstate"),
        ("shipit.review.service", "shipit.review"),
        ("shipit.review.post", "shipit.review"),
        # LOG02-WS04: the remaining print-only surfaces joined the spray.
        # CLI02-WS01 promoted the install family into its domain package; the
        # records live on the one shipit.install logger (the verb is glue +
        # renderers).
        ("shipit.install.reconcile", "shipit.install"),
        ("shipit.install.apply", "shipit.install"),
        # CLI02-WS04 promoted the gh-setup passes into their domain module;
        # the sprayed records moved with them (the verb is print-free glue).
        ("shipit.ghsetup", "shipit.ghsetup"),
        ("shipit.verbs.lint", "shipit.lint"),
        ("shipit.session.liveness", "shipit.session"),
        # LOG02-WS01..WS03: the tree / spawn / review+prstate sprays.
        ("shipit.tree.create", "shipit.tree"),
        ("shipit.tree.cleanup", "shipit.tree"),
        ("shipit.tree.registry", "shipit.tree"),
        ("shipit.tree.provision", "shipit.tree"),
        ("shipit.tree.readonly", "shipit.tree"),
        ("shipit.verbs.tree", "shipit.tree"),
        ("shipit.spawn.launch", "shipit.spawn"),
        ("shipit.spawn.dogfood", "shipit.spawn"),
        # CLI02-WS02 promoted the spawn pipeline out of the verb: the sprayed
        # lifecycle records live on the domain module's logger (the verb is
        # print-free glue + the SPAWNED renderer).
        ("shipit.spawn.subagent", "shipit.spawn"),
        ("shipit.prstate.fetch", "shipit.prstate"),
        ("shipit.prstate.reviewers", "shipit.prstate"),
        ("shipit.gh", "shipit.gh"),
        ("shipit.review.checkrun", "shipit.review"),
        ("shipit.review.producer", "shipit.review"),
        # LOG02-WS05 (#285) sprayed the pr verbs; CLI01-WS03 promoted the
        # sprayed logic into the engine's services, whose records now live on
        # the one prstate logger (the verbs are print-free glue + renderers).
        ("shipit.prstate.request", "shipit.prstate"),
        ("shipit.prstate.flip", "shipit.prstate"),
        ("shipit.prstate.dispatch", "shipit.prstate"),
        ("shipit.checks", "shipit.checks"),
    ]:
        mod = importlib.import_module(modname)
        assert isinstance(mod.logger, logging.Logger)
        assert mod.logger.name == expected


def test_verbs_keep_print_for_user_facing_output():
    """The spray is ADDITIVE: logging joined the verbs, but the user-facing CLI
    output still writes with ``print()`` — logging never replaced stdout. The
    pr family is exempt since CLI01-WS03, install since CLI02-WS01, and
    gh-setup since CLI02-WS04: those verbs render through the shared ADR-0030
    emit seam (:mod:`shipit.verbs._render`), which owns the print."""
    from shipit.verbs import _render, lint

    for mod in (lint, _render):
        src = inspect.getsource(mod)
        assert "print(" in src, (
            f"{mod.__name__} should still use print() for CLI output"
        )


# ---------------------------------------------------------------------------
# Convention sweeps (LOG02-WS05, #285) — one vocabulary across every sprayed
# subsystem, guarded mechanically over the whole package so a new module can
# never quietly reintroduce a retired form.
# ---------------------------------------------------------------------------


def _log_calls():
    """Yield ``(path, ast.Call)`` for every ``logger.<level>(...)`` in shipit."""
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
    """Yield ``(location, fmt)`` for every literal log format string."""
    for path, node in _log_calls():
        if (
            node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            yield f"{path}:{node.lineno}", node.args[0].value


#: A leading single-token identifier ending in ``:``. Only tokens carrying an
#: underscore or a dot are flagged — those are unambiguously CODE identifiers
#: (``start_detached_review:``, ``checkrun.create:``); a plain domain word
#: before a colon (``decision pr#5:``-style phrasing) is human vocabulary.
_CODE_IDENTIFIER_PREFIX = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*:")


def test_event_names_carry_no_code_identifier_prefix():
    """Canon (glassbox PRD): domain-noun + past-tense/imperative human message
    — "tree created …", "review posted …" — NEVER function/method names."""
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
    """One PR rendering in log messages: ``pr#N`` — never the review spray's
    ``pr=#N`` nor the post spray's ``owner/repo#N`` (``%s#%s``)."""
    offenders = [
        f"{where}: {fmt[:70]!r}"
        for where, fmt in _format_strings()
        if "pr=#" in fmt or re.search(r"%[sd]#%", fmt)
    ]
    assert not offenders, "log messages must render a PR as pr#N:\n" + "\n".join(
        offenders
    )


def test_exceptions_attach_via_exc_info_true_and_are_not_reinterpolated():
    """One exc_info form: ``exc_info=True`` (a boolean guard like
    ``exc is not None`` is the same form) — never the instance itself, whose
    unraised ``__traceback__`` is None — and never the exception ALSO
    interpolated into the message (the tree/review duplication)."""
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
