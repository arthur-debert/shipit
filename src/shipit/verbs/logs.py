"""logs — locate and read shipit's durable per-repo JSONL log (LOG01-WS04).

The READER half of WS01's file sink, on the ADR-0030 boundary contract
(CLI02): this module is click glue plus renderers. The read ENGINE — file
reading, tail, ``--follow`` rotation detection, torn-write buffering,
malformed-line resilience — lives in the domain package
(:mod:`shipit.logread`) and yields stored lines out of iterators; everything
that touches the terminal happens HERE, over that output. The flag surface
becomes ONE frozen :class:`~shipit.logread.LogQuery` at parse
(:func:`build_query` — the parse-to-values move), so ``run()`` takes a repo,
a query, and the injected test boundaries instead of a parameter per flag.

The verb NEVER recomputes the log's location — it consumes
:func:`shipit.logsetup.log_file_path` (``resolve_log_dir`` + the handler's
``LOG_FILENAME``), the single source of truth, so reader and writer can never
disagree about where the log lives. No platform ``if`` branch, no bespoke
log-dir env var (the path library owns the location, per the glassbox PRD
``docs/prd/glassbox.md``).

The verb reads JSONL ONLY — a hard cutover, no dual-format sniffing
(ADR-0029; pre-cutover freeform files age out via rotation). Two output
modes: the default renders each record legibly for humans (``ts LEVEL
logger: msg [key=value …]``); ``--raw`` passes the stored lines through
unmodified — and prints nothing else — so stdout pipes straight into jq. In
the rendered view, a line that is not a JSON object is skipped with a stderr
note, never a crash: the log is diagnosis data, and one corrupt line (a torn
write, a rotation seam) must not take down the reader. With NO filter
active, ``--raw`` does not parse at all — malformed lines pass through
untouched, because judging them is the downstream tool's job.

The reader grows FILTERS, not a sibling (LOG04 / ADR-0032): ``--events``
keeps only ``event``-tagged dev-cycle records, and one flag per domain key
selects on the record's flat fields — ``--pr <n>``, ``--session
<id|current>`` (``current`` resolved via :mod:`shipit.session.current`: the
session environment first, the ephemeral Tree leaf second, ADR-0027),
``--epic <code>``, ``--ws <n>`` (accepting ``1``, ``01``, or ``WS01``), and
``--agent <id>`` / ``--role <name>``. All AND-composed, applied client-side
before the tail count, uniform across the static, ``--raw``, and
``--follow`` views (:class:`shipit.logread.Filter` is the one predicate).

``--flow`` renders the filtered records as the session STORY instead of a
record listing: selection stays in the query (``--flow`` implies
``--events``; the domain-key filters compose as usual), the LOOK is the pure
renderer's (:mod:`shipit.flowview` — intent/theme header, relative times,
``EPIC-WSnn:`` prefixes, agent ids behind ``--agent-ids``). A story is a
bounded rendering of the static view, so ``--flow`` refuses ``--raw`` and
``--follow`` at query construction rather than guessing what a raw or
followed story would mean.

The repo whose log we read defaults to the ambient checkout — the ONE root
resolution (ADR-0030, offline off the origin remote), the SAME identity the
sink namespaces the log by, so a log written offline is readable offline. An
explicit ``owner/repo`` argument overrides it (minted to a
:class:`~shipit.identity.Repo` at parse by the shared parameter library).
Exit contract: usage errors (a bad slug, an out-of-grammar ``--ws``, the
``--flow`` contradictions, an unresolvable ``--session current``) are click's
at parse (exit 2); running outside a checkout without an explicit repo is
the one uniform runtime refusal through the error shell (``error: …`` +
exit 1); a log not written yet stays a clean stderr note + exit 1.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import click

from .. import flowview, logcontext, logread, logsetup, redact
from ..identity import Repo
from ..logread import DEFAULT_TAIL, LogQuery
from ..session import current as session_current
from ._context import current_root_context
from ._errors import cli_errors
from ._params import repo_argument

#: Exit code when the log file does not exist yet (a clean "nothing to read",
#: not a crash — the path is valid, the run that writes it just hasn't happened).
_EXIT_NO_LOG = 1

#: Longest malformed-line snippet quoted in the skip note; enough to identify
#: the line without spraying a whole corrupt record onto stderr.
_SNIPPET_LEN = 80

#: Record fields the renderer places explicitly (everything else — the bound
#: domain keys and event extras — trails as ``key=value``).
_RENDERED_FIELDS = ("ts", "level", "logger", "msg", "exception")


def render_record(line: str) -> str | None:
    """Render one JSONL record for humans, or ``None`` when the line is not one.

    A PURE per-record formatter (the render seam): the legible shape mirrors
    the console surface (``LEVEL logger: msg``) with the durable record's
    extra facts folded in — the ``ts`` up front (the file's reason to exist is
    the timestamped history) and every remaining flat field — the bound domain
    keys (``pr``, ``session``, …) and event extras — trailing as sorted
    ``key=value`` pairs. An ``exception`` (WS01 flattens tracebacks to a
    string) lands on the following lines, the way stdlib formatting would.

    Only a JSON *object* is a record; any other parse (or a parse failure) is
    the caller's cue to skip the line with a note.
    """
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
    """The stderr note for a line that is not a record — redacted, truncated.

    Pure string-in, string-out (the render seam). The note is the one path
    that echoes raw file content the writer's pipeline never finished
    redacting (a torn write, a pre-cutover freeform line), so it passes
    through :func:`shipit.redact.redact_text` (token/PEM pattern masking)
    BEFORE truncating — a secret straddling the snippet cut is still seen
    whole by the pattern matcher.
    """
    snippet = redact.redact_text(line)[:_SNIPPET_LEN]
    return f"logs: skipped malformed line: {snippet!r}"


def _emit_line(line: str, *, raw: bool) -> None:
    """Emit one engine-yielded log line in the chosen mode.

    ``raw`` is the jq passthrough: the line goes out exactly as stored, parsed
    by nobody (a malformed line is the downstream tool's to judge). Otherwise
    the line is rendered for humans; a blank line is dropped silently (file
    padding, not a record) and a malformed one is skipped with the redacted
    stderr note — stdout carries only rendered records, and a corrupt line
    never crashes the reader.

    Every ``print`` flushes: ``-f`` output is piped as often as watched
    (``shipit logs -f --raw | jq .``), and Python block-buffers a
    non-interactive stdout — without the flush, records would sit in the
    buffer instead of streaming live.
    """
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
    tail: int = DEFAULT_TAIL,
    follow: bool = False,
    raw: bool = False,
    flow: bool = False,
    show_agents: bool = False,
    current_session: Callable[[], str | None] | None = None,
) -> LogQuery:
    """Mint the frozen :class:`~shipit.logread.LogQuery` at the CLI boundary.

    The parse-to-values step (ADR-0030): flag primitives in, ONE value out,
    with every flag-level failure a :class:`click.UsageError` (the usage tier,
    exit 2) so it never reaches ``run()``. Two jobs live here rather than in
    the domain factory: resolving the ``--session current`` sentinel (it reads
    the process environment — ``current_session`` is the injected boundary,
    defaulting to :func:`shipit.session.current.current_session_id`; an
    unresolvable ``current`` is a usage error, since the caller asked for a
    session this process is not in), and translating the domain factory's
    :class:`ValueError` (out-of-grammar ``--ws``, the ``--flow`` ×
    ``--raw``/``--follow`` contradiction) into click's vocabulary.
    """
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
    """Locate (and read) the per-repo JSONL log. Returns an int exit code.

    ``repo`` is the typed identity (minted at parse, or injected by a direct
    caller); omitted, the ambient checkout's — the ONE root resolution, with
    the uniform outside-a-checkout refusal mapped by the error shell.
    ``path_only`` prints just the resolved absolute path and exits 0 —
    locating the log never depends on it existing yet (or on ``query``), so
    this always succeeds. Otherwise ``query`` (default: the plain read) says
    what to read and how to view it; the engine (:mod:`shipit.logread`)
    yields the selected lines and this function renders them — the path
    header in the human modes, per-line emission via :func:`_emit_line`, the
    story view via :mod:`shipit.flowview`. A missing log file is reported on
    stderr (no traceback) and exits 1.

    ``base_dir`` / ``sleep`` / ``now`` are injected boundaries for tests: the
    log location's base directory, the follow-loop poll, and the flow view's
    clock.
    """
    target = repo if repo is not None else current_root_context().require_repo()
    path = logsetup.log_file_path(target, base_dir=base_dir)

    if path_only:
        # --path is a pure LOCATOR (the module contract): it prints the
        # resolved path and exits, never depending on the file's contents or
        # on the query — the CLI callback does not even build one, so
        # `--path --session current` outside a session still prints the path.
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
        # The path header prints only in the human mode: raw stdout is
        # reserved for JSONL. Ends cleanly on Ctrl-C (exit 0), the way
        # `tail -f` does — the interrupt surfaces out of the engine's
        # generator and is mapped to success HERE, at the terminal seam.
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
        # The story view: same selection (the engine's filtered read), the
        # rendering delegated whole to the pure module. The filter is always
        # active here (--flow implied --events at parse), so a malformed line
        # was dropped silently by the engine. The header themes the WHOLE
        # session (its intent/epics), so it reads the full matching set; only
        # the body lines are tailed — deriving both from the tailed slice
        # would drop the intent header whenever the `session.intent` event
        # fell before the tail window.
        records = [
            record
            for record in (
                logread.parse_record(ln)
                for ln in logread.read_lines(path, record_filter)
            )
            if record is not None
        ]
        instant = (now or (lambda: datetime.now(timezone.utc)))()
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
    flow: bool,
    show_agents: bool,
) -> None:
    """Locate and read shipit's durable per-repo JSONL log.

    REPO is owner/name; omitted, it defaults to the current checkout's repo. The
    path is resolved by the file sink (logsetup), the single source of truth — no
    recomputed platform location. --path prints just that absolute path so an
    agent can `cat`/`grep` it. -f/--follow streams new records; with no flag it
    prints the path plus the last N records, rendered legibly (ts LEVEL logger:
    msg, domain keys trailing); a malformed line is skipped with a stderr note.
    --raw passes the stored lines through unmodified for jq — no parsing, no
    skipping, malformed lines included — UNLESS a filter is active. --events and
    the domain-key filters (--pr/--session/--epic/--ws/--agent/--role) compose
    as AND, apply before the tail count, and work with every view; selecting on
    a field requires parsing, so under an active filter even --raw parses and
    drops a malformed line rather than passing it through. --flow renders the
    session story (intent/theme header, relative times, EPIC-WSnn prefixes;
    --agent-ids reveals agent ids) and implies --events. A log not written yet
    is reported, not crashed.
    """
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
        tail=lines,
        follow=follow,
        raw=raw,
        flow=flow,
        show_agents=show_agents,
    )
    raise SystemExit(run(repo, query=query))
