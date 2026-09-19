#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")/.."
if [[ $(df --output=avail -B1 . | tail -1) -lt 7500000000 ]]; then
  echo 'Installer test needs at least 7.5 GB free for its new virtual target disk.'
  exit 1
fi
exec sudo .venv/bin/python integration-tests/installer_test.py
