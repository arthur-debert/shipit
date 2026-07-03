"""The ``gh`` Tool adapter — shipit's single GitHub boundary (ADR-0028).

Every call that shells out to ``gh`` lives here, so the rest of the package is
pure and unit-testable by patching this one module. PROC02-WS01 (ADR-0028,
glassbox PRD) merged the PR-state engine's former second boundary
(``shipit/prstate/ghapi.py``) into this adapter: the REST and GraphQL helpers,
the pagination-merging helper (defined exactly once, here), and the PR-flow acts
(``pr_ready`` / ``pr_edit_reviewer`` / …) all live here — building a ``gh`` argv
anywhere else is a review defect. The git half of the old combined boundary
lives in its own adapter, :mod:`shipit.git` (PROC02-WS03): building a ``git``
argv here is as much a defect as building a ``gh`` argv there.

Execution routes through the one Exec runner (ADR-0028): every call here is an
Exec via :func:`shipit.execrun.run` with a stated timeout, one structured
record per Exec, and central redaction (:mod:`shipit.redact`) applied to
everything the runner logs or attaches to an error. A failed invocation raises
the single transport error :class:`shipit.execrun.ExecError` — this boundary
defines no error class of its own (the legacy ``GhError`` is deleted, no
alias). The one SEMANTIC error raised here is the engine's user-renderable
:class:`shipit.prstate.errors.PrStateError`, for the failure the Exec record
cannot carry — a ``gh`` call that exited 0 but returned an unusable answer
(a GraphQL response body carrying ``errors``).

The READ surface returns the existing core value objects (PROC03, ADR-0028):
a repo read returns a :class:`shipit.identity.Repo`, a PR-core read returns a
:class:`shipit.pr.PR` (with its :class:`shipit.identity.Sha`-typed head) built
through the ONE :func:`shipit.pr.core_from_node` boundary — never an
adapter-shaped parallel snapshot type. The fleet reads' PR-lifecycle
projection is this adapter's own small frozen value (:class:`HeadPr`, minted
by :func:`pr_for_head`) — scan-shaped, not a parallel PR. Raw JSON survives
only in the documented escapes: the field-list read :func:`pr_view` (whose
extra fields — head branch name, base oid — feed richer views) and the
engine-shaped node read :func:`pr_meta`. A data-shape failure on an Exec that
succeeded
(unparseable/empty JSON, a malformed slug) raises :class:`ValueError` at this
boundary, the same posture :func:`owner_kind` / :func:`default_branch`
already take.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import execrun
from .execrun import ExecError

if TYPE_CHECKING:  # the value objects gh returns; runtime import is deferred
    from .identity import Repo
    from .pr import PR, PrId

# The engine's semantic error is safe to import here: `prstate.errors` is a
# leaf module (stdlib-only, imports nothing from shipit), so no cycle — while
# `identity` composes over THIS module and stays a deferred import below.
from .prstate.errors import PrStateError

#: The adapter's own logger (ADR-0029). The TRANSPORT record for every call is
#: the Exec runner's (one record per Exec on ``shipit.exec``); this boundary
#: records only what the runner cannot see — the GraphQL semantic failure, and
#: the draft-flip milestone it performs on the engine's behalf.
logger = logging.getLogger("shipit.gh")

#: Stated per-Exec timeout (ADR-0028: every Exec carries one; nothing hangs by
#: default). Every call here talks to GitHub, so all get the runner's generous
#: network default.
_NETWORK_TIMEOUT: float = execrun.DEFAULT_TIMEOUT


class UnknownPr:
    """Sentinel: a head's PR state could NOT be read — distinct from ``None`` (no PR).

    :func:`pr_for_head` returns this singleton when ``gh`` failed for a reason OTHER
    than "the branch has no PR" (auth/network/rate-limit), or returned empty/malformed
    output — i.e. whenever the PR state is genuinely *undetermined*, as opposed to
    provably absent (``None``). Keeping the two apart is the whole point of TRE02-WS03:
    a Tree whose PR state is UNKNOWN must never be reclaimed by ``gc`` AND its presence
    must be surfaced (the incomplete-sweep warning) rather than silently collapsed to
    "no PR". A singleton so callers test it with ``pr is gh.UNKNOWN``.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return "UNKNOWN"


#: The singleton unreadable-PR-state sentinel (see :class:`UnknownPr`).
UNKNOWN = UnknownPr()


@dataclass(frozen=True)
class HeadPr:
    """The PR on one head, as the fleet reads consume it — :func:`pr_for_head`'s
    typed hit.

    A scan-shaped projection owned by this adapter, NOT a parallel
    :class:`shipit.pr.PR` (which models a PR's identity + core for the engine
    paths): ``tree list`` / ``tree gc`` / ``spawn`` need exactly the lifecycle
    fields they branch on — the PR ``number``, its ``state``
    (``OPEN``/``MERGED``/``CLOSED``, upper-cased at construction), whether it is
    a draft, and the base it targets. Frozen and thin (ADR-0021); minted only by
    :func:`_head_pr_from_json`, where the shape validation lives, so no caller
    ever sees the raw ``gh pr view --json`` dict (PROC03).
    """

    number: int
    state: str
    is_draft: bool
    base_ref: str

    @property
    def display_state(self) -> str:
        """The fleet's ONE state vocabulary: an open draft reads as ``DRAFT``
        (the turn-signal the dev cycle hinges on), otherwise the GitHub state
        verbatim (``OPEN`` / ``MERGED`` / ``CLOSED``). Both fleet renderers
        (``tree list``'s label, ``gc``'s classifier input) read this property,
        so the draft normalization is written down exactly once.
        """
        if self.state == "OPEN" and self.is_draft:
            return "DRAFT"
        return self.state


def _head_pr_from_json(data: dict) -> HeadPr:
    """Build the :class:`HeadPr` from a ``gh pr view --json`` payload — the ONE
    place this wire shape is read.

    Fail-loud on shape drift (the ``pr_core``/:func:`shipit.pr.core_from_node`
    posture): a payload whose load-bearing fields are missing or mistyped raises
    :class:`ValueError` naming the offending field, so a malformed answer can
    never flow on as a half-usable snapshot (the old ``#None None`` bug).
    ``state`` and ``baseRefName`` are stripped and ``state`` upper-cased here,
    so callers compare against the GitHub vocabulary without re-normalizing —
    the validation checks the stripped value, so the returned snapshot must
    carry the stripped value too (whitespace would silently break the
    ``base_ref`` equality checks in ``spawn``).
    """
    number = data.get("number")
    if isinstance(number, bool) or not isinstance(number, int):
        raise ValueError(
            f"malformed `gh pr view` payload: number must be an int, got {number!r}"
        )
    state = data.get("state")
    if not isinstance(state, str) or not state.strip():
        raise ValueError(
            f"malformed `gh pr view` payload: state must be a non-empty str, "
            f"got {state!r}"
        )
    is_draft = data.get("isDraft")
    if not isinstance(is_draft, bool):
        raise ValueError(
            f"malformed `gh pr view` payload: isDraft must be a bool, got {is_draft!r}"
        )
    base_ref = data.get("baseRefName")
    if not isinstance(base_ref, str) or not base_ref.strip():
        raise ValueError(
            f"malformed `gh pr view` payload: baseRefName must be a non-empty str, "
            f"got {base_ref!r}"
        )
    return HeadPr(
        number=number,
        state=state.strip().upper(),
        is_draft=is_draft,
        base_ref=base_ref.strip(),
    )


def _token_env(token: str | None) -> dict[str, str] | None:
    """The env override that makes ``gh`` authenticate as ``token``.

    ``None`` leaves the user's normal ``gh`` auth in place. Otherwise sets
    ``GH_TOKEN=<token>``; :func:`_run` also *removes* any ``GITHUB_TOKEN`` from
    the child env (rather than blanking it — an empty-but-set var still reads as
    "set" to many tools, and its precedence vs ``GH_TOKEN`` is gh-version
    dependent) so the call authenticates as EXACTLY the token we pass — the seam
    for posting a review AS a GitHub App installation. An installation token
    (``ghs_…``) is a normal bearer token to ``gh``.
    """
    if token is None:
        return None
    return {"GH_TOKEN": token}


def _run(
    args: list[str],
    *,
    input_text: str | None = None,
    cwd: str | None = None,
    token: str | None = None,
    timeout: float | None = _NETWORK_TIMEOUT,
) -> str:
    """Run a command through the Exec runner, returning stdout.

    Raises :class:`ExecError` on failure — the runner normalizes a nonzero
    exit, a timeout expiry, and a missing binary into that one transport error,
    records each Exec exactly once, and redacts everything it logs or raises
    (:mod:`shipit.redact`), so nothing secret rides a record or an exception
    to a sink. The token / child env / stdin body are never logged at all.

    ``token``, when given, runs the subprocess with ``GH_TOKEN=<token>`` (and
    ``GITHUB_TOKEN`` removed) so a ``gh`` call authenticates as that token
    rather than the user's login (see :func:`_token_env`). ``replace_env`` is
    what makes the *removal* possible — an env merge can only add or override.
    """
    env = None
    replace_env = False
    if token is not None:
        import os

        # Drop GITHUB_TOKEN entirely (not blank it) so only GH_TOKEN remains.
        env = {k: v for k, v in os.environ.items() if k != "GITHUB_TOKEN"}
        env.update(_token_env(token))
        replace_env = True
    result = execrun.run(
        args,
        input=input_text,
        cwd=cwd,
        env=env,
        replace_env=replace_env,
        timeout=timeout,
    )
    return result.stdout


def _run_probe(
    args: list[str],
    *,
    cwd: str | None = None,
    timeout: float | None = _NETWORK_TIMEOUT,
) -> execrun.ExecResult:
    """Run a command whose nonzero exit is a NORMAL answer, not a failure.

    ``check=False`` through the runner (ADR-0028): the Exec still gets its one
    record, but at DEBUG — a no-PR lookup, an absent-ref check, or a
    not-a-checkout read happens on every routine scan/hook and must not spray
    ERROR records over normal flows. The caller branches on the result's
    ``rc``/``stderr`` instead of catching :class:`ExecError` (which the runner
    still raises for launch-level failures: missing binary, timeout).
    """
    return execrun.run(args, cwd=cwd, check=False, timeout=timeout)


# --------------------------------------------------------------------------
# gh api
# --------------------------------------------------------------------------


def rest(
    path: str,
    *,
    method: str | None = None,
    body: object | None = None,
    fields: dict[str, str] | None = None,
    paginate: bool = False,
    token: str | None = None,
) -> object:
    """Call ``gh api <path>`` and return the parsed JSON.

    ``method`` sets ``--method`` (GET when omitted). ``body``, when given, is
    JSON-encoded and piped to ``gh api --input -`` (the way to send a structured
    request body); ``fields`` sends string parameters as ``-f key=value`` (the
    lighter form for a flat body — the two are alternatives, not companions).
    ``paginate`` adds ``--paginate``; the per-page JSON arrays are
    concatenated into one list. ``token``, when given, authenticates the call as
    that token (a GitHub App installation token) instead of the user's ``gh``
    login — the seam for posting a review AS ``<app-slug>[bot]``.

    Raises :class:`ValueError` when both ``body`` and ``fields`` are given: the
    two are alternative payload forms, and passing both yields an ambiguous
    ``gh api`` invocation.
    """
    if body is not None and fields:
        raise ValueError("rest() takes body or fields, not both")
    args = ["gh", "api", path]
    if method:
        args += ["--method", method]
    if paginate:
        args.append("--paginate")
    for key, value in (fields or {}).items():
        args += ["-f", f"{key}={value}"]
    input_text = None
    if body is not None:
        args += ["--input", "-"]
        input_text = json.dumps(body)
    out = _run(args, input_text=input_text, token=token)
    if not out.strip():
        return None
    if paginate:
        return _merge_paginated(out)
    return json.loads(out)


def _merge_paginated(output: str) -> list:
    """Concatenate the JSON arrays ``gh api --paginate`` emits back-to-back."""
    merged: list = []
    decoder = json.JSONDecoder()
    idx = 0
    text = output.strip()
    while idx < len(text):
        while idx < len(text) and text[idx].isspace():
            idx += 1
        if idx >= len(text):
            break
        value, end = decoder.raw_decode(text, idx)
        if isinstance(value, list):
            merged.extend(value)
        else:
            merged.append(value)
        idx = end
    return merged


def graphql(query: str, **variables: object) -> dict:
    """Run one of the PR-state ENGINE's own GraphQL queries/mutations; return `data`.

    PURPOSE-BUILT for the engine's cursor/pagination + review-thread queries —
    NOT a general-purpose GraphQL boundary. Two deliberate behaviours make it
    correct for every call the engine makes but surprising to a general caller:

      * a variable whose value is ``None`` is OMITTED entirely (not sent as
        null) — so a first-page ``after: $cursor`` defaults to GraphQL null;
        you cannot pass an *explicit* null through this helper; and
      * a str value is forced as a string via ``-f`` (only int/bool type-infer
        via ``-F``) — required for ``ID!`` variables, which must not be coerced
        to a number.

    A future caller needing explicit-null variables, or float/enum/list
    variables, must not reach for this — build that call against ``gh api
    graphql`` directly. Raises :class:`PrStateError` if the response carries
    errors (a semantic failure: the Exec succeeded, the answer is unusable).
    """
    args = ["gh", "api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        # Omit None entirely: an unprovided nullable GraphQL variable defaults
        # to null, which is what a first-page `after: $cursor` wants. Passing
        # it through would send the literal string "None".
        if value is None:
            continue
        # -F type-infers ints/bools; -f forces a string (needed for ID! vars).
        flag = "-F" if isinstance(value, (int, bool)) else "-f"
        args += [flag, f"{key}={value}"]
    payload = json.loads(_run(args))
    if payload.get("errors"):
        # A propagating semantic failure the Exec record cannot carry (the gh
        # call exited 0): record it at ERROR with the exception attached
        # (glassbox spray) before it leaves this boundary. Raise-then-log so the
        # record carries a real traceback — `exc_info=<unraised instance>` would
        # attach only the type+value (its `__traceback__` is still None).
        try:
            raise PrStateError(f"graphql errors: {payload['errors']}")
        except PrStateError:
            logger.error(
                "gh graphql call returned errors (Exec succeeded, answer unusable)",
                exc_info=True,
            )
            raise
    return payload["data"]


# --------------------------------------------------------------------------
# repo identity
# --------------------------------------------------------------------------


def current_repo(*, cwd: str | None = None) -> Repo:
    """The :class:`~shipit.identity.Repo` in ``cwd`` — the current directory if
    omitted (via ``gh``).

    The API-side repo read (the offline/Tree-safe one is
    :func:`shipit.identity.resolve_repo`): ``gh repo view`` resolves the checkout's
    repo — following GitHub's redirect for a transferred/renamed origin, so the
    answer is CANONICAL — and the slug routes through the ONE canonical parser
    (:func:`shipit.identity.repo_from_slug`), typed at the boundary (PROC03).
    Raises :class:`ValueError` when the Exec succeeded but the output is not a
    usable ``owner/name`` slug (a data-shape problem — the transport failure is
    :class:`ExecError`).
    """
    from .identity import repo_from_slug

    out = _run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
        cwd=cwd,
    )
    return repo_from_slug(out)


def repo_canonical(slug: str) -> Repo:
    """Resolve a (possibly aliased/renamed) ``OWNER/NAME`` slug to its canonical
    :class:`~shipit.identity.Repo`.

    GitHub keeps a 307 redirect from an old/aliased slug to the repo's current
    canonical slug. ``gh api`` follows it for GET but NOT for POST, so a write to
    an aliased slug hard-fails with ``HTTP 307``. Normalizing the slug up front,
    where it enters, keeps every write path on the canonical owner/name — and the
    answer is a typed :class:`~shipit.identity.Repo` (PROC03), minted through the
    one canonical parser. Raises :class:`ValueError` on unusable output (the
    transport failure is :class:`ExecError`).
    """
    from .identity import repo_from_slug

    out = _run(
        ["gh", "repo", "view", slug, "--json", "nameWithOwner", "-q", ".nameWithOwner"]
    )
    return repo_from_slug(out)


def pr_view(pr: str, *, repo: str | None = None, json_fields: list[str]) -> dict:
    """``gh pr view <pr> [--repo …] --json <fields>`` → the parsed JSON object.

    The field-list PR read — the raw-JSON escape hatch for fields with no core
    noun yet (head branch name, base oid). The typed core read is
    :func:`pr_core`. The adapter owns the parse (PROC03: callers never
    ``json.loads`` gh output): raises :class:`ExecError` if gh fails (e.g. the
    PR can't be resolved) and :class:`ValueError` when gh exited 0 but the
    output is not a JSON object (``gh pr view --json`` always emits one, so
    anything else is unusable).
    """
    args = ["gh", "pr", "view", pr]
    if repo is not None:
        args += ["--repo", repo]
    args += ["--json", ",".join(json_fields)]
    out = _run(args).strip()
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        raise ValueError(f"unparseable `gh pr view` output for {pr!r}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(
            f"`gh pr view` output for {pr!r} is not a JSON object: {data!r}"
        )
    return data


def pr_core(pr: PrId) -> PR:
    """The :class:`shipit.pr.PR` core of ``pr`` — the TYPED PR read (PROC03).

    Fetches exactly the core field list (:data:`shipit.pr.CORE_JSON_FIELDS`) and
    routes the node through the ONE :func:`shipit.pr.core_from_node` boundary, so
    the returned core carries the :class:`~shipit.identity.Sha`-typed head and no
    caller re-parses the wire shape. The target arrives as a
    :class:`~shipit.pr.PrId` (ADR-0030): the repo identity the core composes
    rides along on it — never re-derived here per fetch.

    Raises :class:`ExecError` on a failed gh call, :class:`ValueError` on
    unusable output (unparseable JSON, a malformed/abbreviated head sha, a
    non-bool ``isDraft``) and :class:`KeyError` when a required core key is
    missing from the node — the fail-loud-core discipline enforced at the wire.
    """
    from .pr import CORE_JSON_FIELDS, core_from_node

    node = pr_view(str(pr.number), repo=pr.slug, json_fields=list(CORE_JSON_FIELDS))
    return core_from_node(node, pr.repo)


def pr_meta(pr: PrId) -> dict:
    """PR-level metadata the PR-state engine needs in one call.

    A raw ``pullRequest`` node (dict), not a typed core: alongside the core
    fields it carries the check rollup + mergeability the readiness view builder
    (:func:`shipit.prstate.fetch.context_from_raw`) consumes — that builder routes
    the core through :func:`shipit.pr.core_from_node` and partitions the checks
    into the existing check value shapes, so no parallel snapshot type is minted
    here (PROC03). The target arrives as a :class:`~shipit.pr.PrId` (ADR-0030):
    the read is pinned to the identity's repo, never to an ambient cwd
    inference.

    Deliberately does NOT fetch ``reviewRequests``: ``gh pr view --json``
    silently omits Bot-typed requested reviewers (a requested Copilot reads as
    ``[]``), so the engine sources requested reviewers from GraphQL instead
    (`prstate.fetch._threads_and_review_requests`).
    """
    return pr_view(
        str(pr.number),
        repo=pr.slug,
        json_fields=[
            "number",
            "headRefOid",
            "baseRefName",
            "isDraft",
            "mergeable",
            "mergeStateStatus",
            "statusCheckRollup",
        ],
    )


def owner_kind(login: str) -> str:
    """The account type of ``login`` — ``"User"`` or ``"Organization"`` (via ``gh api``).

    The ONE identity call that needs the network: :func:`shipit.identity.Repo`
    identity derives locally from the origin remote, but an **Owner**'s *kind* is a
    lazily-resolved enrichment (:func:`shipit.identity.resolve_owner_kind`) read
    from ``gh api users/<login>``, whose ``type`` field is ``User`` for a user
    account and ``Organization`` for an org (the endpoint serves both).

    Raises :class:`ValueError` when the call succeeded but the response is not
    a usable answer (missing/mistyped ``type``) — a data-shape problem, distinct
    from the transport :class:`ExecError` a failed ``gh`` call raises.
    """
    info = rest(f"users/{login}")
    if not isinstance(info, dict) or "type" not in info:
        raise ValueError(f"could not resolve owner kind for {login!r}")
    return str(info["type"])


def default_branch(repo: str) -> str:
    """The repo's default branch name.

    Raises :class:`ValueError` on a response with no usable ``default_branch``
    (a data-shape problem — the transport failure is :class:`ExecError`).
    """
    info = rest(f"repos/{repo}")
    if not isinstance(info, dict) or "default_branch" not in info:
        raise ValueError(f"could not resolve default branch for {repo}")
    return str(info["default_branch"])


# --------------------------------------------------------------------------
# labels
# --------------------------------------------------------------------------


def label_create(repo: str, name: str, *, description: str, color: str) -> None:
    """Create-or-update a label (``gh label create --force`` is idempotent)."""
    _run(
        [
            "gh",
            "label",
            "create",
            name,
            "--repo",
            repo,
            "--description",
            description,
            "--color",
            color,
            "--force",
        ]
    )


# --------------------------------------------------------------------------
# secrets
# --------------------------------------------------------------------------


def secret_set(name: str, value: str, *, repo: str) -> None:
    """Set an Actions secret, passing the value on stdin (never in argv)."""
    _run(["gh", "secret", "set", name, "--repo", repo], input_text=value)


def secret_list(repo: str) -> list[str]:
    """The names of the repo's Actions secrets."""
    out = _run(
        ["gh", "secret", "list", "--repo", repo, "--json", "name", "-q", ".[].name"]
    )
    return [line for line in out.splitlines() if line.strip()]


# --------------------------------------------------------------------------
# PR reads/writes (the git side of a head lives in :mod:`shipit.git`)
# --------------------------------------------------------------------------


def pr_for_head(branch: str, *, cwd: str | None = None) -> HeadPr | None | UnknownPr:
    """The PR whose head is ``branch`` as a :class:`HeadPr` — or ``None`` /
    :data:`UNKNOWN`.

    Reads ``gh pr view <branch> --json number,state,isDraft,baseRefName`` from inside
    the Tree (``cwd``) and returns a THREE-way result, never crashing the fleet scan:

    - the typed :class:`HeadPr` snapshot when a PR is read cleanly;
    - ``None`` when the branch *provably* has no PR — ``gh`` exits non-zero with its
      documented "no pull requests found" message;
    - :data:`UNKNOWN` when the state is *undetermined* — any OTHER ``gh`` failure
      (auth/network/rate-limit) or empty/malformed/non-JSON output, including a
      payload :func:`_head_pr_from_json` rejects (missing/mistyped fields).

    The ``None`` vs :data:`UNKNOWN` split is load-bearing: an unreadable state must
    NOT masquerade as "no PR" (which would let a conservative caller treat it like an
    abandoned Tree). Callers surface UNKNOWN (``gc``'s incomplete-sweep warning) and
    keep treating it conservatively, but they can now tell the two apart.
    """
    try:
        result = _run_probe(
            ["gh", "pr", "view", branch, "--json", "number,state,isDraft,baseRefName"],
            cwd=cwd,
        )
    except ExecError:
        return UNKNOWN
    if result.rc != 0:
        # gh's documented no-PR message is the one provable absence; every other
        # failure (auth/network/rate-limit) leaves the state undetermined.
        return None if NO_PR_MARKER in result.stderr.lower() else UNKNOWN
    out = result.stdout.strip()
    if not out:
        return UNKNOWN
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return UNKNOWN
    if not isinstance(data, dict):
        return UNKNOWN
    try:
        return _head_pr_from_json(data)
    except ValueError:
        # A dict that decoded cleanly but is missing/mistyped its load-bearing
        # fields (e.g. ``{}`` or ``{"number": null, "state": null}``) is NOT a
        # usable PR snapshot — returning it would have rendered as ``#None None``
        # in ``tree list``. The construction boundary rejected it loudly; for
        # this never-crash scan read that means an undetermined state, the same
        # as malformed/non-JSON output above.
        return UNKNOWN


#: ``gh`` exits non-zero with this message when a head simply has no associated
#: PR — the one failure that is a provable *absence*, not an undetermined state
#: (the exit code is a bare ``1`` for both cases, so the stderr message is the
#: only signal). Matched narrowly on purpose so an unrelated failure is never
#: mistaken for a provable absence. :func:`resolve_pr` keys on the SAME marker
#: when branching on :func:`pr_number_probe`'s result, so the per-tool
#: knowledge is written down exactly once.
NO_PR_MARKER = "no pull requests found for branch"


def pr_number_probe() -> execrun.ExecResult:
    """``gh pr view --json number`` for the CURRENT branch, as a probe.

    The mechanics half of "which PR am I on?" — the argv lives here (ADR-0028:
    gh argv built outside the adapter is a defect) while the three-way
    branching (a number / provably no PR / a real gh failure) lives with
    :func:`resolve_pr`, which keys the no-PR case on :data:`NO_PR_MARKER` in
    the result's stderr. A probe because a PR-less branch is this call's
    NORMAL answer on every bare ``pr`` verb (records at DEBUG, never a
    spurious ERROR).
    """
    return _run_probe(["gh", "pr", "view", "--json", "number"])


def resolve_pr(number: int | None, repo: Repo) -> PrId | None:
    """The typed PR target: the given number, or the current branch's PR
    (``None`` if there is none) — minted into a :class:`~shipit.pr.PrId`.

    The PR-target resolver every ``pr`` verb shares (ADR-0030's deliberate
    exception: click validates only the explicit primitive; "which PR" is a
    runtime boundary call, because "no PR for this branch" is a runtime
    outcome, not a usage error). It lives HERE, at the gh adapter (CLI01-WS03
    promoted it out of ``verbs/pr/``), because it is per-tool knowledge over
    :func:`pr_number_probe`'s answer — the three outcomes are kept DISTINCT so
    callers never have to swallow errors to find them:

      * an explicit / resolved PR number  -> minted into a ``PrId``
      * the branch genuinely has no PR    -> ``None`` (a normal state, not an error)
      * a real gh/auth failure            -> raises ``execrun.ExecError``

    ``repo`` is the identity half of the target — the caller's ambient repo
    from the root context (or an explicit override), never re-derived here. An
    explicit ``number`` is minted directly. The no-PR case is teased apart
    from a genuine failure by ``gh``'s own signal (:data:`NO_PR_MARKER`);
    every other failure (missing gh, auth, transient API error) becomes an
    :class:`~shipit.execrun.ExecError` so a verb can surface it as a clean
    stderr + non-zero exit per the PRD. A read-only verb maps ``None`` to
    ``no_pr``; a mutating verb treats both ``None`` and ``ExecError`` as fatal
    — but each decides, because the cases arrive distinct.
    """
    from .pr import PrId

    if number is not None:
        return PrId(repo=repo, number=number)
    result = pr_number_probe()
    if result.rc != 0:
        # "no PR for this branch" is a normal state, not a failure: gh exits
        # non-zero with NO_PR_MARKER (per-tool knowledge written down once,
        # above). Anything else is a real gh/auth failure — surface the failed
        # Exec as its transport error (pre-redacted by the ExecError
        # constructor), never collapse it into None.
        if NO_PR_MARKER in result.stderr.lower():
            return None
        raise ExecError(
            result.argv,
            rc=result.rc,
            stdout=result.stdout,
            stderr=result.stderr,
            duration_ms=result.duration_ms,
        )
    out = result.stdout
    if not out.strip():
        # Defensive: a non-erroring empty body also means no PR.
        return None
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        raise PrStateError(f"unparseable `gh pr view` output: {exc}") from exc
    resolved = data.get("number")
    if resolved is None:
        return None
    # Pass the raw wire value straight into PrId — its construction IS the
    # validation (exact-int, positive, ADR-0030), the same discipline the
    # sibling wire boundary `pr.core_from_node` applies. A silent `int(resolved)`
    # coercion here would defeat that invariant, accepting a `"99"`/`7.0`/`True`
    # from unexpected `gh` output and minting the wrong PR target. Re-raise with
    # the wire context so a malformed number dies at this read, like the JSON
    # decode above.
    try:
        return PrId(repo=repo, number=resolved)
    except ValueError as exc:
        raise PrStateError(f"malformed `gh pr view` number: {exc}") from exc


def pr_url_for_head(branch: str, *, cwd: str | None = None) -> str | None:
    """The URL of the open PR whose head is ``branch``, or ``None``."""
    out = _run(
        [
            "gh",
            "pr",
            "list",
            "--head",
            branch,
            "--state",
            "open",
            "--json",
            "url",
            "-q",
            ".[0].url",
        ],
        cwd=cwd,
    )
    return out.strip() or None


def pr_create(
    *,
    repo: str | None = None,
    base: str | None = None,
    head: str | None = None,
    title: str,
    body: str,
    draft: bool = True,
    cwd: str | None = None,
) -> str:
    """``gh pr create`` (draft by default); returns the new PR's URL.

    The body is passed on stdin (``--body-file -``) so a long, multi-line PR body
    never hits an argv limit.
    """
    args = ["gh", "pr", "create"]
    if repo is not None:
        args += ["--repo", repo]
    if base is not None:
        args += ["--base", base]
    if head is not None:
        args += ["--head", head]
    if draft:
        args.append("--draft")
    args += ["--title", title, "--body-file", "-"]
    return _run(args, input_text=body, cwd=cwd).strip()


# --------------------------------------------------------------------------
# gh — the PR-flow acts (merged from the engine's ghapi boundary, PROC02-WS01)
# --------------------------------------------------------------------------


def pr_edit_reviewer(pr: PrId, reviewer: str, *, remove: bool = False) -> None:
    """Add (or remove) a reviewer on a PR via ``gh pr edit``.

    ``gh pr edit --add-reviewer`` resolves the reviewer handle to its real
    node id and mutates through GraphQL. That path is load-bearing for bot
    reviewers: the REST ``requested_reviewers`` POST silently no-ops for them
    (returns 200 but leaves ``requested_reviewers`` empty) — never swap this
    for the REST call. The target arrives as a :class:`~shipit.pr.PrId`
    (ADR-0030): the repo rides on the identity, never re-resolved here.
    """
    flag = "--remove-reviewer" if remove else "--add-reviewer"
    _run(["gh", "pr", "edit", str(pr.number), "--repo", pr.slug, flag, reviewer])


def pr_ready(pr: PrId, *, undo: bool = False) -> None:
    """Flip a PR's draft flag via ``gh pr ready`` (``--undo`` for ready→draft).

    ``gh pr ready`` is idempotent: flipping a PR that is already in the target
    state prints a notice and exits 0, so callers don't need to pre-check the
    flag to stay safe — they pre-check only to *say* something more useful. The
    target arrives as a :class:`~shipit.pr.PrId` (ADR-0030): the repo rides on
    the identity, never re-resolved here.
    """
    args = ["gh", "pr", "ready", str(pr.number), "--repo", pr.slug]
    if undo:
        args.append("--undo")
    _run(args)
    # The draft-flag flip is the ONE human hand-off signal in the whole cycle
    # (LOG02 convergence): give it a durable INFO milestone at the boundary that
    # performed it — before this, its only record was the Exec runner's DEBUG
    # line, invisible to an INFO-level read of the story.
    logger.info(
        "pr#%s draft flag flipped %s on %s",
        pr.number,
        "ready→draft" if undo else "draft→ready",
        pr.slug,
        extra={"pr": pr.number, "repo": pr.slug},
    )


def pr_review_reply(pr: PrId, comment_id: int, body: str) -> None:
    """Post a threaded reply to an existing PR review comment.

    Wraps ``POST /repos/{owner}/{name}/pulls/{pr}/comments/{comment_id}/replies``
    — the dedicated reply endpoint that threads the new comment under the
    target rather than starting a fresh top-level review comment. ``comment_id``
    is the numeric REST id (the same handle ``pr resolve-thread`` takes); ``body``
    is the reply text. This is the push-back path: reply with rationale, then
    resolve the thread. The target arrives as a :class:`~shipit.pr.PrId`
    (ADR-0030): the repo rides on the identity, never re-resolved here.
    """
    rest(
        f"repos/{pr.slug}/pulls/{pr.number}/comments/{comment_id}/replies",
        method="POST",
        fields={"body": body},
    )
