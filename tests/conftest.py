from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
import structlog

from shipit.prstate.fetch import context_from_raw
from shipit.prstate.model import ReadinessView
from shipit.prstate.reviewers_config import default_roster
from shipit.prstate.roster import Roster

FIXTURES = Path(__file__).parent / "prstate_fixtures"

DEFAULT_NOW = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)


def load_context(
    name: str, now: datetime | None = None, roster: Roster | None = None
) -> ReadinessView:
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


PIXI_ABSENCE_GUARD = (
    'command -v pixi >/dev/null 2>&1 || { echo "shipit: pixi not on PATH — '
    "skipping this managed hook (pixi-less environment; the full gate runs "
    'wherever pixi is provisioned)."; exit 0; }; '
)

LOCAL_BIN_PATH_LEG = (
    'if [ -n "${HOME:-}" ]; then case ":$PATH:" in *":$HOME/.local/bin:"*) ;; '
    '*) export PATH="$HOME/.local/bin:$PATH" ;; esac; fi; '
)


def managed_cc_hook_command(phase: str) -> str:
    setup_leg = ""
    if phase == "sessionstart":
        setup_leg = (
            "if [ -x ./bin/setup-dev-env.sh ]; then ./bin/setup-dev-env.sh || echo "
            '"shipit: setup-dev-env.sh reported a problem — continuing '
            '(base-system provisioning is best-effort)." >&2; fi; '
            f"{LOCAL_BIN_PATH_LEG}"
        )
    return (
        f'cd "$CLAUDE_PROJECT_DIR" || exit 0; {setup_leg}test -x ./bin/shipit || {{ echo '
        '"shipit: bin/shipit launcher not present or executable here — skipping '
        "this managed hook (run 'shipit install' to (re)provision it).\"; exit 0; "
        f"}}; ./bin/shipit hook {phase}"
    )


def managed_pretooluse_hook_command() -> str:
    return (
        f'{LOCAL_BIN_PATH_LEG}cd "$CLAUDE_PROJECT_DIR" && pixi run --manifest-path '
        '"$CLAUDE_PROJECT_DIR"/pixi.toml -- ./bin/shipit hook pretooluse; '
        "rc=$?; "
        'if [ "$rc" -ne 0 ]; then echo "shipit: PreToolUse guard could not run '
        "(rc=$rc) — refusing edit rather than allowing an unchecked coordinator "
        "edit. Likely causes: CLAUDE_PROJECT_DIR is unset or not a shipit checkout "
        "(the cd failed), pixi is not installed, or the pinned shipit environment "
        "could not be resolved. Install pixi (https://pixi.sh) if it is missing, "
        "then run this command from the project to see the underlying error: "
        'pixi run --manifest-path \\"\\$CLAUDE_PROJECT_DIR\\"/pixi.toml -- '
        './bin/shipit hook pretooluse" >&2; exit 2; fi'
    )


@pytest.fixture
def context():
    return load_context


@pytest.fixture(autouse=True)
def _no_network_staleness_read(monkeypatch):
    from shipit import gh

    monkeypatch.setattr(gh, "commits_ahead", lambda repo, base, head: None)


@pytest.fixture(autouse=True)
def _clean_domain_key_context():
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
def _reset_shipit_logging():
    from shipit import logsetup

    logger = logging.getLogger(logsetup.LOGGER_NAME)
    logsetup._clear_own_handlers(logger)
    yield
    logsetup._clear_own_handlers(logger)


@pytest.fixture(autouse=True)
def _guard_session_store_home(monkeypatch, tmp_path_factory, request):
    if "real_session_store_home" in request.keywords:
        return
    from shipit import sessionstore

    guarded = tmp_path_factory.mktemp("guarded-home")
    monkeypatch.setattr(sessionstore, "_default_home", lambda: guarded)
