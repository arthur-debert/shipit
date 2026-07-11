"""The brew formula render core — release assets → a tap formula. Pure.

The brew Distribution endpoint (TOL02-WS05, PRD story 35) is DERIVED: it
consumes the FINAL release-asset URLs and sha256s, so it runs only after the
``release``-stage endpoints uploaded the assets. This module is its pure
half — everything that turns inputs into the formula text:

- :func:`formula_class` — the PascalCase Ruby class derivation from the
  installed binary name (the legacy ``render-brew-formula`` composite's
  contract: ``lex-cli`` → ``LexCli``).
- :func:`render` — the shared formula template (ONE template for every
  consumer — the legacy ``.rb.tmpl``, inlined here as the registry's single
  assembly point): desc/homepage/version/license, per-platform
  ``on_macos``/``on_linux`` blocks with ``on_arm``/``on_intel`` splits, the
  ``bin.install`` of the declared binary — plus, for a PRIVATE source repo,
  the inlined ``GitHubPrivateRepositoryReleaseDownloadStrategy`` preamble
  and a ``using:`` clause on every url (the tap ships no library code, so
  the strategy travels inside the formula).
- :func:`metadata_for` — desc/homepage/license pulled from ``cargo
  metadata`` output for the artifact's crate, HARD-ERRORING when missing
  (the legacy ``homebrew-formula`` job's contract — a formula without a
  description is a tap defect, never a silent blank).

The effectful half — sha256 over the staged tarballs, the ``ruby -c``
syntax check, the tap clone/commit/push — is the brew endpoint adapter in
:mod:`shipit.release.publish`; nothing here touches disk, network, or a
subprocess.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from .. import config
from . import ReleaseError

#: The private-repo download strategy, inlined into the formula when the
#: source repo is private (the legacy ``render-brew-formula`` composite kept
#: this preamble in a sibling file for a YAML heredoc scar; a Python string
#: constant has no such scar). Resolves the asset id via the GitHub API and
#: fetches it token-authenticated; requires ``HOMEBREW_GITHUB_API_TOKEN``.
PRIVATE_STRATEGY_PREAMBLE = """require "download_strategy"
require "utils/github"

# Download strategy for release assets of a PRIVATE GitHub repository:
# resolves the asset via the GitHub API and fetches it token-authenticated.
# Requires HOMEBREW_GITHUB_API_TOKEN. Inlined because the tap carries no
# library code — the strategy travels inside the formula.
class GitHubPrivateRepositoryReleaseDownloadStrategy < CurlDownloadStrategy
  def initialize(url, name, version, **meta)
    super
    match = url.match(%r{https://github\\.com/([^/]+)/([^/]+)/releases/download/([^/]+)/(.+)})
    raise CurlDownloadStrategyError, "Invalid url pattern for GitHub Release." if match.nil?

    _, @owner, @repo, @tag, @filename = *match
    @github_token = ENV.fetch("HOMEBREW_GITHUB_API_TOKEN") do
      raise CurlDownloadStrategyError,
            "Set HOMEBREW_GITHUB_API_TOKEN to install from a private repository."
    end
  end

  private

  def _fetch(url:, resolved_url:, timeout:)
    curl_download(asset_url, "--header", "Accept: application/octet-stream",
                  "--header", "Authorization: token #{@github_token}",
                  to: temporary_path, timeout: timeout)
  end

  def asset_url
    release = GitHub::API.open_rest(
      "#{GitHub::API_URL}/repos/#{@owner}/#{@repo}/releases/tags/#{@tag}",
    )
    asset = release["assets"].find { |a| a["name"] == @filename }
    raise CurlDownloadStrategyError, "Asset #{@filename} not found in release #{@tag}." if asset.nil?

    asset["url"]
  end
end

"""


def formula_class(name: str) -> str:
    """The Ruby formula class for a binary ``name``. Pure.

    The legacy PascalCase derivation: split on ``-``/``_`` runs, capitalize
    each part (``lex-cli`` → ``LexCli``, ``check_e2e`` → ``CheckE2e``). An
    empty derivation (a name with no word characters) is a
    :class:`ReleaseError` — a formula must have a class.
    """
    parts = [part for part in re.split(r"[-_]+", name) if part]
    if not parts:
        raise ReleaseError(f"cannot derive a formula class from binary name {name!r}")
    return "".join(part[:1].upper() + part[1:] for part in parts)


def metadata_for(metadata: dict, artifact: config.Artifact) -> tuple[str, str, str]:
    """(desc, homepage, license) for ``artifact``'s crate, from parsed
    ``cargo metadata`` output. Pure.

    The crate is the artifact's declared rust ``package``, else the package
    named like the artifact, else the workspace's ONLY package. Missing
    description/license, or neither ``homepage`` nor ``repository``, is a
    HARD :class:`ReleaseError` (the legacy ``homebrew-formula`` job's
    contract) — a formula never renders with silent blanks.
    """
    packages = [p for p in metadata.get("packages", []) if isinstance(p, dict)]
    wanted = next(
        (t.package for t in artifact.build if t.toolchain == "rust" and t.package),
        None,
    )
    if wanted is not None:
        pkg = next((p for p in packages if p.get("name") == wanted), None)
    else:
        pkg = next((p for p in packages if p.get("name") == artifact.name), None)
        if pkg is None and len(packages) == 1:
            pkg = packages[0]
    if pkg is None:
        raise ReleaseError(
            f"[artifacts.{artifact.name}] brew: cannot locate the crate in "
            f"`cargo metadata` output — declare the rust build target's "
            f"`package`, or name the artifact after its crate"
        )
    desc = pkg.get("description")
    license_ = pkg.get("license")
    homepage = pkg.get("homepage") or pkg.get("repository")
    missing = [
        label
        for label, value in (
            ("description", desc),
            ("license", license_),
            ("homepage/repository", homepage),
        )
        if not value
    ]
    if missing:
        raise ReleaseError(
            f"[artifacts.{artifact.name}] brew: crate `{pkg.get('name')}` "
            f"metadata is missing {', '.join(missing)} — the formula requires "
            f"desc, license, and homepage (hard error, never a blank formula)"
        )
    return str(desc), str(homepage), str(license_)


def _ruby_str(value: str) -> str:
    """``value`` escaped for a Ruby DOUBLE-QUOTED string literal. Pure.

    Escapes the backslash, the double quote, and ``#`` (defusing ``#{…}``
    interpolation) so a crate description/homepage/license/binary carrying any
    of them still renders a formula that ``ruby -c`` accepts — never a syntax
    error or an interpolation that reads a Ruby variable at install time.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("#", "\\#")


def _is_arm(triple: str) -> bool:
    """Whether ``triple`` is an arm target (``on_arm`` branch). Pure."""
    return triple.startswith(("aarch64", "arm"))


def _url_sha_lines(url: str, sha: str, *, indent: str, private: bool) -> list[str]:
    using = ", using: GitHubPrivateRepositoryReleaseDownloadStrategy" if private else ""
    return [f'{indent}url "{url}"{using}', f'{indent}sha256 "{sha}"']


def _os_block(
    os_word: str, pairs: dict[str, tuple[str, str]], *, private: bool
) -> list[str]:
    """One ``on_macos``/``on_linux`` block over its ``{triple: (url, sha)}``
    pairs — split ``on_arm``/``on_intel`` when both are present, bare
    url/sha when the OS ships a single target. Pure."""
    arm = [pairs[t] for t in sorted(pairs) if _is_arm(t)]
    intel = [pairs[t] for t in sorted(pairs) if not _is_arm(t)]
    lines = [f"  {os_word} do"]
    if arm and intel:
        for word, (url, sha) in (("on_arm", arm[0]), ("on_intel", intel[0])):
            lines.append(f"    {word} do")
            lines += _url_sha_lines(url, sha, indent="      ", private=private)
            lines.append("    end")
    else:
        url, sha = (arm or intel)[0]
        lines += _url_sha_lines(url, sha, indent="    ", private=private)
    lines.append("  end")
    return lines


def render(
    *,
    binary: str,
    version: str,
    desc: str,
    homepage: str,
    license_: str,
    targets: Mapping[str, tuple[str, str]],
    private: bool,
) -> str:
    """The formula text — the ONE shared template, rendered. Pure.

    ``targets`` maps each target triple to its FINAL release-asset
    ``(url, sha256)`` (the derived-stage contract: gh-release uploaded the
    assets first, so these are what ``brew install`` will actually fetch).
    ``private`` inlines the private-repo download strategy and rides a
    ``using:`` clause on every url. A target set with neither a mac nor a
    linux triple is a :class:`ReleaseError` — brew has nothing to install.
    """
    mac = {t: v for t, v in targets.items() if "apple-darwin" in t}
    linux = {t: v for t, v in targets.items() if "linux" in t}
    if not mac and not linux:
        raise ReleaseError(
            f"brew: no mac or linux release archive among targets "
            f"({', '.join(sorted(targets)) or 'none'}) — the formula has "
            f"nothing to install"
        )
    lines: list[str] = []
    if private:
        lines.append(PRIVATE_STRATEGY_PREAMBLE.rstrip("\n"))
        lines.append("")
    lines.append(f"class {formula_class(binary)} < Formula")
    lines.append(f'  desc "{_ruby_str(desc)}"')
    lines.append(f'  homepage "{_ruby_str(homepage)}"')
    lines.append(f'  version "{_ruby_str(version)}"')
    lines.append(f'  license "{_ruby_str(license_)}"')
    for os_word, pairs in (("on_macos", mac), ("on_linux", linux)):
        if pairs:
            lines.append("")
            lines += _os_block(os_word, pairs, private=private)
    lines += [
        "",
        "  def install",
        f'    bin.install "{_ruby_str(binary)}"',
        "  end",
        "end",
        "",
    ]
    return "\n".join(lines)
