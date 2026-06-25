"""`shipit pr next` — do the ONE next action, then report.

The act counterpart to the read-only `pr status`: resolve the PR → gather a
snapshot → evaluate it → route the lifecycle state through the next-action
:mod:`.dispatch`er to the single act, perform it, and report what happened plus
the resulting status. It is the SINGLE-SHOT form of release's looping `wait` —
there is NO polling loop here: `pr next` takes one safe step and returns; the
driver (a human or an outer loop) calls it again.

The dispatcher is a pure decision (state → act); the doing lives in
:class:`_NextActs`, the concrete :class:`~.dispatch.Acts` boundary this verb
injects. Only two acts mutate the PR — the REVIEWS_PENDING request and the READY
flip — and the flip goes through the SAME guarded re-check (`ready.guarded_flip`)
that `pr ready` uses, so `pr next` can never flip a not-actually-ready PR.

Reconcile seam (WS05): the request act needs the canonical reviewer-request
helper `verbs/pr/_request.py::request_reviewers(...)` (attach-verify), which WS05
owns and is NOT on this branch yet. It is routed through ONE spot —
:meth:`_NextActs.request_review` — with a MINIMAL request-via-adapter + basic
attach check standing in for it, marked `# reconcile:` so the coordinator swaps
it to WS05's helper in a single edit at integration.
"""

from __future__ import annotations

import sys

import click

from ...prstate import ghapi
from ...prstate.fetch import attach_state, gather
from ...prstate.reviewers import required_reviewers
from ...prstate.state import TaskStatus, evaluate, no_pr
from . import ready as ready_verb
from . import status as status_verb
from ._resolve import resolve_pr
from .dispatch import dispatch


class _NextActs:
    """The concrete execution boundary `pr next` injects into the dispatcher.

    Each method performs one act against the live PR and returns the line stating
    what it did. `report` is the no-op act (surface the engine's next-action);
    `request_review` and `flip_ready` are the two mutating acts.
    """

    def __init__(self, pr: int) -> None:
        self._pr = pr

    def report(self, status: TaskStatus) -> str:
        # Report-only: nothing mutates. The engine's next_action already carries
        # the right instruction for no_pr / addressing / reviewed / validating /
        # blocked / waiting-reviews, so surface it verbatim.
        return f"no action taken — {status.next_action}"

    def request_review(self, status: TaskStatus) -> str:
        """Request/re-request the pending required reviewers on the head.

        # reconcile: replace this whole body with a single call to WS05's
        # canonical helper — `from .review import request_reviewers` (the
        # `verbs/pr/_request.py::request_reviewers(pr)` attach-verify path) — once
        # WS05 merges. It is isolated to THIS method so the swap is one edit; the
        # dispatcher and the rest of `pr next` are unaffected.

        Minimal stand-in until then: request each pending required reviewer via
        its adapter (the engine boundary's `gh pr edit --add-reviewer`), then do a
        BASIC attach check (re-read the pending requests once and confirm the
        requestable reviewers show up). This is deliberately NOT the full #614
        attach-verify poll — that is WS05's job; this only un-parks the common
        case and fails loud on an outright request error.
        """
        required = required_reviewers()
        # Which required reviewers actually need (re-)requesting. `status.reviewers`
        # maps name -> lifecycle value. A reviewer already REQUESTED / IN_PROGRESS
        # on the head is mid-review — re-poking it would spam the reviewer and
        # contradict the engine's "wait (already requested…)" advice — so it is
        # SKIPPED here (the dispatcher only routes a MIXED state to this act;
        # the all-waiting case it already reports). DONE reviewers are skipped
        # too. What remains is NOT_REQUESTED / stale-after-push — the ones the
        # engine's request/RE-REQUEST clauses name — which is exactly the set to
        # request. (`adapter.request` is the same call for request and re-request.)
        skip = {"done_clean", "done_comments", "requested", "in_progress"}
        pending = [r for r in required if status.reviewers.get(r.name) not in skip]
        requested: list[str] = []
        for adapter in pending:
            # adapter.request returns True when a real request edge was placed;
            # False for a no-mechanism backend (best-effort). A local-agent
            # adapter raises GhError here (execution deferred) — let it propagate
            # to the verb's clean error path.
            if adapter.request(self._pr):
                requested.append(adapter.name)
        if not requested:
            return f"no requestable reviewer to (re-)request — {status.next_action}"
        # Basic attach check: re-read the pending requests once and confirm the
        # reviewers we just requested actually attached. A silent drop is reported
        # (not a hard failure here — WS05's helper owns the polling retry).
        attached_logins, _reviews = attach_state(self._pr)
        low = [login.lower() for login in attached_logins]
        missing = [
            name
            for name in requested
            if not any(_adapter_for(name, required).matches(login) for login in low)
        ]
        line = f"requested review(s): {', '.join(requested)}"
        if missing:
            line += (
                f" — WARNING: not yet attached: {', '.join(missing)} (re-run to retry)"
            )
        return line

    def flip_ready(self, status: TaskStatus) -> str:
        # The single hand-off. Go through the SHARED guarded re-check so a status
        # that moved since `gather` cannot flip a not-ready PR. guarded_flip
        # re-evaluates live and raises NotReady if it is no longer READY.
        flipped = ready_verb.guarded_flip(self._pr)
        return f"flipped draft→ready — {flipped.next_action}"


def _adapter_for(name: str, required):
    for r in required:
        if r.name == name:
            return r
    raise KeyError(name)  # pragma: no cover — name came from `required`


@click.command(name="next")
@click.argument("pr", required=False, type=int)
@click.option(
    "--json", "as_json", is_flag=True, help="Emit the resulting status as JSON."
)
def cmd(pr: int | None, as_json: bool) -> None:
    """Do the single next action for PR, then report it + the resulting status.

    PR is the number; omitted, it resolves the current branch's PR. Performs at
    most ONE step (request a review / flip draft→ready / report waiting/blocked)
    — the single-shot form of a wait loop, never a polling loop.
    """
    raise SystemExit(run(pr, as_json=as_json))


def run(pr: int | None = None, *, as_json: bool = False) -> int:
    """Resolve → gather → evaluate → dispatch → perform one act → report.

    Returns 0 on a performed/ reported action; non-zero on a real gh/auth failure
    or a guarded-flip refusal (a status that moved out of READY between gather and
    flip). A branch with no PR is a normal report (the act is the human's: create
    a draft PR), exit 0 — matching `pr status`.
    """
    try:
        resolved = resolve_pr(pr)
        if resolved is None:
            status = no_pr()
            _report(_NextActs(0).report(status), status, as_json=as_json)
            return 0
        status = evaluate(gather(resolved), required=required_reviewers())
        action = dispatch(status, _NextActs(resolved))
    except ready_verb.NotReady as exc:
        # The guarded flip refused: the PR moved out of READY between the gather
        # and the flip. Report the real (refused) status as a clean non-zero.
        print(f"refusing to flip: {exc}", file=sys.stderr)
        return 1
    except ghapi.GhError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    # Re-read the status AFTER a mutating act so the reported snapshot reflects
    # what just happened (e.g. a freshly-requested reviewer now REQUESTED). A
    # second gather is cheap and keeps the report honest; on report-only acts it
    # is the same status. Skipped when there is no PR (handled above).
    final = evaluate(gather(resolved), required=required_reviewers())
    _report(action, final, as_json=as_json)
    return 0


def _report(action: str, status: TaskStatus, *, as_json: bool) -> None:
    """Print the action taken, then the resulting status (reusing `status`'s render).

    Reuses :func:`status._emit` for the status block so `pr next` and `pr status`
    render identically. The action line is printed first (text) or carried under
    an ``action`` key (JSON).
    """
    if as_json:
        import json

        payload = {"action": action, "status": status.to_dict()}
        print(json.dumps(payload, indent=2))
        return
    print(f"action: {action}")
    status_verb._emit(status, as_json=False)
