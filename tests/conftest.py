"""Shared fixture loader for the prstate (PR state engine) tests.

Each JSON file under prstate_fixtures/ holds the raw `gh` payloads for one PR
scenario; `context` builds a ReadinessView from one exactly as `fetch.gather()`
would, minus the network. Copied with the engine from release-core (ADR-0001),
re-pointed to `shipit.prstate.*`.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
import structlog
from shipit.prstate.fetch import context_from_raw
from shipit.prstate.model import ReadinessView
from shipit.prstate.reviewers_config import default_roster
from shipit.prstate.roster import Roster

FIXTURES = Path(__file__).parent / "prstate_fixtures"

#: A FIXED injected "now" for the recorded-snapshot tests. The engine never calls
#: a clock — it reads "now" off the snapshot (OBS04-WS01) — so a fixed value here
#: makes every fixture deterministic. A fixture can override it with a top-level
#: `now` (ISO-8601) field, and a test can pass `load_context(name, now=...)` to
#: pin a wait-window relative to the funnel breadcrumb's `started_at`.
DEFAULT_NOW = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def load_context(
    name: str, now: datetime | None = None, roster: Roster | None = None
) -> ReadinessView:
    """Build a ReadinessView from one recorded scenario, as `fetch.gather()` would.

    `roster` defaults to the SHIPPED default Roster (copilot-only, review-once)
    passed as a VALUE (CLI01-WS04) — so the recorded-snapshot tests evaluate
    against the shipped default, never against THIS repo's deployed
    `.shipit.toml` `[reviewers]` policy (shipit dogfoods copilot+codex+agy), and
    there is no module-global cache to pre-seed or reset. A test that varies the
    reviewer configuration passes its own Roster."""
    data = json.loads((FIXTURES / f"{name}.json").read_text())
    if now is None:
        raw_now = data.get("now")
        now = datetime.fromisoformat(raw_now) if raw_now else DEFAULT_NOW
    return context_from_raw(
        meta=data["meta"],
        reviews_json=data.get("reviews", []),
        thread_nodes=data.get("threads", []),
        reactions=data.get("reactions", []),
        issue_comments=data.get("issue_comments", []),
        now=now,
        roster=roster if roster is not None else default_roster(),
    )


@pytest.fixture
def context():
    """Return the loader so a test can pick its scenario: `context('name')`."""
    return load_context


@pytest.fixture(autouse=True)
def _clean_domain_key_context():
    """Isolate the ADR-0029 domain-key log context around every test.

    Binding is a process-context side effect of several production seams (the
    CLI entry, the review detach, the spawn verb), so without this a test that
    drives one of those paths would leak `pr`/`repo`/`tree` onto every record a
    LATER test emits — and the absent-when-unbound contract is only assertable
    from a clean context.

    Ambient `SHIPIT_LOG_CTX_*` env vars are scrubbed for the test's duration
    too (and restored afterwards): `logsetup.configure_logging()` rebinds from
    `os.environ` when no explicit `env` is passed, so a developer/CI shell that
    carries the seam's vars (e.g. a test run spawned BY a shipit process) would
    otherwise make the suite non-deterministic."""
    from shipit import logcontext

    saved = {
        name: os.environ.pop(name)
        for name in list(os.environ)
        if name.startswith(logcontext.ENV_PREFIX)
    }
    structlog.contextvars.clear_contextvars()
    yield
    structlog.contextvars.clear_contextvars()
    os.environ.update(saved)


@pytest.fixture(autouse=True)
def _reset_event_first_sight():
    """Reset the dev-cycle events' first-sight registry around every test.

    :func:`shipit.events.emit_once` dedupes observational events (ADR-0032 /
    LOG04-WS02) for the PROCESS lifetime — which under pytest is the whole
    suite, so without this a fixture PR evaluated by one test would silently
    suppress the same milestone's emission in a later test."""
    from shipit import events

    events._seen.clear()
    yield
    events._seen.clear()


@pytest.fixture(autouse=True)
def _reset_shipit_logging():
    """Detach shipit's sinks from the process-global logger around every test.

    `logsetup.configure_logging()` attaches handlers to the ONE process-wide
    `logging.getLogger("shipit")` singleton — including a stderr `StreamHandler`
    that pins `sys.stderr` AT ATTACH TIME. Under pytest that stderr is the
    per-test `capsys` `CaptureIO`, which pytest closes at the test boundary. A
    handler left attached by one test therefore points at a CLOSED buffer, and
    the next test's pre-`configure_logging` bootstrap records (e.g. the CLI's
    identity-resolution `exec` DEBUG lines, emitted before its sinks are wired)
    hit that dead handler — `ValueError: I/O operation on closed file` — which
    the logging module reports by printing `--- Logging error ---` + traceback
    to the LIVE stderr, corrupting the `error:`-line contract the CLI tests
    assert (surfaces only in CI, where the DEBUG-level CI sink is attached).

    Clearing shipit's own (`shipit-`prefixed) handlers before and after each
    test keeps that per-test stream from leaking forward. Production is a
    one-shot process with a stable stderr, so it never hits this; the reset is a
    test-isolation concern for the shared singleton."""
    from shipit import logsetup

    logger = logging.getLogger(logsetup.LOGGER_NAME)
    logsetup._clear_own_handlers(logger)
    yield
    logsetup._clear_own_handlers(logger)
