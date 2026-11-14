# Image for tests/test_docker.py: the fake ACP agent behind the real
# entrypoint and bridge. Build from the repository root:
#   docker build -f tests/docker/Dockerfile -t agora-test-agent .
#   AGORA_TEST_DOCKER_IMAGE=$(docker inspect --format '{{.Id}}' agora-test-agent) \
#     python3 -m unittest tests/test_docker.py
FROM python:3.13-slim
RUN useradd --create-home --uid 10001 --shell /bin/sh agent
COPY src/agora/bridge.py /opt/agora/agora_bridge.py
COPY images/common/entrypoint.sh /opt/agora/entrypoint.sh
COPY tests/fake_acp_agent.py /opt/agora/fake_acp_agent.py
RUN chmod 0755 /opt/agora/entrypoint.sh
WORKDIR /workspace
ENTRYPOINT ["/opt/agora/entrypoint.sh"]
CMD ["python3", "/opt/agora/fake_acp_agent.py"]
