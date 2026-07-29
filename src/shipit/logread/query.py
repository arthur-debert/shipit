"""``logread/query`` — one read of the log, as one frozen value; construction is validation."""

from __future__ import annotations

from dataclasses import dataclass, field

from .records import Filter, normalize_ws

#: Default number of trailing records the no-flag invocation prints.
DEFAULT_TAIL = 50


@dataclass(frozen=True)
class LogQuery:
    """One read of the durable log; ``tail`` counts trailing records AFTER filtering."""

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
    reviewer: str | None = None,
    run_id: str | None = None,
    round_id: str | None = None,
    tail: int = DEFAULT_TAIL,
    follow: bool = False,
    raw: bool = False,
    flow: bool = False,
    show_agents: bool = False,
) -> LogQuery:
    """Mint the :class:`LogQuery` the CLI's flags describe; ``flow`` implies ``events_only``.

    ``session`` must arrive already resolved — the ``current`` sentinel reads the
    environment, so it is the boundary's job. A bad value raises :class:`ValueError`.
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
        reviewer=reviewer,
        run_id=run_id,
        round_id=round_id,
    )
    return LogQuery(
        record_filter=record_filter,
        tail=tail,
        follow=follow,
        raw=raw,
        flow=flow,
        show_agents=show_agents,
    )
