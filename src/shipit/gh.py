"""The ``gh`` Tool adapter: shipit's single GitHub boundary.
See docs/adr/0028-one-exec-seam-tool-adapters.md
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import execrun
from .execrun import ExecError

if TYPE_CHECKING:
    from .identity import Repo
    from .pr import PR, PrId

# A leaf module (stdlib-only), so this one import can be top-level while
# `identity` and `pr` compose over this module and stay deferred above.
from .prstate.errors import PrStateError

logger = logging.getLogger("shipit.gh")

_NETWORK_TIMEOUT: float = execrun.DEFAULT_TIMEOUT


class UnknownPr:
    """Sentinel: a head's PR state could NOT be read — distinct from ``None`` (no PR)."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return "UNKNOWN"


UNKNOWN = UnknownPr()


@dataclass(frozen=True)
class HeadPr:
    """The PR on one head; ``state`` is GitHub's vocabulary upper-cased (``OPEN``/``MERGED``/``CLOSED``)."""

    number: int
    state: str
    is_draft: bool
    base_ref: str

    @property
    def display_state(self) -> str:
        """``DRAFT`` for an open draft, otherwise the GitHub state verbatim."""
        if self.state == "OPEN" and self.is_draft:
            return "DRAFT"
        return self.state


@dataclass(frozen=True)
class PrAttachment:
    """The existing PR a write Run attaches to, including its current head branch."""

    number: int
    state: str
    is_draft: bool
    base_ref: str
    head_ref: str
    is_cross_repository: bool
    maintainer_can_modify: bool


def _head_pr_from_json(data: dict) -> HeadPr:
    """Build a :class:`HeadPr`, raising :class:`ValueError` naming any missing or mistyped field."""
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


def _pr_attachment_from_json(data: dict) -> PrAttachment:
    head = _head_pr_from_json(data)
    head_ref = data.get("headRefName")
    if not isinstance(head_ref, str) or not head_ref.strip():
        raise ValueError(
            f"malformed `gh pr view` payload: headRefName must be a non-empty str, "
            f"got {head_ref!r}"
        )
    is_cross_repository = data.get("isCrossRepository")
    if not isinstance(is_cross_repository, bool):
        raise ValueError(
            "malformed `gh pr view` payload: isCrossRepository must be a bool, "
            f"got {is_cross_repository!r}"
        )
    maintainer_can_modify = data.get("maintainerCanModify")
    if not isinstance(maintainer_can_modify, bool):
        raise ValueError(
            "malformed `gh pr view` payload: maintainerCanModify must be a bool, "
            f"got {maintainer_can_modify!r}"
        )
    return PrAttachment(
        number=head.number,
        state=head.state,
        is_draft=head.is_draft,
        base_ref=head.base_ref,
        head_ref=head_ref.strip(),
        is_cross_repository=is_cross_repository,
        maintainer_can_modify=maintainer_can_modify,
    )


def _token_env(token: str | None) -> dict[str, str] | None:
    """``GH_TOKEN=<token>``, or ``None`` to leave the user's normal ``gh`` auth in place."""
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
    """Run a command through the Exec runner, returning stdout; a ``token`` REPLACES the child env so only ``GH_TOKEN`` is set."""
    env = None
    replace_env = False
    if token is not None:
        import os

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
    """Run a command whose nonzero exit is a NORMAL answer, not a failure."""
    return execrun.run(args, cwd=cwd, check=False, timeout=timeout)


def rest(
    path: str,
    *,
    method: str | None = None,
    body: object | None = None,
    fields: dict[str, str] | None = None,
    paginate: bool = False,
    token: str | None = None,
) -> object:
    """``gh api <path>`` parsed as JSON; ``body`` and ``fields`` are alternatives and passing both raises."""
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
    """The engine's own GraphQL calls: a ``None`` variable is OMITTED, str values are forced with ``-f``, and response errors raise :class:`PrStateError`."""
    args = ["gh", "api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        if value is None:
            continue
        flag = "-F" if isinstance(value, (int, bool)) else "-f"
        args += [flag, f"{key}={value}"]
    payload = json.loads(_run(args))
    if payload.get("errors"):
        try:
            raise PrStateError(f"graphql errors: {payload['errors']}")
        except PrStateError:
            logger.error(
                "gh graphql call returned errors (Exec succeeded, answer unusable)",
                exc_info=True,
            )
            raise
    return payload["data"]


def current_repo(*, cwd: str | None = None) -> Repo:
    """The canonical :class:`~shipit.identity.Repo` of the checkout at ``cwd``; unusable output raises :class:`ValueError`."""
    from .identity import repo_from_slug

    out = _run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
        cwd=cwd,
    )
    return repo_from_slug(out)


def repo_canonical(slug: str) -> Repo:
    """Resolve a possibly aliased or renamed slug to its canonical :class:`~shipit.identity.Repo`."""
    from .identity import repo_from_slug

    out = _run(
        ["gh", "repo", "view", slug, "--json", "nameWithOwner", "-q", ".nameWithOwner"]
    )
    return repo_from_slug(out)


def pr_view(pr: str, *, repo: str | None = None, json_fields: list[str]) -> dict:
    """The raw ``gh pr view --json`` object; output that is not a JSON object raises :class:`ValueError`."""
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
    from .pr import CORE_JSON_FIELDS, core_from_node

    node = pr_view(str(pr.number), repo=pr.slug, json_fields=list(CORE_JSON_FIELDS))
    return core_from_node(node, pr.repo)


def pr_meta(pr: PrId) -> dict:
    """The raw ``pullRequest`` node the readiness view needs; deliberately WITHOUT ``reviewRequests``, which silently omits Bot reviewers."""
    return pr_view(
        str(pr.number),
        repo=pr.slug,
        json_fields=[
            "number",
            "headRefOid",
            "headRefName",
            "baseRefName",
            "isDraft",
            "mergeable",
            "mergeStateStatus",
            "statusCheckRollup",
        ],
    )


def owner_kind(login: str) -> str:
    """``"User"`` or ``"Organization"``; an unusable response raises :class:`ValueError`."""
    info = rest(f"users/{login}")
    if not isinstance(info, dict) or "type" not in info:
        raise ValueError(f"could not resolve owner kind for {login!r}")
    return str(info["type"])


def default_branch(repo: str) -> str:
    info = rest(f"repos/{repo}")
    if not isinstance(info, dict) or "default_branch" not in info:
        raise ValueError(f"could not resolve default branch for {repo}")
    return str(info["default_branch"])


_COMPARE_TIMEOUT: float = 20.0


def commits_ahead(repo: str, base: str, head: str) -> int | None:
    """How many commits ``head`` has over ``base``; ``None`` on ANY failure — a best-effort advisory read."""
    try:
        result = _run_probe(
            [
                "gh",
                "api",
                f"repos/{repo}/compare/{base}...{head}",
                "--jq",
                ".ahead_by",
            ],
            timeout=_COMPARE_TIMEOUT,
        )
    except ExecError:
        return None
    if not result.ok:
        return None
    try:
        return int(result.stdout.strip())
    except ValueError:
        return None


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


def secret_set(name: str, value: str, *, repo: str) -> None:
    """Set an Actions secret, passing the value on stdin (never in argv)."""
    _run(["gh", "secret", "set", name, "--repo", repo], input_text=value)


def secret_list(repo: str) -> list[str]:
    out = _run(
        ["gh", "secret", "list", "--repo", repo, "--json", "name", "-q", ".[].name"]
    )
    return [line for line in out.splitlines() if line.strip()]


_UPLOAD_TIMEOUT: float = 1800.0


def release_exists(tag: str, *, cwd: str | None = None) -> bool:
    """Whether a GH Release exists for ``tag``; a missing release is a normal answer."""
    return _run_probe(["gh", "release", "view", tag, "--json", "name"], cwd=cwd).rc == 0


def workflow_ref_resolves(repo: str, ref: str) -> bool:
    """Whether ``ref`` resolves to a commit; only a confirmed-missing ref answers ``False``, a degraded probe answers ``True``."""
    result = _run_probe(["gh", "api", f"repos/{repo}/commits/{ref}"])
    if result.rc == 0:
        return True
    stderr = (result.stderr or "").lower()
    if "http 404" in stderr or "no commit found" in stderr:
        return False
    logger.warning(
        "workflow ref probe degraded for %s@%s (rc=%s) — treating as "
        "unknown-not-missing, not an absent ref: %s",
        repo,
        ref,
        result.rc,
        (result.stderr or "").strip(),
    )
    return True


def release_create(
    tag: str, *, notes_file: str, prerelease: bool, cwd: str | None = None
) -> None:
    """Create the release from ``notes_file``; ``--verify-tag`` refuses to mint the tag itself."""
    args = [
        "gh",
        "release",
        "create",
        tag,
        "--verify-tag",
        "--title",
        tag,
        "--notes-file",
        notes_file,
    ]
    if prerelease:
        args.append("--prerelease")
    _run(args, cwd=cwd)


def release_edit(
    tag: str, *, notes_file: str, prerelease: bool, cwd: str | None = None
) -> None:
    """Edit an existing release; the prerelease flag is re-asserted explicitly in both directions."""
    _run(
        [
            "gh",
            "release",
            "edit",
            tag,
            "--notes-file",
            notes_file,
            f"--prerelease={'true' if prerelease else 'false'}",
        ],
        cwd=cwd,
    )


def release_upload(tag: str, files: list[str], *, cwd: str | None = None) -> None:
    """Upload assets with ``--clobber``, so a re-run replaces same-named assets instead of erroring."""
    if not files:
        return
    _run(
        ["gh", "release", "upload", tag, *files, "--clobber"],
        cwd=cwd,
        timeout=_UPLOAD_TIMEOUT,
    )


def workflow_run(
    workflow: str, *, repo: str, ref: str, fields: Mapping[str, str]
) -> None:
    """Dispatch one ``workflow_dispatch`` run; ``fields`` ride as ``-f key=value`` in the given order."""
    args = ["gh", "workflow", "run", workflow, "-R", repo, "--ref", ref]
    for key, value in fields.items():
        args += ["-f", f"{key}={value}"]
    _run(args)


def run_list_dispatched(repo: str, workflow: str, *, limit: int = 20) -> list[dict]:
    """The workflow's most recent ``workflow_dispatch`` runs, newest first; empty output parses to ``[]``."""
    out = _run(
        [
            "gh",
            "run",
            "list",
            "-R",
            repo,
            "--workflow",
            workflow,
            "--event",
            "workflow_dispatch",
            "--json",
            "databaseId,status,conclusion,url",
            "--limit",
            str(limit),
        ]
    )
    return json.loads(out or "[]")


def run_verdict(repo: str, run_id: int) -> dict:
    """One run's ``status``/``conclusion``/``url``; ``status`` is ``completed`` once a conclusion exists."""
    out = _run(
        [
            "gh",
            "run",
            "view",
            str(run_id),
            "-R",
            repo,
            "--json",
            "status,conclusion,url",
        ]
    )
    return json.loads(out or "{}")


def repository_dispatch(
    slug: str,
    *,
    event_type: str,
    payload: Mapping[str, object],
    token: str | None = None,
) -> None:
    """POST a ``repository_dispatch`` at ``slug``; ``token`` must be a cross-repo PAT, as an ambient ``GITHUB_TOKEN`` cannot dispatch."""
    rest(
        f"repos/{slug}/dispatches",
        method="POST",
        body={"event_type": event_type, "client_payload": dict(payload)},
        token=token,
    )


def repo_is_private(slug: str) -> bool:
    data = rest(f"repos/{slug}")
    if not isinstance(data, dict) or not isinstance(data.get("private"), bool):
        raise ValueError(f"malformed repos/{slug} payload: no boolean `private`")
    return data["private"]


def pr_for_head(branch: str, *, cwd: str | None = None) -> HeadPr | None | UnknownPr:
    """The head's PR: a :class:`HeadPr`, ``None`` when provably PR-less, or :data:`UNKNOWN` when undetermined."""
    try:
        result = _run_probe(
            ["gh", "pr", "view", branch, "--json", "number,state,isDraft,baseRefName"],
            cwd=cwd,
        )
    except ExecError:
        return UNKNOWN
    if result.rc != 0:
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
        return UNKNOWN


def pr_for_number(number: int, *, repo: str | None = None) -> PrAttachment:
    """The attachment snapshot for ``number``; unlike :func:`pr_for_head` this fails loud."""
    data = pr_view(
        str(number),
        repo=repo,
        json_fields=[
            "number",
            "state",
            "isDraft",
            "baseRefName",
            "headRefName",
            "isCrossRepository",
            "maintainerCanModify",
        ],
    )
    return _pr_attachment_from_json(data)


NO_PR_MARKER = "no pull requests found for branch"


def pr_number_probe(repo: Repo, branch: str) -> execrun.ExecResult:
    """Probe for the branch's PR number, PINNED to ``repo`` rather than ``gh``'s ambient inference."""
    return _run_probe(
        ["gh", "pr", "view", branch, "--repo", repo.slug, "--json", "number"]
    )


def resolve_pr(number: int | None, repo: Repo, branch: str | None) -> PrId | None:
    """The PR target: an explicit ``number`` or the branch's PR; ``None`` when it has none, :class:`ExecError` on a real gh failure."""
    from .pr import PrId

    if number is not None:
        return PrId(repo=repo, number=number)
    if branch is None:
        return None
    result = pr_number_probe(repo, branch)
    if result.rc != 0:
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
        return None
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        raise PrStateError(f"unparseable `gh pr view` output: {exc}") from exc
    resolved = data.get("number")
    if resolved is None:
        return None
    # The raw wire value goes straight in: PrId's construction IS the exact-int
    # validation, which an `int(resolved)` coercion here would defeat.
    try:
        return PrId(repo=repo, number=resolved)
    except ValueError as exc:
        raise PrStateError(f"malformed `gh pr view` number: {exc}") from exc


def pr_url_for_head(branch: str, *, cwd: str | None = None) -> str | None:
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
    """``gh pr create`` (draft by default) with the body on stdin; returns the new PR's URL."""
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


def pr_edit_reviewer(pr: PrId, reviewer: str, *, remove: bool = False) -> None:
    """Add or remove a reviewer via ``gh pr edit``, whose GraphQL path is the only one that works for bot reviewers."""
    flag = "--remove-reviewer" if remove else "--add-reviewer"
    _run(["gh", "pr", "edit", str(pr.number), "--repo", pr.slug, flag, reviewer])


def pr_ready(pr: PrId, *, undo: bool = False) -> None:
    """Flip a PR's draft flag (``--undo`` for ready→draft); idempotent on a PR already in the target state."""
    args = ["gh", "pr", "ready", str(pr.number), "--repo", pr.slug]
    if undo:
        args.append("--undo")
    _run(args)
    logger.info(
        "pr#%s draft flag flipped %s on %s",
        pr.number,
        "ready→draft" if undo else "draft→ready",
        pr.slug,
        extra={"pr": pr.number, "repo": pr.slug},
    )


def pr_review_reply(pr: PrId, comment_id: int, body: str) -> None:
    """Post a threaded reply under review comment ``comment_id`` (its numeric REST id)."""
    rest(
        f"repos/{pr.slug}/pulls/{pr.number}/comments/{comment_id}/replies",
        method="POST",
        fields={"body": body},
    )
