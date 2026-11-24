# Optional: a prebuilt bundle image for Loop. Not needed if k8s.harness_image is left
# empty; then an init container downloads the same release at pod start.
# Build from the repo root:
#   docker build --platform linux/amd64 -f src/swebench_run/bundles/loop/Dockerfile -t <registry>/loop-bundle:0.7.0 .
# Loop's release is glibc-only. The tarball holds a directory with the binary inside.
FROM debian:bookworm-slim

ARG LOOP_VERSION=v0.7.0
ARG LOOP_TARGET=x86_64-unknown-linux-gnu

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /opt/harness/bin \
 && curl -fsSL "https://github.com/soketlabs/loop/releases/download/${LOOP_VERSION}/loop-${LOOP_TARGET}.tar.gz" -o /tmp/loop.tgz \
 && curl -fsSL "https://github.com/soketlabs/loop/releases/download/${LOOP_VERSION}/loop-${LOOP_TARGET}.tar.gz.sha256" -o /tmp/loop.sha256 \
 && echo "$(cut -d' ' -f1 /tmp/loop.sha256)  /tmp/loop.tgz" | sha256sum -c - \
 && tar -xzf /tmp/loop.tgz --strip-components=1 -C /opt/harness/bin \
 && chmod +x /opt/harness/bin/loop \
 && rm /tmp/loop.tgz /tmp/loop.sha256

COPY src/swebench_run/bundles/common/entrypoint.sh /opt/harness/entrypoint.sh
COPY src/swebench_run/bundles/loop/run-agent.sh /opt/harness/run-agent.sh
RUN chmod +x /opt/harness/entrypoint.sh /opt/harness/run-agent.sh
