"""The consumer-side Artifact-channel projection (ARF01-WS02 #952).

The pure, network-free core: ``[artifact-deps]`` typed values →
tier-derived channel URL → managed pixi :class:`~shipit.install.units.Unit`
blocks. Everything here runs on VALUES — no filesystem beyond a splice
round-trip, no network (visibility is passed in as an already-resolved
boolean), matching the acceptance criterion "projection exercised without
touching the network".

Coverage: the URL derivation and its public/private tier gate; the projected
block structure (conda-direct, ADR-0077: DERIVED channels only — no version pin)
and its TOML validity when spliced into a seeded manifest; the idempotent
reconcile-to-noop and the single-UPDATE on a derived-channel change; the
fail-safe `missing_pins` check that the consumer-owned pin is co-located with the
channel; and a drift guard tying the consumer bucket to the producer's.
"""

import tomllib
from pathlib import Path

import pytest

from shipit import config
from shipit.install import apply as iapply
from shipit.install import artifactdeps as ad
from shipit.install import reconcile as irec
from shipit.install import splice
from shipit.install import units as iunits
from shipit.verbs import install as verb

#: The stale Cascade receive-workflow a consumer that already installed it still
#: carries — retired by conda-direct (ADR-0077). The fixture is the last delivered
#: version (a pristine hash in retired-files.toml).
_CASCADE_WORKFLOW_DEST = ".github/workflows/shipit-artifact-cascade.yml"
_CASCADE_PRISTINE = (
    Path(__file__).parent / "data" / "shipit-artifact-cascade-pristine.yml"
)


def _dep(package="lexd", repo="lex-fmt/lex", feature=None):
    # conda-direct (ADR-0077): `{ repo }` (+ optional feature) is the whole
    # declaration — the version is consumer-owned in the artifact's pixi feature.
    return config.ArtifactDep(package=package, repo=repo, feature=feature)


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
    # A CLEAN, self-contained `[feature.shipit-artifacts]` channel table shipit
    # fully owns — anchor-less (header inside the markers), so an UPDATE replaces
    # it in place without orphaning `channels` (#1094 round-3).
    assert feat.anchor is None
    inner = feat.desired_inner()
    assert "[feature.shipit-artifacts]" in inner
    assert (
        'channels = ["https://storage.googleapis.com/'
        'shipit-artifacts-public/lex-fmt/lex"]' in inner
    )
    # conda-direct (ADR-0077): channels only — no version pin (the consumer owns
    # the version as a co-located `[feature.<X>.dependencies]` pin).
    assert "dependencies" not in inner
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
    # One channels entry (both deps resolve from the same producing repo), and no
    # per-package pin — the derived location is de-duped to a single channel.
    assert inner.count("storage.googleapis.com") == 1
    assert "dependencies" not in inner


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
    # conda-direct: the managed feature carries channels only, no `dependencies`.
    assert parsed["feature"]["shipit-artifacts"]["channels"] == [
        "https://storage.googleapis.com/shipit-artifacts-public/lex-fmt/lex"
    ]
    assert "dependencies" not in parsed["feature"]["shipit-artifacts"]
    assert parsed["environments"]["default"] == ["shipit-artifacts"]
    assert parsed["environments"]["shipit-artifacts-tools"] == [
        "shipit-artifacts-tools"
    ]


def test_dotted_feature_names_are_emitted_as_quoted_toml_keys():
    # A dotted `feature` must render as a QUOTED key in the reserved feature table
    # header and the env wiring, else TOML reads the dot as a key-path separator
    # and the one name splits into nested tables/keys — a silently wrong manifest
    # (ARF01-WS02 review). (conda-direct: the package name is no longer projected
    # — it lives in the consumer's own `[dependencies]` — so only the feature/env
    # names still flow through the projection's quoting.)
    units = _project([_dep(package="ruamel.yaml", feature="tools.v2")])
    feat = next(u for u in units if u.key == "pixi.toml#shipit-artifacts-tools.v2")
    # The dotted feature name is quoted in the block's own `[feature."…"]` header.
    inner = feat.desired_inner()
    assert '[feature."shipit-artifacts-tools.v2"]' in inner
    assert "dependencies" not in inner
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
    assert feature["channels"] == [
        "https://storage.googleapis.com/shipit-artifacts-public/lex-fmt/lex"
    ]
    assert parsed["environments"]["shipit-artifacts-tools.v2"] == [
        "shipit-artifacts-tools.v2"
    ]


def test_bare_safe_feature_names_stay_unquoted():
    # Only names that NEED quoting get it — a plain `tools` feature stays bare so
    # the common case reads cleanly and existing manifests do not churn.
    units = _project([_dep(package="lexd", feature="tools")])
    feat = next(u for u in units if u.key == "pixi.toml#shipit-artifacts-tools")
    inner = feat.desired_inner()
    assert "[feature.shipit-artifacts-tools]" in inner  # bare, unquoted header
    assert '[feature."' not in inner
    env = next(u for u in units if u.key == ad.ENVIRONMENTS_KEY)
    assert env.desired_inner() == 'shipit-artifacts-tools = ["shipit-artifacts-tools"]'


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
# The table-redeclaration guard (ARF01-WS04) — a consumer's hand-written
# [s3-options.<bucket>] table must not be redeclared into an unparseable manifest
# --------------------------------------------------------------------------


#: A consumer that already carries the private-tier `[s3-options.<bucket>]` table
#: by hand (the documented manual runbook), so a first splice of the managed
#: block would redeclare it.
_CONSUMER_PIXI_WITH_MANUAL_S3 = iunits.pixi_manifest_seed("downstream") + (
    "\n[s3-options.shipit-artifacts-private]\n"
    'endpoint-url = "https://storage.googleapis.com"\n'
    'region = "auto"\n'
    "force-path-style = true\n"
)


def _reconcile_private(root):
    units = _project_private([_dep(repo="phos/private")])
    state = irec.gather(root, units, irec.load_retired())
    return units, irec.reconcile(units, irec.load_retired(), state)


def test_preexisting_s3_options_table_is_a_table_conflict(tmp_path):
    # The redeclaration guard: a consumer who already declares
    # [s3-options.shipit-artifacts-private] must NOT get the managed s3-options
    # block spliced in — a second identical table header makes pixi.toml
    # unparseable. The consumer's own table stays authoritative.
    (tmp_path / "pixi.toml").write_text(_CONSUMER_PIXI_WITH_MANUAL_S3)
    _units, plan = _reconcile_private(tmp_path)

    assert plan.pixi_table_conflicts == (
        irec.PixiTableConflict(
            unit_key=ad.S3_OPTIONS_KEY,
            tables=("s3-options.shipit-artifacts-private",),
        ),
    )
    # The conflicted block never reaches the plan; the feature + env wiring does.
    keys = {d.unit.key for d in plan.decisions}
    assert ad.S3_OPTIONS_KEY not in keys
    assert ad.ENVIRONMENTS_KEY in keys
    assert "pixi.toml#shipit-artifacts" in keys
    # Warn-only, and worded off the one formatter (never a broken write).
    warnings = verb.format_plan_warnings(plan)
    assert "pixi block skipped" in warnings
    assert "s3-options.shipit-artifacts-private" in warnings


def test_skipping_the_s3_options_conflict_keeps_pixi_toml_parseable(tmp_path):
    # End to end: apply on a conflicted consumer leaves a pixi.toml pixi can
    # still parse, the consumer's own table intact, and no [managed] entry for
    # the skipped block (nothing delivered, so nothing tracked). The feature and
    # env wiring — reserved names, no clash — still land.
    (tmp_path / "AGENTS.md").write_text("# Downstream\n")
    (tmp_path / "pixi.toml").write_text(_CONSUMER_PIXI_WITH_MANUAL_S3)
    _units, plan = _reconcile_private(tmp_path)
    iapply.apply(plan, iapply.MODE_TREE)

    manifest = tomllib.loads((tmp_path / "pixi.toml").read_text(encoding="utf-8"))
    # The consumer's hand-written table is untouched and NOT duplicated.
    assert manifest["s3-options"]["shipit-artifacts-private"]["region"] == "auto"
    assert (tmp_path / "pixi.toml").read_text().count(
        "[s3-options.shipit-artifacts-private]"
    ) == 1
    assert ad.S3_OPTIONS_OPEN not in (tmp_path / "pixi.toml").read_text()
    managed = config.load_managed(config.load(tmp_path / ".shipit.toml"))
    assert ad.S3_OPTIONS_KEY not in managed
    assert ad.ENVIRONMENTS_KEY in managed


def test_a_spliced_s3_options_block_is_not_a_table_conflict(tmp_path):
    # Contrast with a clean consumer: the block IS delivered, and once its
    # markers are in, a re-reconcile reads the table as the block's own (NOOP),
    # never as a conflict with itself.
    (tmp_path / "AGENTS.md").write_text("# Downstream\n")
    (tmp_path / "pixi.toml").write_text(iunits.pixi_manifest_seed("downstream"))
    _units, plan = _reconcile_private(tmp_path)
    assert plan.pixi_table_conflicts == ()
    iapply.apply(plan, iapply.MODE_TREE)

    _again_units, again = _reconcile_private(tmp_path)
    assert again.pixi_table_conflicts == ()
    s3 = next(d for d in again.decisions if d.unit.key == ad.S3_OPTIONS_KEY)
    assert s3.action == irec.NOOP


def test_an_unrelated_consumer_feature_table_is_no_conflict(tmp_path):
    # The guard checks the LEAF table each block declares, not a shared
    # super-table: a consumer `[feature.foo]` sits under the same `[feature]`
    # super-table as the reserved `[feature.shipit-artifacts]`, but is NOT a
    # clash — the feature block must still deliver.
    (tmp_path / "pixi.toml").write_text(
        iunits.pixi_manifest_seed("downstream")
        + '\n[feature.foo.dependencies]\ncmake = "*"\n'
    )
    _units, plan = _reconcile_private(tmp_path)
    assert plan.pixi_table_conflicts == ()
    feat = next(d for d in plan.decisions if d.unit.key == "pixi.toml#shipit-artifacts")
    assert feat.action == irec.ADD


def _reconcile_public(root, deps):
    units = ad.project([(d, ad.channel_url(d.repo, private=False)) for d in deps])
    state = irec.gather(root, units, irec.load_retired())
    return units, irec.reconcile(units, irec.load_retired(), state)


def test_channel_merges_into_the_shared_feature_table_not_skipped(tmp_path):
    # Major 1 (#1094): the consumer co-locates the pin in
    # `[feature.shipit-artifacts.dependencies]`, so `feature.shipit-artifacts`
    # exists IMPLICITLY. The managed `[feature.shipit-artifacts]` channel block
    # re-opens that implicit super-table (valid TOML), so it is NOT skipped as a
    # redeclaration (round-3 root-cause fix) — else the pin has no channel.
    # End-to-end: no table conflict, the block ADDs, and the applied manifest
    # carries BOTH the consumer pin and the channel.
    (tmp_path / "AGENTS.md").write_text("# Downstream\n")
    (tmp_path / "pixi.toml").write_text(
        iunits.pixi_manifest_seed("downstream")
        + '\n[feature.shipit-artifacts.dependencies]\nlexd = "0.19.3"\n'
    )
    _units, plan = _reconcile_public(tmp_path, [_dep(package="lexd")])
    assert plan.pixi_table_conflicts == ()
    feat = next(d for d in plan.decisions if d.unit.key == "pixi.toml#shipit-artifacts")
    assert feat.action == irec.ADD
    iapply.apply(plan, iapply.MODE_TREE)

    parsed = tomllib.loads((tmp_path / "pixi.toml").read_text(encoding="utf-8"))
    shared = parsed["feature"]["shipit-artifacts"]
    # Pin (consumer) AND channel (merged) co-exist in the one feature table.
    assert shared["dependencies"]["lexd"] == "0.19.3"
    assert shared["channels"] == [
        "https://storage.googleapis.com/shipit-artifacts-public/lex-fmt/lex"
    ]
    # Idempotent: a re-reconcile reads the merged channels as the block's own.
    _again, again = _reconcile_public(tmp_path, [_dep(package="lexd")])
    feat2 = next(
        d for d in again.decisions if d.unit.key == "pixi.toml#shipit-artifacts"
    )
    assert feat2.action == irec.NOOP


def test_dotted_feature_channel_merges_with_its_colocated_pin(tmp_path):
    # The merge holds for a dotted (quoted) feature name too: the consumer's pin
    # lives in `[feature."shipit-artifacts-tools.v2".dependencies]`, and the
    # channel merges under the quoted `[feature."shipit-artifacts-tools.v2"]`.
    (tmp_path / "AGENTS.md").write_text("# Downstream\n")
    (tmp_path / "pixi.toml").write_text(
        iunits.pixi_manifest_seed("downstream")
        + '\n[feature."shipit-artifacts-tools.v2".dependencies]\nlexd = "0.19.3"\n'
    )
    deps = [_dep(package="lexd", feature="tools.v2")]
    _units, plan = _reconcile_public(tmp_path, deps)
    assert plan.pixi_table_conflicts == ()
    iapply.apply(plan, iapply.MODE_TREE)
    parsed = tomllib.loads((tmp_path / "pixi.toml").read_text(encoding="utf-8"))
    shared = parsed["feature"]["shipit-artifacts-tools.v2"]
    assert shared["dependencies"]["lexd"] == "0.19.3"
    assert shared["channels"] == [
        "https://storage.googleapis.com/shipit-artifacts-public/lex-fmt/lex"
    ]


def test_existing_consumer_channel_update_preserves_the_feature_header(tmp_path):
    # Round-3 bug 1 (migration): an EXISTING consumer carries the managed markers
    # around a whole `[feature.shipit-artifacts]` channel table. A channel-URL
    # change is an UPDATE — `splice_block` replaces the marker span IN PLACE, so
    # the `[feature.shipit-artifacts]` header (INSIDE the markers) is preserved and
    # `channels` never orphans into a preceding table. The consumer's co-located
    # pin (outside the markers) is untouched, and `pixi.toml` stays valid.
    (tmp_path / "AGENTS.md").write_text("# Downstream\n")
    (tmp_path / "pixi.toml").write_text(iunits.pixi_manifest_seed("downstream"))
    # 1) The consumer's first install, with an initial derived channel — records
    #    the managed block + its pristine hash (so a later diff is a clean UPDATE).
    old_units = ad.project([(_dep(package="lexd"), "https://old.example/ch")])
    st = irec.gather(tmp_path, old_units, irec.load_retired())
    iapply.apply(irec.reconcile(old_units, irec.load_retired(), st), iapply.MODE_TREE)
    # 2) The consumer authors their co-located pin (after the managed block).
    with (tmp_path / "pixi.toml").open("a", encoding="utf-8") as f:
        f.write('\n[feature.shipit-artifacts.dependencies]\nlexd = "0.19.3"\n')

    # 3) A later install: the real derived channel differs -> UPDATE in place.
    _units, plan = _reconcile_public(tmp_path, [_dep(package="lexd")])
    feat = next(d for d in plan.decisions if d.unit.key == "pixi.toml#shipit-artifacts")
    assert feat.action == irec.UPDATE
    iapply.apply(plan, iapply.MODE_TREE)

    text = (tmp_path / "pixi.toml").read_text()
    parsed = tomllib.loads(text)  # still valid TOML — header not dropped
    shared = parsed["feature"]["shipit-artifacts"]
    assert shared["channels"] == [
        "https://storage.googleapis.com/shipit-artifacts-public/lex-fmt/lex"
    ]
    assert shared["dependencies"]["lexd"] == "0.19.3"  # consumer pin untouched
    assert text.count("[feature.shipit-artifacts]\n") == 1  # header preserved, once


def test_explicit_consumer_feature_table_is_a_real_conflict(tmp_path):
    # The guard still catches a REAL redeclaration: a consumer who EXPLICITLY
    # writes `[feature.shipit-artifacts]` themselves (not merely the implicit super
    # from a `.dependencies` sub-table) would make the managed channel header a
    # duplicate — so it is skipped + warned, the consumer's table authoritative.
    (tmp_path / "pixi.toml").write_text(
        iunits.pixi_manifest_seed("downstream")
        + "\n[feature.shipit-artifacts]\nchannels = []\n"
    )
    _units, plan = _reconcile_public(tmp_path, [_dep(package="lexd")])
    conflict = next(
        c
        for c in plan.pixi_table_conflicts
        if c.unit_key == "pixi.toml#shipit-artifacts"
    )
    assert conflict.tables == ("feature.shipit-artifacts",)
    assert "pixi.toml#shipit-artifacts" not in {d.unit.key for d in plan.decisions}


def test_toml_table_headers_return_verbatim_text_and_split_segments():
    # A dotted segment is quoted at emission (_toml_key). The parser returns the
    # VERBATIM header (for a faithful conflict report) plus the split segments
    # (which read a quoted dot as a literal, not a path separator, so the walk
    # hits the right table) — else `[s3-options."my.bucket"]` would both walk the
    # wrong path AND be reported as the different, nested `s3-options.my.bucket`.
    inner = '[s3-options."my.bucket"]\nregion = "auto"\n\n[feature.plain]\n'
    assert irec._toml_table_headers(inner) == (
        ('s3-options."my.bucket"', ("s3-options", "my.bucket")),
        ("feature.plain", ("feature", "plain")),
    )
    # Array-of-tables and non-header lines are ignored.
    assert irec._toml_table_headers("[[x]]\nk = 1\n") == ()


def test_s3_options_table_conflict_reports_the_quoted_header(tmp_path):
    # Display fidelity (copilot round 2), now exercised via the ONLY anchor-less
    # unit left — the private-tier `[s3-options.<bucket>]` block. A consumer who
    # hand-declares that table (the manual runbook) gets a table conflict whose
    # reported header PRESERVES the exact form, so the warning names the real
    # table to delete. (The feature/channel block is now anchored — it MERGES, so
    # it can no longer produce a table conflict; see the merge tests above.)
    (tmp_path / "pixi.toml").write_text(_CONSUMER_PIXI_WITH_MANUAL_S3)
    _units, plan = _reconcile_private(tmp_path)
    conflict = next(
        c for c in plan.pixi_table_conflicts if c.unit_key == ad.S3_OPTIONS_KEY
    )
    assert conflict.tables == ("s3-options.shipit-artifacts-private",)
    assert "[s3-options.shipit-artifacts-private]" in verb.format_plan_warnings(plan)


def test_table_declared_matches_only_the_full_leaf_path():
    manifest = {"s3-options": {"shipit-artifacts-private": {"region": "auto"}}}
    assert irec._table_declared(manifest, ("s3-options", "shipit-artifacts-private"))
    # A shared super-table alone is NOT a declared leaf table for the block.
    assert irec._table_declared(manifest, ("s3-options",))  # itself a table
    assert not irec._table_declared(manifest, ("s3-options", "other"))
    assert not irec._table_declared(manifest, ())


def test_table_conflict_guard_fails_open_on_an_unparseable_pixi_toml(tmp_path):
    # Best-effort like the key-conflict guard: a consumer who already broke their
    # own TOML hears it from pixi, not from a guard that only inspects.
    (tmp_path / "pixi.toml").write_text("[[[ not toml\n")
    units = _project_private([_dep(repo="phos/private")])
    state = irec.gather(tmp_path, units, irec.load_retired())
    assert state.pixi_table_conflicts == ()


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


def test_channel_change_is_a_single_update():
    # A change to the DERIVED channel set (a repo re-point) IS a real block
    # change: the feature block's channels differ, so the reconcile decides a
    # single UPDATE (consumer==old pristine, desired==new).
    old = _project([_dep(repo="lex-fmt/lex")])
    new = _project([_dep(repo="lex-fmt/other")])
    manifest = _splice_all(iunits.pixi_manifest_seed("x"), old)
    feat_new = next(u for u in new if u.key == "pixi.toml#shipit-artifacts")
    current = splice.extract_block(
        manifest, feat_new.open_marker, feat_new.close_marker
    )
    consumer_hash = config.content_hash(current.encode("utf-8"))
    assert (
        irec.decide(
            consumer_hash=consumer_hash,
            pristine_hash=consumer_hash,
            desired_hash=feat_new.desired_hash(),
        )
        == irec.UPDATE
    )


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


def test_producer_consumer_and_provisioner_share_one_bucket_source_of_truth():
    # ARF01-WS08 convergence: producer (publish), consumer (artifactdeps), AND
    # the WS03 store provisioner must name the SAME buckets — else the
    # provisioner CREATES a bucket the producer never writes to and the consumer
    # never reads from (the incoherence WS08 reconciled). All three now re-export
    # `shipit.channel.buckets`, and this pins them together so none can drift.
    from shipit.channel import buckets
    from shipit.channel import store_provision as sp
    from shipit.release import publish

    assert (
        buckets.PUBLIC_ARTIFACT_BUCKET
        == ad.PUBLIC_ARTIFACT_BUCKET
        == publish.PUBLIC_ARTIFACT_BUCKET
        == sp.bucket_name(sp.TIER_PUBLIC)
    )
    assert (
        buckets.PRIVATE_ARTIFACT_BUCKET
        == ad.PRIVATE_ARTIFACT_BUCKET
        == publish.PRIVATE_ARTIFACT_BUCKET
        == sp.bucket_name(sp.TIER_PRIVATE)
    )
    # The GCS host: consumer public-read + producer S3 endpoint + the URL the
    # provisioner's authless acceptance probe builds all use the one constant.
    assert (
        buckets.CHANNEL_HOST
        == ad.PUBLIC_CHANNEL_HOST
        == publish.CONDA_S3_ENDPOINT
        == sp._GCS_HOST
    )


# --------------------------------------------------------------------------
# Pin co-location (ADR-0077) — the consumer-owned pin lives in the SAME feature
# as the derived channel; `missing_pins` is the pure fail-safe check.
# --------------------------------------------------------------------------


def test_pin_feature_is_the_channels_feature():
    # The pin's feature IS the reserved feature that carries the channel, so a
    # pin declared there resolves against it (default + named).
    assert ad.pin_feature(None) == "shipit-artifacts"
    assert ad.pin_feature("tools") == "shipit-artifacts-tools"


def test_missing_pins_flags_a_dep_with_no_colocated_pin():
    manifest = tomllib.loads(
        '[feature.shipit-artifacts.dependencies]\nlexd = "0.19.3"\n'
    )
    # lexd is pinned in the channel's feature -> present; lexd-lsp is not.
    absent = ad.missing_pins([_dep(package="lexd"), _dep(package="lexd-lsp")], manifest)
    assert [d.package for d, _ in absent] == ["lexd-lsp"]
    (_, table) = absent[0]
    assert table == "[feature.shipit-artifacts.dependencies]"


def test_missing_pins_is_feature_scoped():
    # A pin in the default feature does NOT satisfy a named-feature target — the
    # channel for `feature="tools"` lives in `shipit-artifacts-tools`.
    manifest = tomllib.loads(
        '[feature.shipit-artifacts.dependencies]\nlexd = "0.19.3"\n'
    )
    absent = ad.missing_pins([_dep(package="lexd", feature="tools")], manifest)
    assert [t for _, t in absent] == ["[feature.shipit-artifacts-tools.dependencies]"]


def test_missing_pins_quotes_a_dotted_feature_table():
    manifest = tomllib.loads('[workspace]\nname = "c"\n')
    absent = ad.missing_pins(
        [_dep(package="ruamel.yaml", feature="tools.v2")], manifest
    )
    assert absent[0][1] == '[feature."shipit-artifacts-tools.v2".dependencies]'


def test_missing_pins_empty_when_all_pins_present():
    manifest = tomllib.loads(
        "[feature.shipit-artifacts.dependencies]\n"
        'lexd = "0.19.3"\n'
        "[feature.shipit-artifacts-tools.dependencies]\n"
        'lexd-lsp = "0.20.0"\n'
    )
    deps = [_dep(package="lexd"), _dep(package="lexd-lsp", feature="tools")]
    assert ad.missing_pins(deps, manifest) == []


# --------------------------------------------------------------------------
# The verb glue — `_artifact_dep_units` (visibility injected; no network)
# --------------------------------------------------------------------------


def _write_config(root, text):
    (root / config.CONFIG_NAME).write_text(text, encoding="utf-8")


def _write_pixi(root, text):
    (root / "pixi.toml").write_text(text, encoding="utf-8")


def test_verb_projects_public_deps_resolving_visibility_once_per_repo(tmp_path):
    from shipit.verbs import install as verb

    _write_config(
        tmp_path,
        "[artifact-deps.lexd]\n"
        'repo = "lex-fmt/lex"\n'
        "[artifact-deps.lexd-lsp]\n"
        'repo = "lex-fmt/lex"\n',
    )
    # conda-direct: the consumer owns the pins, co-located with the channel in the
    # artifact's feature. `_artifact_dep_units` requires them present.
    _write_pixi(
        tmp_path,
        "[feature.shipit-artifacts.dependencies]\n"
        'lexd = "0.19.3"\n'
        'lexd-lsp = "0.19.3"\n',
    )
    calls = []

    def fake_is_private(slug):
        calls.append(slug)
        return False

    units = verb._artifact_dep_units(tmp_path, is_private=fake_is_private)
    # No cascade receive-workflow anymore (conda-direct removed the rail).
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

    _write_config(tmp_path, '[artifact-deps.phos-tool]\nrepo = "phos/private"\n')
    _write_pixi(
        tmp_path, '[feature.shipit-artifacts.dependencies]\nphos-tool = "1.0"\n'
    )
    units = verb._artifact_dep_units(tmp_path, is_private=lambda slug: True)
    keys = {u.key for u in units}
    assert ad.S3_OPTIONS_KEY in keys
    feat = next(u for u in units if u.key == "pixi.toml#shipit-artifacts")
    assert "s3://shipit-artifacts-private/phos/private" in feat.desired_inner()


def test_verb_fails_loud_when_the_consumer_pin_is_missing(tmp_path):
    # The fail-safe (ADR-0077, Major 2): a declared artifact-dep with NO
    # consumer-owned pin in the artifact's feature must NOT silently project a
    # channel with nothing to resolve — it fails loud, naming the exact table.
    from shipit.verbs import install as verb

    _write_config(tmp_path, '[artifact-deps.lexd]\nrepo = "lex-fmt/lex"\n')
    _write_pixi(tmp_path, '[workspace]\nname = "c"\n')  # no pin
    with pytest.raises(
        config.ConfigError,
        match=r"no consumer-owned version pin.*feature\.shipit-artifacts\.dependencies",
    ):
        verb._artifact_dep_units(tmp_path, is_private=lambda slug: False)


def test_verb_requires_the_pin_in_the_named_features_dependency_table(tmp_path):
    # A named `feature` scopes BOTH the channel and the pin into
    # `shipit-artifacts-<F>` — a pin in the default feature does not satisfy it.
    from shipit.verbs import install as verb

    _write_config(
        tmp_path,
        '[artifact-deps.lexd]\nrepo = "lex-fmt/lex"\nfeature = "tools"\n',
    )
    # Pin in the WRONG (default) feature → still missing for the named target.
    _write_pixi(tmp_path, '[feature.shipit-artifacts.dependencies]\nlexd = "0.19.3"\n')
    with pytest.raises(
        config.ConfigError,
        match=r"feature\.shipit-artifacts-tools\.dependencies",
    ):
        verb._artifact_dep_units(tmp_path, is_private=lambda slug: False)
    # Pin in the RIGHT feature → projects cleanly.
    _write_pixi(
        tmp_path, '[feature.shipit-artifacts-tools.dependencies]\nlexd = "0.19.3"\n'
    )
    units = verb._artifact_dep_units(tmp_path, is_private=lambda slug: False)
    assert "pixi.toml#shipit-artifacts-tools" in {u.key for u in units}


def test_verb_fails_loud_on_a_malformed_entry(tmp_path):
    from shipit.verbs import install as verb

    _write_config(tmp_path, '[artifact-deps.lexd]\nrepo = "not-a-slug"\n')
    with pytest.raises(config.ConfigError):
        verb._artifact_dep_units(tmp_path, is_private=lambda slug: False)


def test_verb_rejects_the_legacy_version_shape(tmp_path):
    # NO backwards compat: a legacy `{ repo, version }` errors at parse, before any
    # projection or de-provision.
    from shipit.verbs import install as verb

    _write_config(
        tmp_path, '[artifact-deps.lexd]\nrepo = "lex-fmt/lex"\nversion = "0.19.3"\n'
    )
    with pytest.raises(config.ConfigError, match=r"version is no longer allowed"):
        verb._artifact_dep_units(tmp_path, is_private=lambda slug: False)


def test_verb_fails_loud_when_pixi_toml_is_absent(tmp_path):
    # Major 2 / copilot (#1094 review): a repo that declares `[artifact-deps]` but
    # has NO pixi.toml must NOT silently project a channel with no pin — the absent
    # manifest is treated as empty, so every declared dep reads as missing.
    from shipit.verbs import install as verb

    _write_config(tmp_path, '[artifact-deps.lexd]\nrepo = "lex-fmt/lex"\n')
    # no pixi.toml written
    with pytest.raises(
        config.ConfigError,
        match=r"no consumer-owned version pin.*feature\.shipit-artifacts\.dependencies",
    ):
        verb._artifact_dep_units(tmp_path, is_private=lambda slug: False)


def test_verb_fails_loud_when_pixi_toml_is_unparseable(tmp_path):
    # An unparseable pixi.toml is likewise treated as empty (all pins missing) —
    # NOT a silent pass that would project a channel with nothing to resolve.
    from shipit.verbs import install as verb

    _write_config(tmp_path, '[artifact-deps.lexd]\nrepo = "lex-fmt/lex"\n')
    _write_pixi(tmp_path, "this is not valid toml = = =\n")
    with pytest.raises(config.ConfigError, match=r"no consumer-owned version pin"):
        verb._artifact_dep_units(tmp_path, is_private=lambda slug: False)


def test_verb_degrades_on_an_unreadable_shipit_toml(tmp_path):
    # A generally-unreadable `.shipit.toml` degrades to no artifact units (gather
    # warns), it does not crash install — the artifact-dep read is not the place a
    # broken top-level config surfaces.
    from shipit.verbs import install as verb

    _write_config(tmp_path, "this is not valid toml = = =\n")
    assert verb._artifact_dep_units(tmp_path, is_private=lambda slug: False) == []


# --------------------------------------------------------------------------
# env-name mapping + materialized-binary path (TOL03-WS03 #974): the bridge the
# vsix bundle staging reads to locate a tool artifact-dep's on-disk binary.
# --------------------------------------------------------------------------


def test_env_name_maps_default_and_named_features():
    # The default target lands in the `default` env; a named feature in its
    # isolated `shipit-artifacts-<F>` env — the SAME mapping the projection uses.
    assert ad.env_name(None) == "default"
    assert ad.env_name("lint") == "shipit-artifacts-lint"


def test_materialized_bin_path_is_the_env_prefix_bin_package(tmp_path):
    # A unix tool artifact-dep's binary lands at
    # <root>/.pixi/envs/<env>/bin/<package> (ADR-0064: a tool artifact puts a
    # binary on PATH) — pure path arithmetic, no filesystem probe.
    default_dep = _dep(package="lexd-lsp", feature=None)
    assert ad.materialized_bin_path(
        tmp_path, default_dep, target="x86_64-unknown-linux-gnu"
    ) == (tmp_path / ".pixi/envs/default/bin/lexd-lsp")
    lint_dep = _dep(package="lexd", feature="lint")
    assert ad.materialized_bin_path(
        tmp_path, lint_dep, target="aarch64-apple-darwin"
    ) == (tmp_path / ".pixi/envs/shipit-artifacts-lint/bin/lexd")


def test_materialized_bin_path_is_target_aware_for_windows(tmp_path):
    # conda installs a win-64 tool binary to `Scripts/<pkg>.exe`, not `bin/<pkg>`
    # (release.publish._conda_binary_layout) — a win32-x64 vsix leg must resolve
    # THERE, or staging aborts on a path that never exists on that runner.
    dep = _dep(package="lexd-lsp", feature=None)
    assert ad.materialized_bin_path(tmp_path, dep, target="x86_64-pc-windows-msvc") == (
        tmp_path / ".pixi/envs/default/Scripts/lexd-lsp.exe"
    )


# --------------------------------------------------------------------------
# Cascade retirement (ADR-0077) — the stale receive-workflow is deleted from a
# consumer that already installed it (the pristine-leftover-removal pattern).
# --------------------------------------------------------------------------


def test_retirement_deletes_the_stale_cascade_workflow(tmp_path):
    # A consumer that installed the (now-removed) Cascade receive-workflow still
    # carries a pristine copy once its managed unit leaves the catalog. The
    # retired-files pass DELETES it end-to-end (gather -> reconcile -> apply).
    (tmp_path / "AGENTS.md").write_text("# Downstream\n")
    victim = tmp_path / _CASCADE_WORKFLOW_DEST
    victim.parent.mkdir(parents=True)
    victim.write_bytes(_CASCADE_PRISTINE.read_bytes())

    retired = irec.load_retired()
    state = irec.gather(tmp_path, [], retired)
    plan = irec.reconcile([], retired, state)
    decision = next(d for d in plan.retired if d.retired.path == _CASCADE_WORKFLOW_DEST)
    assert decision.action == irec.DELETE
    iapply.apply(plan, iapply.MODE_TREE)
    assert not victim.exists()


def test_retirement_keeps_a_locally_edited_cascade_workflow(tmp_path):
    # Safety: a consumer who HAND-EDITED the stale workflow (content matches no
    # known pristine hash) keeps it, warned — never a destroyed local edit.
    (tmp_path / "AGENTS.md").write_text("# Downstream\n")
    victim = tmp_path / _CASCADE_WORKFLOW_DEST
    victim.parent.mkdir(parents=True)
    victim.write_text("name: my-own-edited-workflow\n")

    retired = irec.load_retired()
    state = irec.gather(tmp_path, [], retired)
    plan = irec.reconcile([], retired, state)
    decision = next(d for d in plan.retired if d.retired.path == _CASCADE_WORKFLOW_DEST)
    assert decision.action == irec.KEEP
    iapply.apply(plan, iapply.MODE_TREE)
    assert victim.exists()


def test_cascade_retirement_hashes_match_the_pristine_fixture():
    # The manifest entry's pristine hash covers the delivered workflow content, so
    # the delete actually fires for a real consumer (not a stale/typo'd hash).
    retired = {r.path: r for r in irec.load_retired()}
    entry = retired[_CASCADE_WORKFLOW_DEST]
    assert config.content_hash(_CASCADE_PRISTINE.read_bytes()) in entry.pristine_hashes
