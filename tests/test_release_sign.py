import io
import json
import tarfile
from pathlib import Path

import pytest

from shipit import execrun
from shipit.release import ReleaseError
from shipit.release import sign as sign_mod
from shipit.verbs import release as release_verb

MACHO_64 = b"\xcf\xfa\xed\xfe" + b"\x00" * 12

FULL_ENV = {
    "APPLE_CERTIFICATE": "Y2VydC1wMTI=",
    "APPLE_CERTIFICATE_PASSWORD": "p12pass",
    "ASC_API_KEY_BASE64": "cDgta2V5",
    "ASC_API_KEY_ID": "KEYID123",
    "ASC_API_ISSUER_ID": "issuer-uuid",
    "APPLE_ID": "dev@example.com",
    "APPLE_PASSWORD": "app-specific",
    "APPLE_TEAM_ID": "TEAM123",
}

APPLE_ID_ENV = {
    "APPLE_CERTIFICATE": "Y2VydC1wMTI=",
    "APPLE_ID": "dev@example.com",
    "APPLE_PASSWORD": "app-specific",
    "APPLE_TEAM_ID": "TEAM123",
}

IDENTITY = "Developer ID Application: Phos (TEAM123)"

FIND_IDENTITY_OUT = f'  1) ABCDEF0123 "{IDENTITY}"\n     1 valid identities found\n'

LOGIN_KEYCHAIN = "/Users/tester/Library/Keychains/login.keychain-db"

LIST_KEYCHAINS_OUT = f'    "{LOGIN_KEYCHAIN}"\n'


def _make_targz(dest: Path, base: Path, names: list[str]) -> None:
    with tarfile.open(dest, "w:gz") as tar:
        for name in names:
            tar.add(base / name, arcname=name)


def _link_member(name: str, target: str, *, hard: bool = False) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = tarfile.LNKTYPE if hard else tarfile.SYMTYPE
    info.linkname = target
    return info


def _special_member(name: str) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = tarfile.FIFOTYPE
    return info


def _make_targz_members(
    dest: Path, files: dict[str, bytes], members: list[tarfile.TarInfo]
) -> None:
    with tarfile.open(dest, "w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        for member in members:
            tar.addfile(member)


def test_required_secret_names_declare_cert_pair_and_both_notary_trios():
    assert sign_mod.SIGNING_SECRETS == (
        "APPLE_CERTIFICATE",
        "APPLE_CERTIFICATE_PASSWORD",
    )
    assert sign_mod.NOTARY_SECRET_SETS == (
        ("ASC_API_KEY_BASE64", "ASC_API_KEY_ID", "ASC_API_ISSUER_ID"),
        ("APPLE_ID", "APPLE_PASSWORD", "APPLE_TEAM_ID"),
    )
    assert sign_mod.required_secret_names() == (
        *sign_mod.SIGNING_SECRETS,
        *sign_mod.ASC_SECRETS,
        *sign_mod.APPLE_ID_SECRETS,
    )


def test_sign_secret_names_match_the_ws02_requirements_registry():
    from shipit.release import secretreq

    assert sign_mod.SIGNING_SECRETS == secretreq.SIGN_MAC_CERT_SECRETS
    assert sign_mod.ASC_SECRETS == secretreq.ASC_NOTARY_SECRETS
    assert sign_mod.APPLE_ID_SECRETS == secretreq.APPLE_ID_NOTARY_SECRETS
    assert sign_mod.NOTARY_SECRET_SETS == tuple(
        alt.names for alt in secretreq.NOTARY_SECRETS.alternatives
    )


def test_resolve_signing_missing_cert_hard_fails_naming_it():
    with pytest.raises(ReleaseError, match="APPLE_CERTIFICATE is not set"):
        sign_mod.resolve_signing({})


def test_resolve_signing_empty_password_is_valid():
    signing = sign_mod.resolve_signing({"APPLE_CERTIFICATE": "Y2VydA=="})
    assert signing.cert_password == ""


def test_resolve_notary_asc_wins_when_both_styles_present():
    creds = sign_mod.resolve_notary(FULL_ENV)
    assert creds.style == "asc"
    assert (creds.key_b64, creds.key_id, creds.issuer_id) == (
        "cDgta2V5",
        "KEYID123",
        "issuer-uuid",
    )


def test_resolve_notary_falls_back_to_apple_id_on_partial_asc():
    env = dict(APPLE_ID_ENV, ASC_API_KEY_BASE64="cDgta2V5")
    creds = sign_mod.resolve_notary(env)
    assert creds.style == "apple-id"
    assert (creds.apple_id, creds.password, creds.team_id) == (
        "dev@example.com",
        "app-specific",
        "TEAM123",
    )


def test_resolve_notary_neither_complete_names_the_missing_of_both_sets():
    env = {"ASC_API_KEY_ID": "KEYID123", "APPLE_ID": "dev@example.com"}
    with pytest.raises(ReleaseError) as excinfo:
        sign_mod.resolve_notary(env)
    message = str(excinfo.value)
    for name in (
        "ASC_API_KEY_BASE64",
        "ASC_API_ISSUER_ID",
        "APPLE_PASSWORD",
        "APPLE_TEAM_ID",
    ):
        assert name in message
    assert "ASC_API_KEY_ID," not in message and "missing: APPLE_ID" not in message


def test_notary_args_asc_trio_with_key_path():
    creds = sign_mod.resolve_notary(FULL_ENV)
    assert sign_mod.notary_args(creds, Path("/tmp/AuthKey.p8")) == [
        "--key",
        "/tmp/AuthKey.p8",
        "--key-id",
        "KEYID123",
        "--issuer",
        "issuer-uuid",
    ]


def test_notary_args_apple_id_trio():
    creds = sign_mod.resolve_notary(APPLE_ID_ENV)
    assert sign_mod.notary_args(creds, None) == [
        "--apple-id",
        "dev@example.com",
        "--password",
        "app-specific",
        "--team-id",
        "TEAM123",
    ]


def test_notary_args_asc_without_key_path_is_a_domain_refusal():
    creds = sign_mod.resolve_notary(FULL_ENV)
    with pytest.raises(ReleaseError, match="requires the decoded .p8 key path"):
        sign_mod.notary_args(creds, None)


def test_codesign_argv_hardened_runtime_timestamp_and_entitlements():
    plain = sign_mod.codesign_argv(IDENTITY, Path("/x/App.app"))
    assert plain == [
        "codesign",
        "--force",
        "--sign",
        IDENTITY,
        "--options",
        "runtime",
        "--timestamp",
        "/x/App.app",
    ]
    with_ent = sign_mod.codesign_argv(
        IDENTITY, Path("/x/App.app"), Path("/x/ent.plist")
    )
    assert with_ent[-3:] == ["--entitlements", "/x/ent.plist", "/x/App.app"]
    with_kc = sign_mod.codesign_argv(
        IDENTITY, Path("/x/App.app"), keychain=Path("/tmp/sign.keychain-db")
    )
    assert with_kc[with_kc.index("--keychain") + 1] == "/tmp/sign.keychain-db"


def test_sign_order_puts_nested_first_and_the_app_last():
    nested = [Path("a/deep/helper"), Path("a/lib.dylib")]
    app = Path("a")
    assert sign_mod.sign_order(nested, app) == [*nested, app]


def test_entitlements_policy_default_is_empty():
    policy = sign_mod.EntitlementsPolicy()
    assert policy.is_empty
    assert not sign_mod.EntitlementsPolicy(app=Path("/a.plist")).is_empty


def test_entitlements_for_reads_the_role_off_the_path():
    policy = sign_mod.EntitlementsPolicy(
        app=Path("/app.plist"),
        helper=Path("/helper.plist"),
        appex=Path("/appex.plist"),
    )
    top = Path("/x/Lexed.app")
    helper = Path("/x/Lexed.app/Contents/Frameworks/Lexed Helper.app")
    appex = Path("/x/Lexed.app/Contents/PlugIns/QL.appex")
    framework = Path("/x/Lexed.app/Contents/Frameworks/Electron Framework.framework")
    dylib = Path("/x/Lexed.app/Contents/Frameworks/libnode.dylib")
    assert sign_mod.entitlements_for(top, is_top_app=True, policy=policy) == Path(
        "/app.plist"
    )
    assert sign_mod.entitlements_for(helper, is_top_app=False, policy=policy) == Path(
        "/helper.plist"
    )
    assert sign_mod.entitlements_for(appex, is_top_app=False, policy=policy) == Path(
        "/appex.plist"
    )
    assert sign_mod.entitlements_for(framework, is_top_app=False, policy=policy) is None
    assert sign_mod.entitlements_for(dylib, is_top_app=False, policy=policy) is None


def test_entitlements_for_empty_policy_never_selects_anything():
    policy = sign_mod.EntitlementsPolicy()
    top = Path("/x/App.app")
    helper = Path("/x/App.app/Contents/Frameworks/Helper.app")
    assert sign_mod.entitlements_for(top, is_top_app=True, policy=policy) is None
    assert sign_mod.entitlements_for(helper, is_top_app=False, policy=policy) is None


def test_is_electron_keys_on_the_electron_framework(tmp_path):
    electron = _electron_app(tmp_path / "e", name="Lexed.app")
    assert sign_mod._is_electron(electron)
    native = _fixture_app(tmp_path / "n")
    assert not sign_mod._is_electron(native)


@pytest.mark.parametrize(
    "head,verdict",
    [
        (b"\xcf\xfa\xed\xfe" + b"\x00" * 4, True),
        (b"\xfe\xed\xfa\xce" + b"\x00" * 4, True),
        (b"\xca\xfe\xba\xbe\x00\x00\x00\x02", True),
        (b"\xca\xfe\xba\xbe\x00\x03\x00\x34", False),
        (b"#!/bin/sh\n", False),
        (b"\xcf\xfa", False),
    ],
)
def test_is_macho_detects_by_content(tmp_path, head, verdict):
    target = tmp_path / "candidate"
    target.write_bytes(head)
    assert sign_mod.is_macho(target) is verdict


def _fixture_app(root: Path, name: str = "Phos.app") -> Path:
    app = root / name
    macos = app / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    (macos / "phos").write_bytes(MACHO_64)
    (macos / "gen_fixtures").write_bytes(MACHO_64)
    fw = app / "Contents" / "Frameworks" / "Foo.framework"
    (fw / "Versions" / "A").mkdir(parents=True)
    (fw / "Versions" / "A" / "Foo").write_bytes(MACHO_64)
    helper = app / "Contents" / "Frameworks" / "Helper.app"
    (helper / "Contents" / "MacOS").mkdir(parents=True)
    (helper / "Contents" / "MacOS" / "helper").write_bytes(MACHO_64)
    resources = app / "Contents" / "Resources"
    resources.mkdir(parents=True)
    (resources / "icon.png").write_bytes(b"\x89PNG....")
    (app / "Contents" / "Current").symlink_to("MacOS")
    return app


def test_nested_signable_inner_first_frameworks_opaque_symlinks_skipped(tmp_path):
    app = _fixture_app(tmp_path)
    paths = sign_mod.nested_signable(app)
    rel = [str(p.relative_to(app)) for p in paths]
    assert rel == [
        "Contents/Frameworks/Helper.app/Contents/MacOS/helper",
        "Contents/Frameworks/Foo.framework",
        "Contents/Frameworks/Helper.app",
        "Contents/MacOS/gen_fixtures",
        "Contents/MacOS/phos",
    ]
    assert app not in paths


def _electron_app(root: Path, name: str = "Lexed.app") -> Path:
    app = root / name
    macos = app / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    (macos / "Lexed").write_bytes(MACHO_64)
    frameworks = app / "Contents" / "Frameworks"
    ef = frameworks / "Electron Framework.framework" / "Versions" / "A"
    ef.mkdir(parents=True)
    (ef / "Electron Framework").write_bytes(MACHO_64)
    for kind in ("", " (GPU)", " (Renderer)", " (Plugin)"):
        helper = frameworks / f"Lexed Helper{kind}.app" / "Contents" / "MacOS"
        helper.mkdir(parents=True)
        (helper / f"Lexed Helper{kind}").write_bytes(MACHO_64)
    return app


def test_nested_signable_over_an_electron_app_orders_every_helper_inner_first(tmp_path):
    app = _electron_app(tmp_path)
    order = sign_mod.sign_order(sign_mod.nested_signable(app), app)
    rel = [str(p.relative_to(tmp_path)) for p in order]
    assert "Lexed.app/Contents/Frameworks/Electron Framework.framework" in rel
    for kind in ("", " (GPU)", " (Renderer)", " (Plugin)"):
        base = f"Lexed.app/Contents/Frameworks/Lexed Helper{kind}.app"
        inner = f"{base}/Contents/MacOS/Lexed Helper{kind}"
        assert rel.index(inner) < rel.index(base) < rel.index("Lexed.app")
    assert rel[-1] == "Lexed.app"


def test_nested_signable_lists_code_plugin_and_bundle_roots(tmp_path):
    app = tmp_path / "App.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "MacOS" / "app").write_bytes(MACHO_64)
    plugin = app / "Contents" / "PlugIns" / "Widget.plugin" / "Contents" / "MacOS"
    plugin.mkdir(parents=True)
    (plugin / "Widget").write_bytes(MACHO_64)
    bundle = app / "Contents" / "Resources" / "Code.bundle" / "Contents" / "MacOS"
    bundle.mkdir(parents=True)
    (bundle / "Code").write_bytes(MACHO_64)

    rel = [str(p.relative_to(app)) for p in sign_mod.nested_signable(app)]
    assert "Contents/PlugIns/Widget.plugin" in rel
    assert "Contents/Resources/Code.bundle" in rel
    assert rel.index("Contents/PlugIns/Widget.plugin") > rel.index(
        "Contents/PlugIns/Widget.plugin/Contents/MacOS/Widget"
    )


def test_nested_signable_skips_data_only_resource_bundle(tmp_path):
    app = tmp_path / "App.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "MacOS" / "app").write_bytes(MACHO_64)
    res = app / "Contents" / "Resources" / "Assets.bundle" / "Contents" / "Resources"
    res.mkdir(parents=True)
    (res / "icon.png").write_bytes(b"\x89PNG....")
    (res.parent / "Info.plist").write_text("<plist/>")

    rel = [str(p.relative_to(app)) for p in sign_mod.nested_signable(app)]
    assert not any("Assets.bundle" in r for r in rel)
    assert rel == ["Contents/MacOS/app"]


class SignRecorder:
    def __init__(self, tmp_path: Path, *, statuses=("Accepted",), effects=None):
        self.calls: list[tuple[tuple[str, ...], float]] = []
        self.tmp_path = tmp_path
        self.statuses = list(statuses)
        self.effects = dict(effects or {})

    def __call__(self, argv, timeout):
        argv = tuple(str(a) for a in argv)
        self.calls.append((argv, timeout))
        override = self.effects.get(argv[0])
        if override is not None:
            result = override(argv)
            if result is not None:
                return result
        return self._respond(argv)

    def _respond(self, argv):
        stdout = ""
        if argv[0] == "security" and argv[1] == "find-identity":
            stdout = FIND_IDENTITY_OUT
        elif argv == ("security", "list-keychains", "-d", "user"):
            stdout = LIST_KEYCHAINS_OUT
        elif argv[0] == "hdiutil":
            Path(argv[-1]).write_bytes(b"signed-dmg")
        elif argv[:3] == ("xcrun", "notarytool", "submit"):
            stdout = json.dumps({"id": "sub-123", "status": "In Progress"})
        elif argv[:3] == ("xcrun", "notarytool", "info"):
            status = self.statuses.pop(0) if self.statuses else "In Progress"
            stdout = json.dumps({"status": status})
        elif argv[:3] == ("xcrun", "notarytool", "log"):
            stdout = '{"issues": [{"message": "nested code unhardened"}]}'
        return execrun.ExecResult(
            argv=argv, rc=0, stdout=stdout, stderr="", duration_ms=1
        )

    @property
    def argvs(self):
        return [argv for argv, _ in self.calls]

    def heads(self, *prefix):
        return [argv for argv in self.argvs if argv[: len(prefix)] == prefix]


def _fixture_tree(tmp_path: Path) -> Path:
    app = _fixture_app(tmp_path / "src")
    tree = tmp_path / "dist"
    tree.mkdir()
    _make_targz(tree / "app.unsigned-app.tar.gz", app.parent, [app.name])
    (tree / "Phos_1.0.0_aarch64.dmg").write_bytes(b"unsigned-dmg")
    return tree


def _request(tmp_path, recorder, *, env=FULL_ENV, out=None, **overrides):
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    uniqs = iter(("u1", "u2", "u3"))
    defaults = dict(
        tree=tmp_path / "dist",
        out_dir=out or (tmp_path / "dist"),
        scratch=scratch,
        run_cmd=recorder,
        env=env,
        uniq=lambda: next(uniqs),
        mint_pass=lambda: "kc-pass",
        sleep=lambda seconds: None,
    )
    defaults.update(overrides)
    return sign_mod.SignRequest(**defaults)


def test_sign_bundle_full_recorded_sequence(tmp_path):
    _fixture_tree(tmp_path)
    recorder = SignRecorder(tmp_path)

    result = sign_mod.sign_bundle(_request(tmp_path, recorder))

    scratch = tmp_path / "scratch"
    kc1, kc2 = (str(scratch / f"signing-{u}.keychain-db") for u in ("u1", "u2"))
    cert1, cert2 = (str(scratch / f"cert-{u}.p12") for u in ("u1", "u2"))
    app = scratch / "unpacked" / "Phos.app"
    signed_dmg = str(scratch / "signed.dmg")

    def keychain_setup(kc, cert):
        return [
            ("security", "create-keychain", "-p", "kc-pass", kc),
            ("security", "set-keychain-settings", "-lut", "3600", kc),
            ("security", "unlock-keychain", "-p", "kc-pass", kc),
            (
                "security",
                "import",
                cert,
                "-k",
                kc,
                "-P",
                "p12pass",
                "-T",
                "/usr/bin/codesign",
            ),
            (
                "security",
                "set-key-partition-list",
                "-S",
                "apple-tool:,apple:",
                "-s",
                "-k",
                "kc-pass",
                kc,
            ),
            ("security", "list-keychains", "-d", "user"),
            ("security", "list-keychains", "-d", "user", "-s", kc, LOGIN_KEYCHAIN),
            ("security", "find-identity", "-v", "-p", "codesigning", kc),
        ]

    def signs(path, kc):
        return [
            (
                "codesign",
                "--force",
                "--sign",
                IDENTITY,
                "--options",
                "runtime",
                "--timestamp",
                "--keychain",
                kc,
                str(path),
            ),
            ("codesign", "--verify", "--strict", str(path)),
        ]

    inner_first = [
        app / "Contents/Frameworks/Helper.app/Contents/MacOS/helper",
        app / "Contents/Frameworks/Foo.framework",
        app / "Contents/Frameworks/Helper.app",
        app / "Contents/MacOS/gen_fixtures",
        app / "Contents/MacOS/phos",
        app,
    ]
    expected = [
        *keychain_setup(kc1, cert1),
        *[argv for path in inner_first for argv in signs(path, kc1)],
        ("security", "list-keychains", "-d", "user", "-s", LOGIN_KEYCHAIN),
        ("security", "delete-keychain", kc1),
        (
            "hdiutil",
            "create",
            "-volname",
            "Phos",
            "-srcfolder",
            str(scratch / "reseal"),
            "-ov",
            "-format",
            "UDZO",
            signed_dmg,
        ),
        *keychain_setup(kc2, cert2),
        *signs(signed_dmg, kc2),
        ("security", "list-keychains", "-d", "user", "-s", LOGIN_KEYCHAIN),
        ("security", "delete-keychain", kc2),
        (
            "xcrun",
            "notarytool",
            "submit",
            signed_dmg,
            "--key",
            str(scratch / "AuthKey.p8"),
            "--key-id",
            "KEYID123",
            "--issuer",
            "issuer-uuid",
            "--output-format",
            "json",
            "--no-wait",
        ),
        (
            "xcrun",
            "notarytool",
            "info",
            "sub-123",
            "--key",
            str(scratch / "AuthKey.p8"),
            "--key-id",
            "KEYID123",
            "--issuer",
            "issuer-uuid",
            "--output-format",
            "json",
        ),
        ("xcrun", "stapler", "staple", signed_dmg),
    ]
    assert recorder.argvs == expected

    staged = tmp_path / "dist" / "Phos_1.0.0_aarch64.dmg"
    assert staged.read_bytes() == b"signed-dmg"
    assert not Path(cert1).exists() and not Path(cert2).exists()
    assert not (scratch / "AuthKey.p8").exists()
    assert result == sign_mod.SignResult(
        app="Phos.app",
        dmg=str(staged),
        identity=IDENTITY,
        submission_id="sub-123",
        stapled=True,
        nested_signed=5,
    )
    assert (scratch / "reseal" / "Phos.app" / "Contents" / "Current").is_symlink()
    assert (scratch / "reseal" / "Applications").is_symlink()


def test_sign_bundle_apple_id_style_when_no_asc(tmp_path):
    _fixture_tree(tmp_path)
    recorder = SignRecorder(tmp_path)

    sign_mod.sign_bundle(_request(tmp_path, recorder, env=APPLE_ID_ENV))

    (submit,) = recorder.heads("xcrun", "notarytool", "submit")
    assert submit[4:10] == (
        "--apple-id",
        "dev@example.com",
        "--password",
        "app-specific",
        "--team-id",
        "TEAM123",
    )
    assert not (tmp_path / "scratch" / "AuthKey.p8").exists()


def test_sign_bundle_empty_cert_password_still_imports(tmp_path):
    _fixture_tree(tmp_path)
    recorder = SignRecorder(tmp_path)
    env = {k: v for k, v in FULL_ENV.items() if k != "APPLE_CERTIFICATE_PASSWORD"}

    sign_mod.sign_bundle(_request(tmp_path, recorder, env=env))

    imports = recorder.heads("security", "import")
    assert len(imports) == 2
    for argv in imports:
        assert argv[argv.index("-P") + 1] == ""


def test_sign_bundle_missing_all_secrets_fails_before_any_work(tmp_path):
    _fixture_tree(tmp_path)
    recorder = SignRecorder(tmp_path)
    with pytest.raises(ReleaseError, match="APPLE_CERTIFICATE is not set"):
        sign_mod.sign_bundle(_request(tmp_path, recorder, env={}))
    assert recorder.calls == []


def test_sign_bundle_missing_notary_secrets_fails_before_any_work(tmp_path):
    _fixture_tree(tmp_path)
    recorder = SignRecorder(tmp_path)
    env = {"APPLE_CERTIFICATE": "Y2VydA=="}
    with pytest.raises(ReleaseError, match="notarization needs one complete"):
        sign_mod.sign_bundle(_request(tmp_path, recorder, env=env))
    assert recorder.calls == []


def test_sign_bundle_refuses_zero_payloads(tmp_path):
    (tmp_path / "dist").mkdir()
    recorder = SignRecorder(tmp_path)
    with pytest.raises(ReleaseError, match=r"no \*\.unsigned-app\.tar\.gz"):
        sign_mod.sign_bundle(_request(tmp_path, recorder))


def test_sign_bundle_refuses_multiple_payloads(tmp_path):
    tree = _fixture_tree(tmp_path)
    (tree / "other.unsigned-app.tar.gz").write_bytes(b"tarball")
    recorder = SignRecorder(tmp_path)
    with pytest.raises(ReleaseError, match="found 2"):
        sign_mod.sign_bundle(_request(tmp_path, recorder))


def test_sign_bundle_refuses_multiple_dmgs(tmp_path):
    tree = _fixture_tree(tmp_path)
    (tree / "Other.dmg").write_bytes(b"second dmg")
    recorder = SignRecorder(tmp_path)
    with pytest.raises(ReleaseError, match=r"at most one \.dmg"):
        sign_mod.sign_bundle(_request(tmp_path, recorder))


def test_sign_bundle_refuses_a_payload_with_two_apps(tmp_path):
    tree = _fixture_tree(tmp_path)
    src = tmp_path / "src"
    _fixture_app(src, name="Other.app")
    _make_targz(tree / "app.unsigned-app.tar.gz", src, ["Phos.app", "Other.app"])

    recorder = SignRecorder(tmp_path)
    with pytest.raises(ReleaseError, match=r"exactly one \.app .* found 2"):
        sign_mod.sign_bundle(_request(tmp_path, recorder))


def test_untar_validated_extracts_a_confined_bundle(tmp_path):
    app = _fixture_app(tmp_path / "src")
    archive = tmp_path / "payload.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(app.parent / app.name, arcname="Phos.app")
        tar.addfile(_link_member("Phos.app/weird", "sub -> dir"))
        dotted = tarfile.TarInfo(".. ")
        dotted.size = 0
        tar.addfile(dotted, io.BytesIO(b""))

    work = tmp_path / "unpacked"
    sign_mod._untar_validated(archive, work, "reseal payload")

    assert (work / "Phos.app" / "Contents" / "Current").is_symlink()
    assert (work / ".. ").exists()


@pytest.mark.parametrize(
    "files,links,match",
    [
        ({"/etc/evil": b"x"}, [], "unsafe path in reseal payload"),
        ({"../../etc/evil": b"x"}, [], "unsafe path in reseal payload"),
        (
            {},
            [_link_member("Phos.app/e", "../../../../etc")],
            "link escaping reseal payload",
        ),
        ({}, [_link_member("Phos.app/e", "/etc/passwd")], "link escaping reseal"),
        (
            {},
            [_link_member("Phos.app/h", "/etc/passwd", hard=True)],
            "link escaping reseal payload",
        ),
        (
            {},
            [_link_member("Phos.app/safe -> bypass", "/etc/passwd")],
            "link escaping reseal payload",
        ),
        (
            {},
            [_link_member("Phos.app/a -> b", "../../../etc/passwd")],
            "link escaping reseal payload",
        ),
        (
            {"Phos.app/foo/../escaped": b"x"},
            [_link_member("Phos.app/foo", ".")],
            "unsafe path in reseal payload",
        ),
        (
            {},
            [_special_member("Phos.app/dev")],
            "non-regular member in reseal payload",
        ),
    ],
)
def test_untar_validated_mac_leg_refuses_escapes(tmp_path, files, links, match):
    archive = tmp_path / "payload.tar.gz"
    _make_targz_members(archive, files, links)
    work = tmp_path / "unpacked"
    with pytest.raises(ReleaseError, match=match):
        sign_mod._untar_validated(archive, work, "reseal payload")
    assert list(work.iterdir()) == []


@pytest.mark.parametrize(
    "files,links,match",
    [
        (
            {},
            [_link_member("pkg/Current", "bin")],
            "non-regular member in archive bundle",
        ),
        (
            {},
            [_link_member("pkg/safe -> bypass", "/etc/passwd")],
            "non-regular member in archive bundle",
        ),
        ({"../../etc/evil": b"x"}, [], "unsafe path in archive bundle"),
        ({}, [_special_member("pkg/dev")], "non-regular member in archive bundle"),
    ],
)
def test_untar_validated_archive_leg_refuses_links_and_escapes(
    tmp_path, files, links, match
):
    archive = tmp_path / "bundle.tar.gz"
    _make_targz_members(archive, files, links)
    work = tmp_path / "unpacked"
    with pytest.raises(ReleaseError, match=match):
        sign_mod._untar_validated(archive, work, "archive bundle", reject_links=True)
    assert list(work.iterdir()) == []


def test_untar_validated_wraps_a_corrupt_archive_as_a_domain_error(tmp_path):
    archive = tmp_path / "payload.tar.gz"
    archive.write_bytes(b"this is not a gzip stream")
    work = tmp_path / "unpacked"
    with pytest.raises(ReleaseError, match="cannot unpack reseal payload"):
        sign_mod._untar_validated(archive, work, "reseal payload")


def test_untar_validated_strips_setuid_and_setgid_bits(tmp_path):
    archive = tmp_path / "payload.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo("Phos.app/Contents/MacOS/tool")
        info.mode = 0o6755
        info.size = 3
        tar.addfile(info, io.BytesIO(b"bin"))

    work = tmp_path / "unpacked"
    sign_mod._untar_validated(archive, work, "reseal payload")

    mode = (work / "Phos.app" / "Contents" / "MacOS" / "tool").stat().st_mode
    assert mode & 0o7000 == 0
    assert mode & 0o0777 == 0o755


def test_sign_bundle_refuses_a_malicious_payload_before_signing(tmp_path):
    _fixture_tree(tmp_path)
    payload = tmp_path / "dist" / "app.unsigned-app.tar.gz"
    _make_targz_members(
        payload, {}, [_link_member("Phos.app/safe -> bypass", "/etc/passwd")]
    )

    recorder = SignRecorder(tmp_path)
    with pytest.raises(ReleaseError, match="link escaping reseal payload"):
        sign_mod.sign_bundle(_request(tmp_path, recorder))
    assert not recorder.heads("security")
    assert not recorder.heads("codesign")


def test_sign_bundle_no_identity_still_tears_the_keychain_down(tmp_path):
    _fixture_tree(tmp_path)

    def no_identity(argv):
        if argv[1] == "find-identity":
            return execrun.ExecResult(
                argv=argv,
                rc=0,
                stdout="0 valid identities found\n",
                stderr="",
                duration_ms=1,
            )
        return None

    recorder = SignRecorder(tmp_path, effects={"security": no_identity})
    with pytest.raises(ReleaseError, match="no codesigning identity"):
        sign_mod.sign_bundle(_request(tmp_path, recorder))
    assert (
        "security",
        "list-keychains",
        "-d",
        "user",
        "-s",
        LOGIN_KEYCHAIN,
    ) in recorder.argvs
    assert recorder.heads("security", "delete-keychain")
    assert not (tmp_path / "scratch" / "cert-u1.p12").exists()


def test_sign_bundle_never_restores_an_unread_search_list(tmp_path):
    _fixture_tree(tmp_path)

    def create_fails(argv):
        if argv[1] == "create-keychain":
            raise execrun.ExecError(list(argv), rc=1, stderr="boom", cause="exit")
        return None

    recorder = SignRecorder(tmp_path, effects={"security": create_fails})
    with pytest.raises(execrun.ExecError):
        sign_mod.sign_bundle(_request(tmp_path, recorder))
    setters = [
        argv for argv in recorder.heads("security", "list-keychains") if "-s" in argv
    ]
    assert setters == []
    assert recorder.heads("security", "delete-keychain")


def test_parse_keychain_list_reads_the_quoted_paths_in_order():
    out = (
        '    "/Users/runner/Library/Keychains/login.keychain-db"\n'
        '    "/Library/Keychains/extra.keychain"\n'
    )
    assert sign_mod._parse_keychain_list(out) == [
        "/Users/runner/Library/Keychains/login.keychain-db",
        "/Library/Keychains/extra.keychain",
    ]
    assert sign_mod._parse_keychain_list("") == []


def test_parse_keychain_list_keeps_a_path_with_an_embedded_quote_whole():
    out = '    "/tmp/my"quote.keychain"\n    "/Library/Keychains/extra.keychain"\n'
    assert sign_mod._parse_keychain_list(out) == [
        '/tmp/my"quote.keychain',
        "/Library/Keychains/extra.keychain",
    ]


def test_parse_keychain_list_keeps_a_path_with_an_embedded_newline_whole():
    out = '    "/tmp/my\nnewline.keychain"\n    "/Library/Keychains/extra.keychain"\n'
    assert sign_mod._parse_keychain_list(out) == [
        "/tmp/my\nnewline.keychain",
        "/Library/Keychains/extra.keychain",
    ]


def test_sign_bundle_invalid_notarization_fetches_the_log_and_fails(tmp_path):
    _fixture_tree(tmp_path)
    recorder = SignRecorder(tmp_path, statuses=("Invalid",))
    with pytest.raises(ReleaseError, match="notarization Invalid.*sub-123"):
        sign_mod.sign_bundle(_request(tmp_path, recorder))
    assert recorder.heads("xcrun", "notarytool", "log")
    assert not (tmp_path / "scratch" / "AuthKey.p8").exists()


def test_sign_bundle_unconfirmed_notarization_is_a_hard_fail(tmp_path):
    _fixture_tree(tmp_path)
    recorder = SignRecorder(tmp_path, statuses=())
    slept: list[float] = []
    request = _request(
        tmp_path, recorder, timeout_minutes=1, sleep=lambda s: slept.append(s)
    )
    with pytest.raises(ReleaseError, match="unconfirmed after 1 min"):
        sign_mod.sign_bundle(request)
    assert len(recorder.heads("xcrun", "notarytool", "info")) == 2
    assert slept == [sign_mod.POLL_INTERVAL]
    assert not recorder.heads("xcrun", "stapler")


def test_sign_bundle_non_positive_notary_timeout_hard_fails_before_any_work(tmp_path):
    _fixture_tree(tmp_path)
    recorder = SignRecorder(tmp_path)
    request = _request(tmp_path, recorder, timeout_minutes=0)
    with pytest.raises(ReleaseError, match="notary timeout must be at least 1 minute"):
        sign_mod.sign_bundle(request)
    assert recorder.calls == []


def test_sign_bundle_flaky_poll_counts_as_unknown_and_polling_continues(tmp_path):
    _fixture_tree(tmp_path)
    polls = iter(("boom", "Accepted"))

    def flaky_info(argv):
        if argv[:3] != ("xcrun", "notarytool", "info"):
            return None
        verdict = next(polls)
        if verdict == "boom":
            raise execrun.ExecError(list(argv), rc=1, stderr="transient", cause="exit")
        return execrun.ExecResult(
            argv=argv,
            rc=0,
            stdout=json.dumps({"status": verdict}),
            stderr="",
            duration_ms=1,
        )

    recorder = SignRecorder(tmp_path, effects={"xcrun": flaky_info})
    result = sign_mod.sign_bundle(_request(tmp_path, recorder))
    assert result.submission_id == "sub-123"


def test_sign_bundle_staple_failure_is_non_fatal(tmp_path):
    _fixture_tree(tmp_path)

    def staple_fails(argv):
        if argv[1] == "stapler":
            raise execrun.ExecError(list(argv), rc=65, stderr="ticket", cause="exit")
        return None

    recorder = SignRecorder(tmp_path, effects={"xcrun": staple_fails})
    result = sign_mod.sign_bundle(_request(tmp_path, recorder))
    assert result.stapled is False
    assert (tmp_path / "dist" / "Phos_1.0.0_aarch64.dmg").read_bytes() == b"signed-dmg"


def test_sign_bundle_non_electron_applies_no_entitlements(tmp_path):
    _fixture_tree(tmp_path)
    recorder = SignRecorder(tmp_path)

    sign_mod.sign_bundle(_request(tmp_path, recorder))

    signs = recorder.heads("codesign", "--force")
    assert signs
    assert all("--entitlements" not in argv for argv in signs)


def _electron_tree(tmp_path: Path, *, name: str = "Lexed") -> Path:
    app = _electron_app(tmp_path / "src", name=f"{name}.app")
    tree = tmp_path / "dist"
    tree.mkdir()
    _make_targz(tree / f"{name}.unsigned-app.tar.gz", app.parent, [app.name])
    (tree / f"{name}_1.2.3_aarch64.dmg").write_bytes(b"unsigned-dmg")
    return tree


def _ent_of(argv: tuple[str, ...]) -> str | None:
    if "--entitlements" not in argv:
        return None
    return argv[argv.index("--entitlements") + 1]


def test_sign_bundle_electron_applies_role_keyed_jit_entitlements(tmp_path):
    _electron_tree(tmp_path)
    recorder = SignRecorder(tmp_path)

    sign_mod.sign_bundle(_request(tmp_path, recorder))

    signs = recorder.heads("codesign", "--force")
    scratch = tmp_path / "scratch"
    app_plist = str(scratch / "electron-app.entitlements.plist")
    helper_plist = str(scratch / "electron-helper.entitlements.plist")
    dmg_pass = signs[-1]
    app_pass = signs[-2]
    assert dmg_pass[-1].endswith("signed.dmg")
    assert _ent_of(dmg_pass) is None
    assert app_pass[-1].endswith("Lexed.app")
    assert _ent_of(app_pass) == app_plist
    helper_passes = [
        a for a in signs if a[-1].endswith(".app") and not a[-1].endswith("Lexed.app")
    ]
    assert len(helper_passes) == 4
    assert all(_ent_of(a) == helper_plist for a in helper_passes)
    non_app = [
        a for a in signs if not a[-1].endswith(".app") and not a[-1].endswith(".dmg")
    ]
    assert non_app
    assert all(_ent_of(a) is None for a in non_app)


def test_sign_bundle_electron_entitlements_content_is_the_minimal_modern_set(tmp_path):
    _electron_tree(tmp_path)
    recorder = SignRecorder(tmp_path)

    captured: dict[str, str] = {}

    def capture(argv):
        if argv[0] == "codesign" and "--entitlements" in argv:
            p = Path(argv[argv.index("--entitlements") + 1])
            captured[p.name] = p.read_text()
        return None

    recorder = SignRecorder(tmp_path, effects={"codesign": capture})
    sign_mod.sign_bundle(_request(tmp_path, recorder))

    app_xml = captured["electron-app.entitlements.plist"]
    helper_xml = captured["electron-helper.entitlements.plist"]
    assert "com.apple.security.cs.allow-jit" in app_xml
    assert "com.apple.security.inherit" not in app_xml
    assert "com.apple.security.cs.allow-jit" in helper_xml
    assert "com.apple.security.inherit" in helper_xml
    for xml in (app_xml, helper_xml):
        assert "get-task-allow" not in xml
        assert "allow-unsigned-executable-memory" not in xml
        assert "disable-library-validation" not in xml


def test_sign_bundle_electron_appex_never_receives_jit(tmp_path):
    app = _electron_app(tmp_path / "src", name="Lexed.app")
    appex = app / "Contents" / "PlugIns" / "LexedQuickLook.appex" / "Contents" / "MacOS"
    appex.mkdir(parents=True)
    (appex / "LexedQuickLook").write_bytes(MACHO_64)
    tree = tmp_path / "dist"
    tree.mkdir()
    _make_targz(tree / "Lexed.unsigned-app.tar.gz", app.parent, [app.name])
    (tree / "Lexed_1.2.3_aarch64.dmg").write_bytes(b"unsigned-dmg")

    recorder = SignRecorder(tmp_path)
    sign_mod.sign_bundle(_request(tmp_path, recorder))

    signs = recorder.heads("codesign", "--force")
    appex_pass = next(a for a in signs if a[-1].endswith(".appex"))
    scratch = tmp_path / "scratch"
    app_plist = str(scratch / "electron-app.entitlements.plist")
    helper_plist = str(scratch / "electron-helper.entitlements.plist")
    assert _ent_of(appex_pass) not in (app_plist, helper_plist)
    assert _ent_of(appex_pass) is None


def test_sign_bundle_without_an_incoming_dmg_stages_under_the_app_name(tmp_path):
    _fixture_tree(tmp_path)
    (tmp_path / "dist" / "Phos_1.0.0_aarch64.dmg").unlink()
    recorder = SignRecorder(tmp_path)
    result = sign_mod.sign_bundle(_request(tmp_path, recorder))
    assert result.dmg == str(tmp_path / "dist" / "Phos.dmg")
    assert Path(result.dmg).read_bytes() == b"signed-dmg"


def test_sign_bundle_garbage_cert_base64_is_a_domain_refusal(tmp_path):
    _fixture_tree(tmp_path)
    recorder = SignRecorder(tmp_path)
    env = dict(FULL_ENV, APPLE_CERTIFICATE="not base64!!")
    with pytest.raises(ReleaseError, match="APPLE_CERTIFICATE is not valid base64"):
        sign_mod.sign_bundle(_request(tmp_path, recorder, env=env))


def test_run_sign_happy_path_emits_the_typed_result(tmp_path, capsys):
    tree = _fixture_tree(tmp_path)
    recorder = SignRecorder(tmp_path)

    rc = release_verb.run_sign(
        str(tree), as_json=True, run_cmd=recorder, env=FULL_ENV, sleep=lambda s: None
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["app"] == "Phos.app"
    assert payload["dmg"] == str(tree / "Phos_1.0.0_aarch64.dmg")
    assert payload["identity"] == IDENTITY
    assert payload["submission_id"] == "sub-123"
    assert payload["stapled"] is True
    assert payload["nested_signed"] == 5


def test_run_sign_missing_secrets_is_one_error_line(tmp_path, capsys):
    tree = _fixture_tree(tmp_path)
    rc = release_verb.run_sign(str(tree), run_cmd=SignRecorder(tmp_path), env={})
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("error: ")
    assert "APPLE_CERTIFICATE" in err


def test_run_sign_stages_into_out_dir_when_given(tmp_path, capsys):
    tree = _fixture_tree(tmp_path)
    out = tmp_path / "signed-out"
    recorder = SignRecorder(tmp_path)

    rc = release_verb.run_sign(str(tree), out=str(out), run_cmd=recorder, env=FULL_ENV)

    assert rc == 0
    assert (out / "Phos_1.0.0_aarch64.dmg").read_bytes() == b"signed-dmg"
    assert (tree / "Phos_1.0.0_aarch64.dmg").read_bytes() == b"unsigned-dmg"
    assert "signed + notarized Phos.app" in capsys.readouterr().out


ARCHIVE_STEM = "lex-aarch64-apple-darwin"
ARCHIVE_NAME = f"{ARCHIVE_STEM}.tar.gz"


class ArchiveRecorder(SignRecorder):
    def _respond(self, argv):
        if argv[0] == "tar" and argv[1] == "-czf":
            Path(argv[2]).write_bytes(b"signed-tar")
            return execrun.ExecResult(
                argv=argv, rc=0, stdout="", stderr="", duration_ms=1
            )
        return super()._respond(argv)


def _archive_tree(tmp_path: Path, *, binaries=("lex",)) -> Path:
    tree = tmp_path / "dist"
    tree.mkdir(parents=True)
    stage = tmp_path / "stage" / ARCHIVE_STEM
    stage.mkdir(parents=True)
    for name in binaries:
        (stage / name).write_bytes(MACHO_64)
    (stage / "README.md").write_text("docs, not Mach-O")
    _make_targz(tree / ARCHIVE_NAME, stage.parent, [ARCHIVE_STEM])
    loose = tree / ARCHIVE_STEM
    loose.mkdir()
    (loose / "lex").write_bytes(MACHO_64)
    return tree


def _keychain_setup(kc: str, cert: str, password: str = "p12pass") -> list[tuple]:
    return [
        ("security", "create-keychain", "-p", "kc-pass", kc),
        ("security", "set-keychain-settings", "-lut", "3600", kc),
        ("security", "unlock-keychain", "-p", "kc-pass", kc),
        (
            "security",
            "import",
            cert,
            "-k",
            kc,
            "-P",
            password,
            "-T",
            "/usr/bin/codesign",
        ),
        (
            "security",
            "set-key-partition-list",
            "-S",
            "apple-tool:,apple:",
            "-s",
            "-k",
            "kc-pass",
            kc,
        ),
        ("security", "list-keychains", "-d", "user"),
        ("security", "list-keychains", "-d", "user", "-s", kc, LOGIN_KEYCHAIN),
        ("security", "find-identity", "-v", "-p", "codesigning", kc),
    ]


def _codesigns(path: str, kc: str) -> list[tuple]:
    return [
        (
            "codesign",
            "--force",
            "--sign",
            IDENTITY,
            "--options",
            "runtime",
            "--timestamp",
            "--keychain",
            kc,
            path,
        ),
        ("codesign", "--verify", "--strict", path),
    ]


def test_detect_shape_routes_payload_to_mac_app_and_tarball_to_archive(tmp_path):
    mac_tree = _fixture_tree(tmp_path)
    assert sign_mod.detect_shape(mac_tree) == "mac-app"
    archive_tree = _archive_tree(tmp_path / "arch")
    assert sign_mod.detect_shape(archive_tree) == "archive"


def test_detect_shape_payload_wins_when_both_shapes_appear(tmp_path):
    tree = _fixture_tree(tmp_path)
    (tree / ARCHIVE_NAME).write_bytes(b"unsigned-tar")
    assert sign_mod.detect_shape(tree) == "mac-app"


def test_detect_shape_nothing_signable_is_a_hard_refusal(tmp_path):
    tree = tmp_path / "dist"
    tree.mkdir()
    (tree / "lex.deb").write_bytes(b"deb")
    with pytest.raises(ReleaseError, match="nothing signable"):
        sign_mod.detect_shape(tree)


def test_sign_archives_full_recorded_sequence(tmp_path):
    tree = _archive_tree(tmp_path)
    recorder = ArchiveRecorder(tmp_path)

    result = sign_mod.sign_archives(_request(tmp_path, recorder))

    scratch = tmp_path / "scratch"
    kc1 = str(scratch / "signing-u1.keychain-db")
    cert1 = str(scratch / "cert-u1.p12")
    binary = str(scratch / "archive-0" / ARCHIVE_STEM / "lex")
    zip_path = str(scratch / "lex-notarize.zip")
    signed_tar = str(scratch / f"signed-0-{ARCHIVE_NAME}")
    expected = [
        *_keychain_setup(kc1, cert1),
        *_codesigns(binary, kc1),
        ("security", "list-keychains", "-d", "user", "-s", LOGIN_KEYCHAIN),
        ("security", "delete-keychain", kc1),
        ("zip", "-j", zip_path, binary),
        (
            "xcrun",
            "notarytool",
            "submit",
            zip_path,
            "--key",
            str(scratch / "AuthKey.p8"),
            "--key-id",
            "KEYID123",
            "--issuer",
            "issuer-uuid",
            "--output-format",
            "json",
            "--no-wait",
        ),
        (
            "xcrun",
            "notarytool",
            "info",
            "sub-123",
            "--key",
            str(scratch / "AuthKey.p8"),
            "--key-id",
            "KEYID123",
            "--issuer",
            "issuer-uuid",
            "--output-format",
            "json",
        ),
        (
            "tar",
            "-czf",
            signed_tar,
            "-C",
            str(scratch / "archive-0"),
            "--",
            ARCHIVE_STEM,
        ),
    ]
    assert recorder.argvs == expected

    assert (tree / ARCHIVE_NAME).read_bytes() == b"signed-tar"
    assert not Path(cert1).exists()
    assert not (scratch / "AuthKey.p8").exists()
    assert not (scratch / "lex-notarize.zip").exists()
    assert result == sign_mod.ArchiveSignResult(
        archives=(str(tree / ARCHIVE_NAME),),
        binaries=("lex",),
        identity=IDENTITY,
        submission_ids=("sub-123",),
    )


def test_sign_archives_signs_every_binary_in_one_keychain_pass(tmp_path):
    _archive_tree(tmp_path, binaries=("lex", "lexd"))
    recorder = ArchiveRecorder(tmp_path, statuses=("Accepted", "Accepted"))

    result = sign_mod.sign_archives(_request(tmp_path, recorder))

    assert len(recorder.heads("security", "create-keychain")) == 1
    signed = [argv[-1] for argv in recorder.heads("codesign", "--force")]
    assert [Path(p).name for p in signed] == ["lex", "lexd"]
    assert len(recorder.heads("xcrun", "notarytool", "submit")) == 2
    assert result.binaries == ("lex", "lexd")
    assert result.submission_ids == ("sub-123", "sub-123")
    assert not recorder.heads("xcrun", "stapler")


def test_sign_archives_missing_secrets_fails_before_any_work(tmp_path):
    _archive_tree(tmp_path)
    recorder = ArchiveRecorder(tmp_path)
    with pytest.raises(ReleaseError, match="APPLE_CERTIFICATE is not set"):
        sign_mod.sign_archives(_request(tmp_path, recorder, env={}))
    assert recorder.calls == []


def test_sign_archives_never_applies_entitlements(tmp_path):
    _archive_tree(tmp_path)
    recorder = ArchiveRecorder(tmp_path)
    sign_mod.sign_archives(_request(tmp_path, recorder))
    signs = recorder.heads("codesign", "--force")
    assert signs
    assert all("--entitlements" not in argv for argv in signs)


def test_sign_archives_no_macho_is_a_hard_fail(tmp_path):
    _archive_tree(tmp_path, binaries=())
    recorder = ArchiveRecorder(tmp_path)
    with pytest.raises(ReleaseError, match="no Mach-O binary inside"):
        sign_mod.sign_archives(_request(tmp_path, recorder))
    assert not recorder.heads("security")


def test_sign_archives_rejects_a_traversal_member(tmp_path):
    tree = _archive_tree(tmp_path)
    _make_targz_members(tree / ARCHIVE_NAME, {"../../etc/evil": b"x"}, [])

    recorder = ArchiveRecorder(tmp_path)
    with pytest.raises(ReleaseError, match="unsafe path in archive bundle"):
        sign_mod.sign_archives(_request(tmp_path, recorder))
    assert not recorder.heads("security")


def test_sign_archives_rejects_a_symlink_member(tmp_path):
    tree = _archive_tree(tmp_path)
    _make_targz_members(
        tree / ARCHIVE_NAME, {}, [_link_member(f"{ARCHIVE_STEM}/evil", "../../../etc")]
    )

    recorder = ArchiveRecorder(tmp_path)
    with pytest.raises(ReleaseError, match="non-regular member in archive bundle"):
        sign_mod.sign_archives(_request(tmp_path, recorder))
    assert not recorder.heads("security")


def test_sign_archives_rejected_notarization_fails_before_any_reemit(tmp_path):
    tree = _archive_tree(tmp_path)
    original = (tree / ARCHIVE_NAME).read_bytes()
    recorder = ArchiveRecorder(tmp_path, statuses=("Invalid",))
    with pytest.raises(ReleaseError, match="notarization Invalid"):
        sign_mod.sign_archives(_request(tmp_path, recorder))
    assert not recorder.heads("tar", "-czf")
    assert (tree / ARCHIVE_NAME).read_bytes() == original


def test_sign_archives_reemit_copy_failure_leaves_the_unsigned_tarball_intact(
    tmp_path, monkeypatch
):
    tree = _archive_tree(tmp_path)
    original = (tree / ARCHIVE_NAME).read_bytes()
    recorder = ArchiveRecorder(tmp_path)

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(sign_mod.shutil, "copy2", boom)
    with pytest.raises(OSError, match="disk full"):
        sign_mod.sign_archives(_request(tmp_path, recorder))
    assert (tree / ARCHIVE_NAME).read_bytes() == original


def test_run_sign_dispatches_the_archive_tree_and_emits_the_typed_result(
    tmp_path, capsys
):
    tree = _archive_tree(tmp_path)
    recorder = ArchiveRecorder(tmp_path)

    rc = release_verb.run_sign(
        str(tree), as_json=True, run_cmd=recorder, env=FULL_ENV, sleep=lambda s: None
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "archives": [str(tree / ARCHIVE_NAME)],
        "binaries": ["lex"],
        "identity": IDENTITY,
        "submission_ids": ["sub-123"],
    }


def test_run_sign_archive_stages_into_out_dir_when_given(tmp_path, capsys):
    tree = _archive_tree(tmp_path)
    original = (tree / ARCHIVE_NAME).read_bytes()
    out = tmp_path / "dist-signed"
    recorder = ArchiveRecorder(tmp_path)

    rc = release_verb.run_sign(str(tree), out=str(out), run_cmd=recorder, env=FULL_ENV)

    assert rc == 0
    assert [p.name for p in sorted(out.iterdir())] == [ARCHIVE_NAME]
    assert (out / ARCHIVE_NAME).read_bytes() == b"signed-tar"
    assert (tree / ARCHIVE_NAME).read_bytes() == original
    assert "signed + notarized 1 binary" in capsys.readouterr().out


def test_run_sign_nothing_signable_is_one_error_line(tmp_path, capsys):
    tree = tmp_path / "dist"
    tree.mkdir()
    (tree / "notes.txt").write_text("nothing here")
    rc = release_verb.run_sign(str(tree), run_cmd=ArchiveRecorder(tmp_path), env={})
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("error: ")
    assert "nothing signable" in err
