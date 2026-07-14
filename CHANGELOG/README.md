# CHANGELOG fragments

Release notes accumulate here as `unreleased-<slug>.md` fragments, one per
feature/fix PR — plain markdown, no per-language logic (the changelog model,
`docs/dev/workflows.lex` §4). The committed `CHANGELOG.md` is a rendered
PROJECTION of this directory — never hand-edited; `shipit changelog render`
regenerates it and `shipit changelog check` fails a drifted render.

At cut time `shipit release prepare` coalesces the fragments into the new
version's `<semver>.md` section here and emits ONE notes text for both the
tag annotation and the GitHub release; a final cut consumes the fragments, a
prerelease leaves them for the final it leads to. Zero fragments is a refused
(empty) release — every release-worthy PR should land one.

Reserved stems: this `README`, and `legacy.md` for pre-model history.
