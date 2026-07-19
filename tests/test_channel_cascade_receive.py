"""The consumer-side artifact-channel Cascade (ARF01-WS07 #956).

The RECEIVE end of the artifact-pinned Cascade: a producer release fans out a
`{upstream, version}` `repository_dispatch`, and the consumer bumps every
matching `[artifact-deps]` pin and opens a draft bump PR. Two layers tested:

- the PURE bump core (`parse_payload` + `bump_artifact_deps`) — payload
  validation, the surgical `version`-line edit across matching / non-matching /
  already-current / unknown-upstream entries, layout preservation, and the loud
  refusals (malformed payload, an entry the edit cannot locate) — over strings,
  no IO;
- the receive ORCHESTRATION (`receive`) — bump → re-render → re-solve →
  branch/commit/push → draft PR — with the git/gh adapters and the pixi
  re-render + lock re-solve recorded on fakes, so the whole flow is exercised
  with no network and no real install.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from shipit import config, gh, git
from shipit.channel import cascade_receive as cr
from shipit.channel.cascade_receive import CascadeError

# --------------------------------------------------------------------------
# parse_payload — loud at the boundary
# --------------------------------------------------------------------------


def test_parse_payload_canonicalizes_the_upstream_slug():
    p = cr.parse_payload("Lex-Fmt/Lex", "0.19.3")
    assert p.upstream == "lex-fmt/lex"  # lowercased, canonical
    assert p.version == "0.19.3"


def test_parse_payload_strips_version_whitespace():
    assert cr.parse_payload("a/b", "  1.2.3  ").version == "1.2.3"


@pytest.mark.parametrize("bad", [None, "", "   ", 123, ["a/b"]])
def test_parse_payload_rejects_a_bad_upstream(bad):
    with pytest.raises(CascadeError, match="upstream"):
        cr.parse_payload(bad, "0.1.0")


@pytest.mark.parametrize("bad", [None, "", "   ", 7])
def test_parse_payload_rejects_a_bad_version(bad):
    with pytest.raises(CascadeError, match="version"):
        cr.parse_payload("a/b", bad)


def test_parse_payload_rejects_a_non_owner_name_upstream():
    with pytest.raises(CascadeError, match="owner/name|slug"):
        cr.parse_payload("not-a-slug", "0.1.0")


# --------------------------------------------------------------------------
# bump_artifact_deps — the surgical text edit
# --------------------------------------------------------------------------

_TOML = """\
# A consumer with cross-repo pins.
[artifact-deps.lexd]
repo = "lex-fmt/lex"
version = "0.19.0"  # the CLI

[artifact-deps.lexd-lsp]
repo = "lex-fmt/lex"
version = "0.19.0"

[artifact-deps.other]
repo = "acme/widget"
version = "1.0.0"
"""


def _bump(text: str, upstream: str, version: str) -> cr.BumpResult:
    return cr.bump_artifact_deps(text, cr.parse_payload(upstream, version))


def test_bumps_every_matching_entry_and_leaves_non_matches_untouched():
    result = _bump(_TOML, "lex-fmt/lex", "0.20.1")

    packages = {b.package: (b.old_version, b.new_version) for b in result.bumped}
    assert packages == {
        "lexd": ("0.19.0", "0.20.1"),
        "lexd-lsp": ("0.19.0", "0.20.1"),
    }
    # The bump re-parses cleanly and the matching pins moved, the non-match held.
    deps = {
        d.package: d.version for d in config.load_artifact_deps(_parse(result.text))
    }
    assert deps == {"lexd": "0.20.1", "lexd-lsp": "0.20.1", "other": "1.0.0"}


def test_only_the_version_lines_change_comments_and_layout_preserved():
    result = _bump(_TOML, "lex-fmt/lex", "0.20.1")
    # The inline comment on lexd's version line survives; the header, repo lines,
    # and the blank-line layout are byte-identical except the two version values.
    assert "# the CLI" in result.text
    assert result.text.count("0.19.0") == 0
    assert result.text.splitlines()[0] == "# A consumer with cross-repo pins."
    # Exactly the two matching version values changed — diff is minimal.
    before = _TOML.splitlines()
    after = result.text.splitlines()
    changed = [(b, a) for b, a in zip(before, after, strict=True) if b != a]
    assert changed == [
        ('version = "0.19.0"  # the CLI', 'version = "0.20.1"  # the CLI'),
        ('version = "0.19.0"', 'version = "0.20.1"'),
    ]


def test_unknown_upstream_bumps_nothing_and_returns_the_text_unchanged():
    result = _bump(_TOML, "ghost/repo", "9.9.9")
    assert result.bumped == ()
    assert result.text == _TOML


def test_an_already_current_version_is_a_no_op():
    # A dispatch at the version every matching entry already carries changes
    # nothing — a redundant re-dispatch is inert (no PR downstream).
    result = _bump(_TOML, "lex-fmt/lex", "0.19.0")
    assert result.bumped == ()
    assert result.text == _TOML


def test_partial_already_current_bumps_only_the_stale_entry():
    text = _TOML.replace(
        'version = "0.19.0"  # the CLI', 'version = "0.20.1"  # the CLI'
    )
    # lexd now at 0.20.1, lexd-lsp still 0.19.0. A 0.20.1 dispatch moves only lsp.
    result = _bump(text, "lex-fmt/lex", "0.20.1")
    assert [b.package for b in result.bumped] == ["lexd-lsp"]


def test_conda_direct_repo_only_entry_is_skipped_not_a_refusal():
    # conda-direct (ADR-0077): a `[artifact-deps.<pkg>] { repo }` entry with NO
    # `version` owns its pin in `[dependencies]` (bumped by a generic bot), so the
    # Cascade has no shipit-managed version line to edit — it SKIPS the entry
    # rather than raising on an unlocatable line. A repo mid-migration (one versioned
    # entry, one conda-direct entry on the same upstream) bumps only the versioned one.
    text = (
        '[artifact-deps.lexd]\nrepo = "lex-fmt/lex"\nversion = "0.19.0"\n'
        '[artifact-deps.lexd-lsp]\nrepo = "lex-fmt/lex"\n'
    )
    result = _bump(text, "lex-fmt/lex", "0.20.0")
    assert [b.package for b in result.bumped] == ["lexd"]
    assert 'version = "0.20.0"' in result.text
    # The conda-direct entry is untouched — no spurious `version` line invented.
    assert result.text.count("version = ") == 1


def test_quoted_dotted_package_header_is_matched_and_bumped():
    text = '[artifact-deps."ruamel.yaml"]\nrepo = "acme/tools"\nversion = "0.17.0"\n'
    result = _bump(text, "acme/tools", "0.18.0")
    assert [b.package for b in result.bumped] == ["ruamel.yaml"]
    assert 'version = "0.18.0"' in result.text


def test_single_quoted_version_value_keeps_its_quote_style():
    text = "[artifact-deps.lexd]\nrepo = \"lex-fmt/lex\"\nversion = '0.19.0'\n"
    result = _bump(text, "lex-fmt/lex", "0.20.0")
    assert "version = '0.20.0'" in result.text


def test_inline_bump_anchors_on_the_whole_version_key_not_a_lookalike():
    # The inline `version = "…"` edit `.search`es the whole line, so it must
    # anchor on the WHOLE key: a look-alike sharing the tail (`previous_version`,
    # `other-version`) must be left alone and only the real `version` rewritten —
    # otherwise the surgical edit moves the wrong field and corrupts the config.
    # (Tested at `_bump_one` directly: the config layer rejects such extra keys
    # before `bump_artifact_deps`, so this locks the regex, its last line of
    # defense, against a hand-authored or future-schema line.)
    lines = [
        "[artifact-deps]",
        'lexd = { repo = "lex-fmt/lex", previous_version = "0.1.0", '
        'version = "0.19.0" }',
    ]
    old = cr._bump_one(lines, "lexd", "0.20.0")
    assert old == "0.19.0"
    assert 'previous_version = "0.1.0"' in lines[1]  # untouched
    assert 'version = "0.20.0"' in lines[1]
    assert "0.19.0" not in lines[1]


def test_inline_table_form_is_bumped_in_place():
    text = (
        "[artifact-deps]\n"
        'lexd = { repo = "lex-fmt/lex", version = "0.19.0" }\n'
        'keep = { repo = "acme/x", version = "1.0.0" }\n'
    )
    result = _bump(text, "lex-fmt/lex", "0.20.0")
    assert [b.package for b in result.bumped] == ["lexd"]
    assert 'lexd = { repo = "lex-fmt/lex", version = "0.20.0" }' in result.text
    assert 'keep = { repo = "acme/x", version = "1.0.0" }' in result.text


def test_an_unlocatable_matching_entry_refuses_rather_than_corrupting():
    # A dotted-key layout tomllib accepts but the surgical edit does not model:
    # the entry is a match, but there is no `[artifact-deps.pkg]` table nor a
    # `pkg = { … }` inline line for the scanner to rewrite.
    text = '[artifact-deps]\nlexd.repo = "lex-fmt/lex"\nlexd.version = "0.19.0"\n'
    with pytest.raises(CascadeError, match="could not locate"):
        _bump(text, "lex-fmt/lex", "0.20.0")


def test_malformed_shipit_toml_fails_loud():
    with pytest.raises(config.ConfigError):
        _bump("[artifact-deps.lexd\nrepo =", "lex-fmt/lex", "0.20.0")


def test_malformed_artifact_deps_entry_fails_loud():
    # A missing `repo` is a construction-is-validation error from the parser.
    text = '[artifact-deps.lexd]\nversion = "0.19.0"\n'
    with pytest.raises(config.ConfigError):
        _bump(text, "lex-fmt/lex", "0.20.0")


def _parse(text: str) -> dict:
    import tomllib

    return tomllib.loads(text)


# --------------------------------------------------------------------------
# The managed receive-workflow unit
# --------------------------------------------------------------------------


def test_receive_workflow_unit_is_a_whole_file_managed_unit():
    unit = cr.receive_workflow_unit()
    assert unit.dest == ".github/workflows/shipit-artifact-cascade.yml"
    assert unit.kind == "file"
    body = unit.content.decode("utf-8")
    assert "repository_dispatch" in body
    assert "types: [upstream-release]" in body
    assert "shipit channel receive" in body
    # The payload rides ENV, never spliced into the run: line (injection-safe).
    assert "UPSTREAM: ${{ github.event.client_payload.upstream }}" in body


def test_receive_workflow_passes_shipits_own_yamllint():
    # #1057: the generated cascade workflow must pass the SAME strict yamllint the
    # fleet ships (line-length max 120), so no consumer needs a `[lint].ignore`
    # for the managed bytes. Render it and run the shipped canonical config over
    # it — a regression guard so a >120-char generator line can't silently return.
    from yamllint import linter
    from yamllint.config import YamlLintConfig

    from shipit import lint

    body = cr.receive_workflow_unit().content.decode("utf-8")
    cfg = YamlLintConfig(file=lint.data_path("yamllint.yaml"))
    problems = [str(p) for p in linter.run(body, cfg)]
    assert problems == [], problems


def test_receive_workflow_no_line_exceeds_the_120_column_cap():
    # #1057: the direct invariant behind the yamllint gate — the managed workflow
    # keeps every line within the 120-column cap shipit's yamllint enforces.
    body = cr.receive_workflow_unit().content.decode("utf-8")
    long = [ln for ln in body.splitlines() if len(ln) > 120]
    assert long == [], long


def test_receive_workflow_pixi_run_uses_the_double_dash_separator():
    # #1057: `pixi run … -- ./bin/shipit …` — without `--`, pixi may treat
    # `./bin/shipit` as a task name and fail at runtime. The generator must emit
    # the separator so the managed workflow runs the launcher, not a phantom task.
    body = cr.receive_workflow_unit().content.decode("utf-8")
    assert "pixi run --locked -- ./bin/shipit channel receive" in body


def test_receive_workflow_guards_against_foreign_upstream_release_payloads():
    # ARF01-WS08: `upstream-release` is SHARED with the pre-existing
    # notify-downstreams rail (ADR-0067 reuses that dispatch rail), whose payload
    # carries no `upstream` key. A repo that is both a notify-downstreams
    # downstream and an [artifact-deps] consumer receives that foreign payload on
    # this same event; the workflow must NO-OP (exit 0) on an empty UPSTREAM
    # rather than call `shipit channel receive ""` (which rightly errors) — so
    # the foreign dispatch is inert, never a red run or a corrupt bump.
    body = cr.receive_workflow_unit().content.decode("utf-8")
    guard = body.index('if [ -z "$UPSTREAM" ]')
    exit0 = body.index("exit 0", guard)
    receive = body.index("./bin/shipit channel receive")
    # The empty-upstream guard and its early exit come BEFORE the receive call.
    assert guard < exit0 < receive


def test_install_delivers_the_workflow_only_when_artifact_deps_declared(tmp_path):
    from shipit.verbs import install as install_verb

    # No [artifact-deps]: no artifact units at all (workflow included).
    (tmp_path / ".shipit.toml").write_text("[reviewers]\n")
    assert install_verb._artifact_dep_units(tmp_path) == []

    # With a declared pin: the workflow unit leads the projected blocks.
    (tmp_path / ".shipit.toml").write_text(
        '[artifact-deps.lexd]\nrepo = "lex-fmt/lex"\nversion = "0.19.0"\n'
    )
    units = install_verb._artifact_dep_units(tmp_path, is_private=lambda slug: False)
    assert units[0].dest == ".github/workflows/shipit-artifact-cascade.yml"
    assert len(units) > 1  # + the projected pixi blocks


# --------------------------------------------------------------------------
# receive — the orchestration, with git/gh recorded on fakes
# --------------------------------------------------------------------------


class _Recorder:
    """Records the git/gh calls receive makes, standing in for the real acts."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.existing_pr: str | None = None

    # git
    def current_branch(self, *, cwd):
        return "main"

    def switch_create(self, branch, *, cwd):
        self.calls.append(("switch_create", branch))

    def switch(self, ref, *, cwd):
        self.calls.append(("switch", ref))

    def add(self, paths, *, cwd):
        self.calls.append(("add", tuple(paths)))

    def commit(self, message, paths, *, cwd, no_verify=False):
        self.calls.append(("commit", message, no_verify))

    def push(self, branch, *, cwd, remote="origin", force=False, no_verify=False):
        self.calls.append(("push", branch, force, no_verify))

    # gh
    def pr_url_for_head(self, branch, *, cwd=None):
        return self.existing_pr

    def pr_create(self, *, head, title, body, draft, cwd, **kw):
        self.calls.append(("pr_create", head, draft))
        self.pr_title = title
        self.pr_body = body
        return "https://github.com/acme/repo/pull/7"

    def names(self):
        return [c[0] for c in self.calls]


@pytest.fixture
def rec(monkeypatch):
    r = _Recorder()
    for name in ("current_branch", "switch_create", "switch", "add", "commit", "push"):
        monkeypatch.setattr(git, name, getattr(r, name))
    for name in ("pr_url_for_head", "pr_create"):
        monkeypatch.setattr(gh, name, getattr(r, name))
    return r


def _consumer(tmp_path: Path) -> Path:
    (tmp_path / ".shipit.toml").write_text(_TOML)
    (tmp_path / "pixi.toml").write_text("[workspace]\nname = 'demo'\n")
    return tmp_path


def test_receive_bumps_reinstalls_and_opens_a_draft_pr(tmp_path, rec):
    root = _consumer(tmp_path)
    reinstalled: list[Path] = []

    result = cr.receive(
        root, "lex-fmt/lex", "0.20.1", reinstall=lambda r: reinstalled.append(r)
    )

    # The pins were written to .shipit.toml.
    deps = {
        d.package: d.version
        for d in config.load_artifact_deps(_parse((root / ".shipit.toml").read_text()))
    }
    assert deps == {"lexd": "0.20.1", "lexd-lsp": "0.20.1", "other": "1.0.0"}

    # The pixi block was re-rendered.
    assert reinstalled == [root]

    # A DRAFT PR was opened; order: branch -> add -> commit -> push -> pr -> restore.
    assert rec.names() == [
        "switch_create",
        "add",
        "commit",
        "push",
        "pr_create",
        "switch",
    ]
    assert ("pr_create", result.branch, True) in rec.calls
    assert ("switch", "main") in rec.calls
    # Both changed files are staged/committed.
    assert ("add", (config.CONFIG_NAME, "pixi.toml")) in rec.calls
    # The commit bypasses hooks, like install's own managed commits.
    assert ("commit", rec.pr_title, True) in rec.calls
    # The push FORCES: the deterministic bump branch is re-created from HEAD each
    # run, so a re-dispatch must update the existing remote branch, not fail
    # non-fast-forward. (force=True, no_verify=True.)
    assert ("push", result.branch, True, True) in rec.calls
    assert result.url == "https://github.com/acme/repo/pull/7"
    assert result.branch == "shipit/artifact-bump/lex-fmt-lex-0.20.1"
    assert "lexd" in rec.pr_body and "0.20.1" in rec.pr_body
    assert "for #956" in rec.pr_body


def test_receive_on_unknown_upstream_is_a_clean_no_op(tmp_path, rec):
    root = _consumer(tmp_path)
    before = (root / ".shipit.toml").read_text()
    reinstalled: list[Path] = []

    result = cr.receive(
        root, "ghost/repo", "9.9.9", reinstall=lambda r: reinstalled.append(r)
    )

    assert result.bumped == ()
    assert result.branch is None and result.url is None
    assert (root / ".shipit.toml").read_text() == before  # untouched
    assert rec.calls == []  # no git, no PR
    assert reinstalled == []  # no re-render either


def test_receive_reuses_an_existing_pr_for_the_same_bump(tmp_path, rec):
    root = _consumer(tmp_path)
    rec.existing_pr = "https://github.com/acme/repo/pull/3"

    result = cr.receive(root, "lex-fmt/lex", "0.20.1", reinstall=lambda r: None)

    assert result.url == "https://github.com/acme/repo/pull/3"
    assert "pr_create" not in rec.names()  # reused, not re-opened


def test_receive_commits_only_shipit_toml_when_there_is_no_pixi_manifest(tmp_path, rec):
    root = tmp_path
    (root / ".shipit.toml").write_text(_TOML)  # no pixi.toml on disk

    cr.receive(root, "lex-fmt/lex", "0.20.1", reinstall=lambda r: None)

    assert ("add", (config.CONFIG_NAME,)) in rec.calls


def test_receive_stages_the_re_resolved_pixi_lock(tmp_path, rec):
    # The reinstall seam's re-solve re-resolves pixi.lock; it rides the bump
    # commit so CI's `--locked` install stays green against the new pin.
    root = _consumer(tmp_path)
    (root / "pixi.lock").write_text("version: 6\n")

    cr.receive(root, "lex-fmt/lex", "0.20.1", reinstall=lambda r: None)

    assert ("add", (config.CONFIG_NAME, "pixi.toml", "pixi.lock")) in rec.calls


def test_default_reinstall_re_solves_the_lock_after_the_tree_render(
    tmp_path, monkeypatch
):
    # The lock-refresh bug guard: tree-mode `shipit install` re-renders the pixi
    # BLOCK but returns BEFORE self-cert's lint-env solve, so it never touches
    # pixi.lock. `_default_reinstall` must therefore run an EXPLICIT `pixi
    # install` re-solve after the render — otherwise the cascade stages a STALE
    # lock and the bump PR's `pixi run --locked` workflow fails CI / resolves the
    # OLD artifact. Both the render and the (lock-mutating) solve are faked.
    root = tmp_path
    (root / "pixi.toml").write_text("[workspace]\nname = 'demo'\n")
    (root / "pixi.lock").write_text("version: 6\nstale-lock\n")
    calls: list[str] = []

    def _fake_install_run(path: str, **_kw) -> int:
        calls.append(f"render:{path}")
        return 0

    def _fake_pixi_install(root_arg, *, environment, env):
        # The real `pixi install` re-solve is what rewrites the lock; the fake
        # mutates it so the test proves the lock is re-resolved, not just that
        # the seam is called.
        calls.append(f"solve:{environment}")
        (Path(root_arg) / "pixi.lock").write_text("version: 6\nfresh-lock\n")

    monkeypatch.setattr("shipit.verbs.install.run", _fake_install_run)
    monkeypatch.setattr(cr.pixienv, "install", _fake_pixi_install)

    cr._default_reinstall(root)

    assert calls == [f"render:{root}", f"solve:{cr.LINT_ENV}"]
    assert (root / "pixi.lock").read_text() == "version: 6\nfresh-lock\n"


def test_default_reinstall_raises_a_cascade_error_when_the_re_solve_fails(
    tmp_path, monkeypatch
):
    # A failed re-solve fails the whole receive CLOSED (the bump-owned triple is
    # rolled back by `receive`): the pixi ExecError becomes a CascadeError, never
    # a stale lock quietly staged into the bump PR.
    root = tmp_path
    (root / "pixi.toml").write_text("[workspace]\nname = 'demo'\n")

    def _boom(root_arg, *, environment, env):
        raise cr.execrun.ExecError(
            ["pixi", "install", "--environment", "lint"], rc=1, stderr="solve blew up"
        )

    monkeypatch.setattr("shipit.verbs.install.run", lambda path, **_kw: 0)
    monkeypatch.setattr(cr.pixienv, "install", _boom)

    with pytest.raises(CascadeError, match="re-solving `pixi.lock`"):
        cr._default_reinstall(root)


def test_receive_force_pushes_the_deterministic_bump_branch(tmp_path, rec):
    # A re-dispatch reuses the branch/PR: switch_create re-cuts the branch from
    # HEAD, so the push must force to update the existing remote branch rather
    # than fail non-fast-forward.
    root = _consumer(tmp_path)
    rec.existing_pr = "https://github.com/acme/repo/pull/3"

    result = cr.receive(root, "lex-fmt/lex", "0.20.1", reinstall=lambda r: None)

    assert ("push", result.branch, True, True) in rec.calls
    assert "pr_create" not in rec.names()  # the forced push refreshed the open PR


def test_receive_rolls_the_whole_triple_back_when_the_re_render_fails(tmp_path, rec):
    # Atomicity across the WHOLE bump-owned triple: `reinstall` is the real
    # `shipit install` (MODE_TREE) plus the `pixi install` lock re-solve, which
    # rewrite pixi.toml/pixi.lock BEFORE it
    # can fail — so a failure that has already touched them must restore all
    # three, not just .shipit.toml, or the tree is left pin-vs-projection
    # desynced. The stub here mutates pixi.toml AND pixi.lock, then raises.
    root = _consumer(tmp_path)
    (root / "pixi.lock").write_text("version: 6\nold-lock\n")
    before = {
        p: (root / p).read_text() for p in (".shipit.toml", "pixi.toml", "pixi.lock")
    }

    def _boom(r: Path):
        # Simulate install's partial writes landing before the failure.
        (r / "pixi.toml").write_text("[workspace]\nname = 'demo'\n# re-rendered\n")
        (r / "pixi.lock").write_text("version: 6\nnew-lock\n")
        raise CascadeError("re-render failed")

    with pytest.raises(CascadeError, match="re-render failed"):
        cr.receive(root, "lex-fmt/lex", "0.20.1", reinstall=_boom)

    # Every bump-owned file is byte-restored to its pre-bump state.
    for p, text in before.items():
        assert (root / p).read_text() == text
    assert rec.calls == []  # no branch, no commit, no PR


def test_receive_rollback_deletes_files_the_re_render_created(tmp_path, rec):
    # A pixi.lock that did not exist before must be DELETED on rollback (the
    # failed re-render's solve created it), not left behind as orphaned state.
    root = _consumer(tmp_path)  # writes .shipit.toml + pixi.toml, no pixi.lock
    assert not (root / "pixi.lock").is_file()

    def _boom(r: Path):
        (r / "pixi.lock").write_text("version: 6\n")  # created by the solve
        raise CascadeError("re-render failed")

    with pytest.raises(CascadeError, match="re-render failed"):
        cr.receive(root, "lex-fmt/lex", "0.20.1", reinstall=_boom)

    assert not (root / "pixi.lock").is_file()  # created-then-failed → removed


def test_receive_missing_shipit_toml_is_a_clean_refusal(tmp_path, rec):
    # A de-shipit'd repo whose cascade workflow still fires: no .shipit.toml to
    # bump. The read must be a loud CascadeError (mapped to error: … + exit 1 by
    # the CLI shell), never a raw FileNotFoundError traceback in the workflow log.
    root = tmp_path  # no .shipit.toml on disk
    with pytest.raises(CascadeError, match="no .shipit.toml"):
        cr.receive(root, "lex-fmt/lex", "0.20.1", reinstall=lambda r: None)
    assert rec.calls == []
