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

After the re-grant + re-consent, the App's installation token carries `checks: write`.
Confirm by minting a token and creating a check run on a throwaway commit:

- the create-installation-token response's `permissions` now includes `checks: write`;
- `POST /repos/<owner>/<repo>/check-runs` returns **201** (not 403).

OBS02-WS03 ships this as a runnable harness — `shipit.review.funnel_verify` — that drives
the **full** funnel lifecycle (kickoff create → terminal transition) on a canary PR and
asserts all of the above on the same run. It is **opt-in** (it hits live GitHub + needs
the Doppler App creds + a canary PR), so it is **never** part of `pixi run test` / CI; run
it explicitly against a throwaway canary PR:

```bash
pixi run -e verify verify-funnel --repo arthur-debert/shipit-canary --pr <N> --agent codex
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
