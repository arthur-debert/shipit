"""logs — locate and read shipit's durable per-repo JSONL log (LOG01-WS04).

The READER half of WS01's file sink: the writer (:mod:`shipit.logsetup`) drops a
durable, per-repo, rotating **JSONL** log (ADR-0029 — one flat JSON object per
record: ``ts``, ``level``, ``logger``, ``msg``, plus domain keys
present-when-bound); this verb finds it and shows it. It NEVER recomputes the
location — it consumes :func:`shipit.logsetup.log_file_path` (``resolve_log_dir``
+ the handler's ``LOG_FILENAME``), the single source of truth, so reader and
writer can never disagree about where the log lives. No platform ``if`` branch,
no bespoke log-dir env var (the path library owns the location, per the glassbox
PRD ``docs/prd/glassbox.md``).

The verb reads JSONL ONLY — a hard cutover, no dual-format sniffing (ADR-0029;
pre-cutover freeform files age out via rotation). Two output modes: the default
renders each record legibly for humans (``ts LEVEL logger: msg [key=value …]``);
``--raw`` passes the stored lines through unmodified — and prints nothing else —
so stdout pipes straight into jq. In the rendered view, a line that is not a
JSON object is skipped with a stderr note, never a crash: the log is diagnosis
data, and one corrupt line (a torn write, a rotation seam) must not take down
the reader. With NO filter active, ``--raw`` does not parse at all — malformed
lines pass through untouched, because judging them is the downstream tool's job.

The reader grows FILTERS, not a sibling (LOG04 / ADR-0032): ``--events`` keeps
only ``event``-tagged dev-cycle records, and one flag per domain key selects on
the record's flat fields — ``--pr <n>``, ``--session <id|current>`` (``current``
resolved via :mod:`shipit.session.current`: the session environment first, the
ephemeral Tree leaf second, ADR-0027), ``--epic <code>``, ``--ws <n>``
(accepting ``1``, ``01``, or ``WS01`` — the display form is never data, so all
three normalize to the int the record carries), ``--agent <id>``, and
``--role <name>``. All AND-composed, applied client-side before the tail count
(the file is bounded by rotation, so whole-file filtering is cheap — no index
until a real slicing gap shows, per the PRD), and uniform across the static,
``--raw``, and ``--follow`` views. Selecting on a field means PARSING, so with
ANY filter active even ``--raw`` parses each line and a malformed one (which
has no fields to match) is dropped rather than passed through — the passthrough
contract holds only when no filter is asked for.

``--flow`` renders the filtered records as the session STORY instead of a
record listing: selection stays here (``--flow`` implies ``--events``; the
domain-key filters compose as usual), the LOOK is the pure renderer's
(:mod:`shipit.flowview` — intent/theme header, relative times, ``EPIC-WSnn:``
prefixes, agent ids behind ``--agent-ids``). A story is a bounded rendering of
the static view, so ``--flow`` refuses ``--raw`` and ``--follow`` rather than
guessing what a raw or followed story would mean.

The repo whose log we read defaults to the current checkout, resolved LOCALLY off
the origin remote (:func:`shipit.identity.resolve_repo`) — the SAME resolver the
sink namespaces the log by (:func:`shipit.logsetup._current_repo`), so a log
written offline is readable offline (reader and writer never disagree on identity,
and neither depends on ``gh``'s API shellout). An explicit ``owner/repo`` argument
overrides it. The repo resolver, the resolution base, and the follow-loop
``sleep`` are injected in tests so nothing touches a real ``$HOME``.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .. import execrun, flowview, identity, logcontext, logsetup, redact
from ..session import current as session_current

#: Default number of trailing lines the no-flag invocation prints.
DEFAULT_TAIL = 50

#: Seconds between polls while following (``-f``); small enough to feel live.
_FOLLOW_INTERVAL = 0.25

#: Exit code when the log file does not exist yet (a clean "nothing to read",
#: not a crash — the path is valid, the run that writes it just hasn't happened).
_EXIT_NO_LOG = 1

#: Exit code for a bad ``owner/repo`` (usage error).
_EXIT_BAD_REPO = 2


def _last_n(lines: list[str], n: int) -> list[str]:
    """The last ``n`` of ``lines``: all when ``n < 0``, none when ``n == 0``,
    else the final ``n``.

    The explicit ``n == 0`` arm guards the ``lines[-0:]`` trap (``-0 == 0``, so a
    naive slice would return EVERY line for ``-n 0`` instead of none).
    """
    if n < 0:
        return lines
    return lines[-n:] if n > 0 else []


def _parse_record(line: str) -> dict[str, Any] | None:
    """The line as ONE JSONL record (a JSON object), or ``None``.

    The single parse every selecting/rendering path shares: only a JSON object
    is a record — any other parse (a torn write, a bare JSON string) is the
    caller's cue to apply its own resilience contract (skip with a note, drop
    silently under a filter), never a crash.
    """
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return None
    return record if isinstance(record, dict) else None


def normalize_ws(value: int | str) -> int:
    """The Work Stream index ``value`` names, as the INT the record carries.

    The CLI never punishes the display form (PRD): ``1``, ``01``, and ``WS01``
    (any case) all name Work Stream 1 — the ``WS`` prefix and zero-padding are
    rendering, stripped here, because the durable record's ``ws`` domain key is
    int-typed (ADR-0032) and selection compares against THAT. Anything else —
    garbage text, or a non-positive index the branch grammar could never have
    written (``shipit.branchid`` derives ``WS00`` to nothing for the same
    reason) — raises :class:`ValueError` for the caller to report as a usage
    error.
    """
    text = str(value).strip()
    if text.upper().startswith("WS"):
        text = text[2:]
    if not text.isdigit():
        raise ValueError(
            f"--ws must be a Work Stream index (1, 01, or WS01); got {value!r}"
        )
    index = int(text)
    if index < 1:
        raise ValueError(
            f"--ws must be a positive Work Stream index (the branch grammar "
            f"starts at WS01); got {value!r}"
        )
    return index


class _Filter:
    """The record filters (LOG04) as ONE predicate.

    Filters compose as AND and are applied BEFORE the tail count and before
    either output mode, so ``-n 5 --pr 231`` means "the last 5 records about
    pr#231" and ``--raw`` pipes exactly the matching stored lines to jq.
    Selection is on the record's flat fields: ``--events`` keeps only records
    carrying an ``event`` field (a dev-cycle event, ADR-0032 — presence is the
    test, never a name list of the reader's own); each domain-key filter
    (``pr``, ``session``, ``epic``, ``ws``, ``agent``, ``role``) keeps records
    whose key EQUALS the value — typed as the record carries it (``pr``/``ws``
    int, the rest strings, ADR-0029/0032), which is why the CLI boundary
    normalizes ``WS01`` to ``1`` before it gets here. A record without the key
    cannot match it: absent means unbound, not wildcard.

    Filtering requires parsing, so with any filter ACTIVE a non-record line
    (blank padding, a torn write) simply cannot match and is dropped silently —
    in both modes: a malformed line's fields are unknowable, and surfacing it
    under a field filter would be a false positive. With NO filter active the
    predicate is vacuously true and both modes keep their unfiltered contracts
    (raw passes malformed lines through; rendered notes them on stderr).
    """

    def __init__(
        self,
        *,
        events_only: bool = False,
        pr: int | None = None,
        session: str | None = None,
        epic: str | None = None,
        ws: int | None = None,
        agent: str | None = None,
        role: str | None = None,
    ) -> None:
        self.events_only = events_only
        self.fields = {
            name: value
            for name, value in {
                "pr": pr,
                "session": session,
                "epic": epic,
                "ws": ws,
                "agent": agent,
                "role": role,
            }.items()
            if value is not None
        }

    @property
    def active(self) -> bool:
        return self.events_only or bool(self.fields)

    def matches_record(self, record: dict[str, Any]) -> bool:
        if self.events_only and "event" not in record:
            return False
        return all(record.get(name) == value for name, value in self.fields.items())

    def matches(self, line: str) -> bool:
        if not self.active:
            return True
        record = _parse_record(line)
        if record is None:
            return False
        return self.matches_record(record)


#: Longest malformed-line snippet quoted in the skip note; enough to identify
#: the line without spraying a whole corrupt record onto stderr.
_SNIPPET_LEN = 80

#: Record fields the renderer places explicitly (everything else — the bound
#: domain keys and event extras — trails as ``key=value``).
_RENDERED_FIELDS = ("ts", "level", "logger", "msg", "exception")


def _render_record(line: str) -> str | None:
    """Render one JSONL record for humans, or ``None`` when the line is not one.

    The legible shape mirrors the console surface (``LEVEL logger: msg``) with
    the durable record's extra facts folded in: the ``ts`` up front (the file's
    reason to exist is the timestamped history) and every remaining flat field —
    the bound domain keys (``pr``, ``session``, …) and event extras — trailing
    as sorted ``key=value`` pairs. An ``exception`` (WS01 flattens tracebacks to
    a string) lands on the following lines, the way stdlib formatting would.

    Only a JSON *object* is a record; any other parse (or a parse failure) is
    the caller's cue to skip the line with a note.
    """
    record = _parse_record(line)
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


def _emit(line: str, *, raw: bool) -> None:
    """Emit one log line in the chosen mode.

    ``raw`` is the jq passthrough: the line goes out exactly as stored, parsed by
    nobody (a malformed line is the downstream tool's to judge). Otherwise the
    line is rendered for humans; a blank line is dropped silently (file padding,
    not a record) and a malformed one is skipped with a stderr note — stdout
    carries only rendered records, and a corrupt line never crashes the reader.
    The malformed-line snippet is the one path that echoes raw file content the
    writer's pipeline never finished redacting (a torn write, a pre-cutover
    freeform line), so it passes through :func:`shipit.redact.redact_text`
    (token/PEM pattern masking) before reaching stderr.

    Every ``print`` flushes: ``-f`` output is piped as often as watched
    (``shipit logs -f --raw | jq .``), and Python block-buffers a
    non-interactive stdout — without the flush, records would sit in the buffer
    instead of streaming live.
    """
    if raw:
        print(line, flush=True)
        return
    if not line.strip():
        return
    rendered = _render_record(line)
    if rendered is None:
        # Redact BEFORE truncating, so a secret straddling the snippet cut is
        # still seen whole by the pattern matcher.
        snippet = redact.redact_text(line)[:_SNIPPET_LEN]
        print(
            f"logs: skipped malformed line: {snippet!r}",
            file=sys.stderr,
            flush=True,
        )
        return
    print(rendered, flush=True)


def _follow(
    path: Path,
    *,
    tail: int,
    raw: bool,
    record_filter: _Filter,
    sleep: Callable[[float], None],
) -> int:
    """Stream the log live (``tail -f``): the last ``tail`` lines, then each
    appended line as it lands — every line through the filter then :func:`_emit`,
    so follow and the static view select and render (or pass through)
    identically. The path header prints only in the human mode: raw stdout is
    reserved for JSONL. Ends cleanly on Ctrl-C (exit 0), the way ``tail -f``
    does. ``sleep`` is injected so a test can drive the poll loop and stop it
    deterministically.
    """
    if not raw:
        print(str(path), flush=True)
    fh = path.open("r", encoding="utf-8", errors="replace")
    try:
        matching = [ln for ln in fh.read().splitlines() if record_filter.matches(ln)]
        for line in _last_n(matching, tail):
            _emit(line, raw=raw)
        # fh is now positioned at EOF; subsequent appends are picked up by readline.
        pending = ""
        while True:
            chunk = fh.readline()
            if chunk:
                # A concurrent write can be read mid-line, so readline() may
                # return a fragment with no trailing newline. Buffer until the
                # newline lands before judging the line: a torn read is not a
                # malformed record, and under an active filter parsing a half
                # line would drop it — permanently, since its remainder (read
                # next) is not valid JSON on its own either.
                pending += chunk
                if not pending.endswith("\n"):
                    continue
                stripped = pending.rstrip("\n")
                pending = ""
                if record_filter.matches(stripped):
                    _emit(stripped, raw=raw)
                continue
            # No new data. The writer is a RotatingFileHandler, so the active
            # shipit.log can be rolled over mid-follow — at which point our open
            # handle points at the stale renamed file and would go silent.
            # Detect it by FILE IDENTITY: rollover is a rename + fresh create,
            # so the path's inode no longer matches our handle's. (A size
            # comparison alone is a race — a busy fresh file can outgrow our
            # old read offset between polls and the shrink would never be
            # seen.) The size check stays for the in-place truncation case,
            # where the inode never changes.
            try:
                disk = path.stat()
                rotated = (
                    disk.st_ino != os.fstat(fh.fileno()).st_ino
                    or disk.st_size < fh.tell()
                )
            except OSError:
                rotated = False  # mid-rotation flicker; retry on the next tick
            if rotated:
                fh.close()
                fh = path.open("r", encoding="utf-8", errors="replace")
                continue
            sleep(_FOLLOW_INTERVAL)
    except KeyboardInterrupt:
        return 0
    finally:
        fh.close()


def _default_repo_slug() -> str:
    """The cwd checkout's canonical slug — resolved LOCALLY off the origin remote.

    Delegates to :func:`shipit.identity.resolve_repo`, the SAME resolver the log
    WRITER namespaces by (:func:`shipit.logsetup._current_repo`), NOT the
    ``gh.current_repo`` API shellout. Reader and writer must agree on the repo
    identity, so a log written in a checkout where ``gh`` is unavailable is still
    found by ``shipit logs`` without an explicit repo. Raises
    :class:`shipit.execrun.ExecError` (no origin remote) or :class:`ValueError`
    (unparseable origin URL), both handled by the caller.
    """
    return identity.resolve_repo().slug


def run(
    repo: str | None = None,
    *,
    path_only: bool = False,
    follow: bool = False,
    raw: bool = False,
    tail: int = DEFAULT_TAIL,
    events_only: bool = False,
    pr: int | None = None,
    session: str | None = None,
    epic: str | None = None,
    ws: int | str | None = None,
    agent: str | None = None,
    role: str | None = None,
    flow: bool = False,
    show_agents: bool = False,
    base_dir: str | Path | None = None,
    current_repo: Callable[[], str] | None = None,
    current_session: Callable[[], str | None] | None = None,
    sleep: Callable[[float], None] | None = None,
    now: Callable[[], datetime] | None = None,
) -> int:
    """Locate (and read) the per-repo JSONL log. Returns an int exit code.

    ``repo`` overrides the default (the cwd checkout, resolved via the injected
    ``current_repo`` boundary). ``path_only`` prints just the resolved absolute
    path and exits 0 — locating the log never depends on it existing yet, so this
    always succeeds. Otherwise the file is read: ``follow`` streams appended lines
    (``tail -f``); the default prints the path plus the last ``tail`` records.
    ``raw`` swaps the human rendering for an unmodified-JSONL passthrough (no
    path header — stdout is pure JSONL for jq) and composes with both views. A
    missing log file is reported on stderr (no traceback) and exits non-zero.

    ``events_only`` / ``pr`` / ``session`` / ``epic`` / ``ws`` / ``agent`` /
    ``role`` are the LOG04 record filters (AND-composed, applied before the
    tail count, uniform across the static/follow/raw views). ``session`` takes
    the sentinel ``current``, resolved via the injected ``current_session``
    boundary (default :func:`shipit.session.current.current_session_id`) —
    unresolvable is a usage error, since the caller asked for a session this
    process is not in. ``ws`` accepts the int or any display form
    (:func:`normalize_ws`); a form that names no Work Stream is a usage error.

    ``flow`` renders the filtered records as the session story instead of a
    listing (:mod:`shipit.flowview`) — it implies ``events_only``, refuses
    ``raw``/``follow``, and ``show_agents`` toggles the agent-id display.

    ``base_dir`` / ``current_repo`` / ``current_session`` / ``sleep`` / ``now``
    are injected boundaries for tests.
    """
    current_repo = current_repo or _default_repo_slug
    if flow and (raw or follow):
        print(
            "logs: --flow is a rendered story view; it does not compose with "
            "--raw or --follow.",
            file=sys.stderr,
        )
        return _EXIT_BAD_REPO
    if flow:
        events_only = True  # --flow implies --events (ADR-0032)
    if ws is not None:
        try:
            ws = normalize_ws(ws)
        except ValueError as exc:
            print(f"logs: {exc}", file=sys.stderr)
            return _EXIT_BAD_REPO
    if session == "current":
        session = (current_session or session_current.current_session_id)()
        if session is None:
            print(
                "logs: --session current, but no session is resolvable — neither "
                f"{logcontext.ENV_PREFIX}SESSION in the environment nor "
                "an ephemeral session-Tree cwd (ADR-0027); pass the session id.",
                file=sys.stderr,
            )
            return _EXIT_BAD_REPO
    record_filter = _Filter(
        events_only=events_only,
        pr=pr,
        session=session,
        epic=epic,
        ws=ws,
        agent=agent,
        role=role,
    )
    try:
        slug = repo if repo is not None else current_repo()
        # The ONE canonical slug parser (ADR-0024): lowercases owner/name, so an
        # API-cased or hand-typed slug resolves the SAME log directory the writer
        # (which namespaces by the canonical Repo identity) filled.
        target = identity.repo_from_slug(slug)
    except execrun.ExecError as exc:
        # Resolving the cwd repo read the local origin remote and failed — not a
        # checkout, or no 'origin'. Keep the verb's promise of a clean message.
        print(
            "logs: could not determine the current repo (not a git checkout, or "
            f"no 'origin' remote); pass an explicit owner/repo. ({exc})",
            file=sys.stderr,
        )
        return _EXIT_BAD_REPO
    except ValueError as exc:
        print(f"logs: {exc}", file=sys.stderr)
        return _EXIT_BAD_REPO

    path = logsetup.log_file_path(target, base_dir=base_dir)

    if path_only:
        print(str(path))
        return 0

    if not path.exists():
        print(
            f"logs: no log yet at {path} — it is created on the first shipit run "
            f"that logs for {target.slug}.",
            file=sys.stderr,
        )
        return _EXIT_NO_LOG

    if follow:
        return _follow(
            path,
            tail=tail,
            raw=raw,
            record_filter=record_filter,
            sleep=sleep or time.sleep,
        )

    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()

    if flow:
        # The story view: same selection (parse → filter → tail), rendering
        # delegated whole to the pure module. A malformed line has no fields to
        # match and is dropped silently — the filter is always active here
        # (--flow implies --events), so the active-filter contract applies.
        records = [
            record
            for record in (_parse_record(ln) for ln in lines)
            if record is not None and record_filter.matches_record(record)
        ]
        instant = (now or (lambda: datetime.now(timezone.utc)))()
        for rendered in flowview.render(
            _last_n(records, tail), now=instant, show_agents=show_agents
        ):
            print(rendered, flush=True)
        return 0

    if not raw:
        print(str(path))
    for line in _last_n([ln for ln in lines if record_filter.matches(ln)], tail):
        _emit(line, raw=raw)
    return 0
