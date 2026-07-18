"""`shipit release publish` — the terminal stage's cores and adapters.

The pure cores (:mod:`shipit.release.publish`) are fixture-tested straight:
the scar-#3 refusal gate over EVERY upstream-result × stage-liveness
combination (plus the plan-fact liveness derivations, issue #745), the central
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
import yaml

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

    def repository_dispatch(self, slug, *, event_type, payload, token=None):
        self.calls.append(("dispatch", slug, event_type, dict(payload), token))


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


def _stage_admits(result: str, live: bool) -> bool:
    """The gate's per-stage contract (issue #745): a LIVE build/bundle must
    be success; a plan-proven non-live one may be success or skipped;
    failure/cancelled always block."""
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
    """The full result × liveness matrix (all 256 combinations): publish
    proceeds ONLY when build and bundle each satisfy their liveness contract
    (live → success; non-live → success-or-skipped; failure/cancelled always
    block) and sign is success-or-skipped regardless of liveness."""
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
    """Omitted liveness facts keep the strict contract: a skipped
    build/bundle blocks unless the plan PROVED the stage non-live — the
    laptop/direct-caller default never weakens the gate."""
    with pytest.raises(ReleaseError, match="live build requires success"):
        publish_mod.check_gate("skipped", "success", "skipped")
    with pytest.raises(ReleaseError, match="live bundle requires success"):
        publish_mod.check_gate("success", "skipped", "skipped")


def test_gate_empty_matrix_shape_publishes():
    """The confirmed #745 shape: an empty-matrix plan drives the composed
    chain to build=bundle=skipped (the caller job of an if-skipped inner job
    concludes skipped — canary-confirmed), and the gate accepts it because
    the plan proves both stages non-live."""
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


# --------------------------------------------------------------------------
# The liveness facts (issue #745) — plan JSON verbatim, never result strings
# --------------------------------------------------------------------------


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
    # A garbled fact must never silently read as live OR non-live.
    with pytest.raises(ReleaseError, match="--matrix"):
        publish_mod.build_is_live(raw)
    with pytest.raises(ReleaseError, match="--stages"):
        publish_mod.bundle_is_live(raw)


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
    """The closed registry: exactly the config boundary's ENDPOINTS (now
    including the two VS Code marketplace endpoints, TOL02-WS13, the
    notify-downstreams cascade, TOL02-WS16, and the conda Artifact-channel
    producer, ARF01-WS01); brew + notify-downstreams + conda the derived
    entries; brew + notify-downstreams the stable-only pair (#792 — conda is
    rc-inclusive, ADR-0064); gh-release the one the RC guard keeps."""
    assert publish_mod.names() == config.ENDPOINTS
    assert [a.name for a in publish_mod.ADAPTERS if a.stage == "derived"] == [
        "brew",
        "notify-downstreams",
        "conda",
        "zed",
    ]
    assert [a.name for a in publish_mod.ADAPTERS if not a.external] == ["gh-release"]
    # conda is external (a -release-rc live-fire stays gh-release-only) but NOT
    # stable_only — a plain prerelease still publishes it (rc-inclusive). zed is
    # stable_only (the registry serves stable versions, ADR-0068), like brew.
    assert [a.name for a in publish_mod.ADAPTERS if a.stable_only] == [
        "brew",
        "notify-downstreams",
        "zed",
    ]
    # The repo-reading endpoints: brew (asset URLs) + notify-downstreams
    # (dispatch payload) + conda (per-repo channel root) + zed (submodule rev) —
    # the verb resolves the source slug only for these.
    assert [a.name for a in publish_mod.ADAPTERS if a.needs_repo] == [
        "brew",
        "notify-downstreams",
        "conda",
        "zed",
    ]
    # The marketplace endpoints are external (RC-guarded) release-stage entries.
    assert {"vscode-marketplace", "open-vsx"} <= {a.name for a in publish_mod.ADAPTERS}
    for name in ("vscode-marketplace", "open-vsx"):
        adapter = publish_mod.adapter_for(name)
        assert adapter is not None and adapter.external and adapter.stage == "release"


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
    """The cascade endpoint is derived: gh-release must land the release
    before the downstreams are told to rebuild against it (#792)."""
    dispatched = publish_mod.plan(
        _notify_artifacts(), prerelease=False, live_fire=False
    )
    order = [d.adapter.name for d in dispatched]
    assert order == ["gh-release", "notify-downstreams"]
    assert all(d.skip is None for d in dispatched)


def test_plan_notify_downstreams_alone_refuses_without_an_unskipped_gh_release():
    """notify-downstreams tells the downstreams to rebuild against this
    release; without an unskipped gh-release in the plan it would notify them
    of a release that never landed on GitHub — a hard plan refusal, mirroring
    the brew→gh-release invariant (#792)."""
    artifacts = _artifacts(
        {"parser": {"endpoints": ["notify-downstreams"], "downstreams": ["a/b"]}}
    )
    with pytest.raises(ReleaseError, match="notify-downstreams tells the downstream"):
        publish_mod.plan(artifacts, prerelease=False, live_fire=False)


def test_plan_notify_downstreams_skipped_never_trips_the_gh_release_invariant():
    """The invariant reads the UNSKIPPED set: a prerelease skips
    notify-downstreams, so a plan without gh-release is valid — the endpoint
    fires no cascade to strand."""
    artifacts = _artifacts(
        {"parser": {"endpoints": ["notify-downstreams"], "downstreams": ["a/b"]}}
    )
    dispatched = publish_mod.plan(artifacts, prerelease=True, live_fire=False)
    assert dispatched[0].skip == publish_mod.SKIP_NOTIFY_PRERELEASE


def test_plan_prerelease_skips_notify_downstreams():
    """Fires on REAL releases only: a plain prerelease notifies no one — its
    own stated reason, distinct from brew's stable-only skip (#792)."""
    dispatched = publish_mod.plan(_notify_artifacts(), prerelease=True, live_fire=False)
    verdicts = {d.adapter.name: d.skip for d in dispatched}
    assert verdicts["gh-release"] is None
    assert verdicts["notify-downstreams"] == publish_mod.SKIP_NOTIFY_PRERELEASE


def test_plan_live_fire_skips_notify_downstreams_via_rc_guard():
    """An rc live-fire cut skips it as an external endpoint — the RC guard
    wins over the stable-only skip (checked first), keeping only gh-release."""
    dispatched = publish_mod.plan(_notify_artifacts(), prerelease=True, live_fire=True)
    verdicts = {d.adapter.name: d.skip for d in dispatched}
    assert verdicts["gh-release"] is None
    assert verdicts["notify-downstreams"] == publish_mod.SKIP_RC_GUARD


def test_plan_unknown_endpoint_is_a_hard_error_naming_the_known_set():
    """The config boundary already rejects unknown names; the registry
    re-refuses for hand-built artifacts — never a quiet skip."""
    rogue = config.Artifact(name="ext", endpoints=("zed-extensions",))
    with pytest.raises(ReleaseError, match="unknown endpoint") as err:
        publish_mod.plan([rogue], prerelease=False, live_fire=False)
    assert "gh-release, crates, pypi, npm, vscode-marketplace, open-vsx, brew" in str(
        err.value
    )


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


# --------------------------------------------------------------------------
# --endpoint selector (ARF02-WS01 #1000, ADR-0070) — a plan-level filter
# --------------------------------------------------------------------------


def _seed_artifacts():
    """The ADR-0070 motivating shape: one repo whose sibling artifacts declare
    the channel endpoint (conda) alongside irreversible third-party registries
    (crates, npm) — all fired today by the same event."""
    return _artifacts(
        {
            "lexd": {"endpoints": ["gh-release", "crates", "conda"]},
            "lex-wasm": {"endpoints": ["npm"]},
        }
    )


def test_plan_absent_selector_fires_the_full_plan():
    """`selector=None` — what an absent --endpoint parses to — is today's
    behavior EXACTLY: nothing is selector-skipped (ADR-0070)."""
    dispatched = publish_mod.plan(
        _seed_artifacts(), prerelease=True, live_fire=False, selector=None
    )
    assert all(d.skip is None for d in dispatched)


def test_plan_selector_seeds_the_channel_without_collateral():
    """The ADR-0070 headline: a plain prerelease + `--endpoint gh-release
    --endpoint conda` publishes the complete Release and the .conda, while the
    live third-party registries record their OWN selector skip — the seed the
    Artifact channel was previously unable to make without collateral."""
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
    # The skip is DATA on the plan, so the preview states it before anything
    # external happens — the distinct reason, not the RC guard's.
    assert publish_mod.SKIP_SELECTOR != publish_mod.SKIP_RC_GUARD


def test_plan_selector_cannot_deselect_gh_release():
    """gh-release IS the Release, not a distribution channel: the selector
    narrows distribution only, so deselecting it is refused rather than
    producing the partial release ADR-0009 exists to prevent."""
    with pytest.raises(ReleaseError, match="cannot deselect `gh-release`"):
        publish_mod.plan(
            _seed_artifacts(), prerelease=True, live_fire=False, selector=["conda"]
        )


def test_plan_selector_unknown_endpoint_is_loud_and_names_the_known_set():
    """A misspelling must never be a silent no-op that publishes nothing
    (ADR-0070): the closed registry validates the selector."""
    with pytest.raises(ReleaseError, match="unknown endpoint") as err:
        publish_mod.plan(
            _seed_artifacts(),
            prerelease=True,
            live_fire=False,
            selector=["gh-release", "conda-forge"],
        )
    assert "`conda-forge`" in str(err.value)
    # Derived from the registry, not hard-coded: the point is that the known set
    # is NAMED, not that it has a particular order — a new endpoint must not
    # break this test.
    assert ", ".join(publish_mod.names()) in str(err.value)


def test_plan_selector_undeclared_endpoint_is_loud():
    """The silent no-op in its most confusing form: a registry-VALID endpoint
    no artifact declares would publish everything but what was asked for."""
    with pytest.raises(ReleaseError, match="which no artifact in this repo declares"):
        publish_mod.plan(
            _seed_artifacts(),
            prerelease=True,
            live_fire=False,
            selector=["gh-release", "pypi"],
        )


def test_plan_selector_derived_endpoint_without_its_base_is_refused():
    """Selecting a derived endpoint whose base is absent from the plan is
    REFUSED, not silently repaired — ADR-0009's release-before-derived
    ordering holds under the selector (the conda→gh-release invariant)."""
    artifacts = _artifacts({"lexd": {"endpoints": ["conda", "crates"]}})
    with pytest.raises(ReleaseError, match="a conda endpoint publishes"):
        publish_mod.plan(
            artifacts, prerelease=True, live_fire=False, selector=["conda"]
        )


def test_plan_selector_intersects_with_the_rc_guard():
    """The guards compose by INTERSECTION: a -release-rc cut still skips every
    external endpoint including a SELECTED conda, so the selector can never
    resurrect a live-fire rehearsal into a real publish (a seed uses an
    ordinary prerelease tag). The guard's reason is the one stated."""
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
    """A selected stable_only endpoint on a prerelease keeps its OWN reason:
    the selector adds skips, it never removes one."""
    artifacts = _artifacts({"lex": {"endpoints": ["gh-release", "brew", "crates"]}})
    dispatched = publish_mod.plan(
        artifacts, prerelease=True, live_fire=False, selector=["gh-release", "brew"]
    )
    verdicts = {d.adapter.name: d.skip for d in dispatched}
    assert verdicts["brew"] == publish_mod.SKIP_STABLE_ONLY
    assert verdicts["crates"] == publish_mod.SKIP_SELECTOR


def test_plan_selector_keeps_the_two_stage_ordering():
    """The selector is a FILTER, not a reordering: release-stage endpoints
    still precede derived ones, skips included (story 35)."""
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
                # cargo metadata renders `publish = false` as `[]`; the
                # publish-anywhere default is `null`.
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
    """A lib's test helper depending back on the lib is legal and must not
    read as a publish cycle — dev-dependencies are excluded."""
    metadata = json.loads(
        _cargo_metadata({"core": [], "helper": ["core"]}, dev={"core": ["helper"]})
    )
    assert publish_mod.crates_publish_order(metadata) == ("core", "helper")


def test_crates_publish_order_excludes_publish_false_members():
    """A `publish = false` workspace member (test helper, example crate) must
    not appear in the derived order — a real publish would abort on it
    (issue #849, found by the standout ADP02-WS08 cutover). Dependents may
    still dev-depend on it; that link is already excluded as a dev-dep, and
    the member itself is dropped even when publishable crates sort after it."""
    metadata = json.loads(
        _cargo_metadata(
            {"app": ["core"], "core": [], "test-helper": ["core"]},
            unpublished={"test-helper"},
        )
    )
    assert publish_mod.crates_publish_order(metadata) == ("core", "app")


def test_crates_publish_order_keeps_registry_restricted_members():
    """`publish = ["some-registry"]` restricts WHERE a crate may go; only the
    empty list (`publish = false`) means never-publish and is excluded."""
    metadata = json.loads(_cargo_metadata({"core": []}))
    metadata["packages"][0]["publish"] = ["my-registry"]
    assert publish_mod.crates_publish_order(metadata) == ("core",)


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
# npm — the staged tarball IS the artifact, no rebuild, publish-over-existing
# is success (TOL02-WS12 #788)
# --------------------------------------------------------------------------


def _stage_npm_tarball(tmp_path, name):
    dist = tmp_path / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    (dist / name).write_bytes(b"tgz")
    return dist / name


def test_npm_publishes_the_staged_tarball_without_rebuilding(tmp_path):
    # The declared npm package name (`product-name`) flattens to the `npm pack`
    # tarball name (`@lex-fmt/lex-wasm` -> `lex-fmt-lex-wasm-<version>.tgz`),
    # and that same declaration scopes the publish to THIS artifact's tarball.
    (artifact,) = _artifacts(
        {"wasm": {"product-name": "@lex-fmt/lex-wasm", "endpoints": ["npm"]}}
    )
    tarball = _stage_npm_tarball(tmp_path, "lex-fmt-lex-wasm-1.2.3.tgz")
    probe = SeamRecorder()
    req = _request(
        tmp_path,
        artifact,
        env={"NPM_TOKEN": "npm-tok"},  # looked up under the secret name
        probe=probe,
    )

    published = publish_mod._publish_npm(req)

    argv, cwd, env = probe.calls[0]
    # the STAGED tarball (an absolute path), never a rebuild from a source tree
    assert argv == ("npm", "publish", str(tarball), "--ignore-scripts")
    assert cwd == tmp_path
    # ...and the token is fed to npm under the var npm reads (NODE_AUTH_TOKEN),
    # not the secret name — the two-vocabulary indirection secretreq owns.
    assert env == {"NODE_AUTH_TOKEN": "npm-tok"}
    assert published.actions == (
        "published @lex-fmt/lex-wasm 1.2.3 (lex-fmt-lex-wasm-1.2.3.tgz)",
    )


def test_npm_missing_staged_tarball_is_a_loud_refusal(tmp_path):
    # No tarball staged -> the bundle stage never ran (or the name mismatched);
    # publish refuses loudly rather than a silent no-op or scanning the tree.
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


# --------------------------------------------------------------------------
# vscode-marketplace / open-vsx — per-target .vsix publish (external, RC-guarded)
# --------------------------------------------------------------------------


def _stage_vsix(assets_dir: Path, *names: str) -> None:
    """Drop empty per-target .vsix files (plus a non-vsix asset) in the staged
    tree — the bundle stage's vsix-composition output the endpoints publish."""
    assets_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        (assets_dir / name).write_bytes(b"PK\x03\x04")  # a zip magic; content unused
    (assets_dir / "ext-linux-x64.tar.gz").write_bytes(b"")  # never a .vsix upload


def test_vsix_uploads_selects_only_the_vsix_files():
    names = ["ext-darwin-arm64.vsix", "ext-win32-x64.vsix", "ext-linux-x64.tar.gz"]
    assert publish_mod.vsix_uploads(names, "ext") == (
        "ext-darwin-arm64.vsix",
        "ext-win32-x64.vsix",
    )


def test_vsix_uploads_scopes_to_the_artifact_never_a_sibling():
    # A multi-artifact release coalesces every artifact's .vsix into one
    # assets_dir; publish must ship only THIS artifact's outputs. Both the
    # `<artifact>-` prefix AND the `<vsce-target>` middle must match — a
    # sibling extension's .vsix (or one whose name merely starts the same) is
    # never shipped under this artifact's endpoint/token.
    names = [
        "ext-darwin-arm64.vsix",  # this artifact
        "other-darwin-arm64.vsix",  # a sibling extension
        "ext-9.9.9.vsix",  # `ext-`-prefixed but not a vsce target middle
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

    # One `npm exec -- vsce publish` per staged .vsix, sorted, --packagePath at
    # the staged path; vsce rides `npm exec` (the node_modules/.bin dep).
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
    # Runs from the npm leg dir — vsce reads that leg's package.json manifest.
    assert cwd0 == tmp_path / "editors/vscode"
    # Token rides the env under the var vsce reads (VSCE_PAT), never argv.
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
    # ovsx rides `npm exec` and takes the .vsix positionally (no --packagePath),
    # token under OVSX_PAT; runs from the npm leg dir.
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
    # The ovsx path shares the idempotent-resume rule (already-published stderr
    # is success) — covered independently of vscode-marketplace so a regression
    # in the ovsx argv/token leg cannot hide behind the vsce tests.
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
    # Both marketplace adapters run from the npm leg dir (vsce/ovsx are the
    # extension's node_modules/.bin devDependencies, and `vsce publish` reads
    # the leg's package.json). With a token present but no npm leg mapped, the
    # leg resolution is a loud refusal — never a silent run from req.root.
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
    """A -release-rc live-fire cut keeps ONLY gh-release: the two marketplace
    endpoints are external, so the RC guard skips them (rc = gh-release only —
    the WS13 acceptance: marketplace honors the RC guard)."""
    artifacts = _artifacts(
        {"ext": {"endpoints": ["gh-release", "vscode-marketplace", "open-vsx"]}}
    )
    dispatched = publish_mod.plan(artifacts, prerelease=True, live_fire=True)
    live = {d.adapter.name for d in dispatched if d.skip is None}
    assert live == {"gh-release"}
    for name in ("vscode-marketplace", "open-vsx"):
        skip = next(d.skip for d in dispatched if d.adapter.name == name)
        assert skip == publish_mod.SKIP_RC_GUARD


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


def test_brew_refuses_an_unresolved_source_repo(tmp_path):
    """The verb resolves the source slug for a live needs_repo dispatch; a
    direct caller that omits it gets a loud ReleaseError (not a strippable
    assert) — the formula's asset URLs need the `owner/name`."""
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


#: The no-build "tag is the release" map (issue #745): endpoints only —
#: preflight plans an empty matrix and no bundle stage for it.
TAG_ONLY_TOML = """
[artifacts.lex]
endpoints = ["gh-release"]
"""


def test_publish_verb_accepts_the_empty_matrix_skipped_results(
    tmp_path, monkeypatch, capsys
):
    """The confirmed #745 chain shape end-to-end at the verb: the composed
    caller passes build=bundle=skipped (the if-skipped wf-build caller job's
    result, canary-confirmed) plus the plan facts verbatim — an empty matrix
    and a bundle-less stages list — and publish proceeds to gh-release."""
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
    # The tag IS the release: a notes-only GH release, no staged assets, no
    # external endpoint touched.
    assert ("create", "v1.2.3", str(tmp_path / "RELEASE_NOTES.md"), False) in (
        ghio.calls
    )
    assert not any(call[0] == "upload" for call in ghio.calls)
    assert recorder.calls == []
    assert "published 1.2.3" in capsys.readouterr().out


def test_publish_verb_still_refuses_a_live_skipped_build(tmp_path, monkeypatch, capsys):
    """A NON-empty matrix (live build) with a skipped result stays blocked —
    liveness comes from the plan fact, never from the result string."""
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
    """failure/cancelled always block, live or not: a non-live stage's
    tolerance covers exactly the legitimate skip, never a broken run."""
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
    """The pre-#745 invocation (no --matrix/--stages) is the strict
    contract: skipped build/bundle refuse — a caller that states no plan
    never weakens the gate."""
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
    """A garbled plan fact dies at the gate, before any read or dispatch."""
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


def test_publish_selector_skips_need_no_tokens_and_dispatch_nothing(
    tmp_path, monkeypatch, capsys
):
    """The verb end-to-end under `--endpoint gh-release` (ADR-0027 REPO_TOML
    declares gh-release/crates/brew): a STABLE cut publishes the Release and
    nothing else — no cargo publish, no tap push, and (like the RC guard's
    skips) the selector-skipped endpoints require NO tokens, since token
    validation reads the unskipped set."""
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
        env={},  # no tokens: crates/brew are selector-skipped
    )

    assert rc == 0
    assert recorder.calls == []  # no cargo publish, no ruby -c
    assert [c[0] for c in gitio.calls if c[0] != "root"] == []  # no tap push
    out = capsys.readouterr().out
    assert out.count("skipped: --endpoint selector") == 2  # crates + brew
    assert "[gh-release]" in out  # the Release still fired


def test_publish_selector_refusal_dispatches_nothing(tmp_path, monkeypatch, capsys):
    """A bad selection is refused at PLAN time — before any endpoint runs, so
    a typo'd seed touches nothing external (ADR-0070)."""
    _publish_repo(tmp_path, monkeypatch, assets=[f"lex-{MAC_ARM}.tar.gz"])
    recorder = SeamRecorder()
    ghio = FakeGh(exists=False)
    gitio = FakeGit(root=tmp_path)

    rc = release_verb.run_publish(
        _spec("1.2.3"),
        build_result="success",
        bundle_result="success",
        sign_result="skipped",
        endpoint_selector=["crates"],  # deselects gh-release — the Release
        run_cmd=recorder,
        probe=recorder,
        ghio=ghio,
        gitio=gitio,
        env={"CARGO_REGISTRY_TOKEN": "t"},
    )

    assert rc == 1
    assert "cannot deselect `gh-release`" in capsys.readouterr().err
    assert recorder.calls == []
    assert ghio.calls == []  # no release created


def test_publish_cli_endpoint_is_repeatable_and_absent_means_the_full_plan(
    monkeypatch,
):
    """The click boundary parses --endpoint to a VALUE (ADR-0030): repeated
    flags become the ordered selection; an ABSENT flag is None — never an
    empty selection that would publish nothing."""
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


# --------------------------------------------------------------------------
# Missing pixi-managed endpoint tools (#801, TOL02-WS17 holes 1–3) — the
# publish-side loud reconcile remediation
# --------------------------------------------------------------------------


def _raise_missing_binary(argv, cwd, env=None):
    """An Exec seam whose tool is absent from the runner: what execrun raises
    when argv[0] resolves to nothing (cause=missing-binary, no rc)."""
    raise execrun.ExecError(
        [str(a) for a in argv],
        rc=None,
        stderr=f"[Errno 2] No such file or directory: {argv[0]!r}",
        cause=execrun.CAUSE_MISSING_BINARY,
    )


def test_missing_twine_gets_the_reconcile_remedy(tmp_path, monkeypatch, capsys):
    """TOL02-WS17 open hole 2, closed by #801: the pypi endpoint dying on a
    missing `twine` names the python-release-deps block's COMMITTING install
    reconcile — never a run-time install (#582), never a raw 127."""
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
    """The npm endpoint's probe dying on a missing `npm` (the node-deps block
    absent from the runner) translates at the dispatch loop exactly like the
    run_cmd seam — the missing-binary launch failure raises through
    check=False probes too (execrun's OSError path)."""
    _publish_repo(
        tmp_path,
        monkeypatch,
        toml="""
[artifacts.pkg]
endpoints = ["npm"]
""",
        # The staged tarball IS the artifact (WS12); stage it so the npm probe
        # is REACHED — the missing-binary death is then what the remedy catches.
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


# --------------------------------------------------------------------------
# notify-downstreams adapter (TOL02-WS16 #792)
# --------------------------------------------------------------------------


def test_notify_downstreams_fires_one_dispatch_per_downstream(tmp_path):
    """The cascade's happy path: one `upstream-release` repository_dispatch
    per declared downstream, each carrying the source repo/tag/version/artifact
    in its client payload, authenticated by the cross-repo PAT."""
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
    """The verb resolves the source slug for a live needs_repo dispatch; a
    direct caller that omits it gets a loud ReleaseError (not a strippable
    assert, not a null-repo payload) — the payload names the upstream the
    downstreams rebuild against."""
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
    """The ambient GITHUB_TOKEN cannot dispatch cross-repo, so a missing
    DOWNSTREAM_DISPATCH_TOKEN is a loud refusal — never a silent no-notify."""
    artifact = _notify_artifacts()[0]
    ghio = FakeGh()
    req = _request(tmp_path, artifact, version="1.2.3", env={}, ghio=ghio)
    with pytest.raises(ReleaseError, match="DOWNSTREAM_DISPATCH_TOKEN"):
        publish_mod._publish_notify_downstreams(req)
    assert not [c for c in ghio.calls if c[0] == "dispatch"]


def test_notify_downstreams_secret_mirrors_the_derivation_authority():
    """The endpoint's secret name IS its `secretreq.ENDPOINT_SECRETS` entry —
    gh-setup syncs it and preflight validates its presence from that one map."""
    assert (
        publish_mod.NOTIFY_SECRET
        == (secretreq_mod.ENDPOINT_SECRETS["notify-downstreams"][0])
    )
    adapter = publish_mod.adapter_for("notify-downstreams")
    assert adapter is not None
    assert adapter.secrets == secretreq_mod.ENDPOINT_SECRETS["notify-downstreams"]
    assert adapter.stable_only and adapter.stage == "derived"


# --------------------------------------------------------------------------
# conda (derived) — the Artifact channel producer (ARF01-WS01 #950)
# --------------------------------------------------------------------------

LINUX_ARM = "aarch64-unknown-linux-gnu"
WIN = "x86_64-pc-windows-msvc"
MUSL = "x86_64-unknown-linux-musl"


def test_conda_subdir_maps_served_and_drops_unserved():
    """The closed four-subdir matrix (ADR-0064): the served triples map, the
    unserved ones (osx-64, musl) are None — no invented subdir, matching
    today's `provision` refusal for Intel-mac / musl."""
    assert publish_mod.conda_subdir(MAC_ARM) == "osx-arm64"
    assert publish_mod.conda_subdir(LINUX) == "linux-64"
    assert publish_mod.conda_subdir(LINUX_ARM) == "linux-aarch64"
    assert publish_mod.conda_subdir(WIN) == "win-64"
    assert publish_mod.conda_subdir(MAC_X64) is None  # osx-64 unserved
    assert publish_mod.conda_subdir(MUSL) is None  # musl unserved


def test_conda_assets_selects_served_archives_by_known_name():
    """The endpoint repackages the release stage's KNOWN archive names
    (`<artifact>-<triple>.tar.gz`/`.zip`), not a scrape: served triples land
    under their subdir; unserved archives and non-archives are dropped."""
    names = [
        f"lex-{MAC_ARM}.tar.gz",
        f"lex-{LINUX}.tar.gz",
        f"lex-{WIN}.zip",
        f"lex-{MAC_X64}.tar.gz",  # osx-64 — unserved, dropped
        f"lex-{MUSL}.tar.gz",  # musl — unserved, dropped
        "lex-1.0.0.whatever",  # not an archive
        "sibling-x86_64-pc-windows-msvc.zip",  # other artifact's prefix
    ]
    assets = publish_mod.conda_assets("lex", names)
    assert assets == {
        "osx-arm64": (MAC_ARM, f"lex-{MAC_ARM}.tar.gz"),
        "linux-64": (LINUX, f"lex-{LINUX}.tar.gz"),
        "win-64": (WIN, f"lex-{WIN}.zip"),
    }


def test_conda_package_name_is_the_lowercased_main_binary():
    """The conda package name doubles as the consumer's `[artifact-deps.<key>]`
    key (ADR-0064) — the artifact's main-binary name, lowercased to the conda
    package-name vocabulary."""
    artifact = _artifacts({"lex": {"endpoints": ["conda"], "main-binary": "LexD"}})[0]
    assert publish_mod.conda_package_name(artifact) == "lexd"


def test_conda_package_name_rejects_a_conda_invalid_derived_name():
    """A derived name outside the conda vocabulary — a scoped wasm-pack identity
    (`@scope/name`) or a spaced `product-name` — is a loud ReleaseError pointing
    at the config fix, never handed to rattler-build as a doomed build."""
    scoped = _artifacts({"lex": {"endpoints": ["conda"], "main-binary": "@scope/lex"}})[
        0
    ]
    with pytest.raises(ReleaseError, match="not a valid conda package name"):
        publish_mod.conda_package_name(scoped)
    spaced = _artifacts({"lex": {"endpoints": ["conda"], "product-name": "My Tool"}})[0]
    with pytest.raises(ReleaseError, match="not a valid conda package name"):
        publish_mod.conda_package_name(spaced)


def test_render_conda_recipe_repackages_the_prebuilt_binary():
    """The recipe extracts the local release archive and copies the prebuilt
    binary onto PATH under $PREFIX — unix into bin, windows into Scripts (the
    layout is data, the single-runner repackage runs the copy in the host
    shell)."""
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
    # The archive path is quoted (survives spaces / `#`) — see the as_posix +
    # quote fix so a Windows-native or spaced staging path stays one scalar.
    assert '- path: "/stage/lexd-aarch64-apple-darwin.tar.gz"' in unix
    assert 'cp "lexd" "${PREFIX}/bin/lexd"' in unix
    # Relocation is OFF: the endpoint repackages a PREBUILT, already-SIGNED
    # binary linking only system libraries — there are no conda-prefix paths to
    # relocate, the default relink needs a per-OS toolchain the single runner
    # lacks (macOS install_name_tool on Linux, #1052), and rewriting the
    # Mach-O would invalidate the sign stage's signature.
    assert yaml.safe_load(unix)["build"]["dynamic_linking"] == {
        "binary_relocation": False
    }
    # windows layout: the .exe copied into Scripts (on the win conda PATH).
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
    """The path scalar is JSON-escaped (a JSON string IS a valid YAML 1.2
    double-quoted scalar), so a staging path bearing a `"` or `\\` renders as
    valid YAML that parses back to the EXACT path — bare-quote concatenation
    would break the recipe or silently re-point the source."""
    weird = '/weird/pa"th\\dir/lexd.tar.gz'
    recipe = publish_mod.render_conda_recipe(
        package="lexd",
        version="1.2.3",
        archive_path=weird,
        source_binary="lexd",
        install_dir="bin",
        install_binary="lexd",
    )
    # Round-trips through a real YAML parser to the exact input path.
    doc = yaml.safe_load(recipe)
    assert doc["source"][0]["path"] == weird


class _CondaBuildRecorder(SeamRecorder):
    """A recorded Exec seam that also MATERIALIZES rattler-build's output — a
    `rattler-build build` writes the `.conda` its `--output-dir`/`--target-
    platform` name, so the adapter's post-build glob finds a package (as a live
    build would) without a real build."""

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
            f"lex-{MAC_X64}.tar.gz",  # unserved — never packaged
        ],
    )
    artifact = _artifacts({"lex": {"build": ["rust"], "endpoints": ["conda"]}})[0]
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
    # One build per SERVED subdir (osx-64 dropped), each targeting its subdir.
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
    # One publish (upload + reindex) of the built packages to the per-repo
    # S3 channel, with the S3 endpoint/region/creds on the ENV, never argv.
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
    # The three built .conda files ride as positionals.
    assert sum(1 for a in pub_argv if a.endswith(".conda")) == 3
    # The literal AWS_* names are load-bearing: rattler-build resolves S3
    # config via the AWS SDK credential chain and IGNORES the S3_* names
    # ("Could not determine region from AWS SDK configuration", #1049).
    assert pub_env["AWS_ENDPOINT_URL"] == publish_mod.CONDA_S3_ENDPOINT
    assert pub_env["AWS_REGION"] == "auto"
    assert pub_env["AWS_ACCESS_KEY_ID"] == "chan-key-id"
    assert pub_env["AWS_SECRET_ACCESS_KEY"] == "chan-secret-key"
    # The HMAC secret NEVER rides argv (it is env-only, redactor-registered).
    assert not any("chan-secret-key" in a for a in pub_argv)
    assert any("published 3 package(s)" in a for a in published.actions)


def _conda_publish_target(req):
    """The `--to` channel URL of the single `rattler-build publish` in a run."""
    (pub_argv,) = [
        argv
        for argv, _, _ in req.run_cmd.calls
        if argv[:2] == ("rattler-build", "publish")
    ]
    return pub_argv[pub_argv.index("--to") + 1]


def test_conda_publishes_a_public_repo_to_the_public_bucket(tmp_path):
    # Tier is DERIVED from repo visibility (ADR-0065): a public repo → public
    # bucket, authless-readable downstream.
    req, _ = _conda_request(tmp_path, ghio=FakeGh(private=False))
    publish_mod._publish_conda(req)
    assert _conda_publish_target(req) == "s3://shipit-artifacts-public/acme/widget"
    assert ("private?", "acme/widget") in req.ghio.calls


def test_conda_publishes_a_private_repo_to_the_private_bucket(tmp_path):
    # A private producing repo (phos-shaped) → the private bucket, over the SAME
    # S3-interop rail + write HMAC pair (only the bucket changes, ADR-0065).
    req, _ = _conda_request(tmp_path, ghio=FakeGh(private=True))
    published = publish_mod._publish_conda(req)
    assert _conda_publish_target(req) == "s3://shipit-artifacts-private/acme/widget"
    assert ("private?", "acme/widget") in req.ghio.calls
    assert any("shipit-artifacts-private" in a for a in published.actions)


def test_conda_recipe_source_is_the_bare_binary_name(tmp_path):
    """The rendered build script copies the BARE binary name from the work
    root. The release archive stages the binary under a top-level
    `<artifact>-<triple>/` dir (`bundle._compose_archive`'s contract), but
    rattler-build STRIPS that single top-level dir on extraction, so a
    `<artifact>-<triple>/<binary>` copy source fails `cp: cannot stat` —
    the #1049 seed bug, validated against the real lexd release archive."""
    req, _ = _conda_request(tmp_path)

    publish_mod._publish_conda(req)

    recipe_root = req.assets_dir / publish_mod.CONDA_RECIPE_SCRATCH / req.artifact.name
    # osx-arm64 (unix): the stripped-root binary copied into bin — no
    # `lex-<triple>/` prefix on the cp source.
    unix_recipe = (recipe_root / "osx-arm64" / "recipe.yaml").read_text()
    assert 'cp "lex" "${PREFIX}/bin/lex"' in unix_recipe
    assert f"lex-{MAC_ARM}/lex" not in unix_recipe
    # win-64: the `.exe` at the stripped root, copied into Scripts.
    win_recipe = (recipe_root / "win-64" / "recipe.yaml").read_text()
    assert 'cp "lex.exe" "${PREFIX}/Scripts/lex.exe"' in win_recipe
    assert f"lex-{WIN}/lex.exe" not in win_recipe


def test_conda_scratch_is_namespaced_per_artifact(tmp_path):
    """`assets_dir` is stage-wide, so the recipe/channel scratch trees are
    rooted under the artifact name — a second conda artifact's post-build glob
    must never capture this one's `.conda` files (cross-artifact leak)."""
    req, _ = _conda_request(tmp_path)

    publish_mod._publish_conda(req)

    recipe_root = req.assets_dir / publish_mod.CONDA_RECIPE_SCRATCH
    channel_root = req.assets_dir / publish_mod.CONDA_CHANNEL_SCRATCH
    # The subdir trees live UNDER `<scratch>/<artifact>/`, not directly under
    # the shared `<scratch>/` — so a sibling artifact gets its own root.
    assert (recipe_root / req.artifact.name / "osx-arm64" / "recipe.yaml").is_file()
    assert (channel_root / req.artifact.name / "osx-arm64").is_dir()
    assert not (recipe_root / "osx-arm64").exists()
    assert not (channel_root / "osx-arm64").exists()


def test_conda_without_served_archives_refuses(tmp_path):
    """An unserved-only asset set (osx-64 / musl) publishes nothing — a loud
    refusal, never a silent empty publish."""
    req, _ = _conda_request(
        tmp_path, assets=[f"lex-{MAC_X64}.tar.gz", f"lex-{MUSL}.tar.gz"]
    )
    with pytest.raises(ReleaseError, match="no release archive maps to a served"):
        publish_mod._publish_conda(req)


def test_conda_refuses_an_unresolved_source_repo(tmp_path):
    """The per-repo channel root is `<bucket>/<owner/name>`; a direct caller
    that omits the slug gets a loud ReleaseError, never a mis-rooted write."""
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
    """The write HMAC pair is the endpoint's secret — a missing key is one loud
    refusal (the adapter-local belt; the verb validates the plan's tokens
    first)."""
    req, _ = _conda_request(tmp_path, env={"ARTIFACT_CHANNEL_KEY_ID": "chan-key-id"})
    with pytest.raises(ReleaseError, match="ARTIFACT_CHANNEL_SECRET_KEY"):
        publish_mod._publish_conda(req)


def test_conda_secret_pair_mirrors_the_derivation_authority():
    """conda's write-cred pair IS its `secretreq.ENDPOINT_SECRETS` entry — the
    one derivation authority gh-setup syncs and preflight validates."""
    adapter = publish_mod.adapter_for("conda")
    assert adapter is not None
    assert adapter.secrets == secretreq_mod.ENDPOINT_SECRETS["conda"]
    assert (publish_mod.CONDA_KEY_ID_SECRET, publish_mod.CONDA_SECRET_KEY_SECRET) == (
        secretreq_mod.ENDPOINT_SECRETS["conda"]
    )
    # rc-inclusive: external (a -release-rc stays gh-release-only) but not
    # stable_only, so a plain prerelease still publishes.
    assert adapter.stage == "derived" and adapter.external and not adapter.stable_only
    assert adapter.needs_repo


def test_plan_prerelease_keeps_conda_but_a_live_fire_skips_it():
    """rc-inclusive (ADR-0064): a plain prerelease publishes conda (unlike brew,
    which is stable_only); a -release-rc live-fire cut skips it (external)."""
    artifacts = _artifacts({"lex": {"endpoints": ["gh-release", "brew", "conda"]}})
    pre = publish_mod.plan(artifacts, prerelease=True, live_fire=False)
    verdicts = {d.adapter.name: d.skip for d in pre}
    assert verdicts["conda"] is None  # rc-inclusive — published
    assert verdicts["brew"] == publish_mod.SKIP_STABLE_ONLY  # stable-only — skipped
    live = publish_mod.plan(artifacts, prerelease=True, live_fire=True)
    assert {d.adapter.name: d.skip for d in live}["conda"] == publish_mod.SKIP_RC_GUARD


def test_plan_conda_alone_refuses_without_an_unskipped_gh_release():
    """conda inherits the gh-release-must-exist invariant (ADR-0064): a channel
    package for a version with no landed GitHub release advertises a release
    that never existed — a hard plan refusal, mirroring brew/notify."""
    artifacts = _artifacts({"lex": {"endpoints": ["conda"]}})
    with pytest.raises(ReleaseError, match="a conda endpoint publishes"):
        publish_mod.plan(artifacts, prerelease=False, live_fire=False)


def test_plan_conda_live_fire_skip_never_trips_the_gh_release_invariant():
    """The invariant reads the UNSKIPPED set: a -release-rc live-fire skips
    conda (external), so a plan without a separate gh-release still holds —
    conda publishes nothing to strand."""
    artifacts = _artifacts({"lex": {"endpoints": ["gh-release", "conda"]}})
    dispatched = publish_mod.plan(artifacts, prerelease=True, live_fire=True)
    verdicts = {d.adapter.name: d.skip for d in dispatched}
    assert verdicts["conda"] == publish_mod.SKIP_RC_GUARD
    assert verdicts["gh-release"] is None


def test_missing_rattler_build_gets_the_reconcile_remedy(tmp_path, monkeypatch, capsys):
    """A missing `rattler-build` (the rust-release-deps block absent from the
    runner) names the block's COMMITTING install reconcile — never a raw 127,
    the #801 translation applied to the conda endpoint."""
    _publish_repo(
        tmp_path,
        monkeypatch,
        toml="""
[toolchains]
"." = "rust"

[artifacts.lex]
build = ["rust"]
endpoints = ["gh-release", "conda"]
""",
        assets=[f"lex-{MAC_ARM}.tar.gz"],
    )

    def _seam(argv, cwd, env=None):
        # gh-release rides the gh adapter (FakeGh), so the FIRST run_cmd tool is
        # rattler-build — die missing-binary on it.
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
    assert "pixi.toml#shipit-rust-release-deps" in err
    assert "`shipit install --pr`" in err


# --------------------------------------------------------------------------
# zed (derived) — the Zed-extension registry coordinates (TOL03-WS02 #973)
# --------------------------------------------------------------------------


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
        "../zed-registry",  # path traversal out of the scratch dir
        "..",
        "/tmp/x",  # absolute path
        "foo/bar",  # a slash — a second path segment / submodule-dir escape
        "foo.bar",  # a dot — blurs the `<id>.extensions-toml` filename
        'x]\nversion = "0"',  # closes the TOML table key + injects a line
        "Lex",  # uppercase is outside the lowercase registry vocabulary
        "with space",
        "-leading",  # must start with an alphanumeric
    ],
)
def test_zed_extension_id_refuses_an_id_outside_the_grammar(ext_id):
    # The id is untrusted repo content used as BOTH a TOML key and a filename,
    # so a non-conforming id is a loud refusal — never a mis-scoped write or a
    # malformed registry row (codex/copilot round-1 finding).
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
    # End to end through the adapter: a malicious extension.toml id never
    # reaches render/write — the scratch dir stays empty on refusal.
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
    # The extensions.toml row keyed by the extension id, with the bumped version…
    assert "[lex]\n" in text
    assert 'submodule = "extensions/lex"\n' in text
    assert 'version = "1.2.3"\n' in text
    # …plus the submodule rev the manual PR advances the id's submodule to.
    assert "github.com/lex-fmt/zed-lex @ v1.2.3" in text


def test_zed_renders_the_registry_entry_and_reports_the_manual_step(tmp_path):
    """The happy path: read the extension id from extension.toml, render the
    registry row + submodule rev into a scratch subdir, and report the manual
    PR step. No cross-repo push, no tool invocation (ADR-0068)."""
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
    # No effectful command ran — the endpoint only renders + reports.
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
    """The submodule rev names github.com/<owner/name>@<tag>; a direct caller
    that omits the repo gets a loud refusal, never a null-source row."""
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
    """The zed composition ships extension.toml as the required core; its
    absence at publish is a loud refusal (run bundle first), never a skip."""
    artifact = _zed_artifacts()[0]  # no extension.toml written
    req = _request(
        tmp_path,
        artifact,
        entries=_entries({".": "rust"}),
        version="1.2.3",
        repo="lex-fmt/zed-lex",
    )
    with pytest.raises(ReleaseError, match="cannot read"):
        publish_mod._publish_zed(req)


def test_zed_declares_no_secret_and_is_a_derived_stable_only_endpoint():
    """The endpoint renders the manual-PR coordinates and never pushes into the
    foreign registry, so it declares NO secret — its `secretreq.ENDPOINT_SECRETS`
    entry is empty, like gh-release (ADR-0068)."""
    assert secretreq_mod.ENDPOINT_SECRETS["zed"] == ()
    adapter = publish_mod.adapter_for("zed")
    assert adapter is not None
    assert adapter.secrets == ()
    # derived, stable_only (registry serves stable), external (rc = gh only),
    # needs_repo (the submodule rev names the source slug @ tag).
    assert adapter.stage == "derived"
    assert adapter.stable_only and adapter.external and adapter.needs_repo


def test_plan_prerelease_skips_zed_and_a_live_fire_skips_it_too():
    """stable_only: a plain prerelease renders no registry entry (the registry
    serves stable versions); external: a -release-rc live-fire is gh-only."""
    artifacts = _artifacts({"zed-lex": {"endpoints": ["gh-release", "zed"]}})
    pre = publish_mod.plan(artifacts, prerelease=True, live_fire=False)
    assert {d.adapter.name: d.skip for d in pre}[
        "zed"
    ] == publish_mod.SKIP_ZED_PRERELEASE
    live = publish_mod.plan(artifacts, prerelease=True, live_fire=True)
    assert {d.adapter.name: d.skip for d in live}["zed"] == publish_mod.SKIP_RC_GUARD


def test_plan_zed_alone_needs_no_gh_release():
    """Unlike brew/notify/conda, zed references the `release prepare` tag (not
    gh-release assets, ADR-0068), so a zed-only map is a valid plan — the
    gh-release-must-exist invariant does not extend to it."""
    artifacts = _artifacts({"zed-lex": {"endpoints": ["zed"]}})
    dispatched = publish_mod.plan(artifacts, prerelease=False, live_fire=False)
    assert [d.adapter.name for d in dispatched] == ["zed"]
    assert dispatched[0].skip is None
