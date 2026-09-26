#!/usr/bin/env bash
# The files a GitHub release carries: the installer ISO in parts of 600 MB
# (release assets have a size limit) and the checksum of the whole ISO, as the
# installation instructions expect them.
#
#   scripts/release-parts.sh            # dist/aios-installer-<VERSION>-x86_64.iso
set -Eeuo pipefail
cd "$(dirname "$0")/.."
iso="aios-installer-$(cat VERSION)-x86_64.iso"
[[ -f dist/$iso ]] || { echo "dist/$iso not found: run make iso first" >&2; exit 1; }
out=dist/release
rm -rf "$out"
mkdir -p "$out"
split --bytes=600M --numeric-suffixes=1 --suffix-length=2 "dist/$iso" "$out/$iso.part-"
(cd dist && sha256sum "$iso") > "$out/$iso.sha256"
# Joined back, the parts are the ISO the checksum names.
(cd "$out" && cat "$iso".part-* | sha256sum | cut -d' ' -f1) | grep -qx "$(cut -d' ' -f1 "$out/$iso.sha256")"
ls -l "$out"
