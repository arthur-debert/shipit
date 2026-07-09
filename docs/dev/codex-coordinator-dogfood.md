# Codex coordinator dogfood — the live verification for `shipit session codex`

CDX01 makes Codex a first-class **coordinator** surface: `shipit session codex`
(usually via the managed `./codex-start`) launches an interactive Codex session
in its own ephemeral session Tree, the exact isolated shape `./claude-start`
gets from the `--worktree` hook seam ([ADR-0027](../adr/0027-coordinator-session-tree-ephemeral.md),
`docs/prd/session-bootstrap.md`). Codex has no pre-launch hook seam, so the
launcher inverts the order: mint a `codex-<utc>-<pid>` session id, provision the
central-root ephemeral Tree for it (branch `ephemeral/<id>`, base
`origin/main` — the same Tree machinery every shape uses), then exec
`codex --cd <tree>` in the low-friction coordinator posture with the session
identity riding the child env ([ADR-0020 §codex](../adr/0020-backend-adapter-contract.md)).

Every CDX01 work stream is unit-tested with the Tree orchestrator and the exec
faked (`tests/test_session_bootstrap.py`, `tests/test_session_verb.py`,
`tests/test_install.py`). Those tests prove the launch *contract*; they cannot
prove the load-bearing facts that exist only against the live `codex` binary
and a real clone: that the argv posture parses, that codex actually roots in
the Tree, that the ChatGPT sign-in carries the session with **no API-key
billing**, and that the lifecycle is observable through shipit's logs and Tree
tooling. This runbook is that live counterpart — the coordinator-session
sibling of the spawn-surface gate in
[spawn-dogfood-verification.md](./spawn-dogfood-verification.md), which covers
codex as a *spawned Run* backend (write/reviewer Runs). It is opt-in and
side-effecting (a real Tree on disk, real subscription tokens), never part of
`pixi run test` / CI.

## What a live run asserts

- **Recognizable session id.** The launch mints `codex-<utc-stamp>-<pid>` and
  prints it with the Tree path and the exact exec argv — the only scrollback
  trace before codex takes the terminal over.
- **A real ephemeral session Tree.** A dissociated clone at
  `<trees-root>/<org>/<repo>/ephemeral/<id>` on branch `ephemeral/<id>` (base
  `origin/main`), provisioned like any Tree (`pixi install`, hook install) —
  never a native worktree, never inside the source checkout.
- **codex rooted in the Tree.** codex's own banner reports
  `workdir: <tree>`; `pwd` and `git rev-parse --abbrev-ref HEAD` inside the
  session answer with the Tree path and the `ephemeral/<id>` branch.
- **The coordinator posture.** `--dangerously-bypass-approvals-and-sandbox`
  (banner: `approval: never`, `sandbox: danger-full-access`) — codex's own
  sandboxes deny `.git` writes and/or the network, so a committing/pushing
  coordinator cannot live under them; the disposable Tree IS the external
  isolation that flag documents (ADR-0020 §codex, probed on 0.139).
- **Subscription auth, no API-key billing.** The session runs on the stored
  ChatGPT sign-in (`codex login status` → `Logged in using ChatGPT`); the
  launcher scrubs `OPENAI_API_KEY` / `CODEX_API_KEY` from the child env so a
  stale key can never flip the session onto API billing, and
  `CODEX_ACCESS_TOKEN` (the subscription-token automation conduit) passes
  through.
- **Session identity in the env.** `SHIPIT_LOG_CTX_SESSION` / `_TREE` are
  exported to the child, so every in-session shipit command logs keyed to the
  session — the codex counterpart of the SessionStart hook's `CLAUDE_ENV_FILE`
  write.
- **An observable lifecycle.** `shipit logs --flow --session <id>` renders the
  session's story (the `tree.created` event); the unfiltered
  `shipit logs --session <id>` view shows every provisioning step plus the
  launch milestone with the full argv; `shipit tree list` shows the Tree with
  its at-a-glance state.

## How to run it live (maintainer)

Prerequisites: the `codex` CLI on PATH (0.139+), a ChatGPT sign-in
(`codex login status`), and a shipit checkout to launch from. No
`OPENAI_API_KEY` / `CODEX_API_KEY` is needed — the launcher scrubs them anyway.

### Interactive (the normal coordinator path)

```sh
./codex-start            # or: shipit session codex
```

The launch prints the session id, the Tree path, and the exec argv, then codex
takes the terminal. Extra args forward to codex verbatim
(`./codex-start --model <id>`). Inside the session, verify rooting and
identity:

```sh
pwd                                  # the Tree path
git rev-parse --abbrev-ref HEAD     # ephemeral/<session-id>
echo "$SHIPIT_LOG_CTX_SESSION"      # the minted session id
```

### Headless smoke (CI-less, scriptable)

The same launcher path drives a non-interactive smoke by forwarding codex's
`exec` subcommand — global flags before the subcommand parse fine on 0.139,
so the managed argv (`codex --cd <tree> <posture> exec …`) holds:

```sh
pixi run shipit session codex exec --skip-git-repo-check \
  'Run `pwd`, then `git rev-parse --abbrev-ref HEAD`, then
   `echo "$SHIPIT_LOG_CTX_SESSION"`. Report the three outputs verbatim.
   Do not modify any files.' < /dev/null
```

**stdin MUST be redirected from `/dev/null`**: `codex exec` reads any open
stdin to EOF as extra input (ADR-0020's universal spike finding — with an
inherited non-TTY stdin it blocks indefinitely).

### Inspect afterwards

```sh
shipit logs --flow --session <session-id>   # the session story (tree.created)
shipit logs --session <session-id> -n 50    # every record: provisioning + launch argv
shipit tree list                            # the Tree, its branch, clean/dirty
```

Codex's own transcript lives under `$CODEX_HOME` (default `~/.codex`) keyed by
codex's internal session id (printed in the exec banner); `codex resume` picks
it up — that artifact is codex's, not shipit's.

### Teardown

An abandoned ephemeral Tree is reclaimed by the gc ladder eventually
(ADR-0027: liveness-checked, dirty/unpushed work protected absolutely, hard
time cap as backstop). To reclaim a smoke Tree immediately:

```sh
shipit tree remove <tree-path>
```

## Recorded PASS — 2026-07-09 (CDX01-WS04, issue #606)

Headless smoke through the real launcher path, `codex-cli 0.139.0`, ChatGPT
sign-in, from the WS04 Tree checkout:

- Launch printed `codex session codex-20260709-141312-90203`, the Tree path
  `…/trees/arthur-debert/shipit/ephemeral/codex-20260709-141312-90203`, and
  `exec codex --cd <tree> --dangerously-bypass-approvals-and-sandbox exec
  --skip-git-repo-check '<probe>'`.
- Tree created in ~14.5 s (clone, `ephemeral/<id>` off `origin/main`,
  `pixi install`, lefthook install) — all recorded in the log keyed to the
  session.
- codex banner: `workdir` = the Tree, `approval: never`,
  `sandbox: danger-full-access`, model resolved from codex's own config; probe
  reported `pwd` = the Tree, branch = `ephemeral/codex-20260709-141312-90203`,
  `SHIPIT_LOG_CTX_SESSION` = the minted id. Exit 0, ~27.6k tokens on the
  subscription; no `OPENAI_API_KEY` / `CODEX_API_KEY` present.
- `shipit logs --flow --session codex-20260709-141312-90203` rendered the
  `tree.created` event; `shipit tree list` showed the Tree (ephemeral, clean);
  `shipit tree remove` reclaimed it.

## Residual limitations (known, deliberate, or deferred)

- **The managed `.codex` layer rides the Tree's base, not the launcher.** The
  session Tree's *contents* are `origin/main`, so the repo-local
  `.codex/config.toml` + `.codex/hooks.json` (CDX01-WS01) are only inside the
  session Tree once the epic lands on `main` (and reach consumers as the pin
  catches up, ADR-0033). Until then a codex session is isolated and
  identity-carrying but runs without the shipit guard/sessionstart hooks
  inside the Tree. Self-resolving; noted so a pre-merge dogfood isn't misread
  as a hooks failure.
- **Codex project trust is per-directory.** codex 0.139 loads a repo-local
  `.codex/` layer only after the operator trusts that checkout, and every
  launch mints a *new* Tree path — so expect a trust prompt per fresh
  interactive session before the managed hooks/config activate; headless
  `codex exec` simply runs without the untrusted project layer. Consequence:
  the SessionStart-written liveness pidfile (`.git/shipit-session.json`) only
  appears once the layer runs, so an untrusted or headless session Tree reads
  as not-live to the gc ladder — safe (the dirty/unpushed floor and the
  conservative ladder still protect work), but a live idle session in an
  untrusted Tree relies on the age threshold rather than liveness. Verifying
  the interactive trust-flow + hook firing end-to-end once CDX01 is on `main`
  is the natural follow-up dogfood pass.
- **Launch-side vs in-session log context.** Records written *by the launch*
  (Tree provisioning, the launch milestone) carry the launcher process's log
  context (whatever agent/epic ran `shipit session codex`) alongside the
  session key; records written *inside* the session carry the session identity
  from the env exports. Filter with `--session <id>` and both halves join.
- **Headless smoke needs `< /dev/null`.** Inherited open stdin blocks
  `codex exec` (see above); the interactive path is unaffected.
