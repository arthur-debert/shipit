# Artifact channel access tiers: two buckets, public-authless and private-GCS-creds

The portfolio has both open and closed artifacts — lex's tooling is fully open
source; phos's artifacts must stay private. The Artifact channel (ADR-0064)
must serve both. A private channel forces credentials onto every consumer (CI
*and* dev machines) just to `pixi install`; a public channel needs none. We
wanted the cheapest correct access model.

## Decision

- **Two tiers, two dedicated buckets:** a public-read bucket (open artifacts,
  authless HTTPS channel URL) and a private bucket (no public access, GCS
  credentials required). The tier of a channel is *which bucket it lives in*.
- **Tier is derived from the producing repo's visibility**, not declared by the
  consumer — one less thing to drift.
- **Public tier:** consumers list `https://storage.googleapis.com/<bucket>/<repo>`
  and need no auth.
- **Private tier:** consumers reach the channel as an S3-compatible conda
  channel over GCS's interop endpoint. The working pixi config (verified live)
  is:

  ```toml
  [s3-options.<bucket>]
  endpoint-url     = "https://storage.googleapis.com"
  region           = "auto"
  force-path-style = true
  ```

  with `channels = ["s3://<bucket>/<repo>", ...]`.
- **Credentials are env vars, not `pixi auth login`:** `AWS_ACCESS_KEY_ID` /
  `AWS_SECRET_ACCESS_KEY` (a GCS HMAC interop key) or a `RATTLER_AUTH_FILE`.
  `pixi auth login --s3-*` is unwired in pixi 0.71.0 (see Consequences).
  Locally the creds arrive through the existing Doppler rail (sourced on shell
  → env vars); in CI they ride the **same credential path as sccache**.
- The provisioner **templates the `[s3-options]` TOML directly** and never
  shells out to `pixi config set s3-options.*` (a silent no-op in 0.71.0).

### Alternatives rejected

- **One bucket with prefix-scoped IAM** (a public prefix and a private prefix
  in the same bucket) — impossible under uniform bucket-level access, where
  "public" is a bucket-wide `allUsers` grant (verified live); and even with
  per-object ACLs, one IAM-condition mistake leaks the private artifacts. Two
  buckets is a hard boundary, not a predicate to fat-finger.
- **A capability URL** (a public bucket at an unguessable/secret path injected
  via a secret) — obscurity, not access control, and it leaks through the one
  file the pin-and-commit model commits: `pixi.lock` records the full resolved
  package URL. Revocation would mean rotating the path and re-injecting
  everywhere, and public bucket *listing* would have to be suppressed. Rejected
  in favor of real, revocable auth.
- **prefix.dev private hosting** — ~$60/mo, not justified.

## Consequences

- Both tiers validated **live** against a real GCS bucket (throwaway bucket,
  torn down): the private resolve worked and its **no-creds negative correctly
  failed** (genuinely access-controlled); the public authless resolve worked.
- **Publish** (producer CI writes) needs write creds to the tier's bucket — the
  `conda` endpoint's `ENDPOINT_SECRETS` entry. **Consume** (downstream reads)
  needs read creds *only for the private bucket*; the public bucket needs none.
- `region = "auto"` and `force-path-style = true` are load-bearing for GCS
  interop; the endpoint is the global `https://storage.googleapis.com`. HMAC
  keys are tied to a service account granted `roles/storage.objectViewer` on
  the bucket (bucket-scoped IAM, fine under uniform bucket-level access).
- Two pixi 0.71.0 bugs shape the implementation and must be revisited on a pin
  bump: `pixi config set s3-options.*` no-ops (template TOML directly), and
  `pixi auth login --s3-*` is unwired (use env vars / `RATTLER_AUTH_FILE`).
