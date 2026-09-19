#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")/.."
python3 -m venv .venv
.venv/bin/pip install --upgrade pip==26.2
.venv/bin/pip install -e '.[test]'
npm --prefix frontend-admin ci
npm --prefix frontend-admin run build
