"""Behavioural tests for the domain-key log context (LOG01-WS03, ADR-0029).

Two halves, per the acceptance criteria:

* **In-process binding** — a key bound once appears on every subsequent JSONL
  record through the REAL logsetup pipeline; an unbound key is ABSENT from the
  record, never ``null``; bind/unbind round-trip; the key set is closed.
* **Cross-process propagation** — bound keys export to a child environment as
  ``SHIPIT_LOG_CTX_*`` and rebind from it at the child's logging setup
  (``configure_logging``), with numeric keys (``pr``/``run``) surviving the
  string-typed environment as ints, so ``jq 'select(.pr==231)'`` matches
  records from parent and child alike.

The platformdirs base is injected (``base_dir``) so nothing writes a real
``$HOME``; the environment is always passed explicitly so nothing reads the
runner's. Context isolation between tests comes from the shared autouse
``_clean_domain_key_context`` fixture (conftest).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from shipit import logcontext, logsetup
from shipit.identity import repo_from_slug

REPO = repo_from_slug("acme/widget")


@pytest.fixture(autouse=True)
def _reset_package_logger():
    """Fully reset the process-lifetime ``shipit`` logger around each test (the
    same isolation ``test_logsetup`` uses), so a configured file sink from one
    test never leaks into the next."""
    logger = logging.getLogger(logsetup.LOGGER_NAME)
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


def _records(base_dir: Path) -> list[dict]:
    """Every JSONL record the file sink wrote under the injected base."""
    path = logsetup.log_file_path(REPO, base_dir=base_dir)
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _emit(message: str) -> None:
    logging.getLogger(logsetup.LOGGER_NAME).info(message)


# ==========================================================================
# In-process: bound keys on every subsequent record; unbound keys absent
# ==========================================================================


def test_bound_keys_land_on_every_subsequent_record(tmp_path):
    """A key bound once (the CLI-entry pattern) appears on EVERY record emitted
    after it — through the real pipeline, foreign stdlib call sites included."""
    logsetup.configure_logging(env={}, repo=REPO, base_dir=tmp_path)
    logcontext.bind(repo="acme/widget", pr=231)

    _emit("first")
    _emit("second")

    records = _records(tmp_path)
    assert len(records) == 2
    for record in records:
        assert record["repo"] == "acme/widget"
        assert record["pr"] == 231  # an int on the record — jq `.pr==231` matches


def test_unbound_keys_are_absent_not_null(tmp_path):
    """The absent-not-null contract: a record carries ONLY the bound keys — no
    ``"session": null`` placeholders for the rest of the domain set."""
    logsetup.configure_logging(env={}, repo=REPO, base_dir=tmp_path)
    logcontext.bind(pr=7)

    _emit("hello")

    (record,) = _records(tmp_path)
    assert record["pr"] == 7
    for name in ("session", "tree", "run", "repo"):
        assert name not in record


def test_unbind_removes_the_key_from_later_records(tmp_path):
    logsetup.configure_logging(env={}, repo=REPO, base_dir=tmp_path)
    logcontext.bind(pr=7, repo="acme/widget")

    _emit("while-bound")
    logcontext.unbind("pr")
    _emit("after-unbind")

    while_bound, after = _records(tmp_path)
    assert while_bound["pr"] == 7
    assert "pr" not in after  # absent again, not null
    assert after["repo"] == "acme/widget"  # the other key survives the unbind


def test_bind_drops_none_values():
    """A seam can pass a maybe-known value (``run=run_id``) without guarding:
    ``None`` never binds, so the key stays ABSENT rather than null."""
    logcontext.bind(pr=5, run=None)
    assert logcontext.bound() == {"pr": 5}


def test_the_key_set_is_closed():
    """A typo can never mint new correlation vocabulary: unknown names fail loud
    at the bind/unbind/export site."""
    with pytest.raises(ValueError, match="unknown domain key"):
        logcontext.bind(sesion="oops")
    with pytest.raises(ValueError, match="unknown domain key"):
        logcontext.unbind("sesion")
    with pytest.raises(ValueError, match="unknown domain key"):
        logcontext.env_export({}, sesion="oops")


def test_bound_reports_only_domain_keys():
    """A non-domain contextvar someone bound through structlog directly is not
    correlation vocabulary — it is neither reported nor exported."""
    import structlog

    structlog.contextvars.bind_contextvars(request_id="not-a-domain-key")
    logcontext.bind(tree="/trees/x")
    assert logcontext.bound() == {"tree": "/trees/x"}
    assert "SHIPIT_LOG_CTX_REQUEST_ID" not in logcontext.env_export({})


# ==========================================================================
# Cross-process: env export + rebind round-trip
# ==========================================================================


def test_env_export_carries_bound_keys_without_mutating_the_input():
    logcontext.bind(pr=231, repo="acme/widget")
    base = {"PATH": "/usr/bin"}

    child_env = logcontext.env_export(base)

    assert child_env["SHIPIT_LOG_CTX_PR"] == "231"
    assert child_env["SHIPIT_LOG_CTX_REPO"] == "acme/widget"
    assert child_env["PATH"] == "/usr/bin"  # merged over a COPY of the base
    assert base == {"PATH": "/usr/bin"}  # the input mapping is never mutated
    # Unbound keys export nothing — absence crosses the boundary too.
    assert "SHIPIT_LOG_CTX_SESSION" not in child_env


def test_env_export_extra_reaches_the_child_without_binding_the_parent():
    """The detach seam threads ``run=run_id`` to the CHILD only: it exports, but
    the parent's own context stays unbound (the run is the child's story)."""
    logcontext.bind(pr=5)

    child_env = logcontext.env_export({}, run=555, session=None)

    assert child_env["SHIPIT_LOG_CTX_RUN"] == "555"
    assert "SHIPIT_LOG_CTX_SESSION" not in child_env  # None extras drop like bind
    assert logcontext.bound() == {"pr": 5}  # the extra never bound here


def test_env_export_scrubs_inherited_ctx_vars_for_unbound_keys():
    """A stale inherited SHIPIT_LOG_CTX_* entry never leaks into the child.

    The regression the reviewers flagged: this process may itself have been
    spawned with an env_export environment (so ``env`` carries e.g.
    ``SHIPIT_LOG_CTX_RUN``), but the key is unbound HERE — or explicitly
    ``None`` at the seam (the detach seam passing ``run=None`` when the
    breadcrumb is absent). The export must reflect only THIS process's bound
    keys: absent-when-unbound, not the ancestor's stale correlation."""
    inherited = {"SHIPIT_LOG_CTX_RUN": "111", "SHIPIT_LOG_CTX_PR": "9", "PATH": "/x"}

    # Unbound key: the inherited entry is scrubbed, not passed through.
    child_env = logcontext.env_export(inherited)
    assert "SHIPIT_LOG_CTX_RUN" not in child_env
    assert "SHIPIT_LOG_CTX_PR" not in child_env
    assert child_env["PATH"] == "/x"

    # Explicit run=None at the seam behaves the same — None drops, and the
    # inherited value cannot resurrect it.
    child_env = logcontext.env_export(inherited, run=None)
    assert "SHIPIT_LOG_CTX_RUN" not in child_env

    # A key bound HERE still wins over the inherited entry.
    logcontext.bind(pr=231)
    child_env = logcontext.env_export(inherited)
    assert child_env["SHIPIT_LOG_CTX_PR"] == "231"


def test_bind_from_env_round_trips_bound_keys_with_types():
    """The full seam: bind → export → (new process) rebind reproduces the SAME
    context, ints included — the acceptance-criteria round-trip."""
    logcontext.bind(
        session="work", tree="/trees/x", pr=231, run=555, repo="acme/widget"
    )
    exported = logcontext.env_export({})

    import structlog

    structlog.contextvars.clear_contextvars()  # "the child process"
    logcontext.bind_from_env(exported)

    assert logcontext.bound() == {
        "session": "work",
        "tree": "/trees/x",
        "pr": 231,  # int again, not "231" — the jq contract survives the env
        "run": 555,
        "repo": "acme/widget",
    }


def test_bind_from_env_ignores_absent_and_empty_vars():
    logcontext.bind_from_env({"SHIPIT_LOG_CTX_PR": "", "UNRELATED": "x"})
    assert logcontext.bound() == {}


def test_bind_from_env_degrades_malformed_numeric_to_string():
    """A malformed numeric export must not crash logging setup — the raw string
    binds (still a record field, still greppable) instead of raising."""
    logcontext.bind_from_env({"SHIPIT_LOG_CTX_PR": "not-a-number"})
    assert logcontext.bound() == {"pr": "not-a-number"}


def test_configure_logging_rebinds_parent_exported_keys(tmp_path):
    """The child half lives at logging setup: a child configured with a parent's
    exported environment carries the parent's keys on its records — the detached
    review child's ``pr``/``repo`` story, in-process."""
    logsetup.configure_logging(
        env={"SHIPIT_LOG_CTX_PR": "231", "SHIPIT_LOG_CTX_REPO": "acme/widget"},
        repo=REPO,
        base_dir=tmp_path,
    )

    _emit("child-record")

    (record,) = _records(tmp_path)
    assert record["pr"] == 231
    assert record["repo"] == "acme/widget"


def test_cli_entry_binds_the_resolved_repo(monkeypatch):
    """The CLI-ENTRY half (ADR-0029): the root group binds the best-effort
    resolved repo as the `repo` key BEFORE logging setup, so every record of the
    run carries it. The resolution and setup boundaries are stubbed so the test
    pins the entry glue without gh or real sinks."""
    from shipit import cli

    monkeypatch.setattr(cli, "resolve_current_repo", lambda: REPO)
    seen: dict = {}
    monkeypatch.setattr(cli, "configure_logging", lambda **kw: seen.update(kw))

    # Routed through a real subcommand so the root callback actually fires (a
    # bare top-level --help is eager and short-circuits before the callback).
    rc = cli.main(["lint", "--help"])

    assert rc == 0
    assert logcontext.bound()["repo"] == "acme/widget"
    assert seen["repo"] == REPO  # resolved ONCE, then shared


def test_cli_entry_binds_nothing_outside_a_checkout(monkeypatch):
    """Outside a repo the resolution is None: no `repo` key binds — absent, not
    a null-ish placeholder — mirroring the skipped file sink."""
    from shipit import cli

    monkeypatch.setattr(cli, "resolve_current_repo", lambda: None)
    monkeypatch.setattr(cli, "configure_logging", lambda **kw: None)

    rc = cli.main(["lint", "--help"])

    assert rc == 0
    assert "repo" not in logcontext.bound()


def test_env_rebind_wins_over_an_earlier_best_effort_bind(tmp_path):
    """Precedence at the child: the CLI entry binds its best-effort cwd-resolved
    repo BEFORE configure_logging, so a parent's explicit export — rebound AT
    logging setup — deliberately overrides it."""
    logcontext.bind(repo="cwd/guess")
    logsetup.configure_logging(
        env={"SHIPIT_LOG_CTX_REPO": "parent/explicit"},
        repo=REPO,
        base_dir=tmp_path,
    )

    assert logcontext.bound()["repo"] == "parent/explicit"
