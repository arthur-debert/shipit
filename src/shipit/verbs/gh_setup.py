"""gh-setup — make a GitHub repo conform to the portfolio standard.

Three idempotent passes (install AND update share this command):

  a. ruleset — apply the standard main-branch-protection ruleset, requiring the
     TARGET repo's own checks (auto-discovered, never phos's captured set).
  b. labels  — ensure the standard label set exists (create-or-update).
  c. secrets — resolve each ``[secrets]`` entry from .shipit.toml and push it.

Re-running is a clean no-op: the ruleset is PUT in place when it already exists,
labels are ``--force`` upserts, and a changed secret is re-set to its new value.
"""

from __future__ import annotations

import copy
import json
import logging
import sys
import time
import tomllib
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from .. import checks as checks_mod
from .. import config, execrun, gh, git, secretsrc

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
    """Inject ``checks`` into the template's ``required_status_checks`` rule."""
    body = copy.deepcopy(template)
    contexts = checks_mod.checks_json(checks)
    for rule in body.get("rules", []):
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
# Passes
# --------------------------------------------------------------------------


def apply_ruleset(repo: str, checks: list[str], *, dry_run: bool) -> str:
    """Pass (a). Returns the action taken: ``created`` / ``updated`` / ``dry-run``."""
    template = load_template()
    body = build_payload(template, checks)
    try:
        rulesets = gh.rest(f"repos/{repo}/rulesets")
    except execrun.ExecError:
        # Degraded-but-continuing: an unreadable listing reads as "no existing
        # ruleset", so the pass falls through to a POST.
        logger.warning(
            "could not list rulesets — assuming none exists",
            exc_info=True,
            extra={"repo": repo},
        )
        rulesets = None
    existing = existing_ruleset_id(rulesets, RULESET_NAME)

    print(
        f"  ruleset: {RULESET_NAME} "
        f"(existing id: {existing if existing is not None else 'none'})"
    )
    print(f"  checks:  {', '.join(checks) if checks else '(none)'}")
    if dry_run:
        print("  --- payload (dry-run, not sent) ---")
        print(json.dumps(body, indent=2))
        return "dry-run"
    if existing is not None:
        gh.rest(f"repos/{repo}/rulesets/{existing}", method="PUT", body=body)
        print("  ruleset updated")
        logger.info(
            "ruleset updated",
            extra={"repo": repo, "ruleset": RULESET_NAME, "checks": len(checks)},
        )
        return "updated"
    gh.rest(f"repos/{repo}/rulesets", method="POST", body=body)
    print("  ruleset created")
    logger.info(
        "ruleset created",
        extra={"repo": repo, "ruleset": RULESET_NAME, "checks": len(checks)},
    )
    return "created"


def ensure_labels(repo: str, labels: list[Label], *, dry_run: bool) -> int:
    """Pass (b). Create-or-update each label; returns the count processed."""
    for label in labels:
        if dry_run:
            print(f"  [dry] label {label.name}")
            continue
        gh.label_create(
            repo, label.name, description=label.description, color=label.color
        )
        print(f"  label {label.name}")
        # Per-label upserts are mechanics; the pass milestone is logged below.
        logger.debug("label upserted", extra={"repo": repo, "label": label.name})
    if not dry_run:
        logger.info("labels ensured", extra={"repo": repo, "labels": len(labels)})
    return len(labels)


def push_secrets(
    repo: str,
    sources: list[config.SecretSource],
    *,
    dry_run: bool,
    prompt=None,
) -> tuple[int, int, int]:
    """Pass (c). Resolve and push each secret; returns (set, skipped, failed).

    A required source that can't be resolved is reported and counted as failed
    — it does NOT abort the pass, so one bad secret never strands the others (or
    crashes gh-setup after the ruleset/labels already applied).
    """
    set_count = 0
    skipped = 0
    failed = 0
    for source in sources:
        # Dry-run must have no side effects — do NOT resolve (which would hit
        # doppler or prompt); just report the intended source.
        if dry_run:
            print(f"  [dry] secret {source.name} (from {source.kind})")
            set_count += 1
            continue
        try:
            value = secretsrc.resolve(source, prompt=prompt)
        except secretsrc.SecretSourceError as exc:
            print(f"  FAIL {source.name}: {exc}")
            # Degraded-but-continuing: the pass keeps going so one bad secret
            # never strands the others; the run's exit code carries the failure.
            logger.warning(
                "secret could not be resolved",
                exc_info=True,
                extra={"repo": repo, "secret": source.name, "source": source.kind},
            )
            failed += 1
            continue
        if value is None:
            print(f"  skip {source.name} (optional source absent)")
            logger.debug(
                "secret skipped (optional source absent)",
                extra={"repo": repo, "secret": source.name, "source": source.kind},
            )
            skipped += 1
            continue
        gh.secret_set(source.name, value, repo=repo)
        print(f"  secret {source.name}")
        # The secret NAME is the record; the value never reaches a log call.
        logger.info(
            "secret set",
            extra={"repo": repo, "secret": source.name, "source": source.kind},
        )
        set_count += 1
    return set_count, skipped, failed


# --------------------------------------------------------------------------
# Orchestrator
# --------------------------------------------------------------------------


def run(
    repo: str | None,
    *,
    config_path: str | None,
    checks_override: list[str] | None,
    dry_run: bool,
    prompt=None,
) -> int:
    """Drive the three passes against ``repo`` (current checkout when omitted)."""
    started = time.monotonic()
    toplevel = git.repo_root()
    current = None
    if toplevel:
        try:
            # The typed adapter read (PROC03); this verb's helpers speak slugs.
            # ValueError is gh answering without a usable owner/name — treated
            # like the transport failure: no inferable repo.
            current = gh.current_repo().slug
        except (execrun.ExecError, ValueError):
            current = None
    target = repo or current
    if not target:
        print(
            "gh-setup: no repo given and not inside a GitHub checkout", file=sys.stderr
        )
        logger.error("no repo given and not inside a GitHub checkout")
        return 1
    print(f"gh-setup: {target}{' (dry-run)' if dry_run else ''}")

    # (a) ruleset
    print("ruleset:")
    if checks_override is not None:
        checks = [c for c in checks_override if c]
    else:
        default_branch = gh.default_branch(target)
        # Auto-discovery reads the target's own workflow files, so it needs the
        # target's local checkout. For a different remote target, pass --checks.
        local = toplevel if (toplevel and target == current) else None
        checks = checks_mod.discover(target, default_branch, toplevel=local)
    if not checks:
        print(
            "  warning: no required checks discovered — applying ruleset with an "
            "empty required-checks set. Pass --checks a,b to set them explicitly.",
            file=sys.stderr,
        )
        logger.warning(
            "no required checks discovered — ruleset applied with an empty set",
            extra={"repo": target},
        )
    apply_ruleset(target, checks, dry_run=dry_run)

    # (b) labels
    print("labels:")
    ensure_labels(target, load_labels(), dry_run=dry_run)

    # (c) secrets
    print("secrets:")
    cfg_path = config_path or str(Path(toplevel or ".") / config.CONFIG_NAME)
    try:
        cfg = config.load(cfg_path)
        sources = config.load_secrets(cfg)
    except config.ConfigError as exc:
        print(f"  no secrets applied: {exc}")
        # Degraded-but-continuing: the ruleset/labels passes already applied.
        logger.warning("no secrets applied", exc_info=True, extra={"repo": target})
        sources = []
    set_count = skipped = failed = 0
    if sources:
        set_count, skipped, failed = push_secrets(
            target, sources, dry_run=dry_run, prompt=prompt
        )
        print(f"  {set_count} secret(s) set, {skipped} skipped, {failed} failed")

    print("done.")
    # The verb's milestone: every pass ran; a failed secret degrades the record
    # to WARNING because the run's exit code propagates it.
    log = logger.warning if failed else logger.info
    log(
        "gh-setup complete",
        extra={
            "repo": target,
            "dry_run": dry_run,
            "secrets_set": set_count,
            "secrets_skipped": skipped,
            "secrets_failed": failed,
            "duration_ms": int((time.monotonic() - started) * 1000),
        },
    )
    return 1 if failed else 0
