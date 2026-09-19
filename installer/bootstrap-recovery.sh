#!/usr/bin/env bash
# Privileged half of the SSH recovery path, reachable through one sudoers rule.
# Shows or regenerates the one-time admin bootstrap secret; the Python command
# refuses both once an administrator account exists.
set -Eeuo pipefail
AIOS=/opt/aios/venv/bin/python
case "${1:-show}" in
  show) exec runuser -u aios -- "$AIOS" -m aios bootstrap-show ;;
  reset) exec runuser -u aios -- "$AIOS" -m aios bootstrap-reset ;;
  # Credentials arrive on stdin; the key is printed only after they are verified.
  apikey) exec runuser -u aios -- "$AIOS" -m aios api-key --channel ssh ;;
  *) echo 'Usage: aios-bootstrap-recovery [show|reset|apikey]' >&2; exit 2 ;;
esac
