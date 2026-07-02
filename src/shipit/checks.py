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
``copilot-review`` (which only requests a review, not a check). The bare
caller-job name of a reusable call is never a reported context and would deadlock
every PR — release#602.
"""

from __future__ import annotations

import base64
import glob
import logging
import os
import re
import sys

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
# GitHub caps reusable-workflow nesting at 4; the same cap bounds the recursion.
_MAX_NESTING = 4


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
    so the job id is used (runs-based detection sees the rendered name).
    """
    if isinstance(job, dict):
        name = job.get("name")
        if isinstance(name, str) and "${{" not in name:
            return name
    return job_id


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
    if not isinstance(obj, dict) or "content" not in obj:
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
) -> list[str]:
    """The status-check contexts one workflow job reports.

    A plain job reports its display name. A job that CALLS a reusable workflow
    reports one ``<caller> / <called>`` context per called job (the bare caller
    name is never reported — release#602), recursing through nesting.
    """
    uses = job.get("uses") if isinstance(job, dict) else None
    display = job_display_name(job_id, job)
    if not isinstance(uses, str):
        return [display]
    if depth >= _MAX_NESTING:
        # Degraded-but-continuing (LOG02): discovery drops this job's contexts
        # and carries on — loud on both the user surface and the durable record.
        logger.warning("reusable-workflow nesting too deep at job %r", job_id)
        print(
            f"warning: reusable-workflow nesting too deep at job {job_id!r}",
            file=sys.stderr,
        )
        return []
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
        return []
    with_values = job.get("with") if isinstance(job.get("with"), dict) else {}
    out: list[str] = []
    for called_id, called in doc["jobs"].items():
        if not _called_job_included(called, with_values):
            continue
        for ctx in _job_contexts(
            called_id, called, toplevel=toplevel, cache=cache, depth=depth + 1
        ):
            out.append(f"{display} / {ctx}")
    return out


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
        except (OSError, yaml.YAMLError):
            continue
        if is_pr_workflow(doc) and not pr_trigger_is_path_filtered(doc):
            paths.append(f".github/workflows/{base}")
    return paths


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


def checks_from_workflows(toplevel: str, paths: list[str]) -> list[str]:
    """Static contexts the local workflows declare (the no-runs onboarding case)."""
    found: set[str] = set()
    cache: dict[str, object] = {}
    for path in paths:
        try:
            doc = _load_yaml_file(os.path.join(toplevel, path))
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(doc, dict) or not isinstance(doc.get("jobs"), dict):
            continue
        for job_id, job in doc["jobs"].items():
            found.update(_job_contexts(job_id, job, toplevel=toplevel, cache=cache))
    return sorted(found)


def discover(repo: str, default_branch: str, *, toplevel: str | None) -> list[str]:
    """The required checks for ``repo``: runs-based first, static fallback.

    ``toplevel`` is the local checkout root when shipit runs inside the target
    repo (enabling the static fallback); ``None`` for a remote-only target, in
    which case only runs-based discovery is available.
    """
    paths: list[str] = []
    if toplevel is not None:
        workflows_dir = os.path.join(toplevel, ".github", "workflows")
        if os.path.isdir(workflows_dir):
            paths = pr_workflow_paths(workflows_dir)
    checks = checks_from_runs(repo, default_branch, paths) if paths else []
    if not checks and toplevel is not None and paths:
        checks = checks_from_workflows(toplevel, paths)
    return [c for c in checks if c != ""]
