#!/usr/bin/env bash
# Physical console. It offers a fixed set of operations and never a shell: every
# action is authorised against the AIOS administrator database first.
set -Eeuo pipefail
# Ctrl-C, Ctrl-\ and Ctrl-Z must not end the menu and leave anything behind it.
trap '' INT QUIT TSTP
AIOS=(runuser -u aios -- /opt/aios/venv/bin/python -m aios)
UPDATES=/var/lib/aios/system/updates.json
no_administrator() { "${AIOS[@]}" console-auth --check --action "$1" >/dev/null 2>&1; }
authorize() {
  local account password saved
  # The terminal echoes input the moment it arrives, so anything pasted while a
  # later command is still running would be printed before a read could hide it.
  # Silence the terminal first, before doing anything slow.
  saved=$(stty -g 2>/dev/null || true)
  stty -echo 2>/dev/null || true
  restore_tty() { [[ -n $saved ]] && stty "$saved" 2>/dev/null || stty echo 2>/dev/null || true; }
  trap restore_tty RETURN
  # Ask for nothing while no administrator exists: there is no password to check
  # yet, and the bootstrap secret is already on this screen.
  if no_administrator "$1"; then restore_tty; return 0; fi
  printf 'AIOS user (input hidden): '
  read -r account || { echo; return 1; }
  printf '%s\n' "$account"
  printf 'Password: '
  read -r password || { echo; return 1; }
  printf '\n'
  restore_tty
  printf '%s\n%s\n' "$account" "$password" | "${AIOS[@]}" console-auth --action "$1"
}
pause() { read -r -p 'Press Enter to continue' _ || true; }
show_api_key() {
  local account password saved
  saved=$(stty -g 2>/dev/null || true)
  stty -echo 2>/dev/null || true
  printf 'AIOS user (input hidden): '
  read -r account || { [[ -n $saved ]] && stty "$saved"; echo; return; }
  printf '%s\n' "$account"
  printf 'Password: '
  read -r password || { [[ -n $saved ]] && stty "$saved"; echo; return; }
  [[ -n $saved ]] && stty "$saved" 2>/dev/null || stty echo 2>/dev/null || true
  printf '\n'
  printf '%s\n%s\n' "$account" "$password" | "${AIOS[@]}" api-key --channel console || true
}
# One line saying whether operating system updates are waiting.
update_status() {
  /opt/aios/venv/bin/python - "$UPDATES" <<'PY' 2>/dev/null || echo 'Updates: not checked yet.'
import json, sys, time
s = json.load(open(sys.argv[1]))
when = time.strftime('%d/%m %H:%M', time.localtime(s['checked_at']))
if s.get('error'):
    print(f"Updates: check failed ({when}): {s['error']}.")
elif s['packages']:
    print(f"*** UPDATES AVAILABLE: {s['packages']} packages, {s['security']} security (checked {when}). Choose 3 to install them. ***")
else:
    print(f'System up to date (checked {when}).')
if s.get('reboot_required'):
    print('*** A reboot is required to finish the installed updates: choose 4. ***')
PY
}
while true; do
  clear
  # The banner is generated once at boot, so the bootstrap line would keep
  # advertising a secret that no longer exists. Read that one live instead.
  if [[ -f /run/aios-console-message ]]; then
    grep -v 'Admin bootstrap secret' /run/aios-console-message
    if [[ -f /etc/aios/secrets/bootstrap ]]; then
      echo "Admin bootstrap secret (valid 24h): $(cat /etc/aios/secrets/bootstrap)"
    else
      echo 'Admin bootstrap secret: already used; an administrator exists.'
    fi
  fi
  echo
  update_status
  echo
  echo 'AIOS local console'
  echo '1) Show network and service status'
  echo '2) Regenerate the bootstrap code (only while no administrator exists)'
  echo '3) Check and install system updates'
  echo '4) Reboot'
  echo '5) Power off'
  echo '6) Show API key for external tools (n8n, OpenAI clients)'
  read -r -p 'Selection: ' selection || { sleep 1; continue; }
  case "$selection" in
    1|2|4|5)
      if ! authorize "$selection"; then echo 'Action denied.'; pause; continue; fi ;;
    3)
      # Installing software is never open, not even before the first administrator.
      if no_administrator updates; then echo 'Create an administrator from the web portal first.'; pause; continue; fi
      if ! authorize updates; then echo 'Action denied.'; pause; continue; fi ;;
    6)
      # Unlike the other entries this one is never open: with no administrator
      # there is no key holder to authenticate, so the command refuses.
      show_api_key; pause; continue ;;
    *) echo 'Invalid choice.'; sleep 1; continue ;;
  esac
  case "$selection" in
    1) ip -br address; systemctl --no-pager --failed; pause ;;
    2) "${AIOS[@]}" bootstrap-reset || true; pause ;;
    3) echo 'Checking for updates...'
       if /opt/aios/app/installer/updates.sh check; then
         update_status
         if [[ $(jq -r '.packages' "$UPDATES" 2>/dev/null || echo 0) -gt 0 ]] &&
            read -r -p 'Install the updates now? yes/no [yes]: ' answer && [[ ${answer:-yes} =~ ^(y|yes|s|si)$ ]]; then
           /opt/aios/app/installer/updates.sh apply || echo 'Installing the updates failed.'
         fi
       else
         update_status
       fi
       pause ;;
    4) systemctl reboot ;;
    5) systemctl poweroff ;;
  esac
done
