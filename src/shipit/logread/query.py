"""LogQuery — one read of the log, as one frozen value (ADR-0030).

The reader verb's parameter explosion (~20 keyword arguments through
``run()``) collapses here: everything that shapes a read — the AND-composed
:class:`~.records.Filter`, the tail count, and the view toggles
(follow/raw/flow, the flow view's agent-id display) — travels as ONE value,
minted at argv parse by the CLI boundary and handed to the verb whole.
Construction is validation: a query that contradicts itself (``--flow`` with
``--raw`` or ``--follow``) cannot be built, so the verb never re-checks flag
composition.

:func:`build_query` is the minting path the CLI uses — it owns the two
flag-level normalizations that are POLICY, not parsing: ``--flow`` implies
``--events`` (a story is a rendering of the event stream, ADR-0032), and the
Work Stream display form normalizes to the int the record carries
(:func:`~.records.normalize_ws`). Both raise :class:`ValueError` for the
boundary to report as a usage error (exit 2).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .records import Filter, normalize_ws

#: Default number of trailing records the no-flag invocation prints.
DEFAULT_TAIL = 50


@dataclass(frozen=True)
class LogQuery:
    """One read of the durable log: selection + view, as a frozen value.

    ``record_filter`` is the whole selection (AND-composed, applied before
    ``tail`` — see :class:`~.records.Filter`); ``tail`` counts trailing
    records after filtering (``-1`` means all, ``0`` none); ``follow`` streams
    appended records live; ``raw`` swaps human rendering for the unmodified
    JSONL passthrough; ``flow`` renders the records as the session story
    (:mod:`shipit.flowview`), with ``show_agents`` toggling its agent-id
    display. ``flow`` refuses ``raw``/``follow`` at construction — a raw or
    followed story has no defined meaning, so the contradiction is unbuildable
    rather than checked downstream.
    """

    record_filter: Filter = field(default_factory=Filter)
    tail: int = DEFAULT_TAIL
    follow: bool = False
    raw: bool = False
    flow: bool = False
    show_agents: bool = False

    def __post_init__(self) -> None:
        if self.flow and (self.raw or self.follow):
            raise ValueError(
                "--flow is a rendered story view; it does not compose with "
                "--raw or --follow."
            )


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
) -> LogQuery:
    """Mint the :class:`LogQuery` the CLI's flag primitives describe.

    The one place flag values become the frozen query: ``ws`` accepts the int
    or any display form (``1``, ``01``, ``WS01`` — :func:`~.records.normalize_ws`),
    and ``flow`` implies ``events_only`` (ADR-0032) so the story view never
    depends on the caller remembering to ask for events. Raises
    :class:`ValueError` — an out-of-grammar ``ws``, or the ``flow`` ×
    ``raw``/``follow`` contradiction — for the CLI boundary to map to a usage
    error; ``session`` arrives already resolved (the ``current`` sentinel is
    the boundary's job, since resolving it reads the process environment).
    """
    normalized_ws = normalize_ws(ws) if ws is not None else None
    record_filter = Filter(
        events_only=events_only or flow,
        pr=pr,
        session=session,
        epic=epic,
        ws=normalized_ws,
        agent=agent,
        role=role,
    )
    return LogQuery(
        record_filter=record_filter,
        tail=tail,
        follow=follow,
        raw=raw,
        flow=flow,
        show_agents=show_agents,
    )
