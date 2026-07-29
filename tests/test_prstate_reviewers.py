from __future__ import annotations

import pytest

from shipit.agent import backend as _agent_backend
from shipit.identity import Sha, repo_from_slug
from shipit.pr import PrId
from shipit.prstate.errors import PrStateError
from shipit.prstate.model import ReviewLifecycle
from shipit.prstate.reviewers import (
    REGISTRY,
    AgyAdapter,
    CodeRabbitAdapter,
    CodexAdapter,
    CopilotAdapter,
    GeminiAdapter,
    required_adapters,
)
from shipit.prstate.reviewers_config import default_roster
from shipit.prstate.roster import Roster, RosterEntry

_TARGET_REPO = repo_from_slug("owner/repo")


def _target(number: int) -> PrId:
    return PrId(repo=_TARGET_REPO, number=number)


NEW = Sha("beef" * 10)
OLD = Sha("dead" * 10)
HEAD = Sha("abcd" * 10)

COPILOT = CopilotAdapter()
CODERABBIT = CodeRabbitAdapter()
GEMINI = GeminiAdapter()
CODEX = CodexAdapter()
AGY = AgyAdapter()


def _rerun_roster(name: str) -> Roster:
    return Roster((RosterEntry(name=name, required=True, rerun=True),))


def _review_once_roster(name: str) -> Roster:
    return Roster((RosterEntry(name=name, required=True, rerun=False),))


def test_registry_catalogs_all_adapters():
    assert [r.name for r in REGISTRY] == [
        "copilot",
        "coderabbit",
        "gemini",
        "codex",
        "agy",
    ]
    assert COPILOT.requestable is True
    assert CODERABBIT.requestable is True
    assert GEMINI.requestable is False
    assert CODEX.requestable is True
    assert AGY.requestable is True


def test_default_required_set_is_copilot_only():
    assert [r.name for r in required_adapters(default_roster())] == ["copilot"]


def test_copilot_done_with_open_comment(context):
    ctx = context("copilot_changes_requested")
    assert COPILOT.detect(ctx) == ReviewLifecycle.DONE_COMMENTS
    assert GEMINI.detect(ctx) == ReviewLifecycle.NOT_REQUESTED
    assert len(COPILOT.open_threads(ctx)) == 1


def test_both_done_clean(context):
    ctx = context("copilot_clean_gemini_clean")
    assert COPILOT.detect(ctx) == ReviewLifecycle.DONE_CLEAN
    assert GEMINI.detect(ctx) == ReviewLifecycle.DONE_CLEAN
    assert ctx.open_threads() == []


def test_gemini_eyes_is_in_progress_copilot_requested(context):
    ctx = context("gemini_eyes_copilot_requested")
    assert GEMINI.detect(ctx) == ReviewLifecycle.IN_PROGRESS
    assert COPILOT.detect(ctx) == ReviewLifecycle.REQUESTED


def test_stale_copilot_review_counts_as_done_when_review_once(context):
    ctx = context("copilot_stale_review")
    assert COPILOT.detect(ctx) in (
        ReviewLifecycle.DONE_CLEAN,
        ReviewLifecycle.DONE_COMMENTS,
    )


def test_stale_copilot_review_does_not_count_as_done_when_rerun(context):
    ctx = context("copilot_stale_review")
    ctx.roster = _rerun_roster("copilot")
    assert COPILOT.detect(ctx) == ReviewLifecycle.REQUESTED


def test_gemini_review_on_earlier_head_still_counts_as_done():
    from shipit.prstate.model import Review, readiness_view

    ctx = readiness_view(
        number=1,
        head_sha=NEW,
        is_draft=True,
        reviews=[Review(1, "gemini-code-assist[bot]", "COMMENTED", OLD, "")],
        reactions=[{"content": "eyes", "user": {"login": "gemini-code-assist[bot]"}}],
    )
    assert GEMINI.detect(ctx) == ReviewLifecycle.DONE_CLEAN


def test_copilot_review_on_earlier_head_counts_done_review_once():
    from shipit.prstate.model import Review, readiness_view

    ctx = readiness_view(
        number=1,
        head_sha=NEW,
        is_draft=True,
        reviews=[Review(1, "Copilot", "COMMENTED", OLD, "")],
        requested_logins=["Copilot"],
        roster=_review_once_roster("copilot"),
    )
    assert COPILOT.detect(ctx) in (
        ReviewLifecycle.DONE_CLEAN,
        ReviewLifecycle.DONE_COMMENTS,
    )


def test_copilot_review_on_earlier_head_does_NOT_count_done_when_rerun():
    from shipit.prstate.model import Review, readiness_view

    ctx = readiness_view(
        number=1,
        head_sha=NEW,
        is_draft=True,
        reviews=[Review(1, "Copilot", "COMMENTED", OLD, "")],
        requested_logins=["Copilot"],
        roster=_rerun_roster("copilot"),
    )
    assert COPILOT.detect(ctx) == ReviewLifecycle.REQUESTED


def test_copilot_never_reviewed_is_requested_or_not_requested():
    from shipit.prstate.model import readiness_view

    requested = readiness_view(
        number=1, head_sha=HEAD, is_draft=True, requested_logins=["Copilot"]
    )
    assert COPILOT.detect(requested) == ReviewLifecycle.REQUESTED
    bare = readiness_view(number=1, head_sha=HEAD, is_draft=True)
    assert COPILOT.detect(bare) == ReviewLifecycle.NOT_REQUESTED


def test_dismissed_copilot_review_on_head_does_NOT_count_done():
    from shipit.prstate.model import Review, readiness_view

    ctx = readiness_view(
        number=1,
        head_sha=NEW,
        is_draft=True,
        reviews=[Review(1, "Copilot", "DISMISSED", NEW, "")],
        requested_logins=["Copilot"],
    )
    assert COPILOT.detect(ctx) == ReviewLifecycle.REQUESTED


def test_dismissed_gemini_review_does_NOT_count_done():
    from shipit.prstate.model import Review, readiness_view

    ctx = readiness_view(
        number=1,
        head_sha=NEW,
        is_draft=True,
        reviews=[Review(1, "gemini-code-assist[bot]", "DISMISSED", OLD, "")],
    )
    assert GEMINI.detect(ctx) == ReviewLifecycle.NOT_REQUESTED


def test_resolved_thread_clears_open_but_keeps_authored(context):
    ctx = context("copilot_done_all_resolved")
    assert COPILOT.detect(ctx) == ReviewLifecycle.DONE_COMMENTS
    assert COPILOT.open_threads(ctx) == []
    assert len(COPILOT.authored_threads(ctx)) == 1


def test_by_name_resolves_registry_adapters():
    from shipit.prstate.reviewers import by_name

    assert by_name("copilot") is not None and by_name("copilot").name == "copilot"
    assert by_name("GEMINI") is not None and by_name("GEMINI").name == "gemini"
    assert (
        by_name("coderabbit") is not None and by_name("coderabbit").name == "coderabbit"
    )
    assert by_name("codex") is not None and by_name("codex").name == "codex"
    assert by_name("agy") is not None and by_name("agy").name == "agy"
    assert by_name("nosuchbot") is None


def test_copilot_request_goes_through_gh_pr_edit_graphql(monkeypatch):
    from shipit import gh

    calls: list[tuple] = []
    monkeypatch.setattr(
        gh,
        "pr_edit_reviewer",
        lambda pr, reviewer, remove=False: calls.append((pr, reviewer, remove)),
    )
    assert COPILOT.request(_target(91)) is True
    assert calls == [(_target(91), "@copilot", False)]


def test_copilot_cancel_removes_the_reviewer(monkeypatch):
    from shipit import gh

    calls: list[tuple] = []
    monkeypatch.setattr(
        gh,
        "pr_edit_reviewer",
        lambda pr, reviewer, remove=False: calls.append((pr, reviewer, remove)),
    )
    assert COPILOT.cancel(_target(91)) is True
    assert calls == [(_target(91), "@copilot", True)]


def test_gemini_request_and_cancel_are_noops(monkeypatch):
    from shipit import gh

    def _boom(*a, **k):
        raise AssertionError("gemini must not touch gh")

    monkeypatch.setattr(gh, "pr_edit_reviewer", _boom)
    monkeypatch.setattr(gh, "_run", _boom)
    assert GEMINI.request(_target(91)) is False
    assert GEMINI.cancel(_target(91)) is False


def test_adapters_declare_their_instruction_files():
    assert COPILOT.instruction_files == (".github/copilot-instructions.md",)
    assert CODERABBIT.instruction_files == (".coderabbit.yaml",)
    assert GEMINI.instruction_files == (".gemini/styleguide.md",)
    assert CODEX.instruction_files == (".github/codex-review-instructions.md",)
    assert AGY.instruction_files == (".github/agy-review-instructions.md",)


def test_coderabbit_matches_its_bot_login():
    assert CODERABBIT.matches("coderabbitai[bot]") is True
    assert CODERABBIT.matches("CodeRabbit") is True
    assert CODERABBIT.matches("Copilot") is False


def test_coderabbit_done_on_head_with_open_comment():
    from shipit.prstate.model import Review, ReviewComment, Thread, readiness_view

    thread = Thread(
        thread_id="PRT_cr1",
        is_resolved=False,
        comments=(ReviewComment(1, "a.py", 3, "nit", "coderabbitai[bot]"),),
    )
    ctx = readiness_view(
        number=1,
        head_sha=HEAD,
        is_draft=True,
        reviews=[Review(1, "coderabbitai[bot]", "COMMENTED", HEAD, "")],
        threads=[thread],
    )
    assert CODERABBIT.detect(ctx) == ReviewLifecycle.DONE_COMMENTS
    assert len(CODERABBIT.open_threads(ctx)) == 1


def test_coderabbit_review_once_opt_out_counts_earlier_head():
    from shipit.prstate.model import Review, readiness_view

    ctx = readiness_view(
        number=1,
        head_sha=NEW,
        is_draft=True,
        reviews=[Review(1, "coderabbitai[bot]", "COMMENTED", OLD, "")],
        requested_logins=["coderabbitai[bot]"],
        roster=_review_once_roster("coderabbit"),
    )
    assert CODERABBIT.detect(ctx) in (
        ReviewLifecycle.DONE_CLEAN,
        ReviewLifecycle.DONE_COMMENTS,
    )


def test_coderabbit_is_head_strict_when_rerun():
    from shipit.prstate.model import Review, readiness_view

    ctx = readiness_view(
        number=1,
        head_sha=NEW,
        is_draft=True,
        reviews=[Review(1, "coderabbitai[bot]", "COMMENTED", OLD, "")],
        requested_logins=["coderabbitai[bot]"],
        roster=_rerun_roster("coderabbit"),
    )
    assert CODERABBIT.detect(ctx) == ReviewLifecycle.REQUESTED


def test_dismissed_coderabbit_review_does_not_count_done():
    from shipit.prstate.model import Review, readiness_view

    ctx = readiness_view(
        number=1,
        head_sha=HEAD,
        is_draft=True,
        reviews=[Review(1, "coderabbitai[bot]", "DISMISSED", HEAD, "")],
        requested_logins=["coderabbitai[bot]"],
    )
    assert CODERABBIT.detect(ctx) == ReviewLifecycle.REQUESTED


def test_coderabbit_request_and_cancel_go_through_gh_pr_edit(monkeypatch):
    from shipit import gh

    calls: list[tuple] = []
    monkeypatch.setattr(
        gh,
        "pr_edit_reviewer",
        lambda pr, reviewer, remove=False: calls.append((pr, reviewer, remove)),
    )
    assert CODERABBIT.request(_target(55)) is True
    assert CODERABBIT.cancel(_target(55)) is True
    assert calls == [
        (_target(55), "coderabbitai[bot]", False),
        (_target(55), "coderabbitai[bot]", True),
    ]


def test_codex_and_agy_match_their_bot_logins():
    assert CODEX.matches("adr-codex-review[bot]") is True
    assert CODEX.matches("adr-agy-review[bot]") is False
    assert AGY.matches("adr-agy-review[bot]") is True
    assert AGY.matches("adr-codex-review[bot]") is False
    assert AGY.matches("gemini-code-assist[bot]") is False
    assert CODEX.matches("copilot[bot]") is False
    assert AGY.matches("copilot[bot]") is False


def test_codex_and_agy_do_not_match_human_logins():
    assert CODEX.matches("codexdev") is False
    assert CODEX.matches("codex-fan") is False
    assert CODEX.matches("codex") is False
    assert AGY.matches("agytron") is False
    assert AGY.matches("agy") is False
    assert CODEX.matches("codexbot[bot]") is False
    assert AGY.matches("agy-helper[bot]") is False


def test_codex_and_agy_require_bot_as_suffix_not_substring():
    assert CODEX.matches("adr-codex-review[bot]-staging") is False
    assert AGY.matches("adr-agy-review[bot]y") is False
    assert CODEX.matches("codex-review[bot]x") is False


def test_codex_detect_done_on_head():
    from shipit.prstate.model import Review, readiness_view

    ctx = readiness_view(
        number=1,
        head_sha=HEAD,
        is_draft=True,
        reviews=[Review(1, "adr-codex-review[bot]", "COMMENTED", HEAD, "")],
    )
    assert CODEX.detect(ctx) in (
        ReviewLifecycle.DONE_CLEAN,
        ReviewLifecycle.DONE_COMMENTS,
    )


def test_codex_detect_not_requested_when_empty():
    from shipit.prstate.model import readiness_view

    ctx = readiness_view(number=1, head_sha=HEAD, is_draft=True)
    assert CODEX.detect(ctx) == ReviewLifecycle.NOT_REQUESTED
    assert AGY.detect(ctx) == ReviewLifecycle.NOT_REQUESTED


def test_codex_detect_stale_review_counts_done_review_once():
    from shipit.prstate.model import Review, readiness_view

    ctx = readiness_view(
        number=1,
        head_sha=NEW,
        is_draft=True,
        reviews=[Review(1, "adr-codex-review[bot]", "COMMENTED", OLD, "")],
        roster=_review_once_roster("codex"),
    )
    assert CODEX.detect(ctx) in (
        ReviewLifecycle.DONE_CLEAN,
        ReviewLifecycle.DONE_COMMENTS,
    )


def test_codex_detect_stale_review_is_not_done_when_rerun():
    from shipit.prstate.model import Review, readiness_view

    ctx = readiness_view(
        number=1,
        head_sha=NEW,
        is_draft=True,
        reviews=[Review(1, "adr-codex-review[bot]", "COMMENTED", OLD, "")],
        roster=_rerun_roster("codex"),
    )
    assert CODEX.detect(ctx) == ReviewLifecycle.NOT_REQUESTED


def test_dismissed_codex_review_does_not_count_done():
    from shipit.prstate.model import Review, readiness_view

    ctx = readiness_view(
        number=1,
        head_sha=HEAD,
        is_draft=True,
        reviews=[Review(1, "adr-codex-review[bot]", "DISMISSED", HEAD, "")],
    )
    assert CODEX.detect(ctx) == ReviewLifecycle.NOT_REQUESTED


def test_local_request_detaches_via_service(monkeypatch, tmp_path):
    from shipit.review import service

    calls: list[tuple] = []

    def fake_start_detached(backend, pr, **kwargs):
        calls.append((backend, pr, kwargs))
        return True

    monkeypatch.setattr(service, "start_detached_review", fake_start_detached)
    monkeypatch.chdir(tmp_path)

    assert CODEX.request(_target(7)) is True
    assert AGY.request(_target(9)) is True
    assert calls[0][0] is _agent_backend.CODEX and calls[0][1] == _target(7)
    assert calls[0][2]["as_app"] is True
    assert calls[1][0] is _agent_backend.ANTIGRAVITY and calls[1][1] == _target(9)


def test_local_request_threads_model_and_instructions_from_the_entry(
    monkeypatch, tmp_path
):
    from shipit.prstate.reviewers_config import load_roster
    from shipit.review import service

    (tmp_path / ".shipit.toml").write_text(
        "[reviewers]\n"
        "copilot = {}\n"
        'codex = { model = "flash", instructions = "docs/rev.md" }\n',
        encoding="utf-8",
    )

    captured: dict = {}

    def fake_start_detached(agent, pr, **kwargs):
        captured.update(kwargs)
        return True

    monkeypatch.setattr(service, "start_detached_review", fake_start_detached)
    entry = load_roster(str(tmp_path)).entry("codex")
    assert CODEX.request(_target(3), entry) is True
    assert captured["model"] == "flash"
    assert captured["instructions_path"] == str(tmp_path / "docs" / "rev.md")


def test_local_request_normalizes_failure_to_prstateerror(monkeypatch, tmp_path):
    from shipit.review import service

    def boom(agent, pr, **kwargs):
        raise RuntimeError("backend CLI exploded")

    monkeypatch.setattr(service, "start_detached_review", boom)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(PrStateError, match="codex-local review failed") as excinfo:
        CODEX.request(_target(7))
    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert "backend CLI exploded" in str(excinfo.value.__cause__)


def test_local_request_surfaces_reviewauth_hint_without_traceback_spray(
    monkeypatch, tmp_path, caplog
):
    import logging

    from shipit.review import service
    from shipit.review.ghauth import UNCONFIGURED, ReviewAuthError

    hint = (
        "Could not source the private key for the 'codex' review app from Doppler "
        "(key 'CODEX_REVIEW_APP_PRIVATE_KEY'): doppler: command not found"
    )

    def boom(agent, pr, **kwargs):
        raise ReviewAuthError(hint, kind=UNCONFIGURED)

    monkeypatch.setattr(service, "start_detached_review", boom)
    monkeypatch.chdir(tmp_path)

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(PrStateError) as excinfo:
            CODEX.request(_target(7))

    msg = str(excinfo.value)
    assert "Doppler" in msg
    assert "CODEX_REVIEW_APP_PRIVATE_KEY" in msg
    assert excinfo.value.__cause__ is None
    assert not [
        r
        for r in caplog.records
        if r.levelno >= logging.ERROR and r.exc_info is not None
    ]


def test_local_cancel_is_a_noop():
    assert CODEX.cancel(_target(7)) is False
    assert AGY.cancel(_target(9)) is False


def test_local_request_threads_dimensions_and_table_policy(monkeypatch, tmp_path):
    from shipit.prstate.reviewers_config import load_roster
    from shipit.review import service

    (tmp_path / ".shipit.toml").write_text(
        "[reviewers]\n"
        "nit_cap = 2\n"
        'calibrator = { backend = "claude", reasoning = "medium" }\n'
        "copilot = {}\n"
        'codex = { dimensions = ["correctness", "security-robustness"] }\n',
        encoding="utf-8",
    )

    captured: dict = {}

    def fake_start_detached(agent, pr, **kwargs):
        captured.update(kwargs)
        return True

    monkeypatch.setattr(service, "start_detached_review", fake_start_detached)
    roster = load_roster(str(tmp_path))
    assert CODEX.request(_target(3), roster.entry("codex"), policy=roster.policy)
    assert captured["dimensions"] == ("correctness", "security-robustness")
    assert captured["nit_cap"] == 2
    assert captured["calibrator"].backend == "claude"
    assert captured["calibrator"].reasoning == "medium"


def test_local_request_omits_unset_fanout_config(monkeypatch, tmp_path):
    from shipit.prstate.roster import ReviewPolicy
    from shipit.review import service

    captured: dict = {}

    def fake_start_detached(agent, pr, **kwargs):
        captured.update(kwargs)
        return True

    monkeypatch.setattr(service, "start_detached_review", fake_start_detached)
    monkeypatch.chdir(tmp_path)
    assert CODEX.request(_target(3), policy=ReviewPolicy()) is True
    assert "dimensions" not in captured
    assert "calibrator" not in captured
    assert "nit_cap" not in captured
