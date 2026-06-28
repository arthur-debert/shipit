# Reusable workflows publish from shipit; producing logic lives in the package

The CI/CD logic the fleet runs today lives in `arthur-debert/release` — ~39
`bin-internal/*.sh` scripts, composite signing actions, and N per-toolchain reusable
workflows that consumers reference as `arthur-debert/release/...@v3`. Workflows
re-homes that logic into shipit's model (architecture.lex §3–§5: producing logic is
a pixi task, routing logic is thin YAML, durable logic is one slim package). Two
decisions follow: where the re-homed *logic* lives, and where the *reusable
workflows* publish from.

## Decision

- **Producing logic is owned by the shipit package**, surfaced as pixi tasks /
  `shipit` subcommands that run identically on a laptop, in local Docker, and in CI
  — the kill switch on the "push an RC to find out if it works" loop. Bash is kept
  where bash is genuinely right (`hdiutil`, `codesign`, `tauri bundle`), but as a
  *named, locally-runnable task owned by the package*, never copied into each
  consumer. **Copy-not-depend (ADR-0001) extends to `bin-internal`:** shipit forks
  that logic into its own source, it does not depend on the release repo or wheel.
- **Reusable workflows publish from `arthur-debert/shipit@vN`.** Consumers carry
  only thin callers (`setup-pixi` + `pixi run <task>` for the generic CI **lane**
  fan-out, plus the composable opt-in build→package→sign→publish jobs for the hard
  20%) and upgrade by bumping one version — no thick vendored workflow copies to
  drift.
- **Routing stays thin YAML:** the lane/platform matrix, cross-job artifact
  upload/download, secret injection, the macOS keychain import. None of it is
  pixi-shaped.

### Alternatives rejected

- **Depend on the release repo's workflows/scripts** — violates copy-not-depend and
  leaves the fleet straddling two sources of truth through the cutover.
- **Template the logic into each consumer's `pixi.toml`** — pixi has no cross-
  manifest task inheritance, so this makes the most important config file a
  managed-but-edited drift surface (architecture.lex §5); the binary path avoids it.
- **Keep per-toolchain reusable workflows, just re-homed** — preserves the
  proliferation; the generic-CI + composable-jobs shape (ADR-0007) collapses it.

## Consequences

- The cutover is mechanical and laddered: each `bin-internal` script becomes a pixi
  task / `shipit` subcommand; each consumer's thin caller is re-pointed from
  `release@v3` to `shipit@vN`, one toolchain at a time, keeping the required-check
  name stable so branch protection does not break mid-migration.
- release-core is retired only after shipit cuts **one real release of one real
  consumer** with the artifact inspected (right binary, signed, notarized) — the
  standing rule in `FUTURE_WORK.md`.
