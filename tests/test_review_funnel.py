from __future__ import annotations

import logging

import pytest

from shipit.agent import backend as agent_backend
from shipit.execrun import ExecError
from shipit.identity import repo_from_slug
from shipit.pr import PrId
from shipit.review import service
from shipit.review.backends.base import BackendError
from shipit.review.diff import ReviewView, review_view

TARGET = PrId(repo=repo_from_slug("owner/repo"), number=5)

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
        head_sha="deadbeef" * 5,
        base_ref="main",
        base_sha="cafe" * 10,
        diff=_DIFF,
        is_draft=False,
        changed_files=["foo.py"],
        workdir="/tmp/wd",
    )


@pytest.fixture
def _stub_pipeline(monkeypatch):
    monkeypatch.setattr(service, "resolve_pr", lambda pr, repo=None: _ctx(repo))
    monkeypatch.setattr(
        service.gh, "current_repo", lambda: repo_from_slug("owner/repo")
    )
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


def test_start_detached_opens_inprogress_then_spawns(monkeypatch):
    calls = _fake_checkrun_boundary(monkeypatch)
    monkeypatch.setattr(service, "_resolve_head_sha", lambda pr: "deadbeef")
    ran: list = []
    monkeypatch.setattr(service, "generate_review", lambda *a, **k: ran.append(1))

    spawned: list = []
    rc = service.start_detached_review(
        agent_backend.CODEX, TARGET, spawn=lambda argv, env: spawned.append(list(argv))
    )

    assert rc is True
    posts = [c for c in calls if c["method"] == "POST"]
    assert len(posts) == 1
    assert posts[0]["path"] == "/repos/owner/repo/check-runs"
    assert posts[0]["body"]["status"] == "in_progress"
    assert posts[0]["body"]["started_at"]
    assert not [c for c in calls if c["method"] == "PATCH"]
    assert len(spawned) == 1
    argv = spawned[0]
    assert "_run" in argv
    assert argv[argv.index("--agent") + 1] == "codex"
    assert argv[argv.index("--pr") + 1] == "5"
    assert argv[argv.index("--repo") + 1] == "owner/repo"
    assert argv[argv.index("--run-id") + 1] == "555"
    assert ran == []


def test_start_detached_default_spawn_is_the_exec_seam(monkeypatch):
    _fake_checkrun_boundary(monkeypatch)
    monkeypatch.setattr(service, "_resolve_head_sha", lambda pr: "deadbeef")
    spawned: list = []
    monkeypatch.setattr(
        service.execrun,
        "spawn_detached",
        lambda argv, **_: spawned.append(list(argv)),
    )

    assert service.start_detached_review(agent_backend.CODEX, TARGET) is True
    assert len(spawned) == 1
    assert "_run" in spawned[0]


def test_start_detached_exports_domain_keys_to_the_child_env(monkeypatch):
    from shipit import logcontext

    _fake_checkrun_boundary(monkeypatch)
    monkeypatch.setattr(service, "_resolve_head_sha", lambda pr: "deadbeef")

    envs: list[dict] = []
    rc = service.start_detached_review(
        agent_backend.CODEX, TARGET, spawn=lambda argv, env: envs.append(dict(env))
    )

    assert rc is True
    (child_env,) = envs
    assert child_env["SHIPIT_LOG_CTX_PR"] == "5"
    assert child_env["SHIPIT_LOG_CTX_REPO"] == "owner/repo"
    assert child_env["SHIPIT_LOG_CTX_RUN"] == "555"
    assert logcontext.bound() == {"pr": 5, "repo": "owner/repo"}


def test_start_detached_still_spawns_when_breadcrumb_create_fails(monkeypatch):
    monkeypatch.setattr(
        service.checkrun.ghauth, "installation_token", lambda agent, repo: "ghs_tok"
    )

    def boom_rest(path, *, method=None, body=None, token=None):
        raise ExecError(
            ["gh"], rc=1, stderr="403 Resource not accessible by integration"
        )

    monkeypatch.setattr(service.checkrun.gh, "rest", boom_rest)
    monkeypatch.setattr(service, "_resolve_head_sha", lambda pr: "deadbeef")

    spawned: list = []
    rc = service.start_detached_review(
        agent_backend.CODEX, TARGET, spawn=lambda argv, env: spawned.append(list(argv))
    )

    assert rc is True
    assert len(spawned) == 1
    assert "--run-id" not in spawned[0]


def test_start_detached_as_app_auth_failure_fails_loud_and_does_not_spawn(monkeypatch):
    from shipit.review.ghauth import UNCONFIGURED, ReviewAuthError

    def no_auth(agent, repo):
        raise ReviewAuthError(
            "Could not source the private key from Doppler", kind=UNCONFIGURED
        )

    monkeypatch.setattr(service.checkrun.ghauth, "installation_token", no_auth)
    monkeypatch.setattr(service, "_resolve_head_sha", lambda pr: "deadbeef")

    spawned: list = []
    with pytest.raises(ReviewAuthError, match="Doppler"):
        service.start_detached_review(
            agent_backend.CODEX,
            TARGET,
            spawn=lambda argv, env: spawned.append(list(argv)),
        )
    assert spawned == []


def test_start_detached_no_as_app_auth_failure_stays_best_effort(monkeypatch):
    from shipit.review.ghauth import UNCONFIGURED, ReviewAuthError

    def no_auth(agent, repo):
        raise ReviewAuthError(
            "Could not source the private key from Doppler", kind=UNCONFIGURED
        )

    monkeypatch.setattr(service.checkrun.ghauth, "installation_token", no_auth)
    monkeypatch.setattr(service, "_resolve_head_sha", lambda pr: "deadbeef")

    spawned: list = []
    rc = service.start_detached_review(
        agent_backend.CODEX,
        TARGET,
        as_app=False,
        spawn=lambda argv, env: spawned.append(list(argv)),
    )

    assert rc is True
    assert len(spawned) == 1
    assert "--run-id" not in spawned[0]


def test_resolve_head_sha_raises_on_missing_headrefoid(monkeypatch):
    monkeypatch.setattr(
        service.gh,
        "pr_view",
        lambda pr, *, repo=None, json_fields=None: {"number": 5, "isDraft": False},
    )

    with pytest.raises(service.ReviewError, match="could not resolve target core"):
        service._resolve_head_sha(TARGET)


def test_resolve_head_sha_normalizes_adapter_value_error(monkeypatch):

    def bad_view(pr, *, repo=None, json_fields=None):
        raise ValueError("`gh pr view` output for '5' is not a JSON object: [1]")

    monkeypatch.setattr(service.gh, "pr_view", bad_view)

    with pytest.raises(service.ReviewError, match="could not resolve target core"):
        service._resolve_head_sha(TARGET)


def test_resolve_head_sha_normalizes_malformed_head_sha(monkeypatch):
    monkeypatch.setattr(
        service.gh,
        "pr_view",
        lambda pr, *, repo=None, json_fields=None: {
            "number": 5,
            "headRefOid": "abc",
            "isDraft": False,
        },
    )
    with pytest.raises(service.ReviewError, match="could not resolve target core"):
        service._resolve_head_sha(TARGET)


def test_resolve_head_sha_returns_the_typed_head(monkeypatch):
    head = "cafe" * 10
    seen: dict = {}

    def fake_view(pr, *, repo=None, json_fields=None):
        seen["repo"] = repo
        return {
            "number": 5,
            "headRefOid": head.upper(),
            "baseRefName": "main",
            "isDraft": False,
            "mergeStateStatus": "CLEAN",
        }

    monkeypatch.setattr(service.gh, "pr_view", fake_view)
    assert service._resolve_head_sha(TARGET) == head
    assert seen["repo"] == "owner/repo"


def test_start_detached_closes_run_when_spawn_fails(monkeypatch):
    calls = _fake_checkrun_boundary(monkeypatch)
    monkeypatch.setattr(service, "_resolve_head_sha", lambda pr: "deadbeef")

    def boom_spawn(argv, env):
        raise OSError("cannot fork")

    with pytest.raises(OSError, match="cannot fork"):
        service.start_detached_review(agent_backend.CODEX, TARGET, spawn=boom_spawn)

    posts = [c for c in calls if c["method"] == "POST"]
    assert len(posts) == 1
    patches = [c for c in calls if c["method"] == "PATCH"]
    assert len(patches) == 1
    assert patches[0]["path"] == "/repos/owner/repo/check-runs/555"
    assert patches[0]["body"]["status"] == "completed"
    assert patches[0]["body"]["conclusion"] == "failure"
    assert "cannot fork" in patches[0]["body"]["output"]["summary"]


def test_start_detached_spawn_failure_with_no_run_just_reraises(monkeypatch):
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
    monkeypatch.setattr(service, "_resolve_head_sha", lambda pr: "deadbeef")

    def boom_spawn(argv, env):
        raise OSError("cannot fork")

    with pytest.raises(OSError, match="cannot fork"):
        service.start_detached_review(agent_backend.CODEX, TARGET, spawn=boom_spawn)

    assert not [c for c in calls if c["method"] == "PATCH"]


def test_run_detached_closes_passed_run_without_creating(monkeypatch, _stub_pipeline):
    calls = _fake_checkrun_boundary(monkeypatch)

    result = service.run_detached_review(agent_backend.CODEX, TARGET, run_id=555)

    assert not [c for c in calls if c["method"] == "POST"]
    patches = [c for c in calls if c["method"] == "PATCH"]
    assert len(patches) == 1
    assert patches[0]["path"] == "/repos/owner/repo/check-runs/555"
    assert patches[0]["body"]["status"] == "completed"
    assert patches[0]["body"]["conclusion"] == "success"
    assert _stub_pipeline["called"] is True
    assert result["post"] == {"id": 99}


def test_split_parent_creates_child_closes_one_run(monkeypatch, _stub_pipeline):
    monkeypatch.setattr(service, "_resolve_head_sha", lambda pr: "deadbeef")

    parent_calls = _fake_checkrun_boundary(monkeypatch)
    spawned: list = []
    service.start_detached_review(
        agent_backend.CODEX, TARGET, spawn=lambda argv, env: spawned.append(list(argv))
    )
    argv = spawned[0]
    run_id = int(argv[argv.index("--run-id") + 1])
    assert run_id == 555
    assert len([c for c in parent_calls if c["method"] == "POST"]) == 1

    child_calls = _fake_checkrun_boundary(monkeypatch)
    service.run_detached_review(agent_backend.CODEX, TARGET, run_id=run_id)
    assert not [c for c in child_calls if c["method"] == "POST"]
    patches = [c for c in child_calls if c["method"] == "PATCH"]
    assert len(patches) == 1
    assert patches[0]["path"] == f"/repos/owner/repo/check-runs/{run_id}"


def test_run_detached_empty_transitions_to_failure_with_empty_reason(
    monkeypatch, _stub_pipeline
):
    calls = _fake_checkrun_boundary(monkeypatch)

    def _empty(agent, ctx, **kw):
        raise BackendError("no parseable JSON\nraw output: <not json>")

    monkeypatch.setattr(service, "generate_review", _empty)

    with pytest.raises(BackendError):
        service.run_detached_review(agent_backend.CODEX, TARGET, run_id=555)

    assert not [c for c in calls if c["method"] == "POST"]
    patch = next(c for c in calls if c["method"] == "PATCH")
    assert patch["path"] == "/repos/owner/repo/check-runs/555"
    assert patch["body"]["conclusion"] in {"failure", "neutral"}
    output = patch["body"]["output"]
    assert "empty" in (output["title"] + output["summary"]).lower()


def test_run_detached_backend_error_transitions_to_failure(monkeypatch, _stub_pipeline):
    from shipit.review.backends.base import BackendUnavailable

    calls = _fake_checkrun_boundary(monkeypatch)

    def _boom(agent, ctx, **kw):
        raise BackendUnavailable("the 'codex' CLI was not found")

    monkeypatch.setattr(service, "generate_review", _boom)

    with pytest.raises(BackendUnavailable):
        service.run_detached_review(agent_backend.CODEX, TARGET, run_id=555)

    assert not [c for c in calls if c["method"] == "POST"]
    patches = [c for c in calls if c["method"] == "PATCH"]
    assert len(patches) == 1
    assert patches[0]["path"] == "/repos/owner/repo/check-runs/555"
    assert patches[0]["body"]["conclusion"] == "failure"
    assert _stub_pipeline.get("called") is not True


def test_run_detached_timeout_marker_transitions_to_timed_out(
    monkeypatch, _stub_pipeline
):
    from shipit.review.backends.base import _TIMEOUT_MARKER

    calls = _fake_checkrun_boundary(monkeypatch)

    def _timed(agent, ctx, **kw):
        raise BackendError(
            "codex timed out before returning a complete review\n"
            f"raw output: …{_TIMEOUT_MARKER}"
        )

    monkeypatch.setattr(service, "generate_review", _timed)

    with pytest.raises(BackendError):
        service.run_detached_review(agent_backend.CODEX, TARGET, run_id=555)

    assert not [c for c in calls if c["method"] == "POST"]
    patch = next(c for c in calls if c["method"] == "PATCH")
    assert patch["path"] == "/repos/owner/repo/check-runs/555"
    assert patch["body"]["conclusion"] == "timed_out"


def test_run_detached_structured_timeout_flag_transitions_to_timed_out(
    monkeypatch, _stub_pipeline
):
    from shipit.review.backends.base import _TIMEOUT_MARKER

    calls = _fake_checkrun_boundary(monkeypatch)

    msg = "agy timed out before returning a complete review (try a faster model)"
    assert _TIMEOUT_MARKER not in msg.lower()

    def _timed(agent, ctx, **kw):
        raise BackendError(msg, raw="", timed_out=True)

    monkeypatch.setattr(service, "generate_review", _timed)

    with pytest.raises(BackendError):
        service.run_detached_review(agent_backend.ANTIGRAVITY, TARGET, run_id=555)

    assert not [c for c in calls if c["method"] == "POST"]
    patch = next(c for c in calls if c["method"] == "PATCH")
    assert patch["path"] == "/repos/owner/repo/check-runs/555"
    assert patch["body"]["conclusion"] == "timed_out"


def test_run_detached_salvages_unparseable_content_as_comment(
    monkeypatch, _stub_pipeline
):
    calls = _fake_checkrun_boundary(monkeypatch)
    raw = 'Here is my detailed review prose...\n{"summary": {truncated'
    err = BackendError("no parseable JSON\nraw output: <snip>", raw=raw)

    def _unparseable(agent, ctx, **kw):
        raise err

    monkeypatch.setattr(service, "generate_review", _unparseable)

    with pytest.raises(BackendError):
        service.run_detached_review(agent_backend.ANTIGRAVITY, TARGET, run_id=555)

    assert _stub_pipeline["called"] is True
    assert _stub_pipeline["event"] == "COMMENT"
    body = _stub_pipeline["review"]["summary"]["overall_feedback"]
    assert raw in body
    assert "could not be parsed" in body
    assert not _stub_pipeline["review"]["comments"]
    patch = next(c for c in calls if c["method"] == "PATCH")
    assert patch["body"]["conclusion"] in {"failure", "neutral"}
    output = patch["body"]["output"]
    assert "empty" in (output["title"] + output["summary"]).lower()


def test_run_detached_empty_stdout_does_not_salvage(monkeypatch, _stub_pipeline):
    calls = _fake_checkrun_boundary(monkeypatch)
    err = BackendError("no parseable JSON\nraw output:", raw="")

    def _empty(agent, ctx, **kw):
        raise err

    monkeypatch.setattr(service, "generate_review", _empty)

    with pytest.raises(BackendError):
        service.run_detached_review(agent_backend.ANTIGRAVITY, TARGET, run_id=555)

    assert _stub_pipeline.get("called") is not True
    patch = next(c for c in calls if c["method"] == "PATCH")
    assert patch["body"]["conclusion"] in {"failure", "neutral"}


def test_run_detached_salvages_timeout_content_but_stays_timed_out(
    monkeypatch, _stub_pipeline
):
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
        service.run_detached_review(agent_backend.ANTIGRAVITY, TARGET, run_id=555)

    assert _stub_pipeline["called"] is True
    patch = next(c for c in calls if c["method"] == "PATCH")
    assert patch["body"]["conclusion"] == "timed_out"


def test_run_detached_funnel_summary_carries_snippet_not_full_raw(
    monkeypatch, _stub_pipeline
):
    calls = _fake_checkrun_boundary(monkeypatch)
    full_raw = "SECRET-FULL-RAW-" + "Z" * 5000
    err = BackendError(
        "the agent returned no parseable JSON\nraw output: SNIPPET-ONLY", raw=full_raw
    )

    def _unparseable(agent, ctx, **kw):
        raise err

    monkeypatch.setattr(service, "generate_review", _unparseable)

    with pytest.raises(BackendError):
        service.run_detached_review(agent_backend.ANTIGRAVITY, TARGET, run_id=555)

    patch = next(c for c in calls if c["method"] == "PATCH")
    summary = patch["body"]["output"]["summary"]
    assert "SNIPPET-ONLY" in summary
    assert full_raw not in summary


def test_salvage_body_contains_raw_holding_backtick_fences():
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
    assert raw in body

    fence = next(ln for ln in body.splitlines() if ln and set(ln) == {"`"})
    longest_inner = max((len(m) for m in re.findall(r"`+", raw)), default=0)
    assert len(fence) >= 3
    assert len(fence) > longest_inner
    assert body.count(fence) == 2


def test_run_detached_salvage_post_failure_does_not_mask_outcome(
    monkeypatch, _stub_pipeline
):
    calls = _fake_checkrun_boundary(monkeypatch)
    err = BackendError("no parseable JSON\nraw output: <snip>", raw="some prose")

    def _unparseable(agent, ctx, **kw):
        raise err

    def boom_post(*a, **k):
        raise RuntimeError("salvage post 403")

    monkeypatch.setattr(service, "generate_review", _unparseable)
    monkeypatch.setattr(service.post, "post_review", boom_post)

    with pytest.raises(BackendError):
        service.run_detached_review(agent_backend.ANTIGRAVITY, TARGET, run_id=555)

    patch = next(c for c in calls if c["method"] == "PATCH")
    assert patch["body"]["conclusion"] in {"failure", "neutral"}


def test_run_detached_transition_failure_does_not_mask_success(
    monkeypatch, _stub_pipeline, caplog
):
    monkeypatch.setattr(
        service.checkrun.ghauth, "installation_token", lambda agent, repo: "ghs_tok"
    )

    def fake_rest(path, *, method=None, body=None, token=None):
        raise ExecError(
            ["gh"], rc=1, stderr="PATCH 403 Resource not accessible by integration"
        )

    monkeypatch.setattr(service.checkrun.gh, "rest", fake_rest)

    with caplog.at_level(logging.WARNING, logger="shipit.review"):
        result = service.run_detached_review(agent_backend.CODEX, TARGET, run_id=555)

    assert _stub_pipeline["called"] is True
    assert result["post"] == {"id": 99}
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "transition" in text.lower()


def test_run_detached_transition_failure_on_error_path_still_raises(
    monkeypatch, _stub_pipeline
):
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
        service.run_detached_review(agent_backend.CODEX, TARGET, run_id=555)


def test_run_detached_no_transition_when_no_run_id(monkeypatch, _stub_pipeline):
    calls = _fake_checkrun_boundary(monkeypatch)

    result = service.run_detached_review(agent_backend.CODEX, TARGET, run_id=None)

    assert not [c for c in calls if c["method"] == "PATCH"]
    assert _stub_pipeline["called"] is True
    assert result["post"] == {"id": 99}


def test_run_detached_close_never_leaks_token(monkeypatch, _stub_pipeline, caplog):
    secret = "ghs_leakCanary000111222333"

    def fake_rest(path, *, method=None, body=None, token=None):
        raise ExecError(["gh"], rc=1, stderr="transition failed")

    monkeypatch.setattr(
        service.checkrun.ghauth, "installation_token", lambda agent, repo: secret
    )
    monkeypatch.setattr(service.checkrun.gh, "rest", fake_rest)

    with caplog.at_level(logging.DEBUG, logger="shipit.review"):
        service.run_detached_review(agent_backend.CODEX, TARGET, run_id=555)

    full = "\n".join(r.getMessage() for r in caplog.records)
    assert secret not in full
    assert _stub_pipeline["called"] is True


@pytest.fixture
def _restore_shipit_logger():
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
    from shipit import logsetup

    _fake_checkrun_boundary(monkeypatch)
    attached = logsetup.configure_logging_for_slug("owner/repo", base_dir=tmp_path)
    assert attached is True

    service.run_detached_review(agent_backend.CODEX, TARGET, run_id=555)

    for handler in logging.getLogger("shipit").handlers:
        handler.flush()
    log_file = tmp_path / "owner" / "repo" / "shipit.log"
    assert log_file.exists()
    contents = log_file.read_text()
    assert "child start" in contents
    assert "child done" in contents
    assert "resolved" in contents
    assert "completed/success" in contents


def test_configure_logging_for_slug_is_best_effort_on_bad_slug(tmp_path):
    from shipit import logsetup

    assert logsetup.configure_logging_for_slug("not-a-slug", base_dir=tmp_path) is False


def test_run_detached_resolve_failure_closes_run_failed_and_reraises(monkeypatch):
    calls = _fake_checkrun_boundary(monkeypatch)

    def boom_resolve(pr, repo=None):
        raise ExecError(["gh"], rc=1, stderr="could not fetch PR diff for #5")

    monkeypatch.setattr(service, "resolve_pr", boom_resolve)

    with pytest.raises(ExecError, match="could not fetch PR diff"):
        service.run_detached_review(agent_backend.CODEX, TARGET, run_id=555)

    assert not [c for c in calls if c["method"] == "POST"]
    patches = [c for c in calls if c["method"] == "PATCH"]
    assert len(patches) == 1
    assert patches[0]["path"] == "/repos/owner/repo/check-runs/555"
    assert patches[0]["body"]["status"] == "completed"
    assert patches[0]["body"]["conclusion"] == "failure"


def test_run_detached_resolve_failure_with_no_run_id_just_reraises(monkeypatch):
    calls = _fake_checkrun_boundary(monkeypatch)

    def boom_resolve(pr, repo=None):
        raise ExecError(["gh"], rc=1, stderr="could not fetch PR diff")

    monkeypatch.setattr(service, "resolve_pr", boom_resolve)

    with pytest.raises(ExecError):
        service.run_detached_review(agent_backend.CODEX, TARGET, run_id=None)

    assert not [c for c in calls if c["method"] == "PATCH"]


def test_run_detached_resolve_guard_does_not_overwrite_timeout_close(
    monkeypatch, _stub_pipeline
):
    from shipit.review.backends.base import _TIMEOUT_MARKER

    calls = _fake_checkrun_boundary(monkeypatch)

    def _timed(agent, ctx, **kw):
        raise BackendError(
            "codex timed out before returning a complete review\n"
            f"raw output: …{_TIMEOUT_MARKER}"
        )

    monkeypatch.setattr(service, "generate_review", _timed)

    with pytest.raises(BackendError):
        service.run_detached_review(agent_backend.CODEX, TARGET, run_id=555)

    patches = [c for c in calls if c["method"] == "PATCH"]
    assert len(patches) == 1
    assert patches[0]["body"]["conclusion"] == "timed_out"


def test_run_detached_resolve_guard_does_not_overwrite_empty_close(
    monkeypatch, _stub_pipeline
):
    calls = _fake_checkrun_boundary(monkeypatch)

    def _empty(agent, ctx, **kw):
        raise BackendError("no parseable JSON\nraw output: <not json>")

    monkeypatch.setattr(service, "generate_review", _empty)

    with pytest.raises(BackendError):
        service.run_detached_review(agent_backend.CODEX, TARGET, run_id=555)

    patches = [c for c in calls if c["method"] == "PATCH"]
    assert len(patches) == 1
    assert patches[0]["body"]["conclusion"] in {"failure", "neutral"}
    output = patches[0]["body"]["output"]
    assert "empty" in (output["title"] + output["summary"]).lower()


def test_start_detached_reconciles_against_existing_inflight_run(monkeypatch):
    calls = _fake_checkrun_boundary(monkeypatch)
    monkeypatch.setattr(service, "_resolve_head_sha", lambda pr: "deadbeef")

    spawned: list = []
    rc = service.start_detached_review(
        agent_backend.CODEX,
        TARGET,
        spawn=lambda argv, env: spawned.append(list(argv)),
        find=lambda agent, repo, head_sha: 999,
    )

    assert rc is False
    assert spawned == []
    assert not [c for c in calls if c["method"] in {"POST", "PATCH"}]


def test_start_detached_no_inflight_run_creates_and_spawns(monkeypatch):
    calls = _fake_checkrun_boundary(monkeypatch)
    monkeypatch.setattr(service, "_resolve_head_sha", lambda pr: "deadbeef")

    spawned: list = []
    rc = service.start_detached_review(
        agent_backend.CODEX,
        TARGET,
        spawn=lambda argv, env: spawned.append(list(argv)),
        find=lambda agent, repo, head_sha: None,
    )

    assert rc is True
    assert len(spawned) == 1
    posts = [c for c in calls if c["method"] == "POST"]
    assert len(posts) == 1
    assert posts[0]["body"]["status"] == "in_progress"


def test_start_detached_reconcile_lookup_failure_proceeds_to_spawn(monkeypatch, caplog):
    calls = _fake_checkrun_boundary(monkeypatch)
    monkeypatch.setattr(service, "_resolve_head_sha", lambda pr: "deadbeef")

    def boom_find(agent, repo, head_sha):
        raise ExecError(
            ["gh"], rc=1, stderr="403 Resource not accessible by integration"
        )

    spawned: list = []
    with caplog.at_level(logging.WARNING, logger="shipit.review"):
        rc = service.start_detached_review(
            agent_backend.CODEX,
            TARGET,
            spawn=lambda argv, env: spawned.append(list(argv)),
            find=boom_find,
        )

    assert rc is True
    assert len(spawned) == 1
    assert [c for c in calls if c["method"] == "POST"]
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "reconcile" in text.lower()


def test_unknown_outcome_falls_back_to_failed_without_crashing(monkeypatch, caplog):
    calls = _fake_checkrun_boundary(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="shipit.review"):
        service._close_funnel_breadcrumb(
            agent_backend.CODEX, "owner/repo", 555, outcome="bogus-outcome"
        )

    patches = [c for c in calls if c["method"] == "PATCH"]
    assert len(patches) == 1
    assert patches[0]["body"]["conclusion"] == "failure"
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "unknown funnel outcome" in text.lower()


def test_child_argv_carries_the_fanout_config_when_set(monkeypatch):
    from shipit.review.calibrator import CalibratorConfig

    _fake_checkrun_boundary(monkeypatch)
    monkeypatch.setattr(service, "_resolve_head_sha", lambda pr: "deadbeef")
    spawned: list = []
    service.start_detached_review(
        agent_backend.CODEX,
        TARGET,
        dimensions=("correctness", "test-quality"),
        calibrator=CalibratorConfig(
            backend="claude", model="opus-x", reasoning="medium", timeout="120s"
        ),
        nit_cap=0,
        spawn=lambda argv, env: spawned.append(list(argv)),
    )
    argv = spawned[0]
    assert argv[argv.index("--dimensions") + 1] == "correctness,test-quality"
    assert argv[argv.index("--nit-cap") + 1] == "0"
    assert argv[argv.index("--calibrator-backend") + 1] == "claude"
    assert argv[argv.index("--calibrator-model") + 1] == "opus-x"
    assert argv[argv.index("--calibrator-reasoning") + 1] == "medium"
    assert argv[argv.index("--calibrator-timeout") + 1] == "120s"


def test_child_argv_omits_fanout_flags_at_the_shipped_defaults(monkeypatch):
    _fake_checkrun_boundary(monkeypatch)
    monkeypatch.setattr(service, "_resolve_head_sha", lambda pr: "deadbeef")
    spawned: list = []
    service.start_detached_review(
        agent_backend.CODEX, TARGET, spawn=lambda argv, env: spawned.append(list(argv))
    )
    argv = spawned[0]
    for flag in ("--dimensions", "--nit-cap", "--calibrator-backend"):
        assert flag not in argv
