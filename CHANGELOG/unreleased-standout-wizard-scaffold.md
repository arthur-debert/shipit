- repo: `shipit repo new --stack rust --standout-wizard NAME [PARENT]` scaffolds
  the Rust application with the released `standout new-project` wizard instead of
  the minimal hello-world workspace (#1202). The wizard is an alternate scaffold
  *producer* inside the closed `rust` Creation profile — no new stack, no plugin
  or template mechanism, and the default `--stack rust` path is untouched. Shipit
  copies no Standout templates: it resolves `standout` on your PATH (and refuses
  with installation guidance when it is absent — it never installs it), runs the
  wizard interactively in a scratch directory before staging exists, and imports
  the generated tree verbatim, `<name>lib` naming, resolver 2, edition 2021, crate
  README and all. The Artifact is derived from the generated executable package,
  which the wizard's separate executable-name answer may spell differently from
  the Repo name. Everything else is unchanged — shipit still owns the repository
  files, the managed install, provisioning, the three staged Checks, the single
  initial commit, and the atomic publication. Cancelling the wizard (it exits 0
  and writes nothing), answering a project name other than `NAME`, generating an
  entry shipit will not import, or failing a Check all leave the destination
  absent and remove every scratch directory. Verified end to end against released
  standout 7.10.1: the generated workspace passes all three staged Checks and the
  Repo publishes, with the built binary running from the new Repo. Shipit imports
  the generated manifests as they are and never rewrites them, so the wizard's own
  dependency pins are what the new Repo builds against.
