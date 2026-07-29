# Review-App provisioning — `checks: write` for the local-review funnel

The local-review **funnel** (OBS02, [ADR-0005](../adr/0005-local-review-funnel-via-check-runs.md))
rides on GitHub **check runs authored by the review App**: shipit creates a
`review: <reviewer>` check run (`status: in_progress` → `status: completed` with `conclusion: success`/`failure`/`timed_out`) so a
requested / in-flight / failed local review is visible on the PR. Creating a check run
needs the App's installation token to carry **`checks: write`**.

The review Apps were minted with only `contents:read`, `metadata:read`,
`pull_requests:write` — **no `checks` permission** — so a check-run create returns
`403 Resource not accessible by integration` today. Granting `checks: write` is a
**one-time, owner-only GitHub UI action** (there is no API to change an App's
permission set or to re-consent an installation), and it must be done **per App** and
re-consented **per installation (owner)**. This is the provisioning step the
local-reviewer rollout ([#26](https://github.com/arthur-debert/shipit/issues/26),
[OBS02-WS03 #39](https://github.com/arthur-debert/shipit/issues/39)) depends on.

> Note: only the **`checks: write` permission** is required — shipit *creates* check
> runs via the REST API; it does not *listen* for check events, so no `check_run` /
> `check_suite` webhook subscription is needed.

## The two Apps (owner: `arthur-debert`, user-owned)

| App | slug | permissions settings URL |
| --- | --- | --- |
| codex | `adr-codex-review` | <https://github.com/settings/apps/adr-codex-review/permissions> |
| agy | `adr-agy-review` | <https://github.com/settings/apps/adr-agy-review/permissions> |

## Step 1 — add the permission (per App)

For **each** App's permissions URL above:

1. Open the permissions page.
2. Under **Repository permissions**, find **Checks** and set it to **Read and write**.
3. Click **Save changes** at the bottom.

Saving marks the new permission as *pending approval* on every installation of that App.

> **This Step-2 acceptance is the part that's easy to miss.** The App-level grant
> does NOT propagate on its own — every installation's minted token keeps the OLD
> scopes until its owner explicitly accepts. Verified empirically: after Step 1
> alone, all six installation tokens still lacked `checks` and a check-run create
> returned `403`. Step 1 is necessary but not sufficient; Step 2 is what makes it real.

## Step 2 — re-consent each installation (per owner)

Each installation must separately approve the newly-requested permission. For a
self-owned install, approve it yourself; an org install must be approved by an owner of
that org.

| Owner | codex install | agy install |
| --- | --- | --- |
| `arthur-debert` (User) | <https://github.com/settings/installations/141781663> | <https://github.com/settings/installations/141781645> |
| `phos-editor` (Org) | <https://github.com/organizations/phos-editor/settings/installations/141781718> | <https://github.com/organizations/phos-editor/settings/installations/141781611> |
| `lex-fmt` (Org) | <https://github.com/organizations/lex-fmt/settings/installations/141781689> | <https://github.com/organizations/lex-fmt/settings/installations/141781586> |

On each install page, accept the **"updated permissions"** request (a banner / "Review
request" → "Accept new permissions").

For the immediate shipit / `shipit-canary` work, only the **`arthur-debert`** install
matters; the two org installs (`phos-editor`, `lex-fmt`) are needed when those consumers
adopt local reviews (the #26 rollout).

## Step 3 — verify

Run `shipit verify-apps` from the target repo (or `shipit verify-apps owner/repo` from
anywhere). It mints each App's installation token and reads the granted permissions — a
cheap read that creates no check run. It reports one of three things, and the exit code
says which:

| exit | verdict | meaning | who fixes it |
| --- | --- | --- | --- |
| `0` | `LIVE` | installed on the owner **and** holds `checks: write` | nobody |
| `1` | `NOT LIVE` | the App is absent from the owner, or its token lacks `checks: write` | the owner — Step 1 / Step 2 above |
| `2` | `UNVERIFIED` | nothing was checked: this machine cannot mint App credentials, or the probe itself failed | you, here — see below |

`UNVERIFIED` says nothing about the repo. It means the machine you ran it on could not
produce App credentials (no `doppler` on `PATH`, not logged in, or the key is missing
from `github/prd`) so GitHub was never asked. Fix that and re-run before drawing any
conclusion about the Apps.

After the re-grant + re-consent, the App's installation token carries `checks: write`.
The same thing can be confirmed by hand by minting a token and creating a check run on a
throwaway commit:

- the create-installation-token response's `permissions` now includes `checks: write`;
- `POST /repos/<owner>/<repo>/check-runs` returns **201** (not 403).

OBS02-WS03 ships this as a runnable harness — `shipit.review.funnel_verify` — that drives
the **full** funnel lifecycle (kickoff create → terminal transition) on a canary PR and
asserts all of the above on the same run. It is **opt-in** (it hits live GitHub + needs
the Doppler App creds + a canary PR), so it is **never** part of `pixi run test` / CI; run
it explicitly against a throwaway canary PR:

```bash
pixi run -e review verify-funnel --repo arthur-debert/shipit-canary --pr <N> --agent codex
# or: SHIPIT_FUNNEL_CANARY_{REPO,PR}=… python -m shipit.review.funnel_verify
```

It exits `0` on a full PASS, `1` on any failed check. Verified live for the
`arthur-debert` owner on `shipit-canary` — **PASS** for both `codex` and `agy` (token
carries `checks: write`; create returns 201 `in_progress`+`started_at`; the same run
transitions to `completed`/`success` with `output`+`completed_at`).

## Adding a new consumer later

**The App-level `checks: write` grant does NOT propagate on its own.** Step 1 is global
(done once per App) and is already done — but a token minted for a **new** consumer (a
new install / a new owner) keeps the **old** scopes until *that installation* is
re-consented. So onboarding a new consumer for the OBS02 funnel still requires the
one-time, per-install **Step 2 accept** by that owner; without it, the consumer's minted
token lacks `checks` and its funnel check-run create returns **403** — the local review
still *posts* (that path is unaffected), but the `review: <reviewer>` funnel breadcrumb
never appears.

Concretely, for a new owner: Step 1 is already satisfied (the App permission is
`checks: write` globally), so **only Step 2** — approve the new owner's installation — is
needed when onboarding a consumer per #26. Re-run the Step 3 harness against that
consumer's repo to confirm the token now carries `checks: write` and the create returns
201.

## Opting ONE consumer repo into codex + agy — the whole recipe

A consumer's seeded roster is `copilot` alone, so its PRs get a one-reviewer net. This
is everything that must be true for `codex` and `agy` to work there. Nothing in it is
shipit-repo-specific — that was the [#969](https://github.com/arthur-debert/shipit/issues/969)
bug, and it is fixed: PyJWT rides the base install, and no error points at a pixi env
that exists only in shipit's own checkout.

**On the machine that drives the review** (local-agent reviews run as a detached child
process HERE, not in CI — the operator's machine is where the model runs and where the
App token is minted):

1. **`doppler` on `PATH` and logged in** to the `github` project, `prd` config —
   that is where `CODEX_REVIEW_APP_PRIVATE_KEY` / `CODEX_REVIEW_APP_ID` and the `AGY_…`
   pair live. The PEM is read into memory and signed there; it is never written to disk.
2. **The agent CLIs on `PATH`**: `codex` and `agy`. They are what actually produce the
   review.

Note what is NOT on that list: the shipit build. Which shipit runs in a repo is a
property of the **repo**, not the machine — see the pin below.

**In the consumer repo** (two edits):

1. **Bump the pin to a shipit build ≥ 1.7.0**, so the build the repo runs carries
   PyJWT. `.shipit.toml`'s `[shipit].version` is the full commit sha of that build,
   and the managed `bin/shipit` launcher execs exactly it via `uv` — **`PATH` is never
   consulted** (ADR-0033). The bump normally arrives as the repo's install-reconcile PR;
   `shipit install --pr` stamps it.

2. **Add the reviewers to the roster:**

   ```toml
   # .shipit.toml
   [reviewers]
   copilot = {}
   codex = {}
   agy = {}
   ```

   The `[reviewers]` table is consumer-owned — `shipit install` never rewrites it — so
   this edit survives reconcile. `rerun` defaults off (review-once); add
   `codex = { rerun = true }` to re-review every push.

**Then verify, before relying on it:**

```bash
./bin/shipit verify-apps    # from the repo; expect: LIVE, exit 0
```

Go through `./bin/shipit`, not a bare `shipit`: the launcher is what applies the repo's
pin. A bare `shipit` runs whatever `uv tool` build is on the operator's `PATH`, which
is a different — and probably older — shipit, so it answers for the wrong build. (That
build's one remaining job inside a repo is a virgin repo's first `shipit install`, which
stamps the pin the launcher then needs.)

- exit `2` / `UNVERIFIED` → one of the two machine prerequisites above is missing, or
  the repo's pin predates 1.7.0. Nothing is known about the repo yet.
- exit `1` / `NOT LIVE` → a real gap on the repo's owner: do Step 1 / Step 2 above for
  that owner. `lex-fmt` and `phos-editor` are already consented (see the table); a new
  org is not.

Once `verify-apps` reports LIVE, `./bin/shipit pr next` requests all three reviewers on
the repo's PRs like any other roster.
