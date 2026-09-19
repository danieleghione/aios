#!/usr/bin/env bash
# Guided installation environment. The wizard starts on its own; the menu only
# offers installing again, reboot and power off. There is no shell.
set -u
trap '' QUIT TSTP
# Ctrl-C stops the running step and returns here, never ending the menu.
trap 'echo' INT
banner() {
  clear
  echo 'AIOS INSTALLER ISO READY'
  echo 'Guided AIOS installation.'
  echo
}
# Boot messages would otherwise scroll over the first screen and leave the
# console looking stuck, so say what is happening and draw once units settle.
echo 'AIOS — preparing the installation environment, please wait...'
timeout 60 systemctl is-system-running --wait >/dev/null 2>&1 || true
GUIDED=1
while true; do
  if [[ $GUIDED == 1 ]]; then
    GUIDED=0
    banner
    echo 'You will be asked for: target disk, language, keyboard, hostname, network, time zone, clock and SSH access.'
    echo 'To stop and open the menu, press Ctrl-C.'
    echo
    /opt/aios/app/installer/install.sh || echo 'Installation cancelled or failed.'
    read -r -p 'Press Enter to open the menu' _ || true
    continue
  fi
  banner
  echo '1) Install AIOS'
  echo '2) Reboot'
  echo '3) Power off'
  read -r -p 'Selection: ' choice || { sleep 1; continue; }
  case "$choice" in
    1) /opt/aios/app/installer/install.sh || echo 'Installation cancelled or failed.'; read -r -p 'Press Enter to return to the menu' _ || true ;;
    2) systemctl reboot ;;
    3) systemctl poweroff ;;
    *) echo 'Invalid choice.'; sleep 1 ;;
  esac
done
