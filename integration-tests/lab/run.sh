#!/usr/bin/env bash
# Install an AIOS ISO on a fresh lab VM and run every check against it.
#
#   integration-tests/lab/run.sh dist/aios-installer-<version>-x86_64.iso
#
# Needs root for QEMU/KVM (sudo), Node with the portal's Playwright, and the
# project virtualenv. Results go to stdout; a check that fails prints [BAD].
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
export AIOS_LAB_DIR=${AIOS_LAB_DIR:-$REPO/build/lab}
PY=${PY:-$REPO/.venv/bin/python}
ISO=$(realpath "${1:?usage: run.sh <installer.iso>}")
URL=${AIOS_LAB_URL:-https://127.0.0.1:28443}
mkdir -p "$AIOS_LAB_DIR/work"
cd "$HERE"
[[ -f "$ISO.sha256" ]] && (cd "$(dirname "$ISO")" && sha256sum -c "$(basename "$ISO").sha256") || true
sudo -n pkill -f "[q]emu-system-x86_64.*$AIOS_LAB_DIR/aios.qcow2"; sleep 3
sudo -n env AIOS_LAB_DIR="$AIOS_LAB_DIR" python3 "$HERE/install.py" "$ISO" || { echo 'INSTALL FAILED'; exit 1; }
echo "installed $(date +%H:%M)"
sudo -n env AIOS_LAB_DIR="$AIOS_LAB_DIR" LAB_RAM="${LAB_RAM:-4096}" nohup "$HERE/boot.sh" > "$AIOS_LAB_DIR/boot.log" 2>&1 &
sleep 60
for _ in $(seq 1 120); do curl -skf "$URL/api/v1/aios/auth/status" >/dev/null && break; sleep 5; done
echo "portal up $(date +%H:%M)"
sleep 120  # the chat finishes its first start in the background
$PY portal_api.py bootstrap
node experience.mjs pre
$PY portal_api.py
$PY experience_api.py
node portal.mjs
node experience.mjs
node documents.mjs
$PY voice.py
$PY operations_api.py
node phase_b.mjs
$PY phase_b_api.py
$PY phase_c_api.py
$PY updates.py
$PY publish.py
node chat_image.mjs
node chat_settings.mjs
# The voice model needs more memory than the rest of the suite gives the VM:
# power it off from the portal and boot the same disk with more RAM.
$PY - <<'SHUTDOWN'
import httpx
from lab import ADMIN_PASSWORD, ADMIN_USER, BASE
c = httpx.Client(base_url=BASE, verify=False, timeout=60)
r = c.post('/api/v1/aios/auth/login', json={'username': ADMIN_USER, 'password': ADMIN_PASSWORD})
print('shutdown', c.post('/api/v1/aios/system/power/shutdown', headers={'x-csrf-token': r.json()['csrf']}).status_code)
SHUTDOWN
for _ in $(seq 1 60); do pgrep -f "[q]emu-system-x86_64.*$AIOS_LAB_DIR/aios.qcow2" >/dev/null || break; sleep 5; done
sudo -n env AIOS_LAB_DIR="$AIOS_LAB_DIR" LAB_RAM="${LAB_VOICE_RAM:-8192}" nohup "$HERE/boot.sh" > "$AIOS_LAB_DIR/boot-voice.log" 2>&1 &
sleep 60
for _ in $(seq 1 120); do curl -skf "$URL/api/v1/aios/auth/status" >/dev/null && break; sleep 5; done
sleep 120
echo "voice stage $(date +%H:%M)"
$PY phase_d_api.py
node phase_d.mjs
echo "ALL DONE $(date +%H:%M)"
