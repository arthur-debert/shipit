"""Artifact channel store provisioning (ARF01-WS03): the pure decision core, the
provision boundary with the gcloud Exec seam FAKED, and the verify verdict logic.

The live checks (authless GET 200 / no-cred 403 / scoped read / UBLA / no public
binding) hit real GCS and are never run here — this drives :func:`verify` with
both the ``gcloud`` runner and the HTTP GET faked, so the harness's assertion and
argv-assembly logic can't silently rot (the funnel_verify pattern).
"""

from __future__ import annotations

import json
import urllib.error

import pytest

from shipit import execrun
from shipit.channel import store_provision as sp

# --------------------------------------------------------------------------
# The pure decision core — derived identities
# --------------------------------------------------------------------------


def test_bucket_names_are_the_fixed_shared_constants_and_project_independent():
    # ARF01-WS08: the names are the ONE source of truth (shipit.channel.buckets)
    # the producer writes to and the consumer reads from — fixed, not derived
    # from --project (the provisioner would otherwise make a bucket the other two
    # sides never touch).
    from shipit.channel import buckets

    pub = sp.bucket_name(sp.TIER_PUBLIC)
    priv = sp.bucket_name(sp.TIER_PRIVATE)
    assert pub == buckets.PUBLIC_ARTIFACT_BUCKET == "shipit-artifacts-public"
    assert priv == buckets.PRIVATE_ARTIFACT_BUCKET == "shipit-artifacts-private"
    # Distinct from each other and self-describing (not the sccache bucket).
    assert pub != priv
    assert "sccache" not in pub and "sccache" not in priv


def test_bucket_name_refuses_unknown_tier():
    with pytest.raises(sp.ProvisionError):
        sp.bucket_name("sekret")


def test_served_subdirs_match_the_producers_published_subdir_set():
    # SERVED_SUBDIRS (what `verify` probes) is the SAME closed set the producer
    # publishes to (release.publish.CONDA_SUBDIRS maps each release triple onto
    # one of these). A drift — the producer serving a subdir verify never probes,
    # or vice versa — would make verify pass on a channel a consumer cannot fully
    # resolve, so pin the two together.
    from shipit.channel import buckets
    from shipit.release import publish

    assert set(buckets.SERVED_SUBDIRS) == set(publish.CONDA_SUBDIRS.values())
    assert buckets.SERVED_SUBDIRS == (
        "osx-arm64",
        "linux-64",
        "linux-aarch64",
        "win-64",
    )
    # noarch (ADR-0076) is a DISTINCT always-present subdir a DATA artifact rides
    # — deliberately NOT a member of the per-platform SERVED_SUBDIRS set (the
    # producer maps triples only onto platforms, so folding noarch in would break
    # the drift invariant above), and re-exported by the producer so all three
    # sides share one name.
    assert buckets.NOARCH_SUBDIR == "noarch"
    assert buckets.NOARCH_SUBDIR not in buckets.SERVED_SUBDIRS
    assert publish.NOARCH_SUBDIR == buckets.NOARCH_SUBDIR


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
    # PAP is a BOOLEAN flag, never a =value: public permits the allUsers grant
    # (--no-public-access-prevention), private forbids any public binding
    # (--public-access-prevention).
    assert "--no-public-access-prevention" in pub
    assert "--public-access-prevention" in priv
    assert not any(a.startswith("--public-access-prevention=") for a in pub + priv)
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


def test_has_public_binding_refuses_structurally_malformed_policy():
    # verify() reads `private_no_public_binding = not has_public_binding(...)`, so
    # a structurally-malformed policy must be a REFUSAL (ProvisionError), never a
    # quiet False — a quiet False would report the private bucket safe on an
    # unreadable policy (a false PASS, the opposite of the acceptance property).
    for shape in (
        {"bindings": "nope"},  # bindings not a list
        {"bindings": ["nonsense"]},  # a binding not an object
        {"bindings": [{"members": None}]},  # members not a list
        {"bindings": [{"members": "allUsers"}]},  # members a scalar, not a list
        ["not", "an", "object"],  # top-level not an object
    ):
        with pytest.raises(sp.ProvisionError, match="malformed iam policy"):
            sp.has_public_binding(json.dumps(shape))


def test_has_public_binding_does_not_crash_on_unhashable_member_elements():
    # A member element that is unhashable (a dict / list — malformed, but the
    # `members` container is still a list) must NOT raise TypeError: it is simply
    # not the allUsers/allAuthenticatedUsers literal, so it doesn't match.
    policy = json.dumps({"bindings": [{"members": [{"weird": 1}, ["also-weird"]]}]})
    assert not sp.has_public_binding(policy)
    # A real public member alongside a malformed one is still detected.
    mixed = json.dumps({"bindings": [{"members": [{"weird": 1}, "allUsers"]}]})
    assert sp.has_public_binding(mixed)


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
    rc 1 with gcloud's ``not found: 404`` stderr (the ONLY nonzero shape
    ``_exists`` reads as absent). Non-describe commands return rc 0.
    """

    def __init__(self, existing: set[str] | None = None, stdout_for=None):
        self.existing = existing or set()
        self.stdout_for = stdout_for or (lambda argv: "")
        self.calls: list[list[str]] = []

    def __call__(self, argv, *, check=True, **kw):
        self.calls.append(list(argv))
        rc = 0
        stderr = ""
        if "describe" in argv:
            token = next(
                (a[len("gs://") :] for a in argv if a.startswith("gs://")), None
            )
            if token is None:  # SA describe — the email is a bare positional
                token = next((a for a in argv if "@" in a), None)
            if token not in self.existing:
                rc = 1
                stderr = f"ERROR: gs://{token} not found: 404."
        return execrun.ExecResult(
            argv=tuple(argv),
            rc=rc,
            stdout=self.stdout_for(argv),
            stderr=stderr,
            duration_ms=1,
        )

    def heads(self, verb: str) -> list[list[str]]:
        return [c for c in self.calls if verb in c]


def test_provision_creates_everything_when_nothing_exists_yet():
    # "Nothing exists yet" = empty project STATE (no buckets/SA), NOT an empty
    # project id — the empty-id input guard is test_provision_refuses_empty_project.
    runner = FakeRunner(existing=set())
    report = sp.provision("supage-prod", "US", runner=runner)

    created = {a.resource for a in report.actions if a.action == sp.ACTION_CREATED}
    assert created == {
        "shipit-artifacts-public",
        "shipit-artifacts-private",
        "artifact-channel-reader@supage-prod.iam.gserviceaccount.com",
    }
    # SA + both buckets created; both configured; two IAM bindings added.
    assert len(runner.heads("create")) == 3
    assert len(runner.heads("add-iam-policy-binding")) == 2
    # The public binding is allUsers; the private binding is the SA, never public.
    pub_binding = next(
        c
        for c in runner.heads("add-iam-policy-binding")
        if "gs://shipit-artifacts-public" in c
    )
    priv_binding = next(
        c
        for c in runner.heads("add-iam-policy-binding")
        if "gs://shipit-artifacts-private" in c
    )
    assert "--member=allUsers" in pub_binding
    assert any(a.startswith("--member=serviceAccount:") for a in priv_binding)
    assert "--member=allUsers" not in priv_binding


def test_provision_is_idempotent_when_everything_exists():
    existing = {
        "shipit-artifacts-public",
        "shipit-artifacts-private",
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


def test_provision_stops_on_a_non_not_found_probe_and_creates_nothing():
    """A describe that fails for ANY reason other than not-found (permission
    denied, disabled API, wrong project) must STOP the run, not drive create."""

    def runner(argv, *, check=True, **kw):
        rc = 0
        stderr = ""
        if "describe" in argv:
            rc = 1
            stderr = "ERROR: (gcloud) PERMISSION_DENIED: caller lacks permission"
        return execrun.ExecResult(
            argv=tuple(argv), rc=rc, stdout="", stderr=stderr, duration_ms=1
        )

    calls: list[list[str]] = []

    def recording(argv, *, check=True, **kw):
        calls.append(list(argv))
        return runner(argv, check=check, **kw)

    with pytest.raises(sp.ProvisionError, match="PERMISSION_DENIED"):
        sp.provision("p", runner=recording)
    # No create / update / binding call was made after the failed probe.
    assert not any(
        v in c for c in calls for v in ("create", "update", "add-iam-policy-binding")
    )


def test_provision_does_not_fake_not_found_from_a_marker_in_the_resource_name():
    """A project name that literally contains a not-found WORD marker (here
    ``notfound``) echoes that marker into the error URI. Only the argv-stripping
    in ``_looks_not_found`` keeps a PERMISSION_DENIED probe from reading it as an
    absence — so this test would FAIL if that stripping were broken/removed (a
    real marker, not the retired bare ``404``, is what makes it non-trivial)."""

    def runner(argv, *, check=True, **kw):
        rc = 0
        stderr = ""
        if "describe" in argv:
            rc = 1
            # gcloud echoes the resource URI (which contains "notfound") into the error.
            uri = next((a for a in argv if a.startswith("gs://") or "@" in a), "res")
            stderr = f"ERROR: PERMISSION_DENIED on {uri}"
        return execrun.ExecResult(
            argv=tuple(argv), rc=rc, stdout="", stderr=stderr, duration_ms=1
        )

    calls: list[list[str]] = []

    def recording(argv, *, check=True, **kw):
        calls.append(list(argv))
        return runner(argv, check=check, **kw)

    with pytest.raises(sp.ProvisionError, match="PERMISSION_DENIED"):
        sp.provision("my-project-notfound", runner=recording)
    assert not any(
        v in c for c in calls for v in ("create", "update", "add-iam-policy-binding")
    )


def test_private_bucket_create_enforces_public_access_prevention():
    runner = FakeRunner(existing=set())
    sp.provision("p", runner=runner)
    priv_create = next(
        c for c in runner.heads("create") if "gs://shipit-artifacts-private" in c
    )
    assert "--public-access-prevention" in priv_create
    assert "--no-public-access-prevention" not in priv_create


# --------------------------------------------------------------------------
# The boundary — verify verdict logic, gcloud + HTTP faked
# --------------------------------------------------------------------------


def _verify_runner(
    *,
    scoped_ok=True,
    ubla=True,
    public_binding_on_private=False,
    scoped_stderr="ERROR: the requested object was not found.",
):
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
        stderr = ""
        if "objects" in argv and "describe" in argv and not scoped_ok:
            rc = 1
            stderr = scoped_stderr
        return execrun.ExecResult(
            argv=tuple(argv),
            rc=rc,
            stdout=stdout_for(argv),
            stderr=stderr,
            duration_ms=1,
        )

    return runner


def _subdir_http(public_status, private_status):
    """The per-subdir repodata HTTP-status map `verify` now probes: repodata is
    per-subdir (ADR-0064), so verify fans out over every served subdir under
    each bucket (`<repo>/<subdir>/repodata.json`), not the repo root."""
    m = {}
    for subdir in sp.buckets.SERVED_SUBDIRS:
        obj = f"{subdir}/repodata.json"
        m[sp.public_object_url("shipit-artifacts-public", "r", obj)] = public_status
        m[sp.public_object_url("shipit-artifacts-private", "r", obj)] = private_status
    return m


def test_verify_all_green():
    http = _subdir_http(200, 403)
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


def _noarch_http(public_status, private_status):
    """The single-subdir HTTP-status map the noarch readiness probe (ADR-0076)
    reads: a DATA artifact rides ONE `noarch/repodata.json`, so verify probes
    exactly that one object under each bucket — no per-platform fan-out."""
    obj = f"{sp.buckets.NOARCH_SUBDIR}/repodata.json"
    return {
        sp.public_object_url("shipit-artifacts-public", "r", obj): public_status,
        sp.public_object_url("shipit-artifacts-private", "r", obj): private_status,
    }


def test_verify_noarch_probes_only_the_single_noarch_subdir():
    # ADR-0076: a served DATA artifact is present when its ONE `noarch/` package
    # resolves — a single probe, never the per-platform sweep and never subject
    # to the win-64 pause subtraction. `noarch=True` probes ONLY `noarch/`, so a
    # map carrying only that object is sufficient for a green verdict (a
    # per-platform verify against the same map would KeyError on the missing
    # platform subdirs).
    http = _noarch_http(200, 403)
    report = sp.verify(
        "supage-prod",
        "r",
        noarch=True,
        runner=_verify_runner(),
        http_get=lambda url: http[url],
    )
    assert report.ok
    assert report.public_get_200 and report.private_get_403
    # The probed object is the noarch one — a platform subdir is never touched.
    win = sp.public_object_url(
        "shipit-artifacts-public", "r", f"{sp.buckets.SERVED_SUBDIRS[-1]}/repodata.json"
    )
    assert win not in http


def test_verify_noarch_red_when_the_noarch_package_is_absent():
    # The single probe still fails closed: an absent `noarch/repodata.json` (the
    # data artifact never published) is a red gate, honestly.
    http = _noarch_http(404, 403)
    report = sp.verify(
        "p", "r", noarch=True, runner=_verify_runner(), http_get=lambda url: http[url]
    )
    assert not report.public_get_200
    assert not report.ok


def test_verify_public_get_fails_when_one_served_subdir_is_missing():
    # Per-subdir conjunction: a PARTIAL publish (one subdir's repodata absent →
    # 404) must fail public_get_200, exactly what a root-only probe would miss.
    http = _subdir_http(200, 403)
    missing = sp.public_object_url(
        "shipit-artifacts-public", "r", f"{sp.buckets.SERVED_SUBDIRS[-1]}/repodata.json"
    )
    http[missing] = 404
    report = sp.verify(
        "p", "r", runner=_verify_runner(), http_get=lambda url: http[url]
    )
    assert not report.public_get_200
    assert not report.ok


def _subdir_http_for(subdirs, public_status, private_status):
    """Per-subdir repodata HTTP-status map over an EXPLICIT subdir set (#1076) —
    so a verify scoped to the repo's own subdirs finds only those, and a probe of
    an unpublished subdir (win-64 for a windows-less repo) would KeyError rather
    than pass silently."""
    m = {}
    for subdir in subdirs:
        obj = f"{subdir}/repodata.json"
        m[sp.public_object_url("shipit-artifacts-public", "r", obj)] = public_status
        m[sp.public_object_url("shipit-artifacts-private", "r", obj)] = private_status
    return m


def test_verify_scoped_to_the_repos_own_subdirs_is_ready_without_win64():
    # #1076: a repo that ships linux + darwin but NO windows (lexd's shape)
    # publishes no `win-64/repodata.json`. Probing the fixed all-of-served set
    # reports its correctly-provisioned channel NOT ready (win-64 → 404) — a false
    # negative. Scoped to the repo's OWN three subdirs, the channel verifies READY.
    own = ("osx-arm64", "linux-64", "linux-aarch64")
    http = _subdir_http_for(own, 200, 403)
    report = sp.verify(
        "supage-prod",
        "r",
        subdirs=own,
        runner=_verify_runner(),
        # A KeyError here would mean verify probed a subdir outside the repo's own
        # set — the map deliberately carries no win-64 entry.
        http_get=lambda url: http[url],
    )
    assert report.ok
    assert report.public_get_200 and report.private_get_403
    # win-64 was never probed (the map has no such key, and the verdict is green).
    win = sp.public_object_url("shipit-artifacts-public", "r", "win-64/repodata.json")
    assert win not in http


def test_verify_unscoped_still_false_negs_a_windowsless_repo():
    # The control that motivates #1076: WITHOUT scoping (the pre-fix default),
    # verify probes ALL served subdirs, so a windows-less repo whose win-64 probe
    # 404s reports NOT ready — the exact false negative the scoping removes.
    http = _subdir_http(200, 403)
    http[
        sp.public_object_url("shipit-artifacts-public", "r", "win-64/repodata.json")
    ] = 404
    report = sp.verify(
        "p", "r", runner=_verify_runner(), http_get=lambda url: http[url]
    )
    assert not report.public_get_200
    assert not report.ok


def test_repo_served_subdirs_reads_the_manifest(tmp_path):
    # #1076 CLI wiring: `_repo_served_subdirs` projects the repo's own
    # `.shipit.toml` conda-endpoint platforms onto the served subdir set, so the
    # verify CLI scopes its probe to what the channel publishes.
    (tmp_path / ".shipit.toml").write_text(
        "[artifacts.lexd]\n"
        'build = ["rust"]\n'
        'platforms = ["linux-x86_64", "linux-arm64", "darwin-arm64"]\n'
        'endpoints = ["gh-release", "conda"]\n'
    )
    assert sp._repo_served_subdirs(str(tmp_path / ".shipit.toml")) == (
        "osx-arm64",
        "linux-64",
        "linux-aarch64",
    )


def test_repo_served_subdirs_none_without_a_conda_producer(tmp_path):
    # No conda endpoint, or no/unparseable manifest → None, so the CLI falls back
    # to the full served set (the pre-#1076 behavior for a bare invocation).
    (tmp_path / ".shipit.toml").write_text(
        '[artifacts.cli]\nbuild = ["rust"]\nendpoints = ["gh-release"]\n'
    )
    assert sp._repo_served_subdirs(str(tmp_path / ".shipit.toml")) is None
    assert sp._repo_served_subdirs(str(tmp_path / "absent.toml")) is None


def test_verify_flags_a_public_binding_on_the_private_bucket():
    http = _subdir_http(200, 200)  # private serves authless → leaked
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
    http = _subdir_http(200, 403)
    report = sp.verify(
        "p",
        "r",
        runner=_verify_runner(scoped_ok=False),  # default stderr = not-found
        http_get=lambda url: http[url],
    )
    assert not report.ok
    assert not report.private_scoped_read_ok
    # A genuine not-found gets the "publish it" hint.
    assert any("not found — publish it" in n for n in report.notes)


def test_verify_surfaces_the_actual_error_on_a_non_not_found_scoped_read():
    # A scoped read that fails for a NON-not-found reason (IAM / impersonation /
    # wrong project) must surface gcloud's real error text, NOT the misleading
    # "publish the object" hint.
    http = _subdir_http(200, 403)
    report = sp.verify(
        "p",
        "r",
        runner=_verify_runner(
            scoped_ok=False,
            scoped_stderr="ERROR: PERMISSION_DENIED: unable to impersonate reader SA",
        ),
        http_get=lambda url: http[url],
    )
    assert not report.private_scoped_read_ok
    assert any("PERMISSION_DENIED" in n for n in report.notes)
    assert not any("publish it" in n for n in report.notes)


def test_verify_scoped_not_found_marker_is_not_faked_from_the_resource_uri():
    # The object URI is echoed into the argv; a live WORD marker ("notfound")
    # living in a project NAME must not fake a not-found classification for a real
    # denial. Uses a real marker (not the retired bare 404) so the test actually
    # exercises the argv-stripping in _looks_not_found rather than passing
    # trivially. The stderr echoes the FULL object URI (the argv token stripped).
    http = _subdir_http(200, 403)
    report = sp.verify(
        "pnotfound",
        "r",
        runner=_verify_runner(
            scoped_ok=False,
            scoped_stderr=(
                "ERROR: PERMISSION_DENIED on "
                f"gs://shipit-artifacts-private/r/{sp.buckets.SERVED_SUBDIRS[0]}"
                "/repodata.json"
            ),
        ),
        http_get=lambda url: http[url],
    )
    # The URI (with "notfound") is stripped before marker-matching → NOT absent.
    assert any("PERMISSION_DENIED" in n for n in report.notes)
    assert not any("publish it" in n for n in report.notes)


def test_verify_scoped_not_found_marker_is_not_faked_from_a_bare_flag_value():
    # gcloud may echo the BARE value of a --flag=value arg (here the SA email
    # from --impersonate-service-account=…, which contains the project name and
    # thus the "notfound" marker) rather than the whole token. _looks_not_found
    # must strip that bare value too, else a real denial reads as an absence.
    # This fails if the --flag=value value-extraction is removed.
    sa_email = sp.reader_sa_email("pnotfound")  # …@pnotfound.iam.gserviceaccount.com
    http = _subdir_http(200, 403)
    report = sp.verify(
        "pnotfound",
        "r",
        runner=_verify_runner(
            scoped_ok=False,
            scoped_stderr=f"ERROR: PERMISSION_DENIED impersonating {sa_email}",
        ),
        http_get=lambda url: http[url],
    )
    assert any("PERMISSION_DENIED" in n for n in report.notes)
    assert not any("publish it" in n for n in report.notes)


def test_verify_refuses_empty_project_or_repo():
    with pytest.raises(sp.ProvisionError):
        sp.verify("", "r", runner=_verify_runner(), http_get=lambda url: 200)
    with pytest.raises(sp.ProvisionError):
        sp.verify("p", "", runner=_verify_runner(), http_get=lambda url: 200)


def test_verify_refuses_an_empty_subdirs_sequence():
    # codex minor (#1076): an explicitly EMPTY scope is a caller bug, not "probe
    # nothing" — the `all(...)` conjunctions would pass VACUOUSLY (green over zero
    # subdirs) and then crash at `subdir_objs[0]`. verify refuses it up front with
    # ProvisionError; a scoped caller with nothing to probe passes subdirs=None
    # for the full served set. (The CLI never reaches here — it maps its own empty
    # projection to None — but a direct caller can.)
    with pytest.raises(sp.ProvisionError, match="empty sequence"):
        sp.verify(
            "p", "r", subdirs=(), runner=_verify_runner(), http_get=lambda url: 200
        )


def test_verify_turns_a_network_failure_into_a_clean_refusal(monkeypatch):
    # A DNS/TLS/connectivity failure (URLError) or timeout during the authless
    # GET is NOT a status verdict — the DEFAULT HTTP seam (_http_status) raises
    # ProvisionError so verify/main render `error: …` instead of a traceback.
    def raise_urlerror(url, timeout=None):
        raise urllib.error.URLError("name resolution failed")

    monkeypatch.setattr(sp.urllib.request, "urlopen", raise_urlerror)
    with pytest.raises(sp.ProvisionError, match="HTTPS GET"):
        sp.verify("p", "r", runner=_verify_runner())  # default http_get seam


def test_http_status_default_seam_raises_provision_error_on_network_failure(
    monkeypatch,
):
    def raise_urlerror(url, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(sp.urllib.request, "urlopen", raise_urlerror)
    with pytest.raises(sp.ProvisionError, match="HTTPS GET"):
        sp._http_status("https://storage.googleapis.com/b/r/repodata.json")


# --------------------------------------------------------------------------
# The entrypoint refuses without a project (opt-in operator harness)
# --------------------------------------------------------------------------


def test_main_requires_project_and_subcommand():
    with pytest.raises(SystemExit):
        sp.main([])
    with pytest.raises(SystemExit):
        sp.main(["--project", "p"])  # no subcommand


def test_verify_cli_scopes_only_with_an_explicit_manifest(tmp_path, monkeypatch):
    # #1076 MAJOR (codex): `--repo` names an ARBITRARY <owner>/<repo>, so the CLI
    # must NOT silently scope its probe from an ambient `.shipit.toml` in the cwd
    # — a narrower ambient manifest could probe fewer subdirs than the target
    # publishes and pass a channel that is actually missing one (a false-ready,
    # the dangerous direction). Scoping is OPT-IN via an explicit --manifest;
    # absent it, the probe stays the conservative full served set (subdirs=None).
    seen: dict[str, object] = {}

    def _capture(project, repo, *, obj="repodata.json", noarch=False, subdirs=None):
        seen["subdirs"] = subdirs
        return sp.VerifyReport(
            public_get_200=True,
            private_get_403=True,
            private_scoped_read_ok=True,
            public_ubla_on=True,
            private_ubla_on=True,
            private_no_public_binding=True,
        )

    monkeypatch.setattr(sp, "verify", _capture)
    # A NARROWER ambient manifest sits in the cwd (linux-only), but the target
    # --repo is a different repo and --manifest is NOT passed.
    (tmp_path / ".shipit.toml").write_text(
        '[artifacts.lexd]\nbuild = ["rust"]\n'
        'platforms = ["linux-x86_64"]\nendpoints = ["gh-release", "conda"]\n'
    )
    monkeypatch.chdir(tmp_path)

    rc = sp.main(["--project", "p", "verify", "--repo", "other/repo"])
    assert rc == 0
    assert seen["subdirs"] is None  # unscoped — the ambient manifest was ignored

    # Opting in with an explicit --manifest DOES scope, to that manifest's subdirs.
    rc = sp.main(
        [
            "--project",
            "p",
            "verify",
            "--repo",
            "other/repo",
            "--manifest",
            str(tmp_path / ".shipit.toml"),
        ]
    )
    assert rc == 0
    assert seen["subdirs"] == ("linux-64",)


def test_main_renders_a_checked_gcloud_failure_as_error_not_traceback(
    monkeypatch, capsys
):
    # A checked gcloud call failing (org policy blocking allUsers, insufficient
    # IAM, missing binary) raises execrun.ExecError; main() must catch it, print
    # `error: …`, and return 1 — never let a traceback escape.
    def raise_execerror(project, location=..., **kw):
        raise execrun.ExecError(
            [
                "gcloud",
                "storage",
                "buckets",
                "create",
                "gs://shipit-artifacts-public",
            ],
            rc=1,
            stderr="ERROR: org policy blocks allUsers",
        )

    monkeypatch.setattr(sp, "provision", raise_execerror)
    rc = sp.main(["--project", "p", "provision"])
    assert rc == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("error:")
