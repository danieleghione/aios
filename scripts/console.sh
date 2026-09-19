#!/usr/bin/env bash
set -u
for attempt in $(seq 1 60); do
  if curl -fsS http://127.0.0.1:8081/health >/dev/null; then break; fi
  sleep 2
done
IP=$(hostname -I | awk '{print $1}')
{
  if curl -fsS http://127.0.0.1:8081/health >/dev/null; then echo 'AIOS READY'; else echo 'AIOS DEGRADED: consult journalctl -u aios-control-plane'; fi
  echo "Hostname: $(hostname)"
  echo "IP: ${IP:-DHCP pending}"
  echo "User portal: https://${IP:-aios}/"
  echo "Admin portal: https://${IP:-aios}/admin/"
  if [[ -f /etc/aios/secrets/bootstrap ]]; then
    echo "Admin bootstrap secret (valid 24h): $(cat /etc/aios/secrets/bootstrap)"
  fi
  echo 'Open WebUI: sign in with your AIOS account; users are managed in Admin.'
  echo 'Local console: operations protected by an AIOS user and password.'
} > /run/aios-console-message
cat /run/aios-console-message
cat /run/aios-console-message > /dev/tty1 || true
