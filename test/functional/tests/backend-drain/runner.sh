#!/bin/bash
set -euo pipefail
ulimit -c 0

cd /tests/backend-drain

odyssey /tests/backend-drain/odyssey.conf
trap 'ody-stop || exit 1' EXIT
sleep 1

python3 check_backend_drain.py -v

grep -q "backend closed connection .* ms after Terminate" /var/log/odyssey.log
