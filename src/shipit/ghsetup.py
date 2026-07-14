"""The gh-setup domain — make a GitHub repo conform to the portfolio standard.

Four idempotent passes (install AND update share this surface):

  a. ruleset — apply the standard main-branch-protection ruleset, requiring the
     TARGET repo's own checks (auto-discovered, never phos's captured set).
  b. labels  — ensure the standard label set exists (create-or-update).
  c. secrets — sync the DERIVED requirement set (TOL02-WS02, PRD stories
     44/45): the registry declarations traversed from the artifact map AND
     the ``[reviewers]`` table (#740 — a declared funnel reviewer's App
     credential pair rides the derived set) decide WHICH names must exist
     (:mod:`shipit.release.secretreq`); the ``.shipit.toml [secrets]`` table
     only says where each comes from. A required name with no declared source
     fails the sync naming the requiring entry; a declared entry nothing
     requires is flagged as an orphan and NOT pushed (never under- or
     over-provisions); the doppler/env/prompt resolution path is unchanged.
  d. workflow access — VERIFY-AND-WARN ONLY (#739, decided scope): a PRIVATE
     repo that publishes reusable (``workflow_call``) workflows with the
     Actions access level at ``none`` is uncallable by every other repo
     (TOL02-WS07 finding 5), so the pass reads
     ``repos/{owner}/{repo}/actions/permissions/access`` and warns, naming
     the fix per owner kind (``user`` / ``organization``). It NEVER sets the
     level — cross-owner publishing needs a public repo at any setting
     (ADR-0053), so full management is structurally pointless here. A public
     repo is typed not-applicable WITHOUT touching the access endpoint (it
     422s there); an inspection failure is reported as ``unknown``, distinct
     from a verified ``none``.

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
    ``"failed"`` (required source unresolvable, or — story 45 — a derived
    requirement with no ``[secrets]`` source at all, ``source`` then
    ``"none"`` and ``reason`` naming the requiring entry) / ``"orphan"``
    (declared but nothing requires it — flagged, not pushed) / ``"dry-run"``
    (not resolved — a dry run must not hit doppler or prompt). ``reason``
    says why for the skipped/failed/orphan outcomes; the secret VALUE never
    appears anywhere.
    """

    name: str
    source: str
    action: str
    reason: str | None = None


@dataclass(frozen=True)
class WorkflowAccessOutcome:
    """Pass (d)'s outcome — the verify-and-warn read, never a mutation (#739).

    ``status`` is one of:

    - ``"not-applicable"`` — the repo is public (any repo can call its
      reusable workflows, ADR-0053) or it publishes no ``workflow_call``
      workflow; ``reason`` says which. The access endpoint was NOT called.
    - ``"acceptable"`` — a private publisher whose ``access_level`` is not
      ``none`` (``user``/``organization``/``enterprise``).
    - ``"warn"`` — a private publisher VERIFIED at ``access_level: none``:
      no other repo can call its reusable workflows (TOL02-WS07 finding 5).
      ``recommended_level`` names the fix for the owner kind and ``reason``
      carries the exact command; gh-setup never runs it.
    - ``"unknown"`` — the inspection itself failed (auth/transport/malformed
      payload); ``reason`` carries the error. Distinct from a verified
      ``none`` on purpose: "could not look" must never read as "looked and
      it's broken" (or vice versa).
    """

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
    workflow_access: WorkflowAccessOutcome
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

    @property
    def secrets_orphaned(self) -> int:
        """Declared ``[secrets]`` entries nothing requires (flagged, never
        pushed, never rc-relevant — the drift signal of story 45)."""
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


def verify_workflow_access(
    repo: str, *, local_checkout: str | None
) -> WorkflowAccessOutcome:
    """Pass (d). Verify (never set) the Actions access level of a private
    reusable-workflow publisher (#739).

    Reads only, so a dry run and a real run are the SAME pass. The gates in
    order — each skipping the rest:

    1. A public repo is not-applicable WITHOUT calling the access endpoint
       (it returns 422 for public repos), per ADR-0053: public reusable
       workflows are callable by any repo at any setting.
    2. A private repo publishing no ``workflow_call`` workflow under
       ``.github/workflows/`` is not-applicable — nothing to call. Detection
       reads the local checkout when ``local_checkout`` is given (the ambient
       case), the contents API otherwise (an explicitly named remote repo).
    3. ``GET repos/{repo}/actions/permissions/access`` — ``none`` warns,
       naming ``access_level=user`` or ``organization`` from the owner kind
       (the repos payload's ``owner.type``); anything else is acceptable.

    Every inspection failure (transport, auth, malformed payload) degrades to
    the ``unknown`` outcome — a report fact, never a raise: gh-setup's other
    passes must not be stranded by an advisory read.
    """
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
        # Degraded-but-continuing, and DISTINCT from a verified `none`:
        # "could not look" must never render as a warn (or as acceptable).
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
    """Pass (c). Sync the derived requirement set against the ``[secrets]``
    sources (TOL02-WS02, PRD stories 44/45).

    The required names are the registry declarations traversed from the
    artifact map plus the ``[reviewers]`` declarations
    (:mod:`shipit.release.secretreq`, #740): ``reviewers`` is the validated
    roster's required-name tuple, and each declared funnel reviewer (codex /
    agy) contributes its App credential pair to the required set — so a repo
    that opts an App reviewer in fails LOUD at sync time when the credentials
    are unsourced, and a repo that doesn't sees the seeded pairs flagged as
    orphans instead of pushed. Three outcome groups, in order:

    - the non-orphan declared sources, resolved and pushed by
      :func:`push_secrets` (dry-run resolves nothing); a source whose name is in
      the derived required set is forced non-optional first, so its `optional`
      flag can never turn a missing REQUIRED value into a silent skip (story 44 —
      the sync never under-provisions);
    - one ``failed`` outcome per derived requirement with NO declared source,
      naming the requiring entry (the sync-time error of story 45);
    - one ``failed`` outcome per ALTERNATIVE-SET requirement (#746 — the
      notary trios) with no complete alternative sourced: ONE diagnostic
      naming what is missing from every alternative. A repo sources either
      trio (or both); whichever it declares is pushed, and the unused trio is
      neither demanded nor orphaned;
    - one ``orphan`` outcome per declared source nothing requires — flagged
      and NOT pushed (never over-provisions, story 44).
    """
    orphan_names = set(secretreq.orphans(artifacts, sources, reviewers=reviewers))
    required_names = set(secretreq.required_names(artifacts, reviewers=reviewers))
    # A derived-REQUIRED secret is required by definition: its `optional` flag
    # cannot make an absent value a silent skip, or the sync would succeed while
    # under-provisioning (story 44). The derivation wins over the flag — force
    # required sources non-optional so a missing value resolves to `failed`, not
    # `skipped`. A genuinely optional source (nothing requires it) keeps its
    # flag. A declared reviewer's App credentials are in the required set like
    # any other derived name (#740) — a hand-edited `optional = true` on a pair
    # the repo's reviewers need can no longer sync "clean" and break the App
    # later at review-posting time.
    to_push = [
        replace(source, optional=False)
        if source.optional and source.name in required_names
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
    # The either-satisfies requirements (#746): any complete provisioned
    # alternative satisfies one; none complete is ONE failed outcome whose
    # reason names what is missing from every alternative. A declaration is
    # not enough here: an optional source may resolve absent and be skipped,
    # so only successfully set names can satisfy a live sync. Dry-run cannot
    # resolve sources without side effects, so its prospective report uses
    # the declared set just as the plain-name dry-run does.
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
    """Resolve and push each given secret; returns one outcome per source.

    The WHICH decision happened upstream (:func:`sync_secrets` — the derived
    requirement set); this loop owns only resolution and push. A required
    source that can't be resolved is recorded as failed — it does NOT abort
    the pass, so one bad secret never strands the others (or crashes
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
    """Drive the four passes against ``repo``; returns the one frozen report.

    ``local_checkout`` is the target's local checkout root when shipit runs
    inside the target repo — it enables workflow auto-discovery (a remote-only
    target passes ``None`` and relies on ``checks_override`` or runs-based
    discovery) and feeds pass (d)'s local ``workflow_call`` publisher
    detection (a remote target is inspected via the contents API instead).
    ``config_path`` is the resolved ``.shipit.toml`` location; the
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
            "no required checks found — ruleset applied without a "
            "required-status-checks gate",
            extra={"repo": slug},
        )

    ruleset = apply_ruleset(slug, checks, dry_run=dry_run)
    labels = ensure_labels(slug, load_labels(), dry_run=dry_run)
    # Pass (d) reads only, so dry-run runs it identically — the report is the
    # same either way, and a dry run still surfaces the access warning.
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
        # The reviewers derivation input (#740): parse the already-loaded config
        # through the ONE canonical reviewer parser.  Passing both the dictionary
        # and its exact path keeps [reviewers], [secrets], and [artifacts] on one
        # file even when a direct caller uses a nonstandard config filename.
        reviewers = reviewers_config.parse_roster(
            cfg, config_path=cfg_path
        ).required_names
    except (
        config.ConfigError,
        reviewers_config.RequiredReviewersConfigError,
    ) as exc:
        # Degraded-but-continuing: the ruleset/labels passes already applied.
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
        # No parsed declarations to derive from — the config failure is the
        # report fact; deriving against an empty map would mint phantom
        # missing-source failures on top of it.
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
