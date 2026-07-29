"""``channel/buckets`` — the Artifact channel's bucket names, host, and served subdirs.

See docs/adr/0065-artifact-channel-access-tiers-two-buckets.md.
"""

from __future__ import annotations

#: The public-read / authless tier bucket.
PUBLIC_ARTIFACT_BUCKET = "shipit-artifacts-public"

#: The private tier bucket, reached as an S3-compatible conda channel.
PRIVATE_ARTIFACT_BUCKET = "shipit-artifacts-private"

#: The GCS host both tiers use — a direct read for public, the S3-interop endpoint for private.
CHANNEL_HOST = "https://storage.googleapis.com"

#: The CLOSED set of PER-PLATFORM conda subdirs the channel serves. Repodata is
#: per-subdir, so a root-level probe would miss a partial publish.
SERVED_SUBDIRS: tuple[str, ...] = ("osx-arm64", "linux-64", "linux-aarch64", "win-64")

#: The single platform-independent subdir data artifacts ride, deliberately NOT a
#: member of :data:`SERVED_SUBDIRS`: it has no OS×arch fan-out and never pauses.
NOARCH_SUBDIR: str = "noarch"
