#!/bin/sh
# Session container entrypoint.
# The broker copies the session bundle in as /hb-bundle.tar before starting
# the container. Unpack it (→ /workspace and /home/agent/.hb), give it to the
# unprivileged agent user, then run the ACP adapter as that user. stdout is
# the ACP connection, so nothing else may be printed there.
set -eu

if [ -f /hb-bundle.tar ]; then
    tar -xf /hb-bundle.tar -C / >&2
    rm -f /hb-bundle.tar
    chown -R agent:agent /workspace /home/agent
fi

exec setpriv --reuid=agent --regid=agent --init-groups env HOME=/home/agent USER=agent "$@"
