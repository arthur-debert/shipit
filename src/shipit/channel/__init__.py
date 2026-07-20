"""channel — the Artifact channel's store and (later) endpoint plumbing (ARF01).

The Artifact channel (CONTEXT.md, docs/spec/artifact-channel.md) is the
portfolio's durable, versioned store of published Artifacts, realized as
per-repo conda channels in dedicated GCS buckets. This package is that store's
home in the binary. Its modules are:

- :mod:`.store_provision` — provisions the two access-tier buckets
  (public-authless / private-GCS-creds, ADR-0065) and their IAM, idempotently,
  and verifies the access model live;
- :mod:`.buckets` — the ONE repo-internal source of truth for the two
  access-tier bucket names, shared by the producer endpoint, the consumer
  projection, and the store provisioner (ADR-0065).

Under conda-direct (ADR-0077) there is no artifact-pinned Cascade: a consumer's
version pin is an ordinary pixi dependency bumped by ``pixi update`` / a generic
bot, so the bespoke receive/bump rail was removed. Producer-endpoint and
consumer-projection concerns are OTHER work streams of ARF01 and do NOT live
here.
"""
