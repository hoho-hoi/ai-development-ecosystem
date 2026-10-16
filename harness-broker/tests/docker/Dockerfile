# Image for tests/test_docker.py: the fake ACP agent behind the real
# entrypoint and bridge. Build from the repository root:
#   docker build -f tests/docker/Dockerfile -t hb-test-agent .
#   HB_TEST_DOCKER_IMAGE=$(docker inspect --format '{{.Id}}' hb-test-agent) \
#     python3 -m unittest tests/test_docker.py
FROM python:3.13-slim
RUN useradd --create-home --uid 10001 --shell /bin/sh agent
COPY src/hbroker/bridge.py /opt/hb/hb_bridge.py
COPY images/common/entrypoint.sh /opt/hb/entrypoint.sh
COPY tests/fake_acp_agent.py /opt/hb/fake_acp_agent.py
RUN chmod 0755 /opt/hb/entrypoint.sh
WORKDIR /workspace
ENTRYPOINT ["/opt/hb/entrypoint.sh"]
CMD ["python3", "/opt/hb/fake_acp_agent.py"]
