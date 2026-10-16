# Session image for Claude Code via the claude-agent-acp adapter.
#
# Build from the repository root:
#   docker build -f images/claude-code/Dockerfile -t harness-broker/claude-code .
# then pin the profile to it:
#   local image:    runtime.image: sha256:<id>   (docker inspect --format '{{.Id}}' harness-broker/claude-code)
#   from a registry: runtime.image: <name>@sha256:<digest>   (the RepoDigest after pushing)
#
# Pin BASE_IMAGE and the adapter version; the adapter bundles Claude Code
# through the Claude Agent SDK, so its version fixes the harness version.
ARG BASE_IMAGE=node:22-bookworm-slim
FROM ${BASE_IMAGE}

ARG CLAUDE_AGENT_ACP_VERSION=0.75.1

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates python3 util-linux \
 && rm -rf /var/lib/apt/lists/* \
 && npm install -g "@agentclientprotocol/claude-agent-acp@${CLAUDE_AGENT_ACP_VERSION}" \
 && npm cache clean --force \
 && useradd --create-home --uid 10001 --shell /bin/sh agent

COPY src/hbroker/bridge.py /opt/hb/hb_bridge.py
COPY images/common/entrypoint.sh /opt/hb/entrypoint.sh
RUN chmod 0755 /opt/hb/entrypoint.sh /opt/hb/hb_bridge.py

ENV DISABLE_AUTOUPDATER=1
WORKDIR /workspace
ENTRYPOINT ["/opt/hb/entrypoint.sh"]
CMD ["claude-agent-acp"]
