"""The gh-setup domain: make a GitHub repo conform to the portfolio standard.
Four idempotent passes — ruleset, labels, secrets, workflow access — each typed.
"""

from __future__ import annotations

import copy
import json
import logging
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, replace
from importlib import resources
from pathlib import Path
from typing import Any

from . import checks as checks_mod
from . import config, execrun, gh, secretsrc
from .identity import Repo
from .prstate import reviewers_config
from .release import secretreq

logger = logging.getLogger("shipit.ghsetup")

RULESET_NAME = "main-branch-protection"


@dataclass(frozen=True)
class Label:
    name: str
    description: str
    color: str


def load_template() -> dict:
    """The cleaned ruleset template (no per-repo id/source; empty checks)."""
    text = (resources.files("shipit.data") / "main-branch-protection.json").read_text(
        encoding="utf-8"
    )
    return json.loads(text)


def load_labels() -> list[Label]:
    text = (resources.files("shipit.data") / "issue-labels.toml").read_text(
        encoding="utf-8"
    )
    data = tomllib.loads(text)
    labels: list[Label] = []
    for name, attrs in data.items():
        if not isinstance(attrs, dict):
            continue
        labels.append(
            Label(
                name=name,
                description=str(attrs.get("description", "")),
                color=str(attrs.get("color", "")),
            )
        )
    return labels


def build_payload(template: dict, checks: list[str]) -> dict:
    """Inject ``checks`` into ``required_status_checks``; with zero checks the rule is OMITTED, as the API rejects an empty array."""
    body = copy.deepcopy(template)
    contexts = checks_mod.checks_json(checks)
    rules = body.get("rules", [])
    if not contexts:
        if "rules" in body:
            body["rules"] = [
                rule
                for rule in rules
                if not (
                    isinstance(rule, dict)
                    and rule.get("type") == "required_status_checks"
                )
            ]
        return body
    for rule in rules:
        if isinstance(rule, dict) and rule.get("type") == "required_status_checks":
            rule.setdefault("parameters", {})["required_status_checks"] = contexts
    return body


def existing_ruleset_id(rulesets: object, name: str) -> int | None:
    for rs in rulesets or []:
        if isinstance(rs, dict) and rs.get("name") == name:
            return rs.get("id")
    return None


@dataclass(frozen=True)
class RulesetOutcome:
    """Pass (a)'s outcome; ``action`` is ``created``/``updated``/``dry-run``/``refused``, and a set ``list_error`` makes ``existing_id`` assumed-none rather than verified-absent."""

    name: str
    existing_id: int | None
    checks: tuple[str, ...]
    action: str
    payload: dict[str, Any]
    list_error: str | None = None
    refusal: str | None = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "existing_id": self.existing_id,
            "checks": list(self.checks),
            "action": self.action,
            "payload": self.payload,
            "list_error": self.list_error,
            "refusal": self.refusal,
        }


@dataclass(frozen=True)
class LabelOutcome:
    """One label of pass (b): ``action`` is ``"upserted"`` or ``"dry-run"``."""

    name: str
    action: str


@dataclass(frozen=True)
class SecretOutcome:
    """One secret of pass (c); ``action`` is ``set``/``skipped``/``failed``/``orphan``/``dry-run`` and the VALUE never appears here."""

    name: str
    source: str
    action: str
    reason: str | None = None


@dataclass(frozen=True)
class WorkflowAccessOutcome:
    """Pass (d)'s read-only outcome; ``status`` is ``not-applicable``/``acceptable``/``warn``/``unknown``, where ``unknown`` means the inspection itself failed."""

    status: str
    reason: str
    access_level: str | None = None
    recommended_level: str | None = None

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "reason": self.reason,
            "access_level": self.access_level,
            "recommended_level": self.recommended_level,
        }


@dataclass(frozen=True)
class SetupReport:
    """The one frozen result of a gh-setup run; ``secrets_error`` records a degraded config failure instead of raising."""

    repo: str
    dry_run: bool
    ruleset: RulesetOutcome
    labels: tuple[LabelOutcome, ...]
    workflow_access: WorkflowAccessOutcome
    secrets: tuple[SecretOutcome, ...]
    secrets_error: str | None = None

    @property
    def ruleset_refused(self) -> bool:
        """Whether the ruleset pass refused to write; a refusal makes the run rc 1."""
        return self.ruleset.action == "refused"

    @property
    def secrets_set(self) -> int:
        return sum(1 for s in self.secrets if s.action == "set")

    @property
    def secrets_skipped(self) -> int:
        return sum(1 for s in self.secrets if s.action == "skipped")

    @property
    def secrets_failed(self) -> int:
        return sum(1 for s in self.secrets if s.action == "failed")

    @property
    def secrets_orphaned(self) -> int:
        """Declared ``[secrets]`` entries nothing requires — flagged, never pushed, never rc-relevant."""
        return sum(1 for s in self.secrets if s.action == "orphan")

    def to_dict(self) -> dict:
        return {
            "repo": self.repo,
            "dry_run": self.dry_run,
            "ruleset": self.ruleset.to_dict(),
            "labels": [{"name": lb.name, "action": lb.action} for lb in self.labels],
            "workflow_access": self.workflow_access.to_dict(),
            "secrets": [
                {
                    "name": s.name,
                    "source": s.source,
                    "action": s.action,
                    "reason": s.reason,
                }
                for s in self.secrets
            ],
            "secrets_error": self.secrets_error,
        }


def apply_ruleset(
    repo: str, checks: list[str], *, dry_run: bool, refusal: str | None = None
) -> RulesetOutcome:
    """Pass (a). Create-or-update the standard ruleset; a ``refusal`` short-circuits the pass and writes nothing."""
    if refusal is not None:
        logger.warning(
            "refusing ruleset write — auto-discovery could not name every "
            "PR workflow's checks",
            extra={"repo": repo},
        )
        return RulesetOutcome(
            name=RULESET_NAME,
            existing_id=None,
            checks=(),
            action="refused",
            payload={},
            refusal=refusal,
        )
    template = load_template()
    body = build_payload(template, checks)
    list_error: str | None = None
    try:
        rulesets = gh.rest(f"repos/{repo}/rulesets")
    except execrun.ExecError as exc:
        logger.warning(
            "could not list rulesets — assuming none exists",
            exc_info=True,
            extra={"repo": repo},
        )
        rulesets = None
        list_error = str(exc)
    existing = existing_ruleset_id(rulesets, RULESET_NAME)

    def outcome(action: str) -> RulesetOutcome:
        return RulesetOutcome(
            name=RULESET_NAME,
            existing_id=existing,
            checks=tuple(checks),
            action=action,
            payload=body,
            list_error=list_error,
        )

    if dry_run:
        return outcome("dry-run")
    if existing is not None:
        gh.rest(f"repos/{repo}/rulesets/{existing}", method="PUT", body=body)
        logger.info(
            "ruleset updated",
            extra={"repo": repo, "ruleset": RULESET_NAME, "checks": len(checks)},
        )
        return outcome("updated")
    gh.rest(f"repos/{repo}/rulesets", method="POST", body=body)
    logger.info(
        "ruleset created",
        extra={"repo": repo, "ruleset": RULESET_NAME, "checks": len(checks)},
    )
    return outcome("created")


def ensure_labels(
    repo: str, labels: list[Label], *, dry_run: bool
) -> tuple[LabelOutcome, ...]:
    outcomes: list[LabelOutcome] = []
    for label in labels:
        if dry_run:
            outcomes.append(LabelOutcome(name=label.name, action="dry-run"))
            continue
        gh.label_create(
            repo, label.name, description=label.description, color=label.color
        )
        logger.debug("label upserted", extra={"repo": repo, "label": label.name})
        outcomes.append(LabelOutcome(name=label.name, action="upserted"))
    if not dry_run:
        logger.info("labels ensured", extra={"repo": repo, "labels": len(labels)})
    return tuple(outcomes)


def verify_workflow_access(
    repo: str, *, local_checkout: str | None
) -> WorkflowAccessOutcome:
    """Pass (d). Verify — never set — the Actions access level; every inspection failure degrades to ``unknown``."""
    try:
        info = gh.rest(f"repos/{repo}")
        if not isinstance(info, dict) or not isinstance(info.get("private"), bool):
            raise ValueError(f"malformed repos/{repo} payload: no boolean `private`")
        if not info["private"]:
            return WorkflowAccessOutcome(
                status="not-applicable",
                reason="repository is public — its reusable workflows are "
                "callable by any repo (ADR-0053)",
            )
        if not checks_mod.publishes_reusable_workflows(repo, toplevel=local_checkout):
            return WorkflowAccessOutcome(
                status="not-applicable",
                reason="no workflow_call workflows under .github/workflows — "
                "not a reusable-workflow publisher",
            )
        access = gh.rest(f"repos/{repo}/actions/permissions/access")
        if not isinstance(access, dict) or not isinstance(
            access.get("access_level"), str
        ):
            raise ValueError(
                f"malformed repos/{repo}/actions/permissions/access payload: "
                "no `access_level`"
            )
        level = access["access_level"]
        if level != "none":
            return WorkflowAccessOutcome(
                status="acceptable",
                reason=f"Actions access level is {level!r}",
                access_level=level,
            )
        owner = info.get("owner")
        owner_type = owner.get("type") if isinstance(owner, dict) else None
        recommended = "organization" if owner_type == "Organization" else "user"
        reason = (
            "private reusable-workflow publisher with Actions access level "
            "'none' — no other repo can call its workflows (TOL02-WS07 "
            f"finding 5); fix: gh api -X PUT repos/{repo}/actions/permissions/"
            f"access -f access_level={recommended} (gh-setup verifies only, "
            "never sets it)"
        )
        logger.warning(
            "actions access level is none on a private workflow publisher",
            extra={"repo": repo, "recommended_level": recommended},
        )
        return WorkflowAccessOutcome(
            status="warn",
            reason=reason,
            access_level="none",
            recommended_level=recommended,
        )
    except (execrun.ExecError, ValueError) as exc:
        logger.warning(
            "could not verify actions access level",
            exc_info=True,
            extra={"repo": repo},
        )
        return WorkflowAccessOutcome(
            status="unknown",
            reason=f"could not verify Actions access: {exc}",
        )


def sync_secrets(
    repo: str,
    artifacts: tuple[config.Artifact, ...],
    sources: list[config.SecretSource],
    *,
    reviewers: tuple[str, ...],
    dry_run: bool,
    prompt: Callable[[str], str] | None = None,
) -> tuple[SecretOutcome, ...]:
    """Pass (c). Push the declared sources the derived requirement set demands; an unsourced requirement fails and an unrequired declaration is flagged as an orphan."""
    orphan_names = set(secretreq.orphans(artifacts, sources, reviewers=reviewers))
    required_names = set(secretreq.required_names(artifacts, reviewers=reviewers))
    empty_valid = secretreq.EMPTY_VALID_SECRETS
    # A derived-required source is forced non-optional so a missing value fails
    # instead of silently skipping — except an empty-valid name, which keeps its
    # flag so a repo that legitimately has no value for it still syncs clean.
    to_push = [
        replace(source, optional=False)
        if source.optional
        and source.name in required_names
        and source.name not in empty_valid
        else source
        for source in sources
        if source.name not in orphan_names
    ]
    outcomes = push_secrets(repo, to_push, dry_run=dry_run, prompt=prompt)
    missing = tuple(
        SecretOutcome(
            name=req.name,
            source="none",
            action="failed",
            reason=f"required by {req.required_by}; no [secrets] source declares it",
        )
        for req in secretreq.missing_sources(artifacts, sources, reviewers=reviewers)
    )
    present = (
        {source.name for source in sources}
        if dry_run
        else {outcome.name for outcome in outcomes if outcome.action == "set"}
    )
    unsatisfied = tuple(
        SecretOutcome(
            name=alt_req.sets.label,
            source="none",
            action="failed",
            reason=f"required by {alt_req.required_by}; "
            f"{alt_req.sets.describe_gap(present)}",
        )
        for alt_req in secretreq.alternative_requirements(artifacts)
        if not alt_req.sets.satisfied(present)
    )
    orphans = tuple(
        SecretOutcome(
            name=source.name,
            source=source.kind,
            action="orphan",
            reason="declared in [secrets] but nothing requires it — not pushed",
        )
        for source in sources
        if source.name in orphan_names
    )
    return outcomes + missing + unsatisfied + orphans


def push_secrets(
    repo: str,
    sources: list[config.SecretSource],
    *,
    dry_run: bool,
    prompt: Callable[[str], str] | None = None,
) -> tuple[SecretOutcome, ...]:
    """Resolve and push each given secret; an unresolvable required source is recorded as failed rather than aborting the pass."""
    outcomes: list[SecretOutcome] = []
    for source in sources:
        if dry_run:
            outcomes.append(
                SecretOutcome(name=source.name, source=source.kind, action="dry-run")
            )
            continue
        try:
            value = secretsrc.resolve(source, prompt=prompt)
        except secretsrc.SecretSourceError as exc:
            logger.warning(
                "secret could not be resolved",
                exc_info=True,
                extra={"repo": repo, "secret": source.name, "source": source.kind},
            )
            outcomes.append(
                SecretOutcome(
                    name=source.name,
                    source=source.kind,
                    action="failed",
                    reason=str(exc),
                )
            )
            continue
        if value is None:
            logger.debug(
                "secret skipped (optional source absent)",
                extra={"repo": repo, "secret": source.name, "source": source.kind},
            )
            outcomes.append(
                SecretOutcome(
                    name=source.name,
                    source=source.kind,
                    action="skipped",
                    reason="optional source absent",
                )
            )
            continue
        gh.secret_set(source.name, value, repo=repo)
        logger.info(
            "secret set",
            extra={"repo": repo, "secret": source.name, "source": source.kind},
        )
        outcomes.append(
            SecretOutcome(name=source.name, source=source.kind, action="set")
        )
    return tuple(outcomes)


def setup(
    repo: Repo,
    *,
    checks_override: list[str] | None = None,
    local_checkout: str | None = None,
    config_path: str | None = None,
    dry_run: bool = False,
    prompt: Callable[[str], str] | None = None,
) -> SetupReport:
    """Drive the four passes against ``repo``; in-run degradations are report facts, a broken gh raises."""
    started = time.monotonic()
    slug = repo.slug
    refusal: str | None = None
    if checks_override is not None:
        checks = [c for c in checks_override if c]
    else:
        default_branch = gh.default_branch(slug)
        discovery = checks_mod.discover(slug, default_branch, toplevel=local_checkout)
        checks = list(discovery.checks)
        refusal = discovery.refusal
    if refusal is None and not checks:
        logger.warning(
            "no required checks found — ruleset applied without a "
            "required-status-checks gate",
            extra={"repo": slug},
        )

    ruleset = apply_ruleset(slug, checks, dry_run=dry_run, refusal=refusal)
    labels = ensure_labels(slug, load_labels(), dry_run=dry_run)
    workflow_access = verify_workflow_access(slug, local_checkout=local_checkout)

    cfg_path = config_path or str(Path(local_checkout or ".") / config.CONFIG_NAME)
    secrets_error: str | None = None
    sources: list[config.SecretSource] = []
    artifacts: tuple[config.Artifact, ...] = ()
    reviewers: tuple[str, ...] = ()
    try:
        cfg = config.load(cfg_path)
        sources = config.load_secrets(cfg)
        artifacts = config.load_artifacts(cfg)
        reviewers = reviewers_config.parse_roster(
            cfg, config_path=cfg_path
        ).required_names
    except (
        config.ConfigError,
        reviewers_config.RequiredReviewersConfigError,
    ) as exc:
        secrets_error = str(exc)
        logger.warning("no secrets applied", exc_info=True, extra={"repo": slug})
    if secrets_error is None:
        secrets = sync_secrets(
            slug,
            artifacts,
            sources,
            reviewers=reviewers,
            dry_run=dry_run,
            prompt=prompt,
        )
    else:
        secrets = ()

    report = SetupReport(
        repo=slug,
        dry_run=dry_run,
        ruleset=ruleset,
        labels=labels,
        workflow_access=workflow_access,
        secrets=secrets,
        secrets_error=secrets_error,
    )
    log = logger.warning if report.secrets_failed else logger.info
    log(
        "gh-setup complete",
        extra={
            "repo": slug,
            "dry_run": dry_run,
            "secrets_set": report.secrets_set,
            "secrets_skipped": report.secrets_skipped,
            "secrets_failed": report.secrets_failed,
            "duration_ms": int((time.monotonic() - started) * 1000),
        },
    )
    return report
