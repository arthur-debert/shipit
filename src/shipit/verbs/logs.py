"""`shipit logs` — locate and read shipit's durable per-repo JSONL log."""

from __future__ import annotations

import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import click

from .. import flowview, logcontext, logread, logsetup, redact
from ..identity import Repo
from ..logread import DEFAULT_TAIL, LogQuery
from ..session import current as session_current
from ._context import current_root_context
from ._errors import cli_errors
from ._params import repo_argument

_EXIT_NO_LOG = 1

_SNIPPET_LEN = 80

_RENDERED_FIELDS = ("ts", "level", "logger", "msg", "exception")


def render_record(line: str) -> str | None:
    """Render one JSONL record for humans, or ``None`` when the line is not one."""
    record = logread.parse_record(line)
    if record is None:
        return None
    ts = record.get("ts", "")
    level = str(record.get("level", "")).upper()
    logger = record.get("logger", "")
    msg = record.get("msg", "")
    exception = record.get("exception")
    rendered = f"{ts} {level} {logger}: {msg}"
    extras = " ".join(
        f"{k}={v}" for k, v in sorted(record.items()) if k not in _RENDERED_FIELDS
    )
    if extras:
        rendered = f"{rendered} [{extras}]"
    if exception:
        rendered = f"{rendered}\n{exception}"
    return rendered


def malformed_note(line: str) -> str:
    """The stderr note for a line that is not a record — redacted and truncated."""
    snippet = redact.redact_text(line)[:_SNIPPET_LEN]
    return f"logs: skipped malformed line: {snippet!r}"


def _emit_line(line: str, *, raw: bool) -> None:
    if raw:
        print(line, flush=True)
        return
    if not line.strip():
        return
    rendered = render_record(line)
    if rendered is None:
        print(malformed_note(line), file=sys.stderr, flush=True)
        return
    print(rendered, flush=True)


def build_query(
    *,
    events_only: bool = False,
    pr: int | None = None,
    session: str | None = None,
    epic: str | None = None,
    ws: int | str | None = None,
    agent: str | None = None,
    role: str | None = None,
    reviewer: str | None = None,
    run_id: str | None = None,
    round_id: str | None = None,
    tail: int = DEFAULT_TAIL,
    follow: bool = False,
    raw: bool = False,
    flow: bool = False,
    show_agents: bool = False,
    current_session: Callable[[], str | None] | None = None,
) -> LogQuery:
    """Mint the frozen :class:`~shipit.logread.LogQuery` at the CLI boundary."""
    if session == "current":
        session = (current_session or session_current.current_session_id)()
        if session is None:
            raise click.UsageError(
                "--session current, but no session is resolvable — neither "
                f"{logcontext.ENV_PREFIX}SESSION in the environment nor "
                "an ephemeral session-Tree cwd (ADR-0027); pass the session id."
            )
    try:
        return logread.build_query(
            events_only=events_only,
            pr=pr,
            session=session,
            epic=epic,
            ws=ws,
            agent=agent,
            role=role,
            reviewer=reviewer,
            run_id=run_id,
            round_id=round_id,
            tail=tail,
            follow=follow,
            raw=raw,
            flow=flow,
            show_agents=show_agents,
        )
    except ValueError as exc:
        raise click.UsageError(str(exc)) from exc


@cli_errors
def run(
    repo: Repo | None = None,
    *,
    path_only: bool = False,
    query: LogQuery | None = None,
    base_dir: str | Path | None = None,
    sleep: Callable[[float], None] | None = None,
    now: Callable[[], datetime] | None = None,
) -> int:
    """Locate (and read) the per-repo JSONL log; returns an exit code."""
    target = repo if repo is not None else current_root_context().require_repo()
    path = logsetup.log_file_path(target, base_dir=base_dir)

    if path_only:
        print(str(path))
        return 0

    query = query if query is not None else LogQuery()

    if not path.exists():
        print(
            f"logs: no log yet at {path} — it is created on the first shipit run "
            f"that logs for {target.slug}.",
            file=sys.stderr,
        )
        return _EXIT_NO_LOG

    record_filter = query.record_filter

    if query.follow:
        if not query.raw:
            print(str(path), flush=True)
        try:
            for line in logread.follow_lines(
                path, record_filter, tail=query.tail, sleep=sleep
            ):
                _emit_line(line, raw=query.raw)
        except KeyboardInterrupt:
            return 0
        return 0

    if query.flow:
        records = [
            record
            for record in (
                logread.parse_record(ln)
                for ln in logread.read_lines(path, logread.Filter())
            )
            if record is not None and record_filter.matches_record(record)
        ]
        instant = (now or (lambda: datetime.now(UTC)))()
        for rendered in flowview.render(
            logread.last_n(records, query.tail),
            now=instant,
            show_agents=query.show_agents,
            header_from=records,
        ):
            print(rendered, flush=True)
        return 0

    if not query.raw:
        print(str(path))
    for line in logread.read_lines(path, record_filter, query.tail):
        _emit_line(line, raw=query.raw)
    return 0


@click.command(name="logs")
@repo_argument
@click.option(
    "--path",
    "path_only",
    is_flag=True,
    help='Print the absolute log file path and exit (for `cat "$(shipit logs --path)"`).',
)
@click.option(
    "-f",
    "--follow",
    is_flag=True,
    help="Stream appended log lines live (tail -f); ends on Ctrl-C.",
)
@click.option(
    "--raw",
    is_flag=True,
    help="Emit unmodified JSONL lines (no path header) for piping to jq.",
)
@click.option(
    "-n",
    "--lines",
    "lines",
    type=int,
    default=DEFAULT_TAIL,
    show_default=True,
    help="Trailing records to print in the default (no-flag) view.",
)
@click.option(
    "--events",
    "events_only",
    is_flag=True,
    help="Only dev-cycle event records (records carrying an `event` field).",
)
@click.option(
    "--pr",
    "pr",
    type=int,
    default=None,
    metavar="N",
    help="Only records whose bound `pr` domain key equals this PR number.",
)
@click.option(
    "--session",
    "session",
    default=None,
    metavar="ID|current",
    help="Only this session's records; `current` resolves from the session "
    "environment (or the ephemeral Tree cwd).",
)
@click.option(
    "--epic",
    "epic",
    default=None,
    metavar="CODE",
    help="Only records whose bound `epic` domain key equals this code.",
)
@click.option(
    "--ws",
    "ws",
    default=None,
    metavar="N",
    help="Only this Work Stream's records; accepts 1, 01, or WS01.",
)
@click.option(
    "--agent",
    "agent",
    default=None,
    metavar="ID",
    help="Only records whose bound `agent` domain key equals this spawn id.",
)
@click.option(
    "--role",
    "role",
    default=None,
    metavar="NAME",
    help="Only records whose bound `role` domain key equals this Role name.",
)
@click.option(
    "--reviewer",
    "reviewer",
    default=None,
    metavar="NAME",
    help="Only records whose `reviewer` field equals this reviewing agent.",
)
@click.option(
    "--run",
    "run_id",
    default=None,
    metavar="ID",
    help="Only records whose `run_id` field equals this review sub-agent run "
    "(one dimension/incremental pass or calibrator run).",
)
@click.option(
    "--round",
    "round_id",
    default=None,
    metavar="ID",
    help="Only records whose `round_id` field equals this review round "
    "(one fan-out round's passes group under one id).",
)
@click.option(
    "--flow",
    is_flag=True,
    help="Render the filtered records as the session story (implies --events).",
)
@click.option(
    "--agent-ids",
    "show_agents",
    is_flag=True,
    help="Show agent ids on flow lines (always collected, displayed on request).",
)
def logs_cmd(
    repo: Repo | None,
    path_only: bool,
    follow: bool,
    raw: bool,
    lines: int,
    events_only: bool,
    pr: int | None,
    session: str | None,
    epic: str | None,
    ws: str | None,
    agent: str | None,
    role: str | None,
    reviewer: str | None,
    run_id: str | None,
    round_id: str | None,
    flow: bool,
    show_agents: bool,
) -> None:
    """Locate and read shipit's durable per-repo JSONL log."""
    if path_only:
        raise SystemExit(run(repo, path_only=True))
    query = build_query(
        events_only=events_only,
        pr=pr,
        session=session,
        epic=epic,
        ws=ws,
        agent=agent,
        role=role,
        reviewer=reviewer,
        run_id=run_id,
        round_id=round_id,
        tail=lines,
        follow=follow,
        raw=raw,
        flow=flow,
        show_agents=show_agents,
    )
    raise SystemExit(run(repo, query=query))
