"""cascade_receive — the consumer-side half of the artifact-pinned Cascade (ARF01-WS07).

The RECEIVE end of the artifact-channel Cascade (ADR-0067, docs/spec/artifact-channel.md,
#956). A producer release fans out (WS06) a ``repository_dispatch`` — event
``upstream-release`` with the shared payload contract
``{ "upstream": "<owner>/<repo>", "version": "<semver>" }`` — to every repo that
declares a dependency on it. This module is what the consumer runs when that
dispatch lands: it bumps **every** ``[artifact-deps]`` entry whose ``repo``
matches ``upstream`` to ``version``, re-renders the managed pixi block (WS02's
projection, via ``shipit install``) — whose solve re-resolves ``pixi.lock`` —
and opens a **draft** bump PR (carrying the bumped pins, re-rendered block, and
re-resolved lock) that then rides the normal review loop.

Two halves, kept apart so the decision core stays PURE and network-free:

- **the bump core** (:func:`parse_payload` + :func:`bump_artifact_deps`) — pure
  over strings: it validates the dispatch payload (malformed / empty fields fail
  loudly BEFORE anything is touched), canonicalises the ``upstream`` slug the
  same way the parse does, and returns the rewritten ``.shipit.toml`` TEXT plus
  the set of entries it bumped. It does a SURGICAL text edit — only the
  ``version`` value line of each matching entry changes, every comment / key /
  layout byte else is preserved — so a malformed or unknown-upstream payload can
  never corrupt ``.shipit.toml`` (an unknown upstream bumps nothing; an entry it
  cannot locate for a surgical edit raises rather than guessing).
- **the receive orchestration** (:func:`receive`) — the one effectful path:
  read ``.shipit.toml`` → bump → write → re-render the pixi block → branch,
  commit, push, open the draft PR. Every world-touching call goes through the
  ``git`` / ``gh`` adapters (recorded in tests) and the injectable ``reinstall``
  seam (the pixi re-render), so the flow is exercised end-to-end on fakes with
  no network and no real ``shipit install``.

Delivery (ADR-0066/0067): the receive **workflow** itself is a shipit-managed
unit (:func:`receive_workflow_unit`) — install-reconciled into a consumer's
``.github/workflows/`` ONLY when the repo declares ``[artifact-deps]`` (the
install verb appends it alongside WS02's projected pixi blocks), so a repo with
no cross-repo pin stays free of a dead workflow.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .. import config, gh, git
from ..identity import repo_from_slug
from ..install.units import Unit

#: The ``repository_dispatch`` event type the Cascade fires (the shared
#: WS06/WS07 contract, docs/spec/artifact-channel.md). The receive workflow
#: filters on this one stable name; the ``{upstream, version}`` rides the client
#: payload.
CASCADE_EVENT_TYPE = "upstream-release"

#: The delivered receive-workflow path in a consumer's checkout.
WORKFLOW_DEST = ".github/workflows/shipit-artifact-cascade.yml"
WORKFLOW_KEY = WORKFLOW_DEST

#: The bump branch prefix — one branch (and one draft PR) per (upstream, version)
#: dispatch, so a re-dispatch of the same version re-uses its branch/PR rather
#: than opening a duplicate.
BRANCH_PREFIX = "shipit/artifact-bump"


class CascadeError(RuntimeError):
    """A Cascade dispatch cannot be applied — a malformed / unknown-shape payload
    (:func:`parse_payload`) or a matching ``[artifact-deps]`` entry that cannot be
    surgically located for the version edit (:func:`bump_artifact_deps`). Raised
    BEFORE ``.shipit.toml`` is touched, so a bad dispatch never corrupts it."""


@dataclass(frozen=True)
class CascadePayload:
    """A validated Cascade dispatch payload — ``upstream`` canonicalised to its
    ``owner/name`` slug (so it compares equal to a parsed :class:`~shipit.config.ArtifactDep`
    ``repo``), ``version`` the semver the matching pins bump to."""

    upstream: str
    version: str


@dataclass(frozen=True)
class Bumped:
    """One ``[artifact-deps]`` entry the Cascade bumped: its package (the section
    key), the version it moved FROM, and the version it moved TO."""

    package: str
    old_version: str
    new_version: str


@dataclass(frozen=True)
class BumpResult:
    """The pure bump outcome: the rewritten ``.shipit.toml`` ``text`` and the
    entries it ``bumped``. ``bumped`` is empty for an unknown upstream (no entry
    matched) or a payload whose version every matching entry already carries — in
    both cases ``text`` is the input unchanged and the orchestration opens no PR."""

    text: str
    bumped: tuple[Bumped, ...]


@dataclass(frozen=True)
class ReceiveResult:
    """The receive outcome: the entries ``bumped``, and — when a bump happened —
    the ``branch`` and draft-PR ``url``. An unknown-upstream / already-current
    dispatch returns an empty ``bumped`` with ``branch``/``url`` ``None`` (a clean
    no-op — no branch, no PR)."""

    bumped: tuple[Bumped, ...]
    branch: str | None
    url: str | None


def parse_payload(upstream: object, version: object) -> CascadePayload:
    """Validate a Cascade dispatch payload into a typed :class:`CascadePayload`.

    Loud at the boundary (ADR-0030): a missing / non-string / empty field, or an
    ``upstream`` that is not an ``owner/name`` slug, raises :class:`CascadeError`
    naming what was wrong — so a malformed dispatch dies HERE, before
    ``.shipit.toml`` is read or touched. ``upstream`` is canonicalised through the
    one slug parser (:func:`shipit.identity.repo_from_slug`) so it matches a
    parsed :class:`~shipit.config.ArtifactDep` ``repo`` (also canonical) exactly.
    """
    if not isinstance(upstream, str) or not upstream.strip():
        raise CascadeError(
            f"cascade payload `upstream` must be a non-empty `owner/name` repo "
            f'slug, e.g. "lex-fmt/lex"; got {upstream!r}'
        )
    if not isinstance(version, str) or not version.strip():
        raise CascadeError(
            f"cascade payload `version` must be a non-empty version string, "
            f'e.g. "0.19.3"; got {version!r}'
        )
    try:
        canonical = repo_from_slug(upstream).slug
    except ValueError as exc:
        raise CascadeError(f"cascade payload `upstream`: {exc}") from exc
    return CascadePayload(upstream=canonical, version=version.strip())


# --------------------------------------------------------------------------
# The surgical text edit — replace only the matching entries' `version` value.
# --------------------------------------------------------------------------

#: A TOML table header line: ``[ a.b."c" ]`` (never an array-of-tables ``[[…]]``).
_HEADER_RE = re.compile(r"^\s*\[(?!\[)(?P<inner>[^\]]*)\]\s*(#.*)?$")

#: A dotted-key segment: a quoted string or a bare key. Used to split a header's
#: inner dotted path (``artifact-deps."ruamel.yaml"``) into its segments. The
#: quoted alternatives are deliberately ``[^"]``/``[^']`` (no escape handling):
#: a package name with an ESCAPED quote (``"pkg\"name"``) is not modelled and
#: makes :func:`_header_segments` return ``None`` → the header is skipped → the
#: entry is reported unlocatable and :func:`bump_artifact_deps` raises rather
#: than corrupting ``.shipit.toml``. That safe-fail is the intended behaviour:
#: conda/pixi package names never carry quotes, so the un-modelled case is inert.
_SEGMENT_RE = re.compile(r'\s*(?:"([^"]*)"|\'([^\']*)\'|([^.\s]+))\s*')

#: A ``version = "…"`` assignment line (double- OR single-quoted value),
#: capturing the prefix, quote char, value, and trailing remainder so the edit
#: preserves the quote style, spacing, and any inline comment.
_VERSION_LINE_RE = re.compile(
    r'^(?P<pre>\s*version\s*=\s*)(?P<q>["\'])(?P<val>[^"\']*)(?P=q)(?P<post>.*)$'
)

#: The key at the head of an inline-table entry line (``lexd-lsp = { … }``): a
#: quoted string or a bare key, before the ``=``.
_INLINE_KEY_RE = re.compile(
    r'^\s*(?:"(?P<qk>[^"]*)"|\'(?P<sk>[^\']*)\'|(?P<bk>[^\s=]+))\s*='
)

#: A ``version = "…"`` pair INSIDE an inline table (anywhere on the line). The
#: negative lookbehind anchors on the WHOLE key ``version``: without it the bare
#: ``version\s*=`` would ``.search``-match the tail of another key —
#: ``previous_version = "…"`` or ``other-version = "…"`` — and rewrite the wrong
#: field, leaving the real ``version`` untouched. (The header-table form's
#: :data:`_VERSION_LINE_RE` is already ``^\s*version``-anchored, so only this
#: line-interior search needs the guard.)
_INLINE_VERSION_RE = re.compile(
    r'(?P<pre>(?<![A-Za-z0-9_-])version\s*=\s*)(?P<q>["\'])(?P<val>[^"\']*)(?P=q)'
)


def _header_segments(inner: str) -> list[str] | None:
    """Split a table-header inner (``artifact-deps."ruamel.yaml"``) into its
    unquoted dotted-key segments, or ``None`` when it is not a clean dotted path
    (so a header the edit does not understand is skipped, never mis-matched)."""
    segments: list[str] = []
    pos = 0
    expect_key = True
    while pos < len(inner):
        if expect_key:
            m = _SEGMENT_RE.match(inner, pos)
            if not m:
                return None
            segments.append(next(g for g in m.groups() if g is not None))
            pos = m.end()
            expect_key = False
        else:
            rest = inner[pos:]
            if not rest.strip():
                break
            if not rest.lstrip().startswith("."):
                return None
            pos = inner.index(".", pos) + 1
            expect_key = True
    return segments if segments and not expect_key else None


def _inline_key(line: str) -> str | None:
    """The entry key of an inline-table line (``pkg = { … }``), or ``None``."""
    m = _INLINE_KEY_RE.match(line)
    if not m:
        return None
    return m.group("qk") or m.group("sk") or m.group("bk")


def _bump_one(lines: list[str], package: str, new_version: str) -> str | None:
    """Rewrite ``package``'s ``version`` to ``new_version`` in ``lines`` IN PLACE;
    return the old version, or ``None`` when the entry is not locatable.

    Handles both declaration shapes tomllib accepts: a header table
    (``[artifact-deps.<pkg>]`` with its own ``version = "…"`` line) and an inline
    table under ``[artifact-deps]`` (``<pkg> = { …, version = "…" }``). A match it
    cannot rewrite returns ``None`` so :func:`bump_artifact_deps` fails loudly
    rather than leaving the pin half-edited.
    """
    current: list[str] | None = None  # the current table's segment path
    for i, line in enumerate(lines):
        header = _HEADER_RE.match(line)
        if header:
            current = _header_segments(header.group("inner"))
            continue
        if current == ["artifact-deps", package]:
            # Header-table form: the entry's own `version = "…"` line.
            vm = _VERSION_LINE_RE.match(line)
            if vm:
                lines[i] = (
                    f"{vm.group('pre')}{vm.group('q')}{new_version}{vm.group('q')}{vm.group('post')}"
                )
                return vm.group("val")
        elif current == ["artifact-deps"] and _inline_key(line) == package:
            # Inline-table form: `pkg = { …, version = "…" }` on one line.
            vm = _INLINE_VERSION_RE.search(line)
            if vm:
                old = vm.group("val")
                lines[i] = (
                    line[: vm.start()]
                    + f"{vm.group('pre')}{vm.group('q')}{new_version}{vm.group('q')}"
                    + line[vm.end() :]
                )
                return old
    return None


def bump_artifact_deps(text: str, payload: CascadePayload) -> BumpResult:
    """Bump every ``[artifact-deps]`` entry whose ``repo`` matches
    ``payload.upstream`` to ``payload.version`` — the pure, network-free core.

    Parses ``text`` (loud on malformed TOML / malformed ``[artifact-deps]`` via
    :func:`shipit.config.load_artifact_deps`), selects the entries whose canonical
    ``repo`` equals the payload upstream, and SURGICALLY rewrites only their
    ``version`` value lines — every other byte (comments, non-matching entries,
    layout) is preserved. Entries already AT the target version are left untouched
    and reported as not-bumped, so a redundant re-dispatch is a no-op. An unknown
    upstream matches nothing and returns the text unchanged. A matching entry the
    edit cannot locate for a surgical rewrite raises :class:`CascadeError` (never
    a blind, structure-losing rewrite of ``.shipit.toml``).
    """
    deps = config.load_artifact_deps(_parse(text))
    matching = [d for d in deps if d.repo == payload.upstream]

    lines = text.split("\n")
    bumped: list[Bumped] = []
    for dep in matching:
        if dep.version == payload.version:
            continue
        old = _bump_one(lines, dep.package, payload.version)
        if old is None:
            raise CascadeError(
                f"cascade bump: could not locate the `version` line of "
                f"`[artifact-deps.{dep.package}]` in .shipit.toml for a surgical "
                f"edit — refusing to rewrite the file blind. Declare the entry as "
                f'a `[artifact-deps.<pkg>]` table with its own `version = "…"` '
                f'line (or an inline `{dep.package} = {{ …, version = "…" }}`).'
            )
        bumped.append(Bumped(dep.package, old, payload.version))

    return BumpResult(text="\n".join(lines), bumped=tuple(bumped))


def _parse(text: str) -> dict:
    """Parse ``.shipit.toml`` text into a dict (loud on malformed TOML), using
    the config module's own error type so wording matches the rest of shipit."""
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise config.ConfigError(f"malformed .shipit.toml: {exc}") from None


# --------------------------------------------------------------------------
# The managed receive-workflow unit (delivered only when [artifact-deps] exist).
# --------------------------------------------------------------------------

#: The receive workflow's body. Managed whole-file unit (ADR-0066): on an
#: `upstream-release` repository_dispatch it checks out, provisions the pinned
#: shipit launcher (the same setup-pixi bootstrap the wf-* blocks use), and runs
#: `shipit channel receive` with the client payload passed via ENV (never a
#: `${{ }}`-into-`run:` splice, which would be a shell-injection seam). The
#: token defaults to the ambient Actions token (`github.token`, the fleet
#: convention); a repo that needs the bump PR to TRIGGER its own CI supplies a
#: PAT as SHIPIT_CASCADE_TOKEN (a `github.token`-opened PR does not start
#: further workflow runs — a GitHub constraint). Because `upstream-release` is
#: the SAME event the pre-existing notify-downstreams rail fires (ADR-0067
#: reuses that rail) but with a different payload (`{repo, tag, version,
#: artifact}`, no `upstream`), the run step first GUARDS on a non-empty
#: `$UPSTREAM`: a foreign notify-downstreams payload landing here (a repo that is
#: both a notify-downstreams downstream and an `[artifact-deps]` consumer)
#: no-ops cleanly (exit 0) instead of failing the run or corrupting a bump.
_WORKFLOW_BODY = """\
# Managed by shipit; do not edit. Regenerate via `shipit install`.
#
# Artifact-channel cascade RECEIVE (ARF01-WS07): when an upstream this repo
# pins in `.shipit.toml [artifact-deps]` publishes a release, shipit's fan-out
# fires an `upstream-release` repository_dispatch here. This workflow bumps the
# matching pins, re-renders the managed pixi block, and opens a DRAFT bump PR
# that rides the normal review loop and re-resolves `pixi.lock`.
name: shipit-artifact-cascade
on:
  repository_dispatch:
    types: [upstream-release]
permissions:
  contents: write
  pull-requests: write
jobs:
  bump:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
        with:
          fetch-depth: 0
      - uses: prefix-dev/setup-pixi@v0.9.6
        with:
          pixi-version: v0.71.0
          locked: true
      - name: Bump artifact-deps and open the draft PR
        env:
          # The client payload rides ENV, never a `${{ }}` splice into `run:`
          # (shell-injection safe). Token: the ambient Actions token
          # (`github.token`, the fleet convention — see wf-prepare.yml's
          # `secrets.RELEASE_TOKEN || github.token`) by default; set
          # SHIPIT_CASCADE_TOKEN to a PAT if the bump PR must trigger CI.
          GH_TOKEN: ${{ secrets.SHIPIT_CASCADE_TOKEN || github.token }}
          UPSTREAM: ${{ github.event.client_payload.upstream }}
          VERSION: ${{ github.event.client_payload.version }}
        run: |
          # The `upstream-release` event is SHARED with the pre-existing
          # notify-downstreams rail (ADR-0067 reuses its dispatch rail), whose
          # payload is `{repo, tag, version, artifact}` — no `upstream` key. A
          # repo that is BOTH a notify-downstreams downstream AND declares
          # `[artifact-deps]` receives both payloads on this one event; a foreign
          # (notify-downstreams) payload leaves UPSTREAM empty. Skip it as a
          # clean no-op rather than calling `shipit channel receive ""` (which
          # rightly errors on an empty --upstream) — so the foreign dispatch is
          # inert, never a red workflow run or a corrupt bump.
          if [ -z "$UPSTREAM" ]; then
            echo "no client_payload.upstream — foreign upstream-release dispatch (e.g. the notify-downstreams rail); nothing to bump."
            exit 0
          fi
          git config user.name "shipit-cascade[bot]"
          git config user.email "shipit-cascade@users.noreply.github.com"
          pixi run --locked ./bin/shipit channel receive \\
            --upstream "$UPSTREAM" --version "$VERSION"
"""


def receive_workflow_unit() -> Unit:
    """The shipit-managed receive-workflow, as a whole-file managed
    :class:`~shipit.install.units.Unit`.

    Delivered install-reconciled (ADR-0066/0067) into a consumer's
    ``.github/workflows/`` — but ONLY when the repo declares ``[artifact-deps]``
    (the install verb appends this alongside WS02's projected pixi blocks), so a
    repo with no cross-repo pin never carries a dead cascade workflow. Reconciled
    like every other whole-file unit: a consumer edit surfaces as an override at
    the next ``shipit install``.
    """
    return Unit(
        key=WORKFLOW_KEY,
        dest=WORKFLOW_DEST,
        kind="file",
        content=_WORKFLOW_BODY.encode("utf-8"),
    )


# --------------------------------------------------------------------------
# The receive orchestration — the one effectful path.
# --------------------------------------------------------------------------


def _default_reinstall(root: Path) -> None:
    """Re-render the managed pixi block off the just-bumped ``.shipit.toml`` by
    running ``shipit install`` in working-tree mode (WS02's projection). The
    injectable seam :func:`receive` takes so tests exercise the flow without a
    real reconcile."""
    from ..verbs import install as install_verb

    rc = install_verb.run(str(root))
    if rc != 0:
        raise CascadeError(
            f"cascade bump: re-rendering the managed pixi block via `shipit "
            f"install` failed (exit {rc}); not opening a PR (the bump-owned "
            f"triple is rolled back by :func:`receive`, leaving the tree clean)"
        )


def _branch_name(payload: CascadePayload) -> str:
    """The bump branch for one (upstream, version) — filesystem/ref-safe (the
    slug's ``/`` and any non-``[A-Za-z0-9._-]`` char collapse to ``-``)."""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", f"{payload.upstream}-{payload.version}")
    return f"{BRANCH_PREFIX}/{slug}"


#: The files a bump + re-render OWN — the pin/projection/lock triple. The pins
#: live in ``.shipit.toml``; the managed pixi BLOCK projected FROM them (its
#: pinned version/URL) lives in ``pixi.toml``; the solve's resolution lives in
#: ``pixi.lock``. These are the ONLY files whose content depends on the bumped
#: version, so snapshotting this triple and restoring it fully undoes a bump.
#: Every other file ``shipit install`` (the ``reinstall`` seam) may touch — the
#: managed cascade workflow, policy seeds, retired deletes — is version-
#: INDEPENDENT: a bump never changes it, and any later install re-converges it
#: idempotently, so it is never part of a half-edited-bump hazard.
_BUMP_OWNED: tuple[str, ...] = (config.CONFIG_NAME, "pixi.toml", "pixi.lock")


def _snapshot(root: Path, names: tuple[str, ...]) -> dict[str, bytes | None]:
    """Capture each named file's bytes for a rollback — ``None`` when it is
    absent, so :func:`_restore` can delete a file the failed step created."""
    return {
        name: (root / name).read_bytes() if (root / name).is_file() else None
        for name in names
    }


def _restore(root: Path, snapshot: dict[str, bytes | None]) -> None:
    """Restore a :func:`_snapshot`: rewrite each captured file's bytes and delete
    any that did not exist when the snapshot was taken (the failed step created
    them), leaving the working tree byte-identical to the pre-bump state."""
    for name, data in snapshot.items():
        path = root / name
        if data is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(data)


def _pr_title(payload: CascadePayload) -> str:
    return f"chore(artifact-deps): bump {payload.upstream} to {payload.version}"


def _pr_body(payload: CascadePayload, bumped: tuple[Bumped, ...]) -> str:
    """The draft bump PR body: what was bumped, and how it rides the loop."""
    lines = [
        f"Artifact-channel cascade: `{payload.upstream}` released "
        f"`{payload.version}`, so shipit bumped the matching `[artifact-deps]` "
        f"pin(s) and re-rendered the managed pixi block.",
        "",
        "### Bumped",
    ]
    lines += [f"- `{b.package}`: `{b.old_version}` → `{b.new_version}`" for b in bumped]
    lines += [
        "",
        "The bump commit carries a re-resolved `pixi.lock` (the re-render's "
        "solve), so CI's `--locked` install is green; this draft PR then rides "
        "the normal review loop.",
        "",
        "for #956",
        "",
    ]
    return "\n".join(lines)


def receive(
    root: Path,
    upstream: object,
    version: object,
    *,
    reinstall=_default_reinstall,
) -> ReceiveResult:
    """Apply a Cascade dispatch: bump the matching pins and open the draft PR.

    The one effectful path (validated payload → bump ``.shipit.toml`` → re-render
    the pixi block → branch/commit/push → draft PR). An unknown upstream or an
    already-current version bumps nothing and returns a clean no-op (no write, no
    branch, no PR) — so a redundant or misdirected dispatch is inert, never a
    corrupt ``.shipit.toml`` or an empty PR. The bump is ATOMIC: the whole
    bump-owned triple (``.shipit.toml`` + the re-render's ``pixi.toml`` /
    ``pixi.lock``, :data:`_BUMP_OWNED`) is snapshotted BEFORE the write, and a
    re-render (``reinstall``) that fails restores every one of them before
    propagating — so a failed run leaves the tree byte-identical to how it was
    found, never pin-vs-projection desynced. Every world-touching step goes
    through the ``git`` / ``gh`` adapters and the injectable ``reinstall`` seam,
    so the whole flow is recorded in tests with no network.
    """
    payload = parse_payload(upstream, version)
    toml_path = root / config.CONFIG_NAME
    if not toml_path.is_file():
        # Loud at the boundary (ADR-0030): a de-shipit'd repo whose cascade
        # workflow still fires has no config to bump. Raise a CascadeError so the
        # `cli_errors` shell maps it to `error: …` + exit 1, never a raw
        # FileNotFoundError traceback in the workflow log.
        raise CascadeError(
            f"cascade receive: no {config.CONFIG_NAME} at {root} — the repo has "
            f"no shipit config to bump (deleted, or the repo was de-shipit'd "
            f"while the cascade workflow lingered)"
        )
    text = toml_path.read_text(encoding="utf-8")

    result = bump_artifact_deps(text, payload)
    if not result.bumped:
        # Unknown upstream or already current: nothing to write, nothing to open.
        return ReceiveResult(bumped=(), branch=None, url=None)

    # Snapshot the whole bump-owned triple BEFORE the write + re-render: the
    # re-render (`reinstall` — real `shipit install` in MODE_TREE) rewrites
    # `pixi.toml`/`pixi.lock` and can raise AFTER some of those writes land, so a
    # failure must restore every bump-owned file, not just `.shipit.toml` — else
    # the tree is left pin-vs-projection desynced (the exact half-edited hazard
    # this guards). Everything else install may touch is version-independent
    # (see `_BUMP_OWNED`), so this triple is the complete rollback set.
    snapshot = _snapshot(root, _BUMP_OWNED)
    try:
        # The write is INSIDE the try so a failure mid-write (full disk,
        # permission error, interrupted syscall) that could leave `.shipit.toml`
        # empty or half-written also triggers the rollback — `_restore` rewrites
        # every bump-owned file from the snapshot taken above.
        toml_path.write_text(result.text, encoding="utf-8")
        reinstall(root)
    except Exception:
        _restore(root, snapshot)
        raise

    cwd = str(root)
    branch = _branch_name(payload)
    original = git.current_branch(cwd=cwd)

    paths = [config.CONFIG_NAME]
    if (root / "pixi.toml").is_file():
        paths.append("pixi.toml")
    # The re-render's lint-env solve re-resolves the WHOLE `pixi.lock` (one file,
    # every environment) against the new pin; stage it so the bump PR carries a
    # lock matching the bumped block and CI's `--locked` install stays green —
    # the same laptop/CI-parity move `shipit install` makes (apply.py, #439).
    if (root / "pixi.lock").is_file():
        paths.append("pixi.lock")

    git.switch_create(branch, cwd=cwd)
    try:
        git.add(paths, cwd=cwd)
        git.commit(_pr_title(payload), paths, cwd=cwd, no_verify=True)
        # Force: the bump branch is deterministic (one per upstream/version) and
        # `switch_create` (`git switch -C`) re-creates it from HEAD each run, so
        # a re-dispatch would diverge from an existing remote branch and a plain
        # push would fail non-fast-forward — the same shipit-owned-branch push
        # `shipit install`'s MODE_PR does (apply.py). Reusing the open PR then
        # works: the force-push refreshes its head before `pr_url_for_head`.
        git.push(branch, cwd=cwd, force=True, no_verify=True)
        url = gh.pr_url_for_head(branch, cwd=cwd) or gh.pr_create(
            head=branch,
            title=_pr_title(payload),
            body=_pr_body(payload, result.bumped),
            draft=True,
            cwd=cwd,
        )
    finally:
        if original:
            git.switch(original, cwd=cwd)

    return ReceiveResult(bumped=result.bumped, branch=branch, url=url)
