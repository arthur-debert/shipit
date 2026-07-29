"""``channel/store_provision`` — provision the Artifact channel's two GCS buckets.

Public tier (``allUsers`` reader, UBLA on) and private tier (a dedicated reader
service account, public-access-prevention enforced).
See docs/adr/0065-artifact-channel-store.md.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from .. import execrun
from . import buckets

logger = logging.getLogger("shipit.channel")

TIER_PUBLIC = "public"
TIER_PRIVATE = "private"
TIERS = (TIER_PUBLIC, TIER_PRIVATE)

#: The local part of the private tier's reader service account email.
READER_SA_NAME = "artifact-channel-reader"

#: The read-only bucket-scoped role both tiers grant; neither ever grants write.
OBJECT_VIEWER_ROLE = "roles/storage.objectViewer"

ALL_USERS = "allUsers"

_GCS_HOST = buckets.CHANNEL_HOST

#: :attr:`Action` values.
ACTION_CREATED = "created"
ACTION_NOOP = "noop"


class ProvisionError(RuntimeError):
    """A provisioning refusal; the entrypoint renders it as ``error: …`` + exit 1."""


def bucket_name(tier: str) -> str:
    """The fixed, portfolio-wide bucket name for ``tier``; refuses an unknown tier."""
    if tier == TIER_PUBLIC:
        return buckets.PUBLIC_ARTIFACT_BUCKET
    if tier == TIER_PRIVATE:
        return buckets.PRIVATE_ARTIFACT_BUCKET
    raise ProvisionError(f"store: unknown tier {tier!r}")


def reader_sa_email(project: str) -> str:
    return f"{READER_SA_NAME}@{project}.iam.gserviceaccount.com"


def public_object_url(bucket: str, repo: str, obj: str = "repodata.json") -> str:
    return f"{_GCS_HOST}/{bucket}/{repo}/{obj}"


def _bucket_uri(bucket: str) -> str:
    return f"gs://{bucket}"


def _pap_flag(*, public: bool) -> str:
    return "--no-public-access-prevention" if public else "--public-access-prevention"


def describe_bucket_argv(bucket: str) -> list[str]:
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
    return [
        "gcloud",
        "storage",
        "buckets",
        "create",
        _bucket_uri(bucket),
        f"--project={project}",
        f"--location={location}",
        "--uniform-bucket-level-access",
        _pap_flag(public=public),
    ]


def configure_bucket_argv(bucket: str, *, public: bool) -> list[str]:
    return [
        "gcloud",
        "storage",
        "buckets",
        "update",
        _bucket_uri(bucket),
        "--uniform-bucket-level-access",
        _pap_flag(public=public),
    ]


def add_iam_binding_argv(bucket: str, member: str) -> list[str]:
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
    return [
        "gcloud",
        "storage",
        "buckets",
        "get-iam-policy",
        _bucket_uri(bucket),
        "--format=json",
    ]


def describe_sa_argv(project: str, email: str) -> list[str]:
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
    return [
        "gcloud",
        "storage",
        "objects",
        "describe",
        f"gs://{bucket}/{repo}/{obj}",
        f"--impersonate-service-account={sa_email}",
        "--format=json",
    ]


def _load_json(text: str, what: str) -> object:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProvisionError(f"store: unreadable {what} JSON: {exc}") from exc


def ubla_enabled(describe_json: str) -> bool:
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
    """Fails closed: a structurally-malformed policy raises rather than returning False."""
    data = _load_json(iam_policy_json, "iam policy")
    if not isinstance(data, dict):
        raise ProvisionError(
            f"store: malformed iam policy JSON: expected an object, "
            f"got {type(data).__name__}"
        )
    bindings = data.get("bindings", [])
    if not isinstance(bindings, list):
        raise ProvisionError(
            "store: malformed iam policy JSON: 'bindings' is not a list"
        )
    for binding in bindings:
        if not isinstance(binding, dict):
            raise ProvisionError(
                "store: malformed iam policy JSON: a binding is not an object"
            )
        members = binding.get("members", [])
        if not isinstance(members, list):
            raise ProvisionError(
                "store: malformed iam policy JSON: 'members' is not a list"
            )
        # Compared by == — never set()/hash, so an unhashable member can't raise.
        if any(m == ALL_USERS or m == "allAuthenticatedUsers" for m in members):
            return True
    return False


@dataclass(frozen=True)
class Action:
    resource: str
    action: str  # ACTION_CREATED | ACTION_NOOP

    def to_dict(self) -> dict[str, str]:
        return {"resource": self.resource, "action": self.action}


@dataclass(frozen=True)
class ProvisionReport:
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
    """``notes`` carries any criterion that could not be checked."""

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


#: The stderr shapes gcloud uses to say a resource is genuinely absent. Word
#: markers on purpose: a bare ``404`` would collide with a resource NAME.
_NOT_FOUND_MARKERS = ("not found", "notfound", "not_found", "does not exist")


def _looks_not_found(result: execrun.ExecResult, argv: list[str]) -> bool:
    """Strips the argv tokens first, so a marker in a resource NAME is not an absence."""
    haystack = f"{result.stderr}\n{result.stdout}".lower()
    # Empty tokens are skipped: ``str.replace("", …)`` injects a space per character.
    tokens: set[str] = set()
    for arg in argv:
        if not arg:
            continue
        tokens.add(arg.lower())
        if arg.startswith("--") and "=" in arg:
            value = arg.split("=", 1)[1]
            if value:
                tokens.add(value.lower())
    # Longest-first, so a short generic token can't mangle a longer resource one.
    for token in sorted(tokens, key=len, reverse=True):
        haystack = haystack.replace(token, " ")
    return any(marker in haystack for marker in _NOT_FOUND_MARKERS)


def _exists(argv: list[str], runner: Callable[..., execrun.ExecResult]) -> bool:
    """True on rc 0, False on gcloud's not-found shape; any other nonzero raises."""
    result = runner(argv, check=False)
    if result.rc == 0:
        return True
    if _looks_not_found(result, argv):
        return False
    detail = (result.stderr or result.stdout).strip() or f"rc {result.rc}"
    raise ProvisionError(
        f"store provision: describe probe {' '.join(argv[:5])} failed "
        f"(not a not-found result): {detail}"
    )


def provision(
    project: str,
    location: str = "US",
    *,
    runner: Callable[..., execrun.ExecResult] = execrun.run,
) -> ProvisionReport:
    """Provision both tier buckets + the reader SA + IAM, idempotently.

    Needs the operator's own gcloud credentials with project-admin rights.
    """
    if not project:
        raise ProvisionError("store provision: a --project is required")
    public = bucket_name(TIER_PUBLIC)
    private = bucket_name(TIER_PRIVATE)
    sa_email = reader_sa_email(project)
    actions: list[Action] = []

    # The reader SA must exist before its bucket binding.
    if _exists(describe_sa_argv(project, sa_email), runner):
        actions.append(Action(sa_email, ACTION_NOOP))
    else:
        runner(create_sa_argv(project, READER_SA_NAME))
        actions.append(Action(sa_email, ACTION_CREATED))

    for name, is_public in ((public, True), (private, False)):
        if _exists(describe_bucket_argv(name), runner):
            actions.append(Action(name, ACTION_NOOP))
        else:
            runner(create_bucket_argv(project, name, location, public=is_public))
            actions.append(Action(name, ACTION_CREATED))
        runner(configure_bucket_argv(name, public=is_public))

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


def _http_status(url: str) -> int:
    """Authless HTTPS GET → status code; a network-layer failure raises instead."""
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310 — https literal
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ProvisionError(f"store verify: HTTPS GET of {url} failed: {exc}") from exc


def verify(
    project: str,
    repo: str,
    *,
    obj: str = "repodata.json",
    noarch: bool = False,
    subdirs: Sequence[str] | None = None,
    runner: Callable[..., execrun.ExecResult] = execrun.run,
    http_get: Callable[[str], int] = _http_status,
) -> VerifyReport:
    """Run the live acceptance checks against the provisioned store.

    Repodata is published per-subdir, so object checks fan out over ``subdirs``
    (``None`` = the full served set); ``noarch`` probes the one ``noarch/`` subdir
    instead. An empty ``subdirs`` is refused — the conjunctions would pass vacuously.
    """
    if not project:
        raise ProvisionError("store verify: a --project is required")
    if not repo:
        raise ProvisionError("store verify: a --repo is required")
    public = bucket_name(TIER_PUBLIC)
    private = bucket_name(TIER_PRIVATE)
    sa_email = reader_sa_email(project)
    report = VerifyReport()

    if noarch:
        probe_subdirs: Sequence[str] = (buckets.NOARCH_SUBDIR,)
    elif subdirs is not None:
        probe_subdirs = tuple(subdirs)
        if not probe_subdirs:
            raise ProvisionError(
                "store verify: subdirs= is an empty sequence — a scoped probe "
                "needs at least one served subdir; pass subdirs=None for the "
                "full served set"
            )
    else:
        probe_subdirs = buckets.SERVED_SUBDIRS
    subdir_objs = [f"{subdir}/{obj}" for subdir in probe_subdirs]
    report.public_get_200 = all(
        http_get(public_object_url(public, repo, o)) == 200 for o in subdir_objs
    )
    report.private_get_403 = all(
        http_get(public_object_url(private, repo, o)) == 403 for o in subdir_objs
    )

    # The SA's binding is bucket-wide, so one representative subdir proves the read.
    probe_obj = subdir_objs[0]
    scoped_argv = object_read_as_sa_argv(private, repo, sa_email, probe_obj)
    scoped = runner(scoped_argv, check=False)
    report.private_scoped_read_ok = scoped.rc == 0
    if scoped.rc != 0:
        if _looks_not_found(scoped, scoped_argv):
            report.notes.append(
                f"private scoped read: {repo}/{probe_obj} not found — publish it "
                "under the private bucket to assert the scoped-read positive"
            )
        else:
            detail = (scoped.stderr or scoped.stdout).strip() or f"rc {scoped.rc}"
            report.notes.append(
                f"private scoped read failed (not a not-found result): {detail}"
            )

    report.public_ubla_on = ubla_enabled(runner(describe_bucket_argv(public)).stdout)
    report.private_ubla_on = ubla_enabled(runner(describe_bucket_argv(private)).stdout)
    report.private_no_public_binding = not has_public_binding(
        runner(get_iam_policy_argv(private)).stdout
    )
    return report


def _emit(payload: object, *, as_json: bool, human: str) -> None:
    print(json.dumps(payload, indent=2) if as_json else human)


def _repo_served_subdirs(manifest: str) -> tuple[str, ...] | None:
    """The served conda subdirs the repo at ``manifest`` publishes, or ``None``."""
    from .. import config
    from ..release import publish

    try:
        cfg = config.load(manifest)
        artifacts = config.load_artifacts(cfg)
    except config.ConfigError:
        return None
    served = publish.conda_served_subdirs(artifacts)
    return served or None


def main(argv: list[str] | None = None) -> int:
    """``verify`` exits nonzero on a failed criterion; a refusal is ``error: …`` + 1."""
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
    p_ver.add_argument(
        "--noarch",
        action="store_true",
        help="probe the single noarch/ subdir (a cross-repo DATA artifact, "
        "ADR-0076) instead of the per-platform served-subdir sweep",
    )
    p_ver.add_argument(
        "--manifest",
        default=None,
        help="OPT-IN scoping (#1076): the path to the TARGET repo's .shipit.toml, "
        "whose conda-endpoint artifacts' declared platforms scope the probed "
        "subdirs to what that channel actually publishes. Off by default — the "
        "probe covers the full served set — because --repo names an arbitrary "
        "<owner>/<repo> and silently scoping it from an ambient .shipit.toml in "
        "the current directory could probe a NARROWER set than the target "
        "publishes and pass a channel that is missing a subdir (a false-ready). "
        "Point this at the manifest that belongs to --repo to opt into scoping; "
        "a missing/conda-less manifest falls back to the full served set.",
    )
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
        # Scoping is opt-in: --repo names an arbitrary repo, so an ambient manifest
        # could narrow the probe and pass a channel that is missing a subdir.
        subdirs = (
            None
            if (args.noarch or args.manifest is None)
            else _repo_served_subdirs(args.manifest)
        )
        vreport = verify(
            args.project,
            args.repo,
            obj=args.obj,
            noarch=args.noarch,
            subdirs=subdirs,
        )
        _emit(
            vreport.to_dict(),
            as_json=args.as_json,
            human=f"store verify: {'PASS' if vreport.ok else 'FAIL'} {vreport.to_dict()}",
        )
        return 0 if vreport.ok else 1
    except (ProvisionError, execrun.ExecError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
