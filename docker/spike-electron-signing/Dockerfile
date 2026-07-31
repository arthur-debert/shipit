# Linux container for the electron packaging + signing spike (issue #1197).
# Carries: node (electron build + @electron/packager), rcodesign (signing +
# notarization from Linux), nfpm (deb), xorriso + libdmg-hfsplus `dmg`
# (UDIF dmg assembly on Linux), zip.
FROM node:22-bookworm

ARG RCODESIGN_VERSION=0.29.0
ARG NFPM_VERSION=2.47.0
ARG TARGETARCH

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl git python3 zip xorriso cmake make g++ zlib1g-dev libbz2-dev \
    && rm -rf /var/lib/apt/lists/*

# rcodesign — static musl binary from indygreg/apple-platform-rs releases.
RUN set -eux; \
    case "${TARGETARCH}" in \
      arm64) RC_ARCH=aarch64 ;; \
      amd64) RC_ARCH=x86_64 ;; \
    esac; \
    curl -fsSL -o /tmp/rcodesign.tar.gz \
      "https://github.com/indygreg/apple-platform-rs/releases/download/apple-codesign%2F${RCODESIGN_VERSION}/apple-codesign-${RCODESIGN_VERSION}-${RC_ARCH}-unknown-linux-musl.tar.gz"; \
    tar -xzf /tmp/rcodesign.tar.gz -C /tmp; \
    install -m 0755 /tmp/apple-codesign-*/rcodesign /usr/local/bin/rcodesign; \
    rm -rf /tmp/rcodesign.tar.gz /tmp/apple-codesign-*; \
    rcodesign --version

# nfpm — deb/rpm/apk packager.
RUN set -eux; \
    case "${TARGETARCH}" in \
      arm64) NF_ARCH=arm64 ;; \
      amd64) NF_ARCH=x86_64 ;; \
    esac; \
    curl -fsSL -o /tmp/nfpm.tar.gz \
      "https://github.com/goreleaser/nfpm/releases/download/v${NFPM_VERSION}/nfpm_${NFPM_VERSION}_Linux_${NF_ARCH}.tar.gz"; \
    tar -xzf /tmp/nfpm.tar.gz -C /tmp nfpm; \
    install -m 0755 /tmp/nfpm /usr/local/bin/nfpm; \
    rm -f /tmp/nfpm.tar.gz /tmp/nfpm; \
    nfpm --version

# libdmg-hfsplus `dmg` tool — converts the xorrisofs HFS+ image into a real
# UDIF (UDZO) dmg that rcodesign can sign and the notary service accepts.
# fanquake's fork is the maintained one (used by bitcoin-core release builds).
RUN set -eux; \
    git clone --depth 1 https://github.com/fanquake/libdmg-hfsplus.git /tmp/libdmg; \
    cmake -S /tmp/libdmg -B /tmp/libdmg/build; \
    make -C /tmp/libdmg/build -j"$(nproc)"; \
    install -m 0755 /tmp/libdmg/build/dmg/dmg /usr/local/bin/dmg; \
    rm -rf /tmp/libdmg

WORKDIR /work
