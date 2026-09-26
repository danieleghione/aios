#!/usr/bin/env bash
# Boot the lab appliance installed by install.py. HTTPS on 127.0.0.1:28443.
set -Eeuo pipefail
LAB=${AIOS_LAB_DIR:-$(cd "$(dirname "$0")/../.." && pwd)/build/lab}
[[ -f $LAB/aios.qcow2 ]] || { echo 'Lab disk missing: run install.py first'; exit 1; }
for n in c.sock m.sock; do rm -f "$LAB/$n"; done
: > "$LAB/console.log"
exec qemu-system-x86_64 -enable-kvm -cpu host -machine q35 -m "${LAB_RAM:-4096}" -smp 2 \
  -drive if=pflash,format=raw,readonly=on,file=/usr/share/OVMF/OVMF_CODE_4M.fd \
  -drive "if=pflash,format=raw,file=$LAB/vars.fd" \
  -device virtio-scsi-pci,id=scsi0 -drive "file=$LAB/aios.qcow2,format=qcow2,if=none,id=d" \
  -device scsi-hd,drive=d,bus=scsi0.0 \
  -netdev user,id=n1,hostfwd=tcp:127.0.0.1:28443-:443,hostfwd=tcp:127.0.0.1:28022-:22 -device virtio-net-pci,netdev=n1 \
  -display none -chardev "socket,id=s0,path=$LAB/c.sock,server=on,wait=off,logfile=$LAB/console.log" \
  -serial chardev:s0 -monitor "unix:$LAB/m.sock,server=on,wait=off"
