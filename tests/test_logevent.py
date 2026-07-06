"""Tests for the constrained dev-cycle write path (`shipit log event`,
LOG04-WS03 / ADR-0032) and its hook tier.

The acceptance criteria, as external behavior: a registered name lands a
record carrying ``event``, the env-propagated domain keys, and the
branch-derived ``epic``/``ws`` through the REAL logsetup pipeline; an
unregistered name is a clean ``error:`` + exit 1 and writes NOTHING;
``--about`` is honored only for the skill-scripted names, one capped line;
hook context fails OPEN (exit 0) on any emission failure, including an
unwritable log path; and a real ``git commit`` with the managed hook command
wired as ``post-commit`` produces a ``commit.created`` record — while a broken
log path does not block the commit (prior art: the hook verbs' fail-open
tests, ``test_events``' pipeline pattern).
"""

from __future__ import annotations

import json
import logging
import os
import stat
import subprocess
import sys
from importlib import resources
from pathlib import Path

import pytest
import yaml

from shipit import events, logcontext, logsetup
from shipit.identity import Sha, repo_from_slug
from shipit.verbs import logevent

REPO = repo_from_slug("acme/widget")

#: A full, valid commit sha for the commit.created composition tests.
SHA = Sha("a" * 40)


def _configure(tmp_path: Path, env: dict[str, str] | None = None) -> None:
    logsetup.configure_logging(env=env or {}, repo=REPO, base_dir=tmp_path)


def _records(base_dir: Path) -> list[dict]:
    path = logsetup.log_file_path(REPO, base_dir=base_dir)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


# ==========================================================================
# The emit verb — registered names through the real pipeline
# ==========================================================================


def test_registered_name_lands_event_env_keys_and_branch_identity(tmp_path):
    """The whole write path: `event` from the closed vocabulary, `session`
    from the parent-exported env (the seam the CLI root rebinds), `epic`/`ws`
    derived from the work-stream branch, attributed to the verb's logger."""
    _configure(tmp_path, env={"SHIPIT_LOG_CTX_SESSION": "sess-e2e"})

    rc = logevent.run("review.received", branch="RVW01/WS02")

    assert rc == 0
    (record,) = _records(tmp_path)
    assert record["event"] == "review.received"
    assert record["session"] == "sess-e2e"
    assert record["epic"] == "RVW01"
    assert record["ws"] == 2
    assert record["level"] == "info"
    assert record["logger"] == "shipit.logevent"
    # No --about and not skill-scripted: the composed domain phrase.
    assert record["msg"] == "review received"


def test_umbrella_branch_derives_epic_only(tmp_path):
    _configure(tmp_path)
    assert logevent.run("breaker.fired", branch="RVW01/umbrella") == 0
    (record,) = _records(tmp_path)
    assert record["epic"] == "RVW01"
    assert "ws" not in record


def test_umbrella_branch_suppresses_an_env_bound_ws(tmp_path):
    """The umbrella branch is the local truth for the WHOLE identity: it carries
    an epic but no Work Stream, so an env-propagated `ws` must NOT fuse onto it
    into a mixed identity (env epic=OLD01/ws=3 + branch NEW01/umbrella must not
    yield epic=NEW01/ws=3)."""
    _configure(
        tmp_path,
        env={"SHIPIT_LOG_CTX_EPIC": "OLD01", "SHIPIT_LOG_CTX_WS": "3"},
    )
    assert logevent.run("breaker.fired", branch="NEW01/umbrella") == 0
    (record,) = _records(tmp_path)
    assert record["epic"] == "NEW01"
    assert "ws" not in record


@pytest.mark.parametrize(
    "branch",
    ["issues/375/work", "ephemeral/sess-20260703-1234", "main", None],
)
def test_out_of_grammar_branch_adds_no_identity(tmp_path, branch):
    """Standalone-issue, ephemeral, arbitrary, and absent branches derive
    NOTHING — absent identity stays absent on the record, never a placeholder."""
    _configure(tmp_path)
    assert logevent.run("tree.created", branch=branch) == 0
    (record,) = _records(tmp_path)
    assert "epic" not in record
    assert "ws" not in record


def test_env_bound_identity_shows_through_when_branch_derives_nothing(tmp_path):
    """A spawn-seam export (epic/ws in the env) survives an out-of-grammar
    branch: derivation adds nothing, it does not erase."""
    _configure(
        tmp_path,
        env={"SHIPIT_LOG_CTX_EPIC": "LOG04", "SHIPIT_LOG_CTX_WS": "3"},
    )
    assert logevent.run("agent.done", branch="issues/375/work") == 0
    (record,) = _records(tmp_path)
    assert record["epic"] == "LOG04"
    assert record["ws"] == 3


def test_branch_derived_identity_wins_over_env_bound(tmp_path):
    """Where the branch DOES carry identity, the checkout's branch is the
    local truth (the fetch-seam precedent): the derived halves override."""
    _configure(
        tmp_path,
        env={"SHIPIT_LOG_CTX_EPIC": "OLD01", "SHIPIT_LOG_CTX_WS": "9"},
    )
    assert logevent.run("agent.done", branch="NEW01/WS05") == 0
    (record,) = _records(tmp_path)
    assert record["epic"] == "NEW01"
    assert record["ws"] == 5


def test_branch_binding_is_scoped_to_the_emission(tmp_path):
    """The derived identity unwinds after the emission — a later in-process
    record does not inherit it (logcontext.scoped, not process-lifetime bind)."""
    _configure(tmp_path)
    assert logevent.run("tree.created", branch="RVW01/WS02") == 0
    assert "epic" not in logcontext.bound()
    assert "ws" not in logcontext.bound()


# ==========================================================================
# The emit verb — the closed-vocabulary gate
# ==========================================================================


def test_unknown_name_is_a_clean_error_and_writes_nothing(tmp_path, capsys):
    _configure(tmp_path)
    rc = logevent.run("made.up.name")
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("error: unknown dev-cycle event 'made.up.name'")
    assert _records(tmp_path) == []


def test_unknown_name_stays_loud_even_from_a_hook(tmp_path, capsys):
    """Fail-open covers the log PATH, not the vocabulary: a typo in hook
    wiring is a config bug to surface (and post-commit cannot block anyway)."""
    _configure(tmp_path)
    rc = logevent.run("made.up.name", from_hook=True)
    assert rc == 1
    assert "error: unknown dev-cycle event" in capsys.readouterr().err
    assert _records(tmp_path) == []


# ==========================================================================
# The emit verb — --about and msg composition
# ==========================================================================


def test_about_is_the_msg_for_skill_scripted_events(tmp_path):
    _configure(tmp_path)
    rc = logevent.run("session.intent", about="planning session: reviewer symmetry")
    assert rc == 0
    (record,) = _records(tmp_path)
    assert record["event"] == "session.intent"
    assert record["msg"] == "planning session: reviewer symmetry"


def test_about_is_capped_to_one_short_line(tmp_path):
    _configure(tmp_path)
    long_tail = "x" * 500
    rc = logevent.run(
        "planning.adr.written", about=f"first line {long_tail}\nsecond line"
    )
    assert rc == 0
    (record,) = _records(tmp_path)
    assert "second line" not in record["msg"]
    assert len(record["msg"]) == logevent.ABOUT_MAX_CHARS
    assert record["msg"].startswith("first line ")


def test_about_is_ignored_for_non_skill_scripted_events(tmp_path):
    """A hook- or verb-tier name composes its own msg — the freeform slot is
    exactly as wide as the skill-scripted tier that needs it (ADR-0032)."""
    _configure(tmp_path)
    rc = logevent.run("commit.created", about="dear diary, today I committed")
    assert rc == 0
    (record,) = _records(tmp_path)
    assert record["msg"] == "commit created"
    assert "diary" not in record["msg"]


def test_commit_created_records_the_head_sha(tmp_path):
    _configure(tmp_path)
    rc = logevent.run("commit.created", commit=SHA)
    assert rc == 0
    (record,) = _records(tmp_path)
    assert record["event"] == "commit.created"
    assert record["msg"] == f"commit created {'a' * 12}"
    assert record["sha"] == "a" * 40


def test_skill_scripted_registry_is_a_subset_of_the_vocabulary():
    assert events.SKILL_SCRIPTED_NAMES <= events.EVENT_NAMES


# ==========================================================================
# The emit verb — fail-open posture (prior art: the hook verbs' fail-open tests)
# ==========================================================================


def test_emission_failure_from_hook_fails_open(tmp_path, monkeypatch, capsys):
    _configure(tmp_path)

    def boom(*args, **kwargs):
        raise RuntimeError("pipeline exploded")

    monkeypatch.setattr(logevent.events, "emit", boom)
    rc = logevent.run("commit.created", from_hook=True)
    assert rc == 0
    # No `error:` contract line — the swallow surfaces as a WARNING record
    # (the hook fail-open canon), never as a failure the wiring could act on.
    err_lines = capsys.readouterr().err.splitlines()
    assert not any(line.startswith("error:") for line in err_lines)


def test_emission_failure_without_hook_context_is_loud(tmp_path, monkeypatch, capsys):
    _configure(tmp_path)

    def boom(*args, **kwargs):
        raise RuntimeError("pipeline exploded")

    monkeypatch.setattr(logevent.events, "emit", boom)
    rc = logevent.run("commit.created")
    assert rc == 1
    assert "error: pipeline exploded" in capsys.readouterr().err


@pytest.fixture()
def read_only_dir(tmp_path):
    """A directory the test cannot create children under (restored on exit)."""
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        yield ro
    finally:
        ro.chmod(stat.S_IRWXU)


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="permission bits do not bind as root",
)
def test_unwritable_log_plus_hook_context_exits_zero(read_only_dir):
    """The acceptance arm: an unopenable per-repo log degrades logging setup
    to console-only (WARNING, no crash) and the hook-context emission still
    exits 0 — a broken log path never blocks git."""
    logsetup.configure_logging(env={}, repo=REPO, base_dir=read_only_dir)
    logger = logging.getLogger(logsetup.LOGGER_NAME)
    assert not any(h.name == "shipit-file" for h in logger.handlers), (
        "file sink must be skipped, not crash setup"
    )
    assert logevent.run("commit.created", from_hook=True, branch="LOG04/WS03") == 0


# ==========================================================================
# The managed hook tier — lefthook wiring + end-to-end commit witness
# ==========================================================================


def test_managed_lefthook_config_wires_the_post_commit_emission():
    """The packaged lefthook caller carries the hook tier: a post-commit
    entry invoking the constrained verb with the fail-open flag, through the
    same pinned `-e lint` env as the lint caller. It rides the PINNED launcher
    `./bin/shipit` (#481, ADR-0033), and is fail-open-guarded against a
    pixi-less environment (#482)."""
    raw = resources.files("shipit.data").joinpath("lefthook.yml").read_bytes()
    config = yaml.safe_load(raw)
    entry = config["post-commit"]["commands"]["dev-cycle-event"]
    guard = (
        'command -v pixi >/dev/null 2>&1 || { echo "shipit: pixi not on PATH — '
        "skipping this managed hook (pixi-less environment; the full gate runs "
        'wherever pixi is provisioned)."; exit 0; }; '
    )
    assert entry["run"] == (
        guard + "pixi run -e lint ./bin/shipit log event commit.created --from-hook"
    )


def _init_repo(root: Path) -> None:
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)

    git("init")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    git("config", "commit.gpgsign", "false")
    # Pin the hooks path so a developer's global core.hooksPath cannot bypass
    # the hook under test.
    git("config", "core.hooksPath", ".git/hooks")
    git("remote", "add", "origin", "https://github.com/acme/widget.git")
    git("checkout", "-b", "ACME/WS07")


def _install_post_commit_hook(root: Path, rc_file: Path) -> None:
    """The managed entry's command, minus the pixi indirection: the same
    `shipit log event commit.created --from-hook` invocation, run through this
    test env's interpreter (the pixi layer is provisioning, not behavior)."""
    hook = root / ".git" / "hooks" / "post-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(
        "#!/bin/sh\n"
        f'"{sys.executable}" -m shipit log event commit.created --from-hook\n'
        f'echo $? > "{rc_file}"\n'
    )
    hook.chmod(0o755)


def _hook_env(home: Path) -> dict[str, str]:
    env = {
        k: v for k, v in os.environ.items() if not k.startswith(logcontext.ENV_PREFIX)
    }
    env.pop("GITHUB_STEP_SUMMARY", None)
    # Point every platformdirs base into the fake home so the per-repo log
    # lands (or fails) under the test's control on any platform.
    env["HOME"] = str(home)
    env["XDG_STATE_HOME"] = str(home / "state")
    env["SHIPIT_LOG_CTX_SESSION"] = "e2e-sess"
    return env


def _commit(root: Path, env: dict[str, str], filename: str) -> str:
    (root / filename).write_text("content\n")
    subprocess.run(["git", "add", filename], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", f"add {filename}"],
        cwd=root,
        check=True,
        capture_output=True,
        env=env,
    )
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return head.stdout.strip()


def test_commit_produces_commit_created_record_end_to_end(tmp_path):
    """The hook tier, for real: a `git commit` on an EPIC/WSnn branch fires
    the post-commit emission, and the durable record carries the event, the
    session from the env, the branch-derived epic/ws, and the new HEAD sha."""
    root = tmp_path / "repo"
    root.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    _init_repo(root)
    rc_file = tmp_path / "hook-rc"
    _install_post_commit_hook(root, rc_file)

    sha = _commit(root, _hook_env(home), "file.txt")

    assert rc_file.read_text().strip() == "0"
    logs = list(home.rglob("shipit.log"))
    assert logs, "the per-repo durable log was not written under the fake home"
    (log_path,) = logs
    assert log_path.parent.name == "widget"  # per-repo: <base>/acme/widget/
    records = [json.loads(line) for line in log_path.read_text().splitlines() if line]
    (record,) = [r for r in records if r.get("event") == "commit.created"]
    assert record["session"] == "e2e-sess"
    assert record["epic"] == "ACME"
    assert record["ws"] == 7
    assert record["sha"] == sha
    assert record["msg"] == f"commit created {sha[:12]}"


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="permission bits do not bind as root",
)
def test_broken_log_path_does_not_block_the_commit(tmp_path):
    """The fail-open acceptance arm, end to end: with the log home unwritable
    the commit still lands and the hook command still exits 0."""
    root = tmp_path / "repo"
    root.mkdir()
    home = tmp_path / "ro-home"
    home.mkdir()
    _init_repo(root)
    rc_file = tmp_path / "hook-rc"
    _install_post_commit_hook(root, rc_file)

    home.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        sha = _commit(root, _hook_env(home), "file.txt")
    finally:
        home.chmod(stat.S_IRWXU)

    assert sha  # the commit exists — logging never blocked git
    assert rc_file.read_text().strip() == "0"
    assert list(home.rglob("shipit.log")) == []
