import json

import pytest

from shipit import execrun, fleetposture, fleetsweep
from shipit.release import posture
from shipit.verbs import fleet as fleet_verb

_CANONICAL = [
    "APPLE_CERTIFICATE",
    "APPLE_CERTIFICATE_PASSWORD",
    "ASC_API_KEY_BASE64",
    "ASC_API_KEY_ID",
    "ASC_API_ISSUER_ID",
]

_LEGACY_FLEET_STATE = [
    "APPLE_CERTIFICATE_P12_BASE64",
    "APPLE_CERTIFICATE_PASSWORD",
    "ASC_API_KEY_BASE64",
    "ASC_API_KEY_ID",
    "ASC_API_ISSUER_ID",
]


def _entry(repo="a/b", *, signing="signed", reason=None):
    return fleetsweep.PortfolioEntry(
        stack="s", repo=repo, path="b", signing=signing, signing_reason=reason
    )


def _kinds(findings):
    return sorted({f.kind for f in findings})


def test_canonical_signed_repo_conforms():
    assert posture.conformance("signed", [*_CANONICAL, "RELEASE_TOKEN"]) == ()


def test_legacy_cert_name_is_a_finding_naming_its_canonical_replacement():
    findings = posture.conformance("signed", _LEGACY_FLEET_STATE)
    assert _kinds(findings) == [posture.KIND_LEGACY]
    assert findings[0].secret == "APPLE_CERTIFICATE_P12_BASE64"
    assert "APPLE_CERTIFICATE" in findings[0].detail


def test_a_missing_canonical_name_is_reported_once_per_name():
    findings = posture.conformance("signed", ["APPLE_CERTIFICATE"])
    assert _kinds(findings) == [posture.KIND_MISSING]
    assert sorted(f.secret for f in findings) == sorted(
        n for n in posture.CANONICAL_SIGNING_SECRETS if n != "APPLE_CERTIFICATE"
    )


def test_apple_id_notary_is_noncanonical_even_though_the_block_accepts_it():
    findings = posture.conformance(
        "signed", [*_CANONICAL, "APPLE_ID", "APPLE_PASSWORD", "APPLE_TEAM_ID"]
    )
    assert _kinds(findings) == [posture.KIND_NONCANONICAL_NOTARY]
    assert len(findings) == 3


def test_stray_signing_secrets_are_findings():
    findings = posture.conformance("signed", [*_CANONICAL, "APPLE_SIGNING_IDENTITY"])
    assert [(f.kind, f.secret) for f in findings] == [
        (posture.KIND_STRAY, "APPLE_SIGNING_IDENTITY")
    ]


@pytest.mark.parametrize("declared", ["unsigned", "not-applicable"])
def test_a_nonsigning_repo_must_carry_no_signing_secret(declared):
    assert posture.conformance(declared, ["RELEASE_TOKEN", "PYPI_TOKEN"]) == ()
    findings = posture.conformance(declared, _LEGACY_FLEET_STATE)
    assert _kinds(findings) == [posture.KIND_UNEXPECTED]
    assert len(findings) == len(_LEGACY_FLEET_STATE)


def test_an_unknown_posture_is_refused():
    with pytest.raises(ValueError, match="unknown signing posture"):
        posture.conformance("sorta", [])


def test_row_reports_the_findings_against_the_declared_posture():
    row = fleetposture.check_repo(
        _entry(), names_fn=lambda repo: list(_LEGACY_FLEET_STATE)
    )
    assert row.status == fleetposture.STATUS_DIVERGENT
    assert row.secrets == tuple(sorted(_LEGACY_FLEET_STATE))
    assert "DIVERGENT" in row.summary()


def test_an_unreadable_repo_is_unknown_not_a_pass():
    def boom(repo):
        raise execrun.ExecError(["gh", "secret", "list"], rc=1, stderr="denied")

    row = fleetposture.check_repo(_entry(), names_fn=boom)
    assert row.status == fleetposture.STATUS_UNKNOWN
    assert "could not list Actions secrets" in row.summary()
    assert fleetposture.PostureReport(repos=(row,)).verdict() == 1


def test_conforming_row_shows_the_recorded_reason():
    row = fleetposture.check_repo(
        _entry(signing="unsigned", reason="owner decision #779"),
        names_fn=lambda repo: [],
    )
    assert row.status == fleetposture.STATUS_CONFORMS
    assert "owner decision #779" in row.summary()


def test_report_verdict_is_green_only_when_every_row_conforms():
    rows = tuple(
        fleetposture.check_repo(
            _entry(repo=repo, signing="not-applicable"), names_fn=lambda r: []
        )
        for repo in ("a/b", "c/d")
    )
    assert fleetposture.PostureReport(repos=rows).verdict() == 0


def test_report_json_carries_the_registry_and_the_divergent_repos():
    rows = (
        fleetposture.check_repo(_entry(), names_fn=lambda r: list(_CANONICAL)),
        fleetposture.check_repo(
            _entry(repo="c/d"), names_fn=lambda r: list(_LEGACY_FLEET_STATE)
        ),
    )
    data = fleetposture.PostureReport(repos=rows).to_dict()
    assert data["kind"] == "fleet-posture-report"
    assert data["divergent"] == ["c/d"]
    assert data["canonical_signing_secrets"] == list(posture.CANONICAL_SIGNING_SECRETS)
    assert json.dumps(data)


def test_format_posture_tables_every_repo_and_states_the_verdict():
    rows = (
        fleetposture.check_repo(_entry(), names_fn=lambda r: list(_CANONICAL)),
        fleetposture.check_repo(
            _entry(repo="c/d"), names_fn=lambda r: list(_LEGACY_FLEET_STATE)
        ),
    )
    text = fleet_verb.format_posture(fleetposture.PostureReport(repos=rows))
    assert "REPO" in text
    assert "a/b" in text and "c/d" in text
    assert "1 repo(s) off-posture" in text


def test_run_posture_checks_the_declared_portfolio(tmp_path, monkeypatch, capsys):
    (tmp_path / ".shipit.toml").write_text(
        '[project.portfolio]\ns = [{ repo = "a/b", path = "b", signing = "signed" }]\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    rc = fleet_verb.run_posture(names_fn=lambda repo: list(_CANONICAL))
    assert rc == 0
    assert "homogeneous" in capsys.readouterr().out


def test_run_posture_refuses_a_selector_outside_the_portfolio(tmp_path, monkeypatch):
    (tmp_path / ".shipit.toml").write_text(
        '[project.portfolio]\ns = [{ repo = "a/b", path = "b", signing = "signed" }]\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    assert fleet_verb.run_posture(repos=("x/y",), names_fn=lambda repo: []) != 0
