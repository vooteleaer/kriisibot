#!/bin/sh
# Started before the Reticulum shared instance listens, Python RNS would claim the shared-instance
# role itself with no interfaces, so wait for it first. Kriisibot still runs on MeshCore alone if
# it never comes up. Set RNS_SHARED_INSTANCE_WAIT=0 to skip (e.g. Reticulum disabled).
set -eu
if [ "${RNS_SHARED_INSTANCE_WAIT:-120}" != "0" ]; then
    python3 - <<'PY'
import os, socket, time
port = int(os.environ.get("RNS_SHARED_INSTANCE_PORT", "37428"))
for _ in range(int(os.environ.get("RNS_SHARED_INSTANCE_WAIT", "120"))):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=2).close()
        break
    except OSError:
        time.sleep(1)
else:
    print(f"Reticulum shared instance on 127.0.0.1:{port} did not come up, starting anyway")
PY
fi
exec "$@"
