# The tag is the version authority; the version is supplied, not computed

A Release carries one repo-level version whose authority is the git tag;
manifests are projections of the tag decision, bumped by per-toolchain registry
adapters (rust workspace Cargo.toml, npm package.json, python pyproject, go —
zero files, version injected at build via `-ldflags`; bundle-level files like
`tauri.conf.json` are bumped by an artifact-declared bundle-config hook, keeping
"tauri" out of the dispatch registry per ADR-0007). The version is supplied by
the caller as `<semver>` or a bump word (`major` / `minor` / `patch`, resolved
against the latest tag) — never inferred from fragments or commit messages,
because bump-level inference has real ambiguity (who decides a breaking change?)
and nothing in the fleet asks for it. Prerelease detection stays semver-suffix
(`-release-rc`, `-rc.N`).

## Consequences

- Tag-authoritative makes prepare idempotent-resumable (ADR-0009): tag exists →
  skip the bump, re-emit the tag SHA.
- go's "no manifest bump" is a first-class adapter, not an exception.
- The prepare bump commit passes the same commit/push checks as any commit —
  `RELEASE_TOKEN` exists to satisfy the ruleset, never to skip checks.
