- The standing sign e2e (#899): `shipit wf verify-canary` dispatches
  shipit-canary's blessed release caller through the full sign proof matrix
  on live GitHub — the composed `stage=full` chain (sign+notarize on a real
  macOS runner, the #873/#889 class) and the staged
  `prepare`→`build`→`sign`→`publish` relay (the real cross-run artifact
  hand-off, the #898 class) — watches every run to its verdict, prints the
  proof-citation and teardown blocks, and exits green only when every run
  is. The workflows.lex §9 runbook makes citing both green chains mandatory
  for any PR touching the sign/relay/wf-yml surface, and names the exact
  canary-side surface (signed darwin-arm64 artifact, blessed caller, the
  owner-pushed Apple secret set) the proof rides on.
