- `shipit repo new --stack rust <name> [parent]` creates a new local Repo
  with a complete, verified, shipit-managed baseline (GEN01, #944): it
  scaffolds a two-crate Cargo workspace (a `<name>` CLI over a `lib<name>`
  library), applies the managed install baseline, resolves the pixi lockfile,
  and certifies the Repo by running its lint, test, and build Checks — staging
  the whole tree in a sibling and publishing it with one atomic rename only
  after every Check passes, so a single initial commit lands on `main` and any
  failure leaves the destination untouched. `--stack` is repeatable for future
  multi-toolchain Repos but v1 supports one profile, `rust`. Creation is local
  only — it creates no GitHub repository, remote, or release policy, keeping it
  distinct from `shipit install`, which adopts and reconciles an existing
  repository. See `docs/spec/repo-new.md` for the exhaustive contract.
