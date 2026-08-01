# Linux scip-clang environment for Windows hosts (and optional CI).
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ARG SCIP_CLANG_VERSION=v0.4.0

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates \
      cmake \
      curl \
      g++ \
      make \
      python3 \
 && curl -fsSL \
      "https://github.com/sourcegraph/scip-clang/releases/download/${SCIP_CLANG_VERSION}/scip-clang-x86_64-linux" \
      -o /usr/local/bin/scip-clang \
 && chmod +x /usr/local/bin/scip-clang \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /work
