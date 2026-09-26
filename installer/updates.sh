#!/usr/bin/env bash
# Operating system updates for the appliance console: "check" records what the
# Ubuntu archive offers, "apply" installs it. Only root runs this (console and
# timer); the result is a small status file the console reads.
set -Eeuo pipefail
STATUS=${AIOS_UPDATES_STATUS:-/var/lib/aios/system/updates.json}
APT=(-o Acquire::Retries=3 -o Acquire::http::Timeout=20 -o Acquire::https::Timeout=20)
export DEBIAN_FRONTEND=noninteractive
mkdir -p "$(dirname "$STATUS")"
record() {
  local packages=$1 security=$2 error=${3:-}
  local reboot=false
  [[ -f /run/reboot-required ]] && reboot=true
  printf '{"checked_at": %s, "packages": %s, "security": %s, "reboot_required": %s, "error": "%s"}\n' \
    "$(date +%s)" "$packages" "$security" "$reboot" "${error//\"/}" > "$STATUS.tmp"
  chmod 644 "$STATUS.tmp"
  mv "$STATUS.tmp" "$STATUS"
}
check() {
  if ! timeout 600 apt-get "${APT[@]}" -qq update >/dev/null 2>&1; then
    record 0 0 'Ubuntu archive unreachable: check network, DNS and proxy'
    return 1
  fi
  local plan packages security
  plan=$(apt-get -s --with-new-pkgs upgrade 2>/dev/null | grep '^Inst ' || true)
  packages=$(grep -c . <<<"$plan" || true)
  security=$(grep -c -- '-security' <<<"$plan" || true)
  record "${packages:-0}" "${security:-0}"
}
case "${1:-}" in
  check)
    check ;;
  apply)
    check || { echo 'Could not reach the update archive.'; exit 1; }
    echo 'Installing updates (do not power off the system)...'
    # New dependencies (such as a new kernel) are installed, nothing is removed,
    # and configuration files the appliance manages are kept as they are.
    # The archive is served by several mirrors that are not always in step: a
    # package the index just listed can answer 404 for a few minutes. What was
    # downloaded stays in the cache; read the index again and try once more.
    for attempt in 1 2 3; do
      if apt-get "${APT[@]}" -y --with-new-pkgs -o Dpkg::Options::=--force-confold -o Dpkg::Options::=--force-confdef upgrade; then
        break
      fi
      if (( attempt == 3 )); then
        check || true
        echo 'Updates could not be installed; try again later.'
        exit 1
      fi
      echo "The archive did not serve every package; trying again in a minute (attempt $((attempt + 1)) of 3)..."
      sleep "${AIOS_UPDATES_RETRY_WAIT:-60}"
      timeout 600 apt-get "${APT[@]}" -qq update >/dev/null 2>&1 || true
    done
    apt-get clean
    check || true
    if [[ -f /run/reboot-required ]]; then
      echo 'Updates installed. A reboot is required to finish.'
    else
      echo 'Updates installed.'
    fi ;;
  *)
    echo 'Usage: updates.sh check|apply' >&2; exit 2 ;;
esac
