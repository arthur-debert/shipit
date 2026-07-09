# A stock Ubuntu 24.04 box — the from-zero baseline the self-provisioning
# harness (docker/verify-self-provision.sh) proves Layer 0 against, and the
# reusable base image for future container needs (docs/dev/containers.md).
#
# Only what a stock server/cloud image legitimately has is installed: git,
# curl, CA certs, bash, tar, and a non-root user. Deliberately NO pixi, uv,
# node, cargo, or go — `bin/setup-dev-env.sh` must provision everything above
# this line itself, or the harness fails. Ubuntu 24.04 mirrors the Claude Code
# cloud sandbox's VM base (code.claude.com docs), so a green harness run is
# evidence for the cloud path too.
FROM ubuntu:24.04

RUN apt-get update \
	&& apt-get install -y --no-install-recommends \
	bash \
	ca-certificates \
	curl \
	git \
	tar \
	&& rm -rf /var/lib/apt/lists/*

# The bootstrap targets ~/.local/bin and must never need root: run as the
# stock non-root `ubuntu` user (uid 1000) Ubuntu 24.04 images ship with.
USER ubuntu
WORKDIR /home/ubuntu
