#!/usr/bin/env bash
# archive.ubuntu.com round-robins over many addresses, and some of them accept the
# connection then never answer. apt keeps the address it resolved for the whole
# run, so one dead node fails the build no matter how many retries are allowed.
# Probe the published addresses and pin the ones that actually serve, keeping
# every working node for redundancy. Addresses are discovered, never hardcoded.
pin_mirror() {
  local root=$1 host=${2:-archive.ubuntu.com} ip good=()
  for ip in $(getent ahostsv4 "$host" | awk '{print $1}' | sort -u); do
    if curl -4 -sS --max-time 8 -o /dev/null --resolve "$host:443:$ip" "https://$host/ubuntu/dists/noble/InRelease" 2>/dev/null; then
      good+=("$ip")
    fi
  done
  if [[ ${#good[@]} -eq 0 ]]; then echo "pin_mirror: no reachable address for $host" >&2; return 1; fi
  sed -i "/[[:space:]]${host}\$/d" "$root/etc/hosts"
  for ip in "${good[@]}"; do printf '%s %s\n' "$ip" "$host" >> "$root/etc/hosts"; done
  echo "pin_mirror: $host -> ${good[*]}"
}
# Always undo before the tree is packaged: the product must resolve normally.
unpin_mirror() {
  local root=$1 host=${2:-archive.ubuntu.com}
  sed -i "/[[:space:]]${host}\$/d" "$root/etc/hosts"
}
