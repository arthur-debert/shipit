import functools
import io
import itertools
import json
import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest
import yaml

from shipit import config, execrun, staging
from shipit.release import ReleaseError
from shipit.release import brew as brew_mod
from shipit.release import bundle as bundle_mod
from shipit.release import publish as publish_mod
from shipit.release import secretreq as secretreq_mod
from shipit.verbs import release as release_verb

MAC_ARM = "aarch64-apple-darwin"
MAC_X64 = "x86_64-apple-darwin"
LINUX = "x86_64-unknown-linux-gnu"


def _ok(argv, stdout=""):
    return execrun.ExecResult(
        argv=tuple(str(a) for a in argv), rc=0, stdout=stdout, stderr="", duration_ms=1
    )


def _fail(argv, stderr, rc=101):
    return execrun.ExecResult(
        argv=tuple(str(a) for a in argv), rc=rc, stdout="", stderr=stderr, duration_ms=1
    )


class SeamRecorder:
    def __init__(self, answers=None):
        self.calls = []
        self.answers = dict(answers or {})

    def __call__(self, argv, cwd, env=None):
        argv = [str(a) for a in argv]
        self.calls.append((tuple(argv), Path(cwd), dict(env) if env else None))
        for key, answer in self.answers.items():
            prefix = (key,) if isinstance(key, str) else tuple(key)
            if tuple(argv[: len(prefix)]) == prefix:
                if callable(answer):
                    return answer(argv)
                return _ok(argv, stdout=answer)
        return _ok(argv)

    @property
    def heads(self):
        return [argv[0] for argv, _, _ in self.calls]


class FakeGh:
    def __init__(self, *, exists=False, private=False, slug="acme/widget"):
        self.calls = []
        self.exists = exists
        self.private = private
        self._slug = slug

    def release_exists(self, tag, *, cwd=None):
        self.calls.append(("exists", tag))
        return self.exists

    def release_create(self, tag, *, notes_file, prerelease, cwd=None):
        self.calls.append(("create", tag, notes_file, prerelease))

    def release_edit(self, tag, *, notes_file, prerelease, cwd=None):
        self.calls.append(("edit", tag, notes_file, prerelease))

    def release_upload(self, tag, files, *, cwd=None):
        self.calls.append(("upload", tag, tuple(files)))

    def repo_is_private(self, slug):
        self.calls.append(("private?", slug))
        return self.private

    def current_repo(self, *, cwd=None):
        class _Repo:
            slug = self._slug

        return _Repo()

    def repository_dispatch(self, slug, *, event_type, payload, token=None):
        self.calls.append(("dispatch", slug, event_type, dict(payload), token))


class FakeGit:
    def __init__(self, *, dirty=True, root=None):
        self.calls = []
        self.dirty = dirty
        self.root = root

    def repo_root(self, *, cwd):
        return str(self.root) if self.root is not None else None

    def clone(self, url, dest, *, depth=1):
        self.calls.append(("clone", url, dest))
        Path(dest).mkdir(parents=True, exist_ok=True)

    def status_porcelain(self, *, cwd):
        self.calls.append(("status", cwd))
        return [" M Formula/x.rb"] if self.dirty else []

    def current_branch(self, *, cwd):
        return "main"

    def configure_identity(self, name, email, *, cwd):
        self.calls.append(("identity", name, email))

    def add(self, paths, *, cwd):
        self.calls.append(("add", tuple(paths)))

    def commit(self, message, paths, *, cwd):
        self.calls.append(("commit", message, tuple(paths)))

    def push(self, branch, *, cwd):
        self.calls.append(("push", branch))


def _artifacts(spec: dict) -> tuple[config.Artifact, ...]:
    return config.load_artifacts({"artifacts": spec})


def _entries(mapping: dict) -> tuple[config.ToolchainEntry, ...]:
    return config.load_toolchains({"toolchains": mapping})


def _request(
    tmp_path,
    artifact,
    *,
    entries=(),
    version="1.2.3",
    env=None,
    run_cmd=None,
    probe=None,
    ghio=None,
    gitio=None,
    repo="acme/widget",
    testpypi=False,
):
    from shipit.changelog import is_prerelease

    return publish_mod.PublishRequest(
        artifact=artifact,
        entries=tuple(entries),
        root=tmp_path,
        assets_dir=tmp_path / "dist",
        version=version,
        tag=f"v{version}",
        prerelease=is_prerelease(version),
        notes_path=tmp_path / "RELEASE_NOTES.md",
        env=env or {},
        run_cmd=run_cmd or SeamRecorder(),
        probe=probe or SeamRecorder(),
        ghio=ghio or FakeGh(),
        gitio=gitio or FakeGit(),
        repo=repo,
        testpypi=testpypi,
    )


def _stage_admits(result: str, live: bool) -> bool:
    return result == "success" if live else result in ("success", "skipped")


@pytest.mark.parametrize(
    ("build", "bundle", "sign", "build_live", "bundle_live"),
    list(
        itertools.product(
            publish_mod.STAGE_RESULTS,
            publish_mod.STAGE_RESULTS,
            publish_mod.STAGE_RESULTS,
            (True, False),
            (True, False),
        )
    ),
)
def test_gate_admits_exactly_per_stage_liveness_contract(
    build, bundle, sign, build_live, bundle_live
):
    allowed = (
        _stage_admits(build, build_live)
        and _stage_admits(bundle, bundle_live)
        and sign in ("success", "skipped")
    )
    if allowed:
        publish_mod.check_gate(
            build, bundle, sign, build_live=build_live, bundle_live=bundle_live
        )
    else:
        with pytest.raises(ReleaseError, match="publish refused"):
            publish_mod.check_gate(
                build, bundle, sign, build_live=build_live, bundle_live=bundle_live
            )


def test_gate_defaults_to_live_strict():
    with pytest.raises(ReleaseError, match="live build requires success"):
        publish_mod.check_gate("skipped", "success", "skipped")
    with pytest.raises(ReleaseError, match="live bundle requires success"):
        publish_mod.check_gate("success", "skipped", "skipped")


def test_gate_empty_matrix_shape_publishes():
    publish_mod.check_gate(
        "skipped", "skipped", "skipped", build_live=False, bundle_live=False
    )


def test_gate_refusal_names_every_blocking_input():
    with pytest.raises(ReleaseError) as err:
        publish_mod.check_gate("failure", "cancelled", "failure")
    message = str(err.value)
    assert "build=failure" in message
    assert "bundle=cancelled" in message
    assert "sign=failure" in message


def test_build_is_live_iff_the_plan_matrix_is_non_empty():
    assert publish_mod.build_is_live("[]") is False
    assert publish_mod.build_is_live(json.dumps([{"artifact": "lex"}])) is True


def test_bundle_is_live_iff_the_plan_stages_name_bundle():
    assert publish_mod.bundle_is_live('["preflight","prepare","publish"]') is False
    assert (
        publish_mod.bundle_is_live(
            '["preflight","prepare","bundle","assert-bundle","publish"]'
        )
        is True
    )


@pytest.mark.parametrize("raw", ["", "not json", '{"a":1}'])
def test_liveness_facts_refuse_malformed_plan_json_loudly(raw):
    with pytest.raises(ReleaseError, match="--matrix"):
        publish_mod.build_is_live(raw)
    with pytest.raises(ReleaseError, match="--stages"):
        publish_mod.bundle_is_live(raw)


@pytest.mark.parametrize(
    ("version", "live"),
    [
        ("1.2.3-release-rc", True),
        ("1.2.3-release-rc.2", True),
        ("1.2.3-rc.1", False),
        ("1.2.3", False),
        ("1.2.3-release-rcx", False),
    ],
)
def test_is_live_fire(version, live):
    assert publish_mod.is_live_fire(version) is live


def test_registry_mirrors_the_config_endpoint_set_and_stages():
    assert publish_mod.names() == config.ENDPOINTS
    assert [a.name for a in publish_mod.ADAPTERS if a.stage == "derived"] == [
        "brew",
        "notify-downstreams",
        "conda",
        "zed",
    ]
    assert [a.name for a in publish_mod.ADAPTERS if not a.external] == ["gh-release"]
    assert [a.name for a in publish_mod.ADAPTERS if a.stable_only] == [
        "brew",
        "notify-downstreams",
        "zed",
    ]
    assert [a.name for a in publish_mod.ADAPTERS if a.needs_repo] == [
        "brew",
        "notify-downstreams",
        "conda",
        "zed",
    ]
    assert {"vscode-marketplace", "open-vsx"} <= {a.name for a in publish_mod.ADAPTERS}
    for name in ("vscode-marketplace", "open-vsx"):
        adapter = publish_mod.adapter_for(name)
        assert adapter is not None and adapter.external and adapter.stage == "release"


def test_registry_secret_names_mirror_the_derivation_authority():
    declared = {a.name: a.secrets for a in publish_mod.ADAPTERS}
    assert declared == dict(secretreq_mod.ENDPOINT_SECRETS)


def test_plan_orders_release_endpoints_before_derived():
    artifacts = _artifacts(
        {
            "lex": {"endpoints": ["brew", "crates", "gh-release"]},
            "plugin": {"endpoints": ["npm"]},
        }
    )
    dispatched = publish_mod.plan(artifacts, prerelease=False, live_fire=False)
    order = [(d.artifact.name, d.adapter.name) for d in dispatched]
    assert order == [
        ("lex", "crates"),
        ("lex", "gh-release"),
        ("plugin", "npm"),
        ("lex", "brew"),
    ]
    assert all(d.skip is None for d in dispatched)


def test_plan_rc_guard_keeps_only_gh_release():
    artifacts = _artifacts(
        {"lex": {"endpoints": ["gh-release", "crates", "pypi", "npm", "brew"]}}
    )
    dispatched = publish_mod.plan(artifacts, prerelease=True, live_fire=True)
    verdicts = {d.adapter.name: d.skip for d in dispatched}
    assert verdicts["gh-release"] is None
    for name in ("crates", "pypi", "npm", "brew"):
        assert verdicts[name] == publish_mod.SKIP_RC_GUARD


def test_plan_prerelease_skips_only_brew():
    artifacts = _artifacts({"lex": {"endpoints": ["gh-release", "crates", "brew"]}})
    dispatched = publish_mod.plan(artifacts, prerelease=True, live_fire=False)
    verdicts = {d.adapter.name: d.skip for d in dispatched}
    assert verdicts["gh-release"] is None
    assert verdicts["crates"] is None
    assert verdicts["brew"] == publish_mod.SKIP_STABLE_ONLY


def _notify_artifacts(downstreams=("lex-fmt/vscode", "lex-fmt/nvim")):
    return _artifacts(
        {
            "parser": {
                "endpoints": ["gh-release", "notify-downstreams"],
                "downstreams": list(downstreams),
            }
        }
    )


def test_plan_notify_downstreams_is_derived_after_gh_release():
    dispatched = publish_mod.plan(
        _notify_artifacts(), prerelease=False, live_fire=False
    )
    order = [d.adapter.name for d in dispatched]
    assert order == ["gh-release", "notify-downstreams"]
    assert all(d.skip is None for d in dispatched)


def test_plan_notify_downstreams_alone_refuses_without_an_unskipped_gh_release():
    artifacts = _artifacts(
        {"parser": {"endpoints": ["notify-downstreams"], "downstreams": ["a/b"]}}
    )
    with pytest.raises(ReleaseError, match="notify-downstreams tells the downstream"):
        publish_mod.plan(artifacts, prerelease=False, live_fire=False)


def test_plan_notify_downstreams_skipped_never_trips_the_gh_release_invariant():
    artifacts = _artifacts(
        {"parser": {"endpoints": ["notify-downstreams"], "downstreams": ["a/b"]}}
    )
    dispatched = publish_mod.plan(artifacts, prerelease=True, live_fire=False)
    assert dispatched[0].skip == publish_mod.SKIP_NOTIFY_PRERELEASE


def test_plan_prerelease_skips_notify_downstreams():
    dispatched = publish_mod.plan(_notify_artifacts(), prerelease=True, live_fire=False)
    verdicts = {d.adapter.name: d.skip for d in dispatched}
    assert verdicts["gh-release"] is None
    assert verdicts["notify-downstreams"] == publish_mod.SKIP_NOTIFY_PRERELEASE


def test_plan_live_fire_skips_notify_downstreams_via_rc_guard():
    dispatched = publish_mod.plan(_notify_artifacts(), prerelease=True, live_fire=True)
    verdicts = {d.adapter.name: d.skip for d in dispatched}
    assert verdicts["gh-release"] is None
    assert verdicts["notify-downstreams"] == publish_mod.SKIP_RC_GUARD


def test_plan_unknown_endpoint_is_a_hard_error_naming_the_known_set():
    rogue = config.Artifact(name="ext", endpoints=("zed-extensions",))
    with pytest.raises(ReleaseError, match="unknown endpoint") as err:
        publish_mod.plan([rogue], prerelease=False, live_fire=False)
    assert "gh-release, crates, pypi, npm, vscode-marketplace, open-vsx, brew" in str(
        err.value
    )


def test_plan_brew_alone_refuses_without_an_unskipped_gh_release():
    artifacts = _artifacts({"lex": {"endpoints": ["brew"]}})
    with pytest.raises(ReleaseError, match="brew endpoint renders a formula"):
        publish_mod.plan(artifacts, prerelease=False, live_fire=False)


def test_plan_brew_with_gh_release_on_any_artifact_is_valid():
    artifacts = _artifacts(
        {"lex": {"endpoints": ["brew"]}, "core": {"endpoints": ["gh-release"]}}
    )
    dispatched = publish_mod.plan(artifacts, prerelease=False, live_fire=False)
    assert {(d.artifact.name, d.adapter.name) for d in dispatched} == {
        ("core", "gh-release"),
        ("lex", "brew"),
    }


def test_plan_brew_needs_gh_release_unskipped_not_merely_present(tmp_path):
    artifacts = _artifacts({"lex": {"endpoints": ["brew"]}})
    dispatched = publish_mod.plan(artifacts, prerelease=True, live_fire=False)
    assert dispatched[0].skip == publish_mod.SKIP_STABLE_ONLY


def _seed_artifacts():
    return _artifacts(
        {
            "lexd": {"endpoints": ["gh-release", "crates", "conda"]},
            "lex-wasm": {"endpoints": ["npm"]},
        }
    )


def test_plan_absent_selector_fires_the_full_plan():
    dispatched = publish_mod.plan(
        _seed_artifacts(), prerelease=True, live_fire=False, selector=None
    )
    assert all(d.skip is None for d in dispatched)


def test_plan_selector_seeds_the_channel_without_collateral():
    dispatched = publish_mod.plan(
        _seed_artifacts(),
        prerelease=True,
        live_fire=False,
        selector=["gh-release", "conda"],
    )
    verdicts = {d.adapter.name: d.skip for d in dispatched}
    assert verdicts["gh-release"] is None
    assert verdicts["conda"] is None
    assert verdicts["crates"] == publish_mod.SKIP_SELECTOR
    assert verdicts["npm"] == publish_mod.SKIP_SELECTOR
    assert publish_mod.SKIP_SELECTOR != publish_mod.SKIP_RC_GUARD


def test_plan_selector_cannot_deselect_gh_release():
    with pytest.raises(ReleaseError, match="cannot deselect `gh-release`"):
        publish_mod.plan(
            _seed_artifacts(), prerelease=True, live_fire=False, selector=["conda"]
        )


def test_plan_selector_unknown_endpoint_is_loud_and_names_the_known_set():
    with pytest.raises(ReleaseError, match="unknown endpoint") as err:
        publish_mod.plan(
            _seed_artifacts(),
            prerelease=True,
            live_fire=False,
            selector=["gh-release", "conda-forge"],
        )
    assert "`conda-forge`" in str(err.value)
    assert ", ".join(publish_mod.names()) in str(err.value)


def test_plan_selector_undeclared_endpoint_is_loud():
    with pytest.raises(ReleaseError, match="which no artifact in this repo declares"):
        publish_mod.plan(
            _seed_artifacts(),
            prerelease=True,
            live_fire=False,
            selector=["gh-release", "pypi"],
        )


def test_plan_selector_derived_endpoint_without_its_base_is_refused():
    artifacts = _artifacts({"lexd": {"endpoints": ["brew", "crates"]}})
    with pytest.raises(ReleaseError, match="a brew endpoint renders"):
        publish_mod.plan(
            artifacts, prerelease=False, live_fire=False, selector=["brew"]
        )


def test_plan_selector_intersects_with_the_rc_guard():
    dispatched = publish_mod.plan(
        _seed_artifacts(),
        prerelease=True,
        live_fire=True,
        selector=["gh-release", "conda"],
    )
    verdicts = {d.adapter.name: d.skip for d in dispatched}
    assert verdicts["conda"] == publish_mod.SKIP_RC_GUARD
    assert verdicts["gh-release"] is None


def test_plan_selector_intersects_with_the_stable_only_rule():
    artifacts = _artifacts({"lex": {"endpoints": ["gh-release", "brew", "crates"]}})
    dispatched = publish_mod.plan(
        artifacts, prerelease=True, live_fire=False, selector=["gh-release", "brew"]
    )
    verdicts = {d.adapter.name: d.skip for d in dispatched}
    assert verdicts["brew"] == publish_mod.SKIP_STABLE_ONLY
    assert verdicts["crates"] == publish_mod.SKIP_SELECTOR


def test_plan_selector_keeps_the_two_stage_ordering():
    dispatched = publish_mod.plan(
        _seed_artifacts(),
        prerelease=True,
        live_fire=False,
        selector=["gh-release", "conda"],
    )
    order = [d.adapter.name for d in dispatched]
    assert order == ["gh-release", "crates", "npm", "conda"]


def test_missing_secrets_reports_planned_unskipped_dispatches_only():
    artifacts = _artifacts(
        {"lex": {"endpoints": ["gh-release", "crates", "npm", "brew"]}}
    )
    live = publish_mod.plan(artifacts, prerelease=True, live_fire=True)
    assert publish_mod.missing_secrets(live, {}, testpypi=False) == ()
    final = publish_mod.plan(artifacts, prerelease=False, live_fire=False)
    missing = publish_mod.missing_secrets(final, {}, testpypi=False)
    assert missing == (
        ("crates", "CARGO_REGISTRY_TOKEN"),
        ("npm", "NPM_TOKEN"),
        ("brew", "HOMEBREW_TAP_TOKEN"),
    )


def test_required_env_keys_testpypi_swaps_the_pypi_token():
    assert (
        publish_mod.required_env_keys(publish_mod.PYPI, testpypi=False)
        == (secretreq_mod.ENDPOINT_SECRETS["pypi"])
    )
    assert publish_mod.required_env_keys(publish_mod.PYPI, testpypi=True) == (
        secretreq_mod.TESTPYPI_SECRET,
    )


def _staged_assets(tmp_path, names):
    dist = tmp_path / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    for name in names:
        (dist / name).write_bytes(b"bytes-of-" + name.encode())
    return dist


def _pyproject(dir_path, name):
    dir_path.mkdir(parents=True, exist_ok=True)
    (dir_path / "pyproject.toml").write_text(
        f'[project]\nname = "{name}"\nversion = "0"\n', encoding="utf-8"
    )


def test_gh_release_creates_with_prerelease_from_the_suffix_and_uploads(tmp_path):
    (tmp_path / "RELEASE_NOTES.md").write_text("notes\n", encoding="utf-8")
    _staged_assets(
        tmp_path,
        [
            f"lex-{MAC_ARM}.tar.gz",
            "lex.unsigned-app.tar.gz",
            ".DS_Store",
        ],
    )
    ghio = FakeGh(exists=False)
    artifact = _artifacts({"lex": {"endpoints": ["gh-release"]}})[0]
    req = _request(tmp_path, artifact, version="1.2.3-rc.1", ghio=ghio)

    published = publish_mod._publish_gh_release(req)

    assert ghio.calls[0] == ("exists", "v1.2.3-rc.1")
    assert ghio.calls[1] == (
        "create",
        "v1.2.3-rc.1",
        str(tmp_path / "RELEASE_NOTES.md"),
        True,
    )
    kind, tag, files = ghio.calls[2]
    assert (kind, tag) == ("upload", "v1.2.3-rc.1")
    assert files == (str(tmp_path / "dist" / f"lex-{MAC_ARM}.tar.gz"),)
    assert published.endpoint == "gh-release"


def test_gh_release_resume_edits_and_reasserts_the_prerelease_flag(tmp_path):
    (tmp_path / "RELEASE_NOTES.md").write_text("notes\n", encoding="utf-8")
    ghio = FakeGh(exists=True)
    artifact = _artifacts({"lex": {"endpoints": ["gh-release"]}})[0]
    req = _request(tmp_path, artifact, version="1.2.3", ghio=ghio)

    publish_mod._publish_gh_release(req)

    assert ("edit", "v1.2.3", str(tmp_path / "RELEASE_NOTES.md"), False) in ghio.calls
    assert not any(call[0] == "create" for call in ghio.calls)


def test_gh_release_without_the_notes_file_refuses(tmp_path):
    artifact = _artifacts({"lex": {"endpoints": ["gh-release"]}})[0]
    with pytest.raises(ReleaseError, match="no notes file"):
        publish_mod._publish_gh_release(_request(tmp_path, artifact))


def _cargo_metadata(
    deps: dict[str, list],
    dev: dict[str, list] | None = None,
    unpublished: set[str] | None = None,
) -> str:
    dev = dev or {}
    unpublished = unpublished or set()
    packages = []
    for name, needs in deps.items():
        dependencies = [{"name": d, "kind": None} for d in needs]
        dependencies += [{"name": d, "kind": "dev"} for d in dev.get(name, [])]
        packages.append(
            {
                "id": f"id-{name}",
                "name": name,
                "dependencies": dependencies,
                "publish": [] if name in unpublished else None,
            }
        )
    return json.dumps(
        {
            "packages": packages,
            "workspace_members": [f"id-{name}" for name in deps],
        }
    )


def test_crates_publish_order_is_topological_with_stable_ties():
    metadata = json.loads(
        _cargo_metadata({"app": ["core", "util"], "util": ["core"], "core": []})
    )
    assert publish_mod.crates_publish_order(metadata) == ("core", "util", "app")


def test_crates_publish_order_ignores_dev_dependency_cycles():
    metadata = json.loads(
        _cargo_metadata({"core": [], "helper": ["core"]}, dev={"core": ["helper"]})
    )
    assert publish_mod.crates_publish_order(metadata) == ("core", "helper")


def test_crates_publish_order_excludes_publish_false_members():
    metadata = json.loads(
        _cargo_metadata(
            {"app": ["core"], "core": [], "test-helper": ["core"]},
            unpublished={"test-helper"},
        )
    )
    assert publish_mod.crates_publish_order(metadata) == ("core", "app")


def test_crates_publish_order_keeps_registry_restricted_members():
    metadata = json.loads(_cargo_metadata({"core": []}))
    metadata["packages"][0]["publish"] = ["my-registry"]
    assert publish_mod.crates_publish_order(metadata) == ("core",)


def test_crates_publish_order_refuses_a_real_cycle():
    metadata = json.loads(_cargo_metadata({"a": ["b"], "b": ["a"]}))
    with pytest.raises(ReleaseError, match="dependency cycle"):
        publish_mod.crates_publish_order(metadata)


def test_crates_publishes_in_order_and_resumes_past_already_published(tmp_path):
    artifact = _artifacts(
        {
            "lex": {
                "build": [{"toolchain": "rust", "package": "app"}],
                "endpoints": ["crates"],
            }
        }
    )[0]
    entries = _entries({".": "rust"})
    run_cmd = SeamRecorder(
        {("cargo", "metadata"): _cargo_metadata({"app": ["core"], "core": []})}
    )

    def publish_answer(argv):
        if argv[-1] == "core":
            return _fail(argv, "error: crate version `1.2.3` is already uploaded")
        return _ok(argv)

    probe = SeamRecorder({("cargo", "publish"): publish_answer})
    req = _request(
        tmp_path,
        artifact,
        entries=entries,
        env={"CARGO_REGISTRY_TOKEN": "tok"},
        run_cmd=run_cmd,
        probe=probe,
    )

    published = publish_mod._publish_crates(req)

    assert [argv for argv, _, _ in probe.calls] == [
        ("cargo", "publish", "-p", "core"),
        ("cargo", "publish", "-p", "app"),
    ]
    assert [env for _, _, env in probe.calls] == [
        {"CARGO_REGISTRY_TOKEN": "tok"},
        {"CARGO_REGISTRY_TOKEN": "tok"},
    ]
    assert published.actions == (
        "core 1.2.3 already published — resumed",
        "app 1.2.3 published",
    )


def test_crates_a_real_publish_failure_aborts_with_the_stderr_tail(tmp_path):
    artifact = _artifacts({"lex": {"endpoints": ["crates"]}})[0]
    entries = _entries({".": "rust"})
    run_cmd = SeamRecorder({("cargo", "metadata"): _cargo_metadata({"core": []})})
    probe = SeamRecorder(
        {("cargo", "publish"): lambda argv: _fail(argv, "error: rate limited")}
    )
    req = _request(
        tmp_path,
        artifact,
        entries=entries,
        env={"CARGO_REGISTRY_TOKEN": "tok"},
        run_cmd=run_cmd,
        probe=probe,
    )
    with pytest.raises(ReleaseError, match="rate limited"):
        publish_mod._publish_crates(req)


def test_crates_without_a_rust_leg_refuses(tmp_path):
    artifact = _artifacts({"lex": {"endpoints": ["crates"]}})[0]
    req = _request(tmp_path, artifact, env={"CARGO_REGISTRY_TOKEN": "tok"})
    with pytest.raises(ReleaseError, match="needs a \\[toolchains\\] rust leg"):
        publish_mod._publish_crates(req)


def test_pypi_uploads_selects_the_named_distribution_wheel_and_sdist_only():
    names = [
        "pkg-1.0.0-py3-none-any.whl",
        "pkg-1.0.0.tar.gz",
        "other_pkg-2.0.0-py3-none-any.whl",
        "other_pkg-2.0.0.tar.gz",
        f"lex-{LINUX}.tar.gz",
        "pkg-9.9.9.tar.gz",
    ]
    assert publish_mod.pypi_uploads(names, "pkg") == (
        "pkg-1.0.0-py3-none-any.whl",
        "pkg-1.0.0.tar.gz",
    )


def test_pypi_uploads_matches_a_legacy_hyphenated_sdist_canonically():
    names = [
        "my_awesome_pkg-1.0.0-py3-none-any.whl",
        "my-awesome-pkg-1.0.0.tar.gz",
    ]
    assert publish_mod.pypi_uploads(names, "my-awesome-pkg") == (
        "my_awesome_pkg-1.0.0-py3-none-any.whl",
        "my-awesome-pkg-1.0.0.tar.gz",
    )


def test_pypi_scopes_the_upload_to_the_artifact_distribution(tmp_path):
    dist = _staged_assets(
        tmp_path,
        [
            "pkg-1.0.0-py3-none-any.whl",
            "pkg-1.0.0.tar.gz",
            "other_pkg-2.0.0-py3-none-any.whl",
            "other_pkg-2.0.0.tar.gz",
        ],
    )
    _pyproject(tmp_path, "pkg")
    artifact = _artifacts({"pkg": {"endpoints": ["pypi"]}})[0]
    run_cmd = SeamRecorder()
    req = _request(
        tmp_path,
        artifact,
        entries=_entries({".": "python"}),
        env={"PYPI_TOKEN": "pypi-tok"},
        run_cmd=run_cmd,
    )

    publish_mod._publish_pypi(req)

    argv, cwd, env = run_cmd.calls[0]
    assert argv == (
        "twine",
        "upload",
        "--non-interactive",
        "--skip-existing",
        str(dist / "pkg-1.0.0-py3-none-any.whl"),
        str(dist / "pkg-1.0.0.tar.gz"),
    )
    assert env == {"TWINE_USERNAME": "__token__", "TWINE_PASSWORD": "pypi-tok"}
    assert "pypi-tok" not in " ".join(argv)
    assert "other_pkg" not in " ".join(argv)


def test_pypi_testpypi_flag_reroutes_and_uses_the_staging_token(tmp_path):
    _staged_assets(tmp_path, ["pkg-1.0.0-py3-none-any.whl"])
    _pyproject(tmp_path, "pkg")
    artifact = _artifacts({"pkg": {"endpoints": ["pypi"]}})[0]
    run_cmd = SeamRecorder()
    req = _request(
        tmp_path,
        artifact,
        entries=_entries({".": "python"}),
        env={"TESTPYPI_TOKEN": "staging-tok"},
        run_cmd=run_cmd,
        testpypi=True,
    )

    publish_mod._publish_pypi(req)

    argv, _, env = run_cmd.calls[0]
    assert ("--repository-url", publish_mod.TESTPYPI_URL) == tuple(argv[4:6])
    assert env["TWINE_PASSWORD"] == "staging-tok"


def test_pypi_without_a_wheel_refuses(tmp_path):
    _staged_assets(tmp_path, [f"lex-{LINUX}.tar.gz"])
    _pyproject(tmp_path, "pkg")
    artifact = _artifacts({"pkg": {"endpoints": ["pypi"]}})[0]
    req = _request(
        tmp_path,
        artifact,
        entries=_entries({".": "python"}),
        env={"PYPI_TOKEN": "tok"},
    )
    with pytest.raises(ReleaseError, match="no wheel"):
        publish_mod._publish_pypi(req)


def test_pypi_without_a_python_leg_refuses(tmp_path):
    _staged_assets(tmp_path, ["pkg-1.0.0-py3-none-any.whl"])
    artifact = _artifacts({"pkg": {"endpoints": ["pypi"]}})[0]
    req = _request(tmp_path, artifact, env={"PYPI_TOKEN": "tok"})
    with pytest.raises(ReleaseError, match="needs a \\[toolchains\\] python leg"):
        publish_mod._publish_pypi(req)


def test_pypi_pyproject_without_a_name_refuses(tmp_path):
    _staged_assets(tmp_path, ["pkg-1.0.0-py3-none-any.whl"])
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nversion = '0'\n", encoding="utf-8"
    )
    artifact = _artifacts({"pkg": {"endpoints": ["pypi"]}})[0]
    req = _request(
        tmp_path,
        artifact,
        entries=_entries({".": "python"}),
        env={"PYPI_TOKEN": "tok"},
    )
    with pytest.raises(ReleaseError, match="no \\[project\\].name"):
        publish_mod._publish_pypi(req)


def _stage_npm_tarball(tmp_path, name):
    dist = tmp_path / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    (dist / name).write_bytes(b"tgz")
    return dist / name


def test_npm_publishes_the_staged_tarball_without_rebuilding(tmp_path):
    (artifact,) = _artifacts(
        {"wasm": {"product-name": "@lex-fmt/lex-wasm", "endpoints": ["npm"]}}
    )
    tarball = _stage_npm_tarball(tmp_path, "lex-fmt-lex-wasm-1.2.3.tgz")
    probe = SeamRecorder()
    req = _request(
        tmp_path,
        artifact,
        env={"NPM_TOKEN": "npm-tok"},
        probe=probe,
    )

    published = publish_mod._publish_npm(req)

    argv, cwd, env = probe.calls[0]
    assert argv == ("npm", "publish", str(tarball), "--ignore-scripts")
    assert cwd == tmp_path
    assert env == {"NODE_AUTH_TOKEN": "npm-tok"}
    assert published.actions == (
        "published @lex-fmt/lex-wasm 1.2.3 (lex-fmt-lex-wasm-1.2.3.tgz)",
    )


def test_npm_missing_staged_tarball_is_a_loud_refusal(tmp_path):
    (artifact,) = _artifacts(
        {"wasm": {"product-name": "@lex-fmt/lex-wasm", "endpoints": ["npm"]}}
    )
    req = _request(tmp_path, artifact, env={"NPM_TOKEN": "tok"}, probe=SeamRecorder())
    with pytest.raises(ReleaseError, match="no tarball `lex-fmt-lex-wasm-1.2.3.tgz`"):
        publish_mod._publish_npm(req)


def test_npm_publish_over_existing_is_success(tmp_path):
    (artifact,) = _artifacts(
        {"wasm": {"product-name": "@lex-fmt/lex-wasm", "endpoints": ["npm"]}}
    )
    _stage_npm_tarball(tmp_path, "lex-fmt-lex-wasm-1.2.3.tgz")
    probe = SeamRecorder(
        {
            "npm": lambda argv: _fail(
                argv,
                "npm error 403 You cannot publish over the previously "
                "published versions: 1.2.3.",
            )
        }
    )
    req = _request(
        tmp_path,
        artifact,
        env={"NPM_TOKEN": "tok"},
        probe=probe,
    )
    published = publish_mod._publish_npm(req)
    assert published.actions == ("@lex-fmt/lex-wasm 1.2.3 already published — resumed",)


def _stage_vsix(assets_dir: Path, *names: str) -> None:
    assets_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        (assets_dir / name).write_bytes(b"PK\x03\x04")
    (assets_dir / "ext-linux-x64.tar.gz").write_bytes(b"")


def test_vsix_uploads_selects_only_the_vsix_files():
    names = ["ext-darwin-arm64.vsix", "ext-win32-x64.vsix", "ext-linux-x64.tar.gz"]
    assert publish_mod.vsix_uploads(names, "ext") == (
        "ext-darwin-arm64.vsix",
        "ext-win32-x64.vsix",
    )


def test_vsix_uploads_scopes_to_the_artifact_never_a_sibling():
    names = [
        "ext-darwin-arm64.vsix",
        "other-darwin-arm64.vsix",
        "ext-9.9.9.vsix",
    ]
    assert publish_mod.vsix_uploads(names, "ext") == ("ext-darwin-arm64.vsix",)


def test_vscode_marketplace_publishes_each_staged_vsix(tmp_path):
    artifact = _artifacts({"ext": {"endpoints": ["vscode-marketplace"]}})[0]
    entries = _entries({"editors/vscode": "npm"})
    probe = SeamRecorder()
    req = _request(
        tmp_path, artifact, entries=entries, env={"VSCE_PAT": "pat-tok"}, probe=probe
    )
    _stage_vsix(req.assets_dir, "ext-darwin-arm64.vsix", "ext-win32-x64.vsix")

    published = publish_mod._publish_vscode_marketplace(req)

    argv0, cwd0, env0 = probe.calls[0]
    assert argv0 == (
        "npm",
        "exec",
        "--",
        "vsce",
        "publish",
        "--packagePath",
        str(req.assets_dir / "ext-darwin-arm64.vsix"),
    )
    assert cwd0 == tmp_path / "editors/vscode"
    assert env0 == {"VSCE_PAT": "pat-tok"}
    assert [c[0][-1] for c in probe.calls] == [
        str(req.assets_dir / "ext-darwin-arm64.vsix"),
        str(req.assets_dir / "ext-win32-x64.vsix"),
    ]
    assert published.endpoint == "vscode-marketplace"
    assert published.actions == (
        "published ext-darwin-arm64.vsix",
        "published ext-win32-x64.vsix",
    )


def test_vscode_marketplace_publish_over_existing_is_success(tmp_path):
    artifact = _artifacts({"ext": {"endpoints": ["vscode-marketplace"]}})[0]
    entries = _entries({"editors/vscode": "npm"})
    probe = SeamRecorder(
        {
            "npm": lambda argv: _fail(
                argv, "ERROR  Version 1.2.3 is already published on the Marketplace."
            )
        }
    )
    req = _request(
        tmp_path, artifact, entries=entries, env={"VSCE_PAT": "tok"}, probe=probe
    )
    _stage_vsix(req.assets_dir, "ext-darwin-arm64.vsix")
    published = publish_mod._publish_vscode_marketplace(req)
    assert published.actions == ("ext-darwin-arm64.vsix already published — resumed",)


def test_vscode_marketplace_a_real_failure_aborts_with_the_stderr_tail(tmp_path):
    artifact = _artifacts({"ext": {"endpoints": ["vscode-marketplace"]}})[0]
    entries = _entries({"editors/vscode": "npm"})
    probe = SeamRecorder(
        {"npm": lambda argv: _fail(argv, "ERROR  invalid personal access token")}
    )
    req = _request(
        tmp_path, artifact, entries=entries, env={"VSCE_PAT": "tok"}, probe=probe
    )
    _stage_vsix(req.assets_dir, "ext-darwin-arm64.vsix")
    with pytest.raises(ReleaseError, match="invalid personal access token"):
        publish_mod._publish_vscode_marketplace(req)


def test_vscode_marketplace_without_a_vsix_refuses(tmp_path):
    artifact = _artifacts({"ext": {"endpoints": ["vscode-marketplace"]}})[0]
    entries = _entries({"editors/vscode": "npm"})
    req = _request(tmp_path, artifact, entries=entries, env={"VSCE_PAT": "tok"})
    req.assets_dir.mkdir(parents=True, exist_ok=True)
    with pytest.raises(ReleaseError, match="no .vsix under"):
        publish_mod._publish_vscode_marketplace(req)


def test_vscode_marketplace_missing_token_refuses(tmp_path):
    artifact = _artifacts({"ext": {"endpoints": ["vscode-marketplace"]}})[0]
    req = _request(tmp_path, artifact, env={})
    _stage_vsix(req.assets_dir, "ext-darwin-arm64.vsix")
    with pytest.raises(ReleaseError, match="VSCE_PAT"):
        publish_mod._publish_vscode_marketplace(req)


def test_open_vsx_publishes_with_ovsx_and_its_own_token(tmp_path):
    artifact = _artifacts({"ext": {"endpoints": ["open-vsx"]}})[0]
    entries = _entries({"editors/vscode": "npm"})
    probe = SeamRecorder()
    req = _request(
        tmp_path, artifact, entries=entries, env={"OVSX_PAT": "ovsx-tok"}, probe=probe
    )
    _stage_vsix(req.assets_dir, "ext-linux-arm64.vsix")

    published = publish_mod._publish_open_vsx(req)

    argv0, cwd0, env0 = probe.calls[0]
    assert argv0 == (
        "npm",
        "exec",
        "--",
        "ovsx",
        "publish",
        str(req.assets_dir / "ext-linux-arm64.vsix"),
    )
    assert cwd0 == tmp_path / "editors/vscode"
    assert env0 == {"OVSX_PAT": "ovsx-tok"}
    assert published.endpoint == "open-vsx"
    assert published.actions == ("published ext-linux-arm64.vsix",)


def test_open_vsx_publish_over_existing_is_success(tmp_path):
    artifact = _artifacts({"ext": {"endpoints": ["open-vsx"]}})[0]
    entries = _entries({"editors/vscode": "npm"})
    probe = SeamRecorder(
        {"npm": lambda argv: _fail(argv, "ERROR  already exists in the registry.")}
    )
    req = _request(
        tmp_path, artifact, entries=entries, env={"OVSX_PAT": "tok"}, probe=probe
    )
    _stage_vsix(req.assets_dir, "ext-linux-arm64.vsix")
    published = publish_mod._publish_open_vsx(req)
    assert published.actions == ("ext-linux-arm64.vsix already published — resumed",)


def test_open_vsx_a_real_failure_aborts_with_the_stderr_tail(tmp_path):
    artifact = _artifacts({"ext": {"endpoints": ["open-vsx"]}})[0]
    entries = _entries({"editors/vscode": "npm"})
    probe = SeamRecorder(
        {"npm": lambda argv: _fail(argv, "ERROR  invalid access token")}
    )
    req = _request(
        tmp_path, artifact, entries=entries, env={"OVSX_PAT": "tok"}, probe=probe
    )
    _stage_vsix(req.assets_dir, "ext-linux-arm64.vsix")
    with pytest.raises(ReleaseError, match="invalid access token"):
        publish_mod._publish_open_vsx(req)


def test_open_vsx_without_a_vsix_refuses(tmp_path):
    artifact = _artifacts({"ext": {"endpoints": ["open-vsx"]}})[0]
    entries = _entries({"editors/vscode": "npm"})
    req = _request(tmp_path, artifact, entries=entries, env={"OVSX_PAT": "tok"})
    req.assets_dir.mkdir(parents=True, exist_ok=True)
    with pytest.raises(ReleaseError, match="no .vsix under"):
        publish_mod._publish_open_vsx(req)


def test_open_vsx_missing_token_refuses(tmp_path):
    artifact = _artifacts({"ext": {"endpoints": ["open-vsx"]}})[0]
    entries = _entries({"editors/vscode": "npm"})
    req = _request(tmp_path, artifact, entries=entries, env={})
    _stage_vsix(req.assets_dir, "ext-linux-arm64.vsix")
    with pytest.raises(ReleaseError, match="OVSX_PAT"):
        publish_mod._publish_open_vsx(req)


def test_marketplace_endpoints_refuse_without_an_npm_leg(tmp_path):
    for endpoint, token, publish in (
        ("vscode-marketplace", "VSCE_PAT", publish_mod._publish_vscode_marketplace),
        ("open-vsx", "OVSX_PAT", publish_mod._publish_open_vsx),
    ):
        artifact = _artifacts({"ext": {"endpoints": [endpoint]}})[0]
        req = _request(tmp_path, artifact, entries=(), env={token: "tok"})
        _stage_vsix(req.assets_dir, "ext-darwin-arm64.vsix")
        with pytest.raises(ReleaseError, match="needs a .* npm leg"):
            publish(req)


def test_plan_rc_guard_skips_both_marketplace_endpoints(tmp_path):
    artifacts = _artifacts(
        {"ext": {"endpoints": ["gh-release", "vscode-marketplace", "open-vsx"]}}
    )
    dispatched = publish_mod.plan(artifacts, prerelease=True, live_fire=True)
    live = {d.adapter.name for d in dispatched if d.skip is None}
    assert live == {"gh-release"}
    for name in ("vscode-marketplace", "open-vsx"):
        skip = next(d.skip for d in dispatched if d.adapter.name == name)
        assert skip == publish_mod.SKIP_RC_GUARD


_BREW_METADATA = json.dumps(
    {
        "packages": [
            {
                "id": "id-lex",
                "name": "lex",
                "dependencies": [],
                "description": "A structured text tool",
                "license": "MIT",
                "homepage": "https://lex.example",
            }
        ],
        "workspace_members": ["id-lex"],
    }
)


def _brew_setup(tmp_path, *, private=False, dirty=True):
    _staged_assets(
        tmp_path,
        [f"lex-{MAC_ARM}.tar.gz", f"lex-{LINUX}.tar.gz", "pkg-1.0.0.tar.gz"],
    )
    artifact = _artifacts({"lex": {"build": ["rust"], "endpoints": ["brew"]}})[0]
    entries = _entries({".": "rust"})
    run_cmd = SeamRecorder({("cargo", "metadata"): _BREW_METADATA})
    ghio = FakeGh(private=private)
    gitio = FakeGit(dirty=dirty)
    req = _request(
        tmp_path,
        artifact,
        entries=entries,
        env={"HOMEBREW_TAP_TOKEN": "tap-tok"},
        run_cmd=run_cmd,
        ghio=ghio,
        gitio=gitio,
    )
    return req, run_cmd, ghio, gitio


def test_brew_renders_against_final_asset_urls_and_shas_and_pushes(tmp_path):
    req, run_cmd, _, gitio = _brew_setup(tmp_path)

    published = publish_mod._publish_brew(req)

    rendered = (tmp_path / "dist" / "brew" / "lex.rb").read_text(encoding="utf-8")
    assert (
        f"https://github.com/acme/widget/releases/download/v1.2.3/lex-{MAC_ARM}.tar.gz"
        in rendered
    )
    assert "on_macos" in rendered and "on_linux" in rendered
    import hashlib

    expected_sha = hashlib.sha256(
        (tmp_path / "dist" / f"lex-{MAC_ARM}.tar.gz").read_bytes()
    ).hexdigest()
    assert expected_sha in rendered
    assert "pkg-1.0.0" not in rendered
    assert ("ruby", "-c", str(tmp_path / "dist" / "brew" / "lex.rb")) in [
        argv for argv, _, _ in run_cmd.calls
    ]
    kinds = [call[0] for call in gitio.calls]
    assert kinds == ["clone", "status", "identity", "add", "commit", "push"]
    assert (
        "x-access-token:tap-tok@github.com/arthur-debert/homebrew-tools"
        in (gitio.calls[0][1])
    )
    assert gitio.calls[4][1] == "lex 1.2.3"
    assert any("pushed Formula/lex.rb" in a for a in published.actions)


def test_brew_unchanged_formula_is_a_noop_push(tmp_path):
    req, _, _, gitio = _brew_setup(tmp_path, dirty=False)

    published = publish_mod._publish_brew(req)

    kinds = [call[0] for call in gitio.calls]
    assert kinds == ["clone", "status"]
    assert any("unchanged — nothing to push" in a for a in published.actions)


def test_brew_private_repo_inlines_the_download_strategy(tmp_path):
    req, _, _, _ = _brew_setup(tmp_path, private=True)
    publish_mod._publish_brew(req)
    rendered = (tmp_path / "dist" / "brew" / "lex.rb").read_text(encoding="utf-8")
    assert "GitHubPrivateRepositoryReleaseDownloadStrategy < CurlDownloadStrategy" in (
        rendered
    )
    assert "using: GitHubPrivateRepositoryReleaseDownloadStrategy" in rendered


def test_brew_without_archives_refuses(tmp_path):
    artifact = _artifacts({"lex": {"endpoints": ["brew"]}})[0]
    req = _request(tmp_path, artifact, env={"HOMEBREW_TAP_TOKEN": "tok"})
    with pytest.raises(ReleaseError, match="tar.gz archives"):
        publish_mod._publish_brew(req)


def test_brew_refuses_an_unresolved_source_repo(tmp_path):
    artifact = _artifacts({"lex": {"endpoints": ["brew"]}})[0]
    req = _request(tmp_path, artifact, env={"HOMEBREW_TAP_TOKEN": "tok"}, repo=None)
    with pytest.raises(ReleaseError, match="no source repo resolved"):
        publish_mod._publish_brew(req)


def test_brew_render_core():
    text = brew_mod.render(
        binary="lex-cli",
        version="1.2.3",
        desc="A tool",
        homepage="https://x",
        license_="MIT",
        targets={MAC_ARM: ("https://u/arm", "aa"), MAC_X64: ("https://u/x64", "bb")},
        private=False,
    )
    assert "class LexCli < Formula" in text
    assert "on_arm do" in text and "on_intel do" in text
    assert "on_linux" not in text
    assert 'bin.install "lex-cli"' in text


def test_brew_render_escapes_ruby_string_metadata():
    text = brew_mod.render(
        binary="lex-cli",
        version="1.2.3",
        desc='He said "hi" \\ and used #{ENV}',
        homepage="https://x",
        license_="MIT",
        targets={MAC_ARM: ("https://u/arm", "aa")},
        private=False,
    )
    assert r'desc "He said \"hi\" \\ and used \#{ENV}"' in text
    desc_line = next(
        line for line in text.splitlines() if line.strip().startswith("desc ")
    )
    assert '"hi"' not in desc_line and r"\#{ENV}" in desc_line


def test_brew_metadata_for_hard_errors_on_missing_fields():
    metadata = {
        "packages": [{"name": "lex", "description": "", "license": "MIT"}],
    }
    artifact = _artifacts({"lex": {"endpoints": ["brew"]}})[0]
    with pytest.raises(ReleaseError, match="missing description"):
        brew_mod.metadata_for(metadata, artifact)


REPO_TOML = """
[toolchains]
"." = "rust"

[artifacts.lex]
build = ["rust"]
bundle = { composition = "archive" }
endpoints = ["gh-release", "crates", "brew"]
"""


def _publish_repo(tmp_path, monkeypatch, toml=REPO_TOML, *, notes=True, assets=()):
    (tmp_path / ".shipit.toml").write_text(toml, encoding="utf-8")
    if notes:
        (tmp_path / "RELEASE_NOTES.md").write_text("notes\n", encoding="utf-8")
    if assets:
        _staged_assets(tmp_path, list(assets))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _spec(raw):
    from shipit.release.version import parse_spec

    return parse_spec(raw)


def test_publish_gate_refusal_dispatches_nothing(tmp_path, monkeypatch, capsys):
    _publish_repo(tmp_path, monkeypatch)
    recorder = SeamRecorder()
    ghio = FakeGh()
    gitio = FakeGit(root=tmp_path)

    rc = release_verb.run_publish(
        _spec("1.2.3"),
        build_result="success",
        bundle_result="failure",
        sign_result="skipped",
        run_cmd=recorder,
        probe=recorder,
        ghio=ghio,
        gitio=gitio,
        env={},
    )

    assert rc == 1
    assert "publish refused" in capsys.readouterr().err
    assert recorder.calls == []
    assert ghio.calls == []
    assert gitio.calls == []


def test_publish_failed_sign_blocks_and_skipped_sign_passes_the_gate():
    publish_mod.check_gate("success", "success", "skipped")
    with pytest.raises(ReleaseError):
        publish_mod.check_gate("success", "success", "failure")


TAG_ONLY_TOML = """
[artifacts.lex]
endpoints = ["gh-release"]
"""


def test_publish_verb_accepts_the_empty_matrix_skipped_results(
    tmp_path, monkeypatch, capsys
):
    _publish_repo(tmp_path, monkeypatch, toml=TAG_ONLY_TOML)
    recorder = SeamRecorder()
    ghio = FakeGh(exists=False)
    gitio = FakeGit(root=tmp_path)

    rc = release_verb.run_publish(
        _spec("1.2.3"),
        build_result="skipped",
        bundle_result="skipped",
        sign_result="skipped",
        matrix="[]",
        stages='["preflight", "prepare", "publish"]',
        run_cmd=recorder,
        probe=recorder,
        ghio=ghio,
        gitio=gitio,
        env={},
    )

    assert rc == 0
    assert ("create", "v1.2.3", str(tmp_path / "RELEASE_NOTES.md"), False) in (
        ghio.calls
    )
    assert not any(call[0] == "upload" for call in ghio.calls)
    assert recorder.calls == []
    assert "published 1.2.3" in capsys.readouterr().out


def test_publish_verb_still_refuses_a_live_skipped_build(tmp_path, monkeypatch, capsys):
    _publish_repo(tmp_path, monkeypatch)
    recorder = SeamRecorder()
    ghio = FakeGh()
    gitio = FakeGit(root=tmp_path)

    rc = release_verb.run_publish(
        _spec("1.2.3"),
        build_result="skipped",
        bundle_result="skipped",
        sign_result="skipped",
        matrix=json.dumps([{"artifact": "lex", "platform": "linux-x86_64"}]),
        stages='["preflight", "prepare", "bundle", "assert-bundle", "publish"]',
        run_cmd=recorder,
        probe=recorder,
        ghio=ghio,
        gitio=gitio,
        env={},
    )

    assert rc == 1
    err = capsys.readouterr().err
    assert "publish refused" in err
    assert "live build requires success" in err
    assert recorder.calls == []
    assert ghio.calls == []


def test_publish_verb_refuses_failure_and_cancelled_even_when_non_live(
    tmp_path, monkeypatch, capsys
):
    _publish_repo(tmp_path, monkeypatch, toml=TAG_ONLY_TOML)
    for result in ("failure", "cancelled"):
        rc = release_verb.run_publish(
            _spec("1.2.3"),
            build_result=result,
            bundle_result="skipped",
            sign_result="skipped",
            matrix="[]",
            stages='["preflight", "prepare", "publish"]',
            run_cmd=SeamRecorder(),
            probe=SeamRecorder(),
            ghio=FakeGh(),
            gitio=FakeGit(root=tmp_path),
            env={},
        )
        assert rc == 1
        assert "publish refused" in capsys.readouterr().err


def test_publish_verb_omitted_facts_keep_the_strict_gate(tmp_path, monkeypatch, capsys):
    _publish_repo(tmp_path, monkeypatch, toml=TAG_ONLY_TOML)
    rc = release_verb.run_publish(
        _spec("1.2.3"),
        build_result="skipped",
        bundle_result="skipped",
        sign_result="skipped",
        run_cmd=SeamRecorder(),
        probe=SeamRecorder(),
        ghio=FakeGh(),
        gitio=FakeGit(root=tmp_path),
        env={},
    )
    assert rc == 1
    assert "publish refused" in capsys.readouterr().err


def test_publish_verb_malformed_fact_is_a_loud_refusal(tmp_path, monkeypatch, capsys):
    _publish_repo(tmp_path, monkeypatch, toml=TAG_ONLY_TOML)
    ghio = FakeGh()
    rc = release_verb.run_publish(
        _spec("1.2.3"),
        build_result="skipped",
        bundle_result="skipped",
        sign_result="skipped",
        matrix="not json",
        stages='["publish"]',
        run_cmd=SeamRecorder(),
        probe=SeamRecorder(),
        ghio=ghio,
        gitio=FakeGit(root=tmp_path),
        env={},
    )
    assert rc == 1
    assert "--matrix is not valid JSON" in capsys.readouterr().err
    assert ghio.calls == []


def test_publish_rc_guard_records_no_external_invocation(tmp_path, monkeypatch, capsys):
    _publish_repo(tmp_path, monkeypatch, assets=[f"lex-{MAC_ARM}.tar.gz"])
    recorder = SeamRecorder()
    ghio = FakeGh(exists=False)
    gitio = FakeGit(root=tmp_path)

    rc = release_verb.run_publish(
        _spec("1.2.3-release-rc"),
        build_result="success",
        bundle_result="success",
        sign_result="skipped",
        run_cmd=recorder,
        probe=recorder,
        ghio=ghio,
        gitio=gitio,
        env={},
    )

    assert rc == 0
    assert recorder.calls == []
    assert [c[0] for c in gitio.calls if c[0] != "root"] == []
    assert (
        "create",
        "v1.2.3-release-rc",
        str(tmp_path / "RELEASE_NOTES.md"),
        True,
    ) in (ghio.calls)
    out = capsys.readouterr().out
    assert "live-fire -release-rc: GH release only" in out
    assert out.count("skipped: rc-guard") == 2


def test_publish_selector_skips_need_no_tokens_and_dispatch_nothing(
    tmp_path, monkeypatch, capsys
):
    _publish_repo(tmp_path, monkeypatch, assets=[f"lex-{MAC_ARM}.tar.gz"])
    recorder = SeamRecorder()
    ghio = FakeGh(exists=False)
    gitio = FakeGit(root=tmp_path)

    rc = release_verb.run_publish(
        _spec("1.2.3"),
        build_result="success",
        bundle_result="success",
        sign_result="skipped",
        endpoint_selector=["gh-release"],
        run_cmd=recorder,
        probe=recorder,
        ghio=ghio,
        gitio=gitio,
        env={},
    )

    assert rc == 0
    assert recorder.calls == []
    assert [c[0] for c in gitio.calls if c[0] != "root"] == []
    out = capsys.readouterr().out
    assert out.count("skipped: --endpoint selector") == 2
    assert "[gh-release]" in out


def test_publish_selector_refusal_dispatches_nothing(tmp_path, monkeypatch, capsys):
    _publish_repo(tmp_path, monkeypatch, assets=[f"lex-{MAC_ARM}.tar.gz"])
    recorder = SeamRecorder()
    ghio = FakeGh(exists=False)
    gitio = FakeGit(root=tmp_path)

    rc = release_verb.run_publish(
        _spec("1.2.3"),
        build_result="success",
        bundle_result="success",
        sign_result="skipped",
        endpoint_selector=["crates"],
        run_cmd=recorder,
        probe=recorder,
        ghio=ghio,
        gitio=gitio,
        env={"CARGO_REGISTRY_TOKEN": "t"},
    )

    assert rc == 1
    assert "cannot deselect `gh-release`" in capsys.readouterr().err
    assert recorder.calls == []
    assert ghio.calls == []


def test_publish_cli_endpoint_is_repeatable_and_absent_means_the_full_plan(
    monkeypatch,
):
    from click.testing import CliRunner

    seen: list = []

    def fake_run_publish(spec, **kwargs):
        seen.append(kwargs["endpoint_selector"])
        return 0

    monkeypatch.setattr(release_verb, "run_publish", fake_run_publish)
    argv = [
        "publish",
        "1.2.3",
        "--build-result",
        "success",
        "--bundle-result",
        "success",
        "--sign-result",
        "skipped",
    ]
    runner = CliRunner()
    runner.invoke(
        release_verb.release,
        argv + ["--endpoint", "gh-release", "--endpoint", "conda"],
    )
    runner.invoke(release_verb.release, argv)
    assert seen == [["gh-release", "conda"], None]


def test_publish_missing_tokens_fail_before_any_dispatch(tmp_path, monkeypatch, capsys):
    _publish_repo(tmp_path, monkeypatch)
    recorder = SeamRecorder()
    ghio = FakeGh()
    gitio = FakeGit(root=tmp_path)

    rc = release_verb.run_publish(
        _spec("1.2.3"),
        build_result="success",
        bundle_result="success",
        sign_result="success",
        run_cmd=recorder,
        probe=recorder,
        ghio=ghio,
        gitio=gitio,
        env={},
    )

    assert rc == 1
    err = capsys.readouterr().err
    assert "CARGO_REGISTRY_TOKEN (crates)" in err
    assert "HOMEBREW_TAP_TOKEN (brew)" in err
    assert recorder.calls == []
    assert ghio.calls == []


def test_publish_final_walks_release_endpoints_before_brew(
    tmp_path, monkeypatch, capsys
):
    _publish_repo(
        tmp_path,
        monkeypatch,
        assets=[f"lex-{MAC_ARM}.tar.gz", f"lex-{LINUX}.tar.gz"],
    )
    run_cmd = SeamRecorder({("cargo", "metadata"): _BREW_METADATA})
    probe = SeamRecorder()
    ghio = FakeGh(exists=False)
    gitio = FakeGit(root=tmp_path, dirty=True)
    env = {
        "CARGO_REGISTRY_TOKEN": "crates-token-83f2a1",
        "HOMEBREW_TAP_TOKEN": "tap-token-19bd77",
    }

    rc = release_verb.run_publish(
        _spec("1.2.3"),
        build_result="success",
        bundle_result="success",
        sign_result="skipped",
        run_cmd=run_cmd,
        probe=probe,
        ghio=ghio,
        gitio=gitio,
        env=env,
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert "published 1.2.3 to 3 endpoints" in out
    endpoints = [
        line.split("[")[1].split("]")[0] for line in out.splitlines() if "[" in line
    ]
    assert endpoints == ["gh-release", "crates", "brew"]
    assert any(c[0] == "upload" for c in ghio.calls)
    assert any(c[0] == "push" for c in gitio.calls)


def test_publish_json_carries_the_typed_result(tmp_path, monkeypatch, capsys):
    _publish_repo(
        tmp_path,
        monkeypatch,
        toml="""
[toolchains]
"." = "rust"

[artifacts.lex]
endpoints = ["gh-release"]
""",
    )
    ghio = FakeGh(exists=True)
    gitio = FakeGit(root=tmp_path)

    rc = release_verb.run_publish(
        _spec("2.0.0-rc.1"),
        build_result="success",
        bundle_result="success",
        sign_result="skipped",
        as_json=True,
        run_cmd=SeamRecorder(),
        probe=SeamRecorder(),
        ghio=ghio,
        gitio=gitio,
        env={},
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["version"] == "2.0.0-rc.1"
    assert payload["tag"] == "v2.0.0-rc.1"
    assert payload["prerelease"] is True
    assert payload["live_fire"] is False
    assert payload["published"][0]["endpoint"] == "gh-release"


def test_publish_with_no_endpoints_is_a_clean_noop(tmp_path, monkeypatch, capsys):
    _publish_repo(
        tmp_path,
        monkeypatch,
        toml="""
[toolchains]
"." = "rust"

[artifacts.lex]
build = ["rust"]
""",
    )
    rc = release_verb.run_publish(
        _spec("1.2.3"),
        build_result="success",
        bundle_result="success",
        sign_result="skipped",
        run_cmd=SeamRecorder(),
        probe=SeamRecorder(),
        ghio=FakeGh(),
        gitio=FakeGit(root=tmp_path),
        env={},
    )
    assert rc == 0
    assert "no endpoints declared" in capsys.readouterr().out


def _raise_missing_binary(argv, cwd, env=None):
    raise execrun.ExecError(
        [str(a) for a in argv],
        rc=None,
        stderr=f"[Errno 2] No such file or directory: {argv[0]!r}",
        cause=execrun.CAUSE_MISSING_BINARY,
    )


def test_missing_twine_gets_the_reconcile_remedy(tmp_path, monkeypatch, capsys):
    _publish_repo(
        tmp_path,
        monkeypatch,
        toml="""
[toolchains]
"." = "python"

[artifacts.pkg]
endpoints = ["pypi"]
""",
        assets=["pkg-1.2.3-py3-none-any.whl", "pkg-1.2.3.tar.gz"],
    )
    _pyproject(tmp_path, "pkg")

    rc = release_verb.run_publish(
        _spec("1.2.3"),
        build_result="success",
        bundle_result="success",
        sign_result="skipped",
        run_cmd=_raise_missing_binary,
        probe=SeamRecorder(),
        ghio=FakeGh(),
        gitio=FakeGit(root=tmp_path),
        env={"PYPI_TOKEN": "tok"},
    )

    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("error: ")
    assert "[artifacts.pkg] pypi:" in err
    assert "pixi.toml#shipit-python-release-deps" in err
    assert "`shipit install --pr`" in err
    assert "`shipit install --local`" in err
    assert "pixi.lock" in err


def test_publish_missing_npm_binary_gets_the_reconcile_remedy(
    tmp_path, monkeypatch, capsys
):
    _publish_repo(
        tmp_path,
        monkeypatch,
        toml="""
[artifacts.pkg]
endpoints = ["npm"]
""",
        assets=("pkg-1.2.3.tgz",),
    )

    rc = release_verb.run_publish(
        _spec("1.2.3"),
        build_result="success",
        bundle_result="success",
        sign_result="skipped",
        run_cmd=SeamRecorder(),
        probe=_raise_missing_binary,
        ghio=FakeGh(),
        gitio=FakeGit(root=tmp_path),
        env={"NPM_TOKEN": "tok"},
    )

    assert rc == 1
    err = capsys.readouterr().err
    assert "[artifacts.pkg] npm:" in err
    assert "pixi.toml#shipit-node-deps" in err
    assert "`shipit install --pr`" in err


def test_publish_refuses_outside_a_git_checkout(tmp_path, monkeypatch, capsys):
    (tmp_path / ".shipit.toml").write_text("", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    rc = release_verb.run_publish(
        _spec("1.2.3"),
        build_result="success",
        bundle_result="success",
        sign_result="skipped",
        run_cmd=SeamRecorder(),
        probe=SeamRecorder(),
        ghio=FakeGh(),
        gitio=FakeGit(root=None),
        env={},
    )
    assert rc == 1
    assert "not inside a git checkout" in capsys.readouterr().err


def test_publish_cli_rejects_a_bump_word_as_usage():
    from click.testing import CliRunner

    result = CliRunner().invoke(
        release_verb.release,
        [
            "publish",
            "minor",
            "--build-result",
            "success",
            "--bundle-result",
            "success",
            "--sign-result",
            "skipped",
        ],
    )
    assert result.exit_code == 2
    assert "concrete version" in result.output


def test_publish_cli_requires_the_result_inputs():
    from click.testing import CliRunner

    result = CliRunner().invoke(release_verb.release, ["publish", "1.2.3"])
    assert result.exit_code == 2
    assert "--build-result" in result.output


def test_notify_downstreams_fires_one_dispatch_per_downstream(tmp_path):
    artifact = _notify_artifacts()[0]
    ghio = FakeGh()
    req = _request(
        tmp_path,
        artifact,
        version="1.2.3",
        env={publish_mod.NOTIFY_SECRET: "pat-xyz"},
        ghio=ghio,
    )
    published = publish_mod._publish_notify_downstreams(req)
    dispatches = [c for c in ghio.calls if c[0] == "dispatch"]
    assert [c[1] for c in dispatches] == ["lex-fmt/vscode", "lex-fmt/nvim"]
    for _, _slug, event_type, payload, token in dispatches:
        assert event_type == publish_mod.NOTIFY_EVENT_TYPE
        assert token == "pat-xyz"
        assert payload == {
            "repo": "acme/widget",
            "tag": "v1.2.3",
            "version": "1.2.3",
            "artifact": "parser",
        }
    assert published.endpoint == "notify-downstreams"
    assert published.actions == (
        "dispatched upstream-release to lex-fmt/vscode",
        "dispatched upstream-release to lex-fmt/nvim",
    )


def test_notify_downstreams_refuses_an_unresolved_source_repo(tmp_path):
    artifact = _notify_artifacts()[0]
    ghio = FakeGh()
    req = _request(
        tmp_path,
        artifact,
        version="1.2.3",
        env={publish_mod.NOTIFY_SECRET: "pat-xyz"},
        ghio=ghio,
        repo=None,
    )
    with pytest.raises(ReleaseError, match="no source repo resolved"):
        publish_mod._publish_notify_downstreams(req)
    assert not [c for c in ghio.calls if c[0] == "dispatch"]


def test_notify_downstreams_refuses_without_the_cross_repo_token(tmp_path):
    artifact = _notify_artifacts()[0]
    ghio = FakeGh()
    req = _request(tmp_path, artifact, version="1.2.3", env={}, ghio=ghio)
    with pytest.raises(ReleaseError, match="DOWNSTREAM_DISPATCH_TOKEN"):
        publish_mod._publish_notify_downstreams(req)
    assert not [c for c in ghio.calls if c[0] == "dispatch"]


def test_notify_downstreams_secret_mirrors_the_derivation_authority():
    assert (
        publish_mod.NOTIFY_SECRET
        == (secretreq_mod.ENDPOINT_SECRETS["notify-downstreams"][0])
    )
    adapter = publish_mod.adapter_for("notify-downstreams")
    assert adapter is not None
    assert adapter.secrets == secretreq_mod.ENDPOINT_SECRETS["notify-downstreams"]
    assert adapter.stable_only and adapter.stage == "derived"


LINUX_ARM = "aarch64-unknown-linux-gnu"
WIN = "x86_64-pc-windows-msvc"
MUSL = "x86_64-unknown-linux-musl"


def test_conda_subdir_maps_served_and_drops_unserved():
    assert publish_mod.conda_subdir(MAC_ARM) == "osx-arm64"
    assert publish_mod.conda_subdir(LINUX) == "linux-64"
    assert publish_mod.conda_subdir(LINUX_ARM) == "linux-aarch64"
    assert publish_mod.conda_subdir(WIN) == "win-64"
    assert publish_mod.conda_subdir(MAC_X64) is None
    assert publish_mod.conda_subdir(MUSL) is None


def test_conda_assets_derives_served_subdirs_from_the_platforms_declaration():
    artifact = _artifacts(
        {
            "lex": {
                "endpoints": ["conda"],
                "platforms": [
                    "darwin-arm64",
                    "linux-x86_64",
                    "windows-x86_64",
                    "linux-arm64",
                    "darwin-x86_64",
                    "linux-x86_64-musl",
                ],
            }
        }
    )[0]
    staged = [
        f"lex-{MAC_ARM}.tar.gz",
        f"lex-{LINUX}.tar.gz",
        f"lex-{WIN}.zip",
        f"lex-{MAC_X64}.tar.gz",
        f"lex-{MUSL}.tar.gz",
        "sibling-x86_64-pc-windows-msvc.zip",
    ]
    assets = publish_mod.conda_assets(artifact, staged)
    assert assets == {
        "osx-arm64": (MAC_ARM, f"lex-{MAC_ARM}.tar.gz"),
        "linux-64": (LINUX, f"lex-{LINUX}.tar.gz"),
        "win-64": (WIN, f"lex-{WIN}.zip"),
    }


def test_conda_assets_ignores_a_staged_archive_for_an_undeclared_platform():
    artifact = _artifacts(
        {"lex": {"endpoints": ["conda"], "platforms": ["linux-x86_64"]}}
    )[0]
    staged = [f"lex-{LINUX}.tar.gz", f"lex-{MAC_ARM}.tar.gz"]
    assert publish_mod.conda_assets(artifact, staged) == {
        "linux-64": (LINUX, f"lex-{LINUX}.tar.gz"),
    }


def test_conda_assets_defaults_undeclared_platforms_to_the_linux_lane():
    artifact = _artifacts({"lex": {"endpoints": ["conda"]}})[0]
    staged = [f"lex-{LINUX}.tar.gz", f"lex-{MAC_ARM}.tar.gz"]
    assert publish_mod.conda_assets(artifact, staged) == {
        "linux-64": (LINUX, f"lex-{LINUX}.tar.gz"),
    }


def test_conda_served_subdirs_projects_the_repos_own_platforms():
    artifacts = _artifacts(
        {
            "lexd": {
                "build": ["rust"],
                "platforms": ["linux-x86_64", "linux-arm64", "darwin-arm64"],
                "endpoints": ["gh-release", "conda"],
            }
        }
    )
    assert publish_mod.conda_served_subdirs(artifacts) == (
        "osx-arm64",
        "linux-64",
        "linux-aarch64",
    )
    assert "win-64" not in publish_mod.conda_served_subdirs(artifacts)


def test_conda_served_subdirs_drops_unserved_and_non_conda():
    artifacts = _artifacts(
        {
            "cli": {
                "build": ["rust"],
                "platforms": ["darwin-x86_64", "linux-x86_64-musl", "windows-x86_64"],
                "endpoints": ["gh-release", "conda"],
            },
            "other": {
                "build": ["rust"],
                "platforms": ["linux-arm64"],
                "endpoints": ["gh-release"],
            },
        }
    )
    assert publish_mod.conda_served_subdirs(artifacts) == ("win-64",)


def test_conda_served_subdirs_defaults_empty_platforms_to_the_linux_lane():
    artifacts = _artifacts(
        {"lex": {"build": ["rust"], "endpoints": ["gh-release", "conda"]}}
    )
    assert publish_mod.conda_served_subdirs(artifacts) == ("linux-64",)


def test_conda_served_subdirs_empty_without_a_conda_producer():
    artifacts = _artifacts({"cli": {"build": ["rust"], "endpoints": ["gh-release"]}})
    assert publish_mod.conda_served_subdirs(artifacts) == ()


def test_conda_package_name_is_the_lowercased_main_binary():
    artifact = _artifacts({"lex": {"endpoints": ["conda"], "main-binary": "LexD"}})[0]
    assert publish_mod.conda_package_name(artifact) == "lexd"


def test_conda_package_name_rejects_a_conda_invalid_derived_name():
    scoped = _artifacts({"lex": {"endpoints": ["conda"], "main-binary": "@scope/lex"}})[
        0
    ]
    with pytest.raises(ReleaseError, match="not a valid conda package name"):
        publish_mod.conda_package_name(scoped)
    spaced = _artifacts({"lex": {"endpoints": ["conda"], "product-name": "My Tool"}})[0]
    with pytest.raises(ReleaseError, match="not a valid conda package name"):
        publish_mod.conda_package_name(spaced)


def test_render_conda_recipe_repackages_the_prebuilt_binary():
    unix = publish_mod.render_conda_recipe(
        package="lexd",
        version="1.2.3",
        archive_path="/stage/lexd-aarch64-apple-darwin.tar.gz",
        source_binary="lexd",
        install_dir="bin",
        install_binary="lexd",
    )
    assert "name: lexd" in unix
    assert 'version: "1.2.3"' in unix
    assert '- path: "/stage/lexd-aarch64-apple-darwin.tar.gz"' in unix
    assert 'cp "lexd" "${PREFIX}/bin/lexd"' in unix
    assert yaml.safe_load(unix)["build"]["dynamic_linking"] == {
        "binary_relocation": False
    }
    src, install_dir, install_bin = publish_mod._conda_binary_layout("win-64", "lexd")
    assert (src, install_dir, install_bin) == ("lexd.exe", "Scripts", "lexd.exe")
    win = publish_mod.render_conda_recipe(
        package="lexd",
        version="1.2.3",
        archive_path="/stage/lexd-x86_64-pc-windows-msvc.zip",
        source_binary=src,
        install_dir=install_dir,
        install_binary=install_bin,
    )
    assert 'cp "lexd.exe" "${PREFIX}/Scripts/lexd.exe"' in win


def test_render_conda_recipe_escapes_the_archive_path_scalar():
    weird = '/weird/pa"th\\dir/lexd.tar.gz'
    recipe = publish_mod.render_conda_recipe(
        package="lexd",
        version="1.2.3",
        archive_path=weird,
        source_binary="lexd",
        install_dir="bin",
        install_binary="lexd",
    )
    doc = yaml.safe_load(recipe)
    assert doc["source"][0]["path"] == weird


class _CondaBuildRecorder(SeamRecorder):
    def __call__(self, argv, cwd, env=None):
        argv_s = [str(a) for a in argv]
        if argv_s[:2] == ["rattler-build", "build"]:
            out = Path(argv_s[argv_s.index("--output-dir") + 1])
            subdir = argv_s[argv_s.index("--target-platform") + 1]
            pkg_dir = out / subdir
            pkg_dir.mkdir(parents=True, exist_ok=True)
            (pkg_dir / "lex-1.2.3-h0_0.conda").write_bytes(b"fake-conda")
        return super().__call__(argv, cwd, env)


def _conda_request(tmp_path, *, env=None, assets=None, ghio=None):
    _staged_assets(
        tmp_path,
        assets
        if assets is not None
        else [
            f"lex-{MAC_ARM}.tar.gz",
            f"lex-{LINUX}.tar.gz",
            f"lex-{WIN}.zip",
            f"lex-{MAC_X64}.tar.gz",
        ],
    )
    artifact = _artifacts(
        {
            "lex": {
                "build": ["rust"],
                "endpoints": ["conda"],
                "platforms": [
                    "darwin-arm64",
                    "linux-x86_64",
                    "windows-x86_64",
                    "darwin-x86_64",
                ],
            }
        }
    )[0]
    run_cmd = _CondaBuildRecorder()
    req = _request(
        tmp_path,
        artifact,
        env=env
        if env is not None
        else {
            "ARTIFACT_CHANNEL_KEY_ID": "chan-key-id",
            "ARTIFACT_CHANNEL_SECRET_KEY": "chan-secret-key",
        },
        run_cmd=run_cmd,
        ghio=ghio,
    )
    return req, run_cmd


def test_conda_builds_each_served_subdir_and_publishes_the_channel(tmp_path):
    req, run_cmd = _conda_request(tmp_path)

    published = publish_mod._publish_conda(req)

    builds = [
        argv for argv, _, _ in run_cmd.calls if argv[:2] == ("rattler-build", "build")
    ]
    built_subdirs = sorted(argv[argv.index("--target-platform") + 1] for argv in builds)
    assert built_subdirs == ["linux-64", "osx-arm64", "win-64"]
    for argv in builds:
        assert (
            "--package-format" in argv
            and argv[argv.index("--package-format") + 1] == "conda"
        )
        assert ("--test", "native") == (
            argv[argv.index("--test")],
            argv[argv.index("--test") + 1],
        )
    publishes = [
        (argv, env)
        for argv, _, env in run_cmd.calls
        if argv[:2] == ("rattler-build", "publish")
    ]
    assert len(publishes) == 1
    pub_argv, pub_env = publishes[0]
    assert pub_argv[:3] == ("rattler-build", "publish", "--to")
    assert pub_argv[3] == "s3://shipit-artifacts-public/acme/widget"
    assert "--force" in pub_argv
    assert sum(1 for a in pub_argv if a.endswith(".conda")) == 3
    assert pub_env["AWS_ENDPOINT_URL"] == publish_mod.CONDA_S3_ENDPOINT
    assert pub_env["AWS_REGION"] == "auto"
    assert pub_env["AWS_ACCESS_KEY_ID"] == "chan-key-id"
    assert pub_env["AWS_SECRET_ACCESS_KEY"] == "chan-secret-key"
    assert not any("chan-secret-key" in a for a in pub_argv)
    assert any("published 3 package(s)" in a for a in published.actions)


def _conda_publish_target(req):
    (pub_argv,) = [
        argv
        for argv, _, _ in req.run_cmd.calls
        if argv[:2] == ("rattler-build", "publish")
    ]
    return pub_argv[pub_argv.index("--to") + 1]


def test_conda_publishes_a_public_repo_to_the_public_bucket(tmp_path):
    req, _ = _conda_request(tmp_path, ghio=FakeGh(private=False))
    publish_mod._publish_conda(req)
    assert _conda_publish_target(req) == "s3://shipit-artifacts-public/acme/widget"
    assert ("private?", "acme/widget") in req.ghio.calls


def test_conda_publishes_a_private_repo_to_the_private_bucket(tmp_path):
    req, _ = _conda_request(tmp_path, ghio=FakeGh(private=True))
    published = publish_mod._publish_conda(req)
    assert _conda_publish_target(req) == "s3://shipit-artifacts-private/acme/widget"
    assert ("private?", "acme/widget") in req.ghio.calls
    assert any("shipit-artifacts-private" in a for a in published.actions)


def test_conda_recipe_source_is_the_bare_binary_name(tmp_path):
    req, _ = _conda_request(tmp_path)

    publish_mod._publish_conda(req)

    recipe_root = req.assets_dir / publish_mod.CONDA_RECIPE_SCRATCH / req.artifact.name
    unix_recipe = (recipe_root / "osx-arm64" / "recipe.yaml").read_text()
    assert 'cp "lex" "${PREFIX}/bin/lex"' in unix_recipe
    assert f"lex-{MAC_ARM}/lex" not in unix_recipe
    win_recipe = (recipe_root / "win-64" / "recipe.yaml").read_text()
    assert 'cp "lex.exe" "${PREFIX}/Scripts/lex.exe"' in win_recipe
    assert f"lex-{WIN}/lex.exe" not in win_recipe


def test_conda_scratch_is_namespaced_per_artifact(tmp_path):
    req, _ = _conda_request(tmp_path)

    publish_mod._publish_conda(req)

    recipe_root = req.assets_dir / publish_mod.CONDA_RECIPE_SCRATCH
    channel_root = req.assets_dir / publish_mod.CONDA_CHANNEL_SCRATCH
    assert (recipe_root / req.artifact.name / "osx-arm64" / "recipe.yaml").is_file()
    assert (channel_root / req.artifact.name / "osx-arm64").is_dir()
    assert not (recipe_root / "osx-arm64").exists()
    assert not (channel_root / "osx-arm64").exists()


def test_conda_without_served_archives_refuses(tmp_path):
    req, _ = _conda_request(
        tmp_path, assets=[f"lex-{MAC_X64}.tar.gz", f"lex-{MUSL}.tar.gz"]
    )
    with pytest.raises(ReleaseError, match="no declared platform maps"):
        publish_mod._publish_conda(req)


def test_conda_refuses_an_unresolved_source_repo(tmp_path):
    _staged_assets(tmp_path, [f"lex-{MAC_ARM}.tar.gz"])
    artifact = _artifacts({"lex": {"endpoints": ["conda"]}})[0]
    req = _request(
        tmp_path,
        artifact,
        env={
            "ARTIFACT_CHANNEL_KEY_ID": "chan-key-id",
            "ARTIFACT_CHANNEL_SECRET_KEY": "chan-secret-key",
        },
        repo=None,
    )
    with pytest.raises(ReleaseError, match="no source repo resolved"):
        publish_mod._publish_conda(req)


def test_conda_requires_the_write_credentials(tmp_path):
    req, _ = _conda_request(tmp_path, env={"ARTIFACT_CHANNEL_KEY_ID": "chan-key-id"})
    with pytest.raises(ReleaseError, match="ARTIFACT_CHANNEL_SECRET_KEY"):
        publish_mod._publish_conda(req)


CONDA_CREDS = {
    "ARTIFACT_CHANNEL_KEY_ID": "chan-key-id",
    "ARTIFACT_CHANNEL_SECRET_KEY": "chan-secret-key",
}


def _noarch_artifact():
    return _artifacts(
        {
            "grammar": {
                "build": ["tree-sitter"],
                "bundle": {
                    "composition": "tarball",
                    "leg": "tree-sitter",
                    "payload": [{"path": "src", "required": True}],
                },
                "endpoints": ["conda"],
            }
        }
    )[0]


def _wasm_noarch_artifact(scope="lex-fmt"):
    bundle = {"composition": "wasm-pack"}
    if scope is not None:
        bundle["scope"] = scope
    return _artifacts(
        {"lex-wasm": {"build": ["rust"], "bundle": bundle, "endpoints": ["conda"]}}
    )[0]


def test_conda_noarch_eligible_for_every_platform_independent_composition():
    assert publish_mod.conda_noarch_eligible(_noarch_artifact())
    assert publish_mod.conda_noarch_eligible(_wasm_noarch_artifact())
    zed = _artifacts(
        {
            "ext": {
                "build": ["tree-sitter"],
                "bundle": {
                    "composition": "zed",
                    "leg": "tree-sitter",
                    "payload": [{"path": "src", "required": True}],
                },
                "endpoints": ["conda"],
            }
        }
    )[0]
    assert publish_mod.conda_noarch_eligible(zed)
    tool = _artifacts(
        {
            "lex": {
                "build": ["rust"],
                "bundle": {"composition": "archive"},
                "endpoints": ["conda"],
            }
        }
    )[0]
    assert not publish_mod.conda_noarch_eligible(tool)
    bare = _artifacts({"lex": {"build": ["rust"], "endpoints": ["conda"]}})[0]
    assert not publish_mod.conda_noarch_eligible(bare)


def test_conda_noarch_asset_is_the_single_untripled_archive():
    grammar = _noarch_artifact()
    assert (
        publish_mod.conda_noarch_asset(
            grammar, "1.2.3", ["grammar.tar.gz", "other.tar.gz"]
        )
        == "grammar.tar.gz"
    )
    assert (
        publish_mod.conda_noarch_asset(grammar, "1.2.3", [f"grammar-{LINUX}.tar.gz"])
        is None
    )
    assert publish_mod.conda_noarch_asset(grammar, "1.2.3", []) is None


def test_conda_noarch_asset_for_wasm_pack_is_the_npm_tgz():
    wasm = _wasm_noarch_artifact()
    want = "lex-fmt-lex-wasm-1.2.3.tgz"
    assert publish_mod.conda_noarch_asset_name(wasm, "1.2.3") == want
    assert publish_mod.conda_noarch_asset(wasm, "1.2.3", [want, "other.tgz"]) == want
    assert publish_mod.conda_noarch_asset(wasm, "1.2.3", ["lex-wasm.tar.gz"]) is None


def test_conda_noarch_package_name_flattens_a_scoped_wasm_identity():
    assert (
        publish_mod.conda_noarch_package_name(_wasm_noarch_artifact())
        == "lex-fmt-lex-wasm"
    )
    assert (
        publish_mod.conda_noarch_package_name(_wasm_noarch_artifact(scope=None))
        == "lex-wasm"
    )
    assert publish_mod.conda_noarch_package_name(_noarch_artifact()) == "grammar"
    spaced = _artifacts(
        {
            "x": {
                "build": ["tree-sitter"],
                "bundle": {
                    "composition": "tarball",
                    "leg": "tree-sitter",
                    "payload": [{"path": "src", "required": True}],
                },
                "endpoints": ["conda"],
                "product-name": "my grammar",
            }
        }
    )[0]
    with pytest.raises(ReleaseError, match="not a valid conda package name"):
        publish_mod.conda_noarch_package_name(spaced)


def test_render_conda_noarch_recipe_is_generic_and_installs_the_payload():
    recipe = publish_mod.render_conda_noarch_recipe(
        package="grammar",
        version="1.2.3",
        archive_path="/stage/grammar.tar.gz",
        install_dir="share",
    )
    doc = yaml.safe_load(recipe)
    assert doc["package"] == {"name": "grammar", "version": "1.2.3"}
    assert doc["build"]["noarch"] == "generic"
    assert "dynamic_linking" not in doc["build"]
    assert doc["source"][0]["path"] == "/stage/grammar.tar.gz"
    assert doc["source"][0]["target_directory"] == "payload"
    script = "\n".join(doc["build"]["script"])
    assert 'cp -R payload/. "${PREFIX}/share/grammar"' in script


def test_render_conda_noarch_recipe_escapes_the_archive_path_scalar():
    weird = '/weird/pa"th\\dir/grammar.tar.gz'
    doc = yaml.safe_load(
        publish_mod.render_conda_noarch_recipe(
            package="grammar", version="1.2.3", archive_path=weird, install_dir="share"
        )
    )
    assert doc["source"][0]["path"] == weird


class _CondaNoarchBuildRecorder(SeamRecorder):
    def __call__(self, argv, cwd, env=None):
        argv_s = [str(a) for a in argv]
        if argv_s[:2] == ["rattler-build", "build"]:
            out = Path(argv_s[argv_s.index("--output-dir") + 1])
            pkg_dir = out / "noarch"
            pkg_dir.mkdir(parents=True, exist_ok=True)
            (pkg_dir / "grammar-1.2.3-h0_0.conda").write_bytes(b"fake-conda")
        return super().__call__(argv, cwd, env)


def _noarch_request(tmp_path, *, assets=None, run_cmd=None, ghio=None, env=None):
    _staged_assets(tmp_path, assets if assets is not None else ["grammar.tar.gz"])
    req = _request(
        tmp_path,
        _noarch_artifact(),
        env=CONDA_CREDS if env is None else env,
        run_cmd=run_cmd or _CondaNoarchBuildRecorder(),
        ghio=ghio,
    )
    return req, req.run_cmd


def test_conda_noarch_builds_one_generic_package_and_publishes_to_noarch(tmp_path):
    req, run_cmd = _noarch_request(tmp_path)

    published = publish_mod._publish_conda(req)

    builds = [
        argv for argv, _, _ in run_cmd.calls if argv[:2] == ("rattler-build", "build")
    ]
    assert len(builds) == 1
    (build_argv,) = builds
    assert "--target-platform" not in build_argv
    recipe_path = Path(build_argv[build_argv.index("--recipe") + 1])
    assert yaml.safe_load(recipe_path.read_text())["build"]["noarch"] == "generic"
    publishes = [
        (argv, env)
        for argv, _, env in run_cmd.calls
        if argv[:2] == ("rattler-build", "publish")
    ]
    assert len(publishes) == 1
    pub_argv, pub_env = publishes[0]
    assert (
        pub_argv[pub_argv.index("--to") + 1]
        == "s3://shipit-artifacts-public/acme/widget"
    )
    assert "--force" in pub_argv
    assert sum(1 for a in pub_argv if a.endswith(".conda")) == 1
    assert pub_env["AWS_ENDPOINT_URL"] == publish_mod.CONDA_S3_ENDPOINT
    assert pub_env["AWS_ACCESS_KEY_ID"] == "chan-key-id"
    assert not any("chan-secret-key" in a for a in pub_argv)
    assert any("noarch" in a for a in published.actions)
    assert published.endpoint == "conda"


def test_conda_noarch_publishes_a_private_repo_to_the_private_bucket(tmp_path):
    req, run_cmd = _noarch_request(tmp_path, ghio=FakeGh(private=True))
    publish_mod._publish_conda(req)
    (pub_argv,) = [
        argv for argv, _, _ in run_cmd.calls if argv[:2] == ("rattler-build", "publish")
    ]
    assert (
        pub_argv[pub_argv.index("--to") + 1]
        == "s3://shipit-artifacts-private/acme/widget"
    )


def test_conda_noarch_scratch_is_namespaced_per_artifact(tmp_path):
    req, _ = _noarch_request(tmp_path)
    publish_mod._publish_conda(req)
    recipe = (
        req.assets_dir
        / publish_mod.CONDA_RECIPE_SCRATCH
        / "grammar"
        / "noarch"
        / "recipe.yaml"
    )
    channel = req.assets_dir / publish_mod.CONDA_CHANNEL_SCRATCH / "grammar" / "noarch"
    assert recipe.is_file()
    assert channel.is_dir()
    assert not (req.assets_dir / publish_mod.CONDA_RECIPE_SCRATCH / "noarch").exists()


def _wasm_noarch_request(tmp_path, *, assets=None, run_cmd=None):
    _staged_assets(
        tmp_path, assets if assets is not None else ["lex-fmt-lex-wasm-1.2.3.tgz"]
    )
    req = _request(
        tmp_path,
        _wasm_noarch_artifact(),
        env=CONDA_CREDS,
        run_cmd=run_cmd or _CondaNoarchBuildRecorder(),
    )
    return req, req.run_cmd


def test_conda_noarch_wasm_pack_takes_the_noarch_path(tmp_path):
    req, run_cmd = _wasm_noarch_request(tmp_path)

    published = publish_mod._publish_conda(req)

    builds = [
        argv for argv, _, _ in run_cmd.calls if argv[:2] == ("rattler-build", "build")
    ]
    assert len(builds) == 1
    (build_argv,) = builds
    assert "--target-platform" not in build_argv
    recipe = yaml.safe_load(
        Path(build_argv[build_argv.index("--recipe") + 1]).read_text()
    )
    assert recipe["package"]["name"] == "lex-fmt-lex-wasm"
    assert recipe["build"]["noarch"] == "generic"
    assert recipe["source"][0]["path"].endswith("lex-fmt-lex-wasm-1.2.3.tgz")
    publishes = [
        argv for argv, _, _ in run_cmd.calls if argv[:2] == ("rattler-build", "publish")
    ]
    assert len(publishes) == 1
    assert sum(1 for a in publishes[0] if a.endswith(".conda")) == 1
    assert any("noarch" in a for a in published.actions)


def test_conda_noarch_wasm_pack_without_the_tgz_refuses(tmp_path):
    req, _ = _wasm_noarch_request(tmp_path, assets=["lex-wasm.tar.gz"])
    with pytest.raises(
        ReleaseError, match=r"no `lex-fmt-lex-wasm-1\.2\.3\.tgz`.*wasm-pack"
    ):
        publish_mod._publish_conda(req)


def test_conda_noarch_without_the_archive_refuses(tmp_path):
    req, _ = _noarch_request(tmp_path, assets=[f"grammar-{LINUX}.tar.gz"])
    with pytest.raises(ReleaseError, match=r"no `grammar\.tar\.gz`"):
        publish_mod._publish_conda(req)


def test_conda_noarch_requires_the_write_credentials(tmp_path):
    req, _ = _noarch_request(tmp_path, env={"ARTIFACT_CHANNEL_KEY_ID": "chan-key-id"})
    with pytest.raises(ReleaseError, match="ARTIFACT_CHANNEL_SECRET_KEY"):
        publish_mod._publish_conda(req)


def _conda_package_files(conda_path: Path) -> list[str]:
    zstandard = pytest.importorskip("zstandard")

    names: list[str] = []
    with zipfile.ZipFile(conda_path) as z:
        pkg = next(
            n for n in z.namelist() if n.startswith("pkg-") and n.endswith(".zst")
        )
        raw = zstandard.ZstdDecompressor().decompress(
            z.read(pkg), max_output_size=1 << 26
        )
    with tarfile.open(fileobj=io.BytesIO(raw)) as tf:
        for name in tf.getnames():
            if not name.startswith("info/"):
                names.append(name)
    return names


class _RealBuildFakePublish:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, cwd, env=None):
        argv_s = [str(a) for a in argv]
        self.calls.append((tuple(argv_s), Path(cwd), dict(env) if env else None))
        if argv_s[:2] == ["rattler-build", "publish"]:
            return _ok(argv_s)
        return execrun.run(
            argv_s, cwd=str(cwd), env=dict(env) if env else None, timeout=600
        )


@pytest.mark.skipif(
    shutil.which("rattler-build") is None,
    reason="rattler-build not on PATH (present in the `test` env; a bare host skips)",
)
def test_conda_noarch_real_repackage_builds_a_generic_conda(tmp_path):
    payload = tmp_path / "payload"
    (payload / "src").mkdir(parents=True)
    (payload / "src" / "parser.c").write_text("/* generated */\n", encoding="utf-8")
    (payload / "queries").mkdir()
    (payload / "queries" / "highlights.scm").write_text("; q\n", encoding="utf-8")
    (payload / "grammar.js").write_text("module.exports = {}\n", encoding="utf-8")
    dist = _staged_assets(tmp_path, [])
    with tarfile.open(dist / "grammar.tar.gz", "w:gz") as tf:
        for entry in ("src", "queries", "grammar.js"):
            tf.add(payload / entry, arcname=entry)

    run_cmd = _RealBuildFakePublish()
    req = _request(tmp_path, _noarch_artifact(), env=CONDA_CREDS, run_cmd=run_cmd)

    published = publish_mod._publish_conda(req)

    channel = req.assets_dir / publish_mod.CONDA_CHANNEL_SCRATCH / "grammar" / "noarch"
    built = list(channel.glob("*.conda"))
    assert len(built) == 1, f"expected one noarch .conda, got {built}"
    files = sorted(_conda_package_files(built[0]))
    assert files == [
        "share/grammar/grammar.js",
        "share/grammar/queries/highlights.scm",
        "share/grammar/src/parser.c",
    ]
    (pub_argv, _, _) = next(
        c for c in run_cmd.calls if c[0][:2] == ("rattler-build", "publish")
    )
    assert (
        pub_argv[pub_argv.index("--to") + 1]
        == "s3://shipit-artifacts-public/acme/widget"
    )
    assert any(a.endswith(".conda") for a in pub_argv)
    assert any("noarch" in a for a in published.actions)


@pytest.mark.skipif(
    shutil.which("rattler-build") is None,
    reason="rattler-build not on PATH (present in the `test` env; a bare host skips)",
)
def test_conda_noarch_wasm_pack_real_repackage_builds_a_generic_conda(tmp_path):
    pkg = tmp_path / "package"
    (pkg / "snippets").mkdir(parents=True)
    (pkg / "lex_wasm_bg.wasm").write_bytes(b"\x00asm\x01\x00\x00\x00")
    (pkg / "lex_wasm.js").write_text("export const x = 1\n", encoding="utf-8")
    (pkg / "package.json").write_text(
        '{"name":"@lex-fmt/lex-wasm"}\n', encoding="utf-8"
    )
    (pkg / "snippets" / "helper.js").write_text("// snip\n", encoding="utf-8")
    dist = _staged_assets(tmp_path, [])
    with tarfile.open(dist / "lex-fmt-lex-wasm-1.2.3.tgz", "w:gz") as tf:
        tf.add(pkg, arcname="package")

    run_cmd = _RealBuildFakePublish()
    req = _request(tmp_path, _wasm_noarch_artifact(), env=CONDA_CREDS, run_cmd=run_cmd)

    published = publish_mod._publish_conda(req)

    channel = req.assets_dir / publish_mod.CONDA_CHANNEL_SCRATCH / "lex-wasm" / "noarch"
    built = list(channel.glob("*.conda"))
    assert len(built) == 1, f"expected one noarch .conda, got {built}"
    files = sorted(_conda_package_files(built[0]))
    assert files == [
        "share/lex-fmt-lex-wasm/lex_wasm.js",
        "share/lex-fmt-lex-wasm/lex_wasm_bg.wasm",
        "share/lex-fmt-lex-wasm/package.json",
        "share/lex-fmt-lex-wasm/snippets/helper.js",
    ]
    assert not any("package/package/" in f for f in files)
    assert any("noarch" in a for a in published.actions)


_SUBDIR_TRIPLE = {
    subdir: triple for triple, subdir in publish_mod.CONDA_SUBDIRS.items()
}


@functools.cache
def _load_roundtrip_harness():
    import importlib.util

    path = (
        Path(__file__).resolve().parent.parent / "tools" / "conda_channel_roundtrip.py"
    )
    spec = importlib.util.spec_from_file_location("conda_channel_roundtrip", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load the round-trip harness from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _conda_tool_artifact():
    return _artifacts(
        {
            "lex": {
                "build": ["rust"],
                "endpoints": ["conda"],
                "platforms": [
                    "darwin-arm64",
                    "linux-x86_64",
                    "linux-arm64",
                    "windows-x86_64",
                ],
            }
        }
    )[0]


def _prebuilt_tool_archive(dist, artifact, triple, binary):
    top = dist / f"{artifact}-{triple}"
    top.mkdir(parents=True, exist_ok=True)
    staged = top / binary
    staged.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    staged.chmod(staged.stat().st_mode | 0o755)
    with tarfile.open(dist / f"{artifact}-{triple}.tar.gz", "w:gz") as tf:
        tf.add(top, arcname=top.name)


@pytest.mark.skipif(
    shutil.which("rattler-build") is None,
    reason="rattler-build not on PATH (present in the `test` env; a bare host skips)",
)
def test_conda_per_platform_real_repackage_cross_target_builds_a_conda(tmp_path):
    roundtrip = _load_roundtrip_harness()
    try:
        host = roundtrip.host_conda_subdir()
    except RuntimeError as exc:
        pytest.skip(str(exc))
    target = next(s for s in ("osx-arm64", "linux-64", "linux-aarch64") if s != host)
    triple = _SUBDIR_TRIPLE[target]

    dist = _staged_assets(tmp_path, [])
    _prebuilt_tool_archive(dist, "lex", triple, "lex")

    run_cmd = _RealBuildFakePublish()
    req = _request(tmp_path, _conda_tool_artifact(), env=CONDA_CREDS, run_cmd=run_cmd)

    published = publish_mod._publish_conda(req)

    builds = [c for c in run_cmd.calls if c[0][:2] == ("rattler-build", "build")]
    assert len(builds) == 1
    (build_argv, _, _) = builds[0]
    assert build_argv[build_argv.index("--target-platform") + 1] == target

    channel = req.assets_dir / publish_mod.CONDA_CHANNEL_SCRATCH / "lex" / target
    built = list(channel.glob("*.conda"))
    assert len(built) == 1, f"expected one {target} .conda, got {built}"
    assert sorted(_conda_package_files(built[0])) == ["bin/lex"]

    recipe = yaml.safe_load(
        Path(build_argv[build_argv.index("--recipe") + 1]).read_text()
    )
    assert recipe["build"]["dynamic_linking"]["binary_relocation"] is False

    (pub_argv, _, _) = next(
        c for c in run_cmd.calls if c[0][:2] == ("rattler-build", "publish")
    )
    assert any(a.endswith(".conda") for a in pub_argv)
    assert any(target in a for a in published.actions)


@pytest.mark.skipif(
    shutil.which("rattler-build") is None or shutil.which("pixi") is None,
    reason="rattler-build + pixi needed for the file:// round trip (both in `test`)",
)
def test_conda_file_channel_roundtrip_resolves_and_stages_the_binary(tmp_path):
    roundtrip = _load_roundtrip_harness()
    try:
        native = roundtrip.host_conda_subdir()
    except RuntimeError as exc:
        pytest.skip(str(exc))
    triple = _SUBDIR_TRIPLE[native]

    dist = _staged_assets(tmp_path, [])
    _prebuilt_tool_archive(dist, "lex", triple, "lex")

    run_cmd = _RealBuildFakePublish()
    req = _request(tmp_path, _conda_tool_artifact(), env=CONDA_CREDS, run_cmd=run_cmd)
    publish_mod._publish_conda(req)

    channel = req.assets_dir / publish_mod.CONDA_CHANNEL_SCRATCH / "lex"
    assert list((channel / native).glob("*.conda")), "producer built no .conda"

    staged = roundtrip.resolve_from_file_channel(
        channel_dir=channel,
        package="lex",
        version="1.2.3",
        binary="lex",
        scratch=tmp_path,
    )

    assert staged.exists()
    assert (staged.parent.name, staged.name) == ("bin", "lex")
    assert "echo hi" in staged.read_text(encoding="utf-8")

    assert staged.stat().st_mode & 0o111, (
        f"staged {staged} is not executable (mode {oct(staged.stat().st_mode)}) — "
        f"a consumer resolving this package could not run it"
    )
    ran = subprocess.run([str(staged)], capture_output=True, text=True, timeout=30)
    assert ran.returncode == 0, f"staged tool exited {ran.returncode}: {ran.stderr!r}"
    assert "hi" in ran.stdout


@pytest.mark.skipif(
    shutil.which("rattler-build") is None or shutil.which("pixi") is None,
    reason="rattler-build + pixi needed for the file:// round trip (both in `test`)",
)
def test_conda_roundtrip_stage_from_prefix_copies_binary_into_resources(tmp_path):
    roundtrip = _load_roundtrip_harness()
    try:
        native = roundtrip.host_conda_subdir()
    except RuntimeError as exc:
        pytest.skip(str(exc))
    triple = _SUBDIR_TRIPLE[native]

    dist = _staged_assets(tmp_path, [])
    _prebuilt_tool_archive(dist, "lex", triple, "lex")

    run_cmd = _RealBuildFakePublish()
    req = _request(tmp_path, _conda_tool_artifact(), env=CONDA_CREDS, run_cmd=run_cmd)
    publish_mod._publish_conda(req)

    channel = req.assets_dir / publish_mod.CONDA_CHANNEL_SCRATCH / "lex"
    staged_bin = roundtrip.resolve_from_file_channel(
        channel_dir=channel,
        package="lex",
        version="1.2.3",
        binary="lex",
        scratch=tmp_path,
    )
    assert staged_bin.stat().st_mode & 0o111, "precondition: resolved binary runnable"

    root = tmp_path / "resolve"
    assert (root / ".pixi" / "envs" / "default" / "bin" / "lex").exists()

    entry = config.StageEntry(package="lex", source="bin/lex", dest="resources/lex")
    result = staging.stage(root, [entry])

    dest = root / "resources" / "lex"
    assert dest.is_file(), "the resolved binary was not copied under resources/"
    assert dest.read_text(encoding="utf-8") == staged_bin.read_text(encoding="utf-8")
    assert dest.stat().st_mode & 0o111, (
        f"staged {dest} lost its exec bit (mode {oct(dest.stat().st_mode)}) — the "
        f"shipped bundle would carry a non-runnable binary"
    )
    ran = subprocess.run([str(dest)], capture_output=True, text=True, timeout=30)
    assert ran.returncode == 0 and "hi" in ran.stdout
    assert result == [
        staging.StagedFile(
            package="lex",
            source="bin/lex",
            dest="resources/lex",
            is_dir=False,
            executable=True,
        )
    ]


def test_roundtrip_main_skips_cleanly_on_an_unmapped_host(monkeypatch, capsys):
    roundtrip = _load_roundtrip_harness()
    monkeypatch.setattr(roundtrip.shutil, "which", lambda *args, **kwargs: None)
    monkeypatch.setattr(roundtrip, "_HOST_SUBDIR", {})

    rc = roundtrip._main([])

    assert rc == 0
    err = capsys.readouterr().err
    assert "skipping" in err and "unsupported host" in err


def test_conda_secret_pair_mirrors_the_derivation_authority():
    adapter = publish_mod.adapter_for("conda")
    assert adapter is not None
    assert adapter.secrets == secretreq_mod.ENDPOINT_SECRETS["conda"]
    assert (publish_mod.CONDA_KEY_ID_SECRET, publish_mod.CONDA_SECRET_KEY_SECRET) == (
        secretreq_mod.ENDPOINT_SECRETS["conda"]
    )
    assert adapter.stage == "derived" and adapter.external and not adapter.stable_only
    assert adapter.needs_repo


def test_plan_prerelease_keeps_conda_but_a_live_fire_skips_it():
    artifacts = _artifacts({"lex": {"endpoints": ["gh-release", "brew", "conda"]}})
    pre = publish_mod.plan(artifacts, prerelease=True, live_fire=False)
    verdicts = {d.adapter.name: d.skip for d in pre}
    assert verdicts["conda"] is None
    assert verdicts["brew"] == publish_mod.SKIP_STABLE_ONLY
    live = publish_mod.plan(artifacts, prerelease=True, live_fire=True)
    assert {d.adapter.name: d.skip for d in live}["conda"] == publish_mod.SKIP_RC_GUARD


def test_plan_conda_alone_is_valid_without_a_gh_release():
    artifacts = _artifacts({"lex": {"endpoints": ["conda"]}})
    dispatched = publish_mod.plan(artifacts, prerelease=False, live_fire=False)
    verdicts = {d.adapter.name: d.skip for d in dispatched}
    assert verdicts == {"conda": None}


def test_plan_conda_direct_needs_no_gh_release_even_selected_alone():
    artifacts = _artifacts({"lex": {"endpoints": ["conda", "crates"]}})
    dispatched = publish_mod.plan(
        artifacts, prerelease=True, live_fire=False, selector=["conda"]
    )
    verdicts = {d.adapter.name: d.skip for d in dispatched}
    assert verdicts["conda"] is None
    assert verdicts["crates"] == publish_mod.SKIP_SELECTOR


def test_missing_rattler_build_gets_the_reconcile_remedy(tmp_path, monkeypatch, capsys):
    _publish_repo(
        tmp_path,
        monkeypatch,
        toml="""
[toolchains]
"." = "rust"

[artifacts.lex]
build = ["rust"]
endpoints = ["gh-release", "conda"]
platforms = ["darwin-arm64"]
""",
        assets=[f"lex-{MAC_ARM}.tar.gz"],
    )

    def _seam(argv, cwd, env=None):
        if [str(a) for a in argv][:1] == ["rattler-build"]:
            _raise_missing_binary(argv, cwd, env)
        return _ok(argv)

    rc = release_verb.run_publish(
        _spec("1.2.3"),
        build_result="success",
        bundle_result="success",
        sign_result="skipped",
        run_cmd=_seam,
        probe=SeamRecorder(),
        ghio=FakeGh(),
        gitio=FakeGit(root=tmp_path),
        env={
            "ARTIFACT_CHANNEL_KEY_ID": "chan-key-id",
            "ARTIFACT_CHANNEL_SECRET_KEY": "chan-secret-key",
        },
    )

    assert rc == 1
    err = capsys.readouterr().err
    assert "[artifacts.lex] conda:" in err
    assert "pixi.toml#shipit-conda-packager" in err
    assert "`shipit install --pr`" in err


def _zed_artifacts():
    return _artifacts(
        {"zed-lex": {"build": ["rust"], "endpoints": ["gh-release", "zed"]}}
    )


def _write_zed_manifest(tmp_path, *, ext_id="lex"):
    (tmp_path / "extension.toml").write_text(
        f'id = "{ext_id}"\nname = "Lex"\nversion = "0.0.0"\n', encoding="utf-8"
    )


@pytest.mark.parametrize("ext_id", ["lex", "zed-lex", "lex_2", "html", "toml0"])
def test_zed_extension_id_reads_a_valid_manifest_id(ext_id):
    assert publish_mod.zed_extension_id(f'id = "{ext_id}"\nname = "Lex"\n') == ext_id


@pytest.mark.parametrize("text", ['name = "Lex"\n', "", "id = 3\n"])
def test_zed_extension_id_refuses_a_manifest_without_a_string_id(text):
    with pytest.raises(ReleaseError, match="no top-level `id`"):
        publish_mod.zed_extension_id(text)


@pytest.mark.parametrize(
    "ext_id",
    [
        "../zed-registry",
        "..",
        "/tmp/x",
        "foo/bar",
        "foo.bar",
        'x]\nversion = "0"',
        "Lex",
        "with space",
        "-leading",
    ],
)
def test_zed_extension_id_refuses_an_id_outside_the_grammar(ext_id):
    manifest = f"id = {json.dumps(ext_id)}\n"
    with pytest.raises(ReleaseError, match="is not a valid Zed extension id"):
        publish_mod.zed_extension_id(manifest)


def test_zed_extension_id_refuses_an_empty_id():
    with pytest.raises(ReleaseError, match="no top-level `id`"):
        publish_mod.zed_extension_id('id = ""\n')


def test_zed_extension_id_refuses_unparseable_toml():
    with pytest.raises(ReleaseError, match="cannot parse"):
        publish_mod.zed_extension_id("id = = broken")


def test_zed_publish_refuses_a_traversal_id_before_writing(tmp_path):
    artifact = _zed_artifacts()[0]
    (tmp_path / "extension.toml").write_text('id = "../escape"\n', encoding="utf-8")
    req = _request(
        tmp_path,
        artifact,
        entries=_entries({".": "rust"}),
        version="1.2.3",
        repo="lex-fmt/zed-lex",
    )
    with pytest.raises(ReleaseError, match="is not a valid Zed extension id"):
        publish_mod._publish_zed(req)
    assert not (tmp_path / "dist" / publish_mod.ZED_SCRATCH).exists()


def test_render_zed_registry_entry_emits_the_row_and_submodule_rev():
    text = publish_mod.render_zed_registry_entry(
        ext_id="lex", version="1.2.3", repo="lex-fmt/zed-lex", tag="v1.2.3"
    )
    assert "[lex]\n" in text
    assert 'submodule = "extensions/lex"\n' in text
    assert 'version = "1.2.3"\n' in text
    assert "github.com/lex-fmt/zed-lex @ v1.2.3" in text


def test_zed_renders_the_registry_entry_and_reports_the_manual_step(tmp_path):
    artifact = _zed_artifacts()[0]
    _write_zed_manifest(tmp_path, ext_id="lex")
    seam = SeamRecorder()
    req = _request(
        tmp_path,
        artifact,
        entries=_entries({".": "rust"}),
        version="1.2.3",
        run_cmd=seam,
        repo="lex-fmt/zed-lex",
    )
    published = publish_mod._publish_zed(req)
    assert seam.calls == []
    rendered = tmp_path / "dist" / publish_mod.ZED_SCRATCH / "lex.extensions-toml"
    assert rendered.is_file()
    assert 'submodule = "extensions/lex"' in rendered.read_text()
    assert published.endpoint == "zed"
    assert published.actions[0] == (
        "rendered zed-industries/extensions registry entry for lex 1.2.3 "
        "(submodule extensions/lex -> github.com/lex-fmt/zed-lex@v1.2.3)"
    )
    assert "manual step" in published.actions[1]


def test_zed_refuses_an_unresolved_source_repo(tmp_path):
    artifact = _zed_artifacts()[0]
    _write_zed_manifest(tmp_path)
    req = _request(
        tmp_path,
        artifact,
        entries=_entries({".": "rust"}),
        version="1.2.3",
        repo=None,
    )
    with pytest.raises(ReleaseError, match="no source repo resolved"):
        publish_mod._publish_zed(req)


def test_zed_refuses_a_missing_extension_manifest(tmp_path):
    artifact = _zed_artifacts()[0]
    req = _request(
        tmp_path,
        artifact,
        entries=_entries({".": "rust"}),
        version="1.2.3",
        repo="lex-fmt/zed-lex",
    )
    with pytest.raises(ReleaseError, match="cannot read"):
        publish_mod._publish_zed(req)


def _zed_bundled_artifacts(*, leg="rust", payload=None, leg_name="rust"):
    return _artifacts(
        {
            "zed-lex": {
                "build": [leg_name],
                "endpoints": ["gh-release", "zed"],
                "bundle": {
                    "composition": "zed",
                    "leg": leg,
                    "payload": payload
                    or [
                        {"path": "extension.toml", "required": True},
                        {"path": "shared"},
                    ],
                },
            }
        }
    )


def test_zed_bundle_and_endpoint_read_the_same_declared_leg(tmp_path):
    artifact = _zed_bundled_artifacts(leg="tree-sitter", leg_name="tree-sitter")[0]
    (tmp_path / "grammar").mkdir()
    _write_zed_manifest(tmp_path / "grammar", ext_id="lex")
    (tmp_path / "grammar" / "shared").mkdir()
    (tmp_path / "crate").mkdir()
    _write_zed_manifest(tmp_path / "crate", ext_id="decoy")
    entries = _entries({"grammar": "tree-sitter", "crate": "rust"})
    recorder = SeamRecorder()

    bundle_mod.ZED.compose(
        bundle_mod.ComposeRequest(
            artifact=artifact,
            entries=entries,
            root=tmp_path,
            out_dir=tmp_path / "dist",
            target="x86_64-unknown-linux-gnu",
            run_cmd=recorder,
            build_target=None,
            artifact_deps=(),
        )
    )
    published = publish_mod._publish_zed(
        _request(tmp_path, artifact, entries=entries, repo="lex-fmt/zed-lex")
    )

    ((argv, *_rest),) = recorder.calls
    assert argv[argv.index("-C") + 1] == str(tmp_path / "grammar")
    assert "for lex 1.2.3" in published.actions[0]
    assert "decoy" not in published.actions[0]
    assert (
        tmp_path / "dist" / publish_mod.ZED_SCRATCH / "lex.extensions-toml"
    ).is_file()


def test_zed_refuses_a_declaration_that_omits_the_manifest(tmp_path):
    artifact = _zed_bundled_artifacts(payload=[{"path": "shared", "required": True}])[0]
    _write_zed_manifest(tmp_path, ext_id="lex")
    req = _request(
        tmp_path,
        artifact,
        entries=_entries({".": "rust"}),
        version="1.2.3",
        repo="lex-fmt/zed-lex",
    )

    with pytest.raises(ReleaseError) as excinfo:
        publish_mod._publish_zed(req)
    message = str(excinfo.value)
    assert "does not declare `extension.toml` as a required entry" in message
    assert not (tmp_path / "dist" / publish_mod.ZED_SCRATCH).exists()


def test_zed_refuses_a_manifest_declared_only_when_present(tmp_path):
    artifact = _zed_bundled_artifacts(
        payload=[{"path": "shared", "required": True}, {"path": "extension.toml"}]
    )[0]
    _write_zed_manifest(tmp_path, ext_id="lex")
    req = _request(
        tmp_path,
        artifact,
        entries=_entries({".": "rust"}),
        version="1.2.3",
        repo="lex-fmt/zed-lex",
    )
    with pytest.raises(ReleaseError, match="as a required entry"):
        publish_mod._publish_zed(req)


def test_zed_endpoint_only_artifact_reads_the_crate_leg(tmp_path):
    artifact = _zed_artifacts()[0]
    assert artifact.bundle is None
    (tmp_path / "crate").mkdir()
    _write_zed_manifest(tmp_path / "crate", ext_id="lex")
    req = _request(
        tmp_path,
        artifact,
        entries=_entries({"crate": "rust"}),
        version="1.2.3",
        repo="lex-fmt/zed-lex",
    )
    published = publish_mod._publish_zed(req)
    assert "for lex 1.2.3" in published.actions[0]


def test_zed_declares_no_secret_and_is_a_derived_stable_only_endpoint():
    assert secretreq_mod.ENDPOINT_SECRETS["zed"] == ()
    adapter = publish_mod.adapter_for("zed")
    assert adapter is not None
    assert adapter.secrets == ()
    assert adapter.stage == "derived"
    assert adapter.stable_only and adapter.external and adapter.needs_repo


def test_plan_prerelease_skips_zed_and_a_live_fire_skips_it_too():
    artifacts = _artifacts({"zed-lex": {"endpoints": ["gh-release", "zed"]}})
    pre = publish_mod.plan(artifacts, prerelease=True, live_fire=False)
    assert {d.adapter.name: d.skip for d in pre}[
        "zed"
    ] == publish_mod.SKIP_ZED_PRERELEASE
    live = publish_mod.plan(artifacts, prerelease=True, live_fire=True)
    assert {d.adapter.name: d.skip for d in live}["zed"] == publish_mod.SKIP_RC_GUARD


def test_plan_zed_alone_needs_no_gh_release():
    artifacts = _artifacts({"zed-lex": {"endpoints": ["zed"]}})
    dispatched = publish_mod.plan(artifacts, prerelease=False, live_fire=False)
    assert [d.adapter.name for d in dispatched] == ["zed"]
    assert dispatched[0].skip is None
