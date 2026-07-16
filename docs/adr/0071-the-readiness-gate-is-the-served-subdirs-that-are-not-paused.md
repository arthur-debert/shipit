# The channel readiness gate is the served subdirs that are not owner-paused

The spec's bootstrap gate (`docs/spec/artifact-channel.md` §Risks And Rabbit
Holes) blocks ADR-0066's `provision lexd` retirement until the channel serves
`lexd` for **every** subdir of the closed served set — `osx-arm64`, `linux-64`,
`linux-aarch64`, `win-64` (`SERVED_SUBDIRS`, `src/shipit/channel/buckets.py`) —
and hands over a copy-pasteable probe loop that exits non-zero on any 404.

That gate cannot be met. Windows build legs are owner-paused (shipit#895), and
`lex-fmt/lex` accordingly declares `lexd` and `lexd-lsp` on the three
non-Windows platforms only. So a `win-64` probe will 404 for as long as the
pause holds, and the retirement — a Linux/macOS lint-gate concern — waits
indefinitely on a Windows workstream that was deliberately stopped. Meanwhile
`shipit provision lexd`, the bespoke trust-on-first-use fetcher ADR-0066 exists
to delete, lives on. The gate and the pause contradict each other; one must give.

## Decision

- **The readiness gate is the served subdirs minus the owner-paused ones** —
  today `osx-arm64`, `linux-64`, `linux-aarch64`. The channel must serve `lexd`
  authless on each of those before the cutover; `win-64` is not probed while
  #895 holds.
- **`win-64` stays in the closed served set.** ADR-0064 is unchanged: the subdir
  is still served, it is merely **not currently produced**. It re-enters the gate
  automatically when the pause lifts — no ADR revision, no re-widening decision.
- **An owner-level pause is the only sanctioned subtraction.** A subdir leaves
  the gate only because a pause stops producing it — never because a build is
  failing, slow, or inconvenient. A missing non-paused subdir is still a red
  gate, and "fails to build" must never be laundered into "paused".
- **Windows consumers fail closed, loudly.** After retirement, a `win-64`
  `pixi install` / lint solve finds no `lexd` in the channel and fails to
  resolve. That is the honest consequence of the pause, and it is not softened:
  ADR-0066's no-fallback cutover stands, and no `provision` fallback is retained
  for Windows.

### Alternatives rejected

- **Keep the gate as written** — ADR-0066 stays undelivered for a reason
  unrelated to the channel, gated on a workstream with no scheduled end. The
  bespoke fetcher and its compiled-in SHAs survive by accident rather than by
  decision.
- **Unpause Windows to satisfy the gate** — reopens a workstream the owner
  stopped, and makes producing a Windows binary a prerequisite for a lint gate
  that runs on Linux and macOS.
- **Drop `win-64` from the closed served set** — overstates a temporary
  condition: the pause is meant to lift, so this buys an ADR-0064 amendment now
  and its exact reversal later.
- **Retain a `provision` fallback for `win-64` only** — the two-mechanisms-for-one-job
  outcome ADR-0066 explicitly rejected, reintroduced under a narrower name.

## Consequences

- ADR-0066's cutover can proceed under the pause. The spec's probe loop drops to
  the three non-paused subdirs and carries a note tying `win-64`'s return to
  #895.
- **Windows becomes load-bearing on the lint gate the moment #895 lifts:**
  `win-64` must be produced and served *before* any `win-64` repo can lint,
  because the cutover retains no fallback. Unpausing Windows is therefore not
  purely additive — it is sequenced work, and this ADR is the reason why.
- The gate's definition now depends on a pause list — owner state that lives in
  the tracker (#895), not in code. That is a deliberate seam: pauses are
  decisions, not configuration. It does mean the gate cannot be evaluated from
  the repo alone; whoever runs it must know what is paused.
