"""`shipit release prepare` — recorded-invocation tests over the injected seams.

The shell is driven end-to-end against real tmp-path trees (the changelog
read/roll and manifest edits hit the real filesystem) with the TWO effectful
boundaries recorded (PRD Testing Decisions): the adapter-command Exec seam
(``run_cmd`` — exact command lines, exact cwds) and the git adapter surface
(``gitio`` — a recorded fixture whose reads are scripted and whose mutations
are captured, so the resume path is verified through the exec seam's shape
without a live repo). Prior art: the build verb's recorder tests.
"""

import json

import pytest

from shipit.identity import Sha
from shipit.release import version as version_mod
from shipit.verbs import release as release_verb

BASE_SHA = Sha("a" * 40)
BUMP_SHA = Sha("b" * 40)
TAG_SHA = Sha("c" * 40)


def spec(raw):
    return version_mod.parse_spec(raw)


class FakeGit:
    """A recorded git fixture: reads are scripted, mutations are captured.

    ``status_lines`` scripts the post-bump ``git status --porcelain`` answer
    (the recorded shape of what the bump commands changed); ``commit()``
    advances ``head`` to :data:`BUMP_SHA` exactly like the real adapter's
    commit would. Every mutating call lands in ``calls`` for exact-order
    assertions. ``commit``'s signature deliberately has NO ``no_verify``
    parameter: a bypass attempt (story 24's forbidden path) would fail the
    test as a ``TypeError``, structurally.
    """

    def __init__(self, *, tags=(), status_lines=(), branch="main"):
        self.tags = list(tags)
        self.status_lines = list(status_lines)
        self.branch = branch
        self.head = BASE_SHA
        self.calls = []

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
        return list(self.status_lines)

    def add(self, paths, *, cwd):
        self.calls.append(("add", tuple(paths)))

    def commit(self, message, paths, *, cwd):
        self.calls.append(("commit", message, tuple(paths)))
        self.head = BUMP_SHA

    def tag_annotated(self, name, message, *, cwd):
        self.calls.append(("tag", name, message))

    def push(self, branch, *, cwd):
        self.calls.append(("push", branch))

    def push_tag(self, name, *, cwd):
        self.calls.append(("push_tag", name))

    def reset_hard(self, rev, *, cwd):
        self.calls.append(("reset_hard", rev))
        self.head = BASE_SHA

    def mutated(self):
        """The mutating verbs recorded, in order (reads filtered out)."""
        return [c[0] for c in self.calls if c[0] != "resolve_commit"]


class CmdRecorder:
    """The adapter-command exec boundary: records ``(argv, cwd)``, runs nothing."""

    def __init__(self):
        self.calls = []

    def __call__(self, argv, cwd):
        self.calls.append((tuple(argv), cwd))


def make_repo(tmp_path, monkeypatch, *, toml, fragments=("unreleased-x.md",), files=()):
    """A tmp-path repo: ``.shipit.toml``, a changelog tree, extra ``files``."""
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


# --------------------------------------------------------------------------
# The fresh final cut — bump, roll, commit, tag, push, typed outputs
# --------------------------------------------------------------------------


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

    # The manifest projection and the changelog roll happened on disk.
    assert 'version = "0.2.0"' in (python_repo / "pyproject.toml").read_text()
    assert (python_repo / "CHANGELOG" / "0.2.0.md").is_file()
    assert not (python_repo / "CHANGELOG" / "unreleased-x.md").exists()

    # Stage-only-intended-files, then commit → tag → push branch → push tag.
    assert fake.mutated() == ["add", "commit", "tag", "push", "push_tag"]
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
    assert "- a fix" in tag[2]  # the annotation carries THE notes text
    assert ("push", "main") in fake.calls
    assert ("push_tag", "v0.2.0") in fake.calls

    # Uniform typed outputs (--json), consumed without re-parsing.
    out = json.loads(capsys.readouterr().out)
    assert out["version"] == "0.2.0"
    assert out["tag"] == "v0.2.0"
    assert out["release_sha"] == str(BUMP_SHA)
    assert out["prerelease"] is False
    assert out["resume"] is False
    assert out["branch"] == "main"

    # The notes artifact holds the same text the tag annotation carries.
    notes = (python_repo / release_verb.DEFAULT_NOTES_FILE).read_text()
    assert notes == tag[2]


def test_recorded_adapter_command_lines_per_leg(tmp_path, monkeypatch):
    """Exact command lines, exact leg cwds — rust workspace bump + lock
    refresh at the rust leg, npm version at the npm leg (PRD Testing
    Decisions). A prerelease cut, so the changelog only extracts."""
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
    # -rc.N: notes extracted, fragments KEPT for the final, branch still pushed.
    assert (root / "CHANGELOG" / "unreleased-x.md").is_file()
    assert not (root / "CHANGELOG" / "1.0.0-rc.1.md").exists()
    assert fake.mutated() == ["add", "commit", "tag", "push", "push_tag"]
    add = next(c for c in fake.calls if c[0] == "add")
    assert "CHANGELOG.md" not in add[1]  # nothing rolled on a prerelease


def test_bundle_config_hook_bumps_in_lockstep(tmp_path, monkeypatch):
    """Story 25: the artifact-declared hook bumps tauri.conf.json alongside
    the leg adapters — no "tauri" dispatch label anywhere."""
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


# --------------------------------------------------------------------------
# Resume (ADR-0009/0041) — tag exists → skip everything, re-emit the SHA
# --------------------------------------------------------------------------


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
    # Bump skipped entirely: no adapter command, no git mutation of any kind.
    assert recorder.calls == []
    assert fake.mutated() == []
    assert ("resolve_commit", "v1.2.3^{commit}") in fake.calls
    out = json.loads(capsys.readouterr().out)
    assert out["resume"] is True
    assert out["release_sha"] == str(TAG_SHA)
    assert out["prerelease"] is False
    assert out["branch"] is None
    # The identical notes re-emitted from the committed section (ADR-0009).
    assert "- the fix" in (root / release_verb.DEFAULT_NOTES_FILE).read_text()
    # The manifest is untouched by a resume.
    assert 'version = "0.1.0"' in (root / "pyproject.toml").read_text()


# --------------------------------------------------------------------------
# Refusals — empty release, no-op bump, detached HEAD
# --------------------------------------------------------------------------


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
    # The refusal fired BEFORE any bump: manifest untouched, nothing run.
    assert recorder.calls == []
    assert fake.mutated() == []
    assert 'version = "0.1.0"' in (root / "pyproject.toml").read_text()


def test_noop_bump_is_a_hard_error_never_an_empty_commit(python_repo, capsys):
    """The tree already carries the version but the tag does not exist: the
    leg's declared files change nothing → hard error, no commit (story 24)."""
    (python_repo / "pyproject.toml").write_text(
        _PYPROJECT.replace("0.1.0", "0.2.0"), encoding="utf-8"
    )
    fake = gitio_for(python_repo, status_lines=[" M CHANGELOG.md"])
    rc = release_verb.run_prepare(spec("0.2.0"), gitio=fake, run_cmd=CmdRecorder())
    assert rc == 1
    err = capsys.readouterr().err
    assert "no-op bump" in err
    assert "python leg" in err
    assert fake.mutated() == []  # nothing committed, nothing pushed


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


# --------------------------------------------------------------------------
# -release-rc — the tag-only live-fire contract (legacy release#663)
# --------------------------------------------------------------------------


def test_release_rc_is_tag_only_and_unadvances_the_branch(python_repo, capsys):
    fake = gitio_for(python_repo, status_lines=[" M pyproject.toml"])
    rc = release_verb.run_prepare(
        spec("0.2.0-release-rc"), as_json=True, gitio=fake, run_cmd=CmdRecorder()
    )
    assert rc == 0
    # Commit lands, tag names it, then the branch ref moves BACK and only the
    # tag is pushed — the branch's version line stays clean.
    assert fake.mutated() == ["add", "commit", "tag", "reset_hard", "push_tag"]
    assert ("reset_hard", str(BASE_SHA)) in fake.calls
    assert ("push_tag", "v0.2.0-release-rc") in fake.calls
    assert not any(c[0] == "push" for c in fake.calls)
    out = json.loads(capsys.readouterr().out)
    assert out["prerelease"] is True
    assert out["tag_only"] is True
    assert out["branch"] is None
    assert out["release_sha"] == str(BUMP_SHA)
    # Prerelease: fragments kept for the final.
    assert (python_repo / "CHANGELOG" / "unreleased-x.md").is_file()


# --------------------------------------------------------------------------
# The go leg — zero files, tag-only version carriage (story 22)
# --------------------------------------------------------------------------


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
    assert recorder.calls == []  # the zero-file adapter runs nothing
    add = next(c for c in fake.calls if c[0] == "add")
    assert set(add[1]) == {
        "CHANGELOG.md",
        "CHANGELOG/1.0.0.md",
        "CHANGELOG/unreleased-x.md",
    }


def test_go_prerelease_tags_head_without_a_commit(tmp_path, monkeypatch, capsys):
    """A go repo's -rc.N cut changes NOTHING on disk: no commit at all — the
    tag names the current HEAD (the tag alone carries the version)."""
    root = make_repo(tmp_path, monkeypatch, toml='[toolchains]\n"." = "go"\n')
    fake = gitio_for(root, status_lines=[])
    rc = release_verb.run_prepare(
        spec("1.0.0-rc.1"), as_json=True, gitio=fake, run_cmd=CmdRecorder()
    )
    assert rc == 0
    assert fake.mutated() == ["tag", "push", "push_tag"]
    out = json.loads(capsys.readouterr().out)
    assert out["release_sha"] == str(BASE_SHA)
    assert out["prerelease"] is True
