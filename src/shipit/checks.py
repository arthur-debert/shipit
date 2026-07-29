"""Required-status-check discovery for the branch ruleset.

Runs-based first, static workflow parsing second; a context that cannot be named statically is dropped, never guessed.
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

logger = logging.getLogger("shipit.checks")

_NON_CHECK_WORKFLOWS = ("copilot-review.yml", "copilot-review.yaml")


class _WorkflowLoader(yaml.SafeLoader):
    """A YAML loader that does NOT treat ``on``/``off``/``yes``/``no`` as bools, so a workflow's ``on:`` key stays a string."""


_WorkflowLoader.yaml_implicit_resolvers = {
    ch: [(tag, rx) for tag, rx in resolvers if tag != "tag:yaml.org,2002:bool"]
    for ch, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_BOOL_1_2 = re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$")
for _ch in "tTfF":
    _WorkflowLoader.add_implicit_resolver("tag:yaml.org,2002:bool", _BOOL_1_2, _ch)

_USES_RE = re.compile(
    r"^(?P<owner>[^/]+)/(?P<repo>[^/]+)/(?P<path>[^@]+\.ya?ml)@(?P<ref>.+)$"
)
_VN_RE = re.compile(r"^v\d+$")
# GitHub caps reusable-workflow nesting at 4; the same cap bounds the recursion.
_MAX_NESTING = 4

RELEASE_CALLER_WORKFLOW = "shipit-release.yml"


@dataclass(frozen=True)
class DroppedJob:
    """A job static discovery could not name: ``reason`` is ``"matrix"`` or ``"dynamic name"``."""

    job: str
    reason: str


@dataclass(frozen=True)
class WorkflowContexts:
    """One PR workflow's static-discovery outcome: the contexts it certainly reports, and the jobs dropped."""

    workflow: str
    certain: tuple[str, ...]
    dropped: tuple[DroppedJob, ...]


@dataclass(frozen=True)
class Discovery:
    """The discovery result: ``checks`` to require, or a ``refusal`` message (with no checks) the caller must surface instead of writing the ruleset."""

    checks: tuple[str, ...]
    refusal: str | None = None


def workflow_triggers(workflow: object) -> list[str]:
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
    return "pull_request" in workflow_triggers(workflow)


def is_reusable_workflow(workflow: object) -> bool:
    return "workflow_call" in workflow_triggers(workflow)


def pr_trigger_is_path_filtered(workflow: object) -> bool:
    """True if the ``pull_request`` trigger carries a ``paths``/``paths-ignore``; such a workflow is conditional and unsafe to require."""
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
    """A job's reported check name: a static ``name:`` override, else the job id."""
    if isinstance(job, dict):
        name = job.get("name")
        if isinstance(name, str) and "${{" not in name:
            return name
    return job_id


def job_unpredictable(job: object) -> str | None:
    """Why a job's reported check name is statically unpredictable (``"matrix"`` / ``"dynamic name"``), else ``None``."""
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
    """Whether a called workflow's job contributes a context: a job-level ``if: inputs.<key>`` is resolved against the caller's ``with:``, any other ``if:`` is included."""
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


def _load_yaml_text(text: str) -> object:
    return yaml.load(text, Loader=_WorkflowLoader)


def _load_yaml_file(path: str) -> object:
    with open(path, encoding="utf-8") as fh:
        return yaml.load(fh, Loader=_WorkflowLoader)


def _fetch_called_workflow(uses: str, toplevel: str | None) -> object:
    """The parsed definition of a ``uses:`` target — local for ``./…``, else the contents API at the pinned ref."""
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
    """The contexts one workflow job reports, and the jobs dropped; a reusable call reports ``<caller> / <called>`` per called job."""
    unpredictable = job_unpredictable(job)
    uses = job.get("uses") if isinstance(job, dict) else None
    display = job_display_name(job_id, job)
    if not isinstance(uses, str):
        if unpredictable is not None:
            return [], [DroppedJob(job=job_id, reason=unpredictable)]
        return [display], []
    if unpredictable is not None:
        return [], [DroppedJob(job=job_id, reason=unpredictable)]
    if depth >= _MAX_NESTING:
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
    """The floating-major ``@vN`` reusable-workflow pins the release caller at ``caller_path`` dispatches, as sorted-unique ``(owner/repo, ref)`` tuples."""
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
    """Whether ``repo`` publishes any ``workflow_call`` workflow; raises rather than reporting False when the repo cannot be read."""
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
            return False
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
    """Loudly report one dropped job: user-facing stderr + WARNING."""
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
    """Per-PR-workflow static discovery; a workflow that will not parse yields an empty ``certain``."""
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
    """The flat certain-context set static discovery names across ``paths``."""
    certain: set[str] = set()
    for wf in static_workflow_contexts(toplevel, paths):
        certain.update(wf.certain)
    return sorted(certain)


def _refusal_message(workflows: list[WorkflowContexts]) -> str:
    """The actionable refusal shown when a PR workflow contributes zero certain contexts."""
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
    """The required checks for ``repo``: runs-based first, then — when ``toplevel`` gives a local checkout — the static fallback, which may refuse."""
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
        return Discovery(checks=())
    workflows = static_workflow_contexts(toplevel, paths)
    if any(not wf.certain for wf in workflows):
        return Discovery(checks=(), refusal=_refusal_message(workflows))
    certain = sorted({c for wf in workflows for c in wf.certain})
    return Discovery(checks=tuple(certain))
