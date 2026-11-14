#!/bin/sh
# Session container entrypoint.
# The broker copies the session bundle in as /agora-bundle.tar before starting
# the container. Unpack it (→ /workspace and /home/agent/.agora), give it to the
# unprivileged agent user, then run the ACP adapter as that user. stdout is
# the ACP connection, so nothing else may be printed there.
set -eu

if [ -f /agora-bundle.tar ]; then
    tar -xf /agora-bundle.tar -C / >&2
    rm -f /agora-bundle.tar
    chown -R agent:agent /workspace /home/agent
fi

exec setpriv --reuid=agent --regid=agent --init-groups env HOME=/home/agent USER=agent "$@"
