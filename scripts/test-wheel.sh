#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")/.."
WORK=$(mktemp -d "$PWD/build/wheel-test.XXXXXX")
trap 'rm -rf "$WORK"' EXIT
.venv/bin/pip wheel --no-deps . -w "$WORK" >/dev/null
.venv/bin/python - "$WORK" <<'PY'
import pathlib,sys,zipfile
wheel=next(pathlib.Path(sys.argv[1]).glob('*.whl'))
with zipfile.ZipFile(wheel) as archive:
    assert 'aios/schema.sql' in archive.namelist(), 'Database migration missing from installable wheel'
    archive.extractall(pathlib.Path(sys.argv[1])/'installed')
PY
AIOS_DATA="$WORK/data" AIOS_ETC="$WORK/etc" PYTHONPATH="$WORK/installed" .venv/bin/python -m aios init
