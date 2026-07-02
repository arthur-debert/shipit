"""Tests for the funnel breadcrumb across the async detach split (OBS03).

OBS02 fixed the breadcrumb's two halves and their best-effort contract; OBS03
SPLITS them across a process boundary, so this suite exercises them on the async
path the local reviewer actually runs:

OBS03-WS01: `start_detached_review` (the PARENT) does the cheap synchronous work —
resolve `(repo, head_sha)`, open the `in_progress` `review: <reviewer>` check run
— then spawns a DETACHED child and returns in-flight WITHOUT running the agent.
`run_detached_review` (the CHILD body) does the heavy resolve + generate + post
and CLOSES the SAME `run_id` the parent handed it. The invariant: exactly ONE
check run — the parent creates, the child closes — never two. The spawn boundary
is injected so the parent's detach is asserted WITHOUT forking.

The create is **best-effort**: per the PRD prerequisite, until the App's
`checks:write` re-grant propagates a create can 403, and the local review must
STILL run — a failed breadcrumb is logged (the failure FACT, never the token) and
swallowed (the child still spawns, with no `--run-id`). The terminal close is
best-effort too: at the CHILD boundary a PATCH failure / a `run_id is None`
(the parent opened no run) never crashes the flow nor masks the review's real
outcome (the original error still propagates on the failure paths), mapping
posted → success, an empty run (no parseable review) → failure with an `empty`
reason, a backend error → failure, a timeout → timed_out.

The App-token boundary (`ghauth`) and the `gh` check-run POST/PATCH are FAKED —
never live GitHub.
"""

from __future__ import annotations

import logging

import pytest

from shipit.agent import backend as agent_backend
from shipit.review import service
from shipit.review.backends.base import BackendError
from shipit.review.diff import ReviewView, review_view
from shipit.execrun import ExecError

_DIFF = """\
diff --git a/foo.py b/foo.py
--- a/foo.py
+++ b/foo.py
@@ -1,2 +1,3 @@
 import os
+x = 1
 y = 2
"""

_REVIEW = {
    "summary": {"status": "COMMENT", "overall_feedback": "looks ok"},
    "comments": [],
}


def _ctx(repo: str | None = "owner/repo") -> ReviewView:
    return review_view(
        number=5,
        repo=repo,
        head_sha="deadbeef",
        base_ref="main",
        base_sha="cafe",
        diff=_DIFF,
        is_draft=False,
        changed_files=["foo.py"],
        workdir="/tmp/wd",
    )


@pytest.fixture
def _stub_pipeline(monkeypatch):
    """Stub the PR resolve + review generation + post so a `run_detached_review`
    call exercises ONLY the funnel-breadcrumb wiring. Records the post call."""
    # The detached child resolves the PR from its `--repo` arg; the stub returns a
    # ctx for it and also stubs `gh.current_repo()` for any slug inference path.
    monkeypatch.setattr(service, "resolve_pr", lambda pr, repo=None: _ctx(repo))
    monkeypatch.setattr(service.gh, "current_repo", lambda: "owner/repo")
    monkeypatch.setattr(
        service, "generate_review", lambda agent, ctx, **kw: dict(_REVIEW)
    )
    posted: dict = {}

    def fake_post_review(review, ctx, *, backend, event, dry_run, as_app):
        posted["called"] = True
        posted["agent"] = backend.funnel_agent
        posted["review"] = review
        posted["event"] = event
        return {"id": 99}

    monkeypatch.setattr(service.post, "post_review", fake_post_review)
    return posted


def _fake_checkrun_boundary(monkeypatch, *, create_id: int | None = 555) -> list[dict]:
    """Fake the App-token mint + the `gh` REST seam for BOTH the kickoff create
    (POST -> a run id) and the terminal transition (PATCH -> recorded). Returns the
    list of `{method, path, body}` calls so a test can assert one create + one
    PATCH on the same run id (never live GitHub)."""
    monkeypatch.setattr(
        service.checkrun.ghauth, "installation_token", lambda agent, repo: "ghs_tok"
    )
    calls: list[dict] = []

    def fake_rest(path, *, method=None, body=None, token=None):
        calls.append({"method": method, "path": path, "body": body})
        if method == "POST":
            return {"id": create_id} if create_id is not None else {}
        return {}

    monkeypatch.setattr(service.checkrun.gh, "rest", fake_rest)
    return calls


# --- OBS03-WS01: the detach split (parent creates, child closes) -------------


def test_start_detached_opens_inprogress_then_spawns(monkeypatch):
    """The PARENT: (a) opens the `in_progress` funnel run with a `started_at`,
    (b) spawns the detached child carrying repo + pr + the created run id, and
    (c) returns in-flight WITHOUT running the agent — no model run on the request
    path. The spawn boundary is injected so nothing forks."""
    calls = _fake_checkrun_boundary(monkeypatch)  # create POST -> run id 555
    monkeypatch.setattr(
        service, "_resolve_target", lambda pr: ("owner/repo", "deadbeef")
    )
    # If the agent ran in the parent, this records it — it must NOT.
    ran: list = []
    monkeypatch.setattr(service, "generate_review", lambda *a, **k: ran.append(1))

    spawned: list = []
    rc = service.start_detached_review(
        agent_backend.CODEX, 5, spawn=lambda argv, env: spawned.append(list(argv))
    )

    assert rc is True  # in-flight
    # (a) the in_progress funnel run was opened with an honest started_at.
    posts = [c for c in calls if c["method"] == "POST"]
    assert len(posts) == 1
    assert posts[0]["path"] == "/repos/owner/repo/check-runs"
    assert posts[0]["body"]["status"] == "in_progress"
    assert posts[0]["body"]["started_at"]
    # The parent does NOT close the run — the child does (no terminal PATCH here).
    assert not [c for c in calls if c["method"] == "PATCH"]
    # (b) the detached child was spawned with the args it reconstructs from.
    assert len(spawned) == 1
    argv = spawned[0]
    assert "_run" in argv
    assert argv[argv.index("--agent") + 1] == "codex"
    assert argv[argv.index("--pr") + 1] == "5"
    assert argv[argv.index("--repo") + 1] == "owner/repo"
    assert argv[argv.index("--run-id") + 1] == "555"
    # (c) the agent never ran on the request path.
    assert ran == []


def test_start_detached_default_spawn_is_the_exec_seam(monkeypatch):
    """With no injected ``spawn``, the detach boundary is the exec seam's
    :func:`shipit.execrun.spawn_detached` (issue #272, ADR-0028): the review
    path owns no raw subprocess call — the one non-Exec lives in execrun."""
    _fake_checkrun_boundary(monkeypatch)
    monkeypatch.setattr(
        service, "_resolve_target", lambda pr: ("owner/repo", "deadbeef")
    )
    spawned: list = []
    monkeypatch.setattr(
        service.execrun,
        "spawn_detached",
        lambda argv, **_: spawned.append(list(argv)),
    )

    assert service.start_detached_review(agent_backend.CODEX, 5) is True
    assert len(spawned) == 1
    assert "_run" in spawned[0]


def test_start_detached_exports_domain_keys_to_the_child_env(monkeypatch):
    """The DETACH SEAM for the domain-key context (LOG01-WS03, ADR-0029): the
    parent binds `pr`/`repo` at the seam — its own records from here carry them —
    and the child's environment carries `pr`/`repo` PLUS the funnel `run` id as
    `SHIPIT_LOG_CTX_*` vars (the run is exported to the child's story WITHOUT
    binding in the parent). The child rebinds them at its logging setup."""
    from shipit import logcontext

    _fake_checkrun_boundary(monkeypatch)  # create POST -> run id 555
    monkeypatch.setattr(
        service, "_resolve_target", lambda pr: ("owner/repo", "deadbeef")
    )

    envs: list[dict] = []
    rc = service.start_detached_review(
        agent_backend.CODEX, 5, spawn=lambda argv, env: envs.append(dict(env))
    )

    assert rc is True
    (child_env,) = envs
    assert child_env["SHIPIT_LOG_CTX_PR"] == "5"
    assert child_env["SHIPIT_LOG_CTX_REPO"] == "owner/repo"
    assert child_env["SHIPIT_LOG_CTX_RUN"] == "555"
    # The parent bound the SEAM's keys (pr/repo) — but never the child's run id.
    assert logcontext.bound() == {"pr": 5, "repo": "owner/repo"}


def test_start_detached_still_spawns_when_breadcrumb_create_fails(monkeypatch):
    """The breadcrumb create is BEST-EFFORT: a 403 before the `checks:write`
    re-grant must not fail the request — the child is still spawned (with no
    `--run-id`, so it runs without an in_progress marker) and the request returns
    in-flight."""
    monkeypatch.setattr(
        service.checkrun.ghauth, "installation_token", lambda agent, repo: "ghs_tok"
    )

    def boom_rest(path, *, method=None, body=None, token=None):
        raise ExecError(
            ["gh"], rc=1, stderr="403 Resource not accessible by integration"
        )

    monkeypatch.setattr(service.checkrun.gh, "rest", boom_rest)
    monkeypatch.setattr(
        service, "_resolve_target", lambda pr: ("owner/repo", "deadbeef")
    )

    spawned: list = []
    rc = service.start_detached_review(
        agent_backend.CODEX, 5, spawn=lambda argv, env: spawned.append(list(argv))
    )

    assert rc is True
    assert len(spawned) == 1
    assert "--run-id" not in spawned[0]  # no run was opened, so none is threaded


def test_resolve_target_raises_on_missing_headrefoid(monkeypatch):
    """The synchronous validation path: a `gh pr view` response without
    `headRefOid` (schema change / unexpected output) must raise `ReviewError`, NOT
    silently return an empty SHA — otherwise the request reports in-flight with no
    target commit for the funnel check."""
    monkeypatch.setattr(service.gh, "current_repo", lambda: "owner/repo")
    monkeypatch.setattr(service.gh, "pr_view", lambda pr, json_fields=None: "{}")

    with pytest.raises(service.ReviewError, match="no headRefOid"):
        service._resolve_target(5)


def test_resolve_target_raises_on_non_dict_json(monkeypatch):
    """A truthy non-dict (e.g. a JSON list) must not `AttributeError` out of
    `_resolve_target` — it is parseable but malformed, so it raises `ReviewError` like
    the other malformed-output cases."""
    monkeypatch.setattr(service.gh, "current_repo", lambda: "owner/repo")
    monkeypatch.setattr(service.gh, "pr_view", lambda pr, json_fields=None: "[1]")

    with pytest.raises(service.ReviewError, match="no headRefOid"):
        service._resolve_target(5)


def test_resolve_target_normalizes_malformed_repo_slug(monkeypatch):
    """`gh.current_repo()` returning an empty/non-`owner/name` slug makes
    `repo_from_slug` raise raw `ValueError`. The synchronous boundary normalizes it
    to a typed `ReviewError` with a clear message, not a leaked traceback."""
    monkeypatch.setattr(service.gh, "current_repo", lambda: "not-a-slug")
    monkeypatch.setattr(
        service.gh,
        "pr_view",
        lambda pr, json_fields=None: '{"headRefOid": "abc", "isDraft": false}',
    )
    with pytest.raises(service.ReviewError, match="could not resolve target"):
        service._resolve_target(5)


def test_resolve_target_normalizes_missing_core_key(monkeypatch):
    """A node with `headRefOid` present but a required core key (`number`) missing
    makes `core_from_node` raise raw `KeyError`; the boundary normalizes it to a
    typed `ReviewError` rather than leaking the `KeyError`."""
    monkeypatch.setattr(service.gh, "current_repo", lambda: "owner/repo")
    monkeypatch.setattr(
        service.gh,
        "pr_view",
        lambda pr, json_fields=None: '{"headRefOid": "abc", "isDraft": false}',
    )
    with pytest.raises(service.ReviewError, match="could not resolve target"):
        service._resolve_target(5)


def test_start_detached_closes_run_when_spawn_fails(monkeypatch):
    """If the detached spawn fails AFTER the parent opened the `in_progress` run,
    that run would hang forever with no child to close it. The parent closes it as
    failed (terminal PATCH on the SAME run) and re-raises so the adapter still
    normalizes the failure to `PrStateError`."""
    calls = _fake_checkrun_boundary(monkeypatch)  # create POST -> run id 555
    monkeypatch.setattr(
        service, "_resolve_target", lambda pr: ("owner/repo", "deadbeef")
    )

    def boom_spawn(argv, env):
        raise OSError("cannot fork")

    with pytest.raises(OSError, match="cannot fork"):
        service.start_detached_review(agent_backend.CODEX, 5, spawn=boom_spawn)

    # The parent opened the run (POST) and then closed it as failed (terminal
    # PATCH on the SAME run) — no dangling in_progress.
    posts = [c for c in calls if c["method"] == "POST"]
    assert len(posts) == 1
    patches = [c for c in calls if c["method"] == "PATCH"]
    assert len(patches) == 1
    assert patches[0]["path"] == "/repos/owner/repo/check-runs/555"
    assert patches[0]["body"]["status"] == "completed"
    assert patches[0]["body"]["conclusion"] == "failure"
    # The spawn failure's reason is recorded in the close (detail=str(exc)), so the
    # check-run output carries WHY it failed — consistent with the resolve path.
    assert "cannot fork" in patches[0]["body"]["output"]["summary"]


def test_start_detached_spawn_failure_with_no_run_just_reraises(monkeypatch):
    """When the best-effort breadcrumb create returned no run (`run_id is None`) and
    the spawn THEN fails, there is nothing to close — the parent must re-raise
    without attempting a terminal PATCH (which would crash on `run_id is None`)."""
    monkeypatch.setattr(
        service.checkrun.ghauth, "installation_token", lambda agent, repo: "ghs_tok"
    )
    calls: list[dict] = []

    def fake_rest(path, *, method=None, body=None, token=None):
        calls.append({"method": method, "path": path, "body": body})
        if method == "POST":
            raise ExecError(
                ["gh"], rc=1, stderr="403 Resource not accessible by integration"
            )
        return {}

    monkeypatch.setattr(service.checkrun.gh, "rest", fake_rest)
    monkeypatch.setattr(
        service, "_resolve_target", lambda pr: ("owner/repo", "deadbeef")
    )

    def boom_spawn(argv, env):
        raise OSError("cannot fork")

    with pytest.raises(OSError, match="cannot fork"):
        service.start_detached_review(agent_backend.CODEX, 5, spawn=boom_spawn)

    # No run was opened, so no terminal PATCH was attempted.
    assert not [c for c in calls if c["method"] == "PATCH"]


def test_run_detached_closes_passed_run_without_creating(monkeypatch, _stub_pipeline):
    """The CHILD: it NEVER creates a run (the parent already did) — it CLOSES the
    SAME `run_id` it was handed to completed/success, and the review still
    generates + posts."""
    calls = _fake_checkrun_boundary(monkeypatch)

    result = service.run_detached_review(
        agent_backend.CODEX, 5, repo="owner/repo", run_id=555
    )

    assert not [c for c in calls if c["method"] == "POST"]  # child never creates
    patches = [c for c in calls if c["method"] == "PATCH"]
    assert len(patches) == 1
    assert patches[0]["path"] == "/repos/owner/repo/check-runs/555"  # the SAME run
    assert patches[0]["body"]["status"] == "completed"
    assert patches[0]["body"]["conclusion"] == "success"
    assert _stub_pipeline["called"] is True
    assert result["post"] == {"id": 99}


def test_split_parent_creates_child_closes_one_run(monkeypatch, _stub_pipeline):
    """The OBS03 invariant end to end: the run id the PARENT creates is the run id
    the CHILD closes — exactly ONE check run, never two."""
    monkeypatch.setattr(
        service, "_resolve_target", lambda pr: ("owner/repo", "deadbeef")
    )

    # Parent: create -> 555; capture the child argv instead of forking.
    parent_calls = _fake_checkrun_boundary(monkeypatch)
    spawned: list = []
    service.start_detached_review(
        agent_backend.CODEX, 5, spawn=lambda argv, env: spawned.append(list(argv))
    )
    argv = spawned[0]
    run_id = int(argv[argv.index("--run-id") + 1])
    assert run_id == 555
    assert len([c for c in parent_calls if c["method"] == "POST"]) == 1

    # Child: hand it that run id; it closes the SAME run and creates none.
    child_calls = _fake_checkrun_boundary(monkeypatch)
    service.run_detached_review(
        agent_backend.CODEX, 5, repo="owner/repo", run_id=run_id
    )
    assert not [c for c in child_calls if c["method"] == "POST"]
    patches = [c for c in child_calls if c["method"] == "PATCH"]
    assert len(patches) == 1
    assert patches[0]["path"] == f"/repos/owner/repo/check-runs/{run_id}"


# --- OBS03-WS02: terminal-transition contract at the CHILD boundary ----------
# The mapping itself lives once in `_generate_post_and_close`; these pin it at the
# `run_detached_review` boundary the detached child actually runs through — posted
# -> success (`test_run_detached_closes_passed_run_without_creating` above), empty
# -> failure(empty), backend/post error -> failure, timeout marker -> timed_out —
# and assert the child NEVER creates a run (the parent already did).


def test_run_detached_empty_transitions_to_failure_with_empty_reason(
    monkeypatch, _stub_pipeline
):
    """The CHILD boundary: an EMPTY review (a `BackendError` without the timeout
    marker) closes the handed `run_id` to failure with an `empty` reason and
    re-raises — no create, exactly one terminal PATCH on the same run."""
    calls = _fake_checkrun_boundary(monkeypatch)

    def _empty(agent, ctx, **kw):
        raise BackendError("no parseable JSON\nraw output: <not json>")

    monkeypatch.setattr(service, "generate_review", _empty)

    with pytest.raises(BackendError):
        service.run_detached_review(
            agent_backend.CODEX, 5, repo="owner/repo", run_id=555
        )

    assert not [c for c in calls if c["method"] == "POST"]  # child never creates
    patch = next(c for c in calls if c["method"] == "PATCH")
    assert patch["path"] == "/repos/owner/repo/check-runs/555"
    assert patch["body"]["conclusion"] in {"failure", "neutral"}
    output = patch["body"]["output"]
    assert "empty" in (output["title"] + output["summary"]).lower()


def test_run_detached_backend_error_transitions_to_failure(monkeypatch, _stub_pipeline):
    """The CHILD boundary: an agent error (missing CLI / crash) closes the handed
    `run_id` to failure and the original error still propagates."""
    from shipit.review.backends.base import BackendUnavailable

    calls = _fake_checkrun_boundary(monkeypatch)

    def _boom(agent, ctx, **kw):
        raise BackendUnavailable("the 'codex' CLI was not found")

    monkeypatch.setattr(service, "generate_review", _boom)

    with pytest.raises(BackendUnavailable):
        service.run_detached_review(
            agent_backend.CODEX, 5, repo="owner/repo", run_id=555
        )

    assert not [c for c in calls if c["method"] == "POST"]  # child never creates
    patches = [c for c in calls if c["method"] == "PATCH"]
    assert len(patches) == 1
    assert patches[0]["path"] == "/repos/owner/repo/check-runs/555"
    assert patches[0]["body"]["conclusion"] == "failure"
    # Generation failed first, so the review POST never ran.
    assert _stub_pipeline.get("called") is not True


def test_run_detached_timeout_marker_transitions_to_timed_out(
    monkeypatch, _stub_pipeline
):
    """The CHILD boundary: a `BackendError` carrying the `_TIMEOUT_MARKER` closes
    the handed `run_id` to timed_out and re-raises."""
    from shipit.review.backends.base import _TIMEOUT_MARKER

    calls = _fake_checkrun_boundary(monkeypatch)

    def _timed(agent, ctx, **kw):
        raise BackendError(
            "codex timed out before returning a complete review\n"
            f"raw output: …{_TIMEOUT_MARKER}"
        )

    monkeypatch.setattr(service, "generate_review", _timed)

    with pytest.raises(BackendError):
        service.run_detached_review(
            agent_backend.CODEX, 5, repo="owner/repo", run_id=555
        )

    assert not [c for c in calls if c["method"] == "POST"]  # child never creates
    patch = next(c for c in calls if c["method"] == "PATCH")
    assert patch["path"] == "/repos/owner/repo/check-runs/555"
    assert patch["body"]["conclusion"] == "timed_out"


def test_run_detached_structured_timeout_flag_transitions_to_timed_out(
    monkeypatch, _stub_pipeline
):
    """Regression (Copilot #194): the outcome split reads the STRUCTURED
    `BackendError.timed_out` flag, NOT a string match on the message. A timeout whose
    `_capture` message PARAPHRASES the timeout (no `_TIMEOUT_MARKER` in the text — the
    real nonzero-exit / marker-in-stderr path) must still close the run `timed_out`,
    not `empty`/`neutral`. Before the fix the string match found no marker and
    misclassified it as `empty`."""
    from shipit.review.backends.base import _TIMEOUT_MARKER

    calls = _fake_checkrun_boundary(monkeypatch)

    msg = "agy timed out before returning a complete review (try a faster model)"
    assert _TIMEOUT_MARKER not in msg.lower()  # the message does NOT carry the marker

    def _timed(agent, ctx, **kw):
        raise BackendError(msg, raw="", timed_out=True)

    monkeypatch.setattr(service, "generate_review", _timed)

    with pytest.raises(BackendError):
        service.run_detached_review(
            agent_backend.ANTIGRAVITY, 5, repo="owner/repo", run_id=555
        )

    assert not [c for c in calls if c["method"] == "POST"]  # child never creates
    patch = next(c for c in calls if c["method"] == "PATCH")
    assert patch["path"] == "/repos/owner/repo/check-runs/555"
    assert patch["body"]["conclusion"] == "timed_out"  # NOT neutral/empty


# --- #76: salvage content-but-unparseable output as a top-level comment ------
# A local agent (agy) routinely returns review PROSE but truncated/invalid JSON on a
# large diff -> `BackendError`. Rather than drop it, the content is posted as a single
# top-level COMMENT (salvage) — but the funnel outcome STAYS the degraded `empty`
# (failure): the salvage is additive and never flips the run to success.


def test_run_detached_salvages_unparseable_content_as_comment(
    monkeypatch, _stub_pipeline
):
    """Content-but-unparseable JSON (a `BackendError` carrying `raw`) posts the raw
    text as a single top-level COMMENT, AND the funnel still records the degraded
    `empty`/failure — salvage is additive, never a flip to success."""
    calls = _fake_checkrun_boundary(monkeypatch)
    raw = 'Here is my detailed review prose...\n{"summary": {truncated'
    err = BackendError("no parseable JSON\nraw output: <snip>", raw=raw)

    def _unparseable(agent, ctx, **kw):
        raise err

    monkeypatch.setattr(service, "generate_review", _unparseable)

    with pytest.raises(BackendError):
        service.run_detached_review(
            agent_backend.ANTIGRAVITY, 5, repo="owner/repo", run_id=555
        )

    # (a) the salvage comment was posted as a COMMENT carrying the raw + a marker.
    assert _stub_pipeline["called"] is True
    assert _stub_pipeline["event"] == "COMMENT"
    body = _stub_pipeline["review"]["summary"]["overall_feedback"]
    assert raw in body
    assert "could not be parsed" in body
    assert not _stub_pipeline["review"]["comments"]  # a single top-level comment
    # (b) the funnel STILL records the degraded `empty`/failure (NOT success).
    patch = next(c for c in calls if c["method"] == "PATCH")
    assert patch["body"]["conclusion"] in {"failure", "neutral"}
    output = patch["body"]["output"]
    assert "empty" in (output["title"] + output["summary"]).lower()


def test_run_detached_empty_stdout_does_not_salvage(monkeypatch, _stub_pipeline):
    """A genuinely EMPTY stdout (no content on `raw`) posts NO salvage comment — the
    degraded `empty` close is unchanged from before #76."""
    calls = _fake_checkrun_boundary(monkeypatch)
    err = BackendError("no parseable JSON\nraw output:", raw="")  # nothing to salvage

    def _empty(agent, ctx, **kw):
        raise err

    monkeypatch.setattr(service, "generate_review", _empty)

    with pytest.raises(BackendError):
        service.run_detached_review(
            agent_backend.ANTIGRAVITY, 5, repo="owner/repo", run_id=555
        )

    assert _stub_pipeline.get("called") is not True  # no salvage post
    patch = next(c for c in calls if c["method"] == "PATCH")
    assert patch["body"]["conclusion"] in {"failure", "neutral"}


def test_run_detached_salvages_timeout_content_but_stays_timed_out(
    monkeypatch, _stub_pipeline
):
    """A TIMED-OUT agy run still emits truncated content before its marker — that is
    salvaged too, but the funnel outcome stays `timed_out` (honest), not flipped."""
    from shipit.review.backends.base import _TIMEOUT_MARKER

    calls = _fake_checkrun_boundary(monkeypatch)
    raw = f'{{"summary": {{"status": "COMMENT"... {_TIMEOUT_MARKER}'
    err = BackendError(
        f"agy timed out before returning a complete review\nraw output: …{raw}",
        raw=raw,
    )

    def _timed(agent, ctx, **kw):
        raise err

    monkeypatch.setattr(service, "generate_review", _timed)

    with pytest.raises(BackendError):
        service.run_detached_review(
            agent_backend.ANTIGRAVITY, 5, repo="owner/repo", run_id=555
        )

    assert _stub_pipeline["called"] is True  # content salvaged
    patch = next(c for c in calls if c["method"] == "PATCH")
    assert patch["body"]["conclusion"] == "timed_out"  # ...but outcome stays honest


def test_run_detached_funnel_summary_carries_snippet_not_full_raw(
    monkeypatch, _stub_pipeline
):
    """#75: the funnel check-run summary (a PR surface) carries only the snippet
    from the `BackendError` message — never the full raw, which belongs in the file
    sink. The full raw is salvaged to a comment + logged, but not dumped here."""
    calls = _fake_checkrun_boundary(monkeypatch)
    full_raw = "SECRET-FULL-RAW-" + "Z" * 5000
    err = BackendError(
        "the agent returned no parseable JSON\nraw output: SNIPPET-ONLY", raw=full_raw
    )

    def _unparseable(agent, ctx, **kw):
        raise err

    monkeypatch.setattr(service, "generate_review", _unparseable)

    with pytest.raises(BackendError):
        service.run_detached_review(
            agent_backend.ANTIGRAVITY, 5, repo="owner/repo", run_id=555
        )

    patch = next(c for c in calls if c["method"] == "PATCH")
    summary = patch["body"]["output"]["summary"]
    assert "SNIPPET-ONLY" in summary  # the snippet from the message is carried
    assert full_raw not in summary  # ...but never the full raw


def test_salvage_body_contains_raw_holding_backtick_fences():
    """#76 fence safety: raw that itself contains ``` (and a longer ```` run) must be
    fully CONTAINED by the salvage fence — a fixed ``` fence would close early and let
    the untrusted remainder render as live GitHub markdown (an injection surface). The
    body fences with a delimiter LONGER than the longest backtick run in the raw, so
    nothing inside can break out."""
    import re

    raw = (
        "Here is my review.\n"
        "```json\n"
        '{"summary": {"status": "COMMENT"}}\n'
        "```\n"
        "and a longer run: ````\nstill inside the fence\n````\n"
        "## not a real heading  @nobody  [x](http://evil)  - [ ] not a checkbox"
    )
    body, truncated = service._salvage_body("agy", raw)

    assert truncated is False
    assert raw in body  # the whole raw appears verbatim, fully contained

    # The opening fence is the FIRST backtick-only line (it precedes the raw content);
    # it must be longer than the longest backtick run inside raw (4 -> >= 5).
    fence = next(ln for ln in body.splitlines() if ln and set(ln) == {"`"})
    longest_inner = max((len(m) for m in re.findall(r"`+", raw)), default=0)
    assert len(fence) >= 3
    assert len(fence) > longest_inner  # cannot be closed early by any run in raw
    # The exact opening delimiter appears EXACTLY twice (open + close) — the raw's own
    # shorter runs never match it, so the fence can't be broken out of.
    assert body.count(fence) == 2


def test_run_detached_salvage_post_failure_does_not_mask_outcome(
    monkeypatch, _stub_pipeline
):
    """The salvage post is BEST-EFFORT: if posting the salvage comment fails, the
    funnel still records the degraded `empty` and the original `BackendError` still
    propagates (the salvage never masks the real outcome)."""
    calls = _fake_checkrun_boundary(monkeypatch)
    err = BackendError("no parseable JSON\nraw output: <snip>", raw="some prose")

    def _unparseable(agent, ctx, **kw):
        raise err

    def boom_post(*a, **k):
        raise RuntimeError("salvage post 403")

    monkeypatch.setattr(service, "generate_review", _unparseable)
    monkeypatch.setattr(service.post, "post_review", boom_post)

    with pytest.raises(BackendError):  # original error still propagates
        service.run_detached_review(
            agent_backend.ANTIGRAVITY, 5, repo="owner/repo", run_id=555
        )

    patch = next(c for c in calls if c["method"] == "PATCH")
    assert patch["body"]["conclusion"] in {"failure", "neutral"}  # degraded recorded


# --- OBS03-WS02: the terminal close stays BEST-EFFORT at the child boundary ---
# The breadcrumb must NEVER crash the review nor mask its real outcome. OBS02
# pinned this on the (now-removed) synchronous `run_and_post`; it lives on at the
# detached-child boundary the local reviewer actually runs through — the close
# helper `_close_funnel_breadcrumb` is shared, so these assert it at the seam the
# child closes the run through.


def test_run_detached_transition_failure_does_not_mask_success(
    monkeypatch, _stub_pipeline, caplog
):
    """A PATCH failure on the terminal transition is best-effort: on the SUCCESS
    path the review has already posted, so `run_detached_review` returns its normal
    result and never crashes."""
    monkeypatch.setattr(
        service.checkrun.ghauth, "installation_token", lambda agent, repo: "ghs_tok"
    )

    def fake_rest(path, *, method=None, body=None, token=None):
        # The child never creates (parent did); the only call is the terminal PATCH.
        raise ExecError(
            ["gh"], rc=1, stderr="PATCH 403 Resource not accessible by integration"
        )

    monkeypatch.setattr(service.checkrun.gh, "rest", fake_rest)

    with caplog.at_level(logging.WARNING, logger="shipit.review"):
        result = service.run_detached_review(
            agent_backend.CODEX, 5, repo="owner/repo", run_id=555
        )

    assert _stub_pipeline["called"] is True
    assert result["post"] == {"id": 99}
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "transition" in text.lower()


def test_run_detached_transition_failure_on_error_path_still_raises(
    monkeypatch, _stub_pipeline
):
    """On a failure path, a PATCH failure during the terminal transition must NOT
    mask the review's real error — the original BackendError still propagates."""
    monkeypatch.setattr(
        service.checkrun.ghauth, "installation_token", lambda agent, repo: "ghs_tok"
    )

    def fake_rest(path, *, method=None, body=None, token=None):
        raise ExecError(["gh"], rc=1, stderr="PATCH failed")

    monkeypatch.setattr(service.checkrun.gh, "rest", fake_rest)

    def _empty(agent, ctx, **kw):
        raise BackendError("no parseable JSON\nraw output:")

    monkeypatch.setattr(service, "generate_review", _empty)

    with pytest.raises(BackendError):
        service.run_detached_review(
            agent_backend.CODEX, 5, repo="owner/repo", run_id=555
        )


def test_run_detached_no_transition_when_no_run_id(monkeypatch, _stub_pipeline):
    """When the parent opened no run (`run_id is None`) and the review SUCCEEDS,
    there is nothing to transition — no PATCH is sent, no crash, and the review
    still posts."""
    calls = _fake_checkrun_boundary(monkeypatch)

    result = service.run_detached_review(
        agent_backend.CODEX, 5, repo="owner/repo", run_id=None
    )

    assert not [c for c in calls if c["method"] == "PATCH"]
    assert _stub_pipeline["called"] is True
    assert result["post"] == {"id": 99}


def test_run_detached_close_never_leaks_token(monkeypatch, _stub_pipeline, caplog):
    """Even when the terminal transition fails, no installation-token value reaches
    a record on the detached-child path."""
    secret = "ghs_leakCanary000111222333"

    def fake_rest(path, *, method=None, body=None, token=None):
        raise ExecError(["gh"], rc=1, stderr="transition failed")

    monkeypatch.setattr(
        service.checkrun.ghauth, "installation_token", lambda agent, repo: secret
    )
    monkeypatch.setattr(service.checkrun.gh, "rest", fake_rest)

    with caplog.at_level(logging.DEBUG, logger="shipit.review"):
        service.run_detached_review(
            agent_backend.CODEX, 5, repo="owner/repo", run_id=555
        )

    full = "\n".join(r.getMessage() for r in caplog.records)
    assert secret not in full
    assert _stub_pipeline["called"] is True


# --- OBS03-WS02: the detached child's records reach the OBS01 file sink -------
# Story 5: a crashed/finished detached run leaves a durable "why" in the per-repo
# file sink. The child entrypoint wires that sink deterministically from its
# `--repo` arg (`configure_logging_for_slug`); here we drive that wiring with an
# injected `base_dir` (the logsetup test seam), run the child body, and assert its
# run records landed in `<base>/<owner>/<repo>/shipit.log`.


@pytest.fixture
def _restore_shipit_logger():
    """Snapshot + restore the process-lifetime `shipit` logger so a file sink this
    test attaches (to a tmp dir) never leaks into another test."""
    logger = logging.getLogger("shipit")
    saved = list(logger.handlers)
    saved_level, saved_prop = logger.level, logger.propagate
    for handler in saved:
        logger.removeHandler(handler)
    try:
        yield
    finally:
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)
        for handler in saved:
            logger.addHandler(handler)
        logger.setLevel(saved_level)
        logger.propagate = saved_prop


def test_detached_child_records_reach_the_file_sink(
    monkeypatch, _stub_pipeline, _restore_shipit_logger, tmp_path
):
    """The story-5 regression: with the file sink wired from the child's known
    `owner/repo` slug, the detached run's records (start, resolve, terminal close)
    land in the per-repo log file — so a finished/crashed detached run leaves a
    durable, readable trail even with no terminal attached."""
    from shipit import logsetup

    _fake_checkrun_boundary(monkeypatch)
    # Wire the file sink the way the `_run` child does — deterministically from the
    # repo slug — but into an injected base_dir so nothing touches a real $HOME.
    attached = logsetup.configure_logging_for_slug("owner/repo", base_dir=tmp_path)
    assert attached is True

    service.run_detached_review(agent_backend.CODEX, 5, repo="owner/repo", run_id=555)

    for handler in logging.getLogger("shipit").handlers:
        handler.flush()
    log_file = tmp_path / "owner" / "repo" / "shipit.log"
    assert log_file.exists()
    contents = log_file.read_text()
    # The child's own framing records reconstruct the run...
    assert "child start" in contents
    assert "child done" in contents
    # ...including the heavy-resolve shape and the terminal transition.
    assert "resolved" in contents
    assert "completed/success" in contents


def test_configure_logging_for_slug_is_best_effort_on_bad_slug(tmp_path):
    """A malformed slug attaches no file sink and never raises — a logging glitch
    must not crash the detached review."""
    from shipit import logsetup

    assert logsetup.configure_logging_for_slug("not-a-slug", base_dir=tmp_path) is False


# --- OBS03-WS03: child self-resolution of the resolve region --------------------
# The ONE remaining observable `in_progress` gap: `resolve_pr` runs OUTSIDE
# `_generate_post_and_close`'s own terminal-close region, so a resolve failure would
# kill the child before any close and leave the parent-opened run stuck
# `in_progress` forever. WS03 wraps PRECISELY that region — and nothing more, so a
# correct timeout/empty close is never overwritten with `failed`.


def test_run_detached_resolve_failure_closes_run_failed_and_reraises(monkeypatch):
    """A `resolve_pr` failure (fetch / auth / network) closes the handed `run_id` to
    `failed` EXACTLY ONCE on the SAME run and RE-RAISES — no dangling `in_progress`,
    no create."""
    calls = _fake_checkrun_boundary(monkeypatch)

    def boom_resolve(pr, repo=None):
        raise ExecError(["gh"], rc=1, stderr="could not fetch PR diff for #5")

    monkeypatch.setattr(service, "resolve_pr", boom_resolve)

    with pytest.raises(ExecError, match="could not fetch PR diff"):
        service.run_detached_review(
            agent_backend.CODEX, 5, repo="owner/repo", run_id=555
        )

    assert not [c for c in calls if c["method"] == "POST"]  # child never creates
    patches = [c for c in calls if c["method"] == "PATCH"]
    assert len(patches) == 1  # closed exactly once
    assert patches[0]["path"] == "/repos/owner/repo/check-runs/555"  # the SAME run
    assert patches[0]["body"]["status"] == "completed"
    assert patches[0]["body"]["conclusion"] == "failure"


def test_run_detached_resolve_failure_with_no_run_id_just_reraises(monkeypatch):
    """When the parent opened no run (`run_id is None`) and resolve THEN fails, there
    is nothing to close — the child re-raises without a terminal PATCH (which would
    otherwise crash on `run_id is None`)."""
    calls = _fake_checkrun_boundary(monkeypatch)

    def boom_resolve(pr, repo=None):
        raise ExecError(["gh"], rc=1, stderr="could not fetch PR diff")

    monkeypatch.setattr(service, "resolve_pr", boom_resolve)

    with pytest.raises(ExecError):
        service.run_detached_review(
            agent_backend.CODEX, 5, repo="owner/repo", run_id=None
        )

    assert not [c for c in calls if c["method"] == "PATCH"]


def test_run_detached_resolve_guard_does_not_overwrite_timeout_close(
    monkeypatch, _stub_pipeline
):
    """The guard's scope is PRECISELY the resolve region: it must NOT wrap
    `_generate_post_and_close`, which already closes with the CORRECT conclusion.
    With resolve SUCCEEDING, a timeout still closes `timed_out` (NOT overwritten to
    `failed`), exactly once."""
    from shipit.review.backends.base import _TIMEOUT_MARKER

    calls = _fake_checkrun_boundary(monkeypatch)

    def _timed(agent, ctx, **kw):
        raise BackendError(
            "codex timed out before returning a complete review\n"
            f"raw output: …{_TIMEOUT_MARKER}"
        )

    monkeypatch.setattr(service, "generate_review", _timed)

    with pytest.raises(BackendError):
        service.run_detached_review(
            agent_backend.CODEX, 5, repo="owner/repo", run_id=555
        )

    patches = [c for c in calls if c["method"] == "PATCH"]
    assert len(patches) == 1  # closed exactly once...
    assert patches[0]["body"]["conclusion"] == "timed_out"  # ...with its OWN conclusion


def test_run_detached_resolve_guard_does_not_overwrite_empty_close(
    monkeypatch, _stub_pipeline
):
    """Same no-overwrite guarantee for the EMPTY path: with resolve SUCCEEDING, an
    empty review closes `failure` with the `empty` reason — NOT overwritten to a
    bare `failed` — exactly once."""
    calls = _fake_checkrun_boundary(monkeypatch)

    def _empty(agent, ctx, **kw):
        raise BackendError("no parseable JSON\nraw output: <not json>")

    monkeypatch.setattr(service, "generate_review", _empty)

    with pytest.raises(BackendError):
        service.run_detached_review(
            agent_backend.CODEX, 5, repo="owner/repo", run_id=555
        )

    patches = [c for c in calls if c["method"] == "PATCH"]
    assert len(patches) == 1
    assert patches[0]["body"]["conclusion"] in {"failure", "neutral"}
    output = patches[0]["body"]["output"]
    assert "empty" in (output["title"] + output["summary"]).lower()


# --- OBS03-WS03: idempotent reconcile against an in-flight run -------------------
# A re-request whose funnel run is already non-terminal for the CURRENT head must
# reconcile (report in-flight) — NOT open a second breadcrumb + spawn a second child
# that double-posts. Read-then-decide in the PARENT, against the check run only (no
# local/daemon state). The find boundary is injected so "already in-flight" is
# simulated without the network.


def test_start_detached_reconciles_against_existing_inflight_run(monkeypatch):
    """When the find boundary reports an existing in-flight run, the re-request
    RECONCILES: it returns in-flight WITHOUT opening a breadcrumb (no POST) or
    spawning a child — so no second review is ever posted."""
    calls = _fake_checkrun_boundary(monkeypatch)
    monkeypatch.setattr(
        service, "_resolve_target", lambda pr: ("owner/repo", "deadbeef")
    )

    spawned: list = []
    rc = service.start_detached_review(
        agent_backend.CODEX,
        5,
        spawn=lambda argv, env: spawned.append(list(argv)),
        find=lambda agent, repo, head_sha: 999,
    )

    assert rc is True  # reported in-flight
    assert spawned == []  # no duplicate child spawned
    # No breadcrumb create and no terminal PATCH — reconciled against run 999.
    assert not [c for c in calls if c["method"] in {"POST", "PATCH"}]


def test_start_detached_no_inflight_run_creates_and_spawns(monkeypatch):
    """The reconcile is a NO-OP when nothing is in flight: the find boundary returns
    None, so the normal path runs — one `in_progress` create and one detached
    child."""
    calls = _fake_checkrun_boundary(monkeypatch)
    monkeypatch.setattr(
        service, "_resolve_target", lambda pr: ("owner/repo", "deadbeef")
    )

    spawned: list = []
    rc = service.start_detached_review(
        agent_backend.CODEX,
        5,
        spawn=lambda argv, env: spawned.append(list(argv)),
        find=lambda agent, repo, head_sha: None,
    )

    assert rc is True
    assert len(spawned) == 1  # the normal detached child
    posts = [c for c in calls if c["method"] == "POST"]
    assert len(posts) == 1  # the in_progress create still happened
    assert posts[0]["body"]["status"] == "in_progress"


def test_start_detached_reconcile_lookup_failure_proceeds_to_spawn(monkeypatch, caplog):
    """The reconcile read is BEST-EFFORT: if the in-flight lookup raises (e.g. a 403
    before the `checks` re-grant), the request must NOT fail — it logs the fact and
    proceeds to open + spawn a fresh run (at worst a duplicate, never a blocked
    request)."""
    calls = _fake_checkrun_boundary(monkeypatch)
    monkeypatch.setattr(
        service, "_resolve_target", lambda pr: ("owner/repo", "deadbeef")
    )

    def boom_find(agent, repo, head_sha):
        raise ExecError(
            ["gh"], rc=1, stderr="403 Resource not accessible by integration"
        )

    spawned: list = []
    with caplog.at_level(logging.WARNING, logger="shipit.review"):
        rc = service.start_detached_review(
            agent_backend.CODEX,
            5,
            spawn=lambda argv, env: spawned.append(list(argv)),
            find=boom_find,
        )

    assert rc is True
    assert len(spawned) == 1  # proceeded to spawn a fresh run
    assert [c for c in calls if c["method"] == "POST"]  # ...and opened a fresh run
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "reconcile" in text.lower()


def test_unknown_outcome_falls_back_to_failed_without_crashing(monkeypatch, caplog):
    """Defensive (Copilot #66): `_close_funnel_breadcrumb` must not KeyError on an
    unexpected/typo outcome — that would escape this best-effort path and mask the
    review's real result. An unknown outcome maps to the `failed` conclusion and is
    logged, never raised."""
    calls = _fake_checkrun_boundary(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="shipit.review"):
        service._close_funnel_breadcrumb(
            agent_backend.CODEX, "owner/repo", 555, outcome="bogus-outcome"
        )

    patches = [c for c in calls if c["method"] == "PATCH"]
    assert len(patches) == 1
    assert patches[0]["body"]["conclusion"] == "failure"  # the `failed` mapping
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "unknown funnel outcome" in text.lower()
