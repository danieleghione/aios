#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")/.."
[[ -f dist/aios-x86_64.img ]] || { echo 'Run make image first'; exit 1; }
[[ -x .venv/bin/python ]] || ./scripts/build.sh
if [[ $EUID != 0 && -e /dev/kvm && ! -w /dev/kvm ]]; then
  exec sudo env AIOS_ADMIN_TEST="${AIOS_ADMIN_TEST:-0}" AIOS_BROWSER_TEST="${AIOS_BROWSER_TEST:-0}" AIOS_MODEL_SMOKE="${AIOS_MODEL_SMOKE:-1}" AIOS_TEST_RAM="${AIOS_TEST_RAM:-3072}" AIOS_TEST_PORT="${AIOS_TEST_PORT:-18443}" .venv/bin/python integration-tests/qemu_test.py
fi
exec .venv/bin/python integration-tests/qemu_test.py
