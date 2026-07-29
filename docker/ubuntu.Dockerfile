# Stock baseline: the bootstrap must provide pixi, uv, and language toolchains.
FROM ubuntu:24.04

RUN apt-get update \
	&& apt-get install -y --no-install-recommends \
	bash \
	ca-certificates \
	curl \
	git \
	tar \
	&& rm -rf /var/lib/apt/lists/*

USER ubuntu
WORKDIR /home/ubuntu
