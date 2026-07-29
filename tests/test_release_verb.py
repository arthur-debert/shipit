import json

import pytest

from shipit import execrun
from shipit.identity import Sha
from shipit.release import version as version_mod
from shipit.verbs import release as release_verb

BASE_SHA = Sha("a" * 40)
BUMP_SHA = Sha("b" * 40)
TAG_SHA = Sha("c" * 40)


def spec(raw):
    return version_mod.parse_spec(raw)


class FakeGit:
    def __init__(
        self, *, tags=(), status_lines=(), branch="main", pre_status=(), fail_on=None
    ):
        self.tags = list(tags)
        self.status_lines = list(status_lines)
        self.branch = branch
        self.pre_status = list(pre_status)
        self.fail_on = fail_on
        self.head = BASE_SHA
        self.calls = []
        self._status_calls = 0

    def _maybe_fail(self, verb):
        if verb == self.fail_on:
            raise execrun.ExecError(["git", verb], rc=1, stderr="boom")

    def repo_root(self, *, cwd):
        return self.root

    def list_tags(self, *, cwd):
        return list(self.tags)

    def resolve_commit(self, rev, *, cwd):
        self.calls.append(("resolve_commit", rev))
        return TAG_SHA

    def current_branch(self, *, cwd):
        return self.branch

    def head_commit(self, *, cwd):
        return self.head

    def status_porcelain(self, *, cwd):
        self._status_calls += 1
        return list(self.pre_status if self._status_calls == 1 else self.status_lines)

    def add(self, paths, *, cwd):
        self.calls.append(("add", tuple(paths)))

    def commit(self, message, paths, *, cwd):
        self.calls.append(("commit", message, tuple(paths)))
        self.head = BUMP_SHA

    def tag_annotated(self, name, message, *, cwd):
        self.calls.append(("tag", name, message))
        self.tags.append(name)

    def push(self, branch, *, cwd):
        self.calls.append(("push", branch))
        self._maybe_fail("push")

    def push_tag(self, name, *, cwd):
        self.calls.append(("push_tag", name))
        self._maybe_fail("push_tag")

    def push_atomic(self, branch, tag, *, cwd):
        self.calls.append(("push_atomic", branch, tag))
        self._maybe_fail("push_atomic")

    def delete_tag(self, name, *, cwd):
        self.calls.append(("delete_tag", name))
        if name in self.tags:
            self.tags.remove(name)

    def reset_hard(self, rev, *, cwd):
        self.calls.append(("reset_hard", rev))
        self.head = BASE_SHA

    def mutated(self):
        return [c[0] for c in self.calls if c[0] != "resolve_commit"]


class CmdRecorder:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, cwd):
        self.calls.append((tuple(argv), cwd))


def make_repo(tmp_path, monkeypatch, *, toml, fragments=("unreleased-x.md",), files=()):
    (tmp_path / ".shipit.toml").write_text(toml, encoding="utf-8")
    changelog = tmp_path / "CHANGELOG"
    changelog.mkdir()
    for name in fragments:
        (changelog / name).write_text("### Fixed\n\n- a fix\n", encoding="utf-8")
    for rel, content in files:
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


_PYPROJECT = '[project]\nname = "demo"\nversion = "0.1.0"\n'
_PY_TOML = '[toolchains]\n"." = "python"\n'


@pytest.fixture
def python_repo(tmp_path, monkeypatch):
    return make_repo(
        tmp_path, monkeypatch, toml=_PY_TOML, files=[("pyproject.toml", _PYPROJECT)]
    )


def gitio_for(root, **kwargs):
    fake = FakeGit(**kwargs)
    fake.root = str(root)
    return fake


def test_final_cut_end_to_end(python_repo, capsys):
    fake = gitio_for(
        python_repo,
        tags=["v0.1.0"],
        status_lines=[
            " M pyproject.toml",
            " M CHANGELOG.md",
            "?? CHANGELOG/0.2.0.md",
            " D CHANGELOG/unreleased-x.md",
        ],
    )
    rc = release_verb.run_prepare(
        spec("0.2.0"), as_json=True, gitio=fake, run_cmd=CmdRecorder()
    )
    assert rc == 0

    assert 'version = "0.2.0"' in (python_repo / "pyproject.toml").read_text()
    assert (python_repo / "CHANGELOG" / "0.2.0.md").is_file()
    assert not (python_repo / "CHANGELOG" / "unreleased-x.md").exists()

    assert fake.mutated() == ["add", "commit", "tag", "push_atomic"]
    add = next(c for c in fake.calls if c[0] == "add")
    assert set(add[1]) == {
        "pyproject.toml",
        "CHANGELOG.md",
        "CHANGELOG/0.2.0.md",
        "CHANGELOG/unreleased-x.md",
    }
    commit = next(c for c in fake.calls if c[0] == "commit")
    assert commit[1] == "release: 0.2.0"
    tag = next(c for c in fake.calls if c[0] == "tag")
    assert tag[1] == "v0.2.0"
    assert "- a fix" in tag[2]
    assert ("push_atomic", "main", "v0.2.0") in fake.calls

    out = json.loads(capsys.readouterr().out)
    assert out["version"] == "0.2.0"
    assert out["tag"] == "v0.2.0"
    assert out["release_sha"] == str(BUMP_SHA)
    assert out["prerelease"] is False
    assert out["resume"] is False
    assert out["branch"] == "main"

    notes = (python_repo / release_verb.DEFAULT_NOTES_FILE).read_text()
    assert notes == tag[2]


def test_recorded_adapter_command_lines_per_leg(tmp_path, monkeypatch):
    root = make_repo(
        tmp_path,
        monkeypatch,
        toml='[toolchains]\n"." = "rust"\n"web" = "npm"\n',
        files=[("web/package.json", "{}")],
    )
    fake = gitio_for(
        root,
        status_lines=[
            " M Cargo.toml",
            " M Cargo.lock",
            " M web/package.json",
        ],
    )
    recorder = CmdRecorder()
    rc = release_verb.run_prepare(spec("1.0.0-rc.1"), gitio=fake, run_cmd=recorder)
    assert rc == 0
    assert recorder.calls == [
        (("cargo", "set-version", "--workspace", "1.0.0-rc.1"), root),
        (("cargo", "update", "--workspace"), root),
        (("npm", "version", "1.0.0-rc.1", "--no-git-tag-version"), root / "web"),
    ]
    assert (root / "CHANGELOG" / "unreleased-x.md").is_file()
    assert not (root / "CHANGELOG" / "1.0.0-rc.1.md").exists()
    assert fake.mutated() == ["add", "commit", "tag", "push_atomic"]
    add = next(c for c in fake.calls if c[0] == "add")
    assert "CHANGELOG.md" not in add[1]


def test_bundle_config_hook_bumps_in_lockstep(tmp_path, monkeypatch):
    conf = '{\n  "productName": "demo",\n  "version": "0.1.0"\n}\n'
    root = make_repo(
        tmp_path,
        monkeypatch,
        toml=(
            _PY_TOML
            + "[artifacts.app]\n"
            + 'build = ["python"]\n'
            + 'bundle-config = "src-tauri/tauri.conf.json"\n'
        ),
        files=[("pyproject.toml", _PYPROJECT), ("src-tauri/tauri.conf.json", conf)],
    )
    fake = gitio_for(
        root,
        status_lines=[
            " M pyproject.toml",
            " M src-tauri/tauri.conf.json",
            " M CHANGELOG.md",
            "?? CHANGELOG/0.2.0.md",
            " D CHANGELOG/unreleased-x.md",
        ],
    )
    rc = release_verb.run_prepare(spec("0.2.0"), gitio=fake, run_cmd=CmdRecorder())
    assert rc == 0
    assert '"version": "0.2.0"' in (root / "src-tauri/tauri.conf.json").read_text()
    add = next(c for c in fake.calls if c[0] == "add")
    assert "src-tauri/tauri.conf.json" in add[1]


def test_resume_reemits_tag_sha_and_notes(tmp_path, monkeypatch, capsys):
    root = make_repo(
        tmp_path,
        monkeypatch,
        toml=_PY_TOML,
        fragments=(),
        files=[
            ("pyproject.toml", _PYPROJECT),
            (
                "CHANGELOG/1.2.3.md",
                "## 1.2.3 - 2026-07-01\n\n### Fixed\n\n- the fix\n",
            ),
        ],
    )
    fake = gitio_for(root, tags=["v1.2.3"])
    recorder = CmdRecorder()
    rc = release_verb.run_prepare(
        spec("1.2.3"), as_json=True, gitio=fake, run_cmd=recorder
    )
    assert rc == 0
    assert recorder.calls == []
    assert fake.mutated() == []
    assert ("resolve_commit", "v1.2.3^{commit}") in fake.calls
    out = json.loads(capsys.readouterr().out)
    assert out["resume"] is True
    assert out["release_sha"] == str(TAG_SHA)
    assert out["prerelease"] is False
    assert out["branch"] is None
    assert "- the fix" in (root / release_verb.DEFAULT_NOTES_FILE).read_text()
    assert 'version = "0.1.0"' in (root / "pyproject.toml").read_text()


def test_empty_release_refused_before_any_mutation(tmp_path, monkeypatch, capsys):
    root = make_repo(
        tmp_path,
        monkeypatch,
        toml=_PY_TOML,
        fragments=(),
        files=[("pyproject.toml", _PYPROJECT)],
    )
    fake = gitio_for(root)
    recorder = CmdRecorder()
    rc = release_verb.run_prepare(spec("0.2.0"), gitio=fake, run_cmd=recorder)
    assert rc == 1
    assert "refusing an empty release" in capsys.readouterr().err
    assert recorder.calls == []
    assert fake.mutated() == []
    assert 'version = "0.1.0"' in (root / "pyproject.toml").read_text()


def test_noop_bump_is_a_hard_error_never_an_empty_commit(python_repo, capsys):
    (python_repo / "pyproject.toml").write_text(
        _PYPROJECT.replace("0.1.0", "0.2.0"), encoding="utf-8"
    )
    fake = gitio_for(python_repo, status_lines=[" M CHANGELOG.md"])
    rc = release_verb.run_prepare(spec("0.2.0"), gitio=fake, run_cmd=CmdRecorder())
    assert rc == 1
    err = capsys.readouterr().err
    assert "no-op bump" in err
    assert "python leg" in err
    assert fake.mutated() == []


def test_detached_head_refused_before_any_bump(python_repo, capsys):
    fake = gitio_for(python_repo, branch=None)
    recorder = CmdRecorder()
    rc = release_verb.run_prepare(spec("0.2.0"), gitio=fake, run_cmd=recorder)
    assert rc == 1
    assert "detached HEAD" in capsys.readouterr().err
    assert recorder.calls == []


def test_outside_a_checkout_is_refused(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    class NoRepo(FakeGit):
        def repo_root(self, *, cwd):
            return None

    rc = release_verb.run_prepare(spec("0.2.0"), gitio=NoRepo(), run_cmd=CmdRecorder())
    assert rc == 1
    assert "not inside a git checkout" in capsys.readouterr().err


def test_dirty_tree_is_refused_before_any_mutation(python_repo, capsys):
    fake = gitio_for(python_repo, pre_status=[" M pyproject.toml"])
    recorder = CmdRecorder()
    rc = release_verb.run_prepare(spec("0.2.0"), gitio=fake, run_cmd=recorder)
    assert rc == 1
    err = capsys.readouterr().err
    assert "uncommitted changes" in err
    assert recorder.calls == []
    assert fake.mutated() == []


def test_untracked_files_do_not_block_a_fresh_cut(python_repo):
    fake = gitio_for(
        python_repo,
        pre_status=["?? scratch.log", "?? RELEASE_NOTES.md"],
        status_lines=[" M pyproject.toml"],
    )
    rc = release_verb.run_prepare(spec("0.2.0-rc.1"), gitio=fake, run_cmd=CmdRecorder())
    assert rc == 0
    assert fake.mutated() == ["add", "commit", "tag", "push_atomic"]


def test_resume_is_not_blocked_by_a_dirty_tree(tmp_path, monkeypatch, capsys):
    root = make_repo(
        tmp_path,
        monkeypatch,
        toml=_PY_TOML,
        fragments=(),
        files=[
            ("pyproject.toml", _PYPROJECT),
            ("CHANGELOG/1.2.3.md", "## 1.2.3 - 2026-07-01\n\n### Fixed\n\n- a fix\n"),
        ],
    )
    fake = gitio_for(
        root,
        tags=["v1.2.3"],
        pre_status=[" M pyproject.toml", "?? RELEASE_NOTES.md"],
    )
    rc = release_verb.run_prepare(
        spec("1.2.3"), as_json=True, gitio=fake, run_cmd=CmdRecorder()
    )
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["resume"] is True
    assert fake.mutated() == []


def test_push_failure_deletes_the_local_tag(python_repo, capsys):
    fake = gitio_for(
        python_repo, status_lines=[" M pyproject.toml"], fail_on="push_atomic"
    )
    rc = release_verb.run_prepare(spec("0.2.0-rc.1"), gitio=fake, run_cmd=CmdRecorder())
    assert rc == 1
    assert ("push_atomic", "main", "v0.2.0-rc.1") in fake.calls
    assert ("delete_tag", "v0.2.0-rc.1") in fake.calls
    assert ("reset_hard", str(BASE_SHA)) in fake.calls
    assert "v0.2.0-rc.1" not in fake.tags


def test_push_failure_rollback_is_best_effort(python_repo, capsys):

    class DeleteRaises(FakeGit):
        def delete_tag(self, name, *, cwd):
            self.calls.append(("delete_tag", name))
            raise execrun.ExecError(["git", "tag", "-d"], rc=1, stderr="nope")

    fake = DeleteRaises(status_lines=[" M pyproject.toml"], fail_on="push_atomic")
    fake.root = str(python_repo)
    rc = release_verb.run_prepare(spec("0.2.0-rc.1"), gitio=fake, run_cmd=CmdRecorder())
    assert rc == 1
    assert ("delete_tag", "v0.2.0-rc.1") in fake.calls
    assert ("reset_hard", str(BASE_SHA)) in fake.calls


def test_release_rc_is_tag_only_and_unadvances_the_branch(python_repo, capsys):
    fake = gitio_for(python_repo, status_lines=[" M pyproject.toml"])
    rc = release_verb.run_prepare(
        spec("0.2.0-release-rc"), as_json=True, gitio=fake, run_cmd=CmdRecorder()
    )
    assert rc == 0
    assert fake.mutated() == ["add", "commit", "tag", "reset_hard", "push_tag"]
    assert ("reset_hard", str(BASE_SHA)) in fake.calls
    assert ("push_tag", "v0.2.0-release-rc") in fake.calls
    assert not any(c[0] == "push" for c in fake.calls)
    out = json.loads(capsys.readouterr().out)
    assert out["prerelease"] is True
    assert out["tag_only"] is True
    assert out["branch"] is None
    assert out["release_sha"] == str(BUMP_SHA)
    assert (python_repo / "CHANGELOG" / "unreleased-x.md").is_file()


def test_go_final_commits_only_the_changelog_roll(tmp_path, monkeypatch):
    root = make_repo(tmp_path, monkeypatch, toml='[toolchains]\n"." = "go"\n')
    fake = gitio_for(
        root,
        status_lines=[
            " M CHANGELOG.md",
            "?? CHANGELOG/1.0.0.md",
            " D CHANGELOG/unreleased-x.md",
        ],
    )
    recorder = CmdRecorder()
    rc = release_verb.run_prepare(spec("1.0.0"), gitio=fake, run_cmd=recorder)
    assert rc == 0
    assert recorder.calls == []
    add = next(c for c in fake.calls if c[0] == "add")
    assert set(add[1]) == {
        "CHANGELOG.md",
        "CHANGELOG/1.0.0.md",
        "CHANGELOG/unreleased-x.md",
    }


def test_go_prerelease_tags_head_without_a_commit(tmp_path, monkeypatch, capsys):
    root = make_repo(tmp_path, monkeypatch, toml='[toolchains]\n"." = "go"\n')
    fake = gitio_for(root, status_lines=[])
    rc = release_verb.run_prepare(
        spec("1.0.0-rc.1"), as_json=True, gitio=fake, run_cmd=CmdRecorder()
    )
    assert rc == 0
    assert fake.mutated() == ["tag", "push_atomic"]
    out = json.loads(capsys.readouterr().out)
    assert out["release_sha"] == str(BASE_SHA)
    assert out["prerelease"] is True


def test_unprovisioned_cargo_edit_aborts_with_the_reconcile_remedy(
    tmp_path, monkeypatch, capsys
):
    root = make_repo(tmp_path, monkeypatch, toml='[toolchains]\n"." = "rust"\n')
    fake = gitio_for(root)

    def run_cmd(argv, cwd):
        raise execrun.ExecError(
            list(argv), rc=101, stderr="error: no such command: `set-version`"
        )

    rc = release_verb.run_prepare(spec("0.2.0"), gitio=fake, run_cmd=run_cmd)
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("error: ")
    assert "cargo-edit" in err
    assert "`shipit install --pr`" in err
    assert "`shipit install --local`" in err
    assert "pixi.lock" in err
    assert "pixi.toml#shipit-rust-release-deps" in err
    assert fake.mutated() == []


def test_an_unknown_bump_failure_stays_the_untranslated_exec_error(
    tmp_path, monkeypatch, capsys
):
    root = make_repo(tmp_path, monkeypatch, toml='[toolchains]\n"." = "rust"\n')
    fake = gitio_for(root)

    def run_cmd(argv, cwd):
        raise execrun.ExecError(
            list(argv), rc=101, stderr="error: failed to parse manifest at Cargo.toml"
        )

    rc = release_verb.run_prepare(spec("0.2.0"), gitio=fake, run_cmd=run_cmd)
    assert rc == 1
    err = capsys.readouterr().err
    assert "failed to parse manifest" in err
    assert "shipit install" not in err
    assert fake.mutated() == []


def _missing_binary(argv):
    return execrun.ExecError(
        list(argv),
        rc=None,
        stderr=f"[Errno 2] No such file or directory: {argv[0]!r}",
        cause=execrun.CAUSE_MISSING_BINARY,
    )


def test_missing_cargo_binary_gets_the_reconcile_remedy(tmp_path, monkeypatch, capsys):
    root = make_repo(tmp_path, monkeypatch, toml='[toolchains]\n"." = "rust"\n')
    fake = gitio_for(root)

    def run_cmd(argv, cwd):
        raise _missing_binary(argv)

    rc = release_verb.run_prepare(spec("0.2.0"), gitio=fake, run_cmd=run_cmd)
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("error: ")
    assert "pixi.toml#shipit-rust-release-toolchain" in err
    assert "`shipit install --pr`" in err
    assert "`shipit install --local`" in err
    assert "pixi.lock" in err
    assert "cargo install" not in err
    assert fake.mutated() == []


def test_missing_npm_gets_the_reconcile_remedy(tmp_path, monkeypatch, capsys):
    root = make_repo(
        tmp_path,
        monkeypatch,
        toml='[toolchains]\n"." = "npm"\n',
        files=[("package.json", '{"name": "demo", "version": "0.1.0"}\n')],
    )
    fake = gitio_for(root)

    def run_cmd(argv, cwd):
        raise _missing_binary(argv)

    rc = release_verb.run_prepare(spec("0.2.0"), gitio=fake, run_cmd=run_cmd)
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("error: ")
    assert "pixi.toml#shipit-node-deps" in err
    assert "`shipit install --pr`" in err
    assert "`shipit install --local`" in err
    assert "pixi.lock" in err
    assert fake.mutated() == []


def test_a_non_missing_binary_launch_failure_stays_untranslated(
    tmp_path, monkeypatch, capsys
):
    root = make_repo(tmp_path, monkeypatch, toml='[toolchains]\n"." = "rust"\n')
    fake = gitio_for(root)

    def run_cmd(argv, cwd):
        raise execrun.ExecError(
            list(argv),
            rc=None,
            stderr="[Errno 13] Permission denied",
            cause=execrun.CAUSE_OS,
        )

    rc = release_verb.run_prepare(spec("0.2.0"), gitio=fake, run_cmd=run_cmd)
    assert rc == 1
    err = capsys.readouterr().err
    assert "shipit install" not in err
    assert fake.mutated() == []


_SECTION = "## 0.2.0 - 2026-01-01\n\n### Fixed\n\n- a fix\n"


def test_notes_reemits_the_committed_section_verbatim(tmp_path, monkeypatch, capsys):
    root = make_repo(
        tmp_path,
        monkeypatch,
        toml=_PY_TOML,
        fragments=(),
        files=[("CHANGELOG/0.2.0.md", _SECTION), ("pyproject.toml", _PYPROJECT)],
    )
    rc = release_verb.run_notes("0.2.0", gitio=gitio_for(root))
    assert rc == 0
    assert capsys.readouterr().out == "### Fixed\n\n- a fix\n"
    assert (root / "CHANGELOG" / "0.2.0.md").read_text(encoding="utf-8") == _SECTION


def test_notes_out_writes_the_file_and_reports(tmp_path, monkeypatch, capsys):
    root = make_repo(
        tmp_path,
        monkeypatch,
        toml=_PY_TOML,
        fragments=(),
        files=[("CHANGELOG/0.2.0.md", _SECTION)],
    )
    rc = release_verb.run_notes("0.2.0", out="RELEASE_NOTES.md", gitio=gitio_for(root))
    assert rc == 0
    assert (root / "RELEASE_NOTES.md").read_text(
        encoding="utf-8"
    ) == "### Fixed\n\n- a fix\n"
    assert "wrote" in capsys.readouterr().out


def test_notes_prerelease_extracts_from_the_fragments(tmp_path, monkeypatch, capsys):
    root = make_repo(tmp_path, monkeypatch, toml=_PY_TOML)
    rc = release_verb.run_notes("0.2.0-rc.1", gitio=gitio_for(root))
    assert rc == 0
    assert capsys.readouterr().out == "### Fixed\n\n- a fix\n"
    assert (root / "CHANGELOG" / "unreleased-x.md").is_file()


def test_notes_refuses_an_uncut_final_and_mutates_nothing(
    tmp_path, monkeypatch, capsys
):
    root = make_repo(tmp_path, monkeypatch, toml=_PY_TOML)
    rc = release_verb.run_notes("0.2.0", gitio=gitio_for(root))
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("error: ")
    assert "never cuts" in err
    assert (root / "CHANGELOG" / "unreleased-x.md").is_file()
    assert not (root / "CHANGELOG" / "0.2.0.md").exists()


def test_notes_refuses_outside_a_checkout(tmp_path, monkeypatch, capsys):
    class NoRepo(FakeGit):
        def repo_root(self, *, cwd):
            return None

    monkeypatch.chdir(tmp_path)
    rc = release_verb.run_notes("0.2.0", gitio=NoRepo())
    assert rc == 1
    assert "not inside a git checkout" in capsys.readouterr().err


def test_notes_empty_state_is_the_changelog_refusal(tmp_path, monkeypatch, capsys):
    root = make_repo(tmp_path, monkeypatch, toml=_PY_TOML, fragments=())
    rc = release_verb.run_notes("0.2.0", gitio=gitio_for(root))
    assert rc == 1
    assert "refusing an empty release" in capsys.readouterr().err
