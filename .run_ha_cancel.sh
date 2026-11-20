#!/bin/sh
# Local copy of the cached run_ha.sh for this worktree; not committed.
# usage: .run_ha_cancel.sh <pattern> [unittest -k args...]
cd /home/ultra/dev/ha-tinyprint/.worktrees/studio-cancel-final
pat="$1"; shift
docker run --rm --cpus 2 --memory 2g --entrypoint /bin/sh -v "$PWD:/workspace" -w /workspace \
  ghcr.io/home-assistant/home-assistant:2026.9.3 -c "python -m pip install -q --target /tmp/test-deps --no-deps qrcode==8.2 >/dev/null 2>&1; export PYTHONPATH=/workspace:/tmp/test-deps PYTHONDONTWRITEBYTECODE=1; python -m unittest discover -s tests -p '$pat' -v $*"
