from __future__ import annotations

import json
import urllib.error

import pytest

from shipit import execrun
from shipit.channel import store_provision as sp


def test_bucket_names_are_the_fixed_shared_constants_and_project_independent():
    from shipit.channel import buckets

    pub = sp.bucket_name(sp.TIER_PUBLIC)
    priv = sp.bucket_name(sp.TIER_PRIVATE)
    assert pub == buckets.PUBLIC_ARTIFACT_BUCKET == "shipit-artifacts-public"
    assert priv == buckets.PRIVATE_ARTIFACT_BUCKET == "shipit-artifacts-private"
    assert pub != priv
    assert "sccache" not in pub and "sccache" not in priv


def test_bucket_name_refuses_unknown_tier():
    with pytest.raises(sp.ProvisionError):
        sp.bucket_name("sekret")


def test_served_subdirs_match_the_producers_published_subdir_set():
    from shipit.channel import buckets
    from shipit.release import publish

    assert set(buckets.SERVED_SUBDIRS) == set(publish.CONDA_SUBDIRS.values())
    assert buckets.SERVED_SUBDIRS == (
        "osx-arm64",
        "linux-64",
        "linux-aarch64",
        "win-64",
    )
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


def test_create_bucket_argv_sets_ubla_and_tier_public_access_prevention():
    pub = sp.create_bucket_argv("p", "b-public", "US", public=True)
    priv = sp.create_bucket_argv("p", "b-private", "US", public=False)
    assert pub[:4] == ["gcloud", "storage", "buckets", "create"]
    assert (
        "--uniform-bucket-level-access" in pub
        and "--uniform-bucket-level-access" in priv
    )
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
    for shape in (
        {"bindings": "nope"},
        {"bindings": ["nonsense"]},
        {"bindings": [{"members": None}]},
        {"bindings": [{"members": "allUsers"}]},
        ["not", "an", "object"],
    ):
        with pytest.raises(sp.ProvisionError, match="malformed iam policy"):
            sp.has_public_binding(json.dumps(shape))


def test_has_public_binding_does_not_crash_on_unhashable_member_elements():
    policy = json.dumps({"bindings": [{"members": [{"weird": 1}, ["also-weird"]]}]})
    assert not sp.has_public_binding(policy)
    mixed = json.dumps({"bindings": [{"members": [{"weird": 1}, "allUsers"]}]})
    assert sp.has_public_binding(mixed)


def test_verdict_readers_refuse_unreadable_json():
    with pytest.raises(sp.ProvisionError):
        sp.ubla_enabled("not json")


class FakeRunner:
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
            if token is None:
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
    runner = FakeRunner(existing=set())
    report = sp.provision("supage-prod", "US", runner=runner)

    created = {a.resource for a in report.actions if a.action == sp.ACTION_CREATED}
    assert created == {
        "shipit-artifacts-public",
        "shipit-artifacts-private",
        "artifact-channel-reader@supage-prod.iam.gserviceaccount.com",
    }
    assert len(runner.heads("create")) == 3
    assert len(runner.heads("add-iam-policy-binding")) == 2
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

    assert all(a.action == sp.ACTION_NOOP for a in report.actions)
    assert runner.heads("create") == []
    assert len(runner.heads("update")) == 2
    assert len(runner.heads("add-iam-policy-binding")) == 2


def test_provision_refuses_empty_project():
    with pytest.raises(sp.ProvisionError):
        sp.provision("", runner=FakeRunner())


def test_provision_stops_on_a_non_not_found_probe_and_creates_nothing():

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
    assert not any(
        v in c for c in calls for v in ("create", "update", "add-iam-policy-binding")
    )


def test_provision_does_not_fake_not_found_from_a_marker_in_the_resource_name():

    def runner(argv, *, check=True, **kw):
        rc = 0
        stderr = ""
        if "describe" in argv:
            rc = 1
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
    obj = f"{sp.buckets.NOARCH_SUBDIR}/repodata.json"
    return {
        sp.public_object_url("shipit-artifacts-public", "r", obj): public_status,
        sp.public_object_url("shipit-artifacts-private", "r", obj): private_status,
    }


def test_verify_noarch_probes_only_the_single_noarch_subdir():
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
    win = sp.public_object_url(
        "shipit-artifacts-public", "r", f"{sp.buckets.SERVED_SUBDIRS[-1]}/repodata.json"
    )
    assert win not in http


def test_verify_noarch_red_when_the_noarch_package_is_absent():
    http = _noarch_http(404, 403)
    report = sp.verify(
        "p", "r", noarch=True, runner=_verify_runner(), http_get=lambda url: http[url]
    )
    assert not report.public_get_200
    assert not report.ok


def test_verify_public_get_fails_when_one_served_subdir_is_missing():
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
    m = {}
    for subdir in subdirs:
        obj = f"{subdir}/repodata.json"
        m[sp.public_object_url("shipit-artifacts-public", "r", obj)] = public_status
        m[sp.public_object_url("shipit-artifacts-private", "r", obj)] = private_status
    return m


def test_verify_scoped_to_the_repos_own_subdirs_is_ready_without_win64():
    own = ("osx-arm64", "linux-64", "linux-aarch64")
    http = _subdir_http_for(own, 200, 403)
    report = sp.verify(
        "supage-prod",
        "r",
        subdirs=own,
        runner=_verify_runner(),
        http_get=lambda url: http[url],
    )
    assert report.ok
    assert report.public_get_200 and report.private_get_403
    win = sp.public_object_url("shipit-artifacts-public", "r", "win-64/repodata.json")
    assert win not in http


def test_verify_unscoped_still_false_negs_a_windowsless_repo():
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
    (tmp_path / ".shipit.toml").write_text(
        '[artifacts.cli]\nbuild = ["rust"]\nendpoints = ["gh-release"]\n'
    )
    assert sp._repo_served_subdirs(str(tmp_path / ".shipit.toml")) is None
    assert sp._repo_served_subdirs(str(tmp_path / "absent.toml")) is None


def test_verify_flags_a_public_binding_on_the_private_bucket():
    http = _subdir_http(200, 200)
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
        runner=_verify_runner(scoped_ok=False),
        http_get=lambda url: http[url],
    )
    assert not report.ok
    assert not report.private_scoped_read_ok
    assert any("not found — publish it" in n for n in report.notes)


def test_verify_surfaces_the_actual_error_on_a_non_not_found_scoped_read():
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
    assert any("PERMISSION_DENIED" in n for n in report.notes)
    assert not any("publish it" in n for n in report.notes)


def test_verify_scoped_not_found_marker_is_not_faked_from_a_bare_flag_value():
    sa_email = sp.reader_sa_email("pnotfound")
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
    with pytest.raises(sp.ProvisionError, match="empty sequence"):
        sp.verify(
            "p", "r", subdirs=(), runner=_verify_runner(), http_get=lambda url: 200
        )


def test_verify_turns_a_network_failure_into_a_clean_refusal(monkeypatch):
    def raise_urlerror(url, timeout=None):
        raise urllib.error.URLError("name resolution failed")

    monkeypatch.setattr(sp.urllib.request, "urlopen", raise_urlerror)
    with pytest.raises(sp.ProvisionError, match="HTTPS GET"):
        sp.verify("p", "r", runner=_verify_runner())


def test_http_status_default_seam_raises_provision_error_on_network_failure(
    monkeypatch,
):
    def raise_urlerror(url, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(sp.urllib.request, "urlopen", raise_urlerror)
    with pytest.raises(sp.ProvisionError, match="HTTPS GET"):
        sp._http_status("https://storage.googleapis.com/b/r/repodata.json")


def test_main_requires_project_and_subcommand():
    with pytest.raises(SystemExit):
        sp.main([])
    with pytest.raises(SystemExit):
        sp.main(["--project", "p"])


def test_verify_cli_scopes_only_with_an_explicit_manifest(tmp_path, monkeypatch):
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
    (tmp_path / ".shipit.toml").write_text(
        '[artifacts.lexd]\nbuild = ["rust"]\n'
        'platforms = ["linux-x86_64"]\nendpoints = ["gh-release", "conda"]\n'
    )
    monkeypatch.chdir(tmp_path)

    rc = sp.main(["--project", "p", "verify", "--repo", "other/repo"])
    assert rc == 0
    assert seen["subdirs"] is None

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
