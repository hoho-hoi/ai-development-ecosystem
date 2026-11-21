# Session image for pi, driven by agora's own adapter (agora-pi.mjs), which
# speaks ACP to agora and runs conversations with pi's SDK.
#
# Build from the repository root:
#   docker build -f images/pi/Dockerfile -t agora/pi .
#
# PI_VERSION is the module's harness version; the image (whose ID every
# session records) fixes it together with the adapter.
ARG BASE_IMAGE=node:22-bookworm-slim
FROM ${BASE_IMAGE}

ARG PI_VERSION=1.5.2

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates python3 util-linux \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /opt/agora-pi \
 && npm install --prefix /opt/agora-pi --no-fund --no-audit "@earendil-works/pi-coding-agent@${PI_VERSION}" \
 && npm cache clean --force \
 && useradd --create-home --uid 10001 --shell /bin/sh agent

COPY images/pi/agora-pi.mjs /opt/agora-pi/agora-pi.mjs
COPY images/common/entrypoint.sh /opt/agora/entrypoint.sh
RUN chmod 0755 /opt/agora/entrypoint.sh \
 && printf '#!/bin/sh\nexec node /opt/agora-pi/agora-pi.mjs "$@"\n' > /usr/local/bin/agora-pi \
 && chmod 0755 /usr/local/bin/agora-pi

WORKDIR /workspace
ENTRYPOINT ["/opt/agora/entrypoint.sh"]
CMD ["agora-pi"]
