### Fixed

- Standalone `wf-build` dispatches are now a relay-complete source run for the
  sign/publish stages: a new standalone-only `notes` job re-derives the
  `release-notes` artifact at the tag via the new read-only
  `shipit release notes` verb, so a staged chain whose sign/publish names a
  build run as its source no longer fails `carry-notes` with
  `Artifact not found for name: release-notes` (#898).
