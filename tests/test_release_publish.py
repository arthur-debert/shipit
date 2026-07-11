"""`shipit release publish` — the terminal stage's cores and adapters.

The pure cores (:mod:`shipit.release.publish`) are fixture-tested straight:
the scar-#3 refusal gate over EVERY upstream-result combination, the central
``-release-rc`` guard, the release-before-derived ordering plan, the crates
topological order, and the brew render core (:mod:`shipit.release.brew`).
The adapters are driven with the ONE effectful boundary recorded (PRD
Testing Decisions): the Exec seam (``run_cmd``/``probe`` — exact command
lines, exact cwds, exact env) plus fake gh/git adapters, so every
acceptance assertion — the RC guard's "no external invocation recorded",
crates' resume-past-published, gh-release's create-vs-edit with the
prerelease flag re-asserted, brew's unchanged-formula no-op push — reads
off recorded invocations. Prior art: the bundle stage's recorder tests.
"""

import itertools
import json
from pathlib import Path

import pytest

from shipit import config, execrun
from shipit.release import ReleaseError
from shipit.release import brew as brew_mod
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
    """The recorded Exec seam: exact argv, cwd, env — with scripted answers.

    ``answers`` maps a command head (or an ``argv`` prefix tuple) to either a
    ready :class:`ExecResult`-factory ``callable(argv) -> ExecResult`` or a
    static stdout string (wrapped as rc=0). Unscripted commands succeed empty.
    """

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
    """The recorded gh-adapter seam: release calls + repo reads."""

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


class FakeGit:
    """The recorded git-adapter seam for the tap push (clone → status →
    add/commit/push). ``dirty`` scripts the post-copy porcelain answer —
    the changed-vs-unchanged formula branch."""

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


# --------------------------------------------------------------------------
# The refusal gate (scar #3, story 32) — every result combination
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("build", "bundle", "sign"),
    list(itertools.product(publish_mod.STAGE_RESULTS, repeat=3)),
)
def test_gate_admits_exactly_success_success_and_sign_skipped_or_success(
    build, bundle, sign
):
    """Publish proceeds ONLY on build=bundle=success with sign
    success-or-skipped: a failed sign blocks, a skipped sign passes, a
    failed (or skipped, or cancelled) bundle blocks — all 64 combinations."""
    allowed = (
        build == "success"
        and bundle == "success"
        and sign
        in (
            "success",
            "skipped",
        )
    )
    if allowed:
        publish_mod.check_gate(build, bundle, sign)
    else:
        with pytest.raises(ReleaseError, match="publish refused"):
            publish_mod.check_gate(build, bundle, sign)


def test_gate_refusal_names_every_blocking_input():
    with pytest.raises(ReleaseError) as err:
        publish_mod.check_gate("failure", "cancelled", "failure")
    message = str(err.value)
    assert "build=failure" in message
    assert "bundle=cancelled" in message
    assert "sign=failure" in message


# --------------------------------------------------------------------------
# The RC guard predicate (story 33)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("version", "live"),
    [
        ("1.2.3-release-rc", True),
        ("1.2.3-release-rc.2", True),  # the legacy contains('-release-rc.') arm
        ("1.2.3-rc.1", False),  # a plain prerelease is NOT live-fire
        ("1.2.3", False),
        ("1.2.3-release-rcx", False),  # suffix must be exact or dotted
    ],
)
def test_is_live_fire(version, live):
    assert publish_mod.is_live_fire(version) is live


# --------------------------------------------------------------------------
# The ordering plan (story 35) + the closed registry
# --------------------------------------------------------------------------


def test_registry_mirrors_the_config_endpoint_set_and_stages():
    """The closed registry: exactly the config boundary's ENDPOINTS (no
    marketplace-class adapters — PRD Out of Scope), brew the one derived
    entry, gh-release the one the RC guard keeps."""
    assert publish_mod.names() == config.ENDPOINTS
    assert [a.name for a in publish_mod.ADAPTERS if a.stage == "derived"] == ["brew"]
    assert [a.name for a in publish_mod.ADAPTERS if not a.external] == ["gh-release"]


def test_registry_secret_names_mirror_the_derivation_authority():
    """Story 43: each adapter's declared secret names ARE
    `secretreq.ENDPOINT_SECRETS` (the one derivation authority, WS02) — publish
    consumes that map rather than re-declaring names that could drift from what
    gh-setup syncs and preflight validates. gh-release declares none (ambient
    gh auth)."""
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
    # brew is DECLARED FIRST on lex but dispatches LAST: every release-stage
    # endpoint (declaration order across artifacts) precedes the derived one.
    assert order == [
        ("lex", "crates"),
        ("lex", "gh-release"),
        ("plugin", "npm"),
        ("lex", "brew"),
    ]
    assert all(d.skip is None for d in dispatched)


def test_plan_rc_guard_keeps_only_gh_release():
    """Story 33: a live-fire cut skips EVERY external endpoint centrally —
    the skip verdicts are data, one implementation, no per-job YAML."""
    artifacts = _artifacts(
        {"lex": {"endpoints": ["gh-release", "crates", "pypi", "npm", "brew"]}}
    )
    dispatched = publish_mod.plan(artifacts, prerelease=True, live_fire=True)
    verdicts = {d.adapter.name: d.skip for d in dispatched}
    assert verdicts["gh-release"] is None
    for name in ("crates", "pypi", "npm", "brew"):
        assert verdicts[name] == publish_mod.SKIP_RC_GUARD


def test_plan_prerelease_skips_only_brew():
    """A plain -rc.N prerelease still publishes externally (testpypi is the
    staging lane) but NEVER moves the tap formula (stable channel)."""
    artifacts = _artifacts({"lex": {"endpoints": ["gh-release", "crates", "brew"]}})
    dispatched = publish_mod.plan(artifacts, prerelease=True, live_fire=False)
    verdicts = {d.adapter.name: d.skip for d in dispatched}
    assert verdicts["gh-release"] is None
    assert verdicts["crates"] is None
    assert verdicts["brew"] == publish_mod.SKIP_STABLE_ONLY


def test_plan_unknown_endpoint_is_a_hard_error_naming_the_known_set():
    """The config boundary already rejects unknown names; the registry
    re-refuses for hand-built artifacts — never a quiet skip."""
    rogue = config.Artifact(name="ext", endpoints=("vs-marketplace",))
    with pytest.raises(ReleaseError, match="unknown endpoint") as err:
        publish_mod.plan([rogue], prerelease=False, live_fire=False)
    assert "gh-release, crates, pypi, npm, brew" in str(err.value)


def test_plan_brew_alone_refuses_without_an_unskipped_gh_release():
    """The brew formula points at gh-release assets; a brew endpoint with no
    unskipped gh-release in the plan would push a formula referencing a release
    this run never created — a hard plan refusal, not a broken tap."""
    artifacts = _artifacts({"lex": {"endpoints": ["brew"]}})
    with pytest.raises(ReleaseError, match="brew endpoint renders a formula"):
        publish_mod.plan(artifacts, prerelease=False, live_fire=False)


def test_plan_brew_with_gh_release_on_any_artifact_is_valid():
    """One unskipped gh-release (any artifact) uploads the whole asset tree, so
    it satisfies every brew formula's asset URLs — the invariant is plan-wide."""
    artifacts = _artifacts(
        {"lex": {"endpoints": ["brew"]}, "core": {"endpoints": ["gh-release"]}}
    )
    dispatched = publish_mod.plan(artifacts, prerelease=False, live_fire=False)
    assert {(d.artifact.name, d.adapter.name) for d in dispatched} == {
        ("core", "gh-release"),
        ("lex", "brew"),
    }


def test_plan_brew_needs_gh_release_unskipped_not_merely_present(tmp_path):
    """A prerelease that skips brew never trips the invariant; but a stable cut
    whose only gh-release was RC-skipped would — the check reads the UNSKIPPED
    set. Here brew is skipped by the prerelease rule, so the plan is valid even
    without gh-release."""
    artifacts = _artifacts({"lex": {"endpoints": ["brew"]}})
    dispatched = publish_mod.plan(artifacts, prerelease=True, live_fire=False)
    assert dispatched[0].skip == publish_mod.SKIP_STABLE_ONLY


def test_missing_secrets_reports_planned_unskipped_dispatches_only():
    artifacts = _artifacts(
        {"lex": {"endpoints": ["gh-release", "crates", "npm", "brew"]}}
    )
    live = publish_mod.plan(artifacts, prerelease=True, live_fire=True)
    # RC guard active: externals are skipped, so NO tokens are required.
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


# --------------------------------------------------------------------------
# gh-release — create-or-edit + asset upload
# --------------------------------------------------------------------------


def _staged_assets(tmp_path, names):
    dist = tmp_path / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    for name in names:
        (dist / name).write_bytes(b"bytes-of-" + name.encode())
    return dist


def _pyproject(dir_path, name):
    """A minimal ``pyproject.toml`` naming the distribution — what the pypi
    adapter reads to scope its upload to this artifact."""
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
            "lex.unsigned-app.tar.gz",  # the reseal payload never ships
            ".DS_Store",  # hidden files never ship
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
        True,  # prerelease derived from the semver suffix
    )
    kind, tag, files = ghio.calls[2]
    assert (kind, tag) == ("upload", "v1.2.3-rc.1")
    assert files == (str(tmp_path / "dist" / f"lex-{MAC_ARM}.tar.gz"),)
    assert published.endpoint == "gh-release"


def test_gh_release_resume_edits_and_reasserts_the_prerelease_flag(tmp_path):
    """The release#726 scar: `gh release edit` leaves the prerelease flag
    unchanged unless passed — the resume path states it explicitly (False
    for a final), and edits rather than duplicating."""
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


# --------------------------------------------------------------------------
# crates — topological order + already-published resume (story 36)
# --------------------------------------------------------------------------


def _cargo_metadata(deps: dict[str, list], dev: dict[str, list] | None = None) -> str:
    dev = dev or {}
    packages = []
    for name, needs in deps.items():
        dependencies = [{"name": d, "kind": None} for d in needs]
        dependencies += [{"name": d, "kind": "dev"} for d in dev.get(name, [])]
        packages.append(
            {"id": f"id-{name}", "name": name, "dependencies": dependencies}
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
    """A lib's test helper depending back on the lib is legal and must not
    read as a publish cycle — dev-dependencies are excluded."""
    metadata = json.loads(
        _cargo_metadata({"core": [], "helper": ["core"]}, dev={"core": ["helper"]})
    )
    assert publish_mod.crates_publish_order(metadata) == ("core", "helper")


def test_crates_publish_order_refuses_a_real_cycle():
    metadata = json.loads(_cargo_metadata({"a": ["b"], "b": ["a"]}))
    with pytest.raises(ReleaseError, match="dependency cycle"):
        publish_mod.crates_publish_order(metadata)


def test_crates_publishes_in_order_and_resumes_past_already_published(tmp_path):
    """Mid-workspace resumption: crate `core` is already on the registry
    (a prior partial run) — cargo's nonzero already-uploaded answer is
    SUCCESS and the walk continues to the dependents."""
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
    # The registry token rides the child env (never argv, never the ambient
    # process env) — so an injected `env` authenticates the publish, exactly
    # like the pypi/npm adapters.
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


# --------------------------------------------------------------------------
# pypi — wheel+sdist selection, --skip-existing, the testpypi lane
# --------------------------------------------------------------------------


def test_pypi_uploads_selects_the_named_distribution_wheel_and_sdist_only():
    names = [
        "pkg-1.0.0-py3-none-any.whl",
        "pkg-1.0.0.tar.gz",
        "other_pkg-2.0.0-py3-none-any.whl",  # another artifact's wheel
        "other_pkg-2.0.0.tar.gz",  # ...and its sdist: both out of scope
        f"lex-{LINUX}.tar.gz",  # an archive-composition tarball: NOT python
        "pkg-9.9.9.tar.gz",  # an sdist with no matching wheel: not selected
    ]
    assert publish_mod.pypi_uploads(names, "pkg") == (
        "pkg-1.0.0-py3-none-any.whl",
        "pkg-1.0.0.tar.gz",
    )


def test_pypi_uploads_matches_a_legacy_hyphenated_sdist_canonically():
    """A wheel PEP 427-escapes the dist name to underscores, but a legacy
    sdist may keep the original hyphens/dots — the match is canonical, so the
    sdist is still selected (never silently skipped)."""
    names = [
        "my_awesome_pkg-1.0.0-py3-none-any.whl",
        "my-awesome-pkg-1.0.0.tar.gz",
    ]
    assert publish_mod.pypi_uploads(names, "my-awesome-pkg") == (
        "my_awesome_pkg-1.0.0-py3-none-any.whl",
        "my-awesome-pkg-1.0.0.tar.gz",
    )


def test_pypi_scopes_the_upload_to_the_artifact_distribution(tmp_path):
    """A multi-artifact bundle tree carries another artifact's wheel; publish
    uploads ONLY this artifact's distribution — never leaks a foreign wheel to
    the index under this token (registry publishes are irreversible)."""
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


# --------------------------------------------------------------------------
# npm — prebuilt tree, no rebuild, publish-over-existing is success
# --------------------------------------------------------------------------


def test_npm_publishes_the_prebuilt_tree_without_rebuilding(tmp_path):
    artifact = _artifacts({"wasm": {"endpoints": ["npm"]}})[0]
    entries = _entries({"pkg": "npm"})
    probe = SeamRecorder()
    req = _request(
        tmp_path,
        artifact,
        entries=entries,
        env={"NPM_TOKEN": "npm-tok"},  # looked up under the secret name
        probe=probe,
    )

    published = publish_mod._publish_npm(req)

    argv, cwd, env = probe.calls[0]
    assert argv == ("npm", "publish", "--ignore-scripts")
    assert cwd == tmp_path / "pkg"  # the prebuilt tree, not a rebuild
    # ...and fed to npm under the var npm reads (NODE_AUTH_TOKEN), not the
    # secret name — the two-vocabulary indirection secretreq owns.
    assert env == {"NODE_AUTH_TOKEN": "npm-tok"}
    assert published.actions == ("published 1.2.3 from pkg",)


def test_npm_publish_over_existing_is_success(tmp_path):
    artifact = _artifacts({"wasm": {"endpoints": ["npm"]}})[0]
    entries = _entries({"pkg": "npm"})
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
        entries=entries,
        env={"NPM_TOKEN": "tok"},
        probe=probe,
    )
    published = publish_mod._publish_npm(req)
    assert published.actions == ("1.2.3 already published — resumed",)


# --------------------------------------------------------------------------
# brew — render, syntax check, tap push (derived; unchanged = no-op)
# --------------------------------------------------------------------------

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
    # Final release-asset URLs (the derived-stage contract) + local sha256s.
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
    # The wheel-ish tarball never reads as a target triple.
    assert "pkg-1.0.0" not in rendered
    # ruby -c syntax-checked the rendered file through the exec seam.
    assert ("ruby", "-c", str(tmp_path / "dist" / "brew" / "lex.rb")) in [
        argv for argv, _, _ in run_cmd.calls
    ]
    # Tap push: token-authenticated clone, identity stated, formula committed.
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
    assert kinds == ["clone", "status"]  # no add, no commit, no push
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
    assert "on_linux" not in text  # no linux target: no empty block
    assert 'bin.install "lex-cli"' in text


def test_brew_render_escapes_ruby_string_metadata():
    """Metadata carrying a double quote, a backslash, or a ``#{`` interpolation
    still renders a formula ``ruby -c`` accepts — the literals are escaped, so
    a valid crate description never breaks the tap publish."""
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
    # Every inner double quote is backslash-escaped (no bare `"` closes the
    # literal early) and the interpolation is defused.
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


# --------------------------------------------------------------------------
# The verb — gate first, RC guard through the exec seam, ordering, tokens
# --------------------------------------------------------------------------

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
    """Story 32, all-blocking: a failed bundle refuses BEFORE any read or
    dispatch — no git, no gh, no exec invocation recorded."""
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


def test_publish_rc_guard_records_no_external_invocation(tmp_path, monkeypatch, capsys):
    """Story 33's acceptance shape: a -release-rc publish, with NO tokens in
    the env and no CI environment, creates only the GH prerelease — the exec
    seam records no cargo/twine/npm call, git records no tap push."""
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
        env={},  # no tokens needed: every external endpoint is skipped
    )

    assert rc == 0
    assert recorder.calls == []  # no cargo publish, no twine, no npm, no ruby
    assert [c[0] for c in gitio.calls if c[0] != "root"] == []  # no tap push
    assert (
        "create",
        "v1.2.3-release-rc",
        str(tmp_path / "RELEASE_NOTES.md"),
        True,
    ) in (ghio.calls)
    out = capsys.readouterr().out
    assert "live-fire -release-rc: GH release only" in out
    assert out.count("skipped: rc-guard") == 2  # crates + brew


def test_publish_missing_tokens_fail_before_any_dispatch(tmp_path, monkeypatch, capsys):
    """Stories 43-45: a final publish with no tokens is ONE loud refusal
    naming every missing token — nothing dispatched, never a silent skip."""
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
    """The end-to-end ordering read off the result: gh-release and crates
    complete before brew renders against the uploaded assets."""
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
    # Ordering: the published list is plan-ordered — brew LAST.
    endpoints = [
        line.split("[")[1].split("]")[0] for line in out.splitlines() if "[" in line
    ]
    assert endpoints == ["gh-release", "crates", "brew"]
    # gh-release uploaded before brew cloned the tap.
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
    """The click boundary: publish ships the version prepare cut — a bump
    word dies as exit 2, never re-resolved."""
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
    """Story 32: the results are EXPLICIT inputs — omitting one is a usage
    error, so no caller can publish without stating the upstream verdicts."""
    from click.testing import CliRunner

    result = CliRunner().invoke(release_verb.release, ["publish", "1.2.3"])
    assert result.exit_code == 2
    assert "--build-result" in result.output
