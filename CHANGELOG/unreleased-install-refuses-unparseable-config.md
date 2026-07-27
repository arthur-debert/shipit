- install: an unparseable `.shipit.toml` now **refuses the run** instead of
  degrading to an empty unit set (#1101). `shipit install` caught the
  `ConfigError` from the `[artifacts]` parse and reconciled the repo as if it
  declared no signals and no endpoints — dropping exactly the managed blocks its
  artifacts gate — while exiting 0. Unit absence never *deletes* a block
  (removal rides the explicit retired list), so those blocks stayed on disk,
  fell out of the managed set, and went stale silently. A config the tool cannot
  parse is not a config declaring nothing: the parse error, whose message is
  already migration-pointing, now reaches the user as `error: …` + exit 1.
  Concretely, a `tarball` artifact predating the producer-declared payload
  (#1092) would have un-managed its repo's `tree-sitter-cli` toolchain block and
  its `rattler-build` conda-packager block on the next reconcile.
- The same posture now covers all three config reads in the install verb
  (declared signals, declared endpoints, `[artifact-deps]` projection), so
  install has ONE answer for a config it cannot parse. An **absent** config is
  unchanged — a missing map declares nothing and installs cleanly.
