# Fleet baseline image, rust layer — the container Substrate's dev-loop image.
# Carries the rust toolchain, the linters the fleet's rust verbs need, gh, and
# shipit itself; code mounts from the host at /work and the shipit verbs
# execute in here. Version pins mirror the fleet's managed pixi pins so a
# container run and a host run resolve the same tool versions.
#
# Build context: a scratch dir holding wheels/shipit-*.whl — assembled by
# `lab/substrate-spike/spike.sh build-image`. linux-arm64 download pins only.
FROM ubuntu:24.04@sha256:4fbb8e6a8395de5a7550b33509421a2bafbc0aab6c06ba2cef9ebffbc7092d90

ARG RUST_VERSION=1.96.1
ARG NEXTEST_VERSION=0.9.140
ARG NODE_VERSION=24.18.0
ARG PRETTIER_VERSION=3.8.5
ARG MARKDOWNLINT_CLI_VERSION=0.49.0
ARG SHELLCHECK_VERSION=0.10.0
ARG SHFMT_VERSION=3.13.1
ARG ACTIONLINT_VERSION=1.7.12
ARG YAMLLINT_VERSION=1.38.0
ARG RUFF_VERSION=0.15.20
ARG UV_VERSION=0.11.28
ARG GH_VERSION=2.97.0
ARG PYTHON_VERSION=3.13

# build-essential: the linker cargo needs; xz-utils: node/shellcheck tarballs.
RUN apt-get update \
	&& apt-get install -y --no-install-recommends \
	bash \
	build-essential \
	ca-certificates \
	curl \
	git \
	tar \
	xz-utils \
	&& rm -rf /var/lib/apt/lists/*

# Standalone linter + gh binaries.
RUN curl -fsSL "https://github.com/koalaman/shellcheck/releases/download/v${SHELLCHECK_VERSION}/shellcheck-v${SHELLCHECK_VERSION}.linux.aarch64.tar.xz" \
	| tar -xJ -C /tmp \
	&& mv "/tmp/shellcheck-v${SHELLCHECK_VERSION}/shellcheck" /usr/local/bin/ \
	&& rm -rf "/tmp/shellcheck-v${SHELLCHECK_VERSION}" \
	&& curl -fsSL -o /usr/local/bin/shfmt \
	"https://github.com/mvdan/sh/releases/download/v${SHFMT_VERSION}/shfmt_v${SHFMT_VERSION}_linux_arm64" \
	&& chmod +x /usr/local/bin/shfmt \
	&& curl -fsSL "https://github.com/rhysd/actionlint/releases/download/v${ACTIONLINT_VERSION}/actionlint_${ACTIONLINT_VERSION}_linux_arm64.tar.gz" \
	| tar -xz -C /usr/local/bin actionlint \
	&& curl -fsSL "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_arm64.tar.gz" \
	| tar -xz -C /tmp \
	&& mv "/tmp/gh_${GH_VERSION}_linux_arm64/bin/gh" /usr/local/bin/ \
	&& rm -rf "/tmp/gh_${GH_VERSION}_linux_arm64"

# node + the node-hosted linters.
RUN curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-arm64.tar.xz" \
	| tar -xJ -C /opt \
	&& mv "/opt/node-v${NODE_VERSION}-linux-arm64" /opt/node \
	&& PATH="/opt/node/bin:${PATH}" npm install -g --no-fund --no-audit \
	"prettier@${PRETTIER_VERSION}" \
	"markdownlint-cli@${MARKDOWNLINT_CLI_VERSION}"

# rust toolchain (cargo + clippy + rustfmt) shared system-wide, + nextest.
ENV RUSTUP_HOME=/opt/rustup CARGO_HOME=/opt/cargo
RUN curl -fsSL https://sh.rustup.rs \
	| sh -s -- -y --no-modify-path --profile minimal \
	--default-toolchain "${RUST_VERSION}" \
	--component clippy --component rustfmt \
	&& curl -fsSL "https://get.nexte.st/${NEXTEST_VERSION}/linux-arm" \
	| tar -xz -C /opt/cargo/bin

# uv-managed python: the python-hosted linters and shipit itself as uv tools.
ENV UV_PYTHON_INSTALL_DIR=/opt/uv/python \
	UV_TOOL_DIR=/opt/uv/tools \
	UV_TOOL_BIN_DIR=/usr/local/bin
RUN curl -fsSL "https://astral.sh/uv/${UV_VERSION}/install.sh" \
	| env UV_INSTALL_DIR=/usr/local/bin sh \
	&& uv python install "${PYTHON_VERSION}" \
	&& uv tool install --python "${PYTHON_VERSION}" "yamllint==${YAMLLINT_VERSION}" \
	&& uv tool install --python "${PYTHON_VERSION}" "ruff==${RUFF_VERSION}"

COPY wheels /tmp/wheels
RUN uv tool install --python "${PYTHON_VERSION}" /tmp/wheels/shipit-*.whl \
	&& rm -rf /tmp/wheels

# The mounted checkout is host-owned; git must still resolve it as a repo.
# Scoped to the fixed mount point, not '*'.
RUN git config --system --add safe.directory /work

ENV PATH="/opt/cargo/bin:/opt/node/bin:${PATH}"
WORKDIR /work
