"""Shared fixture loader for the prstate (PR state engine) tests.

Each JSON file under prstate_fixtures/ holds the raw `gh` payloads for one PR
scenario; `context` builds a ReadinessView from one exactly as `fetch.gather()`
would, minus the network. Copied with the engine from release-core (ADR-0001),
re-pointed to `shipit.prstate.*`.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
import structlog
from shipit.prstate.fetch import context_from_raw
from shipit.prstate.model import ReadinessView

FIXTURES = Path(__file__).parent / "prstate_fixtures"

#: A FIXED injected "now" for the recorded-snapshot tests. The engine never calls
#: a clock — it reads "now" off the snapshot (OBS04-WS01) — so a fixed value here
#: makes every fixture deterministic. A fixture can override it with a top-level
#: `now` (ISO-8601) field, and a test can pass `load_context(name, now=...)` to
#: pin a wait-window relative to the funnel breadcrumb's `started_at`.
DEFAULT_NOW = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def load_context(name: str, now: datetime | None = None) -> ReadinessView:
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
def _reset_required_cache():
    """Isolate the required-reviewer cache around every prstate test.

    Two jobs:

    * clear the process-lifetime cache before/after each test, so a test that
      resolves the required set never leaks its result into the next; and
    * PRE-SEED the cache with the shipped DEFAULT reviewer set (copilot-only) so
      the state-machine unit tests resolve against that default, NOT against THIS
      repo's deployed `[reviewers]` policy. shipit dogfoods its local reviewers —
      its own `.shipit.toml` now requires copilot+codex+agy — but the engine unit
      tests assert behaviour for a known set and must not couple to the repo's
      deployment config. A test that varies the config resets the cache itself
      (and drives resolution via a monkeypatched `load_override`), so the seed is
      transparent to it.

    Importing `shipit.prstate.reviewers` is cheap and side-effect-free, so this
    autouse fixture is harmless for the non-prstate suites that also run under
    this conftest (they never read the cache)."""
    from shipit.prstate import reviewers, reviewers_config

    reviewers._reset_required_cache()
    default_names = tuple(reviewers_config.DEFAULT_REVIEWERS)
    reviewers._REQUIRED_CACHE = tuple(
        reviewers_config.required_reviewers(default_names)
    )
    reviewers._RERUN_CACHE = dict(reviewers_config.DEFAULT_REVIEWERS)
    yield
    reviewers._reset_required_cache()
