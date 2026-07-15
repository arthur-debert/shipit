"""store_provision — provision the Artifact channel's two access-tier buckets (ARF01-WS03).

The Artifact channel store is **two dedicated GCS buckets** in the existing
sccache GCP project, on a lifecycle *separate from the sccache bucket* (ADR-0065,
docs/spec/artifact-channel.md):

- a **public** bucket — open artifacts, authless HTTPS reads: ``allUsers`` is
  granted ``roles/storage.objectViewer`` bucket-wide; and
- a **private** bucket — closed artifacts, GCS credentials required: a dedicated
  reader **service account** is granted bucket-scoped ``roles/storage.objectViewer``
  and there is **no** public binding.

Both buckets have **uniform bucket-level access (UBLA)** on — the access model is
IAM-only, no per-object ACLs (ADR-0065: "public" is a bucket-wide ``allUsers``
grant under UBLA, which is exactly why the two tiers are two buckets, not two
prefixes). The private bucket additionally enforces **public-access-prevention**
so a public binding cannot even be added by mistake; the public bucket leaves it
``inherited`` so the ``allUsers`` grant is permitted.

Separate lifecycle: these buckets carry **no** object-lifecycle / TTL rule (the
sccache purge targets the sccache bucket by name; artifacts are permanent), so a
cache purge can never touch them. They are distinct, clearly-named buckets — see
:func:`bucket_name`.

## Shape (ADR-0028 / ADR-0021 — pure core + one Exec seam)

The decision core is pure and unit-tested directly: the derived
:func:`bucket_name` / :func:`reader_sa_email`, every ``gcloud`` argv builder,
and the :func:`ubla_enabled` / :func:`has_public_binding` verdict readers.
Everything that touches the world goes through the one injectable ``runner``
(:mod:`shipit.execrun`) — :func:`provision` orchestrates create-then-configure
idempotently, :func:`verify` runs the live acceptance checks.

## Idempotence

:func:`provision` is a **describe-then-act** orchestration: it probes each
resource (service account, buckets) and only creates what is absent, then
re-asserts UBLA / public-access-prevention (``update`` is a no-op when already
set) and re-adds the IAM bindings (``add-iam-policy-binding`` of an existing
binding is a no-op). Running it twice on a fully-provisioned project performs no
mutation — every action reports ``noop``.

## Siting — NOT in the test checks, NOT a consumer verb

This provisions live cloud infrastructure in the operator's GCP project and
needs the operator's own ``gcloud`` credentials, so — like the review-App
provisioning harness (:mod:`shipit.review.funnel_verify`,
docs/dev/review-app-provisioning.md) — it is an **opt-in operator** entrypoint,
never part of ``pixi run test`` / CI (which have no gcloud / no project) and not
a per-consumer ``shipit`` verb. It is a ``python -m shipit.channel.store_provision``
entrypoint that REFUSES to run without an explicit ``--project`` and is never
collected by pytest (it lives in ``src/``); its orchestration and verdict logic
are regression-covered by ``tests/test_channel_store_provision.py`` with the
``gcloud`` boundary FAKED. The runbook — idempotent steps, teardown, and key
rotation — is ``docs/dev/artifact-channel-store-provisioning.md``.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field

from .. import execrun

logger = logging.getLogger("shipit.channel")

# --------------------------------------------------------------------------
# The pure decision core — derived identities and vocabulary
# --------------------------------------------------------------------------

#: The two access tiers (ADR-0065). The tier of a channel is which bucket it
#: lives in; the producing repo's visibility selects the tier (WS elsewhere).
TIER_PUBLIC = "public"
TIER_PRIVATE = "private"
TIERS = (TIER_PUBLIC, TIER_PRIVATE)

#: The infix that makes a bucket name unmistakably the Artifact channel store —
#: and unmistakably NOT the sccache bucket (the "clearly named, distinct from
#: the sccache bucket" acceptance criterion). GCS bucket names are globally
#: unique, 3-63 chars, lowercase; deriving from the project keeps them unique
#: without a second config knob.
_BUCKET_INFIX = "artifact-channel"

#: The private tier's reader service account short name (the local part of its
#: email). Its ONLY grant is bucket-scoped ``objectViewer`` on the private
#: bucket; HMAC interop keys minted for it are what a private-tier consumer uses
#: (ADR-0065). Rotation = mint a new HMAC key for this SA, roll consumers, delete
#: the old key (see the runbook) — the SA and its bucket binding never change.
READER_SA_NAME = "artifact-channel-reader"

#: The bucket-scoped role both tiers grant (read-only object access). Neither
#: tier ever grants a write role here — publish (producer CI) is a SEPARATE
#: credential on a SEPARATE work stream (ADR-0065 Consequences).
OBJECT_VIEWER_ROLE = "roles/storage.objectViewer"

#: The public tier's grantee: every principal, authenticated or not — an
#: authless HTTPS GET resolves (ADR-0065).
ALL_USERS = "allUsers"

#: The GCS global HTTPS host authless public reads use (ADR-0065).
_GCS_HOST = "https://storage.googleapis.com"

#: :attr:`Action` values.
ACTION_CREATED = "created"  # the resource was absent and was created
ACTION_NOOP = "noop"  # the resource already existed; only re-asserted config


class ProvisionError(RuntimeError):
    """A provisioning refusal — a missing project, a gcloud failure the
    orchestration cannot treat as "already exists", or a malformed argument.
    Rendered as ``error: …`` + exit 1 by the entrypoint."""


def bucket_name(project: str, tier: str) -> str:
    """The derived, globally-unique bucket name for ``project`` + ``tier``.

    e.g. ``supage-prod`` → ``supage-prod-artifact-channel-public`` /
    ``…-artifact-channel-private``. The ``artifact-channel`` infix is what makes
    the name self-describing and distinct from the sccache bucket. Refuses an
    unknown tier (a mistyped tier must never resolve to a real bucket).
    """
    if tier not in TIERS:
        raise ProvisionError(f"store provision: unknown tier {tier!r}")
    return f"{project}-{_BUCKET_INFIX}-{tier}"


def reader_sa_email(project: str) -> str:
    """The private-tier reader service account's email in ``project``."""
    return f"{READER_SA_NAME}@{project}.iam.gserviceaccount.com"


def public_object_url(bucket: str, repo: str, obj: str = "repodata.json") -> str:
    """The authless HTTPS URL of ``<repo>/<obj>`` in ``bucket``.

    The per-repo channel root is a subdir keyed by the producing repo
    (ADR-0065: each repo is the sole writer of its own ``repodata.json``), so the
    acceptance probe fetches ``<repo>/repodata.json`` — a 200 on the public
    bucket, a 403 on the private one.
    """
    return f"{_GCS_HOST}/{bucket}/{repo}/{obj}"


# --------------------------------------------------------------------------
# The pure decision core — gcloud argv builders (ADR-0028 assembly home)
# --------------------------------------------------------------------------
#
# Every ``gcloud`` argv shipit assembles is built HERE and only here (the
# whitelisted adapter home in tests/test_tool_argv_sweep.py). Each is a literal
# starting with "gcloud" so the AST sweep can see it.


def _bucket_uri(bucket: str) -> str:
    return f"gs://{bucket}"


def describe_bucket_argv(bucket: str) -> list[str]:
    """``gcloud`` argv reading a bucket's metadata as JSON (the existence probe
    and the UBLA / public-access-prevention verdict source)."""
    return [
        "gcloud",
        "storage",
        "buckets",
        "describe",
        _bucket_uri(bucket),
        "--format=json",
    ]


def create_bucket_argv(
    project: str, bucket: str, location: str, *, public: bool
) -> list[str]:
    """``gcloud`` argv creating ``bucket`` with UBLA on and the tier's
    public-access-prevention.

    Public tier: ``--public-access-prevention=inherited`` so the ``allUsers``
    binding is permitted. Private tier: ``--public-access-prevention=enforced``
    so a public binding cannot be added at all — the "no public access
    (verified)" criterion made structural, not just unbound.
    """
    pap = "inherited" if public else "enforced"
    return [
        "gcloud",
        "storage",
        "buckets",
        "create",
        _bucket_uri(bucket),
        f"--project={project}",
        f"--location={location}",
        "--uniform-bucket-level-access",
        f"--public-access-prevention={pap}",
    ]


def configure_bucket_argv(bucket: str, *, public: bool) -> list[str]:
    """``gcloud`` argv re-asserting UBLA + public-access-prevention on an
    existing bucket — idempotent (a no-op when already so)."""
    pap = "inherited" if public else "enforced"
    return [
        "gcloud",
        "storage",
        "buckets",
        "update",
        _bucket_uri(bucket),
        "--uniform-bucket-level-access",
        f"--public-access-prevention={pap}",
    ]


def add_iam_binding_argv(bucket: str, member: str) -> list[str]:
    """``gcloud`` argv granting ``member`` bucket-scoped ``objectViewer`` —
    idempotent (adding an existing binding returns the policy unchanged)."""
    return [
        "gcloud",
        "storage",
        "buckets",
        "add-iam-policy-binding",
        _bucket_uri(bucket),
        f"--member={member}",
        f"--role={OBJECT_VIEWER_ROLE}",
    ]


def get_iam_policy_argv(bucket: str) -> list[str]:
    """``gcloud`` argv reading a bucket's IAM policy as JSON (the
    "no public binding on the private bucket" verdict source)."""
    return [
        "gcloud",
        "storage",
        "buckets",
        "get-iam-policy",
        _bucket_uri(bucket),
        "--format=json",
    ]


def describe_sa_argv(project: str, email: str) -> list[str]:
    """``gcloud`` argv probing whether the reader service account exists."""
    return [
        "gcloud",
        "iam",
        "service-accounts",
        "describe",
        email,
        f"--project={project}",
        "--format=json",
    ]


def create_sa_argv(project: str, name: str) -> list[str]:
    """``gcloud`` argv creating the reader service account."""
    return [
        "gcloud",
        "iam",
        "service-accounts",
        "create",
        name,
        f"--project={project}",
        "--display-name=Artifact channel private-tier reader",
    ]


def object_read_as_sa_argv(
    bucket: str, repo: str, sa_email: str, obj: str = "repodata.json"
) -> list[str]:
    """``gcloud`` argv reading a private object AS the reader SA (impersonation)
    — the scoped-credential positive: the SA's bucket binding grants the read."""
    return [
        "gcloud",
        "storage",
        "objects",
        "describe",
        f"gs://{bucket}/{repo}/{obj}",
        f"--impersonate-service-account={sa_email}",
        "--format=json",
    ]


# --------------------------------------------------------------------------
# The pure decision core — verdict readers over gcloud --format=json output
# --------------------------------------------------------------------------


def _load_json(text: str, what: str) -> object:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProvisionError(f"store provision: unreadable {what} JSON: {exc}") from exc


def ubla_enabled(describe_json: str) -> bool:
    """Whether ``buckets describe`` output shows UBLA enabled.

    gcloud renders it as ``{"uniform_bucket_level_access": true}`` (storage
    client spelling) or the API's nested
    ``{"iamConfiguration": {"uniformBucketLevelAccess": {"enabled": true}}}``;
    accept either so the verdict survives a gcloud output-shape difference.
    """
    data = _load_json(describe_json, "bucket describe")
    if not isinstance(data, dict):
        return False
    if data.get("uniform_bucket_level_access") is True:
        return True
    iam_cfg = data.get("iamConfiguration")
    if isinstance(iam_cfg, dict):
        ubla = iam_cfg.get("uniformBucketLevelAccess")
        if isinstance(ubla, dict):
            return ubla.get("enabled") is True
    return False


def has_public_binding(iam_policy_json: str) -> bool:
    """Whether an IAM policy grants any role to ``allUsers`` / ``allAuthenticatedUsers``.

    The private-tier "no public access" check: a policy with an ``allUsers``
    (or ``allAuthenticatedUsers``) member in ANY binding is public. gcloud
    ``get-iam-policy --format=json`` renders ``{"bindings": [{"members": […]}]}``.
    """
    data = _load_json(iam_policy_json, "iam policy")
    bindings = data.get("bindings", []) if isinstance(data, dict) else []
    public_members = {ALL_USERS, "allAuthenticatedUsers"}
    for binding in bindings:
        if isinstance(binding, dict) and public_members & set(
            binding.get("members", [])
        ):
            return True
    return False


# --------------------------------------------------------------------------
# The typed reports (ADR-0030 — rendered at the edge)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Action:
    """One provisioning action: a named resource and whether it was created or
    already existed. Config re-assertions (UBLA, IAM binding) run every time and
    are not separately reported — they are idempotent no-ops when already set."""

    resource: str
    action: str  # ACTION_CREATED | ACTION_NOOP

    def to_dict(self) -> dict[str, str]:
        return {"resource": self.resource, "action": self.action}


@dataclass(frozen=True)
class ProvisionReport:
    """The typed result of one provisioning run."""

    project: str
    location: str
    public_bucket: str
    private_bucket: str
    reader_sa: str
    actions: tuple[Action, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "project": self.project,
            "location": self.location,
            "public_bucket": self.public_bucket,
            "private_bucket": self.private_bucket,
            "reader_sa": self.reader_sa,
            "actions": [a.to_dict() for a in self.actions],
        }


@dataclass
class VerifyReport:
    """The typed result of the live acceptance run — one bool per criterion.

    ``ok`` is the conjunction: every acceptance check passed. ``notes`` carries
    any check that could not run (e.g. the private positive read needs a
    published object) so a partial verification is HONEST, never silently a pass.
    """

    public_get_200: bool = False
    private_get_403: bool = False
    private_scoped_read_ok: bool = False
    public_ubla_on: bool = False
    private_ubla_on: bool = False
    private_no_public_binding: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(
            (
                self.public_get_200,
                self.private_get_403,
                self.private_scoped_read_ok,
                self.public_ubla_on,
                self.private_ubla_on,
                self.private_no_public_binding,
            )
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "public_get_200": self.public_get_200,
            "private_get_403": self.private_get_403,
            "private_scoped_read_ok": self.private_scoped_read_ok,
            "public_ubla_on": self.public_ubla_on,
            "private_ubla_on": self.private_ubla_on,
            "private_no_public_binding": self.private_no_public_binding,
            "notes": list(self.notes),
        }


# --------------------------------------------------------------------------
# The boundary — provision
# --------------------------------------------------------------------------


def _exists(argv: list[str], runner: Callable[..., execrun.ExecResult]) -> bool:
    """Run a describe probe; True on rc 0, False on a nonzero exit.

    ``check=False`` — a missing resource is gcloud's NORMAL "not found" (nonzero)
    answer, not a transport failure — and a launch failure (gcloud absent) is a
    real refusal, so it is left to raise as :class:`~shipit.execrun.ExecError`.
    """
    return runner(argv, check=False).rc == 0


def provision(
    project: str,
    location: str = "US",
    *,
    runner: Callable[..., execrun.ExecResult] = execrun.run,
) -> ProvisionReport:
    """Provision both tier buckets + the reader SA + IAM, idempotently.

    Describe-then-act: the reader SA and each bucket are created only when
    absent; UBLA / public-access-prevention are re-asserted (``update`` no-ops
    when already set) and the IAM bindings re-added (``add-iam-policy-binding``
    no-ops on an existing binding). A second run on a fully-provisioned project
    mutates nothing — every :class:`Action` reports ``noop``.

    The world-touching steps go through ``runner`` (the one Exec seam, ADR-0028;
    injectable for tests). Requires the operator's own ``gcloud`` credentials
    with project-admin rights — see docs/dev/artifact-channel-store-provisioning.md.
    """
    if not project:
        raise ProvisionError("store provision: a --project is required")
    public = bucket_name(project, TIER_PUBLIC)
    private = bucket_name(project, TIER_PRIVATE)
    sa_email = reader_sa_email(project)
    actions: list[Action] = []

    # 1. The private-tier reader service account — before its bucket binding.
    if _exists(describe_sa_argv(project, sa_email), runner):
        actions.append(Action(sa_email, ACTION_NOOP))
    else:
        runner(create_sa_argv(project, READER_SA_NAME))
        actions.append(Action(sa_email, ACTION_CREATED))

    # 2. Each bucket: create-if-absent, then re-assert UBLA + PAP.
    for name, is_public in ((public, True), (private, False)):
        if _exists(describe_bucket_argv(name), runner):
            actions.append(Action(name, ACTION_NOOP))
        else:
            runner(create_bucket_argv(project, name, location, public=is_public))
            actions.append(Action(name, ACTION_CREATED))
        # Idempotent re-assertion — safe whether just-created or pre-existing.
        runner(configure_bucket_argv(name, public=is_public))

    # 3. IAM bindings — public bucket to allUsers, private bucket to the SA.
    runner(add_iam_binding_argv(public, ALL_USERS))
    runner(add_iam_binding_argv(private, f"serviceAccount:{sa_email}"))

    logger.info(
        "artifact-channel store provisioned",
        extra={
            "project": project,
            "public_bucket": public,
            "private_bucket": private,
            "reader_sa": sa_email,
        },
    )
    return ProvisionReport(
        project=project,
        location=location,
        public_bucket=public,
        private_bucket=private,
        reader_sa=sa_email,
        actions=tuple(actions),
    )


# --------------------------------------------------------------------------
# The boundary — verify (the live acceptance checks)
# --------------------------------------------------------------------------


def _http_status(url: str) -> int:
    """Authless HTTPS GET → status code. 200 on success; the HTTP error code on
    a 4xx/5xx (403 is the expected private-tier no-creds answer, not a failure)."""
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310 — https literal
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def verify(
    project: str,
    repo: str,
    *,
    obj: str = "repodata.json",
    runner: Callable[..., execrun.ExecResult] = execrun.run,
    http_get: Callable[[str], int] = _http_status,
) -> VerifyReport:
    """Run the ARF01-WS03 acceptance checks against the provisioned store.

    Each criterion → one boolean on the :class:`VerifyReport`:

    - public authless GET of ``<repo>/<obj>`` returns **200**;
    - private authless GET returns **403** (no creds → denied);
    - private read AS the reader SA succeeds (scoped credential works);
    - UBLA is on for **both** buckets;
    - the private bucket has **no** public IAM binding.

    ``runner`` (gcloud) and ``http_get`` (an HTTPS GET → status) are injectable
    so the verdict logic is unit-tested without live cloud. Live, this needs a
    published ``<repo>/<obj>`` object in each bucket; when the private object is
    absent the scoped-read positive cannot be asserted and lands in ``notes``.
    """
    public = bucket_name(project, TIER_PUBLIC)
    private = bucket_name(project, TIER_PRIVATE)
    sa_email = reader_sa_email(project)
    report = VerifyReport()

    report.public_get_200 = http_get(public_object_url(public, repo, obj)) == 200
    report.private_get_403 = http_get(public_object_url(private, repo, obj)) == 403

    scoped = runner(object_read_as_sa_argv(private, repo, sa_email, obj), check=False)
    report.private_scoped_read_ok = scoped.rc == 0
    if scoped.rc != 0:
        report.notes.append(
            "private scoped read failed — publish a "
            f"{repo}/{obj} object under the private bucket to assert the positive"
        )

    report.public_ubla_on = ubla_enabled(runner(describe_bucket_argv(public)).stdout)
    report.private_ubla_on = ubla_enabled(runner(describe_bucket_argv(private)).stdout)
    report.private_no_public_binding = not has_public_binding(
        runner(get_iam_policy_argv(private)).stdout
    )
    return report


# --------------------------------------------------------------------------
# The opt-in operator entrypoint (never a pytest-collected verb)
# --------------------------------------------------------------------------


def _emit(payload: object, *, as_json: bool, human: str) -> None:
    print(json.dumps(payload, indent=2) if as_json else human)


def main(argv: list[str] | None = None) -> int:
    """``python -m shipit.channel.store_provision`` — REFUSES without ``--project``.

    Subcommands: ``provision`` (idempotent create/configure) and ``verify`` (the
    live acceptance checks). ``verify`` exits nonzero when any criterion fails.
    """
    parser = argparse.ArgumentParser(
        prog="python -m shipit.channel.store_provision",
        description="Provision / verify the Artifact channel's two GCS buckets (ARF01-WS03).",
    )
    parser.add_argument(
        "--project", required=True, help="the GCP project (the sccache project)"
    )
    parser.add_argument("--json", action="store_true", dest="as_json", help="emit JSON")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_prov = sub.add_parser(
        "provision", help="idempotently create/configure the buckets + IAM"
    )
    p_prov.add_argument(
        "--location", default="US", help="bucket location (default: US)"
    )
    p_ver = sub.add_parser("verify", help="run the live acceptance checks")
    p_ver.add_argument(
        "--repo", required=True, help="the per-repo channel subdir to probe"
    )
    p_ver.add_argument("--object", default="repodata.json", dest="obj")
    args = parser.parse_args(argv)

    try:
        if args.cmd == "provision":
            report = provision(args.project, args.location)
            _emit(
                report.to_dict(),
                as_json=args.as_json,
                human="\n".join(
                    f"store provision: {a.resource} — {a.action}"
                    for a in report.actions
                )
                + f"\n  public : gs://{report.public_bucket}"
                + f"\n  private: gs://{report.private_bucket} (reader {report.reader_sa})",
            )
            return 0
        vreport = verify(args.project, args.repo, obj=args.obj)
        _emit(
            vreport.to_dict(),
            as_json=args.as_json,
            human=f"store verify: {'PASS' if vreport.ok else 'FAIL'} {vreport.to_dict()}",
        )
        return 0 if vreport.ok else 1
    except ProvisionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover — the module entrypoint
    raise SystemExit(main())
