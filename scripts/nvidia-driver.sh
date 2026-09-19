#!/usr/bin/env bash
# Install the NVIDIA kernel modules that match the cards in this machine.
#
# NVIDIA ships two module flavours and neither covers every card: the open
# modules are required from the RTX 50 series on and support Turing (RTX 20 /
# GTX 16) and newer, while Pascal and Maxwell cards (GTX 10 / 900, Tesla P40,
# P100) need the proprietary ones. The image carries both as packages; this runs
# at boot as root and installs the right one, offline, for the running kernel.
# Machines without an NVIDIA GPU exit immediately and never load either.
set -Eeuo pipefail
BRANCH=580-server
# Metapackages that make system updates bring the modules for each new kernel.
META=generic-hwe-24.04
BUNDLE=/opt/aios/drivers/nvidia
STATE=/var/lib/aios/system/nvidia-driver.json
KERNEL=$(uname -r)

record() {
  mkdir -p "$(dirname "$STATE")"
  printf '{"flavour": "%s", "kernel": "%s", "status": "%s", "detail": "%s", "checked_at": %s}\n' \
    "$1" "$KERNEL" "$2" "${3//\"/}" "$(date +%s)" > "$STATE"
  chmod 644 "$STATE"
}

ids=()
for dev in "${AIOS_PCI_DEVICES:-/sys/bus/pci/devices}"/*; do
  [[ $(cat "$dev/vendor" 2>/dev/null) == 0x10de ]] || continue
  [[ $(cat "$dev/class" 2>/dev/null) == 0x03* ]] || continue
  ids+=("$(cat "$dev/device")")
done
if [[ ${#ids[@]} == 0 ]]; then
  [[ ${1:-} == --decide ]] && { echo none; exit 0; }
  rm -f "$STATE"
  exit 0
fi

# Turing and later start at device ID 0x1e00. One older card among newer ones
# decides for the proprietary modules, which drive both generations.
flavour=open
for id in "${ids[@]}"; do
  if (( id < 0x1e00 )); then flavour=closed; fi
done
# --decide prints the choice and changes nothing, for tests and diagnostics.
if [[ ${1:-} == --decide ]]; then
  echo "$flavour"
  exit 0
fi
mkdir -p /var/log/aios
suffix=$([[ $flavour == open ]] && echo "-open" || echo "")
wanted="linux-modules-nvidia-$BRANCH$suffix-$KERNEL"
other="linux-modules-nvidia-$BRANCH$([[ $flavour == open ]] && echo "" || echo "-open")-$KERNEL"

if ! dpkg-query -W -f='${Status}' "$wanted" 2>/dev/null | grep -q 'install ok installed'; then
  export DEBIAN_FRONTEND=noninteractive
  # The other flavour carries its own nvidia.ko: remove it first so the two never compete.
  if dpkg-query -W -f='${Status}' "$other" 2>/dev/null | grep -q 'install ok installed'; then
    dpkg --purge "${other%-$KERNEL}-$META" "$other" 2>/dev/null || true
  fi
  debs=("$BUNDLE/$flavour/"*"-${KERNEL}_"*.deb "$BUNDLE/$flavour/linux-modules-nvidia-$BRANCH$suffix-${META}_"*.deb)
  if compgen -G "$BUNDLE/$flavour/*-${KERNEL}_*.deb" >/dev/null; then
    if ! dpkg -i "${debs[@]}" >/var/log/aios/nvidia-driver.log 2>&1; then
      record "$flavour" failed "offline install of the bundled modules failed; see /var/log/aios/nvidia-driver.log"
      exit 0
    fi
  elif ! apt-get install -y --no-install-recommends "$wanted" "linux-modules-nvidia-$BRANCH$suffix-$META" >/var/log/aios/nvidia-driver.log 2>&1; then
    # A kernel installed by a later update has no bundled modules: they come from
    # the archive, which needs network access.
    record "$flavour" failed "no modules bundled for kernel $KERNEL and the Ubuntu archive is unreachable"
    exit 0
  fi
  depmod -a "$KERNEL"
fi

if modprobe nvidia 2>>/var/log/aios/nvidia-driver.log; then
  modprobe nvidia-uvm 2>/dev/null || true
  # The driver's own udev rules create /dev/nvidia*; replay the PCI events they react to.
  udevadm trigger --subsystem-match=pci --action=add >/dev/null 2>&1 || true
  udevadm settle || true
  record "$flavour" loaded "${#ids[@]} NVIDIA GPU(s)"
elif modprobe nouveau 2>>/var/log/aios/nvidia-driver.log; then
  # Cards this driver branch no longer supports (Kepler and older) refuse the
  # nvidia module. The packages blacklist nouveau only for automatic loading:
  # loaded explicitly it gives Mesa's NVK Vulkan driver a chance; if NVK cannot
  # drive the card either, llama.cpp finds no GPU and runs on the CPU.
  record "$flavour" nouveau "the nvidia module did not load, using nouveau; see /var/log/aios/nvidia-driver.log"
else
  record "$flavour" failed "the nvidia module did not load; see /var/log/aios/nvidia-driver.log"
fi
