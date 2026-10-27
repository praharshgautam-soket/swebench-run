# Harness bundle for Loop. Build from the repo root:
#   docker build --platform linux/amd64 -f bundles/loop/Dockerfile -t <registry>/loop-bundle:0.5.0 .
# The bundle is copied into each instance pod by an init container, so the
# multi-GB SWE-bench images are never rebuilt. Loop's release is glibc-only.
FROM debian:bookworm-slim

ARG LOOP_VERSION=v0.5.0
ARG LOOP_TARGET=x86_64-unknown-linux-gnu

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /opt/harness/bin \
 && curl -fsSL "https://github.com/soketlabs/loop/releases/download/${LOOP_VERSION}/loop-${LOOP_TARGET}.tar.gz" -o /tmp/loop.tgz \
 && curl -fsSL "https://github.com/soketlabs/loop/releases/download/${LOOP_VERSION}/loop-${LOOP_TARGET}.tar.gz.sha256" -o /tmp/loop.sha256 \
 && echo "$(cut -d' ' -f1 /tmp/loop.sha256)  /tmp/loop.tgz" | sha256sum -c - \
 && tar -xzf /tmp/loop.tgz -C /opt/harness/bin \
 && chmod +x /opt/harness/bin/loop \
 && rm /tmp/loop.tgz /tmp/loop.sha256

COPY bundles/common/entrypoint.sh /opt/harness/entrypoint.sh
COPY bundles/loop/run-agent.sh /opt/harness/run-agent.sh
RUN chmod +x /opt/harness/entrypoint.sh /opt/harness/run-agent.sh
