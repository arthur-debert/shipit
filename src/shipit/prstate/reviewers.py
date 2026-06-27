"""Reviewer adapters — the only place that knows reviewer-specific mechanics.

The state machine and the CLI consume the adapter interface (`required`,
`detect`, `open_threads` on the read side; `request`, `cancel`,
`instruction_files` on the act side) and never branch on a reviewer's name.
Adding a reviewer is adding an adapter to `REGISTRY`; nothing downstream
changes. This is what keeps the core stable as the coding-agent landscape
shifts.
"""

from __future__ import annotations

from . import ghapi
from .model import PullContext, ReviewLifecycle, Thread


class ReviewerAdapter:
    """Base adapter. Subclasses define the read side (`matches`, `detect`) and
    the act side (`request`, `cancel`); `instruction_files` declares where the
    reviewer's per-repo code-review instructions live."""

    name: str = ""
    # Whether this adapter HAS a request mechanism (a real `review_requested`
    # edge it can place + the #614 attach-verification). Best-effort
    # auto-triggering backends (Gemini) set this False and can never be a
    # required, gating reviewer. WHICH requestable adapters are *currently*
    # required is NOT decided here — it is the config knob in
    # `reviewers_config` (release#622); this flag only marks eligibility.
    requestable: bool = False
    # Whether this reviewer is addressed by a GitHub `review_requested` edge (so
    # `requested_logins` is meaningful for it). App reviewers (Copilot,
    # CodeRabbit) do; LOCAL backends (codex / agy) are run+posted synchronously
    # and have no requested edge — they override this False so base `detect`
    # never reads `requested_logins` for them.
    has_requested_edge: bool = True
    # Repo-relative path(s) of this reviewer's code-review instruction file(s).
    # Structure only: the adapter declares the location; whether content ships
    # there is a per-reviewer onboarding decision.
    instruction_files: tuple[str, ...] = ()

    def matches(self, login: str) -> bool:
        raise NotImplementedError

    def _rerun(self, ctx: PullContext) -> bool:
        """This reviewer's rerun policy for `ctx` (default False = review-once).

        rerun comes from config (`reviewers_config.reviewer_rerun`), threaded
        into the context at the build site. False is the shipped default for
        EVERY reviewer (all reviewers are token-billed / cost a model run, so
        re-reviewing each push is explicit opt-in)."""
        return ctx.reviewer_rerun.get(self.name, False)

    def detect(self, ctx: PullContext) -> ReviewLifecycle:
        """Where this reviewer stands — rerun-aware, shared across adapters.

        The lifecycle depends on the reviewer's rerun flag:

          * rerun=False (default, review-once): a non-DISMISSED review by this
            reviewer on ANY commit of the PR reads DONE — it is NEVER stale
            after a push. The reviewer won't be asked to look again, so an
            earlier-head review still satisfies the gate.
          * rerun=True (opt-in, head-strict): the review must be on the CURRENT
            head to count DONE; a review only on an older head is stale and the
            reviewer reads back as REQUESTED (needs re-request for the new head).

        When no review counts: REQUESTED if the reviewer is currently requested
        (only for adapters with a real requested edge — `has_requested_edge`),
        else NOT_REQUESTED. A DISMISSED review is a retracted verdict and never
        counts as done. Adapters differ only by `matches` / `has_requested_edge`
        / `requestable`, NOT by this detection algorithm (Gemini, which signals
        weakly and is not requestable, is the one exception and overrides this)."""
        candidates = (
            ctx.reviews_on_head() if self._rerun(ctx) else ctx.reviews_any_head()
        )
        if any(self.matches(r.author) and r.state != "DISMISSED" for r in candidates):
            return self._done_state(ctx)
        if self.has_requested_edge and any(
            self.matches(login) for login in ctx.requested_logins
        ):
            return ReviewLifecycle.REQUESTED
        return ReviewLifecycle.NOT_REQUESTED

    def request(self, pr: int) -> bool:
        """Request — or re-request, same call — this reviewer on `pr`.

        Returns True when a request was actually placed, False when the
        reviewer has no request mechanism (auto-triggering / best-effort
        backends). Re-request after a fixup push is not a separate verb:
        the state machine's never-requested vs stale-after-push distinction
        is a read-side concern (`state._has_stale_review`); the act is the
        same either way.

        Placement only: True means the call was accepted, not that the
        `review_requested` edge exists — GitHub can silently drop the attach
        (release#614). The `pr review request` verb verifies the edge for
        every adapter that returns True, generically; False-returning
        (no-mechanism) adapters are never verified.
        """
        raise NotImplementedError

    def cancel(self, pr: int) -> bool:
        """Withdraw a pending review request on `pr`.

        Returns True when a request was withdrawn, False when there is no
        request mechanism to withdraw from (no-op backends).
        """
        raise NotImplementedError

    def authored_threads(self, ctx: PullContext) -> list[Thread]:
        """All threads (resolved or not) rooted in a comment by this reviewer."""
        return [t for t in ctx.threads if t.author and self.matches(t.author)]

    def open_threads(self, ctx: PullContext) -> list[Thread]:
        """Unresolved threads by this reviewer — the ones still needing action."""
        return [t for t in self.authored_threads(ctx) if not t.is_resolved]

    def _done_state(self, ctx: PullContext) -> ReviewLifecycle:
        return (
            ReviewLifecycle.DONE_COMMENTS
            if self.authored_threads(ctx)
            else ReviewLifecycle.DONE_CLEAN
        )


class CopilotAdapter(ReviewerAdapter):
    """Copilot posts a discrete review object on the PR head SHA.

    Copilot has a real `review_requested` edge and no observable mid-review
    signal, so it goes REQUESTED -> DONE. Whether an earlier-head review counts
    as done is the per-reviewer rerun policy (see base `detect`): review-once
    (default) counts any-head; rerun=True is head-strict (an earlier-head review
    is stale and the reviewer reads back REQUESTED for the new head).
    """

    name = "copilot"
    requestable = True
    instruction_files = (".github/copilot-instructions.md",)

    def matches(self, login: str) -> bool:
        return "copilot" in login.lower()

    def request(self, pr: int) -> bool:
        # `gh pr edit --add-reviewer @copilot` — GraphQL with the bot's real
        # node_id (via ghapi.pr_edit_reviewer; the REST requested_reviewers
        # POST silently no-ops for Copilot). Re-request is the same call.
        ghapi.pr_edit_reviewer(pr, "@copilot")
        return True

    def cancel(self, pr: int) -> bool:
        ghapi.pr_edit_reviewer(pr, "@copilot", remove=True)
        return True


class CodeRabbitAdapter(ReviewerAdapter):
    """CodeRabbit is a requestable GitHub App that posts a discrete review on the
    PR head SHA — structurally the same model as Copilot. It is being PILOTED on
    the phos-org repos (the only place the App is installed); a pilot repo opts
    in via the `[reviewers]` table in its `.shipit.toml`. It is NOT in the
    default required set: on a repo without the App, the request edge silently
    drops (#613-style) and a required gate would park every PR at
    REVIEWS_PENDING. Whether it gates is a config decision, not an adapter
    property — this adapter only declares CodeRabbit *requestable* (it has a
    real request edge + the #614 attach-verification, so it is ELIGIBLE to be
    required wherever the App is installed).

    When a repo requires both Copilot and CodeRabbit, the policy is
    parallel-required, not fallback: each gates Ready, so a PR is reviewed only
    when BOTH have a fresh review on the current head. The accepted trade-off is
    availability — one required reviewer's outage holds Ready until it recovers —
    in exchange for always-on dual coverage and no single point of failure on
    review *quality*.

    Structurally identical to Copilot: a real `review_requested` edge and a
    discrete head-SHA review. Whether an earlier-head review counts as done is
    the per-reviewer rerun policy (base `detect`), not an adapter property. The
    request goes through `gh pr edit --add-reviewer` (the GraphQL path that
    resolves the App's real node id and creates a real `review_requested` edge)
    — so the generic #614 attach-verification in `pr review request` applies
    unchanged: a silently dropped attach fails loud.
    """

    name = "coderabbit"
    requestable = True
    instruction_files = (".coderabbit.yaml",)
    # The reviewer handle `gh pr edit --add-reviewer` resolves to the App's node
    # id. CodeRabbit's bot login on submitted reviews / pending requests is
    # `coderabbitai[bot]`; `matches` keys off the stable `coderabbit` substring.
    _REVIEWER_HANDLE = "coderabbitai[bot]"

    def matches(self, login: str) -> bool:
        return "coderabbit" in login.lower()

    def request(self, pr: int) -> bool:
        # Same GraphQL add-reviewer path Copilot uses: it resolves the App's
        # real node id and creates a real review_requested edge (the REST
        # requested_reviewers POST silently no-ops for App reviewers).
        ghapi.pr_edit_reviewer(pr, self._REVIEWER_HANDLE)
        return True

    def cancel(self, pr: int) -> bool:
        ghapi.pr_edit_reviewer(pr, self._REVIEWER_HANDLE, remove=True)
        return True


class GeminiAdapter(ReviewerAdapter):
    """Gemini signals weakly and is best-effort.

    The app triggers automatically (no discrete request event); an eyes reaction
    from the bot means it is looking; a review or issue comment means it is done.
    It goes over quota silently, so the state machine treats a timed-out Gemini
    as skipped rather than blocking Ready — that timing decision lives in the
    state machine, not here.

    Crucially, **Gemini reviews a PR once and does not re-review pushes** — so a
    review on *any* commit of this PR counts as done, unlike Copilot's
    head-strict model. (The eyes reaction is not commit-scoped and lingers after
    the review, so a fixup that creates a new head would otherwise read as a
    fresh "in_progress" forever.) This per-reviewer difference is exactly what
    the adapter layer exists to hold.
    """

    name = "gemini"
    requestable = False  # auto-triggers; no request edge, so never a required gate
    has_requested_edge = False  # no requested edge; overrides detect entirely anyway
    # Declared location only — no content shipped until Gemini is onboarded
    # as a required reviewer.
    instruction_files = (".gemini/styleguide.md",)

    def matches(self, login: str) -> bool:
        return "gemini" in login.lower()

    def request(self, pr: int) -> bool:
        # The Gemini app auto-triggers on PR open; there is no request
        # mechanism, and it is best-effort anyway — a no-op, not an error.
        return False

    def cancel(self, pr: int) -> bool:
        return False

    def detect(self, ctx: PullContext) -> ReviewLifecycle:
        # Any-head, not head-strict: Gemini won't review the new head again.
        # A DISMISSED review is retracted, so it doesn't count as done.
        if any(self.matches(r.author) and r.state != "DISMISSED" for r in ctx.reviews):
            return self._done_state(ctx)
        if any(
            self.matches((c.get("user") or {}).get("login", ""))
            for c in ctx.issue_comments
        ):
            return ReviewLifecycle.DONE_COMMENTS
        if self._is_looking(ctx):
            return ReviewLifecycle.IN_PROGRESS
        return ReviewLifecycle.NOT_REQUESTED

    def _is_looking(self, ctx: PullContext) -> bool:
        return any(
            r.get("content") == "eyes"
            and self.matches((r.get("user") or {}).get("login", ""))
            for r in ctx.reactions
        )


class _LocalReviewAdapter(ReviewerAdapter):
    """A LOCAL review backend (codex / agy) surfaced as a reviewer adapter.

    Unlike the GitHub-App reviewers (Copilot / CodeRabbit), these reviewers do
    not exist as an installed App that GitHub auto-triggers or that an
    `--add-reviewer` edge addresses. The review is GENERATED locally — the agent
    CLI runs in the PR checkout — and POSTED as the agent's own bot identity. In
    release that runs through its `review` service's run-and-post, so `request`
    is SYNCHRONOUS (it runs the review + posts it now); there is no
    `review_requested` edge to place or withdraw, so `cancel` is a no-op.

    In shipit the local-agent review *execution* engine lives in
    `shipit.review` (PRF01-WS07): this adapter's `request` LAZILY imports
    `shipit.review.service`. Since OBS03 the run is ASYNC — `request` detaches the
    agent run and returns IN-FLIGHT rather than blocking for the length of a model
    run (see `request` for the inverted contract). The import is lazy so the
    optional `review` extra (pyjwt) is only pulled in when a local review is
    actually requested. The DETECTION path is fully intact — `detect` reads an
    existing local-agent review exactly as in release.

    Detection is the shared rerun-aware base `detect`: review-once (default)
    counts a non-DISMISSED review by `matches` on any head as done; rerun=True
    is head-strict. There is no requested edge for a local reviewer
    (`has_requested_edge = False`), so `requested_logins` is never consulted —
    either a counting review exists (done) or the reviewer hasn't run
    (NOT_REQUESTED). The bot login is matched on BOTH the GitHub App `[bot]` suffix
    AND a stable slug fragment (`codex-review` / `agy-review`) — so a future
    prefix (`adr-codex-review[bot]`) still matches, but a bare human login that
    merely contains `codex` / `agy` (e.g. `codexdev`, `agytron`) does NOT, which
    would otherwise misread as a bot review and falsely report DONE. The
    user-specific app-name prefix (e.g. `adr-`) is never hardcoded.
    """

    requestable = True
    # No `review_requested` edge: the review is generated + posted synchronously,
    # so base `detect` must never consult `requested_logins` for these.
    has_requested_edge = False
    # The stable bot-login slug fragment this reviewer matches (set by each
    # subclass). `matches` requires the `[bot]` suffix AND this fragment.
    bot_slug_fragment: str = ""

    def matches(self, login: str) -> bool:
        # Require the GitHub App `[bot]` SUFFIX (not just the substring
        # anywhere) AND the stable slug fragment. `adr-codex-review[bot]` /
        # `adr-agy-review[bot]` end with `[bot]`, so they still match; a login
        # that merely contains `[bot]` mid-string (e.g. `x[bot]y`) does not.
        low = login.lower()
        return low.endswith("[bot]") and self.bot_slug_fragment in low

    def request(self, pr: int) -> bool:
        """DETACH a local-agent review and return IN-FLIGHT (OBS03).

        Fire-and-forget: this does the cheap, synchronous work — resolve the PR's
        ``(repo, head_sha)``, open the OBS02 ``in_progress`` funnel check run — then
        spawns a DETACHED child process that runs the agent over the PR diff, posts
        the verdict as the bot, and closes that SAME check run to its terminal
        state. It returns immediately (``True`` = in-flight); the OUTCOME is read
        LATER from the PR (the funnel check run + the posted review), never from
        this return. This inverts the pre-OBS03 contract — there is no longer a
        blocking model run inside `request`. There is still no `review_requested`
        edge, so a local reviewer is never edge-verified.

        `shipit.review.service` is imported LAZILY here, so the optional `review`
        extra (pyjwt) is only pulled in when a local review is actually requested —
        the detection path and every non-local reviewer stay free of that
        dependency. The agent's per-reviewer `model` / `instructions` / `timeout`
        (the `[reviewers]` options) are read from `.shipit.toml` and threaded to
        the detached child.

        Any failure in the SYNCHRONOUS part — a `gh`/auth failure resolving the PR,
        a spawn failure — is normalized to `ghapi.GhError`, the one error type the
        `pr review request` CLI renders as a clean message + exit 1, so a request
        never crashes with a raw traceback. (A failure INSIDE the detached child
        resolves to a visible failed/timed-out check run on the PR, not to this
        return — that is the whole point of detaching.)
        """
        # Lazy: keep the optional `review`/pyjwt import off the detection path
        # and out of every non-local reviewer. `review` never imports `prstate`,
        # so this one-way edge has no cycle.
        from . import reviewers_config

        try:
            from ..review import service
        except ImportError as exc:  # pragma: no cover - only when the extra is absent
            raise ghapi.GhError(
                f"{self.name}-local review needs the optional `review` extra "
                f"(pyjwt): install shipit with `pip install 'shipit[review]'`. ({exc})"
            ) from exc

        options = reviewers_config.reviewer_run_options(self.name)
        run_kwargs: dict[str, object] = {"as_app": True}
        if "model" in options:
            run_kwargs["model"] = options["model"]
        if "instructions" in options:
            run_kwargs["instructions_path"] = options["instructions"]
        if "timeout" in options:
            run_kwargs["timeout"] = options["timeout"]

        try:
            service.start_detached_review(self.name, pr, **run_kwargs)
        except ghapi.GhError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalize every failure mode uniformly
            raise ghapi.GhError(
                f"{self.name}-local review failed on #{pr}: {exc}"
            ) from exc
        return True

    def cancel(self, pr: int) -> bool:
        """No-op: a posted review can't be withdrawn.

        A local reviewer leaves a real, submitted review rather than a pending
        `review_requested` edge — there is nothing to cancel. Returns False, the
        same shape a no-mechanism backend uses.
        """
        return False


class CodexAdapter(_LocalReviewAdapter):
    """Codex — a LOCAL review backend posted as the `adr-codex-review[bot]`
    identity. See :class:`_LocalReviewAdapter` for the synchronous-request /
    no-cancel / head-strict contract."""

    name = "codex"
    instruction_files = (".github/codex-review-instructions.md",)
    bot_slug_fragment = "codex-review"


class AgyAdapter(_LocalReviewAdapter):
    """Agy — a LOCAL review backend posted as the `adr-agy-review[bot]` identity.

    Matches on the `agy-review` slug fragment + `[bot]` suffix (NOT `gemini`:
    the bot login is `adr-agy-review`, and `gemini` belongs to the separate
    auto-triggering GeminiAdapter). See :class:`_LocalReviewAdapter` for the
    request/cancel/detect contract."""

    name = "agy"
    instruction_files = (".github/agy-review-instructions.md",)
    bot_slug_fragment = "agy-review"


# The adapter CATALOG: every reviewer the engine knows how to read/request. This
# is the registry (#558) — adding a backend is adding an adapter here. WHICH of
# these gate Ready is NOT decided here: that is the config knob in
# `reviewers_config` (release#622), default [copilot] (coderabbit is a
# phos-org pilot, opted in per-repo). codex / agy are LOCAL review backends
# (generated + posted locally), unified under the same adapter interface.
REGISTRY: list[ReviewerAdapter] = [
    CopilotAdapter(),
    CodeRabbitAdapter(),
    GeminiAdapter(),
    CodexAdapter(),
    AgyAdapter(),
]


# Process-lifetime cache of the resolved config (required adapters + the
# per-reviewer rerun map). Resolving reads the consumer's `.shipit.toml`
# `[reviewers]` table; `evaluate` resolves the required set on every call, so
# caching avoids re-reading + re-validating the config each time. The config
# cannot change mid-command, so caching for the process lifetime is safe. The
# adapter set is held as an IMMUTABLE tuple so a caller mutating the returned
# list can't corrupt the cache; tests reset it via `_reset_required_cache()`.
_REQUIRED_CACHE: tuple[ReviewerAdapter, ...] | None = None
_RERUN_CACHE: dict[str, bool] | None = None


def _resolve_config() -> None:
    """Resolve the required adapters + rerun map from config into the caches."""
    global _REQUIRED_CACHE, _RERUN_CACHE
    if _REQUIRED_CACHE is not None and _RERUN_CACHE is not None:
        return
    from . import reviewers_config

    override = reviewers_config.load_override()
    resolved = reviewers_config.resolve_reviewers(override)
    names = tuple(resolved)
    _REQUIRED_CACHE = tuple(reviewers_config.required_reviewers(names))
    _RERUN_CACHE = dict(resolved)


def required_reviewers() -> list[ReviewerAdapter]:
    """The currently-required reviewer adapters, resolved from config (cached).

    The required SET is data (`reviewers_config`: a shipped default plus a
    per-repo `.shipit.toml` `[reviewers]` override), not the registry's structure — so
    swapping/re-ordering required reviewers is a one-line config edit. Names map
    back to these adapters; an unknown name fails loud. Resolved once per
    process (see `_REQUIRED_CACHE`); each call returns a FRESH list copy, so a
    caller may mutate it freely without disturbing the cache.
    """
    _resolve_config()
    assert _REQUIRED_CACHE is not None
    return list(_REQUIRED_CACHE)


def reviewer_rerun() -> dict[str, bool]:
    """The per-reviewer rerun policy (name -> bool), resolved from config (cached).

    Default False for every required reviewer that doesn't opt in. Threaded into
    the `PullContext` at the build site so adapter detection is head-strict only
    for rerun=True reviewers and review-once (any-head) for everyone else."""
    _resolve_config()
    assert _RERUN_CACHE is not None
    return dict(_RERUN_CACHE)


def _reset_required_cache() -> None:
    """Clear the resolved-config cache — for tests that vary the config."""
    global _REQUIRED_CACHE, _RERUN_CACHE
    _REQUIRED_CACHE = None
    _RERUN_CACHE = None


def by_name(name: str) -> ReviewerAdapter | None:
    """Look an adapter up by its registry name (the `--reviewer` selector)."""
    for r in REGISTRY:
        if r.name == name.lower():
            return r
    return None
