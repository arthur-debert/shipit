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
from datetime import UTC, datetime
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
DEFAULT_NOW = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)


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


#: The pixi-absence fail-open guard (#482) every managed lefthook leg is prefixed
#: with: a `command -v pixi` probe that skips (note + `exit 0`) when pixi is not on
#: PATH, so a pixi-less environment (a consumer's legacy CI spine) is not regressed
#: to red. This is the SINGLE source of the expected string — both the install and
#: logevent managed-hook tests assert the packaged legs carry it, so a drift in the
#: product's guard text shows up in exactly one place here.
PIXI_ABSENCE_GUARD = (
    'command -v pixi >/dev/null 2>&1 || { echo "shipit: pixi not on PATH — '
    "skipping this managed hook (pixi-less environment; the full gate runs "
    'wherever pixi is provisioned)."; exit 0; }; '
)

#: The guarded ``~/.local/bin`` PATH-prepend leg (#601, #848) shared by every
#: managed hook command that must resolve Layer 0's install dir (setup-dev-env.sh
#: provisions pinned pixi/uv exactly there): the idempotent case-guard idiom,
#: wrapped for an unset ``$HOME`` (an unguarded prepend pushes a literal
#: "/.local/bin" onto PATH in HOME-less hook shells). SINGLE source of the
#: expected string — the claude and codex twins must carry it byte-identically,
#: so a parity drift (the codex pair shipped with NO prepend at all: gemini on
#: tree-sitter-lex#89, copilot on supage#171) shows up in exactly one place here.
LOCAL_BIN_PATH_LEG = (
    'if [ -n "${HOME:-}" ]; then case ":$PATH:" in *":$HOME/.local/bin:"*) ;; '
    '*) export PATH="$HOME/.local/bin:$PATH" ;; esac; fi; '
)


def managed_cc_hook_command(phase: str) -> str:
    """The exact managed ``.claude/settings.json`` hook command for ``phase`` (#491).

    Covers the four ADDITIVE hooks only — ``sessionstart``, ``stop``,
    ``subagent-stop``, ``worktreecreate`` — never ``pretooluse``: that one is the
    ADR-0012 coordinator-edit GUARD, and #529 gave it its own fail-closed command
    (:func:`managed_pretooluse_hook_command`) after a fail-open regression on this
    shared shape disabled it silently. SINGLE source of the expected string for the
    four it does cover — the install and hook tests assert the packaged data files
    and shipit's own dogfood settings carry it, so a drift in the product's hook
    command shows up in exactly one place here.

    Two properties this string encodes (#491):

    - **No ``pixi run`` wrap.** ``./bin/shipit`` is the pinned, pixi-independent
      launcher (ADR-0033), so the ``hook`` subcommands ride the pin directly — the
      old ``pixi run`` prefix added a hard pixi dependency and startup cost for
      nothing (contrast the lefthook ``lint`` legs, which need ``-e lint`` for the
      TOOLCHAIN).
    - **A launcher-presence fail-open guard**, symmetric with the #482 lefthook
      guard: after ``cd``-ing to the project dir, the hook skips (note + ``exit
      0``) when the managed ``./bin/shipit`` launcher is not present/executable —
      so a Claude session in a checkout without a provisioned launcher is not
      disrupted. Where the launcher IS present it runs, and its real ``hook``
      exit code propagates unchanged: fail-open is for a runtime that is genuinely
      absent, never for a hook that ran and errored. Legitimate for these four —
      they are additive (advisory/bookkeeping), never the security guard.

    The ``sessionstart`` variant alone carries a THIRD property (#547 Layer 0):
    before the launcher guard it runs the managed ``./bin/setup-dev-env.sh``
    (guarded on existence+executability, ``|| warn`` — the script itself is
    fail-open too), so the base system (pixi + uv at their pins, the pixi env
    solves) is provisioned before anything in the session needs it. The
    ``shipit hook sessionstart`` marker substring is unchanged, so the JSON-hook
    reconcile identity keeps recognising the managed entry across the change.

    And a FOURTH (#601): between the setup-dev-env leg and the launcher guard it
    idempotently prepends ``~/.local/bin`` to PATH (the same case-guard idiom
    setup-dev-env.sh appends to ``CLAUDE_ENV_FILE``). The bootstrap runs as a
    SUBPROCESS, so its own PATH export cannot reach the hook shell, and the
    ``CLAUDE_ENV_FILE`` line only affects later Bash calls — without this leg,
    the first session start in an environment where ``~/.local/bin`` is not
    already on PATH installed uv and then immediately failed the
    ``./bin/shipit hook sessionstart`` on the same command line with "uv is not
    on PATH" (exit 127; the ADR-0033 launcher hard-requires uv). Since #848 the
    leg is the shared :data:`LOCAL_BIN_PATH_LEG` — guarded for an unset
    ``$HOME`` and byte-identical across every hook command that carries it.
    """
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
    """The exact managed ``PreToolUse`` (coordinator-guard) hook command (#529).

    SINGLE source of the expected string — the install and hook tests assert the
    packaged data file and shipit's own dogfood settings carry it byte-identically,
    so a drift in the guard's command shows up in exactly one place here.

    This is the ONE managed hook command that must never fail open (ADR-0012: it
    is the sole enforcer of "the coordinator never implements"). #505/#491 moved
    every managed hook — guard included — onto the shared `managed_cc_hook_command`
    shape: no `pixi run`, and a launcher-presence probe that silently `exit 0`s
    (ALLOW, no decision) when resolution fails. On a bare PreToolUse process (no
    `CLAUDE_ENV_FILE` sourcing, ergo no pixi activation) that made guard liveness
    depend on ambient PATH resolution — and when it failed, the coordinator ran
    fully unguarded with zero signal (#529). Restores `pixi run` in the
    adapter-equivalent form `pixi run --manifest-path "$CLAUDE_PROJECT_DIR"/pixi.toml
    -- ./bin/shipit …` (mirroring `shipit.pixienv.run.run_argv`): the explicit
    `--manifest-path` overrides any leaked `PIXI_PROJECT_MANIFEST` so the guard
    resolves THIS project's env rather than a parent's (the same ambiguity dodge the
    pixi adapter uses), and `--` fences pixi's flags from the child argv, so the
    guard resolves reliably in the canonical pixi/dogfood repo, `pixi` supplying the
    activated env and `./bin/shipit` the pin per ADR-0033. Replaces the
    fail-open probe with a fail-CLOSED tail: capture the resolution+run chain's own
    exit code, and if it is anything but 0 (the launcher missing, the pin/uv/pixi
    chain unresolvable, `cd` itself failing), print an actionable message to
    stderr and `exit 2` — Claude Code's documented blocking-error exit code — so
    the tool call is refused rather than silently allowed. When the chain DOES run
    (rc 0), the real `shipit hook pretooluse` process already wrote its own
    decision (a `deny` JSON payload, or nothing for an allow) straight through to
    stdout, and its own exit code is always 0 by contract (see
    `shipit.verbs.hook.pretooluse` — fail-open is that inner boundary's contract
    for MALFORMED PAYLOADS it received, an orthogonal concern to this outer
    wrapper's "could the guard run AT ALL" contract). Note there is no bare
    `exit 0` anywhere in this string — the invariant this hook enforces (never
    silently allow an edit it did not check) is true by construction, not by
    convention.

    Open design point (flagged, not resolved here): a genuinely pixi-less
    consumer now gets every coordinator code-edit BLOCKED (rc 127 from `pixi run`
    unresolved) rather than the pre-#529 unguarded allow — the correct direction
    per the never-silent-allow invariant, but the pixi-less mechanics (how such a
    consumer gets a working guard at all) are left for a follow-up.

    #848: the command opens with the shared :data:`LOCAL_BIN_PATH_LEG` — a
    PreToolUse process never sources ``CLAUDE_ENV_FILE``, so on a bare hook
    PATH the Layer 0 pixi (``~/.local/bin``) was unresolvable and the guard
    fail-closed on checkouts whose ONLY pixi is the provisioned pin. The leg
    makes the guard runnable there; resolution failing anyway still blocks.
    """
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
    """Return the loader so a test can pick its scenario: `context('name')`."""
    return load_context


@pytest.fixture(autouse=True)
def _no_network_staleness_read(monkeypatch):
    """Keep the ADR-0033 pin-staleness read off the network in every test.

    The SessionStart hook's staleness advisory reads GitHub best-effort via
    :func:`shipit.gh.commits_ahead`. Tests that drive the hook against a cwd
    that happens to carry a valid pin (including ``Path.cwd()`` fallbacks into
    this very checkout) must never turn that into a live ``gh api`` call; the
    hook resolves the boundary at call time, so patching the module function
    is enough. Staleness tests inject their own fake through the ``run()``
    parameter, which takes precedence over this stub.
    """
    from shipit import gh

    monkeypatch.setattr(gh, "commits_ahead", lambda repo, base, head: None)


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


@pytest.fixture(autouse=True)
def _guard_session_store_home(monkeypatch, tmp_path_factory, request):
    """Point the session store's default ``~`` at a tmp dir for EVERY test (ADR-0073).

    `shipit.sessionstore` plants a symlink at `~/.claude/projects/<cwd-slug>` pointing
    at the repo's store. Its entry points all take a `home` override, but its *callers*
    (`tree create`, `shipit install`) pass nothing — so any test that exercises a caller
    resolves the real `~` and plants real symlinks in the developer's actual
    `~/.claude/projects/`, keyed on whatever the test's fixture remote parsed to.

    That is not a hypothetical. Before this guard, one run of `tests/test_tree_create.py`
    left seven live symlinks in the real `~/.claude/projects/` and created a real
    `~/.claude/stores/<pytest-tmpdir-name>/remote/`. A suite that adopts, moves, or
    refuses against a developer's real store is itself the data-loss bug ADR-0073 exists
    to prevent — so the guard is autouse and suite-wide rather than opt-in per test: the
    tests that need protecting are exactly the ones whose authors never think about
    session stores.

    Tests that want a store of their own still pass `home=` explicitly; this only
    replaces the *default*. The one test that pins the real default marks itself
    `@pytest.mark.real_session_store_home` — it reads a path value and touches nothing.
    """
    if "real_session_store_home" in request.keywords:
        return
    from shipit import sessionstore

    guarded = tmp_path_factory.mktemp("guarded-home")
    monkeypatch.setattr(sessionstore, "_default_home", lambda: guarded)
