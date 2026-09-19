#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")/.."
[[ -x .venv/bin/pytest && -d frontend-admin/node_modules ]] || ./scripts/build.sh
.venv/bin/ruff check backend tests
.venv/bin/mypy --follow-imports=silent --ignore-missing-imports backend/aios
.venv/bin/pytest -q tests
./scripts/test-wheel.sh
npm --prefix frontend-admin run test
npm --prefix frontend-admin run build
