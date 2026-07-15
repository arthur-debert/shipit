"""The consumer-side artifact-channel Cascade (ARF01-WS07 #956).

The RECEIVE end of the artifact-pinned Cascade: a producer release fans out a
`{upstream, version}` `repository_dispatch`, and the consumer bumps every
matching `[artifact-deps]` pin and opens a draft bump PR. Two layers tested:

- the PURE bump core (`parse_payload` + `bump_artifact_deps`) — payload
  validation, the surgical `version`-line edit across matching / non-matching /
  already-current / unknown-upstream entries, layout preservation, and the loud
  refusals (malformed payload, an entry the edit cannot locate) — over strings,
  no IO;
- the receive ORCHESTRATION (`receive`) — bump → re-render → branch/commit/push
  → draft PR — with the git/gh adapters and the pixi re-render recorded on
  fakes, so the whole flow is exercised with no network and no real install.
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
    # The re-render's solve re-resolves pixi.lock; it rides the bump commit so
    # CI's `--locked` install stays green against the new pin.
    root = _consumer(tmp_path)
    (root / "pixi.lock").write_text("version: 6\n")

    cr.receive(root, "lex-fmt/lex", "0.20.1", reinstall=lambda r: None)

    assert ("add", (config.CONFIG_NAME, "pixi.toml", "pixi.lock")) in rec.calls


def test_receive_force_pushes_the_deterministic_bump_branch(tmp_path, rec):
    # A re-dispatch reuses the branch/PR: switch_create re-cuts the branch from
    # HEAD, so the push must force to update the existing remote branch rather
    # than fail non-fast-forward.
    root = _consumer(tmp_path)
    rec.existing_pr = "https://github.com/acme/repo/pull/3"

    result = cr.receive(root, "lex-fmt/lex", "0.20.1", reinstall=lambda r: None)

    assert ("push", result.branch, True, True) in rec.calls
    assert "pr_create" not in rec.names()  # the forced push refreshed the open PR


def test_receive_rolls_shipit_toml_back_when_the_re_render_fails(tmp_path, rec):
    # Atomicity: a re-render failure after the pins are written must restore the
    # original .shipit.toml, never strand a half-edited tree (bumped pins, stale
    # pixi block).
    root = _consumer(tmp_path)
    before = (root / ".shipit.toml").read_text()

    def _boom(_root):
        raise CascadeError("re-render failed")

    with pytest.raises(CascadeError, match="re-render failed"):
        cr.receive(root, "lex-fmt/lex", "0.20.1", reinstall=_boom)

    assert (root / ".shipit.toml").read_text() == before  # rolled back
    assert rec.calls == []  # no branch, no commit, no PR
