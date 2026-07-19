"""Required-status-check discovery for the branch ruleset.

Ported from release-core's ``verbs/apply_ruleset.py``: the ruleset must require
the TARGET repo's own checks, never a captured set from another repo. Two modes,
tried in order:

1. From runs — the job names of the latest default-branch run of each
   required-PR-check workflow (purely the GitHub API; catches matrix expansion
   and ``name:`` overrides that static parsing can't see).
2. Static — when no runs exist yet (the onboarding case), the contexts the
   workflow files themselves declare, resolving reusable-workflow calls to their
   nested ``<caller> / <called>`` contexts.

A workflow contributes a required check only when it triggers on ``pull_request``
WITHOUT a ``paths:`` / ``paths-ignore:`` filter (a path-filtered job is
conditional and would deadlock unrelated PRs — release#416) and is not
``copilot-review`` (the retired Copilot-request caller: it only requested a
review, never a check. ADR-0031 made the engine the sole requester and deleted
the workflow here; the filter stays because portfolio repos still carry the
file until their own cutover). The bare
caller-job name of a reusable call is never a reported context and would deadlock
every PR — release#602.

Static discovery NEVER guesses a context it cannot name (#1056). A job whose
reported check name is statically unpredictable — a ``${{ … }}`` display name,
or a ``strategy.matrix`` job (it reports ``id (values)``, never the bare id) —
is DROPPED, not resolved to its job id: guessing there minted a phantom
``<caller> / run`` on ``lex-fmt/lex`` that no job ever reported, and requiring
it bricked every PR. Each drop is warned loudly (stderr + WARNING, LOG02). The
guard is per-workflow: :func:`discover` writes the ruleset only when EVERY PR
workflow still contributes at least one certain context; if any PR workflow is
left with zero, discovery REFUSES (a :class:`Discovery` carrying a ``refusal``
message, no checks) rather than write a weaker rule, and gh-setup surfaces the
refusal as a failed ruleset pass demanding explicit ``--checks``.

This module also owns the workflow-file parsing seam gh-setup's Actions
access-level verify pass (#739) uses: :func:`is_reusable_workflow` /
:func:`publishes_reusable_workflows` detect ``workflow_call`` publishers, from
the local checkout or via the contents API — same loader, same YAML-1.1
``on:`` gotcha handling. And the CONSUMER side of the same grammar:
:func:`workflow_pin_refs` enumerates the ``owner/repo/wf.yml@vN`` pins the
RELEASE CALLER (:data:`RELEASE_CALLER_WORKFLOW`) dispatches, so the release
preflight pin gate (#917) can verify each ``@vN`` resolves on its publisher
before GitHub emits a raw HTTP 422 at dispatch.
"""

from __future__ import annotations

import base64
import glob
import logging
import os
import re
import sys
from dataclasses import dataclass

import yaml

from . import execrun, gh

#: This module's logger (LOG02 convergence): the degraded discovery outcomes —
#: a too-deep nesting, an unresolvable reusable workflow — used to go ONLY to
#: stderr, reaching no sink; they now also record at WARNING (the prints stay
#: as the user-facing surface).
logger = logging.getLogger("shipit.checks")

_NON_CHECK_WORKFLOWS = ("copilot-review.yml", "copilot-review.yaml")


class _WorkflowLoader(yaml.SafeLoader):
    """A YAML loader that does NOT treat ``on``/``off``/``yes``/``no`` as bools.

    GitHub workflows key their triggers under ``on:``. PyYAML follows YAML 1.1,
    where ``on`` is the boolean ``True`` — so a naive parse turns the ``on:`` key
    into ``True`` and every workflow looks trigger-less. This loader strips the
    YAML-1.1 bool resolver and re-adds a YAML-1.2 one (``true``/``false`` only),
    so ``on`` stays the string key it is in the file (the same result ``yq``
    gives, which is what release-core relied on).
    """


_WorkflowLoader.yaml_implicit_resolvers = {
    ch: [(tag, rx) for tag, rx in resolvers if tag != "tag:yaml.org,2002:bool"]
    for ch, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_BOOL_1_2 = re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$")
for _ch in "tTfF":
    _WorkflowLoader.add_implicit_resolver("tag:yaml.org,2002:bool", _BOOL_1_2, _ch)

# Cross-repo reusable reference: owner/repo/.github/workflows/x.yml@ref
_USES_RE = re.compile(
    r"^(?P<owner>[^/]+)/(?P<repo>[^/]+)/(?P<path>[^@]+\.ya?ml)@(?P<ref>.+)$"
)
# The floating v-major ref shape (ADR-0010): `v1`, `v2`, … — a BRANCH
# advance-major.yml force-moves onto each stable tag. The pin gate enumerates
# ONLY these (a `@main`, a SHA, a `@v1.2.3` release tag is outside the
# bootstrap contract and gets no phantom "bootstrap the v-major branch"
# remediation — #917, workflows.lex §10).
_VN_RE = re.compile(r"^v\d+$")
# GitHub caps reusable-workflow nesting at 4; the same cap bounds the recursion.
_MAX_NESTING = 4

#: The blessed stage-choice release-caller filename (workflows.lex §8, the
#: shape `shipit wf test` lints) shipit installs and dogfoods — the ONE
#: workflow a release dispatch resolves its ``@vN`` stage pins from. The pin
#: gate scopes to it, NOT every ``.github/workflows`` file: an unrelated
#: CI/manual/experimental workflow's stale cross-repo ref is not part of the
#: release dispatch and must never block a cut (#917).
RELEASE_CALLER_WORKFLOW = "shipit-release.yml"


# --------------------------------------------------------------------------
# Discovery value objects (#1056)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DroppedJob:
    """A job static discovery could not statically name, so dropped (#1056).

    ``job`` is the job label (caller-prefixed — ``<caller> / <called>`` — at
    nested levels). ``reason`` is ``"matrix"`` (a ``strategy.matrix`` job reports
    ``id (values)``, never the bare id) or ``"dynamic name"`` (its display name
    carries a ``${{ … }}`` expression). Either way the reported context can't be
    predicted statically, so the job contributes no required check.
    """

    job: str
    reason: str


@dataclass(frozen=True)
class WorkflowContexts:
    """One PR workflow's static-discovery outcome (#1056).

    ``certain`` is the set of contexts its jobs reliably report (sorted, unique);
    ``dropped`` is every job dropped as statically unpredictable. A workflow with
    an empty ``certain`` is one discovery could not confidently name — it drives
    :func:`discover`'s refusal.
    """

    workflow: str
    certain: tuple[str, ...]
    dropped: tuple[DroppedJob, ...]


@dataclass(frozen=True)
class Discovery:
    """The required-check discovery result (#1056).

    ``checks`` is the set to require. ``refusal`` is ``None`` on success, or an
    actionable message when static discovery left a PR workflow contributing zero
    certain contexts: the caller must NOT write the ruleset (``checks`` is empty
    then) and must surface the refusal as a failed pass demanding ``--checks``.
    """

    checks: tuple[str, ...]
    refusal: str | None = None


# --------------------------------------------------------------------------
# Pure helpers — no network, fixture-tested.
# --------------------------------------------------------------------------


def workflow_triggers(workflow: object) -> list[str]:
    """The ``on:`` triggers of a parsed workflow, normalized to a flat list."""
    if not isinstance(workflow, dict):
        return []
    on = workflow.get("on")
    if isinstance(on, str):
        return [on]
    if isinstance(on, list):
        return [str(x) for x in on]
    if isinstance(on, dict):
        return list(on.keys())
    return []


def is_pr_workflow(workflow: object) -> bool:
    """True if the workflow triggers on ``pull_request``."""
    return "pull_request" in workflow_triggers(workflow)


def is_reusable_workflow(workflow: object) -> bool:
    """True if the workflow triggers on ``workflow_call`` (a reusable-workflow
    publisher — the callable side of ADR-0010's distribution model)."""
    return "workflow_call" in workflow_triggers(workflow)


def pr_trigger_is_path_filtered(workflow: object) -> bool:
    """True if the ``pull_request`` trigger carries a ``paths``/``paths-ignore``.

    Path-filtered jobs are conditional, so requiring them deadlocks any PR that
    doesn't touch the matching files (release#416). Only unfiltered
    ``pull_request`` workflows are always-run checks and safe to require.
    """
    if not isinstance(workflow, dict):
        return False
    on = workflow.get("on")
    if not isinstance(on, dict):
        return False
    pr = on.get("pull_request")
    if not isinstance(pr, dict):
        return False
    return bool(pr.get("paths") or pr.get("paths-ignore"))


def checks_json(checks: list[str]) -> list[dict]:
    """Map check names → the ``required_status_checks`` array, dropping empties."""
    return [{"context": name} for name in checks if name != ""]


def job_display_name(job_id: str, job: object) -> str:
    """A job's reported check name: a static ``name:`` override, else the job id.

    A ``name:`` carrying a ``${{ … }}`` expression can't be resolved statically,
    so the job id is used (runs-based detection sees the rendered name). Callers
    that must NOT guess such a name gate on :func:`job_unpredictable` first.
    """
    if isinstance(job, dict):
        name = job.get("name")
        if isinstance(name, str) and "${{" not in name:
            return name
    return job_id


def job_unpredictable(job: object) -> str | None:
    """Why a job's reported check name is statically unpredictable, else ``None``.

    Returns ``"matrix"`` when the job has a ``strategy.matrix`` (each lane reports
    ``id (values)`` / a per-lane ``name``, never the bare id) or ``"dynamic
    name"`` when its ``name:`` carries a ``${{ … }}`` expression that only renders
    at run time. Static discovery must DROP such a job rather than guess its job
    id: on ``lex-fmt/lex`` a matrix ``run`` job minted the phantom ``checks /
    run`` context that bricked every PR (#1056). Matrix is reported in preference
    to a dynamic name (a matrix job's per-lane name is the same unpredictability).
    """
    if not isinstance(job, dict):
        return None
    strategy = job.get("strategy")
    if isinstance(strategy, dict) and "matrix" in strategy:
        return "matrix"
    name = job.get("name")
    if isinstance(name, str) and "${{" in name:
        return "dynamic name"
    return None


def _called_job_included(job: object, with_values: dict) -> bool:
    """Whether a called workflow's job contributes a required context.

    A job-level ``if: inputs.<key>`` (optionally ``${{ … }}``-wrapped) is
    resolved against the caller's ``with:`` — reusable CI conditions its optional
    jobs exactly this way, so a consumer that doesn't enable the feature must
    not have the context required. Any other ``if:`` is included: a skipped job
    still reports a (``skipped``) check run, which satisfies the ruleset.
    """
    if not isinstance(job, dict):
        return True
    cond = job.get("if")
    if cond is None:
        return True
    if not isinstance(cond, str):
        return bool(cond)
    expr = cond.strip()
    if expr.startswith("${{") and expr.endswith("}}"):
        expr = expr[3:-2].strip()
    match = re.fullmatch(r"inputs\.([A-Za-z_][A-Za-z0-9_-]*)", expr)
    if match is None:
        return True
    value = with_values.get(match.group(1))
    return value is True or (isinstance(value, str) and value.lower() == "true")


# --------------------------------------------------------------------------
# Workflow loading — local fs and the contents API.
# --------------------------------------------------------------------------


def _load_yaml_text(text: str) -> object:
    return yaml.load(text, Loader=_WorkflowLoader)


def _load_yaml_file(path: str) -> object:
    with open(path, encoding="utf-8") as fh:
        return yaml.load(fh, Loader=_WorkflowLoader)


def _fetch_called_workflow(uses: str, toplevel: str | None) -> object:
    """The parsed definition of a ``uses:`` target.

    A repo-local ``./…`` reference is read from the same working tree the caller
    was read from; a cross-repo ``owner/repo/path@ref`` is fetched at its PINNED
    ref via the contents API — the only source matching what the consumer runs.
    """
    if uses.startswith("./"):
        if toplevel is None:
            raise ValueError(f"local reusable ref with no checkout: {uses!r}")
        return _load_yaml_file(os.path.join(toplevel, uses[2:]))
    match = _USES_RE.match(uses)
    if match is None:
        raise ValueError(f"unrecognized reusable-workflow reference: {uses!r}")
    obj = gh.rest(
        "repos/{owner}/{repo}/contents/{path}?ref={ref}".format(**match.groupdict())
    )
    if not isinstance(obj, dict) or not isinstance(obj.get("content"), str):
        raise ValueError(f"no content for reusable workflow: {uses!r}")
    text = base64.b64decode(obj["content"]).decode("utf-8")
    return _load_yaml_text(text)


def _job_contexts(
    job_id: str,
    job: object,
    *,
    toplevel: str | None,
    cache: dict[str, object],
    depth: int = 0,
) -> tuple[list[str], list[DroppedJob]]:
    """The status-check contexts one workflow job reports, and the jobs dropped.

    A plain job reports its display name — UNLESS it is statically unpredictable
    (a ``${{ … }}`` display name or a ``strategy.matrix``, see
    :func:`job_unpredictable`), in which case it contributes no context and is
    recorded as a :class:`DroppedJob` (#1056): guessing the bare job id there
    minted phantom required checks that bricked rulesets. A job that CALLS a
    reusable workflow reports one ``<caller> / <called>`` context per called job
    (the bare caller name is never reported — release#602), recursing through
    nesting; an unpredictable CALLER drops the whole subtree, since its
    ``<caller>`` prefix can't be named. Nested drops are surfaced caller-prefixed
    too, so the warning names the full path.
    """
    unpredictable = job_unpredictable(job)
    uses = job.get("uses") if isinstance(job, dict) else None
    display = job_display_name(job_id, job)
    if not isinstance(uses, str):
        if unpredictable is not None:
            return [], [DroppedJob(job=job_id, reason=unpredictable)]
        return [display], []
    if unpredictable is not None:
        # The caller's own name is unpredictable, so no nested `<caller> / …`
        # context can be named — drop the entire subtree, don't recurse.
        return [], [DroppedJob(job=job_id, reason=unpredictable)]
    if depth >= _MAX_NESTING:
        # Degraded-but-continuing (LOG02): discovery drops this job's contexts
        # and carries on — loud on both the user surface and the durable record.
        logger.warning("reusable-workflow nesting too deep at job %r", job_id)
        print(
            f"warning: reusable-workflow nesting too deep at job {job_id!r}",
            file=sys.stderr,
        )
        return [], []
    if uses not in cache:
        try:
            cache[uses] = _fetch_called_workflow(uses, toplevel)
        except (execrun.ExecError, ValueError, OSError, yaml.YAMLError) as exc:
            # Degraded-but-continuing: the called workflow's contexts are
            # skipped, not fatal — warning with the exception attached.
            logger.warning(
                "cannot resolve reusable workflow %r called by job %r",
                uses,
                job_id,
                exc_info=True,
            )
            print(
                f"warning: cannot resolve reusable workflow {uses!r} "
                f"called by job {job_id!r}: {exc}",
                file=sys.stderr,
            )
            cache[uses] = None
    doc = cache[uses]
    if not isinstance(doc, dict) or not isinstance(doc.get("jobs"), dict):
        return [], []
    with_values = job.get("with") if isinstance(job.get("with"), dict) else {}
    out: list[str] = []
    dropped: list[DroppedJob] = []
    for called_id, called in doc["jobs"].items():
        if not _called_job_included(called, with_values):
            continue
        ctxs, sub_dropped = _job_contexts(
            called_id, called, toplevel=toplevel, cache=cache, depth=depth + 1
        )
        for ctx in ctxs:
            out.append(f"{display} / {ctx}")
        for d in sub_dropped:
            dropped.append(DroppedJob(job=f"{display} / {d.job}", reason=d.reason))
    return out, dropped


# --------------------------------------------------------------------------
# Discovery — boundary calls into gh / the filesystem.
# --------------------------------------------------------------------------


def pr_workflow_paths(workflows_dir: str) -> list[str]:
    """``.github/workflows/<name>`` of the local always-run PR-check workflows."""
    names: list[str] = []
    for ext in ("*.yml", "*.yaml"):
        names.extend(sorted(glob.glob(os.path.join(workflows_dir, ext))))
    paths: list[str] = []
    for path in names:
        base = os.path.basename(path)
        if base in _NON_CHECK_WORKFLOWS:
            continue
        try:
            doc = _load_yaml_file(path)
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            continue
        if is_pr_workflow(doc) and not pr_trigger_is_path_filtered(doc):
            paths.append(f".github/workflows/{base}")
    return paths


def workflow_pin_refs(caller_path: str) -> list[tuple[str, str]]:
    """The floating-major ``@vN`` reusable-workflow pins the RELEASE CALLER
    dispatches, as sorted-unique ``(owner/repo, ref)`` tuples.

    Reads the ONE release caller workflow (``caller_path`` — the blessed
    :data:`RELEASE_CALLER_WORKFLOW` shape) and collects each job's
    ``uses: owner/repo/path@vN`` — the pins GitHub resolves when it dispatches
    that caller (#917). Scoped to the caller, NOT every ``.github/workflows``
    file: an unrelated CI/manual/experimental workflow with a stale cross-repo
    ref is not part of the release dispatch, so a missing ref there must never
    block a cut. The caller's OWN direct pins are sufficient to catch the
    missing-``@vN`` failure — the whole stage chain rides the SAME floating
    ``@vN`` branch, so if that branch is absent the caller's very first pin
    already fails at GitHub's workflow-resolution step.

    Filtered to the ``@vN`` shape (:data:`_VN_RE`): the gate's refusal and the
    §10 bootstrap contract are floating-v-major-BRANCH specific (advance-major
    force-moves a ``vN`` branch — ADR-0010), so a non-``vN`` pin (``@main``, a
    SHA, a ``@v1.2.3`` release tag) is outside this contract and gets no
    phantom "bootstrap the v-major branch" remediation. NOT a pin either, so
    skipped: a repo-local ``./…`` ``uses:`` (resolved against the caller's own
    repo — no remote ref to miss) and a step/job ``uses:`` naming an ACTION
    rather than a workflow (``actions/checkout@v6`` — no ``.yml`` path, so
    :data:`_USES_RE` does not match). A caller that is absent or will not parse
    contributes nothing (the same tolerance :func:`pr_workflow_paths` keeps).

    Reuses the reusable-workflow ref grammar (:data:`_USES_RE`), the same
    parser :func:`_fetch_called_workflow` resolves ``uses:`` targets with, so a
    preflight pin gate enumerates exactly the refs a dispatch will resolve."""
    try:
        doc = _load_yaml_file(caller_path)
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return []
    jobs = doc.get("jobs") if isinstance(doc, dict) else None
    if not isinstance(jobs, dict):
        return []
    pins: set[tuple[str, str]] = set()
    for job in jobs.values():
        uses = job.get("uses") if isinstance(job, dict) else None
        if not isinstance(uses, str) or uses.startswith("./"):
            continue
        match = _USES_RE.match(uses)
        if match is None:
            continue
        fields = match.groupdict()
        if not _VN_RE.match(fields["ref"]):
            continue
        pins.add((f"{fields['owner']}/{fields['repo']}", fields["ref"]))
    return sorted(pins)


def publishes_reusable_workflows(repo: str, *, toplevel: str | None) -> bool:
    """Whether ``repo`` publishes any reusable (``workflow_call``) workflow.

    The publisher detection of gh-setup's access-level verify pass (#739).
    With ``toplevel`` (the ambient-checkout case) the LOCAL
    ``.github/workflows/`` files are scanned — the same source ruleset
    discovery reads; a file that won't parse is skipped (it publishes
    nothing). Without it (an explicitly named remote repo) the contents API
    serves the listing and each file body; a missing workflows directory
    (HTTP 404) means "not a publisher".

    Raises :class:`~shipit.execrun.ExecError` on any OTHER remote read
    failure (auth, transport), and :class:`ValueError` on a malformed remote
    directory payload: the caller must report "could not inspect", never
    mistake an unreadable repo for a verified non-publisher.
    """
    if toplevel is not None:
        workflows_dir = os.path.join(toplevel, ".github", "workflows")
        paths: list[str] = []
        for ext in ("*.yml", "*.yaml"):
            paths.extend(sorted(glob.glob(os.path.join(workflows_dir, ext))))
        for path in paths:
            try:
                doc = _load_yaml_file(path)
            except (OSError, UnicodeDecodeError, yaml.YAMLError):
                continue
            if is_reusable_workflow(doc):
                return True
        return False
    try:
        listing = gh.rest(f"repos/{repo}/contents/.github/workflows")
    except execrun.ExecError as exc:
        if "HTTP 404" in exc.stderr:
            return False  # no workflows directory at all — not a publisher
        raise
    if not isinstance(listing, list):
        raise ValueError(
            f"malformed repos/{repo}/contents/.github/workflows payload: "
            "expected a list"
        )
    for entry in listing:
        name = entry.get("name") if isinstance(entry, dict) else None
        if not isinstance(name, str) or not name.endswith((".yml", ".yaml")):
            continue
        obj = gh.rest(f"repos/{repo}/contents/.github/workflows/{name}")
        if not isinstance(obj, dict) or not isinstance(obj.get("content"), str):
            continue
        try:
            doc = _load_yaml_text(base64.b64decode(obj["content"]).decode("utf-8"))
        except (ValueError, yaml.YAMLError):
            continue
        if is_reusable_workflow(doc):
            return True
    return False


def checks_from_runs(repo: str, default_branch: str, paths: list[str]) -> list[str]:
    """Job-run names from the latest default-branch run of each workflow path."""
    found: set[str] = set()
    try:
        workflows_obj = gh.rest(f"repos/{repo}/actions/workflows")
    except execrun.ExecError:
        workflows_obj = None
    by_path: dict[str, object] = {}
    if isinstance(workflows_obj, dict):
        for wf in workflows_obj.get("workflows") or []:
            if isinstance(wf, dict) and wf.get("path"):
                by_path[wf["path"]] = wf.get("id")
    for path in paths:
        wid = by_path.get(path)
        if not wid:
            continue
        try:
            runs_obj = gh.rest(
                f"repos/{repo}/actions/workflows/{wid}/runs"
                f"?branch={default_branch}&per_page=1"
            )
        except execrun.ExecError:
            continue
        runs = runs_obj.get("workflow_runs") if isinstance(runs_obj, dict) else None
        if not runs:
            continue
        run_id = runs[0].get("id") if isinstance(runs[0], dict) else None
        if not run_id:
            continue
        try:
            jobs_obj = gh.rest(
                f"repos/{repo}/actions/runs/{run_id}/jobs", paginate=True
            )
        except execrun.ExecError:
            continue
        for job in jobs_obj or []:
            if isinstance(job, dict) and job.get("name"):
                found.add(job["name"])
    return sorted(found)


def _warn_dropped(workflow: str, dropped: DroppedJob) -> None:
    """Loudly report one dropped job (LOG02): user-facing stderr + WARNING."""
    logger.warning(
        "dropping statically-unpredictable job %r in %s (%s)",
        dropped.job,
        workflow,
        dropped.reason,
    )
    print(
        f"warning: {workflow}: dropping job {dropped.job!r} ({dropped.reason}) — "
        "its reported check name can't be predicted statically, so requiring it "
        "would brick every PR",
        file=sys.stderr,
    )


def static_workflow_contexts(toplevel: str, paths: list[str]) -> list[WorkflowContexts]:
    """Per-PR-workflow static discovery — one :class:`WorkflowContexts` per path.

    Each entry names the workflow, the contexts its jobs certainly report, and
    every job dropped as statically unpredictable (#1056); each drop is warned
    loudly as it is found. A workflow that will not parse (or declares no
    ``jobs``) yields an empty ``certain`` — :func:`discover` treats a
    zero-certain workflow as one it could not name and refuses over it.
    """
    cache: dict[str, object] = {}
    results: list[WorkflowContexts] = []
    for path in paths:
        try:
            doc = _load_yaml_file(os.path.join(toplevel, path))
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            results.append(WorkflowContexts(workflow=path, certain=(), dropped=()))
            continue
        if not isinstance(doc, dict) or not isinstance(doc.get("jobs"), dict):
            results.append(WorkflowContexts(workflow=path, certain=(), dropped=()))
            continue
        certain: set[str] = set()
        dropped: list[DroppedJob] = []
        for job_id, job in doc["jobs"].items():
            ctxs, drops = _job_contexts(job_id, job, toplevel=toplevel, cache=cache)
            certain.update(c for c in ctxs if c != "")
            dropped.extend(drops)
        for d in dropped:
            _warn_dropped(path, d)
        results.append(
            WorkflowContexts(
                workflow=path,
                certain=tuple(sorted(certain)),
                dropped=tuple(dropped),
            )
        )
    return results


def checks_from_workflows(toplevel: str, paths: list[str]) -> list[str]:
    """The flat certain-context set static discovery names across ``paths``.

    A thin flattening of :func:`static_workflow_contexts` — the union of every
    workflow's certain contexts, dropping the statically-unpredictable jobs
    (#1056). Callers that need the per-workflow refusal guard use
    :func:`discover`, which reads the structured contexts directly.
    """
    certain: set[str] = set()
    for wf in static_workflow_contexts(toplevel, paths):
        certain.update(wf.certain)
    return sorted(certain)


def _refusal_message(workflows: list[WorkflowContexts]) -> str:
    """The actionable refusal shown when a PR workflow contributes zero certain
    contexts (#1056): why the write is refused plus the per-workflow breakdown."""
    lines = [
        "required-check auto-discovery could not confidently name every PR "
        "workflow's checks, so it refuses to write a ruleset that would brick "
        'PRs. Re-run with explicit --checks (e.g. --checks "a,b,c"). '
        "Per-workflow breakdown:",
    ]
    for wf in workflows:
        certain = ", ".join(wf.certain) if wf.certain else "(none)"
        lines.append(f"  {wf.workflow}: certain [{certain}]")
        for d in wf.dropped:
            lines.append(f"    dropped {d.job!r} ({d.reason})")
    return "\n".join(lines)


def discover(repo: str, default_branch: str, *, toplevel: str | None) -> Discovery:
    """The required checks for ``repo``: runs-based first, static fallback (#1056).

    ``toplevel`` is the local checkout root when shipit runs inside the target
    repo (enabling the static fallback); ``None`` for a remote-only target, in
    which case only runs-based discovery is available.

    Runs-based discovery is authoritative (its names come from real runs) and
    never refuses. Only the static fallback can refuse: when no runs exist yet
    and static discovery leaves ANY PR workflow contributing zero certain
    contexts (every nameable job dropped, or an unresolvable/unparseable file),
    the returned :class:`Discovery` carries no checks and a ``refusal`` message —
    the caller must not write the ruleset and must demand explicit ``--checks``.
    """
    paths: list[str] = []
    if toplevel is not None:
        workflows_dir = os.path.join(toplevel, ".github", "workflows")
        if os.path.isdir(workflows_dir):
            paths = pr_workflow_paths(workflows_dir)
    runs_checks = checks_from_runs(repo, default_branch, paths) if paths else []
    runs_checks = [c for c in runs_checks if c != ""]
    if runs_checks:
        return Discovery(checks=tuple(runs_checks))
    if toplevel is None or not paths:
        # No static fallback available (remote target, or no PR-check workflow at
        # all) — an empty set is an honest "nothing to require", not a refusal.
        return Discovery(checks=())
    workflows = static_workflow_contexts(toplevel, paths)
    if any(not wf.certain for wf in workflows):
        return Discovery(checks=(), refusal=_refusal_message(workflows))
    certain = sorted({c for wf in workflows for c in wf.certain})
    return Discovery(checks=tuple(certain))
