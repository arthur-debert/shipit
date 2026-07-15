# Artifact channel store provisioning — the two access-tier buckets (ARF01-WS03)

The Artifact channel's store is **two dedicated GCS buckets** in the existing
sccache GCP project, on a lifecycle **separate from the sccache bucket**
([ADR-0065](../adr/0065-artifact-channel-access-tiers-two-buckets.md),
[Spec](../spec/artifact-channel.md)):

| Tier | Bucket (derived) | Access model |
| --- | --- | --- |
| **public** | `<project>-artifact-channel-public` | `allUsers` → `roles/storage.objectViewer`; authless HTTPS reads. Public-access-prevention **inherited** (the `allUsers` grant is permitted). |
| **private** | `<project>-artifact-channel-private` | a dedicated reader service account (`artifact-channel-reader@<project>.iam.gserviceaccount.com`) granted **bucket-scoped** `roles/storage.objectViewer`; **no** public binding; public-access-prevention **enforced**. |

Both buckets have **uniform bucket-level access (UBLA)** on — IAM-only, no
per-object ACLs. Neither carries an object-lifecycle / TTL rule (artifacts are
permanent, and the sccache purge targets the sccache bucket by name), so a cache
purge can never touch them. The names carry the `artifact-channel` infix so they
are unmistakably distinct from the sccache bucket.

This runbook is executed by the **idempotent provisioner**
[`shipit.channel.store_provision`](../../src/shipit/channel/store_provision.py):
its decision core (bucket names, every `gcloud` argv, the UBLA / public-binding
verdict readers) is pure and unit-tested; the boundary drives `gcloud` through
the one Exec seam. It is an **opt-in operator** entrypoint (like the review-App
harness, [`review-app-provisioning.md`](review-app-provisioning.md)): it needs
the operator's own `gcloud` credentials and provisions live cloud infra, so it
is **never** part of `pixi run test` / CI and is **not** a per-consumer `shipit`
verb. Its logic is regression-covered by `tests/test_channel_store_provision.py`
with the `gcloud` boundary faked.

## Prerequisites

- `gcloud` authenticated as a principal with **project-admin** rights on the
  sccache project (bucket create, IAM policy set, service-account create):
  `gcloud auth login` and `gcloud config set project <project>`.
- The **project id** of the sccache project and the **bucket location** (e.g.
  `US` multi-region, or the sccache bucket's region for locality).

## Provision (idempotent, repeatable)

```bash
python -m shipit.channel.store_provision --project <sccache-project> provision --location US
```

Describe-then-act: the reader service account and each bucket are created only
when absent; UBLA and public-access-prevention are re-asserted (`buckets update`
no-ops when already set) and the IAM bindings re-added (`add-iam-policy-binding`
of an existing binding is a no-op). **Running it twice mutates nothing** — every
action reports `noop`. Add `--json` for a machine-readable report.

The exact `gcloud` operations (all assembled in the provisioner, ADR-0028):

1. `gcloud iam service-accounts create artifact-channel-reader …` (skipped if it
   already exists).
2. `gcloud storage buckets create gs://<project>-artifact-channel-public …
   --uniform-bucket-level-access --no-public-access-prevention` (PAP inherited;
   skipped if it exists), then `gcloud storage buckets update …` to re-assert.
3. same for `…-artifact-channel-private` with `--public-access-prevention`
   (PAP enforced).

   `gcloud storage buckets` spells public-access-prevention as a **boolean**
   flag — `--public-access-prevention` (enforced) / `--no-public-access-prevention`
   (inherited), not a `=value`.
4. `gcloud storage buckets add-iam-policy-binding gs://…-public
   --member=allUsers --role=roles/storage.objectViewer`.
5. `gcloud storage buckets add-iam-policy-binding gs://…-private
   --member=serviceAccount:artifact-channel-reader@<project>.iam.gserviceaccount.com
   --role=roles/storage.objectViewer`.

## Private-tier consumer credentials (HMAC interop keys)

A private-tier consumer reaches the channel as an S3-compatible conda channel
over GCS's interop endpoint (ADR-0065). Mint an **HMAC key** for the reader SA —
its only grant is bucket-scoped `objectViewer`, so the key can read the private
bucket and nothing else:

```bash
gcloud storage hmac keys create artifact-channel-reader@<project>.iam.gserviceaccount.com
```

The returned `accessId` / `secret` become the consumer's `AWS_ACCESS_KEY_ID` /
`AWS_SECRET_ACCESS_KEY` (Doppler locally, the sccache credential path in CI —
never `pixi auth login`, which is unwired for S3 in pixi 0.71.0, ADR-0065). This
runbook mints the credential the store honours; the consumer's **pixi config**
is projected by `shipit install` (ARF01-WS04).

### Consumer read-cred path (private tier)

A downstream repo that declares an `[artifact-deps.<pkg>]` pin on a **private**
producing repo gets, from `shipit install`
([`shipit.install.artifactdeps`](../../src/shipit/install/artifactdeps.py)), a
managed pixi block projecting the `s3://<bucket>/<repo>` channel plus the
validated `[s3-options.<bucket>]` config (`endpoint-url`, `region = "auto"`,
`force-path-style = true`) — templated **directly** into the manifest, never via
`pixi config set s3-options.*` (a silent no-op in 0.71.0). The tier is derived
from the producing repo's visibility, so no consumer flag selects it.

The **credentials are never committed** — the projected manifest carries only
the endpoint config. pixi's S3 backend reads them from the environment at
resolve time:

- **`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`** — the reader SA's HMAC key
  pair minted above; **or**
- **`RATTLER_AUTH_FILE`** — a rattler auth JSON pointing at the same HMAC pair.

Delivery mirrors the sccache credential rail: **Doppler** sourced onto the shell
locally, and the **same credential path as sccache** in CI. `pixi auth login` is
**not** used (unwired for S3 in 0.71.0). Because the credentials live only in the
environment, a consumer with none genuinely **cannot** resolve the private
channel — the access control is real, not cosmetic.

## Verify (the live acceptance checks)

```bash
python -m shipit.channel.store_provision --project <project> verify --repo <owner>/<repo>
```

Asserts, on the same run (exit 0 only if all pass):

- public authless GET of `<repo>/repodata.json` → **200**;
- private authless GET → **403** (no creds → denied);
- private read **as the reader SA** (impersonation) → succeeds (scoped cred works);
- UBLA on **both** buckets;
- the private bucket has **no** public IAM binding.

The positive scoped read needs a published `<repo>/repodata.json` object in the
private bucket; until the producer endpoint (a later WS) publishes one, that one
check is reported in `notes` rather than silently passed — provision + the other
four checks still verify the access model.

## Teardown

```bash
# Delete objects then the buckets (both tiers), and the reader SA:
gcloud storage rm --recursive gs://<project>-artifact-channel-public
gcloud storage rm --recursive gs://<project>-artifact-channel-private
gcloud storage buckets delete gs://<project>-artifact-channel-public
gcloud storage buckets delete gs://<project>-artifact-channel-private
gcloud iam service-accounts delete artifact-channel-reader@<project>.iam.gserviceaccount.com
```

Teardown is destructive and non-idempotent by nature — it is an operator action,
not part of the provisioner. The sccache bucket is a **different** bucket and is
never touched by any command here.

## Rotation

- **HMAC key rotation** (the routine case — the reader SA and its bucket binding
  never change): mint a new key (`gcloud storage hmac keys create …`), roll it
  into the consumer credential rail, then delete the old key
  (`gcloud storage hmac keys delete <accessId>`). Old `pixi.lock`-recorded
  package URLs keep resolving (the URL is not the credential — ADR-0065's
  rejection of the capability-URL model).
- **Reader-SA rotation** (compromise): create a replacement SA, re-run
  `provision` after updating `READER_SA_NAME`, mint its HMAC key, roll consumers,
  then delete the old SA and its bucket binding.

## Org-policy caveat (a possible external blocker)

The public tier's `allUsers` grant requires the org policy `iam.allowedPolicyMemberDomains`
to permit `allUsers`, and public-access-prevention to be allowed on that bucket.
If an org policy **enforces** public-access-prevention project-wide, the public
tier cannot be provisioned as-is — that is an org-admin exception, tracked as a
provisioning prerequisite, not something the provisioner can work around.
