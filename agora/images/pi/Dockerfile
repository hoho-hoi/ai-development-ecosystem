# Session image for pi, spoken to over ACP through pi-acp (which runs
# `pi --mode rpc`).
#
# Build from the repository root:
#   docker build -f images/pi/Dockerfile -t agora/pi .
#
# PI_VERSION is the module's harness version. PI_ACP_VERSION is pinned here:
# the image (whose ID every session records) fixes both.
ARG BASE_IMAGE=node:22-bookworm-slim
FROM ${BASE_IMAGE}

ARG PI_VERSION=1.5.2
ARG PI_ACP_VERSION=0.0.34

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates python3 util-linux \
 && rm -rf /var/lib/apt/lists/* \
 && npm install -g "@earendil-works/pi-coding-agent@${PI_VERSION}" "pi-acp@${PI_ACP_VERSION}" \
 && npm cache clean --force \
 && useradd --create-home --uid 10001 --shell /bin/sh agent

COPY src/agora/bridge.py /opt/agora/agora_bridge.py
COPY images/common/entrypoint.sh /opt/agora/entrypoint.sh
RUN chmod 0755 /opt/agora/entrypoint.sh /opt/agora/agora_bridge.py

WORKDIR /workspace
ENTRYPOINT ["/opt/agora/entrypoint.sh"]
CMD ["pi-acp"]
