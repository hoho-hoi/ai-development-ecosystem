# Session image for OpenCode V2, which speaks ACP itself (`opencode acp`).
#
# Build from the repository root:
#   docker build -f images/opencode/Dockerfile -t agora/opencode .
#
# Pin BASE_IMAGE and the CLI version: the version fixes the harness.
ARG BASE_IMAGE=node:22-bookworm-slim
FROM ${BASE_IMAGE}

ARG OPENCODE_VERSION=2.3.0

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates python3 util-linux \
 && rm -rf /var/lib/apt/lists/* \
 && npm install -g "@opencode/cli@${OPENCODE_VERSION}" \
 && npm cache clean --force \
 && useradd --create-home --uid 10001 --shell /bin/sh agent

COPY images/common/entrypoint.sh /opt/agora/entrypoint.sh
RUN chmod 0755 /opt/agora/entrypoint.sh

WORKDIR /workspace
ENTRYPOINT ["/opt/agora/entrypoint.sh"]
CMD ["opencode", "acp"]
