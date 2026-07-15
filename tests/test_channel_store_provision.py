"""Artifact channel store provisioning (ARF01-WS03): the pure decision core, the
provision boundary with the gcloud Exec seam FAKED, and the verify verdict logic.

The live checks (authless GET 200 / no-cred 403 / scoped read / UBLA / no public
binding) hit real GCS and are never run here — this drives :func:`verify` with
both the ``gcloud`` runner and the HTTP GET faked, so the harness's assertion and
argv-assembly logic can't silently rot (the funnel_verify pattern).
"""

from __future__ import annotations

import json

import pytest

from shipit import execrun
from shipit.channel import store_provision as sp

# --------------------------------------------------------------------------
# The pure decision core — derived identities
# --------------------------------------------------------------------------


def test_bucket_names_are_derived_distinct_and_tier_tagged():
    pub = sp.bucket_name("supage-prod", sp.TIER_PUBLIC)
    priv = sp.bucket_name("supage-prod", sp.TIER_PRIVATE)
    assert pub == "supage-prod-artifact-channel-public"
    assert priv == "supage-prod-artifact-channel-private"
    # Distinct from each other and self-describing (not the sccache bucket).
    assert pub != priv
    assert "sccache" not in pub and "sccache" not in priv


def test_bucket_name_refuses_unknown_tier():
    with pytest.raises(sp.ProvisionError):
        sp.bucket_name("p", "sekret")


def test_reader_sa_email_is_derived_in_project():
    assert (
        sp.reader_sa_email("supage-prod")
        == "artifact-channel-reader@supage-prod.iam.gserviceaccount.com"
    )


def test_public_object_url_is_the_authless_https_channel_url():
    url = sp.public_object_url("b", "lex-fmt/lex")
    assert url == "https://storage.googleapis.com/b/lex-fmt/lex/repodata.json"


# --------------------------------------------------------------------------
# The pure decision core — gcloud argv builders
# --------------------------------------------------------------------------


def test_create_bucket_argv_sets_ubla_and_tier_public_access_prevention():
    pub = sp.create_bucket_argv("p", "b-public", "US", public=True)
    priv = sp.create_bucket_argv("p", "b-private", "US", public=False)
    assert pub[:4] == ["gcloud", "storage", "buckets", "create"]
    assert (
        "--uniform-bucket-level-access" in pub
        and "--uniform-bucket-level-access" in priv
    )
    # Public permits the allUsers grant; private forbids any public binding.
    assert "--public-access-prevention=inherited" in pub
    assert "--public-access-prevention=enforced" in priv
    assert "--location=US" in pub and "--project=p" in pub


def test_add_iam_binding_argv_grants_object_viewer():
    argv = sp.add_iam_binding_argv("b-public", sp.ALL_USERS)
    assert argv[:5] == [
        "gcloud",
        "storage",
        "buckets",
        "add-iam-policy-binding",
        "gs://b-public",
    ]
    assert "--member=allUsers" in argv
    assert f"--role={sp.OBJECT_VIEWER_ROLE}" in argv


def test_every_gcloud_argv_builder_heads_with_gcloud():
    builders = [
        sp.describe_bucket_argv("b"),
        sp.create_bucket_argv("p", "b", "US", public=True),
        sp.configure_bucket_argv("b", public=False),
        sp.add_iam_binding_argv("b", "allUsers"),
        sp.get_iam_policy_argv("b"),
        sp.describe_sa_argv("p", "sa@p.iam.gserviceaccount.com"),
        sp.create_sa_argv("p", "reader"),
        sp.object_read_as_sa_argv("b", "r", "sa@p.iam.gserviceaccount.com"),
    ]
    for argv in builders:
        assert argv[0] == "gcloud"


# --------------------------------------------------------------------------
# The pure decision core — verdict readers
# --------------------------------------------------------------------------


def test_ubla_enabled_accepts_both_gcloud_output_shapes():
    assert sp.ubla_enabled(json.dumps({"uniform_bucket_level_access": True}))
    assert sp.ubla_enabled(
        json.dumps(
            {"iamConfiguration": {"uniformBucketLevelAccess": {"enabled": True}}}
        )
    )
    assert not sp.ubla_enabled(json.dumps({"uniform_bucket_level_access": False}))
    assert not sp.ubla_enabled(json.dumps({}))


def test_has_public_binding_detects_allusers_and_allauthenticated():
    public = json.dumps(
        {"bindings": [{"role": "roles/storage.objectViewer", "members": ["allUsers"]}]}
    )
    authed = json.dumps({"bindings": [{"members": ["allAuthenticatedUsers"]}]})
    private = json.dumps(
        {"bindings": [{"members": ["serviceAccount:reader@p.iam.gserviceaccount.com"]}]}
    )
    assert sp.has_public_binding(public)
    assert sp.has_public_binding(authed)
    assert not sp.has_public_binding(private)
    assert not sp.has_public_binding(json.dumps({}))


def test_verdict_readers_refuse_unreadable_json():
    with pytest.raises(sp.ProvisionError):
        sp.ubla_enabled("not json")


# --------------------------------------------------------------------------
# The boundary — provision, with the gcloud Exec seam faked
# --------------------------------------------------------------------------


class FakeRunner:
    """Records every argv and answers describe probes from an existence set.

    ``existing`` is the set of resource tokens (SA email / bucket name) that
    already exist: their describe probes return rc 0, everything absent returns
    rc 1 (gcloud's not-found). Non-describe commands return rc 0.
    """

    def __init__(self, existing: set[str] | None = None, stdout_for=None):
        self.existing = existing or set()
        self.stdout_for = stdout_for or (lambda argv: "")
        self.calls: list[list[str]] = []

    def __call__(self, argv, *, check=True, **kw):
        self.calls.append(list(argv))
        rc = 0
        if "describe" in argv:
            token = next(
                (a[len("gs://") :] for a in argv if a.startswith("gs://")), None
            )
            if token is None:  # SA describe — the email is a bare positional
                token = next((a for a in argv if "@" in a), None)
            rc = 0 if token in self.existing else 1
        return execrun.ExecResult(
            argv=tuple(argv),
            rc=rc,
            stdout=self.stdout_for(argv),
            stderr="",
            duration_ms=1,
        )

    def heads(self, verb: str) -> list[list[str]]:
        return [c for c in self.calls if verb in c]


def test_provision_on_empty_project_creates_everything():
    runner = FakeRunner(existing=set())
    report = sp.provision("supage-prod", "US", runner=runner)

    created = {a.resource for a in report.actions if a.action == sp.ACTION_CREATED}
    assert created == {
        "supage-prod-artifact-channel-public",
        "supage-prod-artifact-channel-private",
        "artifact-channel-reader@supage-prod.iam.gserviceaccount.com",
    }
    # SA + both buckets created; both configured; two IAM bindings added.
    assert len(runner.heads("create")) == 3
    assert len(runner.heads("add-iam-policy-binding")) == 2
    # The public binding is allUsers; the private binding is the SA, never public.
    pub_binding = next(
        c
        for c in runner.heads("add-iam-policy-binding")
        if "gs://supage-prod-artifact-channel-public" in c
    )
    priv_binding = next(
        c
        for c in runner.heads("add-iam-policy-binding")
        if "gs://supage-prod-artifact-channel-private" in c
    )
    assert "--member=allUsers" in pub_binding
    assert any(a.startswith("--member=serviceAccount:") for a in priv_binding)
    assert "--member=allUsers" not in priv_binding


def test_provision_is_idempotent_when_everything_exists():
    existing = {
        "supage-prod-artifact-channel-public",
        "supage-prod-artifact-channel-private",
        "artifact-channel-reader@supage-prod.iam.gserviceaccount.com",
    }
    runner = FakeRunner(existing=existing)
    report = sp.provision("supage-prod", runner=runner)

    # Nothing is created on a fully-provisioned project.
    assert all(a.action == sp.ACTION_NOOP for a in report.actions)
    assert runner.heads("create") == []
    # UBLA/PAP re-asserted (idempotent) and IAM bindings re-added (idempotent).
    assert len(runner.heads("update")) == 2
    assert len(runner.heads("add-iam-policy-binding")) == 2


def test_provision_refuses_empty_project():
    with pytest.raises(sp.ProvisionError):
        sp.provision("", runner=FakeRunner())


def test_private_bucket_create_enforces_public_access_prevention():
    runner = FakeRunner(existing=set())
    sp.provision("p", runner=runner)
    priv_create = next(
        c for c in runner.heads("create") if "gs://p-artifact-channel-private" in c
    )
    assert "--public-access-prevention=enforced" in priv_create


# --------------------------------------------------------------------------
# The boundary — verify verdict logic, gcloud + HTTP faked
# --------------------------------------------------------------------------


def _verify_runner(*, scoped_ok=True, ubla=True, public_binding_on_private=False):
    def stdout_for(argv):
        if "get-iam-policy" in argv:
            members = (
                ["allUsers"]
                if public_binding_on_private
                else ["serviceAccount:r@p.iam.gserviceaccount.com"]
            )
            return json.dumps({"bindings": [{"members": members}]})
        if "describe" in argv and "buckets" in argv:
            return json.dumps({"uniform_bucket_level_access": ubla})
        return ""

    def runner(argv, *, check=True, **kw):
        rc = 0
        if "objects" in argv and "describe" in argv:
            rc = 0 if scoped_ok else 1
        return execrun.ExecResult(
            argv=tuple(argv), rc=rc, stdout=stdout_for(argv), stderr="", duration_ms=1
        )

    return runner


def test_verify_all_green():
    http = {
        sp.public_object_url("supage-prod-artifact-channel-public", "r"): 200,
        sp.public_object_url("supage-prod-artifact-channel-private", "r"): 403,
    }
    report = sp.verify(
        "supage-prod",
        "r",
        runner=_verify_runner(),
        http_get=lambda url: http[url],
    )
    assert report.ok
    assert report.public_get_200 and report.private_get_403
    assert report.private_scoped_read_ok
    assert report.public_ubla_on and report.private_ubla_on
    assert report.private_no_public_binding
    assert report.notes == []


def test_verify_flags_a_public_binding_on_the_private_bucket():
    http = {
        sp.public_object_url("p-artifact-channel-public", "r"): 200,
        sp.public_object_url("p-artifact-channel-private", "r"): 200,  # leaked!
    }
    report = sp.verify(
        "p",
        "r",
        runner=_verify_runner(public_binding_on_private=True),
        http_get=lambda url: http[url],
    )
    assert not report.ok
    assert not report.private_no_public_binding
    assert not report.private_get_403


def test_verify_notes_a_missing_private_object_instead_of_silently_passing():
    http = {
        sp.public_object_url("p-artifact-channel-public", "r"): 200,
        sp.public_object_url("p-artifact-channel-private", "r"): 403,
    }
    report = sp.verify(
        "p",
        "r",
        runner=_verify_runner(scoped_ok=False),
        http_get=lambda url: http[url],
    )
    assert not report.ok
    assert not report.private_scoped_read_ok
    assert any("private scoped read failed" in n for n in report.notes)


# --------------------------------------------------------------------------
# The entrypoint refuses without a project (opt-in operator harness)
# --------------------------------------------------------------------------


def test_main_requires_project_and_subcommand():
    with pytest.raises(SystemExit):
        sp.main([])
    with pytest.raises(SystemExit):
        sp.main(["--project", "p"])  # no subcommand
