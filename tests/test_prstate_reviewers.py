"""Adapter detection over recorded PR scenarios.

Each test asserts where the Copilot/Gemini adapters place a reviewer in the
lifecycle, exercising the load-bearing rules: head-SHA filtering, the
resolved-thread filter, and Gemini's weak (reaction/comment) signals.
"""

from __future__ import annotations

import pytest
from shipit.agent import backend as _agent_backend
from shipit.identity import Sha
from shipit.prstate.model import ReviewLifecycle
from shipit.prstate.errors import PrStateError
from shipit.prstate.reviewers import (
    REGISTRY,
    AgyAdapter,
    CodeRabbitAdapter,
    CodexAdapter,
    CopilotAdapter,
    GeminiAdapter,
    required_reviewers,
)

# Full, validated commit identities (COR02): the current head, an earlier
# (stale) head, and a generic head for single-commit scenarios.
NEW = Sha("beef" * 10)
OLD = Sha("dead" * 10)
HEAD = Sha("abcd" * 10)

COPILOT = CopilotAdapter()
CODERABBIT = CodeRabbitAdapter()
GEMINI = GeminiAdapter()
CODEX = CodexAdapter()
AGY = AgyAdapter()


def test_registry_catalogs_all_adapters():
    # The registry is the CATALOG; which entries hold Ready is the config knob. The
    # local backends (codex / agy) join the GitHub-App reviewers under one
    # interface.
    assert [r.name for r in REGISTRY] == [
        "copilot",
        "coderabbit",
        "gemini",
        "codex",
        "agy",
    ]
    # `requestable` marks eligibility to be a required (holding) reviewer (a real request
    # edge + the #614 attach-verification, or — for the local backends — a
    # synchronous run-and-post), NOT the current required set.
    assert COPILOT.requestable is True
    assert CODERABBIT.requestable is True
    assert GEMINI.requestable is False
    assert CODEX.requestable is True
    assert AGY.requestable is True


def test_default_required_set_is_copilot_only():
    # The shipped default config: Copilot holds Ready. CodeRabbit is a phos-org
    # pilot — requestable (eligible), but required only where a repo opts in.
    assert [r.name for r in required_reviewers()] == ["copilot"]


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
    # DEFAULT policy is review-once (rerun=False): a review against an earlier
    # commit still counts as done — the reviewer won't be asked to look again.
    ctx = context("copilot_stale_review")
    assert COPILOT.detect(ctx) in (
        ReviewLifecycle.DONE_CLEAN,
        ReviewLifecycle.DONE_COMMENTS,
    )


def test_stale_copilot_review_does_not_count_as_done_when_rerun(context):
    # rerun=True (opt-in, head-strict): a review against an earlier commit is
    # stale and must not read as done on this head.
    ctx = context("copilot_stale_review")
    ctx.reviewer_rerun = {"copilot": True}
    assert COPILOT.detect(ctx) == ReviewLifecycle.REQUESTED


def test_gemini_review_on_earlier_head_still_counts_as_done():
    # The exact #345-fixup case: Gemini reviewed the OLD head, a fixup made a new
    # head, and the lingering eyes reaction must NOT downgrade Gemini to
    # in_progress — it reviews once and won't re-review the push.
    from shipit.prstate.model import readiness_view, Review

    ctx = readiness_view(
        number=1,
        head_sha=NEW,
        is_draft=True,
        reviews=[Review(1, "gemini-code-assist[bot]", "COMMENTED", OLD, "")],
        reactions=[{"content": "eyes", "user": {"login": "gemini-code-assist[bot]"}}],
    )
    assert GEMINI.detect(ctx) == ReviewLifecycle.DONE_CLEAN


def test_copilot_review_on_earlier_head_counts_done_review_once():
    # DEFAULT (review-once): an earlier-head Copilot review still counts as done.
    from shipit.prstate.model import readiness_view, Review

    ctx = readiness_view(
        number=1,
        head_sha=NEW,
        is_draft=True,
        reviews=[Review(1, "Copilot", "COMMENTED", OLD, "")],
        requested_logins=["Copilot"],
    )
    assert COPILOT.detect(ctx) in (
        ReviewLifecycle.DONE_CLEAN,
        ReviewLifecycle.DONE_COMMENTS,
    )


def test_copilot_review_on_earlier_head_does_NOT_count_done_when_rerun():
    # rerun=True: Copilot is head-strict — a review on an old head is stale.
    from shipit.prstate.model import readiness_view, Review

    ctx = readiness_view(
        number=1,
        head_sha=NEW,
        is_draft=True,
        reviews=[Review(1, "Copilot", "COMMENTED", OLD, "")],
        requested_logins=["Copilot"],
        reviewer_rerun={"copilot": True},
    )
    assert COPILOT.detect(ctx) == ReviewLifecycle.REQUESTED


def test_copilot_never_reviewed_is_requested_or_not_requested():
    # Never reviewed: REQUESTED when currently requested, else NOT_REQUESTED —
    # independent of the rerun flag.
    from shipit.prstate.model import readiness_view

    requested = readiness_view(
        number=1, head_sha=HEAD, is_draft=True, requested_logins=["Copilot"]
    )
    assert COPILOT.detect(requested) == ReviewLifecycle.REQUESTED
    bare = readiness_view(number=1, head_sha=HEAD, is_draft=True)
    assert COPILOT.detect(bare) == ReviewLifecycle.NOT_REQUESTED


def test_dismissed_copilot_review_on_head_does_NOT_count_done():
    # A DISMISSED review (cleared by an admin/author) is retracted — even on the
    # current head it must not read as done; the PR falls back to REQUESTED.
    from shipit.prstate.model import readiness_view, Review

    ctx = readiness_view(
        number=1,
        head_sha=NEW,
        is_draft=True,
        reviews=[Review(1, "Copilot", "DISMISSED", NEW, "")],
        requested_logins=["Copilot"],
    )
    assert COPILOT.detect(ctx) == ReviewLifecycle.REQUESTED


def test_dismissed_gemini_review_does_NOT_count_done():
    # Same for best-effort Gemini: a dismissed review is not a standing verdict.
    from shipit.prstate.model import readiness_view, Review

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


# --- the act side (request / cancel / instruction files; release#555) -------


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
    # The GraphQL `gh pr edit --add-reviewer @copilot` path is load-bearing:
    # the REST requested_reviewers POST silently no-ops for Copilot.
    from shipit import gh

    calls: list[tuple] = []
    monkeypatch.setattr(
        gh,
        "pr_edit_reviewer",
        lambda pr, reviewer, remove=False: calls.append((pr, reviewer, remove)),
    )
    assert COPILOT.request(91) is True
    assert calls == [(91, "@copilot", False)]


def test_copilot_cancel_removes_the_reviewer(monkeypatch):
    from shipit import gh

    calls: list[tuple] = []
    monkeypatch.setattr(
        gh,
        "pr_edit_reviewer",
        lambda pr, reviewer, remove=False: calls.append((pr, reviewer, remove)),
    )
    assert COPILOT.cancel(91) is True
    assert calls == [(91, "@copilot", True)]


def test_gemini_request_and_cancel_are_noops(monkeypatch):
    # Gemini auto-triggers and is best-effort: no request mechanism, no gh call.
    from shipit import gh

    def _boom(*a, **k):  # any gh traffic is a bug
        raise AssertionError("gemini must not touch gh")

    monkeypatch.setattr(gh, "pr_edit_reviewer", _boom)
    monkeypatch.setattr(gh, "_run", _boom)
    assert GEMINI.request(91) is False
    assert GEMINI.cancel(91) is False


def test_adapters_declare_their_instruction_files():
    # Structure only (#555): the adapter declares where its review-instruction
    # file lives; shipping content there is a separate onboarding decision.
    assert COPILOT.instruction_files == (".github/copilot-instructions.md",)
    assert CODERABBIT.instruction_files == (".coderabbit.yaml",)
    assert GEMINI.instruction_files == (".gemini/styleguide.md",)
    assert CODEX.instruction_files == (".github/codex-review-instructions.md",)
    assert AGY.instruction_files == (".github/agy-review-instructions.md",)


# --- CodeRabbit adapter (release#622) ---------------------------------------


def test_coderabbit_matches_its_bot_login():
    assert CODERABBIT.matches("coderabbitai[bot]") is True
    assert CODERABBIT.matches("CodeRabbit") is True
    assert CODERABBIT.matches("Copilot") is False


def test_coderabbit_done_on_head_with_open_comment():
    # Head-strict + leaves a thread → DONE_COMMENTS, with the open thread tracked.
    from shipit.prstate.model import readiness_view, Review, ReviewComment, Thread

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


def test_coderabbit_review_once_by_default_counts_earlier_head():
    # DEFAULT review-once: an earlier-head CodeRabbit review counts as done.
    from shipit.prstate.model import readiness_view, Review

    ctx = readiness_view(
        number=1,
        head_sha=NEW,
        is_draft=True,
        reviews=[Review(1, "coderabbitai[bot]", "COMMENTED", OLD, "")],
        requested_logins=["coderabbitai[bot]"],
    )
    assert CODERABBIT.detect(ctx) in (
        ReviewLifecycle.DONE_CLEAN,
        ReviewLifecycle.DONE_COMMENTS,
    )


def test_coderabbit_is_head_strict_when_rerun():
    # rerun=True: a review on an earlier head is stale — must NOT read as done.
    from shipit.prstate.model import readiness_view, Review

    ctx = readiness_view(
        number=1,
        head_sha=NEW,
        is_draft=True,
        reviews=[Review(1, "coderabbitai[bot]", "COMMENTED", OLD, "")],
        requested_logins=["coderabbitai[bot]"],
        reviewer_rerun={"coderabbit": True},
    )
    assert CODERABBIT.detect(ctx) == ReviewLifecycle.REQUESTED


def test_dismissed_coderabbit_review_does_not_count_done():
    from shipit.prstate.model import readiness_view, Review

    ctx = readiness_view(
        number=1,
        head_sha=HEAD,
        is_draft=True,
        reviews=[Review(1, "coderabbitai[bot]", "DISMISSED", HEAD, "")],
        requested_logins=["coderabbitai[bot]"],
    )
    assert CODERABBIT.detect(ctx) == ReviewLifecycle.REQUESTED


def test_coderabbit_request_and_cancel_go_through_gh_pr_edit(monkeypatch):
    # The same GraphQL add-reviewer path Copilot uses — it creates a real
    # review_requested edge, so the generic #614 attach-verification applies.
    from shipit import gh

    calls: list[tuple] = []
    monkeypatch.setattr(
        gh,
        "pr_edit_reviewer",
        lambda pr, reviewer, remove=False: calls.append((pr, reviewer, remove)),
    )
    assert CODERABBIT.request(55) is True
    assert CODERABBIT.cancel(55) is True
    assert calls == [(55, "coderabbitai[bot]", False), (55, "coderabbitai[bot]", True)]


# --- local review backends: codex / agy (Phase 3) ---------------------------


def test_codex_and_agy_match_their_bot_logins():
    # Requires the `[bot]` suffix AND the stable `*-review` slug fragment —
    # matches the `adr-*-review[bot]` logins (and any future prefix) WITHOUT
    # hardcoding the user-specific `adr-` slug.
    assert CODEX.matches("adr-codex-review[bot]") is True
    assert CODEX.matches("adr-agy-review[bot]") is False
    assert AGY.matches("adr-agy-review[bot]") is True
    assert AGY.matches("adr-codex-review[bot]") is False
    # agy keys off `agy-review`, NOT `gemini` (the bot login is `adr-agy-review`).
    assert AGY.matches("gemini-code-assist[bot]") is False
    # Neither matches Copilot.
    assert CODEX.matches("copilot[bot]") is False
    assert AGY.matches("copilot[bot]") is False


def test_codex_and_agy_do_not_match_human_logins():
    # A human login that merely CONTAINS the substring (no `[bot]` suffix, no
    # `*-review` fragment) must NOT misread as the bot — that would falsely
    # report a DONE review.
    assert CODEX.matches("codexdev") is False
    assert CODEX.matches("codex-fan") is False
    assert CODEX.matches("codex") is False
    assert AGY.matches("agytron") is False
    assert AGY.matches("agy") is False
    # `[bot]` alone isn't enough — the slug fragment must also be present.
    assert CODEX.matches("codexbot[bot]") is False
    assert AGY.matches("agy-helper[bot]") is False


def test_codex_and_agy_require_bot_as_suffix_not_substring():
    # `[bot]` must be a SUFFIX, not appear mid-string: a login carrying the
    # slug fragment AND `[bot]` somewhere in the middle (but not at the end)
    # must NOT match — `endswith`, not substring containment.
    assert CODEX.matches("adr-codex-review[bot]-staging") is False
    assert AGY.matches("adr-agy-review[bot]y") is False
    # The slug fragment alone, with `[bot]` mid-string, is still False.
    assert CODEX.matches("codex-review[bot]x") is False


def test_codex_detect_done_on_head():
    # A review by the codex bot on the current head reads as done (head-strict).
    from shipit.prstate.model import readiness_view, Review

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
    # No review by the local reviewer → NOT_REQUESTED (no requested edge exists
    # for a local backend, so requested_logins is never consulted).
    from shipit.prstate.model import readiness_view

    ctx = readiness_view(number=1, head_sha=HEAD, is_draft=True)
    assert CODEX.detect(ctx) == ReviewLifecycle.NOT_REQUESTED
    assert AGY.detect(ctx) == ReviewLifecycle.NOT_REQUESTED


def test_codex_detect_stale_review_counts_done_review_once():
    # DEFAULT review-once: an earlier-head local review still counts as done.
    from shipit.prstate.model import readiness_view, Review

    ctx = readiness_view(
        number=1,
        head_sha=NEW,
        is_draft=True,
        reviews=[Review(1, "adr-codex-review[bot]", "COMMENTED", OLD, "")],
    )
    assert CODEX.detect(ctx) in (
        ReviewLifecycle.DONE_CLEAN,
        ReviewLifecycle.DONE_COMMENTS,
    )


def test_codex_detect_stale_review_is_not_done_when_rerun():
    # rerun=True: head-strict — a review against an earlier head is stale. A local
    # backend has no requested edge, so a staled review reads NOT_REQUESTED (it
    # must be re-run), never REQUESTED.
    from shipit.prstate.model import readiness_view, Review

    ctx = readiness_view(
        number=1,
        head_sha=NEW,
        is_draft=True,
        reviews=[Review(1, "adr-codex-review[bot]", "COMMENTED", OLD, "")],
        reviewer_rerun={"codex": True},
    )
    assert CODEX.detect(ctx) == ReviewLifecycle.NOT_REQUESTED


def test_dismissed_codex_review_does_not_count_done():
    from shipit.prstate.model import readiness_view, Review

    ctx = readiness_view(
        number=1,
        head_sha=HEAD,
        is_draft=True,
        reviews=[Review(1, "adr-codex-review[bot]", "DISMISSED", HEAD, "")],
    )
    assert CODEX.detect(ctx) == ReviewLifecycle.NOT_REQUESTED


def test_local_request_detaches_via_service(monkeypatch, tmp_path):
    # OBS03: requesting a codex/agy review LAZILY calls
    # `shipit.review.service.start_detached_review` (open the in_progress funnel run
    # + spawn the detached child) and returns True (in-flight; a local reviewer is
    # never edge-verified). The detach boundary is faked here — no fork, no network.
    from shipit.review import service

    calls: list[tuple] = []

    def fake_start_detached(backend, pr, **kwargs):
        calls.append((backend, pr, kwargs))
        return True

    monkeypatch.setattr(service, "start_detached_review", fake_start_detached)
    # No `.shipit.toml` in tmp cwd → no per-reviewer model/instructions options.
    monkeypatch.chdir(tmp_path)

    assert CODEX.request(7) is True
    assert AGY.request(9) is True
    # The adapters hand the service their ONE registry identity (COR02-WS03).
    assert calls[0][0] is _agent_backend.CODEX and calls[0][1] == 7
    assert calls[0][2]["as_app"] is True
    assert calls[1][0] is _agent_backend.ANTIGRAVITY and calls[1][1] == 9


def test_local_request_threads_model_and_instructions_from_config(
    monkeypatch, tmp_path
):
    # The per-reviewer `model` / `instructions` from `[reviewers]` are read and
    # threaded to the detached child (force scope: codex need not be a required
    # (holding) reviewer).
    from shipit.review import service

    (tmp_path / ".shipit.toml").write_text(
        "[reviewers]\n"
        "copilot = {}\n"
        'codex = { model = "flash", instructions = "docs/rev.md" }\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    captured: dict = {}

    def fake_start_detached(agent, pr, **kwargs):
        captured.update(kwargs)
        return True

    monkeypatch.setattr(service, "start_detached_review", fake_start_detached)
    assert CODEX.request(3) is True
    assert captured["model"] == "flash"
    # The instructions path is anchored to the config dir (absolute).
    assert captured["instructions_path"] == str(tmp_path / "docs" / "rev.md")


def test_local_request_normalizes_failure_to_prstateerror(monkeypatch, tmp_path):
    # Any failure in the synchronous detach (a `gh`/auth failure, a spawn failure)
    # is normalized to a clean PrStateError (the one error type the CLI renders + exit
    # 1) — never a raw traceback.
    from shipit.review import service

    def boom(agent, pr, **kwargs):
        raise RuntimeError("backend CLI exploded")

    monkeypatch.setattr(service, "start_detached_review", boom)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(PrStateError, match="codex-local review failed") as excinfo:
        CODEX.request(7)
    # The original failure was WRAPPED (normalized), not re-raised: the chained
    # cause is the underlying RuntimeError, so no raw traceback escapes.
    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert "backend CLI exploded" in str(excinfo.value.__cause__)


def test_local_cancel_is_a_noop():
    # A posted review can't be withdrawn — cancel returns False, like a
    # no-mechanism backend.
    assert CODEX.cancel(7) is False
    assert AGY.cancel(9) is False
