#!/usr/bin/env bash
# Login shell of the SSH recovery account. It is a shell only in name: it takes
# no command from the client, so "ssh host <command>" cannot run anything else.
set -u
sudo -n /usr/local/sbin/aios-bootstrap-recovery show || true
if [[ ! -t 0 ]]; then exit 0; fi
api_key() {
  local account password saved
  # Silence the terminal before anything else: input pasted early is echoed on
  # arrival, which would print the password in the SSH session.
  saved=$(stty -g 2>/dev/null || true)
  stty -echo 2>/dev/null || true
  printf 'AIOS user (input hidden): '
  read -r account || { [[ -n $saved ]] && stty "$saved"; echo; return; }
  printf '%s\n' "$account"
  printf 'Password: '
  read -r password || { [[ -n $saved ]] && stty "$saved"; echo; return; }
  [[ -n $saved ]] && stty "$saved" 2>/dev/null || stty echo 2>/dev/null || true
  printf '\n'
  printf '%s\n%s\n' "$account" "$password" | sudo -n /usr/local/sbin/aios-bootstrap-recovery apikey || true
}
while true; do
  echo
  echo 'AIOS — recovery access'
  echo '1) Show the bootstrap code (only while no administrator exists)'
  echo '2) Regenerate the bootstrap code (invalidates the previous one, valid 24 hours)'
  echo '3) Show the API key for external tools (needs an AIOS user and password)'
  echo '4) Quit'
  read -r -p 'Selection: ' choice || exit 0
  case "$choice" in
    1) sudo -n /usr/local/sbin/aios-bootstrap-recovery show || true ;;
    2) sudo -n /usr/local/sbin/aios-bootstrap-recovery reset || true ;;
    3) api_key ;;
    4) exit 0 ;;
    *) echo 'Invalid choice.' ;;
  esac
done
