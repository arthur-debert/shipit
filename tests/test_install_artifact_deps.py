"""The consumer-side Artifact-channel projection (ARF01-WS02 #952).

The pure, network-free core: ``[artifact-deps]`` typed values →
tier-derived channel URL → managed pixi :class:`~shipit.install.units.Unit`
blocks. Everything here runs on VALUES — no filesystem beyond a splice
round-trip, no network (visibility is passed in as an already-resolved
boolean), matching the acceptance criterion "projection exercised without
touching the network".

Coverage: the URL derivation and its public/private tier gate; the projected
block structure and its TOML validity when spliced into a seeded manifest; the
idempotent reconcile-to-noop and the single-UPDATE version bump; and a drift
guard tying the consumer bucket to the producer's.
"""

import tomllib

import pytest

from shipit import config
from shipit.install import artifactdeps as ad
from shipit.install import reconcile as irec
from shipit.install import splice
from shipit.install import units as iunits


def _dep(package="lexd", repo="lex-fmt/lex", version="0.19.3", feature=None):
    return config.ArtifactDep(
        package=package, repo=repo, version=version, feature=feature
    )


# --------------------------------------------------------------------------
# Tier derivation from visibility (ADR-0065) — public HTTPS vs private S3-interop
# --------------------------------------------------------------------------


def test_public_channel_url_is_the_authless_per_repo_https_root():
    assert (
        ad.public_channel_url("lex-fmt/lex")
        == "https://storage.googleapis.com/shipit-artifacts-public/lex-fmt/lex"
    )


def test_public_repo_resolves_to_the_public_url():
    assert ad.channel_url("lex-fmt/lex", private=False) == ad.public_channel_url(
        "lex-fmt/lex"
    )


def test_private_channel_url_is_the_s3_interop_per_repo_root():
    assert (
        ad.private_channel_url("phos/private")
        == "s3://shipit-artifacts-private/phos/private"
    )


def test_private_repo_resolves_to_the_s3_url():
    # WS04: a private producing repo is no longer refused — it resolves to its
    # `s3://` S3-interop channel (ADR-0065), which drives the `[s3-options]`
    # projection below.
    assert ad.channel_url("phos/private", private=True) == ad.private_channel_url(
        "phos/private"
    )


# --------------------------------------------------------------------------
# Projection structure + TOML validity
# --------------------------------------------------------------------------


def test_no_deps_projects_nothing():
    assert ad.project([]) == []


def _project(deps):
    return ad.project([(d, ad.channel_url(d.repo, private=False)) for d in deps])


def test_default_target_projects_a_feature_and_a_default_env_wiring():
    units = _project([_dep()])
    keys = {u.key for u in units}
    assert keys == {
        "pixi.toml#shipit-artifacts",
        ad.ENVIRONMENTS_KEY,
    }
    feat = next(u for u in units if u.key == "pixi.toml#shipit-artifacts")
    inner = feat.desired_inner()
    assert "[feature.shipit-artifacts]" in inner
    assert (
        'channels = ["https://storage.googleapis.com/'
        'shipit-artifacts-public/lex-fmt/lex"]' in inner
    )
    assert "[feature.shipit-artifacts.dependencies]" in inner
    assert 'lexd = "0.19.3"' in inner
    env = next(u for u in units if u.key == ad.ENVIRONMENTS_KEY)
    assert env.anchor == "[environments]"
    assert env.desired_inner() == 'default = ["shipit-artifacts"]'


def test_named_feature_projects_an_isolated_feature_and_env():
    units = _project([_dep(feature="tools")])
    keys = {u.key for u in units}
    assert "pixi.toml#shipit-artifacts-tools" in keys
    env = next(u for u in units if u.key == ad.ENVIRONMENTS_KEY)
    assert env.desired_inner() == 'shipit-artifacts-tools = ["shipit-artifacts-tools"]'


def test_deps_sharing_a_repo_share_one_channel_entry():
    units = _project([_dep(package="lexd"), _dep(package="lexd-lsp")])
    feat = next(u for u in units if u.key == "pixi.toml#shipit-artifacts")
    inner = feat.desired_inner()
    # One channels entry (both pins resolve from the same producing repo)...
    assert inner.count("storage.googleapis.com") == 1
    # ...and both pins present.
    assert 'lexd = "0.19.3"' in inner
    assert 'lexd-lsp = "0.19.3"' in inner


def test_projected_blocks_splice_into_a_seed_manifest_as_valid_toml():
    manifest = iunits.pixi_manifest_seed("downstream")
    units = _project([_dep(), _dep(package="lexd-lsp", feature="tools")])
    for unit in units:
        manifest = splice.splice_block(
            manifest,
            unit.desired_inner(),
            unit.open_marker,
            unit.close_marker,
            unit.anchor,
        )
    parsed = tomllib.loads(manifest)  # must parse — no duplicate tables/keys
    assert parsed["feature"]["shipit-artifacts"]["dependencies"]["lexd"] == "0.19.3"
    assert parsed["environments"]["default"] == ["shipit-artifacts"]
    assert parsed["environments"]["shipit-artifacts-tools"] == [
        "shipit-artifacts-tools"
    ]


def test_dotted_names_are_emitted_as_quoted_toml_keys():
    # A dotted conda package (`ruamel.yaml` is a real one — the producer's
    # vocabulary admits dots) and a dotted `feature` must render as QUOTED keys,
    # else TOML reads the dot as a key-path separator and the one name splits
    # into nested tables/keys — a silently wrong manifest (ARF01-WS02 review).
    units = _project([_dep(package="ruamel.yaml", feature="tools.v2")])
    feat = next(u for u in units if u.key == "pixi.toml#shipit-artifacts-tools.v2")
    inner = feat.desired_inner()
    assert '[feature."shipit-artifacts-tools.v2"]' in inner
    assert '[feature."shipit-artifacts-tools.v2".dependencies]' in inner
    assert '"ruamel.yaml" = "0.19.3"' in inner
    env = next(u for u in units if u.key == ad.ENVIRONMENTS_KEY)
    assert (
        env.desired_inner()
        == '"shipit-artifacts-tools.v2" = ["shipit-artifacts-tools.v2"]'
    )


def test_dotted_names_splice_into_a_seed_manifest_as_valid_toml():
    # The quoted keys survive a splice round-trip: TOML parses the dotted names
    # as SINGLE literal feature/package/env names, not nested tables/keys.
    manifest = iunits.pixi_manifest_seed("downstream")
    units = _project([_dep(package="ruamel.yaml", feature="tools.v2")])
    for unit in units:
        manifest = splice.splice_block(
            manifest,
            unit.desired_inner(),
            unit.open_marker,
            unit.close_marker,
            unit.anchor,
        )
    parsed = tomllib.loads(manifest)
    feature = parsed["feature"]["shipit-artifacts-tools.v2"]
    assert feature["dependencies"]["ruamel.yaml"] == "0.19.3"
    assert parsed["environments"]["shipit-artifacts-tools.v2"] == [
        "shipit-artifacts-tools.v2"
    ]


def test_bare_safe_names_stay_unquoted():
    # Only names that NEED quoting get it — a plain `lexd`/`tools` stays bare so
    # the common case reads cleanly and existing manifests do not churn.
    units = _project([_dep(package="lexd", feature="tools")])
    feat = next(u for u in units if u.key == "pixi.toml#shipit-artifacts-tools")
    inner = feat.desired_inner()
    assert "[feature.shipit-artifacts-tools]" in inner
    assert 'lexd = "0.19.3"' in inner
    assert '"' not in inner.split("dependencies]")[1].split("=")[0]


# --------------------------------------------------------------------------
# Private tier (ADR-0065) — s3:// channel + the validated [s3-options] block
# --------------------------------------------------------------------------


def _project_private(deps):
    return ad.project([(d, ad.channel_url(d.repo, private=True)) for d in deps])


def test_private_dep_channel_is_the_s3_url():
    units = _project_private([_dep(repo="phos/private")])
    feat = next(u for u in units if u.key == "pixi.toml#shipit-artifacts")
    inner = feat.desired_inner()
    assert 'channels = ["s3://shipit-artifacts-private/phos/private"]' in inner


def test_private_dep_projects_the_validated_s3_options_block():
    units = _project_private([_dep(repo="phos/private")])
    s3 = next(u for u in units if u.key == ad.S3_OPTIONS_KEY)
    # Anchor-less: a fresh reserved top-level table appended at EOF.
    assert s3.anchor is None
    inner = s3.desired_inner()
    assert "[s3-options.shipit-artifacts-private]" in inner
    assert 'endpoint-url = "https://storage.googleapis.com"' in inner
    assert 'region = "auto"' in inner
    assert "force-path-style = true" in inner


def test_public_dep_projects_no_s3_options_block():
    # A purely-public consumer never gets the private-tier block.
    units = _project([_dep()])
    assert not any(u.key == ad.S3_OPTIONS_KEY for u in units)


def test_one_s3_options_table_per_distinct_private_bucket():
    # Every private repo shares the single private bucket, so however many
    # private pins are declared, exactly ONE [s3-options.<bucket>] table appears.
    units = _project_private(
        [_dep(package="a", repo="phos/one"), _dep(package="b", repo="phos/two")]
    )
    s3 = next(u for u in units if u.key == ad.S3_OPTIONS_KEY)
    assert s3.desired_inner().count("[s3-options.") == 1


def test_private_blocks_splice_into_a_seed_manifest_as_valid_toml():
    manifest = iunits.pixi_manifest_seed("downstream")
    units = _project_private([_dep(repo="phos/private")])
    for unit in units:
        manifest = splice.splice_block(
            manifest,
            unit.desired_inner(),
            unit.open_marker,
            unit.close_marker,
            unit.anchor,
        )
    parsed = tomllib.loads(manifest)  # must parse — no duplicate tables/keys
    s3 = parsed["s3-options"]["shipit-artifacts-private"]
    assert s3["endpoint-url"] == "https://storage.googleapis.com"
    assert s3["region"] == "auto"
    assert s3["force-path-style"] is True
    assert parsed["feature"]["shipit-artifacts"]["channels"] == [
        "s3://shipit-artifacts-private/phos/private"
    ]


def test_private_projection_embeds_no_credentials():
    # The no-creds negative at the highest SAFE (offline) fidelity: the private
    # channel is genuinely access-controlled precisely because the COMMITTED
    # manifest carries NO credential material — reads need `AWS_ACCESS_KEY_ID` /
    # `AWS_SECRET_ACCESS_KEY` (or `RATTLER_AUTH_FILE`) supplied as ENV VARS out of
    # band (ADR-0065). If the projection ever baked a key in, a consumer would
    # resolve without creds and the access control would be a fiction. (The LIVE
    # no-creds-403 proof against a real GCS bucket is ADR-0065's; see PR Context.)
    manifest = _splice_all(
        iunits.pixi_manifest_seed("x"), _project_private([_dep(repo="phos/private")])
    )
    lowered = manifest.lower()
    for secret_marker in (
        "access-key",
        "access_key",
        "secret-key",
        "secret_key",
        "aws_",
        "rattler_auth",
        "password",
    ):
        assert secret_marker not in lowered


def test_private_s3_options_reconcile_is_idempotent():
    manifest = _splice_all(
        iunits.pixi_manifest_seed("x"), _project_private([_dep(repo="phos/private")])
    )
    s3 = next(
        u
        for u in _project_private([_dep(repo="phos/private")])
        if u.key == ad.S3_OPTIONS_KEY
    )
    current = splice.extract_block(manifest, s3.open_marker, s3.close_marker)
    assert current is not None
    assert (
        irec.decide(
            consumer_hash=config.content_hash(current.encode("utf-8")),
            pristine_hash=config.content_hash(current.encode("utf-8")),
            desired_hash=s3.desired_hash(),
        )
        == irec.NOOP
    )


# --------------------------------------------------------------------------
# Reconcile idempotency + version bump (the managed-block contract)
# --------------------------------------------------------------------------


def _splice_all(manifest, units):
    for unit in units:
        manifest = splice.splice_block(
            manifest,
            unit.desired_inner(),
            unit.open_marker,
            unit.close_marker,
            unit.anchor,
        )
    return manifest


def test_reconcile_to_noop_after_projection_is_idempotent():
    manifest = _splice_all(iunits.pixi_manifest_seed("x"), _project([_dep()]))
    for unit in _project([_dep()]):
        current = splice.extract_block(manifest, unit.open_marker, unit.close_marker)
        assert current is not None
        # present, hash == desired -> NOOP (the re-install no-op)
        assert (
            irec.decide(
                consumer_hash=config.content_hash(current.encode("utf-8")),
                pristine_hash=config.content_hash(current.encode("utf-8")),
                desired_hash=unit.desired_hash(),
            )
            == irec.NOOP
        )


def test_version_bump_is_a_single_update():
    old = _project([_dep(version="0.19.3")])
    new = _project([_dep(version="0.20.0")])
    manifest = _splice_all(iunits.pixi_manifest_seed("x"), old)
    feat_new = next(u for u in new if u.key == "pixi.toml#shipit-artifacts")
    current = splice.extract_block(
        manifest, feat_new.open_marker, feat_new.close_marker
    )
    consumer_hash = config.content_hash(current.encode("utf-8"))
    # A bump re-resolves transparently: consumer==old pristine, desired==new.
    assert (
        irec.decide(
            consumer_hash=consumer_hash,
            pristine_hash=consumer_hash,
            desired_hash=feat_new.desired_hash(),
        )
        == irec.UPDATE
    )
    # And the env wiring is byte-identical across a pure version bump (no churn).
    env_old = next(u for u in old if u.key == ad.ENVIRONMENTS_KEY)
    env_new = next(u for u in new if u.key == ad.ENVIRONMENTS_KEY)
    assert env_old.desired_inner() == env_new.desired_inner()


# --------------------------------------------------------------------------
# Drift guard — the consumer bucket must match the producer's
# --------------------------------------------------------------------------


def test_consumer_bucket_matches_the_producer_endpoint():
    # The `conda` endpoint (WS01) publishes to these buckets over GCS S3-interop;
    # the consumer reads from the same buckets (the public tier over its authless
    # HTTPS mirror, the private tier over the same s3:// interop rail). If either
    # drifts, a consumer resolves against a bucket the producer never wrote to.
    from shipit.release import publish

    assert ad.PUBLIC_ARTIFACT_BUCKET == publish.PUBLIC_ARTIFACT_BUCKET
    assert ad.PRIVATE_ARTIFACT_BUCKET == publish.PRIVATE_ARTIFACT_BUCKET
    assert ad.PUBLIC_CHANNEL_HOST == publish.CONDA_S3_ENDPOINT
    # The private-tier `[s3-options]` endpoint/region are the same GCS-interop
    # constants the producer writes over — the S3 backend on both sides.
    assert ad.S3_OPTIONS_ENDPOINT_URL == publish.CONDA_S3_ENDPOINT
    assert ad.S3_OPTIONS_REGION == publish.CONDA_S3_REGION


# --------------------------------------------------------------------------
# The verb glue — `_artifact_dep_units` (visibility injected; no network)
# --------------------------------------------------------------------------


def _write_config(root, text):
    (root / config.CONFIG_NAME).write_text(text, encoding="utf-8")


def test_verb_projects_public_deps_resolving_visibility_once_per_repo(tmp_path):
    from shipit.verbs import install as verb

    _write_config(
        tmp_path,
        "[artifact-deps.lexd]\n"
        'repo = "lex-fmt/lex"\n'
        'version = "0.19.3"\n'
        "[artifact-deps.lexd-lsp]\n"
        'repo = "lex-fmt/lex"\n'
        'version = "0.19.3"\n',
    )
    calls = []

    def fake_is_private(slug):
        calls.append(slug)
        return False

    units = verb._artifact_dep_units(tmp_path, is_private=fake_is_private)
    assert {u.key for u in units} == {
        "pixi.toml#shipit-artifacts",
        ad.ENVIRONMENTS_KEY,
    }
    # Visibility resolved ONCE per distinct producing repo, never per dep.
    assert calls == ["lex-fmt/lex"]


def test_verb_stays_offline_with_no_artifact_deps(tmp_path):
    from shipit.verbs import install as verb

    _write_config(tmp_path, '[shipit]\nversion = "abc"\n')

    def boom(slug):  # pragma: no cover - must never be called
        raise AssertionError("visibility must not be resolved with no deps")

    assert verb._artifact_dep_units(tmp_path, is_private=boom) == []


def test_verb_projects_a_private_producing_repo_over_s3(tmp_path):
    # WS04: a private producing repo now projects an s3:// channel + the
    # [s3-options] block (was refused with a WS04 pointer in WS02).
    from shipit.verbs import install as verb

    _write_config(
        tmp_path,
        '[artifact-deps.phos-tool]\nrepo = "phos/private"\nversion = "1.0"\n',
    )
    units = verb._artifact_dep_units(tmp_path, is_private=lambda slug: True)
    keys = {u.key for u in units}
    assert ad.S3_OPTIONS_KEY in keys
    feat = next(u for u in units if u.key == "pixi.toml#shipit-artifacts")
    assert "s3://shipit-artifacts-private/phos/private" in feat.desired_inner()


def test_verb_fails_loud_on_a_malformed_entry(tmp_path):
    from shipit.verbs import install as verb

    _write_config(
        tmp_path,
        '[artifact-deps.lexd]\nrepo = "not-a-slug"\nversion = "1.0"\n',
    )
    with pytest.raises(config.ConfigError):
        verb._artifact_dep_units(tmp_path, is_private=lambda slug: False)


def test_verb_degrades_on_a_generally_unreadable_manifest(tmp_path):
    from shipit.verbs import install as verb

    _write_config(tmp_path, "this is not valid toml = = =\n")
    # An unreadable manifest degrades to no artifact units (gather warns), it
    # does not crash install here.
    assert verb._artifact_dep_units(tmp_path, is_private=lambda slug: False) == []
