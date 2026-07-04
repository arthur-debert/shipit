"""The gh-setup domain — make a GitHub repo conform to the portfolio standard.

Three idempotent passes (install AND update share this surface):

  a. ruleset — apply the standard main-branch-protection ruleset, requiring the
     TARGET repo's own checks (auto-discovered, never phos's captured set).
  b. labels  — ensure the standard label set exists (create-or-update).
  c. secrets — resolve each ``[secrets]`` entry from .shipit.toml and push it.

Re-running is a clean no-op: the ruleset is PUT in place when it already exists,
labels are ``--force`` upserts, and a changed secret is re-set to its new value.

The domain home per ADR-0030 (CLI02-WS04): each pass returns a typed outcome —
what was checked, what changed, what was skipped and why — and :func:`setup`
one frozen :class:`SetupReport`. Nothing here prints: rendering lives at the
verb (:mod:`shipit.verbs.gh_setup`); the durable log twin (ADR-0029) stays here
with the actions. A dry run walks the same passes and returns the same report
shape, performing no mutations (reads only — it lists rulesets, it never
resolves a secret).
"""

from __future__ import annotations

import copy
import json
import logging
import time
import tomllib
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any, Callable

from . import checks as checks_mod
from . import config, execrun, gh, secretsrc
from .identity import Repo

logger = logging.getLogger("shipit.ghsetup")

RULESET_NAME = "main-branch-protection"


@dataclass(frozen=True)
class Label:
    name: str
    description: str
    color: str


# --------------------------------------------------------------------------
# Packaged data
# --------------------------------------------------------------------------


def load_template() -> dict:
    """The cleaned ruleset template (no per-repo id/source; empty checks)."""
    text = (resources.files("shipit.data") / "main-branch-protection.json").read_text(
        encoding="utf-8"
    )
    return json.loads(text)


def load_labels() -> list[Label]:
    """The standard label set, in declaration order."""
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


# --------------------------------------------------------------------------
# Pure ruleset payload logic
# --------------------------------------------------------------------------


def build_payload(template: dict, checks: list[str]) -> dict:
    """Inject ``checks`` into the template's ``required_status_checks`` rule.

    With zero checks (none discovered, none passed) the rule is OMITTED from
    the payload entirely: the live rulesets API rejects an empty
    ``required_status_checks`` array with a 422 ("Expected at least 1
    elements" — #441), so an empty set must never be sent.
    """
    body = copy.deepcopy(template)
    contexts = checks_mod.checks_json(checks)
    rules = body.get("rules", [])
    if not contexts:
        body["rules"] = [
            rule
            for rule in rules
            if not (
                isinstance(rule, dict) and rule.get("type") == "required_status_checks"
            )
        ]
        return body
    for rule in rules:
        if isinstance(rule, dict) and rule.get("type") == "required_status_checks":
            rule.setdefault("parameters", {})["required_status_checks"] = contexts
    return body


def existing_ruleset_id(rulesets: object, name: str) -> int | None:
    """The id of the first ruleset named ``name``, or ``None``."""
    for rs in rulesets or []:
        if isinstance(rs, dict) and rs.get("name") == name:
            return rs.get("id")
    return None


# --------------------------------------------------------------------------
# Typed outcomes (ADR-0030) — one frozen value per pass, one report per run
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RulesetOutcome:
    """Pass (a)'s outcome: what was checked and what happened.

    ``action`` is ``"created"`` / ``"updated"`` (a mutation happened) or
    ``"dry-run"`` (nothing sent). ``payload`` is the full ruleset body that was
    sent — or, on a dry run, WOULD have been sent — so a caller can see exactly
    what would change. ``list_error`` records the degraded-but-continuing
    listing failure: when it is set, ``existing_id is None`` means "could not
    list, assumed none", NOT "verified absent".
    """

    name: str
    existing_id: int | None
    checks: tuple[str, ...]
    action: str
    payload: dict[str, Any]
    list_error: str | None = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "existing_id": self.existing_id,
            "checks": list(self.checks),
            "action": self.action,
            "payload": self.payload,
            "list_error": self.list_error,
        }


@dataclass(frozen=True)
class LabelOutcome:
    """One label of pass (b): ``action`` is ``"upserted"`` or ``"dry-run"``."""

    name: str
    action: str


@dataclass(frozen=True)
class SecretOutcome:
    """One secret of pass (c).

    ``action`` is ``"set"`` / ``"skipped"`` (optional source absent) /
    ``"failed"`` (required source unresolvable) / ``"dry-run"`` (not resolved —
    a dry run must not hit doppler or prompt). ``reason`` says why for the
    skipped/failed outcomes; the secret VALUE never appears anywhere.
    """

    name: str
    source: str
    action: str
    reason: str | None = None


@dataclass(frozen=True)
class SetupReport:
    """The one frozen result of a gh-setup run — per-pass outcomes, no prints.

    ``secrets_error`` carries the degraded-but-continuing config failure ("no
    secrets applied: …"): the ruleset/labels passes already applied, so a
    missing/malformed ``.shipit.toml`` is recorded here, never raised. The exit
    contract derives from the report: any failed secret makes the run rc 1.
    """

    repo: str
    dry_run: bool
    ruleset: RulesetOutcome
    labels: tuple[LabelOutcome, ...]
    secrets: tuple[SecretOutcome, ...]
    secrets_error: str | None = None

    @property
    def secrets_set(self) -> int:
        """Secrets actually pushed (a dry run pushes none)."""
        return sum(1 for s in self.secrets if s.action == "set")

    @property
    def secrets_skipped(self) -> int:
        return sum(1 for s in self.secrets if s.action == "skipped")

    @property
    def secrets_failed(self) -> int:
        return sum(1 for s in self.secrets if s.action == "failed")

    def to_dict(self) -> dict:
        return {
            "repo": self.repo,
            "dry_run": self.dry_run,
            "ruleset": self.ruleset.to_dict(),
            "labels": [{"name": lb.name, "action": lb.action} for lb in self.labels],
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


# --------------------------------------------------------------------------
# Passes
# --------------------------------------------------------------------------


def apply_ruleset(repo: str, checks: list[str], *, dry_run: bool) -> RulesetOutcome:
    """Pass (a). Create-or-update the standard ruleset; returns its outcome."""
    template = load_template()
    body = build_payload(template, checks)
    list_error: str | None = None
    try:
        rulesets = gh.rest(f"repos/{repo}/rulesets")
    except execrun.ExecError as exc:
        # Degraded-but-continuing: an unreadable listing reads as "no existing
        # ruleset", so the pass falls through to a POST — and the guess is a
        # report fact (``list_error``), so a consumer can tell "verified
        # absent" from "could not list, assumed none".
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
    """Pass (b). Create-or-update each label; returns one outcome per label."""
    outcomes: list[LabelOutcome] = []
    for label in labels:
        if dry_run:
            outcomes.append(LabelOutcome(name=label.name, action="dry-run"))
            continue
        gh.label_create(
            repo, label.name, description=label.description, color=label.color
        )
        # Per-label upserts are mechanics; the pass milestone is logged below.
        logger.debug("label upserted", extra={"repo": repo, "label": label.name})
        outcomes.append(LabelOutcome(name=label.name, action="upserted"))
    if not dry_run:
        logger.info("labels ensured", extra={"repo": repo, "labels": len(labels)})
    return tuple(outcomes)


def push_secrets(
    repo: str,
    sources: list[config.SecretSource],
    *,
    dry_run: bool,
    prompt=None,
) -> tuple[SecretOutcome, ...]:
    """Pass (c). Resolve and push each secret; returns one outcome per source.

    A required source that can't be resolved is recorded as failed — it does
    NOT abort the pass, so one bad secret never strands the others (or crashes
    gh-setup after the ruleset/labels already applied).
    """
    outcomes: list[SecretOutcome] = []
    for source in sources:
        # Dry-run must have no side effects — do NOT resolve (which would hit
        # doppler or prompt); just record the intended source.
        if dry_run:
            outcomes.append(
                SecretOutcome(name=source.name, source=source.kind, action="dry-run")
            )
            continue
        try:
            value = secretsrc.resolve(source, prompt=prompt)
        except secretsrc.SecretSourceError as exc:
            # Degraded-but-continuing: the pass keeps going so one bad secret
            # never strands the others; the run's exit code carries the failure.
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
        # The secret NAME is the record; the value never reaches a log call.
        logger.info(
            "secret set",
            extra={"repo": repo, "secret": source.name, "source": source.kind},
        )
        outcomes.append(
            SecretOutcome(name=source.name, source=source.kind, action="set")
        )
    return tuple(outcomes)


# --------------------------------------------------------------------------
# Orchestrator
# --------------------------------------------------------------------------


def setup(
    repo: Repo,
    *,
    checks_override: list[str] | None = None,
    local_checkout: str | None = None,
    config_path: str | None = None,
    dry_run: bool = False,
    prompt: Callable[[str], str] | None = None,
) -> SetupReport:
    """Drive the three passes against ``repo``; returns the one frozen report.

    ``local_checkout`` is the target's local checkout root when shipit runs
    inside the target repo — it enables workflow auto-discovery (a remote-only
    target passes ``None`` and relies on ``checks_override`` or runs-based
    discovery). ``config_path`` is the resolved ``.shipit.toml`` location; the
    CLI threads the ambient checkout's, a direct caller may omit it to read
    ``local_checkout``'s (falling back to the current directory).

    Raises nothing for the in-run degradations (unresolvable secret, missing
    config — those are report facts); a boundary failure applying the ruleset
    or a label IS raised (:class:`~shipit.execrun.ExecError`), since the run
    cannot meaningfully continue past a broken gh.
    """
    started = time.monotonic()
    slug = repo.slug
    if checks_override is not None:
        checks = [c for c in checks_override if c]
    else:
        default_branch = gh.default_branch(slug)
        checks = checks_mod.discover(slug, default_branch, toplevel=local_checkout)
    if not checks:
        logger.warning(
            "no required checks discovered — ruleset applied without a "
            "required-status-checks gate",
            extra={"repo": slug},
        )

    ruleset = apply_ruleset(slug, checks, dry_run=dry_run)
    labels = ensure_labels(slug, load_labels(), dry_run=dry_run)

    cfg_path = config_path or str(Path(local_checkout or ".") / config.CONFIG_NAME)
    secrets_error: str | None = None
    sources: list[config.SecretSource] = []
    try:
        cfg = config.load(cfg_path)
        sources = config.load_secrets(cfg)
    except config.ConfigError as exc:
        # Degraded-but-continuing: the ruleset/labels passes already applied.
        secrets_error = str(exc)
        logger.warning("no secrets applied", exc_info=True, extra={"repo": slug})
    secrets = push_secrets(slug, sources, dry_run=dry_run, prompt=prompt)

    report = SetupReport(
        repo=slug,
        dry_run=dry_run,
        ruleset=ruleset,
        labels=labels,
        secrets=secrets,
        secrets_error=secrets_error,
    )
    # The run's milestone: every pass ran; a failed secret degrades the record
    # to WARNING because the run's exit code propagates it.
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
