"""buckets — the Artifact channel's two access-tier bucket names (ARF01-WS08 convergence).

The ONE repo-internal source of truth for the Artifact channel store's bucket
names, shared by the three sides that must agree on them (ADR-0065,
docs/spec/artifact-channel.md):

- the **producer** ``conda`` endpoint (:mod:`shipit.release.publish`) — WRITES a
  release's ``.conda`` to ``<bucket>/<owner/name>`` over GCS S3-interop;
- the **consumer** projection (:mod:`shipit.install.artifactdeps`) — READS from
  the same ``<bucket>/<owner/name>`` per-repo channel (the public tier over its
  authless HTTPS URL, the private tier over the ``s3://`` interop rail); and
- the **store provisioner** (:mod:`shipit.channel.store_provision`) — CREATES
  exactly these buckets.

They previously drifted: WS01/WS02/WS04 baked the fixed names below while WS03
provisioning DERIVED ``<project>-artifact-channel-<tier>`` from its ``--project``,
so the provisioner created buckets the producer never wrote to and the consumer
never read from. The authoritative Spec (docs/spec/artifact-channel.md — the
readiness gate names ``shipit-artifacts-public`` and WS03 as its provisioner)
already fixed the names, so this module makes those fixed names the single
definition all three import, and a drift test
(``tests/test_install_artifact_deps.py``) pins the three consumers to it.

There is exactly ONE shipit-portfolio Artifact channel: every producing repo is
the sole writer of its own ``<bucket>/<owner/name>`` subdir (ADR-0064), so the
two portfolio-wide bucket names are fixed constants, not a per-repo/per-project
derivation. The names are globally unique (GCS bucket names are global) and
unmistakably NOT the sccache bucket.
"""

from __future__ import annotations

#: The public-read / authless tier bucket (ADR-0065). Consumers list
#: ``https://storage.googleapis.com/<bucket>/<owner/name>`` and need no auth; the
#: producer writes to it with the ``conda`` endpoint's HMAC write credential.
PUBLIC_ARTIFACT_BUCKET = "shipit-artifacts-public"

#: The private / credentialed tier bucket (ADR-0065). No public access; reached
#: as an S3-compatible conda channel (``s3://<bucket>/<owner/name>``) over GCS's
#: interop endpoint with env-var HMAC read credentials.
PRIVATE_ARTIFACT_BUCKET = "shipit-artifacts-private"

#: The GCS global HTTPS host both tiers use (ADR-0065): the public tier reads
#: over it directly, and it is the S3-interop endpoint the private tier's
#: ``[s3-options]`` and the producer's rattler-build S3 backend both point at.
CHANNEL_HOST = "https://storage.googleapis.com"

#: The CLOSED set of conda subdirs the channel serves (ADR-0064: osx-arm64,
#: linux-64, linux-aarch64, win-64 — no osx-64, no musl). The SoT for "which
#: subdirs exist", shared by the producer (``release.publish.CONDA_SUBDIRS``
#: maps each release triple ONTO one of these; a drift test pins their value set
#: to this) and the store provisioner (:func:`shipit.channel.store_provision.verify`
#: probes ``repodata.json`` under EACH of these). Repodata is PER-SUBDIR, so a
#: root-level probe would miss the real channel layout / a partial publish — the
#: readiness gate (docs/spec/artifact-channel.md §3) checks all of these, and so
#: does ``verify``.
SERVED_SUBDIRS: tuple[str, ...] = ("osx-arm64", "linux-64", "linux-aarch64", "win-64")
