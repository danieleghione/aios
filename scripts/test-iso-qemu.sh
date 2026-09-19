#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")/.."
[[ -f dist/aios-installer-x86_64.iso ]] || { echo 'Run make iso first'; exit 1; }
exec sudo .venv/bin/python integration-tests/iso_test.py
