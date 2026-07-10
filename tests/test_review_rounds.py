"""Round-scope decision: pure `decide_round`, the `plan_for_view` I/O boundary,
and the `planable` gate (RVW02-WS06, ADR-0045).

Round 1 is the full-PR dimension fan-out; a round after the first — this reviewer
already reviewed an earlier head still an ancestor of the new head — is ONE
incremental pass over the fix range `last-reviewed-head..new-head`. A
rebase/force-push (the old head is no longer an ancestor) or a first round falls
back to (or is) a full round: fail toward over-reviewing.
"""

from __future__ import annotations

from types import SimpleNamespace

from shipit.identity import Sha
from shipit.review import roundrecord, rounds

MB = Sha("a" * 40)  # merge base
OLD = Sha("b" * 40)  # an earlier reviewed head
NEW = Sha("c" * 40)  # the head now under review


# --- decide_round (pure) ----------------------------------------------------


def test_first_round_has_no_prior_head_and_is_full():
    plan = rounds.decide_round(
        merge_base=MB, new_head=NEW, last_reviewed_head=None, last_is_ancestor=False
    )
    assert plan.incremental is False
    assert plan.base == MB and plan.head == NEW
    assert plan.fallback_reason is None  # a first round is not a fallback


def test_ancestor_prior_head_is_an_incremental_fix_range():
    plan = rounds.decide_round(
        merge_base=MB, new_head=NEW, last_reviewed_head=OLD, last_is_ancestor=True
    )
    assert plan.incremental is True
    assert plan.base == OLD and plan.head == NEW  # the fix range
    assert plan.fallback_reason is None


def test_force_push_non_ancestor_falls_back_to_a_full_round():
    # A rebase/force-push rewrote history: the old head is no longer an ancestor,
    # so the incremental premise is void → a full round over the merge base, with
    # the reason recorded so the over-review is explained.
    plan = rounds.decide_round(
        merge_base=MB, new_head=NEW, last_reviewed_head=OLD, last_is_ancestor=False
    )
    assert plan.incremental is False
    assert plan.base == MB and plan.head == NEW
    assert plan.fallback_reason and "not an ancestor" in plan.fallback_reason


def test_prior_head_equal_to_new_head_is_a_full_round():
    # A re-review of the exact same head has no fix range → full round (defensive).
    plan = rounds.decide_round(
        merge_base=MB, new_head=NEW, last_reviewed_head=NEW, last_is_ancestor=True
    )
    assert plan.incremental is False
    assert plan.base == MB


# --- planable gate ----------------------------------------------------------


def test_planable_true_only_with_all_fields():
    full = SimpleNamespace(
        base_sha=MB, head_sha=NEW, repo="acme/widget", workdir="/wd", number=1
    )
    assert rounds.planable(full) is True
    bare = SimpleNamespace(diff="d", workdir="/wd", number=1, head_ref="b")
    assert rounds.planable(bare) is False
    no_repo = SimpleNamespace(base_sha=MB, head_sha=NEW, repo=None, workdir="/wd")
    assert rounds.planable(no_repo) is False


# --- plan_for_view (I/O boundary) -------------------------------------------


def _view(**kw):
    base = dict(base_sha=MB, head_sha=NEW, repo="acme/widget", workdir="/wd", number=7)
    base.update(kw)
    return SimpleNamespace(**base)


def _record_prior_head(tmp_path, *, pr=7, reviewer="codex", head=str(OLD)):
    roundrecord.record_round(
        {"summary": {"status": "COMMENT"}, "comments": []},
        repo_slug="acme/widget",
        pr=pr,
        base_sha=str(MB),
        head_sha=head,
        reviewer=reviewer,
        model="pro",
        timeout="600s",
        instructions_path=None,
        base_dir=tmp_path / "state",
    )


def test_plan_for_view_no_history_is_full_round(tmp_path):
    plan = rounds.plan_for_view(_view(), "codex", base_dir=tmp_path / "state")
    assert plan.incremental is False


def test_plan_for_view_incremental_when_prior_head_is_ancestor(tmp_path, monkeypatch):
    _record_prior_head(tmp_path)
    monkeypatch.setattr(rounds.git, "is_ancestor", lambda a, b, *, cwd: True)
    plan = rounds.plan_for_view(_view(), "codex", base_dir=tmp_path / "state")
    assert plan.incremental is True
    assert plan.base == OLD and plan.head == NEW


def test_plan_for_view_force_push_falls_back_to_full(tmp_path, monkeypatch):
    _record_prior_head(tmp_path)
    monkeypatch.setattr(rounds.git, "is_ancestor", lambda a, b, *, cwd: False)
    plan = rounds.plan_for_view(_view(), "codex", base_dir=tmp_path / "state")
    assert plan.incremental is False
    assert plan.fallback_reason and "ancestor" in plan.fallback_reason


def test_plan_for_view_ignores_other_reviewers_history(tmp_path, monkeypatch):
    _record_prior_head(tmp_path, reviewer="agy")  # a co-reviewer's round
    monkeypatch.setattr(rounds.git, "is_ancestor", lambda a, b, *, cwd: True)
    plan = rounds.plan_for_view(_view(), "codex", base_dir=tmp_path / "state")
    assert plan.incremental is False  # codex has no prior head of its own


def test_plan_for_view_no_repo_is_full_round(tmp_path):
    plan = rounds.plan_for_view(_view(repo=None), "codex", base_dir=tmp_path / "state")
    assert plan.incremental is False
