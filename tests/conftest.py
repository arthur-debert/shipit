"""Shared fixture loader for the prstate (PR state engine) tests.

Each JSON file under prstate_fixtures/ holds the raw `gh` payloads for one PR
scenario; `context` builds a PullContext from one exactly as `fetch.gather()`
would, minus the network. Copied with the engine from release-core (ADR-0001),
re-pointed to `shipit.prstate.*`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from shipit.prstate.fetch import context_from_raw
from shipit.prstate.model import PullContext

FIXTURES = Path(__file__).parent / "prstate_fixtures"


def load_context(name: str) -> PullContext:
    data = json.loads((FIXTURES / f"{name}.json").read_text())
    return context_from_raw(
        meta=data["meta"],
        reviews_json=data.get("reviews", []),
        thread_nodes=data.get("threads", []),
        reactions=data.get("reactions", []),
        issue_comments=data.get("issue_comments", []),
    )


@pytest.fixture
def context():
    """Return the loader so a test can pick its scenario: `context('name')`."""
    return load_context


@pytest.fixture(autouse=True)
def _reset_required_cache():
    """Clear the process-lifetime required-reviewer cache around every prstate
    test, so a test that resolves the required set (which reads `.shipit.toml`)
    never leaks its result into the next.

    Importing `shipit.prstate.reviewers` is cheap and side-effect-free, so this
    autouse fixture is harmless for the non-prstate suites that also run under
    this conftest (it only resets an in-memory cache they never touch)."""
    from shipit.prstate import reviewers

    reviewers._reset_required_cache()
    yield
    reviewers._reset_required_cache()
