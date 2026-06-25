"""service — the programmatic run-and-post path for a local review backend.

This is the in-process entry the ``prstate`` reviewer adapters call to GENERATE
a review and POST it, without shelling through a CLI.

Two functions, layered:

  * :func:`generate_review` — resolve the backend, preflight it, build the shared
    prompt over a resolved PR's diff, and run the backend → the parsed review
    dict. No GitHub posting.
  * :func:`run_and_post` — resolve the PR, generate the review, and post it via
    :func:`shipit.review.post.post_review`, returning a small result dict.

``prstate`` may import this module (``prstate → review`` is a ONE-WAY edge —
``review`` never imports ``prstate``), so the reviewer adapters' synchronous
``request`` can run a local review here.
"""

from __future__ import annotations

from . import post
from .backends import get_backend
from .diff import resolve_pr
from .instructions import load_instructions
from .prompt import build_prompt
from .schema import REVIEW_SCHEMA


def generate_review(
    agent: str,
    ctx,
    *,
    instructions_path: str | None = None,
    model: str = "pro",
) -> dict:
    """Run ``agent`` over ``ctx``'s diff and return the parsed review dict.

    Resolves the backend, preflights it (a missing CLI raises
    :class:`~shipit.review.backends.base.BackendUnavailable`, which is allowed to
    propagate — these are LOCAL backends and a missing binary must fail loud),
    loads the review instructions (bundled default unless ``instructions_path``
    is given), builds the shared prompt over ``ctx.diff`` (with the schema
    described in-prose only for ``agy``, which has no native schema enforcement),
    and runs the backend in ``ctx.workdir``.
    """
    backend = get_backend(agent, model=model)
    backend.preflight()
    instructions = load_instructions(instructions_path)
    prompt = build_prompt(instructions, ctx.diff, schema_inline=(agent == "agy"))
    return backend.run(prompt, REVIEW_SCHEMA, cwd=ctx.workdir)


def run_and_post(
    agent: str,
    pr: int,
    *,
    repo: str | None = None,
    model: str = "pro",
    instructions_path: str | None = None,
    event: str | None = None,
    as_app: bool = True,
    dry_run: bool = False,
) -> dict:
    """Resolve ``pr``, generate a review with ``agent``, and post it.

    Returns ``{"review": <dict>, "post": <dict>, "ctx_repo": <str|None>,
    "pr": <int>}``.

    With ``as_app=True`` (the default), the review is posted AS the agent's
    GitHub App (``adr-<agent>-review[bot]``) — the App credentials are sourced
    from Doppler at post time (:mod:`shipit.review.ghauth`); there is no local
    app-registration step to precheck. ``event=None`` lets the review's own
    summary status drive APPROVE/REQUEST_CHANGES/COMMENT (the bot is a distinct
    identity, so a self-review 422 does not apply).
    """
    ctx = resolve_pr(pr, repo=repo)
    review = generate_review(
        agent, ctx, instructions_path=instructions_path, model=model
    )
    result = post.post_review(
        review,
        ctx,
        agent_name=agent,
        event=event,
        dry_run=dry_run,
        as_app=as_app,
    )
    return {"review": review, "post": result, "ctx_repo": ctx.repo, "pr": pr}
