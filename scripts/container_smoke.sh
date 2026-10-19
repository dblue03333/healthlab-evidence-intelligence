#!/bin/sh
# Exercise the installed package offline and persist a run across containers.
set -eu
image=${1:-healthlab-signal:local}
volume="healthlab-smoke-$(date +%s)-$$"
docker volume create "$volume" >/dev/null
trap 'docker volume rm "$volume" >/dev/null' EXIT

docker run --rm --network none --read-only --tmpfs /tmp "$image" --help >/dev/null
docker run --rm --network none --read-only --tmpfs /tmp \
  --mount "type=volume,src=$volume,dst=/data" "$image" check-config
run_id=$(docker run --rm --network none --read-only --tmpfs /tmp \
  --mount "type=volume,src=$volume,dst=/data" --entrypoint python "$image" -c '
import os
from pathlib import Path
from importlib.resources import files
from healthlab.store import RunStore
assert os.getuid() == 10001
assert files("healthlab.signal").joinpath("html_profiles.json").is_file()
assert not Path("/app/.env").exists()
assert not Path("/app/outputs").exists()
s = RunStore(Path("/data/signal"))
r = s.new("container_smoke", {})
r["snapshot"] = s.put(b"offline-persistence-smoke")
s.save(r)
print(r["run_id"])
')
docker run --rm --network none --read-only --tmpfs /tmp \
  --mount "type=volume,src=$volume,dst=/data" --entrypoint python "$image" -c '
import sys
from pathlib import Path
from healthlab.store import RunStore
s = RunStore(Path("/data/signal"))
r = s.load(sys.argv[1])
assert s.read(r["snapshot"]) == b"offline-persistence-smoke"
print("Installed package and persistence across containers: passed")
' "$run_id"
