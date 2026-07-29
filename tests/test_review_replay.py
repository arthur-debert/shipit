from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from shipit.agent import backend as agent_backend
from shipit.review import producer, replay
from shipit.review.calibrator import CalibratorConfig
from shipit.review.diff import ReviewError
from shipit.spawn.launch import LaunchResult

_VALID = (
    '{"summary": {"status": "COMMENT", "overall_feedback": "ok"}, "comments": '
    '[{"file": "f.txt", "line": 1, "text": "t", "severity": "minor", '
    '"category": "correctness", "confidence": 0.5, "evidence": "e", "fix": ""}]}'
)


def _git(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def checkout(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "remote", "add", "origin", "https://github.com/acme/widget.git")
    (repo / "f.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "first")
    (repo / "f.txt").write_text("one\ntwo\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "second")
    return repo


def test_parse_range_two_dot_and_three_dot():
    assert replay.parse_range("main..feature") == ("main", "feature", False)
    assert replay.parse_range("main...feature") == ("main", "feature", True)


@pytest.mark.parametrize(
    "spec", ["", "main", "..head", "base..", "a..b..c", "a....b", "a.....b"]
)
def test_parse_range_rejects_unusable_specs_with_the_grammar(spec):
    with pytest.raises(ReviewError, match="<base>..<head>"):
        replay.parse_range(spec)


def test_parse_range_keeps_dotted_tag_endpoints():
    assert replay.parse_range("v1.2.3..v1.3.0") == ("v1.2.3", "v1.3.0", False)


def test_resolve_range_resolves_endpoints_diff_and_repo_identity(checkout):
    view = replay.resolve_range("HEAD~1..HEAD", workdir=str(checkout))
    assert view.repo.slug == "acme/widget"
    assert view.base_sha != view.head_sha
    assert "two" in view.diff
    assert view.changed_files == ["f.txt"]
    assert view.workdir == str(checkout)


def test_resolve_range_three_dot_reviews_from_the_merge_base(checkout):
    two_dot = replay.resolve_range("HEAD~1..HEAD", workdir=str(checkout))
    three_dot = replay.resolve_range("HEAD~1...HEAD", workdir=str(checkout))
    assert three_dot.base_sha == two_dot.base_sha
    assert three_dot.head_sha == two_dot.head_sha


def test_unknown_revision_is_a_loud_offline_error(checkout):
    with pytest.raises(ReviewError, match="unknown revision 'no-such-branch'"):
        replay.resolve_range("no-such-branch..HEAD", workdir=str(checkout))


def test_not_a_checkout_is_a_clean_error(tmp_path):
    with pytest.raises(ReviewError, match="not a git checkout"):
        replay.resolve_range("a..b", workdir=str(tmp_path))


def test_checkout_without_origin_cannot_key_a_record(tmp_path):
    repo = tmp_path / "bare"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    with pytest.raises(ReviewError, match="origin"):
        replay.resolve_range("HEAD~1..HEAD", workdir=str(repo))


def test_empty_diff_range_refuses_rather_than_billing_a_model_run(checkout):
    with pytest.raises(ReviewError, match="empty diff"):
        replay.resolve_range("HEAD..HEAD", workdir=str(checkout))


@pytest.fixture
def launcher(monkeypatch):
    monkeypatch.setattr(producer.shutil, "which", lambda binary: f"/usr/bin/{binary}")
    captured: dict = {}

    def _launch(cmd, *, cwd, env, timeout=None):
        captured["cmd"] = cmd
        captured["cwd"] = cwd
        captured["timeout"] = timeout
        return LaunchResult(returncode=0, stdout=_VALID, stderr="")

    captured["launch"] = _launch
    return captured


def test_range_producer_launches_in_the_checkout_with_the_git_diff_task(
    checkout, launcher
):
    view = replay.resolve_range("HEAD~1..HEAD", workdir=str(checkout))
    captured = producer.run_range_review(
        agent_backend.CODEX, view, launcher=launcher["launch"]
    )
    assert captured.review["summary"]["status"] == "COMMENT"
    assert launcher["cwd"] == str(checkout)
    prompt = launcher["cmd"][-1]
    assert f"git diff {view.base_sha}..{view.head_sha}" in prompt
    assert "NO pull request" in prompt
    assert "Do NOT call `gh`" in prompt


def test_run_replay_writes_the_round_record_and_touches_no_pr(
    checkout, launcher, tmp_path
):
    view = replay.resolve_range("HEAD~1..HEAD", workdir=str(checkout))
    result = replay.run_replay(
        agent_backend.CODEX,
        view,
        launcher=launcher["launch"],
        base_dir=tmp_path / "state",
    )
    record_path = result["record_path"]
    [line] = record_path.read_text(encoding="utf-8").splitlines()
    record = json.loads(line)
    assert record["round.pr"] is None
    assert record["round.range"] == {
        "base": str(view.base_sha),
        "head": str(view.head_sha),
    }
    assert record["round.reviewer"] == "codex"
    [finding] = record["round.findings"]
    assert finding["severity"] == "minor"
    assert finding["disposition"] == "post"
    assert record["round.variant"]["content_hash"].startswith("sha256:")
    assert record["round.usage"]["duration_ms"] >= 0
    assert record["round.usage"]["total_tokens"] is None
    assert "review-rounds" in str(record_path)
    assert "acme" in str(record_path) and "widget" in str(record_path)


def test_run_replay_records_cli_reported_usage_on_the_round(
    checkout, launcher, tmp_path
):
    def _launch(cmd, *, cwd, env, timeout=None):
        return LaunchResult(
            returncode=0, stdout=_VALID, stderr="codex\ntokens used\n1,234\n"
        )

    view = replay.resolve_range("HEAD~1..HEAD", workdir=str(checkout))
    result = replay.run_replay(
        agent_backend.CODEX,
        view,
        launcher=_launch,
        base_dir=tmp_path / "state",
    )
    [line] = result["record_path"].read_text(encoding="utf-8").splitlines()
    record = json.loads(line)
    assert record["round.usage"]["total_tokens"] == 1234


def test_run_replay_propagates_a_record_write_failure(
    checkout, launcher, tmp_path, monkeypatch
):
    view = replay.resolve_range("HEAD~1..HEAD", workdir=str(checkout))

    def boom(*args, **kwargs):
        raise OSError("store unwritable")

    monkeypatch.setattr(replay.roundrecord, "record_round", boom)
    with pytest.raises(OSError, match="store unwritable"):
        replay.run_replay(
            agent_backend.CODEX,
            view,
            launcher=launcher["launch"],
            base_dir=tmp_path / "state",
        )


def test_replay_verb_unknown_agent_is_one_clean_error_line(capsys):
    from shipit.verbs.pr import review as review_verb

    rc = review_verb.run_replay(
        "a..b", agent="nope", model="pro", timeout="600s", instructions=None
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("error: unknown review agent 'nope'")


def test_replay_verb_bad_range_is_one_clean_error_line(checkout, capsys, monkeypatch):
    from shipit.verbs.pr import review as review_verb

    monkeypatch.chdir(checkout)
    rc = review_verb.run_replay(
        "nonsense", agent="codex", model="pro", timeout="600s", instructions=None
    )
    assert rc == 1
    assert "error: unusable commit range 'nonsense'" in capsys.readouterr().err


def test_replay_verb_unreadable_instructions_die_before_any_run(capsys, tmp_path):
    from shipit.verbs.pr import review as review_verb

    rc = review_verb.run_replay(
        "a..b",
        agent="codex",
        model="pro",
        timeout="600s",
        instructions=str(tmp_path / "missing.txt"),
    )
    assert rc == 1
    assert "error: cannot read review instructions" in capsys.readouterr().err


def test_replay_verb_bad_timeout_is_one_clean_error_line_before_any_run(capsys):
    from shipit.verbs.pr import review as review_verb

    rc = review_verb.run_replay(
        "a..b", agent="codex", model="pro", timeout="bad", instructions=None
    )
    assert rc == 1
    assert "error: invalid --timeout 'bad'" in capsys.readouterr().err


_CALIBRATION = json.dumps(
    {
        "summary": {"overall_feedback": "judged"},
        "findings": [
            {
                "id": 0,
                "merged": [],
                "severity": "major",
                "disposition": "post",
                "text": "t",
                "evidence": "e",
                "fix": "",
            }
        ],
    }
)


@pytest.fixture
def fanout_launcher(monkeypatch):
    monkeypatch.setattr(producer.shutil, "which", lambda binary: f"/usr/bin/{binary}")
    captured: dict = {"launches": []}

    def _launch(cmd, *, cwd, env, timeout=None):
        captured["launches"].append({"cmd": cmd, "cwd": cwd, "timeout": timeout})
        stdout = _CALIBRATION if cmd[0] == "claude" else _VALID
        return LaunchResult(returncode=0, stdout=stdout, stderr="")

    captured["launch"] = _launch
    return captured


def test_run_fanout_replay_runs_every_pass_offline_and_writes_the_record(
    checkout, fanout_launcher, tmp_path
):
    view = replay.resolve_range("HEAD~1..HEAD", workdir=str(checkout))
    result = replay.run_fanout_replay(
        agent_backend.CODEX,
        view,
        launcher=fanout_launcher["launch"],
        base_dir=tmp_path / "state",
    )

    launches = fanout_launcher["launches"]
    assert len(launches) == 4
    for launch in launches:
        assert launch["cwd"] == str(checkout)
        prompt = launch["cmd"][-1]
        assert f"git diff {view.base_sha}..{view.head_sha}" in prompt
        assert "DIMENSION FOCUS" in prompt
        assert "gh pr diff" not in prompt

    [line] = result["record_path"].read_text(encoding="utf-8").splitlines()
    record = json.loads(line)
    assert record["round.pr"] is None
    assert record["round.range"] == {
        "base": str(view.base_sha),
        "head": str(view.head_sha),
    }
    assert record["round.reviewer"] == "codex"
    runs = record["round.runs"]
    assert [run["kind"] for run in runs] == ["dimension-pass"] * 4
    assert sorted(run["dimension"] for run in runs) == sorted(
        ["correctness", "cross-file-invariants", "security-robustness", "test-quality"]
    )
    assert all(run["variant"]["content_hash"].startswith("sha256:") for run in runs)
    posted = [
        f
        for f in record["round.findings"]
        if f["disposition"] == "post" and f["duplicate_of"] is None
    ]
    assert len(posted) == 1
    assert len(record["round.findings"]) == 4
    assert "review-rounds" in str(result["record_path"])


def test_run_fanout_replay_round_variant_folds_the_dimension_set(
    checkout, fanout_launcher, tmp_path
):
    from shipit.harness.eval.variant import variant_of
    from shipit.review.dimensions import fanout_variant_text
    from shipit.review.instructions import load_instructions

    view = replay.resolve_range("HEAD~1..HEAD", workdir=str(checkout))
    result = replay.run_fanout_replay(
        agent_backend.CODEX,
        view,
        dimensions=["correctness"],
        launcher=fanout_launcher["launch"],
        base_dir=tmp_path / "state",
    )
    [line] = result["record_path"].read_text(encoding="utf-8").splitlines()
    record = json.loads(line)
    instructions = load_instructions(None)
    expected = variant_of(fanout_variant_text(instructions, ["correctness"]))
    assert record["round.variant"]["content_hash"] == expected.content_hash
    assert expected.content_hash != variant_of(instructions).content_hash


def test_run_fanout_replay_round_variant_folds_invocation_overrides(
    checkout, fanout_launcher, tmp_path
):
    from shipit.harness.eval.variant import variant_of
    from shipit.review.dimensions import fanout_variant_text
    from shipit.review.instructions import load_instructions

    view = replay.resolve_range("HEAD~1..HEAD", workdir=str(checkout))
    overrides = {"correctness": {"model": "o3"}}
    result = replay.run_fanout_replay(
        agent_backend.CODEX,
        view,
        dimensions=["correctness"],
        invocation_overrides=overrides,
        launcher=fanout_launcher["launch"],
        base_dir=tmp_path / "state",
    )
    [line] = result["record_path"].read_text(encoding="utf-8").splitlines()
    record = json.loads(line)
    instructions = load_instructions(None)
    expected = variant_of(fanout_variant_text(instructions, ["correctness"], overrides))
    assert record["round.variant"]["content_hash"] == expected.content_hash
    plain = variant_of(fanout_variant_text(instructions, ["correctness"]))
    assert expected.content_hash != plain.content_hash


def test_run_fanout_replay_sums_cli_reported_usage_onto_the_round(
    checkout, monkeypatch, tmp_path
):
    monkeypatch.setattr(producer.shutil, "which", lambda binary: f"/usr/bin/{binary}")

    def _launch(cmd, *, cwd, env, timeout=None):
        return LaunchResult(
            returncode=0, stdout=_VALID, stderr="codex\ntokens used\n1,234\n"
        )

    view = replay.resolve_range("HEAD~1..HEAD", workdir=str(checkout))
    result = replay.run_fanout_replay(
        agent_backend.CODEX,
        view,
        launcher=_launch,
        base_dir=tmp_path / "state",
    )

    [line] = result["record_path"].read_text(encoding="utf-8").splitlines()
    record = json.loads(line)
    assert record["round.usage"]["total_tokens"] == 4 * 1234


def test_run_fanout_replay_single_pass_flags_apply_unchanged(
    checkout, fanout_launcher, tmp_path, monkeypatch
):
    from shipit.harness.eval.variant import VARIANT_LABEL_ENV

    monkeypatch.setenv(VARIANT_LABEL_ENV, "arm-B")
    instructions = tmp_path / "custom.txt"
    instructions.write_text("REVIEW ONLY THE SPACING", encoding="utf-8")
    view = replay.resolve_range("HEAD~1..HEAD", workdir=str(checkout))
    result = replay.run_fanout_replay(
        agent_backend.CODEX,
        view,
        model="high",
        timeout="120s",
        instructions_path=str(instructions),
        dimensions=("correctness",),
        launcher=fanout_launcher["launch"],
        base_dir=tmp_path / "state",
    )
    [launch] = fanout_launcher["launches"]
    assert "REVIEW ONLY THE SPACING" in launch["cmd"][-1]
    assert launch["timeout"] == 120.0
    [line] = result["record_path"].read_text(encoding="utf-8").splitlines()
    record = json.loads(line)
    assert record["round.invocation"] == {
        "model": "high",
        "timeout": "120s",
        "instructions_path": str(instructions),
    }
    assert record["round.variant"]["label"] == "arm-B"
    [run] = record["round.runs"]
    assert run["model"] == "high"
    assert run["variant"]["label"] == "arm-B"


def test_run_fanout_replay_calibrator_runs_offline_with_provisioned_agent_defs(
    checkout, fanout_launcher, tmp_path
):
    view = replay.resolve_range("HEAD~1..HEAD", workdir=str(checkout))
    result = replay.run_fanout_replay(
        agent_backend.CODEX,
        view,
        dimensions=("correctness",),
        calibrator=CalibratorConfig(),
        launcher=fanout_launcher["launch"],
        base_dir=tmp_path / "state",
    )
    assert (checkout / ".claude" / "agents" / "reviewer.md").is_file()
    assert not (checkout / ".agents" / "agents" / "reviewer" / "agent.md").exists()
    [judge] = [
        entry for entry in fanout_launcher["launches"] if entry["cmd"][0] == "claude"
    ]
    assert judge["cwd"] == str(checkout)
    task = judge["cmd"][2]
    assert f"git diff {view.base_sha}..{view.head_sha}" in task
    assert "gh pr diff" not in task
    [line] = result["record_path"].read_text(encoding="utf-8").splitlines()
    record = json.loads(line)
    kinds = [run["kind"] for run in record["round.runs"]]
    assert kinds == ["dimension-pass", "calibrator"]


def test_run_fanout_replay_provisioning_failure_is_a_clean_review_error(
    checkout, fanout_launcher, tmp_path, monkeypatch
):
    def boom(workdir):
        raise OSError("read-only file system")

    monkeypatch.setattr(replay, "provision_agent_defs", boom)
    view = replay.resolve_range("HEAD~1..HEAD", workdir=str(checkout))
    with pytest.raises(ReviewError, match=r"drop the `--calibrator-\*` options"):
        replay.run_fanout_replay(
            agent_backend.CODEX,
            view,
            dimensions=("correctness",),
            calibrator=CalibratorConfig(),
            launcher=fanout_launcher["launch"],
            base_dir=tmp_path / "state",
        )


def test_provision_agent_defs_writes_missing_and_never_clobbers(checkout):
    agents_dir = checkout / ".claude" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "reviewer.md").write_text("MY EDITED ARM", encoding="utf-8")

    written = replay.provision_agent_defs(str(checkout))

    assert (agents_dir / "reviewer.md").read_text(encoding="utf-8") == "MY EDITED ARM"
    assert (agents_dir / "reviewer.md") not in written
    names = {path.name for path in written}
    assert "implementer.md" in names
    assert all(path.is_file() for path in written)
    assert replay.provision_agent_defs(str(checkout)) == []


def test_provision_agent_defs_refuses_symlinked_component(checkout, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (checkout / ".claude").symlink_to(outside, target_is_directory=True)

    written = replay.provision_agent_defs(str(checkout))

    assert list(outside.iterdir()) == []
    assert not any(".claude" in path.parts for path in written)


def test_run_replay_uncalibrated_does_not_provision_defs(checkout, launcher, tmp_path):
    claude_def = checkout / ".claude" / "agents" / "reviewer.md"
    assert not claude_def.exists()

    view = replay.resolve_range("HEAD~1..HEAD", workdir=str(checkout))
    replay.run_replay(
        agent_backend.ANTIGRAVITY,
        view,
        launcher=launcher["launch"],
        base_dir=tmp_path / "state",
    )

    assert not claude_def.exists()


def test_run_fanout_replay_uncalibrated_does_not_provision_defs(
    checkout, fanout_launcher, tmp_path
):
    claude_def = checkout / ".claude" / "agents" / "reviewer.md"
    assert not claude_def.exists()

    view = replay.resolve_range("HEAD~1..HEAD", workdir=str(checkout))
    replay.run_fanout_replay(
        agent_backend.ANTIGRAVITY,
        view,
        dimensions=("correctness",),
        launcher=fanout_launcher["launch"],
        base_dir=tmp_path / "state",
    )

    assert not claude_def.exists()


def test_provision_agent_defs_nested_symlink_aborts_the_tree_fail_closed(
    checkout, tmp_path
):
    outside = tmp_path / "outside"
    outside.mkdir()
    (checkout / ".claude").mkdir(parents=True)
    (checkout / ".claude" / "agents").symlink_to(outside, target_is_directory=True)

    written = replay.provision_agent_defs(str(checkout))

    assert list(outside.iterdir()) == []
    assert not any(".claude" in path.parts for path in written)
    assert replay.provision_agent_defs(str(checkout)) == []


def test_replay_verb_fanout_flags_require_fanout(capsys):
    from shipit.verbs.pr import review as review_verb

    rc = review_verb.run_replay(
        "a..b",
        agent="codex",
        model="pro",
        timeout="600s",
        instructions=None,
        dimensions="correctness",
    )
    assert rc == 1
    assert "error: --dimensions and --calibrator-*" in capsys.readouterr().err


def test_replay_verb_calibrator_flag_requires_fanout(capsys):
    from shipit.verbs.pr import review as review_verb

    rc = review_verb.run_replay(
        "a..b",
        agent="codex",
        model="pro",
        timeout="600s",
        instructions=None,
        calibrator_backend="claude",
    )
    assert rc == 1
    assert "error: --dimensions and --calibrator-*" in capsys.readouterr().err


def test_replay_verb_unknown_dimension_dies_before_any_run(capsys):
    from shipit.verbs.pr import review as review_verb

    rc = review_verb.run_replay(
        "a..b",
        agent="codex",
        model="pro",
        timeout="600s",
        instructions=None,
        fanout=True,
        dimensions="correctness,nope",
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("error: unknown review dimension 'nope'")
    assert "correctness" in err


def test_replay_verb_bad_calibrator_options_die_before_any_run(capsys):
    from shipit.verbs.pr import review as review_verb

    rc = review_verb.run_replay(
        "a..b",
        agent="codex",
        model="pro",
        timeout="600s",
        instructions=None,
        fanout=True,
        calibrator_backend="nope",
    )
    assert rc == 1
    assert "error: invalid --calibrator-* options" in capsys.readouterr().err


def test_replay_verb_fanout_end_to_end(
    checkout, fanout_launcher, tmp_path, capsys, monkeypatch
):
    from shipit.verbs.pr import review as review_verb

    monkeypatch.chdir(checkout)
    monkeypatch.setattr(
        replay.fanout.producer,
        "run_range_review",
        lambda backend, view, **kw: producer.CapturedReview(
            review={
                "summary": {"status": "COMMENT", "overall_feedback": "ok"},
                "comments": [],
            },
            usage=producer.UNREPORTED,
            reasoning=kw.get("reasoning"),
        ),
    )
    monkeypatch.setattr(
        replay.roundrecord,
        "record_round",
        lambda *a, **kw: tmp_path / "state" / "rounds.jsonl",
    )
    rc = review_verb.run_replay(
        "HEAD~1..HEAD",
        agent="codex",
        model="pro",
        timeout="600s",
        instructions=None,
        fanout=True,
        dimensions="correctness",
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "(fan-out)" in out
    assert "no PR touched" in out


def test_run_replay_persists_the_range_pass_bundle_and_correlation(
    checkout, launcher, tmp_path
):
    view = replay.resolve_range("HEAD~1..HEAD", workdir=str(checkout))
    result = replay.run_replay(
        agent_backend.CODEX,
        view,
        launcher=launcher["launch"],
        base_dir=tmp_path / "state",
    )
    [line] = result["record_path"].read_text(encoding="utf-8").splitlines()
    record = json.loads(line)
    assert record["round.id"]
    artifacts_dir = Path(record["round.artifacts"])
    assert artifacts_dir == (
        tmp_path / "state" / "review-artifacts" / "acme" / "widget" / record["round.id"]
    )
    [run] = record["round.runs"]
    assert run["kind"] == "range-pass"
    assert run["outcome"] == "success"
    assert run["artifacts"] == str(artifacts_dir / run["run_id"])
    bundle = artifacts_dir / run["run_id"]
    assert (
        f"git diff {view.base_sha}..{view.head_sha}"
        in (bundle / "prompt.txt").read_text()
    )
    assert (bundle / "stdout.raw").read_text() == _VALID
    meta = json.loads((bundle / "meta.json").read_text())
    assert meta["outcome"] == "success"
    assert meta["run_id"] == run["run_id"]
    assert meta["exit_code"] == 0
    [finding] = record["round.findings"]
    assert finding["run_id"] == run["run_id"]
