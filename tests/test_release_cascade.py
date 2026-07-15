"""`shipit release cascade` — the release-side artifact-pinned Cascade (ARF01-WS06).

Three seams, tested to their nature (PRD Testing Decisions):

- the PURE derivation core (:func:`shipit.release.cascade.derive_targets`) is
  fixture-tested straight on values — who pins the upstream, case-insensitive
  match, the self-exclusion, the no-match empty set;
- the BOUNDED portfolio scan (:func:`~.scan_portfolio`) is driven over a temp
  source-root layout so exactly-the-declared-portfolio and the missing/absent
  `[artifact-deps]` cases read off real files;
- the dispatch (:func:`~.dispatch_targets` / :func:`~.run_cascade`) is exercised
  through a recorded gh seam (`FakeGh`) so every acceptance — the exact
  `{upstream, version}` payload, the stable-only rc skip, "nothing dispatched"
  on a dry run / empty set, the missing-token refusal — reads off recorded
  invocations. A drift guard pins the shared event-type/token contract to the
  notify rail.
"""

from pathlib import Path

import pytest

from shipit import config
from shipit.release import cascade as cascade_mod
from shipit.release import publish as publish_mod
from shipit.release import secretreq as secretreq_mod
from shipit.verbs import release as release_verb


class FakeGh:
    """The recorded gh-adapter seam — just the one write the Cascade makes."""

    def __init__(self, *, root=None):
        self.calls = []
        self._root = root

    def repository_dispatch(self, slug, *, event_type, payload, token=None):
        self.calls.append(("dispatch", slug, event_type, dict(payload), token))


class FakeGit:
    """The recorded git seam — only `repo_root` is read by the verb."""

    def __init__(self, root):
        self.root = root

    def repo_root(self, *, cwd=None):
        return str(self.root)


def _dep(package, repo, version="1.0.0", feature=None):
    return config.ArtifactDep(
        package=package, repo=repo, version=version, feature=feature
    )


# --------------------------------------------------------------------------
# The pure derivation core
# --------------------------------------------------------------------------


def test_derive_targets_matches_a_consumer_pinning_the_upstream():
    consumers = [
        ("acme/app", [_dep("lexd", "lex-fmt/lex")]),
        ("acme/other", [_dep("thing", "someone/else")]),
    ]
    targets = cascade_mod.derive_targets("lex-fmt/lex", consumers)
    assert [t.repo for t in targets] == ["acme/app"]
    assert targets[0].packages == ("lexd",)


def test_derive_targets_is_case_insensitive_on_the_slug():
    """A case-only difference between the pin and the releasing upstream still
    matches — both normalize through the one canonical slug parser."""
    consumers = [("Acme/App", [_dep("lexd", "Lex-Fmt/Lex")])]
    targets = cascade_mod.derive_targets("lex-fmt/lex", consumers)
    assert [t.repo for t in targets] == ["acme/app"]


def test_derive_targets_excludes_the_upstream_targeting_itself():
    """A repo that pins its OWN artifact does not get a cross-repo bump from its
    own release."""
    consumers = [("lex-fmt/lex", [_dep("lexd", "lex-fmt/lex")])]
    assert cascade_mod.derive_targets("lex-fmt/lex", consumers) == ()


def test_derive_targets_ignores_consumers_with_no_matching_pin():
    consumers = [("acme/app", [_dep("x", "other/one"), _dep("y", "other/two")])]
    assert cascade_mod.derive_targets("lex-fmt/lex", consumers) == ()


def test_derive_targets_collects_every_matching_package_in_order():
    consumers = [
        (
            "acme/app",
            [
                _dep("lexd", "lex-fmt/lex"),
                _dep("unrelated", "other/one"),
                _dep("lexd-lsp", "lex-fmt/lex"),
            ],
        )
    ]
    targets = cascade_mod.derive_targets("lex-fmt/lex", consumers)
    assert targets[0].packages == ("lexd", "lexd-lsp")


def test_derive_targets_keeps_portfolio_first_seen_order():
    consumers = [
        ("acme/b", [_dep("lexd", "lex-fmt/lex")]),
        ("acme/a", [_dep("lexd", "lex-fmt/lex")]),
    ]
    targets = cascade_mod.derive_targets("lex-fmt/lex", consumers)
    assert [t.repo for t in targets] == ["acme/b", "acme/a"]


def test_derive_targets_refuses_a_malformed_upstream_slug():
    with pytest.raises(cascade_mod.CascadeError, match="invalid repo slug"):
        cascade_mod.derive_targets("not-a-slug", [])


# --------------------------------------------------------------------------
# The bounded portfolio scan
# --------------------------------------------------------------------------


def _portfolio_cfg(*entries):
    """A `[project.portfolio]` cfg with one stack of `(repo, path)` entries."""
    return {
        "project": {
            "portfolio": {
                "stack": [{"repo": repo, "path": path} for repo, path in entries]
            }
        }
    }


def _write_shipit_toml(root: Path, path: str, body: str) -> None:
    checkout = root / path
    checkout.mkdir(parents=True, exist_ok=True)
    (checkout / config.CONFIG_NAME).write_text(body, encoding="utf-8")


def test_scan_portfolio_reads_each_declared_repos_artifact_deps(tmp_path):
    _write_shipit_toml(
        tmp_path,
        "acme/app",
        '[artifact-deps.lexd]\nrepo = "lex-fmt/lex"\nversion = "0.19.3"\n',
    )
    cfg = _portfolio_cfg(("acme/app", "acme/app"))
    scanned = cascade_mod.scan_portfolio(cfg, source_root=tmp_path)
    assert len(scanned) == 1
    repo, deps = scanned[0]
    assert repo == "acme/app"
    assert [d.package for d in deps] == ["lexd"]
    assert deps[0].repo == "lex-fmt/lex"


def test_scan_portfolio_treats_a_missing_toml_as_no_deps(tmp_path):
    """A portfolio repo whose local checkout is absent contributes an empty dep
    tuple — it simply never becomes a target, no crash."""
    cfg = _portfolio_cfg(("acme/ghost", "acme/ghost"))
    scanned = cascade_mod.scan_portfolio(cfg, source_root=tmp_path)
    assert scanned == (("acme/ghost", ()),)


def test_scan_portfolio_treats_no_artifact_deps_as_empty(tmp_path):
    _write_shipit_toml(tmp_path, "acme/plain", "[shipit]\nversion = 'abc'\n")
    cfg = _portfolio_cfg(("acme/plain", "acme/plain"))
    scanned = cascade_mod.scan_portfolio(cfg, source_root=tmp_path)
    assert scanned == (("acme/plain", ()),)


def test_scan_portfolio_is_bounded_to_the_declared_portfolio(tmp_path):
    """Only the declared `[project.portfolio]` entries are read — a checkout on
    disk that is NOT in the portfolio is never scanned (no fleet crawl)."""
    _write_shipit_toml(
        tmp_path,
        "acme/app",
        '[artifact-deps.lexd]\nrepo = "lex-fmt/lex"\nversion = "1"\n',
    )
    _write_shipit_toml(
        tmp_path,
        "acme/stray",
        '[artifact-deps.lexd]\nrepo = "lex-fmt/lex"\nversion = "1"\n',
    )
    cfg = _portfolio_cfg(("acme/app", "acme/app"))
    scanned = cascade_mod.scan_portfolio(cfg, source_root=tmp_path)
    assert [repo for repo, _ in scanned] == ["acme/app"]


def test_scan_portfolio_expands_user_in_source_root(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_shipit_toml(
        tmp_path,
        "acme/app",
        '[artifact-deps.lexd]\nrepo = "lex-fmt/lex"\nversion = "1"\n',
    )
    cfg = _portfolio_cfg(("acme/app", "acme/app"))
    scanned = cascade_mod.scan_portfolio(cfg, source_root=Path("~"))
    assert [repo for repo, _ in scanned] == ["acme/app"]


# --------------------------------------------------------------------------
# Dispatch through the recorded gh seam
# --------------------------------------------------------------------------


def test_dispatch_targets_fires_the_exact_payload_per_target():
    targets = [
        cascade_mod.CascadeTarget("acme/app", ("lexd",)),
        cascade_mod.CascadeTarget("acme/tool", ("lexd-lsp",)),
    ]
    gh = FakeGh()
    payload = {"upstream": "lex-fmt/lex", "version": "0.19.3"}
    dispatched = cascade_mod.dispatch_targets(
        targets, payload, token="pat-123", ghio=gh
    )
    assert dispatched == ("acme/app", "acme/tool")
    assert gh.calls == [
        ("dispatch", "acme/app", "upstream-release", payload, "pat-123"),
        ("dispatch", "acme/tool", "upstream-release", payload, "pat-123"),
    ]


# --------------------------------------------------------------------------
# The orchestrator (scan -> derive -> dispatch)
# --------------------------------------------------------------------------


def _scan(*consumers):
    """A `scan_fn` stub returning fixed `(slug, [ArtifactDep])` pairs."""

    def scan(cfg, *, source_root):
        return list(consumers)

    return scan


def test_run_cascade_dispatches_to_the_derived_stable_set():
    gh = FakeGh()
    report = cascade_mod.run_cascade(
        "lex-fmt/lex",
        "0.19.3",
        cfg={},
        source_root=Path("/unused"),
        prerelease=False,
        token="pat-123",
        ghio=gh,
        scan_fn=_scan(("acme/app", [_dep("lexd", "lex-fmt/lex")])),
    )
    assert report.dispatched == ("acme/app",)
    assert report.skipped is None
    assert gh.calls == [
        (
            "dispatch",
            "acme/app",
            "upstream-release",
            {"upstream": "lex-fmt/lex", "version": "0.19.3"},
            "pat-123",
        )
    ]


def test_run_cascade_skips_a_prerelease_without_scanning_or_dispatching():
    """rc / prerelease versions dispatch NOTHING (stable-only, ADR-0067) — and
    short-circuit before the scan even runs."""
    gh = FakeGh()

    def exploding_scan(cfg, *, source_root):  # must never be called
        raise AssertionError("scan ran on a prerelease")

    report = cascade_mod.run_cascade(
        "lex-fmt/lex",
        "0.20.0-rc.1",
        cfg={},
        source_root=Path("/unused"),
        prerelease=True,
        token="pat-123",
        ghio=gh,
        scan_fn=exploding_scan,
    )
    assert report.dispatched == ()
    assert report.skipped == cascade_mod.SKIP_PRERELEASE
    assert gh.calls == []


def test_run_cascade_empty_target_set_dispatches_nothing():
    gh = FakeGh()
    report = cascade_mod.run_cascade(
        "lex-fmt/lex",
        "0.19.3",
        cfg={},
        source_root=Path("/unused"),
        prerelease=False,
        token="pat-123",
        ghio=gh,
        scan_fn=_scan(("acme/app", [_dep("x", "other/one")])),
    )
    assert report.targets == ()
    assert report.dispatched == ()
    assert "no portfolio repo declares" in report.skipped
    assert gh.calls == []


def test_run_cascade_dry_run_derives_but_dispatches_nothing():
    gh = FakeGh()
    report = cascade_mod.run_cascade(
        "lex-fmt/lex",
        "0.19.3",
        cfg={},
        source_root=Path("/unused"),
        prerelease=False,
        token=None,
        ghio=gh,
        dry_run=True,
        scan_fn=_scan(("acme/app", [_dep("lexd", "lex-fmt/lex")])),
    )
    assert [t.repo for t in report.targets] == ["acme/app"]
    assert report.dispatched == ()
    assert "dry run" in report.skipped
    assert gh.calls == []


def test_run_cascade_refuses_a_live_dispatch_without_a_token():
    gh = FakeGh()
    with pytest.raises(cascade_mod.CascadeError, match="DOWNSTREAM_DISPATCH_TOKEN"):
        cascade_mod.run_cascade(
            "lex-fmt/lex",
            "0.19.3",
            cfg={},
            source_root=Path("/unused"),
            prerelease=False,
            token=None,
            ghio=gh,
            scan_fn=_scan(("acme/app", [_dep("lexd", "lex-fmt/lex")])),
        )
    assert gh.calls == []


def test_run_cascade_report_to_dict_carries_the_payload_contract():
    report = cascade_mod.run_cascade(
        "lex-fmt/lex",
        "0.19.3",
        cfg={},
        source_root=Path("/unused"),
        prerelease=False,
        token="pat",
        ghio=FakeGh(),
        scan_fn=_scan(("acme/app", [_dep("lexd", "lex-fmt/lex")])),
    )
    d = report.to_dict()
    assert d["payload"] == {"upstream": "lex-fmt/lex", "version": "0.19.3"}
    assert d["event_type"] == "upstream-release"
    assert d["dispatched"] == ["acme/app"]


# --------------------------------------------------------------------------
# The shared-contract drift guard
# --------------------------------------------------------------------------


def test_event_type_mirrors_the_notify_dispatch_rail():
    """The Cascade reuses the notify-downstreams dispatch rail's event name
    (ADR-0067) — pinned so the two can never silently diverge."""
    assert cascade_mod.CASCADE_EVENT_TYPE == publish_mod.NOTIFY_EVENT_TYPE


def test_dispatch_token_mirrors_the_notify_downstreams_secret():
    """The cross-repo dispatch PAT is the SAME secret the notify-downstreams
    endpoint declares."""
    assert (
        cascade_mod.DISPATCH_TOKEN_ENV
        == secretreq_mod.ENDPOINT_SECRETS["notify-downstreams"][0]
    )


# --------------------------------------------------------------------------
# The verb (recorded seams end-to-end)
# --------------------------------------------------------------------------


def test_verb_dispatches_and_registers_the_token(tmp_path, monkeypatch, capsys):
    """`run_release_cascade` reads the portfolio off the checkout, scans local
    checkouts, and fires the dispatch — with the PAT registered for redaction
    before any dispatch."""
    (tmp_path / config.CONFIG_NAME).write_text(
        '[project.portfolio]\nstack = [{ repo = "acme/app", path = "acme/app" }]\n',
        encoding="utf-8",
    )
    _write_shipit_toml(
        tmp_path,
        "acme/app",
        '[artifact-deps.lexd]\nrepo = "lex-fmt/lex"\nversion = "0.19.3"\n',
    )
    registered = []
    monkeypatch.setattr(release_verb.redact, "register_secret", registered.append)
    gh = FakeGh()
    rc = release_verb.run_release_cascade(
        "lex-fmt/lex",
        "0.19.3",
        source_root=tmp_path,
        gitio=FakeGit(tmp_path),
        ghio=gh,
        env={"DOWNSTREAM_DISPATCH_TOKEN": "pat-xyz"},
    )
    assert rc == 0
    assert registered == ["pat-xyz"]
    assert gh.calls == [
        (
            "dispatch",
            "acme/app",
            "upstream-release",
            {"upstream": "lex-fmt/lex", "version": "0.19.3"},
            "pat-xyz",
        )
    ]


def test_verb_prerelease_dispatches_nothing(tmp_path):
    (tmp_path / config.CONFIG_NAME).write_text(
        '[project.portfolio]\nstack = [{ repo = "acme/app", path = "acme/app" }]\n',
        encoding="utf-8",
    )
    gh = FakeGh()
    rc = release_verb.run_release_cascade(
        "lex-fmt/lex",
        "0.20.0-rc.1",
        source_root=tmp_path,
        gitio=FakeGit(tmp_path),
        ghio=gh,
        env={},
    )
    assert rc == 0
    assert gh.calls == []


def test_verb_dry_run_needs_no_token(tmp_path):
    (tmp_path / config.CONFIG_NAME).write_text(
        '[project.portfolio]\nstack = [{ repo = "acme/app", path = "acme/app" }]\n',
        encoding="utf-8",
    )
    _write_shipit_toml(
        tmp_path,
        "acme/app",
        '[artifact-deps.lexd]\nrepo = "lex-fmt/lex"\nversion = "0.19.3"\n',
    )
    gh = FakeGh()
    rc = release_verb.run_release_cascade(
        "lex-fmt/lex",
        "0.19.3",
        source_root=tmp_path,
        dry_run=True,
        gitio=FakeGit(tmp_path),
        ghio=gh,
        env={},
    )
    assert rc == 0
    assert gh.calls == []
