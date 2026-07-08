# Containers: the stock-Ubuntu image and the self-provisioning harness

`docker/` carries shipit's container tooling (#547): a stock `ubuntu:24.04`
image and an end-to-end harness that proves the Layer 0 bootstrap
(`bin/setup-dev-env.sh`) provisions the whole base system from zero.

## The image (`docker/ubuntu.Dockerfile`)

A deliberately bare Ubuntu 24.04 box: `git`, `curl`, CA certificates, `bash`,
`tar`, and a non-root user — only what a stock server or cloud image
legitimately has. **No pixi, no uv, no node/cargo/go.** That emptiness is the
point: anything the harness finds working afterwards was provisioned by the
code under test, not inherited from the image.

The image is intended for reuse. Future container needs — an `act` harness for
workflow testing, WF01 lanes, any "does this work on a machine that isn't a
laptop?" question — should start from this Dockerfile rather than grow a
second base image.

## The harness (`docker/verify-self-provision.sh`)

```sh
bash docker/verify-self-provision.sh
```

Host-side driver, fail-hard (unlike the bootstrap itself, which is fail-open
because it runs from the managed SessionStart hook). It:

1. clones the repo's **HEAD** into a clean copy (never the live working tree,
   so uncommitted edits can neither help nor hurt the verdict; `.git` rides
   along because the lint gate reads its scope from `git ls-files`);
2. builds the image and ships the clone in over a tar pipe (no bind mount —
   container-local files dodge the host-uid ownership mismatch a mount hits on
   CI runners);
3. runs `bin/setup-dev-env.sh` inside as the non-root user, **from zero**, and
   asserts:
   - `pixi --version` / `uv --version` resolve exactly the script's pins (the
     harness reads the pins out of the script — it carries no second copy that
     could drift);
   - a second run is a fast no-op (idempotence: no `reconciling` line);
   - `pixi install --locked` succeeds for the default and `lint` envs;
   - `pixi run -e lint lint` goes green end-to-end (lexd provisioned the same
     way CI does it, via the managed `provision-lexd` task).

Host-arch native: the same script works on a linux/amd64 CI runner and on
linux/arm64 Docker Desktop. There is deliberately no qemu multi-arch matrix.

## Cloud parity

The Claude Code cloud sandbox (per the official docs at code.claude.com) runs
Ubuntu 24.04 VMs; rust/go/node/python/uv/docker are preinstalled there but
**pixi is not**, and `CLAUDE_CODE_REMOTE=true` gates the cloud environment.
Its default "Trusted" network egress allowlist carries `github.com` and
`release-assets.githubusercontent.com` (and `conda.anaconda.org` for the
conda-forge solves) but **not** `pixi.sh` or `astral.sh` — which is why
`bin/setup-dev-env.sh` fetches pinned, sha256-verified GitHub release tarballs
and never a `curl | sh` vendor installer. A green harness run on this image is
therefore evidence for the cloud path too: same OS, same egress-compatible
fetch path, same from-zero start for pixi.

## Deliberately not in CI

The harness downloads on the order of a gigabyte of toolchains per run, so it
is opt-in and **not** wired into `ci.yml` or the required checks. If it earns
a standing lane later, the natural adoption is a `workflow_dispatch` /
nightly job that runs `bash docker/verify-self-provision.sh` on
`ubuntu-latest` — cheap to add, and this doc plus the two files under
`docker/` are the whole surface it needs.
