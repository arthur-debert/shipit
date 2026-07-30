# A Shipit pin is a release Version or development Revision

A consumer Repo records exactly one Shipit pin in `.shipit.toml`: normal
installs use a semantic release `version`, while exceptional unreleased testing
uses a mutually exclusive full git `revision`. A Version is easier to reason
about and communicates expected disruption; duplicating its resolved Revision
would add mismatch states without changing what Git can check out. Fleet update
therefore targets releases only and writes a Version, while exact Revision pins
remain a manual per-Repo testing escape hatch.

This supersedes ADR-0033’s Sha-only pin representation now that Shipit has
release machinery. The invariant survives: the selected build writes its own pin
together with the managed files it produced.
