"""channel — the Artifact channel's store and (later) endpoint plumbing (ARF01).

The Artifact channel (CONTEXT.md, docs/spec/artifact-channel.md) is the
portfolio's durable, versioned store of published Artifacts, realized as
per-repo conda channels in dedicated GCS buckets. This package is that store's
home in the binary. The first module — :mod:`.store_provision` — provisions the
two access-tier buckets (public-authless / private-GCS-creds, ADR-0065) and
their IAM, idempotently, and verifies the access model live.

Producer-endpoint, consumer-projection, and cascade concerns are OTHER work
streams of ARF01 and do NOT live here yet.
"""
