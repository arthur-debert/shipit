- channel: `store_provision.verify()` now probes the served conda subdirs the
  repo ACTUALLY publishes — (its declared conda-endpoint platforms ∩ the served
  set) — instead of the fixed all-of-served set (#1076). It required a
  `repodata.json` under **every** `SERVED_SUBDIRS` entry incl. `win-64`, but a
  producer only publishes the subdirs it builds (`conda_assets`/`conda_subdir`
  drop unserved/undeclared platforms), so a correctly-provisioned channel for a
  repo that ships fewer platforms — e.g. lexd (linux x86_64/aarch64 + darwin-arm64,
  no windows) — reported **NOT ready** (false negative on the absent win-64). A
  new pure `release.publish.conda_served_subdirs(artifacts)` projects the repo's
  conda-endpoint artifacts' platforms onto the served subdirs (the same
  platform→triple→subdir derivation the publish stage uses); `verify` takes a
  `subdirs` probe set (an explicitly empty one is refused, never a vacuous
  all-pass), and the `verify` CLI derives it from the TARGET repo's `.shipit.toml`
  only when the operator OPTS IN with an explicit `--manifest` — `--repo` is an
  arbitrary `<owner>/<repo>`, so silently scoping from an ambient `.shipit.toml`
  could probe a narrower set than the target publishes and pass a channel missing
  a subdir (a false-ready). Absent `--manifest` (and for a conda-less manifest),
  the probe stays the conservative full served set. The same wrong-axis family as
  #1072, on the channel-readiness surface.
