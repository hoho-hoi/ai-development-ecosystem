# Session image for Codex via the codex-acp adapter.
#
# Build from the repository root:
#   docker build -f images/codex/Dockerfile -t agora/codex .
#
# The adapter ships its own @openai/codex dependency, so its version fixes
# the harness version.
ARG BASE_IMAGE=node:22-bookworm-slim
FROM ${BASE_IMAGE}

ARG CODEX_ACP_VERSION=2.2.2

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates python3 util-linux \
 && rm -rf /var/lib/apt/lists/* \
 && npm install -g "@agentclientprotocol/codex-acp@${CODEX_ACP_VERSION}" \
 && npm cache clean --force \
 && useradd --create-home --uid 10001 --shell /bin/sh agent

COPY src/agora/bridge.py /opt/agora/agora_bridge.py
COPY images/common/entrypoint.sh /opt/agora/entrypoint.sh
RUN chmod 0755 /opt/agora/entrypoint.sh /opt/agora/agora_bridge.py

WORKDIR /workspace
ENTRYPOINT ["/opt/agora/entrypoint.sh"]
CMD ["codex-acp"]
